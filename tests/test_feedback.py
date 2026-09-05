"""Non-gate feedback ledger: agent judgment + product bugs (store v7)."""

from __future__ import annotations

import pytest

from skodun import feedback
from skodun.cli import main
from skodun.store import SCHEMA_VERSION, Store


def test_schema_has_feedback_events(tmp_path):
    st = Store.open(tmp_path / "s.db")
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    names = {
        r[0]
        for r in st._c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "feedback_events" in names
    st.close()


def test_record_product_bug_and_list(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with st:
        row = feedback.record(
            st,
            kind="product_bug",
            actor="agent",
            body="MCP refuse-if-busy still queues under some clients; "
                 "repro with two tools/call in one tick",
            source="test",
        )
        assert row["seq"] >= 1
        assert row["kind"] == "product_bug"
        assert row["actor"] == "agent"
        rows = feedback.list_feedback(st, kind="product_bug")
        assert len(rows) == 1
        assert "refuse-if-busy" in rows[0]["body"]


def test_finding_judgment_requires_review_and_index(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with st:
        with pytest.raises(feedback.FeedbackError, match="review_id"):
            feedback.record(
                st, kind="finding_judgment", body="x" * 25, actor="agent")
        with pytest.raises(feedback.FeedbackError, match="finding_index"):
            feedback.record(
                st, kind="finding_judgment", body="x" * 25,
                actor="agent", review_id="sk_x")
        row = feedback.record(
            st, kind="finding_judgment", body="disagree: guard already exists "
            "in the caller at line 12",
            actor="agent", review_id="sk_x", finding_index=0)
        assert row["finding_index"] == 0


def test_short_body_refused(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with st:
        with pytest.raises(feedback.FeedbackError, match="20"):
            feedback.record(
                st, kind="product_note", body="too short", actor="human")


def test_feedback_does_not_appear_in_triage_state(tmp_path):
    """Feedback must not clear or touch the triage/gate ledger."""
    st = Store.open(tmp_path / "s.db")
    with st:
        feedback.record(
            st, kind="finding_judgment",
            body="agent thinks this is a false positive for reason Xxxxxxx",
            actor="agent", review_id="sk_1", finding_index=0)
        assert st.triage_for("main", "a" * 40) == {}


def test_cli_feedback_add_and_list(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    body = "product bug: dual-hold off still serializes with legacy lock path"
    assert main([
        "feedback", "add",
        "--kind", "product_bug",
        "--actor", "agent",
        body,
    ]) == 0
    out = capsys.readouterr().out
    assert "recorded #" in out
    assert main(["feedback", "list", "--kind", "product_bug", "-n", "10"]) == 0
    listed = capsys.readouterr().out
    assert "product_bug" in listed
    assert "dual-hold" in listed


def test_svc_feedback_refuses_bad_kind(tmp_path):
    from skodun.services import svc_feedback_add

    st = Store.open(tmp_path / "s.db")
    with st:
        code, text = svc_feedback_add(
            st, kind="nope", body="x" * 30, actor="agent")
        assert code == 1
        assert "refused" in text
