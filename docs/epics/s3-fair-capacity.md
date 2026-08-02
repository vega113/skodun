# Epic S3 — Fair review capacity (FIFO admission + telemetry)

> **Live issue:** https://github.com/vega113/skodun/issues/42  
> This file is the in-repo design seed and agent handoff.

## Goal

Replace multi-hour “wait for the repo lock with no inference” starvation with
**explicit capacity**, **FIFO admission**, **bounded wait**, and **persisted
queue telemetry** — without weakening fail-closed gate/trust semantics.

## Why

Repository-wide foreground lock is safe but not fair under multi-agent /
multi-worktree load (see operational audit: 2580s wait ceilings, capacity
timeouts without inference). Host-wide multi-MCP fair queue for *all* work is
out of scope; this epic is **review capacity only**.

## Done when

- [x] Admission API (internal): acquire/release review capacity with resource
      class at least `review-fg` (optional `review-bg`, later `provider:<id>`)
- [x] Default capacity: configurable, default **1** FG; document how to raise
- [x] Waiters: **FIFO**; short admission timeout; no blind infinite requeue after
      expiry (durable `expired` / `rejected` outcome)
- [x] Persist telemetry: `queued_at`, `admitted_at`, `started_at`, `ended_at`,
      `wait_ms`, expire reason
- [x] MCP: either keep refuse-if-busy **or** admit with **diff/worktree
      fingerprint** re-check at start (tree moved → cancel, no silent stale run).
      Document the choice; default prefer fingerprint-safe admit or explicit refuse
      — **chosen: refuse-if-busy**
- [x] CLI wait path surfaces queue position / wait budget (not silent spin only)
- [x] Compatibility: dual-lock or documented bridge while tubescribes legacy
      scripts still exist (shadow period)
- [x] Preflight: if entire provider chain is known unavailable, fail fast
      **without** holding capacity for a full timeout budget
- [x] Tests + `examples/fragments/concurrency.md` + integrate guide updated
- [x] `gate.py` / `trust.py` unchanged unless owner-approved

## Non-goals

- Scheduling retention/doctor jobs (already CLI/`schedule install`)
- TubeScribes full-gate tiers / Karma / DB locks
- Parallel multi-provider voting by default

## Children

1. Design: capacity store + FIFO waiters + telemetry schema — shipped
   (`docs/superpowers/specs/2026-08-02-s3-fair-capacity-design.md`)
2. Wire FG `run_review` + MCP refuse-if-busy policy — shipped
3. Preflight unavailable short-circuit — shipped
4. Docs + migration notes for legacy lock name — shipped (store schema v6)

## Method

Design first under `docs/superpowers/specs/` (non-trivial). Implement minimum
verified change; one atomic store version bump if DDL required. PR `refs` this
epic.

## Shipped knobs

| Knob | Default |
|---|---|
| `SKODUN_REVIEW_FG_CAPACITY` | `1` |
| `SKODUN_ADMISSION_WAIT_SECONDS` | same as lock wait |
| `SKODUN_LOCK_WAIT_SECONDS` / `POLL` / `STALE` | unchanged dual-hold |

See `examples/fragments/concurrency.md` and the S3 design spec.
