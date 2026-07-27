"""The gate: does a trustworthy review cover the content about to be pushed?

THE EXIT CONTRACT, verbatim and non-negotiable:

    0  clean, or every finding carries a triage record
    1  findings remain open
    2  no trustworthy review covers this exact content

**Every unexpected exception maps to 2, never 1.** That asymmetry is the whole
design. `1` is an instruction to a human -- "go read the findings and triage
them" -- and it is only meaningful if the findings exist. A corrupt store, an
unreadable artifact or a git failure that surfaced as `1` would send someone to
triage a review that was never performed, and the ledger they wrote would then
certify the push. Read as `2` the same failures say "there is no usable review
here", which is exactly true.

PARITY-CRITICAL: ported from the oracle's `grok_review_triage.py::cmd_gate` and
the `--gate` branch of `grok-review-now.sh`. Behaviour pinned to the oracle:

  * The key is the `diff_hash` of the outgoing change, never HEAD. Committing
    already-reviewed working-tree edits moves HEAD without changing what ships
    and must not force a re-review; reverting reviewed-but-uncommitted edits
    changes what ships without moving HEAD and must not pass. There is
    deliberately no HEAD fallback: a fallback engages exactly when something is
    already wrong, and it would silently select the weaker policy this gate
    exists to replace.
  * An EMPTY outgoing diff is PASS(0). The oracle's `--diff-hash` seam exits 3
    on an empty capture and the shell maps that to `PASS ... nothing to
    review`. There is no content to review, so there is nothing to withhold.
  * The identity helper's warnings are ECHOED, not swallowed. They report
    exactly the conditions that make an identity under-scoped -- no main ref,
    or an untracked scan that hit its cap -- and both still produce a
    valid-looking hash. The enforcement point is the worst possible place for a
    degraded computation to be silent.
  * The index is re-asserted against the artifact, and a `base_sha` mismatch is
    a rebase and refuses.

TWO DELIBERATE DIVERGENCES from the oracle, both strictly *stronger* and both
fail-safe in the same direction (worst case: one extra review, never a wrong
PASS):

  1. The oracle's `is_trustworthy(artifact)` short-circuits on the artifact's
     `trustworthy` field when present, recomputing from the axes only for
     pre-2026-07-14 rows that predate the field. So an artifact hand-edited to
     `degraded: true` while keeping `trustworthy: true` passes the oracle's
     re-assertion. skodun has no pre-field rows -- `Store.save_review` computes
     the field on every write -- so it recomputes unconditionally AND requires
     the artifact's own field to agree. A record that contradicts itself
     certifies nothing.
  2. The oracle's rebase check is guarded by `if base_sha and
     artifact.get("base_sha")`, i.e. an artifact with an empty `base_sha`
     silently skips it. Here the comparison is unconditional;
     `load_valid_artifact` already guarantees the key is a string, and an empty
     one simply fails to match the real merge-base.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitio
from .config import Config
from .store import Store
from .triage import ArtifactError, load_valid_artifact, open_findings
from .trust import is_trustworthy

_OUTCOMES = {0: "pass", 1: "open-findings", 2: "no-review"}


@dataclass(frozen=True)
class GateResult:
    """A decision, and the identity of the content it was made about.

    Frozen: once a decision exists it is what gets recorded and returned, and a
    later stage must not be able to edit `code` in place.

    `diff_hash` carries the two hash-less conventions of the contract. A
    **skipped** decision records `None`: the bypass must work even when identity
    computation is itself broken -- that is what a bypass is for -- so it never
    depends on one. An **empty-diff pass** records `""`, the defined
    empty-change identity, which is a real answer rather than an absent one.
    """

    code: int
    message: str
    diff_hash: str | None = None


def _gate(store: Store, repo: Path, cfg: Config) -> GateResult:
    notes: list[str] = []
    base = gitio.resolve_base(repo)
    if base.warning:
        notes.append(f"identity note: {base.warning}")
    diff = gitio.capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    if diff.truncated_untracked:
        notes.append(f"identity note: untracked scan capped at {cfg.defaults.untracked_max}")
    prefix = "".join(f"SKODUN GATE: {n}\n" for n in notes)

    # ORACLE PARITY: empty capture => "nothing to review", PASS. `capture_diff`
    # already right-strips, but this must not depend on that: the emptiness
    # test and the identity function have to agree about what "empty" means,
    # or a diff of nothing but newlines would take the FAIL(2) branch and
    # demand a review of no content.
    if diff.data.rstrip(b"\n") == b"":
        return GateResult(0, prefix + "SKODUN GATE: PASS no outgoing change "
                                      "vs base -- nothing to review", "")

    dh = gitio.diff_identity(diff.data)
    review = store.latest_trustworthy_for(dh)
    if review is None:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) no trustworthy review "
                                      f"covers diff_hash={dh[:12]} -- run a review "
                                      "before pushing", dh)

    review = load_valid_artifact(review)

    # Re-assert artifact against index. The index row is a DERIVED SUMMARY and
    # the findings come from the artifact, so trusting the index's verdict
    # while reading the artifact's findings means trusting two records to
    # agree. They can diverge: a crashed writer, a partial rewrite, a reused
    # id, a hand-edited archive. A derived summary is never trusted alone.
    axes = (review.get("parse_ok"), review.get("degraded"),
            review.get("diff_truncated"), review.get("trustworthy"))
    if any(not isinstance(v, bool) for v in axes):
        # `is_trustworthy` coerces by truthiness by design, so `parse_ok: 1` or
        # `degraded: "false"` would sail through the invariant unexamined. The
        # gate type-checks before it asks.
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) artifact trust fields "
                                      f"are not booleans on review "
                                      f"{review.get('id')}", dh)
    recomputed = is_trustworthy(axes[0], axes[1], axes[2])
    if not recomputed or review["trustworthy"] is not recomputed:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) index and artifact "
                                      f"disagree on trust for review {review['id']} "
                                      "-- the archive is inconsistent; re-run the "
                                      "review", dh)
    if review.get("diff_hash") != dh:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) index and artifact "
                                      f"disagree on diff_hash for review "
                                      f"{review['id']} -- the archive is "
                                      "inconsistent; re-run the review", dh)

    # A rebase can leave the diff bytes identical while moving the merge-base.
    # The review still matches on content, but it was formed against different
    # surrounding code -- and its dismissals are scoped to the OLD base, so
    # accepting it here would keep alive exactly the amnesty the rebase re-opens.
    if review["base_sha"] != base.sha:
        return GateResult(2, prefix + f"SKODUN GATE: FAIL(2) base_sha mismatch on "
                                      f"review {review['id']}: taken against "
                                      f"{review['base_sha'][:12]}, current base is "
                                      f"{base.sha[:12]} (rebase detected) -- "
                                      "re-review required", dh)

    remaining = open_findings(review, store.triage_for(review["branch"],
                                                       review["base_sha"]))
    if remaining:
        return GateResult(1, prefix + f"SKODUN GATE: FAIL(1) {len(remaining)} "
                                      f"finding(s) open on review {review['id']}", dh)
    total = len(review["findings"])
    detail = f"{total} finding(s), all triaged" if total else "0 findings"
    return GateResult(0, prefix + f"SKODUN GATE: PASS {detail} on review "
                                  f"{review['id']} for diff_hash={dh[:12]}", dh)


def run_gate(store: Store, repo: Path, cfg: Config, env=os.environ) -> GateResult:
    """Decide, record the decision, and return it. Never raises.

    `env` defaults to the live `os.environ` mapping rather than a snapshot, so
    a caller that sets the bypass after import still gets it.
    """

    def _record(result: GateResult, outcome: str) -> GateResult:
        try:
            branch = None
            try:
                branch = gitio.current_branch(repo)
            except BaseException:
                pass   # best-effort: a skip must survive a broken repo identity
            store.log_gate_event(dict(
                at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                repo=str(repo), branch=branch, diff_hash=result.diff_hash,
                outcome=outcome, code=result.code,
                note=result.message.splitlines()[-1] if result.message else ""))
        except BaseException as e:
            # A gate that cannot write its own record is running on a broken
            # store and must not certify anything -- including a bypass, which
            # is only a decision because it leaves a trace.
            return GateResult(
                2, result.message + "\nSKODUN GATE: FAIL(2) could not record "
                   f"gate event: {e!r}", result.diff_hash)
        return result

    # Checked FIRST, before any identity work: see `GateResult.diff_hash`.
    if env.get("SKODUN_GATE_SKIP") == "1":
        return _record(GateResult(0, "SKODUN GATE: SKIPPED — recorded as a decision"),
                       "skipped")
    try:
        r = _gate(store, repo, cfg)
        return _record(r, _OUTCOMES[r.code])
    except ArtifactError as e:
        return _record(GateResult(2, f"SKODUN GATE: FAIL(2) invalid artifact: {e}"),
                       "error")
    except BaseException as e:
        # EVERY unexpected error is 2, never 1. `BaseException` and not
        # `Exception`: an interrupt or a MemoryError escaping here would
        # surface as the interpreter's own exit code of 1, which is precisely
        # the value that means "findings remain open".
        return _record(GateResult(2, f"SKODUN GATE: FAIL(2) internal error: {e!r}"),
                       "error")
