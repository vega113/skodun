"""Slice A: one machine-wide store must stay readable under multi-opener load.

Tests drive the shipped ``inspect_schema`` / ``Store.open`` / ``run_doctor``
paths. They never open the live default store.
"""

from __future__ import annotations

import os
from contextlib import closing
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


def test_doctor_machine_cap_follows_toml_when_env_is_unset(tmp_path, monkeypatch):
    """Doctor must print the same effective cap pipeline resolves from config."""
    from skodun.capacity import resolved_machine_capacity
    from skodun.config import load_config

    db = tmp_path / "s.db"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[capacity]\nmachine = 2\n", encoding="utf-8")
    monkeypatch.delenv("SKODUN_REVIEW_MACHINE_CAPACITY", raising=False)
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(cfg_path))
    _write_review(db, "doc")
    cfg = load_config(tmp_path, global_path=cfg_path)
    expected = resolved_machine_capacity(cfg)
    report = run_doctor(repo=tmp_path, store_path=db, config_path=cfg_path)
    cap_check = next(c for c in report.checks if c.name == "capacity")
    assert cap_check.ok
    assert f"machine_cap={expected}" in cap_check.detail
    assert expected == 2


@pytest.mark.parametrize("damage", ["table", "index", "column"])
def test_recovery_rejects_incomplete_current_schema(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    good = tmp_path / "good.db"
    _write_review(good, "retained")
    with closing(sqlite3.connect(good, isolation_level=None)) as conn:
        if damage == "table":
            conn.execute("DROP TABLE capacity_admissions")
        elif damage == "index":
            index = conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                 "AND sql IS NOT NULL LIMIT 1").fetchone()[0]
            conn.execute(f'DROP INDEX "{index}"')
        else:
            conn.execute("ALTER TABLE reviews ADD COLUMN surprise TEXT")
        sql = '\n'.join(conn.iterdump()) + f'\nPRAGMA user_version={SCHEMA_VERSION};'
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=sql))
    assert not mod._recover_sqlite_image(good, tmp_path / "recovered.db")


def test_verified_recovery_preserves_review_and_private_permissions(tmp_path, monkeypatch):
    import skodun.store as mod
    good = tmp_path / "good.db"
    _write_review(good, "retained")
    with closing(sqlite3.connect(good, isolation_level=None)) as conn:
        sql = '\n'.join(conn.iterdump()) + f'\nPRAGMA user_version={SCHEMA_VERSION};'
    db = tmp_path / "broken.db"
    original = _make_torn_wal(db)
    db.chmod(0o600)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=sql))
    with Store.open(db) as store:
        assert store._c.execute("SELECT id FROM reviews").fetchall()[0][0] == "retained"
    assert db.stat().st_mode & 0o777 == 0o600
    quarantine = _quarantines(db)[0]
    assert quarantine.read_bytes() == original
    assert quarantine.stat().st_mode & 0o777 == 0o600


def test_recovery_uses_absolute_path_for_leading_dash(tmp_path, monkeypatch):
    import skodun.store as mod
    monkeypatch.chdir(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="")
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert not mod._recover_sqlite_image(Path("-unsafe.db"), tmp_path / "out")
    assert calls[0][1] == "-readonly"
    assert calls[0][2] == f"file:{tmp_path}/-unsafe.db?mode=ro&immutable=1"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_quarantine_refuses_unsafe_source(tmp_path, kind):
    import skodun.store as mod
    src = tmp_path / "source"
    if kind == "symlink":
        target = tmp_path / "secret"
        target.write_text("must not be copied")
        src.symlink_to(target)
    else:
        os.mkfifo(src)
    with pytest.raises(SchemaLifecycleError):
        mod._copy_store_image(src, tmp_path / "quarantine")
    assert not (tmp_path / "quarantine").exists()


def test_recovery_rechecks_under_lifecycle_lock(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / "s.db"
    _make_torn_wal(db)
    stale = inspect_schema(db)
    # Simulate another opener completing recovery before this caller takes lock.
    db.unlink()
    Path(str(db) + "-wal").unlink(missing_ok=True)
    _write_review(db, "peer-recovered")
    monkeypatch.setattr(mod, "_recover_sqlite_image", lambda *a:
                        pytest.fail("must not replace the peer's recovered store"))
    mod._repair_malformed_store(db, stale)
    with Store.open(db) as store:
        assert store._c.execute("SELECT id FROM reviews").fetchone()[0] == "peer-recovered"
    assert not Path(str(db) + ".migration.lock").exists()


def test_repair_refuses_when_lifecycle_lock_is_held(tmp_path):
    import skodun.store as mod
    db = tmp_path / "s.db"
    original = _make_torn_wal(db)
    lock = Path(str(db) + ".migration.lock")
    lock.write_text(str(os.getpid()))
    with pytest.raises(SchemaLifecycleError, match="lifecycle lock"):
        mod._repair_malformed_store(db, inspect_schema(db))
    assert db.read_bytes() == original
    assert lock.exists()


def test_delete_store_failed_integrity_is_invalid(tmp_path):
    db = tmp_path / "s.db"
    _write_review(db, "retained")
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        index = conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                             "AND tbl_name='reviews' AND sql IS NOT NULL LIMIT 1").fetchone()[0]
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET rootpage=999999 WHERE name=?", (index,))
    info = inspect_schema(db)
    assert info.state == "invalid"
    assert info.reason_code == "invalid_sqlite"


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_ordinary_open_skips_full_integrity_scan(tmp_path, monkeypatch, readonly, journal_mode):
    import skodun.store as mod
    db = tmp_path / "s.db"
    _write_review(db, "retained")
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    if journal_mode == "WAL":
        assert _header_write_version(db) == 2
        assert not Path(str(db) + "-wal").exists()
    statements = []
    real = mod.sqlite3.connect
    def connect(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(mod.sqlite3, "connect", connect)
    opener = Store.open_readonly if readonly else Store.open
    with opener(db):
        pass
    assert not any('integrity_check' in sql for sql in statements)
    assert inspect_schema(db).integrity_check == "ok"


def test_quarantine_rejects_replaced_regular_inode(tmp_path):
    import skodun.store as mod
    source = tmp_path / "source"
    source.write_bytes(b"original")
    original = source.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"different")
    os.replace(replacement, source)
    with pytest.raises(SchemaLifecycleError, match="changed before quarantine"):
        mod._copy_store_image(source, tmp_path / "quarantine", expected=original)
    assert not (tmp_path / "quarantine").exists()


def test_corruption_probe_preserves_bytes_and_missing_sidecars(tmp_path):
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    Path(str(db) + '-wal').unlink(missing_ok=True)
    Path(str(db) + '-shm').unlink(missing_ok=True)
    assert mod._live_image_is_corrupt(db)
    assert db.read_bytes() == original
    assert not Path(str(db) + '-wal').exists()
    assert not Path(str(db) + '-shm').exists()


def test_doctor_other_corruption_directs_manual_restore(tmp_path, monkeypatch):
    db = tmp_path / 'broken.db'
    db.write_bytes(b'not a sqlite database')
    monkeypatch.setenv('SKODUN_CONFIG', str(tmp_path / 'absent.toml'))
    report = run_doctor(repo=tmp_path, store_path=db)
    check = next(c for c in report.checks if c.name == 'store')
    assert not check.ok
    assert 'restore manually' in check.detail
    assert 'next writable' not in check.detail


@pytest.mark.parametrize('always_race', [False, True])
def test_inspection_retries_changed_source_instead_of_reporting_corruption(
        tmp_path, monkeypatch, always_race):
    import skodun.store as mod
    db = tmp_path / 'healthy.db'
    _write_review(db, 'retained')
    real_snapshot = mod._snapshot_database
    copies = []
    def snapshot(path):
        result = real_snapshot(path)
        _temporary, image, error = result
        copies.append(image)
        if error is None and (always_race or len(copies) == 1):
            # A mixed raw snapshot can be malformed although the live writer
            # committed a healthy image after the copy started.
            with closing(sqlite3.connect(db)) as writer:
                writer.execute('UPDATE reviews SET summary=? WHERE id=?',
                               (f'writer-{len(copies)}', 'retained'))
                writer.commit()
            raw = bytearray(image.read_bytes())
            raw[4096:8192] = b'\xff' * 4096
            image.write_bytes(raw)
        return result
    monkeypatch.setattr(mod, '_snapshot_database', snapshot)
    info = inspect_schema(db)
    if always_race:
        assert len(copies) == 3
        assert info.reason_code == 'busy'
        assert info.detail == 'source_changed'
        assert info.integrity_check is None
    else:
        assert len(copies) == 2
        assert info.state == 'current'
        assert info.integrity_check == 'ok'
    with closing(sqlite3.connect(db)) as original:
        assert original.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not _quarantines(db)


def test_doctor_torn_wal_guidance_does_not_promise_every_open_will_recover(tmp_path, monkeypatch):
    db = tmp_path / 'torn.db'
    original = _make_torn_wal(db)
    monkeypatch.setenv('SKODUN_CONFIG', str(tmp_path / 'absent.toml'))
    report = run_doctor(repo=tmp_path, store_path=db)
    check = next(c for c in report.checks if c.name == 'store')
    assert not check.ok
    assert 'may attempt quarantined recovery' in check.detail
    assert 'next writable' not in check.detail
    assert db.read_bytes() == original


def test_failed_recovery_reuses_one_unchanged_quarantine(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('sqlite3 unavailable')
    monkeypatch.setattr(mod.subprocess, 'run', unavailable)
    for _ in range(3):
        with pytest.raises(SchemaLifecycleError):
            Store.open(db)
    copies = _quarantines(db)
    assert len(copies) == 1
    assert copies[0].read_bytes() == original
    assert db.read_bytes() == original


def test_changed_corrupt_image_gets_a_separate_quarantine(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    monkeypatch.setattr(mod, '_recover_sqlite_image', lambda *args: False)
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    changed = bytearray(original)
    changed[-1] ^= 1
    db.write_bytes(changed)
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    assert len(_quarantines(db)) == 2
    assert {p.read_bytes() for p in _quarantines(db)} == {original, bytes(changed)}


def test_conflicting_cached_quarantine_is_preserved_and_refused(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    monkeypatch.setattr(mod, '_recover_sqlite_image', lambda *args: False)
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    quarantine = _quarantines(db)[0]
    quarantine.write_bytes(b'preserve this conflicting image')
    with pytest.raises(SchemaLifecycleError, match='preserved both images'):
        Store.open(db)
    assert len(_quarantines(db)) == 1
    assert quarantine.read_bytes() == b'preserve this conflicting image'
    assert db.read_bytes() == original


def test_nonprivate_cached_quarantine_is_not_reused(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    monkeypatch.setattr(mod, '_recover_sqlite_image', lambda *args: False)
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    quarantine = _quarantines(db)[0]
    quarantine.chmod(0o666)
    with pytest.raises(SchemaLifecycleError, match='private permissions'):
        Store.open(db)
    assert db.read_bytes() == original
    assert len(_quarantines(db)) == 1


def test_recovery_sql_cannot_attach_another_database(tmp_path, monkeypatch):
    import skodun.store as mod
    outside = tmp_path / 'outside.db'
    literal = str(outside).replace("'", "''")
    sql = f"ATTACH DATABASE '{literal}' AS escaped; CREATE TABLE escaped.wrong(x);"
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=sql))
    assert not mod._recover_sqlite_image(tmp_path / 'source', tmp_path / 'dest')
    assert not outside.exists()


@pytest.mark.parametrize('fail_on', [1, 2])
def test_failed_copy_removes_its_partial_files_and_retry_can_recover(tmp_path, monkeypatch, fail_on):
    import errno
    import skodun.store as mod
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    copy_image = mod._copy_store_image
    copy_bytes = mod.shutil.copyfileobj
    calls = []
    def failing_bytes(source, target, **kwargs):
        calls.append(1)
        target.write(source.read(128))
        if len(calls) == fail_on:
            raise OSError(errno.ENOSPC, 'injected full disk')
        return copy_bytes(source, target, **kwargs)
    def failing_image(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(mod.shutil, 'copyfileobj', failing_bytes)
            return copy_image(*args, **kwargs)
    monkeypatch.setattr(mod, '_copy_store_image', failing_image)
    with pytest.raises(OSError):
        Store.open(db)
    assert len(calls) == fail_on
    assert not _quarantines(db)
    assert not list(tmp_path.glob(db.name + '.malformed-*'))
    assert db.read_bytes() == original
    monkeypatch.setattr(mod, '_copy_store_image', copy_image)
    recoveries = []
    def unavailable(*args):
        recoveries.append(1)
        return False
    monkeypatch.setattr(mod, '_recover_sqlite_image', unavailable)
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    assert recoveries == [1]
    assert len(_quarantines(db)) == 1
    assert _quarantines(db)[0].read_bytes() == original


def test_failed_copy_does_not_remove_a_preexisting_sidecar(tmp_path):
    import skodun.store as mod
    src, dest = tmp_path / 'source', tmp_path / 'quarantine'
    src.write_bytes(b'original')
    Path(str(src) + '-wal').write_bytes(b'wal')
    existing = Path(str(dest) + '-wal')
    existing.write_bytes(b'preexisting')
    with pytest.raises(FileExistsError):
        mod._copy_store_image(src, dest)
    assert not dest.exists()
    assert existing.read_bytes() == b'preexisting'


def test_failed_copy_preserves_a_replacement_at_the_created_path(tmp_path, monkeypatch):
    import skodun.store as mod
    src, dest = tmp_path / 'source', tmp_path / 'quarantine'
    src.write_bytes(b'original')
    def replace_then_fail(source, target, **kwargs):
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(b'preserve replacement')
        os.replace(replacement, dest)
        raise OSError('injected failure after replacement')
    monkeypatch.setattr(mod.shutil, 'copyfileobj', replace_then_fail)
    with pytest.raises(OSError):
        mod._copy_store_image(src, dest)
    assert dest.read_bytes() == b'preserve replacement'


@pytest.mark.parametrize('remove_source', [False, True])
def test_failed_copy_keeps_evidence_if_the_source_changed(tmp_path, monkeypatch, remove_source):
    import skodun.store as mod
    src, dest = tmp_path / 'source', tmp_path / 'quarantine'
    src.write_bytes(b'original evidence')
    def change_then_fail(source, target, **kwargs):
        target.write(source.read())
        if remove_source:
            src.unlink()
        else:
            src.write_bytes(b'new source')
        raise OSError('source changed during failure')
    monkeypatch.setattr(mod.shutil, 'copyfileobj', change_then_fail)
    with pytest.raises(OSError):
        mod._copy_store_image(src, dest)
    assert dest.read_bytes() == b'original evidence'
