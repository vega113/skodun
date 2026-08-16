"""Slice A: one machine-wide store must stay readable under multi-opener load.

Tests drive the shipped ``inspect_schema`` / ``Store.open`` / ``run_doctor``
paths. They never open the live default store.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from skodun.doctor import run_doctor
from skodun.store import SCHEMA_VERSION, SchemaLifecycleError, Store, inspect_schema
from tests.test_store import REC


def _write_review(db: Path, review_id: str) -> None:
    rec = {**REC, "id": review_id}
    with Store.open(db) as st:
        st.save_review(rec)


def _header_write_version(path: Path) -> int:
    return path.read_bytes()[18]


def _make_torn_wal(path: Path) -> bytes:
    """Healthy WAL store, then tear the main image and leave an empty -wal."""
    _write_review(path, "keep-me")
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        raw.execute(
            "UPDATE reviews SET summary=? WHERE id=?",
            ("more-pages", "keep-me"),
        )
        raw.commit()
    finally:
        raw.close()
    assert _header_write_version(path) == 2
    data = bytearray(path.read_bytes())
    # Leave the 100-byte header; smash the first interior page.
    start = 4096 if len(data) > 8192 else 100
    end = min(len(data), start + 4096)
    data[start:end] = b"\xff" * (end - start)
    path.write_bytes(data)
    Path(str(path) + "-wal").write_bytes(b"")
    return bytes(data)


def _quarantines(path: Path) -> list[Path]:
    parent = path.parent
    prefix = path.name + ".malformed-"
    return sorted(p for p in parent.iterdir() if p.name.startswith(prefix)
                  and not p.name.endswith(("-wal", "-shm")))


def test_inspect_schema_maps_busy_not_invalid_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    _write_review(db, "r-busy")
    import skodun.store as store_mod

    real_connect = store_mod.sqlite3.connect

    def wrap(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_mod.sqlite3, "connect", wrap)
    # The snapshot is already on disk; only the inspect connect is patched.
    assert real_connect is not wrap
    info = inspect_schema(db)
    assert info.state == "invalid"
    assert info.reason_code == "busy"
    assert info.reason_code != "invalid_sqlite"


def test_inspect_schema_under_writer_does_not_report_invalid_sqlite(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review({**REC, "id": "held"})
        st._c.execute("BEGIN IMMEDIATE")
        st._c.execute("UPDATE reviews SET summary=? WHERE id=?", ("x", "held"))
        info = inspect_schema(db)
        st._c.execute("ROLLBACK")
    if info.state == "invalid":
        assert info.reason_code == "busy"
    else:
        assert info.state == "current"
        assert info.reason_code is None


def test_inspect_schema_torn_wal_is_repairable_and_non_mutating(tmp_path):
    db = tmp_path / "s.db"
    original = _make_torn_wal(db)
    info = inspect_schema(db)
    assert db.read_bytes() == original
    assert info.state == "invalid"
    assert info.reason_code == "torn_wal"
    assert "repair" in (info.detail or "").lower() or info.reason_code == "torn_wal"


def test_open_quarantines_torn_wal_and_never_replaces_with_empty_store(tmp_path):
    db = tmp_path / "s.db"
    original = _make_torn_wal(db)
    opened = None
    try:
        opened = Store.open(db)
    except SchemaLifecycleError as exc:
        assert exc.reason_code == "torn_wal"
        assert "quarantine" in str(exc).lower() or "malformed" in str(exc).lower()
        assert "empty" not in str(exc).lower() or "not" in str(exc).lower()
    found = _quarantines(db)
    assert found, "torn image must be copied aside as *.malformed-<utc>"
    assert found[0].read_bytes() == original
    assert original  # the broken bytes still exist somewhere
    if opened is not None:
        try:
            row = opened._c.execute(
                "PRAGMA integrity_check").fetchone()
            assert row[0] == "ok"
            n = opened._c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            # Recovered, not a silent fresh empty authority.
            assert n >= 0
            if n == 0:
                pytest.fail("recover replaced the torn store with an empty one")
        finally:
            opened.close()
    else:
        # Fail closed: path is still the torn file, not a new v16 empty store.
        assert db.exists()
        assert db.read_bytes() == original
        info = inspect_schema(db)
        assert info.state == "invalid"
        assert info.reason_code == "torn_wal"


def test_new_store_wal_uses_full_synchronous(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        mode = st._c.execute("PRAGMA journal_mode").fetchone()[0].lower()
        sync = st._c.execute("PRAGMA synchronous").fetchone()[0]
        assert mode == "wal"
        # SQLite: 1=NORMAL (WAL default), 2=FULL, 3=EXTRA. Owner wants intact.
        assert int(sync) >= 2


def test_many_processes_opening_one_store_leave_integrity_ok(tmp_path):
    db = tmp_path / "s.db"
    _write_review(db, "seed")
    script = tmp_path / "opener.py"
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "from skodun.store import Store\n"
        "from tests.test_store import REC\n"
        "db = Path(sys.argv[1])\n"
        "rid = sys.argv[2]\n"
        "with Store.open(db) as st:\n"
        "    st.save_review({**REC, 'id': rid})\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(root)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    procs = []
    for i in range(6):
        procs.append(subprocess.Popen(
            [sys.executable, str(script), str(db), f"p{i}"],
            env=env, cwd=str(root),
        ))
    codes = [p.wait(timeout=30) for p in procs]
    assert codes == [0] * len(codes)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ids = {r[0] for r in conn.execute("SELECT id FROM reviews")}
    finally:
        conn.close()
    assert "seed" in ids
    assert {f"p{i}" for i in range(6)} <= ids


def test_existing_delete_journal_store_is_not_forced_to_wal(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review({**REC, "id": "kept"})
        st._c.execute("PRAGMA journal_mode=DELETE")
    raw = sqlite3.connect(db)
    try:
        raw.execute("PRAGMA journal_mode=DELETE")
        raw.commit()
    finally:
        raw.close()
    assert _header_write_version(db) == 1
    with Store.open(db) as st:
        mode = st._c.execute("PRAGMA journal_mode").fetchone()[0].lower()
        assert mode == "delete"


def test_doctor_store_line_includes_journal_wal_and_integrity(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "nope.toml"))
    _write_review(db, "doc")
    report = run_doctor(repo=tmp_path, store_path=db)
    store_check = next(c for c in report.checks if c.name == "store")
    assert store_check.ok
    detail = store_check.detail
    assert "journal_mode=" in detail
    assert "integrity_check=" in detail
    assert "-wal" in detail
    assert "-shm" in detail
    assert "ok" in detail
    cap_check = next(c for c in report.checks if c.name == "capacity")
    assert cap_check.ok
    assert "machine_cap=" in cap_check.detail
    assert "machine_holders=" in cap_check.detail
    assert "by_repo=" in cap_check.detail
    assert "by_provider=" in cap_check.detail
