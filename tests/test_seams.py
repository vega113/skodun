"""The UNIFYING seam matrix over every Phase-3 surface, plus the gate.py /
trust.py trust-boundary byte pin.

Tasks 3, 10, 12 and 13 each already pinned their own surface's exit-code
seam (`triage --reopen` in `test_cli.py`; `dispatch`/`worker`/`install-hooks`
in `test_cli.py`; `surface` in `test_cli.py`; `mcp` in `test_mcpserver.py`).
Those files stay: they are the deep, per-surface outcome coverage (every
exit code a command can produce, not just one). This module is the shallow,
WIDE one -- a single registry (`SURFACES` below) and a small number of
generic test functions parameterized over it, so that a surface added after
this task either (a) gets added to `SURFACES` and is immediately covered by
every applicable cell, or (b) is missing from `SURFACES` and the coverage
gap is a `git diff` away from visible, rather than a silent omission nobody
would notice until a real seam broke in production.

The seam being pinned, over and over, is always the same one: a `print()`
meeting a closed pipe raises `BrokenPipeError`, which -- unless the command
catches it -- hands the shell the INTERPRETER's own exit code of 1. For a
gate-shaped command that is indistinguishable from "findings remain open";
for `dispatch` it is a BLOCKED PUSH from a broken pipe. So every cell below
asserts two things together: the exit code the surface's own contract
promises, AND the absence of a traceback -- because a mutation that
"fixes" the exit code by papering over the exception, or one that changes
the code AND leaves a stack trace, both have to fail somewhere, and the
combination is where they get caught.

Matrix dimensions:

  * SURFACES: dispatch, worker, surface, mcp, install-hooks, triage --reopen
    (the six Phase-3 additions named in the plan's Global Constraints).
  * MODES (every surface): normal (in-process `main()` call -- the cheapest
    form, and the only one that also proves the Python-level contract, not
    just the process-level one), closed_stdout (a `BrokenPipeError` on every
    write), pipe_head (`| head -1` under `set -o pipefail`, a live pipe
    rather than a simulated closed one), module_m (`python -m skodun`, a
    fresh interpreter and import graph), console_script (`skodun.cli:entry`,
    the shape of the installed console-scripts wrapper).
  * NO-TERMINAL MODES (dispatch, worker, mcp only -- these three run without
    a human ever attached: a pre-push hook, a detached background worker,
    and a client-driven protocol server): stdin_closed (no data, ever),
    dead_reader (the reader is gone before the first byte is written),
    no_tty (`start_new_session=True`, no controlling terminal at all).
  * MISUSE: one argv per surface that argparse (or the surface's own
    pre-store argument validation) refuses -- exit 2, a message, never a
    traceback.

One explicit skip: `mcp` under `pipe_head`. `mcp` is a long-running stdio
loop that answers requests until stdin reaches EOF; with no request sent
(the representative, cheap case every other cell in this row uses) there is
nothing for it to write before EOF, so piping that through `head -1` proves
nothing that the `closed_stdout` cell does not already prove more directly
-- a closed downstream write must exit cleanly, and that IS `closed_stdout`'s
assertion. `test_mcpserver.py`'s own `dead-reader` and `pipefail` forms
additionally cover the "there IS a live response in flight" case with a real
request, which is what would make the row meaningful.

`worker`'s `normal` cell is NOT a skip: a worker with no such reservation to
claim is a real, cheap, terminal outcome (exit 2), not a null case -- it is
exactly what a detached worker sees when a newer push has already superseded
the record it was spawned for.
"""

import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

import skodun
from skodun.cli import main
from skodun.store import Store

_SRC = str(Path(skodun.__file__).resolve().parents[1])
_PKG_DIR = Path(skodun.__file__).resolve().parent


# ===========================================================================
# Part 1: the gate.py / trust.py trust-boundary byte pin
# ===========================================================================
#
# Recorded at Phase 3's start (docs/superpowers/plans/2026-07-29-skodun-
# phase3.md, sdd/progress.md): "gate.py and trust.py are byte-identical
# before and after this phase". `_PKG_DIR` is derived from the imported
# `skodun` package's own `__file__`, never a hardcoded checkout path -- a
# scratch checkout that imports its OWN `src/skodun` (via `pythonpath` or an
# editable install) is read here exactly as this repository's own checkout
# is, which is what lets a byte appended to `gate.py` in a scratch checkout
# fail this same test THERE, not just here.

GATE_SHA256 = "62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f"
TRUST_SHA256 = "8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gate_py_is_byte_identical_to_the_phase_start_pin():
    got = _sha256_of(_PKG_DIR / "gate.py")
    assert got == GATE_SHA256, (
        f"gate.py has changed since Phase 3 started (sha256 {got}, expected "
        f"{GATE_SHA256}) -- the byte pledge in the plan's Global Constraints "
        "has been broken")


def test_trust_py_is_byte_identical_to_the_phase_start_pin():
    got = _sha256_of(_PKG_DIR / "trust.py")
    assert got == TRUST_SHA256, (
        f"trust.py has changed since Phase 3 started (sha256 {got}, expected "
        f"{TRUST_SHA256}) -- the byte pledge in the plan's Global Constraints "
        "has been broken")


# ===========================================================================
# Part 2: shared seam-testing plumbing
# ===========================================================================

def _subprocess_env(overrides: dict) -> dict:
    """The real environment, plus this cell's overrides, plus what a
    subprocess needs to import the SAME `skodun` pytest is testing.

    `SKODUN_PREPUSH_SKIP` is dropped unconditionally: a developer's own shell
    may have it set for their real repository, and a dispatch cell must not
    silently no-op because of it.
    """
    env = dict(os.environ)
    env.update(overrides)
    env.pop("SKODUN_PREPUSH_SKIP", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def _ov_plain(tmp_path: Path) -> dict:
    """Env overrides for a cell that touches a store but never git: pin
    `SKODUN_DB` into `tmp_path` and point `SKODUN_CONFIG` at a path that does
    not exist, so nothing here can read a real `~/.config/skodun/config.toml`
    or write to `~/.local/share/skodun`."""
    return {"SKODUN_DB": str(tmp_path / "s.db"),
            "SKODUN_CONFIG": str(tmp_path / "absent-config.toml")}


def _ov_git(tmp_path: Path) -> dict:
    """`_ov_plain` plus a hermetic git: this machine's global config may
    carry a leaked `core.hooksPath`, and `dispatch`/`install-hooks` correctly
    honour whatever `core.hooksPath` they are told about -- which would
    reach outside `tmp_path` if the real global config leaked through."""
    ov = _ov_plain(tmp_path)
    ov["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    ov["GIT_CONFIG_SYSTEM"] = str(tmp_path / "gitsystem")
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    (tmp_path / "gitsystem").write_text("", encoding="utf-8")
    return ov


def _tiny_repo(tmp_path: Path, env: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=env)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c0"], check=True,
                   capture_output=True, env=env)
    return repo


def _empty_store(db: Path) -> None:
    with Store.open(db):
        pass


#: `main()`'s own outermost `except BaseException` (the belt for every
#: inner guard's braces) prints exactly this marker before returning 2.
INTERNAL_ERROR_MARKER = "internal CLI error"


def _assert_seam_clean(stdout_text: str, stderr_text: str) -> None:
    """The one assertion every cell below makes, in one place, because exit
    code equality is NOT enough to catch every mutation.

    A literal `Traceback (most recent call last):` means an exception
    escaped `main()` itself (or `entry()`'s own `SystemExit` wrapping) --
    the interpreter's own uncaught-exception path, not this project's.

    `INTERNAL_ERROR_MARKER` catches a subtler, and more likely, escape: an
    inner guard (`_emit`, `_warn`, a subcommand's own `try/except
    BaseException`) failing to swallow something -- for example a
    `BrokenPipeError` re-raised on closed stdout -- so it falls through to
    `main()`'s own outermost catch-all. That catch-all returns 2, which is
    the SAME exit code `worker`'s own no-such-reservation case already
    returns when nothing is wrong: exit-code equality alone could not tell
    "the guard worked" from "the guard failed and something else papered
    over it" on that row. Verified directly against a `print()` substituted
    for `_emit()` in `_cmd_worker`: identical exit code (2), stderr gains
    exactly this marker on the mutated path and nothing else changes.
    """
    assert "Traceback" not in stdout_text and "Traceback" not in stderr_text, (
        f"stdout={stdout_text!r} stderr={stderr_text!r}")
    assert INTERNAL_ERROR_MARKER not in stderr_text, (
        f"an exception escaped an inner guard and was caught only by "
        f"main()'s outermost handler -- a properly guarded seam should "
        f"never reach it: stderr={stderr_text!r}")


def _finding(i: int = 0) -> dict:
    return dict(file=f"a{i}.py", line=3 + i, severity="high", category="bug",
               title=f"NPE {i}", detail="boom")


def _artifact(findings, review_id="rev1", **extra) -> dict:
    rec = dict(extra_passes={}, id=review_id, branch="feat", base_sha="s" * 40,
               diff_hash="d" * 40, reviewed_at="2026-07-27T10:00:00Z",
               head="h" * 20, base_ref="origin/main", context_hash="",
               mode="now", model="m", adapter="grok", status="findings",
               parse_ok=True, degraded=False, diff_truncated=False,
               trustworthy=True, stop_reason="EndTurn", summary="findings",
               findings_total=len(findings),
               severity={"high": len(findings), "medium": 0, "low": 0},
               findings=list(findings))
    rec.update(extra)
    return rec


REOPEN_REASON = "the guard was deleted in a refactor and this crashes on main"
FIRST_DISMISS_REASON = "a first reason clearly long enough to clear the audit floor"


# ===========================================================================
# Part 3: the surface registry
# ===========================================================================
#
# Each surface names how to build its ONE representative, cheap, minimal
# invocation (the `setup` callable), the exit code that invocation promises,
# and a misuse argv+needle. Adding a seventh surface later means adding one
# `Surface(...)` entry here; every generic test function below iterates
# `SURFACES` (and, where relevant, `NO_TERMINAL_SURFACES`), so a surface that
# is added to the registry is automatically driven through every mode -- and
# a surface that is NOT added is, correctly, not covered at all, which is
# the visible gap the module's docstring promises.

@dataclass(frozen=True)
class Surface:
    name: str
    overrides: Callable[[Path], dict]
    #: `(tmp_path, overrides) -> (argv, cwd)`. May have side effects (writing
    #: a store, initializing a git repo) using paths derived from `overrides`
    #: and `tmp_path`.
    setup: Callable[[Path, dict], tuple]
    expected: int
    misuse_argv: list
    misuse_needle: str
    no_terminal: bool = False
    #: Non-empty only for `mcp`'s `dead_reader` cell, which needs a real
    #: request in flight for a dead reader to mean anything.
    dead_reader_stdin: str = ""
    #: Set to skip the `pipe_head` cell for this surface, with a reason.
    pipe_head_skip: str = ""


def _dispatch_setup(tmp_path: Path, overrides: dict):
    env = _subprocess_env(overrides)
    repo = _tiny_repo(tmp_path, env)
    return (["dispatch"], repo)


def _worker_setup(tmp_path: Path, overrides: dict):
    env = _subprocess_env(overrides)
    repo = _tiny_repo(tmp_path, env)
    _empty_store(Path(overrides["SKODUN_DB"]))
    return (["worker", "--record-id", "sk_absent", "--repo", str(repo),
             "--branch", "b", "--local-oid", "a" * 40, "--base-sha", "b" * 40,
             "--base-ref", "main"], repo)


def _surface_setup(tmp_path: Path, overrides: dict):
    _empty_store(Path(overrides["SKODUN_DB"]))
    return (["surface", "--branch", "feat"], tmp_path)


def _mcp_setup(tmp_path: Path, overrides: dict):
    return (["mcp"], tmp_path)


def _install_hooks_setup(tmp_path: Path, overrides: dict):
    env = _subprocess_env(overrides)
    repo = _tiny_repo(tmp_path, env)
    return (["install-hooks", "--repo", str(repo)], tmp_path)


def _reopen_setup(tmp_path: Path, overrides: dict):
    from skodun.triage import dismiss

    db = Path(overrides["SKODUN_DB"])
    with Store.open(db) as store:
        store.save_review(_artifact([_finding(0)]))
        dismiss(store, store.get_review("rev1"), 0, FIRST_DISMISS_REASON,
                now="2026-07-27T10:00:00Z")
    return (["triage", "--reopen", "rev1", "0", REOPEN_REASON], tmp_path)


SURFACES = [
    Surface("dispatch", _ov_git, _dispatch_setup, expected=0,
            misuse_argv=["dispatch", "one", "two", "three"],
            misuse_needle="usage:", no_terminal=True),
    Surface("worker", _ov_git, _worker_setup, expected=2,
            misuse_argv=["worker"], misuse_needle="usage:", no_terminal=True),
    Surface("surface", _ov_plain, _surface_setup, expected=0,
            misuse_argv=["surface", "--hook-format", "yaml"],
            misuse_needle="usage:"),
    Surface("mcp", _ov_plain, _mcp_setup, expected=0,
            misuse_argv=["mcp", "--no-such-flag"], misuse_needle="usage:",
            no_terminal=True,
            dead_reader_stdin='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
            pipe_head_skip=(
                "mcp writes JSON-RPC only until EOF; with no request sent "
                "(this row's cheap representative case) there is nothing to "
                "write before EOF, so piping it through `head` exercises "
                "nothing beyond what the closed_stdout cell already asserts "
                "more directly -- a downstream close must still exit "
                "cleanly. test_mcpserver.py's own pipefail/dead-reader forms "
                "cover the 'a real response is in flight' case with an "
                "actual request.")),
    Surface("install-hooks", _ov_git, _install_hooks_setup, expected=0,
            misuse_argv=["install-hooks", "--no-such-flag"],
            misuse_needle="usage:"),
    Surface("triage-reopen", _ov_plain, _reopen_setup, expected=0,
            misuse_argv=["triage", "--reopen", "rev1"],
            misuse_needle="--reopen"),
]

SURFACES_BY_NAME = {s.name: s for s in SURFACES}
NO_TERMINAL_SURFACES = [s for s in SURFACES if s.no_terminal]

assert {"dispatch", "worker", "surface", "mcp", "install-hooks",
        "triage-reopen"} == set(SURFACES_BY_NAME), (
    "the plan's Global Constraints name exactly six Phase-3 surfaces for "
    "this matrix -- the registry above has drifted from that list")
assert {s.name for s in NO_TERMINAL_SURFACES} == {"dispatch", "worker", "mcp"}


# ===========================================================================
# Part 4: the five-mode matrix, common to every surface
# ===========================================================================

MODES = ["normal", "closed_stdout", "pipe_head", "module_m", "console_script"]


@pytest.mark.parametrize("surface", SURFACES, ids=[s.name for s in SURFACES])
@pytest.mark.parametrize("mode", MODES)
def test_seam_matrix(tmp_path, monkeypatch, capsys, surface, mode):
    """Exit code correctness for one representative, cheap invocation of
    every surface, across every invocation form. Every branch below asserts
    BOTH the surface's promised exit code and the absence of a traceback --
    a `BrokenPipeError` escaping unguarded would corrupt one or the other,
    and a mutation that "fixes" just one of the two must still fail here.
    """
    if mode == "pipe_head" and surface.pipe_head_skip:
        pytest.skip(surface.pipe_head_skip)

    overrides = surface.overrides(tmp_path)
    argv, cwd = surface.setup(tmp_path, overrides)

    if mode == "normal":
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
        if surface.name == "mcp":
            # `serve_stdio` reads `sys.stdout.buffer` directly (the protocol
            # is raw UTF-8 bytes with its own newline framing) -- a
            # `TextIOWrapper` around an in-memory buffer exposes exactly
            # that attribute, the same way the real `sys.stdout` does.
            monkeypatch.setattr(sys, "stdout",
                                io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        code = main(argv)
        assert code == surface.expected
        cap = capsys.readouterr()
        _assert_seam_clean(cap.out, cap.err)
        return

    env = _subprocess_env(overrides)

    if mode == "closed_stdout":
        r_fd, w_fd = os.pipe()
        os.close(r_fd)
        try:
            p = subprocess.run([sys.executable, "-m", "skodun", *argv],
                               stdout=w_fd, stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.DEVNULL, cwd=str(cwd), env=env,
                               timeout=120)
        finally:
            os.close(w_fd)
        assert p.returncode == surface.expected, f"stderr={p.stderr!r}"
        _assert_seam_clean("", p.stderr)
        return

    if mode == "pipe_head":
        quoted = " ".join(shlex.quote(a) for a in argv)
        script = (f'set -o pipefail; {shlex.quote(sys.executable)} -m skodun '
                  f'{quoted} < /dev/null | head -1; '
                  f'echo "SKODUN_EXIT=${{PIPESTATUS[0]}}"')
        p = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, cwd=str(cwd), env=env, timeout=120)
        m = re.search(r"SKODUN_EXIT=(\d+)", p.stdout)
        assert m, f"stdout={p.stdout!r} stderr={p.stderr!r}"
        assert int(m.group(1)) == surface.expected, \
            f"stdout={p.stdout!r} stderr={p.stderr!r}"
        _assert_seam_clean("", p.stderr)
        return

    if mode == "module_m":
        p = subprocess.run([sys.executable, "-m", "skodun", *argv],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, cwd=str(cwd), env=env,
                           timeout=120)
        assert p.returncode == surface.expected, \
            f"stdout={p.stdout!r} stderr={p.stderr!r}"
        _assert_seam_clean(p.stdout, p.stderr)
        return

    if mode == "console_script":
        p = subprocess.run(
            [sys.executable, "-c", "from skodun.cli import entry; entry()",
             *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL,
            cwd=str(cwd), env=env, timeout=120)
        assert p.returncode == surface.expected, \
            f"stdout={p.stdout!r} stderr={p.stderr!r}"
        _assert_seam_clean(p.stdout, p.stderr)
        return

    raise AssertionError(f"unhandled mode {mode!r} -- a mode added to MODES "
                         "must be handled above, not silently skipped")


# ===========================================================================
# Part 5: the three no-terminal variants -- dispatch, worker, mcp only
# ===========================================================================
#
# These three run detached from any human: a pre-push hook, a background
# worker `dispatch` spawns with `start_new_session=True`, and a client-driven
# protocol server. None of the three has a controlling tty, a live stdin, or
# anyone reading its stdout in production, so a library probing for a
# terminal (argparse asks stdout about colour support while building its
# formatter) must not turn that probe into an exception.

NO_TERMINAL_MODES = ["stdin_closed", "dead_reader", "no_tty"]


@pytest.mark.parametrize("surface", NO_TERMINAL_SURFACES,
                         ids=[s.name for s in NO_TERMINAL_SURFACES])
@pytest.mark.parametrize("mode", NO_TERMINAL_MODES)
def test_seam_matrix_no_terminal(tmp_path, surface, mode):
    overrides = surface.overrides(tmp_path)
    argv, cwd = surface.setup(tmp_path, overrides)
    env = _subprocess_env(overrides)

    if mode == "stdin_closed":
        p = subprocess.run([sys.executable, "-m", "skodun", *argv],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, cwd=str(cwd), env=env,
                           timeout=120)
        assert p.returncode == surface.expected, \
            f"stdout={p.stdout!r} stderr={p.stderr!r}"
        _assert_seam_clean(p.stdout, p.stderr)
        return

    if mode == "no_tty":
        p = subprocess.run([sys.executable, "-m", "skodun", *argv],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True, cwd=str(cwd),
                           env=env, start_new_session=True, timeout=120)
        assert p.returncode == surface.expected, f"stderr={p.stderr!r}"
        _assert_seam_clean("", p.stderr)
        return

    if mode == "dead_reader":
        # `true` reads nothing and exits immediately: by the time this
        # process (a slower Python startup) writes anything, the read end is
        # very likely already closed. A `BrokenPipeError` escaping any of
        # these three commands must not change the exit code they promise.
        quoted = " ".join(shlex.quote(a) for a in argv)
        script = (f'{shlex.quote(sys.executable)} -m skodun {quoted} | true; '
                  f'echo "SKODUN_EXIT=${{PIPESTATUS[0]}}"')
        p = subprocess.run(["bash", "-c", script],
                           input=surface.dead_reader_stdin,
                           capture_output=True, text=True, cwd=str(cwd),
                           env=env, timeout=120)
        m = re.search(r"SKODUN_EXIT=(\d+)", p.stdout)
        assert m, f"stdout={p.stdout!r} stderr={p.stderr!r}"
        assert int(m.group(1)) == surface.expected, \
            f"stdout={p.stdout!r} stderr={p.stderr!r}"
        _assert_seam_clean("", p.stderr)
        return

    raise AssertionError(f"unhandled no-terminal mode {mode!r}")


# ===========================================================================
# Part 6: misuse -- a clear message, never a traceback
# ===========================================================================

@pytest.mark.parametrize("surface", SURFACES, ids=[s.name for s in SURFACES])
def test_seam_misuse_is_a_message_never_a_traceback(tmp_path, monkeypatch,
                                                     capsys, surface):
    """Misuse is refused before any of the surface's own state is touched --
    a bare `tmp_path` (never the surface's representative fixture) is enough
    to prove the argv itself was rejected, not something it pointed at."""
    overrides = surface.overrides(tmp_path)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    assert main(surface.misuse_argv) == 2
    cap = capsys.readouterr()
    _assert_seam_clean(cap.out, cap.err)
    assert surface.misuse_needle in cap.out or surface.misuse_needle in cap.err, (
        f"expected {surface.misuse_needle!r} in stdout={cap.out!r} or "
        f"stderr={cap.err!r}")


def test_an_unknown_reviewer_request_is_a_message_never_a_traceback(
        tmp_path, monkeypatch, capsys):
    """`review --reviewer <name>` -- the one misuse in this module argparse
    CANNOT catch, which is why it is a test of its own rather than a
    `Surface(...)` row.

    `review` is not in `SURFACES`: that registry is the six Phase-3 surfaces the
    plan names, pinned by the assertion under it, and every row's misuse is an
    argv argparse itself refuses with `usage:`. This one is well-formed argv --
    only the loaded config knows the name is wrong -- so it is refused by
    `run_review`'s own preflight instead. The seam is the same seam: exit 2, a
    message that names the mistake, and no traceback. The extra assertion is the
    one `review` owes on every path: the LAST line of stdout is a verdict, even
    when nothing ran.
    """
    overrides = _ov_git(tmp_path)
    env = _subprocess_env(overrides)
    repo = _tiny_repo(tmp_path, env)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "primary"\nprovider = "xai"\n'
        'model = "grok-4.20-0309-reasoning"\nrole = "finder"\n',
        encoding="utf-8")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    # A plain checkout is the primary one; without this the run is refused for
    # a different reason entirely and this test would prove nothing.
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    # Nothing here may reach a real provider CLI. Not belt-and-braces: this is
    # the ONE cell in this module whose command spends model calls when it is
    # not refused, and `adapters.grok` prefers `~/.grok/bin/grok` over PATH, so
    # a regression that turned this refusal into a review would run the
    # developer's own paid CLI against a throwaway repo. (Verified: with the
    # refusal mutated away, this variable is what makes the run fail rather
    # than call out.)
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "no-bin" / "grok"))
    monkeypatch.chdir(tmp_path)

    assert main(["review", "--repo", str(repo), "--reviewer", "no-such"]) == 2

    cap = capsys.readouterr()
    _assert_seam_clean(cap.out, cap.err)
    last = cap.out.strip().splitlines()[-1]
    assert last.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "no-such" in last and "no review ran" in last
    # Nothing ran, so nothing was recorded. (The store itself is opened by the
    # CLI seam before the service is called, so its EXISTENCE proves nothing;
    # its emptiness is the assertion worth making.)
    with Store.open(Path(overrides["SKODUN_DB"])) as store:
        assert store.list_reviews(None, 10) == []
