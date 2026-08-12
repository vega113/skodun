"""Coverage projection contract: incomplete evidence stays visible and ineligible."""

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
