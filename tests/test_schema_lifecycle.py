"""Shipped-path checks for non-mutating inspection and explicit migration.

Contract: read-only and refused schema paths preserve database bytes and
filesystem entries, while only the explicit maintenance path advances schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from skodun.cli import main
from skodun.store import (SCHEMA_VERSION, SchemaInfo, SchemaLifecycleError,
                          Store, inspect_schema, migration_blockers,
                          migration_receipt_path)


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
    assert inspect_schema(db).state == "older"
    with pytest.raises(SchemaLifecycleError, match="explicit migration"):
        Store.open(db)
    assert db.read_bytes() == before
    assert inspect_schema(db).version == 12


def test_explicit_migration_creates_backup_and_bounded_receipt(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["schema_from"] == 12
    assert receipt["schema_to"] == SCHEMA_VERSION
    assert receipt["result"] == "success"
    assert db.with_name(
        db.name + ".backup-before-v" + str(SCHEMA_VERSION)
    ).stat().st_mode & 0o077 == 0
    saved = json.loads(migration_receipt_path(db).read_text())
    assert saved["backup_sha256"] == receipt["backup_sha256"]
    assert inspect_schema(db).state == "current"


def test_cli_migration_apply_uses_the_shipped_maintenance_path(tmp_path, capsys,
                                                              monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    monkeypatch.setattr("skodun.provenance.code_provenance",
                        lambda: {"skodun_commit": "b" * 40})
    assert main(["store", "migrate", "--apply", "--db", str(db),
                 "--build-commit", "b" * 40]) == 0
    assert inspect_schema(db).state == "current"
    assert "applied v12" in capsys.readouterr().out


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


def test_inspect_schema_refuses_fifo_without_hanging(tmp_path):
    fifo = tmp_path / "store.db"
    os.mkfifo(fifo)
    start = time.monotonic()
    info = inspect_schema(fifo)
    assert time.monotonic() - start < 2.0
    assert info.state == "invalid"
    assert info.reason_code == "not_a_file"


def test_inspect_schema_refuses_symlink(tmp_path):
    target = tmp_path / "real.db"
    target.write_bytes(b"not-sqlite")
    link = tmp_path / "store.db"
    link.symlink_to(target)
    info = inspect_schema(link)
    assert info.state == "invalid"
    assert info.reason_code == "symlink"


def test_source_layout_refuses_default_shared_with_embed(tmp_path, monkeypatch,
                                                         capsys):
    shared = tmp_path / "shared.db"
    with Store.open(shared):
        pass
    _downgrade(shared)
    monkeypatch.setattr("skodun.cli._DEFAULT_DB", shared)
    monkeypatch.delenv("SKODUN_DB", raising=False)
    monkeypatch.setattr("skodun.provenance.code_provenance",
                        lambda: {"skodun_commit": "c" * 40})
    monkeypatch.setattr("skodun.provenance._embedded_identity",
                        lambda: {"skodun_commit": "c" * 40, "source": "sdist"})
    assert main(["store", "migrate", "--apply"]) == 2
    assert inspect_schema(shared).state == "older"
    assert "source_checkout_default_store" in capsys.readouterr().out


def test_cli_wheel_apply_uses_embedded_commit(tmp_path, capsys, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    monkeypatch.setattr("skodun.provenance.code_provenance",
                        lambda: {"skodun_commit": "c" * 40})
    monkeypatch.setattr("skodun.provenance._embedded_identity",
                        lambda: {"skodun_commit": "c" * 40, "source": "wheel"})
    assert main(["store", "migrate", "--apply", "--db", str(db)]) == 0
    assert inspect_schema(db).state == "current"
    assert "applied v12" in capsys.readouterr().out


def test_cli_apply_rejects_mismatched_build_commit(tmp_path, monkeypatch, capsys):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    monkeypatch.setattr("skodun.provenance.code_provenance",
                        lambda: {"skodun_commit": "c" * 40})
    assert main(["store", "migrate", "--apply", "--db", str(db),
                 "--build-commit", "d" * 40]) == 2
    assert inspect_schema(db).state == "older"
    assert "build_identity_mismatch" in capsys.readouterr().out


def test_stale_running_review_without_pid_does_not_block_forever(tmp_path):
    db = tmp_path / "v1-stale-running.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reviews (
          id TEXT PRIMARY KEY, reviewed_at TEXT, branch TEXT, head TEXT,
          base_ref TEXT, base_sha TEXT, diff_hash TEXT, context_hash TEXT,
          mode TEXT, model TEXT, adapter TEXT, status TEXT,
          parse_ok INTEGER, degraded INTEGER, diff_truncated INTEGER,
          trustworthy INTEGER, stop_reason TEXT, findings_total INTEGER,
          sev_high INTEGER, sev_medium INTEGER, sev_low INTEGER, summary TEXT,
          source TEXT DEFAULT 'skodun', artifact_json TEXT
        );
        CREATE TABLE triage (
          ledger_key TEXT PRIMARY KEY, finding_key TEXT, review_id TEXT,
          branch TEXT, base_sha TEXT, file TEXT, line INTEGER, severity TEXT,
          title TEXT, dismissed_reason TEXT, dismissed_at TEXT
        );
        CREATE TABLE gate_events (
          at TEXT, repo TEXT, branch TEXT, diff_hash TEXT, outcome TEXT,
          code INTEGER, note TEXT
        );
    """)
    conn.execute("INSERT INTO reviews(id, status) VALUES ('sk_stale', 'running')")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    assert "active_review" not in migration_blockers(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["result"] == "success"


def test_live_pid_running_review_blocks_migration(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, pid) VALUES (?,?,?)",
            ("sk_live", "running", os.getpid()))
    _downgrade(db)
    assert "active_review" in migration_blockers(db)


def test_dead_pid_running_review_does_not_block_migration(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, pid) VALUES (?,?,?)",
            ("sk_dead", "running", 2 ** 22))
    _downgrade(db)
    assert "active_review" not in migration_blockers(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["result"] == "success"


def test_fresh_null_pid_running_review_blocks_migration(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, pid, reviewed_at) VALUES (?,?,?,?)",
            ("sk_preattach", "running", None, time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    _downgrade(db)
    assert "active_review" in migration_blockers(db)


def test_stale_null_pid_running_review_does_not_block_migration(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, pid, reviewed_at) VALUES (?,?,?,?)",
            ("sk_orphan", "running", None, "2020-01-01T00:00:00Z"))
    _downgrade(db)
    assert "active_review" not in migration_blockers(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["result"] == "success"


def test_dead_capacity_admission_does_not_block_migration(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store.capacity_enqueue(
            admission_id="ca_dead", resource_class="review-fg",
            scope=str(tmp_path), pid=2 ** 22)
    _downgrade(db)
    assert "active_capacity_admission" not in migration_blockers(db)
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["result"] == "success"


def test_legacy_fg_lock_blocks_without_capacity_row(tmp_path):
    db = _authority_db(tmp_path)
    common = tmp_path / "repo.git"
    common.mkdir()
    lock = common / "grok-reviews-foreground.lock"
    lock.mkdir()
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nstarted={int(time.time())}\nworktree={tmp_path}\n",
        encoding="utf-8")
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, worktree_root) VALUES (?,?,?)",
            ("sk_lock", "clean", str(tmp_path)))
    # worktree_root may not be a git repo; plant the lock at .git instead.
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    planted = gitdir / "grok-reviews-foreground.lock"
    if not planted.exists():
        os.rename(lock, planted)
    _downgrade(db)
    assert "legacy_fg_lock" in migration_blockers(db)


def test_legacy_fg_lock_blocks_for_worktree_git_file(tmp_path, monkeypatch):
    db = _authority_db(tmp_path)
    common = tmp_path / "main.git"
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    lock = common / "grok-reviews-foreground.lock"
    lock.mkdir()
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nstarted={int(time.time())}\nworktree={worktree}\n",
        encoding="utf-8")
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, worktree_root) VALUES (?,?,?)",
            ("sk_wt", "clean", str(worktree)))
    def _boom(_repo):
        raise RuntimeError("no git")
    monkeypatch.setattr("skodun.gitio.git_common_dir", _boom)
    _downgrade(db)
    assert "legacy_fg_lock" in migration_blockers(db)


def test_legacy_fg_lock_blocks_from_pre_v9_repo_column(tmp_path):
    db = _authority_db(tmp_path)
    common = tmp_path / "common.git"
    common.mkdir()
    lock = common / "grok-reviews-foreground.lock"
    lock.mkdir()
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nstarted={int(time.time())}\nworktree={tmp_path}\n",
        encoding="utf-8")
    with Store.open(db) as store:
        store._c.execute(
            "INSERT INTO reviews(id, status, repo) VALUES (?,?,?)",
            ("sk_repo", "clean", str(common)))
    _downgrade(db)
    assert "legacy_fg_lock" in migration_blockers(db)


def test_partial_ladder_keeps_pre_migration_backup(tmp_path, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db, version=12)
    import skodun.store as store_mod
    real = store_mod._migrate

    def wrap(conn):
        conn.execute("PRAGMA user_version = 13")
        raise ValueError("injected mid-ladder")

    monkeypatch.setattr(store_mod, "_migrate", wrap)
    with pytest.raises(ValueError, match="injected mid-ladder"):
        Store.migrate_existing(db, build_commit="a" * 40)
    monkeypatch.setattr(store_mod, "_migrate", real)
    assert inspect_schema(db).version == 13
    backup = Path(str(db) + ".backup-before-v" + str(SCHEMA_VERSION))
    assert backup.exists()
    assert not Path(str(db) + ".migration.lock").exists()
    assert not migration_receipt_path(db).exists()


def test_failed_apply_is_retryable_when_store_stays_old(tmp_path, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    original = Path.chmod

    def chmod(self, mode):
        if str(self).endswith(".backup-before-v" + str(SCHEMA_VERSION)):
            raise OSError("injected chmod failure")
        return original(self, mode)

    monkeypatch.setattr(Path, "chmod", chmod)
    with pytest.raises(OSError):
        Store.migrate_existing(db, build_commit="a" * 40)
    monkeypatch.setattr(Path, "chmod", original)
    assert inspect_schema(db).state == "older"
    assert not migration_receipt_path(db).exists()
    leftover = Path(str(db) + ".backup-before-v" + str(SCHEMA_VERSION))
    assert leftover.exists()
    leftover.unlink()
    receipt = Store.migrate_existing(db, build_commit="a" * 40)
    assert receipt["result"] == "success"


def test_inspect_schema_refuses_dangling_wal_symlink(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    Path(str(db) + "-wal").symlink_to(tmp_path / "missing-wal")
    info = inspect_schema(db)
    assert info.state == "invalid"
    assert info.reason_code == "symlink"


def test_open_readonly_cleans_snapshot_if_connect_fails(tmp_path, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    import skodun.store as store_mod
    real = store_mod.sqlite3.connect
    calls = {"ro": 0}

    def wrap(*args, **kwargs):
        target = args[0] if args else ""
        if isinstance(target, str) and "mode=ro" in target:
            calls["ro"] += 1
            if calls["ro"] >= 2:
                raise sqlite3.OperationalError("injected readonly connect")
        return real(*args, **kwargs)

    monkeypatch.setattr(store_mod.sqlite3, "connect", wrap)
    before = set(Path(tempfile.gettempdir()).glob("skodun-inspect-*"))
    with pytest.raises(sqlite3.OperationalError, match="injected readonly"):
        Store.open_readonly(db)
    after = set(Path(tempfile.gettempdir()).glob("skodun-inspect-*"))
    assert after <= before


def test_unreadable_blocker_snapshot_refuses_migration(tmp_path, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    info = inspect_schema(db)
    import skodun.store as store_mod
    monkeypatch.setattr(store_mod, "inspect_schema", lambda path: info)
    monkeypatch.setattr(
        store_mod, "_snapshot_database",
        lambda path: (None, None, SchemaInfo(
            "invalid", str(path), None, SCHEMA_VERSION,
            reason_code="temp_unavailable")))
    assert "blockers_unreadable" in migration_blockers(db)
    with pytest.raises(SchemaLifecycleError, match="blockers_unreadable"):
        Store.migrate_existing(db, build_commit="a" * 40)


def test_open_readonly_does_not_create_shm_beside_original(tmp_path):
    db = _authority_db(tmp_path)
    with Store.open(db) as store:
        store._c.execute("PRAGMA journal_mode=WAL")
    shm = Path(str(db) + "-shm")
    if shm.exists():
        shm.unlink()
    with Store.open_readonly(db):
        pass
    assert not shm.exists()
