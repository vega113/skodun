"""Exact-identity, bounded state for resumable batched orchestration.

Checkpoint payloads are deliberately not review artifacts.  This module owns
the strict serialization door used before such payloads reach their separate
store tables; it has no gate, triage, reuse, or delivery dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


PLANNER_VERSION = "skodun-batch-v1"

# Bounds are intentionally much wider than a normal normalized sub-review but
# finite.  The final review artifact already carries the same findings and
# attempt summaries; checkpoints must not become an unbounded transcript store.
MAX_TEXT_CHARS = 65_536
MAX_FINDINGS = 1_000
MAX_ATTEMPTS = 64
MAX_CHECKPOINT_JSON_BYTES = 2 * 1024 * 1024

_PAYLOAD_FIELDS = frozenset({
    "parse_ok", "degraded", "degraded_reason", "stop_reason",
    "diff_truncated", "summary", "findings", "failure_reason", "attempts",
    "provenance", "accepted",
})
_BOOL_FIELDS = ("parse_ok", "degraded", "diff_truncated")
_TEXT_FIELDS = ("degraded_reason", "summary", "failure_reason")
_FORBIDDEN_FIELDS = frozenset({
    "prompt", "prompt_text", "full_prompt", "transcript", "stdout", "stderr",
    "environment", "environment_values", "env_values", "full_path", "path_env",
})


def _text(label: str, value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"{label} exceeds {MAX_TEXT_CHARS} characters")
    return value


def _plain_int(label: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class PassIdentity:
    """Identity of one planned batch or integration provider invocation."""

    kind: str
    index: int
    prompt_hash: str | None
    diff_hash: str
    boundary_hash: str

    def __post_init__(self) -> None:
        if self.kind not in ("batch", "integration"):
            raise ValueError(f"unknown checkpoint pass kind {self.kind!r}")
        minimum = 1 if self.kind == "batch" else 0
        _plain_int("pass index", self.index, minimum=minimum)
        if self.kind == "integration" and self.index != 0:
            raise ValueError("integration pass index must be 0")
        if self.kind == "batch" and self.prompt_hash is None:
            raise ValueError("a batch prompt_hash is required")
        _text("prompt_hash", self.prompt_hash, optional=True)
        _text("diff_hash", self.diff_hash)
        _text("boundary_hash", self.boundary_hash)


_IDENTITY_FIELDS = (
    "repo_id", "worktree_root", "branch", "head", "base_ref", "base_sha",
    "diff_hash", "tree_fingerprint", "context_hash", "checklist_hash",
    "reviewer_hash", "config_hash", "policy_hash", "planner_version",
    "batch_budget", "batch_count", "boundary_digest",
    "integration_plan_digest", "pass_identities",
)


@dataclass(frozen=True)
class OrchestrationIdentity:
    """Complete deterministic identity required for checkpoint reuse."""

    repo_id: str
    worktree_root: str
    branch: str
    head: str
    base_ref: str
    base_sha: str
    diff_hash: str
    tree_fingerprint: str
    context_hash: str | None
    checklist_hash: str | None
    reviewer_hash: str
    config_hash: str
    policy_hash: str
    planner_version: str
    batch_budget: int
    batch_count: int
    boundary_digest: str
    integration_plan_digest: str
    pass_identities: tuple[PassIdentity, ...]

    def __post_init__(self) -> None:
        for name in (
                "repo_id", "worktree_root", "branch", "head", "base_ref",
                "base_sha", "diff_hash", "tree_fingerprint", "reviewer_hash",
                "config_hash", "policy_hash", "planner_version",
                "boundary_digest", "integration_plan_digest"):
            _text(name, getattr(self, name))
        _text("context_hash", self.context_hash, optional=True)
        _text("checklist_hash", self.checklist_hash, optional=True)
        _plain_int("batch_budget", self.batch_budget, minimum=1)
        _plain_int("batch_count", self.batch_count, minimum=1)
        if (not isinstance(self.pass_identities, tuple)
                or not self.pass_identities
                or any(not isinstance(value, PassIdentity)
                       for value in self.pass_identities)):
            raise ValueError("pass_identities must be a non-empty tuple of PassIdentity")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def first_mismatch(left: OrchestrationIdentity,
                   right: OrchestrationIdentity) -> str | None:
    """Return the first exact identity field that differs, in stable order."""
    if not isinstance(left, OrchestrationIdentity) \
            or not isinstance(right, OrchestrationIdentity):
        raise ValueError("first_mismatch requires two OrchestrationIdentity values")
    for field in _IDENTITY_FIELDS:
        if getattr(left, field) != getattr(right, field):
            return field
    return None


def _scan_json(value: Any, *, label: str) -> None:
    """Refuse secret/transcript-shaped fields and non-JSON/unbounded strings."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-string object key")
            if key.casefold() in _FORBIDDEN_FIELDS:
                raise ValueError(f"{label} contains forbidden field {key!r}")
            _scan_json(item, label=f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_json(item, label=f"{label}[{index}]")
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT_CHARS:
            raise ValueError(f"{label} exceeds {MAX_TEXT_CHARS} characters")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ValueError(f"{label} contains non-JSON value {type(value).__name__}")


@dataclass(frozen=True)
class CheckpointPayload:
    """Validated canonical JSON for one terminal normalized sub-review."""

    json_text: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CheckpointPayload":
        if not isinstance(raw, Mapping):
            raise ValueError("checkpoint payload must be an object")
        unknown = set(raw) - _PAYLOAD_FIELDS
        missing = _PAYLOAD_FIELDS - set(raw)
        if unknown:
            raise ValueError(f"checkpoint payload has unknown fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"checkpoint payload is missing fields: {sorted(missing)}")
        for name in _BOOL_FIELDS:
            if type(raw[name]) is not bool:
                raise ValueError(
                    f"checkpoint payload {name} must be bool, got "
                    f"{type(raw[name]).__name__}")
        for name in _TEXT_FIELDS:
            if not isinstance(raw[name], str):
                raise ValueError(f"checkpoint payload {name} must be a string")
            if len(raw[name]) > MAX_TEXT_CHARS:
                raise ValueError(
                    f"checkpoint payload {name} exceeds {MAX_TEXT_CHARS} characters")
        if not isinstance(raw["findings"], list) \
                or any(not isinstance(item, Mapping) for item in raw["findings"]):
            raise ValueError("checkpoint payload findings must be a list of objects")
        if len(raw["findings"]) > MAX_FINDINGS:
            raise ValueError(
                f"checkpoint payload findings exceeds {MAX_FINDINGS} entries")
        if not isinstance(raw["attempts"], list) \
                or any(not isinstance(item, Mapping) for item in raw["attempts"]):
            raise ValueError("checkpoint payload attempts must be a list of objects")
        if len(raw["attempts"]) > MAX_ATTEMPTS:
            raise ValueError(
                f"checkpoint payload attempts exceeds {MAX_ATTEMPTS} entries")
        if not isinstance(raw["provenance"], Mapping):
            raise ValueError("checkpoint payload provenance must be an object")
        if raw["accepted"] is not None and not isinstance(raw["accepted"], Mapping):
            raise ValueError("checkpoint payload accepted must be an object or null")
        _scan_json(raw, label="checkpoint payload")
        try:
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"checkpoint payload is not canonical JSON: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_CHECKPOINT_JSON_BYTES:
            raise ValueError(
                f"checkpoint payload exceeds {MAX_CHECKPOINT_JSON_BYTES} bytes")
        return cls(text)

    def __post_init__(self) -> None:
        if not isinstance(self.json_text, str):
            raise ValueError("checkpoint json_text must be a string")
        if len(self.json_text.encode("utf-8")) > MAX_CHECKPOINT_JSON_BYTES:
            raise ValueError(
                f"checkpoint payload exceeds {MAX_CHECKPOINT_JSON_BYTES} bytes")

    def as_dict(self) -> dict[str, Any]:
        value = json.loads(self.json_text)
        if not isinstance(value, dict):  # defensive against direct construction
            raise ValueError("checkpoint payload JSON must decode to an object")
        return value

