"""Tests for the process-group watchdog runner.

Every failure mode of `skodun.runner` is timing- or process-related, so these
tests spawn real processes rather than mocking. Timing assertions are always
upper bounds (never exact durations) so a loaded machine does not make them
flaky.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from skodun import runner
from skodun.runner import run_with_watchdog


def _kill_ok(pid: int) -> bool:
    """True if `pid` still exists (and is signalable by us)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def _process_is_running(pid: int) -> bool:
    """Treat an unreaped zombie as stopped when procfs exposes its state."""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
    except OSError:
        return _kill_ok(pid)
    closing = raw.rfind(")")
    if closing < 0:
        return _kill_ok(pid)
    fields = raw[closing + 1:].split()
    return not fields or fields[0] != "Z"


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_completes_within_budget(tmp_path):
    t0 = time.monotonic()
    r = run_with_watchdog(
        [sys.executable, "-c", "print('hi')"],
        10,
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
    )
    wall_clock_elapsed = time.monotonic() - t0
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_text(encoding="utf-8").strip() == "hi"
    # `duration_sec >= 0.0` alone is vacuous on a monotonic clock -- it can
    # never be false. Bound it above by wall clock actually observed around
    # the call (plus slack for measurement overhead) so a bogus/huge value
    # would be caught.
    assert 0.0 <= r.duration_sec <= wall_clock_elapsed + 1.0


def test_stderr_is_captured_separately(tmp_path):
    code = "import sys; sys.stdout.write('O'); sys.stderr.write('E')"
    r = run_with_watchdog(
        [sys.executable, "-c", code], 10, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_text(encoding="utf-8") == "O"
    assert (tmp_path / "err").read_text(encoding="utf-8") == "E"


def test_nonzero_exit_code_is_reported_faithfully(tmp_path):
    r = run_with_watchdog(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        10,
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
    )
    assert r.rc == 3
    assert r.timed_out is False


def test_runs_in_the_given_cwd(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    code = "import os,sys; sys.stdout.write(os.path.realpath(os.getcwd()))"
    run_with_watchdog(
        [sys.executable, "-c", code], 10, workdir, tmp_path / "out", tmp_path / "err"
    )
    assert (tmp_path / "out").read_text(encoding="utf-8") == os.path.realpath(workdir)


def test_kills_whole_group_even_if_leader_dies_and_grandchild_ignores_term(tmp_path):
    # grandchild ignores SIGTERM and records its pid; leader dies on TERM
    gc = (
        "import os,signal,time;"
        f"open({str(tmp_path / 'gc.pid')!r},'w').write(str(os.getpid()));"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{gc!r}]);"
        "time.sleep(60)"
    )
    t0 = time.monotonic()
    r = run_with_watchdog(
        [sys.executable, "-c", code], 2, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    elapsed = time.monotonic() - t0
    assert r.timed_out
    # 2s budget + 3s TERM grace + slack; a hang would blow far past this.
    assert elapsed < 15, f"watchdog took {elapsed:.1f}s"

    gc_pid = int((tmp_path / "gc.pid").read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2.0
    while _kill_ok(gc_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    # The grandchild -- not merely the leader -- must be gone.
    assert not _kill_ok(gc_pid), f"grandchild {gc_pid} survived the watchdog"


def test_early_leader_exit_reaps_wrapper_descendants(tmp_path):
    """A clean wrapper exit must not leave its native child running."""
    pidfile = tmp_path / "gc.pid"
    gc = (
        "import os,time;"
        f"open({str(pidfile)!r},'w').write(str(os.getpid()));"
        "time.sleep(60)"
    )
    code = (
        "import pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{gc!r}])\n"
        f"p=pathlib.Path({str(pidfile)!r})\n"
        "while not p.exists():\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('wrapper stdout\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('wrapper stderr\\n'); sys.stderr.flush()\n"
    )
    result = run_with_watchdog(
        [sys.executable, "-c", code], 10, tmp_path,
        tmp_path / "out", tmp_path / "err",
    )
    assert result.rc == 0 and not result.timed_out
    assert result.descendants_killed
    gc_pid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2.0
    while _process_is_running(gc_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_is_running(gc_pid), \
        f"descendant {gc_pid} survived early wrapper exit"
    assert (tmp_path / "out").read_bytes() == b""
    assert (tmp_path / "err").read_bytes() == b""


def test_timeout_leaves_no_stray_process_group(tmp_path):
    # The child is its own process-group leader, so its pid IS the pgid.
    #
    # This test is vacuous under a plausible mutation: if `start_new_session`
    # were dropped, the child would run in *this test's own* process group
    # instead of forming a new one, so `os.killpg(child_pid, 0)` would raise
    # ProcessLookupError -- not because the group died, but because no group
    # with that ID (the child's bare pid) ever existed. `_group_exists` alone
    # would then trivially report "gone" even though the real child process is
    # still alive. Guard against that two ways: (1) also check the child pid
    # directly with `os.kill` (works regardless of grouping), and (2) confirm
    # the group is observed genuinely alive while the run is still in flight,
    # from a background thread, so "gone" at the end means something changed.
    pidfile = tmp_path / "child.pid"
    code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"

    observed_alive = threading.Event()
    pid_holder: dict[str, int] = {}

    def _watch_for_liveness() -> None:
        deadline = time.monotonic() + 5.0
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not pidfile.exists():
            return
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        pid_holder["pid"] = pid
        if _group_exists(pid) and _kill_ok(pid):
            observed_alive.set()

    watcher = threading.Thread(target=_watch_for_liveness)
    watcher.start()

    r = run_with_watchdog(
        [sys.executable, "-c", code], 1, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    watcher.join(timeout=5.0)

    assert r.timed_out
    assert "pid" in pid_holder, "child never wrote its pid file"
    assert observed_alive.is_set(), "process group was never observed alive while the run was in flight"

    pgid = pid_holder["pid"]
    deadline = time.monotonic() + 2.0
    while (_group_exists(pgid) or _kill_ok(pgid)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _kill_ok(pgid), f"child {pgid} survived the watchdog"
    assert not _group_exists(pgid), f"process group {pgid} survived the watchdog"
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def test_parent_interrupt_terminates_the_child(tmp_path, monkeypatch):
    # If the parent is interrupted (an exception in our own code, or a signal
    # like Ctrl-C) while blocked inside the poll loop, the child must still be
    # terminated. Without that, start_new_session=True makes things worse, not
    # better: the child is a detached session/process-group leader outside the
    # terminal's foreground group, so an interactive SIGINT would not even
    # reach it on its own -- it would run to completion, orphaned.
    #
    # Simulate the interruption by making the loop's own `time.sleep` raise,
    # rather than actually signalling this test process (which would be racy
    # to aim precisely and could disrupt the test runner). The first couple of
    # calls are left alone so the child has time to start and record its pid
    # before anything is interrupted.
    import skodun.runner as runner_mod

    real_sleep = time.sleep
    calls = {"n": 0}

    def _sleep_then_interrupt(seconds):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(runner_mod.time, "sleep", _sleep_then_interrupt)

    pidfile = tmp_path / "child.pid"
    code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"

    with pytest.raises(KeyboardInterrupt):
        run_with_watchdog(
            [sys.executable, "-c", code], 10, tmp_path, tmp_path / "out", tmp_path / "err"
        )

    deadline = time.monotonic() + 3.0
    while not pidfile.exists() and time.monotonic() < deadline:
        real_sleep(0.05)
    assert pidfile.exists(), "child never started"
    pid = int(pidfile.read_text(encoding="utf-8").strip())

    deadline = time.monotonic() + 2.0
    while (_group_exists(pid) or _kill_ok(pid)) and time.monotonic() < deadline:
        real_sleep(0.05)
    assert not _kill_ok(pid), f"child {pid} survived the parent's interruption"
    assert not _group_exists(pid), f"process group {pid} survived the parent's interruption"


def test_timed_out_stdout_is_discarded(tmp_path):
    # prints a plausible clean envelope, then hangs -- output must not survive
    code = (
        "print('{\"structuredOutput\":{\"summary\":\"ok\",\"findings\":[]}}',flush=True);"
        "import time;time.sleep(60)"
    )
    r = run_with_watchdog(
        [sys.executable, "-c", code], 2, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.timed_out
    assert (tmp_path / "out").read_bytes() == b""
    assert (tmp_path / "out").stat().st_size == 0


def test_large_stdout_does_not_deadlock(tmp_path):
    # Several MB -- far more than any pipe buffer would hold. This completes
    # only because stdout streams straight to a file instead of through a pipe.
    n_chunks, chunk = 80, 64 * 1024  # 5 MiB
    code = (
        "import sys\n"
        f"buf = b'x' * {chunk}\n"
        f"for _ in range({n_chunks}): sys.stdout.buffer.write(buf)\n"
        "sys.stdout.buffer.flush()\n"
    )
    r = run_with_watchdog(
        [sys.executable, "-c", code], 30, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").stat().st_size == n_chunks * chunk


def test_first_output_sec_is_none_when_process_never_writes(tmp_path):
    r = run_with_watchdog(
        [sys.executable, "-c", "import time; time.sleep(0.4)"],
        10,
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_bytes() == b""
    assert r.first_output_sec is None


def test_first_output_sec_is_recorded_when_process_writes(tmp_path):
    code = "print('hello', flush=True); import time; time.sleep(0.8)"
    r = run_with_watchdog(
        [sys.executable, "-c", code], 10, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.rc == 0 and not r.timed_out
    assert r.first_output_sec is not None
    assert 0.0 <= r.first_output_sec <= r.duration_sec


def test_first_output_sec_is_recorded_for_a_timed_out_run(tmp_path):
    # The phase signal must survive even though the output itself is discarded:
    # output-then-hang is a mid-generation stall, not a bootstrap stall.
    code = "print('partial', flush=True); import time; time.sleep(60)"
    r = run_with_watchdog(
        [sys.executable, "-c", code], 1, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.timed_out
    assert (tmp_path / "out").read_bytes() == b""
    assert r.first_output_sec is not None
    assert r.first_output_sec < 1.5


def _spy_open(monkeypatch):
    """Record every file object `runner` opens, so a leak is assertable.

    `runner` calls the builtin `open`, and a builtin is resolved through the
    module's own globals, so patching the name on the module intercepts every
    call this run makes -- stdout, stderr and (once it exists) the stdin file.
    The point is not the count but the closure: an fd this module opens and
    does not close outlives the run and, for the stdin file specifically, keeps
    a prompt file open for as long as the process lives.
    """
    opened = []
    real = open

    def spy(*a, **kw):
        f = real(*a, **kw)
        opened.append(f)
        return f

    import skodun.runner as runner_mod
    monkeypatch.setattr(runner_mod, "open", spy, raising=False)
    return opened


def test_stdin_path_is_fed_to_the_child(tmp_path):
    # The prompt travels as a FILE either way; `stdin_path` only says who opens
    # it. An adapter whose CLI takes the prompt on stdin (codex: argv ends in
    # `-`) HANGS until the watchdog kills it if this is not wired up.
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes("héllo from the prompt file\n".encode("utf-8"))
    r = run_with_watchdog(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        10, tmp_path, tmp_path / "out", tmp_path / "err", stdin_path=prompt,
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_bytes() == prompt.read_bytes()


def test_stdin_defaults_to_devnull(tmp_path):
    # The Phase 1 default, unchanged: a child that reads stdin gets EOF at
    # once rather than blocking on an inherited terminal.
    code = ("import sys; d = sys.stdin.buffer.read();"
            " sys.stdout.write('EOF' if d == b'' else repr(d))")
    r = run_with_watchdog(
        [sys.executable, "-c", code], 10, tmp_path, tmp_path / "out",
        tmp_path / "err",
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_text(encoding="utf-8") == "EOF"


def test_the_stdin_file_is_closed_on_the_normal_path(tmp_path, monkeypatch):
    opened = _spy_open(monkeypatch)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x\n", encoding="utf-8")
    run_with_watchdog(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], 10, tmp_path,
        tmp_path / "out", tmp_path / "err", stdin_path=prompt,
    )
    assert len(opened) == 3          # stdout, stderr, stdin
    assert all(f.closed for f in opened)


def test_the_stdin_file_is_closed_on_the_timeout_path(tmp_path, monkeypatch):
    opened = _spy_open(monkeypatch)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x\n", encoding="utf-8")
    r = run_with_watchdog(
        [sys.executable, "-c", "import time; time.sleep(60)"], 1, tmp_path,
        tmp_path / "out", tmp_path / "err", stdin_path=prompt,
    )
    assert r.timed_out
    assert len(opened) == 3
    assert all(f.closed for f in opened)


def test_the_stdin_file_is_closed_when_the_parent_is_interrupted(tmp_path,
                                                                 monkeypatch):
    import skodun.runner as runner_mod

    opened = _spy_open(monkeypatch)
    real_sleep = time.sleep
    calls = {"n": 0}

    def _sleep_then_interrupt(seconds):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(runner_mod.time, "sleep", _sleep_then_interrupt)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x\n", encoding="utf-8")
    with pytest.raises(KeyboardInterrupt):
        run_with_watchdog(
            [sys.executable, "-c", "import time; time.sleep(60)"], 10, tmp_path,
            tmp_path / "out", tmp_path / "err", stdin_path=prompt,
        )
    assert len(opened) == 3
    assert all(f.closed for f in opened)


def test_finishing_just_before_the_deadline_is_not_a_timeout(tmp_path):
    # The oracle re-checks liveness after its final tick so a run that lands in
    # the last moment keeps its (valid) output instead of being killed.
    code = "import time; time.sleep(0.9); print('done', flush=True)"
    r = run_with_watchdog(
        [sys.executable, "-c", code], 2, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.rc == 0
    assert r.timed_out is False
    assert (tmp_path / "out").read_text(encoding="utf-8").strip() == "done"
    assert r.duration_sec >= 0.9


# --------------------------------------------------------------------------
# the cancellation token (Task 10)
#
# The BACKGROUND worker's SIGTERM has to reach the provider, which runs in its
# OWN process group precisely so this watchdog can signal it -- a bare SIGTERM
# death of the worker would orphan a live model call and let it overlap the
# replacement review. The signal handler cannot kill the group itself (it does
# not know the pid), so it sets a token this loop checks.
# --------------------------------------------------------------------------


def test_review_cancelled_is_a_base_exception_that_carries_a_partial():
    """`BaseException`, not `Exception`, and that is load-bearing.

    Every layer between the tick loop and the worker catches `Exception` to
    demote rather than destroy a review (`pipeline._run_sub`,
    `pipeline._extra_pass`, `dispatch.build_dedup_evidence`). A cancellation
    caught by one of those would be turned into a degraded REVIEW -- a
    trustworthy-shaped record for a run that was killed -- instead of reaching
    the worker's failed finalize.
    """
    from skodun.runner import ReviewCancelled
    assert issubclass(ReviewCancelled, BaseException)
    assert not issubclass(ReviewCancelled, Exception)
    assert ReviewCancelled("x").partial is None
    assert ReviewCancelled("x", partial={"id": "r1"}).partial == {"id": "r1"}


def test_an_already_set_token_spawns_nothing_at_all(tmp_path):
    """Checked BEFORE `Popen`, so a token set while the previous attempt was
    being written up does not start one more model call."""
    marker = tmp_path / "ran"
    cancel = threading.Event()
    cancel.set()
    from skodun.runner import ReviewCancelled
    with pytest.raises(ReviewCancelled):
        run_with_watchdog(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
            10, tmp_path, tmp_path / "out", tmp_path / "err", cancel=cancel)
    assert not marker.exists(), "a cancelled run must not spawn a process"


def test_a_token_set_mid_run_kills_the_whole_group_and_raises(tmp_path):
    """The provider group dies, exactly as it does on a timeout.

    The child below leaks a grandchild that ignores SIGTERM into its own group,
    which is the shape the group SIGKILL exists for -- a model CLI that spawns
    a helper.
    """
    from skodun.runner import ReviewCancelled
    pidfile = tmp_path / "pgid"
    code = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpgid(0)))\n"
        "sys.stdout.write('x'); sys.stdout.flush()\n"
        "time.sleep(60)\n")
    cancel = threading.Event()
    t = threading.Timer(0.6, cancel.set)
    t.start()
    try:
        with pytest.raises(ReviewCancelled):
            run_with_watchdog([sys.executable, "-c", code], 60, tmp_path,
                              tmp_path / "out", tmp_path / "err", cancel=cancel)
    finally:
        t.cancel()
    pgid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _group_exists(pgid):
        time.sleep(0.05)
    assert not _group_exists(pgid), "the provider's process group outlived the cancel"


def test_no_token_is_the_shipped_behaviour_exactly(tmp_path):
    """`cancel=None` is the foreground/legacy path and changes nothing."""
    r = run_with_watchdog([sys.executable, "-c", "print('hi')"], 10, tmp_path,
                          tmp_path / "out", tmp_path / "err", cancel=None)
    assert r.rc == 0 and not r.timed_out


# --------------------------------------------------------------------------
# reading the token: a Ctrl-C is not an unreadable token
#
# Both readers guard the token because it crosses a signal-handler boundary and
# an Event-shaped-but-not value (a `Mock`, a stale proxy) must not turn a review
# into a crash. `KeyboardInterrupt` is the one thing that guard may not eat: in
# the FOREGROUND, `_sleep_or_cancelled` is what a lock wait blocks in, so a
# swallowed Ctrl-C there means the poll loop keeps going and the operator's
# interrupt does nothing. `SystemExit` is deliberately NOT re-raised -- the same
# distinction `svc_gate` makes (commit d204c6f): an arbitrary exit code, 0
# included, escaping a fail-closed path is worse than the bug.
# --------------------------------------------------------------------------


class _RaisingToken:
    """An Event-shaped token whose every read raises. Counts its reads."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.reads = 0

    def is_set(self):
        self.reads += 1
        raise self._exc

    def wait(self, timeout=None):
        self.reads += 1
        raise self._exc


def _no_sleep(monkeypatch):
    """Spy on `runner.time.sleep` and never actually sleep."""
    import skodun.runner as runner_mod
    slept = []
    monkeypatch.setattr(runner_mod.time, "sleep", slept.append)
    return slept


def test_a_ctrl_c_during_a_token_wait_aborts_the_wait(monkeypatch):
    """A foreground lock wait blocks in `cancel.wait(seconds)`. Swallowing the
    Ctrl-C raised there left the caller polling for the rest of the window."""
    from skodun.runner import _sleep_or_cancelled
    slept = _no_sleep(monkeypatch)
    token = _RaisingToken(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _sleep_or_cancelled(token, 30.0)
    assert token.reads == 1
    assert slept == [], "an interrupted wait must not fall back to sleeping"


def test_a_ctrl_c_while_reading_the_token_aborts_the_check(monkeypatch):
    """`_cancelled` has the same guard and the same rule."""
    from skodun.runner import _cancelled
    with pytest.raises(KeyboardInterrupt):
        _cancelled(_RaisingToken(KeyboardInterrupt()))


def test_a_system_exit_from_the_token_is_still_swallowed(monkeypatch):
    """NOT re-raised, deliberately. `SystemExit` carries an arbitrary code --
    including 0 -- and letting one out of the tick loop would turn a review
    that could not read its token into a clean exit."""
    from skodun.runner import _cancelled, _sleep_or_cancelled
    slept = _no_sleep(monkeypatch)
    assert _cancelled(_RaisingToken(SystemExit(0))) is False
    assert _sleep_or_cancelled(_RaisingToken(SystemExit(0)), 2.5) is False
    assert slept == [2.5], "the fallback sleep still bounds the wait"


def test_an_ordinary_unreadable_token_is_unchanged(monkeypatch):
    """The shipped behaviour for the case the guard exists for."""
    from skodun.runner import _cancelled, _sleep_or_cancelled
    slept = _no_sleep(monkeypatch)
    assert _cancelled(_RaisingToken(RuntimeError("stale proxy"))) is False
    assert _sleep_or_cancelled(_RaisingToken(RuntimeError("x")), 1.5) is False
    assert slept == [1.5]


# --------------------------------------------------------------------------
# EPERM from the group kill
# --------------------------------------------------------------------------


def test_a_group_we_may_not_signal_does_not_abort_the_kill_path(tmp_path,
                                                                monkeypatch):
    """`os.killpg` raises `PermissionError` as well as `ProcessLookupError`,
    and `_terminate_group` has to survive it.

    A child is its own group leader, so its PGID is its PID -- and once the
    group is gone that number is free to be reused. Signal it after the reuse
    and the kernel answers EPERM (or ESRCH if we are lucky). The window is the
    gap between deciding to kill and killing, which a loaded machine widens;
    this reproduced as a real full-suite failure.

    Letting EPERM propagate skipped everything after it: the grace loop, the
    unconditional final SIGKILL that `_terminate_group`'s docstring exists to
    explain, and `proc.wait()` -- so the run left an unreaped child and, on
    the SIGTERM call, a group that never got its SIGKILL. It also surfaced to
    the caller as `the review failed: PermissionError(...)` in place of the
    cancellation verdict.

    Swallowing it is also the SAFE reading: EPERM means the pgid is not ours
    to signal, so there is nothing useful left to do to it, and pressing on
    would only mean signalling somebody else's process.
    """
    import subprocess as _sp

    proc = _sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                     start_new_session=True)
    pg = os.getpgid(proc.pid)
    refused: list[int] = []

    def killpg(target, sig):
        refused.append(sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    try:
        rc = runner._terminate_group(proc, pg)
    finally:
        monkeypatch.undo()
        if proc.poll() is None:                 # pragma: no cover - safety net
            proc.kill()
            proc.wait()

    # It ran to the end: both signals were attempted, and the leader was
    # reaped rather than left for the OS.
    assert signal.SIGTERM in refused and signal.SIGKILL in refused
    assert rc is not None and proc.poll() is not None


def test_a_cancel_survives_a_group_we_may_not_signal(tmp_path, monkeypatch):
    """The same fault through the shipped entry point: the caller must still
    get `ReviewCancelled`, not the `PermissionError` underneath it.

    That distinction is the whole of the reported symptom -- `svc_review`'s
    general guard turns anything that is not `ReviewCancelled` into "the
    review failed", so a cancelled review reported a traceback instead of a
    cancellation.
    """
    from skodun.runner import ReviewCancelled

    real_killpg = os.killpg

    def killpg(target, sig):
        if sig in (signal.SIGTERM, signal.SIGKILL):
            real_killpg(target, sig)            # really kill it...
            raise PermissionError(1, "Operation not permitted")   # ...then EPERM
        return real_killpg(target, sig)

    cancel = threading.Event()
    t = threading.Timer(0.6, cancel.set)
    t.start()
    monkeypatch.setattr(runner.os, "killpg", killpg)
    try:
        with pytest.raises(ReviewCancelled):
            run_with_watchdog(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                60, tmp_path, tmp_path / "out", tmp_path / "err", cancel=cancel)
    finally:
        t.cancel()


def test_a_refused_signal_is_reported_rather_than_swallowed(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    """EPERM has two causes and they are not distinguishable from here.

    One is harmless: the group is gone and its pgid has been reused, so the
    refusal is about a stranger. The other is not: a descendant that changed
    credentials -- exec'ing a setuid helper -- leaves a group that is alive
    and that we may no longer signal, and returning quietly from the kill path
    would report a clean shutdown over a live process. `_group_alive` answers
    EPERM with `True` exactly because it cannot tell them apart either.

    So the shutdown continues (raising would skip the final SIGKILL and the
    reaping, which is worse), and the fact that the group could not be shown
    dead is said out loud.
    """
    import subprocess as _sp

    proc = _sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                     start_new_session=True)
    pg = os.getpgid(proc.pid)

    def killpg(target, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    try:
        runner._terminate_group(proc, pg)
    finally:
        monkeypatch.undo()
        if proc.poll() is None:                 # pragma: no cover - safety net
            proc.kill()
            proc.wait()

    said = capsys.readouterr().err
    assert f"provider group {pg} refused signal" in said, said
    assert "EPERM" in said


def test_a_ctrl_c_while_reporting_a_refused_signal_is_not_eaten(tmp_path,
                                                               monkeypatch):
    """In this module, "never raises" has exactly one exception, and it is
    the operator's own interrupt.

    `_cancelled` and `_sleep_or_cancelled` both re-raise `KeyboardInterrupt`
    out of their catch-all, and `_sleep_or_cancelled` does so because
    swallowing it left a Ctrl-C during a lock wait doing nothing at all
    (issue #6). A diagnostic on the shutdown path is not a reason to break
    that.
    """
    def boom(_message):
        raise KeyboardInterrupt

    monkeypatch.setattr("skodun.pipeline._note", boom)
    with pytest.raises(KeyboardInterrupt):
        runner._note_unsignalable(4242, signal.SIGKILL)


def test_an_ordinary_broken_sink_still_cannot_break_the_shutdown(tmp_path,
                                                                 monkeypatch):
    """Everything that is not the interrupt stays swallowed: a note is never
    worth failing a kill over."""
    def boom(_message):
        raise RuntimeError("the sink is gone")

    monkeypatch.setattr("skodun.pipeline._note", boom)
    runner._note_unsignalable(4242, signal.SIGKILL)      # must not raise
