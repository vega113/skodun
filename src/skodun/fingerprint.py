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
from itertools import islice
from typing import Any


VERSION = "finding_fingerprint_v2"
ALGORITHM = "canonical-json-sha256-v1"
UNKNOWN = "unknown"
CANDIDATE_LIMIT = 200
MAX_LINEAGE_PROMPT_BYTES = 1024
_PROMPT_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PROMPT_REASONS = frozenset({
    "new", "repeated", "moved", "scope_changed", "ambiguous", "prior",
})
_PROMPT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
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
    *, incomplete_exact: Iterable[str] = (),
) -> list[object]:
    """Add fingerprints without asserting uniqueness after incomplete reads.

    Incomplete exact keys remain ambiguous even if recent/fuzzy fallback
    supplies a singleton: another unseen exact occurrence may still exist.
    """
    incomplete = frozenset(incomplete_exact)
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
        if digest in incomplete:
            reason = "ambiguous"
        elif len(matches) == 1:
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


def _prompt_field(value: object, *, limit: int = 256) -> str:
    """Flatten untrusted finding text so it cannot break a prompt line."""
    text = _PROMPT_CONTROL.sub("", str(value or "").replace("\r", " ").replace("\n", " "))
    return " ".join(text.split())[:limit]


def rank_prompt_candidates(rows: Iterable[object], *, changed_paths=(),
                           owner_ids=()) -> tuple[list[dict], int]:
    """Rank and deduplicate bounded hints, preserving exact path identity.

    Input order is newest first, so ties preserve recency. A known historical
    disposition wins a duplicate only within the same relevance rank.
    """
    paths = {_path(path) for path in changed_paths}
    owners = {_norm(owner) for owner in owner_ids if owner}
    candidates = []
    for item in islice(rows, CANDIDATE_LIMIT):
        if not isinstance(item, dict):
            continue
        scope = item.get("scope_attribution")
        owner = (scope.get("owner_slice_id") or scope.get("dependency_id")
                 if isinstance(scope, Mapping) else None)
        path_match = _path(item.get("file")) in paths
        owner_match = bool(owner and _norm(owner) in owners)
        disposition = item.get("_lineage_disposition")
        known_disposition = disposition in ("open", "dismiss", "defer", "reopen")
        candidates.append(((not path_match, not owner_match, not known_disposition), item))
    candidates.sort(key=lambda pair: pair[0])
    seen = set()
    selected = []
    matched = 0
    for rank, item in candidates:
        digest = item.get("finding_fingerprint_v2")
        if not isinstance(digest, str) or _PROMPT_DIGEST.fullmatch(digest) is None:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        selected.append(item)
        matched += int(not rank[0] or not rank[1])
    return selected, matched


def render_prompt_context(rows: Iterable[object],
                          max_bytes: int = MAX_LINEAGE_PROMPT_BYTES) -> tuple[bytes, bool]:
    """Render a compact prior-fingerprint hint for provider prompts.

    Digests and paths only: never claims, transcripts, or raw finding bodies.
    Missing or malformed rows are omitted rather than invented.
    """
    if type(max_bytes) is not int or max_bytes < 128:
        raise ValueError("lineage prompt context budget must be an int >= 128")
    lines = ["----- BEGIN PRIOR FINDINGS -----"]
    count = 0
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        digest = item.get("finding_fingerprint_v2")
        if not isinstance(digest, str) or not digest:
            digest = finding_fingerprint(item)
        if not isinstance(digest, str) or _PROMPT_DIGEST.fullmatch(digest) is None:
            continue
        path = json.dumps(_prompt_field(_path(item.get("file"))) or UNKNOWN)
        lineage = item.get("finding_lineage_v2")
        reason = lineage.get("match_reason") if isinstance(lineage, Mapping) else None
        if reason not in _PROMPT_REASONS:
            reason = "prior"
        disposition = item.get("_lineage_disposition")
        suffix = (f" disposition={disposition}"
                  if disposition in ("open", "dismiss", "defer", "reopen", "unknown") else "")
        lines.append(f"{digest} path={path} reason={reason}{suffix}")
        count += 1
    if count == 0:
        return b"", False
    lines.insert(1, f"count={count} truncated=false")
    lines.append("----- END PRIOR FINDINGS -----")
    text = ("\n".join(lines) + "\n").encode("utf-8", "replace")
    if len(text) <= max_bytes:
        return text, False
    marker = b"\n[prior findings truncated; full diff remains authoritative]\n"
    header = (
        "----- BEGIN PRIOR FINDINGS -----\n"
        f"count={count} truncated=true\n"
    ).encode("utf-8")
    budget = max(0, max_bytes - len(marker) - len(header))
    out = bytearray()
    for line in text.splitlines(keepends=True)[2:]:
        if len(line) > budget:
            continue
        if len(out) + len(line) > budget:
            break
        out += line
    return header + bytes(out) + marker, True
