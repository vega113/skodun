"""Coverage projection contract: incomplete evidence stays visible and ineligible."""

from skodun.checkpoints import CheckpointPayload
from skodun.readmodel import project_review


def _record(**changes):
    rec = {"id": "r1", "status": "failed", "trustworthy": False,
           "usable_output": False, "batches": [], "extra_passes": {}}
    rec.update(changes)
    return rec


def test_partial_finder_evidence_reports_next_pass_and_not_gate_eligible():
    p = project_review(
        _record(usable_output=True, batches=[{"parse_ok": True},
                                              {"parse_ok": True},
                                              {"parse_ok": True}]),
        orchestration={"state": "active", "batch_count": 4},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete"},
            {"pass_kind": "batch", "pass_index": 2, "state": "complete"},
            {"pass_kind": "batch", "pass_index": 3, "state": "complete"},
            {"pass_kind": "batch", "pass_index": 4, "state": "pending"},
        ])
    assert p.coverage_state == "partial"
    assert p.usable_evidence is True
    assert p.gate_eligible is False
    assert p.completed_passes == 3 and p.planned_passes == 4
    assert p.next_resumable_pass == 4


def test_complete_clean_finder_is_eligible_without_optional_refuter():
    p = project_review(_record(status="clean", trustworthy=True,
                               usable_output=True),
                       orchestration=None, checkpoints=[])
    assert p.coverage_state == "complete"
    assert p.gate_eligible is True
    assert p.finder_only is True
    assert p.passes["refuter"] == "not_planned"


def test_failed_annotation_refuter_does_not_demote_finder_projection():
    p = project_review(_record(status="clean", trustworthy=True,
                               usable_output=True,
                               extra_passes={"refuter": {"status": "failed"}}))
    assert p.gate_eligible is True
    assert p.passes["refuter"] == "failed"


def test_required_security_failure_blocks_eligibility():
    p = project_review(_record(status="clean", trustworthy=True,
                               usable_output=True,
                               extra_passes={"security": {"status": "failed"}}))
    assert p.gate_eligible is False
    assert p.gate_reason == "required_pass_failed"


def test_no_output_failure_is_none_not_clean():
    p = project_review(_record(status="failed", trustworthy=False))
    assert p.coverage_state == "none"
    assert p.gate_eligible is False


def test_malformed_prompt_bytes_are_ignored_and_plan_digest_wins():
    rec = _record(
        status="clean", trustworthy=True, usable_output=True,
        batch_plan={"planner_version": "plan-v1", "boundary_digest": "plan"},
        batches=[
            {"parse_ok": True, "telemetry": {"bytes": {"prompt": 12},
                                                 "boundary_digest": "slice"}},
            {"parse_ok": True, "telemetry": {"bytes": {"prompt": "bad"}}},
            {"parse_ok": True, "telemetry": {"bytes": {"prompt": True}}},
            {"parse_ok": True, "telemetry": {"bytes": {"prompt": -1}}},
            {"parse_ok": True, "telemetry": {"bytes": "bad"}},
        ])
    p = project_review(rec)
    assert p.prompt_bytes == 12
    assert p.planner_version == "plan-v1"
    assert p.boundary_digest == "plan"


def _checkpoint_payload(**changes) -> str:
    data = {
        "parse_ok": True,
        "degraded": False,
        "degraded_reason": "",
        "stop_reason": "EndTurn",
        "diff_truncated": False,
        "summary": "batch reviewed",
        "findings": [{
            "file": "src/a.py", "line": 3, "severity": "medium",
            "category": "correctness", "title": "Bad edge",
            "detail": "The empty case is not handled.",
        }],
        "failure_reason": "",
        "attempts": [],
        "provenance": {"provider": "xai", "model": "grok", "effort": None,
                       "note": ""},
        "accepted": None,
    }
    data.update(changes)
    return CheckpointPayload.from_mapping(data).json_text


def test_completed_checkpoint_payload_is_usable_when_artifact_batches_are_empty():
    payload = _checkpoint_payload()
    p = project_review(
        _record(status="running", batches=[]),
        orchestration={"state": "active", "batch_count": 4},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete",
             "payload_json": payload},
            {"pass_kind": "batch", "pass_index": 2, "state": "complete",
             "payload_json": payload},
            {"pass_kind": "batch", "pass_index": 3, "state": "complete",
             "payload_json": payload},
            {"pass_kind": "batch", "pass_index": 4, "state": "pending"},
            {"pass_kind": "integration", "pass_index": 0, "state": "pending"},
        ])
    assert p.coverage_state == "partial"
    assert p.usable_evidence is True
    assert p.gate_eligible is False
    assert p.completed_passes == 3 and p.planned_passes == 5
    assert p.passes["integration"] == "queued"
    assert p.next_resumable_pass == 4


def test_pending_checkpoint_is_queued_never_failed():
    p = project_review(
        _record(status="running"),
        orchestration={"state": "active", "batch_count": 1},
        checkpoints=[{"pass_kind": "integration", "pass_index": 0,
                      "state": "pending"}])
    assert p.passes["integration"] == "queued"
    assert p.failed_passes == 0
    assert p.gate_eligible is False


def test_security_and_skeptic_use_persisted_ran_parse_ok_shape():
    p = project_review(_record(
        status="clean", trustworthy=True, usable_output=True,
        extra_passes={
            "security": {"ran": True, "parse_ok": True, "degraded": False},
            "skeptic": {"ran": True, "parse_ok": True, "degraded": False},
            "refuter": {"ran": True, "status": "ran", "degraded": False,
                        "failed": False},
        }))
    assert p.passes["security"] == "complete"
    assert p.passes["skeptic"] == "complete"
    assert p.passes["refuter"] == "complete"
    assert p.refuter_annotation_available is True
    assert p.gate_eligible is True


def test_ran_status_does_not_override_failed_or_unparsed_extra_passes():
    failed = project_review(_record(
        status="clean", trustworthy=True, usable_output=True,
        extra_passes={"refuter": {"status": "ran", "failed": True}}))
    unparsed = project_review(_record(
        status="clean", trustworthy=True, usable_output=True,
        extra_passes={"security": {"ran": True, "status": "ran",
                                   "parse_ok": False}}))
    assert failed.passes["refuter"] == "failed"
    assert failed.refuter_annotation_available is False
    assert unparsed.passes["security"] == "failed"
    assert unparsed.gate_eligible is False


def test_unbatched_completed_finder_counts_one_planned_and_one_completed_pass():
    p = project_review(_record(status="clean", trustworthy=True,
                               usable_output=True))
    assert p.planned_passes == 1
    assert p.completed_passes == 1
    assert p.failed_passes == 0
    assert p.finder_only is True


def test_string_false_usable_output_is_not_evidence():
    p = project_review(_record(status="failed", usable_output="false",
                               parse_ok=False, batches=[]))
    assert p.usable_evidence is False
    assert p.coverage_state == "none"


def test_string_false_usable_output_cannot_make_a_clean_review_eligible():
    p = project_review(_record(status="clean", trustworthy=True,
                               usable_output="false", parse_ok=False, batches=[]))
    assert p.usable_evidence is False
    assert p.coverage_state != "complete"
    assert p.gate_eligible is False


def test_integration_prompt_bytes_join_batch_totals():
    rec = _record(
        status="clean", trustworthy=True, usable_output=True,
        batches=[{"parse_ok": True,
                  "telemetry": {"bytes": {"prompt": 10}}}],
        integration={"telemetry": {"bytes": {"prompt": 7}}})
    p = project_review(rec, orchestration={"state": "consumed", "batch_count": 1})
    assert p.prompt_bytes == 17
