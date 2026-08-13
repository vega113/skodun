"""Adversarial tests for the advisory S7.1 evidence receipt door."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from skodun.evidence import (
    EvidenceError,
    EvidenceIdentity,
    ProducerCommand,
    ProducerPolicy,
    parse_receipt,
    producer_proof,
    receipt_digest,
    verify_receipt,
)
from skodun import mcpserver, services
from skodun.cli import main
from tests.test_mcptools import _HANDSHAKE, _Recorder, _rpc, _serve
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
        provenance_key=b"test-provenance-key-1234",
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
        "producer_proof": "sha256:" + "0" * 64,
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
    value["producer_proof"] = producer_proof(value, policy().provenance_key)
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


def test_schema_version_bool_and_oversized_integer_fail_as_malformed_input(
        monkeypatch):
    raw = receipt_mapping(schema_version=True)
    raw["receipt_digest"] = receipt_digest(raw)
    with pytest.raises(EvidenceError) as boolean_version:
        parse_receipt(json.dumps(raw))
    assert boolean_version.value.reason_code == "invalid_field"

    with pytest.raises(EvidenceError) as oversized:
        parse_receipt('{"schema_version":' + "9" * 5000 + "}")
    assert oversized.value.reason_code == "malformed_json"

    def raise_recursion(*_args, **_kwargs):
        raise RecursionError("too deeply nested")

    monkeypatch.setattr(json, "loads", raise_recursion)
    with pytest.raises(EvidenceError) as recursive:
        parse_receipt("{}")
    assert recursive.value.reason_code == "malformed_json"


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
        provenance_key=b"candidate-provenance-key",
    )
    forged = parse_receipt(json.dumps(receipt_mapping(
        producer_policy_id=candidate.policy_id,
        producer_policy_digest=candidate.digest,
        command_digest=candidate.commands[0].digest,
    )))
    result = verify_receipt(forged, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "policy_mismatch"

    forged_kind = parse_receipt(json.dumps(receipt_mapping(
        evidence_kind="full_gate")))
    result = verify_receipt(forged_kind, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "evidence_kind_mismatch"

    forged_proof = parse_receipt(json.dumps(receipt_mapping(
        counters={"checks": 4})))
    result = verify_receipt(forged_proof, identity(),
                            ProducerPolicy(policy().policy_id,
                                           policy().commands,
                                           b"different-provenance-key"))
    assert result.accepted is False
    assert result.reason_code == "policy_mismatch"


@pytest.mark.parametrize("argv", [
    ("bash", "-lc", "echo unsafe"),
    ("sh", "-xc", "echo unsafe"),
    ("pwsh", "-Command:Write-Host", "unsafe"),
    ("python", "-cprint('unsafe')"),
])
def test_producer_policy_rejects_combined_command_string_flags(argv):
    with pytest.raises(EvidenceError) as exc:
        ProducerCommand("unsafe", argv, ".", ())
    assert exc.value.reason_code == "invalid_command"


@pytest.mark.parametrize("argv", [
    ("cmd", "/c", "echo unsafe"),
    ("perl", "-e", "print unsafe"),
    ("powershell", "-EncodedCommand", "unsafe"),
    ("perl", "-eprint(1)"),
    ("perl", "-E", "say 1"),
    ("ruby", "-eputs(1)"),
    ("node", "-p", "1+1"),
    ("node", "--conditions", "development", "-e", "1+1"),
    ("node", "--import", "data:text/javascript,console.log(1)", "script.js"),
    ("bash", "-O", "extglob", "-c", "echo unsafe"),
    ("bash", "-o", "pipefail", "-c", "echo unsafe"),
    ("sh", "-o", "errexit", "-c", "echo unsafe"),
    ("dash", "-o", "errexit", "-c", "echo unsafe"),
    ("fish", "-C", "echo unsafe"),
    ("fish", "--init-command=echo unsafe"),
    ("python3", "--check-hash-based-pycs", "always", "-c", "print(1)"),
    ("ruby", "-C", "/tmp", "-e", "puts 1"),
    ("ruby", "-E", "UTF-8", "-e", "puts 1"),
])
def test_each_interpreter_command_string_flag_is_rejected(argv):
    with pytest.raises(EvidenceError):
        ProducerCommand("unsafe", argv, ".", ())


@pytest.mark.parametrize("argv", [
    ("/usr/bin/env", "bash", "-c", "echo unsafe"),
    ("/usr/bin/env", "-u", "LD_PRELOAD", "bash", "-c", "echo unsafe"),
    ("env", "-", "bash", "-c", "echo unsafe"),
    ("env", "--", "bash", "-c", "echo unsafe"),
    ("env", "-S", "bash -c echo unsafe"),
    ("env", "env", "bash", "-c", "echo unsafe"),
    ("env", "API_TOKEN=plaintext", "python3", "check.py"),
    ("env", "LD_PRELOAD=/tmp/candidate.so", "python3", "check.py"),
    ("env", "--default-signal", "TERM", "bash", "-c", "echo unsafe"),
    ("env", "-v", "bash", "-c", "echo unsafe"),
    ("busybox", "sh", "-c", "echo unsafe"),
    ("timeout", "10", "bash", "-c", "echo unsafe"),
    ("bash.exe", "-c", "echo unsafe"),
    ("cmd.exe", "/c", "echo unsafe"),
])
def test_wrapped_interpreter_command_string_flags_are_rejected(argv):
    with pytest.raises(EvidenceError):
        ProducerCommand("unsafe", argv, ".", ())


def test_interpreter_like_arguments_are_not_scanned_as_executables():
    ProducerCommand("valid", ("/usr/bin/printf", "%s", "bash", "-c"), ".", ())
    ProducerCommand("valid-python", ("python3.12", "script.py", "-c"), ".", ())
    ProducerCommand("valid-bash", ("bash", "script.sh", "-c"), ".", ())


@pytest.mark.parametrize("argv", [
    ("cc", "-c", "check.c"),
    ("git", "-c", "core.pager=cat", "status"),
])
def test_non_interpreter_command_flags_remain_valid(argv):
    ProducerCommand("valid", argv, ".", ())


def test_case_sensitive_interpreter_options_remain_valid():
    ProducerCommand("ruby-valid", ("ruby", "-E", "UTF-8", "script.rb"), ".", ())
    ProducerCommand("bash-valid", ("bash", "-C", "script.sh"), ".", ())


def test_receipt_metadata_is_bounded_to_identifiers():
    for field in ("tool", "runtime"):
        raw = receipt_mapping(**{field: "token=plaintext"})
        with pytest.raises(EvidenceError) as exc:
            parse_receipt(json.dumps(raw))
        assert exc.value.reason_code == "invalid_field"

    raw = receipt_mapping(counters={"API_TOKEN=secret": 1})
    with pytest.raises(EvidenceError) as exc:
        parse_receipt(json.dumps(raw))
    assert exc.value.reason_code == "invalid_field"


@pytest.mark.parametrize("cwd", ["..\\outside", "C:\\Windows", "\\\\server\\share"])
def test_windows_policy_working_directories_are_rejected(cwd):
    with pytest.raises(EvidenceError):
        ProducerCommand("unsafe", ("python3", "-m", "repo"), cwd, ())


@pytest.mark.parametrize("diagnostic", ["failed", "mismatch", "unverifiable"])
def test_non_ok_diagnostics_never_verify(diagnostic):
    receipt = parse_receipt(json.dumps(receipt_mapping(
        diagnostic_category=diagnostic)))
    result = verify_receipt(receipt, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "diagnostic_mismatch"


def test_signal_exit_codes_are_queryable_rejected_evidence():
    receipt = parse_receipt(json.dumps(receipt_mapping(
        exit_code=-15, terminal_state="failed",
        diagnostic_category="ok")))
    result = verify_receipt(receipt, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "producer_failed"


def test_service_rejects_controlled_identity_digest(tmp_path):
    with Store.open(tmp_path / "store.db") as store:
        code, message = services.svc_evidence_summary(
            store, "sha256:" + "a" * 63 + "\nforged", output="text")
    assert code == 2
    assert message.endswith("identity digest is invalid")


def test_receipt_nested_counters_are_immutable_and_invalid_constructed_receipts_reject():
    parsed = parse_receipt(json.dumps(receipt_mapping()))
    with pytest.raises(TypeError):
        parsed.counters["checks"] = 4
    assert verify_receipt(parsed, identity(), policy()).accepted is True

    forged = replace(parsed, schema_version=True)
    result = verify_receipt(forged, identity(), policy())
    assert result.accepted is False
    assert result.reason_code == "invalid_field"


def test_mapping_proxy_inputs_are_copied_before_freezing():
    parsed = parse_receipt(json.dumps(receipt_mapping()))
    counters = {"checks": 3}
    redaction = {"applied": True, "secrets_removed": True,
                 "logs_included": False}
    forged = replace(parsed, counters=MappingProxyType(counters),
                     redaction=MappingProxyType(redaction))
    counters["checks"] = 99
    redaction["applied"] = False
    assert dict(forged.counters) == {"checks": 3}
    assert dict(forged.redaction)["applied"] is True


def test_directly_constructed_artifact_digests_are_immutable():
    parsed = parse_receipt(json.dumps(receipt_mapping()))
    forged = replace(parsed, artifact_digests=list(parsed.artifact_digests))
    assert isinstance(forged.artifact_digests, tuple)
    assert verify_receipt(forged, identity(), policy()).accepted is True


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
            identity(), policy(), parsed.canonical_json,
            "2026-08-13T16:00:03Z")
        second = store.save_evidence_receipt(
            identity(), policy(), parsed.canonical_json,
            "2026-08-13T16:00:04Z")
        assert first["status"] == "accepted"
        assert second["status"] == "duplicate"
        conflicting = parse_receipt(json.dumps(
            receipt_mapping(counters={"checks": 4})))
        conflict = store.save_evidence_receipt(
            identity(), policy(), conflicting.canonical_json,
            "2026-08-13T16:00:05Z")
        assert conflict["status"] == "conflict"
        rows = store.list_evidence_receipts(identity().digest, 32)
        assert len(rows) == 2
        assert rows[0]["status"] == "conflict"
        assert rows[1]["receipt_digest"] == parsed.receipt_digest


def test_store_derives_rejection_and_keeps_identity_mismatches_queryable(tmp_path):
    candidate = ProducerPolicy(
        policy_id="candidate-policy",
        commands=(ProducerCommand("preflight", ("python3", "-m", "candidate"), ".", ()),),
        provenance_key=b"candidate-provenance-key",
    )
    forged = parse_receipt(json.dumps(receipt_mapping(
        producer_policy_id=candidate.policy_id,
        producer_policy_digest=candidate.digest,
        command_digest=candidate.commands[0].digest,
        nonce="candidate-policy-run")))
    stale = parse_receipt(json.dumps(receipt_mapping(
        current_head="e" * 40, nonce="stale-run")))
    with Store.open(tmp_path / "store.db") as store:
        policy_row = store.save_evidence_receipt(
            identity(), policy(), forged.canonical_json,
            "2026-08-13T16:00:03Z")
        stale_row = store.save_evidence_receipt(
            identity(), policy(), stale.canonical_json,
            "2026-08-13T16:00:04Z")
        assert policy_row == {
            "status": "rejected", "receipt_digest": forged.receipt_digest,
            "reason_code": "policy_mismatch",
        }
        assert stale_row["status"] == "rejected"
        assert stale_row["reason_code"] == "head_mismatch"
        rows = store.list_evidence_receipts(identity().digest, 32)
        assert {row["reason_code"] for row in rows} == {
            "policy_mismatch", "head_mismatch",
        }


def test_store_rejects_invalid_utf8_text_before_size_check(tmp_path):
    with Store.open(tmp_path / "store.db") as store:
        with pytest.raises(ValueError, match="not UTF-8"):
            store.save_evidence_receipt(
                identity(), policy(), "\ud800", "2026-08-13T16:00:03Z")


def test_cli_mcp_and_service_json_projections_are_identical(tmp_path, monkeypatch,
                                                            capsys):
    raw = receipt_mapping()
    parsed = parse_receipt(json.dumps(raw))
    with Store.open(tmp_path / "store.db") as store:
        store.save_evidence_receipt(
            identity(), policy(), parsed.canonical_json,
            "2026-08-13T16:00:03Z")
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store.db"))
    with Store.open(tmp_path / "store.db") as store:
        code, service_json = services.svc_evidence_summary(
            store, identity().digest, output="json")
    assert code == 0
    assert main(["evidence", identity().digest, "--json"]) == 0
    cli_json = capsys.readouterr().out.strip()
    out = _Recorder()
    assert _serve(
        tmp_path / "store.db",
        _HANDSHAKE + _rpc(
            "tools/call", 1, name="evidence",
            arguments={"identity_digest": identity().digest, "output": "json"}),
        out) == 0
    mcp_body = json.loads(out.data.decode("utf-8").splitlines()[1])
    assert mcp_body["result"]["isError"] is False
    mcp_json = mcp_body["result"]["content"][0]["text"]
    assert cli_json == service_json == mcp_json
