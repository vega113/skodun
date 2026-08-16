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
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ids

if TYPE_CHECKING:
    import threading

    from .store import Store

#: Foreground review capacity class (S3/S4).
RESOURCE_REVIEW_FG = "review-fg"
#: Machine-wide outer bound across every repo that shares this store.
RESOURCE_REVIEW_MACHINE = "review-machine"
#: Scope for the outer ticket; one row universe per store file.
MACHINE_SCOPE = "*"
#: Optional name reserved for later; not wired.
RESOURCE_REVIEW_BG = "review-bg"
#: Prefix for per-provider slots: ``provider:xai``, ``provider:openai``, …
PROVIDER_CLASS_PREFIX = "provider:"

DEFAULT_CAPACITY = 1
CAPACITY_ENV = "SKODUN_REVIEW_FG_CAPACITY"
DEFAULT_MACHINE_CAPACITY = 1
MACHINE_CAPACITY_ENV = "SKODUN_REVIEW_MACHINE_CAPACITY"
ADMISSION_WAIT_ENV = "SKODUN_ADMISSION_WAIT_SECONDS"
LEGACY_FG_LOCK_ENV = "SKODUN_LEGACY_FG_LOCK"
PROVIDER_MAX_IN_FLIGHT_ENV = "SKODUN_PROVIDER_MAX_IN_FLIGHT"
DEFAULT_PROVIDER_MAX_IN_FLIGHT = 1
ETA_SAMPLE_K = 20
ETA_MIN_SAMPLES = 3

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
MAX_ADMISSION_WAIT_SEC = 24 * 3600.0


class AdmissionError(RuntimeError):
    """Base for capacity admission refusals."""


class AdmissionTimeout(AdmissionError):
    """The waiter exhausted its bounded admission budget."""

    def __init__(self, message: str, *, ticket: object | None = None) -> None:
        super().__init__(message)
        self.ticket = ticket


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
    queue_wait_ms: int | None = None
    expire_reason: str | None = None
    position: int | None = None
    review_id: str | None = None
    #: Outer machine ticket when this is a per-repo ``review-fg`` holder.
    parent: "Ticket | None" = None


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


def machine_capacity_from_env(env: Mapping[str, str] | None = None) -> int:
    """``SKODUN_REVIEW_MACHINE_CAPACITY`` ≥ 1; junk / missing → default 1."""
    env = os.environ if env is None else env
    raw = env.get(MACHINE_CAPACITY_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_MACHINE_CAPACITY
    try:
        value = int(str(raw).strip(), 10)
    except ValueError:
        return DEFAULT_MACHINE_CAPACITY
    if value < 1:
        return DEFAULT_MACHINE_CAPACITY
    return value


def resolved_machine_capacity(cfg: object | None = None,
                              env: Mapping[str, str] | None = None) -> int:
    """Env wins when set; otherwise optional ``cfg.capacity.machine``; else 1."""
    env = os.environ if env is None else env
    if str(env.get(MACHINE_CAPACITY_ENV) or "").strip():
        return machine_capacity_from_env(env)
    machine = getattr(getattr(cfg, "capacity", None), "machine", None)
    if isinstance(machine, int) and not isinstance(machine, bool) and machine >= 1:
        return machine
    return machine_capacity_from_env(env)


def resolved_fg_capacity(cfg: object | None = None,
                         env: Mapping[str, str] | None = None) -> int:
    """Inner FG cap: env if set, else file, then clipped by the machine cap."""
    env = os.environ if env is None else env
    if str(env.get(CAPACITY_ENV) or "").strip():
        repo = capacity_from_env(env)
    else:
        review_fg = getattr(getattr(cfg, "capacity", None), "review_fg", None)
        if (isinstance(review_fg, int) and not isinstance(review_fg, bool)
                and review_fg >= 1):
            repo = review_fg
        else:
            repo = capacity_from_env(env)
    return effective_fg_capacity(repo, resolved_machine_capacity(cfg, env))


def effective_fg_capacity(repo_capacity: int, machine_capacity: int) -> int:
    """Inner FG slots cannot exceed the machine-wide outer cap."""
    repo = DEFAULT_CAPACITY if int(repo_capacity) < 1 else int(repo_capacity)
    machine = (DEFAULT_MACHINE_CAPACITY if int(machine_capacity) < 1
               else int(machine_capacity))
    return min(repo, machine)


def legacy_fg_lock_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether dual-hold of the legacy mkdir FG lock is enabled.

    Normative (epic S4): unset/empty/``1``/junk → **on** (True); exact ``0``
    → **off** (False). Must **not** reuse duration helpers that reject ``0``.
    """
    env = os.environ if env is None else env
    raw = env.get(LEGACY_FG_LOCK_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip() != "0"


def provider_max_in_flight_from_env(env: Mapping[str, str] | None = None) -> int:
    """``SKODUN_PROVIDER_MAX_IN_FLIGHT`` ≥ 1; junk → default 1."""
    env = os.environ if env is None else env
    raw = env.get(PROVIDER_MAX_IN_FLIGHT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_PROVIDER_MAX_IN_FLIGHT
    try:
        value = int(str(raw).strip(), 10)
    except ValueError:
        return DEFAULT_PROVIDER_MAX_IN_FLIGHT
    if value < 1:
        return DEFAULT_PROVIDER_MAX_IN_FLIGHT
    return value


def provider_resource_class(provider_id: str, quota_pool: str | None = None) -> str:
    """Return a provider-namespaced capacity class for one quota pool.

    Automatically derived pools already carry their provider prefix (for
    example ``google:gemini``), so retain that compact legacy spelling. An
    explicit pool may be shared by configurations from different providers;
    namespace that form to prevent cross-provider admission collisions.
    """
    pid = str(provider_id or "").strip()
    if not pid:
        raise ValueError("provider_id must be a non-empty string")
    pool = str(quota_pool or pid).strip()
    if not pool:
        raise ValueError("quota_pool must be a non-empty string")
    key = (pool if pool == pid or pool.startswith(f"{pid}:")
           else f"{pid}:{pool}")
    return f"{PROVIDER_CLASS_PREFIX}{key}"


def wait_eta_p50_ms(samples: Sequence[int], *, min_samples: int = ETA_MIN_SAMPLES,
                    max_samples: int = ETA_SAMPLE_K) -> int | None:
    """p50 of recent wait_ms samples, or None if fewer than ``min_samples``."""
    cleaned = [int(x) for x in samples if isinstance(x, int) and not isinstance(x, bool) and x >= 0]
    if len(cleaned) < min_samples:
        return None
    ordered = sorted(cleaned)[:max_samples]
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def format_wait_progress(resource_class: str, position: int, remaining_sec: float,
                         *, eta_sec: float | None = None) -> str:
    """One progress line: position, wait budget, optional ETA."""
    msg = (f"{resource_class} queue position {position}; "
           f"wait budget {max(remaining_sec, 0.0):g}s remaining")
    if eta_sec is not None and eta_sec >= 0:
        msg += f"; eta≈{int(eta_sec)}s"
    return msg


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
    if (value < 0 or value > MAX_ADMISSION_WAIT_SEC
            or not math.isfinite(value)):
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
        queue_wait_ms=row.get("queue_wait_ms"),
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
    ticket.queue_wait_ms = updated.queue_wait_ms
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
    """Terminal transition: released / expired / rejected.

    If this ticket holds a machine-wide parent, the parent is finished with
    the same status so two repos cannot leak the outer slot.
    """
    parent = ticket.parent
    try:
        row = store.capacity_finish(
            ticket.id, status=status, expire_reason=expire_reason)
        _apply_row(ticket, row)
    finally:
        if parent is not None:
            ticket.parent = None
            finish(store, parent, status=status, expire_reason=expire_reason)
    return ticket


def _cancelled(cancel: "threading.Event | None") -> bool:
    return cancel is not None and cancel.is_set()


def _resolve_capacity(capacity: int | None,
                      capacity_fn: Callable[[], int] | None) -> int:
    """Resolve admit capacity for one poll; ``0`` is valid (no admits)."""
    if capacity_fn is not None:
        try:
            value = int(capacity_fn())
        except Exception:
            value = DEFAULT_CAPACITY
        return value if value >= 0 else DEFAULT_CAPACITY
    if capacity is None:
        return DEFAULT_CAPACITY
    value = int(capacity)
    return DEFAULT_CAPACITY if value < 0 else value


def acquire(store: "Store", *, scope: str,
            resource_class: str = RESOURCE_REVIEW_FG,
            capacity: int | None = None,
            wait_sec: float,
            poll_sec: float = 0.05,
            stale_sec: float = DEFAULT_STALE_SEC,
            cancel: "threading.Event | None" = None,
            on_progress: Callable[[str], None] | None = None,
            capacity_fn: Callable[[], int] | None = None,
            clock: Callable[[], float] | None = None,
            sleep: Callable[[float], None] | None = None,
            pid_alive_fn: Callable[[int], bool] | None = None) -> Ticket:
    """Enqueue and wait until a store slot is admitted and marked running.

    Store-only path (no legacy lock). Used by unit tests and any future
    resource class that does not dual-hold the FG mkdir lock.

    Each loop reclaims dead/stale peer rows (see ``should_reclaim_admission``)
    before the FIFO admit attempt so a SIGKILLed predecessor cannot poison
    the queue permanently.

    ``capacity=0`` is valid and means **no admits** (S4 provider pressure
    reduction while a provider is in quota backoff). ``None`` and negative
    static values fall back to ``DEFAULT_CAPACITY``.

    When ``capacity_fn`` is set it is re-evaluated **every poll** before
    ``try_admit`` so cross-process pressure reduction (e.g. quota → effective
    0) takes effect for waiters already queued, not only for new acquires.
    """
    now = time.monotonic if clock is None else clock
    pause = time.sleep if sleep is None else sleep

    ticket = enqueue(store, scope=scope, resource_class=resource_class)
    budget = float(wait_sec)
    deadline = now() + budget
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
                    f"gave up after {budget:g}s waiting for {resource_class} "
                    f"capacity (scope={scope})", ticket=ticket)

            # Drop dead/stale peers before position/admit so FIFO is honest.
            reclaim_stale(
                store, scope=scope, resource_class=resource_class,
                stale_sec=stale_sec, pid_alive_fn=pid_alive_fn)

            pos = store.capacity_position(ticket.id)
            ticket.position = pos
            if on_progress is not None and pos is not None and pos != noted_pos:
                noted_pos = pos
                eta = _eta_seconds(store, resource_class, scope)
                on_progress(format_wait_progress(
                    resource_class, pos, max(remaining, 0.0), eta_sec=eta))

            attempted = True
            cap = _resolve_capacity(capacity, capacity_fn)
            try_admit(store, ticket, capacity=cap)
            if ticket.status in HOLDER_STATUSES:
                mark_started(store, ticket)
                return ticket

            remaining = deadline - now()
            if remaining <= 0:
                finish(store, ticket, status=STATUS_EXPIRED,
                       expire_reason=REASON_ADMISSION_TIMEOUT)
                raise AdmissionTimeout(
                    f"gave up after {budget:g}s waiting for {resource_class} "
                    f"capacity (scope={scope})", ticket=ticket)
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


def _eta_seconds(store: "Store", resource_class: str, scope: str) -> float | None:
    try:
        samples = store.capacity_terminal_wait_ms(
            resource_class, scope, limit=ETA_SAMPLE_K)
    except Exception:
        return None
    ms = wait_eta_p50_ms(samples)
    if ms is None:
        return None
    return ms / 1000.0


def acquire_for_fg(
        store: "Store", *, scope: str,
        capacity: int | None = None,
        wait_sec: float,
        poll_sec: float,
        stale_sec: float = DEFAULT_STALE_SEC,
        cancel: "threading.Event | None" = None,
        on_progress: Callable[[str], None] | None = None,
        try_lock: Callable[[float], bool] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None,
        machine_capacity: int | None = None) -> Ticket:
    """FG admission: machine outer cap, then per-repo FIFO, optional dual-hold.

    When ``try_lock`` is ``None``, this is store-only multi-slot admit (S4
    dual-hold off): same as :func:`acquire` for ``review-fg``.

    When ``try_lock`` is set, only the FIFO head may call it; success force-
    admits and marks running (S3 dual-hold).

    The machine ticket is always acquired first (scope ``*``, class
    ``review-machine``) so two MCP/CLI processes sharing the store cannot
    both run when the outer cap is 1. The inner ``review-fg`` capacity is
    ``min(repo, machine)``.
    """
    now = time.monotonic if clock is None else clock
    machine_cap = (machine_capacity_from_env() if machine_capacity is None
                   else int(machine_capacity))
    if machine_cap < 1:
        machine_cap = DEFAULT_MACHINE_CAPACITY
    repo_cap = DEFAULT_CAPACITY if capacity is None else int(capacity)
    if repo_cap < 1:
        repo_cap = DEFAULT_CAPACITY
    inner_cap = effective_fg_capacity(repo_cap, machine_cap)
    deadline = now() + float(wait_sec)
    machine_ticket = acquire(
        store, scope=MACHINE_SCOPE, resource_class=RESOURCE_REVIEW_MACHINE,
        capacity=machine_cap, wait_sec=wait_sec, poll_sec=poll_sec,
        stale_sec=stale_sec, cancel=cancel, on_progress=on_progress,
        clock=clock, sleep=sleep, pid_alive_fn=pid_alive_fn)
    remaining = deadline - now()
    try:
        ticket = _acquire_repo_fg(
            store, scope=scope, capacity=inner_cap,
            wait_sec=max(remaining, 0.0), poll_sec=poll_sec,
            stale_sec=stale_sec, cancel=cancel, on_progress=on_progress,
            try_lock=try_lock, clock=clock, sleep=sleep,
            pid_alive_fn=pid_alive_fn)
    except BaseException:
        finish(store, machine_ticket, status=STATUS_REJECTED,
               expire_reason="inner_admit_failed")
        raise
    ticket.parent = machine_ticket
    return ticket


def _acquire_repo_fg(
        store: "Store", *, scope: str,
        capacity: int,
        wait_sec: float,
        poll_sec: float,
        stale_sec: float,
        cancel: "threading.Event | None",
        on_progress: Callable[[str], None] | None,
        try_lock: Callable[[float], bool] | None,
        clock: Callable[[], float] | None,
        sleep: Callable[[float], None] | None,
        pid_alive_fn: Callable[[int], bool] | None) -> Ticket:
    """Inner per-repo ``review-fg`` admit (legacy dual-hold unchanged)."""
    if try_lock is None:
        return acquire(
            store, scope=scope, resource_class=RESOURCE_REVIEW_FG,
            capacity=capacity, wait_sec=wait_sec, poll_sec=poll_sec,
            stale_sec=stale_sec, cancel=cancel, on_progress=on_progress,
            clock=clock, sleep=sleep, pid_alive_fn=pid_alive_fn)

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
                eta = _eta_seconds(store, RESOURCE_REVIEW_FG, scope)
                on_progress(format_wait_progress(
                    RESOURCE_REVIEW_FG, pos, max(remaining, 0.0), eta_sec=eta))

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
