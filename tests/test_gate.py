"""The gate is the project's central fail-closed component.

Every test here asserts a specific exit code, and the ones that inject failure
assert `2` rather than merely "non-zero": the whole point of the contract is
that corruption must never be reported as `1` ("findings remain open"), because
a `1` tells a human "go triage the findings" about a review that does not
exist. A test that accepted any non-zero code would pass vacuously on exactly
the bug it exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skodun import gitio, triage
from skodun.config import load_config
from skodun.gate import GateResult, run_gate
from skodun.store import Store
from tests.test_gitio import _git, _mkrepo


@pytest.fixture(autouse=True)
def _no_ambient_config(tmp_path, monkeypatch):
    """Pin `SKODUN_CONFIG` at a path that does not exist.

    `load_config` otherwise falls back to `~/.config/skodun/config.toml`, so a
    developer with a real global config would be running a different
    `untracked_max` than CI and the cap-note test would be a property of their
    machine.
    """
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))


def _cfg(repo: Path):
    return load_config(repo)


def _raiser(exc):
    def _f(*a, **k):
        raise exc
    return _f


def _outgoing(repo: Path) -> Path:
    """Put a real outgoing change in the tree, on a branch off main."""
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo


def _reviewed(store: Store, repo: Path, *, findings=(), trustworthy=True,
              review_id="r1", base_sha=None):
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    store.save_review(dict(
        id=review_id, reviewed_at="2026-07-27T10:00:00Z",
        branch=gitio.current_branch(repo), head=gitio.head_sha(repo),
        base_ref=base.ref, base_sha=base_sha if base_sha is not None else base.sha,
        diff_hash=gitio.diff_identity(diff.data), context_hash="", mode="now",
        model="m", adapter="grok", status="clean", parse_ok=trustworthy,
        degraded=False, diff_truncated=False, trustworthy=trustworthy,
        stop_reason="EndTurn", summary="s", findings_total=len(findings),
        severity={"high": 0, "medium": 0, "low": 0}, findings=list(findings)))
    return base


def _events(store: Store) -> list[dict]:
    return [dict(r) for r in store._c.execute(
        "SELECT * FROM gate_events ORDER BY rowid").fetchall()]


def _artifact(store: Store, review_id="r1") -> dict:
    row = store._c.execute("SELECT artifact_json FROM reviews WHERE id=?",
                           (review_id,)).fetchone()
    return json.loads(row["artifact_json"])


def _hand_edit(store: Store, path: str, sql_value: str) -> None:
    """Corrupt the ARTIFACT behind the index's back.

    `save_review` recomputes `trustworthy` and writes index and artifact in one
    statement, so the two can never disagree by going through the API. A
    crashed writer or a hand-edited archive can still produce the divergence,
    which is precisely why the gate re-asserts instead of trusting the index.
    """
    store._c.execute(
        f"UPDATE reviews SET artifact_json=json_set(artifact_json, '{path}', {sql_value})")


_FINDING = dict(file="a.txt", line=1, severity="high", category="bug",
                title="T", detail="d")
_REASON = "the guard already lives in validate_input, three frames up"


# --------------------------------------------------------------------------
# The three exit codes, in their normal (uncorrupted) forms
# --------------------------------------------------------------------------


def test_gate_empty_diff_is_0_no_outgoing_change(tmp_path):
    """ORACLE PARITY: `--diff-hash` exits 3 on an empty capture and
    grok-review-now.sh maps that to `PASS ... nothing to review`, exit 0."""
    repo = _mkrepo(tmp_path)
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 0
    assert "no outgoing change" in r.message
    (ev,) = _events(st)
    assert ev["outcome"] == "pass"
    assert ev["diff_hash"] == ""          # the defined empty-change identity
    assert ev["code"] == 0


def test_gate_no_review_is_2(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "no trustworthy review" in r.message
    (ev,) = _events(st)
    assert ev["outcome"] == "no-review"
    assert ev["diff_hash"] == gitio.diff_identity(
        gitio.capture_diff(repo, gitio.resolve_base(repo).sha, 100).data)


def test_gate_untrustworthy_review_is_2(tmp_path):
    """A 0-finding untrustworthy round said nothing; it did not find nothing."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, trustworthy=False)
    assert run_gate(st, repo, _cfg(repo), env={}).code == 2


def test_gate_clean_is_0_and_edit_invalidates(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 0
    assert "PASS" in r.message and "r1" in r.message

    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    r2 = run_gate(st, repo, _cfg(repo), env={})
    assert r2.code == 2            # exact-content match only
    assert r2.diff_hash != r.diff_hash
    assert [e["outcome"] for e in _events(st)] == ["pass", "no-review"]


def test_gate_open_finding_is_1_until_triaged(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])

    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 1
    assert "1 finding(s) open" in r.message

    triage.dismiss(st, _artifact(st), 0, _REASON, "2026-07-27T11:00:00Z")

    r2 = run_gate(st, repo, _cfg(repo), env={})
    assert r2.code == 0            # every finding triaged
    assert [e["outcome"] for e in _events(st)] == ["open-findings", "pass"]


def test_gate_triage_scoped_to_other_base_does_not_pass(tmp_path):
    """A dismissal recorded under a different base must not silence a finding."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    art = dict(_artifact(st), base_sha="0" * 40)
    triage.dismiss(st, art, 0, _REASON, "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1


# --------------------------------------------------------------------------
# Index/artifact re-assertion — the index is a derived summary, never trusted
# alone
# --------------------------------------------------------------------------


def test_gate_rejects_artifact_index_disagreement(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.degraded", "json('true')")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "disagree" in r.message


def test_gate_rejects_trustworthy_field_disagreeing_with_its_own_axes(tmp_path):
    """The other direction: axes recompute to trustworthy, the field says no.

    Recomputing alone would silently overrule the artifact's own verdict. The
    two must agree, or the artifact is inconsistent and certifies nothing.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.trustworthy", "json('false')")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "disagree" in r.message


def test_gate_rejects_non_bool_trust_axes(tmp_path):
    """`is_trustworthy` coerces by truthiness, so the gate must type-check first.

    A JSON artifact carrying `parse_ok: 1` (or the string "false") would sail
    through the invariant unexamined; the gate is the wrong place to be
    permissive about that.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.parse_ok", "1")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "not booleans" in r.message


def test_gate_rejects_string_false_trust_axis(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.degraded", "'false'")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "not booleans" in r.message


def test_gate_rejects_artifact_with_a_different_diff_hash(tmp_path):
    """Found by the index for this content, but the artifact records another."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.diff_hash", "'" + "d" * 40 + "'")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "diff_hash" in r.message


def test_gate_rejects_stale_base_sha_after_rebase(tmp_path):
    """Same diff bytes, different merge-base: the dismissals are scoped to the
    OLD base, so accepting the review would keep a stale amnesty alive."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, base_sha="0" * 40)
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "base_sha mismatch" in r.message and "rebase" in r.message


def test_gate_invalid_artifact_is_2(tmp_path):
    """`load_valid_artifact` rejects an artifact with no `findings` key; under a
    lenient reading that artifact would mean "zero findings", i.e. clean."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    st._c.execute("UPDATE reviews SET artifact_json=json_remove(artifact_json,"
                  " '$.findings')")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "invalid artifact" in r.message
    assert _events(st)[-1]["outcome"] == "error"


# --------------------------------------------------------------------------
# The recorded bypass
# --------------------------------------------------------------------------


def test_gate_skip_is_recorded(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={"SKODUN_GATE_SKIP": "1"})
    assert r.code == 0
    assert "SKIPPED" in r.message
    (ev,) = _events(st)
    assert ev["outcome"] == "skipped"
    assert ev["diff_hash"] is None     # never depends on identity computation
    assert ev["branch"] == "feat"


def test_gate_skip_works_when_identity_computation_is_broken(tmp_path, monkeypatch):
    """A bypass exists for the case where the machinery itself is broken. If it
    computed the identity first it would fail exactly when it is needed."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    monkeypatch.setattr(gitio, "resolve_base", _raiser(RuntimeError("no git")))
    monkeypatch.setattr(gitio, "capture_diff", _raiser(RuntimeError("no git")))
    monkeypatch.setattr(gitio, "current_branch", _raiser(RuntimeError("no git")))
    r = run_gate(st, repo, _cfg(repo), env={"SKODUN_GATE_SKIP": "1"})
    assert r.code == 0 and "SKIPPED" in r.message
    (ev,) = _events(st)
    assert ev["outcome"] == "skipped"
    assert ev["branch"] is None        # best-effort, not a precondition


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
def test_gate_skip_requires_exactly_1(tmp_path, value):
    """Only the documented value bypasses; anything else gates normally."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={"SKODUN_GATE_SKIP": value})
    assert r.code == 2
    assert "SKIPPED" not in r.message


def test_gate_reads_skip_from_os_environ_by_default(tmp_path, monkeypatch):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    monkeypatch.setenv("SKODUN_GATE_SKIP", "1")
    assert run_gate(st, repo, _cfg(repo)).code == 0


# --------------------------------------------------------------------------
# Every unexpected exception is 2, never 1 — injected at several points
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["resolve_base", "capture_diff", "diff_identity"])
def test_gate_gitio_failure_is_2(tmp_path, monkeypatch, target):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])   # would otherwise be exit 1
    monkeypatch.setattr(gitio, target, _raiser(RuntimeError("boom")))
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2, f"{target} failure must not read as findings"
    assert "internal error" in r.message


def test_gate_branch_lookup_is_best_effort_and_does_not_change_the_verdict(
        tmp_path, monkeypatch):
    """`current_branch` is only used to LABEL the recorded event.

    The verdict itself is scoped by the artifact's own branch, so a failure to
    read the current one must neither flip the decision nor lose the record.
    Failing closed here would mean a detached HEAD could not be gated at all.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    monkeypatch.setattr(gitio, "current_branch", _raiser(RuntimeError("boom")))
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 1
    (ev,) = _events(st)
    assert ev["outcome"] == "open-findings" and ev["branch"] is None


def test_gate_store_corruption_is_2_not_1(tmp_path, monkeypatch):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    monkeypatch.setattr(st, "latest_trustworthy_for", _raiser(RuntimeError("corrupt")))
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert _events(st)[-1]["outcome"] == "error"


def test_gate_triage_lookup_failure_is_2_not_1(tmp_path, monkeypatch):
    """The failure lands AFTER a review with open findings was selected — the
    one place where a lenient handler would most plausibly return 1."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    monkeypatch.setattr(st, "triage_for", _raiser(sqlite_error()))
    assert run_gate(st, repo, _cfg(repo), env={}).code == 2


def sqlite_error():
    import sqlite3
    return sqlite3.DatabaseError("database disk image is malformed")


def test_gate_base_exception_is_2(tmp_path, monkeypatch):
    """`BaseException`, not `Exception`: a KeyboardInterrupt or a
    MemoryError mid-gate must not escape as a Python exit code of 1."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    monkeypatch.setattr(gitio, "capture_diff", _raiser(KeyboardInterrupt()))
    assert run_gate(st, repo, _cfg(repo), env={}).code == 2


# --------------------------------------------------------------------------
# Durability of the decision is itself fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("findings, would_be", [((), 0), ((_FINDING,), 1)])
def test_gate_unrecordable_event_becomes_2(tmp_path, monkeypatch, findings, would_be):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=list(findings))
    assert run_gate(st, repo, _cfg(repo), env={}).code == would_be   # not vacuous

    monkeypatch.setattr(st, "log_gate_event", _raiser(RuntimeError("disk full")))
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "could not record gate event" in r.message


def test_gate_unrecordable_skip_becomes_2(tmp_path, monkeypatch):
    """A bypass that leaves no record is not a decision, it is a hole."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    monkeypatch.setattr(st, "log_gate_event", _raiser(RuntimeError("disk full")))
    r = run_gate(st, repo, _cfg(repo), env={"SKODUN_GATE_SKIP": "1"})
    assert r.code == 2
    assert "could not record gate event" in r.message


def test_gate_records_an_event_for_every_decision(tmp_path):
    repo = _mkrepo(tmp_path)
    st = Store.open(tmp_path / "s.db")
    run_gate(st, repo, _cfg(repo), env={})                        # empty-diff pass
    _outgoing(repo)
    run_gate(st, repo, _cfg(repo), env={})                        # no review
    run_gate(st, repo, _cfg(repo), env={"SKODUN_GATE_SKIP": "1"})  # bypass
    _reviewed(st, repo, findings=[_FINDING])
    run_gate(st, repo, _cfg(repo), env={})                        # open findings
    assert [(e["outcome"], e["code"]) for e in _events(st)] == [
        ("pass", 0), ("no-review", 2), ("skipped", 0), ("open-findings", 1)]


# --------------------------------------------------------------------------
# Identity notes — a degraded identity must be loud at the enforcement point
# --------------------------------------------------------------------------


def test_gate_echoes_base_fallback_warning(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "branch", "-m", "master")     # no github/main|origin/main|main
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 2
    assert "SKODUN GATE: identity note:" in r.message
    assert "no main ref" in r.message


def test_gate_echoes_untracked_cap_note(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    (repo / ".skodun.toml").write_text("[defaults]\nuntracked_max = 1\n",
                                       encoding="utf-8")
    (repo / "u1.txt").write_text("x\n", encoding="utf-8")
    (repo / "u2.txt").write_text("y\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    cfg = _cfg(repo)
    assert cfg.defaults.untracked_max == 1
    r = run_gate(st, repo, cfg, env={})
    assert "SKODUN GATE: identity note: untracked scan capped at 1" in r.message


def test_gate_result_is_frozen_and_carries_the_hash(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert isinstance(r, GateResult)
    assert r.diff_hash and len(r.diff_hash) == 40
    with pytest.raises(Exception):
        r.code = 0          # frozen: a decision is not editable after the fact


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_cli_gate_prints_and_returns_the_code(tmp_path, monkeypatch, capsys):
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli" / "s.db"))
    assert main(["gate", "--repo", str(repo)]) == 2
    assert "SKODUN GATE:" in capsys.readouterr().out

    st = Store.open(tmp_path / "cli" / "s.db")
    _reviewed(st, repo)
    assert main(["gate", "--repo", str(repo)]) == 0
    assert [e["outcome"] for e in _events(st)] == ["no-review", "pass"]


def test_cli_gate_defaults_repo_to_cwd(tmp_path, monkeypatch, capsys):
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli" / "s.db"))
    monkeypatch.chdir(repo)
    assert main(["gate"]) == 2
    assert "SKODUN GATE:" in capsys.readouterr().out


def test_cli_gate_never_touches_the_real_store_path(tmp_path, monkeypatch):
    """`SKODUN_DB` must win over `~/.local/share/skodun/skodun.db`."""
    from skodun import cli

    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "elsewhere.db"))
    assert cli._store_path() == tmp_path / "elsewhere.db"
    monkeypatch.delenv("SKODUN_DB", raising=False)
    assert cli._store_path() == Path.home() / ".local" / "share" / "skodun" / "skodun.db"


def test_cli_gate_setup_failure_is_2_not_a_traceback(tmp_path, monkeypatch, capsys):
    """An uncaught exception would leave Python's own exit code of 1 — the one
    value that means "findings remain open". The CLI seam must fail closed too.
    """
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    (repo / ".skodun.toml").write_text("[defaults]\nnope = 1\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli" / "s.db"))
    assert main(["gate", "--repo", str(repo)]) == 2
    assert "SKODUN GATE: FAIL(2)" in capsys.readouterr().out
