"""Generate launchd plists from `[schedule]` config (macOS).

No scheduler runs inside `skodun mcp`. This module only **writes** plist
files under an install directory; the operator loads them with `launchctl`.
Off macOS, install refuses closed with a clear message (doctor already notes
the policy).
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class ScheduleJob:
    """One `[[schedule.jobs]]` entry after validation."""

    name: str
    # CalendarInterval fields (optional); IntervalSec for simple period.
    interval_sec: int = 0
    hour: int | None = None
    minute: int | None = 0
    weekday: int | None = None  # 0=Sunday … 6=Saturday (launchd)
    # Command: default retain; may be "retain" or "doctor"
    command: str = "retain"
    # Optional absolute repo path for --repo
    repo: str = ""


@dataclass(frozen=True)
class ScheduleConfig:
    jobs: tuple[ScheduleJob, ...] = ()


def parse_schedule_table(raw: object) -> ScheduleConfig:
    """Parse the TOML `[schedule]` table (or None) into jobs."""
    if raw is None:
        return ScheduleConfig()
    if not isinstance(raw, dict):
        raise ValueError("[schedule] must be a table")
    jobs_raw = raw.get("jobs", [])
    if not isinstance(jobs_raw, list):
        raise ValueError("[schedule].jobs must be an array of tables")
    jobs: list[ScheduleJob] = []
    for i, entry in enumerate(jobs_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"[schedule].jobs[{i}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"[schedule].jobs[{i}]: name is required")
        name = name.strip()
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"[schedule].jobs[{i}]: name must be alphanumeric/"
                f"hyphen/underscore, got {name!r}")
        cmd = entry.get("command", "retain")
        if cmd not in ("retain", "doctor"):
            raise ValueError(
                f"[schedule].jobs[{i}]: command must be 'retain' or 'doctor', "
                f"got {cmd!r}")
        interval = entry.get("interval_sec", 0)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
            raise ValueError(
                f"[schedule].jobs[{i}]: interval_sec must be a non-negative int")
        hour = entry.get("hour")
        minute = entry.get("minute", 0 if hour is not None else None)
        weekday = entry.get("weekday")
        for label, val in (("hour", hour), ("minute", minute), ("weekday", weekday)):
            if val is None:
                continue
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValueError(
                    f"[schedule].jobs[{i}]: {label} must be an integer")
        repo = entry.get("repo", "")
        if repo is not None and not isinstance(repo, str):
            raise ValueError(f"[schedule].jobs[{i}]: repo must be a string")
        if interval == 0 and hour is None:
            raise ValueError(
                f"[schedule].jobs[{i}]: set interval_sec > 0 or hour for calendar run")
        jobs.append(ScheduleJob(
            name=name,
            interval_sec=interval,
            hour=hour,
            minute=minute if minute is not None else 0,
            weekday=weekday,
            command=str(cmd),
            repo=str(repo or ""),
        ))
    return ScheduleConfig(jobs=tuple(jobs))


def _plist_label(job: ScheduleJob) -> str:
    return f"com.skodun.{job.name}"


def render_launchd_plist(
    job: ScheduleJob,
    *,
    python: str | None = None,
    working_directory: str | None = None,
) -> str:
    """XML plist body for one job."""
    py = python or sys.executable
    args = [py, "-m", "skodun", job.command]
    if job.repo:
        args.extend(["--repo", job.repo])
    prog_args = "\n".join(
        f"    <string>{escape(a)}</string>" for a in args
    )
    if job.interval_sec > 0:
        when = f"""  <key>StartInterval</key>
  <integer>{job.interval_sec:d}</integer>
"""
    else:
        cal_parts = ["  <key>StartCalendarInterval</key>", "  <dict>"]
        if job.hour is not None:
            cal_parts.append(f"    <key>Hour</key><integer>{job.hour:d}</integer>")
        if job.minute is not None:
            cal_parts.append(f"    <key>Minute</key><integer>{job.minute:d}</integer>")
        if job.weekday is not None:
            cal_parts.append(
                f"    <key>Weekday</key><integer>{job.weekday:d}</integer>")
        cal_parts.append("  </dict>")
        when = "\n".join(cal_parts) + "\n"
    wd = ""
    if working_directory:
        wd = f"""  <key>WorkingDirectory</key>
  <string>{escape(working_directory)}</string>
"""
    label = _plist_label(job)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{prog_args}
  </array>
{when}{wd}  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


@dataclass(frozen=True)
class InstallResult:
    written: tuple[Path, ...]
    labels: tuple[str, ...]


def install_schedule(
    jobs: tuple[ScheduleJob, ...] | list[ScheduleJob],
    dest_dir: Path,
    *,
    python: str | None = None,
    require_darwin: bool = True,
) -> InstallResult:
    """Write one plist per job under ``dest_dir``.

    When ``require_darwin`` is true (default), refuse on non-macOS.
    """
    if require_darwin and platform.system() != "Darwin":
        raise RuntimeError(
            "skodun schedule install only generates launchd plists on macOS; "
            f"this host is {platform.system()!r}. Use cron/systemd yourself or "
            "run `skodun retain` / `skodun doctor` from an external timer."
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    labels: list[str] = []
    for job in jobs:
        label = _plist_label(job)
        path = dest_dir / f"{label}.plist"
        path.write_text(
            render_launchd_plist(job, python=python),
            encoding="utf-8",
        )
        written.append(path)
        labels.append(label)
    return InstallResult(written=tuple(written), labels=tuple(labels))
