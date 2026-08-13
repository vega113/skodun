from __future__ import annotations

from dataclasses import replace

from skodun import batching
from skodun.config import Defaults
from skodun.pipeline import batch_plan
from skodun.services import _validate_batch_target
from skodun.telemetry import attempt_telemetry, batch_telemetry


def _diff(files: int = 4) -> bytes:
    return b"".join(
        f"diff --git a/f{i}.py b/f{i}.py\n@@ -1 +1 @@\n-old\n+new-{i}\n".encode()
        for i in range(files)
    )


def test_smaller_target_is_deterministic_and_never_widens_provider_ceiling():
    diff = _diff(files=8)
    defaults = Defaults(context_pack=False, max_diff_bytes=100)
    ceiling = batch_plan(diff, defaults)
    smaller = replace(defaults, batch_target_bytes=40)
    above_ceiling = replace(defaults, batch_target_bytes=1_000)
    first = batch_plan(diff, smaller)
    second = batch_plan(diff, smaller)
    widened = batch_plan(diff, above_ceiling)

    assert first is not None
    assert [(b.data, b.files, b.truncated) for b in first] == [
        (b.data, b.files, b.truncated) for b in second]
    assert all(len(b.data) <= 40 or b.truncated for b in first)
    assert b"".join(b.data for b in first) == diff
    assert widened is not None
    assert all(len(b.data) <= 100 or b.truncated for b in widened)
    assert b"".join(b.data for b in widened) == diff
    assert len(widened) == len(ceiling or [])


def test_telemetry_keeps_missing_usage_unknown_and_excludes_sensitive_fields():
    attempt = {
        "n": 1,
        "provider": "openai",
        "model": "small",
        "effort": "low",
        "duration_sec": 1.25,
        "timed_out": False,
        "classification": {"kind": "ok", "category": "", "detail": ""},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "stdout": "do not persist",
        "environment": {"SECRET": "do not persist"},
        "execution_provenance": {
            "adapter": "codex", "resolved": "/opt/bin/codex",
            "version": None, "override_source": "env:SKODUN_CODEX_BIN",
            "environment": {"SECRET": "do not persist"},
            "nested": {"prompt": "do not persist"},
        },
    }

    row = attempt_telemetry(attempt, timeout_sec=30)

    assert row["attempt_ordinal"] == 1
    assert row["duration_sec"] == 1.25
    assert row["timeout_sec"] == 30
    assert row["token_usage"] == {
        "input": None, "output": None, "cache": None, "reasoning": None,
        "total": None,
    }
    assert "stdout" not in row and "environment" not in row
    assert row["execution_provenance"]["resolved"] == "/opt/bin/codex"
    assert "environment" not in row["execution_provenance"]
    assert "nested" not in row["execution_provenance"]


def test_attempt_telemetry_preserves_bounded_admission_timing():
    row = attempt_telemetry({
        "n": 1,
        "provider": "grok",
        "model": "mini",
        "effort": "medium",
        "classification": {"kind": "ok"},
        "capacity_timing": {
            "queued_at": "2026-08-13T10:00:00Z",
            "admitted_at": "2026-08-13T10:00:02Z",
            "started_at": "2026-08-13T10:00:03Z",
            "ended_at": "2026-08-13T10:00:04Z",
            "wait_ms": 4000,
            "queue_wait_ms": 2000,
            "secret": "do not persist",
        },
    }, timeout_sec=30)

    assert row["capacity_timing"] == {
        "queued_at": "2026-08-13T10:00:00Z",
        "admitted_at": "2026-08-13T10:00:02Z",
        "started_at": "2026-08-13T10:00:03Z",
        "ended_at": "2026-08-13T10:00:04Z",
        "wait_ms": 4000,
        "queue_wait_ms": 2000,
    }


def test_batch_telemetry_has_identity_and_byte_dimensions():
    result = batch_telemetry(
        planner_version="skodun-batch-v1",
        batch_budget=100,
        boundary_digest="b" * 64,
        batch_index=1,
        batch_count=2,
        diff_bytes=80,
        context_bytes=10,
        checklist_bytes=20,
        prompt_bytes=110,
        attempts=[],
        timeout_sec=30,
    )

    assert result["planner_version"] == "skodun-batch-v1"
    assert result["boundary_digest"] == "b" * 64
    assert result["bytes"] == {
        "diff": 80, "context": 10, "checklist": 20, "prompt": 110,
    }
    assert result["attempts"] == []


def test_shared_surface_validation_rejects_bool_negative_and_unbounded_targets():
    assert _validate_batch_target(None) == (None, None)
    assert _validate_batch_target(0) == (None, None)
    assert _validate_batch_target(True)[1]
    assert _validate_batch_target(-1)[1]
    assert _validate_batch_target(10_000_001)[1]
