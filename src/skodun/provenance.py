"""Which skodun produced a record, and whether this process is still it.

A skodun verdict is trusted ACROSS TIME: the gate honours a review recorded
last week whenever the diff identity still matches. That makes a change in how
skodun itself classifies invisible in the records it left behind — #92 turned a
junie envelope failure from `degraded` into `unavailable`/`harness`, #99 gave
`openai-api` a degradation axis it did not have, and artifacts either side of
those merges describe the same provider behaviour with different verdicts while
nothing on either says which rule applied.

The artifact already names who ANSWERED (`adapter`, `model`, `attempts[]`) and
how the head was CHOSEN (`route_reason`, `routed_reviewer`). This module is the
missing half: which skodun asked.

Two questions, and they are deliberately different:

* `code_provenance()` — the identity of the code THIS PROCESS is running.
  Cached, because that is what it means. It goes on the artifact.
* `stale_against_disk()` — whether the checkout has moved since. Uncached, by
  definition. It answers in three parts, not two: moved, unchanged, and *we
  could not tell* — because a caller that reads a failed probe as "unchanged"
  either announces a move that never happened or goes on paying two
  subprocesses a call to re-learn the same nothing. It is a diagnostic, and it
  is never acted on automatically: see the note on auto-update below.

Nothing here may raise or block. Provenance is a record of what happened, not a
precondition for it, and a machine without git must still be able to review.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from . import __version__

#: `git rev-parse` and `git status` are local reads -- ~27ms together on a
#: normal checkout -- but they sit on the path of every process start, and
#: `run_review` warms this cache immediately before taking the foreground lock.
#: The ceiling is what a WEDGED git costs (a network filesystem, a stuck index
#: lock): two calls, so the worst case is twice this. Kept low deliberately --
#: nobody is waiting on provenance, and a diagnostic field is never worth
#: seconds of a review's wall clock.
_GIT_TIMEOUT_SEC = 2.0

#: The answer for THIS PROCESS, computed once. `None` means "not asked yet".
#: Tests reset it; nothing else may.
_CACHED: dict | None = None

#: Guards the one-time fill of `_CACHED`. The contract is ONE answer per
#: process, and unsynchronized lazy init does not give that: two threads can
#: both find it cold and compute either side of a `git pull`, so one record
#: says the old commit and its neighbour says the new one. The MCP server runs
#: one review at a time today, but `run_review` is called on a worker thread
#: and nothing here should depend on that staying true.
_LOCK = threading.Lock()


def _package_root() -> Path:
    """The directory a checkout of this package would have at its root.

    `…/src/skodun/provenance.py` → `…` for an editable install or source
    checkout. For a wheel in site-packages this is a directory that is not a
    checkout of this package -- though it may well sit INSIDE some other
    repository, which is why `_read_commit` insists the answer's toplevel is
    this exact directory rather than trusting that git found nothing.
    """
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """One git call, or None if git could not be run at all."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC)
    except KeyboardInterrupt:
        # "Never raises" has one exception, the operator's own interrupt --
        # the rule `runner._cancelled` and `_sleep_or_cancelled` follow, and
        # the defect issue #6 was filed for. On a cold cache this runs twice on
        # the review path, under the FG lock, so a Ctrl-C absorbed here is one
        # the operator has to send again.
        raise
    except BaseException:
        # Missing binary, a timeout, a permissions refusal -- all the same
        # answer here, and none of them is worth failing a review over.
        return None


def _read_commit(root: Path) -> str | None:
    """`HEAD` at `root`, suffixed when it does not describe the code, else None.

    Three answers, and the third is the one that is easy to get wrong:

    * `<sha>`          the checkout is exactly this commit
    * `<sha>-dirty`    it is not, and we know that
    * `<sha>-unknown`  we could not establish either way

    The `-dirty` suffix is git-describe's convention and it earns its place: on
    a development machine the tree is usually modified, and a bare commit would
    name code that is not what ran. That is worse than saying nothing, because
    a precise-looking hash invites belief.

    `status --porcelain`, NOT `diff --quiet`. The latter compares the worktree
    against the index only, so it answers "clean" for a STAGED change and for
    an UNTRACKED file -- and an untracked module is code this process can
    import, so a bare hash there names a commit that demonstrably did not
    produce the run. `--porcelain` covers all three and honours `.gitignore`,
    so build droppings do not count as modifications.

    `-unknown` exists because git returns 0 for clean, 1 for dirty and 128/129
    for "that is not a checkout": reading anything-but-1 as clean would publish
    a precise hash on the strength of a failure, which is the same
    invites-belief problem wearing the error path's clothes.

    `--show-toplevel` is asked for in the same breath as `HEAD`, and the answer
    is REQUIRED to be `root` itself. Git searches ancestors, so a wheel in
    `~/.local/pipx/venvs/skodun/...` reports the HEAD of whatever repository
    happens to contain it -- a dotfiles repo at `~` is enough. That is worse
    than the `None` this returns for a frozen install: an unrelated project's
    commit is a confident, checkable-looking answer to a question nobody can
    tell went wrong.
    """
    head = _git(root, "rev-parse", "--show-toplevel", "HEAD")
    if head is None or head.returncode != 0:
        return None
    # splitlines, not split: a checkout path may contain spaces.
    lines = [ln.strip() for ln in head.stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        return None
    toplevel, commit = lines
    try:
        if Path(toplevel).resolve() != root.resolve():
            return None              # a repository, but not this package's
    except OSError:
        return None
    if not commit:
        return None
    state = _git(root, "status", "--porcelain")
    if state is None or state.returncode != 0:
        return f"{commit}-unknown"
    return commit if not state.stdout.strip() else f"{commit}-dirty"


def short(commit: str | None, width: int = 12) -> str:
    """A commit abbreviated for a one-line diagnostic, KEEPING its suffix.

    Truncating the whole string drops `-dirty` / `-unknown`, which is precisely
    the part that says the hash does not describe the running code -- so the
    short form would read as a clean, exact commit whenever it is least true.
    """
    if not commit:
        return "unknown"
    sha, _, mark = commit.partition("-")
    return f"{sha[:width]}-{mark}" if mark else sha[:width]


def code_provenance() -> dict:
    """`{skodun_version, skodun_commit}` for the code this process is running.

    CACHED, and that is the contract rather than an optimisation. A long-lived
    MCP server imported its modules at startup; if somebody runs `git pull`
    underneath it -- exactly what an editable install invites, and what happened
    on this machine mid-session -- the process goes on running the old code.
    Re-reading per review would stamp verdicts with a commit that never
    produced them, which is the opposite of what this field is for.

    `skodun_commit` is `None` for an install that is not a checkout. Explicit
    `None` rather than an absent key, for the reason `requested_reviewer` gives:
    absence would be indistinguishable from a record written before the field
    existed.
    """
    global _CACHED
    if _CACHED is None:
        with _LOCK:
            # Re-checked under the lock: two threads can both have seen it cold
            # above, and the second must take the first's answer rather than
            # compute its own on the other side of a `git pull`.
            if _CACHED is None:
                _CACHED = {
                    "skodun_version": __version__,
                    "skodun_commit": _read_commit(_package_root()),
                }
    return dict(_CACHED)


def cached_provenance() -> dict | None:
    """The answer if it is already known, else None. NEVER computes.

    For callers on a latency-critical path -- the MCP handshake is the one that
    matters -- where the field is worth having but never worth waiting for. A
    client that times out its `initialize` has lost the whole session, and no
    diagnostic is worth that.
    """
    return dict(_CACHED) if _CACHED is not None else None


def warm_async() -> threading.Thread:
    """Fill the cache on a daemon thread. Returns it, mostly for tests.

    Started when a long-lived process begins, so the git work is done long
    before anything asks. It takes ~27ms on a normal checkout, so in practice
    the answer is ready by the time the first request arrives; on a wedged git
    the process simply carries on without it, which is the point.
    """
    t = threading.Thread(target=code_provenance, name="skodun-provenance",
                         daemon=True)
    t.start()
    return t


#: What one drift probe learned. The four answers are kept apart because a
#: caller has to do something DIFFERENT with each, and collapsing them is what
#: made the first version of this both noisy and blind:
#:
#: * `DRIFT_SAME`      the disk is what we are running -- ask again later
#: * `DRIFT_MOVED`     it is not, and we can say what it is now -- report once
#: * `DRIFT_UNREADABLE` the probe failed this time -- say nothing, ask again
#: * `DRIFT_UNCOMPARABLE` there is no answer to be had, ever -- stop asking
DRIFT_SAME = "same"
DRIFT_MOVED = "moved"
DRIFT_UNREADABLE = "unreadable"
DRIFT_UNCOMPARABLE = "uncomparable"


def _sha(commit: str) -> str:
    """The hash without its `-dirty` / `-unknown` marker."""
    return commit.partition("-")[0]


def stale_against_disk() -> tuple[str, str | None]:
    """One drift probe: `(state, the commit on disk)`.

    Diagnostic only. It is deliberately NOT wired to any automatic update or
    restart: a fail-closed gate must not swap its own code underneath a running
    review, since a verdict produced half by one version and half by another
    certifies nothing anyone can reason about. An MCP server cannot restart
    itself either -- the host owns the pipe -- so "auto-restart" reduces to
    exiting and hoping, which turns an upgrade into a silent tool outage.

    UNCACHED, unlike `code_provenance`: the whole question is what the disk
    says NOW. The commit returned is what an operator would get by restarting,
    so the line they read tells them what the restart is worth.

    Two answers are easy to get wrong, and both were:

    * An `-unknown` on EITHER side means we could not establish that tree's
      state. Comparing the strings would read `abc-unknown` as different from
      `abc` and announce a move that never happened -- a transient
      `index.lock` from a concurrent commit is enough to trigger it. So when
      either side is unknown, only the hashes are compared: a different hash is
      still a real move, and the same hash means we simply cannot tell.
    * `DRIFT_UNCOMPARABLE` is not a quieter `DRIFT_SAME`. It means "stop
      asking", and a caller that read it as "no drift" would go on paying two
      subprocesses per call to re-learn the same nothing. A wheel install gives
      it because it will never be a checkout; a git that TIMED OUT gives it
      too, and that one is a deliberate trade -- a timeout may well be
      transient, but re-probing a wedged git spends the whole 2s-per-call
      budget on every request, and an operator whose git is stalling has a
      louder problem than a missing diagnostic.
    """
    running = code_provenance().get("skodun_commit")
    if running is None:
        return DRIFT_UNCOMPARABLE, None  # a frozen install cannot drift
    on_disk = _read_commit(_package_root())
    if on_disk is None:
        return DRIFT_UNCOMPARABLE, None  # not a checkout we can read, ever
    if on_disk == running:
        return DRIFT_SAME, on_disk
    if running.endswith("-unknown") or on_disk.endswith("-unknown"):
        if _sha(on_disk) != _sha(running):
            return DRIFT_MOVED, on_disk  # a different commit is a real move
        return DRIFT_UNREADABLE, None    # same commit, tree state unestablished
    return DRIFT_MOVED, on_disk
