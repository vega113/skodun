"""Exact-identity orchestration checkpoints are bounded non-review state."""

from dataclasses import replace

import pytest

from skodun import checkpoints
from skodun.store import Store


def _identity(**changes):
    base = checkpoints.OrchestrationIdentity(
        repo_id="repo-1",
        worktree_root="/work/repo",
        branch="feature",
        head="h" * 40,
        base_ref="origin/main",
        base_sha="b" * 40,
        diff_hash="d" * 40,
        tree_fingerprint="t" * 64,
        context_hash="c" * 64,
        checklist_hash="k" * 64,
        reviewer_hash="r" * 64,
        config_hash="g" * 64,
        policy_hash="p" * 64,
        planner_version=checkpoints.PLANNER_VERSION,
        batch_budget=1000,
        batch_count=4,
        boundary_digest="o" * 64,
        integration_plan_digest="i" * 64,
        pass_identities=(
            checkpoints.PassIdentity(
                kind="batch", index=1, prompt_hash="1" * 64,
                diff_hash="a" * 40, boundary_hash="q" * 64),
            checkpoints.PassIdentity(
                kind="integration", index=0, prompt_hash=None,
                diff_hash="d" * 40, boundary_hash="i" * 64),
        ),
    )
    return replace(base, **changes)


def _payload(**changes):
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
        "attempts": [{
            "n": 1, "provider": "xai", "model": "grok", "effort": None,
            "rc": 0, "timed_out": False, "duration_sec": 1.0,
            "first_output_sec": 0.2,
            "classification": {"kind": "ok", "category": "", "detail": ""},
        }],
        "provenance": {
            "provider": "xai", "model": "grok", "effort": None,
            "note": "",
        },
        "accepted": {"adapter_name": "grok", "model": "grok"},
    }
    data.update(changes)
    return data


def test_identical_identity_has_stable_canonical_json_and_digest():
    left = _identity()
    right = _identity()
    assert left.canonical_json() == right.canonical_json()
    assert left.digest() == right.digest()
    assert checkpoints.first_mismatch(left, right) is None


@pytest.mark.parametrize(("field", "value"), [
    ("repo_id", "repo-2"),
    ("worktree_root", "/work/other"),
    ("branch", "other"),
    ("head", "x" * 40),
    ("base_ref", "origin/release"),
    ("base_sha", "x" * 40),
    ("diff_hash", "x" * 40),
    ("tree_fingerprint", "x" * 64),
    ("context_hash", "x" * 64),
    ("checklist_hash", "x" * 64),
    ("reviewer_hash", "x" * 64),
    ("config_hash", "x" * 64),
    ("policy_hash", "x" * 64),
    ("planner_version", "future-planner"),
    ("batch_budget", 999),
    ("batch_count", 5),
    ("boundary_digest", "x" * 64),
    ("integration_plan_digest", "x" * 64),
])
def test_first_mismatch_names_each_identity_class(field, value):
    assert checkpoints.first_mismatch(_identity(), _identity(**{field: value})) \
        == field


def test_a_changed_pass_prompt_is_a_named_identity_mismatch():
    changed = replace(
        _identity().pass_identities[0], prompt_hash="x" * 64)
    right = _identity(pass_identities=(
        changed, _identity().pass_identities[1]))
    assert checkpoints.first_mismatch(_identity(), right) == "pass_identities"


def test_checkpoint_payload_round_trips_without_aliasing():
    source = _payload()
    payload = checkpoints.CheckpointPayload.from_mapping(source)
    restored = payload.as_dict()
    assert restored == source
    restored["findings"][0]["title"] = "mutated"
    assert payload.as_dict()["findings"][0]["title"] == "Bad edge"


@pytest.mark.parametrize("changes", [
    {"parse_ok": "true"},
    {"degraded": 0},
    {"diff_truncated": None},
    {"findings": "none"},
    {"findings": ["not a finding"]},
    {"attempts": {}},
    {"provenance": []},
    {"accepted": "grok"},
])
def test_checkpoint_payload_refuses_malformed_shapes(changes):
    with pytest.raises(ValueError):
        checkpoints.CheckpointPayload.from_mapping(_payload(**changes))


def test_checkpoint_payload_refuses_unknown_or_sensitive_fields():
    with pytest.raises(ValueError, match="unknown fields"):
        checkpoints.CheckpointPayload.from_mapping(
            {**_payload(), "prompt_text": "secret prompt"})
    with pytest.raises(ValueError, match="forbidden field"):
        checkpoints.CheckpointPayload.from_mapping(_payload(
            provenance={"provider": "xai", "transcript": "raw output"}))


def test_checkpoint_payload_reuses_strict_finding_and_attempt_shapes():
    with pytest.raises(ValueError, match="findings"):
        checkpoints.CheckpointPayload.from_mapping(_payload(
            findings=[{"severity": "medium"}]))
    with pytest.raises(ValueError, match="attempt"):
        checkpoints.CheckpointPayload.from_mapping(_payload(
            attempts=[{"n": "one"}]))


def test_checkpoint_payload_refuses_unbounded_content():
    with pytest.raises(ValueError, match="summary"):
        checkpoints.CheckpointPayload.from_mapping(
            _payload(summary="x" * (checkpoints.MAX_TEXT_CHARS + 1)))
    with pytest.raises(ValueError, match="findings"):
        checkpoints.CheckpointPayload.from_mapping(_payload(
            findings=_payload()["findings"]
            * (checkpoints.MAX_FINDINGS + 1)))
    with pytest.raises(ValueError, match="attempts"):
        checkpoints.CheckpointPayload.from_mapping(_payload(
            attempts=_payload()["attempts"]
            * (checkpoints.MAX_ATTEMPTS + 1)))


def test_bool_is_not_accepted_as_an_integer_identity():
    with pytest.raises(ValueError, match="batch_budget"):
        replace(_identity(), batch_budget=True)


NOW = "2026-08-12T10:00:00Z"
LATER = "2026-08-12T10:10:00Z"
EXPIRED = "2026-08-12T09:59:59Z"
EARLIER = "2026-08-12T09:00:00Z"


def _created(store, identity=None, *, orchestration_id="orch-1",
             created_at=NOW, expires_at=LATER):
    return store.create_orchestration(
        orchestration_id, identity or _identity(), requested_mode="now",
        created_at=created_at, expires_at=expires_at)


def test_store_creates_an_orchestration_and_ordered_planned_passes(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        row = _created(store)
        planned = store.list_checkpoints("orch-1")
    assert row["state"] == "active"
    assert row["identity_digest"] == _identity().digest()
    assert [(item["pass_kind"], item["pass_index"], item["state"])
            for item in planned] == [
                ("batch", 1, "pending"),
        ("integration", 0, "pending"),
    ]


def test_exact_orchestration_creation_is_idempotent_under_a_racing_starter(
        tmp_path):
    with Store.open(tmp_path / "s.db") as first, Store.open(tmp_path / "s.db") as second:
        created = _created(first, orchestration_id="orch-first")
        same = second.create_orchestration(
            "orch-second", _identity(), requested_mode="now",
            created_at=NOW, expires_at=LATER)
    assert same["id"] == created["id"] == "orch-first"
    assert same["identity_digest"] == _identity().digest()


def test_fresh_orchestration_creation_does_not_reuse_exact_prior_state(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store, orchestration_id="orch-first")
        fresh = store.create_orchestration(
            "orch-fresh", _identity(), requested_mode="now",
            created_at=NOW, expires_at=LATER, reuse_existing=False)
    assert fresh["id"] == "orch-fresh"


def test_resume_candidate_is_scoped_and_mismatch_is_durable(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store)
        assert store.find_resume_candidate(
            "repo-1", "/work/repo", "feature")["id"] == "orch-1"
        assert store.find_resume_candidate(
            "repo-1", "/work/other", "feature") is None
        assert store.record_orchestration_mismatch(
            "orch-1", "planner_version", at=NOW) is True
        candidate = store.get_orchestration("orch-1")
    assert candidate["first_mismatch"] == "planner_version"


def test_one_live_checkpoint_claim_wins_and_the_other_is_in_flight(tmp_path):
    db = tmp_path / "s.db"
    with Store.open(db) as first, Store.open(db) as second:
        _created(first)
        won = first.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-a",
            now=NOW, lease_expires_at=LATER)
        lost = second.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-b",
            now=NOW, lease_expires_at=LATER)
    assert won["decision"] == "claimed"
    assert won["fence"] == 1 and won["claim_token"]
    assert lost["decision"] == "in_flight"
    assert lost["claim_owner"] == "worker-a"


def test_expired_claim_is_reclaimed_and_late_owner_is_fenced(tmp_path):
    db = tmp_path / "s.db"
    payload = checkpoints.CheckpointPayload.from_mapping(_payload())
    with Store.open(db) as first, Store.open(db) as second:
        _created(first)
        stale = first.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-a",
            now=EARLIER, lease_expires_at=EXPIRED)
        current = second.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-b",
            now=NOW, lease_expires_at=LATER)
        assert first.complete_checkpoint(
            "orch-1", "batch", 1, owner="worker-a",
            claim_token=stale["claim_token"], fence=stale["fence"],
            payload=payload, completed_at=NOW) is False
        assert second.complete_checkpoint(
            "orch-1", "batch", 1, owner="worker-b",
            claim_token=current["claim_token"], fence=current["fence"],
            payload=payload, completed_at=NOW) is True
        completed = first.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-c",
            now=NOW, lease_expires_at=LATER)
    assert current["decision"] == "claimed" and current["fence"] == 2
    assert completed["decision"] == "complete"
    assert checkpoints.CheckpointPayload(
        completed["payload_json"]).as_dict() == _payload()


def test_release_only_applies_to_the_current_claim(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store)
        claim = store.claim_checkpoint(
            "orch-1", _identity().pass_identities[0], owner="worker-a",
            now=NOW, lease_expires_at=LATER)
        assert store.release_checkpoint(
            "orch-1", "batch", 1, owner="other",
            claim_token=claim["claim_token"], fence=claim["fence"],
            reason="cancelled", at=NOW) is False
        assert store.release_checkpoint(
            "orch-1", "batch", 1, owner="worker-a",
            claim_token=claim["claim_token"], fence=claim["fence"],
            reason="cancelled", at=NOW) is True
        row = store.list_checkpoints("orch-1")[0]
    assert row["state"] == "pending" and row["failure_reason"] == "cancelled"


def test_incomplete_orchestration_expiry_never_creates_review_coverage(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store, created_at="2026-08-12T09:00:00Z",
                 expires_at=EXPIRED)
        assert store.expire_orchestrations(now=NOW) == 1
        assert store.get_orchestration("orch-1")["state"] == "expired"
        assert store.list_reviews(None, 10) == []
        assert store.reuse_candidates(
            "repo-1", "b" * 40, "d" * 40) == []


def test_orchestration_expiry_discards_terminal_checkpoint_payloads(tmp_path):
    payload = checkpoints.CheckpointPayload.from_mapping(_payload())
    with Store.open(tmp_path / "s.db") as store:
        _created(store, created_at="2026-08-12T09:00:00Z",
                 expires_at=EXPIRED)
        identity = _identity().pass_identities[0]
        claim = store.claim_checkpoint(
            "orch-1", identity, owner="worker", now=EARLIER,
            lease_expires_at=LATER)
        assert store.complete_checkpoint(
            "orch-1", "batch", 1, owner="worker",
            claim_token=claim["claim_token"], fence=claim["fence"],
            payload=payload, completed_at=EARLIER)
        assert store.expire_orchestrations(now=NOW) == 1
        row = store.list_checkpoints("orch-1")[0]
    assert row["state"] == "complete"
    assert row["payload_json"] is None


def _review(review_id="sk-final"):
    return {
        "id": review_id, "reviewed_at": NOW, "branch": "feature",
        "head": "h" * 40, "base_ref": "origin/main",
        "base_sha": "b" * 40, "diff_hash": "d" * 40,
        "context_hash": "c" * 64, "mode": "now", "model": "grok",
        "adapter": "grok", "status": "clean", "parse_ok": True,
        "degraded": False, "diff_truncated": False, "stop_reason": "EndTurn",
        "findings_total": 0, "severity": {
            "high": 0, "medium": 0, "low": 0},
        "summary": "clean", "findings": [],
        "batch_orchestration_id": "orch-1",
        "batch_identity_digest": _identity().digest(),
    }


def _complete_all(store):
    payload = checkpoints.CheckpointPayload.from_mapping(_payload())
    for identity in _identity().pass_identities:
        if identity.prompt_hash is None:
            identity = replace(identity, prompt_hash="z" * 64)
        claim = store.claim_checkpoint(
            "orch-1", identity, owner="worker", now=NOW,
            lease_expires_at=LATER)
        assert store.complete_checkpoint(
            "orch-1", identity.kind, identity.index, owner="worker",
            claim_token=claim["claim_token"], fence=claim["fence"],
            payload=payload, completed_at=NOW)


def test_incomplete_checkpoint_finalization_rolls_back_the_review_write(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store)
        with pytest.raises(ValueError, match="incomplete checkpoints"):
            store.save_checkpointed_review(_review())
        assert store.get_review("sk-final") is None
        assert store.get_orchestration("orch-1")["state"] == "active"


def test_cancelled_incomplete_prepush_finalization_is_untrustworthy(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store)
        # A pre-push reservation row is the existing running review. This
        # direct fixture uses the same identity fields and checks that the
        # checkpoint guard does not turn cancellation into a persistence error.
        reservation = store.reserve_prepush(
            "feature", "h" * 40, "origin/main", "b" * 40, "d" * 40,
            100, {}, repo="repo-1", now=NOW)
        rec = store.get_review(reservation.record_id)
        rec.update(status="failed", degraded=True,
                   degraded_reason="cancelled: before it was recorded",
                   failure_reason="cancelled: before it was recorded")
        rec["batch_orchestration_id"] = "orch-1"
        rec["batch_identity_digest"] = _identity().digest()
        assert store.finalize_review(reservation.record_id, rec) is True
        saved = store.get_review(reservation.record_id)
    assert saved["trustworthy"] is False


def test_final_review_and_checkpoint_consumption_commit_together(tmp_path):
    with Store.open(tmp_path / "s.db") as store:
        _created(store)
        _complete_all(store)
        store.save_checkpointed_review(_review())
        review = store.get_review("sk-final")
        orchestration = store.get_orchestration("orch-1")
    assert review["trustworthy"] is True
    assert orchestration["state"] == "consumed"
    assert orchestration["final_review_id"] == "sk-final"
