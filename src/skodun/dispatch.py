"""Background dispatch: the evidence and rules behind skipping a review.

A push that has already been reviewed should not be reviewed again. That is the
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

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import contextpack, promptbuild

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Defaults
    from .gitio import Diff
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
                         d: "Defaults", dedup_enabled: bool) -> DedupEvidence:
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
    single-shot pipeline's, `pipeline.py:1146-1158`: the prompt's own leftover
    headroom, and `pack_large_added=False` because the pushed diff already
    carries every added file whole. A different headroom or a different
    large-added rule is a different identity for the same commit, so getting
    this wrong would not fail loudly, it would silently stop ever matching.

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
        headroom = promptbuild.context_headroom(d.max_diff_bytes, len(diff.data),
                                                packing=True)
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
