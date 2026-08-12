from skodun import fingerprint
from skodun.store import Store


def test_fingerprint_is_versioned_and_normalizes_path_claim_whitespace():
    left = {"file": "./src\\a.py", "title": "  SQL   injection  "}
    right = {"file": "src/a.py", "title": "SQL injection"}

    assert fingerprint.finding_fingerprint(left) == fingerprint.finding_fingerprint(right)
    assert fingerprint.finding_fingerprint(left).startswith("sha256:")
    assert fingerprint.fingerprint_payload(left)["version"] == "finding_fingerprint_v2"


def test_fingerprint_changes_scope_claim_and_mutation():
    base = {"file": "src/a.py", "title": "bad", "symbol": "run"}
    assert fingerprint.finding_fingerprint(base) != fingerprint.finding_fingerprint(
        {**base, "scope_attribution": {"scope": "inherited_dependency"}})
    assert fingerprint.finding_fingerprint(base) != fingerprint.finding_fingerprint(
        {**base, "claim": "different"})
    assert fingerprint.finding_fingerprint(base) != fingerprint.finding_fingerprint(
        {**base, "mutation_type": "replace"})


def test_semantic_detail_keeps_a_retitled_finding_linked():
    old = {"file": "src/a.py", "title": "old wording", "detail": "same claim"}
    new = {"file": "src/a.py", "title": "new wording", "detail": "same claim"}
    assert fingerprint.finding_fingerprint(old) == fingerprint.finding_fingerprint(new)


def test_lineage_repeats_and_reports_ambiguous_matches_without_triage():
    finding = {"file": "src/a.py", "title": "same"}
    repeated = fingerprint.annotate_findings([finding], [finding])[0]
    assert repeated["finding_lineage_v2"] == {
        "version": "finding_fingerprint_v2",
        "match_reason": "repeated",
        "predecessor_index": 0,
        "predecessor_review_id": None,
        "path": "src/a.py",
        "line": "unknown",
    }
    ambiguous = fingerprint.annotate_findings([finding], [finding, finding])[0]
    assert ambiguous["finding_lineage_v2"]["match_reason"] == "ambiguous"
    assert "finding_key" not in repeated


def test_lineage_reports_moved_and_scope_changed_without_suppressing_claims():
    old = {"file": "src/old.py", "title": "same"}
    moved = fingerprint.annotate_findings([{"file": "src/new.py", "title": "same"}], [old])[0]
    assert moved["finding_lineage_v2"]["match_reason"] == "moved"
    scoped = fingerprint.annotate_findings(
        [{"file": "src/old.py", "title": "same",
          "scope_attribution": {"scope": "current_slice"}}],
        [{"file": "src/old.py", "title": "same",
          "scope_attribution": {"scope": "inherited_dependency"}}],
    )[0]
    assert scoped["finding_lineage_v2"]["match_reason"] == "scope_changed"


def test_lineage_moved_line_uses_location_outside_digest_and_claim_changes_are_new():
    old = {"file": "src/a.py", "line": 10, "title": "same",
           "category": "rule-1", "symbol": "run"}
    moved = {**old, "line": 42}
    result = fingerprint.annotate_findings([moved], [old])[0]
    assert result["finding_lineage_v2"]["match_reason"] == "moved"
    changed = fingerprint.annotate_findings(
        [{**old, "title": "different"}], [old])[0]
    assert changed["finding_lineage_v2"]["match_reason"] == "new"


def test_same_claim_in_unrelated_structure_is_not_linked():
    old = {"file": "src/a.py", "title": "same", "category": "rule-1"}
    unrelated = {"file": "src/b.py", "title": "same", "category": "rule-2"}
    result = fingerprint.annotate_findings([unrelated], [old])[0]
    assert result["finding_lineage_v2"]["match_reason"] == "new"


def test_terminal_store_write_persists_lineage_without_changing_legacy_fields(tmp_path):
    finding = fingerprint.annotate_findings(
        [{"file": "src/a.py", "title": "same"}])[0]
    record = {
        "id": "r1", "reviewed_at": "2026-08-12T10:00:00Z", "branch": "main",
        "head": "h", "base_ref": "origin/main", "base_sha": "b",
        "diff_hash": "d", "context_hash": "", "mode": "now", "model": "m",
        "adapter": "a", "status": "clean", "parse_ok": True, "degraded": False,
        "diff_truncated": False, "trustworthy": True, "stop_reason": "done",
        "findings_total": 1, "severity": {"high": 0, "medium": 0, "low": 0},
        "findings": [finding], "summary": "ok", "repo_id": "repo",
    }
    store = Store.open(tmp_path / "s.db")
    store.save_review(record)
    rows = store.list_lineage("r1")
    assert rows[0]["fingerprint"] == finding["finding_fingerprint_v2"]
    assert store.get_review("r1")["findings"][0]["finding_fingerprint_v2"]
