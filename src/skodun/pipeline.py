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

The stale ceiling and the wait cap default to `lock_stale_ceiling_sec` — the
worst-case runtime the config implies for the primary review *plus both extra
passes*, because those run inside this lock with their own retry budgets. (The
narrower `worst_runtime_sec` covers a single run and stays where the brief pins
it: `recover_stale`.) All three, plus the poll cadence, can be overridden with
`SKODUN_LOCK_WAIT_SECONDS` / `SKODUN_LOCK_POLL_SECONDS` /
`SKODUN_LOCK_STALE_SECONDS`, mirroring the oracle's `GROK_FG_LOCK_*` knobs — a
wedged lock has to be survivable without a code change. Junk in any of them
degrades to the default rather than to a crash or a busy-spin.

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
from dataclasses import dataclass, replace
from pathlib import Path

from . import checklist, contextpack, gitio, passes, promptbuild, runner
from .adapters import (REFUTER_CONTRACT, REVIEW_CONTRACT, UNAVAILABLE_RC,
                       OutputContract, ParseResult, get_adapter)
from .config import Config, Defaults, Reviewer
from .store import Store
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
#: Grace added to the worst-case runtime before a `running` record is swept.
STALE_RECORD_GRACE_SEC = 60

#: How many full reviewer runs one held lock can cover: the primary review plus
#: the security pass plus ONE of the skeptic/refuter pair, each of which runs
#: INSIDE the lock with its own complete retry budget. See
#: `lock_stale_ceiling_sec`.
#:
#: Three, not four, with three extra passes wired up: the skeptic needs the
#: merged record to have ZERO findings and the refuter needs the FINDER to have
#: had at least one, and extra-pass merges only ever append — so a run that
#: schedules one can never schedule the other. `test_refuter.py::
#: test_the_refuter_and_the_skeptic_are_mutually_exclusive` pins that, because
#: this number is what keeps a peer from reclaiming a live holder's lock.
_MAX_PASSES_UNDER_LOCK = 3

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
    is zero-padded to a constant width.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _attempt_budget_sec(d: Defaults) -> int:
    """The longest ONE reviewer run (all of its retries) can legitimately take.

    The oracle's `GROK_WORST_RUNTIME` arithmetic without the grace: each attempt
    can burn up to 2x the timeout (the watchdog's own SIGTERM grace, plus the
    oracle's doubling for a wedged attempt), and there are `1 + timeout_retries
    + degraded_retries` attempts.
    """
    return 2 * d.timeout_sec * (1 + d.timeout_retries + d.degraded_retries)


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
    """The longest one *reviewer run* can legitimately take, plus a grace.

    ORACLE PARITY at `max_chain_width=1`, and the brief pins that formula: it
    is the age at which `recover_stale` sweeps a `running` record. Deliberately
    NOT the lock's stale ceiling — see `lock_stale_ceiling_sec` for why the two
    numbers differ.

    `max_chain_width` scales the whole figure, grace included, because one
    "reviewer run" is now a CHAIN: an entry that classifies `unavailable` is
    followed by the next entry, each with its own complete retry budget. A
    record swept at the single-entry age would be failed while its run was
    still legitimately working through the chain.
    """
    return max_chain_width * (_attempt_budget_sec(d) + STALE_RECORD_GRACE_SEC)


def lock_stale_ceiling_sec(d: Defaults, max_chain_width: int = 1) -> int:
    """The age at which a held foreground lock may be reclaimed from its owner.

    Wider than `worst_runtime_sec` on purpose. That function budgets a single
    reviewer run, but the security and skeptic passes run INSIDE the lock, each
    with its own full timeout/degraded retry budget — so a legitimate holder can
    be alive for roughly `_MAX_PASSES_UNDER_LOCK` times as long. Reclaiming on
    the single-run figure would let a peer take a live holder's lock and put two
    reviews on one inference backend, which is the exact failure the lock
    exists to prevent; the cost of the wider ceiling is only that a genuinely
    wedged lock is tolerated longer, and `SKODUN_LOCK_STALE_SECONDS` exists for
    that.

    `max_chain_width` multiplies it for the same reason it multiplies
    `worst_runtime_sec`, and here it is squarely a LOCK-SAFETY requirement
    rather than bookkeeping: each of those passes may now run a whole chain, so
    a ceiling sized for one entry per pass lets a waiting peer reclaim a live
    long chain's lock and run two reviews concurrently against one inference
    backend.

    `recover_stale` keeps the narrower figure: a `running` *record* is per-run
    bookkeeping the final save always rewrites, so sweeping it early costs
    nothing, while reclaiming a live lock early costs a doubled backend.
    """
    return max_chain_width * (
        _MAX_PASSES_UNDER_LOCK * _attempt_budget_sec(d) + STALE_RECORD_GRACE_SEC)


# ---------------------------------------------------------------------------
# stale-record recovery
# ---------------------------------------------------------------------------


def recover_stale(store: Store, cfg: Config) -> int:
    """Fail every `running` record older than the worst-case runtime.

    Returns how many were swept. A SIGKILLed run never reaches its own
    `finally`, so this startup sweep is the only reliable janitor; without it
    a killed review leaves a `running` row that nothing ever finishes.

    A record whose `reviewed_at` will not parse is left alone: age is the only
    evidence this function has, and it will not act on evidence it does not
    have. Best-effort per record — one unwritable row must not stop the sweep,
    and must certainly not stop the review that follows.
    """
    ceiling = worst_runtime_sec(cfg.defaults, max_chain_width(cfg))
    now = time.time()
    swept = 0
    for rec in store.list_reviews(None, _SCAN_ALL):
        if not isinstance(rec, dict) or rec.get("status") != "running":
            continue
        rid = rec.get("id")
        started = _epoch(rec.get("reviewed_at"))
        if not isinstance(rid, str) or started is None:
            continue
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
                     grace: float = LOCK_WRITE_GRACE_SEC) -> Lock:
    """Take the foreground lock, waiting up to `wait` seconds for it.

    Waiting on a busy lock is the safe behaviour; racing it is not. Raises
    `LockTimeout` if the holder outlasts the wait.
    """
    lock = Path(common_dir) / LOCK_NAME
    worktree = Path(worktree).resolve()
    pid = os.getpid()
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
                # EXACT legacy owner format. The legacy scripts parse this file
                # to judge our liveness during shadow runs, and vice versa.
                (lock / "owner").write_text(
                    f"pid={pid}\nstarted={int(time.time())}\n"
                    f"worktree={worktree}\n", encoding="utf-8")
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
    """
    if _owner_pid(lock.path) != lock.pid:
        return False
    shutil.rmtree(lock.path, ignore_errors=True)
    return True


# ---------------------------------------------------------------------------
# running one reviewer chain
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    """What one reviewer CHAIN (every entry, every retry) produced.

    `accepted` names the attempt whose payload the record now carries —
    `{adapter_name, provider, model, effort}` — and is `None` when no attempt
    produced one. It is what makes the record's indexed `adapter`/`model`
    columns mean "who actually answered" rather than "who was asked first".
    """

    parsed: ParseResult | None
    attempts: list[dict]
    failure_reason: str
    accepted: dict | None = None


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _attempt(n: int, r: Reviewer, *, rc: int | None = None,
             timed_out: bool | None = None, duration_sec: float | None = None,
             first_output_sec: float | None = None,
             classification: dict | None = None,
             skipped: str | None = None) -> dict:
    """One `attempts[]` row, in the ONE shape the artifact schema defines.

    Every row carries the identity of the entry it belongs to (`provider`,
    `model`, `effort`) so a flat list of attempts across a chain is readable
    without cross-referencing the config that produced it — the config may well
    have changed by the time anyone reads the record.

    The four execution fields are explicitly `None` on a row where NO PROCESS
    STARTED (a cache skip, a binary that is not there, an invocation that could
    not be built). `None` is not the same as `0`: a zero duration would read as
    "it ran and returned instantly".

    `classification` is the attempt's full `ClassifyResult` for every completed
    attempt — `ok` and `degraded` as well as `unavailable` — and `None` for a
    timed-out one, whose stdout was truncated to nothing by the runner and so
    has nothing to classify. A `None` classification is therefore read together
    with `timed_out`: `True` there means the timeout, and a `skipped` key means
    nothing ever started.
    """
    row: dict = {
        "n": n,
        "provider": r.provider,
        "model": r.model,
        "effort": r.effort,
        "rc": rc,
        "timed_out": timed_out,
        "duration_sec": duration_sec,
        "first_output_sec": first_output_sec,
        "classification": classification,
    }
    if skipped is not None:
        row["skipped"] = skipped
    return row


def _classification(verdict) -> dict:
    """A `ClassifyResult` as it is persisted. Three keys, always all three."""
    return {"kind": verdict.kind, "category": verdict.category,
            "detail": verdict.detail}


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


def _binary_is_absent(binary: str) -> bool:
    """Whether `binary` names nothing this machine can execute.

    Checked BEFORE spawning, so a missing CLI costs no process and its
    `attempts[]` row is honestly free of execution fields. A path-shaped value
    is tested as a path (the per-adapter `SKODUN_<X>_BIN` overrides and grok's
    `~/.grok/bin/grok` default are both paths); a bare name goes through
    `PATH`. Existence only, not executability: a file that is there but not
    runnable is a permissions problem the spawn should report in its own words
    rather than something to route around as "the provider is unavailable".
    """
    if not binary:
        return True
    if "/" in binary or (os.sep != "/" and os.sep in binary):
        return not Path(binary).exists()
    return shutil.which(binary) is None


def _cached_unavailable(store: Store, provider: str) -> str | None:
    """The cached reason to skip `provider` right now, or None.

    Guarded: the cache is an optimisation that saves a doomed model call, so a
    store that cannot answer costs one attempt rather than the whole review.
    (The store is consulted per ENTRY, deliberately — a `quota` outage one
    entry discovers must be seen by the next entry on the same provider.)
    """
    try:
        return store.provider_unavailable_reason(provider, _iso_now())
    except Exception as e:      # pragma: no cover - defensive
        _note(f"could not read the provider-availability cache: {e!r}")
        return None


def _remember_unavailable(store: Store, provider: str, verdict) -> None:
    """Cache an `unavailable` verdict — but ONLY when it is a `quota` one.

    `quota` is the one category that is a property of the PROVIDER as a whole,
    so it is the only one worth remembering past this attempt. `auth`,
    `binary` and `model` describe THIS reviewer entry's configuration:
    caching them would let one mistyped model id black-hole a whole provider —
    for every other reviewer entry, in every later chain — for the full TTL.

    Best-effort: failing to write the cache costs extra attempts later, and
    must never be the thing that fails a review that otherwise worked.
    """
    if verdict.category != "quota":
        return
    until = _iso_at(time.time() + PROVIDER_UNAVAILABLE_TTL_SEC)
    reason = verdict.detail or "provider reported a quota outage"
    try:
        store.mark_provider_unavailable(provider, reason, "quota", until)
    except Exception as e:      # pragma: no cover - defensive
        _note(f"could not record the {provider} quota outage: {e!r}")
        return
    _note(f"marking provider {provider} unavailable until {until} ({reason})")


def _run_chain(head: Reviewer, cfg: Config, d: Defaults, prompt: bytes,
               cwd: Path, store: Store, scratch: Path, tag: str,
               contract: OutputContract = REVIEW_CONTRACT) -> _Outcome:
    """Run a reviewer chain to a verdict: entry by entry, retry by retry.

    Every retry is a FRESH run of the same prompt — never a resumed session —
    and every entry gets its OWN complete retry budget. Sharing one budget
    across the chain would starve the last entry, which is precisely the entry
    running because everything before it already failed.

    The order of the three decisions inside an entry is the whole safety
    argument, and none of them may be reordered:

    1. **A timed-out attempt is never parsed and never classified.** The runner
       truncated its stdout to nothing on purpose: a process can print a
       complete, clean-looking envelope and then hang.
    2. **Every completed attempt is CLASSIFIED before its output is
       considered.** `classify` is the provider-neutral run-health verdict, and
       `unavailable` means this provider could not serve at all — so the entry
       stops at once (retrying against a dead provider only spends time) and
       the chain advances. `degraded` spends the entry's degraded budget
       exactly as a degraded `parse` does; the two signals are OR-ed, because
       an adapter may see truncation on either axis and a run that is degraded
       by one measure is degraded.
    3. **Only then may the output be believed**, and only `parse_ok` says it is
       worth anything. `classify` returning `ok` means "no positive evidence of
       ill health", NOT "produced usable output": an empty stdout with clean
       stderr is `ok` and worth nothing. Accepting on `kind` alone would be a
       silent false all-clear.

    Advancing the chain is reserved for `unavailable`. An entry that answered
    BADLY — degraded, unparseable, timed out, or one whose invocation could not
    even be built — STOPS the chain and returns its failure: that is a harness
    or config problem, and hopping providers on it would spend someone else's
    quota to hide a bug. An exhausted chain is an explicit failure with
    `parsed=None`, which the record machinery turns into an untrustworthy
    `failed` record. It is never a pass.

    Scratch filenames extend `tag` with the chain ORDINAL and the attempt
    number (`<tag>.e<i>.a<n>`), never with the reviewer's name: names come
    from the user's config, which constrains neither `/` nor `..`. They are
    unique PER ATTEMPT, which is load-bearing rather than tidy — an adapter may
    write a sidecar derived from the prompt path (codex writes its
    `--output-schema` beside it and always overwrites), so two attempts sharing
    one prompt path would swap each other's requested response shape between
    `build_cmd` and `exec`.
    """
    chain = _chain_for(cfg, head)
    attempts: list[dict] = []
    exhausted: list[str] = []
    # `n` counts attempts across the WHOLE chain, so every row in the flat
    # `attempts[]` list has a unique ordinal; the per-entry count below is what
    # the retry budgets and the timeout message talk about.
    n = 0

    for i, entry in enumerate(chain):
        adapter = get_adapter(entry.provider)

        cached = _cached_unavailable(store, entry.provider)
        if cached is not None:
            n += 1
            _note(f"skipping {entry.name} ({entry.provider}): {cached}")
            attempts.append(_attempt(
                n, entry, skipped=f"provider marked unavailable: {cached}",
                classification={"kind": "unavailable", "category": "quota",
                                "detail": cached}))
            exhausted.append(f"{entry.name}/{entry.provider}: {cached}")
            continue

        binary = adapter.resolve_binary()
        if _binary_is_absent(binary):
            n += 1
            verdict = adapter.classify(UNAVAILABLE_RC, b"", b"", contract)
            next_step = ("trying the next entry" if i + 1 < len(chain)
                         else "no entries remain")
            _note(f"{entry.name} ({entry.provider}): binary not found at "
                  f"{binary}; {next_step}")
            attempts.append(_attempt(
                n, entry, skipped=f"binary not found: {binary}",
                classification=_classification(verdict)))
            exhausted.append(f"{entry.name}/{entry.provider}: {verdict.detail}")
            continue

        timeouts_used = 0
        degraded_used = 0
        entry_n = 0
        while True:
            n += 1
            entry_n += 1
            stem = f"{tag}.e{i}.a{n}"
            prompt_file = scratch / f"{stem}.prompt.txt"
            out_path = scratch / f"{stem}.out"
            err_path = scratch / f"{stem}.err"
            try:
                prompt_file.write_bytes(prompt)
                cmd = adapter.build_cmd(prompt_file, entry, d, cwd, contract)
            except Exception as e:
                # An effort this CLI cannot express, an unwritable schema
                # sidecar: a LOCAL failure, not an unavailable provider. It
                # stops the chain rather than routing around it — silently
                # reviewing somewhere else at some other default is exactly the
                # unnoticed downgrade `build_cmd` raises to prevent.
                attempts.append(_attempt(
                    n, entry,
                    skipped=f"could not build the invocation: {e!r}"))
                return _Outcome(None, attempts,
                                f"reviewer {entry.name!r} could not be "
                                f"invoked: {e!r}")

            # The prompt travels as a FILE either way; this only decides who
            # opens it. Without it, an adapter whose argv ends in the CLI's
            # stdin marker hangs until the watchdog kills it.
            stdin_path = (prompt_file
                          if getattr(adapter, "stdin_from_prompt_file", False)
                          else None)
            try:
                result = runner.run_with_watchdog(
                    cmd, d.timeout_sec, cwd, out_path, err_path,
                    stdin_path=stdin_path)
            except FileNotFoundError:
                # The binary existed when we looked and does not now, or the
                # adapter's argv names something else that is missing. Same
                # verdict as the pre-spawn check: nothing ran, so the execution
                # fields stay null, and the chain advances.
                verdict = adapter.classify(UNAVAILABLE_RC, b"", b"", contract)
                attempts.append(_attempt(
                    n, entry, skipped=f"binary not found: {cmd[0]}",
                    classification=_classification(verdict)))
                exhausted.append(
                    f"{entry.name}/{entry.provider}: {verdict.detail}")
                break

            if result.timed_out:
                attempts.append(_attempt(
                    n, entry, rc=result.rc, timed_out=True,
                    duration_sec=round(result.duration_sec, 3),
                    first_output_sec=_round(result.first_output_sec),
                    classification=None))
                if timeouts_used < d.timeout_retries:
                    timeouts_used += 1
                    _note(f"attempt {entry_n} timed out after {d.timeout_sec}s;"
                          f" retrying in a fresh session "
                          f"({timeouts_used}/{d.timeout_retries})")
                    continue
                # NEVER parsed, and never classified: the runner truncated
                # stdout precisely so that a hung run's complete-looking
                # envelope cannot become a review.
                return _Outcome(None, attempts,
                                f"timed out after {entry_n} attempts")

            stdout, stderr = _read(out_path), _read(err_path)
            verdict = adapter.classify(result.rc, stdout, stderr, contract)
            attempts.append(_attempt(
                n, entry, rc=result.rc, timed_out=False,
                duration_sec=round(result.duration_sec, 3),
                first_output_sec=_round(result.first_output_sec),
                classification=_classification(verdict)))

            if verdict.kind == "unavailable":
                _remember_unavailable(store, entry.provider, verdict)
                _note(f"{entry.name} ({entry.provider}) is unavailable "
                      f"({verdict.category}): {verdict.detail}")
                exhausted.append(
                    f"{entry.name}/{entry.provider}: {verdict.detail}")
                break

            parsed = adapter.parse(stdout, stderr, contract)
            if verdict.kind == "degraded" and not parsed.degraded:
                # OR-ed, not preferred: a truncation only `classify` can see is
                # still a truncation, and the record has to say so on the axis
                # the gate reads.
                parsed = replace(parsed, degraded=True,
                                 degraded_reason=verdict.detail)
            if parsed.degraded and degraded_used < d.degraded_retries:
                degraded_used += 1
                _note(f"attempt {entry_n} came back degraded "
                      f"({parsed.degraded_reason}); retrying in a fresh "
                      f"session ({degraded_used}/{d.degraded_retries})")
                continue

            # `parse_ok`, not `kind`: `ok` only means nothing looked ill.
            accepted = None
            if parsed.parse_ok:
                accepted = {"adapter_name": adapter.name,
                            "provider": entry.provider, "model": entry.model,
                            "effort": entry.effort}
            return _Outcome(parsed, attempts, "", accepted)

    summary = "; ".join(exhausted) or "no chain entry could be attempted"
    return _Outcome(None, attempts, f"all providers unavailable: {summary}")


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


#: Extra pass -> the configured reviewer role it prefers over the finder, in
#: the order the passes are scheduled. ONE table: `_pass_reviewer` reads it to
#: pick the reviewer and preflight reads it to validate every reviewer this run
#: may reach for, so a new pass cannot be wired up on one side only.
_EXTRA_PASS_ROLES = {"security": "security", "skeptic": "refuter",
                     "refuter": "refuter"}


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
    # `lock_stale_ceiling_sec`, NOT `worst_runtime_sec`: the extra passes run
    # inside this lock with their own retry budgets, so a live holder can
    # legitimately outlast a single run's worst case. (`recover_stale` above
    # keeps the narrower figure; the docstrings on both say why.) Scaled by the
    # configured chain width, because each of those passes may now work through
    # a whole chain inside this lock.
    ceiling = float(lock_stale_ceiling_sec(d, max_chain_width(cfg)))
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

        common = dict(
            id=rid, reviewed_at=_iso_now(), source="skodun",
            branch=branch, head=head, base_ref=base.ref, base_sha=base.sha,
            diff_hash=diff_hash, mode=mode, model=finder.model,
            adapter=adapter.name, timeout_seconds=d.timeout_sec,
            max_turns=d.max_turns,
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

        # --- 6. checklist -> context pack -> prompt -----------------------
        selection = checklist.select(
            diff.files, "full", _under(root, d.checklist_dir),
            _under(root, d.rules_json), d.checklist_map, d.test_path_patterns)
        if selection.note:
            # `degraded` (not `bool(note)`) separates the two severities: an
            # empty selection is a total failure, a full one with a note means
            # only the cross-file registry was unavailable.
            kind = ("cross-file rules unavailable" if selection.degraded
                    else "path-scoped rules dropped")
            _note(f"checklist: {kind} -- {selection.note}")
        # Budget eviction is otherwise SILENT: `select` quietly drops the
        # least-valuable sections until the injection budget is met, so a
        # review can run with rules the operator believes are in the prompt and
        # nothing anywhere says otherwise. `dropped`/`over_budget` are the two
        # fields that know, and this is where they are read.
        if selection.dropped:
            _note(f"checklist: dropped {', '.join(selection.dropped)} to fit "
                  f"the {checklist.BUDGET}-byte injection budget")
        if selection.over_budget:
            _note(f"checklist: {selection.bytes_total} bytes still exceeds the "
                  f"{checklist.BUDGET}-byte budget after eviction; only "
                  f"undroppable sections remain")

        pack = None
        pack_body = None
        if d.context_pack:
            headroom = promptbuild.context_headroom(
                d.max_diff_bytes, len(diff.data), packing=True)
            # `pack_large_added=False`: this is the SINGLE-SHOT path, so the
            # diff already carries every added file whole. Packing a large one
            # again would spend headroom saying the same thing twice -- and
            # since selection is size-descending it would be packed FIRST,
            # crowding out the modified files whose current contents only the
            # packer can show. (When the diff is truncated the copy in the
            # prompt is incomplete, but a truncated diff is never trustworthy
            # anyway, so no trust decision rides on that case.)
            pack = contextpack.pack(root, diff.files, diff.statuses, headroom,
                                    pack_large_added=False)
            pack_body = pack.body

        prompt = promptbuild.build(
            branch, base.ref, base.sha, f"{head} (working tree)", diff.data,
            d.max_diff_bytes, selection, pack_body)
        if prompt.diff_truncated:
            _note(f"diff is {len(diff.data)} bytes (> {d.max_diff_bytes}); the "
                  f"prompt is truncated and this review cannot be trustworthy")

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
            # READS BACKWARDS UNLESS YOU KNOW `Selection`'s two severities, so:
            # `checklist_degraded` is TRUE for a PARTIAL degradation (sections
            # were selected, but something they depend on -- currently the
            # cross-file rules registry -- was unavailable) and FALSE for a
            # TOTAL selection failure (nothing was selected at all, including
            # the ordinary "this repo has no checklist directory" case). It is
            # not a severity dial: `checklist_note` carries the reason, this
            # field says only which of the two shapes produced it, and
            # `checklist_sections` distinguishes them on its own (empty for a
            # total failure). Nothing about the review's trust rides on it --
            # checklist selection is fail-soft by design.
            checklist_degraded=selection.degraded,
            context_bytes=pack.bytes_total if pack is not None else 0,
            context_files=list(pack.included) if pack is not None else [],
            context_omitted_files=[f"{p} ({r})" for p, r in pack.omitted]
                                  if pack is not None else [],
            attempts=[], summary="", findings=[], findings_total=0,
            severity={"high": 0, "medium": 0, "low": 0}, rule_ids=[],
            extra_passes={}, failure_reason="",
        )

        # --- 7. persist `running`, then run the finder --------------------
        _save(store, rec)
        persisted = True
        _note(f"reviewing {len(diff.files)} file(s) vs {base.ref} as {rid} ...")

        with tempfile.TemporaryDirectory(prefix="skodun-") as tmp:
            scratch = Path(tmp)
            outcome = _run_chain(finder, cfg, d, prompt.text, root, store,
                                 scratch, "primary")
            rec["attempts"] = outcome.attempts
            _apply(rec, outcome)

            # --- 8. THE FINDER SNAPSHOT, taken before any merge -----------
            # The refuter's eligibility, its prompt, and the meaning of every
            # verdict index are all fixed HERE, on what the finder itself
            # produced — never on the merged record. Three things ride on
            # that, and each of them is a real failure the merged record would
            # cause: a security finding must not trigger a refuter the finder
            # did not earn; a security demotion must not suppress one the
            # finder did earn; and a verdict's `index` must mean the finder's
            # own numbering, which stays `0..n-1` in the merged list only
            # because extra-pass merges APPEND.
            finder_trustworthy = is_trustworthy(
                rec["parse_ok"], rec["degraded"], rec["diff_truncated"])
            finder_findings = list(rec["findings"])
            finder_findings_total = rec["findings_total"]
            # Whoever ACTUALLY answered, not whoever was asked: after a
            # fallback the finder's own entry may never have run, and "did a
            # second provider look at this?" is a question about the answering
            # provider.
            finder_provider = (outcome.accepted["provider"]
                               if outcome.accepted is not None
                               else finder.provider)

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
                _note(skip_note)
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
