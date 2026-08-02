"""Config-driven retention: prune durable junk that the gate never re-reads.

Worker logs live beside the store at ``<db>.logs/<record-id>.log``. Nothing in
the trust/gate path opens them after the worker finishes, so they are safe to
delete under age and count bounds. Review artifacts and the triage ledger stay
in SQLite and are never touched here.

Policy is pure: :func:`plan_worker_log_prunes` decides paths; :func:`apply_prunes`
deletes (or dry-runs). CLI and any future schedule job share this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PruneReport:
    """What one retention pass decided and did."""

    candidates: tuple[Path, ...]
    deleted: tuple[Path, ...]
    errors: tuple[tuple[Path, str], ...]
    dry_run: bool

    @property
    def would_delete(self) -> int:
        return len(self.candidates)

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)


def plan_worker_log_prunes(
    log_dir: Path,
    *,
    max_age_days: int = 0,
    max_count: int = 0,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Return worker-log paths that exceed age and/or count bounds.

    Only regular files named ``*.log`` directly under ``log_dir`` are considered
    (the shape :meth:`Store.log_dir` writes). ``max_age_days`` / ``max_count`` of
    ``0`` mean that axis is disabled. When both are set, a file is pruned if it
    fails **either** bound (age OR excess count), so multi-week machines stay
    bounded even if every log is "recent".

    Count retention keeps the **newest** ``max_count`` files (by mtime, then
    name); older ones beyond that are candidates. Age uses mtime vs ``now``.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days must be >= 0")
    if max_count < 0:
        raise ValueError("max_count must be >= 0")
    if not log_dir.is_dir():
        return ()

    files = [
        p for p in log_dir.iterdir()
        if p.is_file() and p.suffix == ".log" and not p.is_symlink()
    ]
    if not files:
        return ()

    clock = time.time() if now is None else now
    by_age: set[Path] = set()
    if max_age_days > 0:
        cutoff = clock - (max_age_days * 86400)
        for p in files:
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                by_age.add(p)

    by_count: set[Path] = set()
    if max_count > 0 and len(files) > max_count:
        ranked = sorted(
            files,
            key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, p.name),
            reverse=True,
        )
        by_count = set(ranked[max_count:])

    doomed = by_age | by_count
    # Stable order for reports/tests: oldest first, then name.
    return tuple(sorted(doomed, key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, p.name)))


def apply_prunes(
    paths: tuple[Path, ...] | list[Path],
    *,
    dry_run: bool = False,
) -> PruneReport:
    """Delete ``paths`` (or report them under ``dry_run``). Never raises for one file."""
    candidates = tuple(paths)
    if dry_run:
        return PruneReport(candidates=candidates, deleted=(), errors=(), dry_run=True)
    deleted: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for p in candidates:
        try:
            p.unlink(missing_ok=True)
            if not p.exists():
                deleted.append(p)
        except OSError as e:
            errors.append((p, repr(e)))
    return PruneReport(
        candidates=candidates,
        deleted=tuple(deleted),
        errors=tuple(errors),
        dry_run=False,
    )


def retain_worker_logs(
    log_dir: Path,
    *,
    max_age_days: int = 0,
    max_count: int = 0,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneReport:
    """Plan and apply worker-log retention in one call."""
    planned = plan_worker_log_prunes(
        log_dir,
        max_age_days=max_age_days,
        max_count=max_count,
        now=now,
    )
    return apply_prunes(planned, dry_run=dry_run)
