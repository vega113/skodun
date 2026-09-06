"""Separate cooperative queue, provider-admission, review and total budgets.

Review allowance starts at the first provider launch and includes subsequent
provider waits, but pauses during foreground readmission. Provider admission allowances spend only waiting, never model
runtime, and share their remainder across a pass's fallback entries. Terminal
observation is frozen; reading status cannot revive or cancel an execution.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import threading
import time
from functools import wraps
from types import SimpleNamespace


def _limit(name, value):
    if value is None:
        return
    if type(value) not in (int, float) or not 0 < value <= 86400 or not math.isfinite(value):
        raise ValueError(f'{name} must be a positive finite number no greater than 86400')


@dataclass(frozen=True)
class Limits:
    queue: float | None = None
    review: float | None = None
    provider_wait: float = 30.0
    total: float | None = None

    def __post_init__(self):
        for name, value in (('max_queue_seconds', self.queue),
                            ('max_review_seconds', self.review),
                            ('max_provider_wait_seconds', self.provider_wait),
                            ('max_wall_seconds', self.total)):
            if name == 'max_provider_wait_seconds' and type(value) in (int, float) and value == 0:
                continue  # legacy zero-wait admission still gets an immediate try
            _limit(name, value)
        if self.provider_wait is None:
            raise ValueError('provider wait policy must have a finite bound')

    @classmethod
    def from_args(cls, *, recover=False, max_queue_seconds=None,
                  max_review_seconds=None, max_provider_wait_seconds=None,
                  max_wall_seconds=None):
        from .capacity import admission_wait_from_env
        return cls(queue=max_queue_seconds, review=max_review_seconds,
                   provider_wait=(max_provider_wait_seconds if max_provider_wait_seconds is not None
                                  else admission_wait_from_env(30.0)),
                   total=(max_wall_seconds if max_wall_seconds is not None else
                          900.0 if recover else None))

    def to_dict(self):
        return dict(max_queue_seconds=self.queue, max_review_seconds=self.review,
                    max_provider_wait_seconds=self.provider_wait, max_wall_seconds=self.total)


class ProviderAllowance:
    """One pass's cumulative admission allowance across configured fallbacks."""

    def __init__(self, seconds, *, clock=None, budget=None):
        self.seconds = max(0.0, float(seconds))
        self.clock, self.budget = time.monotonic if clock is None else clock, budget
        self.spent = 0.0
        self.started = None

    def remaining(self):
        active = 0.0 if self.started is None else max(0.0, self.clock() - self.started)
        return max(0.0, self.seconds - self.spent - active)

    @contextmanager
    def waiting(self):
        self.started = self.clock()
        token = None
        if self.budget is not None:
            token = self.budget.start_provider_wait(self.remaining())
        try:
            yield self
        finally:
            self.spent += max(0.0, self.clock() - self.started)
            self.started = None
            if self.budget is not None:
                self.budget.end_provider_wait(token)


class ReviewBudget:
    """Event-compatible supervisor; composing an upstream cancellation token."""

    # Only pure state crosses worker threads. Store-bound monitors/callbacks
    # belong to each handle and are never copied into this namespace.
    _STATE_FIELDS = frozenset(('limits', 'clock', 'started', 'started_utc',
        'queue_started', 'review_started', 'provider_wait_started', 'queue_elapsed',
        'provider_wait_elapsed', 'queue_deadline_mono', 'queue_at_review_start',
        'provider_waiters', 'provider_deadline_mono', 'ended', '_reason',
        '_marked_reason', '_event', '_lock', '_publication_lock', '_waits', '_next_wait'))

    def __getattr__(self, name):
        if name in self._STATE_FIELDS:
            return getattr(self._shared_state, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self._STATE_FIELDS:
            setattr(self._shared_state, name, value)
        else:
            object.__setattr__(self, name, value)

    def __init__(self, limits, *, cancel=None, clock=None, on_update=None):
        self._shared_state = SimpleNamespace()
        self.limits, self.cancel = limits, cancel
        self.clock = time.monotonic if clock is None else clock
        self.on_update = on_update
        self.started = self.clock()
        self.started_utc = datetime.now(timezone.utc)
        self.queue_started = self.review_started = self.provider_wait_started = None
        self.queue_elapsed = self.provider_wait_elapsed = 0.0
        self.queue_deadline_mono = None
        self.queue_at_review_start = 0.0
        self.provider_waiters = 0
        self.provider_deadline_mono = None
        self.ended = None
        self._reason = None
        self._marked_reason = None
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._publication_lock = threading.Lock()
        self._waits, self._next_wait = {}, 0

    def fork(self, *, cancel, on_update):
        """A worker-local handle with the same pure, synchronized execution state."""
        return self.from_shared(self._shared_state, cancel=cancel, on_update=on_update)

    @classmethod
    def from_shared(cls, state, *, cancel, on_update):
        handle = object.__new__(cls)
        handle._shared_state = state
        handle.cancel, handle.on_update = cancel, on_update
        return handle

    def _update(self):
        # Never called with the state mutex held. Read a fresh snapshot after
        # serialization so same-second writes cannot regress execution state.
        if self.on_update is not None:
            with self._publication_lock:
                self.on_update(self.snapshot())

    def start_queue(self, wait_seconds=None):
        with self._lock:
            if self.queue_started is None:
                self.queue_started = self.clock()
            remaining = self.queue_remaining()
            effective = (wait_seconds if remaining is None else remaining if wait_seconds is None
                         else min(remaining, wait_seconds))
            self.queue_deadline_mono = None if effective is None else self.clock() + effective
        self._update()

    def end_queue(self):
        with self._lock:
            if self.queue_started is not None:
                self.queue_elapsed += max(0.0, self.clock() - self.queue_started)
                self.queue_started = None
                self.queue_deadline_mono = None
        self._update()

    def start_provider_wait(self, seconds=None):
        with self._lock:
            if self.ended is not None:
                raise ValueError('cannot start a wait after budget completion')
            if len(self._waits) >= 2:
                raise ValueError('at most two provider waits may be active')
            if not self._waits:
                self.provider_wait_started = self.clock()
            self._next_wait += 1
            token = self._next_wait
            self._waits[token] = self.clock() + (self.limits.provider_wait if seconds is None else seconds)
            self.provider_waiters = len(self._waits)
            self.provider_deadline_mono = min(self._waits.values())
        try:
            self._update()
        except BaseException:
            self._end_provider_wait(token)
            raise
        return token

    def _end_provider_wait(self, token):
        with self._lock:
            if token is None and len(self._waits) == 1:
                token = next(iter(self._waits))
            if token not in self._waits:
                raise ValueError('unknown or already-ended provider wait')
            del self._waits[token]
            self.provider_waiters = len(self._waits)
            self.provider_deadline_mono = min(self._waits.values(), default=None)
            if not self._waits and self.provider_wait_started is not None:
                self.provider_wait_elapsed += max(0.0, self.clock() - self.provider_wait_started)
                self.provider_wait_started = None

    def end_provider_wait(self, token=None):
        self._end_provider_wait(token)
        self._update()

    def provider_started(self):
        with self._lock:
            if self.review_started is None:
                self.review_started = self.clock()
                self.queue_at_review_start = self._queue_seconds(self.review_started)
        self._update()

    def provider_allowance(self, seconds=None):
        return ProviderAllowance(self.limits.provider_wait if seconds is None else seconds,
                                 clock=self.clock, budget=self)

    @property
    def reason_code(self):
        self.is_set()
        return self._reason

    @reason_code.setter
    def reason_code(self, value):
        # mark_event is also used inside installed signal handlers: only
        # assign in-memory attribution here, never perform Store I/O.
        self._marked_reason = value if isinstance(value, str) else None
        if self.cancel is not None:
            try:
                self.cancel.reason_code = self._marked_reason
            except (AttributeError, TypeError):
                pass

    def is_set(self):
        with self._lock:
            if self.ended is not None or self._reason is not None:
                return self._reason is not None
        # Cancellation may poll SQLite: never hold the shared state lock here.
        try:
            upstream_set = self.cancel is not None and self.cancel.is_set()
        except KeyboardInterrupt:
            raise
        except BaseException:
            with self._lock:
                self._reason = self._reason or 'cancellation_state_unavailable'
            return True
        with self._lock:
            if self.ended is not None or self._reason is not None:
                return self._reason is not None
            if upstream_set:
                observed = getattr(self.cancel, 'reason_code', None)
                self._reason = (self._marked_reason if self._marked_reason and observed in
                                (None, 'unknown_cancel_token', 'cancelled_external') else
                                observed or 'cancelled_external')
            elif self._event.is_set():
                self._reason = self._marked_reason or 'cancelled_external'
            else:
                at = self.clock()
                if self.limits.total is not None and at - self.started >= self.limits.total:
                    self._reason = 'total_budget_exhausted'
                elif (self.queue_started is not None and self.limits.queue is not None
                      and self.queue_elapsed + at - self.queue_started >= self.limits.queue):
                    self._reason = 'queue_budget_exhausted'
                elif (self.review_started is not None and self.limits.review is not None
                      and self._review_seconds(at) >= self.limits.review):
                    self._reason = 'review_budget_exhausted'
            return self._reason is not None

    def set(self):
        self._event.set()
        if self.cancel is not None and hasattr(self.cancel, 'set'):
            self.cancel.set()

    def wait(self, seconds=None):
        end = None if seconds is None else self.clock() + max(0.0, seconds)
        while True:
            if self.is_set():
                return True
            remaining = None if end is None else end - self.clock()
            if remaining is not None and remaining <= 0:
                return False
            duration = .05 if remaining is None else min(.05, remaining)
            if self.cancel is not None and hasattr(self.cancel, 'wait'):
                self.cancel.wait(duration)
            else:
                self._event.wait(duration)

    def finish(self):
        with self._lock:
            if self.ended is None:
                self.ended = self.clock()
        self._update()

    def queue_remaining(self):
        return (None if self.limits.queue is None else
                max(0.0, self.limits.queue - self._queue_seconds(self.clock())))

    def _queue_seconds(self, at):
        return self.queue_elapsed + (max(0.0, at - self.queue_started)
                                     if self.queue_started is not None else 0)

    def _review_seconds(self, at):
        if self.review_started is None:
            return 0.0
        foreground_wait = max(0.0, self._queue_seconds(at) - self.queue_at_review_start)
        return max(0.0, at - self.review_started - foreground_wait)

    def snapshot(self):
        with self._lock:
            at = self.clock() if self.ended is None else self.ended
            queue = self._queue_seconds(at)
            provider = self.provider_wait_elapsed + (max(0.0, at - self.provider_wait_started)
                                                     if self.provider_wait_started is not None else 0)
            def utc(deadline):
                return None if deadline is None else (
                    self.started_utc + timedelta(seconds=deadline - self.started)
                ).strftime('%Y-%m-%dT%H:%M:%SZ')
            return {
                'scope': 'request_execution',
                'phase': ('finished' if self.ended is not None else 'queue' if self.queue_started is not None
                          else 'provider_wait' if self.provider_waiters else
                          'review' if self.review_started is not None else 'preflight'),
                'limits': self.limits.to_dict(),
                'deadlines': {
                    'queue': utc(self.queue_deadline_mono),
                    'review': utc(at + self.limits.review - self._review_seconds(at))
                              if self.review_started is not None and self.limits.review is not None
                              and self.queue_started is None else None,
                    'total': utc(self.started + self.limits.total) if self.limits.total is not None else None,
                    'provider_wait': utc(self.provider_deadline_mono)},
                'timing': {'queue_wait_ms': round(queue * 1000),
                           'provider_wait_ms': round(provider * 1000),
                           'review_wall_ms': round(max(0.0, at - self.review_started) * 1000)
                                             if self.review_started is not None else 0,
                           'review_active_ms': round(self._review_seconds(at) * 1000),
                           'total_ms': round(max(0.0, at - self.started) * 1000)},
                'reason_code': self._reason,
                'review_paused_for_queue': self.queue_started is not None and self.review_started is not None,
                'provider_waits': {'active_count': len(self._waits),
                    'deadlines': sorted(utc(deadline) for deadline in self._waits.values())},
                'updated_at': utc(at),
            }


def current(store=None):
    """Return only the current execution's controller, never another Store's."""
    from .requests import current as request_context
    context = request_context()
    if context is None or (store is not None and context.store is not store):
        return None
    return context.budget


def foreground_wait(fn):
    """Scope the FG wait around the actual shipped admission operation."""
    @wraps(fn)
    def run(store, **kwargs):
        controller = current(store)
        if controller is None:
            return fn(store, **kwargs)
        remaining = controller.queue_remaining()
        if remaining is not None:
            kwargs['wait_sec'] = min(kwargs['wait_sec'], remaining)
        controller.start_queue(kwargs['wait_sec'])
        ticket = None
        try:
            ticket = fn(store, **kwargs)
            return ticket
        finally:
            # Refresh before leaving queue phase so boundary expiry stays
            # queue_budget_exhausted, including a timeout between polls.
            try:
                controller.is_set()
                controller.end_queue()
            except BaseException:
                if ticket is not None:
                    from .capacity import finish, STATUS_REJECTED
                    finish(store, ticket, status=STATUS_REJECTED, expire_reason='error')
                raise
    return run


def record_capacity(store, ticket, effective, *, configured=None, legacy=None):
    from .requests import current as request_context, now
    context = request_context()
    if context is None or context.store is not store or context.budget is None:
        return
    if not store.record_request_capacity(
            context.id, context.execution_seq, context.owner_token,
            admission_id=ticket.id, resource_class=ticket.resource_class,
            scope=ticket.scope, effective_capacity=effective,
            configured_capacity=configured, legacy_dual_hold=legacy, updated_at=now()):
        raise RuntimeError('request capacity observation lost its execution owner')


def provider_wait_overhead(batch_count, *, extra_passes=0):
    """Extend old interop ceilings for newly allowed long provider waits.

    Existing ceilings include 60 seconds per primary/integration batch call.
    Reserve each possible pass's admission allowance against that grace.
    Counting only width one is conservative for a wider configured chain.
    """
    controller = current()
    if controller is None:
        return 0
    from .budget import GRACE_SEC
    calls = batch_count + 1 if batch_count else 1
    passes = calls + extra_passes
    return math.ceil(max(0.0, controller.limits.provider_wait * passes - GRACE_SEC * calls))
