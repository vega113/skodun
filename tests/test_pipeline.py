"""The foreground review pipeline: lock, orchestration, record, banner.

Every test here drives the REAL orchestrator against a REAL git repo and a
REAL child process — the only thing faked is the model CLI itself, a shell
script on disk that `SKODUN_GROK_BIN` points at and that emits a canned
envelope. That keeps the suite free of any subscription or network while still
exercising the argv the adapter builds, the watchdog, the timeout truncation,
and the parse path.

Two isolation rules are enforced by an autouse fixture and are not optional:
`SKODUN_DB` and `SKODUN_GROK_BIN` are pinned into `tmp_path`, so no test can
reach the developer's real store or their real `~/.grok/bin/grok`. (Pinning
`SKODUN_GROK_BIN` rather than merely prepending to `PATH` is required, not
belt-and-braces: `adapters.grok.resolve_grok_bin` prefers `~/.grok/bin/grok`
over `PATH`, so on any machine that has grok installed a PATH-only fake would
silently lose and the tests would run the real CLI.) `SKODUN_CONFIG` is pinned
at a non-existent path for the same reason: the developer's own global config
must not leak into a test's `Defaults`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skodun import pipeline, runner
from skodun.adapters import REVIEW_CONTRACT
from skodun.cli import main
from skodun.config import load_config
from skodun.gitio import capture_diff, diff_identity, git_common_dir, resolve_base
from skodun.pipeline import LockTimeout, PersistenceFailed, PreflightRefused, run_review
from skodun.store import Store
from skodun.triage import load_valid_artifact
from skodun.trust import banner
from tests.test_gitio import _git, _mkrepo

# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

CLEAN = json.dumps({"structuredOutput": {"summary": "ok", "findings": []},
                    "stopReason": "EndTurn"})
CANCELLED = json.dumps({"structuredOutput": {"summary": "s", "findings": []},
                        "stopReason": "Cancelled"})
FINDING = {"file": "a.txt", "line": 1, "severity": "high", "category": "bug",
           "title": "[no-foo] bad thing", "detail": "why"}
DIRTY = json.dumps({"structuredOutput": {"summary": "found one",
                                         "findings": [FINDING]},
                    "stopReason": "EndTurn"})

CFG = """
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
"""


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    # The extra passes are opt-IN for these tests: they double the number of
    # model calls, and every test that wants one turns it on explicitly.
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    # A SHORT default wait, and the reason it is not left on the production
    # default: that default is the config's worst-case runtime for three
    # reviewer runs -- ~43 minutes with the shipped timeouts -- so a regression
    # in `_release_fg_lock` (or in the reclaim rules) would not fail a test, it
    # would HANG the suite for most of an hour and then fail it. There is no
    # per-test timeout to catch that. Every test that cares about the wait
    # passes `lock_wait=` explicitly or sets the variable itself; this only
    # bounds the ones that expect to take the lock uncontended. Production
    # defaults are untouched: this is an env override, set for tests only.
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    # The stale ceiling is left unset so the tests exercise the real, computed
    # one (`pipeline.lock_stale_ceiling_sec`).
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    # Shrink the SIGTERM->SIGKILL grace so the timeout tests cost ~1s, not ~4s.
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


def _fake_grok(tmp_path: Path, body: str) -> Path:
    """Install a fake grok CLI whose shell `body` decides what one call does.

    The body runs with `$CALL` set to the 1-based invocation number and `$D`
    set to the directory it can leave evidence in, so a test can make attempt 1
    time out and attempt 2 succeed.
    """
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    g = b / "grok"
    g.write_text(
        "#!/bin/sh\n"
        'D="$(cd "$(dirname "$0")" && pwd)"\n'
        'echo invoked >> "$D/calls.log"\n'
        'CALL=$(wc -l < "$D/calls.log" | tr -d " ")\n'
        'printf "%s\\n" "$@" > "$D/argv_$CALL.log"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  [ "$prev" = "--prompt-file" ] && cp "$a" "$D/prompt_$CALL.txt"\n'
        '  prev="$a"\n'
        "done\n"
        f"{body}\n",
        encoding="utf-8")
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    return g


def _emit(envelope: str) -> str:
    return f"cat <<'SKODUN_EOF'\n{envelope}\nSKODUN_EOF"


def _emit_then_hang(envelope: str) -> str:
    """Print a perfectly clean envelope, then hang past the timeout.

    The exact shape the runner's stdout truncation exists for: a run that looks
    complete and never finished.
    """
    return _emit(envelope) + "\nsleep 30"


def _per_call(*bodies: str) -> str:
    """Dispatch on `$CALL`; the last body serves every later call."""
    if len(bodies) == 1:
        return bodies[0]
    out = []
    for i, body in enumerate(bodies[:-1], start=1):
        out.append(f'if [ "$CALL" = {i} ]; then\n{body}\nel')
    out.append("se\n" + bodies[-1] + "\nfi")
    return "".join(out)


def _calls(tmp_path: Path) -> int:
    log = tmp_path / "bin" / "calls.log"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def _repo(tmp_path: Path, extra_cfg: str = "") -> Path:
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG + extra_cfg, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo


def _store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "s.db")


def _run(repo: Path, store: Store, **kw) -> dict:
    return run_review(repo, load_config(repo), store, **kw)


def _verdict(rec: dict, capsys) -> str:
    """The banner for a record `run_review` just returned, and the proof that
    the pipeline printed NOTHING while producing it.

    `run_review` used to print this line itself. It no longer writes to stdout at
    all -- `skodun mcp` serves JSON-RPC there from another thread, and one banner
    line desynchronises the client's parser for the rest of the session -- so the
    banner is rendered by the caller from the returned record, through
    `trust.banner`, the ONE definition of it. That is what the CLI does
    (`services.svc_review`) and what the MCP `review` tool does, and it is what
    the assertions below now do.

    The `out == ""` half is load-bearing: without it, re-adding a `print` inside
    the pipeline would pass every one of these tests.
    """
    out = capsys.readouterr().out
    assert out == "", f"the pipeline wrote to stdout: {out!r}"
    return banner(rec)


def _write_owner(lock: Path, pid: int, started: int, worktree: Path) -> None:
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "owner").write_text(
        f"pid={pid}\nstarted={started}\nworktree={worktree}\n", encoding="utf-8")


def _spawned_pid() -> int:
    """The pid of a process that has already exited and been reaped."""
    p = subprocess.Popen(["sh", "-c", "exit 0"])
    p.wait()
    return p.pid


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_clean_run_records_and_banners(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["trustworthy"] is True
    assert rec["status"] == "clean"
    assert rec["parse_ok"] is True and rec["degraded"] is False
    assert rec["diff_truncated"] is False
    assert rec["findings"] == [] and rec["findings_total"] == 0
    assert rec["stop_reason"] == "EndTurn"
    assert _verdict(rec, capsys).startswith(
        "SKODUN VERDICT: trustworthy=true findings=0")
    # The banner is rendered from the PERSISTED record, so the stored row and
    # the returned dict must be the same object's contents.
    assert st.get_review(rec["id"]) == rec


def test_persisted_record_carries_the_full_artifact_schema(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _repo(tmp_path)
    st = _store(tmp_path)

    rec = _run(repo, st)

    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, 100)
    assert rec["branch"] == "feat"
    assert rec["base_ref"] == base.ref and rec["base_sha"] == base.sha
    assert rec["diff_hash"] == diff_identity(diff.data)
    assert rec["head"] == _git(repo, "rev-parse", "HEAD")
    assert rec["mode"] == "now"
    assert rec["model"] == "grok-4.20-0309-reasoning"
    assert rec["adapter"] == "grok"
    assert rec["timeout_seconds"] == 420 and rec["max_turns"] == 40
    assert set(rec["files_changed"]) == set(diff.files)
    assert rec["diff_bytes"] == len(diff.data)
    assert rec["prompt_bytes"] > rec["diff_bytes"]
    assert isinstance(rec["checklist_sections"], list)
    assert isinstance(rec["checklist_bytes"], int)
    assert isinstance(rec["context_files"], list)
    assert isinstance(rec["context_omitted_files"], list)
    assert rec["context_hash"] and len(rec["context_hash"]) == 64
    assert [a["n"] for a in rec["attempts"]] == [1]
    assert rec["severity"] == {"high": 1, "medium": 0, "low": 0}
    assert rec["rule_ids"] == ["no-foo"]
    # No extra pass RAN. The one entry is the refuter saying it was never
    # configured: this review has findings, so it earned a cross-provider
    # re-examination and got none, and a record that never mentions the refuter
    # is indistinguishable from one whose refuter confirmed everything. It
    # annotates nothing and demotes nothing — see tests/test_refuter.py.
    assert set(rec["extra_passes"]) == {"refuter"}
    assert rec["extra_passes"]["refuter"]["status"] == "skipped"
    assert rec["extra_passes"]["refuter"]["ran"] is False
    assert rec["failure_reason"] == ""
    # ...and the gate's fail-closed artifact validator accepts it.
    assert load_valid_artifact(rec) is rec


def test_a_clean_record_also_satisfies_the_strict_artifact_validator(tmp_path,
                                                                     capsys):
    """`load_valid_artifact` demands findings/findings_total/id/branch/base_sha
    on EVERY artifact, clean ones included — a clean review that omits them
    would be rejected by the very gate it exists to clear."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)
    rec = _run(repo, st)
    assert load_valid_artifact(rec)["findings_total"] == 0
    # The gate looks the review up by diff hash; it must be findable.
    assert st.latest_trustworthy_for(rec["diff_hash"])["id"] == rec["id"]


def test_a_failed_record_also_satisfies_the_strict_artifact_validator(tmp_path,
                                                                      capsys):
    _fake_grok(tmp_path, "exit 1")
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    st = _store(tmp_path)
    rec = _run(repo, st)
    assert rec["parse_ok"] is False and rec["status"] == "failed"
    assert load_valid_artifact(rec)["findings_total"] == 0


def test_findings_make_the_review_trustworthy_but_not_clean(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert rec["trustworthy"] is True and rec["findings_total"] == 1
    assert rec["status"] == "clean"   # "clean" is the TRUST status, not "no findings"
    assert _verdict(rec, capsys).startswith(
        "SKODUN VERDICT: trustworthy=true findings=1")


def test_the_reviewer_is_invoked_with_the_configured_model_and_a_prompt_file(
        tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    _run(repo, _store(tmp_path))
    argv = (tmp_path / "bin" / "argv_1.log").read_text(encoding="utf-8").split("\n")
    assert "--prompt-file" in argv
    assert argv[argv.index("-m") + 1] == "grok-4.20-0309-reasoning"
    assert argv[argv.index("--max-turns") + 1] == "40"
    assert "--disallowed-tools" in argv
    prompt = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert "----- BEGIN DIFF -----" in prompt
    assert "+two" in prompt


def test_two_runs_in_the_same_process_second_get_distinct_ids(monkeypatch):
    """Second-resolution time plus pid collides for two runs in one process
    second, and the store's upsert would silently overwrite the first."""
    real_gmtime = time.gmtime
    monkeypatch.setattr(pipeline.time, "gmtime", lambda *a: real_gmtime(0))
    ids = {pipeline._new_id("sk_") for _ in range(200)}
    assert len(ids) == 200
    stamps = {i.rsplit("_", 1)[0] for i in ids}
    assert len(stamps) == 1                       # same second, same pid
    assert stamps.pop() == f"sk_19700101T000000Z_{os.getpid()}"


def test_now_mode_never_dedups(tmp_path, capsys):
    """The oracle's `--now` always reviews; dedup is dispatcher machinery."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)
    r1 = _run(repo, st)
    r2 = _run(repo, st)
    assert r2["id"] != r1["id"]
    assert r1["diff_hash"] == r2["diff_hash"]     # same content, reviewed twice
    assert _calls(tmp_path) == 2
    assert len(st.list_reviews(None, 10)) == 2


def test_an_empty_outgoing_change_is_not_sent_to_the_model(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cfg")
    _git(repo, "checkout", "-b", "feat")
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 0
    assert rec["trustworthy"] is True and rec["findings_total"] == 0
    assert rec["summary"] == "no outgoing changes"
    assert _verdict(rec, capsys).startswith("SKODUN VERDICT: trustworthy=true")


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def test_primary_checkout_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("SKODUN_ALLOW_MAIN", raising=False)
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    with pytest.raises(PreflightRefused):
        _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 0


def test_a_linked_worktree_needs_no_allow_main(tmp_path, monkeypatch):
    monkeypatch.delenv("SKODUN_ALLOW_MAIN", raising=False)
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "side", str(wt))
    (wt / "a.txt").write_text("three\n", encoding="utf-8")
    rec = _run(wt, _store(tmp_path))
    assert rec["branch"] == "side" and rec["status"] == "clean"


def test_a_config_without_a_finder_is_a_preflight_refusal(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "s"\nprovider = "xai"\nmodel = "m"\n'
        'role = "security"\n', encoding="utf-8")
    with pytest.raises(PreflightRefused):
        _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 0


# --------------------------------------------------------------------------
# the foreground lock
# --------------------------------------------------------------------------


def test_lock_lives_at_the_legacy_path_in_the_git_common_dir(tmp_path):
    repo = _repo(tmp_path)
    lock = pipeline._acquire_fg_lock(
        git_common_dir(repo), repo, wait=1, poll=0.1, stale=100, grace=30)
    try:
        assert lock.path == git_common_dir(repo) / "grok-reviews-foreground.lock"
        assert lock.path.is_dir()
    finally:
        pipeline._release_fg_lock(lock)


def test_owner_file_is_the_exact_legacy_byte_format(tmp_path):
    repo = _repo(tmp_path)
    lock = pipeline._acquire_fg_lock(
        git_common_dir(repo), repo, wait=1, poll=0.1, stale=100, grace=30)
    try:
        raw = (lock.path / "owner").read_text(encoding="utf-8")
        m = re.fullmatch(r"pid=(\d+)\nstarted=(\d+)\nworktree=(.+)\n", raw)
        assert m, repr(raw)
        assert int(m.group(1)) == os.getpid()
        assert abs(int(m.group(2)) - int(time.time())) < 60
        assert Path(m.group(3)) == repo.resolve()
        # ...and the legacy side's own parser reads our pid back out of it.
        legacy = subprocess.run(
            ["sed", "-n", "s/^pid=//p", str(lock.path / "owner")],
            capture_output=True, text=True, check=True).stdout.strip()
        assert legacy == str(os.getpid())
    finally:
        pipeline._release_fg_lock(lock)


def test_legacy_format_live_lock_is_respected(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    _write_owner(lock, os.getpid(), int(time.time()), repo)   # live pid
    with pytest.raises(LockTimeout):
        _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert _calls(tmp_path) == 0
    assert lock.is_dir()          # a live peer's lock is never stolen


def test_a_dead_owner_is_reclaimed(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    _write_owner(lock, _spawned_pid(), int(time.time()), repo)
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"
    assert not lock.exists()      # reclaimed, used, released


def test_an_unparsable_owner_is_owner_unknown_and_survives_the_write_grace(
        tmp_path):
    """A plain-integer (or any unparsable) owner file may be a holder that has
    not finished writing yet — only the write grace and the stale ceiling may
    reclaim it, never a pid check that cannot be made."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("4242\n", encoding="utf-8")   # not the format
    with pytest.raises(LockTimeout):
        _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert lock.is_dir()


def test_an_unparsable_owner_is_reclaimed_past_the_write_grace(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("4242\n", encoding="utf-8")
    old = time.time() - 600
    os.utime(lock, (old, old))
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"


def test_a_live_owner_past_the_stale_ceiling_is_reclaimed(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    # worst-case runtime = 2*1*(1+0+0)+60 = 62s, so a 600s-old lock is stale.
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    _write_owner(lock, os.getpid(), int(time.time()) - 600, repo)   # LIVE pid
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"


def test_release_is_a_no_op_when_someone_else_owns_the_lock(tmp_path):
    """ABA: a peer that reclaimed our lock must not have it deleted by us."""
    repo = _repo(tmp_path)
    lock = pipeline._acquire_fg_lock(
        git_common_dir(repo), repo, wait=1, poll=0.1, stale=100, grace=30)
    _write_owner(lock.path, os.getpid() + 1, int(time.time()), repo)
    try:
        assert pipeline._release_fg_lock(lock) is False
        assert lock.path.is_dir()
        assert (lock.path / "owner").read_text(encoding="utf-8").startswith(
            f"pid={os.getpid() + 1}\n")
    finally:
        # `rmtree`, not `unlink(owner) + rmdir`: a held lock also carries the
        # `budget` sidecar (see `test_batched_review.py`), so removing one named
        # file no longer empties the directory. Teardown only; every assertion
        # above is unchanged.
        shutil.rmtree(lock.path, ignore_errors=True)


def test_release_removes_the_lock_when_we_are_still_the_owner(tmp_path):
    repo = _repo(tmp_path)
    lock = pipeline._acquire_fg_lock(
        git_common_dir(repo), repo, wait=1, poll=0.1, stale=100, grace=30)
    assert pipeline._release_fg_lock(lock) is True
    assert not lock.path.exists()


def test_the_lock_is_released_after_a_normal_run(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    _run(repo, _store(tmp_path))
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_the_lock_ceiling_covers_the_extra_passes_that_run_inside_it(tmp_path):
    """A holder part-way through its security and skeptic passes is ALIVE.

    Both run inside the lock with their own full retry budgets, so a ceiling
    sized for a single reviewer run would let a peer reclaim a live holder's
    lock and put two reviews on one inference backend — the exact failure the
    lock exists to prevent. `recover_stale` keeps the narrower figure, which is
    the one the brief pins.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 100\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    d = load_config(repo).defaults
    assert pipeline.worst_runtime_sec(d) == 2 * 100 + 60
    assert pipeline.lock_stale_ceiling_sec(d) == 3 * (2 * 100) + 60

    # One-run-old: `recover_stale` sweeps the record...
    one_run_old = pipeline.worst_runtime_sec(d) + 5
    st = _store(tmp_path)
    _running(st, "sk_one_run_old", one_run_old)
    assert pipeline.recover_stale(st, load_config(repo)) == 1

    # ...and the lock of the same age is left exactly where it is.
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    _write_owner(lock, os.getpid(), int(time.time()) - one_run_old, repo)
    with pytest.raises(LockTimeout):
        _run(repo, st, lock_wait=1, lock_poll=0.2)
    assert lock.is_dir()
    assert _calls(tmp_path) == 0


def test_lock_wait_and_poll_read_env_overrides_and_ignore_junk(monkeypatch):
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "7.5")
    assert pipeline._env_seconds("SKODUN_LOCK_WAIT_SECONDS", 99.0) == 7.5
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "..")
    assert pipeline._env_seconds("SKODUN_LOCK_WAIT_SECONDS", 99.0) == 99.0
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "-3")
    assert pipeline._env_seconds("SKODUN_LOCK_WAIT_SECONDS", 99.0) == 99.0


# --------------------------------------------------------------------------
# stale-record recovery
# --------------------------------------------------------------------------


def _running(store: Store, rid: str, age_sec: float) -> None:
    store.save_review({
        "id": rid,
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - age_sec)),
        "branch": "feat", "base_sha": "b" * 40, "diff_hash": "d" * 40,
        "status": "running", "parse_ok": False, "degraded": False,
        "diff_truncated": False, "findings": [], "findings_total": 0,
    })


def test_recover_stale_fails_old_running_records_and_leaves_fresh_ones(tmp_path):
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)          # worst-case runtime = 62s
    st = _store(tmp_path)
    _running(st, "sk_old", 600)
    _running(st, "sk_fresh", 5)
    assert pipeline.recover_stale(st, cfg) == 1
    assert st.get_review("sk_old")["status"] == "failed"
    assert st.get_review("sk_fresh")["status"] == "running"


def test_recover_stale_scans_past_the_newest_reviews(tmp_path):
    """The sweep orders by `reviewed_at DESC`, so the records it exists to
    clean are the LAST ones it sees; a bounded scan would never reach them."""
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    st = _store(tmp_path)
    _running(st, "sk_old", 900)
    for i in range(40):
        _running(st, f"sk_new{i}", 1)
    assert pipeline.recover_stale(st, load_config(repo)) == 1
    assert st.get_review("sk_old")["status"] == "failed"


def test_recover_stale_ignores_finished_records_and_bad_timestamps(tmp_path):
    repo = _repo(tmp_path)
    st = _store(tmp_path)
    st.save_review({"id": "done", "reviewed_at": "1999-01-01T00:00:00Z",
                    "branch": "b", "base_sha": "s", "status": "clean",
                    "parse_ok": True, "degraded": False, "diff_truncated": False,
                    "findings": [], "findings_total": 0})
    st.save_review({"id": "junkts", "reviewed_at": "not-a-timestamp",
                    "branch": "b", "base_sha": "s", "status": "running",
                    "parse_ok": False, "degraded": False, "diff_truncated": False,
                    "findings": [], "findings_total": 0})
    assert pipeline.recover_stale(st, load_config(repo)) == 0
    assert st.get_review("done")["status"] == "clean"
    assert st.get_review("junkts")["status"] == "running"


def test_run_review_sweeps_stale_records_before_reviewing(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    st = _store(tmp_path)
    _running(st, "sk_old", 600)
    _run(repo, st)
    assert st.get_review("sk_old")["status"] == "failed"


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


def test_degraded_envelope_is_not_trustworthy(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CANCELLED))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["trustworthy"] is False and rec["status"] == "degraded"
    assert rec["parse_ok"] is True          # it parsed; the RUN was cut short
    assert "Cancelled" in rec["degraded_reason"]
    assert _calls(tmp_path) == 1
    assert _verdict(rec, capsys).startswith("SKODUN VERDICT: trustworthy=false")


def test_a_degraded_attempt_is_retried_in_a_fresh_run(tmp_path, capsys):
    _fake_grok(tmp_path, _per_call(_emit(CANCELLED), _emit(CLEAN)))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 1\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert _calls(tmp_path) == 2
    assert [a["n"] for a in rec["attempts"]] == [1, 2]
    assert all(a["timed_out"] is False for a in rec["attempts"])


def test_degraded_retries_exhausted_keeps_the_last_degraded_result(tmp_path,
                                                                   capsys):
    _fake_grok(tmp_path, _emit(CANCELLED))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 2\n")
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 3
    assert rec["degraded"] is True and rec["trustworthy"] is False
    assert len(rec["attempts"]) == 3


def test_a_timed_out_attempt_is_retried_and_a_clean_retry_wins(tmp_path, capsys):
    _fake_grok(tmp_path, _per_call(_emit_then_hang(CLEAN), _emit(CLEAN)))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 1\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert [a["timed_out"] for a in rec["attempts"]] == [True, False]
    assert rec["attempts"][0]["duration_sec"] >= 1.0


def test_a_timed_out_attempt_is_never_parsed(tmp_path, capsys):
    """The hung run printed a complete, clean-looking envelope. Parsing it
    would mint a trustworthy clean review from a run that never finished."""
    _fake_grok(tmp_path, _emit_then_hang(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["parse_ok"] is False and rec["trustworthy"] is False
    assert rec["status"] == "failed"
    assert rec["summary"] != "ok"                  # the envelope was NOT read
    assert rec["failure_reason"] == "timed out after 1 attempts"
    assert rec["stop_reason"] is None


def test_timeout_retries_exhausted_fails_closed(tmp_path, capsys):
    _fake_grok(tmp_path, _emit_then_hang(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 1\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 2
    assert rec["failure_reason"] == "timed out after 2 attempts"
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert [a["timed_out"] for a in rec["attempts"]] == [True, True]


def test_a_missing_reviewer_binary_is_recorded_and_never_retried(tmp_path,
                                                                 capsys,
                                                                 monkeypatch):
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "nope" / "grok"))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_retries = 2\n"
                           "degraded_retries = 2\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["parse_ok"] is False and rec["status"] == "failed"
    assert len(rec["attempts"]) == 1        # retrying cannot conjure a binary
    assert "not found" in rec["failure_reason"]
    assert _verdict(rec, capsys).startswith("SKODUN VERDICT: trustworthy=false")


# --------------------------------------------------------------------------
# size / truncation
# --------------------------------------------------------------------------


def test_oversized_diff_fails_closed(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\nmax_diff_bytes = 64\n")
    (repo / "a.txt").write_text("x" * 4096, encoding="utf-8")
    rec = _run(repo, _store(tmp_path))
    assert rec["diff_truncated"] is True and rec["trustworthy"] is False
    assert rec["status"] == "failed"


def test_context_pack_telemetry_is_recorded(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert "a.txt" in rec["context_files"]
    assert rec["context_bytes"] > 0
    prompt = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert "----- BEGIN FILE CONTEXT: a.txt -----" in prompt


def test_single_shot_does_not_repack_a_large_added_file(tmp_path, capsys):
    """`pack_large_added=False`: a single-shot `--now` diff already carries
    every added file whole, and packing the big one first would crowd the
    modified files out of the headroom."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    (repo / "big.txt").write_text("y" * 20000, encoding="utf-8")
    rec = _run(repo, _store(tmp_path))
    assert "big.txt (already-in-diff)" in rec["context_omitted_files"]
    assert "big.txt" not in rec["context_files"]
    assert "a.txt" in rec["context_files"]


def test_an_unconfigured_checklist_is_noted_but_never_untrustworthy(
        tmp_path, capsys):
    """The DEFAULT repo shape: no checklist directory at all.

    Fail-soft, and it must not read as a failure either -- this branch is hit
    on every run of every repo that has not opted into checklists.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)          # no docs/review/checklists directory
    rec = _run(repo, _store(tmp_path))
    assert rec["checklist_sections"] == []
    assert rec["checklist_note"].startswith("no checklist directory at ")
    assert "fail" not in rec["checklist_note"].lower()
    # `checklist_degraded` is FALSE for a total selection and TRUE only for a
    # partial degradation. It reads backwards; the record comment says so.
    assert rec["checklist_degraded"] is False
    assert rec["trustworthy"] is True     # fail-soft: rules dropped, not the review


def test_a_selected_checklist_reaches_the_prompt(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    cl = repo / "docs" / "review" / "checklists"
    cl.mkdir(parents=True)
    (cl / "core.md").write_text("- rule one\n", encoding="utf-8")
    rec = _run(repo, _store(tmp_path))
    assert rec["checklist_sections"] == ["core"]
    assert rec["checklist_bytes"] == len(b"- rule one\n")
    prompt = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert "----- BEGIN REPO RULES (path-scoped) -----" in prompt
    assert "- rule one" in prompt


# --------------------------------------------------------------------------
# extra passes
# --------------------------------------------------------------------------


def _risky_repo(tmp_path: Path, extra_cfg: str = "") -> Path:
    repo = _repo(tmp_path, extra_cfg)
    (repo / "auth").mkdir()
    (repo / "auth" / "session.py").write_text("token = 1\n", encoding="utf-8")
    return repo


def test_security_pass_runs_on_a_risky_path_and_merges(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(DIRTY)))
    repo = _risky_repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 2
    assert rec["extra_passes"]["security"]["ran"] is True
    assert rec["findings_total"] == 1
    # The title already opens with a [rule-id], so the lens tag goes to detail.
    assert "(extra-pass: security)" in rec["findings"][0]["detail"]
    assert rec["trustworthy"] is True
    # The pass saw the whole diff, so nothing claims partial coverage.
    assert "partial_coverage" not in rec["extra_passes"]["security"]
    assert rec["extra_passes"]["security"]["diff_truncated"] is False
    sec_prompt = (tmp_path / "bin" / "prompt_2.txt").read_text(encoding="utf-8")
    assert "SECURITY-FOCUSED code reviewer" in sec_prompt
    assert "Pass:   security (#3285)" in sec_prompt


def test_security_pass_is_not_scheduled_when_no_path_is_risky(tmp_path, capsys,
                                                              monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 1
    assert rec["extra_passes"] == {}     # never ran => nothing merged at all


def test_a_failed_security_pass_demotes_the_review(tmp_path, capsys,
                                                   monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), "exit 1"))
    repo = _risky_repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["parse_ok"] is False and rec["trustworthy"] is False
    assert rec["status"] == "failed"
    assert "security" in rec["failure_reason"]


def test_a_timed_out_security_pass_demotes_the_review(tmp_path, capsys,
                                                      monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit_then_hang(CLEAN)))
    repo = _risky_repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                                 "timeout_retries = 0\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["extra_passes"]["security"]["failed"] is True
    assert rec["parse_ok"] is False and rec["trustworthy"] is False


def test_skeptic_pass_runs_on_a_clean_review_and_can_break_the_clear(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(DIRTY)))
    repo = _repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 2
    assert rec["extra_passes"]["skeptic"]["ran"] is True
    assert rec["findings_total"] == 1
    assert rec["trustworthy"] is True
    assert _verdict(rec, capsys).startswith(
        "SKODUN VERDICT: trustworthy=true findings=1")
    prompt = (tmp_path / "bin" / "prompt_2.txt").read_text(encoding="utf-8")
    assert "ADVERSARIAL CLEAN-CHECK" in prompt


def test_skeptic_pass_is_not_scheduled_for_a_review_with_findings(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 1              # one model call: the finder's
    # ...and the only `extra_passes` entry is the refuter recording that this
    # config has none. Nothing ran, nothing merged.
    assert set(rec["extra_passes"]) == {"refuter"}
    assert rec["extra_passes"]["refuter"]["status"] == "skipped"


def test_skeptic_pass_is_not_scheduled_for_an_untrustworthy_review(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _emit(CANCELLED))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 1
    assert rec["extra_passes"] == {}


def test_skeptic_eligibility_is_judged_after_the_security_merge(
        tmp_path, capsys, monkeypatch):
    """Security found something, so the review is no longer a clean clear and
    the adversarial check has nothing left to disprove."""
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(DIRTY)))
    repo = _risky_repo(tmp_path)
    rec = _run(repo, _store(tmp_path))
    assert _calls(tmp_path) == 2                 # primary + security only
    assert set(rec["extra_passes"]) == {"security"}


def test_a_degraded_extra_pass_demotes_the_primary(tmp_path, capsys,
                                                   monkeypatch):
    """A Cancelled extra pass must take the primary's clean clear away.

    This is trust wiring nothing else pins END TO END: `_extra_pass` copies the
    pass's `degraded` flag into the record it hands `merge_extra_pass`, and
    hardcoding that copy to `False` leaves the rest of the suite green. A
    cancelled adversarial pass would then leave a clean, trustworthy primary
    standing — a false clear, which is the one outcome the gate must never be
    handed.
    """
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(CANCELLED)))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == 2
    meta = rec["extra_passes"]["skeptic"]
    assert meta["ran"] is True and meta["degraded"] is True
    assert meta["parse_ok"] is True        # it parsed; the RUN was cut short
    assert rec["degraded"] is True
    assert "Cancelled" in rec["degraded_reason"]
    # The demotion rides the degraded axis alone: the pass parsed fine.
    assert rec["parse_ok"] is True
    assert rec["trustworthy"] is False and rec["status"] == "degraded"
    assert rec["summary"].startswith("ok")   # the primary review is still here
    assert _verdict(rec, capsys).startswith(
        "SKODUN VERDICT: trustworthy=false findings=0 degraded=true")


def test_a_size_capped_extra_pass_records_partial_coverage(tmp_path, capsys,
                                                           monkeypatch):
    """`partial_coverage` comes from the PASS's own prompt, not the primary's.

    Telemetry only in Phase 1 — a capped pass records what it saw and demotes
    nothing — but the wiring from `Prompt.diff_truncated` through
    `merge_extra_pass` has to be pinned, or a later change could silently
    report full coverage for a pass that only ever saw the first few KB.
    """
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _risky_repo(tmp_path, "\n[defaults]\nmax_diff_bytes = 64\n")
    rec = _run(repo, _store(tmp_path))

    meta = rec["extra_passes"]["security"]
    assert meta["ran"] is True and meta["parse_ok"] is True
    assert meta["diff_truncated"] is True
    assert meta["partial_coverage"] is True
    assert "partial coverage" in rec["summary"]


@pytest.mark.parametrize("role", ["security", "refuter"])
def test_a_bad_extra_pass_provider_is_a_preflight_refusal(tmp_path, monkeypatch,
                                                          role):
    """Every reviewer this run MAY use is resolved before the lock.

    A typo in an extra pass's provider used to be found only after the primary
    review had already run, which spent a model call to demote its own result
    and report a config error as exit 4. It is a config error: refuse at
    preflight, exit 2, nothing run. Deliberately independent of whether the
    pass would have been scheduled — that depends on which files the change
    touches, and a config error should not be discovered by luck of the diff.
    """
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    (repo / ".skodun.toml").write_text(
        CFG + f'\n[[reviewers]]\nname = "extra"\nprovider = "no-such-provider"\n'
              f'model = "m"\nrole = "{role}"\n', encoding="utf-8")

    with pytest.raises(PreflightRefused) as e:
        _run(repo, _store(tmp_path))
    assert "no-such-provider" in str(e.value)
    assert _calls(tmp_path) == 0        # not one model call was spent on it
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_a_broken_extra_pass_demotes_the_review_instead_of_destroying_it(
        tmp_path, capsys, monkeypatch):
    """The primary already ran. An untrustworthy record the gate can refuse is
    worth far more than an exception that leaves only a `failed` stub.

    The break is injected at RUN time rather than through a bad provider in the
    config: that is now a preflight refusal (above), so what is left to reach
    this guard is exactly what it exists for — something that fails only after
    the primary review is already in hand.
    """
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    real = pipeline._run_chain

    def only_the_skeptic_explodes(head, cfg, d, prompt, cwd, store, scratch,
                                  tag, contract=REVIEW_CONTRACT):
        if tag == "skeptic":
            raise RuntimeError("adapter exploded mid-pass")
        return real(head, cfg, d, prompt, cwd, store, scratch, tag, contract)

    monkeypatch.setattr(pipeline, "_run_chain", only_the_skeptic_explodes)
    rec = _run(repo, _store(tmp_path))
    assert rec["extra_passes"]["skeptic"]["failed"] is True
    assert rec["parse_ok"] is False and rec["trustworthy"] is False
    assert "adapter exploded mid-pass" in rec["failure_reason"]
    assert rec["summary"] == "ok"          # the primary review is still here


def test_a_broken_extra_pass_prompt_demotes_the_review(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)

    def boom(*a, **kw):
        raise ValueError("cannot render")

    monkeypatch.setattr(pipeline.passes, "skeptic_prompt", boom)
    rec = _run(repo, _store(tmp_path))
    assert rec["extra_passes"]["skeptic"]["failed"] is True
    assert rec["trustworthy"] is False
    assert _calls(tmp_path) == 1


def test_extra_passes_run_while_the_lock_is_still_held(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    lock_seen = []
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    _fake_grok(tmp_path, _emit(CLEAN))
    real = pipeline.passes.skeptic_prompt

    def spy(*a, **kw):
        lock_seen.append(lock.is_dir())
        return real(*a, **kw)

    monkeypatch.setattr(pipeline.passes, "skeptic_prompt", spy)
    _run(repo, _store(tmp_path))
    assert lock_seen == [True]


# --------------------------------------------------------------------------
# crash safety
# --------------------------------------------------------------------------


def test_a_crash_mid_run_releases_the_lock_and_downgrades_the_record(
        tmp_path, monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(runner, "run_with_watchdog", boom)
    with pytest.raises(RuntimeError):
        _run(repo, st)

    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()
    rows = st.list_reviews(None, 10)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"      # never left `running`
    assert rows[0]["trustworthy"] is False


def test_a_dead_stdout_never_downgrades_an_already_persisted_record(
        tmp_path, monkeypatch):
    """`skodun review | head` closes stdout under us. The record was already
    saved and is correct; nothing on the way out may rewrite it to `failed`.

    This used to inject a `BrokenPipeError` into `pipeline._emit_banner`. That
    function is GONE -- the pipeline no longer writes to stdout at all, because
    `skodun mcp` serves JSON-RPC on that stream from another thread -- so the
    property is now stronger, and is asserted as such: with a stdout on which
    EVERY operation raises, `run_review` still returns a clean, trustworthy,
    persisted record and still releases the lock. The verdict line is the
    caller's to write (`services.svc_review` renders it, `cli._emit` writes it),
    strictly after the record was persisted and the `finally` block ran, and
    `_emit` reports the code rather than the write failure.
    """

    class _DeadStdout:
        """Every operation raises: not merely a closed pipe, an unusable stream."""

        def write(self, *_a, **_k):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self, *_a, **_k):
            raise BrokenPipeError(32, "Broken pipe")

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)

    monkeypatch.setattr(sys, "stdout", _DeadStdout())
    rec = _run(repo, st)

    stored = st.list_reviews(None, 10)[0]
    assert rec["id"] == stored["id"]
    assert stored["status"] == "clean" and stored["trustworthy"] is True
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_a_persistence_failure_is_reported_as_no_review_recorded(tmp_path,
                                                                 monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)
    calls = {"n": 0}
    real = st.save_review

    def flaky(rec):
        calls["n"] += 1
        if calls["n"] > 1:                    # the FINAL save fails
            raise RuntimeError("disk full")
        real(rec)

    monkeypatch.setattr(st, "save_review", flaky)
    with pytest.raises(PersistenceFailed):
        _run(repo, st)
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()
    assert st.list_reviews(None, 10)[0]["status"] == "failed"


# --------------------------------------------------------------------------
# the CLI seam: exit codes and the banner invariant
# --------------------------------------------------------------------------


def _cli(repo: Path, capsys) -> tuple[int, str]:
    code = main(["review", "--repo", str(repo)])
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "the CLI printed nothing to stdout"
    return code, out[-1]


def test_cli_clean_review_exits_0_with_a_banner(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    code, last = _cli(repo, capsys)
    assert code == 0
    assert last.startswith("SKODUN VERDICT: trustworthy=true findings=0")


def test_cli_findings_exit_1_with_a_banner(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _repo(tmp_path)
    code, last = _cli(repo, capsys)
    assert code == 1
    assert last.startswith("SKODUN VERDICT: trustworthy=true findings=1")


def test_cli_preflight_refusal_exits_2_with_a_banner(tmp_path, capsys,
                                                     monkeypatch):
    monkeypatch.delenv("SKODUN_ALLOW_MAIN", raising=False)
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    code, last = _cli(repo, capsys)
    assert code == 2
    assert last.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "SKODUN_ALLOW_MAIN" in last


def test_cli_lock_give_up_exits_3_with_a_banner(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "1")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.2")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    _write_owner(git_common_dir(repo) / "grok-reviews-foreground.lock",
                 os.getpid(), int(time.time()), repo)
    code, last = _cli(repo, capsys)
    assert code == 3
    assert last.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert _calls(tmp_path) == 0


def test_cli_untrustworthy_review_exits_4_with_a_banner(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CANCELLED))
    repo = _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")
    code, last = _cli(repo, capsys)
    assert code == 4
    assert last.startswith("SKODUN VERDICT: trustworthy=false findings=0")


def test_cli_persistence_failure_exits_4_saying_nothing_was_recorded(
        tmp_path, capsys, monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)

    def boom(self, rec):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "save_review", boom)
    code, last = _cli(repo, capsys)
    assert code == 4
    assert last == "SKODUN VERDICT: trustworthy=false reason=no review was recorded"


def test_cli_a_broken_config_is_a_preflight_refusal(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\nmax_diff_bytes = 0\n", encoding="utf-8")
    code, last = _cli(repo, capsys)
    assert code == 2
    assert last.startswith("SKODUN VERDICT: trustworthy=false reason=")


def test_cli_a_non_git_directory_is_a_preflight_refusal_not_a_failed_review(
        tmp_path, capsys):
    """Exit 2, not 4. `gitio` raises `GitError` before anything can run, and 4
    would report "no trustworthy review exists" about a review that was never
    attempted — the difference between "your config is wrong" and "the model
    let you down"."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    code, last = _cli(plain, capsys)
    assert code == 2
    assert last.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "no review ran" in last
    assert _calls(tmp_path) == 0


def test_cli_uses_the_pinned_store(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    assert _cli(repo, capsys)[0] == 0
    st = Store.open(Path(os.environ["SKODUN_DB"]))
    assert len(st.list_reviews(None, 10)) == 1


def test_cli_review_from_a_subdirectory_behaves_as_from_the_root(tmp_path,
                                                                 capsys):
    """`--repo` may name any directory inside the worktree.

    `load_config` reads `<its argument>/.skodun.toml`, so pointing `--repo` at
    a subdirectory used to load an EMPTY config -- no reviewers at all -- and
    the run was refused with "no enabled reviewer with role 'finder' is
    configured", blaming the user's reviewer table for their cwd. The identity
    was never in question (the pipeline normalises to the worktree root before
    capturing a diff); the config was, and the two must agree.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()

    assert main(["review", "--repo", str(sub)]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("SKODUN VERDICT: trustworthy=true findings=0")

    st = Store.open(Path(os.environ["SKODUN_DB"]))
    (rec,) = st.list_reviews(None, 10)
    # The record is rooted, not cwd-scoped: same identity a root run produces.
    base = resolve_base(repo)
    assert rec["diff_hash"] == diff_identity(capture_diff(repo, base.sha, 100).data)
    assert rec["model"] == "grok-4.20-0309-reasoning"   # the ROOT's config


# --------------------------------------------------------------------------
# review -> gate: the one seam nothing else spans
# --------------------------------------------------------------------------


def _gate(repo: Path, capsys) -> int:
    code = main(["gate", "--repo", str(repo)])
    capsys.readouterr()
    return code


def _banner_id(out: str) -> str:
    """The review id off the banner the run just printed.

    Deliberately NOT "the newest row in the store": `reviewed_at` has
    second resolution, so two reviews in the same test can tie and
    `ORDER BY reviewed_at DESC LIMIT 1` then picks arbitrarily -- which showed
    up as this test passing with the oracle present (slow enough to straddle a
    second) and failing without it.
    """
    m = re.search(r"\bid=(\S+)", out.strip().splitlines()[-1])
    assert m, f"no id= in the banner: {out!r}"
    return m.group(1)


def test_a_review_satisfies_the_gate_it_was_taken_for(tmp_path, capsys):
    """The bridge: `run_review` writes an identity, `run_gate` recomputes one.

    Every other test in the suite stops on one side of that seam -- the closest
    goes as far as the artifact validator -- so a divergence between the
    `diff_hash` the pipeline STORES and the one the gate COMPUTES would pass
    the whole suite while making the gate unsatisfiable in practice: a review
    would run, record cleanly, and the very next `skodun gate` would still say
    "no trustworthy review covers this". Both halves go through the CLI here,
    so they share the pinned store exactly as a real pre-push hook would.

    All three outcomes of the contract, in the order a developer meets them:
    clean review -> 0; a finding -> 1 until it is triaged, then 0; and an edit
    made after the review -> 2, because the gate keys on content, not on HEAD.
    """
    repo = _repo(tmp_path)

    # 1. a clean review, and the gate it was taken for accepts it.
    _fake_grok(tmp_path, _emit(CLEAN))
    assert main(["review", "--repo", str(repo)]) == 0
    capsys.readouterr()
    assert _gate(repo, capsys) == 0

    # 2. an edit AFTER the review changes the content, so nothing covers it.
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    assert _gate(repo, capsys) == 2

    # 3. a review of the NEW content that finds something: 1, not 0...
    _fake_grok(tmp_path, _emit(DIRTY))
    assert main(["review", "--repo", str(repo)]) == 1
    rid = _banner_id(capsys.readouterr().out)
    assert _gate(repo, capsys) == 1

    # ...and an audited dismissal -- nothing else -- is what clears it.
    assert main(["triage", rid, "0",
                 "checked: the caller validates this input two frames up"]) == 0
    capsys.readouterr()
    assert _gate(repo, capsys) == 0


def test_review_subcommand_is_registered(capsys):
    from skodun.cli import build_parser
    args = build_parser().parse_args(["review", "--repo", "x"])
    assert args.command == "review" and str(args.repo) == "x"


# ---------------------------------------------------------------------------
# Phase 3 Task 1: `pipeline._TS_FORMAT` is imported, not re-spelled
# ---------------------------------------------------------------------------

def test_ts_format_is_imported_from_store_not_a_second_literal():
    """`_iso_at`, `_iso_now` and `_epoch` each used to spell out
    `"%Y-%m-%dT%H:%M:%SZ"` directly -- three copies of one format the store
    owns and validates against. A single `_TS_FORMAT`, imported from `store`,
    replaces all three, so a future change to the canonical format only has
    one place to make it (well, two counting `gate.py`'s own copy -- that one
    is explicitly out of scope for this task; see its byte-pledge)."""
    import inspect

    from skodun import store

    assert pipeline._TS_FORMAT is store._TS_FORMAT
    src = inspect.getsource(pipeline)
    assert "%Y-%m-%dT%H:%M:%SZ" not in src, (
        "pipeline.py still spells out the timestamp format literal directly "
        "somewhere instead of using the imported store._TS_FORMAT")
