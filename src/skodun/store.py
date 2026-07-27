"""SQLite persistence for reviews, triage decisions and gate events.

The store is the only place that decides whether a review is trustworthy: it
computes the value from the record's three trust axes via
:func:`skodun.trust.is_trustworthy` and writes it into both the indexed column
and the stored artifact JSON, so an index row that disagrees with its artifact
is impossible by construction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .trust import is_trustworthy

_TRUST_AXES = ("parse_ok", "degraded", "diff_truncated")

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


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    @classmethod
    def open(cls, path: Path) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        return cls(conn)

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
                 sev_high, sev_medium, sev_low, summary, source, artifact_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                 artifact_json=excluded.artifact_json""",
            (rec["id"], rec.get("reviewed_at"), rec.get("branch"), rec.get("head"),
             rec.get("base_ref"), rec.get("base_sha"), rec.get("diff_hash"),
             rec.get("context_hash", ""), rec.get("mode"), rec.get("model"),
             rec.get("adapter"), rec.get("status"), int(bool(rec.get("parse_ok"))),
             int(bool(rec.get("degraded"))), int(bool(rec.get("diff_truncated"))),
             int(bool(rec.get("trustworthy"))), rec.get("stop_reason"),
             int(rec.get("findings_total") or 0), int(sev.get("high") or 0),
             int(sev.get("medium") or 0), int(sev.get("low") or 0),
             rec.get("summary"), rec.get("source", "skodun"),
             json.dumps(rec, ensure_ascii=False)))

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

    def add_triage(self, rec: dict) -> None:
        # Fail closed on the review_id/id spelling: `rec.get("review_id") or
        # rec.get("id")` would silently write NULL (no review linkage) when
        # neither key is present. Require one of the two spellings explicitly
        # so a malformed record raises KeyError instead of persisting an
        # orphaned triage row.
        review_id = rec["review_id"] if "review_id" in rec else rec["id"]
        self._c.execute(
            """INSERT OR REPLACE INTO triage (ledger_key, finding_key, review_id, branch,
                 base_sha, file, line, severity, title, dismissed_reason, dismissed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["ledger_key"], rec["finding_key"],
             review_id, rec["branch"],
             rec["base_sha"], rec.get("file"), rec.get("line"), rec.get("severity"),
             rec.get("title"), rec["dismissed_reason"], rec.get("dismissed_at")))

    def triage_for(self, branch: str, base_sha: str) -> dict[str, dict]:
        rows = self._c.execute("SELECT * FROM triage WHERE branch=? AND base_sha=?",
                               (branch, base_sha)).fetchall()
        return {r["finding_key"]: dict(r) for r in rows}

    def list_reviews(self, branch: str | None, limit: int = 30) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch is not None:
            q += " WHERE branch=?"
            args = (branch,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
        rows = self._c.execute(q, args + (limit,)).fetchall()
        return [json.loads(r["artifact_json"]) for r in rows]
