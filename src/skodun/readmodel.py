"""Pure review coverage projection for status consumers.

The projection is a read model only: checkpoints and pass metadata describe
what ran, while the existing ``trustworthy`` field remains the sole trust
decision used by gate.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence


PASS_STATES = frozenset({
    "not_planned", "queued", "running", "complete", "unavailable",
    "degraded", "failed", "skipped",
})


@dataclass(frozen=True)
class CoverageProjection:
    coverage_state: str
    usable_evidence: bool
    gate_eligible: bool
    gate_reason: str
    planned_passes: int
    completed_passes: int
    failed_passes: int
    next_resumable_pass: int | None
    passes: Mapping[str, str]
    finder_only: bool
    cross_provider_complete: bool
    refuter_annotation_available: bool
    batch_count: int = 0
    prompt_bytes: int | None = None
    planner_version: str | None = None
    boundary_digest: str | None = None

    def to_dict(self) -> dict:
        return {
            "coverage_state": self.coverage_state,
            "usable_evidence": self.usable_evidence,
            "gate_eligible": self.gate_eligible,
            "gate_reason": self.gate_reason,
            "planned_passes": self.planned_passes,
            "completed_passes": self.completed_passes,
            "failed_passes": self.failed_passes,
            "next_resumable_pass": self.next_resumable_pass,
            "passes": dict(self.passes),
            "finder_only": self.finder_only,
            "cross_provider_complete": self.cross_provider_complete,
            "refuter_annotation_available": self.refuter_annotation_available,
            "batch_count": self.batch_count,
            "prompt_bytes": self.prompt_bytes,
            "planner_version": self.planner_version,
            "boundary_digest": self.boundary_digest,
        }


def _pass_state(value: object, default: str = "not_planned") -> str:
    state = value if isinstance(value, str) else default
    if state == "pending":
        return "queued"
    return state if state in PASS_STATES else "failed"


def _nonnegative_int(value: object) -> int | None:
    """Accept only persisted non-negative integers, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _checkpoint_payload(row: Mapping) -> Mapping | None:
    """Decode one bounded completed payload for read-only partial evidence."""
    if row.get("state") != "complete":
        return None
    raw = row.get("payload_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _extra_pass_state(value: object, *, refuter: bool = False) -> str:
    """Normalize legacy optional-pass metadata without treating missing fields as success."""
    if not isinstance(value, Mapping):
        return "not_planned"
    status = value.get("status")
    if value.get("failed") is True:
        return "failed"
    if "parse_ok" in value and value.get("parse_ok") is not True:
        return "failed"
    if status == "pending":
        return "queued"
    if status in {"failed", "unavailable"}:
        return status
    if status == "degraded" or value.get("degraded") is True:
        return "degraded"
    if status in {"ran", "complete"}:
        if refuter and value.get("ran") is True and "parse_ok" not in value:
            return "complete"
        return "complete" if value.get("parse_ok") is True else "failed"
    if value.get("ran") is True:
        return "complete" if value.get("parse_ok") is True else "failed"
    return _pass_state(status)


def _checkpoint_state(row: Mapping, rec: Mapping) -> str:
    """Project a terminal checkpoint from its payload when one is present."""
    state = row.get("state")
    if state != "complete":
        return _pass_state(state)
    payload = _checkpoint_payload(row)
    if payload is not None:
        if payload.get("parse_ok") is not True:
            return "failed"
        return "degraded" if payload.get("degraded") is True else "complete"
    if row.get("pass_kind") == "integration":
        integration = rec.get("integration")
        if (isinstance(integration, Mapping)
                and "parse_ok" in integration):
            if integration.get("parse_ok") is not True:
                return "failed"
            return ("degraded" if integration.get("degraded") is True
                    else "complete")
    return "failed"


def project_review(rec: Mapping, *, orchestration: Mapping | None = None,
                   checkpoints: Sequence[Mapping] = ()) -> CoverageProjection:
    """Derive bounded coverage/pass/gate fields without changing trust."""
    batches = rec.get("batches")
    batches = batches if isinstance(batches, list) else []
    batch_count = int((orchestration or {}).get("batch_count") or
                      len(batches))
    checkpoint_rows = list(checkpoints)
    integration_record = rec.get("integration")
    has_checkpoint_integration = any(
        row.get("pass_kind") == "integration" for row in checkpoint_rows)
    planned = batch_count + (1 if any(
        row.get("pass_kind") == "integration" for row in checkpoint_rows) else
        (1 if isinstance(integration_record, Mapping) else 0))
    if planned == 0:
        planned = 1
    checkpoint_states = [_checkpoint_state(row, rec) for row in checkpoint_rows]
    complete_rows = [state for state in checkpoint_states
                     if state in {"complete", "degraded"}]
    failed_rows = [state for state in checkpoint_states if state == "failed"]
    completed = len(complete_rows)
    failed = len(failed_rows)
    checkpoint_payloads = [(row.get("pass_kind"), payload)
                           for row in checkpoint_rows
                           for payload in [_checkpoint_payload(row)]
                           if payload is not None]
    integration_evidence = ([integration_record]
                             if isinstance(integration_record, Mapping) else [])
    evidence_batches = ((batches + integration_evidence) if batches else
                        [payload for _, payload in checkpoint_payloads]
                        + integration_evidence)
    finder_evidence = batches or [payload for kind, payload in checkpoint_payloads
                                  if kind == "batch"]
    parseable = rec.get("usable_output") is True or any(
        isinstance(b, Mapping) and b.get("parse_ok") is True
        for b in evidence_batches)
    finder_parseable = ((rec.get("usable_output") is True and not batches)
                        or any(
        isinstance(b, Mapping) and b.get("parse_ok") is True
        for b in finder_evidence))
    if not checkpoint_rows and isinstance(batches, list):
        completed = sum(1 for b in batches if isinstance(b, Mapping) and
                        b.get("parse_ok") is True)
        failed = sum(1 for b in batches if isinstance(b, Mapping) and
                     b.get("parse_ok") is False)
        if isinstance(integration_record, Mapping) and not has_checkpoint_integration:
            integration_state = _extra_pass_state(integration_record)
            if integration_state in {"complete", "degraded"}:
                completed += 1
            elif integration_state == "failed":
                failed += 1
        if not batches and parseable:
            completed = 1
    orchestration_state = (orchestration or {}).get("state")
    consumed_complete = (orchestration_state == "consumed" and not failed and
                          (completed == planned or
                           (not checkpoint_rows and parseable)))
    complete = (consumed_complete or
                (not orchestration_state and rec.get("status") in
                 {"clean", "findings"} and parseable) or
                (planned > 0 and completed == planned and not failed))
    coverage_state = "complete" if complete else ("partial" if parseable else "none")
    extras = rec.get("extra_passes")
    extras = extras if isinstance(extras, Mapping) else {}
    passes = {
        "finder": "complete" if finder_parseable else _pass_state(rec.get("status"), "failed"),
        "integration": "not_planned",
        "security": _extra_pass_state(extras.get("security")),
        "skeptic": _extra_pass_state(extras.get("skeptic")),
        "refuter": _extra_pass_state(extras.get("refuter"), refuter=True),
    }
    if (isinstance(integration_record, Mapping)
            and not has_checkpoint_integration):
        passes["integration"] = _extra_pass_state(integration_record)
    if not checkpoint_rows and batches:
        batch_states = [
            ("failed" if not isinstance(batch, Mapping)
             or batch.get("parse_ok") is not True
             else "degraded" if batch.get("degraded") is True
             else "complete")
            for batch in batches]
        if "failed" in batch_states:
            passes["finder"] = "failed"
        elif "degraded" in batch_states:
            passes["finder"] = "degraded"
        elif batch_states:
            passes["finder"] = "complete"
    batch_checkpoint_states = [checkpoint_state for row, checkpoint_state in
                               zip(checkpoint_rows, checkpoint_states)
                               if row.get("pass_kind") == "batch"]
    if batch_checkpoint_states:
        if "failed" in batch_checkpoint_states:
            passes["finder"] = "failed"
        elif "queued" in batch_checkpoint_states:
            passes["finder"] = "queued"
        elif "running" in batch_checkpoint_states:
            passes["finder"] = "running"
        elif "degraded" in batch_checkpoint_states:
            passes["finder"] = "degraded"
        elif all(state == "complete" for state in batch_checkpoint_states):
            passes["finder"] = "complete"
    for row, checkpoint_state in zip(checkpoint_rows, checkpoint_states):
        key = "finder" if row.get("pass_kind") == "batch" else "integration"
        if (key == "finder" and row.get("state") == "running"
                and passes[key] != "failed"):
            passes[key] = "running"
        elif key == "integration":
            passes[key] = checkpoint_state
    next_pass = None
    for row in checkpoint_rows:
        if row.get("state") in {"pending", "failed"}:
            next_pass = (row.get("pass_index") if row.get("pass_kind") == "batch"
                         else batch_count + 1)
            break
    required_fail = any(passes[k] == "failed" for k in ("integration", "security", "skeptic"))
    eligible = coverage_state == "complete" and rec.get("trustworthy") is True and not required_fail
    reason = "eligible" if eligible else (
        "coverage_incomplete" if coverage_state != "complete" else
        "required_pass_failed" if required_fail else
        "untrustworthy" if rec.get("trustworthy") is not True else "required_pass_failed")
    finder_only = (orchestration_state is None and
                   passes["integration"] == "not_planned")
    refuter_available = passes["refuter"] in {"complete", "degraded"}
    cross_provider = passes["integration"] == "complete"
    telemetry_rows = [b.get("telemetry") for b in (batches or [])
                      if isinstance(b, Mapping) and
                      isinstance(b.get("telemetry"), Mapping)]
    integration = rec.get("integration")
    if (isinstance(integration, Mapping)
            and isinstance(integration.get("telemetry"), Mapping)):
        telemetry_rows.append(integration["telemetry"])
    prompt_values = []
    for row in telemetry_rows:
        dimensions = row.get("bytes")
        if not isinstance(dimensions, Mapping):
            continue
        value = _nonnegative_int(dimensions.get("prompt"))
        if value is not None:
            prompt_values.append(value)
    prompt_bytes = sum(prompt_values) if prompt_values else None
    first = telemetry_rows[0] if telemetry_rows else {}
    plan = rec.get("batch_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    planner_version = plan.get("planner_version")
    if not isinstance(planner_version, str):
        planner_version = first.get("planner_version")
    boundary_digest = plan.get("boundary_digest")
    if not isinstance(boundary_digest, str):
        boundary_digest = first.get("boundary_digest")
    return CoverageProjection(
        coverage_state, parseable, eligible, reason,
        planned, completed, failed, next_pass, passes,
        finder_only, cross_provider, refuter_available,
        batch_count=batch_count,
        prompt_bytes=prompt_bytes,
        planner_version=(planner_version if isinstance(planner_version, str)
                         else None),
        boundary_digest=(boundary_digest if isinstance(boundary_digest, str)
                         else None),
    )
