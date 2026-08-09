"""Fail-closed exact-diff reuse for opt-in foreground reviews.

Reuse is deliberately a read-only optimization.  It never changes a review
artifact, gate/trust code, or provider counters; the caller appends an audit
event and either renders a projection of the stored review or falls through to
the ordinary foreground pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import (batching, budget, checklist, contextpack, gitio, passes,
               promptbuild, triage)
from .trust import banner, is_trustworthy


@dataclass(frozen=True)
class ReuseIdentity:
    repo_id: str
    worktree_root: str
    branch: str
    head: str
    base_sha: str
    diff_hash: str
    context_hash: str | None
    checklist_hash: str | None
    tree_fingerprint: str


@dataclass(frozen=True)
class ReuseProbe:
    candidate: dict | None
    identity: ReuseIdentity
    reason: str


def checklist_identity(selection: checklist.Selection) -> str:
    """Hash the selected checklist body and its degradation metadata."""
    body = json.dumps({
        "sections": list(selection.sections),
        "bytes": selection.bytes_total,
        "over_budget": selection.over_budget,
        "dropped": list(selection.dropped),
        "body": selection.body,
        "note": selection.note,
        "degraded": selection.degraded,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def aggregate_checklist_identity(selections) -> str | None:
    """Hash the ordered checklist selections used by a batched review."""
    identities = [checklist_identity(selection) for selection in selections]
    if not identities:
        return None
    body = json.dumps(identities, separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def aggregate_context_identity(context_hashes, *, enabled: bool) -> str | None:
    """Hash the ordered context packs used by a batched review."""
    if not enabled:
        return None
    if (not context_hashes
            or any(not isinstance(value, str) or not value.strip()
                   for value in context_hashes)):
        return None
    body = json.dumps(list(context_hashes), separators=(",", ":"))
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def _reviewer_by_name(cfg, name: object):
    if not isinstance(name, str) or not name:
        return None
    for reviewer in cfg.reviewers:
        if reviewer.enabled and reviewer.name == name:
            return reviewer
    return None


def _context_hash(root: Path, diff, cfg, reviewer):
    if not cfg.defaults.context_pack or reviewer is None:
        return None
    max_bytes = budget.prompt_budget(cfg.defaults, reviewer)
    headroom = promptbuild.context_headroom(
        max_bytes, len(diff.data), packing=True)
    pack = contextpack.pack(
        root, list(diff.files), dict(diff.statuses), headroom,
        pack_large_added=False)
    value = pack.sha256
    return value if isinstance(value, str) and value.strip() else None


def _identity_for(repo: Path, cfg, base, diff, *, branch: str,
                  reviewer_name: str | None, candidate: dict | None = None):
    root = gitio._worktree_root(repo)
    selection = checklist.select(
        diff.files, "full", _under(root, cfg.defaults.checklist_dir),
        _under(root, cfg.defaults.rules_json), cfg.defaults.checklist_map,
        cfg.defaults.test_path_patterns)
    routed = (candidate or {}).get("routed_reviewer")
    reviewer = _reviewer_by_name(cfg, routed) or _reviewer_by_name(
        cfg, reviewer_name)
    if reviewer is not None and len(diff.data) > budget.prompt_budget(
            cfg.defaults, reviewer):
        context_hash, checklist_hash = _batched_identities(
            root, diff, cfg, reviewer)
    else:
        context_hash = _context_hash(root, diff, cfg, reviewer)
        checklist_hash = checklist_identity(selection)
    return ReuseIdentity(
        repo_id=gitio.repository_identity(root),
        worktree_root=gitio.observed_worktree_root(root),
        branch=branch,
        head=gitio.head_sha(root),
        base_sha=base.sha,
        diff_hash=gitio.diff_identity(diff.data),
        context_hash=context_hash,
        checklist_hash=checklist_hash,
        tree_fingerprint=gitio.tree_fingerprint(root),
    )


def _batched_identities(root: Path, diff, cfg, reviewer):
    """Reproduce the pipeline's deterministic batch checklist/context identity."""
    defaults = cfg.defaults
    envelope = budget.prompt_budget(defaults, reviewer)
    batch_budget = envelope // 2 if defaults.context_pack else envelope
    batches = batching.split(diff.data, max(1, batch_budget))
    mode = passes.batch_checklist_mode(len(batches))
    sole = len(batches) == 1
    selections = []
    context_hashes = []
    for batch in batches:
        selection = checklist.select(
            batch.files, mode, _under(root, defaults.checklist_dir),
            _under(root, defaults.rules_json), defaults.checklist_map,
            defaults.test_path_patterns)
        selections.append(selection)
        if defaults.context_pack:
            headroom = promptbuild.context_headroom(
                envelope, len(batch.data), packing=True)
            pack = contextpack.pack(
                root, list(batch.files),
                {f: diff.statuses[f] for f in batch.files
                 if f in diff.statuses}, headroom,
                pack_large_added=not sole)
            context_hashes.append(
                pack.sha256 if isinstance(pack.sha256, str) else None)
    if passes.should_run_integration(len(batches)):
        selections.append(checklist.select(
            diff.files, passes.INTEGRATION_CHECKLIST_MODE,
            _under(root, defaults.checklist_dir), _under(root, defaults.rules_json),
            defaults.checklist_map, defaults.test_path_patterns))
    return (aggregate_context_identity(
                context_hashes, enabled=defaults.context_pack),
            aggregate_checklist_identity(selections))


def _under(root: Path, relative: str) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else root / path


def _candidate_matches(candidate: dict, identity: ReuseIdentity) -> bool:
    if candidate.get("source") != "skodun" or candidate.get("mode") != "now":
        return False
    axes = tuple(candidate.get(key) for key in
                 ("parse_ok", "degraded", "diff_truncated"))
    if any(type(value) is not bool for value in axes):
        return False
    if not is_trustworthy(*axes) or candidate.get("trustworthy") is not True:
        return False
    required = (
        "repo_id", "worktree_root", "base_sha", "diff_hash",
        "checklist_hash", "tree_fingerprint")
    if any(not isinstance(candidate.get(key), str)
           or not candidate[key].strip() for key in required):
        return False
    candidate_context = candidate.get("context_hash")
    if identity.context_hash is None:
        if candidate_context not in (None, ""):
            return False
    elif (not isinstance(candidate_context, str)
          or not candidate_context.strip()
          or candidate_context != identity.context_hash):
        return False
    return all(candidate[key] == getattr(identity, key) for key in required)


def find_exact_candidate(store, identity: ReuseIdentity) -> dict | None:
    """Return the newest strictly matching trustworthy foreground artifact."""
    from .triage import load_valid_artifact

    for candidate in store.reuse_candidates(
            identity.repo_id, identity.base_sha, identity.diff_hash):
        try:
            candidate = load_valid_artifact(candidate)
        except Exception:
            continue
        if _candidate_matches(candidate, identity):
            return candidate
    return None


def probe(store, repo, *, cfg, reviewer: str | None = None,
          client_family: str | None = None,
          intent_client_family: str | None = None) -> ReuseProbe:
    """Capture current identity, probe, then recheck the tree before returning."""
    root = gitio._worktree_root(Path(repo))
    if (gitio.is_primary_checkout(root)
            and os.environ.get("SKODUN_ALLOW_MAIN") != "1"):
        raise RuntimeError(
            f"{root} is the primary checkout; no reuse probe ran")
    base = gitio.resolve_base(root)
    diff = gitio.capture_diff(root, base.sha, cfg.defaults.untracked_max)
    branch = gitio.current_branch(root)
    current_reviewer = reviewer
    if current_reviewer is None:
        default = next(
            (entry for entry in cfg.reviewers
             if entry.enabled and entry.role == "finder"), None)
        current_reviewer = None if default is None else default.name
    identity = _identity_for(
        root, cfg, base, diff, branch=branch, reviewer_name=current_reviewer)
    candidates = store.reuse_candidates(
        identity.repo_id, identity.base_sha, identity.diff_hash)
    candidate = None
    for raw in candidates:
        try:
            loaded = triage.load_valid_artifact(raw)
        except Exception:
            continue
        if (loaded.get("requested_reviewer") is not None
                and loaded.get("requested_reviewer") != reviewer):
            continue
        if (intent_client_family is not None
                and loaded.get("client_family") is not None
                and loaded.get("client_family") != intent_client_family):
            continue
        candidate_identity = _identity_for(
            root, cfg, base, diff, branch=branch,
            reviewer_name=current_reviewer, candidate=loaded)
        if _candidate_matches(loaded, candidate_identity):
            identity = candidate_identity
            candidate = loaded
            break
    if gitio.tree_fingerprint(root) != identity.tree_fingerprint:
        return ReuseProbe(None, identity, "tree moved during reuse probe")
    if candidate is None:
        return ReuseProbe(None, identity, "no exact trustworthy review matched")
    return ReuseProbe(candidate, identity, "exact identity match")


def project(store, candidate: dict, *, branch: str, base_sha: str) -> tuple[int, str]:
    """Render current triage over a reused artifact without persisting it."""
    open_findings = triage.open_findings(
        candidate, store.triage_for(branch, base_sha))
    projection = dict(candidate)
    projection["findings"] = open_findings
    projection["findings_total"] = len(open_findings)
    projection["severity"] = {
        level: sum(1 for finding in open_findings
                   if finding.get("severity") == level)
        for level in ("high", "medium", "low")}
    return (1 if open_findings else 0), banner(projection)
