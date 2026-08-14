"""Shipped-path tests for compiler-valid, non-vacuous mutation proofs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from skodun.evidence import (EvidenceIdentity, ProducerCommand, ProducerPolicy,
                             parse_receipt, verify_receipt)
from skodun.mutation import (MutationError, MutationSpec, parse_mutation_proof,
                             run_mutation)
from skodun.store import Store


BASE = "a" * 40
HEAD = "b" * 40
DIFF = "c" * 40


def identity(root: Path | None = None) -> EvidenceIdentity:
    return EvidenceIdentity("github.com/acme/project",
                            str(root or Path("/tmp/project")),
                            BASE, HEAD, DIFF)


def make_policy(root: Path) -> ProducerPolicy:
    return ProducerPolicy(
        "mutation-policy-v1",
        commands=(
            ProducerCommand("compile", ("python3", "-m", "py_compile", "target.py"), ".", (), "mutation"),
            ProducerCommand("baseline", ("python3", "target.py"), ".", (), "mutation"),
            ProducerCommand("positive", ("python3", "positive.py"), ".", (), "mutation"),
            ProducerCommand("negative", ("python3", "negative.py"), ".", (), "mutation"),
            ProducerCommand("mutant", ("python3", "target.py"), ".", (), "mutation"),
        ),
        provenance_key=b"mutation-provenance-key",
    )


def make_fixture(root: Path) -> None:
    (root / "target.py").write_text(
        "print('MUTATION_SENTINEL')\nraise SystemExit(1)\n", encoding="utf-8")
    (root / "positive.py").write_text(
        "print('MUTATION_SENTINEL positive')\n", encoding="utf-8")
    (root / "negative.py").write_text(
        "print('MUTATION_SENTINEL negative')\nraise SystemExit(1)\n",
        encoding="utf-8")


def spec() -> MutationSpec:
    return MutationSpec(
        mutation_id="mutation-1",
        mutation_type="value_boundary",
        target_file="target.py",
        anchor="SystemExit",
        old_bytes=b"raise SystemExit(1)",
        new_bytes=b"raise SystemExit(0)",
        expected_match_count=1,
        fixture_file="target.py",
        compiler_command_id="compile",
        baseline_command_id="baseline",
        positive_command_id="positive",
        negative_command_id="negative",
        mutant_command_id="mutant",
        sentinel="MUTATION_SENTINEL",
    )


def test_valid_mutation_proves_old_fail_new_pass_and_restores_tree(tmp_path):
    make_fixture(tmp_path)
    before = (tmp_path / "target.py").read_bytes()
    execution = run_mutation(spec(), root=tmp_path, identity=identity(tmp_path),
                             policy=make_policy(tmp_path))

    assert execution.accepted is True
    assert execution.reason_code == "ok"
    proof = parse_mutation_proof(execution.proof)
    assert proof.old_fails_new_passes is True
    assert proof.baseline_result["exit_code"] == 1
    assert proof.mutant_result["exit_code"] == 0
    assert proof.controls["positive"]["marker_seen"] is True
    assert proof.controls["negative"]["marker_seen"] is True
    assert proof.restore_status == "restored"
    assert proof.initial_tree_digest == proof.final_tree_digest
    assert (tmp_path / "target.py").read_bytes() == before

    receipt = parse_receipt(json.dumps(execution.receipt_mapping))
    assert verify_receipt(receipt, identity(tmp_path), make_policy(tmp_path)).accepted
    assert receipt.evidence_kind == "mutation"
    with Store.open(tmp_path / "store.db") as store:
        stored = store.save_evidence_receipt(
            identity(tmp_path), make_policy(tmp_path),
            json.dumps(execution.receipt_mapping, sort_keys=True,
                       separators=(",", ":")),
            "2026-08-14T00:00:00Z")
        assert stored["status"] == "accepted"
        assert store.list_evidence_receipts(identity(tmp_path).digest)[0][
            "evidence_kind"] == "mutation"


@pytest.mark.parametrize(("change", "expected_reason"), [
    ({"old_bytes": b"not present"}, "target_missing"),
    ({"old_bytes": b"print", "expected_match_count": 2}, "target_cardinality"),
    ({"old_bytes": b"raise SystemExit(1)",
      "new_bytes": b"raise SystemExit(1)"}, "no_op"),
])
def test_vacuous_target_selection_is_rejected(tmp_path, change, expected_reason):
    make_fixture(tmp_path)
    values = {**spec().__dict__, **change}
    with pytest.raises(MutationError) as exc:
        run_mutation(MutationSpec(**values), root=tmp_path,
                     identity=identity(tmp_path), policy=make_policy(tmp_path))
    assert exc.value.reason_code == expected_reason
    assert (tmp_path / "target.py").read_text(encoding="utf-8").endswith(
        "raise SystemExit(1)\n")


def test_invalid_mutant_is_rejected_as_compiler_invalid_and_restored(tmp_path):
    make_fixture(tmp_path)
    invalid = MutationSpec(**{**spec().__dict__, "new_bytes": b"raise ("})
    execution = run_mutation(invalid, root=tmp_path, identity=identity(tmp_path),
                             policy=make_policy(tmp_path))
    assert execution.accepted is False
    assert execution.reason_code == "compiler_invalid"
    assert parse_mutation_proof(execution.proof).restore_status == "restored"


def test_compiler_timeout_is_incomplete_and_restores_owned_mutation(tmp_path):
    make_fixture(tmp_path)
    (tmp_path / "slow_compile.py").write_text(
        "import time\ntime.sleep(2)\n", encoding="utf-8")
    base_policy = make_policy(tmp_path)
    commands = list(base_policy.commands)
    commands[0] = replace(commands[0],
                          argv=("python3", "slow_compile.py", "target.py"))
    policy = ProducerPolicy(base_policy.policy_id, tuple(commands),
                            base_policy.provenance_key)
    execution = run_mutation(
        replace(spec(), timeout_sec=0.1), root=tmp_path,
        identity=identity(tmp_path), policy=policy)
    assert execution.accepted is False
    assert execution.reason_code == "timeout"
    proof = parse_mutation_proof(execution.proof)
    assert proof.mapping["compile_validity"]["status"] == "not_run"
    assert proof.restore_status == "restored"


def test_missing_fixture_and_undeclared_command_fail_closed(tmp_path):
    make_fixture(tmp_path)
    missing_fixture = MutationSpec(**{**spec().__dict__,
                                      "fixture_file": "missing.py"})
    with pytest.raises(MutationError) as fixture:
        run_mutation(missing_fixture, root=tmp_path, identity=identity(tmp_path),
                     policy=make_policy(tmp_path))
    assert fixture.value.reason_code == "fixture_missing"

    missing_command = MutationSpec(**{**spec().__dict__,
                                      "compiler_command_id": "not-declared"})
    with pytest.raises(MutationError) as command:
        run_mutation(missing_command, root=tmp_path, identity=identity(tmp_path),
                     policy=make_policy(tmp_path))
    assert command.value.reason_code == "command_undeclared"


def test_mutation_proof_rejects_unknown_fields_and_changed_tree(tmp_path):
    make_fixture(tmp_path)
    execution = run_mutation(spec(), root=tmp_path, identity=identity(tmp_path),
                             policy=make_policy(tmp_path))
    forged = dict(execution.proof)
    forged["unknown"] = True
    with pytest.raises(MutationError) as unknown:
        parse_mutation_proof(forged)
    assert unknown.value.reason_code == "unknown_field"


def test_changed_final_tree_is_rejected(tmp_path):
    make_fixture(tmp_path)
    (tmp_path / "positive.py").write_text(
        "from pathlib import Path\n"
        "Path('unrelated.txt').write_text('changed')\n"
        "print('MUTATION_SENTINEL positive')\n", encoding="utf-8")
    execution = run_mutation(spec(), root=tmp_path,
                             identity=identity(tmp_path),
                             policy=make_policy(tmp_path))
    assert execution.accepted is False
    assert execution.reason_code == "final_tree_changed"


def test_worktree_identity_must_match_root(tmp_path):
    make_fixture(tmp_path)
    with pytest.raises(MutationError) as exc:
        run_mutation(spec(), root=tmp_path, identity=identity(),
                     policy=make_policy(tmp_path))
    assert exc.value.reason_code == "worktree_mismatch"
