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
        }


def _pass_state(value: object, default: str = "not_planned") -> str:
    state = value if isinstance(value, str) else default
    return state if state in PASS_STATES else "failed"


def project_review(rec: Mapping, *, orchestration: Mapping | None = None,
                   checkpoints: Sequence[Mapping] = ()) -> CoverageProjection:
    """Derive bounded coverage/pass/gate fields without changing trust."""
    batches = rec.get("batches")
    batch_count = int((orchestration or {}).get("batch_count") or
                      (len(batches) if isinstance(batches, list) else 0))
    checkpoint_rows = list(checkpoints)
    planned = batch_count + (1 if any(
        row.get("pass_kind") == "integration" for row in checkpoint_rows) else 0)
    if planned == 0:
        planned = 1
    complete_rows = [r for r in checkpoint_rows if r.get("state") == "complete"]
    failed_rows = [r for r in checkpoint_rows if r.get("state") == "failed"]
    completed = len(complete_rows)
    failed = len(failed_rows)
    parseable = bool(rec.get("usable_output")) or any(
        isinstance(b, Mapping) and b.get("parse_ok") is True
        for b in (batches or []) if isinstance(batches, list))
    if not checkpoint_rows and isinstance(batches, list):
        completed = sum(1 for b in batches if isinstance(b, Mapping) and
                        b.get("parse_ok") is True)
        failed = sum(1 for b in batches if isinstance(b, Mapping) and
                     b.get("parse_ok") is False)
    orchestration_state = (orchestration or {}).get("state")
    complete = (orchestration_state == "consumed" or
                (not orchestration_state and rec.get("status") in
                 {"clean", "findings"}) or
                (planned > 0 and completed == planned and not failed))
    coverage_state = "complete" if complete else ("partial" if parseable else "none")
    extras = rec.get("extra_passes")
    extras = extras if isinstance(extras, Mapping) else {}
    passes = {
        "finder": "complete" if parseable else _pass_state(rec.get("status"), "failed"),
        "integration": "not_planned",
        "security": _pass_state((extras.get("security") or {}).get("status")),
        "skeptic": _pass_state((extras.get("skeptic") or {}).get("status")),
        "refuter": _pass_state((extras.get("refuter") or {}).get("status")),
    }
    for row in checkpoint_rows:
        key = "finder" if row.get("pass_kind") == "batch" else "integration"
        if key == "finder" and row.get("state") == "running":
            passes[key] = "running"
        elif key == "integration":
            passes[key] = _pass_state(row.get("state"))
    next_pass = next((i + 1 for i, row in enumerate(checkpoint_rows)
                      if row.get("state") in {"pending", "failed"}), None)
    required_fail = any(passes[k] == "failed" for k in ("integration", "security", "skeptic"))
    eligible = coverage_state == "complete" and rec.get("trustworthy") is True and not required_fail
    reason = "eligible" if eligible else (
        "coverage_incomplete" if coverage_state != "complete" else
        "untrustworthy" if rec.get("trustworthy") is not True else "required_pass_failed")
    finder_only = (orchestration_state is None and passes["integration"] == "not_planned")
    refuter_available = passes["refuter"] in {"complete", "degraded"}
    cross_provider = passes["integration"] == "complete"
    return CoverageProjection(coverage_state, parseable, eligible, reason,
                              planned, completed, failed, next_pass, passes,
                              finder_only, cross_provider, refuter_available)
