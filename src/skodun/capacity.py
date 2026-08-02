"""Fair review capacity: FIFO admission + durable queue telemetry (epic S3).

Foreground reviews used to compete only on the legacy mkdir lock
(``grok-reviews-foreground.lock``). That is safe mutual exclusion but not
fair: multi-process waiters race, waits can run for the full stale ceiling
with no inference, and nothing durable records who waited or why an attempt
expired.

This module is the admission layer for resource class ``review-fg``:

* Waiters **enqueue** into the store (total order by ``queued_at``, ``id``).
* Only the FIFO head may transition to ``admitted`` while holders are under
  the configured capacity (default **1**).
* Admission wait is **bounded**; expiry is durable (``expired`` + reason) and
  that id is never requeued.
* Telemetry fields (``queued_at`` / ``admitted_at`` / ``started_at`` /
  ``ended_at`` / ``wait_ms`` / expire reason) live in ``capacity_admissions``.

Physical mutual exclusion with tubescribes/legacy scripts remains the
legacy FG lock. Callers dual-hold: this layer's ``acquire_for_fg`` takes a
store ticket and invokes a lock callback; the pipeline releases both.

Pure helpers (``decide_admit``, ``queue_position_among``) are free of I/O so
tests drive the real rule without re-implementing it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ids

if TYPE_CHECKING:
    import threading

    from .store import Store

#: Foreground review capacity class (this epic).
RESOURCE_REVIEW_FG = "review-fg"
#: Optional name reserved for later; not wired by S3.
RESOURCE_REVIEW_BG = "review-bg"

DEFAULT_CAPACITY = 1
CAPACITY_ENV = "SKODUN_REVIEW_FG_CAPACITY"
ADMISSION_WAIT_ENV = "SKODUN_ADMISSION_WAIT_SECONDS"

STATUS_QUEUED = "queued"
STATUS_ADMITTED = "admitted"
STATUS_RUNNING = "running"
STATUS_RELEASED = "released"
STATUS_EXPIRED = "expired"
STATUS_REJECTED = "rejected"

ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_ADMITTED, STATUS_RUNNING})
HOLDER_STATUSES = frozenset({STATUS_ADMITTED, STATUS_RUNNING})

REASON_ADMISSION_TIMEOUT = "admission_timeout"
REASON_CANCELLED = "cancelled"
REASON_STALE_PID = "stale_pid_dead"
REASON_STALE_AGE = "stale_age"

#: Default age ceiling for reclaiming holder rows when the caller does not
#: pass the lock stale ceiling. Large enough that short unit waits are not
#: age-reclaimed; dead-pid reclaim does not use this.
DEFAULT_STALE_SEC = 24 * 3600.0


class AdmissionError(RuntimeError):
    """Base for capacity admission refusals."""


class AdmissionTimeout(AdmissionError):
    """The waiter exhausted its bounded admission budget."""


class AdmissionCancelled(AdmissionError):
    """The cancel token fired while waiting for capacity."""


@dataclass(frozen=True)
class WaiterView:
    """One active admission row for pure FIFO decisions."""

    id: str
    status: str
    queued_at: str


@dataclass
class Ticket:
    """In-process handle for one capacity attempt."""

    id: str
    resource_class: str
    scope: str
    status: str
    queued_at: str
    admitted_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    wait_ms: int | None = None
    expire_reason: str | None = None
    position: int | None = None
    review_id: str | None = None


def capacity_from_env(env: Mapping[str, str] | None = None) -> int:
    """``SKODUN_REVIEW_FG_CAPACITY`` as an integer ≥ 1; junk → default 1."""
    env = os.environ if env is None else env
    raw = env.get(CAPACITY_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_CAPACITY
    try:
        value = int(str(raw).strip(), 10)
    except ValueError:
        return DEFAULT_CAPACITY
    if value < 1:
        return DEFAULT_CAPACITY
    return value


def admission_wait_from_env(default: float,
                            env: Mapping[str, str] | None = None) -> float:
    """``SKODUN_ADMISSION_WAIT_SECONDS`` or ``default``; junk → default."""
    env = os.environ if env is None else env
    raw = env.get(ADMISSION_WAIT_ENV)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(str(raw).strip())
    except ValueError:
        return float(default)
    if value < 0:
        return float(default)
    return value


def decide_admit(waiter_id: str, waiters: Sequence[WaiterView],
                 capacity: int) -> bool:
    """Pure FIFO admit rule.

    Eligible only when holders are under capacity and ``waiter_id`` is the
    earliest ``queued`` row by ``(queued_at, id)``.
    """
    if capacity < 1:
        return False
    holders = sum(1 for w in waiters if w.status in HOLDER_STATUSES)
    if holders >= capacity:
        return False
    queued = sorted(
        (w for w in waiters if w.status == STATUS_QUEUED),
        key=lambda w: (w.queued_at, w.id),
    )
    return bool(queued) and queued[0].id == waiter_id


def queue_position_among(waiter_id: str,
                         waiters: Sequence[WaiterView]) -> int | None:
    """1-based position among active waiters ordered by ``(queued_at, id)``."""
    ordered = sorted(waiters, key=lambda w: (w.queued_at, w.id))
    for i, w in enumerate(ordered, start=1):
        if w.id == waiter_id:
            return i
    return None


def pid_alive(pid: int) -> bool:
    """True when ``pid`` still exists (same posture as the FG lock reclaim)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # unknown: never reclaim on an ambiguous answer
    return True


def admission_age_sec(queued_at: str, now_epoch: float | None = None) -> float | None:
    """Seconds since ``queued_at`` (canonical store UTC Z), or None if junk."""
    import calendar
    from .store import _TS_FORMAT, _is_canonical_ts

    if not isinstance(queued_at, str) or not _is_canonical_ts(queued_at):
        return None
    try:
        started = calendar.timegm(time.strptime(queued_at, _TS_FORMAT))
    except ValueError:
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    return max(0.0, now - started)


def should_reclaim_admission(
        *, status: str, pid: int | None, queued_at: str,
        stale_sec: float,
        now_epoch: float | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None) -> str | None:
    """Return an expire reason if this active row should be reclaimed, else None.

    Multi-process safety: a SIGKILLed waiter left as ``queued`` (or a dead
    ``running`` holder) would otherwise poison FIFO forever. Rules:

    * known ``pid`` that is not alive → reclaim immediately (any active status);
    * holders (``admitted`` / ``running``) older than ``stale_sec`` → reclaim
      (matches FG lock stale reclaim, including wedged live pids);
    * ``queued`` with no usable pid and age past ``stale_sec`` → reclaim
      (cannot prove liveness);
    * live ``queued`` waiters are **not** age-reclaimed — admission timeout
      already bounds their wait.
    """
    alive_fn = pid_alive if pid_alive_fn is None else pid_alive_fn
    if pid is not None and int(pid) > 0:
        if not alive_fn(int(pid)):
            return REASON_STALE_PID
    age = admission_age_sec(queued_at, now_epoch)
    if age is None:
        return None
    ceiling = float(stale_sec)
    if ceiling < 0:
        ceiling = 0.0
    if status in HOLDER_STATUSES and age > ceiling:
        return REASON_STALE_AGE
    if (pid is None or int(pid) <= 0) and age > ceiling:
        return REASON_STALE_AGE
    return None


def reclaim_stale(
        store: "Store", *, scope: str,
        resource_class: str = RESOURCE_REVIEW_FG,
        stale_sec: float = DEFAULT_STALE_SEC,
        now_epoch: float | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None) -> list[str]:
    """Finish reclaimable active rows for ``scope``. Returns reclaimed ids."""
    return store.capacity_reclaim_stale(
        resource_class, scope, stale_sec=stale_sec,
        now_epoch=now_epoch, pid_alive_fn=pid_alive_fn)


def _ticket_from_row(row: Mapping) -> Ticket:
    return Ticket(
        id=row["id"],
        resource_class=row["resource_class"],
        scope=row["scope"],
        status=row["status"],
        queued_at=row["queued_at"],
        admitted_at=row.get("admitted_at"),
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        wait_ms=row.get("wait_ms"),
        expire_reason=row.get("expire_reason"),
        review_id=row.get("review_id"),
    )


def _apply_row(ticket: Ticket, row: Mapping) -> Ticket:
    updated = _ticket_from_row(row)
    ticket.status = updated.status
    ticket.admitted_at = updated.admitted_at
    ticket.started_at = updated.started_at
    ticket.ended_at = updated.ended_at
    ticket.wait_ms = updated.wait_ms
    ticket.expire_reason = updated.expire_reason
    ticket.review_id = updated.review_id
    return ticket


def enqueue(store: "Store", *, scope: str,
            resource_class: str = RESOURCE_REVIEW_FG,
            admission_id: str | None = None,
            pid: int | None = None) -> Ticket:
    """Register a new ``queued`` waiter. Returns the durable ticket."""
    aid = admission_id or ids.new_review_id(prefix="ca_")
    row = store.capacity_enqueue(
        admission_id=aid,
        resource_class=resource_class,
        scope=scope,
        pid=os.getpid() if pid is None else pid,
    )
    ticket = _ticket_from_row(row)
    ticket.position = store.capacity_position(aid)
    return ticket


def try_admit(store: "Store", ticket: Ticket, *, capacity: int) -> Ticket:
    """Attempt one FIFO admit transition. Updates ``ticket`` in place."""
    row = store.capacity_try_admit(ticket.id, capacity=capacity)
    if row is not None:
        _apply_row(ticket, row)
    ticket.position = store.capacity_position(ticket.id)
    return ticket


def mark_started(store: "Store", ticket: Ticket,
                 review_id: str | None = None) -> Ticket:
    """Mark an admitted ticket as ``running`` (review body under way)."""
    row = store.capacity_mark_started(ticket.id, review_id=review_id)
    _apply_row(ticket, row)
    return ticket


def finish(store: "Store", ticket: Ticket, *, status: str = STATUS_RELEASED,
           expire_reason: str | None = None) -> Ticket:
    """Terminal transition: released / expired / rejected."""
    row = store.capacity_finish(
        ticket.id, status=status, expire_reason=expire_reason)
    _apply_row(ticket, row)
    return ticket


def _cancelled(cancel: "threading.Event | None") -> bool:
    return cancel is not None and cancel.is_set()


def acquire(store: "Store", *, scope: str,
            resource_class: str = RESOURCE_REVIEW_FG,
            capacity: int | None = None,
            wait_sec: float,
            poll_sec: float = 0.05,
            stale_sec: float = DEFAULT_STALE_SEC,
            cancel: "threading.Event | None" = None,
            on_progress: Callable[[str], None] | None = None,
            clock: Callable[[], float] | None = None,
            sleep: Callable[[float], None] | None = None,
            pid_alive_fn: Callable[[int], bool] | None = None) -> Ticket:
    """Enqueue and wait until a store slot is admitted and marked running.

    Store-only path (no legacy lock). Used by unit tests and any future
    resource class that does not dual-hold the FG mkdir lock.

    Each loop reclaims dead/stale peer rows (see ``should_reclaim_admission``)
    before the FIFO admit attempt so a SIGKILLed predecessor cannot poison
    the queue permanently.
    """
    cap = DEFAULT_CAPACITY if capacity is None else int(capacity)
    if cap < 1:
        cap = DEFAULT_CAPACITY
    now = time.monotonic if clock is None else clock
    pause = time.sleep if sleep is None else sleep

    ticket = enqueue(store, scope=scope, resource_class=resource_class)
    deadline = now() + float(wait_sec)
    noted_pos: int | None = None
    # Always allow one attempt even when wait_sec is 0 (tests + free path).
    attempted = False

    try:
        while True:
            if _cancelled(cancel):
                finish(store, ticket, status=STATUS_REJECTED,
                       expire_reason=REASON_CANCELLED)
                raise AdmissionCancelled(
                    "the review was cancelled while it waited for "
                    f"{resource_class} capacity")

            remaining = deadline - now()
            if attempted and remaining <= 0:
                finish(store, ticket, status=STATUS_EXPIRED,
                       expire_reason=REASON_ADMISSION_TIMEOUT)
                raise AdmissionTimeout(
                    f"gave up after {wait_sec:g}s waiting for {resource_class} "
                    f"capacity (scope={scope})")

            # Drop dead/stale peers before position/admit so FIFO is honest.
            reclaim_stale(
                store, scope=scope, resource_class=resource_class,
                stale_sec=stale_sec, pid_alive_fn=pid_alive_fn)

            pos = store.capacity_position(ticket.id)
            ticket.position = pos
            if on_progress is not None and pos is not None and pos != noted_pos:
                noted_pos = pos
                on_progress(
                    f"{resource_class} queue position {pos}; "
                    f"wait budget {max(remaining, 0.0):g}s remaining")

            attempted = True
            try_admit(store, ticket, capacity=cap)
            if ticket.status in HOLDER_STATUSES:
                mark_started(store, ticket)
                return ticket

            remaining = deadline - now()
            if remaining <= 0:
                finish(store, ticket, status=STATUS_EXPIRED,
                       expire_reason=REASON_ADMISSION_TIMEOUT)
                raise AdmissionTimeout(
                    f"gave up after {wait_sec:g}s waiting for {resource_class} "
                    f"capacity (scope={scope})")
            slice_sleep = min(float(poll_sec), remaining)
            if slice_sleep > 0:
                pause(slice_sleep)
    except (AdmissionTimeout, AdmissionCancelled):
        raise
    except BaseException:
        if ticket.status in ACTIVE_STATUSES:
            finish(store, ticket, status=STATUS_REJECTED,
                   expire_reason="error")
        raise


def acquire_for_fg(
        store: "Store", *, scope: str,
        capacity: int | None = None,
        wait_sec: float,
        poll_sec: float,
        stale_sec: float = DEFAULT_STALE_SEC,
        cancel: "threading.Event | None" = None,
        on_progress: Callable[[str], None] | None = None,
        try_lock: Callable[[float], bool],
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None) -> Ticket:
    """FG dual-hold: FIFO eligibility, then ``try_lock``, then admit+start.

    ``try_lock(slice_sec)`` returns True when this process holds the legacy
    FG lock, False on ordinary contention. It may raise on cancel; the
    caller maps that to ``AdmissionCancelled`` or lets it propagate after
    this helper finishes the ticket in the bare ``except``.

    Only the FIFO head calls ``try_lock``, so later waiters cannot overtake
    on the mkdir race. After a successful lock, the ticket is force-admitted
    and marked ``running``. One attempt is always made even when
    ``wait_sec`` is 0 so a free lock path matches the legacy lock contract.

    Dead or stale peer capacity rows are reclaimed each iteration before the
    FIFO head is decided (``stale_sec`` should be the FG lock stale ceiling).
    """
    cap = DEFAULT_CAPACITY if capacity is None else int(capacity)
    if cap < 1:
        cap = DEFAULT_CAPACITY
    now = time.monotonic if clock is None else clock
    pause = time.sleep if sleep is None else sleep

    ticket = enqueue(store, scope=scope, resource_class=RESOURCE_REVIEW_FG)
    deadline = now() + float(wait_sec)
    noted_pos: int | None = None
    attempted = False

    try:
        while True:
            if _cancelled(cancel):
                finish(store, ticket, status=STATUS_REJECTED,
                       expire_reason=REASON_CANCELLED)
                raise AdmissionCancelled(
                    "the review was cancelled while it waited for "
                    "review-fg capacity")

            remaining = deadline - now()
            if attempted and remaining <= 0:
                finish(store, ticket, status=STATUS_EXPIRED,
                       expire_reason=REASON_ADMISSION_TIMEOUT)
                raise AdmissionTimeout(
                    f"gave up after {wait_sec:g}s waiting for review-fg "
                    f"capacity (scope={scope})")

            reclaim_stale(
                store, scope=scope, resource_class=RESOURCE_REVIEW_FG,
                stale_sec=stale_sec, pid_alive_fn=pid_alive_fn)

            pos = store.capacity_position(ticket.id)
            ticket.position = pos
            if on_progress is not None and pos is not None and pos != noted_pos:
                noted_pos = pos
                on_progress(
                    f"review-fg queue position {pos}; "
                    f"wait budget {max(remaining, 0.0):g}s remaining")

            attempted = True
            active = store.capacity_active_views(RESOURCE_REVIEW_FG, scope)
            if decide_admit(ticket.id, active, cap):
                slice_sec = max(min(float(poll_sec), max(remaining, 0.0)), 0.0)
                got = try_lock(slice_sec)
                if got:
                    row = store.capacity_force_admit(ticket.id)
                    if row is None:
                        finish(store, ticket, status=STATUS_REJECTED,
                               expire_reason="admit_race")
                        raise AdmissionTimeout(
                            "could not record review-fg admission after "
                            "taking the foreground lock")
                    _apply_row(ticket, row)
                    mark_started(store, ticket)
                    return ticket

            remaining = deadline - now()
            if remaining <= 0:
                finish(store, ticket, status=STATUS_EXPIRED,
                       expire_reason=REASON_ADMISSION_TIMEOUT)
                raise AdmissionTimeout(
                    f"gave up after {wait_sec:g}s waiting for review-fg "
                    f"capacity (scope={scope})")
            slice_sleep = min(float(poll_sec), remaining)
            if slice_sleep > 0:
                pause(slice_sleep)
    except (AdmissionTimeout, AdmissionCancelled):
        raise
    except BaseException:
        if ticket.status in ACTIVE_STATUSES:
            finish(store, ticket, status=STATUS_REJECTED,
                   expire_reason="error")
        raise
