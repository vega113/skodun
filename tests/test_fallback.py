"""Fallback chains: hopping on `unavailable`, and failing closed when exhausted.

Every test here drives the REAL pipeline against a REAL git repo and REAL child
processes. The only fakes are the provider CLIs themselves — shell scripts on
disk that the per-adapter env overrides (`SKODUN_GROK_BIN`, `SKODUN_CODEX_BIN`)
point at. Binary control is ALWAYS through those overrides and never through
`PATH`: `adapters.grok.resolve_grok_bin` prefers `~/.grok/bin/grok` over `PATH`,
so a PATH-only fake silently loses on any machine that has grok installed, and
the suite would then be running the developer's real, paid CLI.

The four rules under test, and why each one is a safety property rather than a
convenience:

* **`unavailable` advances the chain, `degraded` does not.** A provider that
  could not serve is worth routing around; a provider that *answered badly* is
  a harness problem, and hopping on it would mask exactly the bug the degraded
  retry exists to surface.
* **Only `quota` is cached provider-wide.** Caching an `auth`, `binary` or
  `model` failure would let one mistyped model id black-hole a whole provider
  for half an hour — including for every other reviewer entry that uses it.
* **An exhausted chain fails closed.** It produces an explicit `failed`,
  untrustworthy record, and the gate then answers 2 for content nothing has
  reviewed. It never mints a pass out of its own failure.
* **A quota outage cannot un-review already-reviewed bytes.** The failed record
  does not delete older coverage: the gate asks for the NEWEST trustworthy row
  for the diff hash and holds THAT row to every artifact check, `base_sha`
  included.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from skodun import pipeline, runner
from skodun.adapters import REVIEW_CONTRACT
from skodun.adapters.grok import GrokAdapter
from skodun.cli import main
from skodun.config import Defaults, load_config
from skodun.gate import run_gate
from skodun.gitio import (capture_diff, current_branch, diff_identity,
                          git_common_dir, resolve_base)
from skodun.pipeline import PreflightRefused, lock_stale_ceiling_sec, run_review, worst_runtime_sec
from skodun.store import Store
from tests.test_gitio import _git, _mkrepo
from tests.test_pipeline import (CANCELLED, CLEAN, DIRTY, _emit, _emit_then_hang,
                                 _per_call, _verdict)

# --------------------------------------------------------------------------
# configs under test
# --------------------------------------------------------------------------

# Deliberately unmistakable placeholder ids: no test here talks to a real
# provider, and a plausible-looking id in a committed file reads as a
# recommendation nobody verified.
FAKE_XAI_MODEL = "grok-4.20-0309-reasoning"
FAKE_OPENAI_MODEL = "gpt-test-0309"

_HEAD_OPENAI = f"""
[[reviewers]]
name = "primary"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"
effort = "high"
fallbacks = ["backup"]
"""

_HEAD_XAI = f"""
[[reviewers]]
name = "primary"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "finder"
effort = "medium"
fallbacks = ["backup"]
"""

_ENTRY_XAI = f"""
[[reviewers]]
name = "backup"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "finder"
effort = "low"
"""

_ENTRY_OPENAI = f"""
[[reviewers]]
name = "backup"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"
"""

CFG_OPENAI_THEN_XAI = _HEAD_OPENAI + _ENTRY_XAI
CFG_XAI_THEN_OPENAI = _HEAD_XAI + _ENTRY_OPENAI

# A junie head with a working fallback: the shape issue #92 was reported on,
# where the head's own wrapper is what fails.
CFG_JUNIE_THEN_XAI = """
[[reviewers]]
name = "primary"
provider = "junie"
model = "junie-test-0309"
role = "finder"
fallbacks = ["backup"]
""" + _ENTRY_XAI

CFG_XAI_ONLY = f"""
[[reviewers]]
name = "primary"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "finder"
"""

CFG_XAI_THEN_XAI = _HEAD_XAI + f"""
[[reviewers]]
name = "backup"
provider = "xai"
model = "{FAKE_XAI_MODEL}-b"
role = "finder"
"""

CFG_WITH_UNKNOWN_FALLBACK_PROVIDER = _HEAD_XAI + """
[[reviewers]]
name = "backup"
provider = "no-such-provider"
model = "m"
role = "finder"
"""

CFG_FOUR_ENTRY = f"""
[[reviewers]]
name = "primary"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "finder"
fallbacks = ["b1", "b2", "b3"]

[[reviewers]]
name = "b1"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"

[[reviewers]]
name = "b2"
provider = "xai"
model = "{FAKE_XAI_MODEL}-2"
role = "finder"

[[reviewers]]
name = "b3"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}-3"
role = "finder"
"""

# A grok model that rejects `--effort` at all, paired with an effort: the
# adapter raises out of `build_cmd` before any process starts.
CFG_UNBUILDABLE_HEAD = f"""
[[reviewers]]
name = "primary"
provider = "xai"
model = "grok-build-fast-1"
role = "finder"
effort = "high"
fallbacks = ["backup"]
""" + _ENTRY_XAI


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """The same isolation `tests/test_pipeline.py` enforces, plus codex.

    Nothing here may reach the developer's real store, their real `~/.grok`,
    their real `codex` on PATH, or their global skodun config. Every provider
    binary is pinned into `tmp_path` BY ITS OWN override, so a test that forgets
    to install a fake gets a missing binary rather than a live model call.
    """
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "bin" / "codex"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.delenv("SKODUN_IGNORE_PROVIDER_STATE", raising=False)
    monkeypatch.delenv("SKODUN_GATE_SKIP", raising=False)
    # A short wait: the production default is the config's worst-case runtime,
    # so a regression in the lock would hang the suite rather than fail it.
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


def _fake_cli(tmp_path: Path, name: str, body: str) -> Path:
    """Install a fake provider CLI called `name` whose `body` decides one call.

    Extends Phase 1's `_fake_grok` with two things the chain tests need: the
    invocation log is SHARED across binaries (so the order providers were tried
    in is recoverable), and every call records its stdin and its
    `--output-schema` sidecar (codex takes the prompt on stdin and names a
    schema file in the argv; both are Task 5/7 wiring nothing else pins
    end-to-end).
    """
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    g = b / name
    g.write_text(
        "#!/bin/sh\n"
        'D="$(cd "$(dirname "$0")" && pwd)"\n'
        f'echo {name} >> "$D/calls.log"\n'
        'CALL=$(wc -l < "$D/calls.log" | tr -d " ")\n'
        'printf "%s\\n" "$@" > "$D/argv_$CALL.log"\n'
        'cat > "$D/stdin_$CALL.txt"\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  [ "$prev" = "--prompt-file" ] && cp "$a" "$D/prompt_$CALL.txt"\n'
        '  [ "$prev" = "--output-schema" ] && cp "$a" "$D/schema_$CALL.json" '
        '&& echo "$a" > "$D/schemapath_$CALL.txt"\n'
        '  prev="$a"\n'
        "done\n"
        f"{body}\n",
        encoding="utf-8")
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    return g


def _codex_stream(payload: dict, terminal: str = "turn.completed") -> str:
    """A codex-shaped JSONL event stream carrying `payload` as the answer."""
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": json.dumps(payload)}}),
        json.dumps({"type": terminal}),
    ]
    return "\n".join(lines)


CODEX_CLEAN = _codex_stream({"summary": "ok", "findings": []})
CODEX_CUT_OFF = _codex_stream({"summary": "ok", "findings": []},
                              terminal="turn.failed")


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "bin" / "calls.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").split()


def _repo(tmp_path: Path, cfg_text: str, extra: str = "") -> Path:
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(cfg_text + extra, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo


def _store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "s.db")


def _run(repo: Path, store: Store, **kw) -> dict:
    return run_review(repo, load_config(repo), store, **kw)


def _gate(repo: Path, store: Store) -> int:
    return run_gate(store, repo, load_config(repo)).code


def _iso(offset_sec: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset_sec))


def _seed_trustworthy_review_for_current_diff(
        store: Store, repo: Path, *, rid: str = "seed",
        reviewed_at: str = "2020-01-01T00:00:00Z",
        base_sha: str | None = None) -> dict:
    """A trustworthy record covering exactly the repo's current outgoing diff.

    Written through `save_review`, so `trustworthy` is computed by the store
    from the three axes exactly as a real review's would be.
    """
    base = resolve_base(repo)
    d = load_config(repo).defaults
    diff = capture_diff(repo, base.sha, d.untracked_max)
    rec = {
        "id": rid, "reviewed_at": reviewed_at, "source": "skodun",
        "branch": current_branch(repo), "head": "0" * 40,
        "base_ref": base.ref, "base_sha": base.sha if base_sha is None else base_sha,
        "diff_hash": diff_identity(diff.data), "context_hash": "",
        "mode": "now", "model": FAKE_XAI_MODEL, "adapter": "grok",
        "status": "clean", "parse_ok": True, "degraded": False,
        "diff_truncated": False, "stop_reason": "EndTurn",
        "findings": [], "findings_total": 0,
        "severity": {"high": 0, "medium": 0, "low": 0}, "rule_ids": [],
        "summary": "seeded coverage", "extra_passes": {}, "failure_reason": "",
        "attempts": [],
    }
    store.save_review(rec)
    return rec


# --------------------------------------------------------------------------
# the chain itself
# --------------------------------------------------------------------------


def test_fallback_chain_recovers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)

    rec = _run(repo, _store(tmp_path))

    assert rec["trustworthy"] is True
    first = rec["attempts"][0]
    assert first["provider"] == "openai"
    assert first["model"] == FAKE_OPENAI_MODEL and first["effort"] == "high"
    # Asserted DIRECTLY off the recorded classification, never inferred from rc:
    # the whole point of Task 1's `classify` is that the chain switches on a
    # provider-neutral verdict rather than on one CLI's exit codes.
    assert first["classification"]["kind"] == "unavailable"
    assert first["classification"]["category"] == "binary"
    # The accepted attempt's identity, exactly -- not a negative assertion.
    assert rec["adapter"] == "grok" and rec["model"] == FAKE_XAI_MODEL
    second = rec["attempts"][1]
    assert second["provider"] == "xai" and second["effort"] == "low"
    assert second["classification"] == {"kind": "ok", "category": "", "detail": ""}
    assert second["rc"] == 0 and second["timed_out"] is False
    assert _calls(tmp_path) == ["grok"]      # the dead head never spawned


def test_spawn_permission_error_advances_to_fallback(tmp_path, monkeypatch,
                                                     capsys):
    """A real Popen permission error is an invocation-unavailable hop."""
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\nprintf never-runs\n", encoding="utf-8")
    # Leave the file non-executable: chain sees it exists, then the real
    # watchdog's Popen raises PermissionError before any stderr is captured.
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)

    rec = _run(repo, _store(tmp_path))

    assert rec["trustworthy"] is True
    first, second = rec["attempts"]
    assert first["provider"] == "openai"
    assert first["classification"]["kind"] == "unavailable"
    assert first["classification"]["category"] == "invocation"
    assert "permission" in first["classification"]["detail"].lower()
    assert second["provider"] == "xai"
    assert second["classification"] == {"kind": "ok", "category": "", "detail": ""}


def test_exhausted_chain_fails_closed_and_gate_semantics(tmp_path, monkeypatch,
                                                         capsys):
    _fake_cli(tmp_path, "codex", "exit 127")
    _fake_cli(tmp_path, "grok", "exit 127")
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "unavailable" in rec["failure_reason"]
    assert [a["provider"] for a in rec["attempts"]] == ["openai", "xai"]
    # `run_review` prints nothing; the banner is rendered from what it returned.
    assert _verdict(rec, capsys).startswith("SKODUN VERDICT: trustworthy=false")
    # Nothing covers this content, so the gate refuses it.
    assert _gate(repo, st) == 2
    # ...and the invariant in the other direction: a quota outage cannot
    # un-review bytes that were already reviewed.
    _seed_trustworthy_review_for_current_diff(st, repo)
    assert _gate(repo, st) == 0


def test_a_rebased_base_is_not_covered_even_by_matching_bytes(tmp_path):
    """Identical diff bytes taken against a different merge-base gate 2.

    The dismissals a review carries are scoped to its own base, so accepting it
    after a rebase would keep alive exactly the amnesty the rebase re-opens.
    """
    repo = _repo(tmp_path, CFG_XAI_ONLY)
    st = _store(tmp_path)
    _seed_trustworthy_review_for_current_diff(st, repo, base_sha="f" * 40)
    assert _gate(repo, st) == 2


def test_newest_trustworthy_row_wins_even_when_an_older_one_matches_the_base(
        tmp_path):
    """`latest_trustworthy_for` is newest-wins, and the gate holds THAT row to
    every artifact check. An older row at the right base does not rescue a
    newer one taken against a different base -- shipped, fail-closed behavior,
    pinned here because the chain's failure path now depends on it."""
    repo = _repo(tmp_path, CFG_XAI_ONLY)
    st = _store(tmp_path)
    _seed_trustworthy_review_for_current_diff(
        st, repo, rid="older-matching", reviewed_at="2020-01-01T00:00:00Z")
    assert _gate(repo, st) == 0
    _seed_trustworthy_review_for_current_diff(
        st, repo, rid="newer-rebased", reviewed_at="2030-01-01T00:00:00Z",
        base_sha="f" * 40)
    assert _gate(repo, st) == 2


def test_degraded_does_not_hop_providers(tmp_path, monkeypatch, capsys):
    _fake_cli(tmp_path, "grok", _emit(CANCELLED))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_XAI_THEN_OPENAI)

    rec = _run(repo, _store(tmp_path))

    assert all(a["provider"] == "xai" for a in rec["attempts"])
    assert rec["trustworthy"] is False
    assert _calls(tmp_path) == ["grok", "grok"]     # the retry, not the hop
    assert rec["degraded"] is True


def test_a_broken_junie_harness_hops_instead_of_burning_the_entry(
        tmp_path, monkeypatch, capsys):
    """Issue #92, end to end through the real chain and the real adapter.

    The outer junie runner refuses when it cannot read the output envelope it
    staged. That refusal used to classify as `degraded`, which is a verdict
    about a REVIEW -- so the chain correctly refused to hop, spent both of the
    entry's attempts on a wrapper that could not produce anything, and returned
    `trustworthy=false findings=0` while a configured fallback sat idle.

    The child process is faked (it is `python -I -c ... junie_runner`, and what
    is under test is what the chain does with its output, not Seatbelt); the
    classification, the chain and the record are all real.
    """
    monkeypatch.setenv("SKODUN_JUNIE_BIN", "/bin/sh")   # present, never run
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    spawned: list[str] = []

    real_watchdog = runner.run_with_watchdog

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        if "junie_runner" not in " ".join(cmd):
            return real_watchdog(cmd, timeout_sec, cwd, out, err,
                                 stdin_path=stdin_path, cancel=cancel)
        spawned.append("junie")
        out.write_bytes(b"")
        err.write_bytes(b"junie envelope refused: Expecting value: line 1 "
                        b"column 1 (char 0)\n")
        return runner.RunResult(rc=2, timed_out=False, duration_sec=1.7,
                                first_output_sec=None)

    monkeypatch.setattr(runner, "run_with_watchdog", fake)
    repo = _repo(tmp_path, CFG_JUNIE_THEN_XAI,
                 "\n[defaults]\ndegraded_retries = 1\n")

    rec = _run(repo, _store(tmp_path))

    # ONE junie attempt, not two: the entry could not serve, so the retry
    # budget is not what this failure spends.
    assert spawned == ["junie"]
    assert _calls(tmp_path) == ["grok"]
    junie_attempt, grok_attempt = rec["attempts"]
    assert junie_attempt["classification"] == {
        "kind": "unavailable", "category": "harness",
        "detail": junie_attempt["classification"]["detail"]}
    # ...and the root cause the runner interpolated is IN the record, which is
    # the other half of #92: nothing used to persist it.
    assert "Expecting value: line 1 column 1" in junie_attempt["classification"]["detail"]
    assert grok_attempt["classification"]["kind"] == "ok"
    assert rec["trustworthy"] is True and rec["adapter"] == "grok"


def test_a_classify_only_degradation_consumes_the_retry_and_demotes(
        tmp_path, monkeypatch, capsys):
    """The two degraded signals are OR-ed, and `classify`'s alone is enough.

    Both shipped adapters happen to report a truncated run on BOTH axes today,
    so nothing else in the suite would notice a chain that read only
    `ParseResult.degraded`. Here `parse` is forced to report a healthy run
    while `classify` still sees the truncation: the retry must still be spent
    and the record must still be demoted, or a provider whose degradation is
    only visible to `classify` mints a clean review out of a cut-off run.
    """
    real = GrokAdapter.parse

    def never_degraded(self, stdout, stderr, contract=REVIEW_CONTRACT):
        return replace(real(self, stdout, stderr, contract),
                       degraded=False, degraded_reason="")

    monkeypatch.setattr(GrokAdapter, "parse", never_degraded)
    _fake_cli(tmp_path, "grok", _emit(CANCELLED))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_XAI_THEN_OPENAI,
                 "\n[defaults]\ndegraded_retries = 1\n")

    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == ["grok", "grok"]   # retried, and never hopped
    assert rec["degraded"] is True and rec["trustworthy"] is False
    assert "Cancelled" in rec["degraded_reason"]


def test_a_codex_stream_degradation_consumes_the_same_retry_budget(
        tmp_path, monkeypatch, capsys):
    """A truncated codex stream gets the retry a truncated grok envelope gets.

    `turn.failed` arrives with a perfectly valid payload already on the wire,
    so the run is only distinguishable from a healthy one by the terminal
    event. Accepting it would be the Phase 1 silent false all-clear with a new
    provider's name on it.
    """
    _fake_cli(tmp_path, "codex", _per_call(_emit(CODEX_CUT_OFF),
                                           _emit(CODEX_CLEAN)))
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, _HEAD_OPENAI + _ENTRY_XAI,
                 "\n[defaults]\ndegraded_retries = 1\n")

    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == ["codex", "codex"]   # retried, never hopped
    assert rec["attempts"][0]["classification"]["kind"] == "degraded"
    assert rec["attempts"][1]["classification"]["kind"] == "ok"
    assert rec["trustworthy"] is True
    assert rec["adapter"] == "codex" and rec["model"] == FAKE_OPENAI_MODEL


def test_a_first_entry_timeout_stops_the_chain(tmp_path, monkeypatch, capsys):
    """A timeout is not an availability verdict: the provider answered, slowly.

    Hopping on it would spend a second provider's quota on what is most likely
    an oversized prompt or a wedged harness, and would do it silently.
    """
    _fake_cli(tmp_path, "grok", _emit_then_hang(CLEAN))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_XAI_THEN_OPENAI,
                 "\n[defaults]\ntimeout_sec = 1\ntimeout_retries = 0\n"
                 "degraded_retries = 0\n")

    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == ["grok"]
    assert rec["failure_reason"] == "timed out after 1 attempts"
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    # A timed-out attempt has nothing to classify: its stdout was truncated to
    # zero bytes precisely so a hung run's clean-looking envelope cannot be
    # read. `timed_out`/`duration_sec` carry that story instead.
    only = rec["attempts"][0]
    assert only["classification"] is None
    assert only["timed_out"] is True and only["duration_sec"] >= 1.0


def test_a_build_cmd_failure_stops_the_chain_and_never_starts_a_process(
        tmp_path, monkeypatch, capsys):
    """An effort the adapter cannot express is a LOUD config error.

    Routing around it would review at some other provider's default effort and
    say nothing -- the unnoticed downgrade `build_cmd` raises to prevent.
    """
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_UNBUILDABLE_HEAD)

    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == []          # nothing spawned, head or fallback
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "grok-build-fast-1" in rec["failure_reason"]
    only = rec["attempts"][0]
    assert only["provider"] == "xai" and only["rc"] is None
    assert only["timed_out"] is None and only["classification"] is None
    assert "skipped" in only


def test_an_exhausted_chain_is_exit_4_through_the_cli(tmp_path, monkeypatch,
                                                      capsys):
    """4, not 2: the config was fine and the review was really attempted --
    what is missing is a trustworthy result, which is exactly what 4 says."""
    _fake_cli(tmp_path, "codex", "exit 127")
    _fake_cli(tmp_path, "grok", "exit 127")
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)

    code = main(["review", "--repo", str(repo)])

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 4
    assert out[-1].startswith("SKODUN VERDICT: trustworthy=false findings=0")
    st = Store.open(Path(os.environ["SKODUN_DB"]))
    (rec,) = st.list_reviews(None, 10)
    assert rec["status"] == "failed"


#: The keys EVERY `attempts[]` row carries, whatever kind of row it is.
#: `skipped` is the one optional key, and it appears exactly on the rows where
#: no process ever started.
_ATTEMPT_KEYS = {"n", "provider", "model", "effort", "rc", "timed_out",
                 "duration_sec", "first_output_sec", "classification"}


def _assert_attempt_schema(row: dict) -> None:
    assert _ATTEMPT_KEYS <= set(row), sorted(_ATTEMPT_KEYS - set(row))
    assert set(row) <= (_ATTEMPT_KEYS | {
        "skipped", "execution_provenance", "capacity_timing"}), \
        sorted(set(row) - _ATTEMPT_KEYS)
    if "execution_provenance" in row:
        assert isinstance(row["execution_provenance"], dict)
        assert set(row["execution_provenance"]) <= {
            "adapter", "resolved", "version", "override_source",
        }
    if "capacity_timing" in row:
        assert set(row["capacity_timing"]) <= {
            "queued_at", "admitted_at", "started_at", "ended_at",
            "wait_ms", "queue_wait_ms",
        }


def test_every_attempt_row_carries_the_complete_schema(tmp_path, monkeypatch,
                                                       capsys):
    """One schema, all five row kinds, read back out of the STORE.

    A row that quietly omits a field is worse than a missing row: `attempts[]`
    is the only account of what a chain did, and a reader who finds no `rc` on
    one row and `rc: 0` on another cannot tell "did not run" from "ran fine".
    The five kinds are collected from three runs because no single run can
    produce all of them, and each kind is asserted to have actually appeared --
    otherwise this test would keep passing while covering fewer of them.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    _fake_cli(tmp_path, "grok", _per_call(_emit(CLEAN), _emit(CANCELLED),
                                          _emit_then_hang(CLEAN)))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI,
                 "\n[defaults]\ntimeout_sec = 1\ntimeout_retries = 0\n"
                 "degraded_retries = 0\n")

    # 1. a cache skip, then a completed `ok` attempt.
    st_a = Store.open(tmp_path / "a.db")
    st_a.mark_provider_unavailable("openai", "rate limited", "quota", _iso(3600))
    skip_row, ok_row = _run(repo, st_a)["attempts"]

    # 2. a missing binary, then a completed `degraded` attempt.
    binary_row, degraded_row = _run(repo, Store.open(tmp_path / "b.db"))["attempts"]

    # 3. a missing binary, then a timeout.
    _, timeout_row = _run(repo, Store.open(tmp_path / "c.db"))["attempts"]

    for row in (skip_row, ok_row, binary_row, degraded_row, timeout_row):
        _assert_attempt_schema(row)
        assert isinstance(row["n"], int) and row["provider"] and row["model"]

    assert skip_row["skipped"].startswith("provider marked unavailable")
    assert skip_row["classification"]["category"] == "quota"
    assert (skip_row["rc"], skip_row["timed_out"], skip_row["duration_sec"],
            skip_row["first_output_sec"]) == (None, None, None, None)

    assert ok_row["classification"] == {"kind": "ok", "category": "", "detail": ""}
    assert ok_row["rc"] == 0 and ok_row["timed_out"] is False
    assert isinstance(ok_row["duration_sec"], float)

    assert binary_row["skipped"].startswith("binary not found")
    assert binary_row["classification"]["category"] == "binary"
    assert (binary_row["rc"], binary_row["timed_out"]) == (None, None)

    assert degraded_row["classification"]["kind"] == "degraded"
    assert "Cancelled" in degraded_row["classification"]["detail"]
    assert degraded_row["rc"] == 0 and degraded_row["timed_out"] is False

    assert timeout_row["classification"] is None
    assert timeout_row["timed_out"] is True and timeout_row["duration_sec"] >= 1.0


def test_unusable_ok_output_does_not_claim_the_record_identity(
        tmp_path, monkeypatch, capsys):
    """`classify` returning `ok` means "no positive evidence of ill health",
    NOT "produced usable output": an empty stdout with a clean stderr
    classifies `ok` and is worth nothing. Acceptance must gate on
    `parsed.parse_ok`, not on `verdict.kind == "ok"`.

    The head's binary is missing, so the chain advances to the (last) xai
    fallback, whose fake CLI exits 0 with empty stdout and clean stderr --
    `ok`, but unusable. The record must NOT credit that attempt: the indexed
    `adapter`/`model` columns stay the head's, exactly as the record was
    initialised, because nothing ever produced an accepted payload.

    Kills the mutation at `pipeline.py:842` (`if parsed.parse_ok:` ->
    `if verdict.kind == "ok":`): with the mutation, the fallback's `ok`
    verdict is accepted and `rec["adapter"]` reads `"grok"` instead of
    staying `"codex"` -- this test fails.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    _fake_cli(tmp_path, "grok", "")     # exits 0, empty stdout, clean stderr
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)

    rec = _run(repo, _store(tmp_path))

    last = rec["attempts"][-1]
    assert last["provider"] == "xai"
    assert last["classification"] == {"kind": "ok", "category": "", "detail": ""}
    assert last["rc"] == 0
    # Unusable: the attempt never claims the identity columns.
    assert rec["adapter"] == "codex" and rec["model"] == FAKE_OPENAI_MODEL
    assert rec["status"] == "failed" and rec["trustworthy"] is False


# --------------------------------------------------------------------------
# the provider-availability cache
# --------------------------------------------------------------------------


def test_provider_state_skips_known_dead_provider_when_quota(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)
    st.mark_provider_unavailable("openai", "rate limited", "quota", _iso(3600))

    rec = _run(repo, st)

    first = rec["attempts"][0]
    assert first["skipped"].startswith("provider marked unavailable")
    assert first["classification"] == {"kind": "unavailable",
                                       "category": "quota",
                                       "detail": "rate limited"}
    assert "effort" in first
    assert first["rc"] is None and first["timed_out"] is None
    assert first["duration_sec"] is None and first["first_output_sec"] is None
    assert _calls(tmp_path) == ["grok"]     # codex was never invoked at all
    assert rec["trustworthy"] is True


def test_an_expired_cache_row_does_not_skip_the_entry(tmp_path, capsys):
    """The cache is a TTL, never a tombstone: a provider always comes back."""
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)
    st.mark_provider_unavailable("openai", "rate limited", "quota", _iso(-60))

    rec = _run(repo, st)

    assert _calls(tmp_path) == ["codex"]
    assert "skipped" not in rec["attempts"][0]
    assert rec["trustworthy"] is True


def test_mid_chain_cache_expiry_is_honoured_without_sleep(tmp_path, monkeypatch,
                                                          capsys):
    """A cache entry that expires BETWEEN entries is honoured -- deterministically.

    `run_chain` consults `_cached_unavailable` once per entry with a fresh
    `_iso_now()`. A two-entry chain on the SAME provider (`CFG_XAI_THEN_XAI`)
    therefore reads the cache twice for `xai` *during the chain*. Preflight
    (`_finder_chain_unavailable`) also walks the chain first, so a spy sees
    those earlier reads too.

    Readiness adds one read-only topology pass before execution. Spy policy
    (call order):
      1. preflight entry0 → LIVE (unavailable)
      2. preflight entry1 → EXPIRED so preflight does not refuse the whole run
      3–4. readiness snapshot → EXPIRED, so the topology remains eligible
      5. chain entry0 → LIVE → skipped
      6+. chain entry1 (and any later reads) → EXPIRED → entry runs

    No `sleep`, no flakiness.
    """
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_XAI_THEN_XAI)
    st = _store(tmp_path)
    st.mark_provider_unavailable("xai", "rate limited", "quota", _iso(60))

    real = Store.provider_unavailable_reason
    calls: list[str] = []

    def spy(self, provider, now_iso, env=os.environ):
        calls.append(now_iso)
        # Live only on 1st and 5th consult (preflight e0 + chain e0).
        if len(calls) not in (1, 5):
            now_iso = _iso(3600)
        return real(self, provider, now_iso, env)

    monkeypatch.setattr(Store, "provider_unavailable_reason", spy)

    rec = _run(repo, st)

    assert len(calls) >= 4, calls
    assert rec["attempts"][0]["skipped"].startswith("provider marked unavailable")
    assert "skipped" not in rec["attempts"][1]
    assert _calls(tmp_path) == ["grok"]
    assert rec["trustworthy"] is True


def test_pre_spawn_missing_binary_path_does_not_cache(tmp_path, monkeypatch,
                                                       capsys):
    """The PRE-SPAWN missing-binary shortcut (`_binary_is_absent`) never even
    reaches the quota-only guard: it `continue`s straight past
    `_remember_unavailable`, without a classification, without a spawn.

    This pins that narrow path only. It does NOT exercise the guard in
    `_remember_unavailable` (`if verdict.category != "quota": return` at
    `pipeline.py:648-649`) -- deleting that guard still passes this test,
    because this test's failure never reaches it. The guard itself is pinned
    by `test_spawned_auth_failure_is_not_cached` below, via a REAL spawn that
    classifies `auth`.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/a")   # binary, not quota
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)

    _run(repo, st)

    assert st.provider_unavailable_reason("openai", _iso(), env={}) is None
    assert st.provider_state_rows(_iso()) == []


def test_spawned_auth_failure_is_not_cached(tmp_path, monkeypatch, capsys):
    """The quota-only guard, pinned via a REAL spawn that gets classified.

    Unlike the pre-spawn test above, the head's fake CLI here actually runs:
    it exits 1 with `401 unauthorized` on stderr, so `classify` returns
    `kind == "unavailable", category == "auth"` -- and THAT is what
    `_remember_unavailable` must refuse to cache. `auth`, unlike `quota`,
    describes this one reviewer entry's configuration (a bad key, a rotated
    token); caching it would black-hole the whole provider for every other
    reviewer entry that uses it, for the full 30-minute TTL, over one bad
    credential.

    Kills the mutation at `pipeline.py:648-649` (delete
    `if verdict.category != "quota": return`): with the guard gone, this
    `auth` verdict gets cached and `provider_state_rows` picks up an `openai`
    row -- this test fails.
    """
    _fake_cli(tmp_path, "codex", 'echo "401 unauthorized" >&2\nexit 1')
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)

    rec = _run(repo, st)

    first = rec["attempts"][0]
    assert first["provider"] == "openai"
    assert first["classification"]["kind"] == "unavailable"
    assert first["classification"]["category"] == "auth"
    assert st.provider_unavailable_reason("openai", _iso(), env={}) is None
    assert st.provider_state_rows(_iso()) == []
    assert rec["trustworthy"] is True   # the fallback still recovered


def test_a_quota_outage_is_cached_for_the_whole_provider(tmp_path, monkeypatch,
                                                         capsys):
    _fake_cli(tmp_path, "grok", 'echo "429 rate limit exceeded" >&2\nexit 1')
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_XAI_THEN_OPENAI)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["attempts"][0]["classification"]["category"] == "quota"
    assert _calls(tmp_path) == ["grok", "codex"]      # hopped, did not retry
    reason = st.provider_unavailable_reason("xai", _iso(), env={})
    assert reason is not None and "rate limit" in reason
    (row,) = [r for r in st.provider_state_rows(_iso()) if r["provider"] == "xai"]
    assert row["category"] == "quota" and row["active"] is True
    # The TTL is 30 minutes, and it is a real bound: nothing may be cached
    # without one, or a provider could never come back.
    assert row["unavailable_until"] > _iso(pipeline.PROVIDER_UNAVAILABLE_TTL_SEC - 120)
    assert row["unavailable_until"] <= _iso(pipeline.PROVIDER_UNAVAILABLE_TTL_SEC + 120)


def test_a_quota_mark_made_mid_chain_skips_a_later_entry_on_that_provider(
        tmp_path, monkeypatch, capsys):
    """The cache is consulted per ENTRY, so a mark written by entry 1 is seen
    by entry 2 -- otherwise a chain of two models behind one rate-limited
    account would hammer it twice per review."""
    _fake_cli(tmp_path, "grok", 'echo "quota exceeded" >&2\nexit 1')
    repo = _repo(tmp_path, CFG_XAI_THEN_XAI)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert _calls(tmp_path) == ["grok"]       # the second entry never spawned
    assert rec["attempts"][1]["skipped"].startswith("provider marked unavailable")
    assert rec["status"] == "failed" and rec["trustworthy"] is False


def test_the_cache_bypass_env_still_wins(tmp_path, monkeypatch, capsys):
    """`SKODUN_IGNORE_PROVIDER_STATE` is an operator's escape hatch and must
    reach the chain, not just the store's own unit tests."""
    monkeypatch.setenv("SKODUN_IGNORE_PROVIDER_STATE", "1")
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)
    st.mark_provider_unavailable("openai", "rate limited", "quota", _iso(3600))

    rec = _run(repo, st)

    assert _calls(tmp_path) == ["codex"]
    assert rec["trustworthy"] is True


def test_a_fallback_entry_that_is_itself_unavailable_exhausts_the_chain(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKODUN_GROK_BIN", "/nonexistent/b")
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)
    st = _store(tmp_path)
    st.mark_provider_unavailable("openai", "rate limited", "quota", _iso(3600))

    with pytest.raises(PreflightRefused, match="binary_unavailable"):
        _run(repo, st)
    assert _calls(tmp_path) == []


def test_each_entry_gets_its_own_degraded_budget(tmp_path, monkeypatch, capsys):
    """The head's exhausted budget must not silently shorten the fallback's.

    A budget shared across the chain would make the last entry the one that
    gets no retry at all -- precisely the entry that is running because
    everything else already failed.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/a")
    _fake_cli(tmp_path, "grok", _per_call(_emit(CANCELLED), _emit(CLEAN)))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI,
                 "\n[defaults]\ndegraded_retries = 1\n")

    rec = _run(repo, _store(tmp_path))

    assert _calls(tmp_path) == ["grok", "grok"]
    assert [a["provider"] for a in rec["attempts"]] == ["openai", "xai", "xai"]
    assert [a["n"] for a in rec["attempts"]] == [1, 2, 3]
    assert rec["trustworthy"] is True


# --------------------------------------------------------------------------
# stdin adapters, per-attempt scratch files
# --------------------------------------------------------------------------


def test_a_stdin_adapter_is_fed_the_prompt_file(tmp_path, capsys):
    """codex takes the prompt on stdin (`-`), so without the runner's
    `stdin_path` every codex attempt hangs until the watchdog kills it."""
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI)

    rec = _run(repo, _store(tmp_path))

    argv = (tmp_path / "bin" / "argv_1.log").read_text(
        encoding="utf-8").splitlines()
    assert argv[-1] == "-"
    stdin_seen = (tmp_path / "bin" / "stdin_1.txt").read_text(encoding="utf-8")
    assert "----- BEGIN DIFF -----" in stdin_seen and "+two" in stdin_seen
    assert rec["trustworthy"] is True and rec["adapter"] == "codex"


def test_each_attempt_gets_its_own_prompt_file_and_schema_sidecar(tmp_path,
                                                                  capsys):
    """The codex adapter writes its schema sidecar beside the prompt file and
    always overwrites it. Two attempts sharing one prompt path would therefore
    swap each other's response shape between `build_cmd` and `exec`."""
    _fake_cli(tmp_path, "codex", _per_call(_emit(CODEX_CUT_OFF),
                                           _emit(CODEX_CLEAN)))
    repo = _repo(tmp_path, CFG_OPENAI_THEN_XAI,
                 "\n[defaults]\ndegraded_retries = 1\n")

    _run(repo, _store(tmp_path))

    first = (tmp_path / "bin" / "schemapath_1.txt").read_text(encoding="utf-8")
    second = (tmp_path / "bin" / "schemapath_2.txt").read_text(encoding="utf-8")
    assert first.strip() != second.strip()
    # ...and the name carries the chain ORDINAL, never the reviewer name --
    # names are user input and config constrains neither `/` nor `..`.
    assert "primary.e0.a1" in first and "primary.e0.a2" in second
    assert "primary" == Path(first.strip()).name.split(".")[0]


def test_scratch_filenames_never_carry_the_reviewer_name(tmp_path, capsys):
    """A path-traversal guard, not a style choice: `name` is user input."""
    hostile = f"""
[[reviewers]]
name = "../../etc/evil"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"
"""
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, hostile)

    rec = _run(repo, _store(tmp_path))

    schema_path = (tmp_path / "bin" / "schemapath_1.txt").read_text(
        encoding="utf-8").strip()
    assert "etc" not in schema_path and ".." not in schema_path
    assert rec["trustworthy"] is True


# --------------------------------------------------------------------------
# preflight over the whole graph
# --------------------------------------------------------------------------


def test_unknown_fallback_provider_refused_in_preflight(tmp_path, capsys):
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_WITH_UNKNOWN_FALLBACK_PROVIDER)
    st = _store(tmp_path)
    before = len(st.list_reviews(None, 1000))

    with pytest.raises(PreflightRefused) as e:
        _run(repo, st)

    assert "no-such-provider" in str(e.value)
    assert len(st.list_reviews(None, 1000)) == before     # nothing ran
    assert _calls(tmp_path) == []
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_unknown_fallback_provider_is_exit_2_through_the_cli(tmp_path, capsys):
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_WITH_UNKNOWN_FALLBACK_PROVIDER)

    code = main(["review", "--repo", str(repo)])

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 2                         # a config error, not a bad review
    assert out[-1].startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert _calls(tmp_path) == []
    st = Store.open(Path(os.environ["SKODUN_DB"]))
    assert st.list_reviews(None, 1000) == []


@pytest.mark.parametrize("role", ["security", "refuter"])
def test_an_extra_pass_chain_is_resolved_in_preflight_too(tmp_path, capsys,
                                                          monkeypatch, role):
    """The graph, not just the finder's own chain: a typo on the fallback of a
    security reviewer must not be discovered after the primary review has
    already run and spent a model call."""
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    cfg = CFG_XAI_ONLY + f"""
[[reviewers]]
name = "extra"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "{role}"
fallbacks = ["extra-backup"]

[[reviewers]]
name = "extra-backup"
provider = "no-such-provider"
model = "m"
role = "integrator"
"""
    repo = _repo(tmp_path, cfg)

    with pytest.raises(PreflightRefused) as e:
        _run(repo, _store(tmp_path))

    assert "no-such-provider" in str(e.value)
    assert _calls(tmp_path) == []


# --------------------------------------------------------------------------
# choosing the head for ONE run (issue #16)
# --------------------------------------------------------------------------
#
# `--reviewer <name>` narrows where the chain STARTS. Everything else about the
# chain is unchanged, and the tests below are written as that contrast: the
# requested entry heads the chain, its own `fallbacks` still recover, the extra
# passes still choose by ROLE, and a name that does not resolve is refused
# before the lock rather than quietly falling back to the config's own finder.


def _risky(repo: Path) -> Path:
    (repo / "auth").mkdir()
    (repo / "auth" / "session.py").write_text("token = 1\n", encoding="utf-8")
    return repo


#: Two enabled entries either of which can head a chain. `primary` is the
#: config's own finder -- `_reviewer_for` takes the FIRST enabled `finder` --
#: so `second-opinion` is reachable only by asking for it by name.
CFG_TWO_HEADS = f"""
[[reviewers]]
name = "primary"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "finder"

[[reviewers]]
name = "second-opinion"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"
fallbacks = ["primary"]
"""

CFG_WITH_A_DISABLED_ENTRY = CFG_XAI_ONLY + f"""
[[reviewers]]
name = "retired"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "finder"
enabled = false
"""

#: An entry NOTHING resolves by default: it is not the first enabled finder, it
#: holds no other role, and no chain names it as a fallback. Preflight therefore
#: never reaches it unless the run asks for it by name -- which is exactly what
#: makes it the right fixture for "the request is what found the config error".
CFG_WITH_AN_UNREACHABLE_BAD_PROVIDER = CFG_XAI_ONLY + """
[[reviewers]]
name = "offline"
provider = "no-such-provider"
model = "m"
role = "finder"
"""


def test_the_requested_reviewer_heads_the_chain_instead_of_the_config_finder(
        tmp_path, capsys):
    """The whole feature in one contrast, against ONE config and one repo.

    Asked for by name, `second-opinion` runs and `primary` never spawns; asked
    for nothing, the config's own finder heads the chain exactly as it always
    has. The record says which of the two happened -- `requested_reviewer` is
    the NAME that was asked for, and `None` when nobody asked.
    """
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_TWO_HEADS)
    st = _store(tmp_path)

    chosen = _run(repo, st, reviewer="second-opinion")

    assert chosen["trustworthy"] is True
    assert _calls(tmp_path) == ["codex"]     # the config's finder never spawned
    assert chosen["adapter"] == "codex" and chosen["model"] == FAKE_OPENAI_MODEL
    assert chosen["requested_reviewer"] == "second-opinion"

    default = _run(repo, st)

    assert default["trustworthy"] is True
    assert _calls(tmp_path) == ["codex", "grok"]
    assert default["adapter"] == "grok" and default["model"] == FAKE_XAI_MODEL
    # The field that makes the two distinguishable on the artifact. Explicit
    # `None` rather than an absent key: a reader must be able to tell "nobody
    # asked" from "this record predates the field".
    assert default["requested_reviewer"] is None
    assert "requested_reviewer" in default


def test_the_requested_heads_own_fallbacks_still_apply(tmp_path, monkeypatch,
                                                       capsys):
    """The choice narrows where the chain STARTS, never whether it can recover.

    And it is the strongest form of the provenance rule: after the fallback the
    two fields say different things on purpose -- `requested_reviewer` names who
    was ASKED FOR, `adapter`/`model` name who ANSWERED.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_TWO_HEADS)

    rec = _run(repo, _store(tmp_path), reviewer="second-opinion")

    assert rec["trustworthy"] is True
    assert [a["provider"] for a in rec["attempts"]] == ["openai", "xai"]
    assert rec["attempts"][0]["classification"]["kind"] == "unavailable"
    assert rec["adapter"] == "grok" and rec["model"] == FAKE_XAI_MODEL
    assert rec["requested_reviewer"] == "second-opinion"


def test_the_request_does_not_hijack_a_role_chosen_extra_pass(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """`--reviewer` selects the FINDER head. The extra passes choose by ROLE.

    Two selection systems, and they must not be one: an operator who configured
    a `security` reviewer chose it for the security pass specifically, and a
    request for a second opinion on the FINDER is not a request to re-point that
    pass at a model with no security role.
    """
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    cfg = CFG_TWO_HEADS + f"""
[[reviewers]]
name = "sec"
provider = "xai"
model = "{FAKE_XAI_MODEL}-sec"
role = "security"
"""
    repo = _risky(_repo(tmp_path, cfg))

    rec = _run(repo, _store(tmp_path), reviewer="second-opinion")

    # The finder is the requested entry...
    assert rec["adapter"] == "codex" and rec["model"] == FAKE_OPENAI_MODEL
    # ...and the security pass is still the role's, on the other provider.
    meta = rec["extra_passes"]["security"]
    assert meta["ran"] is True
    assert meta["provider"] == "xai" and meta["model"] == f"{FAKE_XAI_MODEL}-sec"
    assert _calls(tmp_path) == ["codex", "grok"]


def test_an_unknown_requested_reviewer_is_refused_before_anything_runs(
        tmp_path, capsys):
    """A preflight refusal, in the shape every other preflight refusal has:
    exit 2's `PreflightRefused`, "no review ran", no lock, no record, no spawn.

    It also names what IS configured. An agent over MCP has no way to enumerate
    the reviewer table -- `providers` is deliberately not a tool -- so the one
    moment it can learn the real names is the refusal for a name it guessed.
    """
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_TWO_HEADS)
    st = _store(tmp_path)

    with pytest.raises(PreflightRefused) as e:
        _run(repo, st, reviewer="no-such-entry")

    message = str(e.value)
    assert "no-such-entry" in message and "no review ran" in message
    assert "primary (xai)" in message and "second-opinion (openai)" in message
    assert _calls(tmp_path) == []
    assert st.list_reviews(None, 1000) == []
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_a_disabled_requested_reviewer_is_refused_rather_than_enabled(
        tmp_path, capsys):
    """`enabled = false` is the operator taking an entry out of service. A
    request may not put it back: the run would spend a model call on a provider
    the config says not to use, and silently succeeding at that is worse than
    the refusal."""
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_WITH_A_DISABLED_ENTRY)
    st = _store(tmp_path)

    with pytest.raises(PreflightRefused) as e:
        _run(repo, st, reviewer="retired")

    assert "retired" in str(e.value) and "disabled" in str(e.value)
    assert "no review ran" in str(e.value)
    assert _calls(tmp_path) == []
    assert st.list_reviews(None, 1000) == []


def test_a_requested_reviewer_with_no_registered_adapter_is_refused(tmp_path,
                                                                    capsys):
    """The third refusal, and it comes from the SHIPPED preflight resolver
    rather than from a fourth copy of "unknown provider": the requested entry
    simply joins the graph every run already resolves. Nothing else in the
    config reaches this entry, so the request is what found the error."""
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_WITH_AN_UNREACHABLE_BAD_PROVIDER)
    st = _store(tmp_path)

    # ...and with no request at all it is never resolved, so the run is fine.
    assert _run(repo, st)["trustworthy"] is True

    with pytest.raises(PreflightRefused) as e:
        _run(repo, st, reviewer="offline")

    assert "offline" in str(e.value) and "no-such-provider" in str(e.value)
    assert "no review ran" in str(e.value)
    assert _calls(tmp_path) == ["grok"]      # only the unrequested run spawned


def test_the_cli_flag_chooses_the_head_and_a_bad_one_is_exit_2(tmp_path,
                                                               capsys):
    """The flag really reaches `run_review`, and its refusal really is the
    CLI's exit 2 with a verdict line -- not an exception on the way past."""
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_TWO_HEADS)

    assert main(["review", "--repo", str(repo),
                 "--reviewer", "second-opinion"]) == 0
    assert _calls(tmp_path) == ["codex"]
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith(
        "SKODUN VERDICT: trustworthy=true")

    assert main(["review", "--repo", str(repo), "--reviewer", "nope"]) == 2
    out = capsys.readouterr().out.strip().splitlines()
    assert _calls(tmp_path) == ["codex"]     # still nothing new spawned
    assert out[-1].startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "nope" in out[-1] and "no review ran" in out[-1]


# --------------------------------------------------------------------------
# extra-pass provenance
# --------------------------------------------------------------------------


def test_an_extra_pass_records_the_accepted_attempts_provenance(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _risky(_repo(tmp_path, CFG_XAI_ONLY))

    rec = _run(repo, _store(tmp_path))

    meta = rec["extra_passes"]["security"]
    assert meta["ran"] is True
    assert meta["provider"] == "xai" and meta["model"] == FAKE_XAI_MODEL
    assert "effort" in meta and meta["effort"] is None
    assert "note" not in meta


def test_an_extra_pass_with_no_binary_records_null_provenance_and_a_note(
        tmp_path, monkeypatch, capsys):
    """Nothing ever started a process, so there is no provenance to report --
    and an object that quietly omits the fields invites a reader to assume the
    pass ran on the finder's model."""
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    cfg = CFG_XAI_ONLY + f"""
[[reviewers]]
name = "sec"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "security"
"""
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    repo = _risky(_repo(tmp_path, cfg))

    with pytest.raises(PreflightRefused, match="required_pass_unavailable"):
        _run(repo, _store(tmp_path))


def test_a_broken_extra_pass_prompt_records_null_provenance_and_a_note(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    repo = _repo(tmp_path, CFG_XAI_ONLY)

    def boom(*a, **kw):
        raise ValueError("cannot render")

    monkeypatch.setattr(pipeline.passes, "skeptic_prompt", boom)
    rec = _run(repo, _store(tmp_path))

    meta = rec["extra_passes"]["skeptic"]
    assert meta["provider"] is None and meta["model"] is None
    assert "cannot render" in meta["note"]


def test_an_extra_pass_that_fell_back_records_the_fallbacks_provenance(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    cfg = CFG_XAI_ONLY + f"""
[[reviewers]]
name = "sec"
provider = "openai"
model = "{FAKE_OPENAI_MODEL}"
role = "security"
effort = "low"
fallbacks = ["sec-backup"]

[[reviewers]]
name = "sec-backup"
provider = "xai"
model = "{FAKE_XAI_MODEL}"
role = "integrator"
effort = "high"
"""
    repo = _risky(_repo(tmp_path, cfg))

    rec = _run(repo, _store(tmp_path))

    meta = rec["extra_passes"]["security"]
    assert meta["ran"] is True
    assert meta["provider"] == "xai" and meta["effort"] == "high"


# --------------------------------------------------------------------------
# runtime and lock ceilings
# --------------------------------------------------------------------------


def test_runtime_and_lock_ceilings_scale_with_chain_width():
    d = Defaults()
    assert worst_runtime_sec(d, max_chain_width=4) >= \
        4 * worst_runtime_sec(d, max_chain_width=1)
    assert lock_stale_ceiling_sec(d, max_chain_width=4) >= \
        4 * lock_stale_ceiling_sec(d, max_chain_width=1)
    # The single-entry default is unchanged: Phase 1's numbers are what the
    # oracle-parity ceilings are pinned to.
    assert worst_runtime_sec(d) == worst_runtime_sec(d, max_chain_width=1)
    assert lock_stale_ceiling_sec(d) == lock_stale_ceiling_sec(d, max_chain_width=1)


def test_max_chain_width_is_read_off_the_config(tmp_path):
    repo = _repo(tmp_path, CFG_FOUR_ENTRY)
    assert pipeline.max_chain_width(load_config(repo)) == 4
    single = tmp_path / "single"
    single.mkdir()
    repo2 = _repo(single, CFG_XAI_ONLY)
    assert pipeline.max_chain_width(load_config(repo2)) == 1


def test_run_review_derives_the_lock_ceiling_from_the_configured_width(
        tmp_path, monkeypatch, capsys):
    """A waiting peer must not reclaim a live long chain: the ceiling has to
    budget every entry the config can make this run execute."""
    _fake_cli(tmp_path, "grok", _emit(CLEAN))
    _fake_cli(tmp_path, "codex", _emit(CODEX_CLEAN))
    repo = _repo(tmp_path, CFG_FOUR_ENTRY)
    seen: dict = {}
    real = pipeline._acquire_fg_lock

    def spy(common_dir, worktree, *, wait, poll, stale, grace=30.0,
            budget_sec=None):
        seen["stale"] = stale
        return real(common_dir, worktree, wait=wait, poll=poll, stale=stale,
                    grace=grace, budget_sec=budget_sec)

    monkeypatch.setattr(pipeline, "_acquire_fg_lock", spy)
    _run(repo, _store(tmp_path))

    d = load_config(repo).defaults
    assert seen["stale"] == float(lock_stale_ceiling_sec(d, max_chain_width=4))
    assert seen["stale"] >= 4 * float(lock_stale_ceiling_sec(d, max_chain_width=1))


def test_stale_record_recovery_also_scales_with_the_chain(tmp_path, capsys):
    """A `running` record left by a four-entry chain is not stale at the
    one-entry age -- sweeping it would fail a review that is still going."""
    repo = _repo(tmp_path, CFG_FOUR_ENTRY, "\n[defaults]\ntimeout_sec = 100\n"
                                           "timeout_retries = 0\n"
                                           "degraded_retries = 0\n")
    cfg = load_config(repo)
    st = _store(tmp_path)
    one_entry_age = worst_runtime_sec(cfg.defaults, max_chain_width=1) + 5
    st.save_review({
        "id": "sk_live_chain",
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - one_entry_age)),
        "branch": "feat", "base_sha": "b" * 40, "diff_hash": "d" * 40,
        "status": "running", "parse_ok": False, "degraded": False,
        "diff_truncated": False, "findings": [], "findings_total": 0,
    })
    assert pipeline.recover_stale(st, cfg) == 0
    assert st.get_review("sk_live_chain")["status"] == "running"
