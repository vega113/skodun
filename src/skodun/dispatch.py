"""Background dispatch: the pre-push shim, the dispatcher, the worker, and the
evidence and rules behind skipping a review.

THE SHAPE OF THE WHOLE THING, because the ordering is the safety argument
-----------------------------------------------------------------------
A `git push` runs the installed shim, which buffers the ref list to a temp file,
runs any hook that was there before skodun (with the same argv and the same
bytes, propagating its refusal), and only then feeds the same bytes to
`skodun dispatch`. Nothing about review machinery may block a push once the
foreign hook has passed -- including skodun failing to start -- so the shim turns
ANY dispatcher-process failure into a stderr warning and `exit 0`.

`skodun dispatch` then, per pushed branch: resolves the base, captures the ref
diff, gathers dedup evidence, and RESERVES a `running` record inside one
`BEGIN IMMEDIATE` transaction that also owns the authoritative dedup decision,
the audit row and the supersede. Only then does it spawn a detached worker and
conditionally attach its pid. Reservation strictly BEFORE the spawn is what makes
a zero-delay double push resolve to exactly one terminal reviewed record: SQLite's
write lock serialises the two transactions, and the second one supersedes the
first's row.

`skodun worker` re-derives the diff, refuses to review content whose identity
moved under it, runs the pipeline, and applies its answer through ONE conditional
`finalize_review` -- so a superseded or stale-recovered record can never be
overwritten by a late worker. A SIGTERM sets a cancellation token rather than
killing the worker outright, because the model CLI runs in its own process group
and a bare death would orphan it.

A push that a bypass disables, a ref that is not an updated branch, and a diff
with nothing in it are the only paths that leave no record at all. Everything
else -- a config that will not load, a git call that fails, a spawn that fails,
an identity that moved -- leaves a fully-shaped `failed` review record, because
Task 12 delivers what the store says and silence is indistinguishable from a
clean review.

DEDUP: A PUSH THAT HAS ALREADY BEEN REVIEWED
--------------------------------------------
That is the
only optimisation in skodun with the power to answer "this content is fine"
without a model ever seeing it, so it is split into two halves that fail in
opposite directions:

  * **Evidence** (this module, `build_dedup_evidence`) is gathered OUTSIDE any
    transaction. It computes ONE fact — what the pushed commit's packed context
    hashes to right now — and it is never a verdict. A racing dispatcher can
    finalize a trustworthy review a millisecond after we look, so anything
    decided out here is stale by the time it is used.
  * **The decision** belongs to the reservation transaction (Task 10's
    `store.reserve_prepush`), which owns the match query, the full artifact
    validation, the suppression and the audit row. It calls
    `evidence_permits_suppression` inside its own lease.

Nothing here persists anything, reads the store, or writes a file. The `store`
argument to `build_dedup_evidence` is accepted for exactly that reason: see its
docstring.

WHAT `context_hash` MEANS, WHICH IS THREE DIFFERENT THINGS
---------------------------------------------------------
Suppression requires that the stored review saw the same MODEL INPUT, which is
the diff plus the packed file context. The diff half is `diff_hash`. The context
half is `context_hash`, and the shipped record has three states for it, which
this module keeps apart because they are not equally trustworthy:

  * **key absent, or JSON `null`** — no context was recorded at all. Only a
    legacy import produces this (`legacy_import._import_index` merges a foreign
    index row and artifact verbatim, and the oracle omitted the field entirely
    before it packed context: 4420 of the 7355 records in the oracle's own
    corpus have no such key). Transitional, and suppresses on the diff hash
    alone — there is no context to disagree about.
  * **a non-empty string** — a real packed-context identity. Suppresses only
    against an equal candidate hash.
  * **`""`** — AMBIGUOUS, and therefore never suppresses. The shipped pipeline
    writes `""` when packing was disabled (`pipeline.py:1173`) and on every
    early record shape, the store writes `""` into its column for a missing
    value, and Task 8's batched aggregate writes it by construction. It cannot
    distinguish "context was compared and was empty" from "nobody looked", and
    ambiguity must not skip a review. The cost is a redundant re-review of a
    packing-disabled or oversized push; the alternative is certifying content
    nothing ever compared.

The rules read the ARTIFACT, never the `reviews.context_hash` COLUMN: the column
holds `""` for a missing value (`store.save_review` binds
`rec.get("context_hash", "")`), so an absent key and an explicit `""` are
indistinguishable there — the column cannot express the first state at all.

ORACLE PARITY (`$SKODUN_ORACLE_DIR/scripts/grok-prepush-review.sh`)
------------------------------------------------------------------
The oracle's `diff_hash_has_successful_review` (208–294: the contract comment
plus the embedded probe) and its dispatcher call site (4721–4787) are a 3-way
protocol: probe the index, and pack the candidate's context ONLY when some
trustworthy match carries a hash (exit 2).
`tests/test_dispatch.py` drives that protocol through the oracle's own
`--dedup-check` seam and compares it with the rules here. Three differences,
all recorded there and in the plan's deviations:

  1. **Eager packing.** The oracle probes first to avoid packing on every
     force-push; skodun packs once, unconditionally, because the probe's answer
     could not be trusted out here anyway (see above) and the transaction is
     where the match is looked up. Cost: one pack per dispatched ref. The
     `store` parameter is where a lazy pre-probe would go if that cost ever
     matters.
  2. **JSON `null` is the legacy state here**, where the oracle's `or ""`
     coercion makes it the empty state. No oracle writer emits `null`, so this
     is unobserved there; skodun states the rule because a legacy import can
     carry any spelling a foreign archive used.
  3. **`""` never suppresses here**, where the oracle's kill-switch path (an
     empty candidate hash passed directly, no probe) suppresses against an
     empty record. Deliberate, fail-closed, and owner-ratified.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import budget, contextpack, promptbuild

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config, Defaults, Dispatch, Reviewer
    from .gitio import Base, Diff
    from .store import Store

#: The three states of an artifact's `context_hash`, as classified by
#: `artifact_context_state`. They are strings rather than an enum because they
#: appear in assertions and diagnostics, and because the vocabulary is closed by
#: the classifier, not by a type.
CONTEXT_LEGACY: Final = "legacy"        # key absent or JSON null
CONTEXT_HASHED: Final = "hashed"        # a non-empty identity string
CONTEXT_AMBIGUOUS: Final = "ambiguous"  # `""`, or anything unusable


def _note(message: str) -> None:
    """Progress and diagnostics go to stderr; stdout is the verdict's alone.

    Deliberately a local copy of `pipeline._note` rather than an import of it:
    this module is the dispatcher's entry point and must not drag the whole
    foreground review pipeline into its import graph for four lines. The
    broken-stderr guard is the load-bearing part — a closed stderr in a detached
    pre-push hook must never be what fails a review.
    """
    try:
        print(f"skodun: {message}", file=sys.stderr, flush=True)
    except BaseException:
        pass


@dataclass(frozen=True)
class DedupEvidence:
    """What the dispatcher learned about the candidate, before any transaction.

    * `enabled` — dedup is configured on. False means no suppression is even
      considered: the reservation goes straight through.
    * `valid` — the evidence is usable. False is the fail-closed value and means
      "we could not establish the candidate's context", which is NOT the same as
      "the candidate has no context" (that is `valid=True` with a `None` hash).
      Invalid evidence can never suppress anything.
    * `candidate_context_hash` — the packed-context identity of the pushed
      commit, or None when a review of it would not pack context at all.

    Frozen, and both fail-closed fields default to the safe answer, so a
    partially-filled value cannot suppress a review by omission.
    """

    enabled: bool
    valid: bool = False
    candidate_context_hash: str | None = None


def _classify(artifact: object) -> tuple[str, str | None]:
    """The state of `artifact`'s `context_hash`, and the hash if it has one.

    One read of the mapping, shared by both public rules: `artifact` arrives as
    a JSON-loaded artifact, and a second `artifact["context_hash"]` would be a
    second chance for the classified state and the compared value to disagree.
    """
    if not isinstance(artifact, Mapping):
        return CONTEXT_AMBIGUOUS, None
    if "context_hash" not in artifact:
        return CONTEXT_LEGACY, None
    value = artifact["context_hash"]
    if value is None:
        return CONTEXT_LEGACY, None
    if isinstance(value, str) and value.strip():
        return CONTEXT_HASHED, value
    return CONTEXT_AMBIGUOUS, None


def artifact_context_state(artifact: object) -> str:
    """Which of the three `context_hash` states `artifact` is in.

    Fail-closed and total: anything that is not a mapping, and any value that is
    neither None nor a non-blank string (a number, a list, a bare `""`, spaces)
    is `CONTEXT_AMBIGUOUS` — the state that never suppresses. A malformed
    artifact must not be able to look like the legacy state, which needs no hash
    comparison at all.

    A `bool` is rejected by the `isinstance(value, str)` test rather than
    accepted as an int-like: `True` is not a hash.
    """
    return _classify(artifact)[0]


def context_permits_suppression(artifact: object,
                                candidate_context_hash: object) -> bool:
    """Whether `artifact`'s context is compatible with the candidate's.

    PURE, and only one of the several conditions a suppression needs: the
    reservation transaction still has to match `diff_hash`, revalidate the
    artifact to the gate's standard, recompute the trust axes and require the
    same `base_sha`. This function answers the context question and nothing else.

    The comparison is exact string equality. A candidate hash that is None,
    blank or not a string cannot match a stored hash — a caller with no context
    identity has nothing to certify a context-bearing review with.
    """
    state, recorded = _classify(artifact)
    if state == CONTEXT_LEGACY:
        # No context was recorded, so there is nothing for the candidate's
        # context to contradict. The diff hash carries the identity alone.
        return True
    if state != CONTEXT_HASHED:
        return False
    if not isinstance(candidate_context_hash, str):
        return False
    if not candidate_context_hash.strip():
        return False
    return recorded == candidate_context_hash


def evidence_permits_suppression(artifact: object,
                                 evidence: DedupEvidence) -> bool:
    """The context gate the reservation transaction calls, evidence included.

    `enabled` and `valid` are tested with `is True`, not for truthiness: `1`,
    `"yes"` and a non-empty list are not permission to skip a review, and this
    is the one place where a sloppy bool would silently disable re-review. The
    `isinstance` guard is the same rule one level up: anything that is not the
    evidence type — a dict shaped like it, None — is no evidence at all.

    An INVALID probe is not a probe with no answer, it is no probe at all — so
    it does not get the legacy rule either. Spec §3's "any probe error, any
    ambiguity, any partial state ⇒ review" is literal: there is no code path
    from a failed evidence build to a suppression.
    """
    if not isinstance(evidence, DedupEvidence):
        return False
    if evidence.enabled is not True or evidence.valid is not True:
        return False
    return context_permits_suppression(artifact, evidence.candidate_context_hash)


def build_dedup_evidence(store: "Store", repo: Path, diff: "Diff", oid: str,
                         d: "Defaults", dedup_enabled: bool,
                         finder: "Reviewer | None" = None) -> DedupEvidence:
    """Pack the pushed commit's context once and report its identity.

    `store` is accepted and deliberately unused. The authoritative match query
    belongs inside the reservation transaction — a lookup out here could not be
    trusted, because a racing dispatcher may finalize a trustworthy review
    between this call and the lease. Keeping the parameter fixes the signature
    Task 10's dispatcher calls, and marks where the oracle's lazy pre-probe
    (pack only when some match carries a hash) would go if the cost of packing
    every dispatched ref ever became worth optimising. It is not read, and
    `tests/test_dispatch.py` passes a store that raises on any attribute access
    to keep it that way.

    The context is packed from the PUSHED COMMIT'S TREE (`source="oid"`), never
    from the working tree: the dispatcher runs from a pre-push hook, and the
    developer's checkout may already be somewhere else entirely. Reading it
    would certify content nobody pushed.

    The settings are the ones a review of this commit would use — the shipped
    single-shot pipeline's, `pipeline._single_shot`: the prompt's own leftover
    headroom, and `pack_large_added=False` because the pushed diff already
    carries every added file whole. A different headroom or a different
    large-added rule is a different identity for the same commit, so getting
    this wrong would not fail loudly, it would silently stop ever matching.

    `finder` is that review's HEAD reviewer, and it is part of those settings
    now that the envelope is per-provider (`budget.prompt_budget`): the worker
    packs against ITS finder's envelope, so a candidate hash computed from the
    global would never match again for any reviewer whose envelope differs from
    it. `None` means "no finder in hand" and answers the global, which is what
    the pre-change behaviour was.

    `d.context_pack` off is a REVIEW SETTING, not a failure: such a review
    records no context hash, so the candidate has none either (`valid=True`,
    hash None). A legacy candidate can still suppress; a context-bearing one
    cannot.

    ANY failure — an exception anywhere on the path, or a pack that comes back
    without a usable hash (the oracle's own `[ -n "$GR_CONTEXT_HASH" ]` guard,
    lines 4760 and 4780) — yields `valid=False` with a stderr note, and invalid
    evidence never suppresses anything. `Exception`, not `BaseException`: a
    `KeyboardInterrupt`, a `SystemExit` or Task 10's `ReviewCancelled` are the
    operator or the process asking to stop, not evidence that failed, and
    swallowing them here would turn a cancellation into a review.

    An oversized diff deserves a remark. Its headroom is zero, so the pack is
    empty and the candidate hash is the empty body's — degenerate, but harmless:
    such a push is reviewed in batches, and a batched aggregate records
    `context_hash=""`, which never suppresses. A same-diff unbatched record
    would be `diff_truncated`, hence untrustworthy, hence never a candidate.
    """
    if dedup_enabled is not True:
        # Not merely `if not dedup_enabled`: the caller passes a config bool, and
        # a non-bool that happens to be truthy must not turn dedup on.
        return DedupEvidence(enabled=False)
    try:
        if not d.context_pack:
            return DedupEvidence(enabled=True, valid=True,
                                 candidate_context_hash=None)
        headroom = promptbuild.context_headroom(
            budget.prompt_budget(d, finder), len(diff.data), packing=True)
        pack = contextpack.pack(Path(repo), list(diff.files), dict(diff.statuses),
                                headroom, source="oid", oid=oid,
                                pack_large_added=False)
        candidate = pack.sha256
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(
                f"context pack produced no identity hash: {candidate!r}")
    except Exception as exc:
        _note(f"dedup evidence unavailable for {oid or '<no oid>'} "
              f"({type(exc).__name__}: {exc}); this push will be reviewed")
        return DedupEvidence(enabled=True, valid=False)
    return DedupEvidence(enabled=True, valid=True, candidate_context_hash=candidate)


# ===========================================================================
# REF PARSING: the pure stage
# ===========================================================================
#
# Separated from everything with a side effect, and classified HERE, because the
# bypass checks below run before the config is even read and still have to be
# able to say how many refs they discarded -- and because the config-failure path
# writes one record per ACTIONABLE ref and must not invent records for tags.


_HEADS = "refs/heads/"

#: Git's pre-push protocol sends this as an oid to mean "no such object": a
#: deletion on the local side, or a branch that does not exist yet on the remote.
#: Matched by SHAPE rather than against a 40-character literal, because a
#: sha256 repository's null oid is 64 characters long and a length-pinned
#: comparison would silently classify a deletion as an update.
def _is_zero(oid: str) -> bool:
    return bool(oid) and set(oid) == {"0"}


@dataclass(frozen=True)
class Ref:
    """One line of git's pre-push stdin, classified.

    `<local ref> <local oid> <remote ref> <remote oid>`, plus the two derived
    facts every later stage needs: whether this ref is ACTIONABLE and, if not,
    why -- so the stderr note and the "no record for this" decision come from
    the same classification rather than from two re-readings of the raw fields.

    `branch` is the SHORT name (`refs/heads/feat` -> `feat`), which is the label
    every downstream surface uses: the store's index column, the supersede query,
    the banner, Task 12's delivery. It is `""` for a non-actionable ref.
    """

    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str
    branch: str = ""
    actionable: bool = False
    skip_reason: str = ""


def parse_ref_lines(text: str) -> list[Ref]:
    """Every line of a pre-push stdin, classified. PURE: no git, no store, no io.

    Actionable means exactly one thing: a non-deletion update of a
    `refs/heads/*` ref. Everything else is skipped with a reason and NEVER given
    a record --

      * **deletions** (a zero local oid) have no content to review;
      * **tags and other ref classes** (`refs/tags/*`, `refs/notes/*`, a bare
        `HEAD`) are not branches, and `branch` is the identity every downstream
        surface keys on -- the gate, the supersede query and the delivery all ask
        "what is the newest review of THIS branch";
      * **malformed lines** -- fewer than four fields. Git always sends four;
        anything else came from a hand-run hook or a corrupted pipe, and guessing
        which fields are missing would mean reviewing an oid we inferred.

    Deletion is tested BEFORE the ref class, so a deleted tag reads as a
    deletion. The two reasons are both true of it and the message is a
    diagnostic, not a decision -- but the order is fixed here rather than left
    to reading order, because the oracle's own loop skips the zero oid first
    (`grok-prepush-review.sh:4677`).

    Blank lines are dropped entirely rather than reported: a trailing newline is
    not a ref anybody tried to push.
    """
    refs: list[Ref] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) < 4:
            refs.append(Ref(*(fields + [""] * (4 - len(fields))),
                            skip_reason=f"malformed pre-push line {raw!r} "
                                        f"({len(fields)} field(s), expected 4)"))
            continue
        local_ref, local_oid, remote_ref, remote_oid = fields[:4]
        if _is_zero(local_oid):
            refs.append(Ref(local_ref, local_oid, remote_ref, remote_oid,
                            skip_reason=f"{remote_ref or local_ref} is a "
                                        f"deletion; nothing to review"))
            continue
        if not local_ref.startswith(_HEADS) or len(local_ref) <= len(_HEADS):
            refs.append(Ref(local_ref, local_oid, remote_ref, remote_oid,
                            skip_reason=f"{local_ref} is not a branch "
                                        f"({_HEADS}*); not reviewed"))
            continue
        refs.append(Ref(local_ref, local_oid, remote_ref, remote_oid,
                        branch=local_ref[len(_HEADS):], actionable=True))
    return refs


# ===========================================================================
# THE BYPASSES
# ===========================================================================


#: Set to exactly `1` to skip review for ONE push. A per-invocation escape hatch
#: (`SKODUN_PREPUSH_SKIP=1 git push`), matched against the literal so a stray
#: `SKODUN_PREPUSH_SKIP=0` cannot read as truthy.
SKIP_ENV = "SKODUN_PREPUSH_SKIP"

#: The per-repo, persistent switch: `git config skodun.prepush false`.
GIT_CONFIG_KEY = "skodun.prepush"


def bypass_reason(repo: Path) -> str | None:
    """Why review is disabled for this push, or None.

    Checked BEFORE the config is loaded, and that ordering is the whole point: a
    project whose `.skodun.toml` is broken must still be pushable with the
    bypass, and a bypass that needed a working config to be read would be exactly
    unavailable in the situation it exists for.

    `git config --bool` is read through a guarded subprocess: a git that will not
    run is not a bypass (it is a problem the dispatch below will report on its own
    terms), so an unreadable config reads as "not disabled".
    """
    if os.environ.get(SKIP_ENV) == "1":
        return f"{SKIP_ENV}=1 for this push"
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "config", "--bool", GIT_CONFIG_KEY],
            capture_output=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if cp.returncode == 0 and cp.stdout.decode("utf-8", "replace").strip() == "false":
        return f"git config {GIT_CONFIG_KEY} is false in this repository"
    return None


def _disabled_note(reason: str, discarded: int) -> None:
    """The ONE stderr shape every disablement uses.

    `[dispatch] enabled = false` and `git config skodun.prepush false` are
    parallel switches -- either alone disables review -- so they are reported
    identically but for the clause that names which one it was. A reader tailing
    a push's stderr must not have to know which layer turned it off to recognise
    that it is off.
    """
    _note(f"pre-push review disabled ({reason}); "
          f"{discarded} ref(s) discarded, the push is not blocked")


# ===========================================================================
# DURABLE FAILURE RECORDS
# ===========================================================================


def failed_record(branch: str, reason: str, *, head: str = "",
                  base_ref: str = "", base_sha: str = "",
                  diff_hash: str = "", repo: str | None = None,
                  now: str | None = None,
                  record_id: str | None = None) -> dict:
    """A fully-shaped `failed` prepush record for a failure with no review.

    Every dispatch failure after ref parsing gets one of these -- a config that
    will not load, a git call that fails, a spawn that fails. They exist because
    Task 12 delivers what the STORE says, and a push whose review machinery broke
    silently is indistinguishable from a push that was reviewed and found clean.

    Branch-shaped on purpose: `mode="prepush"` and the real branch, so it lands
    in the same query the delivery and the gate use. `diff_hash` is `""` when no
    identity was ever computed -- never a guess, and never omitted, because the
    column is what dedup matches on and an invented value would suppress a real
    review.

    `parse_ok=False` with the other two axes False is what makes it untrustworthy
    at the chokepoint (`is_trustworthy` recomputes), and `usable_output=False`
    says the round produced no answer at all, which is the whole difference
    between this and a clean review with no findings.
    """
    from . import ids, pipeline
    return dict(
        pipeline._empty_shell(),
        id=record_id or ids.new_review_id(), reviewed_at=now or _iso_now(),
        source="skodun", branch=branch, head=head, base_ref=base_ref,
        base_sha=base_sha, diff_hash=diff_hash, mode="prepush",
        status="failed", parse_ok=False, degraded=False, diff_truncated=False,
        usable_output=False, failure_reason=reason, worst_runtime_sec=None,
        pid=None, superseded_by=None, repo=repo,
    )


def _iso_now() -> str:
    from .store import _TS_FORMAT
    return time.strftime(_TS_FORMAT, time.gmtime())


def _repo_scope(repo: Path) -> str | None:
    """`repo`'s git common dir for a failure record, or None if git cannot say.

    Best-effort on purpose: this is only ever called on the failure path, where
    raising would cost the durable record entirely. None is honest -- the row is
    then invisible to a scoped `surface`, the same fate as a pre-v5 row and
    strictly better than no row at all.
    """
    from . import gitio
    try:
        return str(gitio.git_common_dir(repo))
    except BaseException as e:      # pragma: no cover - defensive
        _note(f"could not identify the repository for a failure record: {e!r}")
        return None


def _record_failure(store: "Store", branch: str, reason: str, **identity) -> None:
    """Write one `failed_record`, and never let failing to do so raise.

    Best-effort by necessity: this is already the failure path, the push is over,
    and `dispatch` exits 0 regardless. A store that cannot be written costs the
    record (and says so on stderr) rather than costing the exit contract.
    """
    _note(f"{branch or '<unknown branch>'}: {reason}")
    try:
        store.save_review(failed_record(branch, reason, **identity))
    except BaseException as e:      # pragma: no cover - defensive
        _note(f"could not record that failure: {e!r}")


# ===========================================================================
# THE RESERVATION BUDGET
# ===========================================================================


def reservation_defaults(d: "Defaults", dispatch: "Dispatch") -> "Defaults":
    """The `Defaults` whose arithmetic sizes the RESERVATION's stale ceiling.

    NOT the worker's effective defaults, and the difference is deliberate. The
    reservation happens before any prompt exists, so it cannot know whether the
    large-prompt escalation will apply (see `pipeline._escalated`) -- and the two
    errors are not symmetric: a ceiling that is too generous only delays stale
    recovery, while one that is too small makes `recover_stale` reclaim a worker
    that is still running and publish `failed` over a live review.

    So both figures take the MAX of the background and foreground settings:

    * `timeout_sec` -- the max, and NOT simply "the foreground cap", because a
      custom config may legitimately set `[dispatch] timeout_sec` ABOVE
      `[defaults] timeout_sec` and the ceiling has to cover the larger one.
    * `timeout_retries` -- the max too. The brief names only `timeout_sec` here,
      but `budget.attempt_budget` multiplies by `1 + timeout_retries +
      degraded_retries`, so a config with MORE background retries than foreground
      ones would be under-budgeted by exactly the retries it added -- the
      undersized-ceiling failure, arrived at through the other factor. Recorded
      as a deviation in the plan; it can only ever widen the ceiling.

    Every other key is `[defaults]`, untouched.
    """
    return replace(d,
                   timeout_sec=max(dispatch.timeout_sec, d.timeout_sec),
                   timeout_retries=max(dispatch.timeout_retries,
                                       d.timeout_retries))


def effective_defaults(d: "Defaults", dispatch: "Dispatch") -> "Defaults":
    """The `Defaults` the WORKER reviews under: the background budget, exactly.

    `replace(defaults, timeout_sec=dispatch.timeout_sec,
    timeout_retries=dispatch.timeout_retries)`. Nothing is maxed here: the whole
    point of `[dispatch]` is that a detached worker nobody is waiting for gets a
    tighter cap than a foreground review, and taking a max would silently ignore
    the tighter setting. The per-prompt escalation (`pipeline._escalated`) is the
    one thing that raises it, and only for a prompt over
    `large_prompt_bytes`.
    """
    return replace(d, timeout_sec=dispatch.timeout_sec,
                   timeout_retries=dispatch.timeout_retries)


def reserved_budget(cfg: "Config", diff_bytes: bytes) -> int:
    """`worst_runtime_sec` for the record this push is about to reserve.

    The batch count comes from the SAME planner the worker will run
    (`pipeline.batch_plan`), because a multi-batch review makes `batch_count + 1`
    sequential reviewer runs and a ceiling sized for one would invite
    `recover_stale` to reclaim a live worker halfway through batch three. The
    planner reads `context_pack` and `budget.prompt_budget` of the FINDER, which
    the reservation and effective defaults share, so both agree on the count —
    the finder is passed here for exactly that reason: sizing the reservation
    from the global while the worker plans from the finder's own envelope would
    put the two counts out of step on every argv-bound provider.
    """
    from . import pipeline
    d = reservation_defaults(cfg.defaults, cfg.dispatch)
    plan = pipeline.batch_plan(diff_bytes, d, pipeline._reviewer_for(cfg, "finder"))
    return budget.worst_runtime(d, pipeline.max_chain_width(cfg),
                                0 if plan is None else len(plan))


# ===========================================================================
# THE DETACHED WORKER: spawn, env allowlist, pid-reuse guard
# ===========================================================================


#: The two tokens a live worker's `ps -o args=` must BOTH contain before it is
#: signalled. `skodun` alone would match a `skodun review` a human is watching,
#: and `worker` alone would match anything. Together they name this package's
#: worker entrypoint in every invocation form it has (`python -m skodun worker`,
#: `skodun worker`, `<venv>/bin/skodun worker`).
WORKER_ARGV_TOKENS: Final = ("skodun", "worker")

#: The flag `worker_argv` writes the record id behind, and the flag the pid-reuse
#: guard reads it back out of. ONE definition for the same reason the tokens above
#: are one: the spawn and the guard must not be able to drift apart.
WORKER_RECORD_FLAG: Final = "--record-id"

#: The environment variables a detached worker inherits, beyond `SKODUN_*`.
#: An ALLOWLIST rather than a filter, because a pre-push hook inherits whatever
#: the developer's shell had -- a `GIT_INDEX_FILE` or `GIT_DIR` left over from the
#: push would silently repoint every git call the worker makes at the wrong
#: repository, and a `PYTHONSTARTUP`/`PYTHONWARNINGS` can change what the
#: interpreter does before `main` is reached.
_WORKER_ENV_KEYS: Final = ("PATH", "HOME")


def worker_env(store_path: Path) -> dict:
    """The environment a detached worker runs with. An allowlist, plus three
    values this process computes rather than inherits.

    * **`PATH`, `HOME`** -- the model CLI is found through the first and keeps its
      credentials/settings under the second (`~/.grok/settings.json`). Dropping
      either would make every worker report the provider as unavailable.
    * **`LANG`/`LC_ALL` forced to `C.UTF-8`** -- not inherited. The worker's
      stderr goes to a log file and its diffs carry arbitrary bytes; an inherited
      ASCII locale turns a non-ASCII filename in a progress line into a
      `UnicodeEncodeError` inside a detached process nobody is watching.
    * **every `SKODUN_*`** -- the whole configuration surface, including the
      per-adapter `SKODUN_<X>_BIN` overrides a test or an operator set.
    * **`SKODUN_DB` pinned to `store_path`** -- explicitly, even when the parent
      inherited none, so the worker cannot resolve a DIFFERENT default store than
      the one holding its reservation.
    * **`PYTHONPATH` computed from THIS module's location** -- never inherited.
      `python -m skodun` has to be able to import the package, and a source
      checkout (or any non-installed layout, which includes the whole test suite)
      is only importable through it. Computing it means the worker runs the SAME
      code as the dispatcher that spawned it; inheriting it would let a stale
      entry shadow that. DEVIATION from the brief's allowlist, recorded in the
      plan: without it a source checkout cannot dispatch at all.
    """
    env = {k: os.environ[k] for k in _WORKER_ENV_KEYS if k in os.environ}
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env.update({k: v for k, v in os.environ.items() if k.startswith("SKODUN_")})
    env["SKODUN_DB"] = str(store_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def worker_argv(record_id: str, repo: Path, branch: str, local_oid: str,
                base_sha: str, base_ref: str) -> list[str]:
    """The worker's exact argv. ONE definition, so the pid-reuse guard's tokens
    and the spawn cannot drift apart."""
    return [sys.executable, "-m", "skodun", "worker",
            WORKER_RECORD_FLAG, record_id, "--repo", str(repo),
            "--branch", branch, "--local-oid", local_oid,
            "--base-sha", base_sha, "--base-ref", base_ref]


def spawn_worker(store: "Store", record_id: str, repo: Path, branch: str,
                 local_oid: str, base_sha: str, base_ref: str,
                 store_path: Path) -> subprocess.Popen:
    """Start the detached worker for an already-reserved record.

    `start_new_session=True` makes it a session leader, so it survives the push
    and the terminal that ran it, and so a SIGTERM aimed at its pid reaches it
    rather than the whole hook's group. stdin is `DEVNULL` (a worker that read the
    push's stdin would consume the ref list) and stdout is `DEVNULL` (nothing
    reads it; the record is the output). stderr goes to
    `store.log_dir()/<record_id>.log`, which is derivable from the store alone --
    the worker only ever learns `SKODUN_DB`, so any other location would need a
    second piece of configuration that could disagree.

    The log file descriptor is closed in this process immediately: the child holds
    its own, and a dispatcher handling several refs must not accumulate one per
    worker.
    """
    log = store.log_dir() / f"{record_id}.log"
    handle = open(log, "ab")
    try:
        return subprocess.Popen(
            worker_argv(record_id, repo, branch, local_oid, base_sha, base_ref),
            cwd=str(repo), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=handle, env=worker_env(store_path))
    finally:
        handle.close()


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM a worker we just started and REAP it. Never raises.

    The same signal the supersede path sends, and therefore the same cascade: the
    worker's handler sets its cancellation token, the watchdog takes the provider's
    process group down, and the pipeline's `finally` runs. Reaping matters as much
    as signalling -- a dispatcher that left zombies behind would accumulate one per
    failed attach for as long as the push's process lives, and the test suite
    would leak children.

    A child that died BEFORE installing its handler is exactly why the caller
    still finalizes the reservation itself: this signal is best-effort by nature.
    """
    try:
        proc.terminate()
    except BaseException:                    # pragma: no cover - defensive
        # `BaseException`, because this function's docstring promises "never
        # raises" and the callers depend on it: both of them are already on a
        # failure path, and an exception here would skip the durable-record step
        # that is the whole point of being on it.
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:        # pragma: no cover - defensive
        try:
            proc.kill()
            proc.wait(timeout=10)
        except BaseException:
            pass
    except BaseException:                    # pragma: no cover - defensive
        pass


def pid_is_skodun_worker(pid: object, record_id: object) -> bool:
    """Whether `pid` is demonstrably still THE worker of `record_id` (ORACLE A14.4).

    The pid-reuse guard. `kill -0` only proves SOME process owns the pid, and a
    `running` marker can be minutes old -- long enough for the kernel to have
    recycled it onto an unrelated same-user process. Signalling on liveness alone
    would eventually SIGTERM a developer's editor.

    `record_id` is what BINDS the answer to the reservation being retired, and it
    is not optional. The argv tokens alone say "this is a skodun worker" and not
    WHICH one -- so the one process class the guard still let through was another
    skodun worker: a pid recycled onto a live review of a different branch was
    confirmed and signalled, killing a run nothing had superseded. The argv must
    now also carry `--record-id <record_id>` as `worker_argv` writes it, which is
    a fact about this row and no other.

    Matched as the FLAG AND THE VALUE, and with the value's end pinned: `sk_x`
    must not confirm a worker running `sk_x2`. Ids share a branch-and-oid stem,
    so being a prefix of one another is ordinary rather than exotic.

    An UNCONFIRMABLE pid gets no signal at all, and that is safe rather than
    lax precisely because finalization is conditional: the reservation transaction
    has already marked the row `superseded`, so if that worker really is alive it
    will finish, call `finalize_review`, be told the record is no longer running,
    and change nothing. Binding to the record only ever REMOVES signals, so it
    cannot weaken that posture -- it lands more rows in the same safe case.

    Total: a `ps` that cannot be run, a non-integer pid, a pid of 0 or below, a
    `record_id` that is not a non-empty string, all answer False. There is no path
    from "we could not tell" to "signal it".
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(record_id, str) or not record_id:
        return False
    try:
        # `-ww`: UNLIMITED width, and load-bearing rather than tidy. Linux
        # procps truncates `-o args=` to the terminal width -- 80 columns when
        # there is no tty, which is every context skodun runs in -- while BSD
        # `ps` on macOS does not. A real worker argv carries `--repo <absolute
        # path>` after `--record-id <id>`, so it clears 80 columns easily, and
        # the truncated line drops exactly the flag this guard binds on. That
        # reads as "not the worker of this record" and the SIGTERM is silently
        # withheld, which is the fail-closed direction and therefore invisible.
        cp = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)],
                            capture_output=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0:
        return False
    args = cp.stdout.decode("utf-8", "replace")
    if not all(token in args for token in WORKER_ARGV_TOKENS):
        return False
    return _argv_names_record(args, record_id)


def _argv_names_record(args: str, record_id: str) -> bool:
    """Whether `args` (one `ps -o args=` line) carries `--record-id <record_id>`.

    A plain `in` test would match a longer id that merely STARTS with this one,
    which is why the character after the value is checked: the value must end the
    argv or be followed by whitespace, exactly as `worker_argv`'s list-form spawn
    leaves it.
    """
    needle = f"{WORKER_RECORD_FLAG} {record_id}"
    start = 0
    while True:
        at = args.find(needle, start)
        if at < 0:
            return False
        end = at + len(needle)
        if end == len(args) or args[end].isspace():
            return True
        start = at + 1


def signal_superseded(retired) -> int:
    """SIGTERM the workers of the rows this reservation retired. ORACLE A14.4.

    `retired` is what the reservation transaction RETURNED -- never a fresh query,
    which would race: a third dispatcher committing in between would have retired
    our own row too, and we would signal our own worker.

    A row with a NULL pid is a reservation whose worker never attached one (it may
    not even have been spawned), so there is nothing to signal; a row whose pid
    cannot be confirmed as the live worker OF THAT ROW is left alone (see
    `pid_is_skodun_worker`, which takes the row's own id for exactly this reason).
    Returns how many signals were actually sent, which is what the tests count.
    """
    sent = 0
    for row in retired or ():
        pid = row.get("pid") if isinstance(row, Mapping) else None
        record_id = row.get("id") if isinstance(row, Mapping) else None
        if not pid_is_skodun_worker(pid, record_id):
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError, ValueError):
            continue
        sent += 1
        _note(f"superseded an older running review ({row.get('id')}); "
              f"signalled its worker (pid {pid})")
    return sent


# ===========================================================================
# THE DISPATCHER
# ===========================================================================


def run_dispatch(stdin_text: str, repo: Path, store_path: Path, *,
                 remote_name: str = "", remote_url: str = "") -> int:
    """Dispatch one push. ALWAYS returns 0.

    A hook must never block on review machinery, so every failure below is a loud
    stderr line plus a durable `failed` record -- never silence, never a blocked
    push. Only `dispatch`'s own USAGE errors are non-zero, and those are argparse's,
    one level up in `cli`.

    THE STARTUP ORDER IS FIXED, and each step is where it is for a reason:

    1. **Parse and classify every ref line** (`parse_ref_lines`, pure). Before any
       git call and before the config, so the steps below can say how many refs
       they discarded and can write records for exactly the actionable ones.
    2. **The bypasses**, before the config load. A project whose `.skodun.toml`
       is broken must still be pushable with `SKODUN_PREPUSH_SKIP=1` or
       `git config skodun.prepush false`; a bypass that needed a working config
       would be unavailable in the one situation it exists for. The store is not
       even opened.
    3. **Open the store and load the config.** A config-load failure writes ONE
       branch-shaped `failed` record per ACTIONABLE ref (`diff_hash=""`, the
       reason) and exits 0. The store is located by `SKODUN_DB`, which does not
       depend on the config -- that is what makes recording this failure possible
       at all.
    4. **`[dispatch] enabled`.** False discards every ref with one stderr note:
       no capture, no reservation, no worker, no record. The config-level twin of
       the per-repo git-config bypass.
    5. **Sweep stale records**, then dispatch each actionable ref.

    `remote_name`/`remote_url` are git's standard pre-push argv. They are recorded
    into failure notes and otherwise unused; `dispatch` accepts them because
    without them argparse would reject the shim's own invocation.
    """
    from . import pipeline
    from .store import Store

    refs = parse_ref_lines(stdin_text)
    for ref in refs:
        if not ref.actionable:
            _note(f"skipping: {ref.skip_reason}")
    actionable = [r for r in refs if r.actionable]

    reason = bypass_reason(repo)
    if reason is not None:
        _disabled_note(reason, len(actionable))
        return 0
    if not actionable:
        return 0

    where = f" (remote {remote_name or '?'} {remote_url or ''})".rstrip()
    try:
        store = Store.open(store_path)
    except BaseException as e:
        # No store means nowhere to record anything, which is why this is the one
        # dispatch failure that leaves only a stderr line.
        _note(f"could not open the review store at {store_path}: {e!r}; "
              f"{len(actionable)} ref(s) were not reviewed{where}")
        return 0

    with store:
        try:
            cfg = _load_config(repo)
        except BaseException as e:
            for ref in actionable:
                _record_failure(
                    store, ref.branch,
                    f"the skodun config could not be loaded, so no review "
                    f"ran{where}: {e!r}", head=ref.local_oid,
                    repo=_repo_scope(repo))
            return 0

        if cfg.dispatch.enabled is not True:
            _disabled_note("[dispatch] enabled = false", len(actionable))
            return 0

        try:
            pipeline.recover_stale(store, cfg)
        except BaseException as e:      # pragma: no cover - defensive
            _note(f"could not sweep stale reviews: {e!r}")

        for ref in actionable:
            try:
                _dispatch_ref(store, store_path, repo, cfg, ref, where)
            except BaseException as e:
                # Per REF, so one broken branch in a multi-ref push does not cost
                # the others their review.
                _record_failure(
                    store, ref.branch,
                    f"the review could not be dispatched{where}: {e!r}",
                    head=ref.local_oid, repo=_repo_scope(repo))
    return 0


def _load_config(repo: Path):
    """`load_config` against the WORKTREE ROOT, exactly as the gate resolves it.

    A hook can be invoked from a subdirectory, and `load_config` reads
    `<its argument>/.skodun.toml` -- so passing the raw cwd would silently review
    under the DEFAULT config whenever the push was run from anywhere but the top
    level, which is a different diff envelope, a different checklist and a
    different timeout than the gate will judge it by. `cli._repo_root` states the
    whole argument.
    """
    from . import gitio
    from .config import load_config
    return load_config(gitio._worktree_root(repo))


def resolve_dispatch_base(repo: Path, ref: Ref) -> "Base":
    """The base this pushed ref's diff is computed against.

    Two cases, and the split is what makes a force-push to an EXISTING branch
    review only what is new:

    * **The remote branch already exists** (a non-zero remote oid): the base is
      exactly what the remote has, `Base(ref=<the remote ref as pushed>,
      sha=<remote oid>)`. The remote ref STRING is what is persisted and what the
      worker is told, because that is the name a human reading the record needs to
      see the review's scope.
    * **A new branch** (a zero remote oid): there is no remote side to compare
      against, so `gitio.resolve_ref_base` picks the first existing main
      candidate with a merge-base -- and falls back to `<oid>^`, then the oid
      itself, with a loud warning.

    DIVERGENCE from the oracle, recorded in the plan: the oracle prefers
    `merge-base(<main candidate>, local_oid)` and only falls back to the remote
    oid when no main candidate resolves (`grok-prepush-review.sh:4681-4686`). That
    re-reviews every commit already on the remote branch on every force-push. The
    brief's rule -- the remote side wins when it exists -- reviews what is
    actually being added, and Task 5 built `resolve_ref_base` specifically for the
    other case.
    """
    from . import gitio
    if not _is_zero(ref.remote_oid) and ref.remote_oid:
        return gitio.Base(ref=ref.remote_ref or ref.local_ref,
                          sha=ref.remote_oid)
    base = gitio.resolve_ref_base(repo, ref.local_oid)
    if base.warning:
        _note(f"identity note for {ref.branch}: {base.warning}")
    return base


def _dispatch_ref(store: "Store", store_path: Path, repo: Path, cfg,
                  ref: Ref, where: str) -> None:
    """Reserve and dispatch ONE actionable ref. Raises on a failure it cannot
    record itself; `run_dispatch` turns that into a durable `failed` record."""
    from . import gitio, pipeline
    base = resolve_dispatch_base(repo, ref)
    diff = gitio.capture_ref_diff(repo, base.sha, ref.local_oid)
    if diff.data.rstrip(b"\n") == b"":
        # ORACLE PARITY (`[ -z "$DIFF" ] && continue`, 4690) and no record: there
        # is nothing to review, the gate PASSes an empty change before it ever
        # looks a review up, and an empty prompt could mint a clean verdict for a
        # diff nothing looked at. Recorded as a deviation (the brief is silent).
        _note(f"{ref.branch}: nothing outgoing vs {base.ref}; not reviewed")
        return
    diff_hash = gitio.diff_identity(diff.data)

    # OUTSIDE the transaction, on purpose: it is evidence, never a verdict. The
    # reservation lease owns the match query, because a racing dispatcher can
    # finalize a trustworthy review between this call and the lease.
    evidence = build_dedup_evidence(store, repo, diff, ref.local_oid,
                                    cfg.defaults, cfg.dispatch.dedup,
                                    pipeline._reviewer_for(cfg, "finder"))
    reservation = store.reserve_prepush(
        ref.branch, ref.local_oid, base.ref, base.sha, diff_hash,
        reserved_budget(cfg, diff.data), evidence,
        repo=str(gitio.git_common_dir(repo)))
    if reservation.record_id is None:
        _note(f"{ref.branch}: diff {diff_hash} is already covered by review "
              f"{reservation.suppressed_by}; skipping")
        return

    # AFTER the transaction and BEFORE the spawn: the retired rows are already
    # terminal in the database, so signalling first frees the inference backend
    # before the replacement worker asks for it -- the oracle's own order
    # (`supersede_same_branch` then `nohup ... &`).
    signal_superseded(reservation.superseded)

    record_id = reservation.record_id
    _note(f"{ref.branch}: reviewing {len(diff.files)} file(s) vs {base.ref} "
          f"as {record_id} in the background")
    try:
        proc = spawn_worker(store, record_id, repo, ref.branch, ref.local_oid,
                            base.sha, base.ref, store_path)
    except BaseException as e:
        # The record already exists, so the failure is a DEMOTION of it rather
        # than a second record. Conditional: a racing dispatcher may already have
        # superseded us, and its answer is the newer one.
        store.fail_if_running(
            record_id, f"the review worker could not be started{where}: {e!r}")
        _note(f"{ref.branch}: could not start the review worker: {e!r}")
        return

    try:
        attached = store.attach_pid(record_id, proc.pid)
    except BaseException as e:
        # An attach that RAISED leaves the pid unknown, so the child can never be
        # signalled by a later supersede -- terminate and reap it here, then make
        # the reservation terminal ourselves. Without this last step the durable
        # failure would wait for a stale sweep, which is a whole runtime budget
        # away.
        _terminate(proc)
        store.fail_if_running(
            record_id, f"the review worker's pid could not be recorded{where}: "
                       f"{e!r}")
        _note(f"{ref.branch}: could not record the worker's pid: {e!r}")
        return
    if not attached:
        # A racing dispatch superseded this reservation between the lease and the
        # spawn. The record is ALREADY terminal (that transaction wrote
        # `superseded` and `superseded_by`), so there is nothing to demote -- only
        # a child to stop before it reviews content whose record is settled and
        # overlaps the replacement review on one inference backend.
        _note(f"{ref.branch}: reservation {record_id} was superseded before its "
              f"worker attached; stopping it")
        _terminate(proc)


# ===========================================================================
# THE WORKER
# ===========================================================================


@dataclass(frozen=True)
class WorkerOutcome:
    """What one worker run concluded: a line for stdout and an exit code.

    Returned rather than printed so `cli` stays a pure seam -- it owns the
    broken-pipe-safe write (`_emit`) and the exit contract, and this module owns
    the decision. `code` is 0 for every orderly end (a review finalized, a
    cancellation recorded, a reservation found already terminal) and 2 for the
    two states where the worker could do nothing at all: no store, or no such
    reservation.
    """

    code: int
    message: str


def run_worker(record_id: str, repo: Path, branch: str, local_oid: str,
               base_sha: str, base_ref: str, store_path: Path) -> WorkerOutcome:
    """Review a reserved record and apply the answer. NEVER raises.

    The order below is the whole safety argument, and each step exists because
    the state it checks can have changed since the dispatcher spawned this
    process:

    1. **Install the SIGTERM handler FIRST**, before any work. A supersede
       landing during step 2 must set the token, not kill the process: the
       provider runs in its own process group and a bare death would orphan it
       (see `runner`'s cancellation notes). The previous handler is restored on
       the way out, which matters only when this is called in-process by a test.
    2. **Read the reservation.** A record that is gone, or that is no longer
       `running`, is a record this worker must not touch: a newer push already
       superseded it or a stale sweep already reclaimed it. Both are orderly.
    3. **Cross-check the argv identity against the stored row.** They should
       agree; a disagreement means the hook, the store or the argv were tampered
       with between the reservation and now, and reviewing under a base the
       record does not claim would publish the wrong scope at this id.
    4. **Re-derive the diff and verify `diff_identity`.** THE moved-push check:
       the ref can be force-pushed again between the reservation and this
       process's first git call, and the record's `diff_hash` is what dedup and
       the gate match on. Reviewing the new content under the old hash would
       certify a diff nobody reviewed.
    5. **Review** (`pipeline.run_prepush_review`, which persists nothing).
    6. **Check the token immediately before finalizing.** `run_prepush_review`
       cannot do this -- it does not persist, so there is nothing for a check
       inside it to protect.
    7. **ONE conditional `finalize_review`.**
    8. **The post-commit linearization check.** A signal landing DURING the
       SQLite call is unobservable to step 6, which injects before it.
    """
    from . import pipeline
    from .store import Store

    cancel = threading.Event()
    previous = _install_sigterm(cancel)
    try:
        try:
            store = Store.open(store_path)
        except BaseException as e:
            _note(f"could not open the review store at {store_path}: {e!r}")
            return WorkerOutcome(2, _banner_failure(
                f"the review worker could not open its store: {e!r}"))
        with store:
            try:
                return _work(store, cancel, record_id, repo, branch, local_oid,
                             base_sha, base_ref)
            except pipeline.ReviewCancelled as exc:
                # A SIGTERM landing after the last watchdog tick and outside
                # every checkpoint. Converted here into the SAME orderly
                # conditional failed finalize, so it can never surface as a
                # traceback out of a detached process nor leave the reservation
                # `running` for a stale sweep to find a whole budget later.
                return _record_cancellation(store, record_id, exc)
            except BaseException as e:
                # The review did not complete, so it certifies nothing -- and the
                # reservation must not be left `running`. Conditional: a
                # superseded record stays superseded.
                reason = f"the background review failed: {e!r}"
                _note(reason)
                try:
                    store.fail_if_running(record_id, reason)
                except BaseException:       # pragma: no cover - defensive
                    pass
                return WorkerOutcome(0, _banner_failure(reason))
    finally:
        _restore_sigterm(previous)


def _work(store: "Store", cancel: threading.Event, record_id: str, repo: Path,
          branch: str, local_oid: str, base_sha: str,
          base_ref: str) -> WorkerOutcome:
    """Steps 2-8 of `run_worker`. Raises; the caller owns every failure shape."""
    from . import gitio, pipeline
    from .trust import banner

    reserved = store.get_review(record_id)
    if reserved is None:
        _note(f"no reservation {record_id} in the store; nothing to review")
        return WorkerOutcome(2, _banner_failure(
            f"reservation {record_id} does not exist"))
    if reserved.get("status") != "running":
        _note(f"reservation {record_id} is already {reserved.get('status')!r}; "
              f"nothing to do")
        return WorkerOutcome(0, banner(reserved))

    mismatch = _identity_mismatch(reserved, branch=branch, head=local_oid,
                                  base_sha=base_sha, base_ref=base_ref)
    if mismatch:
        reason = (f"the worker was invoked with an identity the reservation does "
                  f"not claim ({mismatch}); no review ran")
        _note(reason)
        store.fail_if_running(record_id, reason)
        return WorkerOutcome(0, _banner_failure(reason))

    base = gitio.Base(ref=base_ref, sha=base_sha)
    diff = gitio.capture_ref_diff(repo, base_sha, local_oid)
    seen = gitio.diff_identity(diff.data)
    if seen != reserved.get("diff_hash"):
        # THE moved-push check. Fail closed and record it: the ref moved under
        # the reservation, so this content has no reserved record and the old
        # content has no review.
        reason = (f"the pushed content moved under this review: reserved "
                  f"diff_hash {reserved.get('diff_hash')!r}, the ref now hashes "
                  f"to {seen!r}; no review ran")
        _note(reason)
        store.fail_if_running(record_id, reason)
        return WorkerOutcome(0, _banner_failure(reason))

    cfg = _load_config(repo)
    d = effective_defaults(cfg.defaults, cfg.dispatch)
    rec = pipeline.run_prepush_review(store, repo, record_id, branch, local_oid,
                                      base, diff, d, cfg, cancel=cancel)

    # STEP 6. A token set between `run_prepush_review`'s own last checkpoint and
    # this line: the returned record can carry perfectly clean axes, and status
    # plus failure_reason alone would NOT make it untrustworthy -- the chokepoint
    # recomputes trust from the three axes only.
    boundary = "after the review returned, before it was recorded"
    if pipeline.runner._cancelled(cancel):
        rec = pipeline.cancellation_transform(rec, boundary)
        _note(f"cancelled {boundary}; recording an untrustworthy record")

    pipeline.annotate_lineage(store, rec)
    applied = store.finalize_review(record_id, rec)
    if not applied:
        # The reservation stopped being `running` while this worker was reviewing:
        # a newer push superseded it, or a stale sweep reclaimed it. Its answer is
        # discarded, which is exactly what conditional finalization is for.
        _note(f"reservation {record_id} is no longer running; this review's "
              f"result was discarded")
        current = store.get_review(record_id)
        return WorkerOutcome(
            0, banner(current) if current is not None else _banner_failure(
                f"reservation {record_id} vanished while it was being reviewed"))

    # STEP 8, the POST-COMMIT linearization check. The pre-check above injects
    # BEFORE the store call and therefore cannot see a signal that arrives while
    # SQLite holds the write lock -- and such a signal would otherwise leave a
    # trustworthy record for a review that was killed.
    if pipeline.runner._cancelled(cancel):
        if store.mark_cancelled(record_id, "cancelled during finalization"):
            _note("cancelled during finalization; the committed record was "
                  "demoted")
    current = store.get_review(record_id) or rec
    return WorkerOutcome(0, banner(current))


def _identity_mismatch(reserved: Mapping, **claimed) -> str:
    """Which claimed identity fields disagree with the reservation, if any.

    `base_ref` is compared as a string and an EMPTY claim is tolerated: a
    hand-run worker may legitimately omit it, and the reservation's own value is
    the one that is persisted either way. Every other field must match exactly --
    they are what the review is ABOUT.
    """
    bad = []
    for name, value in claimed.items():
        if name == "base_ref" and not value:
            continue
        if reserved.get(name) != value:
            bad.append(f"{name}: reserved {reserved.get(name)!r}, "
                       f"invoked with {value!r}")
    return "; ".join(bad)


def _record_cancellation(store: "Store", record_id: str, exc) -> WorkerOutcome:
    """Apply a `ReviewCancelled` to its reservation, orderly and conditionally.

    Two shapes, and the difference is whether there is anything worth keeping:

    * **A partial record** (the cancellation happened at or after the pipeline
      built one) goes through `cancellation_transform` and the normal conditional
      finalize: findings and `usable_output` are PRESERVED, and `degraded=True`
      is what makes it untrustworthy. Throwing the findings away would print
      "NO REVIEW HAPPENED" over real evidence; demoting only status/reason would
      store a TRUSTWORTHY cancelled round, because `finalize_review` recomputes
      trust from the axes alone.
    * **No partial** (cancelled before any record existed) has nothing to
      finalize, so the reservation is demoted in place.
    """
    from . import pipeline
    from .trust import banner
    reason = str(exc) or "the review was cancelled"
    _note(reason)
    partial = getattr(exc, "partial", None)
    if isinstance(partial, Mapping) and partial.get("id") == record_id:
        rec = pipeline.cancellation_transform(dict(partial), reason)
        try:
            pipeline.annotate_lineage(store, rec)
            if store.finalize_review(record_id, rec):
                current = store.get_review(record_id) or rec
                return WorkerOutcome(0, banner(current))
        except BaseException as e:      # pragma: no cover - defensive
            _note(f"could not record the cancelled review: {e!r}")
    try:
        store.fail_if_running(record_id, reason)
    except BaseException:               # pragma: no cover - defensive
        pass
    current = store.get_review(record_id)
    return WorkerOutcome(0, banner(current) if current is not None
                         else _banner_failure(reason))


def _banner_failure(reason: str) -> str:
    from .trust import banner_failure
    return banner_failure(reason)


def _install_sigterm(cancel: threading.Event):
    """Make SIGTERM SET the token instead of killing this process.

    A bare SIGTERM death would orphan the model CLI: it runs in its OWN
    session/process group precisely so the watchdog can signal the whole tree, so
    nothing would ever reap it -- it would keep spending quota on a review whose
    record is already superseded, and overlap the replacement review on one
    inference backend.

    The handler does the minimum a signal handler may do: set an `Event`. The
    watchdog's tick loop is what holds the pgid and takes the group down; the
    pipeline's `finally` blocks then run normally, and the worker finalizes
    `failed` CONDITIONALLY, so a record that was already retired stays retired.

    Returns whatever was installed before, or None when this process cannot
    install handlers at all (not the main thread -- which is every in-process
    test that runs the worker from a thread pool). A worker that cannot be
    cancelled still terminates on its own watchdog budget, so this is degraded
    rather than fatal.
    """
    def handler(signum, frame):        # pragma: no cover - driven by a signal
        cancel.set()
    try:
        # `signal.signal` returns the PREVIOUS handler, which is what
        # `_restore_sigterm` puts back. It can legitimately be `SIG_DFL`, whose
        # integer value is 0 -- hence the `is None` test there rather than a
        # truthiness one, which would silently fail to restore the default.
        return signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, RuntimeError):
        _note("could not install the SIGTERM handler; this worker cannot be "
              "cancelled and will run to its own timeout")
        return None


def _restore_sigterm(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, RuntimeError):   # pragma: no cover - defensive
        pass


# ===========================================================================
# THE PRE-PUSH SHIM
# ===========================================================================


#: The line that identifies a hook as OURS. Re-installation replaces only a hook
#: carrying this marker; anything else is a foreign hook and is chained, never
#: overwritten. Versioned so a future shim shape can recognise (and replace) this
#: one without guessing.
SHIM_MARKER: Final = "SKODUN-PREPUSH-SHIM v1"

#: Where a pre-existing foreign hook is preserved, BESIDE the resolved hook (not
#: beside a guessed `.git/hooks`).
BACKUP_SUFFIX: Final = ".pre-skodun"

#: The shim records its chaining target inside itself, on this line, so a
#: re-install can recover the chain without re-deriving it -- and so a human
#: reading the hook can see what else runs.
#:
#: The value is captured RAW and handed to `_sh_unquote`, rather than matched
#: with `'(?P<path>.*)'`: a path containing a single quote is written as several
#: quoted runs spliced together (see `_sh_quote`), and a pattern that assumes one
#: unbroken run would recover a truncated prefix -- which the re-install would
#: then write back as the chain, silently dropping the rest of a foreign hook's
#: path.
_CHAIN_LINE = re.compile(r"^SKODUN_SHIM_CHAIN=(?P<value>.*)$", re.MULTILINE)


def _sh_quote(value: str) -> str:
    """`value` as ONE POSIX sh word, always single-quoted.

    The standard `'\\''` idiom: close the quoted run, emit an escaped quote,
    reopen. `shlex.quote` would do the same job but leaves a "safe" string
    UNQUOTED, and the shipped shim (and `_CHAIN_LINE`, and every hook already
    installed) is written around the value being quoted -- so this always quotes,
    including the empty string, which becomes `''` exactly as it always has.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _sh_unquote(text: str) -> str | None:
    """The inverse of `_sh_quote`, or None for anything this module did not write.

    A concatenation of single-quoted runs and `\\'` escapes, which is precisely
    what `_sh_quote` emits and precisely what every shim installed by an earlier
    build emits too (a path with no quote in it is one unbroken run). `None`
    rather than a best guess: a chain line that cannot be read is not a chain
    target, and guessing at one would point the next push at the wrong file.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            out.append(text[i + 1:end])
            i = end + 1
        elif text.startswith("\\'", i):
            out.append("'")
            i += 2
        else:
            return None
    return "".join(out)


#: The shim's dispatcher command, as an overridable default. `:` `${VAR:=...}`
#: rather than a hard-coded line so an operator (or a test) can point the shim at
#: a different interpreter without editing the hook -- and so "the dispatcher
#: executable is absent" is reachable, which is the case the warn-and-exit-0
#: policy exists for.
_SHIM_PY_ENV: Final = "SKODUN_SHIM_PY"

_SHIM_TEMPLATE = """\
#!/bin/sh
# {marker} -- managed by `skodun install-hooks`. Re-running that command
# replaces everything below; a hook WITHOUT the marker line above is treated as
# somebody else's and chained rather than overwritten.
#
# Order, and why: the ref list arrives on stdin and can be read only ONCE, so it
# is buffered to a temp file FIRST and both consumers are fed from that file. Any
# pre-existing hook runs next, with the original argv and the same bytes, and its
# refusal is propagated -- the push fails exactly as it would have without skodun.
# Only then is skodun asked to dispatch, and NOTHING about review machinery may
# block the push at that point: every dispatcher failure becomes a warning and an
# exit 0.
set -u
SKODUN_SHIM_CHAIN={chain}
: "${{{py_env}:={python}}}"

_sk_tmp=""
if command -v mktemp >/dev/null 2>&1; then
  _sk_tmp="$(mktemp "${{TMPDIR:-/tmp}}/skodun-prepush.XXXXXX" 2>/dev/null || true)"
fi
if [ -z "$_sk_tmp" ]; then
  # No temp file means stdin cannot be shared. The chained hook is the one that
  # can legitimately BLOCK the push, so it gets the live bytes and skodun is
  # skipped. (This path has not consumed stdin yet.)
  echo "skodun: could not buffer the pre-push ref list; skipping the review" >&2
  if [ -n "$SKODUN_SHIM_CHAIN" ] && [ -x "$SKODUN_SHIM_CHAIN" ]; then
    exec "$SKODUN_SHIM_CHAIN" "$@"
  fi
  exit 0
fi
trap 'rm -f "$_sk_tmp"' EXIT HUP INT TERM
# Buffer must succeed completely. A partial/truncated write would feed the
# chained foreign hook a wrong ref list (inventing a push decision). Fail
# closed: never chain or dispatch from an incomplete buffer. Once `cat`
# starts, live stdin is gone — if a chain exists we must refuse the push
# rather than run it on truncated bytes; if not, skip skodun and exit 0.
if ! cat > "$_sk_tmp"; then
  echo "skodun: failed to buffer the pre-push ref list; refusing truncated stdin" >&2
  if [ -n "$SKODUN_SHIM_CHAIN" ]; then
    exit 1
  fi
  exit 0
fi

if [ -n "$SKODUN_SHIM_CHAIN" ] && [ -x "$SKODUN_SHIM_CHAIN" ]; then
  "$SKODUN_SHIM_CHAIN" "$@" < "$_sk_tmp" || exit $?
fi

"${{{py_env}}}" -m skodun dispatch "$@" < "$_sk_tmp" || \\
  echo "skodun: the pre-push review dispatcher failed (exit $?); the push is \
NOT blocked" >&2
exit 0
"""


def shim_text(chain: str = "", python: str | None = None) -> str:
    """The pre-push hook this build installs.

    `chain` is the absolute path of the hook that was there before, or `""`. It is
    written INTO the shim so a re-install can recover it and so a human reading
    the hook can see what else runs -- deriving it again from the filesystem would
    be a second answer to "what am I chaining", and the two could disagree once a
    backup file was moved.

    It is SHELL-QUOTED on the way in (`_sh_quote`). It is a filesystem path, and
    a repository checked out under a directory whose name contains a single quote
    -- an ordinary thing on a case-preserving filesystem -- otherwise produced a
    hook that would not even parse, i.e. a pre-push that fails for every push in
    that repository.
    """
    return _SHIM_TEMPLATE.format(
        marker=SHIM_MARKER, chain=_sh_quote(chain), py_env=_SHIM_PY_ENV,
        python=python or sys.executable)


def hooks_dir(repo: Path) -> Path:
    """Where this repository's hooks actually live.

    `git rev-parse --git-path hooks`, NEVER a hard-coded `.git/hooks`. Both of the
    reasons are ordinary setups rather than corner cases: a linked worktree's
    `.git` is a FILE pointing into the main repository's `worktrees/<name>`, and
    `core.hooksPath` relocates hooks wherever an operator or a tool
    (pre-commit, husky) put them. Writing to `.git/hooks` in either case installs
    a hook git will never run -- silently, which is the whole problem.

    The answer is relative to the git invocation's directory, so it is resolved
    against `repo`.
    """
    from .gitio import GitError
    try:
        cp = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path",
                             "hooks"], capture_output=True, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise GitError(f"could not ask git where its hooks live: {e!r}") from e
    if cp.returncode != 0:
        raise GitError(
            f"{repo} is not a git repository, or git refused: "
            f"{cp.stderr.decode('utf-8', 'replace').strip()}")
    raw = cp.stdout.decode("utf-8", "replace").strip()
    if not raw:
        raise GitError("git did not say where its hooks live")
    path = Path(raw)
    return path if path.is_absolute() else (Path(repo) / path).resolve()


class HookRefused(Exception):
    """Installation refused rather than risking somebody else's hook."""


def install_hooks(repo: Path, *, force: bool = False,
                  python: str | None = None) -> tuple[Path, str]:
    """Install (or re-install) the pre-push shim. Returns `(path, what happened)`.

    Four cases, and the two refusals are the point of the function:

    * **No hook** -- write the shim, chaining nothing.
    * **Our own shim** -- replace it, PRESERVING the chain target recorded inside
      it. Idempotent: re-installing after an upgrade must not silently drop the
      foreign hook a previous `--force` chained.
    * **A foreign hook, no `--force`** -- REFUSE. Somebody else's hook is not ours
      to move, and the failure mode of guessing is a push that silently stops
      running a check the repository relies on.
    * **A foreign hook with `--force`** -- back it up to `pre-push.pre-skodun` and
      CHAIN it (never discard it). But if that backup name is already occupied by
      a DIFFERENT earlier foreign hook, refuse EVEN under `--force`: overwriting
      it would destroy the first backup with no trace, and only the operator can
      say which of the two hooks they meant to keep. The message names both files.
    """
    directory = hooks_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "pre-push"
    backup = directory / f"pre-push{BACKUP_SUFFIX}"

    if not hook.exists() and not hook.is_symlink():
        _write_shim(hook, "", python)
        return hook, "installed"

    existing = _read_text(hook)
    if SHIM_MARKER in existing:
        match = _CHAIN_LINE.search(existing)
        chain = ""
        if match is not None:
            chain = _sh_unquote(match.group("value"))
            if chain is None:
                # OUR marker, and a chain line we cannot read. Writing `""` here
                # would drop whatever foreign hook it named -- exactly the
                # silent loss `--force`'s backup exists to prevent -- and
                # guessing at a prefix would chain the wrong file.
                raise HookRefused(
                    f"{hook} carries skodun's marker but a chain line this "
                    f"build cannot read ({match.group(0)}); it may name a hook "
                    f"that would be dropped. Fix or delete that line, then "
                    f"re-run")
        _write_shim(hook, chain, python)
        return hook, ("reinstalled, still chaining " + chain if chain
                      else "reinstalled")

    if not force:
        raise HookRefused(
            f"{hook} is not skodun's hook and would have to be moved aside; "
            f"re-run with --force to back it up to {backup} and chain it")
    if (backup.exists() or backup.is_symlink()) and _read_text(backup) != existing:
        raise HookRefused(
            f"{backup} already holds a DIFFERENT hook than the one now at "
            f"{hook}; installing would discard that backup. Resolve it yourself "
            f"-- keep one of the two, or merge them -- then re-run")
    shutil.move(str(hook), str(backup))
    backup.chmod(backup.stat().st_mode | 0o111)
    _write_shim(hook, str(backup), python)
    return hook, f"installed, chaining the previous hook preserved at {backup}"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # A hook that cannot be READ is still a hook that is THERE. Returning ""
        # makes it look foreign, which routes it to the refusal above rather than
        # to a silent overwrite.
        return ""


def _write_shim(hook: Path, chain: str, python: str | None) -> None:
    """Write the shim and make it executable. Atomic-ish: a temp file in the same
    directory then a rename, so a push racing the install sees either the old hook
    or the new one, never a half-written script."""
    tmp = hook.with_name(hook.name + ".skodun-new")
    tmp.write_text(shim_text(chain, python), encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, hook)
