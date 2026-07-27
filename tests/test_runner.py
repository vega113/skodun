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
import time

import pytest

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


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_completes_within_budget(tmp_path):
    r = run_with_watchdog(
        [sys.executable, "-c", "print('hi')"],
        10,
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
    )
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_text(encoding="utf-8").strip() == "hi"
    assert r.duration_sec >= 0.0


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


def test_timeout_leaves_no_stray_process_group(tmp_path):
    # The child is its own process-group leader, so its pid IS the pgid.
    pidfile = tmp_path / "child.pid"
    code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    r = run_with_watchdog(
        [sys.executable, "-c", code], 1, tmp_path, tmp_path / "out", tmp_path / "err"
    )
    assert r.timed_out
    pgid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2.0
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _group_exists(pgid), f"process group {pgid} survived the watchdog"
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


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
