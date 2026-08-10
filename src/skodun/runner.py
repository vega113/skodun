"""Run a model CLI under a hard wall-clock watchdog.

Ported from the oracle's ``run_grok_with_timeout``. Three properties matter and
are the reason this module exists at all:

1. **No pipes.** stdout/stderr stream *directly* to files, and the prompt, when
   a CLI wants it on stdin, arrives as an opened FILE rather than as bytes this
   process writes. A pipe to the parent would fill on a large review and
   deadlock the child forever; a pipe *into* the child would deadlock the same
   way, since nothing here reads the child's output while it writes.
2. **No orphans.** The child is spawned as its own session/process-group leader,
   so the watchdog can signal the whole tree (the model CLI plus any helper it
   spawned) instead of orphaning grandchildren.
3. **A timed-out run's stdout is evidence of nothing.** A process can print a
   complete, clean-looking result envelope and then hang; parsing that output
   after retries are exhausted would mint a trustworthy "clean review" from a
   run that never finished. On timeout the stdout file is truncated to zero
   bytes, exactly as the oracle discards it (`: > "$_tmp"`).

CANCELLATION lives here too, for property 2's reason. The background worker is
SIGTERMed when a newer push supersedes it, and its handler cannot take the
provider down itself -- it does not know the pid, and a signal handler is not
where a process group should be reaped. So the handler only SETS a token, and
this module's tick loop is the one place that both holds the pgid and runs
often enough to notice. A bare SIGTERM death of the worker would leave the model
CLI alive in its own session, spending quota on a review nobody will read and
overlapping the replacement review on one inference backend.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Grace between SIGTERM and SIGKILL for the process group (oracle: `sleep 3`).
_TERM_GRACE_SEC = 3.0
# Watchdog tick. Finer than the oracle's 1s tick, which only sharpens the
# timeout edge and the time-to-first-output resolution.
_POLL_SEC = 0.25


class ReviewCancelled(BaseException):
    """This review was asked to stop; nothing it produced is a verdict.

    `BaseException`, and NOT `Exception`, deliberately. Every layer between this
    tick loop and the worker catches `Exception` in order to DEMOTE rather than
    destroy a review -- `pipeline._run_sub`, `pipeline._extra_pass`,
    `dispatch.build_dedup_evidence`. An `Exception` subclass would therefore be
    swallowed by the first of them and turned into a degraded *review*: a record
    with clean-looking axes for a run that was killed mid-flight. Being outside
    that hierarchy is what makes the cancellation reach the worker's own
    failed-finalize path instead.

    It lives in this module rather than in `pipeline` because this is the lowest
    layer that raises it, and `runner` deliberately imports nothing from the
    package (see the module docstring's no-pipes/no-orphans properties -- it is a
    leaf on purpose). `pipeline` and `cli` alias the name.

    `partial` is the record built so far, attached by `pipeline`'s pass-boundary
    checks as the exception travels out: a cancellation that lands after two
    passes already answered still has findings worth persisting, and the worker
    finalizes them with `degraded=True` rather than throwing them away. It is
    `None` when the cancellation happened before any record existed.
    """

    def __init__(self, *args, partial: dict | None = None):
        super().__init__(*args)
        self.partial = partial


class SpawnError(Exception):
    """A process-start ``OSError`` separated from watchdog I/O failures."""

    def __init__(self, cause: OSError, *, cmd=None, cwd: Path | None = None) -> None:
        self.cause = cause
        self.cmd = tuple(cmd) if cmd is not None else None
        self.cwd = cwd
        super().__init__(str(cause))


def _is_path_shaped(binary: str) -> bool:
    """Whether `binary` should be resolved as a path rather than walked
    through `PATH`: it contains `/`, or the platform's own separator on a
    platform where that differs from `/`.

    THE ONE definition of the path-vs-PATH split. Two callers decide the same
    thing with it -- `chain._binary_is_absent`'s pre-spawn existence check and
    `cli._fmt_binary`'s `providers` diagnostic -- about the same values (the
    per-adapter `SKODUN_<X>_BIN` overrides, grok's own `~/.grok/bin/grok`
    default), and both have to agree with how the adapter's own `Popen` call
    would resolve them. Before it was factored out, `cli._fmt_binary` carried a
    second copy, free to drift.

    It lives HERE, in the leaf, for the same reason `ReviewCancelled` does. It
    was in `pipeline`, which made `skodun providers` -- a read-only diagnostic
    an operator reaches for precisely when a review will not run -- depend on
    the entire review pipeline importing cleanly, i.e. unavailable exactly when
    it is needed. `runner` imports nothing from the package, so no caller pays
    for anyone else's import graph to ask this question.
    """
    return "/" in binary or (os.sep != "/" and os.sep in binary)


def _cancelled(cancel: "threading.Event | None") -> bool:
    """Whether `cancel` is a set token. Total, and never raises.

    `is_set()` is called through a guard because the token crosses a signal
    handler boundary: a caller that passed something Event-shaped-but-not (a
    `Mock`, a stale proxy) must not turn a review into a crash inside the
    watchdog loop. An unreadable token reads as NOT cancelled -- the review
    continues and its own timeout still bounds it, which is strictly safer than
    aborting a run that nobody asked to stop.

    "Never raises" has ONE exception, and it is `KeyboardInterrupt`: see
    `_sleep_or_cancelled` for the whole argument, including why `SystemExit` is
    deliberately not treated the same way.
    """
    if cancel is None:
        return False
    try:
        return bool(cancel.is_set())
    except KeyboardInterrupt:
        raise
    except BaseException:
        return False


def _sleep_or_cancelled(cancel: "threading.Event | None", seconds: float) -> bool:
    """Wait up to `seconds`, returning True if the token became set.

    Never raises, with ONE exception: `KeyboardInterrupt`.

    The difference from `time.sleep` is the only reason it exists: a waiter that
    sleeps on the CLOCK notices a cancellation one whole tick late, and the
    foreground lock's tick is the poll interval — tens of seconds by default.
    Waiting on the EVENT returns the instant it is set, so an MCP client's EOF
    aborts a lock wait in milliseconds rather than at the next poll.

    An Event-shaped-but-not token (a `Mock`, a stale proxy) falls back to a plain
    sleep and reports NOT cancelled, exactly as `_cancelled` does: an unreadable
    token must never abort a review nobody asked to stop.

    A `KeyboardInterrupt` is the one thing that fallback may NOT eat, and this is
    the function where it matters most: in the foreground, a lock wait blocks
    HERE, in the main thread, for the whole poll interval. Swallowing the Ctrl-C
    raised out of `wait` left the operator's interrupt doing nothing at all --
    the loop simply polled again. It is re-raised, and the caller (`cli`, through
    `services`) turns it into the 130 it always has.

    `SystemExit` is deliberately NOT re-raised, the same distinction `svc_gate`
    makes: it carries an arbitrary exit code, 0 included, and letting one escape
    a fail-closed path from inside a token read would be a worse regression than
    the swallowed interrupt. It falls back to the plain sleep like any other
    unreadable token.
    """
    if cancel is None:
        time.sleep(seconds)
        return False
    try:
        return bool(cancel.wait(seconds))
    except KeyboardInterrupt:
        raise
    except BaseException:
        time.sleep(seconds)
        return False


@dataclass(frozen=True)
class RunResult:
    rc: int
    timed_out: bool
    duration_sec: float
    first_output_sec: float | None
    output_limit_exceeded: bool = False
    descendants_killed: bool = False
    descendant_state: str = "none"


def run_with_watchdog(
    cmd: list[str],
    timeout_sec: int,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    stdin_path: Path | None = None,
    cancel: "threading.Event | None" = None,
    max_output_bytes: int | None = None,
) -> RunResult:
    """Run `cmd` with a hard wall-clock timeout, streaming output to files.

    Returns the exit code (negative signal number if killed), whether the run
    timed out, its duration, and the elapsed seconds to first non-empty stdout
    (`None` if the process never wrote anything -- the oracle's `-1`, which is a
    phase signal: no output by the timeout means the model stalled *before* any
    inference; output but no completion means it stalled mid-generation). See
    `_size` for a caveat on what `None` actually proves.

    `stdin_path`, when given, is opened READ-ONLY IN BINARY and becomes the
    child's stdin; otherwise stdin is `DEVNULL`, as it has always been. It
    exists for the adapters whose CLI has no input-file flag and takes the
    prompt on stdin instead (`codex exec ... -`): the prompt still travels as a
    FILE either way, and this only decides who opens it. Without it such a
    child reads an immediately-EOF `DEVNULL` -- or, for a CLI that waits for
    input on a terminal, hangs until this watchdog kills it.

    The descriptor is closed on EVERY path -- normal exit, timeout, and any
    exception, including a `Popen` that never started -- so a long-lived
    caller cannot accumulate open prompt files. `DEVNULL` is deliberately NOT
    routed through the same open: `subprocess` manages that descriptor itself.

    `max_output_bytes`, when given, bounds the file-backed stdout and stderr
    capture. The process group is terminated and the files are truncated to
    that limit before returning, so a diagnostic probe can safely use the same
    no-pipes runner without allowing a broken wrapper to fill its disk.

    `cancel`, when given, is a `threading.Event` the caller's SIGTERM handler
    sets. It is checked BEFORE the spawn and on every tick, and a set token
    takes the process group down exactly as a timeout does and then raises
    `ReviewCancelled`. Three details are deliberate:

      * **Checked before `Popen`.** A token set while the previous attempt was
        being written up must not buy one more model call.
      * **`raise`, not a `RunResult`.** A cancelled run has no exit code worth
        reporting and its stdout is evidence of nothing (property 3 above,
        arrived at by a different road) -- returning a result would invite the
        chain to classify and parse a killed attempt's partial envelope. The
        stdout file is deliberately NOT truncated: nothing will read it, because
        nothing between here and the worker's failed finalize looks at it.
      * **The group dies first, and is waited for.** `_terminate_group` reaps the
        leader and SIGKILLs the group, so by the time the exception propagates
        the provider is gone rather than racing the worker's exit.

    Note: if `cmd[0]` does not exist, `subprocess.Popen` raises
    `FileNotFoundError` before the watchdog loop starts. This function wraps
    that and other process-start `OSError`s in `SpawnError`, while leaving
    `stdout_path`/`stderr_path` behind, created (by the `open()` calls above)
    but empty. Handling a missing binary is out of scope for this module;
    whatever builds a retry loop around this function needs to decide
    deliberately whether that case is retryable.
    """
    if _cancelled(cancel):
        # Before ANY file is opened or process started: this call is not going
        # to happen at all, so it should leave nothing behind either.
        raise ReviewCancelled("the review was cancelled before this attempt started")
    t0 = time.monotonic()
    first_out: float | None = None
    timed_out = False
    output_limit_exceeded = False
    descendants_killed = False
    descendant_state = "none"
    rc: int

    # Opened before the output files and closed in the `finally` below, so
    # every exit path -- including one where `open(stdout_path)` itself fails
    # -- releases it.
    stdin_file = open(stdin_path, "rb") if stdin_path is not None else None
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL if stdin_file is None else stdin_file,
                    start_new_session=True,
                )
            except OSError as e:
                raise SpawnError(e, cmd=cmd, cwd=cwd) from e
            # start_new_session=True makes the child a session+group leader, so its
            # PGID equals its pid. Capture it NOW: os.getpgid(proc.pid) races with
            # the child exiting and would raise once it is reaped.
            pg = proc.pid
            deadline = t0 + timeout_sec

            try:
                while True:
                    if _cancelled(cancel):
                        # The whole reason this loop owns the cancellation: it
                        # holds `pg`. The worker's signal handler only set the
                        # token; the provider (and any helper it spawned into
                        # the same group) is taken down HERE, before the
                        # exception unwinds, so nothing outlives the worker.
                        _terminate_group(proc, pg)
                        raise ReviewCancelled(
                            "the review was cancelled while a reviewer was running")
                    status = proc.poll()
                    if first_out is None and _size(stdout_path) > 0:
                        # `_size` swallows OSError and reports 0 on a stat failure,
                        # same as an empty file. So this branch never firing is not
                        # proof the child stayed silent -- "never wrote" and "stat
                        # failed" are indistinguishable in the `None` this function
                        # returns. See `_size`.
                        first_out = time.monotonic() - t0
                    if (max_output_bytes is not None and
                            (_size(stdout_path) > max_output_bytes or
                             _size(stderr_path) > max_output_bytes)):
                        output_limit_exceeded = True
                        rc = _terminate_group(proc, pg)
                        break
                    if status is not None:
                        # Checked *before* the deadline, so a run that finishes in the
                        # final tick keeps its valid output instead of being recorded as
                        # a timeout (the oracle re-checks liveness after its last tick
                        # for exactly this reason).
                        # The leader may have exited while a wrapper-owned helper
                        # remains in the same process group. Reap that group before
                        # returning, otherwise a successful-looking wrapper can
                        # leave a native provider child writing to our descriptors.
                        if _group_alive(pg):
                            descendant_state = _group_descendant_state(pg, proc.pid)
                            if descendant_state == "live":
                                _terminate_group(proc, pg)
                                descendants_killed = True
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
        elif output_limit_exceeded:
            _truncate(stdout_path, max_output_bytes)
            _truncate(stderr_path, max_output_bytes)
        elif descendant_state != "none":
            # A wrapper that left a live child did not produce a trustworthy
            # completed attempt, even if its own stdout looked complete.
            stdout_path.write_bytes(b"")
            stderr_path.write_bytes(b"")

        return RunResult(
            rc=rc,
            timed_out=timed_out,
            duration_sec=max(0.0, time.monotonic() - t0),
            first_output_sec=first_out,
            output_limit_exceeded=output_limit_exceeded,
            descendants_killed=descendants_killed,
            descendant_state=descendant_state,
        )
    finally:
        if stdin_file is not None:
            stdin_file.close()


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

    "Always issues" is the honest limit of the guarantee: the kernel can still
    refuse the signal with EPERM, and no caller can make it not. `_killpg`
    reports that case rather than raising -- raising would skip this very
    SIGKILL and the reaping below it, which is the worse of the two outcomes.
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
    """Signal a process group, treating "cannot" as done rather than as fatal.

    `ProcessLookupError` is unambiguous success: nothing is left to signal.

    `PermissionError` (EPERM) is a failure, and is swallowed anyway, for two
    reasons. A child is its own group leader, so its PGID is its PID -- and
    the moment the group is gone that number is free to be reused. Signal it
    after the reuse and the kernel answers EPERM instead of ESRCH. The window
    is the gap between deciding to kill and killing, which a loaded machine
    widens; this was observed as a real full-suite failure, reported to the
    caller as `the review failed: PermissionError(...)` in place of the
    cancellation verdict.

    Letting it propagate was strictly worse than ignoring it. It aborted
    `_terminate_group` mid-way: from the SIGTERM call it skipped the grace
    period, the unconditional final SIGKILL that function exists to guarantee,
    and `proc.wait()` -- so the very grandchild the group kill is for could
    survive, and the leader went unreaped.

    But it is NOT silently ignored, because EPERM has a second cause that the
    first reading would paper over. A descendant that changed credentials --
    by exec'ing a setuid helper, say -- leaves a group that is still alive and
    that we may no longer signal. There is no way to tell that apart from pid
    reuse: `_group_alive` answers EPERM with `True` precisely because it cannot
    either. So the kill path continues (the leader is still reaped, the caller
    still gets its own exception rather than this one), and the fact that the
    group could not be shown dead is said out loud instead of being swallowed
    into a clean-looking return.

    `_group_alive` below has handled EPERM since it was written; this function
    not handling it at all was the asymmetry, not a decision.
    """
    try:
        os.killpg(pg, sig)
    except ProcessLookupError:
        pass  # nothing left to signal is success, not an error
    except PermissionError:
        _note_unsignalable(pg, sig)


def _note_unsignalable(pg: int, sig: int) -> None:
    """Say that a group refused our signal. Never raises, never blocks a kill.

    `pipeline._note` is the one progress channel (the caller's sink, else
    stderr), imported lazily because `pipeline` imports this module, and
    guarded because this is only ever called from an `except` that must not
    acquire a second failure mode of its own -- the same shape `routing._note`
    uses for the same reason.

    `KeyboardInterrupt` is re-raised, exactly as `_cancelled` and
    `_sleep_or_cancelled` above re-raise it: "never raises" has one exception
    in this module and it is the operator's own interrupt. Swallowing a Ctrl-C
    that lands in a diagnostic would leave it doing nothing at all, which is
    the defect issue #6 filed against `_sleep_or_cancelled`.

    It reaches a caller-supplied sink, so in principle a slow sink delays a
    shutdown. That is accepted rather than dodged: it is the same channel every
    other line of a review's progress goes through, this site is no more
    exposed than they are, and the alternative -- writing straight to stderr --
    would hide from an MCP client exactly the operator who needs to know their
    provider group may have outlived the review.
    """
    try:
        from .pipeline import _note

        _note(f"provider group {pg} refused signal {sig} (EPERM): it is "
              f"either gone and its pgid reused, or alive under credentials "
              f"we may not signal; continuing the shutdown either way")
    except KeyboardInterrupt:
        raise
    except BaseException:       # pragma: no cover - a note is never worth a raise
        pass


def _group_alive(pg: int) -> bool:
    try:
        os.killpg(pg, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_descendant_state(pg: int, leader_pid: int) -> str:
    """Classify ``pg`` as ``live``, ``none``, or ``inconclusive``.

    ``killpg(..., 0)`` also sees zombie-only groups on hosts whose PID 1 does
    not reap promptly. ``ps`` is available on the macOS/Linux hosts supported
    by the CLI and lets normal wrapper completion preserve its output when the
    only remaining group members are already dead. If inspection itself is
    unavailable or unparseable, return ``inconclusive`` so the fail-closed
    caller discards output without signaling an unrelated process group.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,state="],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "inconclusive"
    if result.returncode != 0:
        return "inconclusive"
    saw_matching_group = False
    saw_valid_row = False
    ambiguous_leader = False
    saw_live_descendant = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if not line.strip():
            continue
        if len(fields) < 3:
            return "inconclusive"
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            return "inconclusive"
        state = fields[2]
        if not state:
            return "inconclusive"
        saw_valid_row = True
        if group != pg:
            continue
        saw_matching_group = True
        try:
            observed_session = os.getsid(pid)
        except (OSError, ProcessLookupError):
            # The row can disappear between ps and getsid. Treat that race as
            # ambiguous rather than signal a numeric PGID that may already be
            # owned by another session.
            return "inconclusive"
        if observed_session != pg:
            # start_new_session makes the owned session id equal to pg. A
            # different session means this numeric PGID was recycled or is
            # otherwise unrelated; never signal it.
            return "inconclusive"
        if pid == leader_pid:
            # The reaped leader may still appear briefly as a zombie. A live
            # row with the same PID is ambiguous even when its session also
            # equals pg: a new start_new_session child can recreate the same
            # numeric PID/PGID/SID after the original leader exits. Never use
            # that row as permission to signal the group.
            if state[0] in {"Z", "X"}:
                continue
            ambiguous_leader = True
            continue
        if state[0] not in {"Z", "X"}:
            saw_live_descendant = True
    # A valid snapshot with no row for this group proves it vanished between
    # liveness and inspection; an empty snapshot is inconclusive and remains
    # fail closed.
    if saw_matching_group:
        if ambiguous_leader:
            return "inconclusive"
        if saw_live_descendant:
            return "live"
        return "none"
    if not saw_valid_row:
        return "inconclusive"
    # A valid snapshot with no row for this group is conclusive only if the
    # group vanished during the race. Visibility restrictions can hide a live
    # member while killpg still reports the group as alive.
    return "none" if not _group_alive(pg) else "inconclusive"


def _group_has_live_descendants(pg: int, leader_pid: int) -> bool:
    """Compatibility boolean for callers that only need the live state."""
    return _group_descendant_state(pg, leader_pid) == "live"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _truncate(path: Path, limit: int | None) -> None:
    if limit is None:
        return
    try:
        with path.open("r+b") as handle:
            handle.truncate(limit)
    except OSError:
        pass
