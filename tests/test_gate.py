"""The gate is the project's central fail-closed component.

Every test here asserts a specific exit code, and the ones that inject failure
assert `2` rather than merely "non-zero": the whole point of the contract is
that corruption must never be reported as `1` ("findings remain open"), because
a `1` tells a human "go triage the findings" about a review that does not
exist. A test that accepted any non-zero code would pass vacuously on exactly
the bug it exists to catch.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import skodun
from skodun import gitio, triage
from skodun.config import load_config
from skodun.gate import GateResult, run_gate
from skodun.store import SCHEMA_VERSION, Store
from tests.test_gitio import _git, _mkrepo
from tests.test_triage import LEGACY, _load_legacy

# `.../src`, so a subprocess started with `python -m skodun` imports the same
# package pytest is testing. In-process the ini's `pythonpath` handles this; a
# subprocess inherits nothing of it.
_SRC = str(Path(skodun.__file__).resolve().parents[1])


class _Boom(BaseException):
    """A `BaseException` that is not `KeyboardInterrupt`.

    The gate's handlers are `except BaseException` by design, and pinning that
    with a real `KeyboardInterrupt` means a regression aborts the entire pytest
    session instead of producing one red test.
    """


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    """Pin `SKODUN_DB` inside `tmp_path` for EVERY test in this module.

    The CLI seam falls back to `~/.local/share/skodun/skodun.db`, so a test
    that exercises it without setting `SKODUN_DB` would write gate events into
    the developer's real store. Individual tests still set it to the exact path
    they want to read back; this fixture is the floor, so forgetting cannot
    reach the real one. (`test_cli_gate_never_touches_the_real_store_path`
    deliberately unsets it again to assert the fallback.)
    """
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse-store" / "skodun.db"))


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
              review_id="r1", base_sha=None, untracked_max=100):
    # `untracked_max` must match what the gate will read out of the repo's
    # config, or the recorded identity is not the one the gate computes.
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, untracked_max)
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
# The triage EVENT STREAM (v3) as the gate sees it
# --------------------------------------------------------------------------
#
# `gate.py` is byte-identical across Phase 3 -- it still asks
# `store.triage_for(branch, base_sha)` and still calls `open_findings`. What
# changed underneath is where that answer comes from: an append-only event
# stream whose last event by `seq` decides. These tests assert the gate's
# answer, which is the only thing that decision is for.

_REOPEN_REASON = "the guard was deleted in the refactor; it crashes again on main"


def _v2_store_with_a_dismissal(db: Path, repo: Path) -> None:
    """A Phase-2-shaped store: v2 schema, one trustworthy review of `repo`'s
    outgoing change, and its only finding dismissed in the SINGLE-ROW `triage`
    ledger -- no event stream, because v2 had none.

    This is the database on a user's disk the moment Phase 3 lands, so the v3
    migration is only correct if the gate's answer about it does not change.
    The DDL comes from the frozen copies in `tests/test_store.py` rather than
    from `store._SCHEMA`, for the reason recorded there: a fixture that tracks
    the code cannot be evidence about upgrading from the old shape.
    """
    from skodun.textnorm import finding_key, ledger_key
    from tests.test_store import (PHASE1_SCHEMA, PHASE2_PROVIDER_STATE,
                                  _insert_legacy_triage)

    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    branch = gitio.current_branch(repo)
    art = dict(id="r1", reviewed_at="2026-07-27T10:00:00Z", branch=branch,
               head=gitio.head_sha(repo), base_ref=base.ref, base_sha=base.sha,
               diff_hash=gitio.diff_identity(diff.data), context_hash="", mode="now",
               model="m", adapter="grok", status="findings", parse_ok=True,
               degraded=False, diff_truncated=False, trustworthy=True,
               stop_reason="EndTurn", summary="s", findings_total=1,
               severity={"high": 1, "medium": 0, "low": 0}, findings=[_FINDING],
               source="skodun")
    fkey = finding_key(_FINDING["file"], _FINDING["title"])

    raw = sqlite3.connect(db)
    raw.executescript(PHASE1_SCHEMA)
    raw.executescript(PHASE2_PROVIDER_STATE)
    raw.execute(
        """INSERT INTO reviews (id, reviewed_at, branch, head, base_ref, base_sha,
             diff_hash, context_hash, mode, model, adapter, status, parse_ok,
             degraded, diff_truncated, trustworthy, stop_reason, findings_total,
             sev_high, sev_medium, sev_low, summary, source, artifact_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,1,?,1,1,0,0,?,'skodun',?)""",
        (art["id"], art["reviewed_at"], branch, art["head"], base.ref, base.sha,
         art["diff_hash"], "", "now", "m", "grok", "findings", "EndTurn", "s",
         json.dumps(art)))
    _insert_legacy_triage(raw, dict(
        ledger_key=ledger_key(branch, base.sha, fkey), finding_key=fkey,
        review_id="r1", branch=branch, base_sha=base.sha, file=_FINDING["file"],
        line=_FINDING["line"], severity=_FINDING["severity"], title=_FINDING["title"],
        dismissed_reason=_REASON, dismissed_at="2026-07-27T11:00:00Z"))
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()


def test_the_v3_migration_preserves_an_existing_dismissals_effect_on_the_gate(tmp_path):
    """A dismissal a human recorded before Phase 3 still passes the gate after
    the store migrates. Without the migration's seeding, every dismissal on the
    real store silently evaporates and every previously-triaged finding comes
    back open -- the exact failure the ledger exists to prevent, delivered by an
    upgrade."""
    repo = _outgoing(_mkrepo(tmp_path))
    db = tmp_path / "s.db"
    _v2_store_with_a_dismissal(db, repo)

    st = Store.open(db)                    # v2 -> v4, seeding the event stream
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 0, r.message
    assert "all triaged" in r.message


def test_that_gate_continuity_test_is_sensitive(tmp_path):
    """The control for the test above: the SAME v2 store with its `triage` row
    deleted gates 1. Without this, a seeding that silently dropped the reason,
    the key, or the row could still leave the test above green for the wrong
    reason -- e.g. if the gate had stopped consulting the ledger at all."""
    repo = _outgoing(_mkrepo(tmp_path))
    db = tmp_path / "s.db"
    _v2_store_with_a_dismissal(db, repo)
    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM triage")
    raw.commit()
    raw.close()

    st = Store.open(db)
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1


def test_reopening_a_dismissed_finding_takes_the_gate_from_0_back_to_1(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1

    triage.dismiss(st, _artifact(st), 0, _REASON, "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 0

    triage.reopen(st, _artifact(st), 0, _REOPEN_REASON, "2026-07-27T12:00:00Z")
    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 1
    assert "1 finding(s) open" in r.message

    # ... and a fresh dismissal closes it again, on top of the same stream.
    triage.dismiss(st, _artifact(st), 0, "the guard is back, with a test this time",
                   "2026-07-27T13:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 0

    from skodun.textnorm import finding_key, ledger_key
    art = _artifact(st)
    lkey = ledger_key(art["branch"], art["base_sha"],
                      finding_key(_FINDING["file"], _FINDING["title"]))
    assert [h["event"] for h in st.triage_history(lkey)] == \
        ["dismiss", "reopen", "dismiss"]


# --------------------------------------------------------------------------
# defer (v4): the gate's own answer, with gate.py still byte-identical
# --------------------------------------------------------------------------
#
# `defer` is the escape from an endless review round -- "real, not blast-radius
# for this change, filed as X" -- and the gate has to treat it as non-blocking
# for that to be true. It does so WITHOUT ONE BYTE OF `gate.py` CHANGING: the
# gate asks `store.triage_for(branch, base_sha)` and tests membership by
# `finding_key`, so widening what that map contains is the whole of the change.
# These tests assert the gate's exit code, which is the only thing that decision
# is for; `tests/test_seams.py` pins the file's sha256 beside them.

_DEFER_REASON = "in-bounds for this surface; the hot path is the batcher upstream"
_TRACKING_REF = "GH-412"


def test_deferring_a_finding_takes_the_gate_from_1_to_0(tmp_path):
    """THE property issue #5 is about, at the enforcement point.

    Mutation this kills: `triage_for` filtering on `dismiss` alone. With
    `CLEARING_EVENTS` narrowed back to `{dismiss}` the deferral is recorded, the
    listing says DEFERRED, and the gate still answers 1 -- a finding a human
    triaged, on the record, with a filed reference, still blocking the push.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1

    triage.defer(st, _artifact(st), 0, _TRACKING_REF, _DEFER_REASON,
                 "2026-07-27T11:00:00Z")

    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 0, r.message
    assert "all triaged" in r.message
    assert [e["outcome"] for e in _events(st)] == ["open-findings", "pass"]


def test_a_refused_deferral_leaves_the_gate_blocking(tmp_path):
    """The control. An unfiled deferral must not be able to clear anything --
    that is what makes "an unfiled deferral and an ignored finding are the same
    artifact" a mechanical fact rather than a slogan."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    for ref in ("", "   ", "I will file it later"):
        with pytest.raises(triage.TriageError):
            triage.defer(st, _artifact(st), 0, ref, _DEFER_REASON,
                         "2026-07-27T11:00:00Z")
        assert run_gate(st, repo, _cfg(repo), env={}).code == 1


def test_reopening_a_deferral_takes_the_gate_from_0_back_to_1(tmp_path):
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    triage.defer(st, _artifact(st), 0, _TRACKING_REF, _DEFER_REASON,
                 "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 0

    triage.reopen(st, _artifact(st), 0, _REOPEN_REASON, "2026-07-27T12:00:00Z")

    r = run_gate(st, repo, _cfg(repo), env={})
    assert r.code == 1
    assert "1 finding(s) open" in r.message


def test_a_deferral_scoped_to_another_base_does_not_clear_the_gate(tmp_path):
    """The rebase rule applies to the new verb exactly as to a dismissal: a
    deferral is scoped to the base it was filed against."""
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    art = dict(_artifact(st), base_sha="0" * 40)
    triage.defer(st, art, 0, _TRACKING_REF, _DEFER_REASON, "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1


def test_a_mixed_ledger_of_dismissals_and_deferrals_passes_the_gate(tmp_path):
    """The ledger keeps the two apart -- outstanding debt versus rejected
    findings -- while the gate treats both as triaged. That separation is the
    whole reason `defer` exists rather than being spelled as a dismissal."""
    second = dict(_FINDING, file="b.py", title="unbounded retry")
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING, second])
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1

    triage.dismiss(st, _artifact(st), 0, _REASON, "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1     # one still open
    triage.defer(st, _artifact(st), 1, _TRACKING_REF, _DEFER_REASON,
                 "2026-07-27T11:00:01Z")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 0

    art = _artifact(st)
    state = st.triage_state(art["branch"], art["base_sha"])
    events = {k: v["event"] for k, v in state.items()}
    assert sorted(events.values()) == ["defer", "dismiss"]
    # ... and only the deferral is outstanding work.
    assert [r["tracking_ref"] for r in st.open_deferrals()] == [_TRACKING_REF]


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


def test_gate_divergence_from_oracle_on_a_self_contradicting_artifact(tmp_path):
    """DELIBERATE DIVERGENCE #1 (see `gate.py`), pinned against the real oracle.

    The oracle's `is_trustworthy(row)` short-circuits on the row's own
    `trustworthy` field and only recomputes from the axes when the field is
    absent -- a fallback that exists solely for rows written before the field
    did. So an artifact carrying `trustworthy: true` alongside `degraded:
    true` satisfies the oracle. skodun has no pre-field rows (`save_review`
    computes it on every write), recomputes unconditionally, and additionally
    requires the stored field to agree: a record that contradicts itself
    certifies nothing.

    This test pins BOTH halves, so the rationale in `gate.py` cannot go stale
    while quietly being wrong about what the oracle does. If it fails because
    the oracle dropped its short-circuit, the fix is to delete the divergence
    note -- never to make skodun lenient.
    """
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo)
    _hand_edit(st, "$.degraded", "json('true')")
    art = _artifact(st)
    assert art["trustworthy"] is True and art["degraded"] is True

    assert legacy.is_trustworthy(art) is True        # the oracle accepts it
    r = run_gate(st, repo, _cfg(repo), env={})       # skodun refuses it
    assert r.code == 2
    assert "disagree" in r.message


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
    MemoryError mid-gate must not escape as a Python exit code of 1.

    `_Boom`, not a real `KeyboardInterrupt`: an actual interrupt escaping the
    gate would tear down the whole pytest session, so the regression this test
    exists to catch would show up as an aborted run rather than a failure.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    monkeypatch.setattr(gitio, "capture_diff", _raiser(_Boom("interrupted")))
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


def test_gate_unrecordable_event_survives_a_base_exception(tmp_path, monkeypatch):
    """`_record`'s inner handler is `except BaseException`, not `except
    Exception`, and this is the test that pins the difference.

    A `MemoryError` or an interrupt raised by the store write would otherwise
    propagate straight out of `run_gate` -- a function whose docstring says it
    never raises -- and reach the interpreter as exit code 1, the one value
    that means "findings remain open". Narrowing the handler to `Exception`
    must turn this red.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    _reviewed(st, repo, findings=[_FINDING])
    assert run_gate(st, repo, _cfg(repo), env={}).code == 1        # not vacuous

    monkeypatch.setattr(st, "log_gate_event", _raiser(_Boom("out of memory")))
    r = run_gate(st, repo, _cfg(repo), env={})
    assert isinstance(r, GateResult)
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


def test_gate_event_note_keeps_the_identity_notes(tmp_path):
    """The RECORD has to show the identity was under-scoped, not just the
    terminal output.

    The note used to be the message's last line only, which kept the verdict
    and dropped every `identity note:` line -- so the one reader who looks at
    `gate_events` after the fact, an auditor, could not see that the decision
    was made against a fallback base or a capped untracked scan.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "branch", "-m", "master")     # no github/main|origin/main|main
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    assert run_gate(st, repo, _cfg(repo), env={}).code == 2
    (ev,) = _events(st)
    assert "identity note:" in ev["note"] and "no main ref" in ev["note"]
    assert "no trustworthy review" in ev["note"]     # the verdict line too
    assert "\n" not in ev["note"]                    # single-line column


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


def _five_untracked_capped_at_three(repo: Path) -> None:
    """A repo whose identity depends on a config value, and visibly so.

    With `untracked_max = 3` and five untracked files the gate must both hash a
    CAPPED capture and say so in an `identity note:` line. Read the config from
    the wrong directory and both disappear silently: the cap reverts to the
    100-file default (a different, larger capture and therefore a different
    `diff_hash`) and the note that exists to make an under-scoped identity loud
    at the enforcement point is never printed.
    """
    (repo / ".skodun.toml").write_text("[defaults]\nuntracked_max = 3\n",
                                       encoding="utf-8")
    for i in range(5):
        (repo / f"u{i}.txt").write_text(f"u{i}\n", encoding="utf-8")


def test_cli_gate_from_a_subdirectory_decides_exactly_as_from_the_root(
        tmp_path, monkeypatch, capsys):
    """The config and the diff identity must resolve against ONE directory.

    `gitio.capture_diff` normalises to the worktree root before any git call --
    its docstring explains at length why that is load-bearing -- but the CLI
    used to hand `load_config` the raw `--repo`, which defaults to `.`. So
    `skodun gate` computed a different `diff_hash` depending on which directory
    it was run from: a review taken from the root could never satisfy a gate
    run from a subdirectory, and pre-push hooks and manual runs routinely
    differ. The cap note vanished with the config that set it.
    """
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    _five_untracked_capped_at_three(repo)
    sub = repo / "sub"
    sub.mkdir()
    db = tmp_path / "cli" / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))

    monkeypatch.chdir(repo)
    from_root_code = main(["gate"])
    from_root = capsys.readouterr().out

    monkeypatch.chdir(sub)
    from_sub_code = main(["gate"])
    from_sub = capsys.readouterr().out

    assert from_root_code == from_sub_code == 2
    assert from_root == from_sub                     # byte for byte
    assert "identity note: untracked scan capped at 3" in from_root
    # ...and the two recorded decisions are about the same content.
    events = _events(Store.open(db))
    assert len(events) == 2
    assert events[0]["diff_hash"] == events[1]["diff_hash"]
    assert "untracked scan capped at 3" in events[1]["note"]


def test_cli_gate_from_a_subdirectory_accepts_a_review_taken_from_the_root(
        tmp_path, monkeypatch, capsys):
    """The consequence that actually stops a push, asserted end to end."""
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    _five_untracked_capped_at_three(repo)
    sub = repo / "sub"
    sub.mkdir()
    db = tmp_path / "cli" / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))

    # A review recorded against the identity computed at the ROOT, under the
    # repo's own cap -- exactly what `skodun review` from the root produces.
    st = Store.open(db)
    _reviewed(st, repo, untracked_max=3)
    monkeypatch.chdir(repo)
    assert main(["gate"]) == 0
    # ...must still satisfy the gate run from a subdirectory.
    monkeypatch.chdir(sub)
    assert main(["gate"]) == 0
    capsys.readouterr()


def test_any_open_finding_blocks_the_gate_regardless_of_severity(tmp_path):
    """The gate blocks on ANY open finding, whatever its severity -- by design.

    Phase 1 declared `[defaults]` keys `severity_gate` and `confidence_threshold`
    as forward-looking stubs: they were accepted and bounds-checked but nothing
    read either one, so a user writing `severity_gate = "high"` could reasonably
    (and wrongly) conclude that `low` findings would not block a push. Phase 2
    removed both keys rather than ever implement that filter: a key that looks
    like it filters findings but does not is a safety trap, and the gate design
    is that severity never gates -- only triage does. (The removal itself, and
    its migration error for a config that still sets either key, is pinned in
    `tests/test_config.py`.) This test is the surviving pin of the actual
    behavior: a single `low` finding still blocks, and only triage -- never
    severity -- clears it.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    st = Store.open(tmp_path / "s.db")
    cfg = _cfg(repo)
    _reviewed(st, repo, findings=[dict(_FINDING, severity="low")])

    r = run_gate(st, repo, cfg, env={})
    assert r.code == 1                               # not 0
    assert "1 finding(s) open" in r.message

    # ...and triage, not severity, is what clears it.
    triage.dismiss(st, _artifact(st), 0, _REASON, "2026-07-27T11:00:00Z")
    assert run_gate(st, repo, cfg, env={}).code == 0


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


def test_cli_gate_setup_failure_still_records_a_gate_event(tmp_path, monkeypatch,
                                                          capsys):
    """A FAIL(2) decided ABOVE `run_gate` is still a decision, and the contract
    is that every decision is recorded.

    `load_config` is what fails here, and the store opens perfectly well — so
    there is somewhere to write the row, and a reported-and-enforced refusal
    with nothing on the record would leave an auditor unable to see that the
    push was stopped at all.
    """
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    (repo / ".skodun.toml").write_text("[defaults]\nnope = 1\n", encoding="utf-8")
    db = tmp_path / "cli" / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    assert main(["gate", "--repo", str(repo)]) == 2
    assert "SKODUN GATE: FAIL(2)" in capsys.readouterr().out

    (ev,) = _events(Store.open(db))
    assert (ev["outcome"], ev["code"]) == ("error", 2)
    assert ev["diff_hash"] is None        # no identity was ever computed
    assert "unknown [defaults] keys" in ev["note"]     # names the failure
    assert ev["branch"] == "feat"


def test_cli_gate_unopenable_store_is_2_and_records_nothing(tmp_path, monkeypatch,
                                                            capsys):
    """The one case where a decision legitimately goes unrecorded.

    If the store cannot be opened there is nowhere to put the row, so 2 with
    no `gate_events` entry is the only option available — and it is the safe
    one: an unrecordable refusal is still a refusal.
    """
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    blocker = tmp_path / "blocked"        # a FILE where a directory must be
    blocker.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_DB", str(blocker / "s.db"))

    assert main(["gate", "--repo", str(repo)]) == 2
    assert "SKODUN GATE: FAIL(2) could not open the store" in capsys.readouterr().out
    assert blocker.read_text(encoding="utf-8") == "not a directory\n"


# --------------------------------------------------------------------------
# The process boundary — the exit code the shell sees is the gate's own
# --------------------------------------------------------------------------


def _subprocess_env(db: Path) -> dict:
    env = dict(os.environ)          # carries the autouse SKODUN_CONFIG pin
    env["SKODUN_DB"] = str(db)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


@pytest.mark.parametrize("module", ["skodun", "skodun.cli"])
def test_module_invocation_runs_the_gate_and_exits_2(tmp_path, module):
    """`python -m skodun` and `python -m skodun.cli` are real invocation forms.

    Without `__main__.py` and the `__main__` guard in `cli.py`, both import
    the module, define `main`, call nothing and exit 0 — a fail-closed
    component silently certifying an unreviewed push, while the console script
    on the same repo exits 2. Only a subprocess exercises the interpreter's
    module-running path, so this cannot be asserted in-process.

    The message is asserted too: an exit 2 that printed nothing would mean the
    process failed on its way to the gate rather than because of it.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    p = subprocess.run(
        [sys.executable, "-m", module, "gate", "--repo", str(repo)],
        capture_output=True, text=True, env=_subprocess_env(tmp_path / "sub" / "s.db"))
    assert p.returncode == 2, f"stdout={p.stdout!r} stderr={p.stderr!r}"
    assert "SKODUN GATE: FAIL(2)" in p.stdout
    assert "no trustworthy review" in p.stdout


@pytest.mark.parametrize("module", ["skodun", "skodun.cli"])
def test_module_invocation_with_no_subcommand_is_not_a_silent_success(tmp_path, module):
    """Same class of hole, one level up: running the module with no subcommand
    must not report success either."""
    p = subprocess.run([sys.executable, "-m", module], capture_output=True, text=True,
                       env=_subprocess_env(tmp_path / "sub" / "s.db"))
    assert p.returncode == 2, f"stdout={p.stdout!r} stderr={p.stderr!r}"
    assert "usage:" in p.stderr


@pytest.mark.parametrize("findings, expected, outcome",
                         [(None, 2, "no-review"), ((_FINDING,), 1, "open-findings")])
def test_gate_exit_code_survives_a_closed_stdout(tmp_path, findings, expected, outcome):
    """`skodun gate | head`, `| grep -q`, a full disk, a closed fd.

    stdout's read end is closed before the child writes a byte, so the write
    raises `BrokenPipeError` deterministically. Escaping, that exception would
    leave the interpreter's exit code of 1 — turning "no trustworthy review
    covers this" into "go triage the findings" about a review that does not
    exist. Both directions are asserted: the 2 must not become a 1, and the
    real 1 must not become a blanket 2 either.

    The recorded event proves the gate ran and decided for itself, so neither
    assertion can be satisfied by a process that died on its way there.
    """
    repo = _outgoing(_mkrepo(tmp_path))
    db = tmp_path / "sub" / "s.db"
    if findings is not None:
        _reviewed(Store.open(db), repo, findings=list(findings))
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run([sys.executable, "-m", "skodun", "gate", "--repo", str(repo)],
                           stdout=w_fd, stderr=subprocess.PIPE, text=True,
                           env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == expected, f"stderr={p.stderr!r}"
    assert [e["outcome"] for e in _events(Store.open(db))] == [outcome]


class _DeadStdout:
    """A stdout whose writes fail, like a pipe whose reader has gone.

    Only the WRITES fail: a broken pipe is still a perfectly valid descriptor,
    and argparse probes `sys.stdout` for colour support before parsing
    anything. `fileno` raises `io.UnsupportedOperation` the way an in-memory
    stream does, which also keeps `_emit`'s devnull redirect from touching a
    real fd while pytest is capturing one.
    """

    encoding = "utf-8"

    def write(self, *a):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")

    def isatty(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


@pytest.mark.parametrize("findings, expected", [(None, 2), ((), 0), ((_FINDING,), 1)])
def test_cli_gate_output_failure_never_edits_the_verdict(tmp_path, monkeypatch,
                                                         findings, expected):
    """Printing is delivery, not decision: a failure to deliver returns the
    gate's own code, all three of them.

    Parametrized across 0/1/2 on purpose — a handler that swallowed the error
    and returned a constant 2 would satisfy the FAIL case alone.
    """
    from skodun.cli import main

    repo = _outgoing(_mkrepo(tmp_path))
    db = tmp_path / "cli" / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    if findings is not None:
        _reviewed(Store.open(db), repo, findings=list(findings))
    monkeypatch.setattr(sys, "stdout", _DeadStdout())
    assert main(["gate", "--repo", str(repo)]) == expected
