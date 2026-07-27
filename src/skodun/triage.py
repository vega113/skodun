"""Human dismissal of review findings, with an audited reason, plus the
fail-closed artifact validation that keeps a corrupt or hand-edited review
from ever satisfying the gate.

PARITY-CRITICAL: ported from the oracle's `grok_review_triage.py`. Where this
module's behavior and the oracle's disagree, the oracle wins; see the
`load_valid_artifact` docstring below for the one place that mattered.
"""

from __future__ import annotations

from .store import Store
from .textnorm import finding_key, ledger_key, norm


class TriageError(ValueError):
    """A dismissal reason or finding index failed validation."""


class ArtifactError(ValueError):
    """A review artifact is self-inconsistent and must not be trusted."""


# PARITY: verbatim from tubescribes/scripts/grok_review_triage.py:58-63 —
# the parity test below asserts exact set equality against the oracle module.
PLACEHOLDER_REASONS = {
    "false positive", "fp", "not a bug", "wontfix", "won't fix", "no", "nope",
    "n/a", "na", "none", "ignore", "ignored", "skip", "skipped", "ok", "fine",
    "invalid", "wrong", "incorrect", "disagree", "not an issue", "no issue",
    "already fixed", "by design", "intentional", "known", "irrelevant",
}
MIN_REASON_CHARS = 20


def validate_reason(reason: str) -> None:
    """Raise TriageError unless `reason` clears the audit floor.

    A dismissal that says nothing is the failure mode this ledger exists to
    prevent: the reason must survive normalization (whitespace collapse +
    lowercase) to at least MIN_REASON_CHARS, and must not be one of the
    PLACEHOLDER_REASONS verbatim.
    """
    n = norm(reason)
    if len(n) < MIN_REASON_CHARS:
        raise TriageError(f"reason too short (<{MIN_REASON_CHARS} chars normalized)")
    if n in PLACEHOLDER_REASONS:
        raise TriageError(f"placeholder reason rejected: {n!r}")


def load_valid_artifact(rec) -> dict:
    """Return `rec` if it is a self-consistent review artifact, else raise.

    PARITY NOTE (deviation from the brief, resolved per the oracle at
    grok_review_triage.py:176-230): the oracle treats a missing/None
    `findings` as an empty list (not an error), and skips the
    `findings_total` check entirely when `findings_total` is missing/None —
    it only validates the field when the artifact actually asserts one. The
    brief's inline Step-3 code instead rejected `findings=None` and
    `findings_total=None` outright, which would fail a well-formed artifact
    that simply omits an empty `findings` list. This implementation follows
    the oracle: missing is not the same as corrupt. What IS still rejected,
    matching the oracle exactly: a non-object artifact; `findings` present
    but not a list; any non-dict member of `findings`; a `findings_total`
    that is present but not a plain int (bool/float/str all rejected —
    `isinstance(True, int)` is True in Python, so the bool check must be
    explicit); and `findings_total != len(findings)`.
    """
    if not isinstance(rec, dict):
        raise ArtifactError("artifact is not an object")
    findings = rec.get("findings")
    if findings is None:
        findings = []
    elif not isinstance(findings, list):
        raise ArtifactError("findings is not a list")
    if any(not isinstance(f, dict) for f in findings):
        raise ArtifactError("findings contains a non-object entry")
    total = rec.get("findings_total")
    if total is not None:
        if isinstance(total, bool) or not isinstance(total, int):
            raise ArtifactError(f"findings_total is not an integer ({total!r})")
        if total != len(findings):
            raise ArtifactError(
                f"findings_total={total} != len(findings)={len(findings)} "
                "(truncated or hand-edited artifact)")
    return rec


def _findings_of(review: dict) -> list[dict]:
    """Effective findings list of an already-validated artifact.

    Mirrors the oracle's own call sites, which never trust the validated
    dict's `findings` key to be present and instead re-derive
    `review.get("findings") or []` (see grok_review_triage.py:317, 382,
    481, 572) — the same defaulting `load_valid_artifact` applies above.
    """
    return [f for f in (review.get("findings") or []) if isinstance(f, dict)]


def dismiss(store: Store, review: dict, index: int, reason: str, now: str) -> dict:
    """Record an audited dismissal of one finding and return the ledger row."""
    review = load_valid_artifact(review)
    validate_reason(reason)
    findings = _findings_of(review)
    if not (0 <= index < len(findings)):   # negative indexes must not
        raise TriageError(f"finding index {index} out of range")  # silently alias
    f = findings[index]
    fkey = finding_key(f.get("file", ""), f.get("title", ""))
    rec = dict(ledger_key=ledger_key(review["branch"], review["base_sha"], fkey),
               finding_key=fkey, id=review["id"], branch=review["branch"],
               base_sha=review["base_sha"], file=f.get("file"), line=f.get("line"),
               severity=f.get("severity"), title=f.get("title"),
               dismissed_reason=reason, dismissed_at=now)
    store.add_triage(rec)
    return rec


def open_findings(review: dict, triaged: dict[str, dict]) -> list[dict]:
    """Findings from `review` with no matching entry in `triaged`."""
    out = []
    for f in _findings_of(load_valid_artifact(review)):
        if finding_key(f.get("file", ""), f.get("title", "")) not in triaged:
            out.append(f)
    return out
