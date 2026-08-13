"""Adversarial tests for the advisory S7.1 evidence receipt door."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skodun.evidence import (
    EvidenceError,
    EvidenceIdentity,
    ProducerCommand,
    ProducerPolicy,
    parse_receipt,
    receipt_digest,
    verify_receipt,
)
from skodun import mcpserver, services
from skodun.cli import main
from skodun.store import Store


BASE = "a" * 40
HEAD = "b" * 40
DIFF = "c" * 40


def identity() -> EvidenceIdentity:
    return EvidenceIdentity(
        repository_id="github.com/acme/project",
        worktree_root="/tmp/project",
        certification_base=BASE,
        current_head=HEAD,
        diff_hash=DIFF,
        stack_slice_id=None,
    )


def policy() -> ProducerPolicy:
    return ProducerPolicy(
        policy_id="base-policy-v1",
        commands=(ProducerCommand(
            command_id="preflight",
            argv=("python3", "-m", "repo_preflight"),
            cwd=".",
            env_allowlist=("CI",),
        ),),
    )


def receipt_mapping(**overrides):
    value = {
        "schema_version": 1,
        "evidence_kind": "preflight",
        "repository_id": identity().repository_id,
        "worktree_root": identity().worktree_root,
        "certification_base": BASE,
        "current_head": HEAD,
        "diff_hash": DIFF,
        "stack_slice_id": None,
        "producer_policy_id": policy().policy_id,
        "producer_policy_digest": policy().digest,
        "command_id": "preflight",
        "command_digest": policy().commands[0].digest,
        "started_at": "2026-08-13T16:00:00Z",
        "completed_at": "2026-08-13T16:00:02Z",
        "exit_code": 0,
        "terminal_state": "passed",
        "duration_ms": 2000,
        "counters": {"checks": 3},
        "artifact_digests": ["sha256:" + "d" * 64],
        "tool": "repo-preflight/1.0",
        "runtime": "python/3.12",
        "diagnostic_category": "ok",
        "nonce": "run-1",
        "redaction": {
            "applied": True, "secrets_removed": True, "logs_included": False,
        },
    }
    value.update(overrides)
    value["receipt_digest"] = receipt_digest(value)
    return value


def test_canonical_receipt_digest_is_stable_and_excludes_claim():
    raw = receipt_mapping()
    parsed = parse_receipt(json.dumps(raw, ensure_ascii=False))
    assert parsed.receipt_digest == raw["receipt_digest"]
    assert receipt_digest({**raw, "receipt_digest": "sha256:" + "0" * 64}) == \
        raw["receipt_digest"]


@pytest.mark.parametrize("mutator,reason", [
    (lambda r: r.update({"unknown": 1}), "unknown_field"),
    (lambda r: r.update({"duration_ms": 1}), "duration_mismatch"),
    (lambda r: r.update({"redaction": {"applied": False,
                                        "secrets_removed": True,
                                        "logs_included": False}}),
     "redaction_required"),
])
def test_receipt_validation_rejects_unverifiable_shapes(mutator, reason):
    raw = receipt_mapping()
    mutator(raw)
    raw["receipt_digest"] = receipt_digest(raw)
    with pytest.raises(EvidenceError) as exc:
        parse_receipt(json.dumps(raw))
    assert exc.value.reason_code == reason


def test_duplicate_json_keys_and_nonfinite_numbers_fail_closed():
    with pytest.raises(EvidenceError) as duplicate:
        parse_receipt('{"schema_version":1,"schema_version":1}')
    assert duplicate.value.reason_code == "duplicate_key"
    raw = receipt_mapping()
    raw["counters"] = {"checks": float("nan")}
    with pytest.raises(EvidenceError) as nonfinite:
        parse_receipt(json.dumps(raw, allow_nan=True))
    assert nonfinite.value.reason_code == "malformed_json"


def test_identity_and_protected_policy_mismatches_never_verify():
    receipt = parse_receipt(json.dumps(receipt_mapping()))
    mismatch = verify_receipt(
        receipt,
        EvidenceIdentity(**{**identity().__dict__, "current_head": "e" * 40}),
        policy(),
    )
    assert mismatch.accepted is False
    assert mismatch.reason_code == "head_mismatch"

    candidate = ProducerPolicy(
        policy_id="candidate-policy",
        commands=(ProducerCommand("preflight", ("python3", "-m", "candidate"), ".", ()),),
    )
    forged = parse_receipt(json.dumps(receipt_mapping(
        producer_policy_id=candidate.policy_id,
        producer_policy_digest=candidate.digest,
        command_digest=candidate.commands[0].digest,
    )))
    result = verify_receipt(forged, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "policy_mismatch"


def test_receipt_file_rejects_symlink_and_oversize(tmp_path: Path):
    from skodun.evidence import load_receipt_file, MAX_RECEIPT_BYTES

    target = tmp_path / "receipt.json"
    target.write_text(json.dumps(receipt_mapping()), encoding="utf-8")
    assert load_receipt_file(target).receipt_digest == receipt_mapping()["receipt_digest"]
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(EvidenceError) as symlink:
        load_receipt_file(link)
    assert symlink.value.reason_code == "unsafe_file"
    target.write_bytes(b"x" * (MAX_RECEIPT_BYTES + 1))
    with pytest.raises(EvidenceError) as large:
        load_receipt_file(target)
    assert large.value.reason_code == "too_large"


def test_store_receipt_ingestion_is_idempotent_and_nonce_conflicts(tmp_path):
    raw = receipt_mapping()
    parsed = parse_receipt(json.dumps(raw))
    verified = verify_receipt(parsed, identity(), policy())
    assert verified.accepted is True
    with Store.open(tmp_path / "store.db") as store:
        first = store.save_evidence_receipt(
            identity().digest, parsed.receipt_digest, parsed.nonce,
            verified.status, verified.reason_code, parsed.canonical_json,
            "2026-08-13T16:00:03Z", parsed.evidence_kind,
            parsed.terminal_state)
        second = store.save_evidence_receipt(
            identity().digest, parsed.receipt_digest, parsed.nonce,
            verified.status, verified.reason_code, parsed.canonical_json,
            "2026-08-13T16:00:04Z", parsed.evidence_kind,
            parsed.terminal_state)
        assert first["status"] == "accepted"
        assert second["status"] == "duplicate"
        conflict = store.save_evidence_receipt(
            identity().digest, "sha256:" + "e" * 64, parsed.nonce,
            "accepted", "ok", parsed.canonical_json,
            "2026-08-13T16:00:05Z", parsed.evidence_kind,
            parsed.terminal_state)
        assert conflict["status"] == "conflict"
        rows = store.list_evidence_receipts(identity().digest, 32)
        assert len(rows) == 1
        assert rows[0]["receipt_digest"] == parsed.receipt_digest


def test_cli_mcp_and_service_json_projections_are_identical(tmp_path, monkeypatch,
                                                            capsys):
    raw = receipt_mapping()
    parsed = parse_receipt(json.dumps(raw))
    with Store.open(tmp_path / "store.db") as store:
        store.save_evidence_receipt(
            identity().digest, parsed.receipt_digest, parsed.nonce,
            "accepted", "ok", parsed.canonical_json,
            "2026-08-13T16:00:03Z", parsed.evidence_kind,
            parsed.terminal_state)
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store.db"))
    with Store.open(tmp_path / "store.db") as store:
        code, service_json = services.svc_evidence_summary(
            store, identity().digest, output="json")
    assert code == 0
    assert main(["evidence", identity().digest, "--json"]) == 0
    cli_json = capsys.readouterr().out.strip()
    call = mcpserver.HandlerCall(
        params={"identity_digest": identity().digest, "output": "json"},
        store_factory=lambda: Store.open(tmp_path / "store.db"),
        cancel=__import__("threading").Event())
    mcp_result = mcpserver._handle_evidence(call)
    assert mcp_result.status == 0
    assert cli_json == service_json == mcp_result.text
