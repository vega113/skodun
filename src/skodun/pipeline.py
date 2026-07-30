"""The foreground review pipeline: one `--now` review, start to banner.

This is the module that turns every other module into a review. It resolves the
base, captures the diff, selects checklist sections, packs file context, builds
the prompt, runs the reviewer under a watchdog with two independent retry axes,
runs the two extra passes, persists one artifact, and prints the verdict banner
from what it persisted. Ported from the oracle's `--now` path
(`scripts/grok-prepush-review.sh`) and its foreground lock
(`scripts/grok-review-now.sh:138-325`).

Four things here are load-bearing and are the reason this file is not just glue.

The foreground lock
-------------------
Two concurrent foreground reviews share one inference backend and do not fail
honestly — in the oracle's own incident a 61s review became a 10-minute timeout
because another worktree was reviewing at the same time. So a review waits for a
busy lock rather than racing it.

The lock is an atomic `mkdir` at `<git-common-dir>/grok-reviews-foreground.lock`
— **the legacy path** — and its `owner` file is **the legacy three-line byte
format** (`pid=`, `started=`, `worktree=`). Both halves are interop, not
decoration: during shadow runs skodun and the legacy shell scripts must
serialize against *each other*, and each must be able to judge the other's
liveness by reading a file the other wrote. Changing either is a silent
regression to two reviewers hammering one backend.

Reclaiming follows the oracle exactly (`_lock_is_reclaimable`): a recorded pid
that is provably dead is reclaimed at once, any lock past the stale ceiling is
reclaimed, and a lock whose owner cannot be parsed at all — a bare integer, a
truncated write, a half-written file — is "owner unknown" and may only be
reclaimed after a 30s write grace, never on a liveness check that cannot be
made. Release is guarded by an ABA check: we remove the lock directory only
while the owner file still names *our* pid, because a peer that legitimately
reclaimed it after our stale window must not have its lock deleted by us.

The stale ceiling and the wait cap default to `budget.lock_stale_ceiling` — the
worst-case runtime the config implies for the primary review *plus both extra
passes*, because those run inside this lock with their own retry budgets, and
scaled by the batch count when the diff has to be batched. (The narrower
`budget.worst_runtime` covers what one record covers and stays where the brief
pins it: `recover_stale`.) All three, plus the poll cadence, can be overridden
with `SKODUN_LOCK_WAIT_SECONDS` / `SKODUN_LOCK_POLL_SECONDS` /
`SKODUN_LOCK_STALE_SECONDS`, mirroring the oracle's `GROK_FG_LOCK_*` knobs — a
wedged lock has to be survivable without a code change. Junk in any of them
degrades to the default rather than to a crash or a busy-spin.

The lock's `budget` sidecar
---------------------------
A waiter decides whether to reclaim using ITS OWN stale ceiling, and a BATCHED
holder legitimately runs for `batch_count + 1` reviewer budgets — so a
small-diff waiter would reclaim a live multi-batch holder and put two reviews on
one backend. The holder therefore publishes its own budget inside the lock
directory (`<lock>/budget`, one line, seconds) and waiters re-read it on every
reclaim decision, using `max(own ceiling, holder budget)`.

Three properties make that safe, and each is pinned by a test:

* **`mkdir` acquires; `budget` then `owner` publish.** `mkdir` is the atomic
  no-replace primitive (`os.rename` of a temp directory silently REPLACES an
  existing empty one, which would clobber a legacy holder between its own
  `mkdir` and its owner write). Publishing the budget FIRST is what makes "a
  complete owner from a skodun holder implies the sidecar exists" true, so a
  waiter can tell a batched skodun holder from a legacy one; `_release_fg_lock`
  retracts the owner first for the same reason.
* **Reclaim rules are otherwise the shipped ones, untouched.** A bare or
  unparsable owner still reclaims past the 30s write grace, and a provably dead
  pid still reclaims at once whatever the sidecar says — so the sidecar can only
  ever protect a holder whose process is genuinely alive. A skodun holder's
  few-millisecond initialization window carries exactly the write-grace exposure
  a legacy holder's own owner write has always had: unchanged, recorded, not
  redesigned.
* **It grows, never shrinks.** The pre-lock capture only SIZES the lock; the
  authoritative plan is rebuilt under it and republishes a bigger budget when
  the worktree grew while we waited.

RECORDED LIMITATION (transitional): a coexisting LEGACY waiter reads only
`owner` and honours its own fixed ceiling, so during shadow coexistence a
batched foreground run longer than that could still be reclaimed by the legacy
scripts. Accepted — the sidecar is additive by design, and coexistence ends when
the legacy scripts do. Note also that an explicit `SKODUN_LOCK_STALE_SECONDS`
cannot shrink a holder's published budget: the escape hatch for a genuinely
wedged batched lock is removing the lock directory, which is what the
`LockTimeout` message already tells the operator.

Batched review
--------------
A diff over `max_diff_bytes` is not truncated (which could never be
trustworthy); it is split into deterministic size-bounded batches, each reviewed
as a full sub-review, followed by one cross-file integration pass over the seams
the split cut — and persisted as ONE artifact at the FULL diff's identity, so
the gate, dedup and the fallback contract see exactly what they see for a
single-shot review. `_orchestrate` owns that, and its own comment block explains
the aggregation rules; small diffs never enter it at all.

One divergence from the oracle is deliberate here. The oracle demotes an
aggregate whose sub-reviews did not all report the SAME model
("provider-model-mismatch"), because in its world a second model appearing is an
invisible fallback nobody asked for. In skodun a fallback hop is a designed,
recorded outcome — `chain.run_chain` advances only on `unavailable`, and
`batches[].provider/model` says exactly who answered which batch — so a
legitimate hop demotes nothing; the aggregate's indexed `model`/`adapter`
columns simply keep the configured finder's identity unless every sub-review
agrees on one answering entry.

Startup recovery
----------------
A run killed with SIGKILL never reaches its own `finally`, so its `running`
record would sit in the store forever. `recover_stale` is the only reliable
janitor: at the start of every run it fails any `running` record older than the
worst-case runtime this config can produce.

Retries are always fresh runs, and a chain hops only on `unavailable`
---------------------------------------------------------------------
Two independent retry axes, exactly as the oracle: a hard timeout retries up to
`timeout_retries`, a *degraded* result retries up to `degraded_retries`, and
neither ever resumes a session. **A timed-out attempt is never parsed.** The
runner truncates a timed-out run's stdout to zero bytes for precisely this
reason: a process can print a complete, clean-looking envelope and then hang,
and parsing that would mint a trustworthy clean review out of a run that never
finished.

A third axis sits above both: a reviewer may declare an ordered chain of
fallback entries, and `_run_chain` advances to the next one when — and only
when — an attempt classifies `unavailable`. An entry that answered *badly*
stops the chain instead, because a degraded or unparseable answer is a harness
problem and hopping providers on it would spend someone else's quota to hide a
bug. An exhausted chain is an explicit, untrustworthy `failed` record: the
whole point of the mechanism is that it fails closed, never that it finds a
way to produce a pass.

The banner comes from the record
--------------------------------
The last line of stdout is always a verdict, and on every path where a record
exists it is rendered from the *persisted* record — read back, never recomputed
— so the banner and the row the gate later reads cannot disagree. Paths that
never persisted anything raise, and the caller (`cli.py`) renders
`banner_failure`. That is the only division of labour here: this module prints
the record-backed banner, the CLI prints the failure banner and owns the exit
codes.
"""

from __future__ import annotations

import calendar
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import (batching, budget, chain, checklist, contextpack, gitio, passes,
               promptbuild)
from .adapters import REFUTER_CONTRACT, get_adapter
from .config import Config, Defaults, Reviewer
from .store import Store, _TS_FORMAT
from .trust import banner, is_trustworthy

#: The legacy lock directory name. Interop-critical: see the module docstring.
LOCK_NAME = "grok-reviews-foreground.lock"
#: How long a `quota` outage is remembered for the WHOLE provider. Only
#: `quota` is ever cached (see `_remember_unavailable`), and never without a
#: TTL: a provider must always become eligible again on its own.
PROVIDER_UNAVAILABLE_TTL_SEC = 1800
#: Poll cadence while another worktree holds the lock (oracle default).
LOCK_POLL_SEC = 10.0
#: How long an owner file may be missing/unparsable before the lock is assumed
#: orphaned. Writing the owner is near-instant; 30s is the oracle's grace.
LOCK_WRITE_GRACE_SEC = 30.0
#: The holder's own runtime budget, in seconds, published INSIDE the lock
#: directory beside the byte-pinned `owner` file. Additive: the legacy scripts
#: parse only `owner`, so a legacy holder simply has no sidecar and a legacy
#: waiter never looks for one. See `_write_budget` for the whole protocol.
LOCK_BUDGET_NAME = "budget"
#: The largest sidecar value that is read as a runtime budget rather than as
#: junk. Numeric sanity only — see `_holder_budget`.
LOCK_BUDGET_MAX_SEC = 365 * 24 * 3600
#: Grace added to the worst-case runtime before a `running` record is swept, and
#: how many full reviewer runs one held lock can cover. Both now live in
#: `budget.py` — where the batch-count scaling lives too — and are aliased back
#: under their shipped names so that nothing importing them has to move.
STALE_RECORD_GRACE_SEC = budget.GRACE_SEC
_MAX_PASSES_UNDER_LOCK = budget.MAX_PASSES_UNDER_LOCK

#: `Store.list_reviews` passes this straight to SQLite's `LIMIT`, where a
#: negative value means "no upper bound". `recover_stale` must see EVERY row:
#: the query orders by `reviewed_at DESC`, so the stale records it exists to
#: clean are the last ones it would reach, and any finite cap would skip
#: exactly them on a busy store.
_SCAN_ALL = -1


class PipelineError(RuntimeError):
    """Base class for the pipeline's own refusals."""


class PreflightRefused(PipelineError):
    """The run was refused before any review could be attempted (exit 2)."""


class LockTimeout(PipelineError):
    """Another foreground review held the lock for the whole wait (exit 3)."""


class PersistenceFailed(PipelineError):
    """The review ran but could not be recorded (exit 4).

    The review is worthless if it cannot be read back: the gate reads the
    store, not this process's memory.
    """


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _note(message: str) -> None:
    """Progress goes to stderr; stdout carries the verdict and nothing else."""
    try:
        print(f"skodun: {message}", file=sys.stderr, flush=True)
    except BaseException:
        pass   # a broken stderr must never be what fails a review


def _iso_at(epoch: float) -> str:
    """`epoch` as the ONE timestamp shape the store accepts (UTC, seconds, Z).

    The store validates this format at its door and orders `provider_state`
    TTLs by plain string comparison, which is only correct because every field
    is zero-padded to a constant width. `_TS_FORMAT` is `store`'s own
    definition, imported rather than re-spelled here, so the two can never
    quietly drift apart.
    """
    return time.strftime(_TS_FORMAT, time.gmtime(epoch))


def _iso_now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


def _new_id(prefix: str = "sk_") -> str:
    """`sk_<utcstamp>_<pid>_<uuid8>`.

    The uuid component is mandatory. Second-resolution time plus pid collides
    for two runs in the same process-second — which the review loop does
    routinely — and `Store.save_review` upserts by id, so the second run would
    silently overwrite the first.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}{stamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _env_seconds(name: str, default: float) -> float:
    """A positive float from the environment, or `default`.

    Mirrors the oracle's numeric-guard pattern on `GROK_FG_LOCK_*`: junk
    degrades to the default rather than to a crash or a busy-spin.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _epoch(stamp: object) -> float | None:
    """Parse a stored `reviewed_at` into epoch seconds, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(stamp, _TS_FORMAT)))
    except (TypeError, ValueError):
        return None


#: The per-entry attempt budget, now `budget.attempt_budget`. Alias kept because
#: this module's docstrings and comments talk about it by name.
_attempt_budget_sec = budget.attempt_budget


def max_chain_width(cfg: Config) -> int:
    """The most reviewer entries one pass of this config can execute.

    A fallback chain is `head + len(head.fallbacks)` entries, and `config`
    caps a chain at four. This is the *configured* width, not the observed one:
    the ceilings below have to budget the worst run the config permits, and how
    many entries a given run actually reaches is not knowable before it starts.

    Every reviewer is considered, disabled ones included. Over-estimating the
    width only makes the ceilings wider, which is the fail-safe direction for
    the one thing that rides on them — never reclaiming a lock whose holder is
    still alive.
    """
    return max((1 + len(r.fallbacks) for r in cfg.reviewers), default=1)


def worst_runtime_sec(d: Defaults, max_chain_width: int = 1) -> int:
    """The longest one UNBATCHED review can legitimately take, plus a grace.

    The shipped name and the shipped number, kept as a wrapper over
    `budget.worst_runtime(..., batch_count=0)`: this is the figure an unbatched
    review's record carries and the age at which `recover_stale` sweeps a
    `running` record that carries no budget of its own. A batched review needs
    `budget.worst_runtime` directly, with its batch count — see that module for
    why the two multipliers exist and why the lock's ceiling is a different,
    wider number.
    """
    return budget.worst_runtime(d, max_chain_width, 0)


def lock_stale_ceiling_sec(d: Defaults, max_chain_width: int = 1) -> int:
    """The age at which an UNBATCHED holder's foreground lock may be reclaimed.

    The shipped name and the shipped number, over
    `budget.lock_stale_ceiling(..., batch_count=0)`. `run_review` calls the
    helper directly with the batch count its diff implies, and publishes the
    result in the lock's `budget` sidecar so a small-diff waiter cannot reclaim
    a live batched holder.
    """
    return budget.lock_stale_ceiling(d, max_chain_width, 0)


# ---------------------------------------------------------------------------
# stale-record recovery
# ---------------------------------------------------------------------------


def _record_budget(rec: dict) -> int | None:
    """The runtime budget a record persisted for ITSELF, or None.

    Only a positive plain `int` is evidence. `isinstance(True, int)` is True in
    Python, so the bool check has to be explicit and has to come first; a string,
    a float or a non-positive number is treated as absent, never as "never sweep
    this row" — a janitor that can be switched off by one malformed field is not
    a janitor.
    """
    value = rec.get("worst_runtime_sec")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def recover_stale(store: Store, cfg: Config) -> int:
    """Fail every `running` record older than the worst-case runtime.

    Returns how many were swept. A SIGKILLed run never reaches its own
    `finally`, so this startup sweep is the only reliable janitor; without it
    a killed review leaves a `running` row that nothing ever finishes.

    The record's OWN persisted `worst_runtime_sec` is preferred over
    recomputation (the oracle's `.meta max_runtime_seconds` solution). A batched
    review is one record covering `batch_count + 1` sequential model calls, so
    the single-review ceiling this config computes would call it stale long
    before it is over — and sweeping it marks a review that is still running as
    failed. Recomputation is also the wrong question: it uses the config as it
    is NOW, and the only budget that can judge a run is the one its own config
    implied when it started. A record with no usable budget of its own (every
    pre-Phase-3 row) keeps the computed ceiling exactly as before.

    A record whose `reviewed_at` will not parse is left alone: age is the only
    evidence this function has, and it will not act on evidence it does not
    have. Best-effort per record — one unwritable row must not stop the sweep,
    and must certainly not stop the review that follows.
    """
    computed = worst_runtime_sec(cfg.defaults, max_chain_width(cfg))
    now = time.time()
    swept = 0
    for rec in store.list_reviews(None, _SCAN_ALL):
        if not isinstance(rec, dict) or rec.get("status") != "running":
            continue
        rid = rec.get("id")
        started = _epoch(rec.get("reviewed_at"))
        if not isinstance(rid, str) or started is None:
            continue
        persisted = _record_budget(rec)
        ceiling = computed if persisted is None else persisted
        if now - started <= ceiling:
            continue
        try:
            store.set_status(rid, "failed")
        except Exception as e:      # pragma: no cover - defensive
            _note(f"could not recover stale review {rid}: {e!r}")
            continue
        swept += 1
        _note(f"recovered stale review {rid} (older than {ceiling}s) as failed")
    return swept


# ---------------------------------------------------------------------------
# the foreground lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lock:
    """A held foreground lock, and the pid that must still own it to release."""

    path: Path
    pid: int


def _write_owner(lock: Path, pid: int, worktree: Path) -> None:
    """Publish the owner file: the EXACT legacy three-line byte format.

    Written LAST of the two files inside the lock (see `_write_budget`), and
    written whole in one call — the legacy scripts judge our liveness by parsing
    this file during shadow runs, and vice versa, so its bytes are interop and
    not decoration.
    """
    (lock / "owner").write_text(
        f"pid={pid}\nstarted={int(time.time())}\nworktree={worktree}\n",
        encoding="utf-8")


def _write_budget(lock: Path, seconds: float) -> None:
    """Publish the holder's own runtime budget inside the lock directory.

    ONE line, integer seconds. It exists because a WAITER decides whether to
    reclaim using ITS OWN stale ceiling, and a batched holder legitimately runs
    for `batch_count + 1` reviewer budgets: without this file a small-diff
    waiter would take a live multi-batch holder's lock and put two reviews on
    one inference backend — the exact failure the lock exists to prevent.

    Two properties are load-bearing:

    * **Additive.** The legacy scripts read only `owner`, so this file is
      invisible to them; a legacy holder simply has none, which is precisely
      how a waiter tells the two apart (complete `owner` + sidecar → a skodun
      holder whose budget is knowable; complete `owner` + no sidecar → a legacy
      holder, judged by the waiter's own ceiling — the recorded coexistence
      limitation).
    * **Published BEFORE the owner, and replaced atomically.** The temp file
      lives inside the lock directory so the rename is same-filesystem and
      therefore atomic, and it carries the pid so two processes cannot collide
      on it. A reader therefore never sees a half-written budget, and a complete
      owner from a skodun holder always implies the sidecar is already there.
    """
    tmp = lock / f".{LOCK_BUDGET_NAME}.{os.getpid()}.tmp"
    tmp.write_text(f"{int(seconds)}\n", encoding="utf-8")
    os.replace(tmp, lock / LOCK_BUDGET_NAME)


def _holder_budget(lock: Path) -> float | None:
    """The holder's published budget in seconds, or None.

    None for every unusable value — no file, an empty or non-numeric first
    line, a non-positive number, or a number too large to be a runtime budget
    at all (`LOCK_BUDGET_MAX_SEC`) — because this figure can only ever WIDEN a
    waiter's stale ceiling. A corrupt sidecar must therefore read as "no
    information" and never as "never reclaim this lock".

    The upper bound is numeric sanity, not policy: `isdigit()` accepts a
    400-digit integer, which `float()` cannot even represent. It is deliberately
    far above any ceiling a real config can compute (a 4-wide chain over a
    hundred batches is still weeks, not years). Note also what this value can
    and cannot delay: a reclaim on a provably DEAD pid, and the write grace on a
    bare or unparsable owner, are both decided after the age check and are
    unaffected — so even a nonsense sidecar can only ever protect a holder whose
    process is genuinely alive, which is exactly the holder that must not be
    reclaimed.
    """
    try:
        raw = (lock / LOCK_BUDGET_NAME).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not line.isdigit():
        return None
    value = int(line)
    if not 0 < value <= LOCK_BUDGET_MAX_SEC:
        return None
    return float(value)


def _grow_budget(lock: Path, seconds: float) -> bool:
    """Raise the published budget to `seconds`. NEVER lower it.

    Returns whether the sidecar was rewritten. The under-lock authoritative
    batch plan can be BIGGER than the pre-lock estimate the lock was sized with
    (a long wait can change the worktree), and a waiter reading the pre-wait
    number would reclaim a live holder. It can also be smaller — a shrinking
    ceiling is never republished, because the only thing riding on this number
    is not acting on a run that is still alive.
    """
    current = _holder_budget(lock)
    if current is not None and current >= seconds:
        return False
    _write_budget(lock, seconds)
    return True


def _owner_field(lock: Path, key: str) -> str | None:
    """One `key=value` line from the legacy owner file, or None.

    Parsed exactly the way the legacy scripts parse it (`sed -n 's/^pid=//p'`),
    so an owner file written by either side reads identically on both.
    """
    try:
        raw = (lock / "owner").read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    for line in raw.splitlines():
        k, sep, v = line.partition("=")
        if sep and k == key:
            return v.strip()
    return None


def _owner_pid(lock: Path) -> int | None:
    """The owner's pid, or None for a missing/unparsable owner ("unknown")."""
    value = _owner_field(lock, "pid")
    if value is None or not value.isdigit():
        return None
    pid = int(value)
    # pid 0 addresses our whole process group in `kill(2)`; never signal it.
    return pid if pid > 0 else None


def _owner_started(lock: Path) -> float | None:
    value = _owner_field(lock, "started")
    return float(value) if value is not None and value.isdigit() else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # exists, owned by someone else
    except OSError:
        return True     # unknown: never reclaim on an ambiguous answer
    return True


def _lock_age(lock: Path, started: float | None) -> float:
    """Seconds since the lock was taken.

    From the owner file's `started=` when it parses — that is what the legacy
    side records and the only value that survives a directory whose mtime moved
    — and from the directory mtime otherwise. A lock that cannot be stat'ed at
    all reports age 0, i.e. "too fresh to reclaim": the conservative answer.
    """
    if started is not None:
        return time.time() - started
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return 0.0


def _lock_is_reclaimable(lock: Path, stale: float, grace: float) -> bool:
    """Whether this lock may be taken from its owner. Oracle-equivalent.

    The SHIPPED rules, verbatim and untouched, with exactly one addition: the
    holder's published budget is RE-READ here, on every reclaim decision, and
    widens `stale` when it is larger. Re-read rather than sampled once, because
    the holder rewrites it when its under-lock plan turns out bigger than the
    plan its lock was sized with. `max(own ceiling, holder budget)` and never
    the holder's alone: a holder cannot be allowed to make its own lock
    permanently unreclaimable by publishing a large number.

    Positive evidence only, in the oracle's own order:

    * past the stale ceiling — the holder exceeded its own worst case, so it is
      wedged or dead either way;
    * a recorded pid that is provably dead — reclaimed at once, no grace: the
      pid is in the file, so the holder got far enough to write it, and it is
      gone. (All worktree runs are the same user, so `EPERM` ambiguity does not
      arise; `_pid_alive` still errs towards "alive" if it ever did.)
    * owner unknown — the file is missing, truncated, or in some other format
      (a bare integer, say). That is exactly what a holder that has not
      finished writing its owner file looks like, so only the write grace may
      reclaim it, never a liveness check that cannot be made.
    """
    if not lock.is_dir():
        return False
    holder = _holder_budget(lock)
    if holder is not None and holder > stale:
        stale = holder
    pid = _owner_pid(lock)
    started = _owner_started(lock)
    age = _lock_age(lock, started)
    if age > stale:
        return True
    if pid is not None:
        return not _pid_alive(pid)
    # Owner unknown. The oracle requires BOTH fields unreadable before the
    # short grace applies; a lock whose `started=` parsed has a real age and is
    # governed by the stale ceiling above.
    return started is None and age > grace


def _acquire_fg_lock(common_dir: Path, worktree: Path, *, wait: float,
                     poll: float, stale: float,
                     grace: float = LOCK_WRITE_GRACE_SEC,
                     budget_sec: float | None = None) -> Lock:
    """Take the foreground lock, waiting up to `wait` seconds for it.

    Waiting on a busy lock is the safe behaviour; racing it is not. Raises
    `LockTimeout` if the holder outlasts the wait.

    Acquisition is `mkdir` — the atomic no-replace primitive — and the two files
    inside it are published in a FIXED ORDER: `budget` first, `owner` last.

    `mkdir` is not one of several ways to do this. `os.rename` of a prepared
    temp directory silently REPLACES an existing empty directory on POSIX
    (verified on this host), which would clobber a legacy holder caught between
    its own `mkdir` and its owner write — the one moment that holder cannot
    defend itself. `EEXIST` is contention, exactly as it has always been.

    `budget_sec` is the holder's own runtime budget for the sidecar, and it
    defaults to `stale` — which is the same number by construction: `run_review`
    derives both from `budget.lock_stale_ceiling` for the batch plan its diff
    implies. It is a parameter so that a caller with a better figure (or a test)
    can publish it explicitly.
    """
    lock = Path(common_dir) / LOCK_NAME
    worktree = Path(worktree).resolve()
    pid = os.getpid()
    published = float(stale if budget_sec is None else budget_sec)
    deadline = time.monotonic() + wait
    noted = False

    while True:
        taken = True
        try:
            lock.mkdir(parents=True)
        except FileExistsError:
            taken = False
        if taken:
            try:
                # BUDGET FIRST, owner second. That order is what makes "a
                # complete owner from a skodun holder implies the sidecar
                # exists" true, and it is the only way a waiter can tell a
                # batched skodun holder (whose budget it must honour) from a
                # legacy one (which has none) instead of guessing.
                _write_budget(lock, published)
                _write_owner(lock, pid, worktree)
                return Lock(path=lock, pid=pid)
            except OSError:
                # Never hold the lock bare: release only removes a lock whose
                # owner names us, so an ownerless hold would wedge every peer
                # until the stale ceiling.
                shutil.rmtree(lock, ignore_errors=True)

        if _lock_is_reclaimable(lock, stale, grace):
            _note(f"reclaiming stale foreground review lock (owner "
                  f"pid={_owner_pid(lock) or 'unknown'}): {lock}")
            shutil.rmtree(lock, ignore_errors=True)
            if not lock.exists():
                # Retry immediately: a waiter that just freed the lock must not
                # give up on the same tick it made progress on.
                continue
        elif not noted:
            noted = True
            _note(f"another foreground review is running "
                  f"(pid={_owner_pid(lock) or 'unknown'}); waiting -- "
                  f"serializing avoids the shared-inference timeout")

        if time.monotonic() >= deadline:
            raise LockTimeout(
                f"gave up after {wait:g}s waiting for the foreground review "
                f"lock held by pid={_owner_pid(lock) or 'unknown'}; re-run "
                f"when that review finishes, or remove {lock} if it is wedged")
        time.sleep(poll)


def _release_fg_lock(lock: Lock) -> bool:
    """Remove the lock directory iff its owner file still names our pid.

    The ABA guard, and the reason it is not optional: our stale window may have
    expired while we were still running, a peer may have legitimately reclaimed
    the lock and started its own review, and deleting *its* lock on our way out
    would put two reviews on the backend at once — the exact failure the lock
    exists to prevent.

    The owner is RETRACTED first, then the directory removed, which keeps the
    acquisition invariant true in both directions: at no instant does this lock
    show a complete owner with no budget sidecar beside it. A plain `rmtree`
    deletes entries in directory order, so it can remove `budget` first and
    leave a lock that reads to a waiter exactly like a live legacy holder — a
    momentary lie in the one file peers make liveness decisions from.
    """
    if _owner_pid(lock.path) != lock.pid:
        return False
    try:
        (lock.path / "owner").unlink()
    except OSError:
        pass    # already gone, or a directory we are about to remove anyway
    shutil.rmtree(lock.path, ignore_errors=True)
    return True


# ---------------------------------------------------------------------------
# running one reviewer chain
# ---------------------------------------------------------------------------


#: `run_chain`'s output shape now lives in `chain.py`; aliased back here so
#: `_apply`'s type hint keeps resolving and nothing constructing/reading an
#: outcome needs to know it moved.
_Outcome = chain._Outcome


def _chain_for(cfg: Config, head: Reviewer) -> list[Reviewer]:
    """`[head] + head.fallbacks`, resolved by name against the config.

    Only the HEAD's list is followed: a chain member's own `fallbacks` are not
    expanded, so a chain is exactly as long as the entry that started it says
    (`config.Reviewer.fallbacks` documents both halves, and validates the
    stricter one at load time — every name exists, is enabled, and no cycle).

    A name that does not resolve is a `PreflightRefused` rather than a silent
    shorter chain: preflight walks this same function for every reviewer this
    run may reach, so the refusal lands before the lock and before any record.
    """
    by_name = {r.name: r for r in cfg.reviewers}
    chain = [head]
    for name in head.fallbacks:
        entry = by_name.get(name)
        if entry is None:
            raise PreflightRefused(
                f"reviewer {head.name!r}: fallback {name!r} does not exist; "
                f"no review ran")
        chain.append(entry)
    return chain


def _is_path_shaped(binary: str) -> bool:
    """Whether `binary` should be resolved as a path rather than walked
    through `PATH`: it contains `/`, or the platform's own separator on a
    platform where that differs from `/`.

    The ONE definition of the path-vs-PATH split, shared by
    `chain._binary_is_absent` and `cli._fmt_binary`'s diagnostic -- both
    decide whether a per-adapter `SKODUN_<X>_BIN` override or grok's own
    `~/.grok/bin/grok` default gets checked directly, exactly how the
    adapter's own `Popen` call would resolve it. Before this was factored
    out, `cli._fmt_binary` carried its own copy of this exact condition,
    free to drift from this one.
    """
    return "/" in binary or (os.sep != "/" and os.sep in binary)


#: The chain executor itself now lives in `chain.py` as `run_chain`; this
#: one-line alias is the whole compatibility surface -- existing tests
#: monkeypatch `pipeline._run_chain` by name (`test_pipeline.py`,
#: `test_refuter.py`), and `run_review`/`_extra_pass`/`_refuter_pass` below
#: still call the bare name `_run_chain`, so the patched value is what they
#: see.
_run_chain = chain.run_chain


def _provenance(outcome: _Outcome) -> dict:
    """`{provider, model, effort}` for the attempt an extra pass should credit.

    The ACCEPTED attempt when there is one; else the TERMINAL attempt — the
    last one that actually executed, which is what a degraded or unparseable
    pass should be attributed to; else explicit `None`s and a `note` naming the
    cause, because nothing ever started a process. Explicit nulls rather than
    absent keys: a meta object that quietly omits the fields invites a reader
    to assume the pass ran on the finder's model.
    """
    if outcome.accepted is not None:
        a = outcome.accepted
        return {"provider": a["provider"], "model": a["model"],
                "effort": a["effort"]}
    for row in reversed(outcome.attempts):
        if "skipped" not in row:
            return {"provider": row.get("provider"), "model": row.get("model"),
                    "effort": row.get("effort")}
    return {"provider": None, "model": None, "effort": None,
            "note": outcome.failure_reason or "no attempt started a process"}


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


# Findings telemetry: ONE definition, imported rather than re-spelled here.
# `passes._merge` recomputes both from the merged findings, so its versions win
# on every record that goes through an extra pass — a second definition in this
# module could therefore only ever drift into disagreeing with the record the
# gate actually reads. (Both are safe on this module's input: the adapter's
# validator rejects any payload whose findings are not all dicts, so a
# `parse_ok` outcome never carries a non-mapping finding.)
_severity_counts = passes._severity_counts
_rule_ids = passes._rule_ids


def _status_for(rec: dict) -> str:
    """The oracle's status mapping, in its order.

    `parse_ok` first, so a review that both failed to parse and came back
    degraded reads as `failed`; a truncated diff (trustworthy false with both
    other axes clean) is `failed` too — the model did not see the change.
    """
    if rec.get("parse_ok") is not True:
        return "failed"
    if rec.get("degraded") is True:
        return "degraded"
    if not is_trustworthy(rec.get("parse_ok"), rec.get("degraded"),
                          rec.get("diff_truncated")):
        return "failed"
    return "clean"


def _reviewer_for(cfg: Config, role: str) -> Reviewer | None:
    for r in cfg.reviewers:
        if r.enabled and r.role == role:
            return r
    return None


#: Pass -> the configured reviewer role it prefers over the finder, in the
#: order the passes are scheduled. ONE table: `_pass_reviewer` reads it to
#: pick the reviewer and preflight reads it to validate every reviewer this run
#: may reach for, so a new pass cannot be wired up on one side only.
#:
#: `integration` is in the table but is NOT a `--now` extra pass: it is the
#: cross-file pass a BATCHED review runs over its own seams, scheduled by batch
#: count (`passes.should_run_integration`) in either mode. It belongs here for
#: exactly the reason the table exists — an `integrator` reviewer with a bogus
#: provider must be refused before the lock and before any model call, not
#: discovered by the luck of a diff that happened to need splitting.
#:
#: `skeptic` and `refuter` collide on the role name `refuter` — pre-existing
#: (the skeptic already preferred that role before this pass existed) and
#: still safe now that `refuter` is a real, separately-scheduled pass: the two
#: are mutually exclusive by `should_run_skeptic`/`refuter_decision`'s own
#: eligibility (the skeptic only runs on a trustworthy CLEAN finder, the
#: refuter only on a trustworthy finder WITH findings), so at most one of them
#: ever reads this table in a given run. If that mutual exclusion ever
#: changes, this shared name needs a second look.
_EXTRA_PASS_ROLES = {"security": "security", "skeptic": "refuter",
                     "refuter": "refuter",
                     passes.INTEGRATION_PASS: passes.INTEGRATION_ROLE}


def _pass_reviewer(cfg: Config, pass_name: str, finder: Reviewer) -> Reviewer:
    """The reviewer an extra pass will use: its role's, else the finder's.

    See `_extra_pass` for why the role-specific preference exists at all.
    """
    reviewer = _reviewer_for(cfg, _EXTRA_PASS_ROLES[pass_name])
    return reviewer if reviewer is not None else finder


def _adapter_for(reviewer: Reviewer):
    """Resolve one reviewer's adapter, or refuse the run before anything ran.

    An unknown provider is a CONFIG error, not a review failure, so it must
    surface as a `PreflightRefused` (exit 2) and not as the bare `ValueError`
    the registry raises — which the CLI can only read as "the review failed"
    (exit 4), a verdict about a review that never happened.
    """
    try:
        return get_adapter(reviewer.provider)
    except ValueError as e:
        raise PreflightRefused(
            f"reviewer {reviewer.name!r} (role {reviewer.role!r}): {e}; "
            f"no review ran") from e


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------


def run_review(repo: Path, cfg: Config, store: Store, mode: str = "now",
               lock_wait: float | None = None, lock_poll: float | None = None,
               id_prefix: str = "sk_",
               lock_stale: float | None = None) -> dict:
    """Run one foreground review and return the record that was persisted.

    Prints progress to stderr and the verdict banner — rendered from the record
    read back out of the store — as the last line of stdout. Raises
    `PreflightRefused`, `LockTimeout` or `PersistenceFailed` on the paths where
    no record could be produced; `cli.py` maps those to exit codes and to a
    `banner_failure` line, so the "last stdout line is a verdict" invariant
    holds on every path, including the ones that never got this far.
    """
    repo = Path(repo)
    d = cfg.defaults

    # --- 1. preflight -----------------------------------------------------
    # Refused so that a model session bound to the main checkout cannot review
    # (or be pointed at) the wrong tree while agents work in linked worktrees.
    if gitio.is_primary_checkout(repo) and os.environ.get("SKODUN_ALLOW_MAIN") != "1":
        raise PreflightRefused(
            f"{repo} is the primary checkout; run the review from a linked "
            f"worktree or set SKODUN_ALLOW_MAIN=1; no review ran")
    finder = _reviewer_for(cfg, "finder")
    if finder is None:
        raise PreflightRefused(
            "no enabled reviewer with role 'finder' is configured; no review ran")
    # Resolved here, before anything is locked or persisted: an unknown
    # provider is a config error, not a review failure. EVERY reviewer this run
    # may reach for is resolved now, not just the finder's — a bad provider on a
    # configured `security`/`refuter` reviewer used to surface only after the
    # primary review had already run, demoting it to untrustworthy and spending
    # a model call to report a config typo as exit 4.
    #
    # The whole GRAPH, not just the heads: a fallback entry is a reviewer this
    # run may execute, so a typo on one is refused here too. Discovering it
    # mid-chain would mean discovering it only on the runs where the head
    # happened to fail — a config error found by luck of the outage.
    adapter = _adapter_for(finder)
    for reviewer in (finder, *(_pass_reviewer(cfg, p, finder)
                               for p in _EXTRA_PASS_ROLES)):
        for entry in _chain_for(cfg, reviewer):
            _adapter_for(entry)

    # --- 2. sweep the wreckage of any SIGKILLed predecessor ---------------
    recover_stale(store, cfg)

    # --- 3. the foreground lock -------------------------------------------
    # `lock_stale_ceiling`, NOT `worst_runtime`: the extra passes run inside this
    # lock with their own retry budgets, so a live holder can legitimately
    # outlast a single run's worst case. (`recover_stale` above keeps the
    # narrower figure; the docstrings on both say why.) Scaled by the configured
    # chain width, because each of those passes may now work through a whole
    # chain inside this lock — and by a pre-lock ESTIMATE of the batch count,
    # because a batched review makes `batch_count + 1` sequential model calls
    # inside it.
    #
    # STAGE ONE of the two-stage ordering, and it is only ever an estimate: this
    # capture happens before the lock, the wait can be long, and the worktree can
    # change under us. It sizes the ceiling and nothing else — every field the
    # review persists comes from the authoritative capture under the lock, and
    # the ceiling is re-derived there from the LARGER of the two plans.
    width = max_chain_width(cfg)
    estimate = _estimate_batch_count(repo, d)
    ceiling = float(budget.lock_stale_ceiling(d, width, estimate))
    stale = lock_stale if lock_stale is not None else _env_seconds(
        "SKODUN_LOCK_STALE_SECONDS", ceiling)
    wait = lock_wait if lock_wait is not None else _env_seconds(
        "SKODUN_LOCK_WAIT_SECONDS", stale)
    poll = lock_poll if lock_poll is not None else _env_seconds(
        "SKODUN_LOCK_POLL_SECONDS", LOCK_POLL_SEC)

    # `_worktree_root` is package-internal on purpose: it is the same
    # normalisation `capture_diff` applies, and re-deriving it here with a
    # second `git rev-parse` would be a second definition of "the root" that
    # could drift from the one the diff was captured against.
    root = gitio._worktree_root(repo)
    lock = _acquire_fg_lock(gitio.git_common_dir(repo), root,
                            wait=wait, poll=poll, stale=stale)

    rid = _new_id(id_prefix)
    persisted = False
    finalized = False
    try:
        # --- 4. identity: base, diff, diff hash. No dedup: `--now` always
        # runs a fresh review; the dedup probe is dispatcher machinery.
        base = gitio.resolve_base(repo)
        if base.warning:
            _note(f"identity note: {base.warning}")
        diff = gitio.capture_diff(repo, base.sha, d.untracked_max)
        if diff.truncated_untracked:
            _note(f"identity note: untracked scan capped at {d.untracked_max}")
        diff_hash = gitio.diff_identity(diff.data)
        branch = gitio.current_branch(repo)
        head = gitio.head_sha(repo)

        # STAGE TWO of the two-stage ordering: the AUTHORITATIVE batch plan,
        # built from the capture above — the only diff this review persists
        # anything about. The pre-lock estimate sized the lock and nothing else,
        # and a long wait can change the worktree, so if this plan needs MORE
        # batches the lock's published budget is raised to match. It is never
        # lowered: the only thing riding on that number is a peer not reclaiming
        # a lock whose holder is still running.
        plan = batch_plan(diff.data, d)
        planned = 0 if plan is None else len(plan)
        # Republished UNCONDITIONALLY from the authoritative plan, and
        # `_grow_budget` is what makes that safe: it never lowers the value. Not
        # conditional on the estimate having been wrong, because the sidecar
        # answers "how long may this holder legitimately need", which is a fact
        # about the plan alone — while `stale` (the value acquisition published)
        # can also be an operator's `SKODUN_LOCK_STALE_SECONDS`, i.e. a statement
        # about how long a WAITER will wait. Two runs with the same plan must
        # publish the same budget whether or not their pre-lock estimate happened
        # to guess it.
        needs = float(budget.lock_stale_ceiling(d, width, planned))
        if _grow_lock_budget(lock, needs) and planned > estimate:
            _note(f"this diff needs {planned} batch(es); the lock's published "
                  f"budget is now {int(needs)}s")

        common = dict(
            id=rid, reviewed_at=_iso_now(), source="skodun",
            branch=branch, head=head, base_ref=base.ref, base_sha=base.sha,
            diff_hash=diff_hash, mode=mode, model=finder.model,
            adapter=adapter.name, timeout_seconds=d.timeout_sec,
            max_turns=d.max_turns,
            # The budget for THIS review's own shape, persisted on the record so
            # `recover_stale` never has to recompute it from a config that may
            # since have changed — and never sweeps a live multi-batch run at
            # the single-review ceiling.
            worst_runtime_sec=budget.worst_runtime(d, width, planned),
        )

        if diff.data.rstrip(b"\n") == b"":
            # ORACLE PARITY: `--now` with nothing outgoing prints a clean
            # verdict rather than spending a model call on an empty diff. It is
            # recorded so the run leaves a trace, and it certifies nothing the
            # gate does not already grant: the gate PASSes an empty change
            # before it ever looks a review up.
            _note("no outgoing changes vs " + (base.ref or "HEAD^"))
            rec = dict(common, status="clean", parse_ok=True, degraded=False,
                       degraded_reason="", stop_reason=None,
                       diff_truncated=False, context_hash="",
                       files_changed=[], diff_bytes=0, prompt_bytes=0,
                       checklist_sections=[], checklist_bytes=0,
                       checklist_note="", checklist_degraded=False,
                       context_bytes=0, context_files=[],
                       context_omitted_files=[], attempts=[],
                       summary="no outgoing changes", findings=[],
                       findings_total=0,
                       severity={"high": 0, "medium": 0, "low": 0},
                       rule_ids=[], extra_passes={}, failure_reason="")
            stored = _persist(store, rec)
            finalized = True
            _emit_banner(stored)
            return stored

        # --- 5. the security hold, decided BEFORE anything is persisted ---
        hold_for_security = passes.should_run_security(
            mode, diff.files, d.security_path_segments,
            d.security_basename_patterns)

        if plan is not None:
            # ORACLE's own stderr line for this branch: an oversized diff is
            # reviewed in pieces rather than truncated, and the operator is told
            # how many pieces and whether a cross-file pass follows.
            _note(f"diff is {len(diff.data)} bytes (> {d.max_diff_bytes}); "
                  f"reviewing {len(diff.files)} file(s) in {planned} "
                  f"deterministic batch(es) + "
                  f"{int(passes.should_run_integration(planned))} "
                  f"integration pass")

        if plan is not None and not plan:
            # ORACLE: "diff batching produced no batches" is a recorded terminal
            # failure. `batching.split` returns nothing only for an EMPTY diff
            # (handled above), so this is the shape that should not happen — and
            # it is recorded as a failure precisely because the alternative is a
            # clean verdict for a diff nothing looked at.
            _note("diff batching produced no batches; failing closed")
            rec = dict(common, status="failed", parse_ok=False,
                       degraded=False, degraded_reason="", stop_reason=None,
                       diff_truncated=False, context_hash="",
                       files_changed=list(diff.files),
                       diff_bytes=len(diff.data), prompt_bytes=0,
                       checklist_sections=[], checklist_bytes=0,
                       checklist_note="", checklist_degraded=False,
                       context_bytes=0, context_files=[],
                       context_omitted_files=[], attempts=[],
                       summary="", findings=[], findings_total=0,
                       severity={"high": 0, "medium": 0, "low": 0},
                       rule_ids=[], extra_passes={},
                       batched=True, batch_count=0, batches=[],
                       usable_output=False,
                       failure_reason="diff batching produced no batches")
            stored = _persist(store, rec)
            finalized = True
            _emit_banner(stored)
            return stored

        # --- 6/7. the primary review: ONE prompt, or a batch orchestration.
        # Both branches leave a persisted `running` record and a `rec` whose
        # trust axes and findings are the primary result; everything after them
        # — the snapshot, the extra passes, the finalize and the banner — is
        # shared, so a batched aggregate cannot drift from an unbatched review on
        # any of it.
        with tempfile.TemporaryDirectory(prefix="skodun-") as tmp:
            scratch = Path(tmp)
            if plan is not None:
                # --- 6a. BATCHED: N sub-reviews + one cross-file pass ------
                rec = dict(
                    common, status="running", parse_ok=False, degraded=False,
                    degraded_reason="", stop_reason=None, diff_truncated=False,
                    context_hash="", files_changed=list(diff.files),
                    diff_bytes=len(diff.data), prompt_bytes=0,
                    checklist_sections=[], checklist_bytes=0,
                    checklist_note="", checklist_degraded=False,
                    context_bytes=0, context_files=[],
                    context_omitted_files=[], attempts=[], summary="",
                    findings=[], findings_total=0,
                    severity={"high": 0, "medium": 0, "low": 0}, rule_ids=[],
                    extra_passes={}, failure_reason="", batched=True,
                    batch_count=len(plan), batches=[], usable_output=False,
                )
                # Persisted `running` BEFORE the first model call and already
                # carrying its BATCHED `worst_runtime_sec`: that is the only
                # moment at which `recover_stale` can learn not to sweep this row
                # at the single-review ceiling.
                _save(store, rec)
                persisted = True
                _note(f"reviewing {len(diff.files)} file(s) vs {base.ref} as "
                      f"{rid} in {len(plan)} batch(es) ...")
                rec = _orchestrate(
                    rec, diff, batches=plan, cfg=cfg, d=d, root=root,
                    store=store, scratch=scratch, finder=finder, branch=branch,
                    base_ref=base.ref, base_sha=base.sha,
                    head_label=f"{head} (working tree)")
                answering_provider = _answering_provider(rec, finder)
            else:
                # --- 6b. UNBATCHED: checklist -> context pack -> prompt ----
                selection = checklist.select(
                    diff.files, "full", _under(root, d.checklist_dir),
                    _under(root, d.rules_json), d.checklist_map,
                    d.test_path_patterns)
                if selection.note:
                    # `degraded` (not `bool(note)`) separates the two
                    # severities: an empty selection is a total failure, a full
                    # one with a note means only the cross-file registry was
                    # unavailable.
                    kind = ("cross-file rules unavailable" if selection.degraded
                            else "path-scoped rules dropped")
                    _note(f"checklist: {kind} -- {selection.note}")
                # Budget eviction is otherwise SILENT: `select` quietly drops
                # the least-valuable sections until the injection budget is met,
                # so a review can run with rules the operator believes are in
                # the prompt and nothing anywhere says otherwise.
                # `dropped`/`over_budget` are the two fields that know, and this
                # is where they are read.
                if selection.dropped:
                    _note(f"checklist: dropped {', '.join(selection.dropped)} "
                          f"to fit the {checklist.BUDGET}-byte injection budget")
                if selection.over_budget:
                    _note(f"checklist: {selection.bytes_total} bytes still "
                          f"exceeds the {checklist.BUDGET}-byte budget after "
                          f"eviction; only undroppable sections remain")

                pack = None
                pack_body = None
                if d.context_pack:
                    headroom = promptbuild.context_headroom(
                        d.max_diff_bytes, len(diff.data), packing=True)
                    # `pack_large_added=False`: this is the SINGLE-SHOT path, so
                    # the diff already carries every added file whole. Packing a
                    # large one again would spend headroom saying the same thing
                    # twice -- and since selection is size-descending it would be
                    # packed FIRST, crowding out the modified files whose current
                    # contents only the packer can show. (When the diff is
                    # truncated the copy in the prompt is incomplete, but a
                    # truncated diff is never trustworthy anyway, so no trust
                    # decision rides on that case.)
                    pack = contextpack.pack(root, diff.files, diff.statuses,
                                            headroom, pack_large_added=False)
                    pack_body = pack.body

                prompt = promptbuild.build(
                    branch, base.ref, base.sha, f"{head} (working tree)",
                    diff.data, d.max_diff_bytes, selection, pack_body)
                if prompt.diff_truncated:
                    # Reachable only for a diff that is over the envelope and
                    # was NOT batched, i.e. one this build refused to split.
                    _note(f"diff is {len(diff.data)} bytes "
                          f"(> {d.max_diff_bytes}); the prompt is truncated and "
                          f"this review cannot be trustworthy")

                rec = dict(
                    common,
                    context_hash=pack.sha256 if pack is not None else "",
                    status="running",
                    parse_ok=False, degraded=False, degraded_reason="",
                    stop_reason=None, diff_truncated=prompt.diff_truncated,
                    files_changed=list(diff.files), diff_bytes=len(diff.data),
                    prompt_bytes=prompt.prompt_bytes,
                    checklist_sections=list(selection.sections),
                    checklist_bytes=selection.bytes_total,
                    checklist_note=selection.note,
                    # READS BACKWARDS UNLESS YOU KNOW `Selection`'s two
                    # severities, so: `checklist_degraded` is TRUE for a PARTIAL
                    # degradation (sections were selected, but something they
                    # depend on -- currently the cross-file rules registry --
                    # was unavailable) and FALSE for a TOTAL selection failure
                    # (nothing was selected at all, including the ordinary "this
                    # repo has no checklist directory" case). It is not a
                    # severity dial: `checklist_note` carries the reason, this
                    # field says only which of the two shapes produced it, and
                    # `checklist_sections` distinguishes them on its own (empty
                    # for a total failure). Nothing about the review's trust
                    # rides on it -- checklist selection is fail-soft by design.
                    checklist_degraded=selection.degraded,
                    context_bytes=pack.bytes_total if pack is not None else 0,
                    context_files=list(pack.included) if pack is not None else [],
                    context_omitted_files=[f"{p} ({r})" for p, r in pack.omitted]
                                          if pack is not None else [],
                    attempts=[], summary="", findings=[], findings_total=0,
                    severity={"high": 0, "medium": 0, "low": 0}, rule_ids=[],
                    extra_passes={}, failure_reason="",
                )

                # --- 7. persist `running`, then run the finder ------------
                _save(store, rec)
                persisted = True
                _note(f"reviewing {len(diff.files)} file(s) vs {base.ref} as "
                      f"{rid} ...")
                outcome = _run_chain(finder, cfg, d, prompt.text, root, store,
                                     scratch, "primary")
                rec["attempts"] = outcome.attempts
                _apply(rec, outcome)
                # Whoever ACTUALLY answered, not whoever was asked: after a
                # fallback the finder's own entry may never have run, and "did a
                # second provider look at this?" is a question about the
                # answering provider.
                answering_provider = (outcome.accepted["provider"]
                                      if outcome.accepted is not None
                                      else finder.provider)

            # --- 8. THE FINDER SNAPSHOT, taken before any merge -----------
            # The refuter's eligibility, its prompt, and the meaning of every
            # verdict index are all fixed HERE, on what the primary review
            # itself produced — never on the merged record. Three things ride on
            # that, and each of them is a real failure the merged record would
            # cause: a security finding must not trigger a refuter the finder
            # did not earn; a security demotion must not suppress one the
            # finder did earn; and a verdict's `index` must mean the finder's
            # own numbering, which stays `0..n-1` in the merged list only
            # because extra-pass merges APPEND.
            #
            # For a BATCHED run the "finder" is the aggregate: the extra passes
            # are opinions about the whole change, so their eligibility is
            # judged on what the whole change's review concluded — exactly the
            # axes and findings this record now carries.
            finder_trustworthy = is_trustworthy(
                rec["parse_ok"], rec["degraded"], rec["diff_truncated"])
            finder_findings = list(rec["findings"])
            finder_findings_total = rec["findings_total"]
            finder_provider = answering_provider

            # --- 9. the extra passes, still under the lock ----------------
            if hold_for_security:
                rec = _extra_pass(
                    rec, "security",
                    lambda: passes.security_prompt(
                        branch, base.ref, base.sha, f"{head} (working tree)",
                        diff.data, d.max_diff_bytes, d.security_prompt_slots),
                    _pass_reviewer(cfg, "security", finder), cfg, d, root,
                    store, scratch)

            if passes.should_run_skeptic(
                    mode,
                    is_trustworthy(rec["parse_ok"], rec["degraded"],
                                   rec["diff_truncated"]),
                    rec["findings_total"]):
                rec = _extra_pass(
                    rec, "skeptic",
                    lambda: passes.skeptic_prompt(
                        branch, base.ref, base.sha, f"{head} (working tree)",
                        diff.data, d.max_diff_bytes),
                    _pass_reviewer(cfg, "skeptic", finder), cfg, d, root,
                    store, scratch)

            # --- 10. the refuter: a DIFFERENT provider re-examines the
            # finder's findings. It EXECUTES last, so the published record is
            # complete in one write and the banner is printed once — but every
            # input it uses is the snapshot above, not the record it is about
            # to annotate. No fail-closed hold: a refuter that could not answer
            # is an absent annotation, never a demotion.
            run_refuter, skip_note = passes.refuter_decision(
                mode, finder_trustworthy, finder_findings_total, cfg)
            if run_refuter:
                rec = _refuter_pass(
                    rec, finder_findings_total,
                    lambda: passes.refuter_prompt(
                        finder_findings, diff.data, branch, base.ref, base.sha,
                        f"{head} (working tree)", d.max_diff_bytes),
                    _pass_reviewer(cfg, "refuter", finder), cfg, d, root,
                    store, scratch, finder_provider)
            elif skip_note:
                # `skip_note` is non-empty for exactly one case: an eligible
                # review with no refuter configured (`refuter_decision`'s own
                # contract). The brief calls that "silently skipped with a
                # note" — the note belongs on the record
                # (`extra_passes.refuter.status == "skipped"`), not on
                # stderr, so it stays off `_note` here. A genuine refuter
                # FAILURE — configured but unavailable, degraded, or
                # unparseable — is a different path (`_refuter_pass`) and IS
                # narrated there: that is an event the operator wants to
                # know about, not a no-op default configuration.
                rec = passes.skipped_refuter_pass(rec, skip_note)

        # --- 11. persist the final record, then banner from what was stored
        rec["trustworthy"] = is_trustworthy(
            rec["parse_ok"], rec["degraded"], rec["diff_truncated"])
        rec["status"] = _status_for(rec)
        stored = _persist(store, rec)
        finalized = True
        _emit_banner(stored)
        return stored
    finally:
        # --- 12. never leave a `running` record or a held lock behind -----
        if persisted and not finalized:
            try:
                store.set_status(rid, "failed")
            except Exception:
                pass   # the crash that got us here is the story, not this
        _release_fg_lock(lock)


def _under(root: Path, relative: str) -> Path:
    p = Path(relative)
    return p if p.is_absolute() else root / p


def _save(store: Store, rec: dict) -> None:
    try:
        store.save_review(rec)
    except Exception as e:
        raise PersistenceFailed(f"could not record the review: {e!r}") from e


def _persist(store: Store, rec: dict) -> dict:
    """Save the record and return it as READ BACK OUT of the store.

    Read back rather than returned from memory for two reasons:
    `Store.save_review` computes `trustworthy` itself, and the banner must be
    rendered from exactly what the gate will later see. A record that cannot be
    read back was not recorded, whatever the write said.
    """
    _save(store, rec)
    stored = store.get_review(rec["id"])
    if stored is None:
        raise PersistenceFailed(
            f"review {rec['id']} was not readable back out of the store")
    return stored


def _emit_banner(stored: dict) -> None:
    """Print the verdict banner. Called only AFTER the record is final.

    Order matters: `run_review` marks the run finalized between the save and
    this call, so a stdout failure here (a broken pipe from
    `skodun review | head`) can never make the `finally` block downgrade a
    review that was already persisted correctly.
    """
    print(banner(stored))
    sys.stdout.flush()


def _apply(rec: dict, outcome: _Outcome) -> None:
    """Fold one reviewer outcome into the primary record, in place."""
    if outcome.accepted is not None:
        # The indexed columns must name whoever ACTUALLY answered, not whoever
        # was asked first: after a fallback the record is initialised with the
        # head's identity and the payload came from somewhere else entirely.
        # The `adapter` column takes the adapter NAME (`"codex"`); the provider
        # id (`"openai"`) lives in `attempts[]`, which carries the whole chain.
        rec["adapter"] = outcome.accepted["adapter_name"]
        rec["model"] = outcome.accepted["model"]
    if outcome.parsed is None:
        rec["parse_ok"] = False
        rec["failure_reason"] = outcome.failure_reason
        return
    p = outcome.parsed
    findings = list(p.findings)
    rec.update(
        parse_ok=p.parse_ok,
        degraded=p.degraded,
        degraded_reason=p.degraded_reason,
        stop_reason=p.stop_reason,
        summary=p.summary,
        findings=findings,
        findings_total=len(findings),
        severity=_severity_counts(findings),
        rule_ids=_rule_ids(findings),
    )
    if not p.parse_ok:
        rec["failure_reason"] = "the reviewer produced no parseable review"


# ---------------------------------------------------------------------------
# batched review: the orchestrator
# ---------------------------------------------------------------------------
#
# A diff bigger than one prompt's envelope used to be reviewed truncated, i.e.
# `diff_truncated` and therefore never trustworthy: unreviewable, and so
# ungateable. The orchestrator reviews it in deterministic size-bounded pieces
# instead, adds ONE cross-file pass over the seams it cut, and records the whole
# thing as a SINGLE artifact at the FULL diff's identity — so dedup, the gate
# and the fallback contract behave exactly as they do for a single-shot review.
#
# Three rules hold the design together:
#
#   * **Small diffs never come here.** The unbatched path is untouched, not
#     "equivalent"; `test_a_small_diff_never_enters_the_orchestrator` pins that.
#   * **A sub-review is a review, minus everything that publishes.** Each batch
#     runs the finder's whole chain (full retry/fallback budget) and produces no
#     record, no index row, no banner and no delivery. Only the aggregate does.
#   * **Aggregation demotes.** `parse_ok` is ALL of them, `degraded` and
#     `diff_truncated` are ANY of them, and the integration pass participates in
#     each — a partial pass must never read as a full all-clear.


def _batch_budget(d: Defaults) -> int:
    """The per-batch DIFF budget an over-budget diff is split on.

    HALF the envelope when context packing is on, which is the oracle's own
    `_batch_diff_budget=$((GROK_BATCH_BYTES / 2))` (`GROK_BATCH_BYTES` defaults
    to `MAX_DIFF_BYTES`). The reason is arithmetic rather than taste: a batch
    filled to the whole envelope leaves `context_headroom` exactly zero, so
    batched context packing would be a silent no-op on precisely the reviews
    that need context most — each batch shows a slice of the change and the
    packer is the only thing that can show the rest of the file.

    Clamped to at least 1, exactly as `batching.split` clamps its own budget: a
    computed budget can arrive at zero, and splitting maximally with every unit
    flagged as an irreducible floor says strictly more than refusing to split.
    """
    envelope = d.max_diff_bytes
    if d.context_pack:
        envelope //= 2
    return max(1, envelope)


def batch_plan(diff: bytes, d: Defaults) -> list[batching.Batch] | None:
    """The batch plan for `diff`, or None when it fits one prompt.

    None is "this review is not batched at all", and it is the answer for every
    diff up to and including the envelope — the shipped single-shot path. The
    threshold is the oracle's (`REVIEW_DIFF_BYTES -gt MAX_DIFF_BYTES`).

    An EMPTY list is a real answer and a terminal failure ("diff batching
    produced no batches"), never "nothing to review": `batching.split` returns
    no batches only for empty input, and an empty batch would send an empty
    prompt and risk minting a clean verdict for a diff nothing looked at.
    """
    if len(diff) <= d.max_diff_bytes:
        return None
    return batching.split(diff, _batch_budget(d))


def _estimate_batch_count(repo: Path, d: Defaults) -> int:
    """A PRE-LOCK guess at how many batches this review will need.

    STAGE ONE of the two-stage ordering, and an ESTIMATE by construction: it is
    taken before the lock is held, the wait for that lock can be long, and the
    worktree can change in the meantime. It sizes the lock's stale ceiling — a
    batched review holds the lock for `batch_count + 1` reviewer budgets, so a
    ceiling sized for one would invite a peer to reclaim a live holder — and
    NOTHING else. Everything the review persists comes from the authoritative
    capture under the lock, which also re-derives the ceiling from the larger of
    the two plans.

    Best-effort: a capture that fails here returns 0, i.e. the unbatched
    ceiling. The same capture happens again under the lock, where a genuine
    failure surfaces as the exception it is, with the lock held and released
    properly — this call must not be the thing that turns a broken repo into a
    different error at a different stage.
    """
    try:
        base = gitio.resolve_base(repo)
        diff = gitio.capture_diff(repo, base.sha, d.untracked_max)
        plan = batch_plan(diff.data, d)
    except Exception as e:
        _note(f"could not size the lock budget before taking the lock: {e!r}")
        return 0
    return 0 if plan is None else len(plan)


def _grow_lock_budget(lock: Lock, seconds: float) -> bool:
    """Raise our lock's published budget, iff the lock is still OURS.

    The ABA guard `_release_fg_lock` uses, for the same reason: our stale window
    may have expired while we were capturing, a peer may have legitimately
    reclaimed the lock, and writing our budget into ITS lock directory would
    extend a protection we have no business granting. Best-effort — failing to
    widen the sidecar costs at worst an early reclaim, and must never be the
    thing that fails a review.
    """
    try:
        if _owner_pid(lock.path) != lock.pid:
            return False
        return _grow_budget(lock.path, seconds)
    except OSError:
        return False


def _answering_provider(rec: dict, finder: Reviewer) -> str:
    """Which provider actually answered a batched aggregate's sub-reviews.

    The refuter is only worth its call when a DIFFERENT provider re-examines the
    findings, so that question has to be asked about whoever answered rather than
    whoever was configured. For an aggregate there can be several answers: a
    chain that fell through to a fallback on one batch is a designed, recorded
    outcome. When they do not agree there is no single answering provider, and
    the configured finder is the honest fallback — it keeps the comparison
    conservative (the refuter is more likely to be flagged as same-provider, and
    a flag is a note, never a demotion).
    """
    seen = {b.get("provider") for b in (rec.get("batches") or ())
            if isinstance(b.get("provider"), str) and b.get("provider")}
    return seen.pop() if len(seen) == 1 else finder.provider


def usable_output(batches: list | tuple | None,
                  integration: dict | None = None) -> bool:
    """Whether ANY pass in this review produced a parse-ok answer.

    NORMATIVE for aggregates, and deliberately not derivable from the finding
    count. A batched round whose batches all answered "nothing wrong" and whose
    cross-file pass then failed is untrustworthy AND has zero findings — but it
    is emphatically not a round that said nothing, and a surface that judged by
    `findings_total` would print "NO REVIEW HAPPENED" over three real reviews
    and hide the partial evidence they produced.

    `parse_ok is True`, not truthiness: a sub-review that merely omits the key
    has not told us it parsed.
    """
    if any(b.get("parse_ok") is True for b in (batches or ())):
        return True
    return integration is not None and integration.get("parse_ok") is True


@dataclass(frozen=True)
class _Sub:
    """What ONE sub-review (a batch, or the integration pass) produced.

    A normalised, total shape: every field is present on every path, including
    the paths where no process ever started. The aggregation step below reads
    only this, so it cannot accidentally treat "the key is missing" as "the
    answer was fine".
    """

    parse_ok: bool
    degraded: bool
    degraded_reason: str
    stop_reason: object
    diff_truncated: bool
    summary: str
    findings: list
    failure_reason: str
    attempts: list
    provenance: dict
    accepted: dict | None


def _run_sub(reviewer: Reviewer, cfg: Config, d: Defaults, prompt, root: Path,
             store: Store, scratch: Path, tag: str, label: str) -> _Sub:
    """Run one sub-review chain and normalise the outcome into a `_Sub`.

    Anything the chain raises DEMOTES the aggregate rather than destroying it,
    for `_extra_pass`'s reason: the other sub-reviews already ran and are worth
    persisting as an untrustworthy record the gate can refuse, which is strictly
    more useful than an exception that leaves a `failed` stub behind.

    `diff_truncated` comes from the PROMPT, never from the splitter's own floor
    flag. They are different facts (ORACLE, `grok-prepush-review.sh:3425-3429`):
    the splitter flags a unit that exceeded the per-batch budget, but the batch
    still carries it whole, and the prompt cap is twice that budget — so such a
    batch is usually shown to the model in full. Only a cut PROMPT is a coverage
    hole, and only a coverage hole may demote.
    """
    trunc = bool(prompt.diff_truncated)
    try:
        outcome = _run_chain(reviewer, cfg, d, prompt.text, root, store,
                             scratch, tag)
    except Exception as e:
        _note(f"{label} failed; the aggregate cannot be trustworthy: {e!r}")
        reason = f"{label} failed: {e!r}"
        return _Sub(False, False, "", None, trunc, "", [], reason, [],
                    {"provider": None, "model": None, "effort": None,
                     "note": reason}, None)
    p = outcome.parsed
    if p is None:
        return _Sub(False, False, "", None, trunc, "", [],
                    outcome.failure_reason or f"{label} produced no review",
                    outcome.attempts, _provenance(outcome), outcome.accepted)
    return _Sub(
        parse_ok=p.parse_ok is True,
        degraded=p.degraded is True,
        degraded_reason=str(p.degraded_reason or ""),
        stop_reason=p.stop_reason,
        diff_truncated=trunc,
        summary=str(p.summary or ""),
        findings=passes._as_findings(list(p.findings)),
        failure_reason=("" if p.parse_ok is True
                        else f"{label} produced no parseable review"),
        attempts=outcome.attempts,
        provenance=_provenance(outcome),
        accepted=outcome.accepted,
    )


def _batch_labels(branch: str, head_label: str, index: int, count: int,
                  files: list) -> tuple[str, str]:
    """The branch/head labels one batch's prompt carries.

    For a real split, the oracle's own two labels, which tell the reviewer it is
    holding a slice and which slice it is (`grok-prepush-review.sh:3452-3453`).

    For the SOLE batch, the UNBATCHED labels — the same extension of the
    oracle's own sole-batch rule that makes `passes.batch_checklist_mode(1)`
    return `full`: with one batch there is no integration pass and that batch IS
    the whole diff, so it is prompted exactly as the whole diff, byte for byte
    (pinned by `test_the_sole_batch_prompt_is_byte_identical_to_the_unbatched_prompt`).
    Anything else would make a one-batch run review LESS than the same diff
    reviewed unbatched, and label it as a fragment it is not. DIVERGENCE from
    the oracle, which decorates unconditionally and therefore says
    "batch 1/1".
    """
    if count == 1:
        return branch, head_label
    return (f"{branch} (batch {index}/{count})",
            f"{head_label} -- batch {index} of {count}; "
            f"files: {', '.join(files)}")


def _orchestrate(rec: dict, diff, *, batches: list, cfg: Config, d: Defaults,
                 root: Path, store: Store, scratch: Path, finder: Reviewer,
                 branch: str, base_ref: str, base_sha: str, head_label: str,
                 tag: str = "primary") -> dict:
    """Review `batches` as sub-reviews plus one cross-file pass; AGGREGATE.

    Returns a new record: `rec` with every aggregate field filled in. `rec` is
    not mutated and nothing this builds is shared with it.

    `batches` is keyword-only and is the whole test seam: passing the WHOLE diff
    as a single batch is how the one-batch prompt is compared against the
    unbatched builder's bytes. Production always passes `batch_plan`'s answer.

    Everything persisted here describes the FULL diff: the identity fields are
    the caller's, the trust axes are aggregated across every sub-review, and
    `batches[]` carries the per-batch provenance (files, bytes, attempts, trust
    axes, checklist selection) that makes the merged findings attributable —
    they are merged in batch order, then the integration pass's, and only the
    latter are tagged.
    """
    count = len(batches)
    mode = passes.batch_checklist_mode(count)
    sole = count == 1

    metas: list[dict] = []
    subs: list[_Sub] = []
    findings: list[dict] = []
    prompt_bytes = 0
    ctx_bytes = 0
    ctx_files: list[str] = []
    ctx_omitted: list[str] = []
    sections: list[str] = []
    checklist_bytes = 0
    checklist_notes: list[str] = []
    checklist_degraded = False

    def _fold_checklist(selection) -> None:
        """Fold one prompt's selection into the aggregate's checklist telemetry.

        `sections` is the UNION (which rules this review carried anywhere) and
        `bytes_total` is the SUM (what they actually cost across N+1 prompts, so
        a section injected into every batch is counted every time — that is the
        real number, and the per-prompt detail is in `batches[].checklist`).
        """
        nonlocal checklist_bytes, checklist_degraded
        for name in selection.sections:
            if name not in sections:
                sections.append(name)
        checklist_bytes += selection.bytes_total
        if selection.note and selection.note not in checklist_notes:
            checklist_notes.append(selection.note)
        checklist_degraded = checklist_degraded or selection.degraded is True

    for index, batch in enumerate(batches, 1):
        # Per batch: never a cross-file rule (`mode`), and context for exactly
        # the files this batch shows. A file split across two batches has its
        # context packed into both -- accepted, because the batches are reviewed
        # independently and each one has to stand on its own.
        selection = checklist.select(
            batch.files, mode, _under(root, d.checklist_dir),
            _under(root, d.rules_json), d.checklist_map, d.test_path_patterns)
        _fold_checklist(selection)

        pack = None
        if d.context_pack:
            headroom = promptbuild.context_headroom(
                d.max_diff_bytes, len(batch.data), packing=True)
            # `pack_large_added` is True for a real split (a batch may hold only
            # part of a large added file, so packing it whole adds what the
            # slice cannot show) and False for the sole batch, which already
            # carries every added file whole -- the unbatched rule, because the
            # sole batch IS the unbatched diff.
            pack = contextpack.pack(
                root, list(batch.files),
                {f: diff.statuses[f] for f in batch.files
                 if f in diff.statuses},
                headroom, pack_large_added=not sole)
            ctx_bytes += pack.bytes_total
            for name in pack.included:
                if name not in ctx_files:
                    ctx_files.append(name)
            for path, reason in pack.omitted:
                entry = f"{path} ({reason})"
                if entry not in ctx_omitted:
                    ctx_omitted.append(entry)

        b_branch, b_head = _batch_labels(branch, head_label, index, count,
                                         list(batch.files))
        prompt = promptbuild.build(b_branch, base_ref, base_sha, b_head,
                                   batch.data, d.max_diff_bytes, selection,
                                   pack.body if pack is not None else None)
        prompt_bytes += prompt.prompt_bytes
        _note(f"batch {index}/{count} ({len(batch.files)} file(s), "
              f"{len(batch.data)} bytes) ...")
        if prompt.diff_truncated:
            _note(f"batch {index}/{count} is {len(batch.data)} bytes "
                  f"(> {d.max_diff_bytes}); its prompt is truncated and this "
                  f"review cannot be trustworthy")
        sub = _run_sub(finder, cfg, d, prompt, root, store, scratch,
                       f"{tag}.b{index}", f"batch {index}")
        subs.append(sub)
        findings.extend(sub.findings)
        metas.append({
            # Provenance FIRST so the explicit fields below always win: which
            # provider answered is `_provenance`'s vocabulary to widen, and a new
            # key there must never be able to overwrite a trust axis here.
            **sub.provenance,
            "index": index,
            "id": f"{rec.get('id', '')}.b{index}",
            "files": list(batch.files),
            "diff_bytes": len(batch.data),
            # The splitter's irreducible-floor flag, recorded beside -- never
            # merged into -- the prompt truncation the trust axes read.
            "splitter_truncated": batch.truncated is True,
            "parse_ok": sub.parse_ok,
            "degraded": sub.degraded,
            "degraded_reason": sub.degraded_reason,
            "diff_truncated": sub.diff_truncated,
            "stop_reason": sub.stop_reason,
            "summary": sub.summary,
            "findings_total": len(sub.findings),
            "failure_reason": sub.failure_reason,
            "prompt_bytes": prompt.prompt_bytes,
            "checklist": passes.checklist_meta(mode, selection),
            "attempts": sub.attempts,
        })

    # --- the cross-file pass over the seams the split just cut --------------
    integration: dict | None = None
    integration_sub: _Sub | None = None
    if passes.should_run_integration(count):
        selection = checklist.select(
            diff.files, passes.INTEGRATION_CHECKLIST_MODE,
            _under(root, d.checklist_dir), _under(root, d.rules_json),
            d.checklist_map, d.test_path_patterns)
        _fold_checklist(selection)
        meta_checklist = passes.checklist_meta(
            passes.INTEGRATION_CHECKLIST_MODE, selection)
        reviewer = _pass_reviewer(cfg, passes.INTEGRATION_PASS, finder)
        _note(f"integration pass over {count} batch seams ...")
        try:
            prompt = passes.integration_prompt(
                [passes.BatchSummary(files=list(b.files), diff=b.data,
                                     summary=s.summary, findings=s.findings)
                 for b, s in zip(batches, subs)],
                selection, d.max_diff_bytes)
        except Exception as e:
            # ORACLE: "integration context build produced no prompt" is a FAILED
            # pass, not an absent one -- the aggregate is demoted rather than
            # clearing with its cross-file coverage silently missing.
            _note(f"integration prompt build failed; demoting the aggregate: {e!r}")
            reason = f"the integration pass could not be prepared: {e!r}"
            integration_sub = _Sub(False, False, "", None, False, "", [],
                                   reason, [], {"provider": None, "model": None,
                                                "effort": None, "note": reason},
                                   None)
            integration = passes.integration_meta(
                "failed", ran=False, checklist=meta_checklist, note=reason,
                provenance=integration_sub.provenance)
        else:
            prompt_bytes += prompt.prompt_bytes
            if prompt.diff_truncated:
                _note(f"NOTE integration context capped at {d.max_diff_bytes} "
                      f"bytes; some cross-file relationships were not shown")
            integration_sub = _run_sub(
                reviewer, cfg, d, prompt, root, store, scratch,
                passes.INTEGRATION_PASS, "the integration pass")
            # Tagged BEFORE they are merged: nothing downstream can tell a
            # cross-file finding from a within-batch one afterwards.
            tagged = passes.tag_integration_findings(integration_sub.findings)
            findings.extend(tagged)
            status = ("ran" if integration_sub.parse_ok
                      and not integration_sub.degraded
                      else "degraded" if integration_sub.parse_ok else "failed")
            integration = passes.integration_meta(
                status, ran=True, parse_ok=integration_sub.parse_ok,
                degraded=integration_sub.degraded,
                diff_truncated=integration_sub.diff_truncated,
                findings_total=len(tagged), attempts=integration_sub.attempts,
                provenance=integration_sub.provenance,
                checklist=meta_checklist,
                note=integration_sub.failure_reason)

    # --- aggregate ---------------------------------------------------------
    # ORACLE (`grok-prepush-review.sh:3703-3820`): parse_ok is ALL, degraded and
    # diff_truncated are ANY, and the integration pass is one of the terms in
    # each. With exactly one batch there is no integration pass and its terms
    # are NEUTRAL -- `integration{}` is then absent from the artifact entirely
    # (readers tolerate absence; a "skipped" status would be a second, weaker
    # way of saying the same thing).
    everyone = subs + ([integration_sub] if integration_sub is not None else [])
    parse_ok = all(s.parse_ok for s in everyone) and bool(everyone)
    degraded = any(s.degraded for s in everyone)
    truncated = any(s.diff_truncated for s in everyone)

    unreviewed = [str(m["index"]) for m in metas if not m["parse_ok"]]
    if integration_sub is not None and not integration_sub.parse_ok:
        unreviewed.append(passes.INTEGRATION_PASS)
    reasons = [s.failure_reason for s in everyone if s.failure_reason]
    failure_reason = ""
    if unreviewed:
        failure_reason = ("one or more batches were not reviewed (%s)"
                          % ", ".join(unreviewed))
        if reasons:
            failure_reason += "; " + "; ".join(reasons)

    degraded_reasons = []
    for label, sub in [(f"batch {m['index']}", s) for m, s in zip(metas, subs)] \
            + ([("the integration pass", integration_sub)]
               if integration_sub is not None else []):
        if sub.degraded:
            degraded_reasons.append(
                f"{label}: {sub.degraded_reason or 'no reason given'}")

    out = dict(rec)
    out.update(
        batched=True,
        batch_count=count,
        batches=metas,
        parse_ok=parse_ok,
        degraded=degraded,
        degraded_reason="; ".join(degraded_reasons),
        diff_truncated=truncated,
        stop_reason=_aggregate_stop_reason(everyone),
        summary=_aggregate_summary(count, integration is not None,
                                   len(findings), unreviewed, degraded,
                                   truncated),
        findings=findings,
        findings_total=len(findings),
        severity=_severity_counts(findings),
        rule_ids=_rule_ids(findings),
        failure_reason=failure_reason,
        usable_output=usable_output(metas, integration),
        prompt_bytes=prompt_bytes,
        # `attempts` deliberately stays EMPTY on the aggregate: a flat list
        # across N+1 sub-reviews has duplicate ordinals and no way to say which
        # sub-review each row belongs to, so it would be telemetry that
        # misleads. Every row is in `batches[].attempts` / `integration.attempts`
        # instead, which is where it can be read against the pass that produced
        # it. (`_extra_pass` refuses to carry attempts it cannot record honestly
        # for the same reason.)
        attempts=[],
        checklist_sections=sections,
        checklist_bytes=checklist_bytes,
        checklist_note="; ".join(checklist_notes),
        checklist_degraded=checklist_degraded,
        context_bytes=ctx_bytes,
        context_files=ctx_files,
        context_omitted_files=ctx_omitted,
        # DELIBERATE: a batched aggregate is never dedup-suppressible. There is
        # no single canonical context pack behind it -- each batch packed its own
        # files -- so publishing one hash would certify context nothing was
        # reviewed against. The cost is a redundant re-review of a rare
        # oversized diff; the alternative risks certifying unpacked context.
        context_hash="",
    )
    if integration is not None:
        out["integration"] = integration
    # Whoever ACTUALLY answered, when every sub-review agrees. A chain that fell
    # through to a fallback on ONE batch is a designed, recorded outcome
    # (`batches[].provider`), so it does not demote -- see the module docstring's
    # divergence note -- but the aggregate's indexed columns then keep the
    # configured finder's identity rather than picking one batch's arbitrarily.
    accepted = {(s.accepted["adapter_name"], s.accepted["model"])
                for s in everyone if s.accepted is not None}
    if len(accepted) == 1:
        out["adapter"], out["model"] = accepted.pop()
    return out


def _aggregate_stop_reason(subs: list) -> object:
    """The FIRST abnormal `stop_reason` across the sub-reviews, in order.

    Batch order, then the integration pass. ORACLE, and its reasoning is the
    point: reporting the last one, or the most common one, would let a single
    truncated batch hide behind its healthy siblings — the exact false all-clear
    this field exists to expose. `EndTurn` only when every sub-review that
    reported one completed normally, and None when none reported at all (the
    shipped record's own "nothing to say" value).
    """
    reported = [s.stop_reason for s in subs
                if isinstance(s.stop_reason, str) and s.stop_reason]
    for value in reported:
        if value != "EndTurn":
            return value
    return "EndTurn" if reported else None


def _aggregate_summary(count: int, integration: bool, findings: int,
                       unreviewed: list, degraded: bool,
                       truncated: bool) -> str:
    """The oracle's own one-line summary of a batched run, in its own order."""
    pieces = ["batched review: %d batch(es)" % count]
    if integration:
        pieces.append("+ cross-file pass")
    pieces.append("%d finding(s)" % findings)
    if unreviewed:
        pieces.append("UNREVIEWED batch(es): %s" % ", ".join(unreviewed))
    if degraded:
        pieces.append("degraded")
    if truncated:
        pieces.append("truncated hunk(s)")
    return "; ".join(pieces)


def _with_provenance(rec: dict, name: str, provenance: dict) -> dict:
    """Copy `rec` with `provenance` folded into `extra_passes[name]`.

    Copy-on-write rather than a mutation: `passes._merge` shares an EARLIER
    pass's meta dict with the record it was handed, and writing through that
    reference would edit a dict this module does not own. (The dict for the
    pass just merged is freshly built, so this is belt-and-braces — but the
    invariant is `passes`' to state, not this module's to depend on.)
    """
    extras = dict(rec.get("extra_passes") or {})
    meta = dict(extras.get(name) or {})
    meta.update(provenance)
    extras[name] = meta
    out = dict(rec)
    out["extra_passes"] = extras
    return out


def _failed_pass(rec: dict, name: str, reason: str, note: str) -> dict:
    """Merge a pass that produced nothing, with explicit null provenance.

    Every no-process-start outcome ends here — a prompt that would not build,
    an exception out of the chain — and each one records `provider`/`model`/
    `effort` as explicit `None` plus a `note` saying why, so nothing reads as
    "the pass ran on the finder's model".
    """
    merged = passes.merge_failed_extra_pass(rec, name, reason)
    return _with_provenance(merged, name, {
        "provider": None, "model": None, "effort": None, "note": note})


def _extra_pass(rec: dict, name: str, build_prompt, reviewer: Reviewer,
                cfg: Config, d: Defaults, cwd: Path, store: Store,
                scratch: Path) -> dict:
    """Run one extra pass and merge it into `rec`, returning the new record.

    `reviewer` is the caller's choice, and the caller makes a DELIBERATE one:
    a configured, enabled reviewer whose role matches the pass (`security` for
    the security pass, `refuter` for the skeptic pass) is preferred, and the
    finder is the fallback. That is slightly more than Phase 1 promised — the
    brief says the extra passes reuse the finder's adapter — but a config that
    names a cheaper or differently-specialised model for a lens has said what it
    wants, and silently ignoring it would be the surprise. The cost of the wider
    behaviour is one more thing that can be misconfigured, which is why
    `run_review`'s preflight now resolves those reviewers' adapters too, before
    the lock and before any model call.

    Merge semantics are Task 14's, and the choice between them is made here:
    a pass that produced a record — even an unparseable one — goes through
    `merge_extra_pass`, and a pass that produced nothing at all (timed out,
    binary missing, prompt build failed) goes through
    `merge_failed_extra_pass`, which demands a reason and demotes. A pass
    `should_run_*` declined is never merged at all.

    `build_prompt` is a callable rather than a `Prompt` so that the prompt
    build sits INSIDE the guard below, exactly as it does in the oracle ("NOTE
    skeptic prompt build failed; demoting review"). Anything an extra pass
    raises — a prompt it could not render, a reviewer whose provider has no
    adapter — demotes the review rather than destroying it: the primary review
    already ran and is worth persisting as an untrustworthy record the gate
    can refuse, which is strictly more useful than an exception that leaves
    only a `failed` stub behind.
    """
    _note(f"{name} pass ...")
    try:
        prompt = build_prompt()
    except Exception as e:
        _note(f"{name} prompt build failed; demoting review: {e!r}")
        reason = f"extra pass {name} could not be prepared: {e!r}"
        return _failed_pass(rec, name, reason, reason)
    if prompt.diff_truncated:
        _note(f"NOTE {name} pass diff capped at {d.max_diff_bytes} bytes "
              f"(partial coverage, one-call bound)")
    try:
        outcome = _run_chain(reviewer, cfg, d, prompt.text, cwd, store,
                             scratch, name)
    except Exception as e:
        _note(f"{name} pass failed; demoting review: {e!r}")
        reason = f"extra pass {name} failed: {e!r}"
        return _failed_pass(rec, name, reason, reason)
    if outcome.parsed is None:
        return _with_provenance(
            passes.merge_failed_extra_pass(
                rec, name,
                outcome.failure_reason or passes.failed_pass_reason(name)),
            name, _provenance(outcome))
    p = outcome.parsed
    extra = {
        "id": f"{rec['id']}.{name}",
        "parse_ok": p.parse_ok,
        "degraded": p.degraded,
        "degraded_reason": p.degraded_reason,
        # From the Prompt, not the primary: this pass is one call against a
        # separately-capped copy of the diff, and `partial_coverage` hangs off
        # exactly that.
        "diff_truncated": prompt.diff_truncated,
        "stop_reason": p.stop_reason,
        "summary": p.summary,
        "findings": list(p.findings),
        "findings_total": len(p.findings),
        "failure_reason": ("" if p.parse_ok else passes.failed_pass_reason(name)),
        # NO `attempts` key: `passes._merge` builds the `extra_passes[<name>]`
        # meta dict from a fixed set of keys and never copies one, so an
        # `attempts` list here was built, handed over, and dropped on the floor.
        # A field that looks like telemetry and records nothing is worse than an
        # absent one — it invites a reader to trust a number that is not there.
        # Carrying it through means widening the meta schema `passes` owns; if
        # that is ever wanted, add it there and populate it from
        # `outcome.attempts` here.
    }
    # WHICH provider answered this pass is not in the meta schema `passes`
    # owns, and it cannot be inferred from the reviewer that was asked: a pass
    # with its own fallback chain may have been answered by any entry in it.
    return _with_provenance(passes.merge_extra_pass(rec, extra, name), name,
                            _provenance(outcome))


def _refuter_failed(rec: dict, finder_findings_total: int, note: str,
                    provenance: dict | None = None,
                    **kw) -> dict:
    """Record a refuter that produced nothing. It demotes NOTHING.

    Every no-verdicts outcome ends here — a prompt that would not build, an
    exception out of the chain, an exhausted chain, an unparseable answer — and
    each one is a note on an otherwise untouched review. That is the whole
    difference from `_failed_pass`, which is the security/skeptic path and
    clears `parse_ok` on the way past. Role semantics decide demotion, never
    provider identity: this is not a laxer copy of that function, it is the
    other rule.
    """
    prov = dict(provenance or {"provider": None, "model": None, "effort": None})
    if not str(prov.get("note") or "").strip():
        prov["note"] = note
    return passes.merge_refuter_pass(rec, None, prov, finder_findings_total,
                                     **kw)


def _refuter_pass(rec: dict, finder_findings_total: int, build_prompt,
                  reviewer: Reviewer, cfg: Config, d: Defaults, cwd: Path,
                  store: Store, scratch: Path,
                  finder_provider: str) -> dict:
    """Run the refuter pass and annotate `rec`, returning the new record.

    Structurally a sibling of `_extra_pass` and semantically its opposite in
    the one way that matters: NOTHING on any path through this function can
    make the review less trustworthy than the finder left it. The record is
    annotated, or it is annotated with the news that the refuter could not
    answer.

    `build_prompt` is a callable for the same reason it is one in
    `_extra_pass`: the prompt build sits inside the guard, so a prompt that
    will not render is a failed pass rather than an exception that destroys a
    review already in hand.

    The chain runs under `REFUTER_CONTRACT`, which is what makes every adapter
    request, classify and validate the verdicts shape — and what gives this
    pass Task 7's fallback support for free.
    """
    _note("refuter pass (annotation only) ...")
    try:
        prompt = build_prompt()
    except Exception as e:
        _note(f"refuter prompt build failed; the review keeps its verdict: {e!r}")
        return _refuter_failed(
            rec, finder_findings_total,
            f"the refuter prompt could not be prepared: {e!r}")

    notes: list[str] = []
    if prompt.diff_truncated:
        _note(f"NOTE refuter pass diff capped at {d.max_diff_bytes} bytes "
              f"(partial coverage, one-call bound)")
        notes.append("the refuter saw a size-capped diff (partial coverage)")

    try:
        outcome = _run_chain(reviewer, cfg, d, prompt.text, cwd, store, scratch,
                             "refuter", contract=REFUTER_CONTRACT)
    except Exception as e:
        _note(f"refuter pass failed; the review keeps its verdict: {e!r}")
        return _refuter_failed(rec, finder_findings_total,
                               f"the refuter pass failed: {e!r}",
                               partial_coverage=prompt.diff_truncated,
                               notes=notes)

    prov = _provenance(outcome)
    p = outcome.parsed
    if p is None or not p.parse_ok or not isinstance(p.payload, dict):
        _note("the refuter produced no usable verdicts; the review is "
              "unannotated and otherwise unchanged")
        return _refuter_failed(
            rec, finder_findings_total,
            outcome.failure_reason or "the refuter produced no usable verdicts",
            prov, partial_coverage=prompt.diff_truncated, notes=notes)

    if p.degraded:
        notes.append("the refuter run was degraded: %s"
                     % (p.degraded_reason or "no reason given"))
    merged = passes.merge_refuter_pass(
        rec, p.payload, prov, finder_findings_total, degraded=p.degraded,
        partial_coverage=prompt.diff_truncated, notes=notes)

    # The entire point of this pass is that a DIFFERENT provider looks at the
    # findings; a model asked to check its own work is agreeable about it. A
    # config may still put the refuter on the finder's provider — that is the
    # operator's call, and it is better than no re-examination — but the record
    # says so, because that is exactly what makes a verdict less worth
    # adopting. Compared on the answering providers, not the configured ones:
    # either side may have fallen through to a different entry.
    if prov.get("provider") is not None and prov["provider"] == finder_provider:
        merged = _with_provenance(merged, "refuter",
                                  {"same_provider_as_finder": True})
    return merged
