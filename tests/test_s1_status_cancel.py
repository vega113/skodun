"""Epic S1: first-class review status + cancel (services, CLI, MCP, stale).

Hermetic only: fake provider, tmp store, no real model. Drives the shipped
entry points (`services.svc_review_status` / `svc_review_cancel`, CLI `main`,
MCP handlers) rather than re-implementing them.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from skodun import mcpserver, pipeline, services
from skodun.cli import main
from skodun.config import load_config
from skodun.gitio import git_common_dir
from skodun.mcpserver import HandlerCall, HandlerResult
from skodun.pipeline import ReviewCancelled, run_review
from skodun.store import Store
from tests.test_cancellation import (
    _Run, _assert_cancelled, _fake_grok, _group_alive, _hang, _pgid, _repo,
    _store, _wait_for,
)
from tests.test_cli import _artifact, _finding
from tests.test_gitio import _git, _mkrepo
from tests.test_pipeline import CLEAN, _emit
from tests.test_refuter import CFG_FINDER_XAI

CFG = CFG_FINDER_XAI


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "bin" / "codex"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.delenv("SKODUN_REFUTER_PASS", raising=False)
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    from skodun import runner
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


def _db_with(tmp_path: Path, *records) -> Path:
    db = tmp_path / "s1.db"
    with Store.open(db) as st:
        for rec in records:
            st.save_review(rec)
    return db


def _tool(name: str, db: Path, **params) -> HandlerResult:
    specs = {s.name: s for s in mcpserver.default_registry()}
    spec = specs[name]
    call = HandlerCall(
        params=params,
        store_factory=lambda: Store.open(db),
        cancel=threading.Event(),
    )
    return spec.handler(call)


# ==========================================================================
# report_state vocabulary
# ==========================================================================

def test_report_state_maps_cancel_reason_to_cancelled_not_failed():
    rec = _artifact([_finding()], status="failed", trustworthy=False,
                    parse_ok=True, degraded=True,
                    failure_reason="cancelled: after the review",
                    degraded_reason="cancelled: after the review",
                    findings_total=0)
    assert services.report_state(rec) == "cancelled"


def test_report_state_clean_vs_findings():
    clean = _artifact([], status="clean", findings_total=0, summary="ok")
    dirty = _artifact([_finding()], status="clean", findings_total=1,
                      summary="findings")
    assert services.report_state(clean) == "clean"
    assert services.report_state(dirty) == "findings"


def test_report_state_queued_prepush_without_pid():
    rec = _artifact([], status="running", mode="prepush", pid=None,
                    findings_total=0)
    assert services.report_state(rec) == "queued"


def test_report_state_running_with_pid():
    rec = _artifact([], status="running", mode="now", pid=12345,
                    findings_total=0)
    assert services.report_state(rec) == "running"


# ==========================================================================
# status service
# ==========================================================================

def _fresh_ts() -> str:
    """A reviewed_at that will not look stale under default worst_runtime."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def test_svc_review_status_by_id_and_current_for_repo(tmp_path):
    repo = "/repos/s1"
    now = _fresh_ts()
    running = _artifact([], id="sk_run", status="running", mode="now",
                        pid=os.getpid(), findings_total=0, repo=repo,
                        reviewed_at=now, model="m1",
                        adapter="grok", worst_runtime_sec=86_400)
    terminal = _artifact([_finding()], id="sk_done", status="clean",
                         findings_total=1, repo=repo,
                         reviewed_at="2020-01-01T11:00:00Z", model="m0",
                         adapter="grok")
    db = _db_with(tmp_path, terminal, running)
    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id="sk_run")
        assert code == 0
        assert "id=sk_run" in text
        assert "state=running" in text
        assert "provider=grok" in text
        assert "model=m1" in text

        code, text = services.svc_review_status(st, repo=repo)
        assert code == 0
        assert "id=sk_run" in text and "state=running" in text

        code, text = services.svc_review_status(st, review_id="sk_done")
        assert code == 0
        assert "id=sk_done" in text
        assert "state=findings" in text


def test_svc_review_status_json_exposes_batched_telemetry_projection(tmp_path):
    rec = _artifact([], id="sk_batch", status="clean", trustworthy=True,
                    usable_output=True, findings_total=0,
                    batches=[
                        {"parse_ok": True, "telemetry":
                         {"bytes": {"prompt": 7}}},
                        {"parse_ok": True, "telemetry":
                         {"bytes": {"prompt": 9}}}],
                    batch_plan={"planner_version": "plan-v1",
                                "boundary_digest": "d" * 8},
                    batch_count=2)
    db = _db_with(tmp_path, rec)
    with Store.open(db) as st:
        code, payload = services.svc_review_status(
            st, review_id="sk_batch", output="json")
        code_text, text = services.svc_review_status(st, review_id="sk_batch")
    assert code == 0
    import json
    coverage = json.loads(payload)["coverage"]
    assert coverage["batch_count"] == 2
    assert coverage["prompt_bytes"] == 16
    assert coverage["planner_version"] == "plan-v1"
    assert coverage["boundary_digest"] == "d" * 8
    assert code_text == 0
    assert "batch_count=2" in text
    assert "failed_passes=0" in text
    assert "boundary_digest=" + ("d" * 8) in text


def test_svc_review_status_missing_id_refuses(tmp_path):
    db = _db_with(tmp_path)
    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id="nope")
    assert code == 2
    assert "no such review" in text


def test_svc_review_status_after_terminal_cancel_reason(tmp_path):
    rec = _artifact([], id="sk_c", status="failed", trustworthy=False,
                    failure_reason=services.REVIEW_CANCEL_DURABLE_REASON,
                    degraded=True, degraded_reason=services.REVIEW_CANCEL_DURABLE_REASON,
                    findings_total=0, repo="/r")
    db = _db_with(tmp_path, rec)
    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id="sk_c")
    assert code == 0
    assert "state=cancelled" in text
    assert "id=sk_c" in text


# ==========================================================================
# cancel service + mid-provider (shipped path)
# ==========================================================================

def test_svc_review_cancel_refuses_missing_and_terminal(tmp_path):
    rec = _artifact([], id="sk_t", status="clean", findings_total=0)
    db = _db_with(tmp_path, rec)
    with Store.open(db) as st:
        code, text = services.svc_review_cancel(st, "missing")
        assert code == 2 and "no such review" in text
        code, text = services.svc_review_cancel(st, "sk_t")
        assert code == 2 and "already terminal" in text
        assert "clean" in text or "findings" in text or "failed" in text


def test_mid_provider_cancel_via_svc_review_cancel(tmp_path):
    """Cancel-by-id during a hanging fake provider: durable terminal + free lock."""
    _fake_grok(tmp_path, _hang())
    repo = _repo(tmp_path)
    db = tmp_path / "s.db"

    with _Run(repo, db) as run:
        pgid = _wait_for(lambda: _pgid(tmp_path), what="provider start",
                         run=run)
        rid_box: dict = {}

        def _rid():
            with Store.open(db) as st:
                rows = st.list_reviews(None, 10)
            if rows:
                rid_box["id"] = rows[0]["id"]
                return rows[0]["id"]
            return None

        rid = _wait_for(_rid, what="running row", run=run)
        with Store.open(db) as st:
            code, text = services.svc_review_status(st, review_id=rid)
            assert code == 0 and "state=running" in text
            code, text = services.svc_review_cancel(st, rid)
            assert code == 0
            assert rid in text
        run.join()

    rec = _assert_cancelled(run, tmp_path, repo, expect_record=True)
    assert rec["id"] == rid
    assert rec["trustworthy"] is False
    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id=rid)
    assert code == 0
    assert f"id={rid}" in text
    assert "state=cancelled" in text
    _wait_for(lambda: not _group_alive(pgid), timeout=30,
              what="provider group death")


def test_cancel_dead_running_row_demotes_durably(tmp_path):
    """No live holder: fail_if_running leaves cancelled terminal, free for FG."""
    dead_pid = 2**30  # not a live process on this host
    rec = _artifact([], id="sk_dead", status="running", mode="now",
                    pid=dead_pid, findings_total=0, repo="/repos/x",
                    reviewed_at=_fresh_ts(),
                    worst_runtime_sec=86_400)
    db = _db_with(tmp_path, rec)
    with Store.open(db) as st:
        code, text = services.svc_review_cancel(st, "sk_dead")
        assert code == 0
        assert "sk_dead" in text
        stored = st.get_review("sk_dead")
        assert stored is not None
        assert stored["status"] == "failed"
        assert stored["trustworthy"] is False
        assert "cancel" in (stored.get("failure_reason") or "").lower()
        code, text = services.svc_review_status(st, review_id="sk_dead")
        assert code == 0 and "state=cancelled" in text


# ==========================================================================
# stale FG sweep via status path (aligned with recover_stale)
# ==========================================================================

def test_status_sweeps_aged_fg_running_row(tmp_path, monkeypatch):
    """An ancient FG running row is recovered on status, same as prepush."""
    # reviewed_at far in the past; worst_runtime_sec tiny → over ceiling
    rec = _artifact([], id="sk_stale", status="running", mode="now",
                    pid=None, findings_total=0, repo="/repos/stale",
                    reviewed_at="2000-01-01T00:00:00Z",
                    worst_runtime_sec=1)
    db = _db_with(tmp_path, rec)
    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id="sk_stale")
        assert code == 0
        assert "state=running" not in text
        stored = st.get_review("sk_stale")
        assert stored["status"] == "failed"
        assert stored["trustworthy"] is False
        assert "stale" in (stored.get("failure_reason") or "").lower()


# ==========================================================================
# CLI dispatch
# ==========================================================================

def test_cli_review_status_and_cancel_stdout(tmp_path, monkeypatch, capsys):
    rec = _artifact([], id="sk_cli", status="running", mode="now",
                    pid=2**30, findings_total=0, repo="/r",
                    reviewed_at=_fresh_ts(), model="m",
                    adapter="grok", worst_runtime_sec=86_400)
    db = _db_with(tmp_path, rec)
    monkeypatch.setenv("SKODUN_DB", str(db))

    assert main(["review-status", "sk_cli"]) == 0
    out1 = capsys.readouterr().out
    assert "id=sk_cli" in out1 and "state=running" in out1

    assert main(["review-status", "sk_cli"]) == 0
    out1b = capsys.readouterr().out
    assert "id=sk_cli" in out1b and "state=running" in out1b

    assert main(["review-cancel", "sk_cli"]) == 0
    out2 = capsys.readouterr().out
    assert "sk_cli" in out2

    assert main(["review-cancel", "sk_cli"]) == 2  # already terminal
    assert "terminal" in capsys.readouterr().out

    assert main(["review-status", "sk_cli"]) == 0
    out3 = capsys.readouterr().out
    assert "id=sk_cli" in out3 and "state=cancelled" in out3

    assert main(["review-status", "sk_cli"]) == 0
    out3b = capsys.readouterr().out
    assert "state=cancelled" in out3b


# ==========================================================================
# MCP parity (same services)
# ==========================================================================

def test_mcp_review_status_and_cancel_parity(tmp_path):
    finding = {"file": "src/a.py", "title": "same",
               "finding_fingerprint_v2": "sha256:" + "a" * 64,
               "finding_lineage_v2": {"match_reason": "moved"}}
    rec = _artifact([finding], id="sk_mcp", status="running", mode="now",
                    pid=2**30, findings_total=0, repo="/r",
                    reviewed_at=_fresh_ts(), model="m",
                    adapter="grok", worst_runtime_sec=86_400)
    db = _db_with(tmp_path, rec)

    def _sans_age(line: str) -> str:
        # age=Ns is wall-clock; parity must not depend on it.
        return " ".join(p for p in line.split() if not p.startswith("age="))

    res = _tool("review_status", db, review_id="sk_mcp")
    assert res.status == 0
    assert "id=sk_mcp" in res.text and "state=running" in res.text
    assert "fingerprint_version=finding_fingerprint_v2" in res.text
    assert "lineage_counts=moved:1" in res.text

    with Store.open(db) as st:
        code, text = services.svc_review_status(st, review_id="sk_mcp")
    assert code == res.status
    assert _sans_age(text) == _sans_age(res.text)

    res = _tool("review_cancel", db, review_id="sk_mcp")
    assert res.status == 0
    assert "sk_mcp" in res.text

    # second cancel: terminal refusal — compare MCP vs service (no intervening
    # cancel that would change shared state between the two surfaces).
    res2 = _tool("review_cancel", db, review_id="sk_mcp")
    with Store.open(db) as st:
        code2, text2 = services.svc_review_cancel(st, "sk_mcp")
    assert res2.status == code2 and res2.text == text2
    assert res2.status == 2 and "terminal" in res2.text

    res3 = _tool("review_status", db, review_id="sk_mcp")
    assert res3.status == 0 and "state=cancelled" in res3.text


def test_tool_registry_includes_s1_tools():
    names = [s.name for s in mcpserver.default_registry()]
    assert "review_status" in names
    assert "review_cancel" in names
    assert names.index("review_status") > names.index("triage_defer")


# ==========================================================================
# MCP worker-thread + cross-process cancel (the S1 bug the skeptic found)
# ==========================================================================

def test_cross_process_review_cancel_of_mcp_held_review(tmp_path):
    """CLI review-cancel against a real skodun mcp process mid-provider.

    MCP runs `review` on a worker thread where `signal.signal` cannot install a
    handler. Without a main-thread SIGTERM forwarder, cross-process cancel would
    SIGTERM the MCP pid with SIG_DFL, kill the process without setting the cancel
    token, and leave: orphan provider (start_new_session), forever-running row,
    FG lock until reclaim. This drill is the regression lock for that path.
    """
    import json
    import subprocess
    import sys

    import skodun
    from tests.test_mcptools import _HANDSHAKE, _env, _rpc
    from tests.test_pipeline import _fake_grok

    hang = (
        'python3 -c "import os; open(\'$D/started.pgid\',\'w\')'
        '.write(str(os.getpgid(0)))"\n'
        "trap '' TERM\n"
        "sleep 300\n"
    )
    _fake_grok(tmp_path, hang)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")

    env = _env(tmp_path)
    env["SKODUN_GROK_BIN"] = str(tmp_path / "bin" / "grok")
    env["SKODUN_CODEX_BIN"] = str(tmp_path / "bin" / "codex")
    env["SKODUN_ALLOW_MAIN"] = "1"
    env["SKODUN_SECURITY_PASS"] = "0"
    env["SKODUN_SKEPTIC_PASS"] = "0"
    env["SKODUN_LOCK_WAIT_SECONDS"] = "5"
    env["SKODUN_LOCK_POLL_SECONDS"] = "0.05"
    src = str(Path(skodun.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        [src] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    db = Path(env["SKODUN_DB"])
    pgid_file = tmp_path / "bin" / "started.pgid"
    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True)
    # Drain pipes so a long-running review cannot block on a full buffer.
    drained: dict[str, list[bytes]] = {"out": [], "err": []}

    def _drain(stream, key: str) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                drained[key].append(chunk)
        except Exception:
            pass

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True),
    ]
    for t in readers:
        t.start()

    def _err_text() -> str:
        return b"".join(drained["err"]).decode("utf-8", "replace")

    try:
        proc.stdin.write(_HANDSHAKE)
        proc.stdin.write(_rpc("tools/call", 1, name="review",
                              arguments={"repo": str(repo)}))
        proc.stdin.flush()

        # Wait for the hanging provider AND a durable running row with an id.
        rid_box: dict = {}
        deadline = time.monotonic() + 120

        def _ready():
            if not pgid_file.exists():
                return False
            if not db.exists():
                return False
            try:
                with Store.open(db) as st:
                    rows = [r for r in st.list_reviews(None, 20)
                            if r.get("status") == "running"]
            except Exception:
                return False
            if not rows:
                return False
            rid_box["id"] = rows[0]["id"]
            rid_box["pid"] = rows[0].get("pid")
            return True

        while time.monotonic() < deadline and not _ready():
            assert proc.poll() is None, (
                f"MCP exited before reviewing: {_err_text()!r}")
            time.sleep(0.05)
        assert pgid_file.exists(), "the provider never started"
        assert "id" in rid_box, "no running review row was recorded"
        rid = rid_box["id"]
        pgid = int(pgid_file.read_text(encoding="utf-8").strip())
        # The stored pid must be the MCP process (FG attach), not the provider.
        assert rid_box["pid"] == proc.pid, (
            f"expected FG pid={proc.pid}, got {rid_box['pid']!r}")

        # Cross-process cancel via the SHIPPED CLI entry point (not request_cancel).
        cancel = subprocess.run(
            [sys.executable, "-m", "skodun", "review-cancel", rid],
            env=env, capture_output=True, text=True, timeout=60)
        assert cancel.returncode == 0, (
            f"review-cancel failed: out={cancel.stdout!r} err={cancel.stderr!r}")
        assert rid in cancel.stdout

        # Durable terminal + free lock + dead provider group.
        deadline = time.monotonic() + 60
        terminal = None
        while time.monotonic() < deadline:
            with Store.open(db) as st:
                terminal = st.get_review(rid)
            if terminal is not None and terminal.get("status") != "running":
                break
            time.sleep(0.05)
        assert terminal is not None and terminal.get("status") != "running", (
            f"row still running after cancel: {terminal!r}")
        assert terminal.get("trustworthy") is False
        assert services.report_state(terminal) == "cancelled"
        # "cancel completed" / demote via the holder's finally: not the dead-pid
        # "holder was not reachable" path, which would leave the FG lock behind.
        assert "not reachable" not in cancel.stdout, cancel.stdout

        lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and lock.exists():
            time.sleep(0.05)
        assert not lock.exists(), (
            "the cancelled MCP review kept the foreground lock")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _group_alive(pgid):
            time.sleep(0.05)
        assert not _group_alive(pgid), (
            "the model CLI outlived the cross-process cancelled review")

        # MCP should still be alive: SIGTERM was forwarded to the cancel token,
        # not left as SIG_DFL process death. (If it already exited 0 after the
        # tool result, that is also fine — just not a crash mid-hang.)
        if proc.poll() is not None:
            assert proc.returncode == 0, (
                f"MCP crashed on cancel: rc={proc.returncode} "
                f"err={_err_text()!r}")
    finally:
        if proc.poll() is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
