"""Run a model CLI under a hard wall-clock watchdog.

Ported from the oracle's ``run_grok_with_timeout``. Three properties matter and
are the reason this module exists at all:

1. **No pipes.** stdout/stderr stream *directly* to files. A pipe to the parent
   would fill on a large review and deadlock the child forever.
2. **No orphans.** The child is spawned as its own session/process-group leader,
   so the watchdog can signal the whole tree (the model CLI plus any helper it
   spawned) instead of orphaning grandchildren.
3. **A timed-out run's stdout is evidence of nothing.** A process can print a
   complete, clean-looking result envelope and then hang; parsing that output
   after retries are exhausted would mint a trustworthy "clean review" from a
   run that never finished. On timeout the stdout file is truncated to zero
   bytes, exactly as the oracle discards it (`: > "$_tmp"`).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Grace between SIGTERM and SIGKILL for the process group (oracle: `sleep 3`).
_TERM_GRACE_SEC = 3.0
# Watchdog tick. Finer than the oracle's 1s tick, which only sharpens the
# timeout edge and the time-to-first-output resolution.
_POLL_SEC = 0.25


@dataclass(frozen=True)
class RunResult:
    rc: int
    timed_out: bool
    duration_sec: float
    first_output_sec: float | None


def run_with_watchdog(
    cmd: list[str],
    timeout_sec: int,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> RunResult:
    """Run `cmd` with a hard wall-clock timeout, streaming output to files.

    Returns the exit code (negative signal number if killed), whether the run
    timed out, its duration, and the elapsed seconds to first non-empty stdout
    (`None` if the process never wrote anything -- the oracle's `-1`, which is a
    phase signal: no output by the timeout means the model stalled *before* any
    inference; output but no completion means it stalled mid-generation). See
    `_size` for a caveat on what `None` actually proves.

    Note: if `cmd[0]` does not exist, `subprocess.Popen` raises
    `FileNotFoundError` before the watchdog loop starts. That exception
    propagates uncaught out of this function -- `stdout_path`/`stderr_path`
    are left behind, created (by the `open()` calls above) but empty. Handling
    a missing binary is out of scope for this module; whatever builds a retry
    loop around this function needs to decide deliberately whether that case
    is retryable.
    """
    t0 = time.monotonic()
    first_out: float | None = None
    timed_out = False
    rc: int

    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # start_new_session=True makes the child a session+group leader, so its
        # PGID equals its pid. Capture it NOW: os.getpgid(proc.pid) races with
        # the child exiting and would raise once it is reaped.
        pg = proc.pid
        deadline = t0 + timeout_sec

        try:
            while True:
                status = proc.poll()
                if first_out is None and _size(stdout_path) > 0:
                    # `_size` swallows OSError and reports 0 on a stat failure,
                    # same as an empty file. So this branch never firing is not
                    # proof the child stayed silent -- "never wrote" and "stat
                    # failed" are indistinguishable in the `None` this function
                    # returns. See `_size`.
                    first_out = time.monotonic() - t0
                if status is not None:
                    # Checked *before* the deadline, so a run that finishes in the
                    # final tick keeps its valid output instead of being recorded as
                    # a timeout (the oracle re-checks liveness after its last tick
                    # for exactly this reason).
                    rc = status
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    rc = _terminate_group(proc, pg)
                    break
                time.sleep(_POLL_SEC)
        except BaseException:
            # The parent was interrupted mid-wait -- an exception raised by our
            # own code, or a signal such as Ctrl-C. start_new_session=True
            # makes the child a detached session/process-group leader, so it
            # is outside the terminal's foreground group: an interactive
            # SIGINT does not reach it on its own, and nothing else will ever
            # reap it. Take the group down exactly as a timeout would, then
            # let the original interruption keep propagating. (Only a SIGKILL
            # of this process itself is out of reach here; that belongs to
            # future CLI signal handling, not this function.)
            _terminate_group(proc, pg)
            raise

    if timed_out:
        # Truncate only after the group is dead, so nothing can re-extend the
        # file behind our back through an inherited descriptor.
        stdout_path.write_bytes(b"")

    return RunResult(
        rc=rc,
        timed_out=timed_out,
        duration_sec=max(0.0, time.monotonic() - t0),
        first_output_sec=first_out,
    )


def _terminate_group(proc: subprocess.Popen, pg: int) -> int:
    """SIGTERM the group, allow the grace period, then always SIGKILL it.

    The unconditional final SIGKILL is a deliberate divergence from the
    oracle, not parity with it. In the oracle, the worker's `wait` on the
    leader unblocks the instant the leader dies from SIGTERM, and the very
    next line kills the *watchdog subshell* -- which is mid-`sleep 3` -- so
    the watchdog's own `kill -KILL -- -$pgid` never runs. A grandchild that
    ignored SIGTERM and stayed in the group survives the oracle in exactly
    that scenario. This port always finishes the grace period and always
    issues the group SIGKILL, so it does not leak that grandchild. Stronger
    than the oracle, not equivalent to it.
    """
    _killpg(pg, signal.SIGTERM)
    grace_end = time.monotonic() + _TERM_GRACE_SEC
    while time.monotonic() < grace_end:
        if proc.poll() is None:
            try:
                proc.wait(timeout=_POLL_SEC)
            except subprocess.TimeoutExpired:
                pass
        elif not _group_alive(pg):
            # Leader reaped and nothing else left in the group: the remaining
            # grace would buy nothing. Anything still there gets the full grace.
            break
        else:
            time.sleep(_POLL_SEC)

    # ALWAYS nuke the group, even when the leader is already gone: a grandchild
    # that ignored SIGTERM lives on in the same PGID and must not survive us.
    # (This is where this port diverges from -- and strengthens -- the oracle;
    # see the docstring above.)
    _killpg(pg, signal.SIGKILL)

    if proc.poll() is None:
        # Defensive: only reachable if the leader escaped its own group.
        proc.kill()
    proc.wait()
    return proc.returncode


def _killpg(pg: int, sig: int) -> None:
    try:
        os.killpg(pg, sig)
    except ProcessLookupError:
        pass  # nothing left to signal is success, not an error


def _group_alive(pg: int) -> bool:
    try:
        os.killpg(pg, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
