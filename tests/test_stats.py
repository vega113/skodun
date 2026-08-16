"""Tests for the additive v9 telemetry read model and CLI rendering."""

from __future__ import annotations

import json
import sqlite3

from skodun.cli import build_parser
from skodun.stats import render, since_iso
from skodun.store import SCHEMA_VERSION, Store


def test_v9_migration_adds_nullable_telemetry_columns(tmp_path):
    db = tmp_path / "stats.db"
    raw = sqlite3.connect(db)
    raw.executescript("""
    CREATE TABLE reviews (
      id TEXT PRIMARY KEY, reviewed_at TEXT, branch TEXT, head TEXT,
      base_ref TEXT, base_sha TEXT, diff_hash TEXT, context_hash TEXT,
      mode TEXT, model TEXT, adapter TEXT, status TEXT,
      parse_ok INTEGER, degraded INTEGER, diff_truncated INTEGER, trustworthy INTEGER,
      stop_reason TEXT, findings_total INTEGER, sev_high INTEGER, sev_medium INTEGER,
      sev_low INTEGER, summary TEXT, source TEXT DEFAULT 'skodun', artifact_json TEXT
    );
    CREATE INDEX ix_reviews_diff ON reviews(diff_hash, trustworthy);
    CREATE INDEX ix_reviews_branch ON reviews(branch, reviewed_at);
    CREATE TABLE triage (
      ledger_key TEXT PRIMARY KEY, finding_key TEXT, review_id TEXT, branch TEXT,
      base_sha TEXT, file TEXT, line INTEGER, severity TEXT, title TEXT,
      dismissed_reason TEXT, dismissed_at TEXT
    );
    CREATE INDEX ix_triage_scope ON triage(branch, base_sha);
    CREATE TABLE gate_events (
      at TEXT, repo TEXT, branch TEXT, diff_hash TEXT, outcome TEXT,
      code INTEGER, note TEXT
    );
    """)
    raw.execute(
        "INSERT INTO reviews(id, reviewed_at, artifact_json) VALUES (?,?,?)",
        ("legacy", "2026-08-08T00:00:00Z", json.dumps({"id": "legacy"})))
    raw.commit()
    raw.close()

    st = Store._open_for_migration_tests(db)
    assert SCHEMA_VERSION == 16
    cols = {r[1] for r in st._c.execute("PRAGMA table_info(reviews)")}
    assert {"review_started_at", "review_completed_at", "repo_id",
            "worktree_root", "orchestration_id", "attempt_ordinal",
            "terminal_reason", "outcome"} <= cols
    reuse_cols = {r[1] for r in st._c.execute(
        "PRAGMA table_info(reuse_events)")}
    assert {"at", "outcome", "reason", "matched_review_id",
            "security_policy_hash"} <= reuse_cols
    row = st._c.execute(
        "SELECT review_started_at, repo_id FROM reviews WHERE id='legacy'").fetchone()
    assert row["review_started_at"] is None and row["repo_id"] is None
    st.close()


def test_stats_use_explicit_stage_and_capacity_fields(tmp_path):
    st = Store.open(tmp_path / "stats.db")
    st._c.execute(
        """INSERT INTO reviews
           (id, reviewed_at, review_started_at, review_completed_at, repo,
            repo_id, status, parse_ok, degraded, diff_truncated, trustworthy,
            findings_total, artifact_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("r1", "2026-08-08T00:00:00Z", "2026-08-08T00:00:00Z",
         "2026-08-08T00:00:02Z", "/repo/.git", "/repo/.git", "clean",
         1, 0, 0, 1, 2, json.dumps({"id": "r1"})))
    st._c.execute(
        """INSERT INTO reviews
           (id, reviewed_at, status, parse_ok, degraded, diff_truncated,
            trustworthy, findings_total, artifact_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("legacy", "2026-08-08T00:00:00Z", "clean", 1, 0, 0, 1, 0,
         json.dumps({"id": "legacy"})))
    st._c.execute(
        """INSERT INTO capacity_admissions
           (id, resource_class, scope, status, queued_at, admitted_at,
            started_at, ended_at, wait_ms, queue_wait_ms, run_ms,
            total_admission_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("a1", "review-fg", "/repo/.git", "released",
         "2026-08-08T00:00:00Z", "2026-08-08T00:00:01Z",
         "2026-08-08T00:00:02Z", "2026-08-08T00:00:05Z", 5000, 1000, 3000,
         5000))
    data = st.telemetry_stats(since_iso="2026-08-08T00:00:00Z")
    assert data["reviews"] == {
        "total": 2, "telemetry_rows": 1, "legacy_rows": 1,
        "repo_coverage": 1, "trustworthy": 2, "trustworthy_rate": 1.0,
        "findings": 2,
        "by_repo": [{"repo_id": "/repo/.git", "reviews": 1,
                      "trustworthy": 1, "findings": 2, "legacy_rows": 0,
                      "trustworthy_rate": 1.0},
                     {"repo_id": "legacy:unresolved", "reviews": 1,
                      "trustworthy": 1, "findings": 0, "legacy_rows": 1,
                      "trustworthy_rate": 1.0}],
    }
    assert data["timing"]["review_ms"]["p50_ms"] == 2000
    assert data["timing"]["capacity_queue_ms"]["p90_ms"] == 1000
    assert data["timing"]["capacity_run_ms"]["p90_ms"] == 3000
    assert data["timing"]["capacity_total_admission_ms"]["total_ms"] == 5000
    assert json.loads(render(data, fmt="json")) == data
    assert "review_ms=count:1 p50:2000 p90:2000" in render(data)
    st.close()


def test_stats_count_append_only_reuse_events(tmp_path):
    with Store.open(tmp_path / "stats.db") as st:
        st.append_reuse_event(
            at="2026-08-09T00:00:00Z", outcome="hit",
            reason="exact identity match", matched_review_id="r1")
        st.append_reuse_event(
            at="2026-08-09T00:00:01Z", outcome="miss",
            reason="tree changed")
        data = st.telemetry_stats(since_iso="2026-08-08T00:00:00Z")
    assert data["reuse"] == {"hits": 1, "misses": 1}


def test_stats_does_not_count_reuse_bypasses_or_errors_as_misses(tmp_path):
    with Store.open(tmp_path / "stats.db") as st:
        for outcome in ("bypass", "error"):
            st.append_reuse_event(
                at="2026-08-09T00:00:00Z", outcome=outcome,
                reason="explicit caller intent")
        data = st.telemetry_stats(since_iso="2026-08-08T00:00:00Z")
    assert data["reuse"] == {"hits": 0, "misses": 0}


def test_stats_machine_cap_follows_toml_when_env_is_unset(tmp_path, monkeypatch):
    from skodun.capacity import resolved_machine_capacity
    from skodun.config import load_config
    from skodun.services import svc_stats

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[capacity]\nmachine = 2\n", encoding="utf-8")
    monkeypatch.delenv("SKODUN_REVIEW_MACHINE_CAPACITY", raising=False)
    monkeypatch.setenv("SKODUN_CONFIG", str(cfg_path))
    expected = resolved_machine_capacity(load_config(None, global_path=cfg_path))
    with Store.open(tmp_path / "stats.db") as st:
        code, text = svc_stats(st, since_days=7, fmt="text")
    assert code == 0
    assert f"machine_cap={expected}" in text
    assert expected == 2


def test_stats_rejects_bool_days_and_parser_exposes_json(tmp_path):
    assert since_iso(0, now=0) == "1970-01-01T00:00:00Z"
    try:
        since_iso(True)
    except ValueError as e:
        assert "non-negative int" in str(e)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("bool must not be accepted as a day count")
    args = build_parser().parse_args(["stats", "--since-days", "3", "--json"])
    assert args.command == "stats" and args.since_days == 3
    assert args.json_output is True
