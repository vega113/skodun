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
  definition. It is a diagnostic, and it is never acted on automatically: see
  the note on auto-update below.

Nothing here may raise or block. Provenance is a record of what happened, not a
precondition for it, and a machine without git must still be able to review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import __version__

#: `git rev-parse` is a local read, but it is on the path of every process
#: start. A ceiling keeps a wedged git (a network filesystem, a stuck index
#: lock) from turning provenance into a hang.
_GIT_TIMEOUT_SEC = 5.0

#: The answer for THIS PROCESS, computed once. `None` means "not asked yet".
#: Tests reset it; nothing else may.
_CACHED: dict | None = None


def _package_root() -> Path:
    """The directory a checkout of this package would have at its root.

    `…/src/skodun/provenance.py` → `…` for an editable install or source
    checkout. For a wheel in site-packages this is a directory that is simply
    not a git repository, which `_read_commit` answers `None` for.
    """
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """One git call, or None if git could not be run at all."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC)
    except BaseException:
        # Missing binary, a timeout, a permissions refusal -- all the same
        # answer here, and none of them is worth failing a review over.
        return None


def _read_commit(root: Path) -> str | None:
    """`HEAD` of the checkout at `root`, `-dirty` when it has edits, else None.

    The `-dirty` suffix is git-describe's convention and it earns its place: on
    a development machine the tree is usually modified, and a bare commit would
    name code that is not what ran. That is worse than saying nothing, because
    a precise-looking hash invites belief.
    """
    head = _git(root, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return None
    commit = head.stdout.strip()
    if not commit:
        return None
    # A failure to ANSWER "is it dirty" is not a claim that it is clean, so an
    # unusable result leaves the suffix off rather than guessing either way.
    edits = _git(root, "diff", "--quiet")
    if edits is not None and edits.returncode == 1:
        return f"{commit}-dirty"
    return commit


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
        _CACHED = {
            "skodun_version": __version__,
            "skodun_commit": _read_commit(_package_root()),
        }
    return dict(_CACHED)


def stale_against_disk() -> str | None:
    """The on-disk commit when it differs from the running one, else None.

    Diagnostic only. It is deliberately NOT wired to any automatic update or
    restart: a fail-closed gate must not swap its own code underneath a running
    review, since a verdict produced half by one version and half by another
    certifies nothing anyone can reason about. An MCP server cannot restart
    itself either -- the host owns the pipe -- so "auto-restart" reduces to
    exiting and hoping, which turns an upgrade into a silent tool outage.

    Returns the value an operator would get by restarting, so the line they
    read tells them what the restart is worth.
    """
    running = code_provenance().get("skodun_commit")
    if running is None:
        return None                      # a frozen install cannot drift
    on_disk = _read_commit(_package_root())
    if on_disk is None or on_disk == running:
        return None
    return on_disk
