"""Protected language capability profiles and bounded repository context.

Profiles are advisory capability descriptions.  They select commands already
bound by a :class:`~skodun.evidence.ProducerPolicy`, execute them through the
bounded process-group watchdog, and return only digest-based summaries.  They
never parse source, certify coverage, or contribute to gate/trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .evidence import EvidenceIdentity, ProducerPolicy
from .runner import RunResult, SpawnError, run_with_watchdog


MAX_PROFILE_OUTPUT_BYTES = 64 * 1024
MAX_PROFILE_TIMEOUT_SEC = 600
MAX_PROFILE_FIXTURES = 64
MAX_RECEIPT_CONTEXT_ITEMS = 32
MAX_RECEIPT_CONTEXT_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OID = re.compile(r"[0-9a-f]{40}")
_CAPABILITIES = frozenset({
    "version_discovery", "syntax_compile", "fixture_harness",
    "symbol_query", "mutation_locator", "mutation_execution",
})


class ProfileError(ValueError):
    """Stable validation or unavailable-capability reason."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = " ".join(str(detail).split())[:160]

    def __str__(self) -> str:
        return f"{self.reason_code}: {self.detail}" if self.detail else self.reason_code


def _text(value: object, label: str, maximum: int = 512) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ProfileError("invalid_profile", label)
    return value


def _identifier(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    value = _text(value, label, 128)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ProfileError("invalid_profile", label)
    return value


def _relative_path(value: object, label: str = "fixture path") -> str:
    value = _text(value, label, 512).replace("\\", "/")
    parts = value.split("/")
    if (value.startswith("/") or any(part in {"", ".", ".."} for part in parts)
            or re.match(r"^[A-Za-z]:", value)):
        raise ProfileError("unsafe_fixture", label)
    return value


def _plain_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError("invalid_profile", label)
    if value < minimum or value > maximum:
        raise ProfileError("invalid_profile", label)
    return value


@dataclass(frozen=True)
class FixtureExpectation:
    """One repository-relative fixture and its bounded harness markers."""

    path: str
    expected_status: str
    expected_output: str
    harness_output: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path))
        if self.expected_status not in {"accepted", "rejected"}:
            raise ProfileError("invalid_profile", "fixture expected_status")
        output = _text(self.expected_output, "fixture expected_output", 128)
        object.__setattr__(self, "expected_output", output)
        if self.harness_output is not None:
            object.__setattr__(
                self, "harness_output",
                _text(self.harness_output, "fixture harness_output", 128))


@dataclass(frozen=True)
class LanguageCapabilityProfile:
    """A protected, versioned language capability mapping."""

    profile_id: str
    language: str
    version: str
    version_command_id: str
    compile_command_id: str | None
    harness_command_id: str | None
    capabilities: tuple[str, ...]
    fixtures: tuple[FixtureExpectation, ...]
    symbol_query_command_id: str | None = None
    locator_command_id: str | None = None
    mutation_command_id: str | None = None
    version_prefix: str | None = None
    timeout_sec: int = 60
    max_output_bytes: int = MAX_PROFILE_OUTPUT_BYTES

    def __post_init__(self) -> None:
        for value, label in ((self.profile_id, "profile_id"),
                             (self.language, "language"),
                             (self.version, "version")):
            _identifier(value, label)
        for value, label in (
                (self.version_command_id, "version_command_id"),
                (self.compile_command_id, "compile_command_id"),
                (self.harness_command_id, "harness_command_id"),
                (self.symbol_query_command_id, "symbol_query_command_id"),
                (self.locator_command_id, "locator_command_id"),
                (self.mutation_command_id, "mutation_command_id")):
            _identifier(value, label, optional=value is None)
        if (not isinstance(self.capabilities, tuple) or not self.capabilities
                or len(set(self.capabilities)) != len(self.capabilities)
                or any(capability not in _CAPABILITIES
                       for capability in self.capabilities)):
            raise ProfileError("invalid_profile", "capabilities")
        required = {
            "version_discovery": ("version_command_id", self.version_command_id),
            "syntax_compile": ("compile_command_id", self.compile_command_id),
            "fixture_harness": ("harness_command_id", self.harness_command_id),
            "symbol_query": ("symbol_query_command_id", self.symbol_query_command_id),
            "mutation_locator": ("locator_command_id", self.locator_command_id),
            "mutation_execution": ("mutation_command_id", self.mutation_command_id),
        }
        for capability in self.capabilities:
            field, command_id = required[capability]
            if command_id is None:
                raise ProfileError("invalid_profile", field)
        if (not isinstance(self.fixtures, tuple)
                or len(self.fixtures) > MAX_PROFILE_FIXTURES
                or any(not isinstance(item, FixtureExpectation)
                       for item in self.fixtures)):
            raise ProfileError("invalid_profile", "fixtures")
        if len({item.path for item in self.fixtures}) != len(self.fixtures):
            raise ProfileError("invalid_profile", "duplicate fixture path")
        if self.fixtures and (self.compile_command_id is None
                              or self.harness_command_id is None):
            raise ProfileError("invalid_profile", "fixture compile/harness command")
        if self.version_prefix is not None:
            _text(self.version_prefix, "version_prefix", 128)
        _plain_int(self.timeout_sec, "timeout_sec", 1, MAX_PROFILE_TIMEOUT_SEC)
        _plain_int(self.max_output_bytes, "max_output_bytes", 1,
                   MAX_PROFILE_OUTPUT_BYTES)


@dataclass(frozen=True)
class ProfileRun:
    accepted: bool
    reason_code: str
    profile_id: str
    language: str
    version: str | None
    capabilities: tuple[str, ...]
    checks: tuple[Mapping[str, object], ...]
    raw_output: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(
            MappingProxyType(dict(check)) for check in self.checks))


@dataclass(frozen=True)
class RepositoryReceipt:
    """A redacted lifecycle summary, never a coverage/trust assertion."""

    receipt_kind: str
    current_head: str
    status: str
    reason_code: str
    summary: Mapping[str, object]
    receipt_digest: str

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.receipt_kind) is None:
            raise ProfileError("invalid_receipt", "receipt_kind")
        if _OID.fullmatch(self.current_head) is None:
            raise ProfileError("invalid_receipt", "current_head")
        if self.status not in {"passed", "failed", "unavailable"}:
            raise ProfileError("invalid_receipt", "status")
        _identifier(self.reason_code, "reason_code")
        if not isinstance(self.summary, Mapping):
            raise ProfileError("invalid_receipt", "summary")
        object.__setattr__(self, "summary",
                           MappingProxyType(dict(self.summary)))
        if _DIGEST.fullmatch(self.receipt_digest) is None:
            raise ProfileError("invalid_receipt", "receipt_digest")

    def to_mapping(self) -> dict[str, object]:
        return {"receipt_kind": self.receipt_kind,
                "current_head": self.current_head, "status": self.status,
                "reason_code": self.reason_code, "summary": dict(self.summary),
                "receipt_digest": self.receipt_digest}


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ProfileError("unsafe_root", str(root))
    return root


def _safe_fixture(root: Path, relative: str) -> bytes:
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ProfileError("unsafe_fixture", relative)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise ProfileError("unsafe_fixture", "safe-open flags unavailable")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow | nonblock)
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_nlink != 1:
            raise ProfileError("unsafe_fixture", relative)
        data = os.read(fd, MAX_PROFILE_OUTPUT_BYTES + 1)
        after = os.fstat(fd)
        fields = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                                value.st_nlink, value.st_size, value.st_mtime_ns)
        if fields(stat_result) != fields(after):
            raise ProfileError("unsafe_fixture", "fixture changed during read")
    except ProfileError:
        raise
    except OSError as exc:
        raise ProfileError("fixture_missing", relative) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > MAX_PROFILE_OUTPUT_BYTES:
        raise ProfileError("fixture_too_large", relative)
    return data


def _command(policy: ProducerPolicy, command_id: str, *, mutation: bool = False):
    command = policy.command(command_id)
    if command is None:
        raise ProfileError("command_undeclared", command_id)
    if mutation and command.evidence_kind != "mutation":
        raise ProfileError("command_kind_mismatch", command_id)
    if not mutation and command.evidence_kind == "mutation":
        raise ProfileError("command_kind_mismatch", command_id)
    return command


def _command_cwd(root: Path, relative: str) -> Path:
    if relative == ".":
        return root
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ProfileError("unsafe_command", "command cwd")
    if not path.is_dir():
        raise ProfileError("unsafe_command", "command cwd")
    return path


def _resolve_executable_for_cwd(command, cwd: Path) -> str:
    """Resolve an argv path with the same cwd semantics as Popen."""
    executable = command.argv[0]
    if "/" not in executable:
        resolved = shutil.which(executable)
    elif Path(executable).is_absolute():
        resolved = executable
    else:
        resolved = str(cwd / executable)
    if not resolved or not Path(resolved).is_file():
        raise ProfileError("command_missing", command.command_id)
    for argument in command.argv[1:]:
        if (argument != "{fixture}" and os.path.isabs(argument)
                and not Path(argument).exists()):
            raise ProfileError("command_missing", command.command_id)
    return resolved


def _read_output(path: Path, limit: int) -> bytes:
    try:
        return path.read_bytes()[:limit]
    except OSError:
        return b""


def _run_command(command, root: Path, scratch: Path, run_id: str,
                 timeout_sec: int, output_limit: int,
                 fixture: Path | None = None) -> tuple[dict[str, object], bytes]:
    cwd = _command_cwd(root, command.cwd)
    resolved = _resolve_executable_for_cwd(command, cwd)
    argv = [resolved if index == 0 else argument for index, argument
            in enumerate(command.argv)]
    for index, argument in enumerate(argv):
        if argument == "{fixture}":
            if fixture is None:
                raise ProfileError("invalid_profile", "fixture placeholder")
            argv[index] = str(fixture)
        elif "{fixture}" in argument:
            raise ProfileError("invalid_profile", "fixture placeholder")
    stdout = scratch / f"{run_id}.stdout"
    stderr = scratch / f"{run_id}.stderr"
    env = {name: os.environ[name] for name in command.env_allowlist
           if name in os.environ}
    try:
        result: RunResult = run_with_watchdog(
            argv, timeout_sec, cwd, stdout, stderr,
            max_output_bytes=output_limit, env=env)
    except SpawnError as exc:
        raise ProfileError("command_missing", command.command_id) from exc
    out = _read_output(stdout, output_limit)
    err = _read_output(stderr, output_limit)
    check = {
        "command_id": command.command_id,
        "run_id": run_id,
        "exit_code": result.rc,
        "passed": result.rc == 0 and not result.timed_out
                   and not result.output_limit_exceeded
                   and not result.descendants_killed
                   and result.descendant_state == "none",
        "timed_out": result.timed_out,
        "output_limit_exceeded": result.output_limit_exceeded,
        "descendants_clean": (not result.descendants_killed
                               and result.descendant_state == "none"),
        "output_digest": _digest(out.decode("utf-8", "replace")),
        "error_digest": _digest(err.decode("utf-8", "replace")),
    }
    if result.timed_out:
        raise ProfileError("timeout", command.command_id)
    if result.output_limit_exceeded:
        raise ProfileError("output_limit_exceeded", command.command_id)
    if result.descendants_killed or result.descendant_state != "none":
        raise ProfileError("descendant_cleanup", command.command_id)
    return check, out


def _failed(profile: LanguageCapabilityProfile, reason: str,
            checks: Sequence[Mapping[str, object]] = (),
            version: str | None = None) -> ProfileRun:
    return ProfileRun(False, reason, profile.profile_id, profile.language, version,
                      profile.capabilities, tuple(checks))


def run_profile(profile: LanguageCapabilityProfile, policy: ProducerPolicy,
                root: Path) -> ProfileRun:
    """Run a profile through protected commands and return bounded evidence."""
    if not isinstance(profile, LanguageCapabilityProfile):
        raise ProfileError("invalid_profile", "profile")
    if not isinstance(policy, ProducerPolicy):
        raise ProfileError("invalid_profile", "policy")
    try:
        root = _root(root)
    except ProfileError as exc:
        return _failed(profile, exc.reason_code)
    selected = [profile.version_command_id]
    selected.extend(command_id for command_id in (
        profile.compile_command_id, profile.harness_command_id,
        profile.symbol_query_command_id, profile.locator_command_id,
        profile.mutation_command_id) if command_id is not None)
    commands = {}
    try:
        for command_id in selected:
            commands[command_id] = _command(
                policy, command_id,
                mutation=command_id == profile.mutation_command_id)
            command = commands[command_id]
            _resolve_executable_for_cwd(
                command, _command_cwd(root, command.cwd))
    except ProfileError as exc:
        return _failed(profile, exc.reason_code)

    checks: list[Mapping[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="skodun-profile-") as scratch_name:
        scratch = Path(scratch_name)
        try:
            version_check, version_bytes = _run_command(
                commands[profile.version_command_id], root, scratch, "version",
                profile.timeout_sec, profile.max_output_bytes)
        except ProfileError as exc:
            return _failed(profile, exc.reason_code, checks)
        checks.append(version_check)
        version_text = version_bytes.decode("utf-8", "replace").splitlines()
        discovered = version_text[0][:128] if version_text else ""
        if (profile.version_prefix is not None
                and not discovered.startswith(profile.version_prefix)):
            return _failed(profile, "version_mismatch", checks, discovered or None)
        for index, fixture in enumerate(profile.fixtures):
            try:
                data = _safe_fixture(root, fixture.path)
            except ProfileError as exc:
                return _failed(profile, exc.reason_code, checks, discovered or None)
            fixture_copy = scratch / f"fixture-{index}.source"
            fixture_copy.write_bytes(data)
            try:
                compile_check, compile_bytes = _run_command(
                    commands[profile.compile_command_id], root, scratch,
                    f"compile-{index}", profile.timeout_sec,
                    profile.max_output_bytes, fixture_copy)
                harness_check, harness_bytes = _run_command(
                    commands[profile.harness_command_id], root, scratch,
                    f"harness-{index}", profile.timeout_sec,
                    profile.max_output_bytes, fixture_copy)
            except ProfileError as exc:
                return _failed(profile, exc.reason_code, checks,
                               discovered or None)
            compile_text = compile_bytes.decode("utf-8", "replace")
            harness_text = harness_bytes.decode("utf-8", "replace")
            compile_ok = ((compile_check["exit_code"] == 0)
                          == (fixture.expected_status == "accepted"))
            harness_ok = ((harness_check["exit_code"] == 0)
                          == (fixture.expected_status == "accepted"))
            markers_ok = (fixture.expected_output in compile_text
                          and (fixture.harness_output
                               or fixture.expected_output.replace(
                                   "COMPILE_", "HARNESS_")) in harness_text)
            checks.extend((compile_check, harness_check))
            if not (compile_ok and harness_ok and markers_ok):
                return _failed(profile, "invalid_fixture", checks,
                               discovered or None)
    return ProfileRun(True, "ok", profile.profile_id, profile.language,
                      discovered or None, profile.capabilities, tuple(checks))


def scala3_profile() -> LanguageCapabilityProfile:
    """Return the fixture-only Scala 3 pilot profile."""
    valid = (
        "indentation.scala", "given-with.scala", "nested-anonymous.scala",
        "xml.scala", "names.scala", "inheritance.scala", "quoted.scala",
        "unicode.scala",
    )
    fixtures = tuple(FixtureExpectation(
        f"tests/fixtures/languages/scala3/valid/{name}", "accepted",
        "COMPILE_OK", "HARNESS_OK") for name in valid)
    fixtures += (FixtureExpectation(
        "tests/fixtures/languages/scala3/invalid/invalid.scala", "rejected",
        "COMPILE_REJECTED", "HARNESS_REJECTED"),)
    return LanguageCapabilityProfile(
        profile_id="scala3-pilot", language="scala", version="3.3",
        version_command_id="scala_version", compile_command_id="scala_compile",
        harness_command_id="scala_harness",
        symbol_query_command_id="scala_symbols",
        locator_command_id="scala_locator", mutation_command_id="scala_mutation",
        capabilities=("version_discovery", "syntax_compile", "fixture_harness",
                      "symbol_query", "mutation_locator", "mutation_execution"),
        fixtures=fixtures, version_prefix="Scala 3.", timeout_sec=60,
        max_output_bytes=MAX_PROFILE_OUTPUT_BYTES)


def _head(expected: EvidenceIdentity, value: object) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise ProfileError("invalid_receipt", "current_head")
    if value != expected.current_head:
        raise ProfileError("head_mismatch", "current_head")
    return value


def _receipt(kind: str, head: str, status: str, reason: str,
             summary: Mapping[str, object]) -> RepositoryReceipt:
    body = {"receipt_kind": kind, "current_head": head, "status": status,
            "reason_code": reason, "summary": dict(summary)}
    return RepositoryReceipt(kind, head, status, reason, dict(summary),
                             _digest(body))


def _coerce_mapping(raw: Mapping[str, object] | str, label: str
                    ) -> Mapping[str, object]:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ProfileError("receipt_too_large", label)
        try:
            raw = json.loads(raw, object_pairs_hook=_receipt_pairs,
                             parse_constant=_reject_constant)
        except (TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileError("invalid_receipt", label) from exc
    if not isinstance(raw, Mapping):
        raise ProfileError("invalid_receipt", label)
    return raw


def _receipt_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def adapt_local_receipt(raw: Mapping[str, object] | str, expected: EvidenceIdentity,
                        evidence_kind: str) -> RepositoryReceipt:
    """Adapt a local preflight/full-gate summary without accepting logs."""
    if evidence_kind not in {"preflight", "full_gate"}:
        raise ProfileError("invalid_receipt", "evidence_kind")
    raw = _coerce_mapping(raw, "local receipt")
    if ("evidence_kind" in raw and raw.get("evidence_kind") != evidence_kind):
        raise ProfileError("evidence_kind_mismatch", "evidence_kind")
    for field in ("repository_id", "worktree_root", "certification_base",
                  "diff_hash"):
        if raw.get(field) != getattr(expected, field):
            raise ProfileError(f"{field}_mismatch", field)
    head = _head(expected, raw.get("current_head"))
    terminal = raw.get("terminal_state")
    exit_code = raw.get("exit_code")
    if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ProfileError("invalid_receipt", "exit_code")
    passed = terminal == "passed" and exit_code == 0
    digest = raw.get("receipt_digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ProfileError("invalid_receipt", "receipt_digest")
    return _receipt("local_" + evidence_kind, head,
                    "passed" if passed else "failed",
                    "ok" if passed else "producer_failed",
                    {"receipt_digest": digest, "evidence_kind": evidence_kind,
                     "terminal_state": str(terminal)[:32]})


def adapt_mutation_log(raw: Mapping[str, object] | str, expected: EvidenceIdentity
                       ) -> RepositoryReceipt:
    """Adapt compiler-valid mutation proof metadata, excluding mutation logs."""
    raw = _coerce_mapping(raw, "mutation log")
    head = _head(expected, raw.get("current_head"))
    mutation_id = _text(raw.get("mutation_id"), "mutation_id", 128)
    compiler_valid = raw.get("compiler_valid")
    compile_validity = raw.get("compile_validity")
    compiler_ok = compiler_valid is True or (
            isinstance(compile_validity, Mapping)
            and compile_validity.get("status") == "passed")
    if not compiler_ok or raw.get("old_fails_new_passes") is not True:
        raise ProfileError("compiler_invalid", mutation_id)
    if raw.get("restore_status") != "restored" or raw.get("cleanup_status") != "clean":
        return _receipt("mutation", head, "unavailable", "cleanup_incomplete",
                        {"mutation_id": mutation_id})
    return _receipt("mutation", head, "passed", "ok",
                    {"mutation_id": mutation_id, "compiler_valid": True})


def adapt_ci_receipt(raw: Mapping[str, object] | str, expected: EvidenceIdentity
                     ) -> RepositoryReceipt:
    raw = _coerce_mapping(raw, "ci receipt")
    run_id = _text(raw.get("run_id"), "run_id", 128)
    head = _head(expected, raw.get("head_sha", raw.get("commit")))
    conclusion = raw.get("conclusion")
    if conclusion not in {"success", "failure", "cancelled", "neutral", "skipped"}:
        raise ProfileError("invalid_receipt", "conclusion")
    status = ("passed" if conclusion == "success" else
              "failed" if conclusion == "failure" else "unavailable")
    reason = "ok" if status == "passed" else "lifecycle_unavailable"
    return _receipt("ci_run", head, status, reason,
                    {"run_id": run_id, "conclusion": conclusion})


def adapt_review_threads(raw: Mapping[str, object] | str, expected: EvidenceIdentity
                         ) -> RepositoryReceipt:
    raw = _coerce_mapping(raw, "review thread receipt")
    snapshot = _text(raw.get("snapshot_id"), "snapshot_id", 128)
    head = _head(expected, raw.get("head_sha", raw.get("commit")))
    unresolved = raw.get("unresolved")
    if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved < 0:
        raise ProfileError("invalid_receipt", "unresolved")
    return _receipt("review_threads", head,
                    "passed" if unresolved == 0 else "failed",
                    "ok" if unresolved == 0 else "unresolved_threads",
                    {"snapshot_id": snapshot, "unresolved": unresolved})


def compact_stored_receipt_context(
        rows: Sequence[Mapping[str, object]], identity_digest: str, *,
        max_items: int = MAX_RECEIPT_CONTEXT_ITEMS,
        max_bytes: int = MAX_RECEIPT_CONTEXT_BYTES) -> bytes:
    """Render store projections as bounded, redacted prompt context."""
    if _DIGEST.fullmatch(identity_digest) is None:
        raise ProfileError("invalid_context", "identity_digest")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ProfileError("invalid_context", "max_items")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 128:
        raise ProfileError("invalid_context", "max_bytes")
    safe_rows = []
    allowed = ("receipt_digest", "nonce", "status", "reason_code",
               "evidence_kind", "terminal_state", "ingested_at")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProfileError("invalid_context", "receipt row")
        safe = {key: _text(row[key], key, 512) for key in allowed
                if key in row and row[key] is not None}
        if "receipt_digest" not in safe:
            raise ProfileError("invalid_context", "receipt_digest")
        safe_rows.append(safe)
    selected = []
    truncated = len(safe_rows) > max_items
    for row in safe_rows[:max_items]:
        candidate = {"identity_digest": identity_digest,
                     "receipts": [*selected, row], "truncated": truncated}
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_bytes:
            truncated = True
            break
        selected.append(row)
    body = json.dumps({"identity_digest": identity_digest,
                       "receipts": selected, "truncated": truncated},
                      ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    rendered = ("----- BEGIN REPOSITORY EVIDENCE -----\n" + body
                + "\n----- END REPOSITORY EVIDENCE -----\n").encode("utf-8")
    if len(rendered) > max_bytes:
        raise ProfileError("invalid_context", "max_bytes too small")
    return rendered


def compact_receipt_context(receipts: Sequence[RepositoryReceipt], *,
                            max_items: int = MAX_RECEIPT_CONTEXT_ITEMS,
                            max_bytes: int = MAX_RECEIPT_CONTEXT_BYTES) -> str:
    """Render deterministic receipt summaries under an explicit byte cap."""
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ProfileError("invalid_context", "max_items")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 128:
        raise ProfileError("invalid_context", "max_bytes")
    if any(not isinstance(receipt, RepositoryReceipt) for receipt in receipts):
        raise ProfileError("invalid_context", "receipts")
    selected = []
    truncated = len(receipts) > max_items
    for receipt in sorted(receipts, key=lambda item: item.receipt_digest)[:max_items]:
        candidate = {"receipts": [*selected, receipt.to_mapping()],
                     "truncated": truncated}
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_bytes:
            truncated = True
            break
        selected.append(receipt.to_mapping())
    return json.dumps({"receipts": selected, "truncated": truncated},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
