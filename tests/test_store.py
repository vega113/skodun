import itertools
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

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

    # A second decision on the same ledger_key appends an event; the effective
    # state is the latest one, so the map still holds exactly one entry per
    # finding_key. (Before v3 this was an `INSERT OR REPLACE` that DISCARDED the
    # first dismissal; the visible result here is deliberately identical.)
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


# --- the append-only triage event stream ------------------------------------
#
# `add_triage` used to `INSERT OR REPLACE` one row per ledger key, so a
# re-dismissal overwrote the previous one and a reopen had nowhere to live. At
# v3 every decision is an EVENT, the effective state is the last event by
# `seq`, and nothing is ever overwritten. `seq` and not `at`: the store's
# timestamps have one-second resolution, and a dismiss and a reopen recorded in
# the same second are exactly the pair whose order decides whether the gate
# passes.

REOPEN_REASON = "the fix regressed on main; the null check is gone again"
DISMISS_REASON = "the guard already lives in validate_input, three frames up"


def _dismissal(**over) -> dict:
    rec = dict(ledger_key="b\0" + "s" * 40 + "\0k1", finding_key="k1", review_id="r1",
               branch="b", base_sha="s" * 40, file="a.py", line=7, severity="high",
               title="boom", dismissed_reason=DISMISS_REASON,
               dismissed_at="2026-07-27T10:00:00Z")
    rec.update(over)
    return rec


def _reopening(**over) -> dict:
    rec = dict(ledger_key="b\0" + "s" * 40 + "\0k1", finding_key="k1", review_id="r1",
               branch="b", base_sha="s" * 40, file="a.py", line=7, severity="high",
               title="boom", reason=REOPEN_REASON, at="2026-07-27T12:00:00Z")
    rec.update(over)
    return rec


def test_add_triage_appends_an_event_and_leaves_the_legacy_table_alone(tmp_path):
    """The legacy `triage` table is READ-ONLY from v3 on: it is the audit source
    the migration seeded from, and writing to it again would create a second,
    disagreeing record of the same decision."""
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    assert st._c.execute("SELECT count(*) FROM triage").fetchone()[0] == 0
    events = _events(st)
    assert len(events) == 1
    e = events[0]
    assert (e["event"], e["finding_key"], e["review_id"]) == ("dismiss", "k1", "r1")
    assert e["reason"] == DISMISS_REASON
    assert e["at"] == "2026-07-27T10:00:00Z"
    assert e["seq"] == 1


def test_a_re_dismissal_appends_instead_of_overwriting_history(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    st.add_triage(_dismissal(dismissed_reason="a second, differently worded reason",
                             dismissed_at="2026-07-27T13:00:00Z"))
    history = st.triage_history(_dismissal()["ledger_key"])
    assert [h["event"] for h in history] == ["dismiss", "dismiss"]
    assert [h["reason"] for h in history] == [DISMISS_REASON,
                                              "a second, differently worded reason"]
    # ... and the effective state is the LATEST one.
    row = st.triage_for("b", "s" * 40)["k1"]
    assert row["dismissed_reason"] == "a second, differently worded reason"


def test_triage_reopen_takes_a_finding_back_out_of_the_dismissed_set(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    assert set(st.triage_for("b", "s" * 40)) == {"k1"}

    st.triage_reopen(_reopening())

    assert st.triage_for("b", "s" * 40) == {}          # the gate sees it as open
    state = st.triage_state("b", "s" * 40)["k1"]
    assert state["event"] == "reopen"
    assert state["reopened_at"] == "2026-07-27T12:00:00Z"
    assert state["dismissed_at"] == "2026-07-27T10:00:00Z"   # both timestamps kept
    assert state["dismissed_reason"] == DISMISS_REASON


def test_a_re_dismissal_after_a_reopen_flips_back_with_all_three_events(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    st.triage_reopen(_reopening())
    st.add_triage(_dismissal(dismissed_reason="fixed for real this time, see the test",
                             dismissed_at="2026-07-27T14:00:00Z"))

    assert set(st.triage_for("b", "s" * 40)) == {"k1"}
    history = st.triage_history(_dismissal()["ledger_key"])
    assert [h["event"] for h in history] == ["dismiss", "reopen", "dismiss"]
    assert [h["seq"] for h in history] == sorted(h["seq"] for h in history)
    assert [h["reason"] for h in history] == [
        DISMISS_REASON, REOPEN_REASON, "fixed for real this time, see the test"]
    state = st.triage_state("b", "s" * 40)["k1"]
    assert state["reopened_at"] == "2026-07-27T12:00:00Z"     # the reopen is not lost
    assert state["dismissed_at"] == "2026-07-27T14:00:00Z"


def test_same_second_dismiss_reopen_dismiss_resolves_by_seq(tmp_path):
    """The store's timestamps are seconds-resolution, so three decisions taken
    inside one second are indistinguishable by `at`. `seq` is a total order and
    is the only thing that may decide this."""
    same = "2026-07-27T10:00:00Z"
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal(dismissed_at=same))
    st.triage_reopen(_reopening(at=same))
    st.add_triage(_dismissal(dismissed_at=same,
                             dismissed_reason="re-dismissed within the same second"))

    assert set(st.triage_for("b", "s" * 40)) == {"k1"}
    assert st.triage_for("b", "s" * 40)["k1"]["dismissed_reason"] == \
        "re-dismissed within the same second"
    assert [h["event"] for h in st.triage_history(_dismissal()["ledger_key"])] == \
        ["dismiss", "reopen", "dismiss"]


def test_the_last_event_by_seq_wins_even_when_the_timestamps_disagree(tmp_path):
    """The mutation killer for "order by `at`".

    A same-second test cannot catch it: SQLite left to itself returns tied rows
    in rowid order, which is `seq` order, so `ORDER BY at` would still answer
    correctly there. Here the timestamps order the events BACKWARDS -- which is
    what a legacy seeded dismissal, a clock adjustment, or an operator-supplied
    `now` produces -- and only `seq` gives the right answer.
    """
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal(dismissed_at="2026-07-27T10:00:02Z"))
    st.triage_reopen(_reopening(at="2026-07-27T10:00:01Z"))   # EARLIER timestamp

    assert st.triage_for("b", "s" * 40) == {}, (
        "the reopen was recorded later and must win; timestamps are display-only")
    assert st.triage_state("b", "s" * 40)["k1"]["event"] == "reopen"


def test_triage_for_is_exactly_the_dismissed_subset_of_triage_state(tmp_path):
    """ONE definition of effective state. The gate reads `triage_for` and the
    listing reads `triage_state`; if those were two independent queries the
    listing could print DISMISSED for a finding the gate still counts as open."""
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    st.add_triage(_dismissal(ledger_key="b\0" + "s" * 40 + "\0k2", finding_key="k2"))
    st.triage_reopen(_reopening(ledger_key="b\0" + "s" * 40 + "\0k2",
                                finding_key="k2"))

    state = st.triage_state("b", "s" * 40)
    assert set(state) == {"k1", "k2"}
    assert st.triage_for("b", "s" * 40) == {
        k: v for k, v in state.items() if v["event"] == "dismiss"}
    assert set(st.triage_for("b", "s" * 40)) == {"k1"}


def test_triage_state_and_history_are_scoped_and_empty_by_default(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    assert st.triage_state("other-branch", "s" * 40) == {}
    assert st.triage_state("b", "nope") == {}
    assert st.triage_history("no such ledger key") == []


def test_triage_for_keeps_its_shipped_row_shape(tmp_path):
    """`gate.open_findings` and `cli triage --list` read these keys off the map,
    and Task 3 may not change the shape they read."""
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    row = st.triage_for("b", "s" * 40)["k1"]
    assert {"ledger_key", "finding_key", "review_id", "branch", "base_sha", "file",
            "line", "severity", "title", "dismissed_reason", "dismissed_at"} <= set(row)
    assert (row["file"], row["line"], row["severity"], row["title"]) == \
        ("a.py", 7, "high", "boom")


def test_triage_reopen_accepts_either_review_id_spelling(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    rec = _reopening()
    rec.pop("review_id")
    st.triage_reopen({**rec, "id": "rev-b"})
    assert st.triage_history(rec["ledger_key"])[-1]["review_id"] == "rev-b"


def test_triage_reopen_rejects_missing_review_id_and_id(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = _reopening()
    rec.pop("review_id")
    with pytest.raises(KeyError):
        st.triage_reopen(rec)
    assert _events(st) == []


@pytest.mark.parametrize("at", [None, "2026-07-27", "2026-7-27T12:00:00Z",
                                "2026-07-27T12:00:00", 1751000000])
def test_triage_reopen_rejects_a_non_canonical_timestamp(tmp_path, at):
    """A reopen is skodun's OWN write -- there is no legacy data to accommodate
    here -- and an unorderable timestamp in an audit stream is a record nobody
    can read back."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.triage_reopen(_reopening(at=at))
    assert _events(st) == []


@pytest.mark.parametrize("reason", [None, "", "   ", 7])
def test_triage_reopen_rejects_a_missing_or_blank_reason(tmp_path, reason):
    """The audit floor itself is `triage.validate_reason`'s job; this is the
    door: a reopen with no reason at all must never reach the stream."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.triage_reopen(_reopening(reason=reason))
    assert _events(st) == []


def test_the_event_column_admits_nothing_but_dismiss_and_reopen(tmp_path):
    """A third verb would be read as "not a dismissal" by `triage_for` and as
    "dismissed" by nothing -- the CHECK constraint keeps the vocabulary closed
    even against a hand-written INSERT."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        st._c.execute("INSERT INTO triage_events (ledger_key, finding_key, event)"
                      " VALUES ('lk', 'k1', 'deleted')")
    for event in ("dismiss", "reopen"):
        st._c.execute("INSERT INTO triage_events (ledger_key, finding_key, event)"
                      " VALUES ('lk', 'k1', ?)", (event,))


def test_the_event_vocabulary_is_spelled_once_everywhere_it_matters():
    """`Store.EVENT_DISMISS` is what `triage_for` filters on, and the same word
    is a LITERAL in the v3 CHECK constraint and in the migration's seeding
    statement. A constant that drifted from those literals would make every
    seeded legacy dismissal stop matching -- silently, because the rows would
    all still be there."""
    from skodun.store import _MIGRATION_V3

    ddl = _MIGRATION_V3[0]
    assert "triage_events" in ddl
    assert f"'{Store.EVENT_DISMISS}'" in ddl and f"'{Store.EVENT_REOPEN}'" in ddl
    seeding = [s for s in _MIGRATION_V3 if "INSERT INTO triage_events" in s]
    assert len(seeding) == 1
    assert f"'{Store.EVENT_DISMISS}'" in seeding[0]


def test_triage_events_survive_a_reopen_of_the_store(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.add_triage(_dismissal())
    st.triage_reopen(_reopening())
    st.close()
    again = Store.open(db)
    assert [h["event"] for h in again.triage_history(_dismissal()["ledger_key"])] == \
        ["dismiss", "reopen"]
    assert again.triage_for("b", "s" * 40) == {}


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


#: The v2 delta's DDL, copied VERBATIM, for exactly the reason `PHASE1_SCHEMA`
#: is copied: a v2 fixture is only evidence about the v2 -> v3 upgrade if it is
#: frozen at the shape that upgrade has to start from. Do NOT re-point this at
#: `store._MIGRATION_V2`.
PHASE2_PROVIDER_STATE = """
CREATE TABLE IF NOT EXISTS provider_state (
  provider TEXT PRIMARY KEY, unavailable_until TEXT, reason TEXT,
  category TEXT, recorded_at TEXT
);
"""

#: Every object and column the ONE v3 delta must install, frozen here. Phase 3
#: installs all of its DDL in this single migration on purpose -- the ladder
#: runs a delta only while `user_version < target`, so a later task cannot add
#: a column to a store that has already been stamped v3. A delta that quietly
#: loses one of these has to be a red test here, not a discovery made against
#: the live store three tasks later.
V3_TABLES = {"triage_events", "dedup_events", "deliveries"}
V3_REVIEW_COLUMNS = {"worst_runtime_sec", "pid", "superseded_by"}
V3_TRIAGE_EVENT_COLUMNS = ["seq", "ledger_key", "finding_key", "event", "review_id",
                           "branch", "base_sha", "file", "line", "severity", "title",
                           "reason", "at"]
V3_DEDUP_EVENT_COLUMNS = ["at", "branch", "diff_hash", "matched_review_id"]
V3_DELIVERY_COLUMNS = ["review_id", "delivered_at", "channel"]

#: One legacy `triage` row, in the shipped single-row-per-ledger-key shape the
#: v3 migration has to seed an event from.
LEGACY_TRIAGE = dict(ledger_key="b\0" + "s" * 40 + "\0k1", finding_key="k1",
                     review_id="r1", branch="b", base_sha="s" * 40, file="a.py",
                     line=7, severity="high", title="boom",
                     dismissed_reason="the guard already lives in the caller, verified",
                     dismissed_at="2026-07-27T10:00:00Z")


def _columns(path, table) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _insert_legacy_triage(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO triage (ledger_key, finding_key, review_id, branch, base_sha,
             file, line, severity, title, dismissed_reason, dismissed_at)
           VALUES (:ledger_key, :finding_key, :review_id, :branch, :base_sha, :file,
             :line, :severity, :title, :dismissed_reason, :dismissed_at)""", row)


def _v2_db(path, *, triage_rows=(LEGACY_TRIAGE,)):
    """A real v2-shaped store: Phase 1 DDL + `provider_state`, stamped v2.

    This is what a store written by Phase 2 skodun actually contains, which is
    the database the v3 delta has to upgrade in production.
    """
    raw = sqlite3.connect(path)
    raw.executescript(PHASE1_SCHEMA)
    raw.executescript(PHASE2_PROVIDER_STATE)
    raw.execute("INSERT INTO reviews (id, diff_hash, trustworthy, artifact_json)"
                " VALUES ('r1', ?, 1, ?)",
                ("d" * 40, json.dumps({"id": "r1", "summary": "ok"})))
    for row in triage_rows:
        _insert_legacy_triage(raw, row)
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()
    return path


def _events(path_or_store) -> list[dict]:
    """Every `triage_events` row in `seq` order, read raw."""
    if isinstance(path_or_store, Store):
        rows = path_or_store._c.execute(
            "SELECT * FROM triage_events ORDER BY seq").fetchall()
        return [dict(r) for r in rows]
    conn = sqlite3.connect(path_or_store)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM triage_events ORDER BY seq")]
    finally:
        conn.close()


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
    assert SCHEMA_VERSION == 3
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert ("table", "provider_state") in _objects(db)
    for table in V3_TABLES:
        assert ("table", table) in _objects(db)
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))


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
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == 3
    assert st.get_review("r1")["summary"] == "ok"     # rows preserved
    st.mark_provider_unavailable("openai", "quota", "quota",
                                 "2026-07-28T12:00:00Z")  # new table exists


def test_phase1_store_upgrade_preserves_every_table_index_and_row(tmp_path):
    """The live store holds thousands of imported reviews. Opening it with the
    new code must ADD the v2 and v3 objects and nothing else: every Phase 1
    table, every Phase 1 index and every row survives."""
    db = _phase1_db(tmp_path / "s.db", reviews=("r1", "r2", "r3"))
    before = _objects(db)
    assert ("table", "provider_state") not in before

    st = Store.open(db)

    after = _objects(db)
    assert before <= after, before - after            # nothing dropped
    assert after - before == {("table", "provider_state")} | {
        ("table", t) for t in V3_TABLES}              # nothing else added
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
    assert not (V3_TABLES & tables)


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


def test_a_store_one_version_above_this_build_is_refused_untouched(tmp_path):
    """The refusal is `> SCHEMA_VERSION`, not `> 2`: the interesting case is the
    NEXT version, not 99. A real v3 store stamped v4 by a newer skodun must come
    back byte-identical, exactly as the v0/v2 fixtures above do."""
    db = _v2_db(tmp_path / "s.db")
    Store.open(db).close()                              # a real, migrated v3 store
    assert _user_version(db) == 3
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1:d}")
    raw.commit()
    raw.close()
    before = db.read_bytes()

    with pytest.raises(ValueError, match="newer"):
        Store.open(db)

    assert db.read_bytes() == before
    assert _user_version(db) == SCHEMA_VERSION + 1       # not stamped down


# --- v3: ONE atomic delta installing every Phase 3 object -------------------
#
# Phase 3 puts ALL of its DDL in this one migration deliberately: the ladder
# runs a delta only while `user_version < target`, so a store already stamped
# v3 would never receive a column a later task tried to add. The delta is also
# the first one that is NOT replay-idempotent -- `ALTER TABLE ADD COLUMN`
# raises on a second application -- which is why it runs inside one explicit
# transaction with its own version stamp, and why the crash-injection test
# below is the load-bearing one in this section.


def test_a_v2_store_gains_every_v3_delta(tmp_path):
    db = _v2_db(tmp_path / "s.db")
    assert _user_version(db) == 2
    before = _objects(db)

    st = Store.open(db)

    assert _user_version(db) == 3
    assert before <= _objects(db)                          # nothing dropped
    assert _objects(db) - before == {("table", t) for t in V3_TABLES}
    assert _columns(db, "triage_events") == V3_TRIAGE_EVENT_COLUMNS
    assert _columns(db, "dedup_events") == V3_DEDUP_EVENT_COLUMNS
    assert _columns(db, "deliveries") == V3_DELIVERY_COLUMNS
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))
    # ... and the Phase 1 rows are all still there.
    assert st.get_review("r1")["summary"] == "ok"


def test_the_v3_reviews_columns_are_nullable_and_default_null(tmp_path):
    """T8/T10 write them later; every existing row must read as "not set"
    rather than as a number the stale-recovery sweep would act on."""
    db = _v2_db(tmp_path / "s.db")
    st = Store.open(db)
    row = st._c.execute("SELECT worst_runtime_sec, pid, superseded_by FROM reviews"
                        " WHERE id='r1'").fetchone()
    assert dict(row) == {"worst_runtime_sec": None, "pid": None, "superseded_by": None}
    st.save_review(REC)                     # and a NEW row is writable unchanged
    assert st.get_review("r1") is not None


def test_the_v3_delta_seeds_one_dismiss_event_per_existing_triage_row(tmp_path):
    """The legacy `triage` table is single-row-per-ledger-key and becomes
    READ-ONLY at v3. Every dismissal a human already recorded has to arrive in
    the event stream, with every field it was recorded with -- a dismissal
    silently lost here reopens a finding that was litigated months ago."""
    second = dict(LEGACY_TRIAGE, ledger_key="b\0" + "s" * 40 + "\0k2",
                  finding_key="k2", severity="low", title="second",
                  dismissed_at="2026-07-27T11:00:00Z")
    db = _v2_db(tmp_path / "s.db", triage_rows=(LEGACY_TRIAGE, second))

    st = Store.open(db)

    events = _events(st)
    assert [e["event"] for e in events] == ["dismiss", "dismiss"]
    assert [e["finding_key"] for e in events] == ["k1", "k2"]
    first = events[0]
    assert first["ledger_key"] == LEGACY_TRIAGE["ledger_key"]
    assert first["review_id"] == "r1"
    assert (first["branch"], first["base_sha"]) == ("b", "s" * 40)
    assert (first["file"], first["line"]) == ("a.py", 7)
    assert (first["severity"], first["title"]) == ("high", "boom")
    assert first["reason"] == LEGACY_TRIAGE["dismissed_reason"]
    assert first["at"] == LEGACY_TRIAGE["dismissed_at"]
    # ... and the effective state the gate reads is unchanged by the migration.
    assert set(st.triage_for("b", "s" * 40)) == {"k1", "k2"}
    assert st.triage_for("b", "s" * 40)["k1"]["dismissed_reason"] == first["reason"]
    # The legacy table is preserved, not rewritten: it is the audit source the
    # seeding came from.
    assert st._c.execute("SELECT count(*) FROM triage").fetchone()[0] == 2


def test_the_v3_delta_seeds_nothing_on_a_store_with_no_dismissals(tmp_path):
    db = _v2_db(tmp_path / "s.db", triage_rows=())
    st = Store.open(db)
    assert _events(st) == []
    assert st.triage_for("b", "s" * 40) == {}


def test_seeding_runs_once_and_a_reopen_survives_the_next_open(tmp_path):
    """The seeding is part of the 2 -> 3 delta, so it must not re-run on a
    store that is already v3: a second seeding would append a fresh `dismiss`
    event on top of a human's later `reopen` and silently re-dismiss it."""
    db = _v2_db(tmp_path / "s.db")
    st = Store.open(db)
    st.triage_reopen(dict(LEGACY_TRIAGE, at="2026-07-28T09:00:00Z",
                          reason="the crash reproduces on main, reopening it"))
    st.close()

    st2 = Store.open(db)
    assert [e["event"] for e in _events(st2)] == ["dismiss", "reopen"]
    assert st2.triage_for("b", "s" * 40) == {}


def _broken_v3_ladder(monkeypatch):
    """The real v3 delta with one failing statement injected AFTER the ALTERs.

    This is the crash: the columns have been added inside the transaction and
    the version has NOT been stamped yet. `max()` raising here would mean the
    delta no longer contains an `ALTER TABLE` at all, which is itself the thing
    that makes the transaction mandatory -- so the lookup is deliberately not
    defensive.
    """
    from skodun import store as store_mod

    real = list(store_mod._MIGRATION_V3)
    last_alter = max(i for i, s in enumerate(real)
                     if s.lstrip().upper().startswith("ALTER TABLE"))
    broken = tuple(real[:last_alter + 1]
                   + ["INSERT INTO no_such_table_boom (x) VALUES (1)"]
                   + real[last_alter + 1:])
    monkeypatch.setattr(store_mod, "_MIGRATIONS",
                        tuple((t, broken if t == 3 else d)
                              for t, d in store_mod._MIGRATIONS))


def test_a_crash_mid_v3_delta_leaves_a_clean_v2_store_that_migrates_on_retry(
        tmp_path, monkeypatch):
    """THE reason the v3 delta is transactional.

    The shipped ladder is deliberately non-transactional and `IF NOT EXISTS`
    idempotent, so a crash between "delta applied" and "version stamped"
    replays harmlessly. `ALTER TABLE ADD COLUMN` breaks that property: replayed,
    it raises `duplicate column name`, and every subsequent open of the store
    would fail -- a bricked store holding thousands of reviews. So the whole
    delta, seeding and version stamp included, commits or does nothing.
    """
    db = _v2_db(tmp_path / "s.db")
    _broken_v3_ladder(monkeypatch)

    with pytest.raises(sqlite3.OperationalError):
        Store.open(db)

    # NOTHING from the delta survived: not the tables, not the columns, not the
    # stamp -- and the legacy dismissal is untouched.
    assert _user_version(db) == 2
    assert not (V3_TABLES & {name for _, name in _objects(db)})
    assert not (V3_REVIEW_COLUMNS & set(_columns(db, "reviews")))
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM triage").fetchone()[0] == 1
    finally:
        conn.close()

    monkeypatch.undo()                               # the crash is over; retry
    st = Store.open(db)
    assert _user_version(db) == 3
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))
    assert [e["event"] for e in _events(st)] == ["dismiss"]


def test_a_crashed_v3_migration_never_leaves_the_store_open(tmp_path, monkeypatch):
    """`Store.open` closes the connection on any failure (shipped rule), and a
    half-applied migration must not be the one exception -- a leaked connection
    still holding the write lock would make the retry above fail too."""
    db = _v2_db(tmp_path / "s.db")
    _broken_v3_ladder(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        Store.open(db)
    monkeypatch.undo()
    # A second process can take the write lock immediately, which it could not
    # if the failed attempt still held an open transaction.
    other = sqlite3.connect(db, isolation_level=None, timeout=0.5)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_the_v3_delta_is_not_replay_idempotent_which_is_why_it_is_atomic(tmp_path):
    """Pins the premise of the transaction rather than restating it in prose: a
    second application of the same statements really does raise."""
    from skodun.store import _MIGRATION_V3, _apply_atomic

    db = _v2_db(tmp_path / "s.db")
    st = Store.open(db)
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        _apply_atomic(st._c, 3, _MIGRATION_V3)
    # ... and the failed replay rolled back, so the store is still usable.
    assert _user_version(db) == 3
    assert [e["event"] for e in _events(st)] == ["dismiss"]


def test_no_non_transactional_delta_carries_a_non_idempotent_statement():
    """The ladder now has two lanes, and putting a delta in the wrong one is
    silent until a crash: a `str` delta is `executescript`ed OUTSIDE any
    transaction (the shipped v2 contract, kept exactly), while a tuple of
    statements runs inside one `BEGIN IMMEDIATE` with its own stamp. `ALTER
    TABLE` in the non-transactional lane is the bricking case."""
    from skodun.store import _MIGRATIONS

    for target, delta in _MIGRATIONS:
        if isinstance(delta, str):
            assert "ALTER TABLE" not in delta.upper(), target
        else:
            assert isinstance(delta, tuple) and all(isinstance(s, str) for s in delta)
    # The v3 delta specifically is the transactional kind, and it is the last
    # rung: `SCHEMA_VERSION` is what a fresh store is stamped with.
    assert _MIGRATIONS[-1][0] == SCHEMA_VERSION == 3
    assert isinstance(_MIGRATIONS[-1][1], tuple)


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


# --- store lifetime: close(), context manager -------------------------------
#
# Phase 3 makes a `Store`'s connection lifetime real: the dispatcher and MCP
# server (later tasks) hold one open far longer than any one-shot CLI
# invocation ever did, so "does closing actually happen, and does using a
# closed store fail loudly" stops being academic.

def test_close_is_idempotent(tmp_path):
    """Calling `close()` twice -- or on a store nothing was ever written
    through -- must not raise. `sqlite3.Connection.close()` is already
    idempotent; `Store.close()` only has to forward to it without adding a
    guard that could get in the way of that."""
    st = Store.open(tmp_path / "s.db")
    st.close()
    st.close()   # must not raise


def test_context_manager_returns_the_store_itself(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        assert isinstance(st, Store)
        st.save_review(REC)
        assert st.get_review("r1")["summary"] == "ok"


def test_context_manager_closes_on_normal_exit(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review(REC)
    with pytest.raises(sqlite3.ProgrammingError):
        st.get_review("r1")


def test_context_manager_closes_even_when_the_body_raises(tmp_path):
    """The point of `__exit__`: a review or triage failure mid-command must
    not leak the connection just because the code that was using it never
    reached its own cleanup."""
    db = tmp_path / "s.db"
    st_ref = {}
    with pytest.raises(RuntimeError, match="boom"):
        with Store.open(db) as st:
            st_ref["st"] = st
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        st_ref["st"].get_review("nope")


def test_operating_on_a_closed_store_raises_programming_error_not_swallowed(tmp_path):
    """The mutation-killer for a no-op `close()`: if `close()` stopped
    actually closing the underlying connection, every assertion below would
    fail because the store would still answer normally instead of raising.
    `sqlite3.ProgrammingError` specifically, and uncaught -- `close()` must
    never wrap this in something that quietly returns None or an empty
    result instead."""
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    st.close()
    with pytest.raises(sqlite3.ProgrammingError):
        st.get_review("r1")
    with pytest.raises(sqlite3.ProgrammingError):
        st.save_review({**REC, "id": "r2"})
    with pytest.raises(sqlite3.ProgrammingError):
        st.list_reviews(None, 10)


# --- ResourceWarning-clean store suite: supplementary regression net --------

#: Every test module that opens a `Store` (directly, or by driving `cli.main`
#: /`run_gate`/`run_review`/`import_legacy`/`shadow.compare` against a real
#: one). `test_passes.py` matches `Store` in a grep only because of two
#: `svc/credential/Store.go` / `UserStore.scala` fixture PATH STRINGS -- it
#: never opens one -- so it is deliberately excluded.
_STORE_TOUCHING_MODULES = (
    "tests/test_store.py",
    "tests/test_cli.py",
    "tests/test_gate.py",
    "tests/test_fallback.py",
    "tests/test_pipeline.py",
    "tests/test_legacy_import.py",
    "tests/test_shadow.py",
    "tests/test_refuter.py",
    "tests/test_triage.py",
)


#: Guards against the recursion this test would otherwise cause: `test_store.py`
#: is itself one of `_STORE_TOUCHING_MODULES`, so the subprocess it spawns
#: collects THIS test too, which would spawn another subprocess, forever.
#: `--deselect` below is the primary defence; this is the belt for it, so a
#: future rename of this test (which would silently break a `--deselect`
#: pointed at the old nodeid) cannot reintroduce an unbounded process tree.
_RESOURCEWARNING_SUBPROCESS_GUARD_ENV = "_SKODUN_RESOURCEWARNING_SUBPROCESS_ACTIVE"
_THIS_TEST_NODEID = ("tests/test_store.py::"
                     "test_store_touching_modules_run_clean_under_resourcewarning_error")


def test_store_touching_modules_run_clean_under_resourcewarning_error(tmp_path):
    """A supplementary regression net, NOT the mutation-killer for a no-op
    `close()` -- `test_operating_on_a_closed_store_raises_programming_error_
    not_swallowed` above is. `sqlite3.Connection`'s own "unclosed database"
    warning is emitted from the connection's finalizer during garbage
    collection, and a warning raised from a finalizer is UNRAISABLE: Python
    cannot let it propagate as a real exception there (that is exactly what
    `sys.unraisablehook` exists to handle instead), so `-W error::
    ResourceWarning` cannot turn a GC-collected leaked connection into a
    failing assertion no matter how strict the filter is. Confirmed
    empirically on the local interpreter: pytest's own `unraisableexception`
    plugin re-reports it as `pytest.PytestUnraisableExceptionWarning` -- a
    `UserWarning` subclass, not a `ResourceWarning` -- specifically so it
    does not vanish silently, but that also means this specific filter never
    matches it. This run's job is narrower and still worth having: every
    store-touching test module must complete with zero test FAILURES under
    the stricter flag, so a future change that explicitly warns/raises a real
    `ResourceWarning` (rather than relying on GC finalization timing) is still
    caught here rather than only in a developer's interactive `-W` run.
    """
    if os.environ.get(_RESOURCEWARNING_SUBPROCESS_GUARD_ENV):
        # We ARE the nested subprocess this test spawns -- see the guard's
        # own docstring above for why this check exists at all.
        pytest.skip("nested inside the subprocess this test itself spawns")

    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    # Never the real store, even though every module above already pins
    # `SKODUN_DB` itself (autouse fixture or explicit `tmp_path` construction):
    # this is defence in depth for the one process-wide default the CLI falls
    # back to before any of that runs.
    env["SKODUN_DB"] = str(tmp_path / "unused" / "skodun.db")
    env["PYTHONUNBUFFERED"] = "1"
    env[_RESOURCEWARNING_SUBPROCESS_GUARD_ENV] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "src"), str(repo_root)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    proc = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "pytest", "-q",
         "--deselect", _THIS_TEST_NODEID, *_STORE_TOUCHING_MODULES],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        f"store-touching subset failed under -W error::ResourceWarning "
        f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
