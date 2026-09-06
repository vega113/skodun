"""Epic S3: fair review-fg capacity — FIFO, expire, telemetry, preflight.

Tests drive the shipped ``capacity`` helpers and ``Store`` admission methods
(and, where needed, ``pipeline.run_review``) — not a re-implementation of the
admit rule inside the test.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from skodun import capacity, config, gitio, pipeline
from skodun.capacity import (
    REASON_ADMISSION_TIMEOUT,
    REASON_STALE_AGE,
    REASON_STALE_PID,
    RESOURCE_REVIEW_FG,
    STATUS_EXPIRED,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_RELEASED,
    STATUS_RUNNING,
    WaiterView,
    acquire,
    acquire_for_fg,
    capacity_from_env,
    decide_admit,
    enqueue,
    finish,
    queue_position_among,
    reclaim_stale,
    should_reclaim_admission,
    try_admit,
)
from skodun.pipeline import LockTimeout, PreflightRefused
from skodun.store import SCHEMA_VERSION, Store


# ---------------------------------------------------------------------------
# pure FIFO helpers (real decide_admit / queue_position_among)
# ---------------------------------------------------------------------------


def test_decide_admit_is_fifo_under_capacity_one():
    a = WaiterView("a", STATUS_QUEUED, "2026-08-02T10:00:00Z")
    b = WaiterView("b", STATUS_QUEUED, "2026-08-02T10:00:01Z")
    assert decide_admit("a", [a, b], 1) is True
    assert decide_admit("b", [a, b], 1) is False


def test_decide_admit_blocks_when_holder_at_capacity():
    holder = WaiterView("h", STATUS_RUNNING, "2026-08-02T10:00:00Z")
    waiter = WaiterView("w", STATUS_QUEUED, "2026-08-02T10:00:01Z")
    assert decide_admit("w", [holder, waiter], 1) is False
    assert decide_admit("w", [holder, waiter], 2) is True


def test_queue_position_among_is_1_based_fifo_order():
    a = WaiterView("a", STATUS_QUEUED, "2026-08-02T10:00:02Z")
    b = WaiterView("b", STATUS_RUNNING, "2026-08-02T10:00:00Z")
    c = WaiterView("c", STATUS_QUEUED, "2026-08-02T10:00:01Z")
    assert queue_position_among("b", [a, b, c]) == 1
    assert queue_position_among("c", [a, b, c]) == 2
    assert queue_position_among("a", [a, b, c]) == 3
    assert queue_position_among("missing", [a, b, c]) is None


def test_capacity_from_env_defaults_and_rejects_junk():
    assert capacity_from_env({}) == 1
    assert capacity_from_env({"SKODUN_REVIEW_FG_CAPACITY": "3"}) == 3
    assert capacity_from_env({"SKODUN_REVIEW_FG_CAPACITY": "0"}) == 1
    assert capacity_from_env({"SKODUN_REVIEW_FG_CAPACITY": "nope"}) == 1


# ---------------------------------------------------------------------------
# store-backed admission (real Store methods)
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    st = Store.open(tmp_path / "cap.db")
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    yield st
    st.close()


def test_enqueue_try_admit_finish_persists_telemetry(store):
    t = enqueue(store, scope="/repo", admission_id="ca_1")
    assert t.status == STATUS_QUEUED
    assert t.queued_at
    row = store.capacity_get("ca_1")
    assert row is not None
    assert row["queued_at"] == t.queued_at
    assert row["admitted_at"] is None

    try_admit(store, t, capacity=1)
    assert t.status == "admitted"
    assert t.admitted_at
    capacity.mark_started(store, t, review_id="sk_x")
    assert t.status == STATUS_RUNNING
    assert t.started_at
    assert t.review_id == "sk_x"

    finish(store, t, status=STATUS_RELEASED)
    assert t.status == STATUS_RELEASED
    assert t.ended_at
    assert t.wait_ms is not None and t.wait_ms >= 0
    assert t.expire_reason is None

    final = store.capacity_get("ca_1")
    assert final["status"] == STATUS_RELEASED
    assert final["queued_at"] and final["admitted_at"] and final["started_at"]
    assert final["ended_at"] and final["wait_ms"] is not None


def test_fifo_second_waiter_does_not_overtake(store):
    first = enqueue(store, scope="/repo", admission_id="ca_first")
    second = enqueue(store, scope="/repo", admission_id="ca_second")
    assert store.capacity_position("ca_first") == 1
    assert store.capacity_position("ca_second") == 2

    assert try_admit(store, second, capacity=1).status == STATUS_QUEUED
    assert try_admit(store, first, capacity=1).status == "admitted"
    # Still blocked while first holds the slot.
    assert try_admit(store, second, capacity=1).status == STATUS_QUEUED

    finish(store, first, status=STATUS_RELEASED)
    assert try_admit(store, second, capacity=1).status == "admitted"


def test_acquire_expires_durably_without_requeue(store):
    holder = enqueue(store, scope="/repo", admission_id="ca_holder")
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)

    progress = []
    with pytest.raises(capacity.AdmissionTimeout):
        acquire(
            store, scope="/repo", capacity=1, wait_sec=0.05, poll_sec=0.01,
            on_progress=progress.append,
        )

    # acquire mints its own id; find the expired row.
    rows = store._c.execute(
        "SELECT * FROM capacity_admissions WHERE status=?",
        (STATUS_EXPIRED,)).fetchall()
    assert len(rows) == 1
    expired = dict(rows[0])
    assert expired["expire_reason"] == REASON_ADMISSION_TIMEOUT
    assert expired["ended_at"]
    assert expired["wait_ms"] is not None
    # Terminal: try_admit on that id must not resurrect it.
    ticket = capacity.Ticket(
        id=expired["id"], resource_class=expired["resource_class"],
        scope=expired["scope"], status=expired["status"],
        queued_at=expired["queued_at"])
    try_admit(store, ticket, capacity=1)
    assert store.capacity_get(expired["id"])["status"] == STATUS_EXPIRED


def test_acquire_progress_reports_queue_position_and_budget(store):
    holder = enqueue(store, scope="/repo", admission_id="ca_h")
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)

    notes = []

    def on_progress(msg: str) -> None:
        notes.append(msg)
        # Release the holder from the same thread after the waiter has seen
        # a position note (SQLite connections are not cross-thread).
        if holder.status == STATUS_RUNNING:
            finish(store, holder, status=STATUS_RELEASED)

    ticket = acquire(
        store, scope="/repo", capacity=1, wait_sec=2.0, poll_sec=0.02,
        on_progress=on_progress)
    assert ticket.status == STATUS_RUNNING
    assert any("queue position" in n and "wait budget" in n for n in notes)
    finish(store, ticket, status=STATUS_RELEASED)


def test_acquire_for_fg_dual_hold_order(store):
    """Later waiter cannot try_lock while an earlier queued peer is ahead."""
    lock_attempts: list[str] = []

    def try_lock_a(_slice: float) -> bool:
        lock_attempts.append("a")
        return True

    # First waiter takes the slot immediately.
    t1 = capacity.acquire_for_fg(
        store, scope="/repo", capacity=1, wait_sec=1, poll_sec=0.01,
        try_lock=try_lock_a)
    assert t1.status == STATUS_RUNNING
    assert lock_attempts == ["a"]

    attempts_b = []

    def try_lock_b(_slice: float) -> bool:
        attempts_b.append("b")
        return True

    # Second waiter: not FIFO head while t1 is running — must not call try_lock.
    with pytest.raises(capacity.AdmissionTimeout):
        capacity.acquire_for_fg(
            store, scope="/repo", capacity=1, wait_sec=0.08, poll_sec=0.02,
            try_lock=try_lock_b)
    assert attempts_b == []

    finish(store, t1, status=STATUS_RELEASED)
    t2 = capacity.acquire_for_fg(
        store, scope="/repo", capacity=1, wait_sec=1, poll_sec=0.01,
        try_lock=try_lock_b)
    assert attempts_b == ["b"]
    finish(store, t2, status=STATUS_RELEASED)


# ---------------------------------------------------------------------------
# pipeline preflight: entire chain unavailable
# ---------------------------------------------------------------------------


def _mini_cfg() -> config.Config:
    """Hand-built config with a two-entry finder chain (no global file merge)."""
    return config.Config(
        defaults=config.Defaults(
            max_diff_bytes=100_000, timeout_sec=30, max_turns=4,
            timeout_retries=0, degraded_retries=0),
        reviewers=(
            config.Reviewer(
                name="primary", provider="xai", model="test-model",
                role="finder", fallbacks=("backup",)),
            config.Reviewer(
                name="backup", provider="openai", model="test-model-2",
                role="finder"),
        ),
    )


def _linked_worktree(tmp_path: Path) -> Path:
    """A non-primary worktree so main-checkout preflight does not fire."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True,
                   capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "feat"],
        cwd=repo, check=True, capture_output=True)
    return wt


def test_preflight_short_circuits_when_entire_finder_chain_unavailable(
        tmp_path, monkeypatch):
    """Must refuse before burning the admission wait budget."""
    repo = _linked_worktree(tmp_path)
    cfg = _mini_cfg()
    st = Store.open(tmp_path / "s.db")
    # Both providers in the finder chain are cached-unavailable.
    far = "2099-01-01T00:00:00Z"
    st.mark_provider_unavailable("xai", "quota hit", "quota", far)
    st.mark_provider_unavailable("openai", "quota hit", "quota", far)

    waited = {"n": 0}
    real_acquire = capacity.acquire_for_fg

    def no_wait(*a, **k):
        waited["n"] += 1
        return real_acquire(*a, **k)

    monkeypatch.setattr(capacity, "acquire_for_fg", no_wait)
    monkeypatch.setattr(pipeline.capacity, "acquire_for_fg", no_wait)

    t0 = time.monotonic()
    with pytest.raises(PreflightRefused, match="entire finder provider chain"):
        pipeline.run_review(
            repo, cfg, st, lock_wait=30.0, lock_poll=5.0, lock_stale=30.0)
    elapsed = time.monotonic() - t0
    assert waited["n"] == 0, "admission must not run when chain is unavailable"
    assert elapsed < 5.0, "must not consume a long lock/admission wait"
    st.close()


def test_preflight_does_not_fire_when_one_provider_is_free(tmp_path):
    cfg = _mini_cfg()
    st = Store.open(tmp_path / "s.db")
    far = "2099-01-01T00:00:00Z"
    st.mark_provider_unavailable("xai", "quota hit", "quota", far)
    # openai free → short-circuit must not apply.
    reason = pipeline._finder_chain_unavailable(
        st, cfg, pipeline._reviewer_for(cfg, "finder"))
    assert reason is None
    st.close()


def test_missing_finder_binary_is_refused_before_review_fg_admission(
        tmp_path, monkeypatch):
    repo = _linked_worktree(tmp_path)
    cfg = config.Config(
        defaults=config.Defaults(timeout_sec=1, max_turns=1),
        reviewers=(config.Reviewer(name="finder", provider="xai",
                                   model="test-model"),),
    )
    st = Store.open(tmp_path / "s.db")
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "missing-grok"))
    called = {"n": 0}

    def no_admission(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("review-fg admission must not be acquired")

    monkeypatch.setattr(pipeline.capacity, "acquire_for_fg", no_admission)
    with pytest.raises(PreflightRefused, match="binary_unavailable"):
        pipeline.run_review(repo, cfg, st, lock_wait=30.0)
    assert called["n"] == 0
    st.close()


def test_run_review_surfaces_queue_progress_under_contention(tmp_path):
    """CLI path: progress callback from acquire_for_fg includes position/budget.

    Drives the shipped ``acquire_for_fg`` progress path (same strings
    ``run_review`` surfaces via ``_note`` / ``progress_sink``).
    """
    st = Store.open(tmp_path / "s.db")
    holder = enqueue(st, scope="/repo", admission_id="ca_holder")
    try_admit(st, holder, capacity=1)
    capacity.mark_started(st, holder)

    notes: list[str] = []

    def on_progress(msg: str) -> None:
        notes.append(msg)
        if holder.status == STATUS_RUNNING:
            finish(st, holder, status=STATUS_RELEASED)

    def try_lock(_slice: float) -> bool:
        return True

    ticket = capacity.acquire_for_fg(
        st, scope="/repo", capacity=1, wait_sec=2.0, poll_sec=0.02,
        on_progress=on_progress, try_lock=try_lock)
    assert ticket.status == STATUS_RUNNING
    assert any(
        "review-fg queue position" in n and "wait budget" in n for n in notes)
    finish(st, ticket, status=STATUS_RELEASED)
    st.close()


def test_mcp_policy_remains_refuse_if_busy():
    """S3 documents refuse-if-busy; pin the shipped constants and tool text."""
    from skodun import mcpserver

    assert mcpserver.BUSY_TEXT == "review already in flight"
    assert mcpserver.BUSY_STATUS == 2
    review = next(s for s in mcpserver.default_registry()
                  if s.name == "review")
    assert "refused" in review.description.lower()
    assert "not queued" in review.description.lower()


# ---------------------------------------------------------------------------
# dead / stale capacity reclaim (multi-process poison rows)
# ---------------------------------------------------------------------------


#: A pid that is not alive on this host (process lookup fails).
_DEAD_PID = 2_147_483_646


def test_should_reclaim_dead_pid_immediately():
    reason = should_reclaim_admission(
        status=STATUS_QUEUED, pid=_DEAD_PID,
        queued_at="2026-08-02T10:00:00Z", stale_sec=3600.0,
        now_epoch=1_700_000_000.0,
        pid_alive_fn=lambda p: False)
    assert reason == REASON_STALE_PID


def test_should_not_reclaim_live_queued_on_age_alone():
    reason = should_reclaim_admission(
        status=STATUS_QUEUED, pid=os.getpid(),
        queued_at="2020-01-01T00:00:00Z", stale_sec=1.0,
        pid_alive_fn=lambda p: True)
    assert reason is None


def test_should_reclaim_old_running_holder_by_age():
    reason = should_reclaim_admission(
        status=STATUS_RUNNING, pid=os.getpid(),
        queued_at="2020-01-01T00:00:00Z", stale_sec=1.0,
        pid_alive_fn=lambda p: True)
    assert reason == REASON_STALE_AGE


def test_reclaim_stale_marks_dead_queued_head_rejected(store):
    dead = enqueue(store, scope="/repo", admission_id="ca_dead_q",
                   pid=_DEAD_PID)
    live = enqueue(store, scope="/repo", admission_id="ca_live_q")
    assert store.capacity_position("ca_dead_q") == 1
    assert store.capacity_position("ca_live_q") == 2

    reclaimed = reclaim_stale(
        store, scope="/repo", stale_sec=3600.0,
        pid_alive_fn=lambda p: p != _DEAD_PID)
    assert "ca_dead_q" in reclaimed
    row = store.capacity_get("ca_dead_q")
    assert row["status"] == STATUS_REJECTED
    assert row["expire_reason"] == REASON_STALE_PID
    assert row["ended_at"] and row["wait_ms"] is not None
    # Live peer is now sole head.
    assert store.capacity_position("ca_live_q") == 1
    assert dead.status == STATUS_QUEUED  # in-memory ticket not auto-updated


def test_reclaim_stale_marks_dead_running_holder_rejected(store):
    holder = enqueue(store, scope="/repo", admission_id="ca_dead_run",
                     pid=_DEAD_PID)
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)
    assert store.capacity_get("ca_dead_run")["status"] == STATUS_RUNNING

    reclaimed = reclaim_stale(
        store, scope="/repo", stale_sec=3600.0,
        pid_alive_fn=lambda p: False)
    assert reclaimed == ["ca_dead_run"]
    row = store.capacity_get("ca_dead_run")
    assert row["status"] == STATUS_REJECTED
    assert row["expire_reason"] == REASON_STALE_PID


def test_acquire_for_fg_reclaims_dead_queued_head_and_admits(store):
    """A SIGKILLed peer left as queued head must not block try_lock forever."""
    enqueue(store, scope="/repo", admission_id="ca_poison_head",
            pid=_DEAD_PID)
    assert store.capacity_position("ca_poison_head") == 1

    lock_attempts: list[str] = []

    def try_lock(_slice: float) -> bool:
        lock_attempts.append("ok")
        return True

    ticket = acquire_for_fg(
        store, scope="/repo", capacity=1, wait_sec=1.0, poll_sec=0.01,
        stale_sec=3600.0, try_lock=try_lock,
        pid_alive_fn=lambda p: p != _DEAD_PID)
    assert ticket.status == STATUS_RUNNING
    assert lock_attempts == ["ok"]
    poison = store.capacity_get("ca_poison_head")
    assert poison["status"] == STATUS_REJECTED
    assert poison["expire_reason"] == REASON_STALE_PID
    finish(store, ticket, status=STATUS_RELEASED)


def test_acquire_for_fg_reclaims_dead_running_holder_and_admits(store):
    """A dead running holder must free the slot when the legacy lock is free."""
    holder = enqueue(store, scope="/repo", admission_id="ca_poison_run",
                     pid=_DEAD_PID)
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)
    assert store.capacity_get("ca_poison_run")["status"] == STATUS_RUNNING

    lock_attempts: list[str] = []

    def try_lock(_slice: float) -> bool:
        lock_attempts.append("ok")
        return True

    ticket = acquire_for_fg(
        store, scope="/repo", capacity=1, wait_sec=1.0, poll_sec=0.01,
        stale_sec=3600.0, try_lock=try_lock,
        pid_alive_fn=lambda p: p != _DEAD_PID)
    assert ticket.status == STATUS_RUNNING
    assert lock_attempts == ["ok"]
    poison = store.capacity_get("ca_poison_run")
    assert poison["status"] == STATUS_REJECTED
    assert poison["expire_reason"] == REASON_STALE_PID
    finish(store, ticket, status=STATUS_RELEASED)


def test_acquire_store_only_reclaims_dead_holder(store):
    holder = enqueue(store, scope="/repo", admission_id="ca_h_dead",
                     pid=_DEAD_PID)
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)

    ticket = acquire(
        store, scope="/repo", capacity=1, wait_sec=1.0, poll_sec=0.01,
        stale_sec=3600.0, pid_alive_fn=lambda p: p != _DEAD_PID)
    assert ticket.status == STATUS_RUNNING
    assert store.capacity_get("ca_h_dead")["status"] == STATUS_REJECTED
    finish(store, ticket, status=STATUS_RELEASED)


# ---------------------------------------------------------------------------
# S4 Phase A — dual-hold env + multi-slot store-only FG
# ---------------------------------------------------------------------------


def test_legacy_fg_lock_from_env_normative_bool():
    """Exact 0 → off; unset/empty/1/junk → on (not via duration helpers)."""
    assert capacity.legacy_fg_lock_from_env({}) is True
    assert capacity.legacy_fg_lock_from_env({"SKODUN_LEGACY_FG_LOCK": ""}) is True
    assert capacity.legacy_fg_lock_from_env({"SKODUN_LEGACY_FG_LOCK": "1"}) is True
    assert capacity.legacy_fg_lock_from_env({"SKODUN_LEGACY_FG_LOCK": "yes"}) is True
    assert capacity.legacy_fg_lock_from_env({"SKODUN_LEGACY_FG_LOCK": "0"}) is False
    # Duration helpers reject 0; this reader must not.
    assert capacity.legacy_fg_lock_from_env({"SKODUN_LEGACY_FG_LOCK": " 0 "}) is False


def test_provider_max_in_flight_from_env_defaults():
    assert capacity.provider_max_in_flight_from_env({}) == 1
    assert capacity.provider_max_in_flight_from_env(
        {"SKODUN_PROVIDER_MAX_IN_FLIGHT": "2"}) == 2
    assert capacity.provider_max_in_flight_from_env(
        {"SKODUN_PROVIDER_MAX_IN_FLIGHT": "0"}) == 1
    assert capacity.provider_max_in_flight_from_env(
        {"SKODUN_PROVIDER_MAX_IN_FLIGHT": "nope"}) == 1


def test_provider_resource_class_naming():
    assert capacity.provider_resource_class("xai") == "provider:xai"
    assert capacity.provider_resource_class("openai") == "provider:openai"
    assert capacity.provider_resource_class("xai", "shared") \
        != capacity.provider_resource_class("openai", "shared")
    with pytest.raises(ValueError):
        capacity.provider_resource_class("")


def test_s4_dual_hold_off_two_concurrent_store_only_holders(store):
    """T1: dual-hold off path (try_lock=None) admits N concurrent holders."""
    t1 = acquire_for_fg(
        store, scope="/repo", capacity=2, wait_sec=0.5, poll_sec=0.01,
        try_lock=None, machine_capacity=2)
    t2 = acquire_for_fg(
        store, scope="/repo", capacity=2, wait_sec=0.5, poll_sec=0.01,
        try_lock=None, machine_capacity=2)
    assert t1.status == STATUS_RUNNING
    assert t2.status == STATUS_RUNNING
    assert t1.id != t2.id
    assert store.capacity_holder_count(RESOURCE_REVIEW_FG, "/repo") == 2
    finish(store, t1, status=STATUS_RELEASED)
    finish(store, t2, status=STATUS_RELEASED)
    assert store.capacity_holder_count(RESOURCE_REVIEW_FG, "/repo") == 0


def test_s4_dual_hold_on_try_lock_still_required(store):
    """T2: with try_lock set, only FIFO head may call it (S3 dual-hold pin)."""
    held = []

    def try_lock_hold(_slice: float) -> bool:
        held.append("a")
        return True

    t1 = acquire_for_fg(
        store, scope="/repo", capacity=2, wait_sec=1, poll_sec=0.01,
        try_lock=try_lock_hold, machine_capacity=2)
    assert t1.status == STATUS_RUNNING
    assert held == ["a"]

    # Second waiter: dual-hold still serializes via try_lock gating even when
    # store capacity would allow a second holder — only the head calls lock.
    # With t1 running, FIFO head is still t1's holder slot; a new waiter is
    # not head until free... actually with capacity=2, second CAN be admitted
    # after try_lock. Dual-hold path: only FIFO head among *queued* may
    # try_lock; holders don't block try_lock of next if under capacity?
    # Looking at acquire_for_fg: it uses decide_admit style - holders count
    # toward capacity. With capacity=2 and 1 holder, second queued becomes
    # head of queue and may try_lock. So both can hold with capacity=2.
    # T2 dual-hold ON with capacity=1 is the regression pin for single mutex.
    finish(store, t1, status=STATUS_RELEASED)

    t_block = enqueue(store, scope="/repo", admission_id="ca_hold")
    try_admit(store, t_block, capacity=1)
    capacity.mark_started(store, t_block)

    lock_calls = []

    def try_lock_b(_slice: float) -> bool:
        lock_calls.append("b")
        return True

    with pytest.raises(capacity.AdmissionTimeout):
        acquire_for_fg(
            store, scope="/repo", capacity=1, wait_sec=0.08, poll_sec=0.02,
            try_lock=try_lock_b)
    # Not head while t_block holds sole capacity → no try_lock.
    assert lock_calls == []
    finish(store, t_block, status=STATUS_RELEASED)


# ---------------------------------------------------------------------------
# S4 Phase B — provider:<id> max_in_flight + capacity 0 pressure
# ---------------------------------------------------------------------------


def test_s4_provider_cap_one_second_waiter_blocks(store):
    """T3: max_in_flight=1 — second concurrent acquire waits/expires."""
    rc = capacity.provider_resource_class("xai")
    t1 = acquire(
        store, scope="xai", resource_class=rc, capacity=1,
        wait_sec=0.5, poll_sec=0.01)
    assert t1.status == STATUS_RUNNING
    assert store.capacity_holder_count(rc, "xai") == 1

    with pytest.raises(capacity.AdmissionTimeout):
        acquire(
            store, scope="xai", resource_class=rc, capacity=1,
            wait_sec=0.05, poll_sec=0.01)
    finish(store, t1, status=STATUS_RELEASED)
    t2 = acquire(
        store, scope="xai", resource_class=rc, capacity=1,
        wait_sec=0.5, poll_sec=0.01)
    assert t2.status == STATUS_RUNNING
    finish(store, t2, status=STATUS_RELEASED)


def test_s4_provider_cap_two_allows_two(store):
    """T4: max_in_flight=2 — two concurrent holders both running."""
    rc = capacity.provider_resource_class("xai")
    t1 = acquire(
        store, scope="xai", resource_class=rc, capacity=2,
        wait_sec=0.5, poll_sec=0.01)
    t2 = acquire(
        store, scope="xai", resource_class=rc, capacity=2,
        wait_sec=0.5, poll_sec=0.01)
    assert t1.status == STATUS_RUNNING and t2.status == STATUS_RUNNING
    assert store.capacity_holder_count(rc, "xai") == 2
    finish(store, t1, status=STATUS_RELEASED)
    finish(store, t2, status=STATUS_RELEASED)


def test_s4_provider_capacity_zero_never_admits(store):
    """T5: effective max_in_flight 0 (quota pressure) → no admit, timeout."""
    rc = capacity.provider_resource_class("xai")
    with pytest.raises(capacity.AdmissionTimeout):
        acquire(
            store, scope="xai", resource_class=rc, capacity=0,
            wait_sec=0.05, poll_sec=0.01)
    rows = store._c.execute(
        "SELECT status FROM capacity_admissions WHERE resource_class=?",
        (rc,)).fetchall()
    assert rows and all(r["status"] == STATUS_EXPIRED for r in rows)


def test_s4_capacity_fn_recheck_zero_blocks_mid_wait_admit(store):
    """capacity_fn re-evaluated each poll: pressure drop to 0 mid-wait denies admit."""
    rc = capacity.provider_resource_class("xai")
    holder = acquire(
        store, scope="xai", resource_class=rc, capacity=1,
        wait_sec=0.5, poll_sec=0.01)
    assert holder.status == STATUS_RUNNING
    state = {"cap": 1}
    notes = []

    def on_progress(msg: str) -> None:
        notes.append(msg)
        # Free the physical holder, but drop effective capacity so the waiter
        # must not take the slot (quota pressure arrived mid-wait).
        if holder.status == STATUS_RUNNING:
            finish(store, holder, status=STATUS_RELEASED)
            state["cap"] = 0

    with pytest.raises(capacity.AdmissionTimeout):
        acquire(
            store, scope="xai", resource_class=rc, wait_sec=0.2, poll_sec=0.02,
            capacity_fn=lambda: state["cap"], on_progress=on_progress)
    assert store.capacity_holder_count(rc, "xai") == 0
    assert notes  # waited long enough to see progress / re-check


def test_s4_wait_eta_p50_requires_min_samples():
    """T8 helper: p50 only with ≥3 samples."""
    assert capacity.wait_eta_p50_ms([]) is None
    assert capacity.wait_eta_p50_ms([10, 20]) is None
    assert capacity.wait_eta_p50_ms([10, 20, 30]) == 20
    assert capacity.wait_eta_p50_ms([100, 200, 300, 400]) is not None


def test_s4_format_wait_progress_labels_history_and_sample_count():
    msg = capacity.format_wait_progress("provider:xai", 2, 12.5, historical_median_sec=4.0, sample_count=3)
    assert "provider:xai queue position 2" in msg
    assert "wait budget 12.5s remaining" in msg
    assert "historical median wait=4s" in msg
    assert "samples=3" in msg
    assert "method=median" in msg
    assert "eta" not in msg
    bare = capacity.format_wait_progress("review-fg", 1, 5.0)
    assert "eta≈" not in bare


def test_s4_progress_eta_from_terminal_samples(store):
    """Progress lines include eta≈ when ≥3 terminal wait_ms exist."""
    # Seed three released admissions with known wait_ms via finish path.
    for i, ms in enumerate((1000, 2000, 3000), start=1):
        t = enqueue(store, scope="/repo", admission_id=f"ca_seed_{i}")
        try_admit(store, t, capacity=10)
        capacity.mark_started(store, t)
        finish(store, t, status=STATUS_RELEASED)
        # Overwrite wait_ms so p50 is deterministic.
        store._c.execute(
            "UPDATE capacity_admissions SET wait_ms=? WHERE id=?",
            (ms, t.id))
        store._c.commit()

    samples = store.capacity_terminal_wait_ms(RESOURCE_REVIEW_FG, "/repo")
    assert len(samples) >= 3
    assert capacity.wait_eta_p50_ms(samples) is not None

    notes = []
    holder = enqueue(store, scope="/repo", admission_id="ca_block")
    try_admit(store, holder, capacity=1)
    capacity.mark_started(store, holder)

    def on_progress(msg: str) -> None:
        notes.append(msg)
        if holder.status == STATUS_RUNNING:
            finish(store, holder, status=STATUS_RELEASED)

    ticket = acquire(
        store, scope="/repo", capacity=1, wait_sec=2.0, poll_sec=0.02,
        on_progress=on_progress)
    assert any("historical median wait=" in n and "samples=3" in n for n in notes)
    finish(store, ticket, status=STATUS_RELEASED)


# ---------------------------------------------------------------------------
# Machine-wide outer cap (shared store, inner review-fg stays per-repo)
# ---------------------------------------------------------------------------


def test_machine_capacity_from_env_defaults_and_rejects_junk():
    assert capacity.machine_capacity_from_env({}) == 1
    assert capacity.machine_capacity_from_env(
        {"SKODUN_REVIEW_MACHINE_CAPACITY": "2"}) == 2
    assert capacity.machine_capacity_from_env(
        {"SKODUN_REVIEW_MACHINE_CAPACITY": "0"}) == 1
    assert capacity.machine_capacity_from_env(
        {"SKODUN_REVIEW_MACHINE_CAPACITY": "nope"}) == 1


def test_effective_fg_capacity_is_min_of_machine_and_repo():
    assert capacity.effective_fg_capacity(8, 1) == 1
    assert capacity.effective_fg_capacity(1, 2) == 1
    assert capacity.effective_fg_capacity(3, 3) == 3


def test_two_repo_scopes_cannot_both_run_when_machine_cap_is_one(store):
    first = acquire_for_fg(
        store, scope="/repo-a/.git", capacity=1, wait_sec=0.2, poll_sec=0.01,
        try_lock=None, machine_capacity=1)
    assert first.status == STATUS_RUNNING
    with pytest.raises(capacity.AdmissionTimeout):
        acquire_for_fg(
            store, scope="/repo-b/.git", capacity=1, wait_sec=0.08,
            poll_sec=0.02, try_lock=None, machine_capacity=1)
    finish(store, first, status=STATUS_RELEASED)
    second = acquire_for_fg(
        store, scope="/repo-b/.git", capacity=1, wait_sec=0.2, poll_sec=0.01,
        try_lock=None, machine_capacity=1)
    assert second.status == STATUS_RUNNING
    finish(store, second, status=STATUS_RELEASED)


def test_repo_fg_env_cannot_exceed_machine_cap(store):
    first = acquire_for_fg(
        store, scope="/repo-a/.git", capacity=8, wait_sec=0.2, poll_sec=0.01,
        try_lock=None, machine_capacity=1)
    assert first.status == STATUS_RUNNING
    with pytest.raises(capacity.AdmissionTimeout):
        acquire_for_fg(
            store, scope="/repo-a/.git", capacity=8, wait_sec=0.08,
            poll_sec=0.02, try_lock=None, machine_capacity=1)
    finish(store, first, status=STATUS_RELEASED)
def test_same_second_fifo_uses_committed_enqueue_order(tmp_path, monkeypatch):
    from skodun import store as store_module
    monkeypatch.setattr(store_module, '_iso_now', lambda: '2026-09-05T00:00:00Z')
    with Store.open(tmp_path / 'db') as store:
        store.capacity_enqueue(admission_id='z-first', resource_class='provider:xai', scope='xai')
        store.capacity_enqueue(admission_id='a-second', resource_class='provider:xai', scope='xai')
        assert store.capacity_position('z-first') == 1
        assert store.capacity_try_admit('a-second', capacity=1) is None
        assert store.capacity_try_admit('z-first', capacity=1)['status'] == 'admitted'


def test_enqueue_order_survives_new_process_and_terminal_rows(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from skodun import store as store_module
    monkeypatch.setattr(store_module, '_iso_now', lambda: '2026-09-05T00:00:00Z')
    db = tmp_path / 'db'
    with Store.open(db) as store:
        store.capacity_enqueue(admission_id='z-first', resource_class='provider:xai', scope='xai')
        store.capacity_enqueue(admission_id='a-second', resource_class='provider:xai', scope='xai')
    script = "from skodun.store import Store; import sys; s=Store.open(sys.argv[1]); print(s.capacity_position('z-first')); print(s.capacity_try_admit('a-second',capacity=1)); s.close()"
    result = subprocess.run([sys.executable, '-c', script, str(db)], capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}, check=True)
    assert result.stdout.splitlines() == ['1', 'None']
    with Store.open(db) as store:
        store.capacity_try_admit('z-first', capacity=1)
        store.capacity_finish('z-first', status='released')
        store.capacity_enqueue(admission_id='0-third', resource_class='provider:xai', scope='xai')
        assert store.capacity_position('z-first') is None
        assert store.capacity_position('a-second') == 1
        assert store.capacity_try_admit('0-third', capacity=1) is None


def test_machine_holder_is_not_reclaimed_after_long_queue_or_execution(store):
    first = acquire_for_fg(store, scope="/repo-a", capacity=1, wait_sec=0.1,
                           poll_sec=0.01, machine_capacity=1)
    store._c.execute("UPDATE capacity_admissions SET queued_at='2000-01-01T00:00:00Z', "
                     "admitted_at='2000-01-02T00:00:00Z' WHERE id=?", (first.parent.id,))
    assert capacity.reclaim_stale(store, scope=capacity.MACHINE_SCOPE,
        resource_class=capacity.RESOURCE_REVIEW_MACHINE, stale_sec=0,
        pid_alive_fn=lambda pid: True) == []
    with pytest.raises(capacity.AdmissionTimeout):
        acquire_for_fg(store, scope="/repo-b", capacity=1, wait_sec=0.02,
                       poll_sec=0.01, machine_capacity=1, stale_sec=0,
                       pid_alive_fn=lambda pid: True)
    assert capacity.reclaim_stale(store, scope=capacity.MACHINE_SCOPE,
        resource_class=capacity.RESOURCE_REVIEW_MACHINE, stale_sec=0,
        pid_alive_fn=lambda pid: False) == [first.parent.id]
    finish(store, first)


def test_machine_ticket_released_when_inner_lock_fails(store):
    with pytest.raises(capacity.AdmissionTimeout):
        acquire_for_fg(store, scope="/repo-a", capacity=1, wait_sec=0.02,
                       poll_sec=0.01, machine_capacity=1, try_lock=lambda _: False)
    assert store.capacity_holder_count(capacity.RESOURCE_REVIEW_MACHINE,
                                        capacity.MACHINE_SCOPE) == 0


def test_machine_cap_is_shared_by_independent_processes(tmp_path):
    import os
    import subprocess
    import sys
    db = tmp_path / 'shared.db'
    script = '''
import sys
from pathlib import Path
from skodun.store import Store
from skodun.capacity import acquire_for_fg, finish, AdmissionTimeout
with Store.open(Path(sys.argv[1])) as store:
    try:
        ticket = acquire_for_fg(store, scope=sys.argv[2], capacity=8,
            machine_capacity=1, wait_sec=.1, poll_sec=.01)
    except AdmissionTimeout:
        print('blocked', flush=True)
    else:
        print('held', flush=True)
        if sys.argv[2] == 'repo-a':
            sys.stdin.readline()
        finish(store, ticket)
'''
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    with Store.open(db):
        pass
    with subprocess.Popen([sys.executable, '-c', script, str(db), 'repo-a'],
                          env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout.readline().strip() == 'held'
            blocked = subprocess.run([sys.executable, '-c', script, str(db), 'repo-b'],
                                     env=env, capture_output=True, text=True, timeout=10)
            assert blocked.returncode == 0, blocked.stderr
            assert blocked.stdout.strip() == 'blocked'
        finally:
            stdout, stderr = holder.communicate('\n', timeout=10)
        assert holder.returncode == 0, stderr
    admitted = subprocess.run([sys.executable, '-c', script, str(db), 'repo-b'],
                             env=env, capture_output=True, text=True, timeout=10)
    assert admitted.returncode == 0, admitted.stderr
    assert admitted.stdout.strip() == 'held'
