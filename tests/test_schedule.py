"""launchd schedule install — pure render + install helpers + CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skodun.cli import main
from skodun.config import load_config
from skodun.schedule import (
    ScheduleJob,
    install_schedule,
    parse_schedule_table,
    render_launchd_plist,
)


def test_parse_and_render_interval_job():
    cfg = parse_schedule_table({
        "jobs": [
            {"name": "nightly-retain", "interval_sec": 86400, "command": "retain"},
        ]
    })
    assert len(cfg.jobs) == 1
    xml = render_launchd_plist(cfg.jobs[0], python="/usr/bin/python3")
    assert "com.skodun.nightly-retain" in xml
    assert "<integer>86400</integer>" in xml
    assert "-m</string>" in xml or ">skodun</string>" in xml
    assert "retain" in xml


def test_parse_calendar_job():
    cfg = parse_schedule_table({
        "jobs": [
            {"name": "morning-doctor", "hour": 7, "minute": 30, "command": "doctor"},
        ]
    })
    xml = render_launchd_plist(cfg.jobs[0])
    assert "StartCalendarInterval" in xml
    assert "<key>Hour</key><integer>7</integer>" in xml
    assert "doctor" in xml


def test_install_writes_plists(tmp_path):
    jobs = (
        ScheduleJob(name="keep-logs", interval_sec=3600, command="retain"),
    )
    result = install_schedule(jobs, tmp_path / "agents", require_darwin=False)
    assert len(result.written) == 1
    path = result.written[0]
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "com.skodun.keep-logs" in text
    assert result.labels == ("com.skodun.keep-logs",)


def test_install_refuses_off_darwin_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr("skodun.schedule.platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="macOS"):
        install_schedule(
            [ScheduleJob(name="x", interval_sec=60)],
            tmp_path,
            require_darwin=True,
        )


def test_config_loads_schedule_jobs(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(
        "[schedule]\n"
        "[[schedule.jobs]]\n"
        'name = "weekly"\n'
        "interval_sec = 604800\n"
        'command = "retain"\n',
        encoding="utf-8",
    )
    cfg = load_config(None, global_path=g)
    assert len(cfg.schedule_jobs) == 1
    assert cfg.schedule_jobs[0].name == "weekly"


def test_cli_schedule_install(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    (repo / ".skodun.toml").write_text(
        "[[schedule.jobs]]\n"
        'name = "retain-daily"\n'
        "interval_sec = 86400\n"
        'command = "retain"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "missing.toml"))
    dest = tmp_path / "LaunchAgents"
    code = main([
        "schedule", "install",
        "--repo", str(repo),
        "--dest", str(dest),
        "--force-platform",
    ])
    assert code == 0
    plists = list(dest.glob("*.plist"))
    assert len(plists) == 1
    assert "retain" in plists[0].read_text(encoding="utf-8")
