"""Tests for `skodun doctor` — real CLI entry + report shape."""

from __future__ import annotations

import os
from pathlib import Path

from skodun.cli import main
from skodun.doctor import run_doctor
from skodun.store import SCHEMA_VERSION, Store


def test_run_doctor_reports_store_and_adapters(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "nope.toml"))
    with Store.open(db) as st:
        assert st is not None
    report = run_doctor(repo=tmp_path, store_path=db)
    names = {c.name for c in report.checks}
    assert "python" in names
    assert "store" in names
    assert "adapters_registered" in names
    assert "mcp" in names
    store_check = next(c for c in report.checks if c.name == "store")
    assert store_check.ok
    assert f"v{SCHEMA_VERSION}" in store_check.detail or f"schema_v={SCHEMA_VERSION}" in store_check.detail
    assert "junie" in next(
        c.detail for c in report.checks if c.name == "adapters_registered")


def test_doctor_does_not_mutate_store(tmp_path, monkeypatch):
    from tests.test_store import REC

    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    with Store.open(db) as st:
        st.save_review({**REC, "id": "before-doctor"})
    run_doctor(repo=tmp_path, store_path=db)
    with Store.open(db) as st:
        assert st.get_review("before-doctor") is not None
        n = st._c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        assert n == 1


def test_cli_doctor_exit_codes(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "nope.toml"))
    with Store.open(db) as st:
        st.log_dir()  # ensure schema exists without leaking the connection
    code = main(["doctor", "--repo", str(tmp_path)])
    # Missing provider binaries may yield exit 1; store/config should still report.
    assert code in (0, 1)
    out = capsys.readouterr().out
    assert "skodun doctor:" in out
    assert "store" in out
    assert "mcp" in out
