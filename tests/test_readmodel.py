"""Coverage projection contract: incomplete evidence stays visible and ineligible."""

import json

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


def test_completed_checkpoint_payload_is_projected_as_partial_evidence():
    payload = {
        "parse_ok": True, "degraded": False, "degraded_reason": "",
        "stop_reason": "done", "diff_truncated": False,
        "summary": "batch one", "findings": [], "failure_reason": "",
        "attempts": [], "provenance": {}, "accepted": None,
    }
    p = project_review(
        _record(batches=[], usable_output=False),
        orchestration={"state": "active", "batch_count": 4},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete",
             "payload_json": json.dumps(payload)},
            {"pass_kind": "batch", "pass_index": 2, "state": "pending"},
            {"pass_kind": "batch", "pass_index": 3, "state": "pending"},
            {"pass_kind": "batch", "pass_index": 4, "state": "pending"},
        ])
    assert p.coverage_state == "partial"
    assert p.usable_evidence is True
    assert p.gate_eligible is False
    assert p.completed_passes == 1
    assert p.next_resumable_pass == 2


def test_pending_checkpoint_is_queued_and_extra_pass_shapes_are_decoded():
    p = project_review(
        _record(status="clean", trustworthy=True, usable_output=True,
                extra_passes={
                    "security": {"ran": True, "parse_ok": True,
                                 "degraded": False},
                    "skeptic": {"ran": True, "parse_ok": False,
                                "degraded": False},
                    "refuter": {"ran": True, "status": "ran",
                                "degraded": False},
                }),
        orchestration={"state": "active", "batch_count": 1},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete"},
            {"pass_kind": "integration", "pass_index": 0, "state": "pending"},
        ])
    assert p.passes["integration"] == "queued"
    assert p.passes["security"] == "complete"
    assert p.passes["skeptic"] == "failed"
    assert p.passes["refuter"] == "complete"


def test_failed_integration_checkpoint_payload_is_not_projected_complete():
    payload = {
        "parse_ok": False, "degraded": False, "degraded_reason": "",
        "stop_reason": "parse_failed", "diff_truncated": False,
        "summary": "", "findings": [], "failure_reason": "bad output",
        "attempts": [], "provenance": {}, "accepted": None,
    }
    p = project_review(
        _record(status="clean", trustworthy=True, usable_output=True),
        orchestration={"state": "active", "batch_count": 2},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete"},
            {"pass_kind": "batch", "pass_index": 2, "state": "complete"},
            {"pass_kind": "integration", "pass_index": 0,
             "state": "complete", "payload_json": json.dumps(payload)},
        ])
    assert p.passes["integration"] == "failed"
    assert p.cross_provider_complete is False
    assert p.coverage_state == "partial"


def test_consumed_failed_checkpoint_does_not_override_failed_projection():
    payload = {
        "parse_ok": False, "degraded": False, "degraded_reason": "",
        "stop_reason": "parse_failed", "diff_truncated": False,
        "summary": "", "findings": [], "failure_reason": "bad output",
        "attempts": [], "provenance": {}, "accepted": None,
    }
    p = project_review(
        _record(status="clean", trustworthy=True, usable_output=False),
        orchestration={"state": "consumed", "batch_count": 1},
        checkpoints=[{"pass_kind": "batch", "pass_index": 1,
                      "state": "complete", "payload_json": json.dumps(payload)}])
    assert p.coverage_state == "none"
    assert p.gate_eligible is False


def test_integration_evidence_does_not_complete_finder():
    payload = {
        "parse_ok": True, "degraded": False, "degraded_reason": "",
        "stop_reason": "done", "diff_truncated": False,
        "summary": "integration", "findings": [], "failure_reason": "",
        "attempts": [], "provenance": {}, "accepted": None,
    }
    p = project_review(
        _record(status="failed", trustworthy=False, usable_output=False),
        orchestration={"state": "active", "batch_count": 2},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "failed"},
            {"pass_kind": "batch", "pass_index": 2, "state": "failed"},
            {"pass_kind": "integration", "pass_index": 0,
             "state": "complete", "payload_json": json.dumps(payload)},
        ])
    assert p.usable_evidence is True
    assert p.passes["finder"] == "failed"


def test_failed_extra_pass_flag_and_missing_parse_boolean_stay_failed():
    p = project_review(_record(
        status="clean", trustworthy=True, usable_output=True,
        extra_passes={
            "security": {"ran": False, "failed": True},
            "skeptic": {"status": "ran"},
        }))
    assert p.passes["security"] == "failed"
    assert p.passes["skeptic"] == "failed"
    assert p.gate_eligible is False
    assert p.gate_reason == "required_pass_failed"


def test_failed_required_pass_takes_priority_over_untrustworthy_reason():
    p = project_review(_record(
        status="clean", trustworthy=False, usable_output=True,
        extra_passes={"security": {"ran": False, "failed": True}}))
    assert p.gate_reason == "required_pass_failed"


def test_mixed_finder_checkpoint_payloads_do_not_complete_finder():
    good = {"parse_ok": True, "degraded": False, "degraded_reason": "",
            "stop_reason": "done", "diff_truncated": False,
            "summary": "", "findings": [], "failure_reason": "",
            "attempts": [], "provenance": {}, "accepted": None}
    bad = {**good, "parse_ok": False, "stop_reason": "parse_failed"}
    p = project_review(
        _record(status="clean", trustworthy=True, usable_output=False),
        orchestration={"state": "active", "batch_count": 2},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "complete",
             "payload_json": json.dumps(good)},
            {"pass_kind": "batch", "pass_index": 2, "state": "complete",
             "payload_json": json.dumps(bad)},
        ])
    assert p.passes["finder"] == "failed"


def test_completed_integration_checkpoint_is_usable_evidence():
    payload = {
        "parse_ok": True, "degraded": False, "degraded_reason": "",
        "stop_reason": "done", "diff_truncated": False,
        "summary": "integration", "findings": [], "failure_reason": "",
        "attempts": [], "provenance": {}, "accepted": None,
    }
    p = project_review(
        _record(batches=[], usable_output=False),
        orchestration={"state": "active", "batch_count": 2},
        checkpoints=[
            {"pass_kind": "batch", "pass_index": 1, "state": "failed"},
            {"pass_kind": "batch", "pass_index": 2, "state": "failed"},
            {"pass_kind": "integration", "pass_index": 0,
             "state": "complete", "payload_json": json.dumps(payload)},
        ])
    assert p.usable_evidence is True
    assert p.coverage_state == "partial"


def test_unbatched_clean_round_counts_its_finder_and_rejects_string_booleans():
    complete = project_review(_record(status="clean", trustworthy=True,
                                       usable_output=True), checkpoints=[])
    assert complete.planned_passes == 1
    assert complete.completed_passes == 1
    assert complete.coverage_state == "complete"

    forged = project_review(_record(status="clean", trustworthy=True,
                                     usable_output="false"), checkpoints=[])
    assert forged.usable_evidence is False
    assert forged.coverage_state == "none"
    assert forged.gate_eligible is False


def test_projection_prompt_bytes_includes_integration_telemetry():
    p = project_review(_record(
        status="clean", trustworthy=True, usable_output=True,
        batches=[{"parse_ok": True,
                   "telemetry": {"bytes": {"prompt": 10}}}],
        integration={"status": "ran", "telemetry":
                     {"bytes": {"prompt": 20}}},
    ))
    assert p.prompt_bytes == 30
