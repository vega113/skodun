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
one row per decision -- ``dismiss`` or ``reopen`` -- and the effective state of
a finding is its LAST EVENT BY ``seq``. Nothing is ever overwritten or deleted,
so a finding's whole history reads back in order and every reason survives the
decision that overturned it. The pre-v3 single-row ``triage`` table is still
here and is now READ-ONLY: it is the audit source the migration seeded the
stream from.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from .trust import is_trustworthy

_TRUST_AXES = ("parse_ok", "degraded", "diff_truncated")

#: The schema this build of skodun writes and understands. A store stamped
#: higher was written by a newer skodun and is refused, untouched.
SCHEMA_VERSION = 3

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
#     `ALTER TABLE ADD COLUMN` above being the reason it exists.
#
# `test_no_non_transactional_delta_carries_a_non_idempotent_statement` pins the
# rule, because putting a delta in the wrong lane is invisible until a crash.
_MIGRATIONS: tuple[tuple[int, str | tuple[str, ...]], ...] = (
    (2, _MIGRATION_V2),
    (3, _MIGRATION_V3),
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


def _iso_now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


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
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
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
        raise ValueError(f"store schema v{version} is newer than this skodun")

    conn.execute("PRAGMA journal_mode=WAL")
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


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

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
        return cls(conn)

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

    def save_review(self, rec: dict) -> None:
        rec = dict(rec)   # never mutate the caller's dict
        axes = {k: rec.get(k, False) for k in _TRUST_AXES}
        for k, v in axes.items():
            if not isinstance(v, bool):   # bool("false") is True — refuse coercion
                raise ValueError(
                    f"save_review: {k} must be bool, got {type(v).__name__}")
        rec.update(axes)
        rec["trustworthy"] = is_trustworthy(**axes)
        sev = rec.get("severity") or {}
        self._c.execute(
            """INSERT INTO reviews (id, reviewed_at, branch, head, base_ref, base_sha,
                 diff_hash, context_hash, mode, model, adapter, status, parse_ok,
                 degraded, diff_truncated, trustworthy, stop_reason, findings_total,
                 sev_high, sev_medium, sev_low, summary, source, artifact_json,
                 worst_runtime_sec)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 reviewed_at=excluded.reviewed_at, branch=excluded.branch,
                 head=excluded.head, base_ref=excluded.base_ref,
                 base_sha=excluded.base_sha, diff_hash=excluded.diff_hash,
                 context_hash=excluded.context_hash, mode=excluded.mode,
                 model=excluded.model, adapter=excluded.adapter,
                 status=excluded.status, parse_ok=excluded.parse_ok,
                 degraded=excluded.degraded, diff_truncated=excluded.diff_truncated,
                 trustworthy=excluded.trustworthy, stop_reason=excluded.stop_reason,
                 findings_total=excluded.findings_total, sev_high=excluded.sev_high,
                 sev_medium=excluded.sev_medium, sev_low=excluded.sev_low,
                 summary=excluded.summary, source=excluded.source,
                 artifact_json=excluded.artifact_json,
                 -- The v3 runtime-budget column, written from the SAME dict the
                 -- artifact is serialized from, so the indexed value and the
                 -- artifact can never disagree (the Phase 1 rule). NULL when the
                 -- record carries no usable budget, which is every pre-Phase-3
                 -- row and every unbatched foreground review that predates it.
                 worst_runtime_sec=excluded.worst_runtime_sec""",
            (rec["id"], rec.get("reviewed_at"), rec.get("branch"), rec.get("head"),
             rec.get("base_ref"), rec.get("base_sha"), rec.get("diff_hash"),
             rec.get("context_hash", ""), rec.get("mode"), rec.get("model"),
             rec.get("adapter"), rec.get("status"), int(bool(rec.get("parse_ok"))),
             int(bool(rec.get("degraded"))), int(bool(rec.get("diff_truncated"))),
             int(bool(rec.get("trustworthy"))), rec.get("stop_reason"),
             int(rec.get("findings_total") or 0), int(sev.get("high") or 0),
             int(sev.get("medium") or 0), int(sev.get("low") or 0),
             rec.get("summary"), rec.get("source", "skodun"),
             json.dumps(rec, ensure_ascii=False),
             _opt_positive_int(rec.get("worst_runtime_sec"))))

    def get_review(self, review_id: str) -> dict | None:
        row = self._c.execute("SELECT artifact_json FROM reviews WHERE id=?",
                              (review_id,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def latest_trustworthy_for(self, diff_hash: str) -> dict | None:
        row = self._c.execute(
            """SELECT artifact_json FROM reviews
               WHERE diff_hash=? AND trustworthy=1
               ORDER BY reviewed_at DESC LIMIT 1""", (diff_hash,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def set_status(self, review_id: str, status: str) -> None:
        self._c.execute(
            """UPDATE reviews SET status=?,
                 artifact_json=json_set(artifact_json, '$.status', ?)
               WHERE id=?""", (status, status, review_id))

    def log_gate_event(self, rec: dict) -> None:
        self._c.execute(
            "INSERT INTO gate_events (at, repo, branch, diff_hash, outcome, code, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (rec.get("at"), rec.get("repo"), rec.get("branch"), rec.get("diff_hash"),
             rec.get("outcome"), rec.get("code"), rec.get("note")))

    # --- the append-only triage event stream --------------------------------
    #
    # The two verbs, and the ONE writer behind them. Effective state is the last
    # event by `seq` (see `triage_state`), so a re-dismissal after a reopen is
    # just another `dismiss` event -- there is no third verb and nothing here
    # updates or deletes a row.

    #: The closed event vocabulary. Also spelled as a CHECK constraint in the
    #: v3 DDL, so a hand-written INSERT cannot widen it either.
    EVENT_DISMISS = "dismiss"
    EVENT_REOPEN = "reopen"

    def _append_triage_event(self, event: str, rec: dict, reason, at) -> None:
        # Fail closed on the review_id/id spelling: `rec.get("review_id") or
        # rec.get("id")` would silently write NULL (no review linkage) when
        # neither key is present. Require one of the two spellings explicitly
        # so a malformed record raises KeyError instead of persisting an
        # orphaned event.
        review_id = rec["review_id"] if "review_id" in rec else rec["id"]
        self._c.execute(
            """INSERT INTO triage_events (ledger_key, finding_key, event, review_id,
                 branch, base_sha, file, line, severity, title, reason, at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["ledger_key"], rec["finding_key"], event, review_id, rec["branch"],
             rec["base_sha"], rec.get("file"), rec.get("line"), rec.get("severity"),
             rec.get("title"), reason, at))

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
        last `dismiss` and the last `reopen` -- so `dismissed_reason` and
        `dismissed_at` keep the meaning they had before v3 (the dismissal's
        own, not "the latest event's"), and a listing can show both sides of an
        overturned decision.
        """
        rows = self._c.execute(
            "SELECT * FROM triage_events WHERE branch=? AND base_sha=? ORDER BY seq",
            (branch, base_sha)).fetchall()
        state: dict[str, dict] = {}
        for r in rows:
            cur = state.setdefault(r["finding_key"], dict(
                dismissed_reason=None, dismissed_at=None,
                reopen_reason=None, reopened_at=None))
            cur.update(dict(r))          # last event by seq wins
            if r["event"] == self.EVENT_DISMISS:
                cur["dismissed_reason"], cur["dismissed_at"] = r["reason"], r["at"]
            elif r["event"] == self.EVENT_REOPEN:
                cur["reopen_reason"], cur["reopened_at"] = r["reason"], r["at"]
        return state

    def triage_for(self, branch: str, base_sha: str) -> dict[str, dict]:
        """The findings in this scope whose last event is a `dismiss`.

        SHIPPED SHAPE, unchanged: a `finding_key`-keyed map whose rows carry
        `dismissed_reason` and `dismissed_at`. `gate.open_findings` tests
        membership by `finding_key` and reads nothing else, so the gate needed
        no change for the event stream -- and must not need one.
        """
        return {k: v for k, v in self.triage_state(branch, base_sha).items()
                if v["event"] == self.EVENT_DISMISS}

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

    # --- provider availability cache ---------------------------------------

    def mark_provider_unavailable(self, provider: str, reason: str, category: str,
                                  until_iso: str,
                                  recorded_at: str | None = None) -> None:
        """Record that `provider` is unusable until `until_iso`.

        Everything is validated at the door so the read path only ever has to
        cope with rows corrupted from outside skodun. In particular there is no
        "unavailable forever" state: a TTL is mandatory, so a provider always
        becomes eligible again on its own.
        """
        provider = _require_text("provider", provider)
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
            (provider, until_iso, reason, category, recorded_at))

    def provider_unavailable_reason(self, provider: str, now_iso: str,
                                    env: Mapping[str, str] = os.environ) -> str | None:
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
        row = self._c.execute(
            "SELECT unavailable_until, reason FROM provider_state WHERE provider=?",
            (provider,)).fetchone()
        if row is None or not _still_unavailable(row["unavailable_until"], now_iso):
            return None
        return row["reason"] or "provider marked unavailable"

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
        return [{"provider": r["provider"],
                 "unavailable_until": r["unavailable_until"],
                 "reason": r["reason"], "category": r["category"],
                 "active": _still_unavailable(r["unavailable_until"], now_iso)}
                for r in rows]

    def list_reviews(self, branch: str | None, limit: int = 30) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch is not None:
            q += " WHERE branch=?"
            args = (branch,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
        rows = self._c.execute(q, args + (limit,)).fetchall()
        return [json.loads(r["artifact_json"]) for r in rows]
