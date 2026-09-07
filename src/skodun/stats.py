"""CLI-only rendering for the store's operational telemetry read model."""

from __future__ import annotations

import json
import time
from typing import Mapping


def since_iso(days: object, *, now: float | None = None) -> str:
    """Return a canonical UTC lower bound for a non-negative day window."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError(f"since-days must be a non-negative int, got {days!r}")
    epoch = time.time() if now is None else now
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        raise ValueError(f"now must be numeric, got {epoch!r}")
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch - days * 86400))


def render(data: Mapping, *, fmt: str = "text") -> str:
    """Render one stats object; both formats are projections of ``data``."""
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    if fmt != "text":
        raise ValueError(f"stats format must be text or json, got {fmt!r}")
    reviews = data["reviews"]
    timing = data["timing"]
    capacity = data["capacity"]
    lines = [
        f"since={data['since']}",
        (f"reviews={reviews['total']} trustworthy={reviews['trustworthy']} "
         f"trustworthy_rate={reviews['trustworthy_rate']} "
         f"findings={reviews['findings']} telemetry_rows={reviews['telemetry_rows']} "
         f"legacy_rows={reviews['legacy_rows']}"),
        (f"review_ms=count:{timing['review_ms']['count']} "
         f"p50:{timing['review_ms']['p50_ms']} "
         f"p90:{timing['review_ms']['p90_ms']}"),
        (f"queue_wait_ms=count:{timing['capacity_queue_ms']['count']} "
         f"p50:{timing['capacity_queue_ms']['p50_ms']} "
         f"p90:{timing['capacity_queue_ms']['p90_ms']}"),
        (f"run_ms=count:{timing['capacity_run_ms']['count']} "
         f"p50:{timing['capacity_run_ms']['p50_ms']} "
         f"p90:{timing['capacity_run_ms']['p90_ms']}"),
        (f"total_admission_ms=count:{timing['capacity_total_admission_ms']['count']} "
         f"hours:{timing['capacity_total_admission_ms']['total_ms'] / 3600000:.3f}"),
        (f"capacity_rows={capacity['rows']} "
         f"telemetry_rows={capacity['telemetry_rows']} "
         f"expired={capacity['expired']} rejected={capacity['rejected']}"),
        (f"first_trust={data['identities']['first_trust']} "
         f"recovered={data['identities']['recovered']} "
         f"never_trustworthy={data['identities']['never_trustworthy']} "
         f"reuse_hits={data['reuse']['hits']} reuse_misses={data['reuse']['misses']}"),
    ]
    live = data.get("live_capacity")
    if isinstance(live, Mapping):
        repo_bit = ",".join(
            f"{h.get('scope')}={h.get('n')}"
            for h in (live.get("by_repo") or []) if isinstance(h, Mapping)
        ) or "none"
        prov_bit = ",".join(
            f"{h.get('resource_class')}@{h.get('scope')}={h.get('n')}"
            for h in (live.get("by_provider") or []) if isinstance(h, Mapping)
        ) or "none"
        machine_cap = live.get('machine_cap')
        lines.append(
            f"machine_cap={machine_cap if machine_cap is not None else 'unknown'} "
            f"machine_holders={live.get('machine_holders')} "
            f"by_repo={repo_bit} by_provider={prov_bit}")
        if live.get('config_error'):
            lines.append(f"capacity_config_error={live['config_error']}")
    if 'audit_denominators' in data:
        lines.append('audit_denominators=' + json.dumps(data['audit_denominators'], sort_keys=True))
        lines.append('call_observations=' + json.dumps(data['call_observations'], sort_keys=True))
        lines.append('timing_observations=' + json.dumps(timing, sort_keys=True))
    return "\n".join(lines)
