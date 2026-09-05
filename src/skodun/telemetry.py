"""Bounded, non-sensitive projections for S8.3 review telemetry.

This module is deliberately independent of the store and provider runners.
Telemetry is an artifact read model: it may report facts already present on an
attempt, but it must never capture prompts, transcripts, environment values,
or an invented token count.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .store import _is_canonical_ts


EXECUTION_PROVENANCE_KEYS = frozenset({
    "adapter", "resolved", "version", "override_source",
})


def _provenance(raw: object) -> dict:
    if not isinstance(raw, Mapping):
        return {}
    return {key: raw.get(key) for key in EXECUTION_PROVENANCE_KEYS
            if key in raw}


def _reported(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _token_usage(raw: object) -> dict[str, int | None]:
    raw = raw if isinstance(raw, Mapping) else {}
    # Adapter usage is optional. A missing key, and the historical zero used
    # by adapters that did not receive a usage block, are both unknown here.
    def value(*names: str) -> int | None:
        for name in names:
            if name in raw:
                parsed = _reported(raw.get(name))
                if parsed is not None and parsed > 0:
                    return parsed
        return None

    return {
        "input": value("input_tokens", "prompt_tokens"),
        "output": value("output_tokens", "completion_tokens"),
        "cache": value("cache_tokens", "cached_tokens"),
        "reasoning": value("reasoning_tokens"),
        "total": value("total_tokens"),
    }


def _capacity_timing(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    timestamps = ("queued_at", "admitted_at", "started_at", "ended_at")
    waits = ("wait_ms", "queue_wait_ms")
    for key in timestamps:
        if key in raw and raw[key] is not None and not _is_canonical_ts(raw[key]):
            return None
    for key in waits:
        value = raw.get(key)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int) or value < 0):
            return None
    return {key: raw.get(key) for key in (*timestamps, *waits)}


def attempt_launched(attempt: Mapping) -> bool | None:
    """A skipped candidate is not a provider call; absent evidence is unknown."""
    if attempt.get('skipped') or attempt.get('launched') is False:
        return False
    if (attempt.get('launched') is True or type(attempt.get('rc')) is int
            or type(attempt.get('timed_out')) is bool):
        return True
    return None


def attempt_telemetry(attempt: Mapping, *, timeout_sec: int | None) -> dict:
    """Project one attempt into a stable, allowlisted telemetry row."""
    classification = attempt.get("classification")
    classification = classification if isinstance(classification, Mapping) else {}
    attempt_id = attempt.get('attempt_id')
    row = {
        "attempt_id": attempt_id if isinstance(attempt_id, str) and len(attempt_id) <= 256 else None,
        "input_bytes": _reported(attempt.get('input_bytes')),
        "input_scope": "provider_input",
        "launched": attempt_launched(attempt),
        "attempt_ordinal": _reported(attempt.get("n")),
        "provider": attempt.get("provider"),
        "model": attempt.get("model"),
        "effort": attempt.get("effort"),
        "duration_sec": attempt.get("duration_sec"),
        "first_output_sec": attempt.get("first_output_sec"),
        "timeout_sec": timeout_sec,
        "timed_out": attempt.get("timed_out"),
        "retry_category": classification.get("category") or None,
        "stop_kind": classification.get("kind") or None,
        "failure_detail": classification.get("detail") or None,
        "token_usage": _token_usage(attempt.get("usage")),
        "resume_decision": attempt.get("resume_decision"),
        "execution_provenance": _provenance(attempt.get("execution_provenance")),
    }
    capacity_timing = _capacity_timing(attempt.get("capacity_timing"))
    if capacity_timing is not None:
        row["capacity_timing"] = capacity_timing
    return row


def batch_telemetry(*, planner_version: str, batch_budget: int,
                    boundary_digest: str, batch_index: int, batch_count: int,
                    diff_bytes: int, context_bytes: int, checklist_bytes: int,
                    prompt_bytes: int, attempts: Iterable[Mapping],
                    timeout_sec: int | None, **extra: object) -> dict:
    """Build one batch/integration telemetry object from bounded dimensions."""
    out = {
        "planner_version": planner_version,
        "batch_budget": batch_budget,
        "boundary_digest": boundary_digest,
        "batch_index": batch_index,
        "batch_count": batch_count,
        "bytes": {
            "diff": diff_bytes,
            "context": context_bytes,
            "checklist": checklist_bytes,
            "prompt": prompt_bytes,
        },
        "timing": {
            "queued_at": extra.get("queued_at"),
            "admitted_at": extra.get("admitted_at"),
            "started_at": extra.get("started_at"),
            "completed_at": extra.get("completed_at"),
            "queue_duration_sec": extra.get("queue_duration_sec"),
            "run_duration_sec": extra.get("run_duration_sec"),
            "wall_duration_sec": extra.get("wall_duration_sec"),
        },
        "attempts": [attempt_telemetry(a, timeout_sec=timeout_sec)
                     for a in attempts if isinstance(a, Mapping)],
    }
    # An opaque extension point for S7.1 receipt digests may be supplied by a
    # future landed contract. This module never creates or interprets it.
    if "receipt_digests" in extra:
        out["receipt_digests"] = extra["receipt_digests"]
    return out
