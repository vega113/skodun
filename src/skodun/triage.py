"""Human dismissal of review findings, with an audited reason, plus the
fail-closed artifact validation that keeps a corrupt or hand-edited review
from ever satisfying the gate.

PARITY-CRITICAL: ported from the oracle's `grok_review_triage.py`. Where this
module's *review semantics* and the oracle's disagree, the oracle wins — the
placeholder set, the reason floor, and the accept/reject verdict of
`validate_reason` are pinned to it by oracle-loaded tests.

`load_valid_artifact` is the one deliberate, documented exception: it is
strictly *stronger* than the oracle's `load_review`. See the comment block
above that function.
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


# DELIBERATE, DOCUMENTED DIVERGENCE FROM THE ORACLE — read before "fixing"
# this back to match `grok_review_triage.py:176-230`.
#
# The oracle's `load_review` is LENIENT about absent keys: a missing or None
# `findings` is silently coerced to `[]`, and the `findings_total !=
# len(findings)` check is skipped entirely when `findings_total` is missing or
# None (it validates the count only when the artifact actually asserts one).
# That leniency is safe at the oracle's OWN call sites, which re-derive
# `review.get("findings") or []` for display.
#
# It is NOT safe here. In skodun this function is the fail-closed validator the
# GATE (Task 7) runs before a stored review is allowed to certify a push, and
# the check the LEGACY IMPORTER (Task 16) explicitly leans on: a legacy index
# row is a derived summary *without* `findings[]`, and storing it as
# trustworthy "would let it satisfy the gate, whose artifact validation
# (Task 6) then rejects it". Under the lenient rule an artifact carrying no
# `findings` key reads as "zero findings" — i.e. clean — and the gate can PASS
# on a review whose findings were never recorded. That inverts the project's
# central fail-closed posture.
#
# "The oracle wins" exists to keep keys and review semantics byte-compatible
# with the legacy archive; it does not require importing the oracle's weaker
# validation into skodun's gate path. Being STRICTER is safe in this direction:
# the worst case is that a malformed artifact forces a fresh review.
def load_valid_artifact(rec) -> dict:
    """Return `rec` if it is a self-consistent review artifact, else raise.

    Rejects, each with an `ArtifactError` naming the specific problem: a
    non-object artifact; a missing `findings`; a `findings` that is not a
    list; any non-dict member of `findings`; a missing `findings_total`; a
    `findings_total` that is not a plain int (bool/float/str all rejected —
    `isinstance(True, int)` is True in Python, so the bool check must be
    explicit and must come first); and `findings_total != len(findings)`.

    Stricter than the oracle on the two missing-key cases; see the comment
    block above for why that divergence is deliberate and fail-safe.
    """
    if not isinstance(rec, dict):
        raise ArtifactError("artifact is not an object")
    if "findings" not in rec:
        raise ArtifactError("findings is missing")
    findings = rec["findings"]
    if not isinstance(findings, list):
        raise ArtifactError(f"findings is not a list ({findings!r})")
    if any(not isinstance(f, dict) for f in findings):
        raise ArtifactError("findings contains a non-object entry")
    if "findings_total" not in rec:
        raise ArtifactError("findings_total is missing")
    total = rec["findings_total"]
    if isinstance(total, bool) or not isinstance(total, int):
        raise ArtifactError(f"findings_total is not an integer ({total!r})")
    if total != len(findings):
        raise ArtifactError(
            f"findings_total={total} != len(findings)={len(findings)} "
            "(truncated or hand-edited artifact)")
    return rec


def dismiss(store: Store, review: dict, index: int, reason: str, now: str) -> dict:
    """Record an audited dismissal of one finding and return the ledger row."""
    review = load_valid_artifact(review)
    validate_reason(reason)
    # load_valid_artifact guarantees `findings` is present and a list of dicts.
    findings = review["findings"]
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
    for f in load_valid_artifact(review)["findings"]:
        if finding_key(f.get("file", ""), f.get("title", "")) not in triaged:
            out.append(f)
    return out
