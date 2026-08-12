"""Install / MCP readiness diagnostics for `skodun doctor`.

Read-only: never mutates the store or gate state. Collects facts the operator
or agent needs when review/MCP "does not work" — registered adapters, binary
resolve, config load, store open/version, and MCP import readiness.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    exit_code: int = 0  # 0 all ok; 1 problems found; 2 doctor itself failed

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail))
        if not ok and self.exit_code == 0:
            self.exit_code = 1

    def render(self) -> str:
        lines = ["skodun doctor:"]
        for c in self.checks:
            mark = "ok" if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
        if self.exit_code == 0:
            lines.append("skodun doctor: all checks passed")
        else:
            lines.append(f"skodun doctor: {sum(1 for c in self.checks if not c.ok)} problem(s)")
        return "\n".join(lines)


def _binary_status(binary: str) -> str:
    from .runner import _is_path_shaped

    if not binary:
        return "NOT FOUND"
    if _is_path_shaped(binary):
        p = Path(binary)
        if not p.exists():
            return "NOT FOUND"
        if p.is_file() and os.access(p, os.X_OK):
            return "executable"
        return "found, NOT executable"
    return "executable" if shutil.which(binary) else "NOT FOUND"


def run_doctor(
    *,
    repo: Path | None,
    store_path: Path,
    config_path: Path | None = None,
) -> DoctorReport:
    """Run all diagnostic checks. Never raises; failures become report lines."""
    report = DoctorReport()

    # Python runtime
    py = sys.version.split()[0]
    report.add(
        "python",
        sys.version_info >= (3, 12),
        f"{py} (need >= 3.12)",
    )

    # Package / schema this CLI process understands (for CLI↔MCP skew)
    try:
        from . import __version__
        from .provenance import code_provenance, short
        from .store import SCHEMA_VERSION as _SCHEMA_V

        # The COMMIT, not just the version. On an editable install every commit
        # is still 0.4.0, so `version=` alone cannot answer "is this the code I
        # merged?" -- which is the question an operator actually has after a
        # pull (#110).
        # The commit, not just the version: on an editable install every
        # commit is still 0.4.0, so `version=` alone cannot answer "is this the
        # code I merged?".
        #
        # Deliberately NOT a drift check. `doctor` is CLI-only (see "Do not
        # invent" in AGENTS.md) and every run is a fresh process, so it fills
        # its cache from disk and would immediately re-read the same disk --
        # the two sides always agree and the warning could never fire. The
        # drift that matters happens inside a LONG-LIVED MCP server, and that
        # is where the check lives (`mcpserver._warn_if_code_moved`). What
        # doctor gives an operator is the CLI's own commit, to compare against
        # the `serverInfo.commit` their client shows.
        commit = code_provenance().get("skodun_commit")
        running = f" commit={short(commit)}" if commit else ""
        report.add(
            "package", True,
            f"version={__version__} schema_v={_SCHEMA_V}{running} "
            f"(compare with serverInfo from `skodun mcp`; restart it to match)")
    except Exception as e:
        report.add("package", False, f"{e!r}")

    # Config
    try:
        from .config import load_config

        root = repo
        if root is not None and root.is_dir():
            # Prefer the worktree root when `repo` is inside one; otherwise load
            # from the given directory (same posture as `skodun providers`).
            try:
                import subprocess
                cp = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, timeout=30,
                )
                if cp.returncode == 0 and cp.stdout.strip():
                    root = Path(cp.stdout.strip())
            except Exception:
                pass
        elif root is not None and not root.is_dir():
            report.add("config", False, f"repo path is not a directory: {root}")
            root = None
        cfg = load_config(root, global_path=config_path)
        n_rev = len(cfg.reviewers)
        report.add(
            "config",
            True,
            f"loaded; {n_rev} reviewer(s); "
            f"retention age={cfg.retention.worker_log_max_age_days}d "
            f"count={cfg.retention.worker_log_max_count}",
        )
    except Exception as e:
        report.add("config", False, f"load failed: {e!r}")
        cfg = None

    # Store
    try:
        from .store import SCHEMA_VERSION, Store, inspect_schema

        info = inspect_schema(store_path)
        if info["state"] == "missing":
            report.add("store", True,
                       f"missing path={store_path}; no store bytes inspected")
            report.add("worker_logs", True,
                       f"not created for missing store {store_path}")
        elif info["state"] != "current":
            detail = (f"schema state={info['state']} path={store_path} "
                      f"version={info.get('version')} (build expects v"
                      f"{SCHEMA_VERSION}); explicit migration required")
            if info["state"] == "newer":
                detail += (" — newer than this skodun; upgrade this process and "
                           "restart every MCP client")
            report.add("store", False, detail)
            report.add("worker_logs", True, "not inspected by read-only doctor")
        else:
            with Store.open_readonly(store_path) as st:
                n = st._c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            report.add("store", True,
                       f"read-only ok path={store_path} schema_v={SCHEMA_VERSION} "
                       f"(build expects v{SCHEMA_VERSION}) reviews={n}")
            report.add("worker_logs", True, "not created by read-only doctor")
    except Exception as e:
        detail = f"open failed: {e!r}"
        if "newer than this skodun" in str(e):
            detail += (
                " — this CLI/doctor is older than the store; upgrade skodun, "
                "then restart every MCP client so CLI and MCP share one install "
                "(do not keep reviewing via CLI while MCP stays schema-behind)"
            )
        report.add("store", False, detail)

    # Adapters / binaries
    try:
        from .adapters import _REGISTRY, get_adapter

        names = sorted(_REGISTRY)
        report.add("adapters_registered", True, ", ".join(names))
        for provider in names:
            try:
                adapter = get_adapter(provider)
                binary = adapter.resolve_binary()
                status = _binary_status(binary)
                ok = status == "executable"
                # Missing binary is a problem for operators but not always
                # fatal for the install (other providers may work).
                report.add(
                    f"adapter:{provider}",
                    True if status != "found, NOT executable" else False,
                    f"binary={binary!r} ({status})",
                )
                # Optional cheap version probe: never required for ok
                if ok and hasattr(adapter, "version_probe"):
                    try:
                        probe = adapter.version_probe()  # type: ignore[attr-defined]
                        report.add(f"adapter:{provider}:version", True, str(probe))
                    except Exception as e:
                        report.add(
                            f"adapter:{provider}:version",
                            False,
                            f"probe failed: {e!r}",
                        )
            except Exception as e:
                report.add(f"adapter:{provider}", False, f"{e!r}")
    except Exception as e:
        report.add("adapters_registered", False, f"{e!r}")

    # Config reviewers vs registry
    if cfg is not None:
        try:
            from .adapters import _REGISTRY

            bad = [
                r.name for r in cfg.reviewers
                if r.provider not in _REGISTRY
            ]
            if bad:
                report.add(
                    "reviewer_providers",
                    False,
                    f"unknown provider on: {bad}",
                )
            else:
                report.add(
                    "reviewer_providers",
                    True,
                    "all configured providers are registered",
                )
        except Exception as e:
            report.add("reviewer_providers", False, f"{e!r}")

    # MCP readiness: import + module attributes (no serve)
    try:
        mcp = importlib.import_module("skodun.mcpserver")
        has_serve = callable(getattr(mcp, "serve_stdio", None))
        report.add(
            "mcp",
            has_serve,
            "mcpserver.serve_stdio importable"
            if has_serve else "serve_stdio missing",
        )
        report.add(
            "mcp_stdout_policy",
            True,
            "JSON-RPC only on stdout; diagnostics on stderr (by design)",
        )
    except Exception as e:
        report.add("mcp", False, f"import failed: {e!r}")

    # Schedule policy note (scheduler is not inside MCP)
    report.add(
        "scheduler",
        True,
        "no scheduler inside skodun mcp; use `skodun schedule install` (launchd)",
    )

    return report
