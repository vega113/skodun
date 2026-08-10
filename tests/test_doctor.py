"""Tests for `skodun doctor` — real CLI entry + report shape."""

from __future__ import annotations

import os
from pathlib import Path

from skodun.cli import main
from skodun.doctor import run_doctor
from skodun.store import SCHEMA_VERSION, Store


def _codex_script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _doctor_for_codex(tmp_path: Path, monkeypatch, capsys, body: str):
    binary = tmp_path / "codex"
    _codex_script(binary, body)
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(binary))
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "missing.toml"))
    code = main(["doctor", "--repo", str(tmp_path)])
    return code, capsys.readouterr().out


def test_doctor_codex_version_probe_launches_binary(tmp_path, monkeypatch, capsys):
    code, output = _doctor_for_codex(
        tmp_path, monkeypatch,
        capsys,
        'test "$1" = "--version" || exit 19\nprintf "codex-cli 0.147.0\\n"\n',
    )
    assert code == 0
    assert "[ok] adapter:openai:version: codex-cli 0.147.0" in output


def test_doctor_reports_codex_version_probe_failure(tmp_path, monkeypatch, capsys):
    code, output = _doctor_for_codex(
        tmp_path, monkeypatch,
        capsys,
        'printf "Bearer secret-token\\n" >&2\nexit 1\n',
    )
    assert code == 1
    assert "[FAIL] adapter:openai:version:" in output
    assert "<redacted>" in output
    assert "secret-token" not in output


def test_doctor_codex_version_probe_times_out_bounded(tmp_path, monkeypatch, capsys):
    from skodun.adapters import codex

    monkeypatch.setattr(codex, "_VERSION_PROBE_TIMEOUT_SEC", 0.1)
    code, output = _doctor_for_codex(
        tmp_path, monkeypatch,
        capsys,
        'sleep 60\n',
    )
    assert code == 1
    assert "[FAIL] adapter:openai:version:" in output
    assert "timed out" in output


def test_doctor_codex_version_probe_bounds_output(tmp_path, monkeypatch, capsys):
    code, output = _doctor_for_codex(
        tmp_path, monkeypatch, capsys,
        'yes codex-noise\n',
    )
    assert code == 1
    assert "[FAIL] adapter:openai:version:" in output
    assert "output limit" in output


def test_run_doctor_reports_store_and_adapters(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "nope.toml"))
    with Store.open(db) as st:
        assert st is not None
    report = run_doctor(repo=tmp_path, store_path=db)
    names = {c.name for c in report.checks}
    assert "python" in names
    assert "package" in names
    assert "store" in names
    assert "adapters_registered" in names
    assert "mcp" in names
    package_check = next(c for c in report.checks if c.name == "package")
    assert package_check.ok
    assert f"schema_v={SCHEMA_VERSION}" in package_check.detail
    store_check = next(c for c in report.checks if c.name == "store")
    assert store_check.ok
    assert f"v{SCHEMA_VERSION}" in store_check.detail or f"schema_v={SCHEMA_VERSION}" in store_check.detail
    assert "junie" in next(
        c.detail for c in report.checks if c.name == "adapters_registered")


def test_doctor_store_open_failure_for_schema_behind_mentions_restart_mcp(
        tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "future.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1:d}")
    raw.commit()
    raw.close()
    report = run_doctor(repo=tmp_path, store_path=db)
    store_check = next(c for c in report.checks if c.name == "store")
    assert not store_check.ok
    assert "newer than this skodun" in store_check.detail
    assert "restart" in store_check.detail.lower()
    assert "MCP" in store_check.detail


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
