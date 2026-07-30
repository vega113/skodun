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
from tests.test_pipeline import (CFG, CLEAN, DIRTY, _calls, _emit, _fake_grok,
                                 _per_call, _repo, _run, _store)

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
    # Never inherited: an ambient bypass in the developer's shell would turn
    # every dispatcher test below into a no-op that still passed.
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
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


# ===========================================================================
# TASK 10: the dispatcher, the worker and the shim
# ===========================================================================
#
# Everything below drives real processes and real stores. The one thing that is
# NOT allowed to be real is the developer's machine: `_hermetic_git` pins
# `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` inside `tmp_path` for every hook test,
# because a global `core.hooksPath` (which some setups genuinely have) would
# otherwise make `install-hooks` write outside the sandbox -- and the test would
# pass while installing a hook into the developer's real repositories.

import os
import signal
import stat
import sys
import threading
import time
from types import SimpleNamespace

from skodun import cli, gitio, pipeline, store as store_mod
from skodun.config import Defaults, Dispatch, load_config
from skodun.dispatch import (
    BACKUP_SUFFIX,
    SHIM_MARKER,
    HookRefused,
    Ref,
    bypass_reason,
    effective_defaults,
    failed_record,
    hooks_dir,
    install_hooks,
    parse_ref_lines,
    pid_is_skodun_worker,
    reservation_defaults,
    reserved_budget,
    run_dispatch,
    run_worker,
    shim_text,
    signal_superseded,
    worker_env,
)
from skodun.store import Store

_SRC = str(Path(gitio.__file__).resolve().parents[1])
ZERO = "0" * 40


@pytest.fixture
def hermetic_git(tmp_path, monkeypatch):
    """Git with NO ambient global/system config.

    Not tidiness: this machine's global config may carry `core.hooksPath`, and
    `install_hooks` correctly honours it -- which would write a real pre-push hook
    into a real hooks directory outside `tmp_path`.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitsystem"))
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    (tmp_path / "gitsystem").write_text("", encoding="utf-8")


# --------------------------------------------------------------------------
# ref parsing: the pure stage
# --------------------------------------------------------------------------


def test_an_updated_branch_is_the_only_actionable_ref():
    refs = parse_ref_lines(f"refs/heads/feat aaa refs/heads/feat bbb\n")
    assert [r.actionable for r in refs] == [True]
    assert refs[0].branch == "feat", "the SHORT name is what downstream keys on"
    assert refs[0].remote_ref == "refs/heads/feat" and refs[0].remote_oid == "bbb"


def test_a_nested_branch_name_keeps_every_slash_after_refs_heads():
    (ref,) = parse_ref_lines(f"refs/heads/user/x/y aaa refs/heads/user/x/y {ZERO}")
    assert ref.branch == "user/x/y"


@pytest.mark.parametrize("line, why", [
    (f"(delete) {ZERO} refs/heads/gone bbb", "deletion"),
    (f"refs/heads/gone {ZERO} refs/heads/gone bbb", "deletion"),
    ("refs/tags/v1 aaa refs/tags/v1 bbb", "not a branch"),
    ("refs/notes/x aaa refs/notes/x bbb", "not a branch"),
    ("HEAD aaa refs/heads/main bbb", "not a branch"),
    ("refs/heads/ aaa refs/heads/ bbb", "not a branch"),
    ("two fields", "malformed"),
])
def test_everything_else_is_skipped_with_a_reason_and_never_a_record(line, why):
    """Non-actionable refs get a reason and NO record.

    The reason is what `run_dispatch` puts on stderr, and "never a record" is the
    half that matters: a `failed` record for a deleted branch or a pushed tag
    would be a permanent untrustworthy row on a branch nobody is reviewing, and
    the gate reads the newest row per branch.
    """
    (ref,) = parse_ref_lines(line)
    assert ref.actionable is False
    assert ref.branch == ""
    assert ref.skip_reason, "a skipped ref must say why"


def test_a_deletion_of_a_tag_reads_as_a_deletion():
    """Both classifications are true of it; the ORDER is fixed here, not left to
    reading order (the oracle skips the zero oid first, 4677)."""
    (ref,) = parse_ref_lines(f"refs/tags/v1 {ZERO} refs/tags/v1 bbb")
    assert "deletion" in ref.skip_reason


def test_a_sha256_null_oid_is_still_a_deletion():
    """Matched by SHAPE, not against a 40-character literal: a sha256
    repository's null oid is 64 characters, and a length-pinned comparison would
    classify a deletion as an update and review a branch that is being removed."""
    (ref,) = parse_ref_lines(f"refs/heads/gone {'0' * 64} refs/heads/gone bbb")
    assert ref.actionable is False and "deletion" in ref.skip_reason


def test_blank_lines_are_dropped_rather_than_reported():
    assert parse_ref_lines("\n\n  \n") == []


def test_parsing_is_pure_and_keeps_input_order():
    text = ("refs/heads/a 1 refs/heads/a 2\n"
            f"refs/tags/t 3 refs/tags/t 4\n"
            "refs/heads/b 5 refs/heads/b 6\n")
    assert [r.branch for r in parse_ref_lines(text)] == ["a", "", "b"]


def test_extra_fields_on_a_line_are_ignored_not_refused():
    """Only the first four fields are the protocol. A fifth would be a future
    git; refusing the line would stop reviewing pushes on that git entirely."""
    (ref,) = parse_ref_lines("refs/heads/f aaa refs/heads/f bbb extra")
    assert ref.actionable and ref.branch == "f"


# --------------------------------------------------------------------------
# the bypasses: BEFORE the config, on purpose
# --------------------------------------------------------------------------


def _broken_config_repo(tmp_path) -> Path:
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text("this is not = = toml\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    return repo


def _push_line(repo: Path, branch: str = "feat", remote_oid: str = "") -> str:
    oid = _git(repo, "rev-parse", branch)
    remote = remote_oid or ZERO
    return f"refs/heads/{branch} {oid} refs/heads/{branch} {remote}\n"


def _rows(db: Path) -> list[dict]:
    if not db.exists():
        return []
    with Store.open(db) as st:
        return list(st.list_reviews(None, limit=-1))


def test_the_env_bypass_disables_dispatch_even_against_a_broken_config(
        tmp_path, monkeypatch, capsys):
    """The whole reason the bypass is checked BEFORE the config load.

    A project whose `.skodun.toml` will not parse must still be pushable, and a
    bypass that needed a working config to be read would be exactly unavailable in
    the situation it exists for.
    """
    repo = _broken_config_repo(tmp_path)
    monkeypatch.setenv("SKODUN_PREPUSH_SKIP", "1")
    db = tmp_path / "s.db"
    assert run_dispatch(_push_line(repo), repo, db) == 0
    assert not db.exists(), "a bypassed push must not even create a store"
    assert "disabled" in capsys.readouterr().err


def test_the_git_config_bypass_disables_dispatch_even_against_a_broken_config(
        tmp_path, monkeypatch, capsys):
    repo = _broken_config_repo(tmp_path)
    _git(repo, "config", "skodun.prepush", "false")
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
    db = tmp_path / "s.db"
    assert run_dispatch(_push_line(repo), repo, db) == 0
    assert not db.exists()
    assert "disabled" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_only_the_literal_1_is_the_env_bypass(tmp_path, monkeypatch, value):
    repo = _mkrepo(tmp_path)
    monkeypatch.setenv("SKODUN_PREPUSH_SKIP", value)
    assert bypass_reason(repo) is None


def test_only_the_literal_false_is_the_git_config_bypass(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
    assert bypass_reason(repo) is None
    _git(repo, "config", "skodun.prepush", "true")
    assert bypass_reason(repo) is None
    _git(repo, "config", "skodun.prepush", "false")
    assert bypass_reason(repo) is not None


def test_an_unreadable_git_config_is_not_a_bypass(tmp_path, monkeypatch):
    """A git that will not run is a problem the dispatch below reports on its own
    terms -- never a reason to silently stop reviewing."""
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
    assert bypass_reason(tmp_path / "not-a-repo-at-all") is None


def test_both_disablements_are_reported_in_the_same_stderr_shape(
        tmp_path, monkeypatch, capsys):
    """`[dispatch] enabled = false` and `git config skodun.prepush false` are
    parallel switches; a reader tailing a push's stderr must not have to know
    which layer turned review off to recognise that it is off."""
    repo = _repo(tmp_path)
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c1")
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)

    _git(repo, "config", "skodun.prepush", "false")
    run_dispatch(_push_line(repo), repo, tmp_path / "a.db")
    by_git = capsys.readouterr().err

    _git(repo, "config", "--unset", "skodun.prepush")
    (repo / ".skodun.toml").write_text(
        CFG + "\n[dispatch]\nenabled = false\n", encoding="utf-8")
    run_dispatch(_push_line(repo), repo, tmp_path / "b.db")
    by_config = capsys.readouterr().err

    for text in (by_git, by_config):
        assert "pre-push review disabled" in text
        assert "1 ref(s) discarded" in text
        assert "the push is not blocked" in text


def test_dispatch_disabled_by_config_writes_no_record_and_starts_no_worker(
        tmp_path, monkeypatch, capsys):
    """The named mutation is "ignore `Dispatch.enabled`": this test is what dies.

    `enabled = false` is a kill switch, so nothing downstream of it may happen --
    not the capture, not the reservation, not the worker, and above all not a
    record. A record would make the branch look reviewed-and-failed forever.
    """
    repo = _repo(tmp_path, "\n[dispatch]\nenabled = false\n")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c1")
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
    calls = []
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: calls.append(a))
    db = tmp_path / "s.db"
    assert run_dispatch(_push_line(repo), repo, db) == 0
    assert calls == [], "a disabled dispatch started a worker"
    assert _rows(db) == [], "a disabled dispatch left a record behind"
    assert "[dispatch] enabled = false" in capsys.readouterr().err


# --------------------------------------------------------------------------
# durable failure records
# --------------------------------------------------------------------------


def test_a_config_failure_writes_one_record_per_ACTIONABLE_ref_only(tmp_path,
                                                                    capsys):
    """The named mutation is "write config-failure records for tags too".

    A push of one branch, one tag and one deletion against a broken config must
    leave exactly ONE record: the branch's. A record keyed on a tag or a deleted
    branch is a permanent untrustworthy row on something nobody reviews, and the
    gate reads the newest row per branch.
    """
    repo = _broken_config_repo(tmp_path)
    _git(repo, "tag", "v1")
    stdin = (_push_line(repo, "feat")
             + f"refs/tags/v1 {_git(repo, 'rev-parse', 'v1')} refs/tags/v1 {ZERO}\n"
             + f"(delete) {ZERO} refs/heads/old aaa\n")
    db = tmp_path / "s.db"
    assert run_dispatch(stdin, repo, db) == 0
    rows = _rows(db)
    assert [r["branch"] for r in rows] == ["feat"]
    row = rows[0]
    assert row["mode"] == "prepush" and row["status"] == "failed"
    assert row["diff_hash"] == "", "no identity was computed, so none is claimed"
    assert row["parse_ok"] is False and row["trustworthy"] is False
    assert row["usable_output"] is False
    assert "config could not be loaded" in row["failure_reason"]
    assert row["head"] == _git(repo, "rev-parse", "feat")


def test_a_config_failure_record_is_shaped_for_the_gate_and_the_delivery(tmp_path):
    """Branch-shaped and fully populated, because Task 12 delivers what the store
    says and the gate reads the newest row per branch. A record missing
    `findings`/`severity`/`summary` is a record whose readers have to guess."""
    rec = failed_record("feat", "why", head="a" * 40)
    for key in ("findings", "findings_total", "severity", "summary",
                "files_changed", "attempts", "extra_passes", "rule_ids"):
        assert key in rec, key
    assert rec["mode"] == "prepush" and rec["source"] == "skodun"
    assert rec["diff_hash"] == "" and rec["worst_runtime_sec"] is None
    assert rec["superseded_by"] is None


def test_a_config_failure_record_survives_the_store_chokepoint(tmp_path):
    """It has to be persistable AS IS: a shape the store refuses is a failure
    nobody ever sees."""
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(failed_record("feat", "boom"))
        (row,) = st.list_reviews("feat")
    assert row["trustworthy"] is False and row["status"] == "failed"


def test_an_unopenable_store_leaves_a_stderr_line_and_still_exits_0(tmp_path,
                                                                    capsys):
    """The ONE dispatch failure with nowhere to record itself. It must still not
    block the push."""
    repo = _mkrepo(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file\n", encoding="utf-8")
    assert run_dispatch(_push_line(repo, "main"), repo, blocked / "s.db") == 0
    assert "could not open the review store" in capsys.readouterr().err


# --------------------------------------------------------------------------
# the reservation budget: max, never the bare foreground cap
# --------------------------------------------------------------------------


def test_the_reservation_budget_takes_the_LARGER_timeout(tmp_path):
    """The regression the brief names: a config whose BACKGROUND cap is above the
    foreground one. Taking "the foreground cap" literally would under-budget the
    reservation, and an undersized stale ceiling reclaims a LIVE worker and
    publishes `failed` over a review that is still running."""
    d = Defaults(timeout_sec=100)
    assert reservation_defaults(d, Dispatch(timeout_sec=900)).timeout_sec == 900
    assert reservation_defaults(d, Dispatch(timeout_sec=10)).timeout_sec == 100


def test_the_reservation_budget_takes_the_LARGER_retry_count(tmp_path):
    """`budget.attempt_budget` multiplies by `1 + timeout_retries +
    degraded_retries`, so more BACKGROUND retries than foreground ones would
    under-budget by exactly the retries they added -- the same undersized-ceiling
    failure through the other factor."""
    d = Defaults(timeout_retries=0)
    assert reservation_defaults(d, Dispatch(timeout_retries=4)).timeout_retries == 4
    d2 = Defaults(timeout_retries=3)
    assert reservation_defaults(d2, Dispatch(timeout_retries=0)).timeout_retries == 3


def test_the_worker_budget_is_the_background_one_exactly(tmp_path):
    """No max here: the whole point of `[dispatch]` is a TIGHTER cap for a run
    nobody is waiting on, and maxing would silently ignore the tighter setting."""
    d = Defaults(timeout_sec=420, timeout_retries=2)
    e = effective_defaults(d, Dispatch(timeout_sec=240, timeout_retries=0))
    assert (e.timeout_sec, e.timeout_retries) == (240, 0)
    assert e.max_diff_bytes == d.max_diff_bytes, "every other key is untouched"


def test_a_multi_batch_push_reserves_a_multi_batch_budget(tmp_path):
    """`recover_stale` reads the PERSISTED `worst_runtime_sec`, so a batched run
    whose reservation was sized for one review would be swept mid-batch-three."""
    from skodun import budget
    from skodun.config import Config, Reviewer
    d = Defaults(max_diff_bytes=200, context_pack=False)
    cfg = Config(defaults=d, reviewers=(
        Reviewer(name="f", provider="xai", model="m", role="finder"),))
    small = reserved_budget(cfg, b"diff --git a/a b/a\n@@ -1 +1 @@\n-a\n+b\n")
    big = reserved_budget(cfg, b"".join(
        b"diff --git a/f%d b/f%d\n@@ -1 +1 @@\n-a\n+b\n" % (i, i)
        for i in range(40)))
    assert big > small
    rd = reservation_defaults(d, cfg.dispatch)
    assert small == budget.worst_runtime(rd, 1, 0)


# --------------------------------------------------------------------------
# end-to-end dispatch: reservation, spawn, attach, supersede
# --------------------------------------------------------------------------


def _bg_repo(tmp_path, extra_cfg: str = "", body: str | None = None) -> Path:
    """A repo with a COMMITTED `feat` branch and a fake grok CLI.

    Committed, because a pre-push ref diff is between two OIDS -- the working
    tree does not participate at all, which is the whole point of
    `capture_ref_diff`.
    """
    _fake_grok(tmp_path, body if body is not None else _emit(CLEAN))
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG + extra_cfg, encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "cfg")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\nthree\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c1")
    return repo


@pytest.fixture
def spawned(monkeypatch):
    """Every worker `run_dispatch` starts, so the test can REAP it.

    Not optional bookkeeping: a detached worker outlives the test that started it,
    and a suite that leaked one per dispatch would accumulate model calls against
    a store pytest is about to delete.
    """
    from skodun import dispatch as dispatch_mod
    real = dispatch_mod.spawn_worker
    procs = []

    def spy(*a, **kw):
        proc = real(*a, **kw)
        procs.append(proc)
        return proc

    monkeypatch.setattr(dispatch_mod, "spawn_worker", spy)
    yield procs
    for proc in procs:
        try:
            proc.wait(timeout=120)
        except Exception:               # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=30)


def _await(db: Path, review_id: str, *, timeout: float = 120.0) -> dict:
    """The record once it is no longer `running`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Store.open(db) as st:
            rec = st.get_review(review_id)
        if rec is not None and rec.get("status") != "running":
            return rec
        time.sleep(0.05)
    raise AssertionError(f"{review_id} never left `running`")


def _ids(db: Path) -> list[str]:
    return [r["id"] for r in _rows(db)]


def test_the_record_exists_before_the_worker_process_does(tmp_path, monkeypatch):
    """The named mutation is "spawn before reserving": this test is what dies.

    A worker that started before its record existed could finalize nothing, and a
    racing second dispatch would have nothing to supersede -- both pushes would
    review, and the LOSER's answer would be the one left standing if it committed
    second.
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    seen = {}

    def spy(store, record_id, *a, **kw):
        with Store.open(db) as st:
            seen["at_spawn"] = st.get_review(record_id)
        raise OSError("not actually spawning anything")

    monkeypatch.setattr("skodun.dispatch.spawn_worker", spy)
    assert run_dispatch(_push_line(repo), repo, db) == 0
    rec = seen["at_spawn"]
    assert rec is not None, "the worker was spawned before its record existed"
    assert rec["status"] == "running" and rec["mode"] == "prepush"
    assert rec["pid"] is None, "the pid is attached AFTER the spawn, not before"
    assert rec["worst_runtime_sec"] and rec["worst_runtime_sec"] > 0


def test_a_reserved_record_carries_the_remote_ref_as_its_base(tmp_path,
                                                              monkeypatch):
    """An EXISTING remote branch's base is the remote oid, and the persisted
    `base_ref` is the remote ref STRING as pushed -- the name a human reading the
    record needs to see the review's scope."""
    repo = _bg_repo(tmp_path)
    base_oid = _git(repo, "rev-parse", "main")
    db = tmp_path / "s.db"
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no spawn")))
    run_dispatch(_push_line(repo, "feat", remote_oid=base_oid), repo, db)
    (row,) = _rows(db)
    assert row["base_sha"] == base_oid
    assert row["base_ref"] == "refs/heads/feat"


def test_a_brand_new_branch_resolves_its_base_from_the_main_candidates(
        tmp_path, monkeypatch):
    """A zero remote oid means there is no remote side to compare against, so
    Task 5's `resolve_ref_base` picks the main candidate with a merge-base."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no spawn")))
    run_dispatch(_push_line(repo, "feat"), repo, db)
    (row,) = _rows(db)
    assert row["base_ref"] == "main"
    assert row["base_sha"] == _git(repo, "rev-parse", "main")


def test_a_ref_with_nothing_outgoing_is_skipped_with_no_record(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """ORACLE PARITY (`[ -z "$DIFF" ] && continue`, 4690). An empty prompt could
    mint a clean verdict for a diff nothing looked at, and the gate PASSes an
    empty change before it ever looks a review up."""
    repo = _bg_repo(tmp_path)
    head = _git(repo, "rev-parse", "feat")
    db = tmp_path / "s.db"
    calls = []
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: calls.append(a))
    # remote already has exactly what is being pushed
    assert run_dispatch(f"refs/heads/feat {head} refs/heads/feat {head}\n",
                        repo, db) == 0
    assert calls == [] and _rows(db) == []
    assert "nothing outgoing" in capsys.readouterr().err


def test_a_clean_background_review_is_reserved_reviewed_and_finalized(
        tmp_path, spawned, capsys):
    """The whole path, with a real detached worker and a real fake provider."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    assert run_dispatch(_push_line(repo), repo, db) == 0
    (rid,) = _ids(db)
    rec = _await(db, rid)
    assert rec["status"] == "clean", rec.get("failure_reason")
    assert rec["trustworthy"] is True
    assert rec["usable_output"] is True
    assert rec["mode"] == "prepush" and rec["branch"] == "feat"
    assert rec["head"] == _git(repo, "rev-parse", "feat")
    assert rec["pid"], "the dispatcher's pid attach did not survive the finalize"
    assert _calls(tmp_path) == 1


def test_the_worker_stderr_lands_in_the_stores_log_directory(tmp_path, spawned):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    run_dispatch(_push_line(repo), repo, db)
    (rid,) = _ids(db)
    _await(db, rid)
    log = Path(str(db) + ".logs") / f"{rid}.log"
    assert log.exists(), "a detached worker's stderr went nowhere"
    assert "reviewing" in log.read_text(encoding="utf-8", errors="replace")


def test_a_spawn_failure_demotes_the_reservation_and_still_exits_0(
        tmp_path, monkeypatch, capsys):
    """A push must never be blocked by review machinery -- and the failure must be
    DURABLE, because Task 12 delivers what the store says."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    monkeypatch.setattr(
        "skodun.dispatch.spawn_worker",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no exec for you")))
    assert run_dispatch(_push_line(repo), repo, db) == 0
    (row,) = _rows(db)
    assert row["status"] == "failed" and row["trustworthy"] is False
    assert row["parse_ok"] is False
    assert "could not be started" in row["failure_reason"]
    assert "no exec for you" in row["failure_reason"]


def test_a_dispatch_failure_on_one_ref_does_not_cost_the_others_their_review(
        tmp_path, monkeypatch):
    """Per-REF guarding: a multi-ref push with one broken branch."""
    repo = _bg_repo(tmp_path)
    _git(repo, "checkout", "-b", "other")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c2")
    db = tmp_path / "s.db"
    real = gitio.capture_ref_diff

    def boom(repo_, base_sha, local_oid):
        diff = real(repo_, base_sha, local_oid)
        if any(f == "c.txt" for f in diff.files):
            raise gitio.GitError("git fell over on this ref")
        return diff

    monkeypatch.setattr(gitio, "capture_ref_diff", boom)
    monkeypatch.setattr("skodun.dispatch.gitio", gitio, raising=False)
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("stop")))
    stdin = _push_line(repo, "feat") + _push_line(repo, "other")
    assert run_dispatch(stdin, repo, db) == 0
    by_branch = {r["branch"]: r for r in _rows(db)}
    assert set(by_branch) == {"feat", "other"}
    assert "could not be dispatched" in by_branch["other"]["failure_reason"]
    assert "could not be started" in by_branch["feat"]["failure_reason"]


# --------------------------------------------------------------------------
# the race: two pushes, one surviving review
# --------------------------------------------------------------------------


def test_a_zero_delay_double_dispatch_leaves_exactly_one_reviewed_record(
        tmp_path, spawned):
    """THE race this whole design exists to close.

    Two pushes back to back. Both reserve, the second's transaction supersedes the
    first's row, and both workers run to completion -- but only one record is a
    review: the loser's `finalize_review` is refused because its reservation is no
    longer `running`, so its answer is discarded rather than published over the
    newer one.
    """
    repo = _bg_repo(tmp_path, body="sleep 0.7\n" + _emit(CLEAN))
    db = tmp_path / "s.db"
    first = _push_line(repo)
    assert run_dispatch(first, repo, db) == 0
    # A second push of NEW content on the same branch, with no delay.
    (repo / "a.txt").write_text("two\nthree\nfour\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c2")
    assert run_dispatch(_push_line(repo), repo, db) == 0

    ids = _ids(db)
    assert len(ids) == 2, ids
    for rid in ids:
        _await(db, rid)
    rows = {r["id"]: r for r in _rows(db)}
    superseded = [r for r in rows.values() if r["status"] == "superseded"]
    reviewed = [r for r in rows.values() if r["status"] == "clean"]
    assert len(superseded) == 1 and len(reviewed) == 1, rows
    assert superseded[0]["superseded_by"] == reviewed[0]["id"]
    assert superseded[0]["trustworthy"] is False
    assert reviewed[0]["trustworthy"] is True


def test_racing_dispatchers_serialize_into_one_running_row(tmp_path, monkeypatch):
    """SQLite's write lock is what serialises them, so this drives REAL threads.

    Whichever reservation commits second supersedes the first's row, so after four
    concurrent dispatches of the same branch there is exactly ONE `running` prepush
    row and three `superseded` ones -- never two live reviews on one inference
    backend.

    The store is FRESH, which is the other half of the test: two openers of a
    brand-new store both migrate it, and `_apply_atomic` re-reads `user_version`
    under the write lock so the loser is a no-op rather than a `duplicate column
    name` failure. Without that, `Store.open` RAISES for the loser -- and no store
    means nowhere to record the failure, so that push gets no record at all.
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    # A stand-in for the spawn that neither starts a process nor demotes the row,
    # so the SUPERSEDE chain is what the assertions see. It answers the full
    # `Popen` surface the dispatcher uses, because a racing dispatcher WILL lose
    # its attach and terminate this "child".
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: _StubProc())
    line = _push_line(repo)
    ready = threading.Barrier(4)
    errors = []

    def push():
        try:
            ready.wait(timeout=30)
            run_dispatch(line, repo, db)
        except BaseException as e:       # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=push) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == [], errors
    rows = _rows(db)
    assert len(rows) == 4, rows
    running = [r for r in rows if r["status"] == "running"]
    superseded = [r for r in rows if r["status"] == "superseded"]
    assert len(running) == 1, [r["status"] for r in rows]
    assert len(superseded) == 3, [r["status"] for r in rows]
    assert all(r["superseded_by"] for r in superseded)
    assert all(r["trustworthy"] is False for r in rows)


def test_a_fresh_store_survives_concurrent_openers(tmp_path):
    """The migration race in isolation, because its failure mode is the worst one
    available: `Store.open` raises for the loser, and no store means the push gets
    no record at all -- not even a failed one."""
    db = tmp_path / "fresh.db"
    ready = threading.Barrier(6)
    errors = []

    def opener():
        try:
            ready.wait(timeout=30)
            with Store.open(db) as st:
                st.save_review(failed_record("feat", "just proving it works"))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == [], errors
    assert len(_rows(db)) == 6


def test_a_suppressed_push_never_touches_an_in_flight_review(tmp_path,
                                                             monkeypatch):
    """ORACLE ordering (4745): the dedup decision happens BEFORE the supersede so
    a skip never TERMs an in-flight review of DIFFERENT content on this branch."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    spawns = []
    monkeypatch.setattr("skodun.dispatch.spawn_worker",
                        lambda *a, **k: spawns.append(a) or
                        (_ for _ in ()).throw(OSError("no spawn")))
    # A real review of this exact diff, finalized clean.
    run_dispatch(_push_line(repo), repo, db)
    (rid,) = _ids(db)
    with Store.open(db) as st:
        reserved = st.get_review(rid)
        rec = dict(reserved, status="clean", parse_ok=True, degraded=False,
                   diff_truncated=False, usable_output=True, summary="ok",
                   findings=[], findings_total=0, failure_reason="",
                   context_hash="")
        # `context_hash=""` never suppresses, so give it the candidate's hash.
        rec["context_hash"] = _candidate_hash(repo, tmp_path)
        st.save_review(dict(rec, status="clean"))
        # A DIFFERENT in-flight prepush review of the same branch.
        other = st.reserve_prepush("feat", "f" * 40, "main", "b" * 40,
                                   "otherhash", 100, _evidence())
    before = len(spawns)
    run_dispatch(_push_line(repo), repo, db)
    with Store.open(db) as st:
        still = st.get_review(other.record_id)
        events = st._c.execute("SELECT * FROM dedup_events").fetchall()
    assert len(spawns) == before, "a suppressed push started a worker"
    assert len(events) == 1, "a suppression must leave its audit row"
    assert still["status"] == "running", "a suppression superseded a live review"


def _candidate_hash(repo: Path, tmp_path: Path) -> str:
    """The context hash a review of the pushed `feat` tip would record."""
    cfg = load_config(repo)
    base = gitio.resolve_ref_base(repo, _git(repo, "rev-parse", "feat"))
    diff = gitio.capture_ref_diff(repo, base.sha, _git(repo, "rev-parse", "feat"))
    ev = build_dedup_evidence(_NoStore(), repo, diff,
                              _git(repo, "rev-parse", "feat"), cfg.defaults, True)
    assert ev.valid
    return ev.candidate_context_hash


class _NoStore:
    def __getattr__(self, name):        # pragma: no cover - guard, never called
        raise AssertionError(f"the evidence builder touched the store: {name}")


def _evidence(enabled=True, valid=True, candidate=None):
    return DedupEvidence(enabled=enabled, valid=valid,
                         candidate_context_hash=candidate)


# --------------------------------------------------------------------------
# the worker: identity, conditional finalize, cancellation
# --------------------------------------------------------------------------


def _reserve(db: Path, repo: Path, branch: str = "feat") -> tuple[str, dict]:
    """Reserve a record for `branch`'s tip exactly as the dispatcher would."""
    head = _git(repo, "rev-parse", branch)
    base = gitio.resolve_ref_base(repo, head)
    diff = gitio.capture_ref_diff(repo, base.sha, head)
    with Store.open(db) as st:
        res = st.reserve_prepush(branch, head, base.ref, base.sha,
                                gitio.diff_identity(diff.data),
                                reserved_budget(load_config(repo), diff.data),
                                _evidence(valid=False))
    return res.record_id, {"head": head, "base_ref": base.ref,
                           "base_sha": base.sha}


def test_the_worker_reviews_its_reservation_and_finalizes_it(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert rec["usable_output"] is True
    assert rec["id"] == rid and rec["branch"] == "feat"


def test_the_worker_preserves_every_reservation_owned_field(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    with Store.open(db) as st:
        reserved = st.get_review(rid)
        assert st.attach_pid(rid, os.getpid())
    run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
               ident["base_ref"], db)
    with Store.open(db) as st:
        final = st.get_review(rid)
    for key in ("id", "branch", "head", "base_ref", "base_sha", "diff_hash",
                "mode", "worst_runtime_sec"):
        assert final[key] == reserved[key], key
    assert final["pid"] == os.getpid(), "the DATABASE-owned pid was erased"
    assert final["reviewed_at"] == reserved["reviewed_at"], (
        "reviewed_at is the reservation's: it orders the dedup candidate query "
        "and the supersede, and must not jump forward by the review's duration")


def test_the_worker_records_a_mismatch_when_the_pushed_content_moved(tmp_path):
    """The named mutation is "drop the identity re-check in the worker".

    The reserved `diff_hash` is what dedup and the gate match on, so a worker
    whose OWN capture of the same two oids hashes differently must not publish
    under it -- it would certify a diff nobody reviewed.

    Both endpoints are immutable oids, so the trigger is not the oids moving: it
    is the repository's DIFF RENDERING changing under them. `.gitattributes` is
    read from the WORKING TREE, so a `-diff` attribute arriving between the
    reservation and the worker turns the same two blobs into "Binary files
    differ" -- different bytes, different hash, same oids. `git gc --prune=now`
    on an unreachable pushed oid is the other real one, and it lands on the
    generic failure path (the capture itself raises).
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    (repo / ".gitattributes").write_text("a.txt -diff\n", encoding="utf-8")
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "moved under this review" in rec["failure_reason"]
    assert _calls(tmp_path) == 0, "a moved push must not spend a model call"


def test_the_worker_refuses_an_identity_the_reservation_does_not_claim(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    out = run_worker(rid, repo, "not-the-branch", ident["head"],
                     ident["base_sha"], ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed"
    assert "identity the reservation does not claim" in rec["failure_reason"]
    assert _calls(tmp_path) == 0


def test_a_worker_whose_reservation_was_superseded_changes_nothing(tmp_path):
    """The named mutation is "make `finalize_review` unconditional".

    A late worker's answer is about content a newer push replaced. Publishing it
    over the newer record would resurrect a review of the old diff at a row the
    gate reads as current.
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    with Store.open(db) as st:
        newer = st.reserve_prepush("feat", "f" * 40, "main", "b" * 40, "h2",
                                   100, _evidence(valid=False))
        assert st.get_review(rid)["status"] == "superseded"
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "superseded", "a late worker overwrote a retired record"
    assert rec["superseded_by"] == newer.record_id
    assert rec["trustworthy"] is False


def test_a_worker_whose_reservation_was_stale_recovered_changes_nothing(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    with Store.open(db) as st:
        assert st.fail_if_running(rid, "stale recovery: worker exceeded its "
                                       "runtime budget")
    run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
               ident["base_ref"], db)
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed" and "stale recovery" in rec["failure_reason"]
    assert _calls(tmp_path) == 0, "a recovered reservation must not be reviewed"


def test_a_worker_with_no_reservation_at_all_reports_2_and_writes_nothing(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    Store.open(db).close()
    out = run_worker("sk_nope", repo, "feat", "a" * 40, "b" * 40, "main", db)
    assert out.code == 2 and _rows(db) == []


def test_a_worker_whose_config_will_not_load_demotes_its_reservation(tmp_path):
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    (repo / ".skodun.toml").write_text("= not toml =\n", encoding="utf-8")
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "background review failed" in rec["failure_reason"]


# --------------------------------------------------------------------------
# the SIGTERM -> cancellation-token cascade
# --------------------------------------------------------------------------
#
# Every barrier below injects the signal at a DIFFERENT point, because the
# windows fail differently and each one has its own guard:
#
#   after parse, before return   -> `run_prepush_review`'s last checkpoint
#   after return, before finalize -> the worker's pre-finalize check
#   during the finalize call      -> the worker's POST-COMMIT check
#
# Every one of them must end with `trustworthy=False`, gate 2, and no dedup
# candidate. A cancelled review that stored clean axes would certify content the
# model never finished looking at.


def _gate_would_pass(db: Path, repo: Path) -> bool:
    """Whether a trustworthy terminal review of the pushed tip exists at all.

    `latest_trustworthy_for` is the exact query `gate.run_gate` makes (gate.py:112),
    so this is the gate's own requirement rather than a re-statement of it -- it
    just does not need a matching working tree to ask.
    """
    head = _git(repo, "rev-parse", "feat")
    base = gitio.resolve_ref_base(repo, head)
    diff = gitio.capture_ref_diff(repo, base.sha, head)
    with Store.open(db) as st:
        rec = st.latest_trustworthy_for(gitio.diff_identity(diff.data))
    return bool(rec) and rec.get("trustworthy") is True


def _assert_cancelled_shape(db: Path, repo: Path, rid: str) -> dict:
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed", rec
    assert rec["trustworthy"] is False
    assert rec["degraded"] is True, "the DEGRADED axis is what removes trust"
    assert "cancelled" in (rec["degraded_reason"] or "")
    assert type(rec["degraded"]) is bool and type(rec["parse_ok"]) is bool
    assert not _gate_would_pass(db, repo), "a cancelled review satisfied the gate"
    with Store.open(db) as st:
        # And it is not a dedup CANDIDATE either: the reservation transaction only
        # ever suppresses against a trustworthy terminal record.
        assert st.latest_trustworthy_for(rec["diff_hash"]) is None
    return rec


def test_a_cancellation_after_the_review_preserves_findings_and_demotes(tmp_path,
                                                                        monkeypatch):
    """The named mutation is "omit the demotion" (status/reason only).

    `finalize_review` recomputes `trustworthy` from the three axes ALONE, so
    moving only `status` and `failure_reason` would store a cancelled round as a
    TRUSTWORTHY one -- it would satisfy the gate and suppress the next push's
    review. The findings and `usable_output` are kept: the round really did
    produce them, and "NO REVIEW HAPPENED" over real evidence is its own failure.
    """
    repo = _bg_repo(tmp_path, body=_emit(DIRTY))
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    real = pipeline.run_prepush_review

    def signal_after_parse(*a, **kw):
        rec = real(*a, **kw)
        kw["cancel"].set()          # after parse, before the worker sees it
        return rec

    monkeypatch.setattr(pipeline, "run_prepush_review", signal_after_parse)
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    rec = _assert_cancelled_shape(db, repo, rid)
    # The BOUNDARY, not merely "cancelled": it is what distinguishes the
    # pre-finalize barrier from the post-commit one, and it is the diagnostic an
    # operator reading a failed record actually needs -- "when did this stop".
    # Asserting only "cancelled" would let the post-commit check (a different
    # window, a different guard) silently stand in for this one.
    assert "before it was recorded" in rec["degraded_reason"], rec["degraded_reason"]
    assert "during finalization" not in rec["degraded_reason"]
    assert rec["findings_total"] == 1, "the cancellation threw real findings away"
    assert rec["usable_output"] is True, (
        "the round DID produce a parseable answer; a surface judging by the "
        "finding count would print NO REVIEW HAPPENED over it")


def test_a_cancellation_inside_the_pipeline_still_lands_as_untrustworthy(
        tmp_path, monkeypatch):
    """The token set at a pass boundary: `ReviewCancelled` travels out of the
    pipeline carrying the partial, and the worker converts it to an orderly
    conditional failed finalize -- never a traceback out of a detached process."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    real = pipeline._single_shot

    def cancel_before_the_reviewer(common, diff, **kw):
        kw["cancel"].set()
        return real(common, diff, **kw)

    monkeypatch.setattr(pipeline, "_single_shot", cancel_before_the_reviewer)
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    assert "Traceback" not in out.message
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert _calls(tmp_path) == 0, "a cancelled run still invoked the provider"


def test_a_token_set_DURING_the_finalize_call_demotes_the_committed_record(
        tmp_path, monkeypatch):
    """The named mutation is "remove the post-commit check": this test is what dies.

    The pre-finalize barrier injects BEFORE the store call and therefore cannot
    see a signal that arrives while SQLite holds the write lock. Without the
    post-commit check the review commits TRUSTWORTHY and the signal is lost.
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    holder = {}
    real = store_mod.Store.finalize_review

    def finalize_then_signal(self, record_id, rec):
        applied = real(self, record_id, rec)
        holder["cancel"].set()      # the signal landed DURING the call
        return applied

    real_worker = pipeline.run_prepush_review

    def capture(*a, **kw):
        holder["cancel"] = kw["cancel"]
        return real_worker(*a, **kw)

    monkeypatch.setattr(pipeline, "run_prepush_review", capture)
    monkeypatch.setattr(store_mod.Store, "finalize_review", finalize_then_signal)
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    rec = _assert_cancelled_shape(db, repo, rid)
    assert "during finalization" in rec["degraded_reason"], rec["degraded_reason"]


def test_the_post_commit_demotion_leaves_an_already_superseded_record_alone(
        tmp_path, monkeypatch):
    """`mark_cancelled` is guarded on `trustworthy=1`, which makes it
    self-limiting: a record some other transition already settled keeps its own
    answer."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, _ = _reserve(db, repo)
    with Store.open(db) as st:
        st.reserve_prepush("feat", "f" * 40, "main", "b" * 40, "h2", 100,
                           _evidence(valid=False))
        assert st.mark_cancelled(rid, "cancelled") is False
        assert st.get_review(rid)["status"] == "superseded"


def test_a_cancelled_worker_takes_the_providers_process_group_with_it(tmp_path):
    """The named mutation is "remove the worker SIGTERM handler": this test dies.

    A bare SIGTERM death of the worker would orphan the model CLI -- it runs in its
    OWN session/process group so the watchdog can signal the whole tree, so nothing
    would ever reap it. It would keep spending quota on a review whose record is
    already superseded, and overlap the replacement review on one backend.
    """
    pgfile = tmp_path / "provider.pgid"
    body = (f'python3 -c "import os,sys; open({str(pgfile)!r},\'w\')'
            f'.write(str(os.getpgid(0)))"\n'
            "trap '' TERM\n"
            "sleep 120\n")
    repo = _bg_repo(tmp_path, body=body)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    env = dict(os.environ)
    env["SKODUN_DB"] = str(db)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "worker", "--record-id", rid,
         "--repo", str(repo), "--branch", "feat", "--local-oid", ident["head"],
         "--base-sha", ident["base_sha"], "--base-ref", ident["base_ref"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
        start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not pgfile.exists():
            time.sleep(0.05)
        assert pgfile.exists(), "the provider never started"
        pgid = int(pgfile.read_text(encoding="utf-8"))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _pgroup_alive(pgid):
            time.sleep(0.05)
        assert not _pgroup_alive(pgid), (
            "the provider's process group outlived the cancelled worker")
    finally:
        if proc.poll() is None:         # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=30)
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "failed" and rec["trustworthy"] is False


def _pgroup_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:             # pragma: no cover - not our process
        return True
    return True


# --------------------------------------------------------------------------
# supersede signalling: the pid-reuse guard (ORACLE A14.4)
# --------------------------------------------------------------------------


def _fake_worker_process(tmp_path: Path, name: str = "skodun") -> subprocess.Popen:
    """A live process whose `ps -o args=` NAMES the skodun worker entrypoint.

    A script file called `skodun` invoked with `worker ...`, so the guard is tested
    against a real `ps` reading a real argv rather than against a stub of it.
    """
    script = tmp_path / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\ntrap '' TERM\nsleep 120\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return subprocess.Popen([str(script), "worker", "--record-id", "sk_x"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_a_pid_that_ps_confirms_as_a_worker_is_signalled(tmp_path):
    proc = _fake_worker_process(tmp_path)
    try:
        assert pid_is_skodun_worker(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_a_pid_that_is_something_else_entirely_is_never_signalled(tmp_path):
    """The pid-reuse guard. `kill -0` only proves SOME process owns the pid, and a
    `running` marker can be old enough for the kernel to have recycled it onto an
    unrelated same-user process -- a developer's editor, eventually."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            stdout=subprocess.DEVNULL)
    try:
        assert pid_is_skodun_worker(proc.pid) is False
    finally:
        proc.kill()
        proc.wait(timeout=30)


@pytest.mark.parametrize("pid", [None, 0, -1, "123", True, 1.0, ""])
def test_anything_that_is_not_a_real_pid_is_never_signalled(pid):
    """Total, and fail-closed: there is no path from "we could not tell" to
    "signal it". `True` is caught explicitly -- it is an `int` subclass, and
    `os.kill(True, SIGTERM)` would signal pid 1."""
    assert pid_is_skodun_worker(pid) is False


def test_a_reaped_pid_is_never_signalled(tmp_path):
    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait(timeout=30)
    assert pid_is_skodun_worker(proc.pid) is False


def test_signalling_only_touches_confirmed_workers(tmp_path):
    worker = _fake_worker_process(tmp_path)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdout=subprocess.DEVNULL)
    try:
        sent = signal_superseded([
            {"id": "a", "pid": worker.pid},
            {"id": "b", "pid": other.pid},
            {"id": "c", "pid": None},
        ])
        assert sent == 1
        # The unconfirmed one is untouched, which is the guard's whole point.
        assert other.poll() is None
    finally:
        for p in (worker, other):
            p.kill()
            p.wait(timeout=30)


def test_a_live_but_unconfirmABLE_worker_cannot_resurrect_its_record(tmp_path):
    """It gets NO signal -- and that is safe rather than lax, because
    finalization is conditional.

    The reservation transaction already marked the row `superseded`, so when that
    still-running worker finishes it calls `finalize_review`, is told the record is
    no longer running, and changes nothing.
    """
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    # A pid `ps` cannot confirm: this very test process.
    with Store.open(db) as st:
        assert st.attach_pid(rid, os.getpid())
        newer = st.reserve_prepush("feat", "f" * 40, "main", "b" * 40, "h2",
                                   100, _evidence(valid=False))
    assert not pid_is_skodun_worker(os.getpid())
    assert signal_superseded(newer.superseded) == 0, "we signalled a stranger"
    # The unconfirmed worker finishes anyway. Its answer is refused.
    out = run_worker(rid, repo, "feat", ident["head"], ident["base_sha"],
                     ident["base_ref"], db)
    assert out.code == 0
    with Store.open(db) as st:
        rec = st.get_review(rid)
    assert rec["status"] == "superseded" and rec["trustworthy"] is False


def test_a_now_record_is_never_superseded_or_signalled(tmp_path):
    """`--now` runs are fully independent (ORACLE: "never disturb foreground
    --now runs"). A dispatcher that retired one would kill a review a human is
    watching."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review(dict(
            failed_record("feat", ""), id="sk_fg", mode="now", status="running",
            pid=os.getpid(), failure_reason=None))
        res = st.reserve_prepush("feat", "a" * 40, "main", "b" * 40, "h", 100,
                                 _evidence(valid=False))
        assert res.superseded == ()
        assert st.get_review("sk_fg")["status"] == "running"


# --------------------------------------------------------------------------
# the pid attach: the two failure shapes
# --------------------------------------------------------------------------


def test_a_failed_pid_attach_stops_the_child_it_just_started(tmp_path,
                                                             monkeypatch, capsys):
    """The reservation was superseded between the lease and the spawn. The record
    is ALREADY terminal, so there is nothing to demote -- only a child to stop
    before it reviews content whose record is settled and overlaps the replacement
    review on one inference backend."""
    repo = _bg_repo(tmp_path, body="sleep 120\n")
    db = tmp_path / "s.db"
    procs = []
    from skodun import dispatch as dispatch_mod
    real_spawn = dispatch_mod.spawn_worker

    def spy(*a, **kw):
        proc = real_spawn(*a, **kw)
        procs.append(proc)
        return proc

    monkeypatch.setattr(dispatch_mod, "spawn_worker", spy)
    monkeypatch.setattr(store_mod.Store, "attach_pid",
                        lambda self, rid, pid: False)
    assert run_dispatch(_push_line(repo), repo, db) == 0
    assert len(procs) == 1
    assert procs[0].poll() is not None, "the child outlived its failed attach"
    assert "was superseded before its worker attached" in capsys.readouterr().err


def test_an_attach_EXCEPTION_makes_the_reservation_terminal_immediately(
        tmp_path, monkeypatch):
    """The named mutation is "drop the dispatcher's failed-finalize after an attach
    exception": this test is what dies.

    An attach that RAISED leaves the pid unknown, so no later supersede can ever
    signal that child -- and without this the durable failure would wait for a
    stale sweep, which is a whole runtime budget away.
    """
    repo = _bg_repo(tmp_path, body="sleep 120\n")
    db = tmp_path / "s.db"
    procs = []
    from skodun import dispatch as dispatch_mod
    real_spawn = dispatch_mod.spawn_worker

    def spy(*a, **kw):
        proc = real_spawn(*a, **kw)
        procs.append(proc)
        return proc

    monkeypatch.setattr(dispatch_mod, "spawn_worker", spy)
    monkeypatch.setattr(store_mod.Store, "attach_pid",
                        lambda self, rid, pid: (_ for _ in ()).throw(
                            RuntimeError("the database went away")))
    assert run_dispatch(_push_line(repo), repo, db) == 0
    assert procs[0].poll() is not None, "the child was not reaped"
    (row,) = _rows(db)
    assert row["status"] == "failed", "the reservation was left running"
    assert row["trustworthy"] is False
    assert "pid could not be recorded" in row["failure_reason"]


# --------------------------------------------------------------------------
# the detached worker's environment: an ALLOWLIST
# --------------------------------------------------------------------------


def test_the_worker_env_is_an_allowlist_not_a_filter(tmp_path, monkeypatch):
    """A pre-push hook inherits whatever the developer's shell had.

    `GIT_DIR`/`GIT_INDEX_FILE` left over from the push would silently repoint every
    git call the worker makes at the wrong repository, and `PYTHONSTARTUP` can
    change what the interpreter does before `main` is reached. An allowlist means a
    new poison variable is excluded by DEFAULT rather than after someone notices.
    """
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/stale-index")
    monkeypatch.setenv("PYTHONSTARTUP", "/tmp/evil.py")
    monkeypatch.setenv("SKODUN_GROK_BIN", "/pinned/grok")
    monkeypatch.setenv("SKODUN_ANYTHING_AT_ALL", "carried")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = worker_env(tmp_path / "s.db")
    for poison in ("GIT_DIR", "GIT_INDEX_FILE", "PYTHONSTARTUP"):
        assert poison not in env, poison
    assert env["SKODUN_GROK_BIN"] == "/pinned/grok"
    assert env["SKODUN_ANYTHING_AT_ALL"] == "carried"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == str(tmp_path / "home")


def test_the_worker_env_pins_SKODUN_DB_even_when_the_parent_had_none(tmp_path,
                                                                    monkeypatch):
    """Explicitly, so the worker cannot resolve a DIFFERENT default store than the
    one holding its reservation."""
    monkeypatch.delenv("SKODUN_DB", raising=False)
    env = worker_env(tmp_path / "elsewhere" / "s.db")
    assert env["SKODUN_DB"] == str(tmp_path / "elsewhere" / "s.db")


def test_the_worker_env_forces_a_utf8_locale(tmp_path, monkeypatch):
    """NOT inherited. The worker's stderr goes to a log file and its diffs carry
    arbitrary bytes; an inherited ASCII locale turns a non-ASCII filename in a
    progress line into a `UnicodeEncodeError` inside a detached process nobody is
    watching."""
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "en_US.ISO8859-1")
    env = worker_env(tmp_path / "s.db")
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C.UTF-8"


def test_the_worker_env_makes_the_package_importable_from_this_very_checkout(
        tmp_path, monkeypatch):
    """`PYTHONPATH` is COMPUTED from this module's own location, never inherited:
    the worker must run the same code as the dispatcher that spawned it, and a
    source checkout is only importable through it."""
    monkeypatch.setenv("PYTHONPATH", "/somewhere/stale")
    env = worker_env(tmp_path / "s.db")
    assert env["PYTHONPATH"] == _SRC
    assert "/somewhere/stale" not in env["PYTHONPATH"]


def test_a_poison_env_var_really_does_not_reach_a_live_worker(tmp_path, spawned,
                                                             monkeypatch):
    """End to end, through a real detached worker AND the provider it spawns.

    The provider's own environment is the ground truth: everything the worker
    inherited is what it passes on. A neutral marker rather than `GIT_DIR` is used
    deliberately -- `GIT_DIR` would poison the in-process git calls the test and
    the dispatcher itself make, so it could not distinguish "the allowlist worked"
    from "nothing worked".
    """
    repo = _bg_repo(tmp_path, body='env > "$D/env_$CALL.txt"\n' + _emit(CLEAN))
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_CARRIED_MARKER", "yes")
    monkeypatch.setenv("DEFINITELY_NOT_ALLOWLISTED", "poison")
    assert run_dispatch(_push_line(repo), repo, db) == 0
    (rid,) = _ids(db)
    assert _await(db, rid)["status"] == "clean"
    seen = (tmp_path / "bin" / "env_1.txt").read_text(encoding="utf-8")
    assert "DEFINITELY_NOT_ALLOWLISTED" not in seen, (
        "an un-allowlisted variable reached the provider through the worker")
    assert "SKODUN_CARRIED_MARKER=yes" in seen, "every SKODUN_* must be carried"
    assert f"SKODUN_DB={db}" in seen, "the worker must be pinned to OUR store"


# --------------------------------------------------------------------------
# the shim
# --------------------------------------------------------------------------


def test_the_hook_directory_comes_from_git_never_from_a_guess(tmp_path,
                                                              hermetic_git):
    repo = _mkrepo(tmp_path)
    assert hooks_dir(repo) == (repo / ".git" / "hooks").resolve()


def test_a_linked_worktree_installs_into_the_directory_git_actually_uses(
        tmp_path, hermetic_git):
    """A linked worktree's `.git` is a FILE pointing into the main repository's
    `worktrees/<name>`. Writing to `<worktree>/.git/hooks` would install a hook
    git never runs -- silently, which is the whole problem."""
    repo = _mkrepo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", str(linked), "-b", "side")
    assert (linked / ".git").is_file()
    directory = hooks_dir(linked)
    assert not str(directory).startswith(str(linked) + "/.git/hooks")
    path, _ = install_hooks(linked)
    assert SHIM_MARKER in path.read_text(encoding="utf-8")


def test_core_hookspath_is_honoured(tmp_path, hermetic_git):
    """`core.hooksPath` relocates hooks wherever an operator or a tool
    (pre-commit, husky) put them."""
    repo = _mkrepo(tmp_path)
    elsewhere = tmp_path / "custom-hooks"
    _git(repo, "config", "core.hooksPath", str(elsewhere))
    path, _ = install_hooks(repo)
    assert path == elsewhere / "pre-push"
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_installing_into_a_repo_with_no_hook_writes_an_executable_shim(
        tmp_path, hermetic_git):
    repo = _mkrepo(tmp_path)
    path, what = install_hooks(repo)
    assert what == "installed"
    assert SHIM_MARKER in path.read_text(encoding="utf-8")
    assert os.access(path, os.X_OK)
    assert "SKODUN_SHIM_CHAIN=''" in path.read_text(encoding="utf-8")


def test_reinstalling_replaces_only_our_own_shim(tmp_path, hermetic_git):
    repo = _mkrepo(tmp_path)
    path, _ = install_hooks(repo)
    path.write_text(path.read_text(encoding="utf-8") + "\n# hand edit\n",
                    encoding="utf-8")
    path, what = install_hooks(repo)
    assert what == "reinstalled"
    assert "# hand edit" not in path.read_text(encoding="utf-8")


def test_a_foreign_hook_is_refused_without_force(tmp_path, hermetic_git):
    """Somebody else's hook is not ours to move, and the failure mode of guessing
    is a push that silently stops running a check the repository relies on."""
    repo = _mkrepo(tmp_path)
    hook = hooks_dir(repo) / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(HookRefused) as e:
        install_hooks(repo)
    assert "--force" in str(e.value)
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_force_backs_up_a_foreign_hook_and_chains_it(tmp_path, hermetic_git):
    repo = _mkrepo(tmp_path)
    hook = hooks_dir(repo) / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    path, what = install_hooks(repo, force=True)
    backup = hook.parent / f"pre-push{BACKUP_SUFFIX}"
    assert backup.read_text(encoding="utf-8") == "#!/bin/sh\necho theirs\n"
    assert os.access(backup, os.X_OK), "a chained hook must stay executable"
    assert f"SKODUN_SHIM_CHAIN='{backup}'" in path.read_text(encoding="utf-8")
    assert str(backup) in what


def test_reinstalling_after_a_forced_install_keeps_the_chain(tmp_path,
                                                             hermetic_git):
    """Idempotence that MATTERS: an upgrade must not silently drop the foreign
    hook a previous `--force` chained. The shim records its own chain target, so
    the answer comes from one place rather than from re-deriving it."""
    repo = _mkrepo(tmp_path)
    hook = hooks_dir(repo) / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    install_hooks(repo, force=True)
    path, what = install_hooks(repo)
    backup = hook.parent / f"pre-push{BACKUP_SUFFIX}"
    assert f"SKODUN_SHIM_CHAIN='{backup}'" in path.read_text(encoding="utf-8")
    assert "still chaining" in what


def test_a_second_DIFFERENT_foreign_hook_is_refused_even_under_force(
        tmp_path, hermetic_git):
    """Overwriting the backup would destroy the FIRST foreign hook with no trace,
    and only the operator can say which of the two they meant to keep. The
    refusal names both files."""
    repo = _mkrepo(tmp_path)
    hook = hooks_dir(repo) / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho FIRST\n", encoding="utf-8")
    install_hooks(repo, force=True)
    backup = hook.parent / f"pre-push{BACKUP_SUFFIX}"
    # A second, different foreign hook arrives (another tool re-installed itself).
    hook.write_text("#!/bin/sh\necho SECOND\n", encoding="utf-8")
    with pytest.raises(HookRefused) as e:
        install_hooks(repo, force=True)
    assert str(backup) in str(e.value) and str(hook) in str(e.value)
    assert "echo FIRST" in backup.read_text(encoding="utf-8")
    assert "echo SECOND" in hook.read_text(encoding="utf-8")


def test_a_forced_install_over_an_IDENTICAL_backup_proceeds(tmp_path,
                                                            hermetic_git):
    """Nothing would be lost: the backup already holds exactly these bytes."""
    repo = _mkrepo(tmp_path)
    directory = hooks_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pre-push").write_text("#!/bin/sh\necho same\n", encoding="utf-8")
    (directory / f"pre-push{BACKUP_SUFFIX}").write_text(
        "#!/bin/sh\necho same\n", encoding="utf-8")
    path, _ = install_hooks(repo, force=True)
    assert SHIM_MARKER in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the shim AT RUN TIME: the tee, the chain, and warn-and-exit-0
# --------------------------------------------------------------------------


def _shim_repo(tmp_path, *, foreign: str | None = None, force: bool = False):
    """A repo with the shim installed, and a recording stand-in for `skodun`.

    The shim's dispatcher command is `${SKODUN_SHIM_PY} -m skodun dispatch`, so a
    fake "python" that records its argv and stdin is what makes the tee testable
    without running a real review.
    """
    repo = _mkrepo(tmp_path)
    directory = hooks_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    if foreign is not None:
        (directory / "pre-push").write_text(foreign, encoding="utf-8")
        (directory / "pre-push").chmod(0o755)
    fake_py = tmp_path / "fake-python"
    fake_py.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {tmp_path}/dispatch_argv.txt\n'
        f'cat > {tmp_path}/dispatch_stdin.txt\n'
        "exit 0\n", encoding="utf-8")
    fake_py.chmod(0o755)
    hook, _ = install_hooks(repo, force=force, python=str(fake_py))
    return repo, hook


class _StubProc:
    """A `Popen` stand-in: a plausible pid, and terminate/wait/poll that no-op."""

    def __init__(self):
        self.pid = os.getpid()
        self.returncode = 0

    def terminate(self):
        pass

    def kill(self):     # pragma: no cover - the wait below never times out
        pass

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


PUSH_STDIN = ("refs/heads/feat aaaa refs/heads/feat bbbb\n"
              "refs/heads/other cccc refs/heads/other dddd\n")


def _run_hook(hook: Path, stdin: str = PUSH_STDIN, *, argv=("github", "git@x:y"),
              env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(["sh", str(hook), *argv], input=stdin.encode(),
                          capture_output=True, env=env)


def test_the_shim_feeds_the_SAME_bytes_and_argv_to_both_consumers(tmp_path,
                                                                 hermetic_git):
    """The named mutation is "skip the tee and run the old hook directly off live
    stdin": this test is what dies.

    Stdin can be read ONCE. Without the temp file the foreign hook consumes the ref
    list and skodun gets nothing -- so every push would silently review nothing,
    while the hook and the exit code both looked perfectly healthy.
    """
    seen = tmp_path / "foreign.txt"
    foreign = ("#!/bin/sh\n"
               f'printf "%s\\n" "$@" > {seen}.argv\n'
               f'cat > {seen}.stdin\n'
               "exit 0\n")
    repo, hook = _shim_repo(tmp_path, foreign=foreign, force=True)
    cp = _run_hook(hook)
    assert cp.returncode == 0, cp.stderr
    assert Path(f"{seen}.stdin").read_text(encoding="utf-8") == PUSH_STDIN
    assert (tmp_path / "dispatch_stdin.txt").read_text(encoding="utf-8") == PUSH_STDIN
    assert Path(f"{seen}.argv").read_text(encoding="utf-8").split() == \
        ["github", "git@x:y"]
    argv = (tmp_path / "dispatch_argv.txt").read_text(encoding="utf-8").split()
    assert argv == ["-m", "skodun", "dispatch", "github", "git@x:y"]


def test_the_shim_propagates_the_old_hooks_refusal_and_never_dispatches(
        tmp_path, hermetic_git):
    """A chained hook that FAILS must fail the push exactly as it would have
    without skodun -- and skodun must not run at all, because the push is not
    happening."""
    foreign = "#!/bin/sh\necho theirs-said-no >&2\nexit 7\n"
    repo, hook = _shim_repo(tmp_path, foreign=foreign, force=True)
    cp = _run_hook(hook)
    assert cp.returncode == 7, cp.stderr
    assert b"theirs-said-no" in cp.stderr
    assert not (tmp_path / "dispatch_argv.txt").exists()


def test_the_shim_runs_the_chained_hook_BEFORE_skodun(tmp_path, hermetic_git):
    order = tmp_path / "order.txt"
    foreign = f"#!/bin/sh\necho foreign >> {order}\ncat > /dev/null\nexit 0\n"
    repo, hook = _shim_repo(tmp_path, foreign=foreign, force=True)
    # Re-point the dispatcher stand-in so it also appends to the order file.
    (tmp_path / "fake-python").write_text(
        f"#!/bin/sh\necho skodun >> {order}\ncat > /dev/null\nexit 0\n",
        encoding="utf-8")
    (tmp_path / "fake-python").chmod(0o755)
    assert _run_hook(hook).returncode == 0
    assert order.read_text(encoding="utf-8").split() == ["foreign", "skodun"]


def test_the_shim_warns_and_exits_0_when_the_dispatcher_is_absent(tmp_path,
                                                                 hermetic_git):
    """Once the foreign hook has passed, NOTHING about review machinery may block
    the push -- including skodun failing to start."""
    repo, hook = _shim_repo(tmp_path)
    cp = _run_hook(hook, env_extra={"SKODUN_SHIM_PY": "/definitely/not/here"})
    assert cp.returncode == 0, cp.stderr
    assert b"dispatcher failed" in cp.stderr
    assert b"NOT blocked" in cp.stderr


def test_the_shim_warns_and_exits_0_when_the_dispatcher_exits_nonzero(tmp_path,
                                                                     hermetic_git):
    repo, hook = _shim_repo(tmp_path)
    (tmp_path / "fake-python").write_text(
        "#!/bin/sh\ncat > /dev/null\necho boom >&2\nexit 3\n", encoding="utf-8")
    (tmp_path / "fake-python").chmod(0o755)
    cp = _run_hook(hook)
    assert cp.returncode == 0, cp.stderr
    assert b"exit 3" in cp.stderr


def test_the_shim_exits_0_with_no_chained_hook_and_a_working_dispatcher(
        tmp_path, hermetic_git):
    repo, hook = _shim_repo(tmp_path)
    cp = _run_hook(hook)
    assert cp.returncode == 0, cp.stderr
    assert (tmp_path / "dispatch_stdin.txt").read_text(encoding="utf-8") == PUSH_STDIN


def test_the_shim_leaves_no_temp_file_behind(tmp_path, hermetic_git):
    repo, hook = _shim_repo(tmp_path)
    before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("skodun-prepush.*"))
    _run_hook(hook)
    after = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("skodun-prepush.*"))
    assert after <= before, "the shim's buffered ref list was not cleaned up"


def test_the_shim_is_syntactically_valid_posix_sh(tmp_path):
    script = tmp_path / "shim.sh"
    script.write_text(shim_text("/some/where/pre-push.pre-skodun",
                                "/usr/bin/python3"), encoding="utf-8")
    assert subprocess.run(["sh", "-n", str(script)],
                          capture_output=True).returncode == 0


def test_the_shim_survives_a_push_with_no_arguments_at_all(tmp_path, hermetic_git):
    """`set -u` plus `"$@"` on an empty argv: a hand-run hook, and a git that
    someday passes fewer arguments, must not die on an unbound variable."""
    repo, hook = _shim_repo(tmp_path)
    cp = _run_hook(hook, argv=())
    assert cp.returncode == 0, cp.stderr


# --------------------------------------------------------------------------
# `run_prepush_review`: the background pipeline entry
# --------------------------------------------------------------------------


def _prepush(db: Path, repo: Path, **kw) -> dict:
    """Run `run_prepush_review` for `feat`'s tip against a fresh reservation."""
    rid, ident = _reserve(db, repo)
    cfg = load_config(repo)
    base = gitio.Base(ref=ident["base_ref"], sha=ident["base_sha"])
    diff = gitio.capture_ref_diff(repo, ident["base_sha"], ident["head"])
    with Store.open(db) as st:
        return pipeline.run_prepush_review(
            st, repo, rid, "feat", ident["head"], base, diff,
            effective_defaults(cfg.defaults, cfg.dispatch), cfg, **kw)


def test_run_prepush_review_persists_nothing_of_its_own(tmp_path):
    """The worker holds the ONLY write, and that is what makes conditional
    finalization the single gate a background answer passes through."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    with Store.open(db) as st:
        before = json.dumps(st.get_review(rid), sort_keys=True)
        cfg = load_config(repo)
        diff = gitio.capture_ref_diff(repo, ident["base_sha"], ident["head"])
        rec = pipeline.run_prepush_review(
            st, repo, rid, "feat", ident["head"],
            gitio.Base(ref=ident["base_ref"], sha=ident["base_sha"]), diff,
            effective_defaults(cfg.defaults, cfg.dispatch), cfg)
        after = json.dumps(st.get_review(rid), sort_keys=True)
    assert after == before, "run_prepush_review wrote to the store"
    assert rec["status"] == "clean" and rec["id"] == rid


def test_the_background_prompt_shows_the_PUSHED_OID_not_the_working_tree(tmp_path):
    """The dispatcher runs from a pre-push hook and the checkout may already be
    somewhere else entirely; reading it would certify content nobody pushed."""
    repo = _bg_repo(tmp_path)
    # The working tree diverges AFTER the commit that is being pushed.
    (repo / "a.txt").write_text("WORKING TREE ONLY\n", encoding="utf-8")
    (repo / "untracked-only.txt").write_text("nobody pushed me\n", encoding="utf-8")
    db = tmp_path / "s.db"
    rec = _prepush(db, repo)
    prompt = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert "WORKING TREE ONLY" not in prompt
    assert "untracked-only.txt" not in prompt
    assert rec["head"] == _git(repo, "rev-parse", "feat")
    assert "(working tree)" not in prompt, (
        "the head LABEL is the pushed oid; the foreground's label would claim a "
        "scope this review does not have")


def test_the_background_context_pack_reads_the_commit_not_the_checkout(tmp_path):
    """`source="oid"`. The pack is what `context_hash` identifies, and a pack of
    the working tree published under the commit's identity would make dedup
    suppress against context nobody reviewed."""
    repo = _bg_repo(tmp_path, extra_cfg="\n[defaults]\ncontext_pack = true\n")
    (repo / "a.txt").write_text("WORKING TREE ONLY\n", encoding="utf-8")
    db = tmp_path / "s.db"
    rec = _prepush(db, repo)
    prompt = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert "WORKING TREE ONLY" not in prompt
    assert rec["context_hash"], "a packing review must publish an identity"


def test_the_background_context_hash_matches_the_dedup_evidence_exactly(tmp_path):
    """If these two ever disagree, nothing would EVER dedup-match again -- silently,
    because a mismatch looks exactly like "this diff has not been reviewed"."""
    repo = _bg_repo(tmp_path, extra_cfg="\n[defaults]\ncontext_pack = true\n")
    db = tmp_path / "s.db"
    rec = _prepush(db, repo)
    assert rec["context_hash"] == _candidate_hash(repo, tmp_path)


def test_a_large_prompt_escalates_the_background_cap_to_the_foreground_one(
        tmp_path, monkeypatch):
    """ORACLE A14.7. A whole-diff prompt this large legitimately needs longer than
    a background cap allows, and timing it out would spend the whole budget and
    record nothing."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    caps = []
    real = pipeline._run_chain

    def spy(reviewer, cfg, d, *a, **kw):
        caps.append(d.timeout_sec)
        return real(reviewer, cfg, d, *a, **kw)

    monkeypatch.setattr(pipeline, "_run_chain", spy)
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\ntimeout_sec = 999\n"
              "\n[dispatch]\ntimeout_sec = 5\nlarge_prompt_bytes = 1\n",
        encoding="utf-8")
    _prepush(db, repo)
    assert caps == [999], "the large-prompt escalation did not apply"

    caps.clear()
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\ntimeout_sec = 999\n"
              "\n[dispatch]\ntimeout_sec = 5\nlarge_prompt_bytes = 4000000\n",
        encoding="utf-8")
    _prepush(db, repo)
    assert caps == [5], "an ordinary prompt must keep the BACKGROUND cap"


def test_the_escalation_is_never_a_REDUCTION(tmp_path):
    """A config may legitimately set `[dispatch] timeout_sec` ABOVE the foreground
    one (the reservation budget takes the max for exactly that reason). Applying
    the foreground figure literally would give the LARGEST prompts the SHORTEST
    cap -- the opposite of what "escalates" means."""
    d = Defaults(timeout_sec=900)
    assert pipeline._escalated(d, 10_000, (1, 100)).timeout_sec == 900
    assert pipeline._escalated(d, 10_000, (1, 5000)).timeout_sec == 5000
    assert pipeline._escalated(d, 10_000, None).timeout_sec == 900


def test_a_batched_background_review_packs_context_from_the_pushed_commit(tmp_path):
    """THE Task-8 leave-out this task threads: the per-batch pack call used to be
    hard-wired to the working tree.

    Two things had to be arranged for this to be OBSERVABLE at all, and both are
    load-bearing rather than incidental:

    * the files are MODIFIED, not added. An added file below
      `contextpack.ALREADY_IN_DIFF_MAX` is `already-in-diff` and never packed from
      ANY source, so a test built on added files passes identically whichever tree
      the packer reads.
    * the budget leaves each batch real headroom. With none, the pack is empty and
      the source it read from leaves no trace.

    Asserted in both directions: the pushed commit's content IS packed, and the
    checkout's is not.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\nmax_diff_bytes = 2000\ncontext_pack = true\n",
        encoding="utf-8")
    for i in range(8):
        (repo / f"f{i}.txt").write_text(("base %d\n" % i) * 30, encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "base files")
    _git(repo, "checkout", "-b", "feat")
    for i in range(8):
        (repo / f"f{i}.txt").write_text(
            f"COMMITTED-MARKER-{i}\n" + ("changed %d\n" % i) * 30,
            encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "the pushed change")
    # The checkout moves on AFTER the push, which is the ordinary case for a
    # pre-push hook: nothing the worker reads may come from here.
    for i in range(8):
        (repo / f"f{i}.txt").write_text("WORKING TREE ONLY\n", encoding="utf-8")

    db = tmp_path / "s.db"
    rec = _prepush(db, repo)
    assert rec["batched"] is True and rec["batch_count"] >= 2, rec["batch_count"]
    assert rec["context_bytes"] > 0, (
        "no context was packed at all, so this test could not see the source")
    bodies = [q.read_text(encoding="utf-8")
              for q in sorted((tmp_path / "bin").glob("prompt_*.txt"))]
    joined = "\n".join(bodies)
    assert "WORKING TREE ONLY" not in joined, (
        "a batch packed the CHECKOUT; the review would certify content nobody "
        "pushed")
    assert "COMMITTED-MARKER-" in joined
    assert rec["context_hash"] == "", (
        "a batched aggregate has no single canonical pack, so it publishes no "
        "identity -- it must never be dedup-suppressible")
    assert rec["usable_output"] is True


def test_a_background_review_never_runs_the_foreground_only_extra_passes(tmp_path,
                                                                        monkeypatch):
    """`should_run_security`, `should_run_skeptic` and `refuter_decision` are all
    gated on `mode == "now"`, so `prepush` is the primary review and nothing else.
    Pinned because the reservation's runtime budget is sized on that assumption."""
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    repo = _bg_repo(tmp_path, body=_emit(DIRTY))
    db = tmp_path / "s.db"
    rec = _prepush(db, repo)
    assert _calls(tmp_path) == 1, "a background review made more than one call"
    assert rec["extra_passes"] == {}


def test_an_empty_background_diff_fails_closed(tmp_path):
    """UNLIKE `--now`, which prints a clean verdict for an empty diff. The
    dispatcher never reserves one, so reaching here means the ref moved in a way
    the identity check did not catch -- and an empty prompt could mint a clean
    verdict for content nobody looked at."""
    repo = _bg_repo(tmp_path)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)
    cfg = load_config(repo)
    with Store.open(db) as st:
        rec = pipeline.run_prepush_review(
            st, repo, rid, "feat", ident["head"],
            gitio.Base(ref=ident["base_ref"], sha=ident["base_sha"]),
            gitio.Diff(data=b""),
            effective_defaults(cfg.defaults, cfg.dispatch), cfg)
    assert rec["status"] == "failed" and rec["usable_output"] is False
    assert "no outgoing changes" in rec["failure_reason"]
    assert _calls(tmp_path) == 0


def test_usable_output_is_never_the_finding_count(tmp_path):
    """A clean round and a round that produced NOTHING both have zero findings, and
    the difference between them is "reviewed and clean" versus "NO REVIEW
    HAPPENED". Same repo, same diff, two calls: only the provider's answer
    differs, so nothing but that answer can explain the two values."""
    repo = _bg_repo(tmp_path, body=_per_call(_emit(CLEAN),
                                             "echo not json at all\nexit 0\n"))
    db = tmp_path / "s.db"
    clean = _prepush(db, repo)
    assert clean["findings_total"] == 0 and clean["usable_output"] is True
    broken = _prepush(db, repo)
    assert broken["findings_total"] == 0 and broken["usable_output"] is False
    assert broken["parse_ok"] is False


# --------------------------------------------------------------------------
# the whole loop: install the hook, push, and let the gate answer
# --------------------------------------------------------------------------


def test_the_installed_shim_drives_a_real_push_to_a_gate_passing_record(
        tmp_path, hermetic_git, monkeypatch):
    """The one test that runs the product: `git push` -> shim -> dispatch ->
    detached worker -> record -> `skodun gate`.

    Everything else here pins one seam; this pins that the seams are connected.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True,
                   capture_output=True)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "cfg")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\nthree\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c1")

    db = tmp_path / "s.db"
    hook, what = install_hooks(repo)
    assert what == "installed"

    env = dict(os.environ)
    env["SKODUN_DB"] = str(db)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    push = subprocess.run(["git", "-C", str(repo), "push", "origin", "feat"],
                          capture_output=True, env=env)
    assert push.returncode == 0, push.stderr.decode("utf-8", "replace")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not _ids(db):
        time.sleep(0.05)
    ids = _ids(db)
    assert ids, ("the shim never reached the dispatcher: "
                 + push.stderr.decode("utf-8", "replace"))
    rec = _await(db, ids[0])
    assert rec["status"] == "clean" and rec["trustworthy"] is True
    assert rec["branch"] == "feat" and rec["mode"] == "prepush"
    # `base_ref` is the remote ref as pushed: the branch did not exist on the
    # remote, so Task 5's resolution picked the main candidate instead.
    assert rec["base_sha"] == _git(repo, "rev-parse", "main")

    # And the gate accepts it: the whole point of the record.
    from skodun.gate import run_gate
    with Store.open(db) as st:
        result = run_gate(st, repo, load_config(repo))
    assert result.code == 0, result.message


# ===========================================================================
# TASK 11: dispatcher trust-boundary drills
# ===========================================================================
#
# Everything above pins ONE seam at a time -- a unit, an in-process call, a
# stub standing in for a racing peer. These five drive the acceptance
# criteria's own scenarios end to end: real detached worker processes, real
# concurrent dispatchers, a real signal a handler cannot catch. Every one of
# them is reaped explicitly (`spawned`, or a manual `wait`/`killpg`) -- a
# leaked child here would keep spending fake-CLI "model calls" against a store
# the next test is about to replace.


def test_a_sigkilled_worker_is_recovered_by_its_own_persisted_budget_and_the_gate_fails_closed(
        tmp_path):
    """Drill 1: SIGKILL a worker mid-run; only the startup sweep ever notices.

    A `SIGKILL` cannot be caught, so it skips the worker's SIGTERM handler,
    its watchdog and every `finally` block -- none of the cascade
    `test_a_cancelled_worker_takes_the_providers_process_group_with_it` pins
    (a SIGTERM the worker's own handler converts into an orderly cancellation)
    ever runs. `pipeline.recover_stale`'s startup sweep is the ONLY thing left
    that can ever close this record, and it has to do it from the record's own
    persisted `worst_runtime_sec` -- test_pipeline.py's
    `test_recover_stale_fails_old_running_records_and_leaves_fresh_ones`
    fabricates its `running` row with `store.save_review` directly and never
    has a real worker, a real reservation or a real budget at all.

    The wall-clock wait for a real budget to actually elapse would cost a
    real minute-plus at the shipped defaults, so the record is backdated
    directly (as `test_pipeline.py`'s own `_running` helper does) rather than
    slept for: the sweep's decision is about elapsed time, and faking the
    clock on an otherwise completely real record is what keeps this
    deterministic instead of a sleep of hope.
    """
    pgfile = tmp_path / "provider.pgid"
    body = (f'python3 -c "import os,sys; open({str(pgfile)!r},\'w\')'
            f'.write(str(os.getpgid(0)))"\n'
            "sleep 120\n")
    repo = _bg_repo(tmp_path, body=body)
    db = tmp_path / "s.db"
    rid, ident = _reserve(db, repo)

    env = dict(os.environ)
    env["SKODUN_DB"] = str(db)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "worker", "--record-id", rid,
         "--repo", str(repo), "--branch", "feat", "--local-oid", ident["head"],
         "--base-sha", ident["base_sha"], "--base-ref", ident["base_ref"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        start_new_session=True)
    pgid = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not pgfile.exists():
            time.sleep(0.05)
        assert pgfile.exists(), "the worker never reached the provider"
        pgid = int(pgfile.read_text(encoding="utf-8"))
        with Store.open(db) as st:
            assert st.get_review(rid)["status"] == "running"

        proc.kill()                      # SIGKILL: no handler, no `finally`
        proc.wait(timeout=30)
        assert proc.returncode != 0
    finally:
        # The provider is a session leader of its OWN (see `spawn_worker` /
        # `runner._run_once`), so the SIGKILLed worker orphaned it rather than
        # taking it down -- clean it up explicitly or it leaks for 120s.
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _pgroup_alive(pgid):
                time.sleep(0.05)

    with Store.open(db) as st:
        stuck = st.get_review(rid)
    assert stuck["status"] == "running", "the SIGKILLed worker still finalized"
    budget = stuck["worst_runtime_sec"]
    assert isinstance(budget, int) and budget > 0, (
        "the reservation carried no budget of its own -- the fixture, not the "
        "drill, is broken")

    past = time.strftime(store_mod._TS_FORMAT,
                         time.gmtime(time.time() - budget - 5))
    with Store.open(db) as st:
        st.save_review(dict(stuck, reviewed_at=past))

    cfg = load_config(repo)
    with Store.open(db) as st:
        swept = pipeline.recover_stale(st, cfg)
        rec = st.get_review(rid)
    assert swept == 1, "the stale sweep never recovered the SIGKILLed worker"
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert rec["parse_ok"] is False
    assert "stale" in rec["failure_reason"].lower()

    from skodun.gate import run_gate
    with Store.open(db) as st:
        result = run_gate(st, repo, cfg)
    assert result.code == 2, result.message


def test_a_probe_that_precedes_a_racing_finalize_is_still_suppressed_inside_the_lease(
        tmp_path, spawned, monkeypatch):
    """Drill 2, race direction (a): dispatcher 2's evidence build genuinely
    finishes BEFORE dispatcher 1 finalizes, and only the RESERVATION
    TRANSACTION's own re-check -- not the stale evidence -- is what still
    suppresses it.

    `build_dedup_evidence`'s docstring names exactly this: "a racing
    dispatcher can finalize a trustworthy review a millisecond after we
    look". `test_a_suppressed_push_never_touches_an_in_flight_review` pins the
    RULE with a review that is already finalized (via a direct `save_review`)
    before the second dispatch even starts -- so its evidence build has
    nothing stale to race. Here a real `threading.Event` handshake pauses
    dispatcher 2 immediately after its REAL evidence probe returns and
    strictly before it reaches `reserve_prepush`, and it is released only once
    dispatcher 1's REAL detached worker has genuinely committed a trustworthy
    `clean` review -- so the probe really was answered before the fact it
    needed to know about existed.
    """
    repo = _bg_repo(tmp_path, body="sleep 2\n" + _emit(CLEAN))
    db = tmp_path / "s.db"
    push = _push_line(repo)

    real_evidence = dispatch.build_dedup_evidence
    probed = threading.Event()
    release = threading.Event()

    def paused_evidence(*a, **kw):
        ev = real_evidence(*a, **kw)          # the real probe, computed NOW
        probed.set()
        assert release.wait(timeout=60), "dispatcher 1 never finalized"
        return ev

    assert run_dispatch(push, repo, db) == 0
    (rid1,) = _ids(db)

    monkeypatch.setattr(dispatch, "build_dedup_evidence", paused_evidence)
    results: list[int] = []

    def second() -> None:
        results.append(run_dispatch(push, repo, db))

    t = threading.Thread(target=second)
    t.start()
    try:
        assert probed.wait(timeout=30), "dispatcher 2 never reached its probe"
        with Store.open(db) as st:
            assert st.get_review(rid1)["status"] == "running", (
                "dispatcher 1 already finalized before dispatcher 2 even "
                "probed -- this is not the race the drill exists to drive")
        rec1 = _await(db, rid1)
        assert rec1["status"] == "clean" and rec1["trustworthy"] is True
    finally:
        release.set()          # release even if an assertion above raised
    t.join(timeout=30)
    assert not t.is_alive()

    assert results == [0]
    assert _ids(db) == [rid1], "the suppressed push wrote a record of its own"
    with Store.open(db) as st:
        events = st._c.execute("SELECT * FROM dedup_events").fetchall()
    assert len(events) == 1, "the suppression left no audit row"
    assert events[0]["matched_review_id"] == rid1
    assert events[0]["diff_hash"] == rec1["diff_hash"]


def test_a_true_zero_delay_race_leaves_one_review_and_one_superseded_audit_row(
        tmp_path, spawned):
    """Drill 3, race direction (b): two REAL, concurrent dispatchers reserve
    for the same branch at (as near as a `threading.Barrier` can make it) the
    same instant, and dispatcher 1's row is caught genuinely `running` at the
    moment it is retired.

    `test_a_zero_delay_double_dispatch_leaves_exactly_one_reviewed_record`
    already pins the OUTCOME of this race, but drives it by calling
    `run_dispatch` twice in a row on the same thread -- "zero delay" there
    means "no sleep between the two calls", not "the two reservation
    transactions were actually in flight at once". A `Barrier` makes that
    literal, and it is what makes the mid-race assertion below ("dispatcher
    1's row is `running`, not merely superseded-and-not-yet-observed")
    meaningful rather than incidental.
    """
    repo = _bg_repo(tmp_path, body="sleep 1.5\n" + _emit(CLEAN))
    db = tmp_path / "s.db"
    Store.open(db).close()          # settle the fresh-store migration first;
                                     # that race is `test_a_fresh_store_
                                     # survives_concurrent_openers`'s alone.

    oid1 = _git(repo, "rev-parse", "feat")
    (repo / "a.txt").write_text("two\nthree\nfour\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c2")
    oid2 = _git(repo, "rev-parse", "feat")
    assert oid1 != oid2

    line1 = f"refs/heads/feat {oid1} refs/heads/feat {ZERO}\n"
    line2 = f"refs/heads/feat {oid2} refs/heads/feat {ZERO}\n"

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}

    def racer(name: str, line: str) -> None:
        barrier.wait(timeout=30)
        results[name] = run_dispatch(line, repo, db)

    threads = [threading.Thread(target=racer, args=("a", line1)),
               threading.Thread(target=racer, args=("b", line2))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    assert results == {"a": 0, "b": 0}
    rows = _rows(db)
    assert len(rows) == 2, rows
    running_now = [r for r in rows if r["status"] == "running"]
    superseded_now = [r for r in rows if r["status"] == "superseded"]
    assert len(running_now) == 1 and len(superseded_now) == 1, [
        (r["id"], r["status"]) for r in rows]
    winner_id = running_now[0]["id"]
    loser_id = superseded_now[0]["id"]
    assert superseded_now[0]["superseded_by"] == winner_id
    assert superseded_now[0]["trustworthy"] is False

    winner = _await(db, winner_id)
    assert winner["status"] == "clean" and winner["trustworthy"] is True

    # The loser's REAL worker gets the dispatcher's own best-effort SIGTERM
    # once its pid is confirmed (`signal_superseded`), same as any other
    # supersede -- so it typically ends up cancelled rather than clean. Either
    # way, let it run to its own real end rather than asserting how it gets
    # there; `test_a_deleted_branch_does_not_interfere_and_a_cleanly_
    # finishing_superseded_worker_changes_nothing` pins the UNCANCELLED finish
    # specifically, which this race does not reliably produce.
    for proc in spawned:
        proc.wait(timeout=60)

    with Store.open(db) as st:
        loser_final = st.get_review(loser_id)
    assert loser_final["status"] == "superseded", (
        "the loser's late-finishing worker overwrote the superseded audit row")
    assert loser_final["superseded_by"] == winner_id
    assert loser_final["trustworthy"] is False

    final_rows = _rows(db)
    reviewed = [r for r in final_rows if r["status"] == "clean"]
    superseded = [r for r in final_rows if r["status"] == "superseded"]
    assert len(reviewed) == 1 and len(superseded) == 1, final_rows


def test_a_backdated_running_record_is_recovered_by_its_OWN_persisted_budget(
        tmp_path):
    """Drill 4: `recover_stale` prefers the record's OWN persisted
    `worst_runtime_sec` over a fresh recomputation from the CURRENT config --
    and this only tells the two apart when they actually disagree.

    Every existing `recover_stale` test (`test_pipeline.py`'s
    `test_recover_stale_fails_old_running_records_and_leaves_fresh_ones` and
    its neighbours) backdates a HAND-BUILT record that carries no
    `worst_runtime_sec` at all, so they only ever exercise the computed-ceiling
    FALLBACK; the config never changes between reservation and sweep in any of
    them, so a mutation that always recomputed from the CURRENT config would
    still pass every single one. Here the record is reserved for real
    (`reserve_prepush`, so its persisted budget is genuine) under a
    SMALL-timeout config, and the config is then changed to a much LARGER one
    before the sweep runs -- so "use the persisted budget" and "recompute from
    the config as it is now" actively disagree about whether this record is
    stale, and only the persisted-budget answer recovers it.
    """
    repo = _bg_repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                              "timeout_retries = 0\ndegraded_retries = 0\n")
    db = tmp_path / "s.db"
    small_cfg = load_config(repo)
    rid, _ = _reserve(db, repo)
    with Store.open(db) as st:
        reserved = st.get_review(rid)
    small_budget = reserved["worst_runtime_sec"]
    # The reservation's own arithmetic (`reservation_defaults` takes the MAX
    # of `[defaults]` and `[dispatch]`), not a bare `pipeline.worst_runtime_sec`
    # over `[defaults]` alone -- the unmodified `[dispatch] timeout_sec` (240)
    # is what actually dominates this small config.
    assert small_budget == pipeline.worst_runtime_sec(
        reservation_defaults(small_cfg.defaults, small_cfg.dispatch))

    # The config changes to a much larger timeout BEFORE the sweep runs. The
    # record's own persisted budget must not move with it.
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\ntimeout_sec = 100000\n"
              "timeout_retries = 0\ndegraded_retries = 0\n", encoding="utf-8")
    big_cfg = load_config(repo)
    big_ceiling = pipeline.worst_runtime_sec(
        reservation_defaults(big_cfg.defaults, big_cfg.dispatch))
    assert big_ceiling > small_budget * 100, "fixture no longer discriminates"

    # Backdated just past the SMALL persisted budget -- nowhere near the big
    # recomputed one.
    past = time.strftime(store_mod._TS_FORMAT,
                         time.gmtime(time.time() - small_budget - 5))
    with Store.open(db) as st:
        st.save_review(dict(reserved, reviewed_at=past))

    with Store.open(db) as st:
        swept = pipeline.recover_stale(st, big_cfg)
        rec = st.get_review(rid)
    assert swept == 1, "the persisted budget was ignored in favour of a recompute"
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "stale" in rec["failure_reason"].lower()


def test_a_deleted_branch_does_not_interfere_and_a_cleanly_finishing_superseded_worker_changes_nothing(
        tmp_path, spawned, capsys):
    """Drill 5: a branch deletion dispatched while a review is running must
    leave that review completely alone, and a REAL worker that is superseded
    while genuinely still running -- but never signalled -- must not
    resurrect its record with the ordinary, uncancelled `clean` answer it
    finishes with.

    The deletion half is new coverage: every existing deletion test
    (`test_everything_else_is_skipped_with_a_reason_and_never_a_record`,
    `test_a_config_failure_writes_one_record_per_ACTIONABLE_ref_only`) proves a
    deletion writes no record of ITS OWN, never that a running review of the
    SAME branch survives a deletion dispatched moments later.

    The late-finish half is deliberately NOT driven through a second
    `run_dispatch` call: `test_a_true_zero_delay_race_leaves_one_review_and_
    one_superseded_audit_row` already shows that a REAL racing dispatch
    signals a confirmable worker pid, and the signal turns the loser's own
    completion into a CANCELLED one that `fail_if_running` refuses -- a
    DIFFERENT conditional than `finalize_review`'s. Retiring the row directly
    through `store.reserve_prepush` -- the exact call `run_dispatch` itself
    makes for the decision, only without the dispatcher's own best-effort
    SIGTERM on top of it -- lets this REAL worker process run its review to a
    completely ordinary, uncancelled `clean` finish, so it is `finalize_
    review`'s OWN conditional guard that refuses it, not the cancellation
    path's. This is the drill the brief's second named mutation ("make
    `finalize_review` unconditional") is checked against; the pre-reviewer log
    line is the deterministic point at which the worker is known to have
    already passed its own early "still running" check and be about to invoke
    the (real, detached) provider -- not a sleep of hope.
    """
    repo = _bg_repo(tmp_path, body="sleep 1.2\n" + _emit(CLEAN))
    db = tmp_path / "s.db"
    assert run_dispatch(_push_line(repo), repo, db) == 0
    (rid1,) = _ids(db)
    with Store.open(db) as st:
        assert st.get_review(rid1)["status"] == "running"

    # The remote branch is deleted -- a real pre-push deletion notification --
    # while that review is still in flight.
    deletion = f"(delete) {ZERO} refs/heads/feat {'d' * 40}\n"
    assert run_dispatch(deletion, repo, db) == 0
    assert "deletion" in capsys.readouterr().err
    assert len(spawned) == 1, "the deletion started a worker of its own"
    assert _ids(db) == [rid1], "the deletion wrote a record of its own"
    with Store.open(db) as st:
        assert st.get_review(rid1)["status"] == "running", (
            "an unrelated branch deletion disturbed a running review")

    # Wait for the WORKER's own log to show it is past its early reservation
    # check and about to invoke the (real, detached) provider -- observable
    # state, not a guess about timing.
    log = Path(str(db) + ".logs") / f"{rid1}.log"
    marker = f"as {rid1} ..."
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if log.exists() and marker in log.read_text(encoding="utf-8",
                                                     errors="replace"):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the worker never reached its pre-reviewer "
                             "checkpoint")

    # Retired directly through the reservation lease -- no signal reaches the
    # still-running worker.
    with Store.open(db) as st:
        newer = st.reserve_prepush("feat", "f" * 40, "main", "b" * 40, "h2",
                                   100, _evidence(valid=False))
        assert st.get_review(rid1)["status"] == "superseded"
    assert newer.superseded and newer.superseded[0]["id"] == rid1

    # The real worker was never signalled; let it run all the way to its own
    # ordinary, uncancelled completion.
    spawned[0].wait(timeout=60)
    with Store.open(db) as st:
        rec1 = st.get_review(rid1)
    assert rec1["status"] == "superseded", (
        "the cleanly-finishing superseded worker overwrote a retired record")
    assert rec1["superseded_by"] == newer.record_id
    assert rec1["trustworthy"] is False
    log_text = log.read_text(encoding="utf-8", errors="replace")
    assert "cancelled" not in log_text.lower(), (
        "the worker was signalled -- this drill needs an UNCANCELLED finish "
        "to reach finalize_review's own conditional")
