"""R2 churn attribution and R3 round context — annotation only, never gate.

R2 marks whether a finding lands in files that changed since the previous
trustworthy review of the same branch (and repo when scoped). It never
narrows what the model reviews or what the gate certifies.

R3 reports "review N of this branch" and how many findings were already
triaged in earlier rounds. Both are derived from store rows + git; no
schema change.

Fail closed on attribution: a missing file, missing previous head, missing
repo path, or git failure yields "not attributed" rather than a false
"in last fix".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .store import Store

# Finding annotation keys written for display. Optional on older records.
CHURN_KEY = "in_prior_fix_churn"       # True | False | absent (unknown)
CHURN_UNKNOWN = "churn_unknown"         # True when we could not attribute

# Terminal statuses that count as a completed review round for R3 ordinals.
# `running` is not a round anyone has seen; everything else is history.
_TERMINAL = frozenset({"clean", "degraded", "failed", "superseded"})


@dataclass(frozen=True)
class ChurnSummary:
    """Counts for one review's findings against the prior-fix file set."""

    total: int
    in_churn: int
    unknown: int

    @property
    def attributed(self) -> int:
        return self.total - self.unknown

    def line(self) -> str:
        """Human-readable summary for triage list / surface."""
        if self.total == 0:
            return "churn: no findings"
        if self.unknown == self.total:
            return (f"churn: could not attribute any of {self.total} finding(s) "
                    f"to code changed since the previous review")
        if self.unknown:
            return (f"churn: {self.in_churn} of {self.attributed} attributed "
                    f"finding(s) land in code changed since the previous "
                    f"review ({self.unknown} unattributed)")
        return (f"churn: {self.in_churn} of {self.total} finding(s) land in "
                f"code changed since the previous review")


@dataclass(frozen=True)
class RoundContext:
    """R3: where this review sits among prior rounds on the same branch/repo."""

    ordinal: int          # 1-based among terminal reviews, oldest first
    total_rounds: int     # terminal reviews on this branch/repo including this
    prior_triaged: int    # findings still cleared that were cleared on earlier rounds

    def line(self) -> str:
        return (f"round: review {self.ordinal} of {self.total_rounds} on this "
                f"branch; {self.prior_triaged} finding(s) already triaged in "
                f"earlier rounds")


def previous_trustworthy_review(
    store: Store,
    *,
    branch: str,
    repo: str | None,
    before: Mapping[str, Any],
) -> dict | None:
    """The most recent trustworthy terminal review of this branch before `before`.

    Scoped by `repo` when the current review carries one (Phase 4); when the
    current review has `repo IS NULL` (pre-v5), only other NULL-repo rows are
    considered — never inventing cross-repo pairing.
    """
    before_id = before.get("id")
    before_at = before.get("reviewed_at") or ""
    rows = store.list_reviews(branch, limit=500, repo=repo)
    # list_reviews is newest-first.
    for rec in rows:
        if rec.get("id") == before_id:
            continue
        if rec.get("status") not in _TERMINAL:
            continue
        if rec.get("trustworthy") is not True:
            continue
        # Older or equal timestamp but different id: prefer reviewed_at order.
        at = rec.get("reviewed_at") or ""
        if before_at and at > before_at:
            continue
        if before_at and at == before_at and (rec.get("id") or "") >= (before_id or ""):
            # Same second: skip anything not strictly earlier by id for stability.
            continue
        return rec
    return None


def paths_changed_between(repo: Path, old_sha: str, new_sha: str) -> frozenset[str] | None:
    """File paths changed from `old_sha` to `new_sha`, or None if git cannot say.

    None (not empty) is the fail-closed answer: empty would mean "attribute
    every finding as outside churn", which is a false signal when we simply
    could not read history.
    """
    if not old_sha or not new_sha:
        return None
    if old_sha == new_sha:
        return frozenset()
    try:
        from . import gitio
        diff = gitio.capture_ref_diff(Path(repo), old_sha, new_sha)
    except Exception:  # noqa: BLE001 - attribution must never break listing
        return None
    files = diff.files if isinstance(diff.files, list) else []
    return frozenset(str(p) for p in files if p)


def finding_in_churn(finding: Mapping, changed: frozenset[str] | None) -> bool | None:
    """True / False when attributed; None when the finding cannot be attributed."""
    if changed is None:
        return None
    path = finding.get("file") if isinstance(finding, Mapping) else None
    if not isinstance(path, str) or not path.strip():
        return None
    # Exact path match first; also accept a finding path that is a suffix of a
    # changed path or vice versa only when equal after norm — no fuzzy join.
    # Fail closed: unknown path spelling is unknown, not "in churn".
    return path in changed


def annotate_findings_churn(
    findings: list,
    changed: frozenset[str] | None,
) -> tuple[list[dict], ChurnSummary]:
    """Return shallow-copied findings with churn keys, plus a summary.

    Does not mutate the input list or its dicts.
    """
    out: list[dict] = []
    in_churn = 0
    unknown = 0
    total = 0
    for f in findings if isinstance(findings, list) else []:
        total += 1
        base = dict(f) if isinstance(f, Mapping) else {}
        hit = finding_in_churn(base, changed)
        if hit is None:
            base[CHURN_UNKNOWN] = True
            base.pop(CHURN_KEY, None)
            unknown += 1
        else:
            base[CHURN_KEY] = bool(hit)
            base.pop(CHURN_UNKNOWN, None)
            if hit:
                in_churn += 1
        out.append(base)
    return out, ChurnSummary(total=total, in_churn=in_churn, unknown=unknown)


def churn_for_review(
    store: Store,
    review: Mapping[str, Any],
    *,
    repo_path: Path | str | None = None,
) -> tuple[list[dict], ChurnSummary, dict | None]:
    """Annotate `review`'s findings using the previous trustworthy review's head.

    `repo_path` is the on-disk git common dir / worktree; defaults to
    `review["repo"]` when present. Returns (annotated_findings, summary,
    previous_review_or_None).
    """
    findings = review.get("findings")
    findings = findings if isinstance(findings, list) else []
    branch = review.get("branch")
    if not isinstance(branch, str) or not branch:
        annotated, summary = annotate_findings_churn(findings, None)
        return annotated, summary, None

    repo_key = review.get("repo")
    repo_key = repo_key if isinstance(repo_key, str) and repo_key else None
    prev = previous_trustworthy_review(
        store, branch=branch, repo=repo_key, before=review,
    )
    path = repo_path if repo_path is not None else repo_key
    if prev is None or path is None:
        annotated, summary = annotate_findings_churn(findings, None)
        return annotated, summary, prev

    old_head = prev.get("head") if isinstance(prev.get("head"), str) else ""
    new_head = review.get("head") if isinstance(review.get("head"), str) else ""
    changed = paths_changed_between(Path(path), old_head, new_head)
    annotated, summary = annotate_findings_churn(findings, changed)
    return annotated, summary, prev


def round_context_for_review(store: Store, review: Mapping[str, Any]) -> RoundContext | None:
    """R3 context for one review, or None when it cannot be placed on a branch."""
    branch = review.get("branch")
    review_id = review.get("id")
    if not isinstance(branch, str) or not branch or not isinstance(review_id, str):
        return None
    repo_key = review.get("repo")
    repo_key = repo_key if isinstance(repo_key, str) and repo_key else None

    rows = store.list_reviews(branch, limit=500, repo=repo_key)
    # Newest first from the store; reverse to oldest-first for ordinals.
    terminal = [
        r for r in rows
        if r.get("status") in _TERMINAL and isinstance(r.get("id"), str)
    ]
    # list_reviews is newest-first; reverse for chronological ordinal.
    chronological = list(reversed(terminal))
    ordinal = 0
    for i, r in enumerate(chronological, start=1):
        if r["id"] == review_id:
            ordinal = i
            break
    if ordinal == 0:
        # Current review may not be terminal yet, or not in the limited list.
        # Treat it as the next round after all listed terminal ones.
        ordinal = len(chronological) + 1
        total = ordinal
        earlier_ids = {r["id"] for r in chronological}
    else:
        total = len(chronological)
        earlier_ids = {r["id"] for r in chronological[: ordinal - 1]}

    prior = store.count_triaged_on_reviews(branch, earlier_ids)
    return RoundContext(ordinal=ordinal, total_rounds=total, prior_triaged=prior)


def churn_marker(finding: Mapping) -> str:
    """Short parenthetical for one finding line, or empty."""
    if not isinstance(finding, Mapping):
        return ""
    if finding.get(CHURN_UNKNOWN) is True:
        return "churn:?"
    if CHURN_KEY not in finding:
        return ""
    return "churn:yes" if finding.get(CHURN_KEY) is True else "churn:no"
