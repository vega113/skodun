import itertools
import json
import sqlite3

import pytest

from skodun.store import Store
from skodun.trust import is_trustworthy

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
