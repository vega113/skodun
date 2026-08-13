"""Deterministic, additive finding fingerprints and conservative lineage.

The legacy ``finding_key`` remains the triage identity.  This module only
adds a versioned read-model identity to finding projections and never changes
trust, gate, reuse, or triage decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


VERSION = "finding_fingerprint_v2"
ALGORITHM = "canonical-json-sha256-v1"
UNKNOWN = "unknown"
_PASS_MARKER = re.compile(r"^\s*\((security|skeptic|integration|refuter)\)\s*", re.I)
_EXTRA_MARKER = re.compile(r"^\s*\(extra-pass:\s*(security|skeptic|integration|refuter)\)\s*", re.I)


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _claim_norm(value: object) -> str:
    """Normalize claim whitespace without changing identifier identity."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _identity_field(finding: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = finding.get(name)
        if value not in (None, ""):
            return _claim_norm(value) or UNKNOWN
    return UNKNOWN


def _path(value: object) -> str:
    # Git paths are byte/Unicode identities.  Only normalize repository path
    # syntax; compatibility or case folding could merge distinct files.
    text = str(value or "")
    while text.startswith("./"):
        text = text[2:]
    return text or UNKNOWN


def _field(finding: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = finding.get(name)
        if value not in (None, ""):
            return _norm(value) or UNKNOWN
    return UNKNOWN


def _source_and_claim(finding: Mapping[str, Any]) -> tuple[str, str]:
    explicit = _field(finding, "pass_source", "source", "pass")
    marker = None
    for candidate in (finding.get("title"), finding.get("detail")):
        if candidate not in (None, ""):
            marker = (_EXTRA_MARKER.match(str(candidate))
                      or _PASS_MARKER.match(str(candidate)))
            if marker is not None:
                break
    raw = ""
    for name in ("claim", "semantic_claim", "description", "detail", "title"):
        value = finding.get(name)
        if value not in (None, ""):
            raw = str(value)
            break
    if explicit == UNKNOWN and marker is not None:
        explicit = marker.group(1).casefold()
    claim_marker = _EXTRA_MARKER.match(raw) or _PASS_MARKER.match(raw)
    if claim_marker is not None:
        raw = raw[claim_marker.end():]
    return explicit, _claim_norm(raw) or UNKNOWN


def _location(finding: Mapping[str, Any]) -> tuple[str, str]:
    """Return bounded location metadata kept outside the digest."""
    path = _path(finding.get("file"))
    value: Any = UNKNOWN
    for name in ("line", "line_start", "line_end"):
        candidate = finding.get(name)
        if candidate not in (None, ""):
            value = candidate
            break
    return path, _norm(value)


def fingerprint_payload(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded canonical payload used by the v2 digest."""
    scope = finding.get("scope_attribution")
    scope_name = scope.get("scope") if isinstance(scope, Mapping) else None
    scope_reason = scope.get("reason_code") if isinstance(scope, Mapping) else None
    rename = finding.get("rename_ancestry")
    if isinstance(rename, (list, tuple)):
        rename_value: Any = [_path(item) for item in rename]
    else:
        rename_value = _path(rename) if rename not in (None, "") else UNKNOWN
    anchor = _identity_field(finding, "symbol", "hunk_anchor", "anchor")
    pass_source, claim = _source_and_claim(finding)
    scope_owner = (scope.get("owner_slice_id") or scope.get("dependency_id")
                   if isinstance(scope, Mapping) else None)
    return {
        "version": VERSION,
        "algorithm": ALGORITHM,
        "path": _path(finding.get("file")),
        "rename_ancestry": rename_value,
        "anchor": anchor,
        "category": _field(finding, "category", "rule_id", "rule"),
        "claim": claim,
        "pass_source": pass_source,
        "stack_scope": _norm(scope_name) or UNKNOWN,
        "stack_reason": _norm(scope_reason) or UNKNOWN,
        "scope_owner": _norm(scope_owner) or UNKNOWN,
        "mutation": _field(finding, "mutation_type", "mutation", "evidence_id"),
    }


def finding_fingerprint(finding: Mapping[str, Any]) -> str:
    payload = fingerprint_payload(finding)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def annotate_findings(
    findings: Iterable[object],
    previous: Iterable[object] = (),
) -> list[object]:
    """Add fingerprint and conservative lineage metadata to finding dicts."""
    prior: dict[str, list[tuple[str | None, int, str | None]]] = {}
    prior_payloads: list[tuple[Mapping[str, Any], str | None, int, str | None]] = []
    for index, item in enumerate(previous):
        if isinstance(item, Mapping):
            digest = item.get("finding_fingerprint_v2") or finding_fingerprint(item)
            prior.setdefault(str(digest), []).append((
                item.get("_lineage_review_id"),
                int(item.get("_lineage_finding_index", index)),
                item.get("_lineage_reviewed_at")))
            prior_payloads.append((item, item.get("_lineage_review_id"),
                                   int(item.get("_lineage_finding_index", index)),
                                   item.get("_lineage_reviewed_at")))
    out: list[object] = []
    for index, item in enumerate(findings):
        if not isinstance(item, Mapping):
            out.append(item)
            continue
        finding = dict(item)
        digest = finding_fingerprint(finding)
        matches = prior.get(digest, [])
        predecessor_review_id = None
        predecessor = None
        if len(matches) == 1:
            predecessor_review_id, predecessor, predecessor_at = matches[0]
            old = next((candidate for candidate in prior_payloads
                        if candidate[1] == predecessor_review_id
                        and candidate[2] == predecessor), None)
            if old is not None and _location(old[0]) != _location(finding):
                old_payload = fingerprint_payload(old[0])
                # Without a stable anchor, a changed line may be a second
                # independent occurrence rather than the same finding moved.
                # Preserve the conservative ambiguity result instead of
                # inventing a predecessor.
                if (old_payload["anchor"] == UNKNOWN
                        and fingerprint_payload(finding)["anchor"] == UNKNOWN):
                    reason = "ambiguous"
                    predecessor_review_id = None
                    predecessor = None
                else:
                    reason = "moved"
            else:
                reason = "repeated"
        elif len(matches) > 1:
            # Multiple exact occurrences are genuinely ambiguous even when a
            # timestamp could provide a deterministic choice.  Never silently
            # select one predecessor when location identity is not in the hash.
            reason = "ambiguous"
            predecessor = None
        else:
            payload = fingerprint_payload(finding)
            near = []
            for old, old_review_id, old_index, old_at in prior_payloads:
                old_payload = fingerprint_payload(old)
                structural = ("category", "anchor", "pass_source", "mutation",
                              "claim")
                if all(payload[name] == old_payload[name] for name in structural):
                    rename_ok = payload["rename_ancestry"] == old_payload["rename_ancestry"]
                    if not rename_ok and isinstance(payload["rename_ancestry"], list):
                        rename_ok = old_payload["path"] in payload["rename_ancestry"]
                    if not rename_ok:
                        continue
                    if (payload["path"] != old_payload["path"]
                            and payload["anchor"] == UNKNOWN
                            and payload["rename_ancestry"] == UNKNOWN
                            and old_payload["rename_ancestry"] == UNKNOWN):
                        continue
                    near.append((old, old_review_id, old_index, old_at, old_payload))
            if len(near) == 1:
                old, predecessor_review_id, predecessor, predecessor_at, old_payload = near[0]
                old_path, old_line = _location(old)
                new_path, new_line = _location(finding)
                if (payload["path"] != old_payload["path"]
                        or new_path != old_path
                        or (new_line != UNKNOWN and old_line != UNKNOWN
                            and new_line != old_line)):
                    reason = "moved"
                elif (payload["stack_scope"], payload["stack_reason"],
                      payload["scope_owner"]) != (
                          old_payload["stack_scope"], old_payload["stack_reason"],
                          old_payload["scope_owner"]):
                    reason = "scope_changed"
                else:
                    reason = "new"
            elif len(near) > 1:
                reason = "ambiguous"
                predecessor = None
            else:
                reason = "new"
        finding["finding_fingerprint_v2"] = digest
        finding["finding_lineage_v2"] = {
            "version": VERSION,
            "match_reason": reason,
            "predecessor_index": predecessor,
            "predecessor_review_id": predecessor_review_id,
            "path": _location(finding)[0],
            "line": _location(finding)[1],
        }
        out.append(finding)
    return out
