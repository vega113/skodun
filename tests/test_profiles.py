"""Hermetic tests for S7.3 capability profiles and repository receipts."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from skodun.evidence import EvidenceIdentity, ProducerCommand, ProducerPolicy
from skodun import pipeline
from skodun.profiles import (
    FixtureExpectation,
    LanguageCapabilityProfile,
    ProfileError,
    adapt_ci_receipt,
    adapt_local_receipt,
    adapt_mutation_log,
    adapt_review_threads,
    compact_receipt_context,
    compact_stored_receipt_context,
    run_profile,
    scala3_profile,
)


HEAD = "b" * 40
BASE = "a" * 40
DIFF = "c" * 40


def identity(head: str = HEAD) -> EvidenceIdentity:
    return EvidenceIdentity(
        repository_id="github.com/acme/project",
        worktree_root=str(Path(__file__).resolve().parents[1]),
        certification_base=BASE,
        current_head=head,
        diff_hash=DIFF,
    )


def _tool(tmp_path: Path) -> Path:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(
        """import pathlib, sys, os
mode = sys.argv[1]
if mode == 'version':
    if os.environ.get('PROFILE_MODE') == 'slow':
        import time
        time.sleep(2)
    if os.environ.get('PROFILE_MODE') == 'flood':
        print('x' * 10000)
        raise SystemExit(0)
    print('Scala 3.3.1 hermetic-fixture-tool')
    raise SystemExit(0)
path = pathlib.Path(sys.argv[2])
text = path.read_text(encoding='utf-8')
invalid = 'SKODUN_INVALID_FIXTURE' in text
if mode == 'compile':
    print('COMPILE_REJECTED' if invalid else 'COMPILE_OK')
    raise SystemExit(2 if invalid else 0)
if mode == 'harness':
    print('HARNESS_REJECTED' if invalid else 'HARNESS_OK')
    raise SystemExit(2 if invalid else 0)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    return tool


def _policy(tool: Path, *, version: str = "3.3.1") -> ProducerPolicy:
    del version
    common = (sys.executable, str(tool))
    commands = [
        ProducerCommand("scala_version", common + ("version",), ".", ("PROFILE_MODE",)),
        ProducerCommand("scala_compile", common + ("compile", "{fixture}"), ".", ()),
        ProducerCommand("scala_harness", common + ("harness", "{fixture}"), ".", ()),
        ProducerCommand("scala_symbols", common + ("harness", "{fixture}"), ".", ()),
        ProducerCommand("scala_locator", common + ("harness", "{fixture}"), ".", ()),
        ProducerCommand("scala_mutation", common + ("harness", "{fixture}"), ".", (), "mutation"),
    ]
    return ProducerPolicy("scala-fixture-policy", tuple(commands), b"profile-proof-key-1234")


def _profile(*, fixture: str = "fixture.scala", version_prefix: str = "Scala 3.3"):
    return LanguageCapabilityProfile(
        profile_id="scala3-pilot",
        language="scala",
        version="3.3",
        version_command_id="scala_version",
        compile_command_id="scala_compile",
        harness_command_id="scala_harness",
        symbol_query_command_id="scala_symbols",
        locator_command_id="scala_locator",
        mutation_command_id="scala_mutation",
        capabilities=("version_discovery", "syntax_compile", "fixture_harness",
                      "symbol_query", "mutation_locator", "mutation_execution"),
        fixtures=(FixtureExpectation(fixture, "accepted", "COMPILE_OK"),),
        version_prefix=version_prefix,
        timeout_sec=5,
        max_output_bytes=4096,
    )


def test_profile_validation_requires_capability_command_mapping():
    with pytest.raises(ProfileError, match="compile_command_id"):
        LanguageCapabilityProfile(
            profile_id="scala3", language="scala", version="3",
            version_command_id="version", compile_command_id=None,
            harness_command_id=None, capabilities=("syntax_compile",),
            fixtures=(),
        )

    with pytest.raises(ProfileError, match="fixture path"):
        FixtureExpectation("../secret.scala", "accepted", "OK")
    with pytest.raises(ProfileError, match="compile/harness"):
        LanguageCapabilityProfile(
            profile_id="version-only", language="scala", version="3",
            version_command_id="version", compile_command_id=None,
            harness_command_id=None, capabilities=("version_discovery",),
            fixtures=(FixtureExpectation("fixture.scala", "accepted", "OK"),),
        )


def test_scala_pilot_advertises_capabilities_without_parser_claim():
    profile = scala3_profile()
    assert "syntax_compile" in profile.capabilities
    assert "parser" not in profile.capabilities
    assert profile.language == "scala"
    assert len(profile.fixtures) >= 8


def test_profile_runs_version_compile_and_harness_with_digest_only_output(tmp_path):
    tool = _tool(tmp_path)
    fixture = tmp_path / "fixture.scala"
    fixture.write_text("object Valid { val π = 'x' }\n", encoding="utf-8")
    result = run_profile(_profile(), _policy(tool), tmp_path)
    assert result.accepted is True
    assert result.reason_code == "ok"
    assert result.version == "Scala 3.3.1 hermetic-fixture-tool"
    assert result.raw_output == ()
    assert all("output_digest" in check for check in result.checks)


def test_invalid_fixture_is_rejected_before_profile_is_available(tmp_path):
    tool = _tool(tmp_path)
    fixture = tmp_path / "fixture.scala"
    fixture.write_text("// SKODUN_INVALID_FIXTURE\n", encoding="utf-8")
    result = run_profile(_profile(), _policy(tool), tmp_path)
    assert result.accepted is False
    assert result.reason_code == "invalid_fixture"


def test_missing_command_and_version_mismatch_are_stable_unavailable_reasons(tmp_path):
    fixture = tmp_path / "fixture.scala"
    fixture.write_text("object Valid {}\n", encoding="utf-8")
    missing = run_profile(_profile(), _policy(tmp_path / "missing.py"), tmp_path)
    assert missing.accepted is False
    assert missing.reason_code == "command_missing"
    tool = _tool(tmp_path)
    mismatch = run_profile(_profile(version_prefix="Scala 4"), _policy(tool), tmp_path)
    assert mismatch.accepted is False
    assert mismatch.reason_code == "version_mismatch"


def test_profile_rejects_unsafe_fixture_and_timeout_without_acceptance(tmp_path):
    tool = _tool(tmp_path)
    link = tmp_path / "fixture.scala"
    target = tmp_path / "target.scala"
    target.write_text("object Valid {}\n", encoding="utf-8")
    link.symlink_to(target)
    result = run_profile(_profile(), _policy(tool), tmp_path)
    assert result.accepted is False
    assert result.reason_code == "unsafe_fixture"


def test_profile_maps_watchdog_timeout_and_output_limit_to_stable_reasons(
        tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    fixture = tmp_path / "fixture.scala"
    fixture.write_text("object Valid {}\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_MODE", "slow")
    timed = run_profile(replace(_profile(), timeout_sec=1), _policy(tool), tmp_path)
    assert timed.accepted is False
    assert timed.reason_code == "timeout"
    monkeypatch.setenv("PROFILE_MODE", "flood")
    limited = run_profile(replace(_profile(), max_output_bytes=128),
                           _policy(tool), tmp_path)
    assert limited.accepted is False
    assert limited.reason_code == "output_limit_exceeded"


def _local_payload(head: str = HEAD, kind: str = "preflight"):
    return {
        "current_head": head,
        "repository_id": "github.com/acme/project",
        "worktree_root": identity().worktree_root,
        "certification_base": BASE,
        "diff_hash": DIFF,
        "evidence_kind": kind,
        "terminal_state": "passed",
        "exit_code": 0,
        "receipt_digest": "sha256:" + "d" * 64,
    }


def test_repository_receipts_bind_exact_head_and_strip_logs():
    local = adapt_local_receipt(_local_payload(), identity(), "preflight")
    assert local.status == "passed"
    assert "logs" not in local.to_mapping()
    with pytest.raises(ProfileError, match="head_mismatch"):
        adapt_local_receipt(_local_payload("e" * 40), identity(), "preflight")

    mutation = adapt_mutation_log({
        "current_head": HEAD, "mutation_id": "m-1",
        "compile_validity": {"status": "passed"},
        "restore_status": "restored", "cleanup_status": "clean",
        "compiler_valid": True, "old_fails_new_passes": True,
        "logs": "secret",
    }, identity())
    assert mutation.status == "passed"
    assert "logs" not in mutation.to_mapping()

    ci = adapt_ci_receipt({"run_id": "run-1", "conclusion": "success",
                           "head_sha": HEAD, "logs": "ignored"}, identity())
    threads = adapt_review_threads({"snapshot_id": "snap-1", "head_sha": HEAD,
                                    "unresolved": 0}, identity())
    assert ci.status == threads.status == "passed"


def test_repository_receipt_adapters_fail_closed_on_stale_or_invalid_lifecycle():
    with pytest.raises(ProfileError, match="head_mismatch"):
        adapt_ci_receipt({"run_id": "run", "conclusion": "success",
                          "head_sha": "e" * 40}, identity())
    with pytest.raises(ProfileError, match="unresolved"):
        adapt_review_threads({"snapshot_id": "snap", "head_sha": HEAD,
                              "unresolved": -1}, identity())
    failed = adapt_ci_receipt({"run_id": "run", "conclusion": "failure",
                               "head_sha": HEAD}, identity())
    assert failed.status == "failed"
    with pytest.raises(ProfileError, match="exit_code"):
        adapt_local_receipt({**_local_payload(), "exit_code": False},
                            identity(), "preflight")


def test_compact_receipt_context_is_deterministic_and_materially_bounded():
    receipts = [
        adapt_ci_receipt({"run_id": f"run-{i}", "conclusion": "success",
                          "head_sha": HEAD}, identity())
        for i in range(40)
    ]
    compact = compact_receipt_context(receipts, max_items=8, max_bytes=2048)
    assert len(compact.encode("utf-8")) <= 2048
    assert compact == compact_receipt_context(receipts, max_items=8, max_bytes=2048)
    assert len(compact) < len("x" * 127_000)
    payload = json.loads(compact)
    assert payload["receipts"]
    assert payload["truncated"] is True


def test_stored_receipt_context_is_redacted_and_ready_for_the_review_prompt():
    rows = [{
        "receipt_digest": "sha256:" + "d" * 64,
        "nonce": "run-1", "status": "accepted", "reason_code": "ok",
        "evidence_kind": "preflight", "terminal_state": "passed",
        "ingested_at": "2026-08-14T00:00:00Z", "logs": "secret",
    }]
    rendered = compact_stored_receipt_context(rows, "sha256:" + "e" * 64)
    assert b"BEGIN REPOSITORY EVIDENCE" in rendered
    assert b"logs" not in rendered
    assert len(rendered) < 127_000


def test_shared_review_pipeline_includes_exact_identity_receipt_context(monkeypatch):
    class StoreProjection:
        def list_evidence_receipts(self, identity_digest, limit):
            assert identity_digest.startswith("sha256:")
            assert limit == 32
            return [{
                "receipt_digest": "sha256:" + "d" * 64,
                "nonce": "run-1", "status": "accepted", "reason_code": "ok",
                "evidence_kind": "preflight", "terminal_state": "passed",
                "ingested_at": "2026-08-14T00:00:00Z",
            }]

    monkeypatch.setattr(pipeline.gitio, "canonical_repository_identity",
                        lambda _root: "github.com/acme/project")
    context = pipeline._evidence_prompt_context(
        StoreProjection(), Path("/tmp/project"), BASE, HEAD, DIFF)
    assert b"BEGIN REPOSITORY EVIDENCE" in context
    assert b"sha256:" + b"d" * 64 in context
