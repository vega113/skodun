"""Cheap, read-only review-topology readiness for the foreground loop.

This module deliberately stops before review-fg admission and before any model
process.  A provider whose live health has not been observed is ``unknown`` and
therefore eligible; only locally provable configuration, binary, platform,
quota-blackout, or topology failures make a path unavailable.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_OPENAI_API_KEY_NAMES = ("OPENAI_API_KEY", "SKODUN_OPENAI_API_KEY")


@dataclass(frozen=True)
class ReadinessReport:
    """The structured result shared by the CLI and MCP readiness surfaces."""

    ready: bool
    state: str
    reason_code: str
    reason: str
    finder: str | None
    topology: tuple[dict[str, Any], ...]
    passes: tuple[dict[str, Any], ...]
    diff_bytes: int
    prompt_budget_bytes: int | None
    batch_count: int
    estimated_attempts: int
    estimated_worst_runtime_sec: int
    estimated_lock_budget_sec: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "state": self.state,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "finder": self.finder,
            "topology": [dict(row) for row in self.topology],
            "passes": [dict(row) for row in self.passes],
            "diff_bytes": self.diff_bytes,
            "prompt_budget_bytes": self.prompt_budget_bytes,
            "batch_count": self.batch_count,
            "estimated_attempts": self.estimated_attempts,
            "estimated_worst_runtime_sec": self.estimated_worst_runtime_sec,
            "estimated_lock_budget_sec": self.estimated_lock_budget_sec,
        }


def render(report: ReadinessReport, *, output: str = "text") -> str:
    """Render one report as stable JSON or concise human-readable text."""
    if output == "json":
        return json.dumps(report.to_dict(), sort_keys=True,
                          separators=(",", ":"))
    if output != "text":
        raise ValueError(f"unknown readiness output {output!r}")
    lines = [
        "SKODUN REVIEW READINESS: " + report.state,
        f"reason_code={report.reason_code} reason={report.reason}",
        f"finder={report.finder or '-'} diff_bytes={report.diff_bytes} "
        f"batch_count={report.batch_count}",
        f"estimated_attempts={report.estimated_attempts} "
        f"worst_runtime_sec={report.estimated_worst_runtime_sec} "
        f"lock_budget_sec={report.estimated_lock_budget_sec}",
    ]
    for row in report.topology:
        lines.append(
            f"provider={row['provider']} reviewer={row['reviewer']} "
            f"status={row['status']} detail={row['detail']}"
        )
    for row in report.passes:
        lines.append(
            f"pass={row['pass']} scheduled={row['scheduled']} "
            f"reviewer={row['reviewer']} status={row['status']} "
            f"detail={row['detail']}"
        )
    return "\n".join(lines)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _binary_absent(binary: str) -> bool:
    from . import runner
    import shutil

    if runner._is_path_shaped(binary):
        return not Path(binary).exists()
    return shutil.which(binary) is None


def _api_key_present() -> bool:
    return any(os.environ.get(name, "").strip() for name in _OPENAI_API_KEY_NAMES)


def _entry_status(store, entry, now: str) -> tuple[str, str]:
    """Return ``(status, detail)`` without probing the provider."""
    from .adapters import get_adapter
    from .config import quota_pool_for

    if entry.provider == "openai-api" and not _api_key_present():
        return "unavailable", "no OpenAI API key is configured"
    try:
        adapter = get_adapter(entry.provider)
    except ValueError as exc:
        return "unavailable", str(exc)

    if entry.effort not in (None, "none"):
        try:
            if entry.effort not in adapter.effort_map():
                return "unavailable", (
                    f"adapter does not support configured effort {entry.effort!r}")
        except Exception as exc:
            return "unknown", f"effort support is unknown: {exc!r}"

    eligibility = getattr(adapter, "routing_eligibility", None)
    if callable(eligibility):
        try:
            eligible, detail = eligibility()
        except Exception as exc:  # readiness stays read-only and total
            eligible, detail = True, f"platform health unknown: {exc!r}"
        if not eligible:
            return "unavailable", str(detail or "adapter is not eligible")

    try:
        pool = quota_pool_for(entry)
        if pool == entry.provider:
            blackout = store.provider_unavailable_reason(entry.provider, now)
        else:
            blackout = store.provider_unavailable_reason(
                entry.provider, now, quota_pool=pool)
    except Exception as exc:
        return "unknown", f"provider blackout state unreadable: {exc!r}"
    if blackout:
        return "unavailable", f"quota blackout: {blackout}"

    try:
        binary = adapter.resolve_binary()
    except Exception as exc:
        return "unknown", f"binary health unknown: {exc!r}"
    if _binary_absent(binary):
        return "unavailable", f"binary not found: {binary}"
    return "unknown", "live provider health was not probed"


def _path_rows(store, entries, now: str) -> tuple[tuple[dict[str, Any], ...],
                                                    str | None]:
    from .config import quota_pool_for

    rows: list[dict[str, Any]] = []
    for entry in entries:
        status, detail = _entry_status(store, entry, now)
        rows.append({
            "reviewer": entry.name,
            "provider": entry.provider,
            "model": entry.model,
            "quota_pool": quota_pool_for(entry),
            "status": status,
            "detail": detail,
        })
    if not rows:
        return (), "no configured reviewer path"
    if any(row["status"] == "unknown" for row in rows):
        return tuple(rows), None
    return tuple(rows), rows[0]["detail"]


def _report(*, ready: bool, reason_code: str, reason: str,
            finder: str | None, topology=(), passes=(), diff_bytes=0,
            prompt_budget_bytes=None, batch_count=0, attempts=0,
            worst_runtime=0, lock_budget=0) -> ReadinessReport:
    return ReadinessReport(
        ready=ready,
        state="potentially_available" if ready else "known_impossible",
        reason_code=reason_code,
        reason=reason,
        finder=finder,
        topology=tuple(topology),
        passes=tuple(passes),
        diff_bytes=diff_bytes,
        prompt_budget_bytes=prompt_budget_bytes,
        batch_count=batch_count,
        estimated_attempts=attempts,
        estimated_worst_runtime_sec=worst_runtime,
        estimated_lock_budget_sec=lock_budget,
    )


def check(store, repo: Path, cfg, *, requested: str | None = None,
          client_family: str | None = None) -> ReadinessReport:
    """Assess whether the configured review has a plausible path to trust."""
    from . import budget, gitio, passes, pipeline, routing

    repo = Path(repo)
    try:
        root = gitio._worktree_root(repo)
        base = gitio.resolve_base(root)
        diff = gitio.capture_diff(root, base.sha, cfg.defaults.untracked_max)
    except Exception as exc:
        return _report(ready=False, reason_code="repository_unreadable",
                       reason=f"could not inspect repository: {exc!r}",
                       finder=None)

    try:
        finder, _route_meta = pipeline.resolve_review_head(
            cfg, store, requested=requested,
            client_family=routing.resolve_client_family(client_family))
    except pipeline.PreflightRefused as exc:
        return _report(ready=False, reason_code="topology_unavailable",
                       reason=str(exc), finder=None,
                       diff_bytes=len(diff.data))
    except Exception as exc:
        return _report(ready=False, reason_code="topology_unreadable",
                       reason=f"could not resolve review topology: {exc!r}",
                       finder=None, diff_bytes=len(diff.data))

    chain = tuple(pipeline._chain_for(cfg, finder))
    now = _now()
    topology, _ = _path_rows(store, chain, now)
    prompt_limit = budget.prompt_budget(cfg.defaults, finder)
    plan = pipeline.batch_plan(diff.data, cfg.defaults, finder)
    batch_count = 0 if plan is None else len(plan)
    if plan == [] and diff.data:
        return _report(ready=False, reason_code="prompt_unfit",
                       reason="the diff could not be split into review batches",
                       finder=finder.name, topology=topology,
                       diff_bytes=len(diff.data),
                       prompt_budget_bytes=prompt_limit)

    if not any(row["status"] == "unknown" for row in topology):
        detail = "; ".join(row["detail"] for row in topology)
        if any("API key" in row["detail"] for row in topology):
            code = "auth_unavailable"
        elif any("binary not found" in row["detail"] for row in topology):
            code = "binary_unavailable"
        elif any("quota blackout" in row["detail"] for row in topology):
            code = "finder_chain_unavailable"
        else:
            code = "finder_chain_unavailable"
        return _report(ready=False, reason_code=code,
                       reason=f"every finder path is unavailable: {detail}",
                       finder=finder.name, topology=topology,
                       diff_bytes=len(diff.data),
                       prompt_budget_bytes=prompt_limit)

    pass_rows: list[dict[str, Any]] = []
    scheduled: list[tuple[str, str]] = []
    pass_attempts = 0
    if passes.should_run_security("now", diff.files,
                                  cfg.defaults.security_path_segments,
                                  cfg.defaults.security_basename_patterns):
        scheduled.append(("security", "security"))
    if batch_count >= 2:
        scheduled.append((passes.INTEGRATION_PASS, passes.INTEGRATION_PASS))
    for pass_name, role_name in scheduled:
        reviewer = pipeline._pass_reviewer(cfg, pass_name, finder)
        pass_chain = tuple(pipeline._chain_for(cfg, reviewer))
        pass_attempts += max(1, len(pass_chain))
        rows, _ = _path_rows(store, pass_chain, now)
        pass_status = "available" if any(
            row["status"] == "unknown" for row in rows) else "unavailable"
        pass_rows.append({
            "pass": pass_name,
            "scheduled": True,
            "reviewer": reviewer.name,
            "status": pass_status,
            "detail": "; ".join(row["detail"] for row in rows),
        })
        if pass_status == "unavailable":
            return _report(
                ready=False, reason_code="required_pass_unavailable",
                reason=f"required {role_name} pass has no eligible provider path",
                finder=finder.name, topology=topology, passes=pass_rows,
                diff_bytes=len(diff.data), prompt_budget_bytes=prompt_limit)

    width = max(1, len(chain))
    calls = max(1, batch_count + 1)
    attempts = width * calls + pass_attempts
    return _report(
        ready=True, reason_code="health_unknown",
        reason="at least one eligible path is locally ready; live provider health "
               "was not probed",
        finder=finder.name, topology=topology, passes=pass_rows,
        diff_bytes=len(diff.data), prompt_budget_bytes=prompt_limit,
        batch_count=batch_count, attempts=attempts,
        worst_runtime=budget.worst_runtime(cfg.defaults, width, batch_count),
        lock_budget=budget.lock_stale_ceiling(cfg.defaults, width, batch_count),
    )


__all__ = ["ReadinessReport", "check", "render"]
