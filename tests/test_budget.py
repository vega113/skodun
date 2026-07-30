"""Runtime budgets: how long one review may legitimately take.

Two numbers, one shape. `worst_runtime` is the age at which a `running` record
is swept; `lock_stale_ceiling` is the age at which a HELD foreground lock may be
taken from its owner. The second is deliberately the wider of the two (the extra
passes run inside the lock), and both scale by the same two multipliers: the
configured chain width, and the batch count of a batched review.

Every number here is asserted against the arithmetic spelled out, never against
a magic constant, so a change to the formula fails the test that names the term
it dropped: the retry term, the grace term, the chain width, the batch count.
`tests/test_pipeline.py` and `tests/test_fallback.py` pin the shipped
`pipeline.*_sec` wrappers at `batch_count=0`; the delegation test below is what
keeps those two spellings from becoming two implementations.
"""

from __future__ import annotations

import pytest

from skodun import budget, pipeline
from skodun.config import Defaults

#: Nonzero on BOTH retry axes on purpose: a formula that dropped the
#: `(1 + timeout_retries + degraded_retries)` factor is indistinguishable from
#: the real one on the zero-retry default.
RETRIES = Defaults(timeout_sec=100, timeout_retries=2, degraded_retries=3)


def test_worst_runtime_is_the_shipped_shape_with_nonzero_retries():
    # 2x the timeout per attempt (the watchdog's SIGTERM grace, plus the
    # oracle's doubling for a wedged attempt) x every attempt the two retry
    # axes permit, plus the sweep grace.
    assert budget.attempt_budget(RETRIES) == 2 * 100 * (1 + 2 + 3)
    assert budget.worst_runtime(RETRIES, 1, 0) == 2 * 100 * (1 + 2 + 3) + 60


def test_worst_runtime_carries_the_stale_record_grace():
    """The grace is a term of the budget, not slack someone added twice."""
    assert budget.GRACE_SEC == 60
    assert (budget.worst_runtime(RETRIES, 1, 0)
            - budget.attempt_budget(RETRIES)) == budget.GRACE_SEC
    # ...and it is there for a config with no retries at all, which is the one
    # the shipped pipeline tests pin (2*1 + 60 = 62).
    zero = Defaults(timeout_sec=1, timeout_retries=0, degraded_retries=0)
    assert budget.worst_runtime(zero, 1, 0) == 62


def test_the_batch_count_multiplies_the_whole_budget():
    """`batch_count + 1` calls: every batch, plus the integration pass."""
    one = budget.worst_runtime(RETRIES, 1, 0)
    assert budget.worst_runtime(RETRIES, 1, 1) == 2 * one
    assert budget.worst_runtime(RETRIES, 1, 3) == 4 * one
    assert budget.worst_runtime(RETRIES, 1, 40) == 41 * one


def test_chain_width_and_batch_count_compose():
    one = budget.worst_runtime(RETRIES, 1, 0)
    assert budget.worst_runtime(RETRIES, 4, 0) == 4 * one
    assert budget.worst_runtime(RETRIES, 4, 3) == 4 * 4 * one


def test_the_lock_ceiling_is_the_wider_formula_and_scales_identically():
    ceiling = budget.lock_stale_ceiling(RETRIES, 1, 0)
    assert ceiling == (budget.MAX_PASSES_UNDER_LOCK
                       * budget.attempt_budget(RETRIES) + budget.GRACE_SEC)
    assert budget.MAX_PASSES_UNDER_LOCK == 3
    assert ceiling > budget.worst_runtime(RETRIES, 1, 0)
    assert budget.lock_stale_ceiling(RETRIES, 1, 3) == 4 * ceiling
    assert budget.lock_stale_ceiling(RETRIES, 4, 3) == 16 * ceiling


@pytest.mark.parametrize("width", [1, 2, 4])
def test_the_shipped_pipeline_names_delegate_at_batch_count_zero(width):
    """ONE implementation behind two spellings.

    The shipped names are what `test_pipeline.py` / `test_fallback.py` import
    and what an unbatched review still uses; a second copy of the arithmetic
    behind them could only drift.
    """
    for d in (RETRIES, Defaults()):
        assert pipeline.worst_runtime_sec(d, max_chain_width=width) == \
            budget.worst_runtime(d, width, 0)
        assert pipeline.lock_stale_ceiling_sec(d, max_chain_width=width) == \
            budget.lock_stale_ceiling(d, width, 0)
    # The shipped module constants keep naming the same two numbers.
    assert pipeline.STALE_RECORD_GRACE_SEC == budget.GRACE_SEC
    assert pipeline._MAX_PASSES_UNDER_LOCK == budget.MAX_PASSES_UNDER_LOCK


def test_the_default_arguments_are_the_unbatched_single_entry_budget():
    d = Defaults()
    assert budget.worst_runtime(d) == pipeline.worst_runtime_sec(d)
    assert budget.lock_stale_ceiling(d) == pipeline.lock_stale_ceiling_sec(d)


@pytest.mark.parametrize("width,count", [(0, 0), (-4, 0), (1, -5), (0, -1)])
def test_degenerate_arguments_clamp_UP_never_down(width, count):
    """A computed width/count can legitimately arrive at zero or below.

    Clamping to the single-entry, unbatched budget is the fail-safe direction:
    the only thing riding on these numbers is never reclaiming a live holder's
    lock, so a too-SMALL budget is the one error with a cost.
    """
    assert budget.worst_runtime(RETRIES, width, count) == \
        budget.worst_runtime(RETRIES, 1, 0)
    assert budget.lock_stale_ceiling(RETRIES, width, count) == \
        budget.lock_stale_ceiling(RETRIES, 1, 0)


def test_more_batches_never_shrink_either_budget():
    prev_run = prev_lock = 0
    for n in range(0, 12):
        run = budget.worst_runtime(RETRIES, 2, n)
        lock = budget.lock_stale_ceiling(RETRIES, 2, n)
        assert run > prev_run and lock > prev_lock
        assert isinstance(run, int) and isinstance(lock, int)
        prev_run, prev_lock = run, lock
