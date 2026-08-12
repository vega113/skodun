"""Shipped-path checks for non-mutating inspection and explicit migration."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from skodun.cli import main
from skodun.store import (SCHEMA_VERSION, SchemaLifecycleError, Store,
                          inspect_schema)


def _downgrade(path, version=12):
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {version}")


def _authority_db(tmp_path):
    return tmp_path / f"authority-{os.getpid()}.db"


def test_inspection_and_ordinary_open_are_byte_stable_for_older_store(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    before = db.read_bytes()
    assert inspect_schema(db)["state"] == "older"
    with pytest.raises(SchemaLifecycleError, match="explicit migration"):
        Store.open(db)
    assert db.read_bytes() == before
    assert inspect_schema(db)["version"] == 12


def test_explicit_migration_creates_backup_and_bounded_receipt(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["schema_from"] == 12
    assert receipt["schema_to"] == SCHEMA_VERSION
    assert receipt["result"] == "success"
    assert db.with_name(db.name + ".backup-before-v13").stat().st_mode & 0o077 == 0
    saved = json.loads(db.with_name(db.name + ".migration-receipt.json").read_text())
    assert saved["backup_sha256"] == receipt["backup_sha256"]
    assert inspect_schema(db)["state"] == "current"


def test_migration_refuses_dirty_or_active_build(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    with pytest.raises(SchemaLifecycleError, match="clean build"):
        Store.migrate_existing(db, build_commit="a" * 40 + "-dirty")


def test_cli_migration_plan_does_not_create_missing_store(tmp_path, monkeypatch,
                                                          capsys):
    db = tmp_path / "missing" / "authority.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    assert main(["store", "migrate", "--plan"]) == 0
    assert not db.exists()
    assert not db.parent.exists()
    assert "state=missing" in capsys.readouterr().out


def test_cli_readiness_refuses_older_store_without_mutation(tmp_path):
    # The store ResourceWarning sweep runs this module in a child process
    # alongside the parent suite; pytest's tmp_path numbering is independent
    # in those processes, so include the PID to keep their fixtures disjoint.
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    before = db.read_bytes()
    env = os.environ.copy()
    env["SKODUN_DB"] = str(db)
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "src"), str(repo_root)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    result = subprocess.run(
        [sys.executable, "-m", "skodun", "review-readiness", "--repo",
         str(tmp_path)], capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert db.read_bytes() == before
    assert "migration" in (result.stdout + result.stderr).lower()
