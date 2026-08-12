"""Shipped-path checks for non-mutating inspection and explicit migration."""

from __future__ import annotations

import json
import sqlite3

import pytest

from skodun.cli import main
from skodun.store import (SCHEMA_VERSION, SchemaLifecycleError, Store,
                          inspect_schema)


def _downgrade(path, version=12):
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {version}")


def test_inspection_and_ordinary_open_are_byte_stable_for_older_store(tmp_path):
    db = tmp_path / "authority.db"
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
    db = tmp_path / "authority.db"
    with Store.open(db):
        pass
    _downgrade(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["schema_from"] == 12
    assert receipt["schema_to"] == SCHEMA_VERSION
    assert receipt["result"] == "success"
    assert (tmp_path / "authority.db.backup-before-v13").stat().st_mode & 0o077 == 0
    saved = json.loads((tmp_path / "authority.db.migration-receipt.json").read_text())
    assert saved["backup_sha256"] == receipt["backup_sha256"]
    assert inspect_schema(db)["state"] == "current"


def test_migration_refuses_dirty_or_active_build(tmp_path):
    db = tmp_path / "authority.db"
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


def test_cli_readiness_refuses_older_store_without_mutation(tmp_path, monkeypatch,
                                                            capsys):
    db = tmp_path / "authority.db"
    with Store.open(db):
        pass
    _downgrade(db)
    before = db.read_bytes()
    monkeypatch.setenv("SKODUN_DB", str(db))
    assert main(["review-readiness", "--repo", str(tmp_path)]) == 2
    assert db.read_bytes() == before
    assert "migration" in capsys.readouterr().out.lower()
