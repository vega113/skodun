"""Dedup evidence and the context rules the dispatcher's reservation applies.

Three things are pinned here, and they are pinned separately because they fail
separately:

  * **The context rules** are pure functions over an artifact plus a candidate
    hash, and the SHIPPED three-state semantics of `context_hash` are what they
    read: key absent or JSON `null` (a legacy import) is the transitional
    "no context was recorded" state and suppresses on the diff hash alone; a
    non-empty hash suppresses only against an equal candidate; `""` — which the
    shipped pipeline writes for a packing-disabled review AND which Task 8's
    batched aggregate writes by construction — NEVER suppresses.
  * **The evidence builder** packs the PUSHED COMMIT's tree once, with the
    settings a review of that commit would use, and turns any failure into
    invalid evidence rather than into a wrong hash. Invalid evidence can never
    suppress anything, not even a legacy candidate that needs no hash at all.
  * **The foreground never dedups**, behaviourally: a real `--now` review runs
    to completion with the provider invoked while every dedup entry point is
    rigged to explode.

Isolation follows `test_pipeline.py` and is not optional: `SKODUN_DB`,
`SKODUN_CONFIG` and `SKODUN_GROK_BIN` are pinned inside `tmp_path`, so no test
can reach the developer's own store, config or `~/.grok/bin/grok`. Nothing here
writes to a store at all — the builder is handed a store that raises on ANY
attribute access, which is how "nothing in this task persists anything" is
pinned rather than asserted in prose.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skodun import contextpack, dispatch, promptbuild, runner
from skodun.config import Defaults
from skodun.dispatch import (
    CONTEXT_AMBIGUOUS,
    CONTEXT_HASHED,
    CONTEXT_LEGACY,
    DedupEvidence,
    artifact_context_state,
    build_dedup_evidence,
    context_permits_suppression,
    evidence_permits_suppression,
)
from skodun.gitio import capture_ref_diff
from tests.conftest import oracle_dir
from tests.test_gitio import _git, _mkrepo
from tests.test_pipeline import CLEAN, _calls, _emit, _fake_grok, _repo, _run, _store

ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None
_NO_ORACLE = ORACLE is None or not ORACLE.exists()
requires_oracle = pytest.mark.skipif(
    _NO_ORACLE, reason="oracle checkout not present (set SKODUN_ORACLE_DIR)"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

HASH_A = "a" * 64
HASH_B = "b" * 64

#: The four artifact shapes. Only `context_hash` differs; everything else is
#: the ordinary record shape a lookup returns.
ABSENT: dict = {"id": "r1", "diff_hash": "d1"}
NULL: dict = {"id": "r1", "diff_hash": "d1", "context_hash": None}
EMPTY: dict = {"id": "r1", "diff_hash": "d1", "context_hash": ""}
HASHED: dict = {"id": "r1", "diff_hash": "d1", "context_hash": HASH_A}

#: `(artifact, candidate_context_hash, suppressible)` — the whole rule table,
#: all three artifact states times candidate present/absent.
RULE_TABLE = [
    pytest.param(ABSENT, HASH_A, True, id="absent-key+candidate"),
    pytest.param(ABSENT, None, True, id="absent-key+no-candidate"),
    pytest.param(NULL, HASH_A, True, id="json-null+candidate"),
    pytest.param(NULL, None, True, id="json-null+no-candidate"),
    pytest.param(EMPTY, HASH_A, False, id="empty-string+candidate"),
    pytest.param(EMPTY, None, False, id="empty-string+no-candidate"),
    pytest.param(HASHED, HASH_A, True, id="hash+equal-candidate"),
    pytest.param(HASHED, HASH_B, False, id="hash+different-candidate"),
    pytest.param(HASHED, None, False, id="hash+no-candidate"),
]


class _ExplodingStore:
    """A store stand-in that fails on any use at all.

    The builder is documented to persist nothing and to read nothing: the
    authoritative match query belongs to Task 10's reservation transaction. A
    stub that raises on every attribute access turns that into a test rather
    than a promise.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"build_dedup_evidence touched the store: {name}")


def _oid_repo(tmp_path: Path, added_bytes: int = 20_000) -> tuple[Path, str, str]:
    """A repo whose HEAD is COMMITTED and whose working tree has moved on.

    Returns `(repo, base_sha, oid)`. The commit modifies `a.txt` and adds a
    `big.txt` of `added_bytes` (>= `contextpack.ALREADY_IN_DIFF_MAX`, so
    `pack_large_added` changes the packed body), and the working tree is then
    edited so a working-tree pack and a commit-tree pack cannot agree.
    """
    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("modified\n" * 40, encoding="utf-8")
    (repo / "big.txt").write_text("x" * added_bytes, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    oid = _git(repo, "rev-parse", "HEAD")
    # The pushed commit is history now; the developer keeps typing.
    (repo / "a.txt").write_text("moved on\n" * 40, encoding="utf-8")
    return repo, base, oid


def _pack_spy(monkeypatch) -> list[tuple]:
    """Record every `contextpack.pack` call while still doing the real work."""
    calls: list[tuple] = []
    real = contextpack.pack

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(contextpack, "pack", spy)
    return calls


def _explode_pack(monkeypatch, exc: BaseException) -> None:
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(contextpack, "pack", boom)


def _build(repo: Path, diff, oid: str, d: Defaults | None = None,
           enabled: bool = True) -> DedupEvidence:
    return build_dedup_evidence(_ExplodingStore(), repo, diff, oid,
                                d if d is not None else Defaults(), enabled)


# --------------------------------------------------------------------------
# the context rules: three artifact states x candidate present/absent
# --------------------------------------------------------------------------


@pytest.mark.parametrize("artifact,candidate,expected", RULE_TABLE)
def test_the_context_rule_table(artifact, candidate, expected):
    assert context_permits_suppression(artifact, candidate) is expected


@pytest.mark.parametrize("artifact,state", [
    pytest.param(ABSENT, CONTEXT_LEGACY, id="absent-key"),
    pytest.param(NULL, CONTEXT_LEGACY, id="json-null"),
    pytest.param(EMPTY, CONTEXT_AMBIGUOUS, id="empty-string"),
    pytest.param(HASHED, CONTEXT_HASHED, id="hash"),
])
def test_the_three_artifact_states_are_classified_apart(artifact, state):
    assert artifact_context_state(artifact) == state


def test_the_empty_string_is_not_the_legacy_state():
    """The shipped `""` is AMBIGUOUS, never legacy.

    The store writes `""` for a missing value in its column and the pipeline
    writes it into the artifact when packing was disabled or nothing was
    packed, and a batched aggregate writes it by construction. Reading it as
    "no context was recorded" would suppress re-review of a diff whose context
    nobody ever compared.
    """
    assert artifact_context_state(EMPTY) != artifact_context_state(NULL)
    assert artifact_context_state(EMPTY) != artifact_context_state(ABSENT)


@pytest.mark.parametrize("value", [0, 1, [], {}, ["a" * 64], b"a" * 64, 3.5, True])
def test_a_non_string_artifact_context_hash_never_suppresses(value):
    artifact = {"id": "r1", "context_hash": value}
    assert artifact_context_state(artifact) == CONTEXT_AMBIGUOUS
    assert context_permits_suppression(artifact, HASH_A) is False
    assert context_permits_suppression(artifact, None) is False


def test_a_whitespace_only_artifact_context_hash_never_suppresses():
    artifact = {"id": "r1", "context_hash": "   \n"}
    assert artifact_context_state(artifact) == CONTEXT_AMBIGUOUS
    assert context_permits_suppression(artifact, "   \n") is False


@pytest.mark.parametrize("candidate", ["", "   ", None, 0, [], HASH_A + "\n"])
def test_a_blank_or_non_string_candidate_never_matches_a_hash(candidate):
    assert context_permits_suppression(HASHED, candidate) is False


def test_a_non_mapping_artifact_never_suppresses():
    for artifact in (None, "context_hash", 7, ["context_hash"]):
        assert artifact_context_state(artifact) == CONTEXT_AMBIGUOUS
        assert context_permits_suppression(artifact, HASH_A) is False


# --------------------------------------------------------------------------
# the evidence gate in front of those rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize("artifact,candidate,expected", RULE_TABLE)
def test_valid_enabled_evidence_applies_the_rule_table(artifact, candidate, expected):
    ev = DedupEvidence(enabled=True, valid=True, candidate_context_hash=candidate)
    assert evidence_permits_suppression(artifact, ev) is expected


@pytest.mark.parametrize("artifact", [ABSENT, NULL, EMPTY, HASHED])
def test_disabled_evidence_never_suppresses(artifact):
    ev = DedupEvidence(enabled=False)
    assert evidence_permits_suppression(artifact, ev) is False


@pytest.mark.parametrize("artifact", [ABSENT, NULL, EMPTY, HASHED])
def test_invalid_evidence_never_suppresses(artifact):
    """Invalid evidence is not "no context hash" — it is "no answer at all".

    Spec §3's "any probe error ⇒ review" is literal, so the legacy rule (which
    needs no candidate hash) must not become a back door for a failed probe.
    """
    ev = DedupEvidence(enabled=True, valid=False)
    assert evidence_permits_suppression(artifact, ev) is False
    # ...and not even if a hash somehow rode along with the failure.
    ev2 = DedupEvidence(enabled=True, valid=False, candidate_context_hash=HASH_A)
    assert evidence_permits_suppression(artifact, ev2) is False


def test_non_bool_evidence_flags_never_suppress():
    """`1`/`"yes"` are not `True`. Truthiness may not enable a suppression."""
    for ev in (DedupEvidence(enabled=1, valid=1),            # type: ignore[arg-type]
               DedupEvidence(enabled="yes", valid="yes"),    # type: ignore[arg-type]
               DedupEvidence(enabled=True, valid=1)):        # type: ignore[arg-type]
        assert evidence_permits_suppression(ABSENT, ev) is False


@pytest.mark.parametrize("evidence", [
    None,
    {"enabled": True, "valid": True, "candidate_context_hash": HASH_A},
    ("enabled", "valid"),
])
def test_anything_that_is_not_evidence_never_suppresses(evidence):
    """A dict shaped like the evidence is not the evidence."""
    assert evidence_permits_suppression(ABSENT, evidence) is False


def test_the_evidence_shape_is_the_documented_one():
    ev = DedupEvidence(enabled=True, valid=True, candidate_context_hash=HASH_A)
    assert (ev.enabled, ev.valid, ev.candidate_context_hash) == (True, True, HASH_A)
    # Fail-closed defaults: an evidence value nobody filled in cannot suppress.
    bare = DedupEvidence(enabled=True)
    assert bare.valid is False and bare.candidate_context_hash is None
    assert evidence_permits_suppression(ABSENT, bare) is False


# --------------------------------------------------------------------------
# the builder
# --------------------------------------------------------------------------


def test_dedup_disabled_builds_disabled_evidence_and_never_packs(tmp_path,
                                                                monkeypatch):
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    _explode_pack(monkeypatch, AssertionError("packed with dedup disabled"))

    ev = _build(repo, diff, oid, enabled=False)

    assert ev.enabled is False
    assert ev.valid is False
    assert ev.candidate_context_hash is None
    assert evidence_permits_suppression(ABSENT, ev) is False


def test_enabled_dedup_packs_the_pushed_commit_tree_exactly_once(tmp_path,
                                                                monkeypatch):
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    calls = _pack_spy(monkeypatch)

    ev = _build(repo, diff, oid)

    assert len(calls) == 1
    kwargs = calls[0][1]
    assert kwargs["source"] == "oid" and kwargs["oid"] == oid
    assert ev.enabled is True and ev.valid is True
    assert isinstance(ev.candidate_context_hash, str)
    assert len(ev.candidate_context_hash) == 64


def test_the_candidate_hash_is_the_commit_tree_not_the_working_tree(tmp_path):
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    d = Defaults()
    headroom = promptbuild.context_headroom(d.max_diff_bytes, len(diff.data),
                                            packing=True)

    ev = _build(repo, diff, oid, d)

    from_commit = contextpack.pack(repo, diff.files, diff.statuses, headroom,
                                   source="oid", oid=oid, pack_large_added=False)
    from_worktree = contextpack.pack(repo, diff.files, diff.statuses, headroom,
                                     pack_large_added=False)
    assert ev.candidate_context_hash == from_commit.sha256
    assert from_worktree.sha256 != from_commit.sha256


def test_the_candidate_hash_uses_the_settings_a_review_would_use(tmp_path):
    """`pack_large_added=False` and the prompt's own headroom, not defaults.

    The pushed diff carries every added file whole, so the single-shot rule the
    shipped pipeline uses applies here too — and it is observable: `big.txt` is
    over `ALREADY_IN_DIFF_MAX`, so the packer's default (`True`) would pack it
    and produce a different identity for the same commit.
    """
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    d = Defaults()
    headroom = promptbuild.context_headroom(d.max_diff_bytes, len(diff.data),
                                            packing=True)

    ev = _build(repo, diff, oid, d)

    single_shot = contextpack.pack(repo, diff.files, diff.statuses, headroom,
                                   source="oid", oid=oid, pack_large_added=False)
    large_added = contextpack.pack(repo, diff.files, diff.statuses, headroom,
                                   source="oid", oid=oid, pack_large_added=True)
    assert large_added.sha256 != single_shot.sha256, "fixture no longer discriminates"
    assert ev.candidate_context_hash == single_shot.sha256


def test_the_candidate_hash_uses_the_prompt_headroom_not_the_envelope(tmp_path):
    """The diff wins the envelope; context gets the leftover, and only that.

    Pinned with an envelope barely wider than the diff, where the leftover is
    too small for `a.txt`: packing against the whole envelope instead would
    include it and hash to something a real review could never reproduce.
    """
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    envelope = len(diff.data) + 250
    d = Defaults(max_diff_bytes=envelope)
    headroom = promptbuild.context_headroom(envelope, len(diff.data), packing=True)
    assert 0 < headroom < envelope

    ev = _build(repo, diff, oid, d)

    leftover = contextpack.pack(repo, diff.files, diff.statuses, headroom,
                                source="oid", oid=oid, pack_large_added=False)
    whole_envelope = contextpack.pack(repo, diff.files, diff.statuses, envelope,
                                      source="oid", oid=oid, pack_large_added=False)
    assert leftover.sha256 != whole_envelope.sha256, "fixture no longer discriminates"
    assert ev.candidate_context_hash == leftover.sha256


def test_packing_disabled_yields_valid_evidence_with_no_candidate_hash(tmp_path,
                                                                      monkeypatch):
    """`context_pack = false` is a REVIEW setting, not a failure.

    A review run with packing off records no context hash, so the candidate has
    none either: a legacy candidate still suppresses, a context-bearing one
    cannot.
    """
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    _explode_pack(monkeypatch, AssertionError("packed with context_pack off"))

    ev = _build(repo, diff, oid, Defaults(context_pack=False))

    assert ev.enabled is True and ev.valid is True
    assert ev.candidate_context_hash is None
    assert evidence_permits_suppression(ABSENT, ev) is True
    assert evidence_permits_suppression(HASHED, ev) is False
    assert evidence_permits_suppression(EMPTY, ev) is False


def test_a_packing_exception_invalidates_the_evidence(tmp_path, monkeypatch, capsys):
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    _explode_pack(monkeypatch, RuntimeError("git exploded"))

    ev = _build(repo, diff, oid)

    assert ev.enabled is True
    assert ev.valid is False
    assert ev.candidate_context_hash is None
    err = capsys.readouterr().err
    assert "dedup evidence" in err and "git exploded" in err


def test_a_headroom_failure_invalidates_the_evidence(tmp_path):
    """The failure need not come from the packer: anything on the path counts."""
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)

    ev = _build(repo, diff, oid, Defaults(max_diff_bytes=0))

    assert ev.enabled is True and ev.valid is False
    assert ev.candidate_context_hash is None


def test_an_unusable_oid_invalidates_the_evidence(tmp_path):
    """An empty oid would read the INDEX; the packer refuses and so do we."""
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)

    ev = _build(repo, diff, "")

    assert ev.enabled is True and ev.valid is False


def test_a_blank_pack_hash_invalidates_the_evidence(tmp_path, monkeypatch, capsys):
    """Mirrors the oracle's `[ -n "$GR_CONTEXT_HASH" ]` guard before dedup."""
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    monkeypatch.setattr(contextpack, "pack",
                        lambda *a, **kw: contextpack.Pack(body=b"", sha256=""))

    ev = _build(repo, diff, oid)

    assert ev.enabled is True and ev.valid is False
    assert ev.candidate_context_hash is None
    assert "dedup evidence" in capsys.readouterr().err


def test_a_failed_build_cannot_suppress_a_legacy_candidate(tmp_path, monkeypatch):
    """The regression Task 10's transaction inherits.

    A legacy candidate suppresses without any packing comparison, so a builder
    that failed must not hand the transaction something that still satisfies
    that rule. This is the rule-level half; Task 10 wires the transaction.
    """
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    _explode_pack(monkeypatch, RuntimeError("no pack for you"))

    ev = _build(repo, diff, oid)

    assert ev.valid is False
    assert evidence_permits_suppression(ABSENT, ev) is False
    assert evidence_permits_suppression(NULL, ev) is False
    assert evidence_permits_suppression(HASHED, ev) is False
    assert evidence_permits_suppression(EMPTY, ev) is False


def test_a_cancellation_is_not_an_evidence_failure(tmp_path, monkeypatch):
    """`Exception`, not `BaseException`: an interrupt must still interrupt."""
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)
    _explode_pack(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _build(repo, diff, oid)


def test_the_builder_touches_no_store_and_writes_nothing(tmp_path):
    """`_ExplodingStore` raises on any attribute access; a clean build proves it.

    Nothing in this task persists anything: the match query, the suppression and
    the audit row all belong to the reservation transaction.
    """
    repo, base, oid = _oid_repo(tmp_path)
    diff = capture_ref_diff(repo, base, oid)

    assert _build(repo, diff, oid).valid is True
    assert _build(repo, diff, oid, enabled=False).enabled is False


# --------------------------------------------------------------------------
# the foreground never dedups -- behaviourally
# --------------------------------------------------------------------------


def test_a_foreground_now_review_never_builds_dedup_evidence(tmp_path, monkeypatch,
                                                             capsys):
    """A real `--now` review with EVERY dedup entry point rigged to explode.

    Not a grep test: the review runs end to end against the fake provider, and
    the only thing that makes the assertions meaningful is that the foreground
    path never reaches this module at all.
    """
    calls: list[str] = []

    def boom(name):
        def _boom(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"foreground review called {name}")
        return _boom

    for name in ("build_dedup_evidence", "evidence_permits_suppression",
                 "context_permits_suppression", "artifact_context_state"):
        monkeypatch.setattr(dispatch, name, boom(name))

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    store = _store(tmp_path)

    rec = _run(repo, store)

    assert calls == []
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert rec["mode"] == "now"
    assert _calls(tmp_path) == 1, "the provider was not invoked"


# --------------------------------------------------------------------------
# oracle parity: the 3-way probe protocol
# --------------------------------------------------------------------------


def _oracle_suppresses(work: Path, record: dict, diff_hash: str,
                       candidate: str | None) -> bool:
    """Run the oracle's dedup protocol over a one-record index.

    This is the dispatcher's own call sequence (`grok-prepush-review.sh`
    4726–4787): probe first, and pack + dual-hash only when the probe answers 2.
    The oracle reads its index ROWS where skodun reads the stored ARTIFACT, but
    the JSON object and the three `context_hash` states are the same, which is
    what makes the comparison meaningful.
    """
    work.mkdir(parents=True, exist_ok=True)
    index = work / "index.jsonl"
    index.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def check(ctx: str) -> int:
        # `cwd=work`: the seam sits before the oracle's dispatcher and touches
        # neither git nor stdin, and running it from a tmp directory keeps any
        # future preamble that does from landing in the repo.
        return subprocess.run(
            ["sh", str(ORACLE), "--dedup-check", str(index), diff_hash, ctx],
            capture_output=True, cwd=work).returncode

    probe = check("__probe__")
    if probe == 0:
        return True
    if probe != 2:
        return False
    return check(candidate or "") == 0


ORACLE_CASES = [
    pytest.param(ABSENT, HASH_A, True, id="absent-key+candidate"),
    pytest.param(ABSENT, None, True, id="absent-key+no-candidate"),
    pytest.param(EMPTY, HASH_A, False, id="empty-string+candidate"),
    pytest.param(EMPTY, None, False, id="empty-string+no-candidate"),
    pytest.param(HASHED, HASH_A, True, id="hash+equal-candidate"),
    pytest.param(HASHED, HASH_B, False, id="hash+different-candidate"),
    pytest.param(HASHED, None, False, id="hash+no-candidate"),
]


@requires_oracle
@pytest.mark.parametrize("artifact,candidate,expected", ORACLE_CASES)
def test_parity_with_the_oracle_dedup_protocol(tmp_path, artifact, candidate,
                                               expected):
    record = dict(artifact, diff_hash="d1", parse_ok=True)
    assert _oracle_suppresses(tmp_path / "o", record, "d1", candidate) is expected
    assert context_permits_suppression(artifact, candidate) is expected


@requires_oracle
def test_oracle_agrees_an_untrustworthy_record_never_suppresses(tmp_path):
    """Sanity check on the harness: the oracle's own trust axes still apply.

    Trust is Task 10's transaction to enforce, not these rules', so this pins
    the parity harness rather than the rule — a harness that suppressed
    everything would make the table above vacuous.
    """
    for extra in ({"parse_ok": False}, {"degraded": True}, {"diff_truncated": True}):
        record = {**ABSENT, "diff_hash": "d1", "parse_ok": True, **extra}
        assert _oracle_suppresses(tmp_path / "o", record, "d1", HASH_A) is False


@requires_oracle
def test_known_divergence_json_null_context_hash_is_legacy_here(tmp_path):
    """DELIBERATE DIVERGENCE, owner-ratified.

    The oracle coerces the value (`rec.get("context_hash") or ""`), so a JSON
    `null` reads as the explicit-empty state there and never suppresses. skodun
    treats absent and `null` alike as "no context recorded", because a legacy
    import merges a foreign row and artifact verbatim and either spelling can
    reach the store. The oracle's own writers only ever emit an absent key or a
    string (`grok-prepush-review.sh:2719-2721, 3261`), so no record in the
    oracle's corpus exercises this: the coercion is incidental there, the rule
    is deliberate here.
    """
    record = dict(NULL, diff_hash="d1", parse_ok=True)
    assert _oracle_suppresses(tmp_path / "o", record, "d1", HASH_A) is False
    assert context_permits_suppression(NULL, HASH_A) is True


@requires_oracle
def test_known_divergence_empty_string_never_suppresses_here(tmp_path):
    """DELIBERATE DIVERGENCE, owner-ratified (fail-closed).

    Called WITHOUT the probe — the oracle's kill-switch path, which passes an
    empty context hash directly — the oracle suppresses on an explicit-empty
    record. skodun never does: the shipped pipeline writes `""` both when
    packing was disabled and when nothing was packed, and Task 8's batched
    aggregate writes it by construction, so the value cannot distinguish
    "context compared" from "context unknown".
    """
    work = tmp_path / "o"
    work.mkdir(parents=True, exist_ok=True)
    index = work / "index.jsonl"
    index.write_text(json.dumps(dict(EMPTY, diff_hash="d1", parse_ok=True)) + "\n",
                     encoding="utf-8")
    kill_switch = subprocess.run(
        ["sh", str(ORACLE), "--dedup-check", str(index), "d1", ""],
        capture_output=True, cwd=work).returncode
    assert kill_switch == 0, "the oracle's kill-switch path no longer suppresses"
    assert context_permits_suppression(EMPTY, None) is False
