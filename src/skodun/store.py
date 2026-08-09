"""SQLite persistence for reviews, triage decisions and gate events.

The store is the only place that decides whether a review is trustworthy: it
computes the value from the record's three trust axes via
:func:`skodun.trust.is_trustworthy` and writes it into both the indexed column
and the stored artifact JSON, so an index row that disagrees with its artifact
is impossible by construction.

Schema changes go through the migration ladder in :func:`_migrate`, keyed on
``PRAGMA user_version``. Existing stores hold thousands of imported reviews, so
migrations are additive only: no Phase 1 table, index or row is ever dropped or
rewritten, and a store stamped with a version this build does not understand is
refused without being written to at all.

Triage decisions are an APPEND-ONLY EVENT STREAM (v3). ``triage_events`` holds
one row per decision -- ``dismiss``, ``reopen`` or, since v4, ``defer`` -- and
the effective state of a finding is its LAST EVENT BY ``seq``. Nothing is ever
overwritten or deleted, so a finding's whole history reads back in order and
every reason survives the decision that overturned it. The pre-v3 single-row
``triage`` table is still here and is now READ-ONLY: it is the audit source the
migration seeded the stream from.

``defer`` (v4) means *real, not blast-radius for this change, filed as X*. It
CLEARS the gate exactly as ``dismiss`` does -- ``triage_for`` returns both -- and
the only thing that keeps it honest is its filed reference, which is why
``tracking_ref`` is a COLUMN rather than a convention inside ``reason``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import ids
from .trust import is_trustworthy

_TRUST_AXES = ("parse_ok", "degraded", "diff_truncated")

#: The mode/source pair that makes `usable_output` mandatory. See
#: `_requires_usable_output`.
PREPUSH_MODE = "prepush"
SKODUN_SOURCE = "skodun"

#: The record status a reservation writes, and the only status a conditional
#: transition (finalize, stale recovery, pid attach) will act on.
RUNNING = "running"
_REUSE_OUTCOMES = frozenset(("hit", "miss", "bypass", "error"))

#: The schema this build of skodun writes and understands. A store stamped
#: higher was written by a newer skodun and is refused, untouched.
SCHEMA_VERSION = 11


def schema_too_new_message(store_version: int) -> str:
    """Refusal text when the on-disk store was written by a newer skodun.

    The usual real-world case is **version skew**: a freshly upgraded CLI (or a
    newer MCP) migrated the shared DB, while a **long-lived older** `skodun mcp`
    process still has the previous ``SCHEMA_VERSION``. Falling back to the CLI
    for reviews makes the skew worse; the fix is one install and a restarted MCP.
    """
    return (
        f"store schema v{store_version} is newer than this skodun "
        f"(this build understands v{SCHEMA_VERSION}). "
        "Upgrade this process to the same skodun install that wrote the store, "
        "then restart MCP — do not fall back to the CLI while MCP stays on the "
        "old build (CLI and MCP must share one install)."
    )

#: Set to anything other than "0", unset, or blank to ignore `provider_state`
#: entirely.
IGNORE_PROVIDER_STATE_ENV = "SKODUN_IGNORE_PROVIDER_STATE"

#: The store's one timestamp format: ISO-8601 UTC, seconds resolution, `Z`.
#: Every field is zero-padded to a constant width, which is what makes a plain
#: string comparison a correct time comparison. Nothing else may be stored.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
#: `[0-9]`, deliberately NOT `\d`. Python's `\d` is Unicode-aware and matches
#: every Unicode decimal digit -- and `time.strptime` accepts them too, so a
#: timestamp written with fullwidth or Arabic-Indic digits passed as canonical
#: while breaking the one property canonicality exists to guarantee. Those
#: codepoints sort ABOVE every ASCII digit (U+FF12 vs "2"), so the failure is
#: maximally quiet: `shadow-compare --since "２０２６-01-01T00:00:00Z"` put every
#: real row outside the window and reported "0 compared, 0 unparseable rows
#: excluded" -- a clean-looking answer from a filter that matched nothing.
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, reviewed_at TEXT, branch TEXT, head TEXT,
  base_ref TEXT, base_sha TEXT, diff_hash TEXT, context_hash TEXT,
  mode TEXT, model TEXT, adapter TEXT, status TEXT,
  parse_ok INTEGER, degraded INTEGER, diff_truncated INTEGER, trustworthy INTEGER,
  stop_reason TEXT, findings_total INTEGER, sev_high INTEGER, sev_medium INTEGER,
  sev_low INTEGER, summary TEXT, source TEXT DEFAULT 'skodun', artifact_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_diff ON reviews(diff_hash, trustworthy);
CREATE INDEX IF NOT EXISTS ix_reviews_branch ON reviews(branch, reviewed_at);
CREATE TABLE IF NOT EXISTS triage (
  ledger_key TEXT PRIMARY KEY, finding_key TEXT, review_id TEXT, branch TEXT,
  base_sha TEXT, file TEXT, line INTEGER, severity TEXT, title TEXT,
  dismissed_reason TEXT, dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_triage_scope ON triage(branch, base_sha);
CREATE TABLE IF NOT EXISTS gate_events (
  at TEXT, repo TEXT, branch TEXT, diff_hash TEXT, outcome TEXT,
  code INTEGER, note TEXT
);
"""

# --- migration ladder -------------------------------------------------------
#
# v1 *is* the Phase 1 baseline -- the tables in `_SCHEMA` above, which every
# store already has and which `executescript(_SCHEMA)` re-establishes
# idempotently -- so there is no separate v0->v1 delta to run. v2 adds
# `provider_state`. Deltas stay `IF NOT EXISTS` on purpose: the ladder is not
# wrapped in a transaction, so a crash between "delta applied" and "version
# stamped" must leave a database that simply replays the delta harmlessly on
# the next open rather than one that needs repair.
#
# `provider_state` deliberately lives ONLY here and not in `_SCHEMA`: that is
# what makes the ladder load-bearing rather than decorative.
_MIGRATION_V2 = """
CREATE TABLE IF NOT EXISTS provider_state (
  provider TEXT PRIMARY KEY, unavailable_until TEXT, reason TEXT,
  category TEXT, recorded_at TEXT
);
"""

# --- v3: the Phase 3 store, installed as ONE transaction --------------------
#
# THE WHOLE PHASE'S DDL LIVES HERE, deliberately. The ladder runs a delta only
# while `user_version < target`, so a store already stamped v3 would never
# receive a table or column a later task tried to add -- the change would land
# on fresh databases and silently miss every existing one. Later tasks may
# CONSUME this state; they may not extend it.
#
#   * `triage_events` -- the append-only triage stream (see the module
#     docstring). `finding_key` is a COLUMN because that is the key the gate's
#     `open_findings` tests membership by and the key `triage_for` returns its
#     map under; `ledger_key` is what groups one finding's history. The
#     migration SEEDS one `dismiss` event per existing `triage` row so that
#     every dismissal a human already recorded keeps its effect on the gate.
#   * `dedup_events` -- the dispatcher's suppression audit.
#   * `deliveries` -- which review rounds have been surfaced, and on which
#     channel.
#   * three `reviews` columns for background reviews: the persisted runtime
#     budget stale recovery reads, the worker pid, and the superseding record.
#
# UNLIKE the ladder above, this delta runs inside ONE explicit transaction with
# its own version stamp (see `_apply_atomic`), because `ALTER TABLE ADD COLUMN`
# is NOT replay-idempotent: a crash between the column-add and the stamp would
# leave a store that raises `duplicate column name` on every subsequent open --
# bricked, with thousands of reviews inside it. The statements are a tuple
# rather than one script for the same reason: `executescript` commits any
# pending transaction before it runs, which would defeat the transaction.
_MIGRATION_V3: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS triage_events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ledger_key TEXT, finding_key TEXT,
      event TEXT CHECK(event IN ('dismiss','reopen')), review_id TEXT, branch TEXT,
      base_sha TEXT, file TEXT, line INTEGER, severity TEXT, title TEXT,
      reason TEXT, at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS dedup_events (
      at TEXT, branch TEXT, diff_hash TEXT, matched_review_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS deliveries (
      review_id TEXT PRIMARY KEY, delivered_at TEXT, channel TEXT
    )""",
    "ALTER TABLE reviews ADD COLUMN worst_runtime_sec INTEGER",
    "ALTER TABLE reviews ADD COLUMN pid INTEGER",
    "ALTER TABLE reviews ADD COLUMN superseded_by TEXT",
    # LAST, and inside the same transaction: seeding the stream from the legacy
    # ledger. `ORDER BY rowid` so the seeded events land in the order the
    # dismissals were recorded -- `seq` is the total order everything
    # downstream reads, and it must not be arbitrary.
    """INSERT INTO triage_events (ledger_key, finding_key, event, review_id, branch,
         base_sha, file, line, severity, title, reason, at)
       SELECT ledger_key, finding_key, 'dismiss', review_id, branch, base_sha, file,
         line, severity, title, dismissed_reason, dismissed_at
       FROM triage ORDER BY rowid""",
)

# --- v4: a third triage verb, which SQLite makes a TABLE REBUILD ------------
#
# `defer` -- "real, not blast-radius for this change, filed as X" -- is the
# third event verb, and it needs two things v3 cannot be asked for in place:
#
#   * the CHECK constraint widened. v3 spelled the vocabulary INTO the table
#     (`event TEXT CHECK(event IN ('dismiss','reopen'))`) and SQLite has no
#     `ALTER TABLE ... ALTER CONSTRAINT`. Widening it is a rebuild: create a
#     replacement, copy every row, drop the original, rename. That is the whole
#     reason this delta exists and the reason it looks nothing like v3's.
#   * `tracking_ref`, the deferral's filed reference. It is a COLUMN and not a
#     convention inside `reason` because `triage --list` and `skodun deferrals`
#     must READ it back as a value: a reference buried in prose can only be
#     recovered by guessing at free text a human wrote, which is precisely the
#     "an unfiled deferral and an ignored finding are the same artifact" failure
#     the mandatory reference exists to prevent. The rebuild is happening
#     anyway, so the column costs one more name in a column list.
#
# TWO PROPERTIES ARE LOAD-BEARING, and both are pinned by tests:
#
#   1. `seq` VALUES are copied, not regenerated. `seq` is the total order every
#      effective-state read in this project resolves by (`triage_state`,
#      `triage_history`, `open_deferrals`), and `triage_history` hands those
#      numbers to whoever audits the ledger. A rebuild that renumbered them
#      would silently rewrite the order of decisions already recorded. The
#      explicit `seq` in the INSERT column list is that guarantee; `ORDER BY
#      seq` keeps the physical order matching it as well.
#   2. It runs inside ONE `BEGIN IMMEDIATE` (see `_apply_atomic`), and this
#      delta needs that even more than v3 did: it DROPS a shipped table. A crash
#      between the drop and the rename would leave a store with no triage ledger
#      at all -- every dismissal a human ever recorded gone -- and no version
#      stamp to say anything happened.
#
# `tracking_ref` is APPENDED rather than slotted beside `reason`, so every v3
# column keeps its v3 position: the rebuilt table is byte-for-byte the shape an
# `ALTER TABLE ADD COLUMN` would have produced had the CHECK not forced a
# rebuild, which keeps this delta's blast radius exactly one constraint wide.
_MIGRATION_V4: tuple[str, ...] = (
    """CREATE TABLE triage_events_v4 (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ledger_key TEXT, finding_key TEXT,
      event TEXT CHECK(event IN ('dismiss','reopen','defer')), review_id TEXT,
      branch TEXT, base_sha TEXT, file TEXT, line INTEGER, severity TEXT,
      title TEXT, reason TEXT, at TEXT, tracking_ref TEXT
    )""",
    """INSERT INTO triage_events_v4 (seq, ledger_key, finding_key, event, review_id,
         branch, base_sha, file, line, severity, title, reason, at)
       SELECT seq, ledger_key, finding_key, event, review_id, branch, base_sha,
         file, line, severity, title, reason, at
       FROM triage_events ORDER BY seq""",
    "DROP TABLE triage_events",
    "ALTER TABLE triage_events_v4 RENAME TO triage_events",
)

# --- v5: repository scoping -------------------------------------------------
#
# `reviews` was keyed by branch alone, so two repositories sharing one store
# collided on any common branch name: a push in one retired and SIGTERMed the
# other's running worker, and one `surface` call delivered AND acknowledged
# both repositories' rounds. The column carries `gitio.git_common_dir(repo)` --
# the same expression the foreground lock scopes by, so "the same repository"
# has one definition.
#
# NO BACKFILL. A pre-v5 row keeps `repo IS NULL` permanently and `repo = ?`
# excludes it from every scoped query, which is fail-closed: an old row goes
# invisible rather than the wrong repository's worker being killed. The
# accepted cost is that background rounds recorded before the upgrade are
# never delivered by `surface`.
_MIGRATION_V5: tuple[str, ...] = (
    "ALTER TABLE reviews ADD COLUMN repo TEXT",
    # The shipped `ix_reviews_branch` is kept (the Phase 1 additive rule); this
    # one leads with the column the scoped queries now filter on first.
    "CREATE INDEX IF NOT EXISTS ix_reviews_repo_branch"
    " ON reviews(repo, branch, reviewed_at)",
)

# --- v6: fair review capacity (epic S3) --------------------------------------
#
# Cross-process FIFO waiters and durable queue telemetry for `review-fg`.
# Replay-idempotent (`IF NOT EXISTS`): a crash before the version stamp simply
# recreates the empty table/index. Scoped by `git_common_dir` string, the same
# definition the FG lock uses for "one repository".
_MIGRATION_V6 = """
CREATE TABLE IF NOT EXISTS capacity_admissions (
  id TEXT PRIMARY KEY,
  resource_class TEXT NOT NULL,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  queued_at TEXT NOT NULL,
  admitted_at TEXT,
  started_at TEXT,
  ended_at TEXT,
  wait_ms INTEGER,
  expire_reason TEXT,
  pid INTEGER,
  review_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_capacity_scope_status
  ON capacity_admissions(resource_class, scope, status, queued_at, id);
"""

# --- v7: agent/human feedback ledger (non-gate) -----------------------------
#
# Append-only notes from agents or humans about findings, review quality, or
# skodun product bugs. Deliberately separate from `triage_events`: feedback
# never clears the gate. Replay-idempotent (`IF NOT EXISTS`).
_MIGRATION_V7 = """
CREATE TABLE IF NOT EXISTS feedback_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  review_id TEXT,
  finding_index INTEGER,
  provider TEXT,
  repo TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS ix_feedback_at ON feedback_events(at DESC, seq DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_kind ON feedback_events(kind, at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_review ON feedback_events(review_id, seq);
"""

# --- v8: metered API spend ledger (openai-api first) ------------------------
_MIGRATION_V8 = """
CREATE TABLE IF NOT EXISTS api_spend_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT,
  review_id TEXT,
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  request_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_api_spend_provider_day
  ON api_spend_events(provider, at);
"""

# --- v9: explicit stage telemetry and repository identity -----------------
#
# These are additive read-model fields.  Existing rows stay NULL so stats can
# report legacy coverage instead of pretending that an old timestamp had a
# completion meaning it never had.  The tuple is transactional because every
# ALTER TABLE is non-replayable after a partial crash.
_MIGRATION_V9: tuple[str, ...] = (
    "ALTER TABLE reviews ADD COLUMN review_started_at TEXT",
    "ALTER TABLE reviews ADD COLUMN review_completed_at TEXT",
    "ALTER TABLE reviews ADD COLUMN repo_id TEXT",
    "ALTER TABLE reviews ADD COLUMN worktree_root TEXT",
    "ALTER TABLE reviews ADD COLUMN orchestration_id TEXT",
    "ALTER TABLE reviews ADD COLUMN attempt_ordinal INTEGER",
    "ALTER TABLE reviews ADD COLUMN terminal_reason TEXT",
    "ALTER TABLE reviews ADD COLUMN outcome TEXT",
    "ALTER TABLE capacity_admissions ADD COLUMN queue_wait_ms INTEGER",
    "ALTER TABLE capacity_admissions ADD COLUMN run_ms INTEGER",
    "ALTER TABLE capacity_admissions ADD COLUMN total_admission_ms INTEGER",
    """CREATE INDEX IF NOT EXISTS ix_reviews_repo_id_started
       ON reviews(repo_id, review_started_at)""",
    """CREATE INDEX IF NOT EXISTS ix_reviews_orchestration
       ON reviews(orchestration_id, attempt_ordinal)""",
)

# --- v10: foreground exact-reuse audit -------------------------------------
#
# Reuse is a separate optimization from dispatcher suppression.  Its event
# stream records every opt-in probe, including misses and explicit bypasses,
# without rewriting the durable review it considered.
_MIGRATION_V10: tuple[str, ...] = (
"""CREATE TABLE IF NOT EXISTS reuse_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT NOT NULL,
  repo_id TEXT,
  worktree_root TEXT,
  branch TEXT,
  base_sha TEXT,
  diff_hash TEXT,
  context_hash TEXT,
  checklist_hash TEXT,
  tree_fingerprint TEXT,
  requested_reviewer TEXT,
  client_family TEXT,
  matched_review_id TEXT
)""",
"""CREATE INDEX IF NOT EXISTS ix_reuse_events_at
   ON reuse_events(at DESC, seq DESC)""",
"""CREATE INDEX IF NOT EXISTS ix_reuse_events_match
   ON reuse_events(matched_review_id, at DESC)""",
)

# --- v11: security-pass identity for exact foreground reuse ---------------
_MIGRATION_V11: tuple[str, ...] = (
    "ALTER TABLE reuse_events ADD COLUMN security_policy_hash TEXT",
)

# `(target_version, delta)`, applied in order. Keep it sorted ascending and keep
# the last target equal to SCHEMA_VERSION -- both are pinned by a test.
#
# A delta comes in one of two shapes, and the shape IS the contract:
#
#   * a `str` is `executescript`ed OUTSIDE any transaction. Every statement in
#     it must be replay-idempotent (`IF NOT EXISTS`), so a crash before the
#     version stamp simply replays it harmlessly. This is the shipped v2
#     contract, kept exactly.
#   * a `tuple[str, ...]` is applied inside one `BEGIN IMMEDIATE` together with
#     its own version stamp: all of it commits, or none of it does. This is
#     mandatory for any delta containing a statement that cannot be replayed --
#     v3's `ALTER TABLE ADD COLUMN` (which raises `duplicate column name` the
#     second time) and v4's table rebuild (whose replay would silently DROP
#     every stored `tracking_ref`) are both in this lane, and each is why it
#     exists.
#
# `test_no_non_transactional_delta_carries_a_non_idempotent_statement` pins the
# rule, because putting a delta in the wrong lane is invisible until a crash.
_MIGRATIONS: tuple[tuple[int, str | tuple[str, ...]], ...] = (
    (2, _MIGRATION_V2),
    (3, _MIGRATION_V3),
    (4, _MIGRATION_V4),
    (5, _MIGRATION_V5),
    (6, _MIGRATION_V6),
    (7, _MIGRATION_V7),
    (8, _MIGRATION_V8),
    (9, _MIGRATION_V9),
    (10, _MIGRATION_V10),
    (11, _MIGRATION_V11),
)


def _is_canonical_ts(value: object) -> bool:
    """True only for exactly `2026-07-28T12:00:00Z`.

    The regex is not redundant with `strptime`: `strptime` accepts
    `2026-7-8T1:2:3Z`, whose narrower fields destroy the fixed-width property
    that makes `<` on these strings a correct time comparison. Shape is
    checked by the regex, calendar validity (month 13, day 32) by `strptime`.
    """
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        return False
    try:
        time.strptime(value, _TS_FORMAT)
    except ValueError:
        return False
    return True


def _require_ts(label: str, value: object) -> str:
    if not _is_canonical_ts(value):
        raise ValueError(
            f"{label} must be an ISO-8601 UTC timestamp like 2026-07-28T12:00:00Z,"
            f" got {value!r}")
    return value            # type: ignore[return-value]


def _require_text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    return value.strip()


def _state_key(provider: str, quota_pool: str | None) -> str:
    """Encode a non-legacy pool in the existing provider_state key column."""
    provider = _require_text("provider", provider)
    if quota_pool is None:
        return provider
    quota_pool = _require_text("quota_pool", quota_pool)
    if quota_pool == provider:
        return provider
    return f"{provider}::{quota_pool}"


def _decode_state_key(key: str) -> tuple[str, str | None]:
    """Decode a pool-aware key; old provider-only rows remain unchanged."""
    if "::" not in key:
        return key, None
    provider, pool = key.split("::", 1)
    return provider, pool or None


def _iso_now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


def _wait_ms(queued_at: object, ended_at: object) -> int | None:
    """Milliseconds between two canonical store timestamps, or None if junk."""
    return _duration_ms(queued_at, ended_at)


def _duration_ms(start_at: object, end_at: object) -> int | None:
    """Non-negative milliseconds between two canonical timestamps."""
    import calendar

    if not isinstance(start_at, str) or not isinstance(end_at, str):
        return None
    if not _is_canonical_ts(start_at) or not _is_canonical_ts(end_at):
        return None
    try:
        start = time.strptime(start_at, _TS_FORMAT)
        end = time.strptime(end_at, _TS_FORMAT)
    except ValueError:
        return None
    delta = int((calendar.timegm(end) - calendar.timegm(start)) * 1000)
    return max(0, delta)


def _capacity_metrics(row: Mapping, ended_at: str) -> tuple[int | None, int | None,
                                                            int | None]:
    """Return queue-only, model-run, and compatibility-total durations."""
    values = dict(row)
    queue_wait = _duration_ms(values.get("queued_at"), values.get("admitted_at"))
    run_ms = _duration_ms(values.get("started_at"), ended_at)
    total = _duration_ms(values.get("queued_at"), ended_at)
    return queue_wait, run_ms, total


def _opt_positive_int(value: object) -> int | None:
    """A positive plain `int`, or None for everything else.

    Used for the nullable numeric review columns, whose NULL means "not set".
    `isinstance(True, int)` is True in Python, so the bool check is explicit and
    comes first; a float, a numeric string or a non-positive number is stored as
    NULL rather than coerced, because a column that means "how long this review
    may legitimately run" must never carry a value nobody computed.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _provider_state_bypassed(env: Mapping[str, str]) -> bool:
    """Unset, empty/whitespace-only, or exactly `"0"` -> state applies;
    anything else -> bypassed.

    No truthiness coercion anywhere near this: `bool("false")` is True, and a
    kill switch that reads "false" as "yes" is the exact bug class Phase 1 had
    to fix once already.

    Empty and whitespace-only count as unset rather than as an explicit
    bypass request: CI and container tooling routinely materialize an unset
    variable as `""` (`docker run -e VAR` with no host value; a GitHub
    Actions `env:` with an empty expression), and this cache exists
    specifically to stop hammering a rate-limited provider -- silently
    bypassing it on a materialized-empty value burns quota instead of saving
    it. This matches the polarity `passes._killed` already uses: a vague or
    blank value falls through to the default behaviour, not to the special
    one. Genuinely non-empty, non-"0" values (`"1"`, `"false"`, `"no"`, ...)
    still bypass -- the worst case there is one wasted provider attempt,
    whereas ignoring an operator's *explicit* opt-out is a provider they
    cannot reach at all.
    """
    raw = env.get(IGNORE_PROVIDER_STATE_ENV)
    if raw is None or raw.strip() == "":
        return False
    return raw != "0"


def _still_unavailable(until: object, now_iso: str) -> bool:
    """Whether a stored TTL is in the future.

    A TTL that is NULL or not in the canonical form cannot be ordered, so the
    row is treated as **inert** (available), never as "unavailable forever".
    One corrupt row must not be able to permanently disable a working provider.
    """
    return _is_canonical_ts(until) and now_iso < until   # type: ignore[operator]


def _apply_atomic(conn: sqlite3.Connection, target: int,
                  statements: tuple[str, ...]) -> None:
    """Apply one delta and stamp `target`, all inside a single transaction.

    Either the whole delta is in the database and the version says so, or
    nothing happened at all. That is not a nicety: a delta containing `ALTER
    TABLE ADD COLUMN` cannot be replayed (the second attempt raises `duplicate
    column name`), so a crash between the column-add and the version stamp
    would make EVERY later open of that store fail -- and the store holds
    thousands of reviews that are not recoverable from anywhere else. SQLite
    rolls back DDL, the seeded rows, and `PRAGMA user_version` alike, so the
    interrupted store simply comes back at its old version and migrates on the
    next open.

    `BEGIN IMMEDIATE` rather than a deferred `BEGIN`: the write lock is taken
    up front, so two processes opening the same store at once cannot both get
    part-way through the delta and have one of them fail at COMMIT time.

    THE VERSION IS RE-READ UNDER THE LOCK, and that is not belt-and-braces. The
    caller's version read happens outside any transaction, so two openers of the
    same store can both see the OLD version and both arrive here for the same
    delta. The first applies it; the second then waits for the write lock and
    replays an `ALTER TABLE ADD COLUMN` that now exists -- `duplicate column
    name`, i.e. `Store.open` RAISES for the loser. Two concurrent pre-push
    dispatchers on a fresh store is an ordinary case (two worktrees, or one push
    of two branches), and the loser's failure mode is the worst one available: no
    store means nowhere to record the failure, so that push gets no record at all.
    Re-reading here makes a lost race a NO-OP instead.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= target:
            # A concurrent opener applied this delta while we waited for the
            # lock. Nothing to do, and nothing to stamp.
            conn.execute("COMMIT")
            return
        for sql in statements:
            conn.execute(sql)
        # PRAGMA takes no bound parameters; the value is an int constant.
        conn.execute(f"PRAGMA user_version = {target:d}")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except BaseException:
            # A rollback that itself fails changes nothing about what the
            # caller has to be told, and closing the connection (which
            # `Store.open` does on any failure) rolls back anyway. The
            # original exception is the one that matters.
            pass
        raise


def _enable_wal(conn: sqlite3.Connection, attempts: int = 20) -> str:
    """Put the database in WAL mode, tolerating a concurrent opener doing the same.

    `PRAGMA journal_mode=WAL` is the ONE statement SQLite does not route through
    the busy handler: converting the journal takes a brief exclusive lock and
    returns `SQLITE_BUSY` IMMEDIATELY if another connection holds any lock,
    regardless of the connection's `timeout`. So the first-ever concurrent open of
    a store -- two pre-push dispatchers from two worktrees, or one push of two
    branches -- can make `Store.open` raise `database is locked` for the loser.
    That is the worst failure available to the dispatcher: no store means nowhere
    to record the failure, so that push gets no record at all.

    Retried with a short backoff, and NOT fatal if it still loses: the mode is a
    concurrency property, not a correctness one. Every writer here uses explicit
    transactions, so a store left in the rollback-journal mode is slower under
    concurrent readers and otherwise identical -- vastly better than refusing to
    open it. Returns the mode actually in force, for the caller that wants to know.
    """
    mode = ""
    for attempt in range(attempts):
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = (row[0] if row else "") or ""
            if mode.lower() == "wal":
                return mode
        except sqlite3.OperationalError:
            pass        # another opener holds a lock; it is converting it too
        time.sleep(0.02 * (attempt + 1))
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            if row and (row[0] or "").lower() == "wal":
                return row[0]       # a peer finished the conversion for us
        except sqlite3.OperationalError:     # pragma: no cover - defensive
            pass
    return mode


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an open connection's database up to `SCHEMA_VERSION`.

    The order of these four steps is load-bearing and is pinned by
    `test_future_schema_refused_before_any_ddl`:

    1. read `user_version`;
    2. refuse a store written by a newer skodun -- **before anything writes**.
       Not merely before the DDL: `PRAGMA journal_mode=WAL` rewrites the file
       header too, and a store we do not understand (holding reviews we cannot
       interpret) must come back byte-identical;
    3. apply the ordered deltas above the current version, each in the lane its
       shape declares (see `_MIGRATIONS`);
    4. stamp the new version -- if a transactional delta has not already
       stamped it. The final read is deliberately of the DATABASE rather than
       of the `version` local: a delta that stamped its own target inside its
       transaction must not be followed by a redundant write to a store that
       is already correct.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError(schema_too_new_message(version))

    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)         # v1 baseline, idempotent
    for target, delta in _MIGRATIONS:
        if version < target:
            if isinstance(delta, str):
                conn.executescript(delta)       # replay-idempotent, no transaction
            else:
                _apply_atomic(conn, target, delta)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")


def _requires_usable_output(rec: Mapping) -> bool:
    """Whether `usable_output` is MANDATORY on this record.

    The predicate is `source == "skodun" and mode == "prepush"`, and both halves
    are load-bearing. A mode-only rule would reject the shipped legacy import
    outright: `legacy_import._import_index` merges a foreign index row and
    artifact VERBATIM, and a legacy archive row carries `mode="prepush"` with
    `source="legacy"` and no such field -- it predates the concept.

    The field itself is the whole "did this round produce any answer at all"
    fact, and it is deliberately not derivable from the finding count: a round
    whose passes all answered "nothing wrong" and a round that failed before any
    answer both have zero findings, and the difference between them is a clean
    review versus "NO REVIEW HAPPENED".
    """
    return (rec.get("source") == SKODUN_SOURCE
            and rec.get("mode") == PREPUSH_MODE)


def _normalize_record(rec: dict, *, label: str) -> dict:
    """THE persistence chokepoint's validation, shared by both writers.

    ONE routine behind `save_review` and `finalize_review`, because two copies
    of it would be two answers to "may this record certify a push". A worker
    that reached the store through the conditional finalize must be held to
    exactly the standard the foreground save applies:

    * the three trust axes are EXACT bools -- `bool("false")` is True, and a
      coerced axis is how a degraded review becomes a trustworthy one;
    * `trustworthy` is RECOMPUTED and the caller's value overwritten, in both
      directions (a pessimistic caller does not get to veto trust either);
    * `usable_output` is validated conditionally (see `_requires_usable_output`)
      and, whenever present, must be an exact bool.

    Returns a COPY: the caller's dict is never mutated (a shipped guarantee --
    `test_save_review_does_not_mutate_caller_dict`).
    """
    rec = dict(rec)
    axes = {k: rec.get(k, False) for k in _TRUST_AXES}
    for k, v in axes.items():
        if not isinstance(v, bool):   # bool("false") is True — refuse coercion
            raise ValueError(f"{label}: {k} must be bool, got {type(v).__name__}")
    rec.update(axes)
    rec["trustworthy"] = is_trustworthy(**axes)
    if "usable_output" in rec:
        if not isinstance(rec["usable_output"], bool):
            raise ValueError(
                f"{label}: usable_output must be bool, got "
                f"{type(rec['usable_output']).__name__}")
    elif _requires_usable_output(rec):
        raise ValueError(
            f"{label}: usable_output is required on a {SKODUN_SOURCE} "
            f"{PREPUSH_MODE} record (it is the only field that can tell a clean "
            f"round from a round that produced no answer at all)")
    # v9 telemetry is additive.  New callers may omit the derived fields, but
    # when a canonical legacy timestamp is available it is safe to identify it
    # as the start of the recorded round.  Completion is assigned only for a
    # terminal write; old rows remain NULL because migrations never backfill.
    started = rec.get("review_started_at")
    if started is None and _is_canonical_ts(rec.get("reviewed_at")):
        rec["review_started_at"] = rec["reviewed_at"]
    elif started is not None:
        rec["review_started_at"] = _require_ts(
            f"{label}: review_started_at", started)
    if rec.get("status") != RUNNING and rec.get("review_completed_at") is None:
        rec["review_completed_at"] = _iso_now()
    elif rec.get("review_completed_at") is not None:
        rec["review_completed_at"] = _require_ts(
            f"{label}: review_completed_at", rec["review_completed_at"])
    if rec.get("repo_id") is None and rec.get("repo") is not None:
        rec["repo_id"] = rec["repo"]
    if rec.get("repo_id") is not None:
        rec["repo_id"] = _require_text(f"{label}: repo_id", rec["repo_id"])
    if rec.get("worktree_root") is not None:
        rec["worktree_root"] = _require_text(
            f"{label}: worktree_root", rec["worktree_root"])
    if rec.get("orchestration_id") is not None:
        rec["orchestration_id"] = _require_text(
            f"{label}: orchestration_id", rec["orchestration_id"])
    ordinal = rec.get("attempt_ordinal")
    if ordinal is not None and (
            isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0):
        raise ValueError(
            f"{label}: attempt_ordinal must be a non-negative int, got {ordinal!r}")
    if rec.get("terminal_reason") is not None:
        rec["terminal_reason"] = _require_text(
            f"{label}: terminal_reason", rec["terminal_reason"])
    elif rec.get("status") != RUNNING:
        reason = rec.get("failure_reason") or rec.get("stop_reason")
        if isinstance(reason, str) and reason.strip():
            rec["terminal_reason"] = reason.strip()
    if rec.get("outcome") is not None:
        rec["outcome"] = _require_text(f"{label}: outcome", rec["outcome"])
    elif rec.get("status") != RUNNING and isinstance(rec.get("status"), str):
        rec["outcome"] = rec["status"]
    return rec


#: The `reviews` columns both writers bind, in one order, from a normalized
#: record. ONE list: an `INSERT` and an `UPDATE` that disagreed about which
#: columns a record owns would be two records at one id.
_REVIEW_COLUMNS = (
    "reviewed_at", "branch", "head", "base_ref", "base_sha", "diff_hash",
    "context_hash", "mode", "model", "adapter", "status", "parse_ok", "degraded",
    "diff_truncated", "trustworthy", "stop_reason", "findings_total", "sev_high",
    "sev_medium", "sev_low", "summary", "source", "artifact_json",
    "worst_runtime_sec", "pid", "superseded_by", "repo",
    "review_started_at", "review_completed_at", "repo_id", "worktree_root",
    "orchestration_id", "attempt_ordinal", "terminal_reason", "outcome",
)


def _review_values(rec: Mapping) -> tuple:
    """The bind tuple for `_REVIEW_COLUMNS`, from an ALREADY-normalized record.

    `artifact_json` is serialized from the SAME dict the indexed columns are
    read from, which is what makes an index row that disagrees with its artifact
    impossible by construction (the Phase 1 rule).

    THIS TUPLE IS HAND-WRITTEN AND POSITIONAL: it is not derived from
    `_REVIEW_COLUMNS`, while `_INSERT_REVIEW` and `_FINALIZE_REVIEW` size their
    placeholders from that list. Adding a column to one without the other is a
    `sqlite3.ProgrammingError` on every review write.
    """
    sev = rec.get("severity") or {}
    return (
        rec.get("reviewed_at"), rec.get("branch"), rec.get("head"),
        rec.get("base_ref"), rec.get("base_sha"), rec.get("diff_hash"),
        rec.get("context_hash", ""), rec.get("mode"), rec.get("model"),
        rec.get("adapter"), rec.get("status"), int(bool(rec.get("parse_ok"))),
        int(bool(rec.get("degraded"))), int(bool(rec.get("diff_truncated"))),
        int(bool(rec.get("trustworthy"))), rec.get("stop_reason"),
        int(rec.get("findings_total") or 0), int(sev.get("high") or 0),
        int(sev.get("medium") or 0), int(sev.get("low") or 0),
        rec.get("summary"), rec.get("source", SKODUN_SOURCE),
        json.dumps(rec, ensure_ascii=False),
        _opt_positive_int(rec.get("worst_runtime_sec")),
        _opt_positive_int(rec.get("pid")), rec.get("superseded_by"),
        rec.get("repo"),
        rec.get("review_started_at"), rec.get("review_completed_at"),
        rec.get("repo_id"), rec.get("worktree_root"),
        rec.get("orchestration_id"), rec.get("attempt_ordinal"),
        rec.get("terminal_reason"), rec.get("outcome"),
    )


_INSERT_REVIEW = (
    "INSERT INTO reviews (id, %s) VALUES (?,%s)\n"
    "ON CONFLICT(id) DO UPDATE SET %s"
    % (", ".join(_REVIEW_COLUMNS),
       ",".join("?" * len(_REVIEW_COLUMNS)),
       ", ".join(f"{c}=excluded.{c}" for c in _REVIEW_COLUMNS)))

#: The conditional finalize's UPDATE. Identity-pinned by the caller and guarded
#: on `status='running'` HERE, in the statement itself, so a record that stopped
#: running between the read and the write changes nothing.
#:
#: DELIBERATELY REDUNDANT with `finalize_review`'s own `row["status"] != RUNNING`
#: early return: both sit inside one `BEGIN IMMEDIATE`, so the row cannot change
#: between them and either alone suffices today. The pair is kept because they
#: guard against different future mistakes -- the early return against a caller
#: reading a stale record, the predicate against a refactor that moves the read
#: out of the transaction -- and the cost is that neither can be mutation-tested
#: in isolation. `test_finalize_review_refuses_a_superseded_record_and_changes_
#: nothing` dies when BOTH are removed, which is the mutation the brief names.
_FINALIZE_REVIEW = (
    "UPDATE reviews SET %s WHERE id=? AND status='%s'"
    % (", ".join(f"{c}=?" for c in _REVIEW_COLUMNS), RUNNING))

#: The reserved identity fields a finalize must agree with the stored row about.
#: Not `worst_runtime_sec` or `pid`: those are database-owned VALUES, merged
#: rather than compared (see `finalize_review`). These five are the review's
#: IDENTITY -- what content it is about -- and a worker that recomputed one of
#: them differently has reviewed something else.
_RESERVED_IDENTITY = ("branch", "head", "base_ref", "base_sha", "diff_hash")

#: The atomic FAILURE transition, in ONE statement (a single statement is its own
#: transaction in autocommit mode). Status and the trust axes move together,
#: index and artifact together -- because `status='failed'` beside
#: `trustworthy=1` is a row the gate still honours and dedup still suppresses
#: against, which is exactly the stale-recovery bug.
#:
#: `json('false')` and NOT a bound Python `False`: a bound boolean lands in
#: `json_set` as the NUMBER 0, which reloads as `int` and makes the artifact
#: malformed under the strict-bool trust rules -- a demotion that quietly
#: produced an unreadable artifact instead of an untrustworthy one.
_FAIL_REVIEW = """
UPDATE reviews SET status='failed', parse_ok=0, trustworthy=0,
  artifact_json=json_set(artifact_json,
    '$.status', 'failed',
    '$.failure_reason', ?,
    '$.parse_ok', json('false'),
    '$.trustworthy', json('false'))
WHERE id=?"""


@dataclass(frozen=True)
class Reservation:
    """What one `reserve_prepush` transaction decided.

    Exactly one of the first two fields is set:

    * `record_id` -- the reserved `running` record's id. A worker may now be
      spawned for it, and nothing else.
    * `suppressed_by` -- the id of the trustworthy terminal review this push's
      diff is already covered by. No record was written and no worker may run.

    `superseded` is the set of rows this reservation RETIRED -- `{"id", "pid"}`
    each -- and it is RETURNED by the transaction rather than re-queried
    afterwards, because a post-hoc query races: a third dispatcher committing in
    between would have retired our own row too, and we would signal its worker.
    A tuple, so a caller cannot append to the audit of what it retired.
    """

    record_id: str | None = None
    suppressed_by: str | None = None
    superseded: tuple[dict, ...] = field(default_factory=tuple)


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path | None = None):
        self._c = conn
        #: The file this store lives in, when it was opened from one. Only
        #: `log_dir` reads it, and it falls back to asking SQLite itself.
        self._path = None if path is None else Path(path)

    @classmethod
    def open(cls, path: Path) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            _migrate(conn)
        except BaseException:
            conn.close()        # never leave a refused store open or locked
            raise
        return cls(conn, path)

    def close(self) -> None:
        """Close the underlying connection. Idempotent.

        `sqlite3.Connection.close()` is already a no-op on a connection that
        is already closed, so this only has to forward to it -- no extra
        "already closed" bookkeeping to get subtly out of sync with the
        connection's own state. Anything called on this `Store` afterwards
        raises `sqlite3.ProgrammingError` from the connection itself; nothing
        here catches or downgrades that.
        """
        self._c.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # No `except`/return-True here: whatever exception the caller's body
        # raised (or didn't) propagates exactly as it would without this
        # context manager. Closing is the only side effect exiting adds.
        self.close()

    def log_dir(self) -> Path:
        """`<db path>.logs/`, created on first use.

        Where a DETACHED worker's stderr goes. It has to be derivable from the
        store alone: the dispatcher opens the log file before the worker exists,
        and the worker itself only ever learns `SKODUN_DB` -- so any other
        location would need a second piece of configuration that could disagree.

        A sibling of the database rather than a subdirectory of it, so it travels
        with the store when `SKODUN_DB` is repointed and cannot be mistaken for
        part of the SQLite file set.
        """
        path = self._path
        if path is None:
            # A `Store` built straight from a connection (a test, an in-memory
            # database): ask SQLite where `main` actually lives rather than
            # inventing a path.
            row = self._c.execute(
                "SELECT file FROM pragma_database_list WHERE name='main'").fetchone()
            raw = (row[0] if row else "") or ""
            if not raw:
                raise ValueError(
                    "this store has no file on disk, so it has no log directory")
            path = Path(raw)
        directory = Path(str(path) + ".logs")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_review(self, rec: dict) -> None:
        self._write_review(_normalize_record(rec, label="save_review"))

    def _write_review(self, rec: Mapping) -> None:
        """Upsert an ALREADY-normalized record. Never call with a raw dict.

        Private because normalization is not optional: this is the statement
        behind both `save_review` and `reserve_prepush`, and the only reason it
        is factored out is that the reservation runs it INSIDE its own
        transaction while `save_review` runs it in autocommit.
        """
        self._c.execute(_INSERT_REVIEW, (rec["id"],) + _review_values(rec))

    def get_review(self, review_id: str) -> dict | None:
        row = self._c.execute("SELECT artifact_json FROM reviews WHERE id=?",
                              (review_id,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def reuse_candidates(self, repo_id: str, base_sha: str,
                         diff_hash: str) -> list[dict]:
        """Read possible exact-reuse candidates; validation stays in reuse.py."""
        repo_id = _require_text("repo_id", repo_id)
        base_sha = _require_text("base_sha", base_sha)
        diff_hash = _require_text("diff_hash", diff_hash)
        rows = self._c.execute(
            """SELECT artifact_json FROM reviews
               WHERE repo_id=? AND base_sha=? AND diff_hash=?
                 AND trustworthy=1 AND COALESCE(status, '') <> ?
               ORDER BY reviewed_at DESC, id DESC""",
            (repo_id, base_sha, diff_hash, RUNNING)).fetchall()
        out = []
        for row in rows:
            try:
                value = json.loads(row["artifact_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    def append_reuse_event(
            self, *, at: str, outcome: str, reason: str,
            repo_id: str | None = None, worktree_root: str | None = None,
            branch: str | None = None, base_sha: str | None = None,
            diff_hash: str | None = None, context_hash: str | None = None,
            checklist_hash: str | None = None,
            tree_fingerprint: str | None = None,
            security_policy_hash: str | None = None,
            requested_reviewer: str | None = None,
            client_family: str | None = None,
            matched_review_id: str | None = None) -> dict:
        """Append one foreground reuse probe event and return its stored row."""
        at = _require_ts("at", at)
        outcome = _require_text("outcome", outcome)
        if outcome not in _REUSE_OUTCOMES:
            raise ValueError(
                "outcome must be one of: hit, miss, bypass, error")
        reason = _require_text("reason", reason)
        values = {
            "repo_id": repo_id, "worktree_root": worktree_root,
            "branch": branch, "base_sha": base_sha, "diff_hash": diff_hash,
            "context_hash": context_hash, "checklist_hash": checklist_hash,
            "tree_fingerprint": tree_fingerprint,
            "security_policy_hash": security_policy_hash,
            "requested_reviewer": requested_reviewer,
            "client_family": client_family,
            "matched_review_id": matched_review_id,
        }
        for name, value in values.items():
            if value is not None:
                values[name] = _require_text(name, value)
        cur = self._c.execute(
            """INSERT INTO reuse_events
               (at, outcome, reason, repo_id, worktree_root, branch, base_sha,
                diff_hash, context_hash, checklist_hash, tree_fingerprint,
                security_policy_hash, requested_reviewer, client_family,
                matched_review_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (at, outcome, reason, values["repo_id"], values["worktree_root"],
             values["branch"], values["base_sha"], values["diff_hash"],
             values["context_hash"], values["checklist_hash"],
             values["tree_fingerprint"], values["security_policy_hash"],
             values["requested_reviewer"],
             values["client_family"], values["matched_review_id"]))
        seq = int(cur.lastrowid)
        row = self._c.execute(
            "SELECT * FROM reuse_events WHERE seq=?", (seq,)).fetchone()
        assert row is not None
        return dict(row)

    def reuse_events(self, *, since_iso: str | None = None,
                     limit: int = 100) -> list[dict]:
        """Read reuse events newest first without changing the audit stream."""
        if since_iso is not None:
            since_iso = _require_ts("since_iso", since_iso)
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or limit < 1):
            limit = 100
        limit = min(limit, 1000)
        where = " WHERE at>=?" if since_iso is not None else ""
        args: tuple = (since_iso, limit) if since_iso is not None else (limit,)
        rows = self._c.execute(
            f"SELECT * FROM reuse_events{where} ORDER BY at DESC, seq DESC LIMIT ?",
            args).fetchall()
        return [dict(row) for row in rows]

    def latest_trustworthy_for(self, diff_hash: str) -> dict | None:
        row = self._c.execute(
            """SELECT artifact_json FROM reviews
               WHERE diff_hash=? AND trustworthy=1
               ORDER BY reviewed_at DESC LIMIT 1""", (diff_hash,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    # --- the two atomic FAILURE transitions --------------------------------
    #
    # `set_status` used to live here. It wrote a status and NOTHING else, so
    # every one of its callers could leave a row saying `status='failed'` beside
    # `trustworthy=1` -- a row the gate still passes and dedup still suppresses
    # against. It is retired entirely rather than kept as a deprecated shell:
    # the two shapes below are the only failure transitions that exist, and each
    # demotes the trust axes in the same statement as the status.

    def mark_failed(self, review_id: str, reason: str) -> bool:
        """Demote `review_id` to `failed`, trust axes and all. UNCONDITIONAL.

        Returns whether a row changed. No `status='running'` guard, and that is
        the point of having it separate from `fail_if_running`: its one call site
        is the FOREGROUND cleanup, where `_persist` has already autocommitted the
        final save before its readback. A readback failure therefore has to
        demote a record that is already `clean` -- and a guard would leave
        exactly the stale-recovery bug one call site over, a `failed` row still
        carrying `trustworthy=1`.

        `reason` lands in the artifact's `failure_reason`; nothing else about the
        artifact is touched.
        """
        cur = self._c.execute(_FAIL_REVIEW, (reason, review_id))
        return cur.rowcount == 1

    def mark_cancelled(self, review_id: str, reason: str) -> bool:
        """Demote an ALREADY-TERMINAL record because its review was cancelled.

        The worker's POST-COMMIT linearization check, and the only transition that
        acts on a record that is no longer `running`. It exists because a SIGTERM
        can land while SQLite holds the write lock for `finalize_review`: the
        worker's pre-check injects BEFORE that call and cannot see it, so without
        this a killed review would be committed as a trustworthy one.

        It is `cancellation_transform` expressed as ONE atomic statement, so the
        record's shape does not depend on which of the three cancellation paths
        demoted it: `degraded` (not `parse_ok`) is the axis that moves, because
        the reviewer's output really did parse -- what is untrue is that the round
        finished. `findings`, `findings_total` and `usable_output` are left ALONE:
        a round cancelled after two batches answered really did produce those
        findings, and a surface that dropped them would print "NO REVIEW HAPPENED"
        over real evidence.

        Guarded on `trustworthy=1`, which makes it self-limiting rather than
        merely idempotent: a record that is already untrustworthy needs no
        demotion, and the guard means an unnecessary call cannot rewrite a reason
        onto a record some other transition already settled. Nothing else can be
        racing it -- the supersede and the stale sweep both require
        `status='running'`, which a finalized record no longer has.

        `json('true')`/`json('false')`, never a bound Python bool: a bound boolean
        lands in `json_set` as the NUMBER 1/0, which reloads as `int` and makes
        the artifact malformed under the strict-bool trust rules.
        """
        cur = self._c.execute(
            """UPDATE reviews SET status='failed', degraded=1, trustworthy=0,
                 artifact_json=json_set(artifact_json,
                   '$.status', 'failed',
                   '$.degraded', json('true'),
                   '$.degraded_reason', ?,
                   '$.failure_reason', ?,
                   '$.trustworthy', json('false'))
               WHERE id=? AND trustworthy=1""",
            (reason, reason, review_id))
        return cur.rowcount == 1

    def fail_if_running(self, review_id: str, reason: str) -> bool:
        """`mark_failed`, but only while the record is still `running`.

        Stale recovery's terminal transition. Returns whether it applied.

        CONDITIONAL because this is a janitor, and the two things it races with
        both have a better answer than it does: a worker finalizing a real
        review, and a dispatcher superseding this row for a newer push. Whichever
        terminal transition commits FIRST survives; the loser changes nothing.
        (The shipped unconditional `set_status` in this path could overwrite a
        clean, trustworthy record with a guess -- and, worse, could leave
        `status='failed'` beside `trustworthy=1` when a racing writer had already
        rewritten the row.)
        """
        cur = self._c.execute(
            _FAIL_REVIEW + f" AND status='{RUNNING}'", (reason, review_id))
        return cur.rowcount == 1

    # --- the reservation lease ---------------------------------------------

    def reserve_prepush(self, branch: str, head: str, base_ref: str,
                        base_sha: str, diff_hash: str, worst_runtime_sec: int,
                        evidence, *, repo: str, now: str | None = None,
                        id_prefix: str = "sk_") -> Reservation:
        """Decide dedup, retire the branch's older runs, and reserve one record.

        ONE `BEGIN IMMEDIATE` transaction, and everything below is inside it
        because each step is only sound while the write lock is held:

        1. **The AUTHORITATIVE dedup decision.** The dispatcher's `evidence` is
           evidence, never a verdict: a racing dispatcher may finalize a
           trustworthy review between the probe and this lease, which is exactly
           the finalized-during-probe case this closes. The match query, the full
           artifact validation and the context rules all happen here.
        2. **The audit row**, in the same transaction as the suppression it
           records. A suppression that committed without its `dedup_events` row
           would be a skipped review with no trace of why.
        3. **The supersede**, SCOPED TO `repo`, with `superseded_by` persisted
           to the index AND the artifact atomically, and the retired rows
           RETURNED rather than re-queried afterwards.
        4. **The insert** of the new `running` row, `pid=NULL`.

        SQLite's write lock serializes racing dispatchers, so whichever
        transaction commits second supersedes the first's row and exactly one
        `running` prepush row per branch survives.

        The base identity (`base_ref`, `base_sha`) is reservation-owned: it is
        written here and `finalize_review` refuses any record that disagrees
        with it.

        `repo` is REQUIRED and has no default, because a default would have to
        mean "match every repository" -- and one branch name is shared by every
        checkout that ever pushed to this store, so an unscoped supersede
        retires (and SIGTERMs the worker of) a running review that belongs to
        somebody else's tree. It is the string form of
        `gitio.git_common_dir(repo_path)`, computed by the CALLER: the store
        never shells out to git.
        """
        at = _iso_now() if now is None else _require_ts("now", now)
        self._c.execute("BEGIN IMMEDIATE")
        try:
            matched = self._suppression_candidate(diff_hash, base_sha, evidence)
            if matched is not None:
                self._c.execute(
                    "INSERT INTO dedup_events (at, branch, diff_hash,"
                    " matched_review_id) VALUES (?,?,?,?)",
                    (at, branch, diff_hash, matched))
                self._c.execute("COMMIT")
                return Reservation(suppressed_by=matched)

            record_id = ids.new_review_id(id_prefix)
            # BOTH statements carry `repo=?`, and both must: the SELECT decides
            # which workers get signalled, the UPDATE decides which rows get
            # retired, and a scope on one alone is a half-fix. Pre-v5 rows hold
            # `repo=NULL`, which `repo=?` never matches -- deliberately, since a
            # row that cannot say which tree it belongs to must not be retired
            # on the strength of its branch name alone.
            rows = self._c.execute(
                "SELECT id, pid FROM reviews"
                " WHERE repo=? AND branch=? AND mode=? AND status=?",
                (repo, branch, PREPUSH_MODE, RUNNING)).fetchall()
            retired = tuple({"id": r["id"], "pid": r["pid"]} for r in rows)
            if retired:
                self._c.execute(
                    """UPDATE reviews SET status='superseded', superseded_by=?,
                         artifact_json=json_set(artifact_json,
                           '$.status', 'superseded', '$.superseded_by', ?)
                       WHERE repo=? AND branch=? AND mode=? AND status=?""",
                    (record_id, record_id, repo, branch, PREPUSH_MODE, RUNNING))
            # THE reserved record's exact initial shape. Strict bools throughout
            # (`trustworthy` recomputes False from them at the chokepoint), a
            # `usable_output` of False because nothing has answered yet, and the
            # runtime budget already on the row -- that is the only moment at
            # which stale recovery can learn not to sweep this row at the
            # single-review ceiling.
            self._write_review(_normalize_record(dict(
                id=record_id, reviewed_at=at, branch=branch, head=head,
                base_ref=base_ref, base_sha=base_sha, diff_hash=diff_hash,
                mode=PREPUSH_MODE, source=SKODUN_SOURCE, status=RUNNING,
                parse_ok=False, degraded=False, diff_truncated=False,
                findings=[], findings_total=0, summary="", failure_reason=None,
                usable_output=False, worst_runtime_sec=worst_runtime_sec,
                pid=None, superseded_by=None, repo=repo,
            ), label="reserve_prepush"))
            self._c.execute("COMMIT")
            return Reservation(record_id=record_id, superseded=retired)
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except BaseException:
                pass    # the original failure is the one the caller must see
            raise

    def _suppression_candidate(self, diff_hash: str, base_sha: str,
                               evidence) -> str | None:
        """The id of a review this push may be suppressed against, or None.

        Called INSIDE the reservation transaction. Every check below is one the
        GATE itself would apply, because the whole promise of a suppression is
        "the gate will already pass this content" -- and a suppression that
        skipped a check the gate makes would leave the push with no record any
        gate accepts.

        In order:

        * dedup is enabled at all (the `[dispatch] dedup` kill switch; the
          `valid` half is `dispatch.evidence_permits_suppression`'s, deliberately
          NOT re-spelled here -- one definition of "may this evidence suppress");
        * the newest TRUSTWORTHY, TERMINAL record of this `diff_hash`. Terminal,
          not merely trustworthy: an in-flight review certifies nothing;
        * `load_valid_artifact` in FULL -- the gate's own validator. A malformed
          artifact with clean axes must not suppress what the gate would reject;
        * the trust axes RECOMPUTED from the artifact, strictly. `is_trustworthy`
          coerces by truthiness, so an artifact carrying `1`/`0` would pass any
          non-strict read while being malformed to every strict one;
        * the artifact's own stored `trustworthy` agreeing with that recompute;
        * the indexed `id`/`diff_hash` agreeing with the artifact's;
        * `base_sha` equality with THIS reservation's base -- the gate's own
          mandatory rebase check. A same-patch rebase must never be suppressed:
          the gate would answer 2 for it;
        * finally the context rules, with the candidate's context hash.

        Any failure is None, i.e. "review it". There is no path from an
        uncertainty to a suppression.
        """
        # Lazy, and inside the function on purpose: `dispatch` is the
        # dispatcher's own module and `triage` is the gate's, and neither belongs
        # in the store's import graph at load time. (The same pattern
        # `chain.py` uses for its `pipeline` helpers.)
        from . import dispatch as dispatch_mod
        from .triage import load_valid_artifact

        if getattr(evidence, "enabled", None) is not True:
            return None
        row = self._c.execute(
            """SELECT id, diff_hash, base_sha, artifact_json FROM reviews
               WHERE diff_hash=? AND trustworthy=1
                 AND COALESCE(status, '') <> ?
               ORDER BY reviewed_at DESC LIMIT 1""",
            (diff_hash, RUNNING)).fetchone()
        if row is None:
            return None
        try:
            artifact = json.loads(row["artifact_json"])
        except (TypeError, ValueError):
            return None
        try:
            load_valid_artifact(artifact)
        except Exception:
            return None
        axes = [artifact.get(k) for k in _TRUST_AXES]
        if any(not isinstance(v, bool) for v in axes):
            return None
        if not is_trustworthy(*axes):
            return None
        if artifact.get("trustworthy") is not True:
            return None
        if artifact.get("id") != row["id"]:
            return None
        if artifact.get("diff_hash") != row["diff_hash"]:
            return None
        if artifact.get("diff_hash") != diff_hash:
            return None
        if row["base_sha"] != base_sha or artifact.get("base_sha") != base_sha:
            return None
        if not dispatch_mod.evidence_permits_suppression(artifact, evidence):
            return None
        return row["id"]

    def attach_pid(self, review_id: str, pid: int) -> bool:
        """Record the worker's pid on a still-`running`, still-pidless record.

        Returns whether it applied. False means a racing dispatch superseded this
        reservation between the lease and the spawn (or something already
        attached a pid), and the caller's freshly-spawned child must be
        terminated -- it would otherwise review content whose record is already
        terminal, and overlap the replacement review on one inference backend.

        The guard is `status='running' AND pid IS NULL`, which is what makes the
        answer meaningful rather than advisory.
        """
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"attach_pid: pid must be a positive int, got {pid!r}")
        cur = self._c.execute(
            """UPDATE reviews SET pid=?,
                 artifact_json=json_set(artifact_json, '$.pid', ?)
               WHERE id=? AND status=? AND pid IS NULL""",
            (pid, pid, review_id, RUNNING))
        return cur.rowcount == 1

    def finalize_review(self, record_id: str, rec: dict) -> bool:
        """Apply a worker's completed record to its reservation, CONDITIONALLY.

        Returns True when the record was applied, False when the reservation is
        no longer `running` -- superseded by a newer push, or already recovered
        as stale -- in which case NOTHING changes. A late worker can therefore
        never overwrite a terminal record, which is the whole reason the
        dispatcher may retire a run it could not signal.

        Raises `ValueError` (and changes nothing) when the record is not the
        reservation's: a mismatched `id`, or any of the five reserved identity
        fields disagreeing with the stored row. Those are never silent
        overwrites, because a worker that recomputed a different `diff_hash` has
        reviewed different content and would publish it at this record's id.

        The identity read, the database-owned-field merge (`pid`,
        `superseded_by`), the normalization and the conditional UPDATE all run
        under ONE `BEGIN IMMEDIATE`. The shipped store is in autocommit, so
        without it a pid attach committing between the re-read and the update
        would be erased by the stale merge -- and the record would then look like
        a worker that never started, which is precisely what stale recovery acts
        on.

        Normalization is `save_review`'s, exactly (see `_normalize_record`): a
        worker cannot reach the store by a laxer road than the foreground does.
        """
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(
                f"finalize_review: record_id must be a non-empty string, got "
                f"{record_id!r}")
        if rec.get("id") != record_id:
            raise ValueError(
                f"finalize_review: the record's id {rec.get('id')!r} is not the "
                f"reservation {record_id!r}; refusing to overwrite another record")
        self._c.execute("BEGIN IMMEDIATE")
        try:
            row = self._c.execute(
                "SELECT status, pid, superseded_by, branch, head, base_ref,"
                " base_sha, diff_hash FROM reviews WHERE id=?",
                (record_id,)).fetchone()
            if row is None:
                self._c.execute("COMMIT")
                return False
            for name in _RESERVED_IDENTITY:
                if rec.get(name) != row[name]:
                    raise ValueError(
                        f"finalize_review: {name} moved under the reservation "
                        f"{record_id!r}: reserved {row[name]!r}, record "
                        f"{rec.get(name)!r}")
            if row["status"] != RUNNING:
                self._c.execute("COMMIT")
                return False
            merged = dict(rec)
            # DATABASE-owned: the dispatcher wrote them after the worker's dict
            # was built, so the worker's values are stale by construction.
            merged["pid"] = row["pid"]
            merged["superseded_by"] = row["superseded_by"]
            merged = _normalize_record(merged, label="finalize_review")
            cur = self._c.execute(
                _FINALIZE_REVIEW, _review_values(merged) + (record_id,))
            applied = cur.rowcount == 1
            self._c.execute("COMMIT")
            return applied
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except BaseException:
                pass
            raise

    def log_gate_event(self, rec: dict) -> None:
        self._c.execute(
            "INSERT INTO gate_events (at, repo, branch, diff_hash, outcome, code, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (rec.get("at"), rec.get("repo"), rec.get("branch"), rec.get("diff_hash"),
             rec.get("outcome"), rec.get("code"), rec.get("note")))

    # --- the append-only triage event stream --------------------------------
    #
    # The three verbs, and the ONE writer behind them. Effective state is the
    # last event by `seq` (see `triage_state`), so a re-dismissal after a reopen
    # is just another `dismiss` event -- there is no "re-" verb and nothing here
    # updates or deletes a row.

    #: The closed event vocabulary. Also spelled as a CHECK constraint in the
    #: v4 DDL, so a hand-written INSERT cannot widen it either.
    EVENT_DISMISS = "dismiss"
    EVENT_REOPEN = "reopen"
    EVENT_DEFER = "defer"

    #: The verbs whose effective state CLEARS a finding for the gate. ONE
    #: definition, read by `triage_for` and by nothing else -- `gate.py` tests
    #: membership of the map `triage_for` returns and asks no further question,
    #: which is exactly why a third clearing verb needed no gate change.
    #:
    #: `dismiss` and `defer` clear for different reasons and the ledger keeps
    #: them apart: one says the finding is not a defect, the other says it is
    #: one and names where the work is filed. `reopen` is deliberately absent.
    CLEARING_EVENTS = frozenset({EVENT_DISMISS, EVENT_DEFER})

    def _append_triage_event(self, event: str, rec: dict, reason, at,
                             tracking_ref=None) -> None:
        # Fail closed on the review_id/id spelling: `rec.get("review_id") or
        # rec.get("id")` would silently write NULL (no review linkage) when
        # neither key is present. Require one of the two spellings explicitly
        # so a malformed record raises KeyError instead of persisting an
        # orphaned event.
        #
        # `tracking_ref` defaults to NULL and only `triage_defer` passes one:
        # a dismissal carrying a reference would appear in the deferral listing
        # as outstanding work nobody filed.
        review_id = rec["review_id"] if "review_id" in rec else rec["id"]
        self._c.execute(
            """INSERT INTO triage_events (ledger_key, finding_key, event, review_id,
                 branch, base_sha, file, line, severity, title, reason, at,
                 tracking_ref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["ledger_key"], rec["finding_key"], event, review_id, rec["branch"],
             rec["base_sha"], rec.get("file"), rec.get("line"), rec.get("severity"),
             rec.get("title"), reason, at, tracking_ref))

    def add_triage(self, rec: dict) -> None:
        """Append a `dismiss` event. Takes the record shape it always took.

        The pre-v3 `INSERT OR REPLACE INTO triage` is retired: it kept one row
        per ledger key, so a second dismissal DISCARDED the first one's reason
        and a reopen had nowhere to live at all. The legacy table is left
        exactly as it is -- the migration seeded the stream from it, and
        rewriting it now would create a second, disagreeing record of the same
        decisions.

        Validation is deliberately no stricter than it was: `dismissed_at` is
        accepted as-is (the legacy importer replays whatever timestamp the
        archive recorded, canonical or not), and the audit floor on the REASON
        is `triage.validate_reason`'s job at the call sites that record a human
        decision -- including the importer, which applies it before calling
        here. Tightening either would silently start dropping imported history.
        """
        self._append_triage_event(self.EVENT_DISMISS, rec, rec["dismissed_reason"],
                                  rec.get("dismissed_at"))

    def triage_reopen(self, rec: dict) -> None:
        """Append a `reopen` event: the dismissal of this finding is overturned.

        Stricter than `add_triage` on purpose. This path has no legacy data to
        accommodate -- every reopen is written by this build -- so a reason and
        a canonical, orderable timestamp are required at the door. An audit
        stream entry that says a finding was reopened, but not why or when, is
        the one thing this ledger exists to make impossible.

        The audit FLOOR (length, placeholders) stays where it already is:
        `triage.validate_reason`, applied by `triage.reopen` before it gets
        here. This is the door, not the floor.
        """
        reason = _require_text("reason", rec.get("reason"))
        at = _require_ts("at", rec.get("at"))
        self._append_triage_event(self.EVENT_REOPEN, rec, reason, at)

    def triage_defer(self, rec: dict) -> None:
        """Append a `defer` event: the finding is real, and filed as `rec`'s ref.

        As strict at the door as `triage_reopen`, plus one requirement that is
        the whole point of the verb: `tracking_ref` is MANDATORY. A `defer` row
        with no reference clears the gate while naming no work anybody owes,
        which is indistinguishable from having ignored the finding -- so it may
        not reach the stream at all, exactly as a reopen with no reason may not.

        The audit FLOORS -- what a usable reason is, what a usable reference
        looks like -- stay in `triage.validate_reason` and
        `triage.validate_tracking_ref`, applied by `triage.defer` before it gets
        here. This is the door, not the floor.
        """
        reason = _require_text("reason", rec.get("reason"))
        tracking_ref = _require_text("tracking_ref", rec.get("tracking_ref"))
        at = _require_ts("at", rec.get("at"))
        self._append_triage_event(self.EVENT_DEFER, rec, reason, at,
                                  tracking_ref=tracking_ref)

    def triage_state(self, branch: str, base_sha: str) -> dict[str, dict]:
        """Effective triage state per `finding_key` for one review scope.

        THE ONE DEFINITION of "effective state", which `triage_for` filters and
        the CLI listing renders. Two independent queries here would be two
        answers, and the listing could then print DISMISSED for a finding the
        gate still counts as open -- sending a human away from the very thing
        blocking their push.

        The state of a finding is its LAST EVENT BY `seq`, never by `at`: the
        store's timestamps have one-second resolution, so a dismiss and a
        reopen recorded in the same second cannot be ordered by them, and a
        seeded legacy `dismissed_at` or an operator-supplied `now` can order
        BACKWARDS. `seq` is a monotonic total order; timestamps are display.

        Each value carries the last event's own fields plus, independently, the
        last `dismiss`, the last `reopen` and the last `defer` -- so
        `dismissed_reason` and `dismissed_at` keep the meaning they had before
        v3 (the dismissal's own, not "the latest event's"), and a listing can
        show every side of an overturned decision.
        """
        rows = self._c.execute(
            "SELECT * FROM triage_events WHERE branch=? AND base_sha=? ORDER BY seq",
            (branch, base_sha)).fetchall()
        state: dict[str, dict] = {}
        for r in rows:
            cur = state.setdefault(r["finding_key"], dict(
                dismissed_reason=None, dismissed_at=None,
                reopen_reason=None, reopened_at=None,
                defer_reason=None, deferred_at=None, deferred_ref=None))
            cur.update(dict(r))          # last event by seq wins
            if r["event"] == self.EVENT_DISMISS:
                cur["dismissed_reason"], cur["dismissed_at"] = r["reason"], r["at"]
            elif r["event"] == self.EVENT_REOPEN:
                cur["reopen_reason"], cur["reopened_at"] = r["reason"], r["at"]
            elif r["event"] == self.EVENT_DEFER:
                cur["defer_reason"], cur["deferred_at"] = r["reason"], r["at"]
                cur["deferred_ref"] = r["tracking_ref"]
        return state

    def count_triaged_on_reviews(self, branch: str,
                                 review_ids: set[str] | frozenset[str]) -> int:
        """How many finding_keys are currently CLEARED by a last event on one of
        `review_ids` for this branch.

        Used by R3 "findings already triaged in earlier rounds": effective
        state is still the last event by `seq` (same rule as `triage_state`),
        and we only count a finding when that last clearing event was recorded
        against an earlier review id. A reopen after that last clearing is not
        counted — the finding is open again.
        """
        if not review_ids:
            return 0
        rows = self._c.execute(
            "SELECT finding_key, event, review_id FROM triage_events"
            " WHERE branch=? ORDER BY seq",
            (branch,)).fetchall()
        last: dict[str, tuple[str, str]] = {}
        for r in rows:
            last[r["finding_key"]] = (r["event"], r["review_id"])
        n = 0
        for event, rid in last.values():
            if event in self.CLEARING_EVENTS and rid in review_ids:
                n += 1
        return n

    def triage_for(self, branch: str, base_sha: str) -> dict[str, dict]:
        """The findings in this scope whose last event CLEARS them for the gate.

        SHIPPED SHAPE, unchanged: a `finding_key`-keyed map whose rows carry
        `dismissed_reason` and `dismissed_at`. `gate.open_findings` tests
        membership by `finding_key` and reads nothing else, which is what let
        v3's event stream and v4's `defer` verb both land WITHOUT a byte of
        `gate.py` changing -- a deferred finding is in this map, so the gate
        counts it as triaged and stops blocking on it. That is the escape from
        the endless review round, and it is deliberately implemented here rather
        than as a second rule the enforcement point would have to know about.

        `CLEARING_EVENTS`, never a literal: a filter that spelled `dismiss`
        again here would be a second definition of "cleared", and the one place
        it disagreed with the ledger is the place a human is told a finding is
        handled while their push is still blocked.
        """
        return {k: v for k, v in self.triage_state(branch, base_sha).items()
                if v["event"] in self.CLEARING_EVENTS}

    def triage_history(self, ledger_key: str) -> list[dict]:
        """Every decision ever recorded for one finding, oldest first.

        `ledger_key` (branch + base_sha + finding_key) is what groups a
        finding's history; `seq` orders it. This is the audit read: it returns
        the overturned reasons too, because a ledger that only shows the
        current answer cannot be audited.
        """
        rows = self._c.execute(
            "SELECT * FROM triage_events WHERE ledger_key=? ORDER BY seq",
            (ledger_key,)).fetchall()
        return [dict(r) for r in rows]

    def open_deferrals(self, limit: int = 50) -> list[dict]:
        """Every finding, across every review, still standing as DEFERRED.

        The listing that keeps a deferral from rotting. `defer` clears the gate,
        so once it is recorded nothing in the review loop will ever mention that
        finding again -- and the backlog it created is invisible unless
        something asks for it across reviews. Deliberately NOT scoped to a
        branch or a base: a deferral filed on a branch nobody is looking at is
        exactly the one that goes stale.

        "Still deferred" is the SAME last-event-by-`seq` rule `triage_state`
        applies, only grouped by `ledger_key` (branch + base_sha + finding_key)
        instead of scoped to one branch and base -- which is the same grouping,
        because a ledger key is a finding key inside a scope. A separate
        "latest row" query here would be a second definition of effective state,
        and it could report as outstanding a deferral somebody has since
        reopened.

        Newest first by `seq`, not by `at`: `at` is display, and the store's
        one-second resolution cannot order two decisions taken in the same
        second.
        """
        rows = self._c.execute(
            """SELECT e.* FROM triage_events e
               JOIN (SELECT ledger_key, MAX(seq) AS seq FROM triage_events
                     GROUP BY ledger_key) last ON e.seq = last.seq
               WHERE e.event = ?
               ORDER BY e.seq DESC LIMIT ?""",
            (self.EVENT_DEFER, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- provider availability cache ---------------------------------------

    def mark_provider_unavailable(self, provider: str, reason: str, category: str,
                                  until_iso: str,
                                  recorded_at: str | None = None,
                                  quota_pool: str | None = None) -> None:
        """Record that `provider` is unusable until `until_iso`.

        Everything is validated at the door so the read path only ever has to
        cope with rows corrupted from outside skodun. In particular there is no
        "unavailable forever" state: a TTL is mandatory, so a provider always
        becomes eligible again on its own.
        """
        provider = _require_text("provider", provider)
        state_key = _state_key(provider, quota_pool)
        reason = _require_text("reason", reason)
        category = _require_text("category", category)
        until_iso = _require_ts("until_iso", until_iso)
        recorded_at = (_iso_now() if recorded_at is None
                       else _require_ts("recorded_at", recorded_at))
        self._c.execute(
            """INSERT INTO provider_state
                 (provider, unavailable_until, reason, category, recorded_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(provider) DO UPDATE SET
                 unavailable_until=excluded.unavailable_until,
                 reason=excluded.reason, category=excluded.category,
                 recorded_at=excluded.recorded_at""",
            (state_key, until_iso, reason, category, recorded_at))

    def provider_unavailable_reason(self, provider: str, now_iso: str,
                                    env: Mapping[str, str] = os.environ,
                                    quota_pool: str | None = None) -> str | None:
        """Why a fallback chain should skip `provider` right now, or None.

        `env` defaults to the live `os.environ` mapping rather than a snapshot,
        matching `run_gate`, so the bypass can be injected from a test without
        monkeypatching global state.

        A row with an unorderable `unavailable_until` returns None (see
        `_still_unavailable`); a row with a sound TTL but no `reason` still
        returns a non-empty string, so a caller's `if reason:` cannot be
        fooled into using a provider that is genuinely unavailable.
        """
        now_iso = _require_ts("now_iso", now_iso)
        if _provider_state_bypassed(env):
            return None
        provider = _require_text("provider", provider)
        keys = [_state_key(provider, quota_pool)]
        if keys[0] != provider:
            # A legacy provider-wide row remains authoritative until expiry.
            keys.append(provider)
        for key in keys:
            row = self._c.execute(
                "SELECT unavailable_until, reason FROM provider_state WHERE provider=?",
                (key,)).fetchone()
            if row is not None and _still_unavailable(
                    row["unavailable_until"], now_iso):
                return row["reason"] or "provider marked unavailable"
        return None

    def provider_state_rows(self, now_iso: str) -> list[dict]:
        """Every row, expired ones included, each flagged `active`.

        This is the diagnostic listing behind `skodun providers`, not a filter,
        and it deliberately ignores `SKODUN_IGNORE_PROVIDER_STATE`: the bypass
        changes routing, not what an operator is allowed to see.
        """
        now_iso = _require_ts("now_iso", now_iso)
        rows = self._c.execute(
            "SELECT provider, unavailable_until, reason, category FROM provider_state"
            " ORDER BY provider").fetchall()
        out = []
        for r in rows:
            provider, pool = _decode_state_key(r["provider"])
            row = {"provider": provider,
                   "unavailable_until": r["unavailable_until"],
                   "reason": r["reason"], "category": r["category"],
                   "active": _still_unavailable(r["unavailable_until"], now_iso)}
            if pool is not None:
                row["quota_pool"] = pool
            out.append(row)
        return out

    # --- capacity admissions (epic S3) -------------------------------------

    #: Active statuses for FIFO position / holder counts.
    _CAPACITY_ACTIVE = ("queued", "admitted", "running")
    _CAPACITY_HOLDERS = ("admitted", "running")
    _CAPACITY_TERMINAL = ("released", "expired", "rejected")

    def capacity_enqueue(self, *, admission_id: str, resource_class: str,
                         scope: str, pid: int | None = None) -> dict:
        """Insert a new ``queued`` capacity row. Returns the row as a dict."""
        admission_id = _require_text("admission_id", admission_id)
        resource_class = _require_text("resource_class", resource_class)
        scope = _require_text("scope", scope)
        queued_at = _iso_now()
        self._c.execute(
            """INSERT INTO capacity_admissions
                 (id, resource_class, scope, status, queued_at, pid)
               VALUES (?,?,?,?,?,?)""",
            (admission_id, resource_class, scope, "queued", queued_at, pid))
        row = self.capacity_get(admission_id)
        assert row is not None
        return row

    def capacity_get(self, admission_id: str) -> dict | None:
        """One admission row, or None."""
        admission_id = _require_text("admission_id", admission_id)
        row = self._c.execute(
            "SELECT * FROM capacity_admissions WHERE id=?",
            (admission_id,)).fetchone()
        return None if row is None else dict(row)

    def capacity_active_views(self, resource_class: str, scope: str) -> list:
        """Active waiters as ``capacity.WaiterView`` for pure FIFO decisions."""
        from .capacity import WaiterView

        resource_class = _require_text("resource_class", resource_class)
        scope = _require_text("scope", scope)
        rows = self._c.execute(
            """SELECT id, status, queued_at FROM capacity_admissions
               WHERE resource_class=? AND scope=? AND status IN (?,?,?)
               ORDER BY queued_at, id""",
            (resource_class, scope, *self._CAPACITY_ACTIVE)).fetchall()
        return [WaiterView(id=r["id"], status=r["status"],
                           queued_at=r["queued_at"]) for r in rows]

    def capacity_position(self, admission_id: str) -> int | None:
        """1-based FIFO position among active peers, or None if terminal/missing."""
        from .capacity import queue_position_among

        row = self.capacity_get(admission_id)
        if row is None or row["status"] not in self._CAPACITY_ACTIVE:
            return None
        views = self.capacity_active_views(row["resource_class"], row["scope"])
        return queue_position_among(admission_id, views)

    def capacity_terminal_wait_ms(self, resource_class: str, scope: str,
                                  *, limit: int = 20) -> list[int]:
        """Recent terminal ``wait_ms`` values (newest first), for ETA p50."""
        resource_class = _require_text("resource_class", resource_class)
        scope = _require_text("scope", scope)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = 20
        rows = self._c.execute(
            """SELECT wait_ms FROM capacity_admissions
               WHERE resource_class=? AND scope=?
                 AND status IN ('released','expired','rejected')
                 AND wait_ms IS NOT NULL
               ORDER BY ended_at DESC, id DESC LIMIT ?""",
            (resource_class, scope, limit)).fetchall()
        out: list[int] = []
        for r in rows:
            try:
                out.append(int(r["wait_ms"]))
            except (TypeError, ValueError):
                continue
        return out

    def capacity_holder_count(self, resource_class: str, scope: str) -> int:
        """Count of admitted+running holders for a class/scope."""
        resource_class = _require_text("resource_class", resource_class)
        scope = _require_text("scope", scope)
        row = self._c.execute(
            """SELECT COUNT(*) AS n FROM capacity_admissions
               WHERE resource_class=? AND scope=? AND status IN (?,?)""",
            (resource_class, scope, "admitted", "running")).fetchone()
        return int(row["n"]) if row is not None else 0

    def capacity_reclaim_stale(
            self, resource_class: str, scope: str, *, stale_sec: float,
            now_epoch: float | None = None,
            pid_alive_fn=None) -> list[str]:
        """Finish active rows that a dead peer left behind; return reclaimed ids.

        Uses :func:`skodun.capacity.should_reclaim_admission` under a write
        transaction so multi-process waiters agree on the same cleanup. Rows
        reclaimed this way land as ``rejected`` with a durable expire reason
        (``stale_pid_dead`` / ``stale_age``) and never re-enter the queue.
        """
        from .capacity import should_reclaim_admission

        resource_class = _require_text("resource_class", resource_class)
        scope = _require_text("scope", scope)
        reclaimed: list[str] = []
        self._c.execute("BEGIN IMMEDIATE")
        try:
            rows = self._c.execute(
                """SELECT * FROM capacity_admissions
                   WHERE resource_class=? AND scope=? AND status IN (?,?,?)
                   ORDER BY queued_at, id""",
                (resource_class, scope, *self._CAPACITY_ACTIVE)).fetchall()
            ended_at = _iso_now()
            for row in rows:
                reason = should_reclaim_admission(
                    status=row["status"],
                    pid=row["pid"],
                    queued_at=row["queued_at"],
                    stale_sec=stale_sec,
                    now_epoch=now_epoch,
                    pid_alive_fn=pid_alive_fn,
                )
                if reason is None:
                    continue
                queue_wait_ms, run_ms, total_admission_ms = _capacity_metrics(
                    row, ended_at)
                wait_ms = total_admission_ms
                self._c.execute(
                    """UPDATE capacity_admissions
                       SET status='rejected', ended_at=?, wait_ms=?,
                           queue_wait_ms=?, run_ms=?, total_admission_ms=?,
                           expire_reason=?
                       WHERE id=? AND status IN (?,?,?)""",
                    (ended_at, wait_ms, queue_wait_ms, run_ms,
                     total_admission_ms, reason, row["id"],
                     *self._CAPACITY_ACTIVE))
                if self._c.execute("SELECT changes()").fetchone()[0]:
                    reclaimed.append(row["id"])
            self._c.execute("COMMIT")
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return reclaimed

    def capacity_try_admit(self, admission_id: str, *, capacity: int) -> dict | None:
        """Transactionally admit if FIFO-eligible. Returns row or None."""
        from .capacity import WaiterView, decide_admit

        admission_id = _require_text("admission_id", admission_id)
        if capacity < 1:
            return None
        self._c.execute("BEGIN IMMEDIATE")
        try:
            row = self._c.execute(
                "SELECT * FROM capacity_admissions WHERE id=?",
                (admission_id,)).fetchone()
            if row is None or row["status"] != "queued":
                self._c.execute("COMMIT")
                return None if row is None else dict(row)
            peers = self._c.execute(
                """SELECT id, status, queued_at FROM capacity_admissions
                   WHERE resource_class=? AND scope=? AND status IN (?,?,?)""",
                (row["resource_class"], row["scope"],
                 *self._CAPACITY_ACTIVE)).fetchall()
            views = [WaiterView(id=p["id"], status=p["status"],
                                queued_at=p["queued_at"]) for p in peers]
            if not decide_admit(admission_id, views, capacity):
                self._c.execute("COMMIT")
                return None
            admitted_at = _iso_now()
            self._c.execute(
                """UPDATE capacity_admissions
                   SET status='admitted', admitted_at=?
                   WHERE id=? AND status='queued'""",
                (admitted_at, admission_id))
            self._c.execute("COMMIT")
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return self.capacity_get(admission_id)

    def capacity_force_admit(self, admission_id: str) -> dict | None:
        """Mark ``queued`` → ``admitted`` after the dual-hold lock is held.

        Used when the caller already serializes via the legacy FG lock and
        only needs the durable telemetry transition. Still refuses if the row
        is missing or already terminal.
        """
        admission_id = _require_text("admission_id", admission_id)
        self._c.execute("BEGIN IMMEDIATE")
        try:
            row = self._c.execute(
                "SELECT * FROM capacity_admissions WHERE id=?",
                (admission_id,)).fetchone()
            if row is None:
                self._c.execute("COMMIT")
                return None
            if row["status"] in ("admitted", "running"):
                self._c.execute("COMMIT")
                return dict(row)
            if row["status"] != "queued":
                self._c.execute("COMMIT")
                return None
            admitted_at = _iso_now()
            self._c.execute(
                """UPDATE capacity_admissions
                   SET status='admitted', admitted_at=?
                   WHERE id=? AND status='queued'""",
                (admitted_at, admission_id))
            self._c.execute("COMMIT")
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return self.capacity_get(admission_id)

    def capacity_mark_started(self, admission_id: str,
                              review_id: str | None = None) -> dict:
        """``admitted`` (or ``queued``) → ``running``; set ``started_at``."""
        admission_id = _require_text("admission_id", admission_id)
        started_at = _iso_now()
        self._c.execute("BEGIN IMMEDIATE")
        try:
            row = self._c.execute(
                "SELECT * FROM capacity_admissions WHERE id=?",
                (admission_id,)).fetchone()
            if row is None:
                self._c.execute("COMMIT")
                raise ValueError(f"capacity admission {admission_id!r} not found")
            if row["status"] in self._CAPACITY_TERMINAL:
                self._c.execute("COMMIT")
                return dict(row)
            admitted_at = row["admitted_at"] or started_at
            self._c.execute(
                """UPDATE capacity_admissions
                   SET status='running', admitted_at=?, started_at=?,
                       review_id=COALESCE(?, review_id)
                   WHERE id=?""",
                (admitted_at, started_at, review_id, admission_id))
            self._c.execute("COMMIT")
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        out = self.capacity_get(admission_id)
        assert out is not None
        return out

    def capacity_finish(self, admission_id: str, *, status: str,
                        expire_reason: str | None = None) -> dict:
        """Terminal transition with ``ended_at`` and ``wait_ms``."""
        admission_id = _require_text("admission_id", admission_id)
        status = _require_text("status", status)
        if status not in self._CAPACITY_TERMINAL:
            raise ValueError(
                f"capacity finish status must be one of "
                f"{sorted(self._CAPACITY_TERMINAL)}, got {status!r}")
        ended_at = _iso_now()
        self._c.execute("BEGIN IMMEDIATE")
        try:
            row = self._c.execute(
                "SELECT * FROM capacity_admissions WHERE id=?",
                (admission_id,)).fetchone()
            if row is None:
                self._c.execute("COMMIT")
                raise ValueError(f"capacity admission {admission_id!r} not found")
            if row["status"] in self._CAPACITY_TERMINAL:
                self._c.execute("COMMIT")
                return dict(row)
            queue_wait_ms, run_ms, total_admission_ms = _capacity_metrics(
                row, ended_at)
            wait_ms = total_admission_ms
            self._c.execute(
                """UPDATE capacity_admissions
                   SET status=?, ended_at=?, wait_ms=?, queue_wait_ms=?,
                       run_ms=?, total_admission_ms=?, expire_reason=?
                   WHERE id=?""",
                (status, ended_at, wait_ms, queue_wait_ms, run_ms,
                 total_admission_ms, expire_reason, admission_id))
            self._c.execute("COMMIT")
        except BaseException:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        out = self.capacity_get(admission_id)
        assert out is not None
        return out

    def list_reviews(self, branch: str | None, limit: int = 30,
                     repo: str | None = None) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch is not None:
            # Scoped ONLY with a branch: a branch name is the ambiguous key.
            # `branch=None` is a human's "show me everything" and stays
            # unscoped across repositories -- so a `repo` handed in without a
            # branch is ignored, which is this method's published contract and
            # what `log --repo`'s help text says.
            q += " WHERE branch=?"
            args = (branch,)
            if repo is not None:
                q += " AND repo=?"
                args += (repo,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
        rows = self._c.execute(q, args + (limit,)).fetchall()
        return [json.loads(r["artifact_json"]) for r in rows]

    def running_records(self) -> list[dict]:
        """Every `running` row, as the INDEXED columns the stale sweep reads.

        `list_reviews` decodes `artifact_json` for every row it returns, and
        `recover_stale` called it with no branch on every push -- so the sweep
        decoded every artifact ever stored to read a status that is an indexed
        column, on the synchronous `git push` path. This reads three columns
        and decodes nothing.

        UNORDERED, unlike `list_reviews`: the sweep judges every row it is
        given, independently, and never stops early, so `ORDER BY reviewed_at
        DESC` bought it nothing. The ordering exists for the DISPLAY callers
        and stays on `list_reviews` for them.

        Deliberately UNSCOPED by repository: a stale row is stale whichever
        repository recorded it, and scoping the sweep would strand the pre-v5
        rows that `repo IS NULL` already hides from every scoped query.
        """
        rows = self._c.execute(
            "SELECT id, reviewed_at, worst_runtime_sec FROM reviews"
            " WHERE status=?", (RUNNING,)).fetchall()
        return [{"id": r["id"], "reviewed_at": r["reviewed_at"],
                 "worst_runtime_sec": r["worst_runtime_sec"]} for r in rows]

    def current_review(self, repo: str | None = None) -> dict | None:
        """The review status/cancel want by default: live first, else newest.

        Prefer the newest `running` row (optionally scoped to `repo`). When none
        is running, the newest terminal row for that scope. `repo=None` is the
        host-wide view -- same unscoped posture as `list_reviews(branch=None)`.

        Decodes one artifact (the winner), not the whole store. Status is a
        read surface; it must not pay for every historical row.
        """
        if repo is not None:
            row = self._c.execute(
                """SELECT artifact_json FROM reviews
                   WHERE status=? AND repo=?
                   ORDER BY reviewed_at DESC LIMIT 1""",
                (RUNNING, repo)).fetchone()
            if row is None:
                row = self._c.execute(
                    """SELECT artifact_json FROM reviews
                       WHERE repo=?
                       ORDER BY reviewed_at DESC LIMIT 1""",
                    (repo,)).fetchone()
        else:
            row = self._c.execute(
                """SELECT artifact_json FROM reviews
                   WHERE status=?
                   ORDER BY reviewed_at DESC LIMIT 1""",
                (RUNNING,)).fetchone()
            if row is None:
                row = self._c.execute(
                    """SELECT artifact_json FROM reviews
                       ORDER BY reviewed_at DESC LIMIT 1"""
                ).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def telemetry_stats(self, *, since_iso: str) -> dict:
        """Read the v9 operational telemetry model without mutating the store.

        Timing aggregates use only explicitly named v9 fields.  A NULL is
        coverage information, not an invitation to reinterpret the old
        ``reviewed_at`` or ``wait_ms`` columns.
        """
        since_iso = _require_ts("since_iso", since_iso)

        def percentile(values: list[int], p: float) -> int | None:
            if not values:
                return None
            ordered = sorted(values)
            # Nearest-rank percentiles are deterministic for small operational
            # samples and avoid interpolating a value the store never observed.
            rank = max(1, int((len(ordered) * p) + 0.999999999))
            return ordered[rank - 1]

        def timing(values: list[int]) -> dict:
            return {"count": len(values), "p50_ms": percentile(values, .50),
                    "p90_ms": percentile(values, .90),
                    "total_ms": sum(values)}

        review_rows = self._c.execute(
            """SELECT review_started_at, review_completed_at, reviewed_at,
                      repo_id, repo, trustworthy, findings_total,
                      orchestration_id, attempt_ordinal, outcome, terminal_reason
                 FROM reviews
                WHERE COALESCE(review_started_at, reviewed_at) >= ?
                ORDER BY COALESCE(review_started_at, reviewed_at), id""",
            (since_iso,)).fetchall()
        review_total = len(review_rows)
        complete = [r for r in review_rows
                    if r["review_started_at"] is not None
                    and r["review_completed_at"] is not None]
        trustworthy = sum(r["trustworthy"] == 1 for r in review_rows)
        findings = sum(int(r["findings_total"] or 0) for r in review_rows)
        by_repo: dict[str, dict] = {}
        for row in review_rows:
            repo_id = row["repo_id"] or "legacy:unresolved"
            bucket = by_repo.setdefault(repo_id, {
                "repo_id": repo_id, "reviews": 0, "trustworthy": 0,
                "findings": 0, "legacy_rows": 0,
            })
            bucket["reviews"] += 1
            bucket["trustworthy"] += int(row["trustworthy"] == 1)
            bucket["findings"] += int(row["findings_total"] or 0)
            bucket["legacy_rows"] += int(row["repo_id"] is None)
        for bucket in by_repo.values():
            bucket["trustworthy_rate"] = (
                bucket["trustworthy"] / bucket["reviews"]
                if bucket["reviews"] else None)

        capacity_rows = self._c.execute(
            """SELECT resource_class, status, queue_wait_ms, run_ms,
                      total_admission_ms, wait_ms, expire_reason
                 FROM capacity_admissions
                WHERE queued_at >= ?""", (since_iso,)).fetchall()
        queue = [int(r["queue_wait_ms"]) for r in capacity_rows
                 if r["queue_wait_ms"] is not None]
        run = [int(r["run_ms"]) for r in capacity_rows
               if r["run_ms"] is not None]
        total = [int(r["total_admission_ms"]) for r in capacity_rows
                 if r["total_admission_ms"] is not None]
        capacity_by_resource: dict[str, dict] = {}
        for row in capacity_rows:
            bucket = capacity_by_resource.setdefault(row["resource_class"], {
                "resource_class": row["resource_class"], "rows": 0,
                "expired": 0, "rejected": 0, "telemetry_rows": 0,
            })
            bucket["rows"] += 1
            bucket["expired"] += int(row["status"] == "expired")
            bucket["rejected"] += int(row["status"] == "rejected")
            bucket["telemetry_rows"] += int(
                row["queue_wait_ms"] is not None
                and row["run_ms"] is not None
                and row["total_admission_ms"] is not None)

        reuse_rows = self._c.execute(
            "SELECT outcome FROM reuse_events WHERE at>=?", (since_iso,)
        ).fetchall()
        reuse_hits = sum(r["outcome"] == "hit" for r in reuse_rows)
        reuse_misses = sum(r["outcome"] == "miss" for r in reuse_rows)
        return {
            "since": since_iso,
            "reviews": {
                "total": review_total,
                "telemetry_rows": len(complete),
                "legacy_rows": review_total - len(complete),
                "repo_coverage": sum(r["repo_id"] is not None for r in review_rows),
                "trustworthy": trustworthy,
                "trustworthy_rate": trustworthy / review_total
                    if review_total else None,
                "findings": findings,
                "by_repo": sorted(by_repo.values(), key=lambda r: r["repo_id"]),
            },
            "timing": {
                "review_ms": timing([
                    d for r in complete
                    for d in [_duration_ms(r["review_started_at"],
                                           r["review_completed_at"])]
                    if d is not None]),
                "capacity_queue_ms": timing(queue),
                "capacity_run_ms": timing(run),
                "capacity_total_admission_ms": timing(total),
            },
            "capacity": {
                "rows": len(capacity_rows),
                "telemetry_rows": len(total),
                "expired": sum(r["status"] == "expired" for r in capacity_rows),
                "rejected": sum(r["status"] == "rejected" for r in capacity_rows),
                "by_resource": sorted(capacity_by_resource.values(),
                                      key=lambda r: r["resource_class"]),
            },
            "identities": {
                "first_trust": sum(r["attempt_ordinal"] == 0
                                    for r in review_rows),
                "recovered": sum(r["attempt_ordinal"] is not None
                                  and r["attempt_ordinal"] > 0
                                  and r["trustworthy"] == 1
                                  for r in review_rows),
                "never_trustworthy": sum(
                    r["orchestration_id"] is not None and r["trustworthy"] != 1
                    for r in review_rows),
            },
            "reuse": {
                "hits": reuse_hits + sum(r["outcome"] == "reuse"
                                        for r in review_rows),
                "misses": reuse_misses + sum(r["outcome"] == "reuse_miss"
                                             for r in review_rows),
            },
        }

    # --- feedback ledger (v7; non-gate) ------------------------------------

    def feedback_append(
            self, *, at: str, actor: str, kind: str, body: str,
            review_id: str | None = None,
            finding_index: int | None = None,
            provider: str | None = None,
            repo: str | None = None,
            source: str | None = None) -> dict:
        """Append one feedback event. Returns the stored row (with ``seq``).

        Does **not** affect triage or the gate. Callers validate actor/kind/body
        before this method (see ``skodun.feedback``).
        """
        at = _require_ts("at", at)
        actor = _require_text("actor", actor)
        kind = _require_text("kind", kind)
        body = _require_text("body", body)
        if review_id is not None:
            review_id = _require_text("review_id", review_id)
        if finding_index is not None:
            if (not isinstance(finding_index, int)
                    or isinstance(finding_index, bool)
                    or finding_index < 0):
                raise ValueError(
                    f"finding_index must be a non-negative int, "
                    f"got {finding_index!r}")
        if provider is not None:
            provider = _require_text("provider", provider)
        if repo is not None:
            repo = _require_text("repo", repo)
        if source is not None:
            source = _require_text("source", source)
        cur = self._c.execute(
            """INSERT INTO feedback_events
               (at, actor, kind, body, review_id, finding_index,
                provider, repo, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (at, actor, kind, body, review_id, finding_index,
             provider, repo, source))
        seq = int(cur.lastrowid)
        row = self._c.execute(
            "SELECT * FROM feedback_events WHERE seq=?", (seq,)).fetchone()
        assert row is not None
        return dict(row)

    def feedback_list(self, *, kind: str | None = None,
                      review_id: str | None = None,
                      limit: int = 50) -> list[dict]:
        """Newest feedback first. Optional filters on kind and/or review_id."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = 50
        if limit > 500:
            limit = 500
        clauses: list[str] = []
        args: list = []
        if kind is not None:
            clauses.append("kind=?")
            args.append(_require_text("kind", kind))
        if review_id is not None:
            clauses.append("review_id=?")
            args.append(_require_text("review_id", review_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        rows = self._c.execute(
            f"SELECT * FROM feedback_events{where}"
            f" ORDER BY at DESC, seq DESC LIMIT ?",
            tuple(args)).fetchall()
        return [dict(r) for r in rows]

    # --- API spend ledger (v8; metered adapters) ---------------------------
    # Rows are append-only. Daily ceilings sum cost_usd WHERE at LIKE 'YYYY-MM-DD%'
    # (UTC day of the timestamp). Limits are enforced in skodun.spend — default
    # $10 per provider per UTC day (SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY).
    # Not a lifetime total: clients need not raise the cap as history grows.

    def api_spend_append(
            self, *, at: str, provider: str, model: str | None,
            prompt_tokens: int, completion_tokens: int, total_tokens: int,
            cost_usd: float, review_id: str | None = None,
            request_id: str | None = None) -> dict:
        """Append one metered API spend event."""
        at = _require_ts("at", at)
        provider = _require_text("provider", provider)
        if model is not None:
            model = _require_text("model", model)
        if review_id is not None:
            review_id = _require_text("review_id", review_id)
        if request_id is not None:
            request_id = _require_text("request_id", request_id)
        cur = self._c.execute(
            """INSERT INTO api_spend_events
               (at, provider, model, review_id, prompt_tokens,
                completion_tokens, total_tokens, cost_usd, request_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (at, provider, model, review_id,
             int(prompt_tokens), int(completion_tokens), int(total_tokens),
             float(cost_usd), request_id))
        seq = int(cur.lastrowid)
        row = self._c.execute(
            "SELECT * FROM api_spend_events WHERE seq=?", (seq,)).fetchone()
        assert row is not None
        return dict(row)

    def api_spend_sum_usd(self, provider: str, *, day_prefix: str) -> float:
        """Sum ``cost_usd`` for ``provider`` with ``at`` starting with day_prefix.

        ``day_prefix`` is ``YYYY-MM-DD`` (UTC day of the store timestamps).
        """
        provider = _require_text("provider", provider)
        day_prefix = _require_text("day_prefix", day_prefix)
        row = self._c.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) AS s FROM api_spend_events
               WHERE provider=? AND at LIKE ?""",
            (provider, day_prefix + "%")).fetchone()
        return float(row["s"]) if row is not None else 0.0

    def routing_counts(self, *, since_iso: str) -> list[dict]:
        """Routing decisions since `since_iso`, grouped. Read-only, no schema.

        `(adapter, route_reason, routed_reviewer)` with a count each, where
        `adapter` is WHO SERVED (rewritten by the pipeline to whoever actually
        answered) and `routed_reviewer` is who the router CHOSE. After a
        fallback those name different providers, and the gap between them is
        the fallback rate -- see the S5 telemetry design.

        The routing fields live inside `artifact_json` rather than in columns,
        so they are read with `json_extract`, and the grouping happens in SQL:
        `list_reviews` decodes every artifact it returns, which for a whole
        window would be megabytes of findings and attempts to answer a question
        about four scalars. `json_valid` guards the extract so one malformed
        row -- an artifact written by something other than `json.dumps` -- costs
        its own row's attribution rather than blinding the whole query.

        A record with no routing audit yields `route_reason IS NULL`: it is
        either pre-S5 or a background pre-push review, and both consumed a
        provider slot without being a routing decision. The caller decides how
        to present that; this method does not hide it.

        Two exclusions, and both are about the same invariant: the caller
        prints a per-provider share, so every row counted has to be a row some
        provider line can own.

        `adapter IS NOT NULL` drops skodun's own rows that never reached a
        provider -- a `reserve_prepush` row exists before the worker runs, and
        a superseded or fail-if-running row can terminate without one. They are
        real reviews-in-progress, but nothing attributes them, so counting them
        would put a numerator sum below its own denominator on every listing
        that had one.

        Scoped to `source = 'skodun'`, which is load-bearing rather than tidy.
        A store that has run `import-legacy` holds the old grok-reviews
        archive, and those rows never touched a skodun provider slot -- they
        have no adapter at all. Counting them would put a four-figure
        denominator under a three-figure numerator and report a provider
        carrying 28% of the real load as carrying 5%, which is precisely the
        number this method exists to get right.

        The window is a string comparison, correct only because store
        timestamps are fixed-width canonical UTC -- hence `_require_ts`.
        `reviewed_at` carries no index of its own (only `(branch,
        reviewed_at)`), so this is a table scan. That is the right trade for a
        read-only diagnostic at these row counts, and it is cheaper than the
        index would be to maintain on every write.
        """
        since_iso = _require_ts("since_iso", since_iso)
        rows = self._c.execute(
            """SELECT adapter,
                      CASE WHEN json_valid(artifact_json)
                           THEN json_extract(artifact_json, '$.route_reason')
                      END AS route_reason,
                      CASE WHEN json_valid(artifact_json)
                           THEN json_extract(artifact_json, '$.routed_reviewer')
                      END AS routed_reviewer,
                      COUNT(*) AS n
                 FROM reviews
                WHERE reviewed_at >= ? AND source = ?
                      AND adapter IS NOT NULL
             GROUP BY adapter, route_reason, routed_reviewer
             ORDER BY adapter, route_reason, routed_reviewer""",
            (since_iso, SKODUN_SOURCE)).fetchall()
        return [{"adapter": r["adapter"], "route_reason": r["route_reason"],
                 "routed_reviewer": r["routed_reviewer"], "n": int(r["n"])}
                for r in rows]
