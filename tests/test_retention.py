"""Retention: worker-log pruning keeps gate artifacts untouched."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from skodun.config import Retention, load_config
from skodun.retention import plan_worker_log_prunes, retain_worker_logs
from skodun.store import Store


def _touch(path: Path, *, mtime: float) -> Path:
    path.write_text("log\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_plan_age_and_count_bounds(tmp_path):
    log_dir = tmp_path / "db.logs"
    log_dir.mkdir()
    now = 1_700_000_000.0
    day = 86400.0
    old = _touch(log_dir / "old.log", mtime=now - 40 * day)
    mid = _touch(log_dir / "mid.log", mtime=now - 10 * day)
    new = _touch(log_dir / "new.log", mtime=now - 1 * day)
    # Non-log ignored
    (log_dir / "notes.txt").write_text("x", encoding="utf-8")

    by_age = plan_worker_log_prunes(
        log_dir, max_age_days=30, max_count=0, now=now)
    assert set(by_age) == {old}

    # max_count=2 keeps the two newest (mid, new); old is excess.
    by_count = plan_worker_log_prunes(
        log_dir, max_age_days=0, max_count=2, now=now)
    assert set(by_count) == {old}

    both = plan_worker_log_prunes(
        log_dir, max_age_days=5, max_count=2, now=now)
    # age kills old+mid; count alone would kill old only — union is old+mid
    assert set(both) == {old, mid}
    assert new not in both


def test_apply_deletes_and_dry_run(tmp_path):
    log_dir = tmp_path / "db.logs"
    log_dir.mkdir()
    now = time.time()
    victim = _touch(log_dir / "gone.log", mtime=now - 90 * 86400)
    keep = _touch(log_dir / "keep.log", mtime=now)

    dry = retain_worker_logs(
        log_dir, max_age_days=30, max_count=0, dry_run=True, now=now)
    assert dry.dry_run and victim in dry.candidates
    assert victim.exists() and keep.exists()

    real = retain_worker_logs(
        log_dir, max_age_days=30, max_count=0, dry_run=False, now=now)
    assert not real.dry_run
    assert victim in real.deleted
    assert not victim.exists()
    assert keep.exists()


def test_retain_never_touches_review_rows(tmp_path):
    """Gate identity lives in SQLite; log prune must leave it readable."""
    from tests.test_store import REC

    db = tmp_path / "skodun.db"
    with Store.open(db) as st:
        st.save_review({**REC, "id": "rev-retain-1"})
        log_dir = st.log_dir()
        now = time.time()
        victim = _touch(log_dir / "rev-retain-1.log", mtime=now - 90 * 86400)
        retain_worker_logs(log_dir, max_age_days=7, max_count=0, now=now)
        assert not victim.exists()
        got = st.get_review("rev-retain-1")
        assert got is not None
        assert got["id"] == "rev-retain-1"
        assert got.get("trustworthy") is True


def test_retention_config_loads(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(
        "[retention]\nworker_log_max_age_days = 7\nworker_log_max_count = 10\n",
        encoding="utf-8",
    )
    cfg = load_config(None, global_path=g)
    assert cfg.retention == Retention(
        worker_log_max_age_days=7, worker_log_max_count=10)


def test_retention_defaults_when_absent(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text("", encoding="utf-8")
    cfg = load_config(None, global_path=g)
    assert cfg.retention == Retention()


def test_cli_retain_dry_run(tmp_path, monkeypatch):
    from skodun.cli import main
    import subprocess

    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "missing.toml"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    with Store.open(db) as st:
        log_dir = st.log_dir()
        now = time.time()
        _touch(log_dir / "x.log", mtime=now - 90 * 86400)
    code = main(["retain", "--repo", str(repo), "--dry-run"])
    assert code == 0
