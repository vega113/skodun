"""Exact-identity, bounded state for resumable batched orchestration.

Checkpoint payloads are deliberately not review artifacts.  This module owns
the strict serialization door used before such payloads reach their separate
store tables; it has no gate, triage, reuse, or delivery dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
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
    "path", "path_value", "path_values", "executable_path", "argv", "command",
    "binary",
})
_FINDING_FIELD_EXCEPTIONS = frozenset({
    "path", "path_value", "path_values", "executable_path", "argv",
    "command", "binary",
})


def canonical_digest(value: Any) -> str:
    """SHA-256 of deterministic JSON identity material."""
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"identity material is not canonical JSON: {exc}") from exc
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
        if self.kind not in ("batch", "integration", "security", "skeptic"):
            raise ValueError(f"unknown checkpoint pass kind {self.kind!r}")
        minimum = 1 if self.kind == "batch" else 0
        _plain_int("pass index", self.index, minimum=minimum)
        if self.kind != "batch" and self.index != 0:
            raise ValueError("non-batch pass index must be 0")
        if self.kind == "batch" and self.prompt_hash is None:
            raise ValueError("a batch prompt_hash is required")
        _text("prompt_hash", self.prompt_hash, optional=True)
        _text("diff_hash", self.diff_hash)
        _text("boundary_hash", self.boundary_hash)


_IDENTITY_FIELDS = (
    "repo_id", "worktree_root", "branch", "head", "base_ref", "base_sha",
    "diff_hash", "tree_fingerprint", "context_hash", "checklist_hash",
    "reviewer_hash", "planning_policy", "batch_budget", "config_hash", "policy_hash", "planner_version",
    "batch_count", "boundary_digest",
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
    continuation_source: str | None = None
    planning_policy: dict | None = None

    def __post_init__(self) -> None:
        for name in (
                "repo_id", "worktree_root", "branch", "head", "base_ref",
                "base_sha", "diff_hash", "tree_fingerprint", "reviewer_hash",
                "config_hash", "policy_hash", "planner_version",
                "boundary_digest", "integration_plan_digest"):
            _text(name, getattr(self, name))
        from .planning_policy import validate as validate_planning_policy
        validate_planning_policy(self.planning_policy)
        _text("continuation_source", self.continuation_source, optional=True)
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
        fields = asdict(self)
        if self.continuation_source is None:
            fields.pop('continuation_source')  # Preserve existing identity bytes.
        if self.planning_policy is None:
            fields.pop('planning_policy')  # Unknown legacy plans keep their bytes.
        return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> "OrchestrationIdentity":
        try:
            raw = json.loads(text)
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError(f"invalid orchestration identity JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("orchestration identity JSON must be an object")
        raw.setdefault("planning_policy", None)
        expected = set(_IDENTITY_FIELDS)
        if 'continuation_source' in raw:
            expected.add('continuation_source')
        if set(raw) != expected:
            raise ValueError(
                "orchestration identity fields differ: "
                f"missing={sorted(expected - set(raw))} "
                f"unknown={sorted(set(raw) - expected)}")
        passes = raw.pop("pass_identities")
        if not isinstance(passes, list):
            raise ValueError("pass_identities must be an array")
        try:
            raw["pass_identities"] = tuple(
                PassIdentity(**value) if isinstance(value, dict) else value
                for value in passes)
            return cls(**raw)
        except TypeError as exc:
            raise ValueError(f"invalid orchestration identity fields: {exc}") from exc


def first_mismatch(left: OrchestrationIdentity,
                   right: OrchestrationIdentity) -> str | None:
    """Return the first exact content field that differs, in stable order.

    continuation_source namespaces generation ownership and is included in the
    digest, but is not content compatibility. Only the explicit owned fork path
    may seed a different generation after every field below matches.
    """
    if not isinstance(left, OrchestrationIdentity) \
            or not isinstance(right, OrchestrationIdentity):
        raise ValueError("first_mismatch requires two OrchestrationIdentity values")
    for field in _IDENTITY_FIELDS:
        if getattr(left, field) != getattr(right, field):
            if field == "planning_policy":
                from .planning_policy import mismatch
                if right.planning_policy is None:
                    return "planning_identity_missing"
                return mismatch(left.planning_policy, right.planning_policy)
            return field
    return None


def _scan_json(value: Any, *, label: str,
               forbidden_fields: frozenset[str] = _FORBIDDEN_FIELDS) -> None:
    """Refuse secret/transcript-shaped fields and non-JSON/unbounded strings."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-string object key")
            if key.casefold() in forbidden_fields:
                raise ValueError(f"{label} contains forbidden field {key!r}")
            _scan_json(item, label=f"{label}.{key}",
                       forbidden_fields=forbidden_fields)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_json(item, label=f"{label}[{index}]",
                       forbidden_fields=forbidden_fields)
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT_CHARS:
            raise ValueError(f"{label} exceeds {MAX_TEXT_CHARS} characters")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ValueError(f"{label} contains non-JSON value {type(value).__name__}")


def _optional_text(value: object, *, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{label} must be a non-empty string or null")


def _attempt_shape(value: object, *, index: int) -> None:
    label = f"checkpoint payload attempts[{index}]"
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    required = {
        "n", "provider", "model", "effort", "rc", "timed_out",
        "duration_sec", "first_output_sec", "classification",
    }
    missing = required - set(value)
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    if type(value["n"]) is not int or value["n"] < 1:
        raise ValueError(f"{label}.n must be a positive integer")
    for name in ("provider", "model"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"{label}.{name} must be a non-empty string")
    _optional_text(value["effort"], label=f"{label}.effort")
    for name in ("rc",):
        if value[name] is not None and type(value[name]) is not int:
            raise ValueError(f"{label}.{name} must be an integer or null")
    if value["timed_out"] is not None and type(value["timed_out"]) is not bool:
        raise ValueError(f"{label}.timed_out must be bool or null")
    for name in ("duration_sec", "first_output_sec"):
        number = value[name]
        if number is not None:
            try:
                valid_number = (not isinstance(number, bool)
                                and isinstance(number, (int, float))
                                and math.isfinite(float(number))
                                and number >= 0)
            except OverflowError:
                valid_number = False
            if not valid_number:
                raise ValueError(
                    f"{label}.{name} must be a finite number or null")
    classification = value["classification"]
    if classification is not None:
        if not isinstance(classification, Mapping):
            raise ValueError(f"{label}.classification must be an object or null")
        for name in ("kind", "category", "detail"):
            if not isinstance(classification.get(name), str):
                raise ValueError(f"{label}.classification.{name} must be a string")
        if classification["kind"] not in ("ok", "degraded", "unavailable"):
            raise ValueError(f"{label}.classification.kind is unknown")
    if "skipped" in value:
        if not isinstance(value["skipped"], str) or not value["skipped"]:
            raise ValueError(f"{label}.skipped must be a non-empty string")
    if "usage" in value and not isinstance(value["usage"], Mapping):
        raise ValueError(f"{label}.usage must be an object")


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
        if not isinstance(raw["findings"], list):
            raise ValueError("checkpoint payload findings must be a list")
        if len(raw["findings"]) > MAX_FINDINGS:
            raise ValueError(
                f"checkpoint payload findings exceeds {MAX_FINDINGS} entries")
        # Reuse the adapter's trust-critical review validator rather than
        # accepting arbitrary mappings that later aggregation would quietly
        # ignore. A malformed finding must make the checkpoint unusable.
        from .adapters.base import _valid_payload
        if not _valid_payload({"summary": raw["summary"],
                               "findings": raw["findings"]}):
            raise ValueError("checkpoint payload findings have invalid shape")
        if not isinstance(raw["attempts"], list):
            raise ValueError("checkpoint payload attempts must be a list")
        if len(raw["attempts"]) > MAX_ATTEMPTS:
            raise ValueError(
                f"checkpoint payload attempts exceeds {MAX_ATTEMPTS} entries")
        for index, attempt in enumerate(raw["attempts"]):
            _attempt_shape(attempt, index=index)
        if not isinstance(raw["provenance"], Mapping):
            raise ValueError("checkpoint payload provenance must be an object")
        if raw["accepted"] is not None and not isinstance(raw["accepted"], Mapping):
            raise ValueError("checkpoint payload accepted must be an object or null")
        # Finding records are provider-defined review metadata.  Their
        # established ``path``/``command`` fields are valid and already part
        # of the final review artifact; keep the sensitive-field door strict
        # for provenance, attempts, and every other checkpoint field.
        finding_forbidden = frozenset(
            _FORBIDDEN_FIELDS - _FINDING_FIELD_EXCEPTIONS)
        for name, value in raw.items():
            _scan_json(
                value, label=f"checkpoint payload.{name}",
                forbidden_fields=(finding_forbidden
                                  if name == "findings" else _FORBIDDEN_FIELDS))
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


def payload_from_sub(sub: object) -> CheckpointPayload:
    """Copy the normalized pipeline `_Sub` vocabulary through the strict door."""
    raw = {name: getattr(sub, name) for name in _PAYLOAD_FIELDS}
    return CheckpointPayload.from_mapping(raw)


def sub_fields_from_payload(payload: CheckpointPayload) -> dict[str, Any]:
    """A fresh mapping suitable for constructing pipeline `_Sub`."""
    if not isinstance(payload, CheckpointPayload):
        raise ValueError("payload must be a CheckpointPayload")
    return CheckpointPayload.from_mapping(payload.as_dict()).as_dict()



def usable_payload(payload: CheckpointPayload) -> bool:
    """Only validated, parsed, complete and non-degraded evidence is reusable."""
    value = sub_fields_from_payload(payload)
    return (value['parse_ok'] is True and value['degraded'] is False
            and value['diff_truncated'] is False and not value['failure_reason'])
