"""Runtime budgets: how long one review may legitimately still be running.

Two numbers live here, and the difference between them is the whole reason this
module exists.

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
arithmetic over a `Defaults`, which is what makes both ceilings testable without
a repo and comparable against the oracle's shell arithmetic line for line.
"""

from __future__ import annotations

from .config import Defaults

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
