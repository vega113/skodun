"""Strict, advisory repository-evidence receipts.

Receipts are untrusted read-model context.  This module validates bounded
canonical envelopes and protected producer policy digests; it never executes
commands and never contributes to Skodun's trust axes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


EVIDENCE_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 64 * 1024
MAX_TEXT_CHARS = 512
MAX_COUNTERS = 32
MAX_ARTIFACT_DIGESTS = 32
MAX_ARGV = 32
MAX_ENV_NAMES = 32
MAX_DIAGNOSTIC_CHARS = 64

_OID = re.compile(r"[0-9a-f]{40}")
_DIFF = re.compile(r"[0-9a-f]{40,64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_TS = "%Y-%m-%dT%H:%M:%SZ"
_KINDS = frozenset({"preflight", "full_gate", "mutation", "ci_run",
                    "review_threads"})
_TERMINAL = frozenset({"passed", "failed", "cancelled", "unavailable"})
_DIAGNOSTICS = frozenset({"ok", "failed", "cancelled", "unavailable",
                          "mismatch", "redacted", "unverifiable"})


class EvidenceError(ValueError):
    """Stable, bounded validation failure for untrusted evidence."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = " ".join(str(detail).split())[:160]


def _text(label: str, value: object, *, optional: bool = False,
          maximum: int = MAX_TEXT_CHARS) -> str | None:
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise EvidenceError("invalid_field", label)
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvidenceError("invalid_field", label)
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("non_canonical_json", str(exc)) from exc


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_ts(value: object, label: str) -> datetime:
    if not isinstance(value, str) or len(value) != 20:
        raise EvidenceError("invalid_timestamp", label)
    try:
        parsed = datetime.strptime(value, _TS).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise EvidenceError("invalid_timestamp", label) from exc
    if parsed.strftime(_TS) != value:
        raise EvidenceError("invalid_timestamp", label)
    return parsed


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError("invalid_field", label)
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_key", key)
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise EvidenceError("malformed_json", value)


@dataclass(frozen=True)
class EvidenceIdentity:
    repository_id: str
    worktree_root: str
    certification_base: str
    current_head: str
    diff_hash: str
    stack_slice_id: str | None = None

    def __post_init__(self) -> None:
        repository_id = _text("repository_id", self.repository_id)
        if "/" not in repository_id or repository_id.startswith("/"):
            raise EvidenceError("invalid_field", "repository_id")
        root = _text("worktree_root", self.worktree_root)
        if not Path(root).is_absolute():
            raise EvidenceError("invalid_field", "worktree_root")
        for label in ("certification_base", "current_head"):
            value = getattr(self, label)
            if not isinstance(value, str) or _OID.fullmatch(value) is None:
                raise EvidenceError("invalid_field", label)
        if not isinstance(self.diff_hash, str) or _DIFF.fullmatch(self.diff_hash) is None:
            raise EvidenceError("invalid_field", "diff_hash")
        _text("stack_slice_id", self.stack_slice_id, optional=True)

    def to_mapping(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "worktree_root": self.worktree_root,
            "certification_base": self.certification_base,
            "current_head": self.current_head,
            "diff_hash": self.diff_hash,
            "stack_slice_id": self.stack_slice_id,
        }

    @property
    def digest(self) -> str:
        return _sha256(self.to_mapping())


def _is_shell_command_flag(arg: str) -> bool:
    """Reject shell command-string flags, including combined spellings."""
    if arg in {"-c", "--command", "-Command"}:
        return True
    if (arg.startswith("--command=") or arg.startswith("-Command=")
            or arg.startswith("-Command:")):
        return True
    return (arg.startswith("-c")
            or re.fullmatch(r"-[A-Za-z]*c(?:=.*)?", arg) is not None)


@dataclass(frozen=True)
class ProducerCommand:
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    env_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        _text("command_id", self.command_id)
        if (not isinstance(self.argv, tuple) or not self.argv
                or len(self.argv) > MAX_ARGV):
            raise EvidenceError("invalid_command", "argv")
        for arg in self.argv:
            _text("argv", arg, maximum=1024)
        if any(_is_shell_command_flag(arg) for arg in self.argv[1:]):
            raise EvidenceError("invalid_command", "shell command strings are forbidden")
        _text("cwd", self.cwd)
        if self.cwd != ".":
            parts = self.cwd.split("/")
            if (self.cwd.startswith("/") or any(part in {"", ".", ".."}
                                                  for part in parts)):
                raise EvidenceError("invalid_command", "cwd")
        if (not isinstance(self.env_allowlist, tuple)
                or len(self.env_allowlist) > MAX_ENV_NAMES
                or any(_ENV.fullmatch(name) is None
                       for name in self.env_allowlist)):
            raise EvidenceError("invalid_command", "environment allowlist")

    def to_mapping(self) -> dict[str, object]:
        return {"command_id": self.command_id, "argv": list(self.argv),
                "cwd": self.cwd, "env_allowlist": list(self.env_allowlist)}

    @property
    def digest(self) -> str:
        return _sha256(self.to_mapping())


@dataclass(frozen=True)
class ProducerPolicy:
    policy_id: str
    commands: tuple[ProducerCommand, ...]

    def __post_init__(self) -> None:
        _text("policy_id", self.policy_id)
        if (not isinstance(self.commands, tuple) or not self.commands
                or len(self.commands) > MAX_ARGV
                or any(not isinstance(command, ProducerCommand)
                       for command in self.commands)):
            raise EvidenceError("invalid_policy", "commands")
        if len({command.command_id for command in self.commands}) != len(self.commands):
            raise EvidenceError("invalid_policy", "duplicate command id")

    @property
    def digest(self) -> str:
        return _sha256({"policy_id": self.policy_id,
                        "commands": [command.to_mapping()
                                     for command in self.commands]})

    def command(self, command_id: str) -> ProducerCommand | None:
        return next((command for command in self.commands
                     if command.command_id == command_id), None)


_RECEIPT_FIELDS = frozenset({
    "schema_version", "evidence_kind", "repository_id", "worktree_root",
    "certification_base", "current_head", "diff_hash", "stack_slice_id",
    "producer_policy_id", "producer_policy_digest", "command_id",
    "command_digest", "started_at", "completed_at", "exit_code",
    "terminal_state", "duration_ms", "counters", "artifact_digests",
    "tool", "runtime", "diagnostic_category", "nonce", "redaction",
    "receipt_digest",
})


@dataclass(frozen=True)
class EvidenceReceipt:
    schema_version: int
    evidence_kind: str
    repository_id: str
    worktree_root: str
    certification_base: str
    current_head: str
    diff_hash: str
    stack_slice_id: str | None
    producer_policy_id: str
    producer_policy_digest: str
    command_id: str
    command_digest: str
    started_at: str
    completed_at: str
    exit_code: int | None
    terminal_state: str
    duration_ms: int
    counters: Mapping[str, int]
    artifact_digests: tuple[str, ...]
    tool: str
    runtime: str
    diagnostic_category: str
    nonce: str
    redaction: dict[str, bool]
    receipt_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.counters, MappingProxyType):
            object.__setattr__(self, "counters",
                               MappingProxyType(dict(self.counters)))
        if not isinstance(self.redaction, MappingProxyType):
            object.__setattr__(self, "redaction",
                               MappingProxyType(dict(self.redaction)))

    @property
    def canonical_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_kind": self.evidence_kind,
            "repository_id": self.repository_id,
            "worktree_root": self.worktree_root,
            "certification_base": self.certification_base,
            "current_head": self.current_head,
            "diff_hash": self.diff_hash,
            "stack_slice_id": self.stack_slice_id,
            "producer_policy_id": self.producer_policy_id,
            "producer_policy_digest": self.producer_policy_digest,
            "command_id": self.command_id,
            "command_digest": self.command_digest,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "exit_code": self.exit_code,
            "terminal_state": self.terminal_state,
            "duration_ms": self.duration_ms,
            "counters": dict(self.counters),
            "artifact_digests": list(self.artifact_digests),
            "tool": self.tool,
            "runtime": self.runtime,
            "diagnostic_category": self.diagnostic_category,
            "nonce": self.nonce,
            "redaction": dict(self.redaction),
            "receipt_digest": self.receipt_digest,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical(self.canonical_mapping)


def receipt_digest(value: Mapping[str, object]) -> str:
    body = dict(value)
    body.pop("receipt_digest", None)
    return _sha256(body)


def _validate_mapping(raw: Mapping[str, object]) -> EvidenceReceipt:
    unknown = sorted(set(raw) - _RECEIPT_FIELDS)
    missing = sorted(_RECEIPT_FIELDS - set(raw))
    if unknown:
        raise EvidenceError("unknown_field", unknown[0])
    if missing:
        raise EvidenceError("missing_field", missing[0])
    if (_plain_int(raw["schema_version"], "schema_version", minimum=1)
            != EVIDENCE_SCHEMA_VERSION):
        raise EvidenceError("schema_version", "unsupported")
    kind = raw["evidence_kind"]
    if not isinstance(kind, str) or kind not in _KINDS:
        raise EvidenceError("invalid_field", "evidence_kind")
    identity = EvidenceIdentity(
        raw["repository_id"], raw["worktree_root"],
        raw["certification_base"], raw["current_head"],
        raw["diff_hash"], raw["stack_slice_id"])
    policy_id = _text("producer_policy_id", raw["producer_policy_id"])
    policy_digest = _digest(raw["producer_policy_digest"],
                             "producer_policy_digest")
    command_id = _text("command_id", raw["command_id"])
    command_digest = _digest(raw["command_digest"], "command_digest")
    started = _parse_ts(raw["started_at"], "started_at")
    completed = _parse_ts(raw["completed_at"], "completed_at")
    if completed < started:
        raise EvidenceError("timestamp_order", "completed_at")
    duration = _plain_int(raw["duration_ms"], "duration_ms")
    expected_duration = int((completed - started).total_seconds() * 1000)
    if duration != expected_duration:
        raise EvidenceError("duration_mismatch", "duration_ms")
    exit_code = raw["exit_code"]
    if exit_code is not None:
        exit_code = _plain_int(exit_code, "exit_code")
    terminal = raw["terminal_state"]
    if not isinstance(terminal, str) or terminal not in _TERMINAL:
        raise EvidenceError("invalid_field", "terminal_state")
    if terminal == "passed" and exit_code != 0:
        raise EvidenceError("terminal_mismatch", "passed exit code")
    if terminal == "failed" and exit_code == 0:
        raise EvidenceError("terminal_mismatch", "failed exit code")
    counters = raw["counters"]
    if (not isinstance(counters, dict) or len(counters) > MAX_COUNTERS):
        raise EvidenceError("invalid_field", "counters")
    normalized_counters: dict[str, int] = {}
    for key, value in counters.items():
        if not isinstance(key, str) or _text("counter", key, maximum=64) is None:
            raise EvidenceError("invalid_field", "counter")
        normalized_counters[key] = _plain_int(value, "counter")
    artifacts = raw["artifact_digests"]
    if (not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACT_DIGESTS
            or any(_DIGEST.fullmatch(value) is None
                   for value in artifacts if isinstance(value, str))
            or any(not isinstance(value, str) for value in artifacts)):
        raise EvidenceError("invalid_field", "artifact_digests")
    tool = _text("tool", raw["tool"])
    runtime = _text("runtime", raw["runtime"])
    diagnostic = raw["diagnostic_category"]
    if not isinstance(diagnostic, str) or diagnostic not in _DIAGNOSTICS:
        raise EvidenceError("invalid_field", "diagnostic_category")
    nonce = _text("nonce", raw["nonce"])
    redaction = raw["redaction"]
    if (not isinstance(redaction, dict)
            or set(redaction) != {"applied", "secrets_removed", "logs_included"}
            or any(type(value) is not bool for value in redaction.values())):
        raise EvidenceError("invalid_field", "redaction")
    if (redaction["applied"] is not True
            or redaction["secrets_removed"] is not True
            or redaction["logs_included"] is not False):
        raise EvidenceError("redaction_required", "receipt redaction")
    claimed = _digest(raw["receipt_digest"], "receipt_digest")
    if claimed != receipt_digest(raw):
        raise EvidenceError("digest_mismatch", "receipt_digest")
    return EvidenceReceipt(
        schema_version=EVIDENCE_SCHEMA_VERSION, evidence_kind=kind,
        repository_id=identity.repository_id, worktree_root=identity.worktree_root,
        certification_base=identity.certification_base,
        current_head=identity.current_head, diff_hash=identity.diff_hash,
        stack_slice_id=identity.stack_slice_id,
        producer_policy_id=policy_id, producer_policy_digest=policy_digest,
        command_id=command_id, command_digest=command_digest,
        started_at=raw["started_at"], completed_at=raw["completed_at"],
        exit_code=exit_code, terminal_state=terminal, duration_ms=duration,
        counters=normalized_counters, artifact_digests=tuple(artifacts),
        tool=tool, runtime=runtime, diagnostic_category=diagnostic, nonce=nonce,
        redaction=dict(redaction), receipt_digest=claimed)


def parse_receipt(text: str) -> EvidenceReceipt:
    if not isinstance(text, str):
        raise EvidenceError("malformed_json", "receipt must be text")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise EvidenceError("malformed_json", "receipt is not UTF-8") from exc
    if encoded_size > MAX_RECEIPT_BYTES:
        raise EvidenceError("too_large", "receipt")
    try:
        raw = json.loads(text, object_pairs_hook=_pairs,
                         parse_constant=_no_constant)
    except EvidenceError:
        raise
    except (TypeError, UnicodeError, ValueError) as exc:
        raise EvidenceError("malformed_json", str(exc)) from exc
    if not isinstance(raw, dict):
        raise EvidenceError("invalid_field", "receipt object")
    return _validate_mapping(raw)


def _read_regular_file(path: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise EvidenceError("unsafe_file", "required safe-open flags unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow | nonblock
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError("unsafe_file", type(exc).__name__) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvidenceError("unsafe_file", "not a single-link regular file")
        if before.st_size > MAX_RECEIPT_BYTES:
            raise EvidenceError("too_large", "receipt")
        data = os.read(fd, MAX_RECEIPT_BYTES + 1)
        if len(data) > MAX_RECEIPT_BYTES:
            raise EvidenceError("too_large", "receipt")
        after_fd = os.fstat(fd)
        after_path = os.stat(path, follow_symlinks=False)
        fields = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                                value.st_nlink, value.st_size, value.st_mtime_ns)
        if fields(before) != fields(after_fd) or fields(after_fd) != fields(after_path):
            raise EvidenceError("unsafe_file", "receipt moved during read")
        return data
    except OSError as exc:
        raise EvidenceError("unsafe_file", type(exc).__name__) from exc
    finally:
        os.close(fd)


def load_receipt_file(path: Path) -> EvidenceReceipt:
    try:
        data = _read_regular_file(Path(path))
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("malformed_json", "receipt is not UTF-8") from exc
    return parse_receipt(text)


@dataclass(frozen=True)
class EvidenceVerification:
    accepted: bool
    reason_code: str

    @property
    def status(self) -> str:
        return "accepted" if self.accepted else "rejected"


def verify_receipt(receipt: EvidenceReceipt, expected: EvidenceIdentity,
                   protected_policy: ProducerPolicy) -> EvidenceVerification:
    try:
        _validate_mapping(receipt.canonical_mapping)
    except EvidenceError as exc:
        return EvidenceVerification(False, exc.reason_code)
    checks = (
        ("repository_mismatch", receipt.repository_id == expected.repository_id),
        ("worktree_mismatch", receipt.worktree_root == expected.worktree_root),
        ("base_mismatch", receipt.certification_base == expected.certification_base),
        ("head_mismatch", receipt.current_head == expected.current_head),
        ("diff_mismatch", receipt.diff_hash == expected.diff_hash),
        ("stack_slice_mismatch", receipt.stack_slice_id == expected.stack_slice_id),
    )
    for reason, matches in checks:
        if not matches:
            return EvidenceVerification(False, reason)
    if (receipt.producer_policy_id != protected_policy.policy_id
            or receipt.producer_policy_digest != protected_policy.digest):
        return EvidenceVerification(False, "policy_mismatch")
    command = protected_policy.command(receipt.command_id)
    if command is None or command.digest != receipt.command_digest:
        return EvidenceVerification(False, "command_mismatch")
    if receipt.terminal_state != "passed" or receipt.exit_code != 0:
        return EvidenceVerification(False, "producer_failed")
    return EvidenceVerification(True, "ok")
