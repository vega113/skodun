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


def _mock_recovery_output(monkeypatch, sql):
    import skodun.store as mod
    def produce(argv, **kwargs):
        assert 'capture_output' not in kwargs
        kwargs['stdout'].write(sql.encode('utf-8'))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(mod.subprocess, 'run', produce)


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
    _mock_recovery_output(monkeypatch, sql)
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
    _mock_recovery_output(monkeypatch, sql)
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
    _mock_recovery_output(monkeypatch, sql)
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


def _stream_source_dump(monkeypatch, source, *, truncate=False):
    import skodun.store as mod
    sizes = []
    def produce(argv, **kwargs):
        assert 'capture_output' not in kwargs
        assert os.fstat(kwargs['stdout'].fileno()).st_mode & 0o077 == 0
        assert os.fstat(kwargs['stderr'].fileno()).st_mode & 0o077 == 0
        count = kwargs['stdout'].write(b'.dbconfig defensive off\n')
        with closing(sqlite3.connect(source)) as conn:
            for line in conn.iterdump():
                if truncate and line == 'COMMIT;':
                    break
                count += kwargs['stdout'].write((line + '\n').encode())
        count += kwargs['stdout'].write(f'PRAGMA user_version={SCHEMA_VERSION};\n'.encode())
        sizes.append(count)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(mod.subprocess, 'run', produce)
    return sizes


def test_recovery_streams_many_statements_and_preserves_multiline_values(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    summary = 'first\n.keep this line\nlast\r\nend'
    with Store.open(source) as store:
        for i in range(100):
            store.save_review({**REC, 'id': f'row-{i}', 'summary': summary})
    sizes = _stream_source_dump(monkeypatch, source)
    monkeypatch.setattr(mod, '_RECOVERY_STATEMENT_LIMIT', 32 * 1024)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    assert sizes[0] > mod._RECOVERY_STATEMENT_LIMIT
    with closing(sqlite3.connect(dest)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 100
        assert conn.execute('SELECT summary FROM reviews LIMIT 1').fetchone()[0] == summary


def test_recovery_refuses_an_oversized_statement_without_replacing_source(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id': 'large-row', 'summary': 'x' * 65536})
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'normal.db')
    before = source.read_bytes()
    monkeypatch.setattr(mod, '_RECOVERY_STATEMENT_LIMIT', 32 * 1024)
    dest = tmp_path / 'refused.db'
    assert not mod._recover_sqlite_image(source, dest)
    assert not dest.exists()
    assert source.read_bytes() == before


def test_recovery_refuses_a_dump_with_an_unfinished_transaction(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'retained')
    _stream_source_dump(monkeypatch, source, truncate=True)
    dest = tmp_path / 'refused.db'
    assert not mod._recover_sqlite_image(source, dest)
    assert not dest.exists()


def test_changed_snapshot_version_is_retried_before_refusing_migration(tmp_path, monkeypatch):
    import skodun.store as mod
    db = tmp_path / 'current.db'
    _write_review(db, 'kept')
    real = mod._snapshot_database
    copies = []
    def racing_snapshot(path):
        result = real(path)
        _owner, image, error = result
        copies.append(image)
        if error is None and len(copies) == 1:
            # Model a copied v0 main header paired with a WAL checkpointed
            # after that copy. The live source is already at the current schema.
            with closing(sqlite3.connect(image)) as copied:
                copied.execute('PRAGMA user_version=0')
            with closing(sqlite3.connect(db)) as writer:
                writer.execute("UPDATE reviews SET summary='checkpoint completed' WHERE id='kept'")
                writer.commit()
        return result
    monkeypatch.setattr(mod, '_snapshot_database', racing_snapshot)
    with Store.open(db) as store:
        assert store._c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert len(copies) == 2


def test_recovery_rejects_orphaned_checkpoint_rows(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('PRAGMA foreign_keys=OFF')
        raw.execute("INSERT INTO review_checkpoints "
                    "(orchestration_id,pass_kind,pass_index,state,diff_hash,boundary_hash) "
                    "VALUES ('missing-parent','batch',1,'pending','d','b')")
        raw.commit()
        assert raw.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert raw.execute('PRAGMA foreign_key_check').fetchone() is not None
    before = source.read_bytes()
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert not mod._recover_sqlite_image(source, dest)
    assert not dest.exists()
    assert source.read_bytes() == before


def test_recovery_accepts_complete_parent_and_child_relationships(tmp_path, monkeypatch):
    import skodun.store as mod
    from tests.test_checkpoints import _created
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id': 'kept'})
        _created(store)
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with closing(sqlite3.connect(dest)) as restored:
        assert restored.execute('PRAGMA foreign_key_check').fetchone() is None
        assert restored.execute('SELECT COUNT(*) FROM review_checkpoints').fetchone()[0] == 2


@pytest.mark.parametrize('extra', ['trigger', 'table'])
def test_recovery_rejects_undeclared_schema_objects(tmp_path, monkeypatch, extra):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        if extra == 'trigger':
            raw.execute("CREATE TRIGGER deny_review BEFORE INSERT ON reviews "
                        "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        else:
            raw.execute('CREATE TABLE undeclared(value TEXT)')
        raw.commit()
        assert raw.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert raw.execute('PRAGMA foreign_key_check').fetchone() is None
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert not mod._recover_sqlite_image(source, dest)
    assert not dest.exists()


def test_recovery_schema_comparison_preserves_constraint_literal_case(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('PRAGMA writable_schema=ON')
        raw.execute("UPDATE sqlite_master SET sql=replace(sql, ?, ?) WHERE name='review_orchestrations'",
                    ("'active'", "'ACTIVE'"))
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('damage', ['malformed', 'not_object', 'id', 'axis', 'trust', 'index', 'duplicate', 'nan'])
def test_recovery_rejects_invalid_or_inconsistent_review_artifacts(tmp_path, monkeypatch, damage):
    import json
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        text = raw.execute('SELECT artifact_json FROM reviews').fetchone()[0]
        artifact = json.loads(text)
        if damage == 'malformed':
            text = '{'
        elif damage == 'not_object':
            text = '[]'
        elif damage == 'duplicate':
            text = text[:-1] + ', "id":"kept"}'
        elif damage == 'nan':
            text = text[:-1] + ', "extra":NaN}'
        elif damage == 'index':
            raw.execute("UPDATE reviews SET head='different-indexed-head'")
        else:
            if damage == 'id':
                artifact['id'] = 'different-id'
            elif damage == 'axis':
                artifact['parse_ok'] = 'false'
            else:
                artifact['trustworthy'] = not artifact['trustworthy']
            text = json.dumps(artifact)
        raw.execute('UPDATE reviews SET artifact_json=?', (text,))
        raw.commit()
    before = source.read_bytes()
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert not mod._recover_sqlite_image(source, dest)
    assert not dest.exists()
    assert source.read_bytes() == before


def test_recovery_preserves_valid_legacy_nullable_projections(tmp_path, monkeypatch):
    import json
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        artifact = json.loads(raw.execute('SELECT artifact_json FROM reviews').fetchone()[0])
        for name in ('review_started_at', 'review_completed_at', 'repo_id', 'terminal_reason', 'outcome'):
            artifact.pop(name, None)
            raw.execute(f'UPDATE reviews SET {name}=NULL')
        artifact.pop('context_hash', None)
        raw.execute('UPDATE reviews SET context_hash=NULL, artifact_json=?', (json.dumps(artifact),))
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as restored:
        assert restored.get_review('kept') == artifact


def _recovery_control_fixture(source):
    import json
    from tests.test_budget_store import begin, snapshot, NOW as budget_now
    from tests.test_checkpoints import _created, _identity, _payload, NOW, LATER
    from tests.test_evidence import identity, policy, receipt_mapping
    from skodun.checkpoints import CheckpointPayload
    from skodun.evidence import parse_receipt
    with Store.open(source) as store:
        store.save_review({**REC, 'id': 'kept'})
        _created(store)
        claim = store.claim_checkpoint('orch-1', _identity().pass_identities[0],
                                       owner='worker', now=NOW, lease_expires_at=LATER)
        assert store.complete_checkpoint('orch-1', 'batch', 1, owner='worker',
            claim_token=claim['claim_token'], fence=claim['fence'],
            payload=CheckpointPayload.from_mapping(_payload()), completed_at=NOW)
        rid, seq = begin(store)
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        request = store.get_request(rid)
        store.record_cancellation(target_id=rid, request=request, identity=request['identity'],
            actor='test', source='test', caller_pid=123, caller_worktree='/work',
            reason='recovery fixture audit', cause='requested_cancel', now=budget_now)
        assert store.finish_request(rid, owner_token='private-owner', state='failed',
            reason_code='test_failed', result={'status':4, 'text':'failed', 'metadata':{}}, now=budget_now)
        store.save_evidence_receipt(identity(), policy(),
            parse_receipt(json.dumps(receipt_mapping())).canonical_json, '2026-08-13T16:00:03Z')
        store.save_evidence_receipt(identity(), policy(),
            parse_receipt(json.dumps(receipt_mapping(counters={'checks':4}))).canonical_json,
            '2026-08-13T16:00:04Z')
    return rid


def test_recovery_preserves_readable_request_and_control_payloads(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    rid = _recovery_control_fixture(source)
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        request = store.get_request(rid)
        assert request['identity']['worktree_root'] == '/work'
        assert request['result']['status'] == 4
        assert request['cancellation']
        assert store.request_budget(rid)['timing']['total_ms'] == 10000


@pytest.mark.parametrize('table,column', [
    ('review_requests','identity_json'), ('review_requests','result_json'),
    ('review_orchestrations','identity_json'), ('review_checkpoints','payload_json'),
    ('cancellation_audit','identity_json'), ('request_budget_snapshots','snapshot_json'),
    ('evidence_receipts','receipt_json'), ('evidence_receipt_conflicts','receipt_json'),
])
def test_recovery_rejects_malformed_payloads_across_store_surfaces(tmp_path, monkeypatch, table, column):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with closing(sqlite3.connect(source)) as raw:
        raw.execute(f'UPDATE {table} SET {column}=?', ('{',))
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('damage', ['scope', 'pid', 'execution', 'result_shape', 'orchestration_shape', 'checkpoint_shape'])
def test_recovery_rejects_invalid_request_and_checkpoint_invariants(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with closing(sqlite3.connect(source)) as raw:
        if damage == 'scope':
            raw.execute("UPDATE review_requests SET identity_json='{\"worktree_root\":\"/wrong\"}'")
        elif damage == 'pid':
            raw.execute('UPDATE review_requests SET pid=0')
        elif damage == 'execution':
            raw.execute("UPDATE request_executions SET owner_token='different-owner'")
        elif damage == 'result_shape':
            raw.execute("UPDATE review_requests SET result_json='{}'")
        elif damage == 'orchestration_shape':
            raw.execute("UPDATE review_orchestrations SET identity_json='{}'")
        else:
            raw.execute("UPDATE review_checkpoints SET payload_json='{}' WHERE state='complete'")
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_refuses_request_owner_that_matches_only_an_older_execution(tmp_path, monkeypatch):
    import skodun.store as mod
    from tests.test_budget_store import begin, resume
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id': 'kept'})
        rid, seq = begin(store)
        resume(store, rid, seq)
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'valid.db')
    with closing(sqlite3.connect(source)) as raw:
        raw.execute("UPDATE review_requests SET owner_token='private-owner',pid=123,source='cli' WHERE id=?", (rid,))
        raw.commit()
    assert not mod._recover_sqlite_image(source, tmp_path / 'invalid.db')


@pytest.mark.parametrize('damage', ['accepted_reason', 'accepted_identity', 'rejected_ok', 'rejected_unknown'])
def test_recovery_rejects_inconsistent_receipt_decisions(tmp_path, monkeypatch, damage):
    import json
    from dataclasses import replace
    import skodun.store as mod
    from skodun.evidence import parse_receipt
    from tests.test_evidence import identity, policy, receipt_mapping
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id':'kept'})
        receipt = parse_receipt(json.dumps(receipt_mapping()))
        store.save_evidence_receipt(identity(), policy(), receipt.canonical_json, '2026-08-13T16:00:03Z')
    with closing(sqlite3.connect(source)) as raw:
        if damage == 'accepted_reason':
            raw.execute("UPDATE evidence_receipts SET reason_code='policy_mismatch'")
        elif damage == 'accepted_identity':
            raw.execute('UPDATE evidence_receipts SET identity_digest=?',
                        (replace(identity(), worktree_root='/another').digest,))
        elif damage == 'rejected_ok':
            raw.execute("UPDATE evidence_receipts SET status='rejected',reason_code='ok'")
        else:
            raw.execute("UPDATE evidence_receipts SET status='rejected',reason_code='invented'")
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_preserves_a_valid_identity_mismatch_rejection(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    import skodun.store as mod
    from skodun.evidence import parse_receipt
    from tests.test_evidence import identity, policy, receipt_mapping
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id':'kept'})
        receipt = parse_receipt(json.dumps(receipt_mapping()))
        result = store.save_evidence_receipt(replace(identity(), worktree_root='/another'),
            policy(), receipt.canonical_json, '2026-08-13T16:00:03Z')
        assert result['status'] == 'rejected' and result['reason_code'] == 'worktree_mismatch'
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_rejects_a_failed_producer_relabelled_accepted(tmp_path, monkeypatch):
    import json
    import skodun.store as mod
    from skodun.evidence import parse_receipt
    from tests.test_evidence import identity, policy, receipt_mapping
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id':'kept'})
        receipt = parse_receipt(json.dumps(receipt_mapping(exit_code=1, terminal_state='failed')))
        result = store.save_evidence_receipt(identity(), policy(), receipt.canonical_json, '2026-08-13T16:00:03Z')
        assert result['reason_code'] == 'producer_failed'
        store._c.execute("UPDATE evidence_receipts SET status='accepted',reason_code='ok'")
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_rejects_an_invalid_conflict_projection(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with closing(sqlite3.connect(source)) as raw:
        raw.execute("UPDATE evidence_receipt_conflicts SET reason_code='ok'")
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('damage', ['missing_one', 'missing_all', 'extra', 'diff', 'boundary', 'prompt'])
def test_recovery_requires_complete_checkpoint_plan_projection(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with closing(sqlite3.connect(source)) as raw:
        if damage == 'missing_one':
            raw.execute("DELETE FROM review_checkpoints WHERE pass_kind='batch'")
        elif damage == 'missing_all':
            raw.execute('DELETE FROM review_checkpoints')
        elif damage == 'extra':
            raw.execute("INSERT INTO review_checkpoints "
                        "(orchestration_id,pass_kind,pass_index,state,diff_hash,boundary_hash) "
                        "VALUES ('orch-1','batch',99,'pending','d','b')")
        else:
            column = {'diff':'diff_hash', 'boundary':'boundary_hash', 'prompt':'prompt_hash'}[damage]
            raw.execute(f"UPDATE review_checkpoints SET {column}='changed' WHERE pass_kind='batch'")
        raw.commit()
        assert raw.execute('PRAGMA foreign_key_check').fetchone() is None
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_preserves_expired_checkpoint_envelopes(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with Store.open(source) as store:
        assert store.expire_orchestrations(now='2026-09-06T00:00:00Z') == 1
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert store.get_orchestration('orch-1')['state'] == 'expired'
        assert all(row['payload_json'] is None for row in store.list_checkpoints('orch-1'))


def test_recovery_accepts_runtime_bound_integration_prompt(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_checkpoints import _identity, NOW, LATER
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with Store.open(source) as store:
        store.claim_checkpoint('orch-1', replace(_identity().pass_identities[1], prompt_hash='z'*64),
                               owner='integrator', now=NOW, lease_expires_at=LATER)
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def _recovery_triage_fixture(source, event='defer'):
    from skodun import triage
    from tests.test_store import _a_finding
    with Store.open(source) as store:
        store.save_review({**REC, 'id':'kept', 'status':'findings', 'findings_total':1,
                           'findings':[_a_finding()], 'severity':{'high':1,'medium':0,'low':0}})
        record = store.get_review('kept')
        reason = 'This finding is tracked in a separate reviewed change.'
        if event == 'defer':
            decision = triage.defer(store, record, 0, '#42', reason, '2026-09-06T00:00:00Z')
        else:
            decision = triage.dismiss(store, record, 0, reason, 'legacy timestamp')
    return decision


@pytest.mark.parametrize('damage', ['missing_ref', 'placeholder_ref', 'short_reason', 'missing_reason', 'key', 'scope'])
def test_recovery_rejects_unauditable_triage_decisions(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_triage_fixture(source)
    with closing(sqlite3.connect(source)) as raw:
        sql = {
            'missing_ref': 'UPDATE triage_events SET tracking_ref=NULL',
            'placeholder_ref': "UPDATE triage_events SET tracking_ref='#'",
            'short_reason': "UPDATE triage_events SET reason='TODO'",
            'missing_reason': 'UPDATE triage_events SET reason=NULL',
            'key': "UPDATE triage_events SET finding_key='wrong'",
            'scope': "UPDATE triage_events SET base_sha='wrong'",
        }[damage]
        raw.execute(sql)
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('event', ['dismiss', 'defer'])
def test_recovery_rejects_triage_without_its_audit_review(tmp_path, monkeypatch, event):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_triage_fixture(source, event)
    with Store.open(source) as store:
        store.save_review({**store.get_review('kept'), 'id': 'surviving'})
    with closing(sqlite3.connect(source)) as raw:
        raw.execute("DELETE FROM reviews WHERE id='kept'")
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('event', ['dismiss', 'defer'])
def test_recovery_rejects_triage_without_its_audit_finding(tmp_path, monkeypatch, event):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_triage_fixture(source, event)
    with Store.open(source) as store:
        store.save_review({**store.get_review('kept'), 'id': 'surviving'})
        store.save_review({**REC, 'id': 'kept'})
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('event', ['dismiss', 'defer'])
def test_recovery_preserves_valid_triage_and_its_audit_history(tmp_path, monkeypatch, event):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    decision = _recovery_triage_fixture(source, event)
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert len(store.triage_for(REC['branch'], REC['base_sha'])) == 1
        assert len(store.triage_history(decision['ledger_key'])) == 1


@pytest.mark.parametrize('damage', [
    'claim_token', 'claim_owner', 'claimed_at', 'lease_expires_at',
    'empty_owner', 'zero_fence', 'backward_lease', 'payload', 'completed_at',
    'failure_reason', 'pending_claim', 'complete_time',
    'beyond_parent', 'future_claim',
])
def test_recovery_rejects_inconsistent_checkpoint_claims(tmp_path, monkeypatch, damage):
    from dataclasses import replace
    from tests.test_checkpoints import _identity, NOW, LATER
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with Store.open(source) as store:
        store.claim_checkpoint('orch-1', replace(_identity().pass_identities[1], prompt_hash='z'*64),
                               owner='integrator', now=NOW, lease_expires_at=LATER)
    with closing(sqlite3.connect(source)) as raw:
        if damage in ('claim_token', 'claim_owner', 'claimed_at', 'lease_expires_at'):
            raw.execute(f"UPDATE review_checkpoints SET {damage}=NULL WHERE state='running'")
        else:
            sql = {
                'empty_owner': "UPDATE review_checkpoints SET claim_owner='' WHERE state='running'",
                'zero_fence': "UPDATE review_checkpoints SET fence=0 WHERE state='running'",
                'backward_lease': "UPDATE review_checkpoints SET lease_expires_at=claimed_at WHERE state='running'",
                'payload': "UPDATE review_checkpoints SET payload_json=(SELECT payload_json FROM review_checkpoints WHERE state='complete') WHERE state='running'",
                'completed_at': "UPDATE review_checkpoints SET completed_at=claimed_at WHERE state='running'",
                'failure_reason': "UPDATE review_checkpoints SET failure_reason='stale failure' WHERE state='running'",
                'pending_claim': "UPDATE review_checkpoints SET state='pending' WHERE state='running'",
                'complete_time': "UPDATE review_checkpoints SET completed_at=NULL WHERE state='complete'",
                'beyond_parent': "UPDATE review_checkpoints SET lease_expires_at='9999-12-31T23:59:59Z' WHERE state='running'",
                'future_claim': "UPDATE review_checkpoints SET claimed_at='9999-12-31T23:59:58Z',lease_expires_at='9999-12-31T23:59:59Z' WHERE state='running'",
            }[damage]
            raw.execute(sql)
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('release', [False, True])
def test_recovery_preserves_writer_produced_checkpoint_claims(tmp_path, monkeypatch, release):
    from dataclasses import replace
    from tests.test_checkpoints import _identity, NOW, LATER
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with Store.open(source) as store:
        claim = store.claim_checkpoint('orch-1', replace(_identity().pass_identities[1], prompt_hash='z'*64),
                                       owner='integrator', now=NOW, lease_expires_at=LATER)
        if release:
            assert store.release_checkpoint('orch-1', 'integration', claim['pass_index'],
                owner='integrator', claim_token=claim['claim_token'], fence=claim['fence'],
                reason='retry this pass', at=NOW)
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('damage', [
    'negative_cost', 'infinite_cost', 'text_cost', 'timestamp', 'future_timestamp', 'provider',
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'fractional_tokens', 'total_relationship',
])
def test_recovery_rejects_invalid_spend_ledger(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        store.api_spend_append(at='2026-09-07T00:00:00Z', provider='openai-api', model='model',
            prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.25)
    column, value = {
        'negative_cost': ('cost_usd', -100), 'infinite_cost': ('cost_usd', float('inf')),
        'text_cost': ('cost_usd', 'not a cost'), 'timestamp': ('at', 'not a timestamp'),
        'future_timestamp': ('at', '9999-12-31T23:59:59Z'),
        'provider': ('provider', ''), 'prompt_tokens': ('prompt_tokens', -1),
        'completion_tokens': ('completion_tokens', -1), 'total_tokens': ('total_tokens', -1),
        'fractional_tokens': ('prompt_tokens', 1.5), 'total_relationship': ('total_tokens', 14),
    }[damage]
    with closing(sqlite3.connect(source)) as raw:
        raw.execute(f'UPDATE api_spend_events SET {column}=?', (value,))
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_preserves_daily_spend_total(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        for cost in (0.0, 0.25, 1.5):
            store.api_spend_append(at='2026-09-07T00:00:00Z', provider='openai-api', model='model',
                prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=cost)
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert store.api_spend_sum_usd('openai-api', day_prefix='2026-09-07') == 1.75


@pytest.mark.parametrize('damage', [
    'queued_at', 'pid', 'owner', 'owner_without_pid', 'status', 'scope',
    'admitted_at', 'started_at', 'ended_at', 'wait_ms',
    'future_holder', 'backward_admission', 'backward_start',
])
def test_recovery_rejects_invalid_capacity_holder(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*', pid=123)
        assert store.capacity_try_admit('ticket', capacity=1)
        store.capacity_mark_started('ticket')
    assignments = {
        'queued_at': "queued_at='broken',pid=NULL,owner_start=NULL", 'pid': "pid='broken'",
        'owner': "owner_start=''", 'owner_without_pid': "pid=NULL,owner_start='birth-token'",
        'status': "status='unknown'", 'scope': "scope='wrong'", 'admitted_at': 'admitted_at=NULL',
        'started_at': 'started_at=NULL', 'ended_at': 'ended_at=queued_at', 'wait_ms': 'wait_ms=-1',
        'future_holder': "pid=NULL,owner_start=NULL,queued_at='9999-12-31T23:59:59Z',admitted_at='9999-12-31T23:59:59Z',started_at='9999-12-31T23:59:59Z'",
        'backward_admission': "admitted_at='2000-01-01T00:00:00Z'",
        'backward_start': "started_at='2000-01-01T00:00:00Z'",
    }
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('UPDATE capacity_admissions SET ' + assignments[damage])
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('state', ['queued', 'admitted', 'running', 'released', 'expired', 'rejected'])
def test_recovery_preserves_capacity_lifecycle_states(tmp_path, monkeypatch, state):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*')
        if state in ('admitted', 'running', 'released'):
            assert store.capacity_try_admit('ticket', capacity=1)
        if state in ('running', 'released'):
            store.capacity_mark_started('ticket')
        if state in ('released', 'expired', 'rejected'):
            store.capacity_finish('ticket', status=state)
        expected = store.capacity_get('ticket')
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert store.capacity_get('ticket') == expected


@pytest.mark.parametrize('damage', ['future_ttl', 'future_recorded', 'backward_ttl', 'timestamp', 'provider', 'reason', 'category'])
def test_recovery_rejects_invalid_provider_blackouts(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        store.mark_provider_unavailable('openai', 'quota exhausted', 'quota',
            '2026-09-07T00:30:00Z', recorded_at='2026-09-07T00:00:00Z')
    assignments = {
        'future_ttl': "unavailable_until='9999-12-31T23:59:59Z'",
        'future_recorded': "recorded_at='9999-12-31T23:30:00Z',unavailable_until='9999-12-31T23:59:59Z'",
        'backward_ttl': "unavailable_until='2026-09-06T00:00:00Z'",
        'timestamp': "recorded_at='broken'", 'provider': "provider=''",
        'reason': 'reason=NULL', 'category': "category=''",
    }
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('UPDATE provider_state SET ' + assignments[damage])
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_preserves_provider_blackout(tmp_path, monkeypatch):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        store.mark_provider_unavailable('openai', 'quota exhausted', 'quota',
            '2026-09-07T00:30:00Z', recorded_at='2026-09-07T00:00:00Z')
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert store.provider_unavailable_reason('openai', '2026-09-07T00:10:00Z', env={}) == 'quota exhausted'
        assert store.provider_unavailable_reason('openai', '2026-09-07T00:30:00Z', env={}) is None


@pytest.mark.parametrize('damage', ['timestamp', 'future', 'channel', 'orphan', 'ineligible'])
def test_recovery_rejects_invalid_delivery_acknowledgements(tmp_path, monkeypatch, damage):
    from skodun import delivery
    from tests.test_delivery import _rec
    import skodun.store as mod
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review(_rec())
        delivery.acknowledge(store, ['sk_1'], 'mcp', now='2026-09-07T00:00:00Z')
        if damage == 'ineligible':
            store.save_review(_rec(mode='foreground'))
    if damage != 'ineligible':
        field, value = {
            'timestamp': ('delivered_at', 'broken'), 'future': ('delivered_at', '9999-12-31T23:59:59Z'),
            'channel': ('channel', 'unknown'), 'orphan': ('review_id', 'missing'),
        }[damage]
        with closing(sqlite3.connect(source)) as raw:
            raw.execute(f'UPDATE deliveries SET {field}=?', (value,))
            raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('channel', ['cli-text', 'cli-claude', 'mcp', 'quiet'])
def test_recovery_preserves_valid_delivery_acknowledgements(tmp_path, monkeypatch, channel):
    from skodun import delivery
    from tests.test_delivery import _rec, REPO_A
    import skodun.store as mod
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review(_rec())
        delivery.acknowledge(store, ['sk_1'], channel, now='2026-09-07T00:00:00Z')
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert delivery.undelivered(store, 'b', REPO_A) == []


@pytest.mark.parametrize('damage', ['far_future_expiry', 'future_update', 'backward_update', 'before_creation', 'invalid_time'])
def test_recovery_rejects_invalid_orchestration_lifecycle(tmp_path, monkeypatch, damage):
    from dataclasses import replace
    from tests.test_checkpoints import _identity, NOW, LATER
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    with Store.open(source) as store:
        store.claim_checkpoint('orch-1', replace(_identity().pass_identities[1], prompt_hash='z'*64),
                               owner='integrator', now=NOW, lease_expires_at=LATER)
    assignments = {
        'far_future_expiry': "expires_at='9999-12-31T23:59:59Z'",
        'future_update': "updated_at='9999-12-31T23:59:58Z',expires_at='9999-12-31T23:59:59Z'",
        'backward_update': "updated_at='2000-01-01T00:00:00Z'",
        'before_creation': 'expires_at=created_at', 'invalid_time': "created_at='broken'",
    }
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('UPDATE review_orchestrations SET ' + assignments[damage])
        if damage in ('far_future_expiry', 'future_update'):
            raw.execute("UPDATE review_checkpoints SET lease_expires_at='9999-12-31T23:59:59Z' WHERE state='running'")
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('damage', ['revived', 'outcome', 'timestamp', 'token', 'request', 'identity', 'pid'])
def test_recovery_rejects_invalid_cancellation_audit(tmp_path, monkeypatch, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_control_fixture(source)
    assignments = {
        'revived': "outcome='requested'", 'outcome': "outcome='unknown'",
        'timestamp': "completed_at='broken'", 'token': "execution_token='missing'",
        'request': "request_id='missing'", 'identity': "identity_json='{}'", 'pid': 'caller_pid=-1',
    }
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('UPDATE cancellation_audit SET ' + assignments[damage])
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('complete', [False, True])
def test_recovery_preserves_request_cancellation_state(tmp_path, monkeypatch, complete):
    from tests.test_budget_store import begin, NOW
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        rid, _ = begin(store)
        request = store.get_request(rid)
        store.record_cancellation(target_id=rid, request=request,
            identity={**request['identity'], 'request_id':rid}, actor='test', source='test',
            caller_pid=123, caller_worktree='/work', reason='cancel this review',
            cause='requested_cancel', now=NOW)
        if complete:
            assert store.finish_request(rid, owner_token=request['owner_token'], state='cancelled',
                reason_code='requested_cancel', result={'status':130,'text':'cancelled','metadata':{}}, now=NOW)
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert bool(store.request_cancel_event(rid, request['owner_token'])) is (not complete)


def test_recovery_restores_wal_and_keeps_authority_inode(tmp_path, monkeypatch):
    import skodun.store as mod
    good = tmp_path / 'good.db'
    _write_review(good, 'retained')
    db = tmp_path / 'broken.db'
    _make_torn_wal(db)
    inode = db.stat().st_ino
    _stream_source_dump(monkeypatch, good)
    # A connection which has not accessed the file must resume on the same
    # authority after recovery, not write to an unlinked replacement inode.
    with closing(sqlite3.connect(db)) as idle:
        with Store.open(db) as store:
            assert store._c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.stat().st_ino == inode
        idle.execute("UPDATE reviews SET summary='peer resumed' WHERE id='retained'")
        idle.commit()
    with Store.open(db) as store:
        assert store._c.execute("SELECT summary FROM reviews WHERE id='retained'").fetchone()[0] == 'peer resumed'


def test_recovery_refuses_existing_sqlite_reader(tmp_path, monkeypatch):
    good = tmp_path / 'good.db'
    _write_review(good, 'retained')
    db = tmp_path / 'broken.db'
    original = _make_torn_wal(db)
    inode = db.stat().st_ino
    _stream_source_dump(monkeypatch, good)
    with closing(sqlite3.connect(db)) as reader:
        reader.execute('PRAGMA user_version').fetchone()
        with pytest.raises(SchemaLifecycleError) as caught:
            Store.open(db)
        assert caught.value.reason_code == 'busy'
        assert db.stat().st_ino == inode
        assert db.read_bytes() == original
    with Store.open(db) as store:
        assert store.get_review('retained') is not None


@pytest.mark.parametrize('missing', ['latest', 'middle', 'all'])
def test_recovery_rejects_missing_triage_sequence_entries(tmp_path, monkeypatch, missing):
    from skodun import triage
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _recovery_triage_fixture(source, 'dismiss')
    with Store.open(source) as store:
        review = store.get_review('kept')
        triage.reopen(store, review, 0, 'This finding still needs a reviewed correction.', '2026-09-07T00:00:00Z')
        if missing == 'middle':
            triage.dismiss(store, review, 0, 'The correction has now been independently verified.', '2026-09-07T00:01:00Z')
    with closing(sqlite3.connect(source)) as raw:
        raw.execute('DELETE FROM triage_events' if missing == 'all' else 'DELETE FROM triage_events WHERE seq=2')
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


def test_recovery_preserves_complete_reopened_triage_history(tmp_path, monkeypatch):
    from skodun import triage
    import skodun.store as mod
    source = tmp_path / 'source.db'
    decision = _recovery_triage_fixture(source, 'dismiss')
    with Store.open(source) as store:
        triage.reopen(store, store.get_review('kept'), 0,
            'This finding still needs a reviewed correction.', '2026-09-07T00:00:00Z')
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest)
    with Store.open(dest) as store:
        assert store.triage_for(REC['branch'], REC['base_sha']) == {}
        assert len(store.triage_history(decision['ledger_key'])) == 2


@pytest.mark.parametrize('token,observed,expected', [
    ('garbage', 'Mon Sep 07 07:00:00 2026', False),
    ('Mon Sep 07 06:00:00 2026', 'Mon Sep 07 07:00:00 2026', False),
    ('Mon Sep 07 07:00:00 2026', None, False),
    ('Mon Sep 07 07:00:00 2026', 'Mon Sep 07 07:00:00 2026', True),
    ('linux:00000000-0000-0000-0000-000000000001:123', 'linux:00000000-0000-0000-0000-000000000001:123', True),
    ('linux:broken:123', None, False),
])
def test_recovery_validates_live_capacity_birth_identity(tmp_path, monkeypatch, token, observed, expected):
    from skodun import capacity
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    monkeypatch.setattr(capacity, 'process_birth_token', lambda pid: token)
    with Store.open(source) as store:
        store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*', pid=123)
        assert store.capacity_try_admit('ticket', capacity=1)
        store.capacity_mark_started('ticket')
    monkeypatch.setattr(capacity, 'process_observation', lambda pid: capacity.ProcessObservation(observed))
    monkeypatch.setattr(capacity, 'pid_alive', lambda pid: True)
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db') is expected


@pytest.mark.parametrize('findings,total', [('oops', 0), ([42], 1), ([], 1), ([], False)])
def test_recovery_rejects_malformed_complete_artifacts(tmp_path, monkeypatch, findings, total):
    import json
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with closing(sqlite3.connect(source)) as raw:
        artifact = json.loads(raw.execute('SELECT artifact_json FROM reviews').fetchone()[0])
        artifact['findings'], artifact['findings_total'] = findings, total
        raw.execute('UPDATE reviews SET artifact_json=?,findings_total=?', (json.dumps(artifact), int(total)))
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('missing', ['latest', 'middle', 'all'])
def test_recovery_rejects_missing_spend_sequence_entries(tmp_path, monkeypatch, missing):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        for cost in (1.0, 9.0, 0.5):
            store.api_spend_append(at='2026-09-07T00:00:00Z', provider='openai-api', model='model',
                prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=cost)
    with closing(sqlite3.connect(source)) as raw:
        raw.execute({'latest': 'DELETE FROM api_spend_events WHERE seq=3',
                     'middle': 'DELETE FROM api_spend_events WHERE seq=2',
                     'all': 'DELETE FROM api_spend_events'}[missing])
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('missing', ['latest', 'middle', 'all'])
def test_recovery_rejects_missing_cancellation_sequence_entries(tmp_path, monkeypatch, missing):
    from tests.test_budget_store import begin, NOW
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        rid, _ = begin(store)
        request = store.get_request(rid)
        for _ in range(3):
            store.record_cancellation(target_id=rid, request=request,
                identity={**request['identity'], 'request_id':rid}, actor='test', source='test',
                caller_pid=123, caller_worktree='/work', reason='cancel this review',
                cause='requested_cancel', now=NOW)
    with closing(sqlite3.connect(source)) as raw:
        raw.execute({'latest': 'DELETE FROM cancellation_audit WHERE id=3',
                     'middle': 'DELETE FROM cancellation_audit WHERE id=2',
                     'all': 'DELETE FROM cancellation_audit'}[missing])
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('holder', ['missing', 'wrong_review', 'wrong_pid', 'released', 'matching'])
def test_recovery_accounts_for_running_review_capacity(tmp_path, monkeypatch, holder):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review({**REC, 'id':'running-review', 'status':'running', 'pid':123})
        if holder != 'missing':
            store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*')
            assert store.capacity_try_admit('ticket', capacity=1)
            store.capacity_mark_started('ticket', review_id='other' if holder == 'wrong_review' else 'running-review')
            store._c.execute('UPDATE capacity_admissions SET pid=?', (456 if holder == 'wrong_pid' else 123,))
            if holder == 'released':
                store.capacity_finish('ticket', status='released')
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db') is (holder == 'matching')


@pytest.mark.parametrize('holder', ['missing', 'unlinked', 'wrong_pid', 'released', 'matching', 'queued_ticket'])
@pytest.mark.parametrize('state', ['queued', 'running'])
def test_recovery_accounts_for_running_request_capacity(tmp_path, monkeypatch, holder, state):
    from tests.test_budget_store import begin, NOW
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        rid, _ = begin(store)
        request = store.get_request(rid)
        assert store.advance_request(rid, owner_token=request['owner_token'], state=state, now=NOW)
        if holder != 'missing':
            store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*')
            if holder != 'queued_ticket':
                assert store.capacity_try_admit('ticket', capacity=1)
            store._c.execute('UPDATE capacity_admissions SET pid=?',
                             (request['pid'] + 1 if holder == 'wrong_pid' else request['pid'],))
            if holder != 'unlinked':
                store.link_request(rid, 'capacity', 'ticket')
            if holder == 'released':
                store.capacity_finish('ticket', status='released')
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db') is (
        holder == 'matching' or holder == 'queued_ticket' and state == 'queued')


def test_recovery_preserves_foreground_parent_review_link(tmp_path, monkeypatch):
    from skodun import capacity
    import skodun.store as mod
    source = tmp_path / 'source.db'
    monkeypatch.setattr(capacity, 'process_birth_token', lambda pid: None)
    with Store.open(source) as store:
        ticket = capacity.acquire_for_fg(store, scope='/repo', capacity=1, machine_capacity=1,
                                         wait_sec=.1, poll_sec=.01)
        store.save_review({**REC, 'id':'running-review', 'status':'running', 'pid':os.getpid()})
        capacity.mark_started(store, ticket, review_id='running-review')
        assert store.capacity_get(ticket.parent.id)['review_id'] == 'running-review'
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('state', ['queued', 'admitted', 'running'])
@pytest.mark.parametrize('same_pid', [False, True])
def test_recovery_preserves_background_prelink_capacity(tmp_path, monkeypatch, state, same_pid):
    from tests.test_delivery import _rec
    import skodun.store as mod
    source = tmp_path / 'source.db'
    with Store.open(source) as store:
        store.save_review(_rec(id='background', status='running', pid=123))
        store.capacity_enqueue(admission_id='ticket', resource_class='review-machine', scope='*')
        if state in ('admitted', 'running'):
            assert store.capacity_try_admit('ticket', capacity=1)
        if state == 'running':
            store.capacity_mark_started('ticket')
        store._c.execute('UPDATE capacity_admissions SET pid=?', (123 if same_pid else 456,))
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db') is same_pid


@pytest.mark.parametrize('missing', ['latest', 'middle', 'all'])
def test_recovery_rejects_missing_request_execution_sequences(tmp_path, monkeypatch, missing):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        for i in range(1, 4):
            store.begin_request(request_id=f'request-{i}', scope='/work', request_key=f'key-{i}',
                identity={'worktree_root':'/work'}, intent={}, owner_token=f'owner-{i}', pid=123,
                source='cli', now='2026-09-06T00:00:00Z', expires_at='2026-09-07T00:00:00Z')
    with closing(sqlite3.connect(source)) as raw:
        ids = [1,2,3] if missing == 'all' else [3 if missing == 'latest' else 2]
        for i in ids:
            raw.execute('DELETE FROM request_executions WHERE request_id=?', (f'request-{i}',))
            raw.execute('DELETE FROM review_requests WHERE id=?', (f'request-{i}',))
        raw.commit()
        assert raw.execute('PRAGMA foreign_key_check').fetchone() is None
    _stream_source_dump(monkeypatch, source)
    assert not mod._recover_sqlite_image(source, tmp_path / 'recovered.db')


@pytest.mark.parametrize('children,remove_parent', [(1, True), (2, True), (2, False)])
def test_recovery_accounts_for_foreground_machine_parents(tmp_path, monkeypatch, children, remove_parent):
    from skodun import capacity
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    monkeypatch.setattr(capacity, 'process_birth_token', lambda pid: None)
    with Store.open(source) as store:
        for i in range(children):
            ticket = capacity.acquire_for_fg(store, scope=f'/repo-{i}', capacity=2,
                machine_capacity=2, wait_sec=.1, poll_sec=.01)
        if remove_parent:
            store._c.execute('DELETE FROM capacity_admissions WHERE id=?', (ticket.parent.id,))
    _stream_source_dump(monkeypatch, source)
    assert mod._recover_sqlite_image(source, tmp_path / 'recovered.db') is (not remove_parent)


@pytest.mark.parametrize('table', ['feedback_events', 'reuse_events'])
@pytest.mark.parametrize('damage', ['none', 'latest', 'middle', 'all', 'invalid_body'])
def test_recovery_preserves_complete_operator_audit_streams(tmp_path, monkeypatch, table, damage):
    import skodun.store as mod
    source = tmp_path / 'source.db'
    _write_review(source, 'kept')
    with Store.open(source) as store:
        for _ in range(3):
            if table == 'feedback_events':
                store.feedback_append(at='2026-09-07T00:00:00Z', actor='human', kind='product_note',
                                      body='Please preserve this operator audit note.')
            else:
                store.append_reuse_event(at='2026-09-07T00:00:00Z', outcome='miss', reason='no matching review')
    with closing(sqlite3.connect(source)) as raw:
        if damage == 'invalid_body':
            field = 'body' if table == 'feedback_events' else 'reason'
            raw.execute(f"UPDATE {table} SET {field}=''")
        elif damage != 'none':
            suffix = '' if damage == 'all' else f" WHERE seq={3 if damage == 'latest' else 2}"
            raw.execute(f'DELETE FROM {table}' + suffix)
        raw.commit()
    _stream_source_dump(monkeypatch, source)
    dest = tmp_path / 'recovered.db'
    assert mod._recover_sqlite_image(source, dest) is (damage == 'none')
    if damage == 'none':
        with Store.open(dest) as store:
            rows = store.feedback_list() if table == 'feedback_events' else store.reuse_events()
            assert len(rows) == 3
