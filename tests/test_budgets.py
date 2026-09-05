"""Budget behavior: fake clocks drive the same controller used by execution."""

import threading

import pytest

from skodun.budgets import Limits, ReviewBudget


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_waiting_longer_than_review_allowance_does_not_spend_it():
    clock = Clock()
    budget = ReviewBudget(Limits(queue=200, review=10), clock=clock)
    budget.start_queue()
    clock.value = 100
    assert not budget.is_set()
    budget.end_queue()
    budget.provider_started()
    clock.value = 109
    assert not budget.is_set()
    clock.value = 110
    assert budget.is_set()
    assert budget.reason_code == 'review_budget_exhausted'
    timing = budget.snapshot()['timing']
    assert timing['queue_wait_ms'] == 100000
    assert timing['review_wall_ms'] == 10000


def test_total_expiry_still_bounds_queue_only_request():
    clock = Clock()
    budget = ReviewBudget(Limits(total=5, review=10), clock=clock)
    budget.start_queue()
    clock.value = 5
    assert budget.is_set()
    assert budget.reason_code == 'total_budget_exhausted'
    assert budget.snapshot()['timing']['review_wall_ms'] == 0


def test_queue_expiry_is_distinct_from_execution_expiry():
    clock = Clock()
    budget = ReviewBudget(Limits(queue=2, review=1), clock=clock)
    budget.start_queue()
    clock.value = 2
    assert budget.is_set()
    assert budget.reason_code == 'queue_budget_exhausted'


def test_provider_hops_share_deadline_but_free_fallback_can_still_run():
    clock = Clock()
    budget = ReviewBudget(Limits(provider_wait=3), clock=clock)
    allowance = budget.provider_allowance()
    with allowance.waiting():
        clock.value = 2
        assert allowance.remaining() == 1
    budget.provider_started()
    clock.value = 100  # model runtime must not spend admission allowance
    with allowance.waiting():
        assert allowance.remaining() == 1
        clock.value = 101
        assert allowance.remaining() == 0
    assert not budget.is_set()
    assert budget.snapshot()['timing']['provider_wait_ms'] == 3000
    assert budget.provider_allowance().remaining() == 3


def test_later_provider_wait_counts_against_review_wall_allowance():
    clock = Clock()
    budget = ReviewBudget(Limits(review=5), clock=clock)
    budget.provider_started()
    clock.value = 1
    budget.start_provider_wait()
    clock.value = 5
    assert budget.is_set()
    assert budget.reason_code == 'review_budget_exhausted'


def test_explicit_cancel_reason_is_preserved_and_terminal_timing_freezes():
    class Cancel:
        reason_code = 'mcp_disconnected'

        def is_set(self):
            return True

    clock = Clock()
    budget = ReviewBudget(Limits(total=1), cancel=Cancel(), clock=clock)
    clock.value = 2
    assert budget.is_set() and budget.reason_code == 'mcp_disconnected'
    budget.finish()
    clock.value = 100
    assert budget.snapshot()['timing']['total_ms'] == 2000


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), 86401, '2'])
def test_limits_reject_invalid_values(value):
    with pytest.raises(ValueError):
        Limits(queue=value)


def test_recovery_default_total_is_preserved_and_other_review_can_be_unbounded():
    assert Limits.from_args(recover=True).total == 900
    assert Limits.from_args(recover=False).total is None
    assert Limits.from_args(max_wall_seconds=12).total == 12


def test_zero_wait_observes_cancellation_without_sleeping():
    event = threading.Event()
    budget = ReviewBudget(Limits(), cancel=event)
    assert budget.wait(0) is False
    event.set()
    assert budget.wait(0) is True


def test_legacy_zero_provider_wait_still_allows_an_immediate_attempt(monkeypatch):
    monkeypatch.setenv('SKODUN_ADMISSION_WAIT_SECONDS', '0')
    limits = Limits.from_args()
    assert limits.provider_wait == 0
    budget = ReviewBudget(limits)
    assert budget.provider_allowance().remaining() == 0
    assert not budget.is_set()


def test_foreground_readmission_does_not_spend_remaining_review_allowance():
    clock = Clock()
    budget = ReviewBudget(Limits(queue=200, review=10), clock=clock)
    budget.provider_started()
    clock.value = 5
    budget.start_queue()
    clock.value = 105
    assert not budget.is_set()
    assert budget.snapshot()['timing']['review_active_ms'] == 5000
    assert budget.snapshot()['deadlines']['review'] is None
    budget.end_queue()
    clock.value = 110
    assert budget.is_set() and budget.reason_code == 'review_budget_exhausted'


def test_signal_marker_composes_with_upstream_attribution():
    from skodun.request_cancel import mark_event
    event = threading.Event()
    budget = ReviewBudget(Limits(), cancel=event)
    mark_event(budget, 'signal')
    assert event.is_set() and event.reason_code == 'signal'
    assert budget.is_set() and budget.reason_code == 'signal'
