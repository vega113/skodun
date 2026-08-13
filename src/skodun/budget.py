"""Budgets: how long one review may legitimately run, and how large its prompt
may be.

Three numbers live here. Two are RUNTIME budgets and the difference between
them is most of the reason this module exists; the third, `prompt_budget`, is
the SIZE budget, and it is here for the same reason the other two are — it is
pure arithmetic that several modules must agree on, and a second copy of it
could only drift.

`worst_runtime` is a RECORD budget: the age past which a `running` review record
is swept as wreckage (`pipeline.recover_stale`). Sweeping one early costs
nothing -- the run's own final save rewrites the row -- so this is the narrower
figure, and it is the one the brief pins to the oracle's `GROK_WORST_RUNTIME`
arithmetic.

`lock_stale_ceiling` is a LOCK budget: the age past which a held foreground lock
may be taken from its owner. Reclaiming one early puts two reviews on a single
inference backend, which is the exact failure the lock exists to prevent, so it
budgets everything that can happen inside one held lock -- the primary review
plus the extra passes -- and is deliberately several times wider.

Both scale by the same two multipliers, and both multiplications are lock-safety
requirements rather than bookkeeping:

  * `chain_width` -- a "reviewer run" is a CHAIN. An entry that classifies
    `unavailable` is followed by the next one, each with its own complete retry
    budget, so a ceiling sized for one entry lets a peer reclaim the lock of a
    holder still legitimately working through its fallbacks.
  * `batch_count` -- a batched review makes `batch_count` sequential sub-review
    calls plus one cross-file integration call, each with its own complete retry
    budget (the oracle's own `_batch_calls = BATCH_COUNT + RUN_INTEGRATION`,
    `grok-prepush-review.sh:3357`). `batch_count + 1` is therefore the number of
    full reviewer runs the budget must cover; `batch_count=0` means "not
    batched" and reproduces the shipped single-review numbers exactly.

The shipped spellings `pipeline.worst_runtime_sec` / `pipeline.lock_stale_ceiling_sec`
survive as thin wrappers that pass `batch_count=0`. They are what the shipped
pipeline and fallback tests import, and they are what an unbatched review still
uses; they delegate here rather than re-deriving the arithmetic, because two
copies of a lock-safety formula can only ever drift into disagreeing.

Nothing in this module reads config files, a store, or a clock: it is pure
arithmetic over a `Defaults` (and, for `prompt_budget`, over one `Reviewer` and
its adapter's declared ceiling), which is what makes every ceiling testable
without a repo and comparable against the oracle's shell arithmetic line for
line.
"""

from __future__ import annotations

from . import checklist, fingerprint, promptbuild, stack
from .config import Defaults, Reviewer

#: Grace added to a computed worst case before anything acts on it. One
#: definition, shared by both budgets (`pipeline.STALE_RECORD_GRACE_SEC` is an
#: alias kept for the shipped name).
GRACE_SEC = 60

#: How many full reviewer runs ONE held foreground lock can cover: the primary
#: review, the security pass, and ONE of the skeptic/refuter pair -- each runs
#: inside the lock with its own complete retry budget.
#:
#: Three, not four, with three extra passes wired up: the skeptic needs the
#: merged record to have ZERO findings and the refuter needs the FINDER to have
#: had at least one, and extra-pass merges only ever append -- so a run that
#: schedules one can never schedule the other. `test_refuter.py::
#: test_the_refuter_and_the_skeptic_are_mutually_exclusive` pins that, because
#: this number is what keeps a peer from reclaiming a live holder's lock.
MAX_PASSES_UNDER_LOCK = 3


def attempt_budget(d: Defaults) -> int:
    """The longest ONE reviewer entry (all of its retries) can legitimately take.

    The oracle's `GROK_WORST_RUNTIME` arithmetic without the grace: each attempt
    can burn up to 2x the timeout (the watchdog's own SIGTERM grace, plus the
    oracle's doubling for a wedged attempt), and there are `1 + timeout_retries
    + degraded_retries` attempts.
    """
    return 2 * d.timeout_sec * (1 + d.timeout_retries + d.degraded_retries)


def _calls(chain_width: int, batch_count: int) -> int:
    """How many full reviewer runs the budget must cover.

    Degenerate inputs clamp UP, never down: a caller computing a width or a
    batch count by arithmetic can legitimately arrive at zero or below, and the
    only thing riding on these numbers is never acting on a run that is still
    alive -- so a too-SMALL budget is the one error with a cost. A non-integer
    is not silently coerced by truthiness anywhere near this: `int()` raises for
    anything that is not a number, which is a programming error and belongs at
    the call site.
    """
    width = max(1, int(chain_width))
    batches = max(0, int(batch_count))
    return width * (batches + 1)


def worst_runtime(d: Defaults, chain_width: int = 1,
                  batch_count: int = 0) -> int:
    """The longest one *review* can legitimately take, plus a grace.

    ORACLE PARITY at `chain_width=1, batch_count=0`, and the brief pins that
    formula: it is the age at which `recover_stale` sweeps a `running` record.
    Deliberately NOT the lock's stale ceiling -- see `lock_stale_ceiling`.

    A batched review is persisted as ONE record covering `batch_count`
    sub-reviews plus the integration pass, so the record's budget has to cover
    all of them; `pipeline.run_review` persists the result on the record itself
    (`worst_runtime_sec`) and `recover_stale` prefers that persisted value over
    recomputing from a config that may since have changed.
    """
    return _calls(chain_width, batch_count) * (attempt_budget(d) + GRACE_SEC)


def lock_stale_ceiling(d: Defaults, chain_width: int = 1,
                       batch_count: int = 0) -> int:
    """The age at which a held foreground lock may be reclaimed from its owner.

    Wider than `worst_runtime` on purpose. That function budgets the reviewer
    runs a record covers, but the security and skeptic passes run INSIDE the
    lock, each with its own full timeout/degraded retry budget -- so a
    legitimate holder can be alive for roughly `MAX_PASSES_UNDER_LOCK` times as
    long. Reclaiming on the narrower figure would let a peer take a live
    holder's lock and put two reviews on one inference backend; the cost of the
    wider ceiling is only that a genuinely wedged lock is tolerated longer, and
    `SKODUN_LOCK_STALE_SECONDS` exists for that.

    `recover_stale` keeps the narrower figure: a `running` *record* is per-run
    bookkeeping the final save always rewrites, so sweeping it early costs
    nothing, while reclaiming a live lock early costs a doubled backend.
    """
    return _calls(chain_width, batch_count) * (
        MAX_PASSES_UNDER_LOCK * attempt_budget(d) + GRACE_SEC)


# ---------------------------------------------------------------------------
# the PROMPT budget: how many bytes one reviewer's envelope may hold
# ---------------------------------------------------------------------------
#
# `[defaults] max_diff_bytes` on its own is ONE global envelope, so it has to be
# fitted to the LEAST capable provider in the config -- which needlessly shrinks
# the review for every other one. Worse, the mismatch used to be invisible to
# the planner: only `agy.build_cmd` knew its CLI cannot take a prompt over
# `MAX_PROMPT_ARG_BYTES`, so batches were cut from the global number and refused
# at invocation.
#
# `prompt_budget` is the ONE definition of "the envelope for THIS reviewer", and
# every site that sizes a prompt or plans batches goes through it. Two inputs,
# and the direction of each is deliberate:
#
#   * the OPERATOR's number -- the entry's own `max_diff_bytes` if it has one,
#     otherwise the global. A per-entry value REPLACES the global rather than
#     being min-ed with it: that is what "budget this entry differently" means,
#     in both directions.
#   * the ADAPTER's ceiling, which caps it and never raises it. A ceiling is
#     physics (what the CLI can physically be handed); a configured value is
#     policy, and policy has reasons -- cost, latency, a model's own attention
#     -- that the adapter has no view on. So: `min`.


#: Bytes reserved OUTSIDE the envelope when fitting a prompt into an adapter's
#: declared ceiling.
#:
#: `max_diff_bytes` is the envelope for the DIFF plus the packed FILE CONTEXT
#: and nothing else (see `promptbuild`'s "Context headroom"). Everything else a
#: prompt carries is charged to no budget at all, so a ceiling applied to the
#: envelope directly would be exceeded by the prompt that envelope produces.
#:
#: COMPOSED from the real constants rather than picked, so it tracks the prompt
#: it is reserving for: the reviewer instructions, the response contract, the
#: section markers, and the WHOLE repo-rules injection budget (`checklist.
#: BUDGET` is the most those can cost). `_IDENTITY_SLACK` covers the one part
#: that is genuinely variable -- the branch/base/head block, whose length
#: depends on a branch name and two refs.
#:
#: It is an approximation for the extra passes, whose prompts have their own
#: preambles (the refuter's carries the finder's findings, which no static
#: number can bound). That is tolerable precisely because it is not the only
#: guard: a prompt that still exceeds an adapter's ceiling classifies
#: `unavailable` and the chain advances (`chain.run_chain`), rather than the
#: review dying.
_IDENTITY_SLACK = 512
PROMPT_OVERHEAD_BYTES: int = (
    len(promptbuild._INTRO)
    + len(promptbuild._CONTEXT_INSTRUCTIONS)
    + len(promptbuild._RESPONSE_CONTRACT)
    + len(promptbuild.RULES_BEGIN)
    + len(promptbuild.RULES_END)
    + len(promptbuild.DIFF_BEGIN)
    + len(promptbuild.DIFF_END)
    + len(promptbuild.DIFF_TRUNCATED)
    + checklist.BUDGET
    + _IDENTITY_SLACK
    + stack.MAX_STACK_PROMPT_BYTES
    + promptbuild.ADVISORY_BLOCK_WRAPPER_BYTES
    + fingerprint.MAX_LINEAGE_PROMPT_BYTES
    + promptbuild.ADVISORY_BLOCK_WRAPPER_BYTES
)


def _adapter_ceiling(r: Reviewer | None) -> int | None:
    """`r`'s adapter's declared prompt ceiling, or None if there isn't one.

    None is also the answer when the provider has no adapter at all. That is a
    CONFIG error and it already has an owner -- `pipeline._run_review`'s
    preflight resolves every reviewer in the graph and refuses the run before
    anything is locked or persisted, and `chain.run_chain` would raise on it in
    any case. Raising a second time from an arithmetic helper would only move
    that error somewhere with less to say about it, so this degrades to exactly
    the pre-change behaviour: the configured number, uncapped.
    """
    if r is None or not r.provider:
        return None
    from .adapters import get_adapter
    try:
        limit = get_adapter(r.provider).prompt_limit()
    except Exception:   # noqa: BLE001 - see docstring; config errors have an owner
        return None
    if limit is None or isinstance(limit, bool) or not isinstance(limit, int):
        return None
    return limit


def prompt_budget(d: Defaults, r: Reviewer | None = None) -> int:
    """The prompt envelope, in bytes, for the reviewer that will actually run.

    THE definition. Every site that sizes a prompt or plans batches reads this
    and never `d.max_diff_bytes` directly, so "the budget for this reviewer" has
    exactly one answer no matter who is asking.

    `r=None` means "no particular reviewer", and answers the global — the
    shipped behaviour, unchanged, and what a caller that genuinely has no
    reviewer in hand should get.

    Clamped to at least 1: `promptbuild.build` refuses a non-positive envelope
    (a zero one ships a prompt with no diff in it, which reads to the model as
    "nothing changed"), and a ceiling narrower than `PROMPT_OVERHEAD_BYTES`
    would otherwise compute one. Such a provider cannot review anything at all,
    and saying so at invocation — where the chain can advance past it — is
    strictly better than an exception out of the planner.
    """
    configured = d.max_diff_bytes
    if r is not None and r.max_diff_bytes is not None:
        configured = r.max_diff_bytes
    ceiling = _adapter_ceiling(r)
    if ceiling is None:
        return configured
    return max(1, min(configured, ceiling - PROMPT_OVERHEAD_BYTES))
