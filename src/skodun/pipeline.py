"""The foreground review pipeline: one `--now` review, start to record.

This is the module that turns every other module into a review. It resolves the
base, captures the diff, selects checklist sections, packs file context, builds
the prompt, runs the reviewer under a watchdog with two independent retry axes,
runs the two extra passes, persists one artifact, and RETURNS it -- the verdict
banner is rendered by whoever asked (see "The banner comes from the record"
below). Ported from the oracle's `--now` path
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
the legacy scripts do. Note also that `SKODUN_LOCK_STALE_SECONDS` is a WAITER's
knob and only a waiter's: it says how long THIS process waits before reclaiming
from someone else, and it can shrink neither the budget this process publishes
as a HOLDER (`run_review` passes the ceiling its own batch plan implies, never
the override) nor the budget a PEER holder published (`_lock_is_reclaimable`
takes `max(own ceiling, holder budget)`). The escape hatch for a genuinely
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

The banner comes from the record, and THIS MODULE NEVER PRINTS IT
-----------------------------------------------------------------
`run_review` returns the *persisted* record — read back, never recomputed — and
its caller renders the verdict from that, through `trust.banner`, the one
definition of it. Paths that never persisted anything raise, and the caller
renders `banner_failure` instead. So the banner and the row the gate later reads
still cannot disagree, and the division of labour is sharper than it was: this
module decides and records, the caller presents and owns the exit code.

The reason it changed is the MCP transport. `run_review` used to `print` the
banner itself, and `skodun mcp`'s stdout is a JSON-RPC stream that another
thread may be mid-write on: one banner line there desynchronises the client's
parser for the rest of the session. A process-global `redirect_stdout` would be
no better, for the same reason. STDOUT IS NOT THIS MODULE'S TO WRITE TO.

Progress notes still go to stderr by default. A caller that wants them
elsewhere passes `progress_sink=`; see `_note`.
"""

from __future__ import annotations

import calendar
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import (batching, budget, capacity, chain, checklist, checkpoints,
               contextpack, gitio, ids, passes, promptbuild, provenance, reuse,
               routing, runner, stack, telemetry)
from .adapters import NORMAL_STOP_REASONS, REFUTER_CONTRACT, get_adapter
from .config import Config, Defaults, Reviewer, quota_pool_for
from .store import Store, _TS_FORMAT
from .trust import is_trustworthy

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

#: The `failure_reason` a swept `running` record carries. Task 12 renders it, so
#: it is a constant here rather than a literal at the call site.
STALE_RECOVERY_REASON = "stale recovery: worker exceeded its runtime budget"

#: The `failure_reason` the foreground cleanup writes when a run left a
#: persisted `running`/committed record behind without finalizing it.
UNFINISHED_REASON = ("the review did not finish; its record was never finalized")
#: Same durable demotion as `UNFINISHED_REASON`, but named so S1's
#: `report_state` maps it to `cancelled` rather than generic `failed`.
UNFINISHED_CANCEL_REASON = (
    "cancelled: the review did not finish; its record was never finalized")


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


class CheckpointInFlight(PipelineError):
    """An exact pass is already claimed by a live competing resumer."""


class CheckpointClaimLost(PipelineError):
    """A caller lost its fenced pass claim before it could complete it."""


CHECKPOINT_RETENTION_SEC = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


#: The per-THREAD progress sink, installed by `run_review(progress_sink=...)`.
#:
#: Thread-local, and not a parameter threaded through thirty call sites, because
#: progress is a property of the RUN and a run is a thread: the MCP server
#: answers `ping` on its read loop while a review narrates itself from its own
#: thread, and those two must not be able to see each other's sink. A module
#: global would be exactly that cross-talk; a parameter on `_note` would mean
#: passing one through `_orchestrate`, `_run_sub`, `_extra_pass`, `_refuter_pass`
#: and every helper that says anything, where forgetting one is a silent hole.
#:
#: It is NOT a stdout redirect. A process-global `redirect_stdout` would corrupt
#: the JSON-RPC stream this exists to keep clean.
_PROGRESS = threading.local()

# --- in-process cancel-by-id (epic S1) --------------------------------------
#
# MCP disconnect already sets one Event for the long-running review slot. Status
# and cancel-by-id need the same token keyed by review id so a peer tool call in
# the same process can stop a hang without waiting for session EOF. Background
# workers still use SIGTERM (dispatch installs the handler); this registry only
# covers processes that hold the Event in memory.
_ACTIVE_CANCELS: dict[str, "threading.Event"] = {}
_ACTIVE_CANCELS_LOCK = threading.Lock()


def register_cancel(review_id: str, cancel: "threading.Event") -> None:
    """Publish `cancel` for `review_id` so `request_cancel` can set it."""
    if not isinstance(review_id, str) or not review_id:
        return
    with _ACTIVE_CANCELS_LOCK:
        _ACTIVE_CANCELS[review_id] = cancel


def unregister_cancel(review_id: str) -> None:
    """Drop a published cancel token. Idempotent."""
    if not isinstance(review_id, str) or not review_id:
        return
    with _ACTIVE_CANCELS_LOCK:
        _ACTIVE_CANCELS.pop(review_id, None)


def request_cancel(review_id: str) -> bool:
    """Set the in-process cancel token for `review_id` if one is registered.

    Returns True when a token was found and set. False means this process does
    not hold that review's Event (another process owns it, or nothing is in
    flight under that id here) -- callers should still try process signalling
    and durable demotion.
    """
    if not isinstance(review_id, str) or not review_id:
        return False
    with _ACTIVE_CANCELS_LOCK:
        cancel = _ACTIVE_CANCELS.get(review_id)
    if cancel is None:
        return False
    cancel.set()
    return True


def request_cancel_all() -> int:
    """Set every in-process cancel token. Used by the MCP main-thread SIGTERM
    forwarder: long-running reviews run on a worker thread where
    `signal.signal` cannot install a handler, so the process handler must fan
    out to every registered Event (and the server's own `_worker_cancel`).

    Returns how many tokens were set.
    """
    with _ACTIVE_CANCELS_LOCK:
        tokens = list(_ACTIVE_CANCELS.values())
    for cancel in tokens:
        try:
            cancel.set()
        except Exception:                   # pragma: no cover - defensive
            pass
    return len(tokens)


def _note(message: str) -> None:
    """One progress line. To this thread's sink if it has one, else to stderr.

    Never stdout: the caller owns that stream, and for `skodun mcp` it is a
    protocol. Never raises — a broken sink or a broken stderr must not be what
    fails a review, and a sink that raises still gets the line onto stderr.
    """
    sink = getattr(_PROGRESS, "sink", None)
    if sink is not None:
        try:
            sink(message)
            return
        except BaseException:
            pass   # fall through: a broken sink must not silence the review
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


def _iso_after(seconds: float) -> str:
    return time.strftime(_TS_FORMAT, time.gmtime(time.time() + max(0, seconds)))


#: `sk_<utcstamp>_<pid>_<uuid8>`, now `ids.new_review_id`. An IMPORT, not a
#: wrapper: `store.reserve_prepush` mints the reserved record's id and the store
#: cannot import this module, so the definition had to move somewhere both can
#: reach. Two copies would drift on the one property that matters (see `ids`).
_new_id = ids.new_review_id


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

    Reads `running_records`, not `list_reviews`: everything below is an index
    row column, so filtering `running` out of DECODED artifacts made every
    push pay for every review ever stored — on the synchronous `git push`
    path. Unbounded and unordered on purpose; see that method.
    """
    computed = worst_runtime_sec(cfg.defaults, max_chain_width(cfg))
    now = time.time()
    swept = 0
    for rec in store.running_records():
        rid = rec.get("id")
        started = _epoch(rec.get("reviewed_at"))
        if not isinstance(rid, str) or started is None:
            continue
        persisted = _record_budget(rec)
        ceiling = computed if persisted is None else persisted
        if now - started <= ceiling:
            continue
        try:
            # CONDITIONAL: a worker finalizing a real review, or a dispatcher
            # superseding this row for a newer push, may have committed between
            # the scan above and this line -- and either answer is better than a
            # janitor's. Whichever terminal transition commits first survives.
            # (The shipped unconditional `set_status` also left the trust axes
            # alone, so a swept row could read `status='failed'` beside
            # `trustworthy=1` -- which the gate honours and dedup suppresses
            # against. `fail_if_running` demotes both in one statement.)
            if not store.fail_if_running(rid, STALE_RECOVERY_REASON):
                continue
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
                     budget_sec: float | None = None,
                     cancel: "threading.Event | None" = None) -> Lock:
    """Take the foreground lock, waiting up to `wait` seconds for it.

    Waiting on a busy lock is the safe behaviour; racing it is not. Raises
    `LockTimeout` if the holder outlasts the wait.

    `cancel` makes the WAIT abortable, which matters because the wait is the
    longest thing a foreground review does before it does anything: the default
    is the whole stale ceiling, tens of minutes. An agent that closed its MCP
    session must not leave this loop polling for half an hour on its behalf. So
    the token is checked at the top of every iteration — which covers the
    pre-acquisition case on the first pass — and the sleep between polls WAITS on
    the token rather than on the clock, so EOF is noticed in milliseconds
    instead of at the next tick. Nothing is held at that point (no lock, no
    record), so the abort is a bare raise.

    Acquisition is `mkdir` — the atomic no-replace primitive — and the two files
    inside it are published in a FIXED ORDER: `budget` first, `owner` last.

    `mkdir` is not one of several ways to do this. `os.rename` of a prepared
    temp directory silently REPLACES an existing empty directory on POSIX
    (verified on this host), which would clobber a legacy holder caught between
    its own `mkdir` and its owner write — the one moment that holder cannot
    defend itself. `EEXIST` is contention, exactly as it has always been.

    `budget_sec` is the holder's own runtime budget for the sidecar, and it is
    a DIFFERENT fact from `stale`: `stale` is how long this process will wait
    before reclaiming from someone else (an operator may shrink it with
    `SKODUN_LOCK_STALE_SECONDS`), while the sidecar says how long this holder
    legitimately needs. `run_review` passes `budget.lock_stale_ceiling` for the
    batch plan its diff implies, so an override cannot make the holder advertise
    less than it needs. It defaults to `stale` only for the callers that have no
    separate figure (tests, and the direct-call sites in the suite), where the
    two coincide by construction.
    """
    lock = Path(common_dir) / LOCK_NAME
    worktree = Path(worktree).resolve()
    pid = os.getpid()
    published = float(stale if budget_sec is None else budget_sec)
    deadline = time.monotonic() + wait
    noted = False

    while True:
        if runner._cancelled(cancel):
            raise ReviewCancelled(
                "the review was cancelled while it waited for the foreground "
                "review lock")
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
        if runner._sleep_or_cancelled(cancel, poll):
            raise ReviewCancelled(
                "the review was cancelled while it waited for the foreground "
                "review lock")


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


#: `runner._is_path_shaped`, aliased under this module's name -- the same
#: arrangement `ReviewCancelled` gets below, and for the same reason. It USED to
#: be defined here, which made `cli._fmt_binary` (and therefore the whole of
#: `skodun providers`, a read-only diagnostic) import `pipeline` to ask one
#: question about a string -- so a pipeline that will not import took the
#: diagnostic for diagnosing it down too. It moved to the leaf; nothing here
#: reads it any more, and the name stays resolvable because several docstrings
#: and the by-name spy in `test_cli.py` grew up pointing at it.
_is_path_shaped = runner._is_path_shaped


#: The chain executor itself now lives in `chain.py` as `run_chain`; this
#: one-line alias is the whole compatibility surface -- existing tests
#: monkeypatch `pipeline._run_chain` by name (`test_pipeline.py`,
#: `test_refuter.py`), and `run_review`/`_extra_pass`/`_refuter_pass` below
#: still call the bare name `_run_chain`, so the patched value is what they
#: see.
_run_chain = chain.run_chain


def _cancel_kw(cancel: "threading.Event | None") -> dict:
    """`{"cancel": token}`, or `{}` when there is no token.

    Used at the `_run_chain` call sites the FOREGROUND owns, and the reason is
    the alias above: `_run_chain` is monkeypatched BY NAME across the suite, so a
    run with no cancellation token must call it with exactly the argument list it
    has always been called with -- otherwise a stand-in written against the
    shipped signature starts failing on a keyword it never needed. A run that HAS
    a token passes it, because the watchdog tick loop is the only layer holding
    the provider's pgid and therefore the only one that can take the model down.
    """
    return {} if cancel is None else {"cancel": cancel}


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
        out = {"provider": a["provider"], "model": a["model"],
               "effort": a["effort"]}
        timing = a.get("capacity_timing")
        for row in reversed(outcome.attempts):
            candidate = row.get("capacity_timing")
            if isinstance(candidate, dict):
                timing = candidate
                break
        if isinstance(timing, dict):
            out["capacity_timing"] = dict(timing)
        return out
    for row in reversed(outcome.attempts):
        if "skipped" not in row:
            out = {"provider": row.get("provider"), "model": row.get("model"),
                   "effort": row.get("effort")}
            timing = row.get("capacity_timing")
            if isinstance(timing, dict):
                out["capacity_timing"] = dict(timing)
            return out
    out = {"provider": None, "model": None, "effort": None,
           "note": outcome.failure_reason or "no attempt started a process"}
    for row in reversed(outcome.attempts):
        timing = row.get("capacity_timing")
        if isinstance(timing, dict):
            out["capacity_timing"] = dict(timing)
            break
    return out


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
#: The skeptic deliberately reuses the selected finder entry and its own
#: fallback chain. It is a clean-result adversarial check, not the
#: cross-provider finding annotation pass; coupling it to the `refuter` role
#: would let an unrelated refuter quota outage demote an otherwise clean
#: finder. Only the refuter pass reads the `refuter` role.
_EXTRA_PASS_ROLES = {"security": "security", "skeptic": "finder",
                     "refuter": "refuter",
                     passes.INTEGRATION_PASS: passes.INTEGRATION_ROLE}


def _pass_reviewer(cfg: Config, pass_name: str, finder: Reviewer) -> Reviewer:
    """The reviewer an extra pass will use: its role's, else the finder's.

    See `_extra_pass` for why the role-specific preference exists at all. The
    skeptic's `finder` mapping is intentional: the selected head, including
    its configured fallbacks, is the reviewer for both the primary and clean
    adversarial check unless the pass is the separate annotation-only refuter.

    The skeptic is the exception to role-specific selection: it follows the
    finder chosen by `run_review`'s `reviewer=` request or auto-routing, so its
    own fallback chain is the same chain that protects the primary review.
    Other extra passes remain role-selected and are not redirected by
    `reviewer=` except through the existing `else the finder's` fallback when
    no reviewer for that role is configured.
    """
    if pass_name == "skeptic":
        return finder
    reviewer = _reviewer_for(cfg, _EXTRA_PASS_ROLES[pass_name])
    return reviewer if reviewer is not None else finder


def _requested_head(cfg: Config, requested: str) -> Reviewer:
    """The configured entry a caller asked for BY NAME, or refuse the run.

    `--reviewer <name>` / the MCP `review` tool's `reviewer` argument: it
    narrows where this run's chain STARTS, and nothing else — the entry's own
    `fallbacks` are followed exactly as they would be for the config's finder
    (`_chain_for`), so the choice cannot cost the run its ability to recover.

    Both refusals are `PreflightRefused`, so they land before the lock, before
    any record and before any process, with the "no review ran" every other
    preflight refusal carries. NEITHER may fall back to the config's own finder:
    a caller who asked for a second opinion from a specific provider and
    silently got the first one back has been answered by the model they were
    trying to route around, and nothing in the record would say so.

    A name that resolves but whose PROVIDER has no adapter is not refused here —
    it is refused by `_adapter_for`, in the same words as any other unresolvable
    provider, because the requested entry simply joins the graph preflight
    already walks.

    The refusal for an unknown name lists what IS configured, `name (provider)`
    each. An agent driving the MCP server cannot enumerate the reviewer table —
    `providers` is deliberately not a tool — so the refusal for a name it
    guessed is the one place it can learn the real ones.
    """
    for entry in cfg.reviewers:
        if entry.name == requested:
            if not entry.enabled:
                raise PreflightRefused(
                    f"requested reviewer {requested!r} is disabled in the "
                    f"config; no review ran")
            return entry
    known = ", ".join(f"{r.name} ({r.provider})"
                      for r in cfg.reviewers if r.enabled)
    raise PreflightRefused(
        f"requested reviewer {requested!r} is not configured; enabled "
        f"reviewers: {known or '(none)'}; no review ran")


def _default_head(cfg: Config) -> Reviewer:
    """The config's own finder, or refuse the run. Pre-S5 head selection."""
    finder = _reviewer_for(cfg, "finder")
    if finder is None:
        raise PreflightRefused(
            "no enabled reviewer with role 'finder' is configured; no "
            "review ran")
    return finder


def _auto_fallback_head(cfg: Config) -> Reviewer:
    """The head for an `auto` run the router could not decide.

    An EXPLICIT `[routing] pool` is an exclusion as much as it is a list: the
    way an operator keeps a finder configured for pinning while keeping it out
    of automatic selection is to leave it out of the pool. So the fallback for
    "nothing was routable" is the first entry the operator POOLED, not
    `_reviewer_for(cfg, "finder")` -- which is very likely the entry they
    excluded, and sending an automatic run there would be the one thing the
    pool exists to prevent.

    A blacked-out pool entry heading the chain is the honest outcome, not a
    problem this function should route around: `_finder_chain_unavailable`
    fails the run fast and names the outage, which is what an operator whose
    whole pool is out of quota needs to be told.

    With no explicit pool (the default), every enabled finder IS the pool, so
    the config's finder is already the first candidate and this is exactly
    `_default_head`.
    """
    if cfg.routing.pool:
        pooled = routing.resolve_pool(cfg)
        if pooled:
            return pooled[0]
    return _default_head(cfg)


def resolve_review_head(cfg: Config, store: Store, *,
                        requested: str | None = None,
                        client_family: str | None = None,
                        avoid_providers: set[str] | None = None,
                        prompt_size: int | None = None,
                        ) -> tuple[Reviewer, dict]:
    """The entry that heads THIS run's chain, and the audit of how it was chosen.

    The one place head selection happens, so the CLI and any number of stdio MCP
    processes apply the same rule -- which is the whole reason routing lives here
    and not in the MCP server: those processes cannot see each other except
    through the store, and a policy that lived in one of them would be a policy
    the others do not have.

    Three paths, in priority order:

    * **Pinned.** A `reviewer` name is absolute in every mode. It is not scored,
      not compared against load, and never quietly downgraded -- a caller asking
      a specific provider for a second opinion who silently got the default one
      has been answered by the model they were routing around.
    * **Auto.** `[routing] mode = "auto"` and no pin: `routing.auto_route` scores
      the pool against store-visible load. Once, here -- not per poll.
    * **Config.** `mode = "off"` (the default), and the fallback for an auto run
      with nothing routable: today's first enabled `finder`.

    Whatever is chosen, the head's OWN `fallbacks` chain is unchanged: routing
    picks where the chain starts, never whether it can recover.

    The returned dict is the audit, and it lands verbatim on the artifact.
    `client_family` is recorded whether or not it changed anything -- it
    describes the CALLER, and a record that only mentioned it on the runs where
    it tipped a tie could not answer "was cross-model even in play here?".

    NOTE (Phase A scope): the background pre-push worker keeps config-finder
    selection. It is a different surface with a reserved, identity-pinned
    record, and S5 Phase A is the foreground review loop the design diagram
    names (CLI and MCP through `services.svc_review`).
    """
    if requested is not None:
        head = _requested_head(cfg, requested)
        reason = routing.ROUTE_PINNED
    elif cfg.routing.mode == "auto":
        route = routing.auto_route(
            cfg, store, client_family=client_family,
            excluded_providers=avoid_providers,
            prompt_size=prompt_size)
        if route is None:
            # Nothing routable: an empty pool, every candidate blacked out, or a
            # store that could not answer. A configured head still runs and
            # `_finder_chain_unavailable` below still fails fast in its own
            # words if the outage is real -- a router that refused here would
            # replace a diagnosis with a shrug.
            head = _auto_fallback_head(cfg)
            if avoid_providers and head.provider in avoid_providers:
                alternatives = tuple(
                    reviewer for reviewer in routing.resolve_pool(cfg)
                    if reviewer.provider not in avoid_providers)
                if not alternatives:
                    raise PreflightRefused(
                        "recovery found no alternative reviewer provider; "
                        "the configured providers already reached terminal "
                        "attempts")
                head = alternatives[0]
            reason = routing.ROUTE_DEFAULT_FINDER
        else:
            head, reason = route.reviewer, route.reason
        _note(f"routing: {reason} -> {head.name} ({head.provider})")
    else:
        head, reason = _default_head(cfg), routing.ROUTE_CONFIG_FINDER
    meta = {
        "requested_reviewer": requested,
        "routed_reviewer": head.name,
        "route_reason": reason,
        "client_family": client_family,
    }
    pool = quota_pool_for(head)
    if pool != head.provider:
        meta["quota_pool"] = pool
    return head, meta


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
               lock_stale: float | None = None, *,
               progress_sink=None,
               cancel: "threading.Event | None" = None,
               reviewer: str | None = None,
               client_family: str | None = None,
               avoid_providers: set[str] | None = None,
               resume_checkpoints: bool = True,
               stack_request: "stack.StackRequest | None" = None) -> dict:
    """Run one foreground review and return the record that was persisted.

    WRITES NOTHING TO STDOUT. Progress goes to stderr (or to `progress_sink`);
    the verdict is the caller's to render, from the returned record, through
    `trust.banner`. Raises `PreflightRefused`, `LockTimeout` or
    `PersistenceFailed` on the paths where no record could be produced;
    `services.svc_review` maps those to exit codes and to a `banner_failure`
    line, so the "last stdout line is a verdict" invariant holds on every path,
    including the ones that never got this far.

    Three keyword-only parameters, all defaulting to the shipped behaviour so
    that every existing call site is unchanged:

    * **`progress_sink`** — a `callable(str)` that receives each progress line
      instead of stderr, for the length of THIS call and on THIS thread only
      (`_note`). `None` keeps the stderr behaviour.
    * **`cancel`** — a `threading.Event` the caller sets to stop the review. It is
      the same token Task 10 threads through the background worker, and it is
      checked in the same three kinds of place: the lock wait, every pass
      boundary, and the provider watchdog's tick loop (which is what actually
      takes the model's process group down). `ReviewCancelled` is a
      `BaseException` precisely so the broad `except Exception` demote-don't-
      destroy guards in `_extra_pass`/`_refuter_pass`/`_run_sub` cannot convert a
      cancellation into a mere pass failure that then FINALIZES a trustworthy
      primary review.

      What a cancellation leaves behind depends on where it lands, and the three
      cases are deliberate:

        before the record is persisted   NO record at all. Nothing trustworthy
                                         can exist for content whose review
                                         never even started being written down,
                                         and an empty `failed` row would be a
                                         trace of nothing.
        after `_save`, before finalize   the shipped `finally` demotes it to
                                         `failed` (`mark_failed` moves the status
                                         AND the trust axes, and leaves the
                                         findings alone).
        during the final commit          the POST-COMMIT linearization check
                                         below demotes the committed row through
                                         `store.mark_cancelled`, which is
                                         `cancellation_transform` as one atomic
                                         statement. A token set while SQLite held
                                         the write lock is invisible to the
                                         checkpoint before it, and without this
                                         a killed review would stand as a
                                         trustworthy one.

      The lock is released on every one of them by the same `finally`.

    * **`reviewer`** — the NAME of a configured `[[reviewers]]` entry to head
      this run's chain, instead of the config's own `finder` role. `None` (the
      default, and every pre-existing call site) means "the config decides", and
      is recorded on the artifact as `requested_reviewer: null`. A name that
      does not resolve is a `PreflightRefused` (see `_requested_head`): the
      request is never quietly downgraded to the config default. It selects the
      FINDER head only — the extra passes still choose by role.

    * **`client_family`** — the caller's OWN model family (`xai`, `openai`, …),
      used only for the cross-model preference when `[routing] mode = "auto"`
      and no `reviewer` was pinned. `None` falls through to
      `SKODUN_CLIENT_FAMILY`, and an undeclared family simply scores on
      availability alone. It is a HINT worth one tie-break, never a filter: see
      `routing.pick_finder`.
    """
    repo = Path(repo)
    d = cfg.defaults
    if progress_sink is not None:
        # Installed for the duration of this call, on this thread, and always
        # removed again: a sink left behind would capture the NEXT review's
        # progress on a reused thread.
        _PROGRESS.sink = progress_sink
    try:
        return _run_review(repo, cfg, store, mode, lock_wait, lock_poll,
                           id_prefix, lock_stale, cancel, d, reviewer,
                           client_family, avoid_providers,
                           resume_checkpoints, stack_request)
    finally:
        if progress_sink is not None:
            _PROGRESS.sink = None


def _run_review(repo: Path, cfg: Config, store: Store, mode: str,
                lock_wait: float | None, lock_poll: float | None,
                id_prefix: str, lock_stale: float | None,
                cancel: "threading.Event | None", d: Defaults,
                requested: str | None = None,
                client_family: str | None = None,
                avoid_providers: set[str] | None = None,
                resume_checkpoints: bool = True,
                stack_request: "stack.StackRequest | None" = None) -> dict:
    """`run_review`'s body. Split off ONLY so the progress sink can be installed
    and removed around it without wrapping 400 lines in another indent level."""

    # --- 1. preflight -----------------------------------------------------
    # Refused so that a model session bound to the main checkout cannot review
    # (or be pointed at) the wrong tree while agents work in linked worktrees.
    if gitio.is_primary_checkout(repo) and os.environ.get("SKODUN_ALLOW_MAIN") != "1":
        raise PreflightRefused(
            f"{repo} is the primary checkout; run the review from a linked "
            f"worktree or set SKODUN_ALLOW_MAIN=1; no review ran")
    # Who heads this run's chain, and the audit of why (S5). A caller who named
    # the head supplies the one thing the role lookup exists to produce, so a
    # config with no `finder` role at all is a perfectly runnable review once
    # somebody says which entry to start from. Everything downstream — the
    # chain, the budget, the extra passes' fallback — reads `finder`, so from
    # here the run is shaped exactly as any other however it was chosen.
    client_family = routing.resolve_client_family(client_family)
    finder, route_meta = resolve_review_head(
        cfg, store, requested=requested, client_family=client_family,
        avoid_providers=avoid_providers)
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
    # The ROUTING POOL joins the graph too, and for exactly the reason the
    # comment above gives for fallbacks: an entry the router may choose is an
    # entry this run may execute, so a typo on one has to be refused here. Left
    # out, it would instead be silently ROUTED AROUND -- `provider_loads` marks
    # a provider with no adapter unavailable -- and the config error would then
    # surface only on the runs where every other provider happened to be busy.
    # A misconfiguration found by luck of the load is not found at all.
    #
    # Only when the router could actually have run: `requested is None` AND
    # mode auto. A PINNED run never consults the pool, so a pooled entry is not
    # a reviewer THAT run may reach for, and refusing it would let an unrelated
    # typo break the one request that is supposed to be absolute in every mode.
    # Mode off is the same argument -- nothing can reach the pool, and refusing
    # would refuse configs that worked before S5.
    pool = (routing.resolve_pool(cfg)
            if requested is None and cfg.routing.mode == "auto" else ())
    for reviewer in (finder, *pool, *(_pass_reviewer(cfg, p, finder)
                                      for p in _EXTRA_PASS_ROLES)):
        for entry in _chain_for(cfg, reviewer):
            _adapter_for(entry)

    # --- 1b. entire finder chain known unavailable → fail fast (S3) -------
    # Before spending any admission / lock wait budget: if every entry the
    # finder chain could hop to is cached-unavailable, the wait cannot produce
    # inference. Extra-pass roles are not required for this short-circuit.
    unavailable = _finder_chain_unavailable(store, cfg, finder)
    if unavailable is not None:
        raise PreflightRefused(unavailable)

    _checkpoint(cancel, None, "before review readiness")

    # Static readiness is deliberately before stale recovery, the foreground
    # lock, and review-fg admission. It is a snapshot only: the authoritative
    # diff and pass plan are still captured under the lock below, and chain
    # execution re-checks provider state at every entry boundary. The check
    # never probes a model or acquires capacity, so a known missing binary,
    # static API-key prerequisite, or scheduled hard pass cannot spend the
    # operator's queue budget merely to report an impossible topology.
    from . import readiness
    ready = readiness.check(
        store, repo, cfg, requested=requested, client_family=client_family)
    if not ready.ready:
        raise PreflightRefused(
            f"review readiness {ready.reason_code}: {ready.reason}; no review ran")

    # --- 2. sweep the wreckage of any SIGKILLed predecessor ---------------
    recover_stale(store, cfg)

    # --- 2b. THE envelope this review's finder may fill --------------------
    # Resolved once, here, and it is the only one any of the sizing below reads
    # — the batch plan, the context headroom, the prompt build, and the notes
    # that quote a number at the operator. `budget.prompt_budget` is where "the
    # global, or this entry's own override, capped by what its CLI can
    # physically take" is decided; nothing downstream re-derives it from
    # `d.max_diff_bytes`. The extra passes get their own, from the same helper,
    # because they may run on a different reviewer entirely.
    mdb = budget.prompt_budget(d, finder)

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
    estimate = _estimate_batch_count(repo, d, finder)
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
    # TWO numbers, not one, and they are equal only when the operator has
    # overridden nothing. `stale` is how long THIS process is willing to wait
    # before reclaiming from someone else -- an operator's `SKODUN_LOCK_STALE_
    # SECONDS` belongs there and nowhere else. `budget_sec` is what this holder
    # ADVERTISES it legitimately needs, which is a fact about its own batch plan
    # and about nothing the operator typed. Letting the override supply both
    # published a budget smaller than this holder's plan for the entire window
    # between `mkdir` and the under-lock republish below, and a small-diff
    # waiter reading it inside that window would reclaim a live batched holder:
    # the exact overlap the sidecar exists to prevent.
    #
    # --- 3b. review-fg capacity (FIFO) + optional legacy dual-hold (S3/S4) --
    # Store FIFO orders skodun waiters. Default dual-hold still takes the
    # legacy mkdir lock (tubescribes shadow). SKODUN_LEGACY_FG_LOCK=0 drops
    # the mkdir half so capacity N can truly run N concurrent FG reviews.
    common_dir = gitio.git_common_dir(repo)
    scope = str(common_dir)
    admission_wait = capacity.admission_wait_from_env(wait)
    # Shared admit+bind wall-clock deadline: review-fg wait + provider waits
    # and hops consume the same budget (S4). Not reset per hop or phase.
    admission_deadline = time.monotonic() + float(admission_wait)
    cap_n = capacity.capacity_from_env()
    dual_hold = capacity.legacy_fg_lock_from_env()
    lock_cell: dict = {"lock": None}
    capacity_ticket: capacity.Ticket | None = None

    # BEFORE the lock, on purpose. Reading it shells out to git twice on a cold
    # cache, and the record that needs it is built deep inside the critical
    # section -- so a wedged git (a network checkout, a stuck index lock) would
    # spend its whole timeout budget holding the foreground lock, delaying every
    # peer for work that has nothing to do with the review. It is ~27ms warm and
    # cached for the rest of the process, so the call site below is free.
    provenance.code_provenance()

    def _try_fg_lock(slice_sec: float) -> bool:
        """One dual-hold attempt: True when this process holds the FG lock."""
        try:
            # `_acquire_fg_lock` is spied on by name in the suite; keep calling
            # it here so those tests still drive the real lock path.
            held = _acquire_fg_lock(
                common_dir, root,
                wait=float(slice_sec),
                poll=min(poll, max(float(slice_sec), 0.01)),
                stale=stale,
                budget_sec=ceiling,
                **_cancel_kw(cancel))
        except LockTimeout:
            return False
        except ReviewCancelled as e:
            raise capacity.AdmissionCancelled(str(e)) from e
        lock_cell["lock"] = held
        return True

    try:
        if dual_hold:
            capacity_ticket = capacity.acquire_for_fg(
                store, scope=scope, capacity=cap_n,
                wait_sec=admission_wait, poll_sec=poll,
                stale_sec=stale,
                cancel=cancel, on_progress=_note, try_lock=_try_fg_lock)
        else:
            # Multi-slot path: store capacity only (S4 dual-hold off).
            capacity_ticket = capacity.acquire_for_fg(
                store, scope=scope, capacity=cap_n,
                wait_sec=admission_wait, poll_sec=poll,
                stale_sec=stale,
                cancel=cancel, on_progress=_note, try_lock=None)
    except capacity.AdmissionTimeout as e:
        if lock_cell["lock"] is not None:
            _release_fg_lock(lock_cell["lock"])
            lock_cell["lock"] = None
        raise LockTimeout(str(e)) from e
    except capacity.AdmissionCancelled as e:
        if lock_cell["lock"] is not None:
            _release_fg_lock(lock_cell["lock"])
            lock_cell["lock"] = None
        raise ReviewCancelled(str(e)) from e
    except BaseException:
        if lock_cell["lock"] is not None:
            _release_fg_lock(lock_cell["lock"])
            lock_cell["lock"] = None
        raise
    lock = lock_cell["lock"]
    if dual_hold and lock is None:
        # Dual-hold must return with a held mkdir lock.
        if capacity_ticket is not None:
            capacity.finish(store, capacity_ticket,
                            status=capacity.STATUS_REJECTED,
                            expire_reason="lock_missing")
        raise LockTimeout(
            "review-fg admission succeeded without the foreground lock; "
            f"re-run or remove {common_dir / LOCK_NAME} if it is wedged")

    rid = _new_id(id_prefix)
    # Always have a token once under the lock: cancel-by-id (S1) needs one even
    # when the CLI path did not pass an Event. Callers that already supplied one
    # keep it so MCP disconnect and tests still share the same object.
    if cancel is None:
        cancel = threading.Event()
    register_cancel(rid, cancel)
    # Attach the review id to capacity telemetry once known.
    if capacity_ticket is not None:
        try:
            capacity.mark_started(store, capacity_ticket, review_id=rid)
        except Exception:
            pass
    # SIGTERM sets the token (worker path already does this via dispatch). A bare
    # process death would orphan the provider group and leave a `running` row
    # until recover_stale; with a handler, review-cancel can signal this pid and
    # the existing cancel machinery demotes + releases the FG lock.
    previous_sigterm = _install_fg_sigterm(cancel)
    persisted = False
    finalized = False
    checkpoint_run: _CheckpointRun | None = None
    prepared_plan: _PreparedPlan | None = None
    try:
        # The first boundary INSIDE the lock. Everything above it left nothing
        # behind; from here the `finally` is what cleans up, so the check moves
        # inside the guard.
        _checkpoint(cancel, None, "before the diff was captured")
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
        tree_fingerprint = gitio.tree_fingerprint(repo, paths=diff.files)
        from .requests import validate_admitted
        validate_admitted(
            store, repo_id=scope, worktree_root=str(root.resolve()),
            branch=branch, head=head, base_sha=base.sha, diff_hash=diff_hash,
            tree_fingerprint=tree_fingerprint, cfg=cfg)
        stack_validation = None
        stack_prompt_context = b""
        stack_prompt_truncated = False
        if stack_request is not None:
            stack_validation = stack.validate(
                stack_request, repo=root, certification_base=base.sha,
                current_head=head, full_diff=diff,
                full_tree_fingerprint=tree_fingerprint,
                untracked_max=d.untracked_max)
            stack_prompt_context, stack_prompt_truncated = stack.render_prompt_context(
                stack_validation)

        # STAGE TWO of the two-stage ordering: the AUTHORITATIVE batch plan,
        # built from the capture above — the only diff this review persists
        # anything about. The pre-lock estimate sized the lock and nothing else,
        # and a long wait can change the worktree, so if this plan needs MORE
        # batches the lock's published budget is raised to match. It is never
        # lowered: the only thing riding on that number is a peer not reclaiming
        # a lock whose holder is still running.
        plan = batch_plan(diff.data, d, finder)
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

        review_started_at = _iso_now()
        if (stack_validation is not None
                and stack_validation.status == "valid"
                and stack_validation.manifest is not None):
            lineage_repository_id = stack_validation.manifest.repository_id
        else:
            try:
                lineage_repository_id = (
                    gitio.canonical_repository_identity(root) or "unknown")
            except Exception:
                lineage_repository_id = "unknown"
        lineage_context_diagnostics: dict = {}
        lineage_prompt_context, lineage_prompt_truncated = _lineage_prompt_context(
            store, lineage_repository_id, before=review_started_at,
            changed_paths=diff.files, owner_ids=_lineage_owner_ids(stack_validation),
            diagnostics=lineage_context_diagnostics)
        evidence_prompt_context = _evidence_prompt_context(
            store, root, base.sha, head, diff_hash)
        common = dict(
            id=rid, reviewed_at=review_started_at,
            review_started_at=review_started_at,
            source="skodun",
            branch=branch, head=head, base_ref=base.ref, base_sha=base.sha,
            diff_hash=diff_hash, mode=mode, model=finder.model,
            tree_fingerprint=tree_fingerprint, checklist_hash="",
            security_policy_hash=reuse.security_policy_identity(cfg),
            adapter=adapter.name, timeout_seconds=d.timeout_sec,
            max_turns=d.max_turns,
            # WHICH SKODUN ASKED. `adapter`/`model` name who answered and
            # `route_reason` names how the head was chosen; without this, the
            # one thing the record cannot say is which code reached that
            # verdict. It matters because the gate honours a review across
            # time: a change in how skodun classifies is otherwise invisible
            # in the records it left behind (#110).
            **provenance.code_provenance(),
            # WHO WAS ASKED FOR, which is not the same question as who
            # answered. `adapter`/`model` and the `attempts[]` provenance are
            # rewritten by `_apply` to name whoever actually served, so after a
            # fallback they name a provider nobody requested — and without this
            # field nothing on the artifact would distinguish "this run was
            # explicitly routed to entry X" from "the config's finder happens to
            # be X today". Explicit `None` rather than an absent key: absence
            # would be indistinguishable from a record written before the field
            # existed. The NAME, not the provider id: the name is what was
            # asked for, and it is what would have to be typed again to
            # reproduce the run.
            #
            # Three more fields travel with it (S5), and together they are the
            # whole audit of head selection: `routed_reviewer` (the entry that
            # actually headed the chain — equal to `requested_reviewer` on a
            # pin, and the router's pick otherwise), `route_reason` (which rule
            # produced it: `pinned`, `config-finder`, `auto:free+cross`, …) and
            # `client_family` (what the caller declared itself to be, recorded
            # whether or not it tipped anything). Artifact fields only: they are
            # serialized into `artifact_json` with the rest of the record, so
            # nothing here needs a `SCHEMA_VERSION` bump.
            **route_meta,
            # The budget for THIS review's own shape, persisted on the record so
            # `recover_stale` never has to recompute it from a config that may
            # since have changed — and never sweeps a live multi-batch run at
            # the single-review ceiling.
            worst_runtime_sec=budget.worst_runtime(d, width, planned),
            # WHICH TREE this record is about. The shared git dir, so a repo
            # and all of its linked worktrees agree on one value -- and so a
            # scoped reader cannot tell two checkouts of the same repository
            # apart, which is the intended equivalence.
            repo=scope,
            repo_id=scope,
            lineage_repository_id=lineage_repository_id,
            stack_context_bytes=len(stack_prompt_context),
            stack_context_truncated=stack_prompt_truncated,
            lineage_context_bytes=len(lineage_prompt_context),
            lineage_context_diagnostics=lineage_context_diagnostics,
            lineage_context_truncated=lineage_prompt_truncated,
            worktree_root=str(root),
            # Process identity for cancel-by-id (S1). Background workers already
            # attach a pid via the reservation lease; foreground rows need it
            # too so a peer can SIGTERM a live holder or demote a dead one.
            pid=os.getpid(),
        )
        if stack_validation is not None:
            common.update(
                coverage_scope="certification_full", gate_eligible=True,
                stack=stack_validation.to_dict())

        # THE PRE-PERSISTENCE BOUNDARY, and it covers all three of the persist
        # sites below (the empty-diff record, the no-batches record, and the
        # `running` save the reviewed paths start from). A cancellation here
        # leaves NO record: the `finally` only demotes what was persisted, which
        # is the honest answer for content whose review never started.
        _checkpoint(cancel, None, "before anything was recorded")

        if diff.data.rstrip(b"\n") == b"":
            # ORACLE PARITY: `--now` with nothing outgoing prints a clean
            # verdict rather than spending a model call on an empty diff. It is
            # recorded so the run leaves a trace, and it certifies nothing the
            # gate does not already grant: the gate PASSes an empty change
            # before it ever looks a review up.
            empty_identity = reuse._identity_for(
                repo, cfg, base, diff, branch=branch,
                reviewer_name=finder.name)
            _note("no outgoing changes vs " + (base.ref or "HEAD^"))
            rec = dict(common, status="clean", parse_ok=True, degraded=False,
                       degraded_reason="", stop_reason=None,
                       diff_truncated=False,
                       context_hash=(empty_identity.context_hash
                                     if empty_identity.context_hash is not None
                                     else ""),
                       checklist_hash=empty_identity.checklist_hash or "",
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
            return stored

        # --- 5. the security hold, decided BEFORE anything is persisted ---
        hold_for_security = passes.should_run_security(
            mode, diff.files, d.security_path_segments,
            d.security_basename_patterns)

        if plan is not None:
            # ORACLE's own stderr line for this branch: an oversized diff is
            # reviewed in pieces rather than truncated, and the operator is told
            # how many pieces and whether a cross-file pass follows.
            _note(f"diff is {len(diff.data)} bytes (> {mdb}); "
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
                prepared_plan = _prepare_batch_plan(
                    diff, batches=plan, cfg=cfg, d=d, root=root,
                    finder=finder, branch=branch, base_ref=base.ref,
                    base_sha=base.sha, head_label=f"{head} (working tree)",
                    stack_context=stack_prompt_context,
                    stack_context_truncated=stack_prompt_truncated,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context)
                checkpoint_identity = _orchestration_identity(
                    rec, diff, prepared_plan, cfg=cfg, d=d, root=root,
                    finder=finder, branch=branch, head=head,
                    base_ref=base.ref, base_sha=base.sha,
                    tree_fingerprint=tree_fingerprint)
                checkpoint_run = _begin_checkpoint_run(
                    store, checkpoint_identity, rec,
                    resume=resume_checkpoints)
                rec["batch_orchestration_id"] = \
                    checkpoint_run.orchestration_id
                rec["batch_identity_digest"] = checkpoint_identity.digest()
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
                    head_label=f"{head} (working tree)", cancel=cancel,
                    stack_context=stack_prompt_context,
                    stack_context_truncated=stack_prompt_truncated,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context,
                    prepared_plan=prepared_plan,
                    checkpoint_run=checkpoint_run)
                contributing_providers = _contributing_providers(rec)
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
                        mdb, len(diff.data), packing=True)
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
                    diff.data, mdb, selection, pack_body,
                    stack_context=stack_prompt_context,
                    stack_context_truncated=stack_prompt_truncated,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context)

                # The first route happens before the diff and prompt exist so
                # it can participate in preflight. Once the shipped prompt is
                # rendered, run the same auto-router with its real size. This
                # closes the argv-bound candidate gap without moving routing
                # into the model-call path. A small safety margin covers the
                # candidate-specific head label and invocation framing.
                if (requested is None and cfg.routing.mode == "auto"):
                    reroute = routing.auto_route(
                        cfg, store, client_family=client_family,
                        excluded_providers=avoid_providers,
                        prompt_size=prompt.prompt_bytes + 256)
                    if (reroute is not None
                            and reroute.reviewer.name != finder.name):
                        candidate = reroute.reviewer
                        candidate_mdb = budget.prompt_budget(d, candidate)
                        candidate_plan = batch_plan(diff.data, d, candidate)
                        if candidate_plan is not None:
                            # This branch is already inside the unbatched
                            # orchestration path. A reviewer whose own
                            # envelope would require batches cannot be swapped
                            # in after planning; retain the initial route and
                            # let the normal chain handle it safely.
                            _note(
                                f"routing: keeping {finder.name}; {candidate.name} "
                                "requires a different batch plan")
                        else:
                            previous_mdb = mdb
                            finder = candidate
                            mdb = candidate_mdb
                            route_meta = {
                                "requested_reviewer": requested,
                                "routed_reviewer": finder.name,
                                "route_reason": reroute.reason,
                                "client_family": client_family,
                            }
                            routed_pool = quota_pool_for(finder)
                            if routed_pool != finder.provider:
                                route_meta["quota_pool"] = routed_pool
                            adapter = _adapter_for(finder)
                            common.pop("quota_pool", None)
                            common.update(model=finder.model, adapter=adapter.name,
                                          **route_meta)
                            if candidate_mdb != previous_mdb:
                                pack = None
                                pack_body = None
                                if d.context_pack:
                                    headroom = promptbuild.context_headroom(
                                        mdb, len(diff.data), packing=True)
                                    pack = contextpack.pack(
                                        root, diff.files, diff.statuses,
                                        headroom, pack_large_added=False)
                                    pack_body = pack.body
                            prompt = promptbuild.build(
                                branch, base.ref,
                                base.sha, f"{head} (working tree)",
                                diff.data, mdb, selection, pack_body,
                                stack_context=stack_prompt_context,
                                stack_context_truncated=stack_prompt_truncated,
                                lineage_context=lineage_prompt_context,
                                lineage_context_truncated=lineage_prompt_truncated,
                                evidence_context=evidence_prompt_context)
                if prompt.diff_truncated:
                    # Reachable only for a diff that is over the envelope and
                    # was NOT batched, i.e. one this build refused to split.
                    _note(f"diff is {len(diff.data)} bytes "
                          f"(> {mdb}); the prompt is truncated and "
                          f"this review cannot be trustworthy")

                rec = dict(
                    common,
                    context_hash=pack.sha256 if pack is not None else "",
                    checklist_hash=reuse.checklist_identity(selection),
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
                outcome = _run_chain(
                    finder, cfg, d, prompt.text, root, store,
                    scratch, "primary",
                    admission_deadline=admission_deadline,
                    **_cancel_kw(cancel))
                rec["attempts"] = outcome.attempts
                _apply(rec, outcome)
                # Whoever ACTUALLY answered, not whoever was asked: after a
                # fallback the finder's own entry may never have run, and "did a
                # second provider look at this?" is a question about the
                # answering provider.
                contributing_providers = ([outcome.accepted.get("provider")]
                                          if outcome.accepted is not None else None)

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

            # --- 9. the extra passes, still under the lock ----------------
            if hold_for_security:
                # Sized for the reviewer that will RUN this pass, which may not
                # be the finder and may not be on the finder's provider: a
                # `[reviewers]` entry with role `security` is preferred here
                # (see `_extra_pass`), and it can be a CLI with a tighter
                # ceiling. One `prompt_budget` call per pass, per reviewer.
                sec_reviewer = _pass_reviewer(cfg, "security", finder)
                rec = _extra_pass(
                    rec, "security",
                    lambda: passes.security_prompt(
                        branch, base.ref, base.sha, f"{head} (working tree)",
                        diff.data, budget.prompt_budget(d, sec_reviewer),
                        d.security_prompt_slots),
                    sec_reviewer, cfg, d, root,
                    store, scratch, cancel=cancel)

            if passes.should_run_skeptic(
                    mode,
                    is_trustworthy(rec["parse_ok"], rec["degraded"],
                                   rec["diff_truncated"]),
                    rec["findings_total"]):
                skeptic_reviewer = _pass_reviewer(cfg, "skeptic", finder)
                rec = _extra_pass(
                    rec, "skeptic",
                    lambda: passes.skeptic_prompt(
                        branch, base.ref, base.sha, f"{head} (working tree)",
                        diff.data, budget.prompt_budget(d, skeptic_reviewer)),
                    skeptic_reviewer, cfg, d, root,
                    store, scratch, cancel=cancel)

            # --- 10. the refuter: a DIFFERENT provider re-examines the
            # finder's findings. It EXECUTES last, so the published record is
            # complete in one write and the banner is printed once — but every
            # input it uses is the snapshot above, not the record it is about
            # to annotate. No fail-closed hold: a refuter that could not answer
            # is an absent annotation, never a demotion.
            run_refuter, skip_note = passes.refuter_decision(
                mode, finder_trustworthy, finder_findings_total, cfg)
            if run_refuter:
                ref_reviewer = _pass_reviewer(cfg, "refuter", finder)
                rec = _refuter_pass(
                    rec, finder_findings_total,
                    lambda selected: passes.refuter_prompt(
                        finder_findings, diff.data, branch, base.ref, base.sha,
                        f"{head} (working tree)",
                        budget.prompt_budget(d, selected)),
                    ref_reviewer, cfg, d, root,
                    store, scratch, contributing_providers, cancel=cancel)
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

        # --- 11. persist the final record and hand it back -----------------
        if stack_validation is not None:
            rec["findings"] = stack.classify_findings(
                rec.get("findings", []), stack_validation)
            rec["findings_total"] = len(rec["findings"])

        rec["trustworthy"] = is_trustworthy(
            rec["parse_ok"], rec["degraded"], rec["diff_truncated"])
        rec["status"] = _status_for(rec)
        # THE LAST BOUNDARY THIS FUNCTION OWNS, and it is not decoration: the
        # record above carries clean axes, and `save_review` recomputes trust
        # from those axes ALONE -- so a token set during the write-up would
        # otherwise be committed as a trustworthy review of content the model
        # was killed halfway through looking at. `persisted and not finalized`
        # sends it to the `finally`, which demotes the row it saved.
        _checkpoint(cancel, rec, "after the review, before it was recorded")
        if checkpoint_run is not None:
            mismatch = _revalidate_foreground_orchestration(
                checkpoint_run.identity, rec, repo=repo, cfg=cfg, d=d,
                finder=finder, stack_context=stack_prompt_context,
                stack_context_truncated=stack_prompt_truncated,
                lineage_context=lineage_prompt_context,
                lineage_context_truncated=lineage_prompt_truncated)
            if mismatch is not None:
                try:
                    store.record_orchestration_mismatch(
                        checkpoint_run.orchestration_id, mismatch,
                        at=_iso_now())
                except Exception:
                    pass
                raise CheckpointClaimLost(
                    f"repository or checkpoint identity moved before "
                    f"finalization ({mismatch}); no aggregate was published")
            try:
                stored = store.save_checkpointed_review(
                    rec, lineage_annotator=annotate_lineage)
            except Exception as exc:
                raise PersistenceFailed(
                    f"could not atomically finalize batch checkpoints: "
                    f"{exc!r}") from exc
            if stored is None:
                raise PersistenceFailed(
                    f"review {rec['id']} was not readable after checkpoint "
                    "finalization")
        else:
            stored = _persist(store, rec)
        finalized = True
        if runner._cancelled(cancel):
            # STEP 8's foreground twin: THE POST-COMMIT LINEARIZATION CHECK. The
            # checkpoint above injects BEFORE the store call and therefore cannot
            # see a token set while SQLite held the write lock -- and that token
            # would otherwise leave a trustworthy record for a review that was
            # cancelled. `mark_cancelled` is `cancellation_transform` as one
            # atomic statement (its docstring says so), guarded on
            # `trustworthy=1`, so it demotes exactly the rows that need it and
            # leaves the findings alone.
            if store.mark_cancelled(rid, "cancelled during finalization"):
                _note("cancelled during finalization; the committed record was "
                      "demoted")
            raise ReviewCancelled(
                "the review was cancelled during finalization", partial=stored)
        return stored
    finally:
        # --- 12. never leave a `running` record or a held lock behind -----
        if persisted and not finalized:
            try:
                # UNCONDITIONAL, and it has to be: `_persist` autocommits the
                # final save BEFORE its readback, so a readback failure lands
                # here with a record that is already `clean` and
                # `trustworthy=1`. A `running`-guarded transition would leave
                # that row certifying a review nobody could read back -- the
                # stale-recovery bug, one call site over. `mark_failed` demotes
                # the status AND the trust axes in one statement.
                # When the cancel token is set, name the demotion for S1's
                # report vocabulary (`cancelled`); a crash unfinished stays
                # plain failed.
                reason = (UNFINISHED_CANCEL_REASON
                          if runner._cancelled(cancel) else UNFINISHED_REASON)
                store.mark_failed(rid, reason)
            except Exception:
                pass   # the crash that got us here is the story, not this
        unregister_cancel(rid)
        _restore_fg_sigterm(previous_sigterm)
        if capacity_ticket is not None and capacity_ticket.status in (
                capacity.STATUS_QUEUED, capacity.STATUS_ADMITTED,
                capacity.STATUS_RUNNING):
            try:
                reason = (capacity.REASON_CANCELLED
                          if runner._cancelled(cancel) else None)
                status = (capacity.STATUS_REJECTED if reason
                          else capacity.STATUS_RELEASED)
                capacity.finish(store, capacity_ticket, status=status,
                                expire_reason=reason)
            except Exception:
                pass
        if lock is not None:
            _release_fg_lock(lock)


def _finder_chain_unavailable(store: Store, cfg: Config,
                              finder: Reviewer) -> str | None:
    """Reason string if every finder-chain provider is cached-unavailable.

    Returns None when at least one entry is free of an active provider_state
    TTL (or the chain is empty, which preflight already refused elsewhere).
    """
    chain_entries = _chain_for(cfg, finder)
    if not chain_entries:
        return None
    now = _iso_now()
    parts: list[str] = []
    for entry in chain_entries:
        pool = quota_pool_for(entry)
        if pool == entry.provider:
            reason = store.provider_unavailable_reason(entry.provider, now)
        else:
            reason = store.provider_unavailable_reason(
                entry.provider, now, quota_pool=pool)
        if reason is None:
            return None
        parts.append(f"{entry.provider}: {reason}")
    return (
        "entire finder provider chain is known unavailable "
        f"({'; '.join(parts)}); no review ran")


#: Returned by ``_install_fg_sigterm`` when install is impossible. Distinct
#: from ``None`` (a valid previous disposition from ``signal.signal``).
_SIGTERM_INSTALL_FAILED = object()


def _install_fg_sigterm(cancel: "threading.Event"):
    """Make SIGTERM set the FG cancel token. Same posture as the worker.

    Returns the previous handler, or ``_SIGTERM_INSTALL_FAILED`` when install
    is impossible (not the main thread). Restored in `run_review`'s finally so
    a long-lived process (MCP) does not keep a review-scoped handler after the
    review ends.
    """
    import signal

    def handler(signum, frame):        # pragma: no cover - driven by a signal
        from .request_cancel import mark_event
        mark_event(cancel, "signal")
    try:
        return signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, RuntimeError):
        return _SIGTERM_INSTALL_FAILED


def _restore_fg_sigterm(previous) -> None:
    if previous is _SIGTERM_INSTALL_FAILED:
        return
    import signal
    try:
        signal.signal(signal.SIGTERM,
                      signal.SIG_DFL if previous is None else previous)
    except (ValueError, OSError, RuntimeError):
        pass


#: `runner.ReviewCancelled`, aliased under this module's name. Defined in
#: `runner` because that is the lowest layer that raises it (its watchdog tick
#: loop is the only place holding the provider's pgid); aliased here because
#: every OTHER raiser and the only catcher are pipeline-level. NOT a subclass of
#: `PipelineError`: it must stay outside `Exception` so the `except Exception`
#: demote-don't-destroy guards cannot turn a killed run into a degraded review.
ReviewCancelled = runner.ReviewCancelled


def _checkpoint(cancel: "threading.Event | None", partial: dict | None,
                where: str) -> None:
    """Raise `ReviewCancelled` if the token is set. THE pass-boundary check.

    `partial` is the record as it stands, attached to the exception so the worker
    can finalize the findings that were already produced rather than throwing
    them away. `None` is honest for a boundary with no record yet.
    """
    if runner._cancelled(cancel):
        raise ReviewCancelled(
            f"the review was cancelled {where}", partial=partial)


def cancellation_transform(rec: dict, boundary: str) -> dict:
    """A cancelled review's record, DEMOTED. The one definition of that.

    Called by the worker on all three of its cancellation paths: a partial that
    came out of `run_prepush_review`, a token set between its return and the
    finalize, and the post-commit linearization check.

    THE FOREGROUND applies the same transform, but through the store rather than
    through this function, and the difference is which one is atomic. A cancelled
    `--now` review has a row already committed by the time it needs demoting, so
    it goes through `store.mark_failed` (the shipped `finally`) or
    `store.mark_cancelled` (the post-commit check) — and `mark_cancelled` IS this
    transform written as one UPDATE, with the same three rules and the same
    reasons. Re-deriving the dict here and re-saving it would be a second write
    of a record the store has already accepted, racing nothing and buying
    nothing.

    It is one function because the transform is not obvious and getting it wrong
    is silent:

    * `findings` and `usable_output` are PRESERVED. A round cancelled after two
      batches answered really did produce those findings, and a surface that
      threw them away would print "NO REVIEW HAPPENED" over real evidence.
    * `degraded=True` with a `degraded_reason` naming the boundary is what makes
      the record untrustworthy. Setting only `status`/`failure_reason` would
      NOT: `finalize_review` recomputes `trustworthy` from the three axes alone,
      so a cancelled round with clean axes would be stored as a trustworthy one
      and would satisfy both the gate and dedup.
    * `parse_ok` is left ALONE. It says whether the reviewer's output parsed,
      which is a fact about the output and not about the cancellation; the
      demotion belongs on the axis that means "this round is incomplete".

    Returns a copy; the caller's dict is not mutated.
    """
    out = dict(rec)
    reason = f"cancelled: {boundary}"
    out["degraded"] = True
    existing = str(out.get("degraded_reason") or "")
    out["degraded_reason"] = f"{existing}; {reason}" if existing else reason
    out["status"] = "failed"
    prior = str(out.get("failure_reason") or "")
    out["failure_reason"] = f"{prior}; {reason}" if prior else reason
    out["trustworthy"] = is_trustworthy(
        out.get("parse_ok"), True, out.get("diff_truncated"))
    return out


def run_prepush_review(store: Store, repo: Path, record_id: str, branch: str,
                       local_oid: str, base, diff, d: Defaults, cfg: Config,
                       *, cancel: "threading.Event | None" = None) -> dict:
    """Review a PUSHED ref for an already-reserved record. Persists NOTHING.

    The background half of `run_review`, and the list of what it does NOT do is
    the interface: no foreground lock (the reservation lease already serialised
    this branch, and a detached worker must not block on a human's review), no
    primary-checkout refusal (the pushed commit is read from the object database,
    so the checkout is irrelevant — that refusal exists to stop a model session
    bound to the main checkout from reviewing the wrong tree), no id generation
    (`store.reserve_prepush` minted it), and no persistence at all. The caller —
    the worker, and only the worker — performs the single conditional
    `store.finalize_review`, which is what lets a superseded reservation refuse
    a late worker's answer.

    `d` is the EFFECTIVE defaults the worker built
    (`replace(cfg.defaults, timeout_sec=dispatch.timeout_sec,
    timeout_retries=dispatch.timeout_retries)`); `cfg.defaults` is still read for
    the FOREGROUND cap that a large prompt escalates to (see `_escalated`).

    Reservation-owned fields are carried through untouched — `id`, `branch`,
    `head`, `base_ref`, `base_sha`, `diff_hash`, `mode`, `worst_runtime_sec`,
    `pid`, `repo` — because `finalize_review` refuses a record whose identity
    disagrees with the stored row rather than overwriting it.
    `worst_runtime_sec`, `pid` and `repo` are read back off the reserved row
    rather than taken as parameters: they are DATABASE-owned, so the database is
    where their values come from.

    `reviewed_at` is likewise the RESERVATION's timestamp, not this function's.
    It answers "when was this content pushed", which is what orders the dedup
    candidate query and the log; letting it jump forward by the review's own
    duration would reorder a record relative to the supersede that retired its
    predecessor.

    Cancellation: the token is checked at every pass boundary and immediately
    before returning, and `ReviewCancelled` carries the record as it stands.
    This function CANNOT do the final check itself — it persists nothing, so
    there is nothing for a check here to protect; the worker holds the last one,
    immediately before `finalize_review`.
    """
    repo = Path(repo)
    # The same normalisation `run_review` applies, and for the same reason: the
    # checklist directory, the rules registry and the context packer are all
    # resolved against the worktree root, and a hook invoked from a subdirectory
    # would otherwise select a different checklist than a foreground review of
    # the same change.
    root = gitio._worktree_root(repo)
    try:
        lineage_repository_id = (
            gitio.canonical_repository_identity(root) or "unknown")
    except Exception:
        lineage_repository_id = "unknown"

    finder = _reviewer_for(cfg, "finder")
    if finder is None:
        raise PreflightRefused(
            "no enabled reviewer with role 'finder' is configured; no review ran")
    adapter = _adapter_for(finder)
    # Every reviewer this run may reach for, resolved before any model call. Only
    # the integration pass can run in `prepush` mode (`should_run_security`,
    # `should_run_skeptic` and `refuter_decision` are all gated on `mode ==
    # "now"`), so the graph is the finder's chain plus the integrator's.
    for reviewer in (finder, _pass_reviewer(cfg, passes.INTEGRATION_PASS, finder)):
        for entry in _chain_for(cfg, reviewer):
            _adapter_for(entry)

    reserved = store.get_review(record_id) or {}
    review_started_at = (
        reserved.get("review_started_at") or reserved.get("reviewed_at"))
    lineage_context_diagnostics: dict = {}
    lineage_prompt_context, lineage_prompt_truncated = _lineage_prompt_context(
        store, lineage_repository_id, before=review_started_at,
        changed_paths=diff.files, diagnostics=lineage_context_diagnostics)
    diff_hash = gitio.diff_identity(diff.data)
    evidence_prompt_context = _evidence_prompt_context(
        store, root, base.sha, local_oid, diff_hash)
    common = dict(
        id=record_id, reviewed_at=reserved.get("reviewed_at") or _iso_now(),
        source="skodun", branch=branch, head=local_oid, base_ref=base.ref,
        base_sha=base.sha, diff_hash=diff_hash, mode="prepush",
        model=finder.model, adapter=adapter.name, timeout_seconds=d.timeout_sec,
        max_turns=d.max_turns,
        # The background surface records it too: a pre-push verdict is read by
        # the same gate and is exactly as long-lived.
        **provenance.code_provenance(),
        worst_runtime_sec=reserved.get("worst_runtime_sec"),
        pid=reserved.get("pid"),
        # FROM THE RESERVATION, not from a fresh git call. `finalize_review`
        # binds every column from this dict and merges only `pid` and
        # `superseded_by` back, so omitting `repo` here writes NULL over the
        # value the reservation persisted -- at the exact moment the round
        # becomes deliverable, and background rounds are the only kind
        # `surface` delivers. Read off the reserved row for the same reason
        # `worst_runtime_sec` is: it is a fact about the reservation, and a
        # worker recomputing it could disagree with the row it is finalizing.
        repo=reserved.get("repo"),
        repo_id=reserved.get("repo_id") or reserved.get("repo"),
        lineage_repository_id=lineage_repository_id,
        lineage_context_bytes=len(lineage_prompt_context),
        lineage_context_diagnostics=lineage_context_diagnostics,
        lineage_context_truncated=lineage_prompt_truncated,
        worktree_root=str(root),
        review_started_at=review_started_at,
    )
    #: `(threshold, foreground cap)`, or None when the two caps coincide.
    large_prompt = (cfg.dispatch.large_prompt_bytes, cfg.defaults.timeout_sec)

    rec: dict | None = None
    try:
        if diff.data.rstrip(b"\n") == b"":
            # FAIL-CLOSED, unlike `--now`'s clean verdict for an empty diff. The
            # dispatcher never reserves an empty ref diff (it skips those with a
            # note and no record), so reaching here means the ref moved between
            # the capture and the worker in a way the identity check did not
            # catch — and an empty prompt could mint a clean verdict for content
            # nobody looked at.
            return _prepush_record(
                common, status="failed", parse_ok=False, usable_output=False,
                trustworthy=False,
                failure_reason="the pushed ref has no outgoing changes to review")

        plan = batch_plan(diff.data, d, finder)
        planned = 0 if plan is None else len(plan)
        if plan is not None:
            _note(f"diff is {len(diff.data)} bytes "
                  f"(> {budget.prompt_budget(d, finder)}); "
                  f"reviewing {len(diff.files)} file(s) in {planned} "
                  f"deterministic batch(es) + "
                  f"{int(passes.should_run_integration(planned))} "
                  f"integration pass")
        if plan is not None and not plan:
            # Task 6's terminal rule: `batching.split` returns nothing only for
            # an EMPTY diff, so this is the shape that should not happen — and a
            # clean verdict for a diff nothing looked at is the alternative.
            _note("diff batching produced no batches; failing closed")
            return _prepush_record(
                common, status="failed", parse_ok=False,
                files_changed=list(diff.files), diff_bytes=len(diff.data),
                batched=True, batch_count=0, batches=[], usable_output=False,
                trustworthy=False,
                failure_reason="diff batching produced no batches")

        with tempfile.TemporaryDirectory(prefix="skodun-bg-") as tmp:
            scratch = Path(tmp)
            if plan is not None:
                rec = _prepush_record(
                    common, status="running", parse_ok=False,
                    files_changed=list(diff.files), diff_bytes=len(diff.data),
                    batched=True, batch_count=planned, batches=[],
                    usable_output=False)
                prepared_plan = _prepare_batch_plan(
                    diff, batches=plan, cfg=cfg, d=d, root=root,
                    finder=finder, branch=branch, base_ref=base.ref,
                    base_sha=base.sha, head_label=local_oid,
                    context_source="oid", context_oid=local_oid,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context)
                checkpoint_identity = _orchestration_identity(
                    rec, diff, prepared_plan, cfg=cfg, d=d, root=root,
                    finder=finder, branch=branch, head=local_oid,
                    base_ref=base.ref, base_sha=base.sha,
                    tree_fingerprint=local_oid)
                checkpoint_run = _begin_checkpoint_run(
                    store, checkpoint_identity, rec, resume=True)
                rec["batch_orchestration_id"] = \
                    checkpoint_run.orchestration_id
                rec["batch_identity_digest"] = checkpoint_identity.digest()
                _note(f"reviewing {len(diff.files)} file(s) vs {base.ref} as "
                      f"{record_id} in {planned} batch(es) ...")
                rec = _orchestrate(
                    rec, diff, batches=plan, cfg=cfg, d=d, root=root,
                    store=store, scratch=scratch, finder=finder, branch=branch,
                    base_ref=base.ref, base_sha=base.sha, head_label=local_oid,
                    context_source="oid", context_oid=local_oid,
                    large_prompt=large_prompt, cancel=cancel,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context,
                    prepared_plan=prepared_plan,
                    checkpoint_run=checkpoint_run)
            else:
                rec = _single_shot(
                    common, diff, cfg=cfg, d=d, root=root, store=store,
                    scratch=scratch, finder=finder, branch=branch, base=base,
                    local_oid=local_oid, large_prompt=large_prompt,
                    cancel=cancel, record_id=record_id,
                    lineage_context=lineage_prompt_context,
                    lineage_context_truncated=lineage_prompt_truncated,
                    evidence_context=evidence_prompt_context)

        rec["trustworthy"] = is_trustworthy(
            rec["parse_ok"], rec["degraded"], rec["diff_truncated"])
        rec["status"] = _status_for(rec)
        # The LAST boundary this function owns. A token set during the write-up
        # above must not produce a record that looks complete: the worker's own
        # pre-finalize check is the barrier that catches a signal landing after
        # this one, and the two together are what make the window closed.
        _checkpoint(cancel, rec, "after the review, before returning it")
        return rec
    except ReviewCancelled as exc:
        # A cancellation raised DEEPER than this function (the watchdog tick
        # loop, a chain entry boundary, a batch boundary) carries no partial: the
        # layers below do not know what record is being built. Attach the best
        # one we have so the worker's transform has findings to preserve.
        if exc.partial is None and rec is not None:
            exc.partial = rec
        raise


def _prepush_record(common: dict, **overrides) -> dict:
    """`common` + the empty shell + `overrides`, in that precedence order.

    A helper rather than `dict(common, **_empty_shell(), foo=...)` because that
    form raises `TypeError` the moment an override names a key the shell also
    carries -- which is most of them, and which is a crash inside a DETACHED
    worker whose only trace is a log file. Three layers, applied in order, so an
    override always simply wins.
    """
    rec = dict(common)
    rec.update(_empty_shell())
    rec.update(overrides)
    return rec


def _empty_shell() -> dict:
    """The fields every prepush record carries with nothing in them yet.

    A FUNCTION, not a module constant, because half of these values are mutable
    containers: a shared `findings=[]` or `severity={...}` would be the same
    object on every record this module builds, and one caller appending to it
    would edit records that were already returned.

    Shared at all because a record that OMITS one of these is a record whose
    readers — the banner, the gate, Task 12's delivery — have to guess, and the
    four construction sites would otherwise each omit a different subset.
    """
    return dict(
        degraded=False, degraded_reason="", stop_reason=None,
        diff_truncated=False, context_hash="", files_changed=[], diff_bytes=0,
        prompt_bytes=0, checklist_sections=[], checklist_bytes=0,
        checklist_note="", checklist_degraded=False, context_bytes=0,
        context_files=[], context_omitted_files=[], attempts=[], summary="",
        findings=[], findings_total=0,
        severity={"high": 0, "medium": 0, "low": 0}, rule_ids=[],
        extra_passes={}, failure_reason="",
    )


def _single_shot(common: dict, diff, *, cfg: Config, d: Defaults, root: Path,
                 store: Store, scratch: Path, finder: Reviewer, branch: str,
                 base, local_oid: str, large_prompt: tuple[int, int] | None,
                 cancel: "threading.Event | None", record_id: str,
                 stack_context: bytes | None = None,
                 stack_context_truncated: bool = False,
                 lineage_context: bytes | None = None,
                 lineage_context_truncated: bool = False,
                 evidence_context: bytes | None = None) -> dict:
    """One prompt, one chain, one record: the UNBATCHED background review.

    Deliberately mirrors `run_review`'s 6b branch, with the two differences a
    pushed ref forces:

    * the context pack reads the PUSHED COMMIT's tree (`source="oid"`), never
      the working tree — the checkout may be somewhere else entirely, and
      packing it would certify content nobody pushed;
    * the head LABEL is the pushed oid, where the foreground says
      `"<sha> (working tree)"`. The oracle passes `$LOCAL_OID` here for the same
      reason.

    `usable_output` is the accepted attempt's existence, which is exactly "did
    this round produce a parseable answer" — never the finding count, because a
    clean round and a round that produced nothing both have zero findings.
    """
    selection = checklist.select(
        diff.files, "full", _under(root, d.checklist_dir),
        _under(root, d.rules_json), d.checklist_map, d.test_path_patterns)
    if selection.note:
        kind = ("cross-file rules unavailable" if selection.degraded
                else "path-scoped rules dropped")
        _note(f"checklist: {kind} -- {selection.note}")
    if selection.dropped:
        _note(f"checklist: dropped {', '.join(selection.dropped)} to fit the "
              f"{checklist.BUDGET}-byte injection budget")
    if selection.over_budget:
        _note(f"checklist: {selection.bytes_total} bytes still exceeds the "
              f"{checklist.BUDGET}-byte budget after eviction; only undroppable "
              f"sections remain")

    # The finder's OWN envelope, not the global: `dispatch.build_dedup_evidence`
    # computes its candidate hash from the same call with the same reviewer, and
    # a different headroom is a different identity for the same commit — nothing
    # would ever dedup-match again.
    mdb = budget.prompt_budget(d, finder)

    pack = None
    pack_body = None
    if d.context_pack:
        headroom = promptbuild.context_headroom(
            mdb, len(diff.data), packing=True)
        # `pack_large_added=False`: the single-shot diff already carries every
        # added file whole. This is the SAME call `dispatch.build_dedup_evidence`
        # makes for its candidate hash, and it has to stay that way — a different
        # headroom or a different large-added rule would be a different identity
        # for the same commit, so nothing would ever dedup-match again.
        pack = contextpack.pack(root, list(diff.files), dict(diff.statuses),
                                headroom, source="oid", oid=local_oid,
                                pack_large_added=False)
        pack_body = pack.body

    prompt = promptbuild.build(branch, base.ref, base.sha, local_oid, diff.data,
                              mdb, selection, pack_body,
                              stack_context=stack_context,
                              stack_context_truncated=stack_context_truncated,
                              lineage_context=lineage_context,
                              lineage_context_truncated=lineage_context_truncated,
                              evidence_context=evidence_context)
    if prompt.diff_truncated:
        _note(f"diff is {len(diff.data)} bytes (> {mdb}); the "
              f"prompt is truncated and this review cannot be trustworthy")

    rec = _prepush_record(
        common, status="running", parse_ok=False,
        context_hash=pack.sha256 if pack is not None else "",
        diff_truncated=prompt.diff_truncated,
        files_changed=list(diff.files), diff_bytes=len(diff.data),
        prompt_bytes=prompt.prompt_bytes,
        checklist_sections=list(selection.sections),
        checklist_bytes=selection.bytes_total,
        checklist_note=selection.note, checklist_degraded=selection.degraded,
        context_bytes=pack.bytes_total if pack is not None else 0,
        context_files=list(pack.included) if pack is not None else [],
        context_omitted_files=[f"{p} ({r})" for p, r in pack.omitted]
                              if pack is not None else [],
        usable_output=False,
    )
    _checkpoint(cancel, rec, "before the reviewer was invoked")
    _note(f"reviewing {len(diff.files)} file(s) vs {base.ref} as {record_id} ...")
    # Standalone chain budget: pre-push is not on the FG admit path, so
    # run_chain starts its own shared provider wait deadline (not reset per hop).
    outcome = _run_chain(finder, cfg,
                         _escalated(d, prompt.prompt_bytes, large_prompt),
                         prompt.text, root, store, scratch, "primary",
                         cancel=cancel)
    rec["attempts"] = outcome.attempts
    _apply(rec, outcome)
    rec["usable_output"] = outcome.accepted is not None
    return rec


def _under(root: Path, relative: str) -> Path:
    p = Path(relative)
    return p if p.is_absolute() else root / p


def _accepts_keyword(fn, name: str) -> bool:
    """True when `fn` can take `name` as a keyword argument."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    param = params.get(name)
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _save(store: Store, rec: dict, *, lineage_annotator=None) -> None:
    try:
        if lineage_annotator is None or not _accepts_keyword(
                store.save_review, "lineage_annotator"):
            store.save_review(rec)
        else:
            store.save_review(rec, lineage_annotator=lineage_annotator)
    except Exception as e:
        raise PersistenceFailed(f"could not record the review: {e!r}") from e


def _flatten_lineage_candidates(
        rec: dict, candidates, truncated: bool, limit: int
        ) -> tuple[list[dict], bool]:
    """Compatibility flatten of review artifacts into bounded finding rows."""
    previous: list[dict] = []
    repository_id = rec.get("lineage_repository_id") or "unknown"
    overflow = False
    for prior in candidates:
        if prior.get("id") == rec.get("id"):
            continue
        if prior.get("status") == "running":
            continue
        if (prior.get("lineage_repository_id") or "unknown") != repository_id:
            continue
        for prior_index, previous_finding in enumerate(prior.get("findings") or ()):
            if len(previous) >= limit:
                overflow = True
                break
            if isinstance(previous_finding, dict):
                enriched = dict(previous_finding)
                enriched["_lineage_review_id"] = prior.get("id")
                enriched["_lineage_finding_index"] = prior_index
                enriched["_lineage_reviewed_at"] = prior.get("reviewed_at")
                previous.append(enriched)
        if overflow:
            break
    return previous, bool(truncated) or overflow


def _lineage_prompt_context(
        store, repository_id: str, *, before: str | None,
        changed_paths=(), owner_ids=(), diagnostics: dict | None = None
        ) -> tuple[bytes, bool]:
    """Bounded relevant hints with independent retrieval and byte budgets."""
    meta = diagnostics if diagnostics is not None else {}
    meta.clear()
    meta.update(status="unknown", candidate_count=0, scanned_count=0,
                matched_count=0, selected_count=0, candidate_truncated=False,
                prompt_bytes_truncated=False)
    if not repository_id or repository_id == "unknown" or store is None:
        return b"", False
    try:
        from .fingerprint import (CANDIDATE_LIMIT, rank_prompt_candidates,
                                  render_prompt_context)
        if hasattr(store, "lineage_prompt_candidates"):
            rows, retrieval = store.lineage_prompt_candidates(
                repository_id, before_reviewed_at=before, limit=CANDIDATE_LIMIT)
            truncated = retrieval["truncated"]
            meta.update(scanned_count=retrieval["scanned_count"],
                        scan_truncated=retrieval["scan_truncated"],
                        disposition_scanned_count=retrieval["disposition_scanned_count"],
                        disposition_truncated=retrieval["disposition_truncated"])
        elif hasattr(store, "lineage_finding_candidates_with_meta"):
            rows, truncated = store.lineage_finding_candidates_with_meta(
                repository_id, before_reviewed_at=before, limit=CANDIDATE_LIMIT)
            meta["scanned_count"] = None  # legacy doubles cannot measure scans
        else:
            return b"", False
        selected, matched = rank_prompt_candidates(
            rows, changed_paths=changed_paths, owner_ids=owner_ids)
        text, text_truncated = render_prompt_context(selected)
        meta.update(status="partial" if (truncated or text_truncated) else "complete",
                    candidate_count=len(rows), matched_count=matched,
                    selected_count=len(selected), candidate_truncated=bool(truncated),
                    prompt_bytes_truncated=text_truncated)
        return text, bool(truncated) or text_truncated
    except Exception as exc:
        meta.update(status="unavailable", error=type(exc).__name__)
        return b"", False


def _lineage_owner_ids(stack_validation) -> tuple[str, ...]:
    """Only validated stack identities may influence advisory ranking."""
    if (stack_validation is None or stack_validation.status != "valid"
            or stack_validation.manifest is None):
        return ()
    manifest = stack_validation.manifest
    return (manifest.current_slice.slice_id,
            *(item.slice_id for item in manifest.dependencies))


def _evidence_prompt_context(store, root: Path, certification_base: str,
                             current_head: str, diff_hash: str) -> bytes:
    """Return exact-identity receipt summaries for the next model prompt."""
    try:
        repository_id = gitio.canonical_repository_identity(root)
        if not repository_id or "/" not in repository_id:
            return b""
        from .evidence import EvidenceIdentity
        from .profiles import compact_stored_receipt_context
        identity = EvidenceIdentity(
            repository_id=repository_id, worktree_root=str(root),
            certification_base=certification_base, current_head=current_head,
            diff_hash=diff_hash)
        rows = store.list_evidence_receipts(identity.digest, 32)
        if not rows:
            return b""
        return compact_stored_receipt_context(rows, identity.digest)
    except Exception:
        # Repository receipts are advisory context. An unreadable optional
        # projection must not spend a model call or alter fail-closed trust.
        return b""


def annotate_lineage(store: Store, rec: dict) -> dict:
    """Attach additive finding lineage before any terminal store write.

    The helper is shared by foreground, checkpointed, and detached worker
    publication.  Failure is visible on the artifact but never changes the
    authoritative trust or gate axes.
    """
    try:
        from .fingerprint import CANDIDATE_LIMIT, annotate_findings, finding_fingerprint
        repository_id = rec.get("lineage_repository_id") or "unknown"
        previous: list[dict] = []
        # An unknown canonical remote is deliberately not a lineage scope:
        # linking two unrelated local clones would be worse than a false
        # negative.  Stack manifests and normal remotes provide this value.
        truncated = False
        diagnostics = dict(status="unknown", exact_scanned=0, exact_matched=0,
                           exact_truncated=False, exact_scan_truncated=False,
                           exact_candidate_truncated=False, exact_key_truncated=False,
                           fallback_scanned=0, fallback_scan_truncated=False,
                           fallback_truncated=False, fallback_state="unknown",
                           exact_state="unknown", exact_key_limit=CANDIDATE_LIMIT,
                           exact_candidate_limit=2 * CANDIDATE_LIMIT,
                           fallback_candidate_limit=CANDIDATE_LIMIT,
                           raw_scan_limit=1024 + min((CANDIDATE_LIMIT + 1) * 4, 1024))
        exact = []
        incomplete_exact: set[str] = set()
        if repository_id != "unknown":
            if hasattr(store, "lineage_candidates_with_diagnostics"):
                diagnostics["exact_state"] = "indexed"
                digests = list(dict.fromkeys(
                    finding_fingerprint(item) for item in rec.get("findings") or ()
                    if isinstance(item, dict)))
                incomplete_exact.update(digests[CANDIDATE_LIMIT:])
                diagnostics["exact_key_truncated"] = len(digests) > CANDIDATE_LIMIT
                diagnostics["exact_truncated"] = diagnostics["exact_key_truncated"]
                for digest_index, digest in enumerate(digests[:CANDIDATE_LIMIT]):
                    remaining = 1024 - diagnostics["exact_scanned"]
                    if remaining <= 0:
                        diagnostics["exact_truncated"] = True
                        diagnostics["exact_scan_truncated"] = True
                        incomplete_exact.update(digests[digest_index:CANDIDATE_LIMIT])
                        break
                    matches, meta = store.lineage_candidates_with_diagnostics(
                        repository_id, before_reviewed_at=rec.get("reviewed_at"),
                        fingerprint=digest, limit=2, scan_limit=remaining)
                    exact.extend(matches)
                    if meta["truncated"] and len(matches) < 2:
                        incomplete_exact.add(digest)
                    diagnostics["exact_scanned"] += meta["scanned_count"]
                    diagnostics["exact_truncated"] |= meta["truncated"]
                    diagnostics["exact_scan_truncated"] |= meta["scan_truncated"]
                    diagnostics["exact_candidate_truncated"] |= meta["candidate_truncated"]
                diagnostics["exact_matched"] = len(exact)
                previous, meta = store.lineage_candidates_with_diagnostics(
                    repository_id, before_reviewed_at=rec.get("reviewed_at"),
                    limit=CANDIDATE_LIMIT)
                diagnostics.update(fallback_scanned=meta["scanned_count"],
                                   fallback_truncated=meta["truncated"],
                                   fallback_scan_truncated=meta["scan_truncated"],
                                   fallback_state="bounded_recency")
                truncated = meta["truncated"] or diagnostics["exact_truncated"]
                diagnostics["status"] = "partial" if truncated else "complete"
                # Only duplicate provenance rows are collapsed here. Distinct
                # occurrences retain the existing conservative ambiguity rule.
                unique = {}
                for item in exact + previous:
                    if item.get("_lineage_review_id") != rec.get("id"):
                        key = (item["_lineage_review_id"], item["_lineage_finding_index"])
                        unique.setdefault(key, item)
                previous = list(unique.values())
            elif hasattr(store, "lineage_finding_candidates_with_meta"):
                previous, truncated = store.lineage_finding_candidates_with_meta(
                    repository_id,
                    before_reviewed_at=rec.get("reviewed_at"),
                    limit=CANDIDATE_LIMIT)
                previous = [
                    item for item in previous
                    if item.get("_lineage_review_id") != rec.get("id")
                ]
            elif hasattr(store, "lineage_review_candidates_with_meta"):
                candidates, truncated = store.lineage_review_candidates_with_meta(
                    repository_id,
                    before_reviewed_at=rec.get("reviewed_at"),
                    limit=CANDIDATE_LIMIT)
                previous, truncated = _flatten_lineage_candidates(
                    rec, candidates, truncated, CANDIDATE_LIMIT)
            else:
                candidates = store.lineage_review_candidates(repository_id)
                previous, truncated = _flatten_lineage_candidates(
                    rec, candidates, False, CANDIDATE_LIMIT)
        rec["findings"] = annotate_findings(
            rec.get("findings") or (), previous, incomplete_exact=incomplete_exact)
        rec["fingerprint_status"] = "complete"
        diagnostics["candidate_count"] = len(previous)
        diagnostics["incomplete_exact_count"] = len(incomplete_exact)
        diagnostics["matched_count"] = sum(
            isinstance(item, dict)
            and item.get("finding_fingerprint_v2") not in incomplete_exact
            and item.get("finding_lineage_v2", {}).get("match_reason") != "new"
            for item in rec["findings"])
        rec["fingerprint_diagnostics"] = diagnostics
        rec["fingerprint_candidate_limit"] = CANDIDATE_LIMIT
        rec["fingerprint_candidate_count"] = len(previous)
        rec["fingerprint_candidates_truncated"] = bool(truncated)
        rec.pop("fingerprint_error", None)
    except Exception as exc:
        # A read-model enrichment failure must never change review trust or
        # block persistence of the authoritative artifact.  Persist only the
        # bounded exception type, never provider output or local paths.
        rec["fingerprint_status"] = "unavailable"
        rec["fingerprint_diagnostics"] = {"status": "unavailable"}
        rec["fingerprint_error"] = type(exc).__name__
    return rec


def _persist(store: Store, rec: dict) -> dict:
    """Save the record and return it as READ BACK OUT of the store.

    Read back rather than returned from memory for two reasons:
    `Store.save_review` computes `trustworthy` itself, and the banner must be
    rendered from exactly what the gate will later see. A record that cannot be
    read back was not recorded, whatever the write said.
    """
    _save(store, rec, lineage_annotator=annotate_lineage)
    stored = store.get_review(rec["id"])
    if stored is None:
        raise PersistenceFailed(
            f"review {rec['id']} was not readable back out of the store")
    return stored


#: `_emit_banner` USED TO LIVE HERE and is deliberately gone rather than left
#: unused. It printed `trust.banner(stored)` to stdout as `run_review`'s last
#: act, which made this module a writer on a stream it does not own: `skodun
#: mcp` serves JSON-RPC on stdout from a thread that may be mid-write while a
#: review finishes on another. The record is returned instead, and
#: `services.svc_review` renders the banner from it — one definition, one writer,
#: and the "banner comes from the persisted record" property is unchanged because
#: what is returned IS the read-back record.


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


def _batch_budget(d: Defaults, reviewer: Reviewer | None = None) -> int:
    """The per-batch DIFF budget an over-budget diff is split on.

    HALF the envelope when context packing is on, which is the oracle's own
    `_batch_diff_budget=$((GROK_BATCH_BYTES / 2))` (`GROK_BATCH_BYTES` defaults
    to `MAX_DIFF_BYTES`). The reason is arithmetic rather than taste: a batch
    filled to the whole envelope leaves `context_headroom` exactly zero, so
    batched context packing would be a silent no-op on precisely the reviews
    that need context most — each batch shows a slice of the change and the
    packer is the only thing that can show the rest of the file.

    The envelope is `budget.prompt_budget`'s, never `d.max_diff_bytes` read
    here: batches sized from the global number are batches the provider that
    will actually run them may be unable to accept, and that mismatch used to
    surface only at `build_cmd`. `reviewer=None` answers the global, which is
    what a caller with no reviewer in hand should get.

    Clamped to at least 1, exactly as `batching.split` clamps its own budget: a
    computed budget can arrive at zero, and splitting maximally with every unit
    flagged as an irreducible floor says strictly more than refusing to split.
    """
    envelope = budget.prompt_budget(d, reviewer)
    if d.context_pack:
        envelope //= 2
    return max(1, envelope)


def _effective_batch_budget(d: Defaults,
                            reviewer: Reviewer | None = None) -> int:
    """Return the provider ceiling narrowed by an optional batch hint."""
    budget = _batch_budget(d, reviewer)
    target = d.batch_target_bytes
    if target > 0:
        return min(budget, target)
    return budget


def batch_plan(diff: bytes, d: Defaults,
               reviewer: Reviewer | None = None) -> list[batching.Batch] | None:
    """The batch plan for `diff`, or None when it fits one prompt.

    None is "this review is not batched at all", and it is the answer for every
    diff up to and including the envelope — the shipped single-shot path. The
    threshold is the oracle's (`REVIEW_DIFF_BYTES -gt MAX_DIFF_BYTES`), read
    through `budget.prompt_budget` so that it is THIS reviewer's envelope: a
    diff that fits `codex` and not `agy` must batch for one and not the other,
    or the planner is once again sizing everyone to the smallest provider.

    An EMPTY list is a real answer and a terminal failure ("diff batching
    produced no batches"), never "nothing to review": `batching.split` returns
    no batches only for empty input, and an empty batch would send an empty
    prompt and risk minting a clean verdict for a diff nothing looked at.
    """
    threshold = budget.prompt_budget(d, reviewer)
    if d.batch_target_bytes > 0:
        threshold = min(threshold, d.batch_target_bytes)
    if len(diff) <= threshold:
        return None
    return batching.split(diff, _effective_batch_budget(d, reviewer))


def _estimate_batch_count(repo: Path, d: Defaults,
                          reviewer: Reviewer | None = None) -> int:
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
        plan = batch_plan(diff.data, d, reviewer)
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


def _contributing_providers(rec: dict) -> list[str] | None:
    """All actual aggregate contributors, including clean batches/integration.

    Missing legacy checkpoint provenance cannot borrow the configured finder.
    A partial/unknown answer makes the complete set unknown.
    """
    from .refuter_policy import contributor_families

    contributors = list(rec.get("batches") or ())
    integration = rec.get("integration")
    if integration is not None:
        contributors.append(integration)
    providers = [part.get("provider") if isinstance(part, dict)
                 and part.get("parse_ok") is True else None
                 for part in contributors]
    if contributor_families(providers) is None:
        return None
    return sorted(set(providers))


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


@dataclass(frozen=True)
class _PreparedBatch:
    """One deterministic batch prompt and every input that produced it."""

    batch: batching.Batch
    selection: object
    pack: object | None
    prompt: object
    identity: checkpoints.PassIdentity


@dataclass(frozen=True)
class _PreparedPlan:
    """The complete deterministic plan frozen before provider invocation."""

    batches: tuple[_PreparedBatch, ...]
    integration_selection: object | None
    context_hash: str | None
    checklist_hash: str | None
    boundary_digest: str
    integration_plan_digest: str
    stack_context: bytes = b""
    stack_context_truncated: bool = False
    lineage_context: bytes = b""
    lineage_context_truncated: bool = False


@dataclass(frozen=True)
class _CheckpointRun:
    orchestration_id: str
    owner: str
    identity: checkpoints.OrchestrationIdentity


def _begin_checkpoint_run(store: Store, identity: checkpoints.OrchestrationIdentity,
                          rec: dict, *, resume: bool) -> _CheckpointRun:
    """Resume one exact candidate or create a fresh orchestration."""
    now = _iso_now()
    try:
        store.expire_orchestrations(now=now)
    except Exception as exc:
        raise PersistenceFailed(
            f"could not expire batch checkpoints: {exc!r}") from exc
    candidate = None
    if resume:
        candidate = store.find_resume_candidate(
            identity.repo_id, identity.worktree_root, identity.branch)
    if candidate is not None:
        try:
            stored = checkpoints.OrchestrationIdentity.from_json(
                candidate["identity_json"])
            mismatch = checkpoints.first_mismatch(stored, identity)
        except Exception:
            mismatch = "identity_json"
        if mismatch is None:
            orchestration_id = candidate["id"]
            _note(f"resuming exact batch orchestration {orchestration_id}")
            return _CheckpointRun(
                orchestration_id, ids.new_review_id("sk_owner_"), identity)
        try:
            store.record_orchestration_mismatch(
                candidate["id"], mismatch, at=now)
        except Exception as exc:
            raise PersistenceFailed(
                f"could not record checkpoint mismatch: {exc!r}") from exc
        _note(f"checkpoint resume refused: {mismatch} changed; starting fresh")
    orchestration_id = ids.new_review_id("sk_batch_")
    try:
        created = store.create_orchestration(
            orchestration_id, identity,
            requested_mode=str(rec.get("mode") or "now"),
            created_at=now, expires_at=_iso_after(CHECKPOINT_RETENTION_SEC),
            reuse_existing=resume)
    except Exception as exc:
        raise PersistenceFailed(
            f"could not create batch checkpoints: {exc!r}") from exc
    if created["id"] != orchestration_id:
        _note(f"another resumer created exact batch orchestration {created['id']}")
        return _CheckpointRun(
            created["id"], ids.new_review_id("sk_owner_"), identity)
    _note(f"started batch orchestration {orchestration_id}")
    return _CheckpointRun(
        orchestration_id, ids.new_review_id("sk_owner_"), identity)


def _checkpointed_sub(
        checkpoint_run: _CheckpointRun | None,
        pass_identity: checkpoints.PassIdentity, *, reviewer: Reviewer,
        cfg: Config, d: Defaults, prompt, root: Path, store: Store,
        scratch: Path, tag: str, label: str,
        cancel: "threading.Event | None") -> _Sub:
    """Reuse or exclusively run one exact pass under a fenced store claim."""
    if checkpoint_run is None:
        return _run_sub(reviewer, cfg, d, prompt, root, store, scratch, tag,
                        label, cancel=cancel)
    now = _iso_now()
    width = max(1, len(_chain_for(cfg, reviewer)))
    # The caller passes `_escalated(d, prompt.prompt_bytes, large_prompt)`
    # here, so this lease uses the same raised timeout as the provider attempt
    # for large prompts. Keeping the calculation at the claim boundary makes
    # the lease cover retries plus grace, rather than a stale pre-escalation
    # default.
    lease_seconds = _checkpoint_lease_seconds(d, width)
    try:
        claim = store.claim_checkpoint(
            checkpoint_run.orchestration_id, pass_identity,
            owner=checkpoint_run.owner, now=now,
            lease_expires_at=_iso_after(lease_seconds))
    except Exception as exc:
        raise PersistenceFailed(
            f"could not claim checkpoint for {label}: {exc!r}") from exc
    if claim["decision"] == "complete":
        try:
            payload = checkpoints.CheckpointPayload(claim["payload_json"])
            fields = checkpoints.sub_fields_from_payload(payload)
            sub = _Sub(**fields)
        except Exception as exc:
            raise PersistenceFailed(
                f"stored checkpoint for {label} is invalid: {exc!r}") from exc
        _note(f"{label}: reused completed checkpoint")
        return sub
    if claim["decision"] == "in_flight":
        raise CheckpointInFlight(
            f"{label} is already in flight under another exact resumer; "
            "no duplicate provider call was launched")
    try:
        started = time.monotonic()
        started_at = _iso_now()
        sub = _run_sub(reviewer, cfg, d, prompt, root, store, scratch, tag,
                       label, cancel=cancel)
        completed_at = _iso_now()
        capacity_timing = sub.provenance.get("capacity_timing")
        checkpoint_timing = (dict(capacity_timing)
                             if isinstance(capacity_timing, dict) else {})
        has_started_attempt = bool(checkpoint_timing) or any(
            isinstance(attempt, dict) and "skipped" not in attempt
            for attempt in sub.attempts)
        if has_started_attempt:
            checkpoint_timing.setdefault("started_at", started_at)
            checkpoint_timing["completed_at"] = completed_at
        sub = replace(sub, provenance={
            **sub.provenance,
            "checkpoint_timing": checkpoint_timing,
            # Empty synthetic sub-results have no process or admission work;
            # preserve unknown rather than manufacturing a timing value that
            # would make checkpoint reuse differ from a fresh deterministic run.
            "wall_duration_sec": (
                round(max(0.0, time.monotonic() - started), 3)
                if sub.attempts else None),
        })
        payload = checkpoints.payload_from_sub(sub)
        applied = store.complete_checkpoint(
            checkpoint_run.orchestration_id, pass_identity.kind,
            pass_identity.index, owner=checkpoint_run.owner,
            claim_token=claim["claim_token"], fence=claim["fence"],
            payload=payload, completed_at=_iso_now())
        if not applied:
            raise CheckpointClaimLost(
                f"lost fenced checkpoint claim for {label}; refusing to "
                "publish this provider result")
        return sub
    except BaseException:
        try:
            store.release_checkpoint(
                checkpoint_run.orchestration_id, pass_identity.kind,
                pass_identity.index, owner=checkpoint_run.owner,
                claim_token=claim["claim_token"], fence=claim["fence"],
                reason=f"{label} did not complete", at=_iso_now())
        except Exception:
            pass
        raise


def _checkpoint_lease_seconds(d: Defaults, chain_width: int) -> float:
    """Cover one pass's retries plus its configured provider admission wait."""
    return (budget.worst_runtime(d, chain_width, 0)
            + capacity.admission_wait_from_env(30.0))


def _milliseconds_to_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return round(value / 1000.0, 3)


def _escalated(d: Defaults, prompt_bytes: int,
               large_prompt: tuple[int, int] | None) -> Defaults:
    """`d`, with the timeout raised for a prompt that is over the threshold.

    ORACLE A14.7, and a BACKGROUND-only rule: `large_prompt` is
    `(threshold_bytes, foreground_timeout_sec)` and is None for the foreground,
    whose cap is already the wider of the two. A whole-diff prompt this large
    legitimately needs longer than a background cap allows, and timing it out
    would spend the entire budget and record nothing.

    Measured PER PROMPT, so in a batched run each batch prompt is judged on its
    own size -- one oversized batch escalates that batch's attempts and nothing
    else.

    Never a REDUCTION. A config may set `[dispatch] timeout_sec` ABOVE
    `[defaults] timeout_sec` (the reservation budget takes the max of the two for
    exactly that reason), and applying the foreground figure literally would then
    give the LARGEST prompts the SHORTEST cap -- the opposite of what an
    escalation means. Deviation from the brief's literal "escalates to
    defaults.timeout_sec", recorded in the plan.
    """
    if large_prompt is None:
        return d
    threshold, escalated = large_prompt
    if prompt_bytes <= threshold or escalated <= d.timeout_sec:
        return d
    _note(f"prompt is {prompt_bytes} bytes (> {threshold}); raising this "
          f"attempt's cap from {d.timeout_sec}s to {escalated}s")
    return replace(d, timeout_sec=escalated)


def _run_sub(reviewer: Reviewer, cfg: Config, d: Defaults, prompt, root: Path,
             store: Store, scratch: Path, tag: str, label: str,
             cancel: "threading.Event | None" = None) -> _Sub:
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
                             scratch, tag, cancel=cancel)
    except Exception as e:
        # `Exception`, so `ReviewCancelled` (a `BaseException`) passes straight
        # through: a cancelled sub-review must not be recorded as a sub-review
        # that answered badly.
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


def _prepare_batch_plan(
        diff, *, batches: list, cfg: Config, d: Defaults, root: Path,
        finder: Reviewer, branch: str, base_ref: str, base_sha: str,
        head_label: str, context_source: str = "wt",
        context_oid: str | None = None,
        stack_context: bytes | None = None,
        stack_context_truncated: bool = False,
        lineage_context: bytes | None = None,
        lineage_context_truncated: bool = False,
        evidence_context: bytes | None = None) -> _PreparedPlan:
    """Freeze deterministic pass inputs before any resumable provider call.

    Exact resume cannot learn context/checklist/prompt identity lazily after a
    provider has already answered.  This helper performs the same pure/file
    preparation `_orchestrate` historically performed immediately before each
    call, for every batch up front, while the caller still holds its existing
    repository identity/foreground serialization boundary.
    """
    count = len(batches)
    if count < 1:
        raise ValueError("a checkpoint plan needs at least one batch")
    mode = passes.batch_checklist_mode(count)
    sole = count == 1
    envelope = budget.prompt_budget(d, finder)
    prepared: list[_PreparedBatch] = []
    selections = []
    context_hashes: list[str | None] = []
    boundaries = []

    for index, batch in enumerate(batches, 1):
        selection = checklist.select(
            batch.files, mode, _under(root, d.checklist_dir),
            _under(root, d.rules_json), d.checklist_map,
            d.test_path_patterns)
        selections.append(selection)
        pack = None
        if d.context_pack:
            headroom = promptbuild.context_headroom(
                envelope, len(batch.data), packing=True)
            pack = contextpack.pack(
                root, list(batch.files),
                {name: diff.statuses[name] for name in batch.files
                 if name in diff.statuses},
                headroom, source=context_source, oid=context_oid,
                pack_large_added=not sole)
            context_hashes.append(
                pack.sha256 if isinstance(pack.sha256, str) else None)
        b_branch, b_head = _batch_labels(
            branch, head_label, index, count, list(batch.files))
        prompt = promptbuild.build(
            b_branch, base_ref, base_sha, b_head, batch.data, envelope,
            selection, pack.body if pack is not None else None,
            stack_context=stack_context,
            stack_context_truncated=stack_context_truncated,
            lineage_context=lineage_context,
            lineage_context_truncated=lineage_context_truncated,
            evidence_context=evidence_context)
        boundary = {
            "index": index,
            "diff_hash": gitio.diff_identity(batch.data),
            "files": list(batch.files),
            "splitter_truncated": batch.truncated is True,
        }
        boundary_hash = checkpoints.canonical_digest(boundary)
        boundaries.append(boundary)
        prepared.append(_PreparedBatch(
            batch=batch, selection=selection, pack=pack, prompt=prompt,
            identity=checkpoints.PassIdentity(
                kind="batch", index=index,
                prompt_hash=checkpoints.canonical_digest({
                    "prompt_sha256": gitio.diff_identity(prompt.text),
                    "prompt_bytes": prompt.prompt_bytes,
                    "diff_truncated": prompt.diff_truncated is True,
                }),
                diff_hash=gitio.diff_identity(batch.data),
                boundary_hash=boundary_hash)))

    boundary_digest = checkpoints.canonical_digest(boundaries)
    integration_selection = None
    integration_selection_hash = None
    if passes.should_run_integration(count):
        integration_selection = checklist.select(
            diff.files, passes.INTEGRATION_CHECKLIST_MODE,
            _under(root, d.checklist_dir), _under(root, d.rules_json),
            d.checklist_map, d.test_path_patterns)
        selections.append(integration_selection)
        integration_selection_hash = reuse.checklist_identity(
            integration_selection)
    integration_plan_digest = checkpoints.canonical_digest({
        "scheduled": integration_selection is not None,
        "batch_count": count,
        "boundary_digest": boundary_digest,
        "selection_hash": integration_selection_hash,
        "stack_context_hash": gitio.diff_identity(stack_context or b""),
        "stack_context_truncated": stack_context_truncated is True,
        "lineage_context_hash": gitio.diff_identity(lineage_context or b""),
        "lineage_context_truncated": lineage_context_truncated is True,
    })
    return _PreparedPlan(
        batches=tuple(prepared),
        integration_selection=integration_selection,
        context_hash=reuse.aggregate_context_identity(
            context_hashes, enabled=d.context_pack),
        checklist_hash=reuse.aggregate_checklist_identity(
            selections,
            batch_boundaries=[gitio.diff_identity(batch.data)
                              for batch in batches]),
        boundary_digest=boundary_digest,
        integration_plan_digest=integration_plan_digest,
        stack_context=stack_context or b"",
        stack_context_truncated=stack_context_truncated is True,
        lineage_context=lineage_context or b"",
        lineage_context_truncated=lineage_context_truncated is True)


def _orchestration_identity(
        rec: dict, diff, prepared: _PreparedPlan, *, cfg: Config, d: Defaults,
        root: Path, finder: Reviewer, branch: str, head: str, base_ref: str,
        base_sha: str, tree_fingerprint: str
        ) -> checkpoints.OrchestrationIdentity:
    """Build the complete checkpoint identity from one frozen prepared plan."""
    integrator = _pass_reviewer(cfg, passes.INTEGRATION_PASS, finder)

    def _graph(reviewer: Reviewer) -> list[dict]:
        return [asdict(entry) for entry in _chain_for(cfg, reviewer)]

    reviewer_hash = checkpoints.canonical_digest({
        "requested_reviewer": rec.get("requested_reviewer"),
        "client_family": rec.get("client_family"),
        "routed_reviewer": rec.get("routed_reviewer"),
        "finder": _graph(finder),
        "integration": (_graph(integrator)
                        if prepared.integration_selection is not None else []),
    })
    config_hash = checkpoints.canonical_digest({
        "mode": rec.get("mode"),
        # Hash the complete loaded policy plus the mode-specific effective
        # defaults. A currently non-executing field is still configuration
        # identity: a later planner may begin consuming it, and old durable
        # checkpoints must not silently become approximately compatible.
        "config": asdict(cfg),
        "effective_defaults": asdict(d),
    })
    pass_identities = list(item.identity for item in prepared.batches)
    if prepared.integration_selection is not None:
        pass_identities.append(checkpoints.PassIdentity(
            kind="integration", index=0, prompt_hash=None,
            diff_hash=gitio.diff_identity(diff.data),
            boundary_hash=prepared.integration_plan_digest))
    return checkpoints.OrchestrationIdentity(
        repo_id=gitio.repository_identity(root),
        worktree_root=gitio.observed_worktree_root(root),
        branch=branch,
        head=head,
        base_ref=base_ref,
        base_sha=base_sha,
        diff_hash=gitio.diff_identity(diff.data),
        tree_fingerprint=tree_fingerprint,
        context_hash=prepared.context_hash,
        checklist_hash=prepared.checklist_hash,
        reviewer_hash=reviewer_hash,
        config_hash=config_hash,
        policy_hash=reuse.security_policy_identity(cfg),
        planner_version=checkpoints.PLANNER_VERSION,
        batch_budget=_effective_batch_budget(d, finder),
        batch_count=len(prepared.batches),
        boundary_digest=prepared.boundary_digest,
        integration_plan_digest=prepared.integration_plan_digest,
        pass_identities=tuple(pass_identities))


def _revalidate_foreground_orchestration(
        expected: checkpoints.OrchestrationIdentity, rec: dict, *,
        repo: Path, cfg: Config, d: Defaults, finder: Reviewer,
        stack_context: bytes | None = None,
        stack_context_truncated: bool = False,
        lineage_context: bytes | None = None,
        lineage_context_truncated: bool = False) -> str | None:
    """Recompute the exact repository/plan identity before final publication."""
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, d.untracked_max)
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)
    plan = batch_plan(diff.data, d, finder)
    if not plan:
        return "batch_plan"
    root = gitio._worktree_root(repo)
    prepared = _prepare_batch_plan(
        diff, batches=plan, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)",
        stack_context=stack_context,
        stack_context_truncated=stack_context_truncated,
        lineage_context=lineage_context,
        lineage_context_truncated=lineage_context_truncated)
    current = _orchestration_identity(
        rec, diff, prepared, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, head=head, base_ref=base.ref, base_sha=base.sha,
        tree_fingerprint=gitio.tree_fingerprint(repo, paths=diff.files))
    return checkpoints.first_mismatch(expected, current)


def _orchestrate(rec: dict, diff, *, batches: list, cfg: Config, d: Defaults,
                 root: Path, store: Store, scratch: Path, finder: Reviewer,
                 branch: str, base_ref: str, base_sha: str, head_label: str,
                 tag: str = "primary", context_source: str = "wt",
                 context_oid: str | None = None,
                 large_prompt: tuple[int, int] | None = None,
                 cancel: "threading.Event | None" = None,
                 stack_context: bytes | None = None,
                 stack_context_truncated: bool = False,
                 lineage_context: bytes | None = None,
                 lineage_context_truncated: bool = False,
                 evidence_context: bytes | None = None,
                 prepared_plan: _PreparedPlan | None = None,
                 checkpoint_run: _CheckpointRun | None = None) -> dict:
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

    Four keyword-only parameters exist for the BACKGROUND dispatcher, and all
    four default to the foreground's shipped behaviour:

    * `context_source`/`context_oid` — where each batch's file context is READ
      FROM. The foreground packs the working tree (`"wt"`); a pre-push worker
      packs the PUSHED COMMIT's tree (`"oid"`, the pushed oid), because the
      developer's checkout may already be somewhere else entirely and reading it
      would certify content nobody pushed. This is the one Task-8 seam Task 10
      threads: the per-batch pack call used to be hard-wired to the working tree.
    * `large_prompt` — the per-prompt timeout escalation (see `_escalated`),
      measured on each batch prompt individually.
    * `cancel` — the worker's cancellation token, forwarded to every sub-review
      and checked at each batch boundary. `ReviewCancelled` propagates out
      UNCAUGHT: a cancelled orchestration has no aggregate, and building one
      from the batches that happened to finish would publish a partial review as
      a whole one. (The worker's own transform is what preserves the partial,
      and it marks it `degraded`.)
    """
    count = len(batches)
    mode = passes.batch_checklist_mode(count)
    if prepared_plan is None:
        prepared_plan = _prepare_batch_plan(
            diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
            branch=branch, base_ref=base_ref, base_sha=base_sha,
            head_label=head_label, context_source=context_source,
            context_oid=context_oid, stack_context=stack_context,
            stack_context_truncated=stack_context_truncated,
            lineage_context=lineage_context,
            lineage_context_truncated=lineage_context_truncated,
            evidence_context=evidence_context)
    if len(prepared_plan.batches) != count or any(
            item.batch != batch
            for item, batch in zip(prepared_plan.batches, batches)):
        raise ValueError("the prepared checkpoint plan does not match the batches")
    # Every BATCH prompt is the finder's, so it is the finder's envelope — the
    # same number `batch_plan` cut these batches with, read from the same
    # helper rather than re-derived. The integration pass is a DIFFERENT
    # reviewer and gets its own (see `integration_mdb` below): a cross-file
    # pass on a provider with a tighter ceiling must not be sized for the
    # finder's.
    mdb = budget.prompt_budget(d, finder)

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
    checklist_selections = []
    context_hashes = []

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

    for index, prepared in enumerate(prepared_plan.batches, 1):
        batch = prepared.batch
        # A BATCH BOUNDARY. The checklist selection, the context pack and the
        # prompt build below all happen outside any watchdog, so a token set
        # while the previous batch was being written up would otherwise buy a
        # whole further pack + model call.
        _checkpoint(cancel, None, f"before batch {index} of {count}")
        # Per batch: never a cross-file rule (`mode`), and context for exactly
        # the files this batch shows. A file split across two batches has its
        # context packed into both -- accepted, because the batches are reviewed
        # independently and each one has to stand on its own.
        selection = prepared.selection
        checklist_selections.append(selection)
        _fold_checklist(selection)

        pack = prepared.pack
        if pack is not None:
            context_hashes.append(
                pack.sha256 if isinstance(pack.sha256, str) else None)
            ctx_bytes += pack.bytes_total
            for name in pack.included:
                if name not in ctx_files:
                    ctx_files.append(name)
            for path, reason in pack.omitted:
                entry = f"{path} ({reason})"
                if entry not in ctx_omitted:
                    ctx_omitted.append(entry)

        prompt = prepared.prompt
        prompt_bytes += prompt.prompt_bytes
        _note(f"batch {index}/{count} ({len(batch.files)} file(s), "
              f"{len(batch.data)} bytes) ...")
        if prompt.diff_truncated:
            _note(f"batch {index}/{count} is {len(batch.data)} bytes "
                  f"(> {mdb}); its prompt is truncated and this "
                  f"review cannot be trustworthy")
        effective_d = _escalated(d, prompt.prompt_bytes, large_prompt)
        sub = _checkpointed_sub(
            checkpoint_run, prepared.identity, reviewer=finder, cfg=cfg,
            d=effective_d, prompt=prompt,
            root=root, store=store, scratch=scratch, tag=f"{tag}.b{index}",
            label=f"batch {index}", cancel=cancel)
        run_duration_sec = round(sum(
            float(a.get("duration_sec") or 0.0)
            for a in sub.attempts if isinstance(a, dict)
            and isinstance(a.get("duration_sec"), (int, float))), 3)
        subs.append(sub)
        findings.extend(sub.findings)
        checklist_meta = passes.checklist_meta(mode, selection)
        metas.append({
            # Provenance FIRST so the explicit fields below always win: which
            # provider answered is `_provenance`'s vocabulary to widen, and a new
            # key there must never be able to overwrite a trust axis here.
            **{key: value for key, value in sub.provenance.items()
               if key != "checkpoint_timing"},
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
            "checklist": checklist_meta,
            "attempts": sub.attempts,
            "telemetry": telemetry.batch_telemetry(
                planner_version=checkpoints.PLANNER_VERSION,
                batch_budget=_effective_batch_budget(d, finder),
                boundary_digest=prepared.identity.boundary_hash,
                batch_index=index, batch_count=count,
                diff_bytes=len(batch.data),
                context_bytes=(pack.bytes_total if pack is not None else 0),
                checklist_bytes=int(checklist_meta.get("bytes_total") or 0),
                prompt_bytes=prompt.prompt_bytes,
                attempts=sub.attempts, timeout_sec=effective_d.timeout_sec,
                run_duration_sec=run_duration_sec,
                wall_duration_sec=sub.provenance.get("wall_duration_sec"),
                queued_at=(sub.provenance.get("checkpoint_timing") or {}).get(
                    "queued_at"),
                admitted_at=(sub.provenance.get("checkpoint_timing") or {}).get(
                    "admitted_at"),
                started_at=(sub.provenance.get("checkpoint_timing") or {}).get(
                    "started_at"),
                completed_at=(sub.provenance.get("checkpoint_timing") or {}).get(
                    "completed_at"),
                queue_duration_sec=_milliseconds_to_seconds(
                    (sub.provenance.get("checkpoint_timing") or {}).get(
                        "queue_wait_ms"))),
        })

    # --- the cross-file pass over the seams the split just cut --------------
    integration: dict | None = None
    integration_sub: _Sub | None = None
    if passes.should_run_integration(count):
        # The last pass boundary in an orchestration: the integration prompt is
        # built from every batch's diff and findings, which is real work.
        _checkpoint(cancel, None, "before the integration pass")
        selection = prepared_plan.integration_selection
        if selection is None:
            raise ValueError("the prepared plan omitted a required integration pass")
        checklist_selections.append(selection)
        _fold_checklist(selection)
        meta_checklist = passes.checklist_meta(
            passes.INTEGRATION_CHECKLIST_MODE, selection)
        reviewer = _pass_reviewer(cfg, passes.INTEGRATION_PASS, finder)
        _note(f"integration pass over {count} batch seams ...")
        try:
            integration_mdb = budget.prompt_budget(d, reviewer)
            prompt = passes.integration_prompt(
                [passes.BatchSummary(files=list(b.files), diff=b.data,
                                     summary=s.summary, findings=s.findings)
                 for b, s in zip(batches, subs)],
                selection, integration_mdb,
                stack_context=prepared_plan.stack_context or None,
                stack_context_truncated=prepared_plan.stack_context_truncated,
                lineage_context=prepared_plan.lineage_context or None,
                lineage_context_truncated=prepared_plan.lineage_context_truncated)
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
                stop_reason=integration_sub.stop_reason,
                provenance=integration_sub.provenance)
            integration["telemetry"] = telemetry.batch_telemetry(
                planner_version=checkpoints.PLANNER_VERSION,
                batch_budget=_effective_batch_budget(d, finder),
                boundary_digest=prepared_plan.integration_plan_digest,
                batch_index=0, batch_count=count,
                diff_bytes=len(diff.data), context_bytes=0,
                checklist_bytes=int(meta_checklist.get("bytes_total") or 0),
                prompt_bytes=0, attempts=(), timeout_sec=d.timeout_sec)
        else:
            prompt_bytes += prompt.prompt_bytes
            if prompt.diff_truncated:
                _note(f"NOTE integration context capped at {integration_mdb} "
                      f"bytes; some cross-file relationships were not shown")
            integration_identity = checkpoints.PassIdentity(
                kind="integration", index=0,
                prompt_hash=checkpoints.canonical_digest({
                    "prompt_sha256": gitio.diff_identity(prompt.text),
                    "prompt_bytes": prompt.prompt_bytes,
                    "diff_truncated": prompt.diff_truncated is True,
                }),
                diff_hash=gitio.diff_identity(diff.data),
                boundary_hash=prepared_plan.integration_plan_digest)
            effective_d = _escalated(d, prompt.prompt_bytes, large_prompt)
            integration_sub = _checkpointed_sub(
                checkpoint_run, integration_identity, reviewer=reviewer,
                cfg=cfg, d=effective_d, prompt=prompt,
                root=root, store=store, scratch=scratch,
                tag=passes.INTEGRATION_PASS, label="the integration pass",
                cancel=cancel)
            run_duration_sec = round(sum(
                float(a.get("duration_sec") or 0.0)
                for a in integration_sub.attempts if isinstance(a, dict)
                and isinstance(a.get("duration_sec"), (int, float))), 3)
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
                stop_reason=integration_sub.stop_reason,
                provenance={key: value for key, value in
                            integration_sub.provenance.items()
                            if key != "checkpoint_timing"},
                checklist=meta_checklist,
                note=integration_sub.failure_reason)
            integration["telemetry"] = telemetry.batch_telemetry(
                planner_version=checkpoints.PLANNER_VERSION,
                batch_budget=_effective_batch_budget(d, finder),
                boundary_digest=prepared_plan.integration_plan_digest,
                batch_index=0, batch_count=count,
                diff_bytes=len(diff.data), context_bytes=0,
                checklist_bytes=int(meta_checklist.get("bytes_total") or 0),
                prompt_bytes=prompt.prompt_bytes,
                attempts=integration_sub.attempts,
                timeout_sec=effective_d.timeout_sec,
                run_duration_sec=run_duration_sec,
                wall_duration_sec=integration_sub.provenance.get(
                    "wall_duration_sec"),
                queued_at=(integration_sub.provenance.get(
                    "checkpoint_timing") or {}).get("queued_at"),
                admitted_at=(integration_sub.provenance.get(
                    "checkpoint_timing") or {}).get("admitted_at"),
                started_at=(integration_sub.provenance.get(
                    "checkpoint_timing") or {}).get("started_at"),
                completed_at=(integration_sub.provenance.get(
                    "checkpoint_timing") or {}).get("completed_at"),
                queue_duration_sec=_milliseconds_to_seconds(
                    (integration_sub.provenance.get("checkpoint_timing")
                     or {}).get("queue_wait_ms")))

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
        batch_plan={
            "planner_version": checkpoints.PLANNER_VERSION,
            "batch_budget": _effective_batch_budget(d, finder),
            "batch_count": count,
            "boundary_digest": prepared_plan.boundary_digest,
            "integration_plan_digest": prepared_plan.integration_plan_digest,
        },
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
        # Background artifacts stay outside the foreground reuse contract;
        # their legacy dedup policy intentionally retains empty identities.
        context_hash=(reuse.aggregate_context_identity(
            context_hashes, enabled=d.context_pack) or ""
            if rec.get("mode") == "now" else ""),
        checklist_hash=(reuse.aggregate_checklist_identity(
            checklist_selections,
            batch_boundaries=[gitio.diff_identity(batch.data)
                              for batch in batches])
                        if rec.get("mode") == "now" else ""),
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
    this field exists to expose. `None` when nothing reported at all (the shipped
    record's own "nothing to say" value).

    Two things about "abnormal" that the shipped rule got wrong, both REPORTING
    only — no trust axis is computed here, and `parse_ok`/`degraded`/
    `diff_truncated` are unchanged by either:

    * **Abnormal is measured against `adapters.NORMAL_STOP_REASONS`, not against
      the literal `"EndTurn"`.** That literal is grok's word. A batched review
      can be answered by several adapters (a fallback chain, or a cross-file pass
      configured on a second provider), and agy's normal terminal status —
      `SUCCESS` — was therefore promoted as the round's first ABNORMAL value.

    * **A normal word is only reported for a round in which something actually
      produced a review.** Observed live: nine batches whose chains were
      exhausted, plus a cross-file pass that came back `SUCCESS` with nothing in
      it, published `stop_reason=SUCCESS` in the verdict banner of a round that
      reviewed nothing at all. `usable_output` is the field that draws that line
      (see `usable_output`, whose rule this mirrors on the `_Sub`s), and a
      terminal word from a sub-review that produced no review describes the
      process, not the round.

      An ABNORMAL value is NOT suppressed that way, deliberately. `Cancelled` or
      `MaxOutputTokens` is a diagnostic, and it is worth most precisely when
      there is no review to read instead.

    When every reporting sub-review ended normally, the answer is the FIRST word
    they reported — theirs, not a translation of it into some other adapter's
    vocabulary. For the single-provider grok runs the oracle pins, that is
    `EndTurn`, exactly as before.
    """
    def said(candidates):
        return [s.stop_reason for s in candidates
                if isinstance(s.stop_reason, str) and s.stop_reason]

    # Abnormality is judged across EVERY sub-review, answered or not: a
    # `Cancelled` from a run that produced nothing is exactly when the word
    # matters most.
    for value in said(subs):
        if value not in NORMAL_STOP_REASONS:
            return value
    # A NORMAL word, though, has to come from a sub-review that actually
    # produced a review. A failed one can still report its adapter's normal
    # terminal status -- an exhausted chain whose last attempt exited cleanly
    # with nothing usable in it -- and that word describes the process, not the
    # round. Reporting it would let a batch that answered nothing speak for a
    # batch that did.
    answered = said([s for s in subs if s.parse_ok])
    return answered[0] if answered else None


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
                scratch: Path, *,
                cancel: "threading.Event | None" = None) -> dict:
    """Run one extra pass and merge it into `rec`, returning the new record.

    `reviewer` is the caller's deliberate choice. Security can use its
    configured `security` role; the skeptic receives the selected finder from
    `_pass_reviewer` and therefore follows that finder's chain. The separate
    refuter annotation pass uses its configured `refuter` role in
    `_refuter_pass`. The role-specific choices are resolved in preflight before
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
    # A PASS BOUNDARY, and the cheapest place to notice a cancellation: nothing
    # has been spent on this pass yet, and `rec` is the record as it stands so the
    # exception carries the findings already produced.
    _checkpoint(cancel, rec, f"before the {name} pass")
    _note(f"{name} pass ...")
    try:
        prompt = build_prompt()
    except Exception as e:
        _note(f"{name} prompt build failed; demoting review: {e!r}")
        reason = f"extra pass {name} could not be prepared: {e!r}"
        return _failed_pass(rec, name, reason, reason)
    if prompt.diff_truncated:
        # The budget the CALLER built this prompt with, read from the same
        # helper rather than from `d.max_diff_bytes` — this pass may run on a
        # reviewer with a tighter ceiling than the global, and a note naming a
        # number the prompt was not capped at is worse than no note.
        _note(f"NOTE {name} pass diff capped at "
              f"{budget.prompt_budget(d, reviewer)} bytes "
              f"(partial coverage, one-call bound)")
    try:
        outcome = _run_chain(reviewer, cfg, d, prompt.text, cwd, store,
                             scratch, name, **_cancel_kw(cancel))
    except Exception as e:
        # `Exception`, NEVER `BaseException`. `ReviewCancelled` is outside
        # `Exception` exactly so it passes straight through here: catching it
        # would turn a killed review into a merely-failed PASS, and the caller
        # would go on to finalize the primary review as a trustworthy one. Pinned
        # by `test_a_cancellation_during_an_extra_pass_...`; the named mutation is
        # widening this clause.
        _note(f"{name} pass failed; demoting review: {e!r}")
        reason = f"extra pass {name} failed: {e!r}"
        return _failed_pass(rec, name, reason, reason)
    if outcome.parsed is None:
        reason = outcome.failure_reason or passes.failed_pass_reason(name)
        # Name the pass so a later timeout is not read as "the finder never
        # produced usable evidence". merge_failed_extra_pass already keeps
        # the finder's summary/findings and fail-closes the record.
        if not str(reason).startswith("extra pass"):
            reason = f"extra pass {name}: {reason}"
        return _with_provenance(
            passes.merge_failed_extra_pass(rec, name, reason),
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
    prov["contributing_providers"] = (rec.get("extra_passes", {})
                                      .get("refuter", {}).get("contributing_providers"))
    if not str(prov.get("note") or "").strip():
        prov["note"] = note
    return passes.merge_refuter_pass(rec, None, prov, finder_findings_total,
                                     **kw)


def _refuter_pass(rec: dict, finder_findings_total: int, build_prompt,
                  reviewer: Reviewer, cfg: Config, d: Defaults, cwd: Path,
                  store: Store, scratch: Path,
                  contributing_providers: list[str] | None, *,
                  cancel: "threading.Event | None" = None) -> dict:
    """Run the refuter pass and annotate `rec`, returning the new record.

    Structurally a sibling of `_extra_pass` and semantically its opposite in
    the one way that matters: NOTHING on any path through this function can
    make the review less trustworthy than the finder left it. The record is
    annotated, or it is annotated with the news that the refuter could not
    answer.

    `build_prompt` is a callable for the same reason it is one in
    `_extra_pass`: the prompt build sits inside the guard, so a prompt that
    will not render is a failed pass rather than an exception that destroys a
    review already in hand. It receives the filtered head reviewer so prompt
    sizing uses the provider that will actually be called.

    The chain runs under `REFUTER_CONTRACT`, which is what makes every adapter
    request, classify and validate the verdicts shape — and what gives this
    pass Task 7's fallback support for free.
    """
    _checkpoint(cancel, rec, "before the refuter pass")
    from .refuter_policy import contributor_families, provider_family

    contributors = contributor_families(contributing_providers)
    eligible = ([entry for entry in _chain_for(cfg, reviewer)
                 if entry.enabled and provider_family(entry.provider) is not None
                 and provider_family(entry.provider) not in contributors]
                if contributors is not None else [])
    # Promote only within the configured explicit chain. A promoted fallback's
    # own fallbacks must not introduce an unchecked contributor or provider.
    if not eligible:
        note = ("finding contributor provenance is unknown; independent refuter skipped"
                if contributors is None else
                "no independent provider remains in the configured refuter chain; "
                "findings remain unrefuted")
        return _with_provenance(passes.skipped_refuter_pass(rec, note), "refuter",
                                {"contributing_providers": contributing_providers})
    reviewer = replace(eligible[0], fallbacks=tuple(r.name for r in eligible[1:]))
    rec = _with_provenance(rec, "refuter",
                           {"contributing_providers": contributing_providers})
    _note("refuter pass (annotation only) ...")
    try:
        prompt = build_prompt(reviewer)
    except Exception as e:
        _note(f"refuter prompt build failed; the review keeps its verdict: {e!r}")
        return _refuter_failed(
            rec, finder_findings_total,
            f"the refuter prompt could not be prepared: {e!r}")

    notes: list[str] = []
    if prompt.diff_truncated:
        _note(f"NOTE refuter pass diff capped at "
              f"{budget.prompt_budget(d, reviewer)} bytes "
              f"(partial coverage, one-call bound)")
        notes.append("the refuter saw a size-capped diff (partial coverage)")

    try:
        outcome = _run_chain(reviewer, cfg, d, prompt.text, cwd, store, scratch,
                             "refuter", contract=REFUTER_CONTRACT,
                             **_cancel_kw(cancel))
    except Exception as e:
        # `Exception`, NEVER `BaseException` -- see `_extra_pass`. This clause is
        # the more dangerous of the two to widen: this pass demotes NOTHING, so a
        # swallowed cancellation here would leave a clean, trustworthy primary
        # review standing and finalize it.
        _note(f"refuter pass failed; the review keeps its verdict: {e!r}")
        return _refuter_failed(rec, finder_findings_total,
                               f"the refuter pass failed: {e!r}",
                               partial_coverage=prompt.diff_truncated,
                               notes=notes)

    prov = _provenance(outcome)
    prov["contributing_providers"] = contributing_providers
    p = outcome.parsed
    if p is None or not p.parse_ok or not isinstance(p.payload, dict):
        _note("the refuter produced no usable verdicts; the review is "
              "unannotated and otherwise unchanged")
        return _refuter_failed(
            rec, finder_findings_total,
            outcome.failure_reason or "the refuter produced no usable verdicts",
            prov, partial_coverage=prompt.diff_truncated, notes=notes)

    actual_provider = (outcome.accepted.get("provider")
                       if isinstance(outcome.accepted, dict) else None)
    if (provider_family(actual_provider) is None
            or provider_family(actual_provider) in contributors
            or actual_provider not in {entry.provider for entry in eligible}):
        return _refuter_failed(
            rec, finder_findings_total,
            "the refuter answer lacks independent accepted-provider provenance",
            prov, partial_coverage=prompt.diff_truncated, notes=notes)

    if p.degraded:
        notes.append("the refuter run was degraded: %s"
                     % (p.degraded_reason or "no reason given"))
    merged = passes.merge_refuter_pass(
        rec, p.payload, prov, finder_findings_total, degraded=p.degraded,
        partial_coverage=prompt.diff_truncated, notes=notes)

    return merged
