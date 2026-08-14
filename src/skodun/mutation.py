"""Fail-closed, compiler-backed mutation proof receipts.

This module proves that one declared byte mutation selected exactly one target,
ran protected compiler/harness commands, exercised positive and negative
controls, failed as a deliberate mutant, and restored the worktree.  The
result is advisory evidence only; it never changes review trust or gate state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .evidence import (EvidenceIdentity, ProducerPolicy, producer_proof,
                       receipt_digest)
from .runner import ReviewCancelled, RunResult, run_with_watchdog


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}")
_PATH_PART = re.compile(r"[A-Za-z0-9._+-]{1,128}")
_MUTATION_TYPES = frozenset({
    "value_boundary", "symbol_shadowing", "deletion_omission",
    "fixture_substitution", "transport_sentinel", "unknown",
})
_PROOF_FIELDS = frozenset({
    "schema_version", "mutation_id", "mutation_type", "target_file",
    "anchor", "preimage_hash", "expected_match_count",
    "mutant_content_digest", "fixture_file", "compiler_command_id",
    "baseline_command_id", "positive_command_id", "negative_command_id",
    "mutant_command_id",
    "command_exists", "fixture_exists", "command_executed",
    "fixture_executed", "compile_validity", "controls", "baseline_result",
    "mutant_result", "sentinel_digest", "observations",
    "old_fails_new_passes", "cleanup_status", "restore_status",
    "initial_tree_digest", "final_tree_digest", "artifact_digests",
    "diagnostic", "reason_code",
})
_RUN_FIELDS = frozenset({
    "run_id", "exit_code", "expected_exit_code", "passed", "marker_seen",
    "executed", "timed_out", "output_digest", "error_digest",
})
_MAX_OUTPUT = 64 * 1024
_MAX_TREE_BYTES = 256 * 1024 * 1024


class MutationError(ValueError):
    """Stable fail-closed mutation-proof validation or execution error."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = " ".join(str(detail).split())[:160]


def _text(value: object, label: str, maximum: int = 512) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise MutationError("invalid_field", label)
    return value


def _identifier(value: object, label: str) -> str:
    value = _text(value, label, 128)
    if _IDENTIFIER.fullmatch(value) is None:
        raise MutationError("invalid_field", label)
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise MutationError("invalid_field", label)
    return value


def _plain_int(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MutationError("invalid_field", label)
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise MutationError("invalid_field", label)
    return value


def _relative_path(value: object, label: str) -> str:
    value = _text(value, label, 512).replace("\\", "/")
    parts = value.split("/")
    if (value.startswith("/") or value.startswith("~")
            or any(not _PATH_PART.fullmatch(part) for part in parts)
            or any(part in {".", ".."} for part in parts)):
        raise MutationError("unsafe_path", label)
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MutationError("non_canonical_json", str(exc)) from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_bounded(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MutationError("artifact_unreadable", type(exc).__name__) from exc
    return data[:_MAX_OUTPUT]


def _safe_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    root = root.absolute()
    path = root / relative
    current = root
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            current = current / part
            os.lstat(current)
            if not os.path.isdir(current) or os.path.islink(current):
                raise MutationError("unsafe_path", relative)
        stat_result = os.lstat(path)
        if os.path.islink(path) or not os.path.isfile(path):
            raise MutationError("unsafe_path", relative)
        if stat_result.st_nlink != 1:
            raise MutationError("unsafe_path", relative)
    except FileNotFoundError:
        if must_exist:
            raise MutationError("target_missing", relative)
    except OSError as exc:
        raise MutationError("unsafe_path", type(exc).__name__) from exc
    return path


def _tree_digest(root: Path) -> str:
    entries: list[tuple[str, bytes]] = []
    total = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            if name == "__pycache__":
                continue
            path = Path(current) / name
            if os.path.islink(path):
                raise MutationError("unsafe_path", str(path.relative_to(root)))
            if name != ".git":
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            path = Path(current) / name
            try:
                stat_result = os.lstat(path)
                if os.path.islink(path) or not os.path.isfile(path):
                    raise MutationError("unsafe_path", str(path.relative_to(root)))
                if stat_result.st_nlink != 1:
                    raise MutationError("unsafe_path", str(path.relative_to(root)))
                data = path.read_bytes()
            except OSError as exc:
                raise MutationError("tree_unreadable", type(exc).__name__) from exc
            total += len(data)
            if total > _MAX_TREE_BYTES:
                raise MutationError("tree_too_large")
            entries.append((str(path.relative_to(root)).replace(os.sep, "/"), data))
    digest = hashlib.sha256()
    for name, data in entries:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class MutationSpec:
    mutation_id: str
    mutation_type: str
    target_file: str
    anchor: str
    old_bytes: bytes
    new_bytes: bytes
    expected_match_count: int
    fixture_file: str
    compiler_command_id: str
    baseline_command_id: str
    positive_command_id: str
    negative_command_id: str
    mutant_command_id: str
    sentinel: str
    timeout_sec: float = 10.0

    def __post_init__(self) -> None:
        _identifier(self.mutation_id, "mutation_id")
        if self.mutation_type not in _MUTATION_TYPES:
            raise MutationError("invalid_field", "mutation_type")
        _relative_path(self.target_file, "target_file")
        _text(self.anchor, "anchor", 256)
        if (not isinstance(self.old_bytes, bytes) or not self.old_bytes
                or len(self.old_bytes) > _MAX_OUTPUT):
            raise MutationError("invalid_field", "old_bytes")
        if not isinstance(self.new_bytes, bytes) or len(self.new_bytes) > _MAX_OUTPUT:
            raise MutationError("invalid_field", "new_bytes")
        _plain_int(self.expected_match_count, "expected_match_count", 1)
        _relative_path(self.fixture_file, "fixture_file")
        for label in ("compiler_command_id", "baseline_command_id",
                      "positive_command_id",
                      "negative_command_id", "mutant_command_id"):
            _identifier(getattr(self, label), label)
        _text(self.sentinel, "sentinel", 128)
        if (isinstance(self.timeout_sec, bool)
                or not isinstance(self.timeout_sec, (int, float))
                or not 0 < self.timeout_sec <= 300):
            raise MutationError("invalid_field", "timeout_sec")


@dataclass(frozen=True)
class MutationProof:
    mapping: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mapping",
                           MappingProxyType(dict(self.mapping)))

    @property
    def canonical_mapping(self) -> dict[str, object]:
        return dict(self.mapping)

    @property
    def old_fails_new_passes(self) -> bool:
        return bool(self.mapping["old_fails_new_passes"])

    @property
    def baseline_result(self) -> Mapping[str, object]:
        return self.mapping["baseline_result"]  # type: ignore[return-value]

    @property
    def mutant_result(self) -> Mapping[str, object]:
        return self.mapping["mutant_result"]  # type: ignore[return-value]

    @property
    def controls(self) -> Mapping[str, Mapping[str, object]]:
        return self.mapping["controls"]  # type: ignore[return-value]

    @property
    def restore_status(self) -> str:
        return str(self.mapping["restore_status"])

    @property
    def initial_tree_digest(self) -> str:
        return str(self.mapping["initial_tree_digest"])

    @property
    def final_tree_digest(self) -> str:
        return str(self.mapping["final_tree_digest"])


def _validate_run(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise MutationError("invalid_field", label)
    unknown = sorted(set(raw) - _RUN_FIELDS)
    missing = sorted(_RUN_FIELDS - set(raw))
    if unknown:
        raise MutationError("unknown_field", unknown[0])
    if missing:
        raise MutationError("missing_field", missing[0])
    result = dict(raw)
    _identifier(result["run_id"], f"{label}.run_id")
    for key in ("exit_code", "expected_exit_code"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise MutationError("invalid_field", f"{label}.{key}")
    for key in ("passed", "marker_seen", "executed", "timed_out"):
        _boolean(result[key], f"{label}.{key}")
    _digest(result["output_digest"], f"{label}.output_digest")
    _digest(result["error_digest"], f"{label}.error_digest")
    return result


def parse_mutation_proof(raw: Mapping[str, object]) -> MutationProof:
    if not isinstance(raw, Mapping):
        raise MutationError("invalid_field", "mutation_proof")
    unknown = sorted(set(raw) - _PROOF_FIELDS)
    missing = sorted(_PROOF_FIELDS - set(raw))
    if unknown:
        raise MutationError("unknown_field", unknown[0])
    if missing:
        raise MutationError("missing_field", missing[0])
    result = dict(raw)
    if (isinstance(result["schema_version"], bool)
            or not isinstance(result["schema_version"], int)
            or result["schema_version"] != 1):
        raise MutationError("schema_version")
    _identifier(result["mutation_id"], "mutation_id")
    if result["mutation_type"] not in _MUTATION_TYPES:
        raise MutationError("invalid_field", "mutation_type")
    _relative_path(result["target_file"], "target_file")
    _text(result["anchor"], "anchor", 256)
    _digest(result["preimage_hash"], "preimage_hash")
    _plain_int(result["expected_match_count"], "expected_match_count", 1)
    _digest(result["mutant_content_digest"], "mutant_content_digest")
    _relative_path(result["fixture_file"], "fixture_file")
    for key in ("compiler_command_id", "baseline_command_id",
                "positive_command_id",
                "negative_command_id", "mutant_command_id"):
        _identifier(result[key], key)
    for key in ("command_exists", "fixture_exists", "command_executed",
                "fixture_executed", "old_fails_new_passes"):
        _boolean(result[key], key)
    compile_result = result["compile_validity"]
    if not isinstance(compile_result, dict):
        raise MutationError("invalid_field", "compile_validity")
    if set(compile_result) != {"status", "run"}:
        raise MutationError("invalid_field", "compile_validity")
    if compile_result["status"] not in {"passed", "compiler_invalid", "not_run"}:
        raise MutationError("invalid_field", "compile_validity.status")
    compile_result = {"status": compile_result["status"],
                      "run": _validate_run(compile_result["run"], "compile")}
    result["compile_validity"] = compile_result
    controls = result["controls"]
    if not isinstance(controls, dict) or set(controls) != {"positive", "negative"}:
        raise MutationError("invalid_field", "controls")
    result["controls"] = {
        key: _validate_run(controls[key], f"controls.{key}")
        for key in ("positive", "negative")}
    result["baseline_result"] = _validate_run(result["baseline_result"],
                                               "baseline_result")
    result["mutant_result"] = _validate_run(result["mutant_result"],
                                             "mutant_result")
    run_values = [compile_result["run"], result["baseline_result"],
                  result["controls"]["positive"],
                  result["controls"]["negative"], result["mutant_result"]]
    if len({run["run_id"] for run in run_values}) != len(run_values):
        raise MutationError("invalid_field", "run ids must be unique")
    _digest(result["sentinel_digest"], "sentinel_digest")
    observations = result["observations"]
    if (not isinstance(observations, dict)
            or set(observations) != {"baseline", "positive", "negative", "mutant"}):
        raise MutationError("invalid_field", "observations")
    result["observations"] = {
        key: _digest(observations[key], f"observations.{key}")
        for key in observations}
    for key in ("initial_tree_digest", "final_tree_digest"):
        _digest(result[key], key)
    if result["cleanup_status"] not in {"clean", "incomplete"}:
        raise MutationError("invalid_field", "cleanup_status")
    if result["restore_status"] not in {"restored", "failed", "not_needed"}:
        raise MutationError("invalid_field", "restore_status")
    artifacts = result["artifact_digests"]
    if (not isinstance(artifacts, list) or len(artifacts) > 32
            or any(_DIGEST.fullmatch(item or "") is None for item in artifacts)):
        raise MutationError("invalid_field", "artifact_digests")
    _identifier(result["diagnostic"], "diagnostic")
    _identifier(result["reason_code"], "reason_code")
    if result["reason_code"] == "ok":
        if (not result["old_fails_new_passes"]
                or result["restore_status"] != "restored"
                or result["initial_tree_digest"] != result["final_tree_digest"]
                or not all(result[key] for key in
                           ("command_exists", "fixture_exists",
                            "command_executed", "fixture_executed"))
                or compile_result["status"] != "passed"
                or not compile_result["run"]["passed"]
                or not (result["baseline_result"]["executed"]
                        and not result["baseline_result"]["passed"]
                        and result["baseline_result"]["marker_seen"])
                or not all(run["passed"] and run["marker_seen"]
                           for run in (result["controls"]["positive"],
                                       result["controls"]["negative"],
                                       result["mutant_result"]))):
            raise MutationError("proof_invariant")
    return MutationProof(result)


@dataclass(frozen=True)
class MutationExecution:
    accepted: bool
    reason_code: str
    proof: Mapping[str, object]
    receipt_mapping: Mapping[str, object]


def _run_command(
        command_id: str, policy: ProducerPolicy, root: Path, run_id: str,
        expected_exit_code: int, sentinel: str, timeout_sec: float,
        scratch: Path, cancel: object | None) -> dict[str, object]:
    command = policy.command(command_id)
    if command is None:
        raise MutationError("command_undeclared", command_id)
    cwd = root / command.cwd if command.cwd != "." else root
    if not cwd.is_dir() or os.path.islink(cwd):
        raise MutationError("unsafe_path", "command cwd")
    stdout = scratch / f"{run_id}.stdout"
    stderr = scratch / f"{run_id}.stderr"
    executed = False
    timed_out = False
    try:
        executable = command.argv[0]
        resolved_text = (executable if "/" in executable
                         else shutil.which(executable))
        if not resolved_text or not Path(resolved_text).exists():
            raise MutationError("command_missing", command_id)
        result: RunResult = run_with_watchdog(
            command.argv, cwd=cwd, stdout_path=stdout, stderr_path=stderr,
            timeout_sec=timeout_sec, cancel=cancel)
        executed = True
        timed_out = result.timed_out
        out = _read_bounded(stdout)
        err = _read_bounded(stderr)
        return {
            "run_id": run_id,
            "exit_code": result.rc,
            "expected_exit_code": expected_exit_code,
            "passed": result.rc == expected_exit_code and not result.timed_out,
            "marker_seen": sentinel.encode("utf-8") in out,
            "executed": executed,
            "timed_out": timed_out,
            "output_digest": _sha256_bytes(out),
            "error_digest": _sha256_bytes(err),
        }
    except ReviewCancelled:
        raise
    except MutationError:
        raise
    except OSError as exc:
        raise MutationError("command_failed", type(exc).__name__) from exc


def _proof_mapping(
        spec: MutationSpec, *, preimage_hash: str, mutant_content_digest: str,
        command_exists: bool, fixture_exists: bool, command_executed: bool,
        fixture_executed: bool, compile_validity: Mapping[str, object],
        controls: Mapping[str, object], baseline: Mapping[str, object],
        mutant: Mapping[str, object], sentinel_digest: str,
        observations: Mapping[str, str], old_fails_new_passes: bool,
        cleanup_status: str, restore_status: str, initial_tree_digest: str,
        final_tree_digest: str, artifacts: list[str], diagnostic: str,
        reason_code: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mutation_id": spec.mutation_id,
        "mutation_type": spec.mutation_type,
        "target_file": spec.target_file,
        "anchor": spec.anchor,
        "preimage_hash": preimage_hash,
        "expected_match_count": spec.expected_match_count,
        "mutant_content_digest": mutant_content_digest,
        "fixture_file": spec.fixture_file,
        "compiler_command_id": spec.compiler_command_id,
        "baseline_command_id": spec.baseline_command_id,
        "positive_command_id": spec.positive_command_id,
        "negative_command_id": spec.negative_command_id,
        "mutant_command_id": spec.mutant_command_id,
        "command_exists": command_exists,
        "fixture_exists": fixture_exists,
        "command_executed": command_executed,
        "fixture_executed": fixture_executed,
        "compile_validity": dict(compile_validity),
        "controls": dict(controls),
        "baseline_result": dict(baseline),
        "mutant_result": dict(mutant),
        "sentinel_digest": sentinel_digest,
        "observations": dict(observations),
        "old_fails_new_passes": old_fails_new_passes,
        "cleanup_status": cleanup_status,
        "restore_status": restore_status,
        "initial_tree_digest": initial_tree_digest,
        "final_tree_digest": final_tree_digest,
        "artifact_digests": list(artifacts),
        "diagnostic": diagnostic,
        "reason_code": reason_code,
    }


def run_mutation(spec: MutationSpec, *, root: Path,
                 identity: EvidenceIdentity,
                 policy: ProducerPolicy,
                 cancel: object | None = None) -> MutationExecution:
    """Run one protected mutation proof and return an authenticated receipt."""
    if (not isinstance(spec, MutationSpec)
            or not isinstance(identity, EvidenceIdentity)
            or not isinstance(policy, ProducerPolicy)):
        raise MutationError("invalid_input")
    if (not isinstance(root, Path) or not root.is_absolute()
            or not root.is_dir() or os.path.islink(root)):
        raise MutationError("invalid_root")
    command_ids = (spec.compiler_command_id, spec.baseline_command_id,
                   spec.positive_command_id,
                   spec.negative_command_id, spec.mutant_command_id)
    commands = [policy.command(item) for item in command_ids]
    if any(command is None for command in commands):
        raise MutationError("command_undeclared")
    if any(command.evidence_kind != "mutation" for command in commands
           if command is not None):
        raise MutationError("command_kind_mismatch")
    target = _safe_path(root, spec.target_file)
    fixture = _safe_path(root, spec.fixture_file, must_exist=False)
    if not fixture.exists():
        raise MutationError("fixture_missing")
    original = target.read_bytes()
    count = original.count(spec.old_bytes)
    if count == 0:
        raise MutationError("target_missing")
    if count != spec.expected_match_count:
        raise MutationError("target_cardinality")
    anchor_count = original.count(spec.anchor.encode("utf-8"))
    if anchor_count == 0:
        raise MutationError("anchor_missing")
    if anchor_count != 1:
        raise MutationError("anchor_cardinality")
    if spec.old_bytes == spec.new_bytes:
        raise MutationError("no_op")
    initial_tree = _tree_digest(root)
    preimage = _sha256_bytes(original)
    mutant_bytes = original.replace(spec.old_bytes, spec.new_bytes, 1)
    mutant_digest = _sha256_bytes(mutant_bytes)
    started = datetime.now(timezone.utc).replace(microsecond=0)
    scratch_path = Path(tempfile.mkdtemp(prefix="skodun-mutation-"))
    def blank(run_id: str, expected_exit_code: int = 0) -> dict[str, object]:
        return {
            "run_id": run_id, "exit_code": -1,
            "expected_exit_code": expected_exit_code, "passed": False,
            "marker_seen": False, "executed": False, "timed_out": False,
            "output_digest": _sha256_bytes(b""),
            "error_digest": _sha256_bytes(b""),
        }

    baseline = blank(f"{spec.mutation_id}-baseline-not-run", 0)
    compile_run = blank(f"{spec.mutation_id}-compile-not-run")
    controls = {
        "positive": blank(f"{spec.mutation_id}-positive-not-run"),
        "negative": blank(f"{spec.mutation_id}-negative-not-run", 1),
    }
    mutant = blank(f"{spec.mutation_id}-mutant-not-run", 1)
    artifacts: list[str] = []
    reason = "incomplete"
    diagnostic = "incomplete"
    restore_status = "not_needed"
    cleanup_status = "incomplete"
    mutated = False
    try:
        baseline = _run_command(
            spec.baseline_command_id, policy, root, f"{spec.mutation_id}-baseline",
            0, spec.sentinel, spec.timeout_sec, scratch_path, cancel)
        if baseline["passed"] or not baseline["marker_seen"]:
            reason = "baseline_failed"
            diagnostic = "control_failed"
        else:
            target.write_bytes(mutant_bytes)
            mutated = True
            compile_run = _run_command(
                spec.compiler_command_id, policy, root,
                f"{spec.mutation_id}-compile", 0, spec.sentinel,
                spec.timeout_sec, scratch_path, cancel)
            compile_status = ("passed" if compile_run["passed"]
                              else "not_run" if compile_run["timed_out"]
                              else "compiler_invalid")
            compile_validity = {"status": compile_status, "run": compile_run}
            if compile_status != "passed":
                reason = "timeout" if compile_run["timed_out"] else "compiler_invalid"
                diagnostic = "incomplete" if compile_run["timed_out"] else "compiler_invalid"
            else:
                controls = {
                    "positive": _run_command(
                        spec.positive_command_id, policy,
                        root, f"{spec.mutation_id}-positive", 0,
                        spec.sentinel, spec.timeout_sec, scratch_path, cancel),
                    "negative": _run_command(
                        spec.negative_command_id, policy,
                        root, f"{spec.mutation_id}-negative", 1,
                        spec.sentinel, spec.timeout_sec, scratch_path, cancel),
                }
                mutant = _run_command(
                    spec.mutant_command_id, policy,
                    root, f"{spec.mutation_id}-mutant", 0,
                    spec.sentinel, spec.timeout_sec, scratch_path, cancel)
                old_fails_new_passes = (
                    bool(not baseline["passed"] and baseline["marker_seen"]
                         and controls["positive"]["passed"]
                         and controls["positive"]["marker_seen"]
                         and controls["negative"]["passed"]
                         and controls["negative"]["marker_seen"]
                         and mutant["passed"] and mutant["marker_seen"]))
                reason = "ok" if old_fails_new_passes else "control_failed"
                diagnostic = "ok" if old_fails_new_passes else "control_failed"
        compile_validity = locals().get(
            "compile_validity", {"status": "not_run", "run": compile_run})
    except ReviewCancelled:
        reason = "cancelled"
        diagnostic = "cancelled"
        compile_validity = locals().get(
            "compile_validity", {"status": "not_run", "run": compile_run})
    finally:
        if mutated:
            try:
                target.write_bytes(original)
                restore_status = "restored"
            except OSError:
                restore_status = "failed"
        try:
            final_tree = _tree_digest(root)
        except MutationError:
            final_tree = _sha256_bytes(b"")
        shutil.rmtree(scratch_path, ignore_errors=True)
        cleanup_status = "clean"
    if restore_status == "failed":
        reason = "restore_failed"
        diagnostic = "incomplete"
    if final_tree != initial_tree:
        reason = "final_tree_changed"
        diagnostic = "incomplete"
    accepted = reason == "ok" and restore_status == "restored" and final_tree == initial_tree
    completed = datetime.now(timezone.utc).replace(microsecond=0)
    proof = _proof_mapping(
        spec, preimage_hash=preimage, mutant_content_digest=mutant_digest,
        command_exists=True, fixture_exists=True,
        command_executed=all(bool(run["executed"])
                             for run in (baseline, compile_run,
                                         controls["positive"],
                                         controls["negative"], mutant)),
        fixture_executed=bool(mutant.get("executed")),
        compile_validity=compile_validity, controls=controls,
        baseline=baseline, mutant=mutant,
        sentinel_digest=_sha256_bytes(spec.sentinel.encode("utf-8")),
        observations={
            "baseline": baseline["output_digest"],
            "positive": controls["positive"]["output_digest"],
            "negative": controls["negative"]["output_digest"],
            "mutant": mutant["output_digest"],
        },
        old_fails_new_passes=accepted,
        cleanup_status=cleanup_status, restore_status=restore_status,
        initial_tree_digest=initial_tree, final_tree_digest=final_tree,
        artifacts=artifacts, diagnostic=diagnostic, reason_code=reason)
    parse_mutation_proof(proof)
    command = policy.command(spec.mutant_command_id)
    assert command is not None
    mapping: dict[str, object] = {
        "schema_version": 1,
        "evidence_kind": "mutation",
        **identity.to_mapping(),
        "producer_policy_id": policy.policy_id,
        "producer_policy_digest": policy.digest,
        "command_id": command.command_id,
        "command_digest": command.digest,
        "producer_proof": "sha256:" + "0" * 64,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exit_code": 0 if accepted else -1,
        "terminal_state": "passed" if accepted else "failed",
        "duration_ms": int((completed - started).total_seconds() * 1000),
        "counters": {"controls": 2, "runs": 5},
        "artifact_digests": artifacts,
        "tool": "skodun-mutation/1",
        "runtime": "python/3",
        "diagnostic_category": "ok" if accepted else "unverifiable",
        "nonce": spec.mutation_id,
        "redaction": {"applied": True, "secrets_removed": True,
                       "logs_included": False},
        "mutation_proof": proof,
    }
    mapping["producer_proof"] = producer_proof(mapping, policy.provenance_key)
    mapping["receipt_digest"] = receipt_digest(mapping)
    return MutationExecution(accepted, reason, proof, mapping)
