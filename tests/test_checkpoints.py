"""Exact-identity orchestration checkpoints are bounded non-review state."""

from dataclasses import replace

import pytest

from skodun import checkpoints


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
        "attempts": [{"provider": "xai", "model": "grok", "ordinal": 0}],
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
