import itertools
import json
import sqlite3

import pytest

from skodun.store import SCHEMA_VERSION, Store
from skodun.trust import is_trustworthy

# The Phase 1 `_SCHEMA` DDL, copied VERBATIM. This is what a store written by
# Phase 1 skodun actually contains -- a true v0 database that has never had a
# `provider_state` table and never had `user_version` stamped. Do NOT re-point
# this at `store._SCHEMA`: the migration tests are only evidence if the
# starting database is frozen at the shape the migration has to upgrade *from*.
PHASE1_SCHEMA = """
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

REC = dict(id="r1", reviewed_at="2026-07-27T10:00:00Z", branch="b", head="h"*20,
           base_ref="origin/main", base_sha="s"*40, diff_hash="d"*40, context_hash="",
           mode="now", model="grok-4.20-0309-reasoning", adapter="grok", status="clean",
           parse_ok=True, degraded=False, diff_truncated=False, trustworthy=True,
           stop_reason="EndTurn", summary="ok", findings_total=0,
           severity={"high": 0, "medium": 0, "low": 0}, findings=[])


def _raw_row(path, review_id="r1"):
    """Read a review row straight from the file, bypassing the Store API."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    finally:
        conn.close()


# --- the invariant itself ---------------------------------------------------

def test_is_trustworthy_full_truth_table():
    seen = {}
    for parse_ok, degraded, diff_truncated in itertools.product((True, False), repeat=3):
        got = is_trustworthy(parse_ok, degraded, diff_truncated)
        assert got is (parse_ok and not degraded and not diff_truncated)
        seen[(parse_ok, degraded, diff_truncated)] = got
    assert seen[(True, False, False)] is True
    assert sum(1 for v in seen.values() if v) == 1   # exactly one trustworthy corner


# --- brief's tests ----------------------------------------------------------

def test_roundtrip_and_dedup_query(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    assert st.get_review("r1")["summary"] == "ok"
    assert st.latest_trustworthy_for("d" * 40)["id"] == "r1"


def test_untrustworthy_never_matches_dedup(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "id": "r2", "diff_hash": "e"*40,
                    "degraded": True, "trustworthy": False, "status": "degraded"})
    assert st.latest_trustworthy_for("e" * 40) is None


def test_save_is_upsert(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    st.save_review({**REC, "parse_ok": False, "status": "failed"})
    assert st.latest_trustworthy_for("d" * 40) is None   # demotion visible


def test_trust_is_computed_never_caller_supplied(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "degraded": True, "trustworthy": True})  # liar caller
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.get_review("r1")["trustworthy"] is False   # artifact rewritten too


def test_trust_is_promoted_when_caller_under_claims(tmp_path):
    """Pins the promotion direction of the trust invariant: a caller that
    claims trustworthy=False but whose axes are all clean must still be
    stored (and reported) as trustworthy. Without this test, save_review
    could be mutated to let the caller *veto* trust — e.g.
    `rec["trustworthy"] = bool(rec.get("trustworthy", True)) and
    is_trustworthy(**axes)` — and the rest of the suite (which only ever
    exercises demotion) would still pass."""
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "trustworthy": False})  # pessimistic caller, clean axes
    assert st.latest_trustworthy_for("d" * 40)["id"] == "r1"
    assert st.get_review("r1")["trustworthy"] is True   # artifact rewritten too


def test_non_bool_trust_axis_rejected(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):                       # bool("false") is True
        st.save_review({**REC, "parse_ok": "false"})


def test_set_status_updates_artifact_json_too(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "status": "running"})
    st.set_status("r1", "failed")
    assert st.get_review("r1")["status"] == "failed"


# --- contract points the brief's list leaves uncovered ----------------------

def test_save_review_does_not_mutate_caller_dict(tmp_path):
    st = Store.open(tmp_path / "s.db")
    caller = {**REC, "degraded": True, "trustworthy": True}
    before = json.dumps(caller, sort_keys=True)
    st.save_review(caller)
    assert json.dumps(caller, sort_keys=True) == before
    assert caller["trustworthy"] is True          # the lie stayed in the caller's dict
    assert st.get_review("r1")["trustworthy"] is False   # but never reached the store


def test_upsert_updates_identity_columns_not_just_trust(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.save_review(REC)
    moved = {**REC,
             "reviewed_at": "2026-07-28T11:00:00Z", "branch": "b2", "head": "x"*20,
             "base_ref": "origin/dev", "base_sha": "t"*40, "diff_hash": "f"*40,
             "context_hash": "ctx1", "mode": "queue", "model": "m2", "adapter": "a2",
             "status": "degraded", "summary": "moved", "findings_total": 3,
             "severity": {"high": 1, "medium": 2, "low": 0}, "stop_reason": "MaxTurns"}
    st.save_review(moved)

    row = _raw_row(db)
    for col, want in (("reviewed_at", "2026-07-28T11:00:00Z"), ("branch", "b2"),
                      ("head", "x"*20), ("base_ref", "origin/dev"),
                      ("base_sha", "t"*40), ("diff_hash", "f"*40),
                      ("context_hash", "ctx1"), ("mode", "queue"), ("model", "m2"),
                      ("adapter", "a2"), ("status", "degraded"),
                      ("stop_reason", "MaxTurns"), ("summary", "moved")):
        assert row[col] == want, col
    assert (row["findings_total"], row["sev_high"], row["sev_medium"], row["sev_low"]) \
        == (3, 1, 2, 0)

    # exactly one row, and the old identity no longer resolves
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.latest_trustworthy_for("f" * 40)["id"] == "r1"
    assert st.list_reviews("b", 30) == []
    assert [r["id"] for r in st.list_reviews("b2", 30)] == ["r1"]
    assert st.get_review("r1")["branch"] == "b2"


def test_upsert_updates_source_column(tmp_path):
    """source is the one indexed column with no upsert assertion elsewhere:
    deleting `source=excluded.source` from the ON CONFLICT clause would leave
    the rest of the suite green."""
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.save_review({**REC, "source": "acme"})
    assert _raw_row(db)["source"] == "acme"
    st.save_review({**REC, "source": "other"})
    assert _raw_row(db)["source"] == "other"


def test_missing_trust_axis_defaults_closed(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = {k: v for k, v in REC.items() if k != "parse_ok"}
    st.save_review(rec)
    assert st.get_review("r1")["parse_ok"] is False
    assert st.get_review("r1")["trustworthy"] is False
    assert st.latest_trustworthy_for("d" * 40) is None


@pytest.mark.parametrize("axis", ["parse_ok", "degraded", "diff_truncated"])
@pytest.mark.parametrize("value", ["false", "", 0, 1, None, [], {}])
def test_every_trust_axis_rejects_non_bool(tmp_path, axis, value):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.save_review({**REC, axis: value})
    assert st.get_review("r1") is None      # nothing was written


def test_set_status_updates_indexed_column_too(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.save_review({**REC, "status": "running"})
    st.set_status("r1", "superseded")
    assert _raw_row(db)["status"] == "superseded"
    assert st.get_review("r1")["status"] == "superseded"
    assert st.get_review("r1")["summary"] == "ok"   # rest of the artifact intact


def test_latest_trustworthy_picks_newest(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "id": "old", "reviewed_at": "2026-07-27T09:00:00Z"})
    st.save_review({**REC, "id": "new", "reviewed_at": "2026-07-27T12:00:00Z"})
    st.save_review({**REC, "id": "newest_but_bad", "degraded": True,
                    "reviewed_at": "2026-07-27T13:00:00Z"})
    assert st.latest_trustworthy_for("d" * 40)["id"] == "new"


def test_get_review_missing_returns_none(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.get_review("nope") is None
    assert st.latest_trustworthy_for("z" * 40) is None


def test_list_reviews_orders_and_limits(tmp_path):
    st = Store.open(tmp_path / "s.db")
    for i in range(5):
        st.save_review({**REC, "id": f"r{i}", "reviewed_at": f"2026-07-27T1{i}:00:00Z"})
    st.save_review({**REC, "id": "other", "branch": "zz",
                    "reviewed_at": "2026-07-27T19:00:00Z"})
    assert [r["id"] for r in st.list_reviews(None, 2)] == ["other", "r4"]
    assert [r["id"] for r in st.list_reviews("b", 2)] == ["r4", "r3"]
    assert len(st.list_reviews("b", 30)) == 5


def test_triage_roundtrip_scoped_by_branch_and_base(tmp_path):
    st = Store.open(tmp_path / "s.db")
    base = dict(review_id="r1", file="a.py", line=7, severity="high",
                title="boom", dismissed_reason="wontfix",
                dismissed_at="2026-07-27T10:00:00Z")
    st.add_triage({**base, "ledger_key": "b\0s\0k1", "finding_key": "k1",
                   "branch": "b", "base_sha": "s"*40})
    st.add_triage({**base, "ledger_key": "b\0s\0k2", "finding_key": "k2",
                   "branch": "b", "base_sha": "s"*40, "severity": "low"})
    st.add_triage({**base, "ledger_key": "o\0s\0k3", "finding_key": "k3",
                   "branch": "other", "base_sha": "s"*40})

    got = st.triage_for("b", "s"*40)
    assert set(got) == {"k1", "k2"}
    assert got["k1"]["severity"] == "high"
    assert got["k1"]["dismissed_reason"] == "wontfix"
    assert got["k1"]["file"] == "a.py" and got["k1"]["line"] == 7
    assert st.triage_for("b", "nope") == {}

    # same ledger_key replaces rather than duplicating
    st.add_triage({**base, "ledger_key": "b\0s\0k1", "finding_key": "k1",
                   "branch": "b", "base_sha": "s"*40, "dismissed_reason": "fixed"})
    again = st.triage_for("b", "s"*40)
    assert len(again) == 2
    assert again["k1"]["dismissed_reason"] == "fixed"


def test_triage_accepts_either_review_id_spelling(tmp_path):
    st = Store.open(tmp_path / "s.db")
    common = dict(branch="b", base_sha="s"*40, dismissed_reason="wontfix")
    st.add_triage({**common, "ledger_key": "l1", "finding_key": "k1",
                   "review_id": "rev-a"})
    st.add_triage({**common, "ledger_key": "l2", "finding_key": "k2", "id": "rev-b"})
    got = st.triage_for("b", "s"*40)
    assert got["k1"]["review_id"] == "rev-a"
    assert got["k2"]["review_id"] == "rev-b"


def test_add_triage_rejects_missing_review_id_and_id(tmp_path):
    """A record carrying neither `review_id` nor `id` must fail closed
    (KeyError) rather than silently persisting a triage row with NULL
    review linkage."""
    st = Store.open(tmp_path / "s.db")
    rec = dict(ledger_key="l1", finding_key="k1", branch="b", base_sha="s" * 40,
               dismissed_reason="wontfix")
    with pytest.raises(KeyError):
        st.add_triage(rec)
    assert st.triage_for("b", "s" * 40) == {}


def test_list_reviews_empty_branch_filters_not_all(tmp_path):
    """An explicitly-passed empty branch ("") must filter on that branch
    (returning no rows here), not be treated as falsy and silently ignored.
    None still means 'all branches'."""
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    assert st.list_reviews("", 30) == []
    assert [r["id"] for r in st.list_reviews(None, 30)] == ["r1"]


def test_gate_events_are_durable(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.log_gate_event(dict(at="2026-07-27T10:00:00Z", repo="/r", branch="b",
                           diff_hash="d"*40, outcome="fail", code=1, note="2 open"))
    st.log_gate_event(dict(at="2026-07-27T10:05:00Z", repo="/r", branch="b",
                           diff_hash="d"*40, outcome="skipped", code=2, note=None))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM gate_events ORDER BY at").fetchall()
    finally:
        conn.close()
    assert [(r["outcome"], r["code"], r["note"]) for r in rows] == [
        ("fail", 1, "2 open"), ("skipped", 2, None)]
    assert rows[0]["repo"] == "/r" and rows[0]["diff_hash"] == "d" * 40


def test_open_sets_wal_and_creates_parent_dirs(tmp_path):
    db = tmp_path / "nested" / "deeper" / "s.db"
    st = Store.open(db)
    st.save_review(REC)
    assert db.exists()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_reopen_sees_persisted_rows(tmp_path):
    db = tmp_path / "s.db"
    Store.open(db).save_review(REC)
    assert Store.open(db).get_review("r1")["summary"] == "ok"


# --- schema version + migration ladder --------------------------------------

def _objects(path) -> set:
    """Every table/index in the file, straight from sqlite_master."""
    conn = sqlite3.connect(path)
    try:
        return {(r[0], r[1]) for r in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _user_version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _phase1_db(path, reviews=("r1",)):
    """A real Phase-1-shaped store: v0, Phase 1 DDL, rows in every table."""
    raw = sqlite3.connect(path)
    raw.executescript(PHASE1_SCHEMA)
    for rid in reviews:
        raw.execute(
            "INSERT INTO reviews (id, diff_hash, trustworthy, artifact_json)"
            " VALUES (?, ?, 1, ?)",
            (rid, "d" * 40, json.dumps({"id": rid, "summary": "ok"})))
    raw.execute(
        "INSERT INTO triage (ledger_key, finding_key, review_id, branch, base_sha,"
        " dismissed_reason) VALUES (?, 'k1', 'r1', 'b', ?, 'wontfix')",
        ("b\0s\0k1", "s" * 40))
    raw.execute(
        "INSERT INTO gate_events (at, repo, branch, diff_hash, outcome, code, note)"
        " VALUES ('2026-07-27T10:00:00Z', '/r', 'b', ?, 'fail', 1, '2 open')",
        ("d" * 40,))
    raw.commit()
    raw.close()
    return path


def test_schema_is_frozen_at_the_phase1_baseline():
    """`_SCHEMA` is the immutable v1 baseline every migration in `_MIGRATIONS`
    assumes is already present. Editing it directly (e.g. adding `new_col
    TEXT` to `reviews`) passes the whole suite silently: a fresh database
    gets the column, but `CREATE TABLE IF NOT EXISTS` no-ops on every
    existing store, so the live store never receives it and the next write
    that references the column fails only in production, on the real store.

    Do not fix a failure here by editing `PHASE1_SCHEMA` to match `_SCHEMA`.
    Add a new migration delta to `_MIGRATIONS` instead (bump SCHEMA_VERSION,
    add a `(target_version, ddl)` entry) so existing stores actually receive
    the change."""
    from skodun.store import _SCHEMA
    assert _SCHEMA == PHASE1_SCHEMA, (
        "store._SCHEMA has drifted from the frozen Phase 1 baseline "
        "(PHASE1_SCHEMA above). _SCHEMA must never change: it is re-applied "
        "via `CREATE TABLE IF NOT EXISTS` on every open and is a no-op "
        "against existing stores, so an edit here is invisible to the "
        "5000+-row live store until it breaks on a write. Add a new "
        "migration delta to _MIGRATIONS instead of editing _SCHEMA.")


def test_fresh_db_lands_at_schema_version(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    assert SCHEMA_VERSION == 2
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert ("table", "provider_state") in _objects(db)


def test_migration_ladder_is_ordered_and_reaches_schema_version():
    """A delta added out of order, or a delta added without bumping
    SCHEMA_VERSION, would run on the wrong databases or never be stamped."""
    from skodun.store import _MIGRATIONS
    targets = [t for t, _ in _MIGRATIONS]
    assert targets == sorted(set(targets))
    assert targets[-1] == SCHEMA_VERSION
    assert all(t > 0 for t in targets)


def test_migration_from_true_phase1_db(tmp_path):
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.executescript(PHASE1_SCHEMA)                  # verbatim Phase 1 DDL, v0
    raw.execute("INSERT INTO reviews (id, diff_hash, trustworthy, artifact_json)"
                " VALUES (?, ?, 1, ?)",
                ("r1", "d" * 40, json.dumps({"id": "r1", "summary": "ok"})))
    raw.commit()
    raw.close()
    assert _user_version(db) == 0                     # it really starts at v0
    st = Store.open(db)
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert st.get_review("r1")["summary"] == "ok"     # rows preserved
    st.mark_provider_unavailable("openai", "quota", "quota",
                                 "2026-07-28T12:00:00Z")  # new table exists


def test_phase1_store_upgrade_preserves_every_table_index_and_row(tmp_path):
    """The live store holds thousands of imported reviews. Opening it with the
    new code must add `provider_state` and nothing else: every Phase 1 table,
    every Phase 1 index and every row survives."""
    db = _phase1_db(tmp_path / "s.db", reviews=("r1", "r2", "r3"))
    before = _objects(db)
    assert ("table", "provider_state") not in before

    st = Store.open(db)

    after = _objects(db)
    assert before <= after, before - after            # nothing dropped
    assert after - before == {("table", "provider_state")}   # nothing else added
    assert sorted(r["id"] for r in st.list_reviews(None, 100)) == ["r1", "r2", "r3"]
    assert st.triage_for("b", "s" * 40)["k1"]["dismissed_reason"] == "wontfix"
    assert st._c.execute("SELECT count(*) FROM gate_events").fetchone()[0] == 1
    assert _user_version(db) == SCHEMA_VERSION


def test_reopen_is_idempotent_and_keeps_version_and_rows(tmp_path):
    db = tmp_path / "s.db"
    Store.open(db).mark_provider_unavailable("openai", "quota", "quota",
                                             "2026-07-28T12:00:00Z")
    st = Store.open(db)                               # second open, already v2
    assert _user_version(db) == SCHEMA_VERSION
    assert [r["provider"] for r in st.provider_state_rows("2026-07-28T11:00:00Z")] \
        == ["openai"]


def test_future_schema_refused_before_any_ddl(tmp_path):
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(ValueError, match="newer"):
        Store.open(db)
    raw = sqlite3.connect(db)                         # and it really ran no DDL:
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    raw.close()
    assert "reviews" not in tables and "provider_state" not in tables


def test_future_schema_leaves_a_populated_store_byte_identical(tmp_path):
    """The refusal must not merely skip DDL -- it must not write at all. A
    store written by a newer skodun still holds the user's reviews, and even
    flipping the journal mode rewrites its header."""
    db = _phase1_db(tmp_path / "s.db")
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    before = db.read_bytes()

    with pytest.raises(ValueError, match="newer"):
        Store.open(db)

    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.db"]   # no -wal/-shm
    assert _user_version(db) == 99                     # version not stamped down


def test_future_schema_leaves_a_wal_mode_populated_store_byte_identical(tmp_path):
    """The non-WAL variant above only bites because its fixture starts in
    rollback-journal mode: flipping `PRAGMA journal_mode=WAL` alone rewrites
    header byte 18 (1 -> 2), and that byte flip is what the assertion
    actually detects. The real store is already WAL (confirmed against the
    live file), so `PRAGMA journal_mode=WAL` on it is a no-op -- byte 18
    stays 2 either way, and on an already-Phase1-shaped store `executescript
    (_SCHEMA)` and the (guarded-by-version) migration loop are also no-ops.
    This variant builds the fixture already in WAL mode, so the coverage
    matches deployment: nothing about journal-mode-switching is available to
    mask a wrong refusal order here -- the only write the mutation described
    below can still leak through is the version stamp itself.

    Verified sensitive to a real ordering bug: moving the version check to
    *after* the final `PRAGMA user_version=...` stamp (so a refused store
    still gets stamped down to SCHEMA_VERSION before the ValueError is
    raised) makes this assertion fail -- unlike a check merely moved past
    the WAL pragma alone, which on an already-WAL, already-migrated fixture
    writes nothing either way and so cannot be observed by byte comparison."""
    db = _phase1_db(tmp_path / "s.db")
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    assert db.read_bytes()[18] == 2                     # confirm the fixture is WAL
    before_bytes = db.read_bytes()
    before_files = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(ValueError, match="newer"):
        Store.open(db)

    assert db.read_bytes() == before_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == before_files
    assert _user_version(db) == 99                      # version not stamped down


def test_future_schema_error_names_the_version(tmp_path):
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(ValueError) as e:
        Store.open(db)
    assert "v99" in str(e.value)


# --- provider_state ---------------------------------------------------------

def test_provider_state_ttl_bypass_and_rows(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "rate limited", "quota",
                                 "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z",
                                          env={}) == "rate limited"
    assert st.provider_unavailable_reason("openai", "2026-07-28T13:00:00Z",
                                          env={}) is None
    assert st.provider_unavailable_reason(
        "openai", "2026-07-28T11:00:00Z",
        env={"SKODUN_IGNORE_PROVIDER_STATE": "1"}) is None
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert rows[0]["active"] is True and rows[0]["category"] == "quota"


def test_provider_unavailable_reason_expiry_boundary_is_exclusive(tmp_path):
    """`now == unavailable_until` means the TTL has elapsed: available."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:59:59Z",
                                          env={}) == "quota"
    assert st.provider_unavailable_reason("openai", "2026-07-28T12:00:00Z",
                                          env={}) is None


def test_provider_unavailable_reason_unknown_provider(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.provider_unavailable_reason("nope", "2026-07-28T11:00:00Z",
                                          env={}) is None
    assert st.provider_state_rows("2026-07-28T11:00:00Z") == []


@pytest.mark.parametrize("value,applies", [
    (None, True),        # unset -> state applies
    ("0", True),
    ("1", False),
    ("false", False),    # bool("false") is True -- no truthiness coercion here
    ("no", False),
    ("", True),           # materialized-empty (docker/CI) is unset, not bypass
    ("   ", True),        # whitespace-only likewise
    ("00", False),
])
def test_provider_state_env_bypass_matrix(tmp_path, value, applies):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    env = {} if value is None else {"SKODUN_IGNORE_PROVIDER_STATE": value}
    got = st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z", env=env)
    assert (got == "quota") is applies


def test_provider_state_env_default_is_os_environ(tmp_path, monkeypatch):
    """The `env=os.environ` default is the live mapping, not a snapshot taken
    at import time."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    monkeypatch.delenv("SKODUN_IGNORE_PROVIDER_STATE", raising=False)
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z") == "quota"
    monkeypatch.setenv("SKODUN_IGNORE_PROVIDER_STATE", "1")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z") is None


def test_env_bypass_does_not_hide_rows_from_the_listing(tmp_path, monkeypatch):
    """`skodun providers` is a diagnostic: the bypass changes routing, not
    what the operator can see."""
    monkeypatch.setenv("SKODUN_IGNORE_PROVIDER_STATE", "1")
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert [(r["provider"], r["active"]) for r in rows] == [("openai", True)]


def test_provider_state_write_is_a_single_upsert(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    st.mark_provider_unavailable("openai", "auth expired", "auth",
                                 "2026-07-28T18:00:00Z")
    rows = st.provider_state_rows("2026-07-28T13:00:00Z")
    assert len(rows) == 1
    assert rows[0] == {"provider": "openai", "unavailable_until": "2026-07-28T18:00:00Z",
                       "reason": "auth expired", "category": "auth", "active": True}


def test_provider_state_rows_lists_expired_rows_too(tmp_path):
    """It is the diagnostic listing, not a filter: every row, each flagged."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    st.mark_provider_unavailable("anthropic", "auth", "auth", "2026-07-28T20:00:00Z")
    rows = st.provider_state_rows("2026-07-28T13:00:00Z")
    assert [(r["provider"], r["active"]) for r in rows] == [
        ("anthropic", True), ("openai", False)]        # deterministic order
    assert set(rows[0]) == {"provider", "unavailable_until", "reason", "category",
                            "active"}
    assert rows[1]["reason"] == "quota"                # expired rows keep their data


def test_provider_state_is_per_provider(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("anthropic", "2026-07-28T11:00:00Z",
                                          env={}) is None


def test_provider_state_survives_reopen(tmp_path):
    db = tmp_path / "s.db"
    Store.open(db).mark_provider_unavailable("openai", "quota", "quota",
                                             "2026-07-28T12:00:00Z")
    assert Store.open(db).provider_unavailable_reason(
        "openai", "2026-07-28T11:00:00Z", env={}) == "quota"


@pytest.mark.parametrize("until", [
    None,                     # no expiry is not a supported state
    "",
    "2026-07-28 12:00:00",    # space separator: not lexicographically comparable
    "2026-7-8T12:00:00Z",     # not zero-padded: strptime accepts it, ordering breaks
    "2026-07-28T12:00:00",    # no Z
    "2026-07-28T12:00:00.5Z",
    "2026-07-28T12:00:00+00:00",
    "2026-13-01T00:00:00Z",   # not a real date
    20260728,
    # Unicode decimal digits. `re.\d` matches them and `strptime` accepts them,
    # so both halves of the canonical check waved these through -- while they
    # sort ABOVE every ASCII digit, which is what makes the resulting window or
    # TTL comparison quietly wrong instead of loudly broken.
    "２０２６-01-01T00:00:00Z",       # fullwidth 2026
    "٢٠٢٦-01-01T00:00:00Z",       # Arabic-Indic 2026
    "2026-01-01T00:00:0０Z",                       # one fullwidth digit
])
def test_mark_provider_unavailable_rejects_non_canonical_until(tmp_path, until):
    """Lexicographic TTL comparison is only sound for the fixed-width
    `%Y-%m-%dT%H:%M:%SZ` form, so nothing else may be written."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.mark_provider_unavailable("openai", "quota", "quota", until)
    assert st.provider_state_rows("2026-07-28T11:00:00Z") == []   # nothing written


@pytest.mark.parametrize("provider,reason,category", [
    ("", "quota", "quota"),
    ("openai", "", "quota"),
    ("openai", "quota", ""),
    (None, "quota", "quota"),
    ("openai", None, "quota"),
    ("openai", "quota", None),
])
def test_mark_provider_unavailable_rejects_empty_fields(tmp_path, provider, reason,
                                                        category):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.mark_provider_unavailable(provider, reason, category,
                                     "2026-07-28T12:00:00Z")


def test_provider_unavailable_reason_rejects_non_canonical_now(tmp_path):
    """A bad `now_iso` is a caller bug, not corrupt data: fail loudly rather
    than silently comparing strings that do not order."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    for bad in ("2026-07-28 11:00:00", "2026-7-8T11:00:00Z", "", None):
        with pytest.raises(ValueError):
            st.provider_unavailable_reason("openai", bad, env={})
        with pytest.raises(ValueError):
            st.provider_state_rows(bad)


@pytest.mark.parametrize("stored", [None, "", "later", "2026-7-8T12:00:00Z",
                                    "2026-07-28 12:00:00"])
def test_corrupt_unavailable_until_is_inert_not_a_permanent_ban(tmp_path, stored):
    """A row whose TTL cannot be ordered is unusable. It must not be read as
    "unavailable forever" -- that would permanently disable a working provider
    with no way back except hand-editing the database. It reads as inert, and
    the listing shows it as inactive so an operator can still see it."""
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    st._c.execute("UPDATE provider_state SET unavailable_until=? WHERE provider=?",
                  (stored, "openai"))

    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z",
                                          env={}) is None
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert [(r["provider"], r["active"]) for r in rows] == [("openai", False)]
    assert rows[0]["reason"] == "quota"                # still visible to an operator


@pytest.mark.parametrize("stored", [None, ""])
def test_missing_reason_still_skips_an_unexpired_provider(tmp_path, stored):
    """The opposite direction from a corrupt TTL: the TTL is sound, only the
    explanation is gone. The provider is still unavailable, and the return
    value must stay truthy so a caller's `if reason:` keeps working."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    st._c.execute("UPDATE provider_state SET reason=? WHERE provider=?",
                  (stored, "openai"))
    got = st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z", env={})
    assert got and isinstance(got, str)


def test_mark_provider_unavailable_strips_stored_text_fields(tmp_path):
    """`_require_text` strips only for the emptiness check today, but stores
    the value unstripped. `mark_provider_unavailable(" openai ", ...)` then
    creates a second, unreachable primary-key row: `" openai "` != `"openai"`,
    so `provider_unavailable_reason("openai", ...)` can never match it. The
    stored value must be the stripped one."""
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable(" openai ", " rate limited ", " quota ",
                                 "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z",
                                          env={}) == "rate limited"
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert [r["provider"] for r in rows] == ["openai"]   # not " openai "
    assert rows[0]["reason"] == "rate limited"
    assert rows[0]["category"] == "quota"


def test_mark_provider_unavailable_records_when(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z",
                                 recorded_at="2026-07-28T10:00:00Z")
    assert st._c.execute(
        "SELECT recorded_at FROM provider_state WHERE provider='openai'"
    ).fetchone()[0] == "2026-07-28T10:00:00Z"


def test_mark_provider_unavailable_defaults_recorded_at_to_canonical_now(tmp_path):
    import re
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "quota", "2026-07-28T12:00:00Z")
    got = st._c.execute(
        "SELECT recorded_at FROM provider_state WHERE provider='openai'").fetchone()[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", got)
