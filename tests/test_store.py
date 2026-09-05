"""Store lifecycle and persistence invariants, including warning-free closure."""

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


def _a_finding() -> dict:
    return dict(file="a.py", line=3, severity="high", category="bug",
                title="NPE", detail="why")


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


def test_list_reviews_scopes_by_repo_only_when_a_branch_is_given(tmp_path):
    """`--branch` is the ambiguous key and the only thing `repo` narrows. An
    unscoped listing is a human's "show me everything" and must keep crossing
    repositories -- including the pre-v5 rows no scoped query can reach."""
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(dict(REC, id="in_a", branch="main", repo="/repos/a"))
        st.save_review(dict(REC, id="in_b", branch="main", repo="/repos/b"))
        st.save_review(dict(REC, id="pre_v5", branch="main"))

        assert [r["id"] for r in st.list_reviews("main", 30, "/repos/a")] == ["in_a"]
        assert sorted(r["id"] for r in st.list_reviews(None, 30, "/repos/a")) == [
            "in_a", "in_b", "pre_v5"], "an unscoped listing must not be filtered"
        assert st.list_reviews("main", 30, "/repos/nowhere") == []


def test_running_records_returns_the_indexed_columns_and_only_running_rows(
        tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(dict(REC, id="done", status="clean"))
        res = _reserve(st, branch="main", repo="/repos/a")
        st.save_review(dict(REC, id="legacy", status="running", parse_ok=False,
                            trustworthy=False))

        rows = st.running_records()

        assert sorted(r["id"] for r in rows) == ["legacy", res.record_id]
        assert set(rows[0]) == {"id", "reviewed_at", "worst_runtime_sec"}
        by_id = {r["id"]: r for r in rows}
        assert by_id[res.record_id]["worst_runtime_sec"] == 1234
        assert by_id["legacy"]["worst_runtime_sec"] is None


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


def test_the_event_column_admits_nothing_outside_the_three_verbs(tmp_path):
    """A fourth verb would be read as "not a dismissal" by `triage_for` and as
    "dismissed" by nothing -- the CHECK constraint keeps the vocabulary closed
    even against a hand-written INSERT."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        st._c.execute("INSERT INTO triage_events (ledger_key, finding_key, event)"
                      " VALUES ('lk', 'k1', 'deleted')")
    for event in ("dismiss", "reopen", "defer"):
        st._c.execute("INSERT INTO triage_events (ledger_key, finding_key, event)"
                      " VALUES ('lk', 'k1', ?)", (event,))


def test_the_event_vocabulary_is_spelled_once_everywhere_it_matters():
    """`Store.EVENT_DISMISS` is what `triage_for` filters on, and the same word
    is a LITERAL in the v3 CHECK constraint and in the migration's seeding
    statement. A constant that drifted from those literals would make every
    seeded legacy dismissal stop matching -- silently, because the rows would
    all still be there.

    The v4 rebuild re-spells the whole vocabulary in its own CHECK, so all three
    constants are pinned against that DDL too: a `defer` the constant spells one
    way and the constraint another is a verb that can never be written at all.
    """
    from skodun.store import _MIGRATION_V3, _MIGRATION_V4

    ddl = _MIGRATION_V3[0]
    assert "triage_events" in ddl
    assert f"'{Store.EVENT_DISMISS}'" in ddl and f"'{Store.EVENT_REOPEN}'" in ddl
    seeding = [s for s in _MIGRATION_V3 if "INSERT INTO triage_events" in s]
    assert len(seeding) == 1
    assert f"'{Store.EVENT_DISMISS}'" in seeding[0]

    rebuilt = _MIGRATION_V4[0]
    for verb in (Store.EVENT_DISMISS, Store.EVENT_REOPEN, Store.EVENT_DEFER):
        assert f"'{verb}'" in rebuilt, verb
    # ... and the set the gate clears on is spelled from those same constants.
    assert Store.CLEARING_EVENTS == frozenset(
        {Store.EVENT_DISMISS, Store.EVENT_DEFER})


# --- the THIRD verb: defer, with a mandatory tracking reference -------------
#
# `defer` means "real, not blast-radius for this change, filed as X". It clears
# the gate exactly as `dismiss` does -- that is the escape from the endless
# review round -- and the ONLY thing that keeps it honest is the filed
# reference, which is why it lives in a column of its own rather than inside the
# reason prose. A ledger that cannot answer "what has this project deferred and
# where is it filed" is a ledger in which a deferral and an ignored finding are
# the same artifact.

DEFER_REASON = "in-bounds for this surface; the hot path is the batcher, not this"
TRACKING_REF = "GH-412"


def _deferral(**over) -> dict:
    rec = dict(ledger_key="b\0" + "s" * 40 + "\0k1", finding_key="k1", review_id="r1",
               branch="b", base_sha="s" * 40, file="a.py", line=7, severity="high",
               title="boom", reason=DEFER_REASON, tracking_ref=TRACKING_REF,
               at="2026-07-27T11:00:00Z")
    rec.update(over)
    return rec


def test_triage_defer_appends_a_defer_event_carrying_its_reference(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())
    events = _events(st)
    assert len(events) == 1
    e = events[0]
    assert (e["event"], e["finding_key"], e["review_id"]) == ("defer", "k1", "r1")
    assert e["reason"] == DEFER_REASON
    assert e["tracking_ref"] == TRACKING_REF
    assert e["at"] == "2026-07-27T11:00:00Z"
    # ... and the legacy single-row table stays untouched, as for every verb.
    assert st._c.execute("SELECT count(*) FROM triage").fetchone()[0] == 0


def test_a_deferred_finding_is_cleared_for_the_gate_exactly_like_a_dismissal(
        tmp_path):
    """THE property `gate.py` depends on without changing a byte: `triage_for`
    is the map whose membership clears a finding, and a deferral belongs in it."""
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())
    cleared = st.triage_for("b", "s" * 40)
    assert set(cleared) == {"k1"}
    assert cleared["k1"]["event"] == "defer"
    assert cleared["k1"]["tracking_ref"] == TRACKING_REF


def test_triage_for_is_exactly_the_cleared_subset_of_triage_state(tmp_path):
    """ONE definition of "cleared", spelled from `CLEARING_EVENTS` and nowhere
    else: a reopened finding is open, a dismissed or deferred one is not."""
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    st.triage_defer(_deferral(ledger_key="b\0" + "s" * 40 + "\0k2", finding_key="k2"))
    st.add_triage(_dismissal(ledger_key="b\0" + "s" * 40 + "\0k3", finding_key="k3"))
    st.triage_reopen(_reopening(ledger_key="b\0" + "s" * 40 + "\0k3",
                                finding_key="k3"))

    state = st.triage_state("b", "s" * 40)
    assert set(state) == {"k1", "k2", "k3"}
    assert st.triage_for("b", "s" * 40) == {
        k: v for k, v in state.items() if v["event"] in Store.CLEARING_EVENTS}
    assert set(st.triage_for("b", "s" * 40)) == {"k1", "k2"}


def test_a_defer_after_a_dismissal_wins_and_keeps_both_reasons(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    st.triage_defer(_deferral())
    state = st.triage_state("b", "s" * 40)["k1"]
    assert state["event"] == "defer"
    assert state["deferred_at"] == "2026-07-27T11:00:00Z"
    assert state["defer_reason"] == DEFER_REASON
    assert state["deferred_ref"] == TRACKING_REF
    assert state["dismissed_reason"] == DISMISS_REASON        # not lost
    assert [h["event"] for h in st.triage_history(_dismissal()["ledger_key"])] == \
        ["dismiss", "defer"]


def test_a_reopen_after_a_defer_puts_the_finding_back_in_front_of_the_gate(
        tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())
    st.triage_reopen(_reopening(at="2026-07-27T12:00:00Z"))
    assert st.triage_for("b", "s" * 40) == {}
    state = st.triage_state("b", "s" * 40)["k1"]
    assert state["event"] == "reopen"
    assert state["deferred_ref"] == TRACKING_REF          # the deferral is kept


def test_the_last_defer_event_by_seq_wins_even_when_the_timestamps_disagree(
        tmp_path):
    """The mutation killer for "order by `at`", for the third verb.

    A same-second test cannot catch it -- SQLite returns tied rows in rowid
    order, which is `seq` order. Here the timestamps order the two events
    BACKWARDS, which is what a clock adjustment or an operator-supplied `now`
    produces, and only `seq` gives the right answer.
    """
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral(at="2026-07-27T10:00:02Z"))
    st.triage_reopen(_reopening(at="2026-07-27T10:00:01Z"))   # EARLIER timestamp

    assert st.triage_for("b", "s" * 40) == {}, (
        "the reopen was recorded later and must win; timestamps are display-only")
    assert st.triage_state("b", "s" * 40)["k1"]["event"] == "reopen"

    # ... and the other direction: a defer recorded after a reopen, dated before
    # it, still clears the gate.
    st2 = Store.open(tmp_path / "s2.db")
    st2.add_triage(_dismissal(dismissed_at="2026-07-27T10:00:00Z"))
    st2.triage_reopen(_reopening(at="2026-07-27T10:00:09Z"))
    st2.triage_defer(_deferral(at="2026-07-27T10:00:03Z"))    # EARLIER timestamp
    assert set(st2.triage_for("b", "s" * 40)) == {"k1"}
    assert st2.triage_state("b", "s" * 40)["k1"]["event"] == "defer"


@pytest.mark.parametrize("ref", [None, "", "   ", 7])
def test_triage_defer_rejects_a_missing_or_blank_tracking_reference(tmp_path, ref):
    """The audit floor on the reference's SHAPE is `triage.validate_tracking_ref`;
    this is the door. A `defer` row with no reference is the exact artifact this
    verb exists to make impossible, so it must never reach the stream."""
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.triage_defer(_deferral(tracking_ref=ref))
    assert _events(st) == []


@pytest.mark.parametrize("reason", [None, "", "   ", 7])
def test_triage_defer_rejects_a_missing_or_blank_reason(tmp_path, reason):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.triage_defer(_deferral(reason=reason))
    assert _events(st) == []


@pytest.mark.parametrize("at", [None, "2026-07-27", "2026-7-27T12:00:00Z",
                                "2026-07-27T12:00:00", 1751000000])
def test_triage_defer_rejects_a_non_canonical_timestamp(tmp_path, at):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        st.triage_defer(_deferral(at=at))
    assert _events(st) == []


def test_triage_defer_rejects_missing_review_id_and_id(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = _deferral()
    rec.pop("review_id")
    with pytest.raises(KeyError):
        st.triage_defer(rec)
    assert _events(st) == []


def test_triage_defer_accepts_either_review_id_spelling(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = _deferral()
    rec.pop("review_id")
    st.triage_defer({**rec, "id": "rev-b"})
    assert st.triage_history(rec["ledger_key"])[-1]["review_id"] == "rev-b"


def test_the_other_two_verbs_never_write_a_tracking_reference(tmp_path):
    """`tracking_ref` is the defer verb's column. A dismissal that carried one
    would make the deferral listing report work nobody filed."""
    st = Store.open(tmp_path / "s.db")
    st.add_triage(dict(_dismissal(), tracking_ref="GH-9"))
    st.triage_reopen(dict(_reopening(), tracking_ref="GH-9"))
    assert [e["tracking_ref"] for e in _events(st)] == [None, None]


# --- the cross-review listing that keeps deferrals from rotting -------------

def test_open_deferrals_lists_only_findings_whose_last_event_is_a_defer(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())                                   # still deferred
    st.triage_defer(_deferral(ledger_key="b\0" + "s" * 40 + "\0k2", finding_key="k2",
                              tracking_ref="SKO-7", at="2026-07-27T12:00:00Z"))
    st.triage_reopen(_reopening(ledger_key="b\0" + "s" * 40 + "\0k2",
                                finding_key="k2", at="2026-07-27T13:00:00Z"))
    st.add_triage(_dismissal(ledger_key="b\0" + "s" * 40 + "\0k3",
                             finding_key="k3"))                    # not a deferral

    rows = st.open_deferrals()
    assert [r["finding_key"] for r in rows] == ["k1"]
    assert rows[0]["tracking_ref"] == TRACKING_REF
    assert rows[0]["branch"] == "b" and rows[0]["review_id"] == "r1"


def test_open_deferrals_spans_branches_and_bases_newest_first(tmp_path):
    """The whole point of the listing: a deferral filed on a branch nobody is
    looking at is exactly the one that rots."""
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())
    st.triage_defer(_deferral(ledger_key="other\0" + "z" * 40 + "\0k9",
                              finding_key="k9", branch="other", base_sha="z" * 40,
                              tracking_ref="https://example.invalid/i/3",
                              at="2026-07-27T09:00:00Z"))
    rows = st.open_deferrals()
    # NEWEST FIRST by `seq`, not by `at`: the second row's timestamp is EARLIER,
    # and the listing still puts it first because it was recorded later.
    assert [r["branch"] for r in rows] == ["other", "b"]
    assert st.open_deferrals(limit=1) == rows[:1]


def test_open_deferrals_agrees_with_triage_state_within_one_scope(tmp_path):
    """Two definitions of "still deferred" would be two answers. This one is the
    same last-event-by-seq rule, grouped by `ledger_key` instead of scoped."""
    st = Store.open(tmp_path / "s.db")
    st.triage_defer(_deferral())
    st.add_triage(_dismissal(ledger_key="b\0" + "s" * 40 + "\0k2", finding_key="k2"))
    st.triage_defer(_deferral(ledger_key="b\0" + "s" * 40 + "\0k2", finding_key="k2",
                              at="2026-07-27T12:00:00Z"))
    st.triage_defer(_deferral(ledger_key="b\0" + "s" * 40 + "\0k3", finding_key="k3",
                              at="2026-07-27T12:30:00Z"))
    st.triage_reopen(_reopening(ledger_key="b\0" + "s" * 40 + "\0k3",
                                finding_key="k3", at="2026-07-27T13:00:00Z"))

    from_state = {k for k, v in st.triage_state("b", "s" * 40).items()
                  if v["event"] == Store.EVENT_DEFER}
    assert {r["finding_key"] for r in st.open_deferrals()} == from_state == {"k1", "k2"}


def test_open_deferrals_is_empty_on_a_store_with_none(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.add_triage(_dismissal())
    assert st.open_deferrals() == []


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

#: v4 rebuilds `triage_events` (SQLite cannot widen a CHECK in place) to admit
#: the `defer` verb and to carry the deferral's filed reference. `tracking_ref`
#: is APPENDED, so every v3 column keeps its v3 position -- exactly what an
#: `ALTER TABLE ADD COLUMN` would have produced had the CHECK not forced a
#: rebuild. `V3_TRIAGE_EVENT_COLUMNS` above stays frozen at the pre-v4 shape,
#: for the same reason `PHASE1_SCHEMA` is frozen.
V4_TRIAGE_EVENT_COLUMNS = V3_TRIAGE_EVENT_COLUMNS + ["tracking_ref"]

#: The one object the v5 delta adds (its column is not an object). Named so the
#: "and nothing else was added" assertions stay EXACT sets rather than being
#: loosened to subsets -- a delta that quietly creates or drops something else
#: has to stay a red test.
V5_INDEX = ("index", "ix_reviews_repo_branch")
V6_TABLE = ("table", "capacity_admissions")
V6_INDEX = ("index", "ix_capacity_scope_status")
V6_OBJECTS = {V6_TABLE, V6_INDEX}
V7_TABLE = ("table", "feedback_events")
V7_INDEXES = {
    ("index", "ix_feedback_at"),
    ("index", "ix_feedback_kind"),
    ("index", "ix_feedback_review"),
}
V7_OBJECTS = {V7_TABLE} | V7_INDEXES
V8_TABLE = ("table", "api_spend_events")
V8_INDEX = ("index", "ix_api_spend_provider_day")
V8_OBJECTS = {V8_TABLE, V8_INDEX}
V9_INDEXES = {
    ("index", "ix_reviews_repo_id_started"),
    ("index", "ix_reviews_orchestration"),
}
V9_OBJECTS = V9_INDEXES
V10_OBJECTS = {
    ("table", "reuse_events"),
    ("index", "ix_reuse_events_at"),
    ("index", "ix_reuse_events_match"),
}
V13_OBJECTS = {
    ("table", "review_orchestrations"),
    ("table", "review_checkpoints"),
    ("index", "ix_orchestrations_resume"),
    ("index", "ix_orchestrations_expiry"),
    ("index", "ix_checkpoints_state"),
}
V14_OBJECTS = {
    ("table", "finding_lineage"),
    ("index", "ix_finding_lineage_lookup"),
}
V15_OBJECTS = {
    ("index", "ix_finding_lineage_repo_created_review"),
}
V16_OBJECTS = {
    ("table", "evidence_receipts"),
    ("table", "evidence_receipt_conflicts"),
    ("index", "ix_evidence_receipts_identity_time"),
    ("index", "ix_evidence_receipts_identity_nonce"),
    ("index", "ux_evidence_receipts_identity_nonce"),
    ("index", "ix_evidence_receipt_conflicts_identity_time"),
}

V17_OBJECTS = {
    ("table", "review_requests"),
    ("table", "request_links"),
    ("table", "request_executions"),
    ("index", "ix_review_requests_scope_time"),
    ("index", "ix_request_result_retention"),
    ("index", "ix_request_links_target"),
    ("index", "ix_request_execution_history"),
}

V18_OBJECTS = {
    ("table", "cancellation_audit"),
    ("index", "ix_cancel_target"),
    ("index", "ix_cancel_execution"),
}

V19_OBJECTS = {
    ("table", "request_budget_snapshots"),
    ("table", "request_capacity_layers"),
    ("index", "ix_request_capacity_execution"),
}

V20_OBJECTS = {
    ("table", "review_followup_checkpoints"),
    ("index", "ix_followup_checkpoints_state"),
}

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
    assert SCHEMA_VERSION >= 17
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert ("table", "provider_state") in _objects(db)
    assert V6_TABLE in _objects(db)
    assert V6_INDEX in _objects(db)
    assert V7_TABLE in _objects(db)
    assert V8_TABLE in _objects(db)
    assert V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS) <= _objects(db)
    assert ("table", "finding_lineage") in _objects(db)
    for table in V3_TABLES:
        assert ("table", table) in _objects(db)
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))


def test_reuse_events_are_append_only_and_return_stored_rows(tmp_path):
    with Store.open(tmp_path / "reuse.db") as st:
        first = st.append_reuse_event(
            at="2026-08-09T00:00:00Z", outcome="hit", reason="exact match",
            repo_id="/repo/.git", base_sha="b" * 40, diff_hash="d" * 40,
            context_hash="c" * 64, checklist_hash="k" * 64,
            tree_fingerprint="t" * 64, requested_reviewer=None,
            client_family=None, matched_review_id="r1")
        second = st.append_reuse_event(
            at="2026-08-09T00:00:01Z", outcome="miss", reason="tree changed",
            repo_id="/repo/.git", base_sha="b" * 40, diff_hash="d" * 40,
            context_hash=None, checklist_hash=None, tree_fingerprint="u" * 64,
            requested_reviewer=None, client_family=None, matched_review_id=None)
        rows = st.reuse_events()
    assert first["seq"] < second["seq"]
    assert [row["outcome"] for row in rows] == ["miss", "hit"]
    assert rows[1]["matched_review_id"] == "r1"


def test_reuse_event_rejects_unknown_outcome(tmp_path):
    with Store.open(tmp_path / "reuse.db") as st:
        with pytest.raises(ValueError, match="outcome must be one of"):
            st.append_reuse_event(
                at="2026-08-09T00:00:00Z", outcome="unexpected",
                reason="invalid event")


def test_migration_ladder_is_ordered_and_reaches_schema_version():
    """A delta added out of order, or a delta added without bumping
    SCHEMA_VERSION, would run on the wrong databases or never be stamped."""
    from skodun.store import _MIGRATIONS
    targets = [t for t, _ in _MIGRATIONS]
    assert targets == sorted(set(targets))
    assert targets[-1] == SCHEMA_VERSION
    assert all(t > 0 for t in targets)


def test_v12_store_gains_only_the_v13_checkpoint_objects(tmp_path):
    """A shipped v12 database receives additive checkpoint state only."""
    import unittest.mock as _mock

    from skodun import store as store_mod

    db = tmp_path / "v12.db"
    with _mock.patch.object(store_mod, "SCHEMA_VERSION", 12), \
            _mock.patch.object(
                store_mod, "_MIGRATIONS",
                tuple((target, delta) for target, delta
                      in store_mod._MIGRATIONS if target <= 12)):
        Store._open_for_migration_tests(db).close()
    assert _user_version(db) == 12
    before = _objects(db)
    assert not ((V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS)) & before)

    Store._open_for_migration_tests(db).close()

    assert _user_version(db) == SCHEMA_VERSION
    assert _objects(db) - before == (V13_OBJECTS | V14_OBJECTS | V15_OBJECTS |
                                     (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS))


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
    st = Store._open_for_migration_tests(db)
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert st.get_review("r1")["summary"] == "ok"     # rows preserved
    assert V6_TABLE in _objects(db)
    st.mark_provider_unavailable("openai", "quota", "quota",
                                 "2026-07-28T12:00:00Z")  # new table exists


def test_phase1_store_upgrade_preserves_every_table_index_and_row(tmp_path):
    """The live store holds thousands of imported reviews. Opening it with the
    new code must ADD the v2 and v3 objects and nothing else: every Phase 1
    table, every Phase 1 index and every row survives."""
    db = _phase1_db(tmp_path / "s.db", reviews=("r1", "r2", "r3"))
    before = _objects(db)
    assert ("table", "provider_state") not in before

    st = Store._open_for_migration_tests(db)

    after = _objects(db)
    assert before <= after, before - after            # nothing dropped
    assert after - before == {("table", "provider_state"), V5_INDEX} | {
        ("table", t) for t in V3_TABLES} | V6_OBJECTS | V7_OBJECTS | V8_OBJECTS | V9_OBJECTS | V10_OBJECTS | V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS)  # nothing else
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
        Store._open_for_migration_tests(db)
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
    msg = str(e.value)
    assert "v99" in msg
    assert f"v{SCHEMA_VERSION}" in msg or f"understands v{SCHEMA_VERSION}" in msg
    assert "restart MCP" in msg
    assert "do not fall back to the CLI" in msg


def test_a_store_one_version_above_this_build_is_refused_untouched(tmp_path):
    """The refusal is `> SCHEMA_VERSION`, not `> 2`: the interesting case is the
    NEXT version, not 99. A real v3 store stamped v4 by a newer skodun must come
    back byte-identical, exactly as the v0/v2 fixtures above do."""
    db = _v2_db(tmp_path / "s.db")
    Store._open_for_migration_tests(db).close()     # a real, fully migrated store
    assert _user_version(db) == SCHEMA_VERSION
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

    st = Store._open_for_migration_tests(db)

    assert _user_version(db) == SCHEMA_VERSION
    assert before <= _objects(db)                          # nothing dropped
    assert _objects(db) - before == (
        {("table", t) for t in V3_TABLES} | {V5_INDEX} | V6_OBJECTS | V7_OBJECTS | V8_OBJECTS | V9_OBJECTS | V10_OBJECTS | V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS))
    assert _columns(db, "triage_events") == V4_TRIAGE_EVENT_COLUMNS
    assert _columns(db, "dedup_events") == V3_DEDUP_EVENT_COLUMNS
    assert _columns(db, "deliveries") == V3_DELIVERY_COLUMNS
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))
    # ... and the Phase 1 rows are all still there.
    assert st.get_review("r1")["summary"] == "ok"


def test_the_v3_reviews_columns_are_nullable_and_default_null(tmp_path):
    """T8/T10 write them later; every existing row must read as "not set"
    rather than as a number the stale-recovery sweep would act on."""
    db = _v2_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db)
    row = st._c.execute("SELECT worst_runtime_sec, pid, superseded_by FROM reviews"
                        " WHERE id='r1'").fetchone()
    assert dict(row) == {"worst_runtime_sec": None, "pid": None, "superseded_by": None}
    st.save_review(REC)                     # and a NEW row is writable unchanged
    assert st.get_review("r1") is not None


@pytest.mark.parametrize("value,stored", [
    (1260, 1260), (None, None), (0, None), (-5, None), ("1260", None),
    (True, None), (12.5, None),
])
def test_worst_runtime_sec_is_indexed_from_the_same_dict_as_the_artifact(
        tmp_path, value, stored):
    """T8 writes this column: a batched review's record carries the budget its
    own shape implies, and `pipeline.recover_stale` reads it instead of
    recomputing from a config that may since have changed.

    Indexed value and artifact come from ONE dict, so they cannot disagree (the
    Phase 1 rule). Only a positive plain int is a budget: `isinstance(True, int)`
    is True in Python, and a numeric STRING in a column the sweep compares
    against an age would be a silent type surprise -- both land as NULL, i.e.
    "not set", which is exactly how every pre-Phase-3 row reads.
    """
    st = Store.open(tmp_path / "s.db")
    rec = dict(REC)
    if value is not None:
        rec["worst_runtime_sec"] = value
    st.save_review(rec)
    row = st._c.execute("SELECT worst_runtime_sec FROM reviews WHERE id=?",
                        (rec["id"],)).fetchone()
    assert row["worst_runtime_sec"] == stored
    # The artifact keeps whatever the caller wrote, verbatim: it is the record.
    assert st.get_review(rec["id"]).get("worst_runtime_sec") == value
    # An upsert of the same id keeps the two in step.
    st.save_review({**rec, "worst_runtime_sec": 99})
    row = st._c.execute("SELECT worst_runtime_sec FROM reviews WHERE id=?",
                        (rec["id"],)).fetchone()
    assert row["worst_runtime_sec"] == 99
    assert st.get_review(rec["id"])["worst_runtime_sec"] == 99


def test_the_v3_delta_seeds_one_dismiss_event_per_existing_triage_row(tmp_path):
    """The legacy `triage` table is single-row-per-ledger-key and becomes
    READ-ONLY at v3. Every dismissal a human already recorded has to arrive in
    the event stream, with every field it was recorded with -- a dismissal
    silently lost here reopens a finding that was litigated months ago."""
    second = dict(LEGACY_TRIAGE, ledger_key="b\0" + "s" * 40 + "\0k2",
                  finding_key="k2", severity="low", title="second",
                  dismissed_at="2026-07-27T11:00:00Z")
    db = _v2_db(tmp_path / "s.db", triage_rows=(LEGACY_TRIAGE, second))

    st = Store._open_for_migration_tests(db)

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
    st = Store._open_for_migration_tests(db)
    assert _events(st) == []
    assert st.triage_for("b", "s" * 40) == {}


def test_seeding_runs_once_and_a_reopen_survives_the_next_open(tmp_path):
    """The seeding is part of the 2 -> 3 delta, so it must not re-run on a
    store that is already v3: a second seeding would append a fresh `dismiss`
    event on top of a human's later `reopen` and silently re-dismiss it."""
    db = _v2_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db)
    st.triage_reopen(dict(LEGACY_TRIAGE, at="2026-07-28T09:00:00Z",
                          reason="the crash reproduces on main, reopening it"))
    st.close()

    st2 = Store._open_for_migration_tests(db)
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
        Store._open_for_migration_tests(db)

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
    st = Store._open_for_migration_tests(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert V3_REVIEW_COLUMNS <= set(_columns(db, "reviews"))
    assert [e["event"] for e in _events(st)] == ["dismiss"]


def test_a_crashed_v3_migration_never_leaves_the_store_open(tmp_path, monkeypatch):
    """`Store.open` closes the connection on any failure (shipped rule), and a
    half-applied migration must not be the one exception -- a leaked connection
    still holding the write lock would make the retry above fail too."""
    db = _v2_db(tmp_path / "s.db")
    _broken_v3_ladder(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        Store._open_for_migration_tests(db)
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
    second application of the same STATEMENTS really does raise.

    Driven against the raw statements rather than through `_apply_atomic`, which
    now (correctly) refuses to replay a delta at all -- it re-reads
    `user_version` under the write lock, so a concurrent opener that lost the race
    is a no-op instead of a `duplicate column name` failure. The premise the
    transaction rests on is unchanged, and this is where it is pinned.
    """
    from skodun.store import _MIGRATION_V3

    db = _v2_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db)
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        for sql in _MIGRATION_V3:
            st._c.execute(sql)
    st.close()


def test_apply_atomic_is_a_no_op_when_a_peer_already_applied_the_delta(tmp_path):
    """The migration race, at the statement that closes it.

    The caller's `user_version` read happens OUTSIDE any transaction, so two
    openers of the same store can both see the old version and both arrive at the
    same delta. The loser must find the version already stamped and do nothing --
    otherwise `Store.open` RAISES for it, and no store means a pre-push dispatch
    has nowhere to record its failure, so that push gets no record at all.
    """
    from skodun.store import _MIGRATION_V3, _apply_atomic

    db = _v2_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db) # migrates it all the way up
    assert _user_version(db) == SCHEMA_VERSION
    _apply_atomic(st._c, 3, _MIGRATION_V3)  # the loser's call: a no-op
    assert _user_version(db) == SCHEMA_VERSION
    assert [e["event"] for e in _events(st)] == ["dismiss"], (
        "the no-op re-seeded the triage stream")
    st.close()


def test_wal_mode_survives_a_peer_holding_the_lock(tmp_path):
    """`PRAGMA journal_mode=WAL` is the one statement SQLite does not route
    through the busy handler: it returns `SQLITE_BUSY` immediately, whatever the
    connection's `timeout`. Refusing to open the store over that would be the
    dispatcher's worst failure -- nowhere to record anything.

    A wrapper rather than a monkeypatch: `sqlite3.Connection.execute` is
    read-only.
    """
    import sqlite3 as _sqlite3

    from skodun.store import _enable_wal

    db = tmp_path / "s.db"
    conn = _sqlite3.connect(db, isolation_level=None)
    calls = []

    class _Busy:
        """A connection whose WAL pragma always reports the lock as held."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "journal_mode=WAL" in sql:
                calls.append(sql)
                raise _sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *a, **kw)

    try:
        # It gives up rather than refusing to open: the mode is a concurrency
        # property, not a correctness one -- every writer uses explicit
        # transactions, so a rollback-journal store is slower and nothing else.
        assert _enable_wal(_Busy(conn), attempts=2).lower() != "wal"
        assert len(calls) == 2, "the pragma was not retried"
    finally:
        conn.close()


def test_wal_mode_stops_retrying_once_a_peer_has_converted_the_database(tmp_path):
    """The other half: the loser must NOTICE the conversion rather than burning
    its whole retry budget on a pragma that will keep failing."""
    import sqlite3 as _sqlite3

    from skodun.store import _enable_wal

    db = tmp_path / "s.db"
    conn = _sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")     # a "peer" already did it
    calls = []

    class _Busy:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "journal_mode=WAL" in sql:
                calls.append(sql)
                raise _sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *a, **kw)

    try:
        assert _enable_wal(_Busy(conn), attempts=20).lower() == "wal"
        assert len(calls) == 1, "it kept retrying a conversion already done"
    finally:
        conn.close()


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
    # The last rung is what a fresh store is stamped with. v8 is the
    # v12 records the quota-pool persistence boundary; v11 is transactional
    # because it adds the exact-reuse security column.
    assert _MIGRATIONS[-1][0] == SCHEMA_VERSION
    assert isinstance(_MIGRATIONS[-1][1], tuple)
    assert any(isinstance(d, tuple) for _, d in _MIGRATIONS)


# --- v4: widening the event vocabulary, which is a TABLE REBUILD ------------
#
# SQLite cannot alter a CHECK constraint in place, and v3 spelled the two-verb
# vocabulary into one: `event TEXT CHECK(event IN ('dismiss','reopen'))`. So v4
# is the first delta that DROPS a shipped table -- it copies every row into a
# replacement, drops the original and renames. Two properties make that safe and
# both are pinned below: it runs inside ONE transaction (a half-applied rebuild
# is a store with no triage history at all), and it preserves `seq` VALUES, not
# merely row order, because `seq` is the total order every effective-state read
# in this project depends on.


def _v3_db(path, *, triage_rows=(LEGACY_TRIAGE,)):
    """A real v3-shaped store: the v2 fixture, migrated by the v3 delta ALONE.

    Built by running the ladder with `SCHEMA_VERSION` pinned at 3, rather than
    from a frozen copy of the v3 DDL, because the v3 delta is itself frozen and
    tested above -- what this fixture has to be is exactly what the shipped v3
    build left on a user's disk, which is what the v4 delta must upgrade.
    """
    _v2_db(path, triage_rows=triage_rows)
    with _pinned_at_v3():
        Store._open_for_migration_tests(path).close()
    assert _user_version(path) == 3
    assert _columns(path, "triage_events") == V3_TRIAGE_EVENT_COLUMNS
    return path


def test_a_v3_store_gains_the_widened_vocabulary_and_the_reference_column(tmp_path):
    db = _v3_db(tmp_path / "s.db")
    before = _objects(db)

    st = Store._open_for_migration_tests(db)

    assert _user_version(db) == SCHEMA_VERSION
    # A v3 store climbs v4–v12 in one open: v5 index + capacity, feedback,
    # spend, and telemetry indexes.
    assert _objects(db) == before | {V5_INDEX} | V6_OBJECTS | V7_OBJECTS | V8_OBJECTS | V9_OBJECTS | V10_OBJECTS | V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS), (
        "the rebuild added or dropped an object")
    assert _columns(db, "triage_events") == V4_TRIAGE_EVENT_COLUMNS
    # The seeded legacy dismissal came through the rebuild intact...
    assert [e["event"] for e in _events(st)] == ["dismiss"]
    assert set(st.triage_for("b", "s" * 40)) == {"k1"}
    # ... and the third verb is now writable, which it was not before.
    st.triage_defer(_deferral())
    assert [e["event"] for e in _events(st)] == ["dismiss", "defer"]
    st.close()


def test_the_rebuild_preserves_every_row_and_its_seq_value(tmp_path):
    """`seq` VALUES, not just order. Every effective-state read in this project
    is "the last event by `seq`", and `triage_history` hands those numbers to
    whoever audits the ledger -- a rebuild that renumbered them would silently
    rewrite the order of decisions that are already recorded."""
    db = _v3_db(tmp_path / "s.db")
    # Written with RAW v3-shaped INSERTs, not through this build's `add_triage`:
    # that writer now binds `tracking_ref` and would not fit the v3 table at
    # all. The point of the fixture is rows a shipped v3 build really left.
    raw = sqlite3.connect(db, isolation_level=None)
    try:
        for event, at in (("dismiss", "2026-07-27T10:00:00Z"),
                          ("reopen", "2026-07-27T12:00:00Z")):
            raw.execute(
                """INSERT INTO triage_events (ledger_key, finding_key, event,
                     review_id, branch, base_sha, file, line, severity, title,
                     reason, at)
                   VALUES (?, 'k2', ?, 'r1', 'b', ?, 'a.py', 7, 'high', 'boom',
                     'a reason long enough to clear the audit floor here', ?)""",
                ("b\0" + "s" * 40 + "\0k2", event, "s" * 40, at))
    finally:
        raw.close()
    before = _events(db)
    assert [e["seq"] for e in before] == [1, 2, 3]
    assert [e["event"] for e in before] == ["dismiss", "dismiss", "reopen"]

    st = Store._open_for_migration_tests(db)                # the v4 rebuild

    after = _events(st)
    assert after == [dict(e, tracking_ref=None) for e in before]
    assert [e["seq"] for e in after] == [1, 2, 3]
    # ... and the NEXT event continues the same sequence rather than restarting.
    st.triage_defer(_deferral(ledger_key="b\0" + "s" * 40 + "\0k4",
                              finding_key="k4"))
    assert _events(st)[-1]["seq"] == 4
    st.close()


def _pinned_at_v3():
    """A context manager that makes this build behave as the shipped v3 one."""
    import contextlib
    import unittest.mock as _mock

    from skodun import store as store_mod

    stack = contextlib.ExitStack()
    stack.enter_context(_mock.patch.object(store_mod, "SCHEMA_VERSION", 3))
    stack.enter_context(_mock.patch.object(
        store_mod, "_MIGRATIONS",
        tuple((t, d) for t, d in store_mod._MIGRATIONS if t <= 3)))
    return stack


def test_a_v0_and_a_v2_store_both_climb_the_whole_ladder_to_v5(tmp_path):
    """The ladder is only load-bearing if every rung runs for a store that
    starts below it. A v0 store must arrive with the v2 table, the v3 objects,
    the v4 vocabulary AND the v5 column, in one open."""
    v0 = _phase1_db(tmp_path / "v0.db")
    v2 = _v2_db(tmp_path / "v2.db")
    for db in (v0, v2):
        st = Store._open_for_migration_tests(db)
        assert _user_version(db) == SCHEMA_VERSION, db
        assert ("table", "provider_state") in _objects(db), db
        assert V3_TABLES <= {name for _, name in _objects(db)}, db
        assert _columns(db, "triage_events") == V4_TRIAGE_EVENT_COLUMNS, db
        assert "repo" in _columns(db, "reviews"), db
        assert [e["event"] for e in _events(st)] == ["dismiss"], db
        st.triage_defer(_deferral())                       # the new verb works
        assert set(st.triage_for("b", "s" * 40)) == {"k1"}, db
        st.close()


def test_a_v4_store_is_left_alone_by_a_second_open(tmp_path):
    db = _v2_db(tmp_path / "s.db")
    Store._open_for_migration_tests(db).close()
    before = _events(db)
    st = Store._open_for_migration_tests(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert _events(db) == before, "the rebuild ran again on an already-v4 store"
    assert _columns(db, "triage_events") == V4_TRIAGE_EVENT_COLUMNS
    st.close()


def _broken_v4_ladder(monkeypatch):
    """The real v4 delta with one failing statement injected AFTER the DROP.

    THE crash that matters: the replacement table is populated and the original
    `triage_events` is already gone, so a delta that was not transactional would
    leave a store with no triage history and no version stamp to say so.
    """
    from skodun import store as store_mod

    real = list(store_mod._MIGRATION_V4)
    drop = max(i for i, s in enumerate(real)
               if s.lstrip().upper().startswith("DROP TABLE"))
    broken = tuple(real[:drop + 1]
                   + ["INSERT INTO no_such_table_boom (x) VALUES (1)"]
                   + real[drop + 1:])
    monkeypatch.setattr(store_mod, "_MIGRATIONS",
                        tuple((t, broken if t == 4 else d)
                              for t, d in store_mod._MIGRATIONS))


def test_a_crash_mid_v4_delta_leaves_a_clean_v3_store_that_migrates_on_retry(
        tmp_path, monkeypatch):
    """THE reason the v4 delta is transactional, mirroring the v3 drill above.

    A half-applied rebuild is strictly worse than a half-applied `ALTER`: the
    original table has been DROPPED, so the store would come back with its whole
    triage ledger missing and every dismissal a human ever recorded gone.
    """
    db = _v3_db(tmp_path / "s.db")
    _broken_v4_ladder(monkeypatch)

    with pytest.raises(sqlite3.OperationalError):
        Store._open_for_migration_tests(db)

    # NOTHING from the delta survived: the v3 table is still there, with its
    # v3 columns, its rows and its version.
    assert _user_version(db) == 3
    assert _columns(db, "triage_events") == V3_TRIAGE_EVENT_COLUMNS
    assert [e["event"] for e in _events(db)] == ["dismiss"]
    assert ("table", "triage_events_v4") not in _objects(db)

    monkeypatch.undo()                                # the crash is over; retry
    st = Store._open_for_migration_tests(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert _columns(db, "triage_events") == V4_TRIAGE_EVENT_COLUMNS
    assert [e["event"] for e in _events(st)] == ["dismiss"]
    assert _events(st)[0]["seq"] == 1


def test_a_crashed_v4_migration_never_leaves_the_store_open(tmp_path, monkeypatch):
    """A leaked connection still holding the write lock would make the retry
    above fail too -- and this delta holds the lock across a DROP."""
    db = _v3_db(tmp_path / "s.db")
    _broken_v4_ladder(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        Store._open_for_migration_tests(db)
    monkeypatch.undo()
    other = sqlite3.connect(db, isolation_level=None, timeout=0.5)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_replaying_the_v4_delta_silently_drops_every_tracking_reference(tmp_path):
    """Pins the premise of the transaction rather than restating it in prose,
    and the premise is WORSE than v3's.

    v3 replayed raises `duplicate column name` -- loud, and the store is bricked
    but nothing is lost. v4 replayed does not raise at ALL: the replacement name
    is free again after the rename, so the whole rebuild runs a second time and
    quietly copies the v3 column list, dropping every `tracking_ref` value on
    the way. Silent loss of exactly the field that makes a deferral honest.

    Which is why the version is re-read under the write lock in `_apply_atomic`:
    a second opener that lost the migration race must be a NO-OP, and the test
    below is the one that says so.
    """
    from skodun.store import _MIGRATION_V4

    db = _v3_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db)
    st.triage_defer(_deferral())
    assert _events(st)[-1]["tracking_ref"] == TRACKING_REF

    for sql in _MIGRATION_V4:               # the replay, statement by statement
        st._c.execute(sql)

    assert [e["event"] for e in _events(st)] == ["dismiss", "defer"]
    assert _events(st)[-1]["tracking_ref"] is None, (
        "the replay is meant to demonstrate the loss this delta must never "
        "suffer twice")
    st.close()


def test_apply_atomic_is_a_no_op_for_v4_when_a_peer_already_applied_it(tmp_path):
    """The migration race, at the statement that closes it, for the rebuild.

    The caller's `user_version` read happens OUTSIDE any transaction, so two
    openers can both see v3 and both arrive here. The loser must find v4 already
    stamped and do nothing -- otherwise it rebuilds the table a second time and,
    per the test above, silently discards every filed reference in the ledger.
    """
    from skodun.store import _MIGRATION_V4, _apply_atomic

    db = _v3_db(tmp_path / "s.db")
    st = Store._open_for_migration_tests(db)
    st.triage_defer(_deferral())
    assert _user_version(db) == SCHEMA_VERSION

    _apply_atomic(st._c, 4, _MIGRATION_V4)          # the loser's call: a no-op

    assert _user_version(db) == SCHEMA_VERSION
    assert _events(st)[-1]["tracking_ref"] == TRACKING_REF, (
        "the no-op rebuilt the table and lost the reference")
    st.close()


def test_a_store_stamped_v6_is_still_refused_untouched(tmp_path):
    """The future-version refusal survives the rebuild: a real current-version
    store stamped one higher by a newer skodun comes back byte-identical.

    The stamp tracks `SCHEMA_VERSION + 1` rather than any literal: this pinned
    v5 until v5 became this build, and the rule it is about has always been
    `> SCHEMA_VERSION` (see `test_a_future_version_store_is_refused`).
    """
    db = _v2_db(tmp_path / "s.db")
    Store._open_for_migration_tests(db).close()
    assert _user_version(db) == SCHEMA_VERSION
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1:d}")
    raw.commit()
    raw.close()
    before = db.read_bytes()

    with pytest.raises(ValueError, match="newer"):
        Store.open(db)

    assert db.read_bytes() == before
    assert _user_version(db) == SCHEMA_VERSION + 1


# --- v5: repository scoping -------------------------------------------------
#
# `reviews` was keyed by branch alone, so two repositories sharing one store
# collided on any common branch name. v5 adds a `repo` column and an index that
# leads with it. The delta is additive and BACKFILLS NOTHING: a pre-v5 row keeps
# `repo IS NULL` forever, which `repo = ?` excludes from every scoped query --
# fail-closed, because an invisible old row is strictly better than the wrong
# repository's worker being killed.


def _pinned_at_v4():
    """A context manager that makes this build behave as the shipped v4 one."""
    import contextlib
    import unittest.mock as _mock

    from skodun import store as store_mod

    stack = contextlib.ExitStack()
    stack.enter_context(_mock.patch.object(store_mod, "SCHEMA_VERSION", 4))
    stack.enter_context(_mock.patch.object(
        store_mod, "_MIGRATIONS",
        tuple((t, d) for t, d in store_mod._MIGRATIONS if t <= 4)))
    return stack


def _pinned_at_v5():
    """A context manager that makes this build behave as the shipped v5 one."""
    import contextlib
    import unittest.mock as _mock

    from skodun import store as store_mod

    stack = contextlib.ExitStack()
    stack.enter_context(_mock.patch.object(store_mod, "SCHEMA_VERSION", 5))
    stack.enter_context(_mock.patch.object(
        store_mod, "_MIGRATIONS",
        tuple((t, d) for t, d in store_mod._MIGRATIONS if t <= 5)))
    return stack


def _v4_db(path, *, triage_rows=(LEGACY_TRIAGE,)):
    """A real v4-shaped store: the v3 fixture, migrated by the v4 delta ALONE.

    What a shipped v4 build left on a user's disk, which is what the v5 delta
    must upgrade. Carries `_v2_db`'s `r1` review row, which is the pre-v5 row
    the NULL rule is about.
    """
    _v3_db(path, triage_rows=triage_rows)
    with _pinned_at_v4():
        Store._open_for_migration_tests(path).close()
    assert _user_version(path) == 4
    assert "repo" not in _columns(path, "reviews")
    return path


def test_a_v4_store_gains_the_repo_column_and_its_index(tmp_path):
    """v5 is additive: the column arrives NULL on every existing row and the
    shipped index is kept, not replaced."""
    db = _v4_db(tmp_path / "s.db")
    before = _objects(db)

    st = Store._open_for_migration_tests(db)

    assert _user_version(db) == SCHEMA_VERSION
    assert "repo" in _columns(db, "reviews")
    row = st._c.execute("SELECT repo FROM reviews WHERE id='r1'").fetchone()
    assert row["repo"] is None, "a pre-v5 row must not be backfilled"
    idx = {name for kind, name in _objects(db) if kind == "index"}
    assert "ix_reviews_repo_branch" in idx
    assert "ix_reviews_branch" in idx, "the shipped index is kept, not dropped"
    assert before < _objects(db), "the delta added nothing"
    assert V6_TABLE in _objects(db)
    assert V6_INDEX in _objects(db)
    st.close()


def test_a_v5_store_gains_capacity_admissions(tmp_path):
    """v6 is additive: capacity_admissions + index; no review column change."""
    db = _v4_db(tmp_path / "v5climb.db")
    with _pinned_at_v5():
        Store._open_for_migration_tests(db).close()
    assert _user_version(db) == 5
    before = _objects(db)
    assert V6_TABLE not in before

    st = Store._open_for_migration_tests(db)

    assert _user_version(db) == SCHEMA_VERSION
    assert _objects(db) - before == V6_OBJECTS | V7_OBJECTS | V8_OBJECTS | V9_OBJECTS | V10_OBJECTS | V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS)
    assert "repo" in _columns(db, "reviews")
    st.close()


def test_a_v6_store_gains_feedback_events(tmp_path):
    """v7 is additive: feedback_events + indexes; no triage/capacity rewrite."""
    db = _v4_db(tmp_path / "v6climb.db")
    # Climb to v6 only by opening with a temporary pin is awkward; apply ladder
    # then verify v7 objects appear from a pre-v7 shape: open after forcing
    # user_version 6 without feedback tables is not how the ladder works.
    # Instead: open to current, which includes v7 from empty climb of v5.
    with _pinned_at_v5():
        Store._open_for_migration_tests(db).close()
    # Manually apply only v6 DDL and stamp 6 so feedback is missing.
    import sqlite3
    raw = sqlite3.connect(db)
    from skodun.store import _MIGRATION_V6
    raw.executescript(_MIGRATION_V6)
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()
    assert _user_version(db) == 6
    before = _objects(db)
    assert V7_TABLE not in before

    st = Store._open_for_migration_tests(db)

    assert _user_version(db) == SCHEMA_VERSION
    assert _objects(db) - before == V7_OBJECTS | V8_OBJECTS | V9_OBJECTS | V10_OBJECTS | V13_OBJECTS | V14_OBJECTS | V15_OBJECTS | (V16_OBJECTS | V17_OBJECTS | V18_OBJECTS | V19_OBJECTS | V20_OBJECTS)
    st.close()


def _broken_v5_ladder(monkeypatch):
    """The real v5 delta with one failing statement injected AFTER the ALTER.

    The crash that matters: the column has been added inside the transaction and
    the version has NOT been stamped yet. `max()` raising here would mean the
    delta no longer contains an `ALTER TABLE` at all, which is itself the thing
    that makes the transaction mandatory -- so the lookup is deliberately not
    defensive.
    """
    from skodun import store as store_mod

    real = list(store_mod._MIGRATION_V5)
    last_alter = max(i for i, s in enumerate(real)
                     if s.lstrip().upper().startswith("ALTER TABLE"))
    broken = tuple(real[:last_alter + 1]
                   + ["INSERT INTO no_such_table_boom (x) VALUES (1)"]
                   + real[last_alter + 1:])
    monkeypatch.setattr(store_mod, "_MIGRATIONS",
                        tuple((t, broken if t == 5 else d)
                              for t, d in store_mod._MIGRATIONS))


def test_a_crash_mid_v5_delta_leaves_a_clean_v4_store_that_migrates_on_retry(
        tmp_path, monkeypatch):
    """THE reason the v5 delta is transactional, mirroring the v3 drill.

    `ALTER TABLE ADD COLUMN` is not replay-idempotent: a store that added the
    column and then crashed before the stamp comes back at v4 and replays the
    `ALTER` into `duplicate column name` on every subsequent open -- bricked,
    with every review ever recorded inside it.
    """
    db = _v4_db(tmp_path / "s.db")
    _broken_v5_ladder(monkeypatch)

    with pytest.raises(sqlite3.OperationalError):
        Store._open_for_migration_tests(db)

    # NOTHING from the delta survived: not the column, not the index, not the
    # stamp -- and the v4 store is otherwise exactly as it was.
    assert _user_version(db) == 4
    assert "repo" not in _columns(db, "reviews")
    assert V5_INDEX not in _objects(db)
    assert [e["event"] for e in _events(db)] == ["dismiss"]

    monkeypatch.undo()                                # the crash is over; retry
    st = Store._open_for_migration_tests(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert "repo" in _columns(db, "reviews")
    assert V5_INDEX in _objects(db)
    st.close()


def test_a_crashed_v5_migration_never_leaves_the_store_open(tmp_path, monkeypatch):
    """A leaked connection still holding the write lock would make the retry
    above fail too."""
    db = _v4_db(tmp_path / "s.db")
    _broken_v5_ladder(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        Store._open_for_migration_tests(db)
    monkeypatch.undo()
    other = sqlite3.connect(db, isolation_level=None, timeout=0.5)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()


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


def test_provider_state_is_per_quota_pool(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("google", "claude quota", "quota",
                                 "2026-07-28T12:00:00Z",
                                 quota_pool="google:claude-gpt")
    assert st.provider_unavailable_reason(
        "google", "2026-07-28T11:00:00Z", env={},
        quota_pool="google:claude-gpt") == "claude quota"
    assert st.provider_unavailable_reason(
        "google", "2026-07-28T11:00:00Z", env={},
        quota_pool="google:gemini") is None
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert rows[0]["provider"] == "google"
    assert rows[0]["quota_pool"] == "google:claude-gpt"


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
#:
#: HAND-MAINTAINED, and therefore drifted: six modules that open a `Store`
#: were simply never added, and nothing said so. `test_the_sweep_lists_every_
#: store_touching_module` below is what closes that permanently -- a module can
#: now be left out only by being named in `_SWEEP_EXCLUDED` with a reason.
#:
#: `test_batched_review.py` is here but matches no `Store.open` grep: it opens
#: one through `test_pipeline._store`. The guard below is one-directional for
#: exactly that reason -- it requires every grep hit to be accounted for, never
#: that every listed module be a grep hit.
_STORE_TOUCHING_MODULES = (
    "tests/test_followup_checkpoints.py",
    "tests/test_store.py",
    "tests/test_checkpoints.py",
    "tests/test_requests.py",
    "tests/test_budget_store.py",
    "tests/test_budget_execution.py",
    "tests/test_continuation.py",
    "tests/test_scoped_control.py",
    "tests/test_review_results.py",
    "tests/test_transport_eligibility.py",
    "tests/test_cli.py",
    "tests/test_gate.py",
    "tests/test_fallback.py",
    "tests/test_pipeline.py",
    "tests/test_batched_review.py",
    "tests/test_legacy_import.py",
    "tests/test_shadow.py",
    "tests/test_dispatch.py",
    "tests/test_refuter.py",
    "tests/test_triage.py",
    "tests/test_delivery.py",
    "tests/test_chain.py",
    "tests/test_mcpserver.py",
    "tests/test_seams.py",
    "tests/test_services.py",
    "tests/test_repo_scoping.py",
    "tests/test_roundctx.py",
    "tests/test_doctor.py",
    "tests/test_retention.py",
    "tests/test_schedule.py",
    "tests/test_capacity.py",
    "tests/test_s1_status_cancel.py",
    "tests/test_feedback.py",
    "tests/test_routing.py",
    "tests/test_openai_api.py",
    "tests/test_review_exit_matrix.py",
    "tests/test_provenance.py",
    "tests/test_stats.py",
    "tests/test_evidence.py",
    "tests/test_mutation.py",
    "tests/test_reuse.py",
    "tests/test_readiness.py",
    "tests/test_schema_lifecycle.py",
    "tests/test_fingerprint.py",
    "tests/test_queueview.py",
    "tests/test_plan_preview.py",
)

#: Store-touching modules deliberately kept OUT of the subprocess sweep, with
#: the reason recorded beside each. This is the only sanctioned way to leave one
#: out: a module that is neither here nor in `_STORE_TOUCHING_MODULES` fails
#: `test_the_sweep_lists_every_store_touching_module`.
#:
#: Both exclusions are about RUNTIME and nothing else. The sweep re-runs whole
#: modules in a subprocess, so its cost is the sum of theirs, and it is already
#: the single most expensive test in the suite. These two are the two that drive
#: real subprocess reviews under real sleeps, and neither buys much here: what
#: this sweep can actually catch is an EXPLICIT `ResourceWarning` (a GC-emitted
#: one is unraisable -- see the test's own docstring), which is a property of
#: the store code both of them share with the sixteen modules above.
_SWEEP_EXCLUDED = (
    ("tests/test_mcptools.py",
     "drives real end-to-end MCP reviews in subprocesses with real waits; the "
     "slowest module in the suite, and it exercises no store path the sixteen "
     "swept modules do not"),
    ("tests/test_cancellation.py",
     "spawns real child processes and sleeps through SIGTERM/SIGKILL grace "
     "windows; its cost is wall-clock waiting, not store work"),
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


def test_the_sweep_lists_every_store_touching_module():
    """The cheap half, and the one that makes the expensive half trustworthy.

    `_STORE_TOUCHING_MODULES` is hand-maintained, and it had silently fallen six
    modules behind: `test_cancellation`, `test_chain`, `test_mcpserver`,
    `test_mcptools`, `test_seams` and `test_services` all open a `Store` and
    none of them was swept. Nothing failed, because a list that is merely
    INCOMPLETE still passes every assertion the sweep makes about the modules it
    does name -- which is the whole failure mode of a hand-maintained inventory.

    So: grep, and require every module that opens a `Store` to be either swept
    or explicitly excluded WITH A REASON. Milliseconds, no subprocess, and it is
    what turns "someone remembered" into a property of the suite. Runtime is a
    legitimate reason to exclude one -- that is what `_SWEEP_EXCLUDED` is for --
    but it now has to be written down rather than achieved by omission.
    """
    tests_dir = Path(__file__).resolve().parent
    excluded = dict(_SWEEP_EXCLUDED)
    accounted = set(_STORE_TOUCHING_MODULES) | set(excluded)

    missing = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if "Store.open" not in path.read_text(encoding="utf-8"):
            continue
        name = f"tests/{path.name}"
        if name not in accounted:
            missing.append(name)
    assert missing == [], (
        f"{len(missing)} module(s) open a Store but are neither in "
        f"_STORE_TOUCHING_MODULES nor in _SWEEP_EXCLUDED: {missing}. Add them "
        f"to the sweep, or exclude them with the reason recorded.")

    # An exclusion has to say something, and has to name a module that exists --
    # a stale entry would silently re-open the hole it was written to document.
    for name, reason in _SWEEP_EXCLUDED:
        assert (tests_dir.parent / name).is_file(), f"stale exclusion: {name}"
        assert len(reason) > 30, f"{name}'s exclusion reason says nothing"
        assert name not in _STORE_TOUCHING_MODULES, (
            f"{name} is both swept and excluded")
    # ...and every swept module has to exist too, or the subprocess below fails
    # with a pytest usage error rather than an assertion anyone can read.
    for name in _STORE_TOUCHING_MODULES:
        assert (tests_dir.parent / name).is_file(), f"no such module: {name}"


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
        # Generous, and it has to be: this subset drives real child processes
        # (fake CLIs under a watchdog) and grows with every module added above,
        # so a tight cap turns "the suite got bigger" into a spurious failure.
        # It is a net against a HUNG subprocess, not a performance budget.
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=1200)
    assert proc.returncode == 0, (
        f"store-touching subset failed under -W error::ResourceWarning "
        f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


# ===========================================================================
# Phase 3 Task 10: the reservation lease, conditional finalize, atomic failure
# ===========================================================================
#
# `set_status` is GONE, and its two tests above went with it. It wrote a status
# and nothing else, so every caller of it left a row whose status said `failed`
# beside `trustworthy=1` -- a row the gate still honours and dedup still
# suppresses against. The replacements below are the two shapes that actually
# exist: an atomic FAILURE transition that demotes the trust axes with it
# (`mark_failed`, and its conditional sibling `fail_if_running`), and the
# reservation transaction's own supersede.

#: The reserved record's exact initial shape, as `reserve_prepush` writes it.
RESERVED_KEYS = {
    "id", "reviewed_at", "branch", "head", "base_ref", "base_sha", "diff_hash",
    "mode", "source", "status", "parse_ok", "degraded", "diff_truncated",
    "findings", "findings_total", "summary", "failure_reason", "usable_output",
    "worst_runtime_sec", "pid", "superseded_by", "repo",
    "review_started_at", "repo_id", "request_id",
    # Computed at the chokepoint from the three axes, never caller-supplied.
    "trustworthy",
}

PREPUSH = dict(REC, mode="prepush", source="skodun", usable_output=True)


def _evidence(enabled=True, valid=True, candidate=None):
    from skodun.dispatch import DedupEvidence
    return DedupEvidence(enabled=enabled, valid=valid,
                         candidate_context_hash=candidate)


def _reserve(st, **kw):
    args = dict(branch="b", head="h" * 40, base_ref="origin/main",
                base_sha="s" * 40, diff_hash="d" * 40, worst_runtime_sec=1234,
                evidence=_evidence(enabled=False), repo="/repos/a")
    args.update(kw)
    return st.reserve_prepush(**args)


def _dedup_events(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM dedup_events")]
    finally:
        conn.close()


# --- the reserved record's shape -------------------------------------------


def test_reserve_prepush_writes_the_documented_running_shape(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)

    res = _reserve(st)

    assert res.suppressed_by is None
    assert res.superseded == ()
    rec = st.get_review(res.record_id)
    assert set(rec) == RESERVED_KEYS, set(rec) ^ RESERVED_KEYS
    assert rec["id"] == res.record_id
    assert rec["request_id"] == res.record_id
    assert (rec["branch"], rec["head"], rec["base_ref"], rec["base_sha"],
            rec["diff_hash"]) == ("b", "h" * 40, "origin/main", "s" * 40, "d" * 40)
    assert rec["mode"] == "prepush" and rec["source"] == "skodun"
    assert rec["status"] == "running"
    # STRICT bools, every one of them: a trustworthy computation over ints would
    # be a different function, and the artifact would be malformed under the
    # strict-bool trust rules every reader applies.
    for field in ("parse_ok", "degraded", "diff_truncated", "usable_output"):
        assert rec[field] is False, field
        assert type(rec[field]) is bool, field
    assert rec["findings"] == [] and rec["findings_total"] == 0
    assert rec["summary"] == "" and rec["failure_reason"] is None
    assert rec["worst_runtime_sec"] == 1234
    assert rec["pid"] is None and rec["superseded_by"] is None
    assert rec["repo"] == "/repos/a"
    row = _raw_row(db, res.record_id)
    assert row["repo"] == "/repos/a", "the INDEXED column, not just the JSON"
    assert row["status"] == "running" and row["trustworthy"] == 0
    assert row["worst_runtime_sec"] == 1234
    assert row["pid"] is None and row["superseded_by"] is None
    assert row["mode"] == "prepush" and row["source"] == "skodun"


def test_a_reserved_record_is_never_a_dedup_candidate_or_a_gate_pass(tmp_path):
    st = Store.open(tmp_path / "s.db")
    res = _reserve(st)
    assert st.latest_trustworthy_for("d" * 40) is None
    from skodun.triage import load_valid_artifact
    assert load_valid_artifact(st.get_review(res.record_id)) is not None


def test_reserve_prepush_mints_a_fresh_id_per_call(tmp_path):
    st = Store.open(tmp_path / "s.db")
    first = _reserve(st).record_id
    second = _reserve(st).record_id
    assert first != second
    assert first.startswith("sk_") and second.startswith("sk_")


def test_reserve_prepush_refuses_to_reserve_without_a_repository(tmp_path):
    """`repo` is REQUIRED, with no default. A default would have to mean "match
    everything", which is exactly the silent wrong answer this scoping removes:
    it would reserve a row no scoped reader can see and no scoped supersede can
    retire, and it would let a caller forget the question entirely."""
    with Store.open(tmp_path / "s.db") as st:
        with pytest.raises(TypeError):
            st.reserve_prepush("b", "h" * 40, "origin/main", "s" * 40,
                               "d" * 40, 1, _evidence(enabled=False))


def test_the_reservation_time_is_the_stores_canonical_timestamp(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = st.get_review(_reserve(st).record_id)
    from skodun.store import _is_canonical_ts
    assert _is_canonical_ts(rec["reviewed_at"]), rec["reviewed_at"]


# --- supersede is reservation-owned, and RETURNED ---------------------------


def test_reserving_retires_every_running_prepush_row_of_the_branch(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    first = _reserve(st, diff_hash="1" * 40).record_id
    st.attach_pid(first, 4242)
    # A SECOND concurrent running prepush row on the same branch, written
    # directly: a second reservation would have retired the first one itself
    # (which `test_two_serialized_reservations_leave_exactly_one_running_row`
    # pins), and this test is about retiring MORE THAN ONE row in one lease --
    # the state a store left behind by a killed dispatcher can hold.
    second = "hand-written-running"
    st.save_review({**PREPUSH, "id": second, "branch": "b", "status": "running",
                    "parse_ok": False, "usable_output": False,
                    "diff_hash": "2" * 40, "pid": None,
                    # The SAME repository as `_reserve`'s, or the scoped
                    # supersede would skip it and this test would pass for the
                    # wrong reason -- proving only that a foreign row is spared.
                    "repo": "/repos/a"})

    third = _reserve(st, diff_hash="3" * 40)

    assert third.record_id not in (first, second)
    # RETURNED by the transaction, never re-queried: a post-hoc query races.
    assert sorted(r["id"] for r in third.superseded) == sorted([first, second])
    assert {r["id"]: r["pid"] for r in third.superseded} == {first: 4242, second: None}
    for retired in (first, second):
        assert _raw_row(db, retired)["status"] == "superseded"
        assert _raw_row(db, retired)["superseded_by"] == third.record_id
        art = st.get_review(retired)
        assert art["status"] == "superseded"
        # Written to the ARTIFACT in the same statement -- Task 12 renders it
        # from there, and an index row that disagrees with its artifact is the
        # one thing this store exists to make impossible.
        assert art["superseded_by"] == third.record_id


def test_supersede_never_touches_another_branch_or_a_foreground_run(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    other = _reserve(st, branch="other").record_id
    # `repo` matches `_reserve`'s, so `mode` is the only thing sparing this row.
    st.save_review({**REC, "id": "fg", "branch": "b", "mode": "now",
                    "status": "running", "parse_ok": False,
                    "repo": "/repos/a"})

    res = _reserve(st, branch="b")

    assert [r["id"] for r in res.superseded] == []
    assert _raw_row(db, other)["status"] == "running"
    assert _raw_row(db, "fg")["status"] == "running"
    assert _raw_row(db, "fg")["superseded_by"] is None


def test_supersede_does_not_retire_another_repositorys_running_review(tmp_path):
    """The exact defect: two repositories, one store, the same branch name.
    Reserving in A must not touch B's running row, and must not return it for
    signalling -- returning it is what SIGTERMed an unrelated worker.

    BOTH repositories have a running row, and that is deliberate: with only B's
    row present the scoped SELECT returns nothing, `retired` is empty and the
    UPDATE is skipped entirely (`store.py:858`), so an UNSCOPED UPDATE would
    still leave B alone and the mutation that drops `repo=?` from it would
    survive. A's own row is what makes the UPDATE actually run.
    """
    with Store.open(tmp_path / "s.db") as st:
        in_b = _reserve(st, branch="main", repo="/repos/b")
        in_a = _reserve(st, branch="main", repo="/repos/a")

        newer = _reserve(st, branch="main", repo="/repos/a")

        assert newer.record_id is not None
        assert [r["id"] for r in newer.superseded] == [in_a.record_id], (
            "the supersede must return A's own row and nothing else")
        assert st.get_review(in_b.record_id)["status"] == "running"
        assert st.get_review(in_b.record_id)["superseded_by"] is None
        assert st.get_review(in_a.record_id)["status"] == "superseded"


def test_supersede_leaves_terminal_rows_of_the_same_branch_alone(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    # `repo` matches `_reserve`'s, so `status` is the only thing sparing it.
    st.save_review({**PREPUSH, "id": "done", "branch": "b", "status": "clean",
                    "repo": "/repos/a"})

    res = _reserve(st, branch="b")

    assert [r["id"] for r in res.superseded] == []
    assert _raw_row(db, "done")["status"] == "clean"
    assert _raw_row(db, "done")["trustworthy"] == 1


def test_two_serialized_reservations_leave_exactly_one_running_row(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    a = _reserve(st, diff_hash="1" * 40).record_id
    b = _reserve(st, diff_hash="2" * 40).record_id
    running = [r["id"] for r in _all_rows(db) if r["status"] == "running"]
    assert running == [b]
    assert _raw_row(db, a)["superseded_by"] == b


def _all_rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM reviews")]
    finally:
        conn.close()


# --- the authoritative dedup decision, inside the lease --------------------


def _candidate(st, **kw):
    """A trustworthy TERMINAL record of `d*40` that a lease may suppress against.

    `context_hash` is REMOVED unless a test supplies one: the shipped `""` is the
    AMBIGUOUS state and never suppresses, so a candidate carrying it would make
    every suppression assertion below vacuous. An ABSENT key is the legacy state,
    which suppresses on the diff hash alone.
    """
    rec = {**PREPUSH, "id": "cand", "status": "clean", "base_sha": "s" * 40,
           "diff_hash": "d" * 40}
    rec.pop("context_hash")
    rec.update(kw)
    st.save_review(rec)
    return rec


def test_a_matching_trustworthy_terminal_record_suppresses_with_an_audit_row(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)          # no context_hash key: the legacy state

    res = _reserve(st, evidence=_evidence())

    assert res.record_id is None
    assert res.suppressed_by == "cand"
    assert res.superseded == ()
    assert [r["id"] for r in _all_rows(db)] == ["cand"]     # nothing reserved
    events = _dedup_events(db)
    assert len(events) == 1
    assert events[0]["branch"] == "b"
    assert events[0]["diff_hash"] == "d" * 40
    assert events[0]["matched_review_id"] == "cand"
    from skodun.store import _is_canonical_ts
    assert _is_canonical_ts(events[0]["at"])


def test_a_suppression_can_never_commit_without_its_audit_row(tmp_path, monkeypatch):
    """The audit INSERT is inside the same transaction as the decision.

    Fault-injected at the audit step: the whole transaction must roll back, so
    there is neither a suppression nor a reservation nor an event.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)
    real = st._c.execute

    def boom(sql, *a, **kw):
        if "dedup_events" in sql:
            raise sqlite3.OperationalError("disk full at the audit insert")
        return real(sql, *a, **kw)

    monkeypatch.setattr(st, "_c", _Proxy(st._c, boom))
    with pytest.raises(sqlite3.OperationalError):
        _reserve(st, evidence=_evidence())

    st2 = Store.open(db)
    assert _dedup_events(db) == []
    assert [r["id"] for r in _all_rows(db)] == ["cand"]
    # ...and the store is usable afterwards: the transaction was rolled back,
    # not left open holding the write lock.
    assert _reserve(st2).record_id is not None


def test_the_audit_row_is_inserted_INSIDE_the_lease_not_after_it(tmp_path,
                                                                 monkeypatch):
    """The named mutation is "move the audit insert after COMMIT".

    The fault-injection test above cannot see that mutation, and the reason is
    worth stating: a suppression writes NOTHING durable except its audit row, so
    "the suppression rolled back" and "the audit row was never written" are the
    same observation. What DOES distinguish them is whether the write lock is
    still held when the row is inserted -- `in_transaction` answers exactly that,
    and it is False the moment a `COMMIT` has run.

    If the row is inserted after the commit, then a crash in between (or a disk
    error on that one statement) leaves a review skipped with no trace of why: the
    push looks reviewed and the audit stream disagrees.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)
    real = st._c.execute
    seen = []

    def watch(sql, *a, **kw):
        if "dedup_events" in sql:
            seen.append(st._c.in_transaction)
        return real(sql, *a, **kw)

    monkeypatch.setattr(st, "_c", _Proxy(st._c, watch))
    assert _reserve(st, evidence=_evidence()).suppressed_by == "cand"
    assert seen == [True], (
        "the audit row was inserted outside the reservation lease")
    assert len(_dedup_events(db)) == 1


class _Proxy:
    """A connection stand-in whose `execute` is replaced. Everything else passes."""

    def __init__(self, conn, execute):
        self._conn = conn
        self.execute = execute

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_disabled_dedup_never_suppresses_and_never_audits(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)

    res = _reserve(st, evidence=_evidence(enabled=False))

    assert res.record_id is not None and res.suppressed_by is None
    assert _dedup_events(db) == []


def test_invalid_evidence_never_suppresses_even_a_legacy_candidate(tmp_path):
    """THE mutation-killer for dropping the `evidence.valid` check in the lease.

    A legacy-context candidate needs no hash comparison at all, so a lease that
    asked only the CONTEXT rule (`context_permits_suppression`) instead of the
    evidence gate would suppress here -- certifying a push whose context nobody
    could establish.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)

    res = _reserve(st, evidence=_evidence(valid=False))

    assert res.suppressed_by is None and res.record_id is not None
    assert _dedup_events(db) == []


def test_a_same_diff_different_base_is_never_suppressed(tmp_path):
    """The gate's own mandatory rebase check, inside the lease.

    A rebased branch pushes the same patch against a different base. The gate
    would answer 2 for it (its `base_sha` no longer matches), so dedup must not
    skip the review that would produce the record the gate needs.
    """
    st = Store.open(tmp_path / "s.db")
    _candidate(st, base_sha="OTHER" + "s" * 35)

    res = _reserve(st, evidence=_evidence(), base_sha="s" * 40)

    assert res.suppressed_by is None and res.record_id is not None


def test_a_running_record_of_the_same_diff_never_suppresses(tmp_path):
    """TERMINAL, not merely trustworthy: an in-flight review certifies nothing."""
    st = Store.open(tmp_path / "s.db")
    # A running row cannot be trustworthy through `save_review` (its axes are
    # recomputed), so this is written with clean axes AND a running status --
    # the shape only a hand-edited store could hold, which is exactly what the
    # terminal filter is for.
    st.save_review({**PREPUSH, "id": "live", "status": "running"})
    assert _raw_row(tmp_path / "s.db", "live")["trustworthy"] == 1

    res = _reserve(st, evidence=_evidence())

    assert res.suppressed_by is None and res.record_id is not None


@pytest.mark.parametrize("axis, value", [("parse_ok", False), ("degraded", True),
                                         ("diff_truncated", True)])
def test_an_untrustworthy_candidate_never_suppresses(tmp_path, axis, value):
    st = Store.open(tmp_path / "s.db")
    _candidate(st, **{axis: value, "status": "failed"})
    res = _reserve(st, evidence=_evidence())
    assert res.suppressed_by is None
    assert res.record_id is not None


@pytest.mark.parametrize("mutation, why", [
    ({"findings_total": 3}, "findings_total disagrees with findings"),
    ({"findings": [1]}, "a non-object finding"),
    ({"branch": 7}, "branch is not a string"),
    ({"id": None}, "id is missing"),
])
def test_a_malformed_artifact_never_suppresses_what_the_gate_would_reject(
        tmp_path, mutation, why):
    """`load_valid_artifact`, in full, inside the lease.

    THE mutation-killer for skipping it: each artifact below has CLEAN trust
    axes and a `trustworthy=1` index row, so nothing about the axes would stop
    a suppression -- and every one of them is an artifact the gate itself
    refuses. Suppressing against one would leave the push with no record any
    gate accepts.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)
    _hand_edit_artifact(db, "cand", mutation)

    res = _reserve(st, evidence=_evidence())

    assert res.suppressed_by is None, why
    assert res.record_id is not None


@pytest.mark.parametrize("mutation", [
    {"parse_ok": 1}, {"degraded": 0}, {"diff_truncated": "false"},
    {"trustworthy": 1}, {"trustworthy": False},
])
def test_a_candidate_whose_artifact_axes_are_not_strict_bools_never_suppresses(
        tmp_path, mutation):
    """The axes are RECOMPUTED from the artifact, strictly.

    `is_trustworthy(1, 0, "")` is True by truthiness, and the index column says
    1 -- so an artifact carrying ints would suppress under any coercing read.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)
    _hand_edit_artifact(db, "cand", mutation)

    assert _reserve(st, evidence=_evidence()).suppressed_by is None


@pytest.mark.parametrize("mutation", [{"id": "somebody-else"},
                                      {"diff_hash": "z" * 40}])
def test_an_index_row_disagreeing_with_its_artifact_never_suppresses(tmp_path,
                                                                    mutation):
    db = tmp_path / "s.db"
    st = Store.open(db)
    _candidate(st)
    _hand_edit_artifact(db, "cand", mutation)

    assert _reserve(st, evidence=_evidence()).suppressed_by is None


def test_the_context_rules_are_applied_with_the_evidence_hash(tmp_path):
    st = Store.open(tmp_path / "s.db")
    _candidate(st, context_hash="c" * 64)

    assert _reserve(st, evidence=_evidence(candidate="c" * 64)).suppressed_by == "cand"
    assert _reserve(st, evidence=_evidence(candidate="x" * 64)).suppressed_by is None
    assert _reserve(st, evidence=_evidence(candidate=None)).suppressed_by is None


def test_an_empty_string_context_hash_never_suppresses(tmp_path):
    """The `""`-never-suppresses rule, reaching all the way into the lease."""
    st = Store.open(tmp_path / "s.db")
    _candidate(st, context_hash="")
    for candidate in ("c" * 64, None):
        assert _reserve(st, evidence=_evidence(candidate=candidate)).suppressed_by is None


def test_the_newest_matching_candidate_is_the_one_considered(tmp_path):
    st = Store.open(tmp_path / "s.db")
    _candidate(st, id="old", reviewed_at="2026-07-27T09:00:00Z",
               context_hash="c" * 64)
    _candidate(st, id="new", reviewed_at="2026-07-27T12:00:00Z",
               context_hash="n" * 64)

    assert _reserve(st, evidence=_evidence(candidate="n" * 64)).suppressed_by == "new"
    # The OLDER row's hash does not get a second chance: exactly one candidate
    # is considered, the newest terminal one.
    assert _reserve(st, evidence=_evidence(candidate="c" * 64)).suppressed_by is None


def test_a_suppression_never_supersedes_an_in_flight_review(tmp_path):
    """A skip must not TERM a live worker reviewing DIFFERENT content.

    Oracle-parity in structure: the dedup decision is made before the supersede,
    so a suppressed push leaves the branch's running row exactly as it was.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    live = _reserve(st, diff_hash="1" * 40).record_id
    _candidate(st, diff_hash="2" * 40)

    res = _reserve(st, diff_hash="2" * 40, evidence=_evidence())

    assert res.suppressed_by == "cand"
    assert res.superseded == ()
    assert _raw_row(db, live)["status"] == "running"


def _hand_edit_artifact(path, review_id, mutation):
    """Rewrite one row's artifact_json only -- the index row keeps its values."""
    conn = sqlite3.connect(path)
    try:
        raw = conn.execute("SELECT artifact_json FROM reviews WHERE id=?",
                           (review_id,)).fetchone()[0]
        art = json.loads(raw)
        for k, v in mutation.items():
            if v is None:
                art.pop(k, None)
            else:
                art[k] = v
        conn.execute("UPDATE reviews SET artifact_json=? WHERE id=?",
                     (json.dumps(art), review_id))
        conn.commit()
    finally:
        conn.close()


# --- attach_pid ------------------------------------------------------------


def test_attach_pid_writes_the_column_and_the_artifact(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id

    assert st.attach_pid(rid, 31337) is True

    assert _raw_row(db, rid)["pid"] == 31337
    assert st.get_review(rid)["pid"] == 31337


def test_attach_pid_refuses_a_record_that_is_no_longer_running(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    first = _reserve(st, diff_hash="1" * 40).record_id
    _reserve(st, diff_hash="2" * 40)              # supersedes `first`

    assert st.attach_pid(first, 999) is False

    assert _raw_row(db, first)["pid"] is None
    assert st.get_review(first)["pid"] is None


def test_attach_pid_refuses_a_record_that_already_has_one(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    assert st.attach_pid(rid, 11) is True
    assert st.attach_pid(rid, 22) is False
    assert st.get_review(rid)["pid"] == 11


@pytest.mark.parametrize("pid", [0, -1, True, 1.5, "12", None])
def test_attach_pid_refuses_anything_that_is_not_a_real_pid(tmp_path, pid):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    with pytest.raises(ValueError):
        st.attach_pid(rid, pid)
    assert st.get_review(rid)["pid"] is None


# --- conditional finalization ---------------------------------------------


def _final(rec, **kw):
    """A completed record built from a reserved one: clean, parsed, terminal."""
    out = dict(rec)
    out.update(status="clean", parse_ok=True, degraded=False, diff_truncated=False,
               summary="ok", findings=[], findings_total=0, usable_output=True,
               severity={"high": 0, "medium": 0, "low": 0})
    out.update(kw)
    return out


def test_finalize_review_applies_the_record_and_reports_true(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)

    assert st.finalize_review(rid, _final(reserved, summary="done")) is True

    rec = st.get_review(rid)
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert rec["summary"] == "done"
    assert _raw_row(db, rid)["status"] == "clean"
    assert _raw_row(db, rid)["trustworthy"] == 1
    assert st.latest_trustworthy_for("d" * 40)["id"] == rid
    # Reservation-owned fields survive the finalize untouched.
    assert rec["worst_runtime_sec"] == 1234
    assert _raw_row(db, rid)["worst_runtime_sec"] == 1234


def test_finalize_review_refuses_a_superseded_record_and_changes_nothing(tmp_path):
    """THE mutation-killer for making `finalize_review` unconditional."""
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st, diff_hash="1" * 40).record_id
    reserved = st.get_review(rid)
    newer = _reserve(st, diff_hash="2" * 40).record_id

    assert st.finalize_review(rid, _final(reserved, summary="too late")) is False

    rec = st.get_review(rid)
    assert rec["status"] == "superseded"
    assert rec["superseded_by"] == newer
    assert rec["summary"] == "" and rec["trustworthy"] is False
    assert _raw_row(db, rid)["trustworthy"] == 0
    assert st.latest_trustworthy_for("1" * 40) is None


def test_finalize_review_refuses_a_stale_recovered_record(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    assert st.fail_if_running(rid, "stale recovery: worker exceeded its budget") is True

    assert st.finalize_review(rid, _final(reserved)) is False

    rec = st.get_review(rid)
    assert rec["status"] == "failed" and rec["trustworthy"] is False


def test_finalize_review_refuses_a_record_that_does_not_exist(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.finalize_review("nope", _final({**PREPUSH, "id": "nope"})) is False
    assert st.get_review("nope") is None


def test_finalize_review_recomputes_trust_and_overwrites_a_lie(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)

    assert st.finalize_review(rid, _final(reserved, degraded=True,
                                          trustworthy=True)) is True

    assert st.get_review(rid)["trustworthy"] is False


@pytest.mark.parametrize("axis", ["parse_ok", "degraded", "diff_truncated"])
def test_finalize_review_refuses_a_non_bool_axis(tmp_path, axis):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    with pytest.raises(ValueError):
        st.finalize_review(rid, _final(reserved, **{axis: "false"}))
    assert st.get_review(rid)["status"] == "running"


@pytest.mark.parametrize("field", ["branch", "head", "base_ref", "base_sha",
                                   "diff_hash"])
def test_finalize_review_refuses_an_identity_that_moved(tmp_path, field):
    """Reservation-owned identity is DATABASE-owned at finalization.

    A worker that recomputed one of these and got a different answer has
    reviewed something else; overwriting the row silently would publish a
    record about content nobody asked for, at the reservation's own id.
    """
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    with pytest.raises(ValueError, match=field):
        st.finalize_review(rid, _final(reserved, **{field: "moved"}))
    assert st.get_review(rid)["status"] == "running"


def test_finalize_review_refuses_a_record_whose_id_is_not_the_one_asked_for(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    with pytest.raises(ValueError, match="id"):
        st.finalize_review(rid, _final(reserved, id="somebody-else"))
    assert st.get_review(rid)["status"] == "running"


def test_finalize_review_merges_the_database_owned_pid_and_supersede(tmp_path):
    """`pid` and `superseded_by` are the DATABASE's, not the worker's.

    The worker's dict predates the pid attach, so a finalize that wrote the
    worker's value back would erase it -- and `recover_stale`'s liveness check
    reads exactly that column.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    st.attach_pid(rid, 5150)

    assert st.finalize_review(rid, _final(reserved)) is True

    assert _raw_row(db, rid)["pid"] == 5150
    assert st.get_review(rid)["pid"] == 5150


def test_a_finalized_record_keeps_the_reservations_repo(tmp_path):
    """`finalize_review` binds every column from the WORKER's dict and merges
    only `pid`/`superseded_by` back, so a worker record with no `repo` would
    write NULL over the reservation's value -- on the only rounds `surface`
    ever delivers."""
    with Store.open(tmp_path / "s.db") as st:
        res = _reserve(st, branch="main", repo="/repos/a")
        rec = dict(st.get_review(res.record_id), status="clean", parse_ok=True,
                   usable_output=True, summary="ok", findings=[],
                   findings_total=0)

        assert st.finalize_review(res.record_id, rec) is True

        assert st.get_review(res.record_id)["repo"] == "/repos/a"
        row = _raw_row(tmp_path / "s.db", res.record_id)
        assert row["repo"] == "/repos/a", "the INDEXED column, not just the JSON"


def test_a_pid_attach_between_reread_and_update_is_not_erased(tmp_path):
    """The barrier the explicit `BEGIN IMMEDIATE` exists for.

    The shipped store runs in autocommit. Without one transaction around the
    identity read, the merge and the UPDATE, a pid attach committing in that
    window would be overwritten by the stale merge -- and the record would then
    look like a worker that never started.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    attacher = Store.open(db)
    fired = []

    real = st._c.execute

    def hook(sql, *a, **kw):
        if sql.lstrip().upper().startswith("UPDATE REVIEWS") and not fired:
            fired.append(True)
            # A SECOND connection attaching a pid right now. Under one
            # `BEGIN IMMEDIATE` it cannot get the write lock, so it fails --
            # which is the serialization working. Without the transaction it
            # would succeed and then be clobbered.
            try:
                attacher.attach_pid(rid, 6060)
            except sqlite3.OperationalError:
                fired.append("locked")
        return real(sql, *a, **kw)

    monkey = _Proxy(st._c, hook)
    st._c, saved = monkey, st._c
    try:
        assert st.finalize_review(rid, _final(reserved)) is True
    finally:
        st._c = saved
    assert fired and fired[-1] == "locked", (
        "the finalize did not hold the write lock across its read and update")
    attacher.close()


def test_finalize_review_requires_usable_output_on_a_skodun_prepush_record(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    bare = {k: v for k, v in _final(reserved).items() if k != "usable_output"}
    with pytest.raises(ValueError, match="usable_output"):
        st.finalize_review(rid, bare)
    assert st.get_review(rid)["status"] == "running"


# --- the `usable_output` production contract, at the chokepoint ------------


def test_usable_output_is_required_on_a_skodun_prepush_record(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError, match="usable_output"):
        st.save_review({k: v for k, v in PREPUSH.items() if k != "usable_output"})
    assert st.get_review("r1") is None


@pytest.mark.parametrize("value", [1, 0, "true", "", None, []])
def test_usable_output_must_be_an_exact_bool_wherever_it_appears(tmp_path, value):
    """Not just on the prepush records: a present field must be a real bool.

    `usable_output: 1` reads as True under truthiness and as "not a bool" under
    the strict rules every other axis is read by; a surface that mixed the two
    would print "NO REVIEW HAPPENED" over a round that answered.
    """
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError, match="usable_output"):
        st.save_review({**REC, "usable_output": value})
    with pytest.raises(ValueError, match="usable_output"):
        st.save_review({**PREPUSH, "usable_output": value})
    assert st.get_review("r1") is None


def test_a_legacy_imported_prepush_row_may_omit_usable_output(tmp_path):
    """THE post-v3 legacy-import regression.

    A legacy archive row carries `mode="prepush"` with `source="legacy"` and no
    `usable_output` at all, so a MODE-ONLY requirement would reject the shipped
    import outright.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "id": "legacy1", "mode": "prepush", "source": "legacy"})
    assert st.get_review("legacy1")["mode"] == "prepush"
    assert "usable_output" not in st.get_review("legacy1")


def test_a_foreground_now_record_may_omit_usable_output(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                       # mode="now", source="skodun"
    assert st.get_review("r1")["trustworthy"] is True


def test_usable_output_is_never_derived_from_the_finding_count(tmp_path):
    """THE mutation-killer for deriving it.

    A prepush round whose passes all answered "nothing wrong" has zero findings
    and `usable_output=True`; one that failed before any answer has zero
    findings and `usable_output=False`. A derivation from `findings_total`
    cannot tell them apart, and the difference is "NO REVIEW HAPPENED" versus a
    clean review.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review({**PREPUSH, "id": "answered", "findings": [],
                    "findings_total": 0, "usable_output": True})
    st.save_review({**PREPUSH, "id": "silent", "findings": [], "findings_total": 0,
                    "usable_output": False, "parse_ok": False, "status": "failed"})
    assert st.get_review("answered")["usable_output"] is True
    assert st.get_review("silent")["usable_output"] is False


# --- mark_failed: the atomic failure transition ---------------------------


def test_mark_cancelled_demotes_the_DEGRADED_axis_with_strict_bools(tmp_path):
    """The worker's POST-COMMIT linearization check.

    A SIGTERM can land while SQLite holds the write lock for `finalize_review`, so
    the worker's pre-check cannot see it -- and without this the killed review is
    committed TRUSTWORTHY. `degraded`, not `parse_ok`: the reviewer's output really
    did parse; what is untrue is that the round finished.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.save_review({**REC, "findings": [_a_finding()], "findings_total": 1,
                    "usable_output": True})
    assert _raw_row(db)["trustworthy"] == 1

    assert st.mark_cancelled("r1", "cancelled during finalization") is True

    row = _raw_row(db)
    assert row["status"] == "failed" and row["degraded"] == 1
    assert row["trustworthy"] == 0
    rec = st.get_review("r1")
    assert rec["degraded_reason"] == "cancelled during finalization"
    assert rec["failure_reason"] == "cancelled during finalization"
    # THE STRICT-JSON-BOOLEAN HAZARD, same as `mark_failed`: a bound Python bool
    # lands in `json_set` as the NUMBER 1/0, which reloads as `int` and makes the
    # artifact malformed under the strict-bool trust rules.
    assert rec["degraded"] is True and type(rec["degraded"]) is bool
    assert rec["trustworthy"] is False and type(rec["trustworthy"]) is bool
    assert st.latest_trustworthy_for(REC["diff_hash"]) is None


def test_mark_cancelled_preserves_the_findings_the_round_did_produce(tmp_path):
    """A round cancelled after two batches answered really did produce those
    findings, and a surface that dropped them would print "NO REVIEW HAPPENED"
    over real evidence."""
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "findings": [_a_finding()], "findings_total": 1,
                    "usable_output": True, "summary": "found one"})
    st.mark_cancelled("r1", "cancelled")
    rec = st.get_review("r1")
    assert rec["findings_total"] == 1 and len(rec["findings"]) == 1
    assert rec["usable_output"] is True and rec["summary"] == "found one"
    assert rec["parse_ok"] is True, "parse_ok is a fact about the OUTPUT"


def test_mark_cancelled_is_self_limiting_on_an_untrustworthy_record(tmp_path):
    """Guarded on `trustworthy=1`, so a record some other transition already
    settled keeps its own answer -- a superseded row stays superseded."""
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "status": "superseded", "parse_ok": False})
    assert st.mark_cancelled("r1", "cancelled") is False
    assert st.get_review("r1")["status"] == "superseded"


def test_mark_cancelled_on_a_missing_record_reports_false(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.mark_cancelled("nope", "cancelled") is False



def test_mark_failed_demotes_status_and_the_trust_axes_together(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    st.save_review(REC)                      # clean, trustworthy=1
    assert _raw_row(db)["trustworthy"] == 1

    assert st.mark_failed("r1", "the review could not be read back") is True

    row = _raw_row(db)
    assert row["status"] == "failed"
    assert row["parse_ok"] == 0 and row["trustworthy"] == 0
    rec = st.get_review("r1")
    assert rec["status"] == "failed"
    assert rec["failure_reason"] == "the review could not be read back"
    # THE STRICT-JSON-BOOLEAN HAZARD: a bound Python `False` lands in `json_set`
    # as the NUMBER 0, which reloads as `int` and makes the artifact malformed
    # under the strict-bool trust rules -- so a `failed` row would still look
    # trustworthy to every strict reader.
    for field in ("parse_ok", "trustworthy"):
        assert rec[field] is False, field
        assert type(rec[field]) is bool, field
    # ...and the demotion is visible to the two readers that matter.
    assert st.latest_trustworthy_for("d" * 40) is None


def test_mark_failed_needs_no_running_guard(tmp_path):
    """The one call site is the foreground cleanup, and it must not need one.

    `_persist` autocommits the final save BEFORE its readback, so a readback
    failure has to demote a record that is already `clean` -- a `running` guard
    would leave exactly the stale-recovery bug one call site over.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "status": "clean"})
    assert st.mark_failed("r1", "readback failed") is True
    assert st.get_review("r1")["status"] == "failed"
    assert st.get_review("r1")["trustworthy"] is False


def test_mark_failed_on_a_missing_record_reports_false_and_writes_nothing(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.mark_failed("nope", "reason") is False
    assert st.get_review("nope") is None


def test_mark_failed_leaves_the_rest_of_the_artifact_intact(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "summary": "keep me"})
    st.mark_failed("r1", "reason")
    assert st.get_review("r1")["summary"] == "keep me"
    assert st.get_review("r1")["id"] == "r1"


# --- fail_if_running: stale recovery's conditional terminal transition -----


def test_fail_if_running_demotes_a_running_record_with_strict_bools(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id

    reason = "stale recovery: worker exceeded its runtime budget"
    assert st.fail_if_running(rid, reason) is True

    row = _raw_row(db, rid)
    assert row["status"] == "failed"
    assert row["parse_ok"] == 0 and row["trustworthy"] == 0
    rec = st.get_review(rid)
    assert rec["failure_reason"] == reason
    for field in ("parse_ok", "trustworthy"):
        assert rec[field] is False and type(rec[field]) is bool, field


def test_fail_if_running_loses_to_a_worker_that_already_finalized(tmp_path):
    """Whichever terminal transition commits FIRST survives.

    A worker finalizing clean between the stale scan and this update must win:
    the record it wrote is a real review, and overwriting it would replace a
    trustworthy answer with a janitor's guess.
    """
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st).record_id
    reserved = st.get_review(rid)
    assert st.finalize_review(rid, _final(reserved)) is True

    assert st.fail_if_running(rid, "stale recovery") is False

    assert st.get_review(rid)["status"] == "clean"
    assert st.get_review(rid)["trustworthy"] is True
    assert _raw_row(db, rid)["trustworthy"] == 1


def test_fail_if_running_loses_to_a_dispatcher_that_already_superseded(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db)
    rid = _reserve(st, diff_hash="1" * 40).record_id
    newer = _reserve(st, diff_hash="2" * 40).record_id

    assert st.fail_if_running(rid, "stale recovery") is False

    assert st.get_review(rid)["status"] == "superseded"
    assert st.get_review(rid)["superseded_by"] == newer


def test_fail_if_running_on_a_missing_record_reports_false(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st.fail_if_running("nope", "stale recovery") is False


# --- log_dir --------------------------------------------------------------


def test_log_dir_is_the_db_path_plus_logs_and_is_created_lazily(tmp_path):
    db = tmp_path / "nested" / "s.db"
    st = Store.open(db)
    expected = tmp_path / "nested" / "s.db.logs"
    assert not expected.exists()

    got = st.log_dir()

    assert got == expected
    assert got.is_dir()
    assert st.log_dir() == expected          # idempotent


def test_set_status_is_gone(tmp_path):
    """The unsafe status-only API does not survive as a deprecated shell.

    It wrote a status and nothing else, so every one of its callers could leave
    `status='failed'` beside `trustworthy=1` -- a row the gate honours and dedup
    suppresses against. `mark_failed`/`fail_if_running` replace it.
    """
    st = Store.open(tmp_path / "s.db")
    assert not hasattr(st, "set_status")


# --- routing telemetry (S5 Phase A read-back) -------------------------------


def _routing_review(st, rid, *, at, adapter, reason=None, routed=None):
    """One persisted review with (or without) a routing audit."""
    rec = {
        "id": rid, "reviewed_at": at, "source": "skodun", "branch": "feat",
        "head": "a" * 40, "base_ref": "main", "base_sha": "b" * 40,
        "diff_hash": rid, "mode": "now", "model": "m", "adapter": adapter,
        "status": "clean", "parse_ok": True, "degraded": False,
        "diff_truncated": False, "trustworthy": True, "stop_reason": None,
        "findings": [], "findings_total": 0, "summary": "",
    }
    if reason is not None:
        rec["route_reason"] = reason
        rec["routed_reviewer"] = routed
    st.save_review(rec)


def test_routing_counts_groups_by_adapter_reason_and_head(tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "r1", at="2026-08-02T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "r2", at="2026-08-02T01:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "r3", at="2026-08-02T02:00:00Z", adapter="codex",
                        reason="pinned", routed="finder-codex")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert {(r["adapter"], r["route_reason"], r["routed_reviewer"], r["n"])
            for r in rows} == {
        ("grok", "auto:free", "finder-grok", 2),
        ("codex", "pinned", "finder-codex", 1),
    }


def test_routing_counts_excludes_reviews_before_the_window(tmp_path):
    """The boundary is inclusive: a review AT the cutoff is in the window."""
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "old", at="2026-07-30T23:59:59Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "edge", at="2026-08-01T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert [(r["adapter"], r["n"]) for r in rows] == [("grok", 1)]


def test_routing_counts_reports_unrouted_records_as_their_own_group(tmp_path):
    """Pre-S5 records and background pre-push reviews have no route audit.

    Both consumed a provider slot, so they belong in the total; neither was a
    routing decision, so neither may be counted as one.
    """
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "legacy", at="2026-08-02T00:00:00Z", adapter="grok")
        _routing_review(st, "routed", at="2026-08-02T01:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    by_reason = {r["route_reason"]: r["n"] for r in rows}
    assert by_reason == {None: 1, "auto:free": 1}


def test_routing_counts_ignores_the_imported_legacy_archive(tmp_path):
    """`import-legacy` rows never touched a skodun provider slot.

    Not a tidiness rule: on a real store the legacy archive outnumbers skodun's
    own reviews five to one, so counting it puts a four-figure denominator
    under a three-figure numerator and reports a provider carrying 28% of the
    load as carrying 5% -- the exact number this query exists to get right.
    """
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "mine", at="2026-08-02T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        st.save_review({
            "id": "imported", "reviewed_at": "2026-08-02T01:00:00Z",
            "source": "legacy", "branch": "feat", "head": "c" * 40,
            "base_ref": "main", "base_sha": "b" * 40, "diff_hash": "imported",
            "mode": "now", "model": "m", "adapter": None, "status": "clean",
            "parse_ok": True, "degraded": False, "diff_truncated": False,
            "trustworthy": True, "stop_reason": None, "findings": [],
            "findings_total": 0, "summary": "",
        })
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert [(r["adapter"], r["n"]) for r in rows] == [("grok", 1)]


def test_routing_counts_ignores_rows_no_provider_can_own(tmp_path):
    """skodun's OWN rows can have no adapter: a `reserve_prepush` row exists
    before the worker runs, and a superseded or fail-if-running row can
    terminate without one.

    They are real reviews-in-progress, but nothing attributes them to a
    provider -- so counting them would put the sum of the printed per-provider
    numerators below their shared denominator on every listing that had one.
    """
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "served", at="2026-08-02T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        st.save_review({
            "id": "reserved", "reviewed_at": "2026-08-02T01:00:00Z",
            "source": "skodun", "branch": "feat", "head": "c" * 40,
            "base_ref": "main", "base_sha": "b" * 40, "diff_hash": "reserved",
            "mode": "now", "model": "m", "adapter": None, "status": "clean",
            "parse_ok": True, "degraded": False, "diff_truncated": False,
            "trustworthy": True, "stop_reason": None, "findings": [],
            "findings_total": 0, "summary": "",
        })
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert [(r["adapter"], r["n"]) for r in rows] == [("grok", 1)]
    assert sum(r["n"] for r in rows) == 1, "the denominator counted a row no line owns"


def test_routing_counts_on_an_empty_store_is_an_empty_list(tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        assert st.routing_counts(since_iso="2026-08-01T00:00:00Z") == []


def test_routing_counts_refuses_a_timestamp_it_cannot_order_by(tmp_path):
    """String comparison is only correct for the canonical fixed-width shape."""
    with Store.open(tmp_path / "s.db") as st:
        with pytest.raises(ValueError, match="since_iso"):
            st.routing_counts(since_iso="last tuesday")
"""Store lifecycle and persistence invariants, including warning-free closure."""
