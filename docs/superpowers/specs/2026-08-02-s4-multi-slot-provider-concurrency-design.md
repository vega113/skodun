# skodun S4 — multi-slot review-fg + per-provider concurrency + 429 backoff

Date: 2026-08-02. Status: **design seed for implementation** (epic S4).  
Parent seed: `docs/epics/s4-multi-slot-provider-concurrency.md`.  
Prerequisites: S3 design `2026-08-02-s3-fair-capacity-design.md` (shipped),
S1 status/cancel, cutover doc `docs/cutover-from-legacy-review.md`.

## Scope

Phases **A + B + C** from the epic seed, in full:

| Phase | Outcome |
|---|---|
| **A** | True multi-slot `review-fg` when legacy dual-hold is off |
| **B** | `provider:<id>` max_in_flight + 429/quota pressure reduction + hop/retry recovery |
| **C** | Queue realism (position, budget, preferred ETA) + diagnostics |

**Not in scope:** multi-provider vote on one diff; host non-review locks; API-key
HTTP providers; MCP fingerprint queue (keep refuse-if-busy unless amended).

## Decisions

| Topic | Decision |
|---|---|
| Goal | Throughput of **independent** reviews |
| Repo concurrency | `review-fg` capacity N; FIFO retained from S3 |
| Physical multi-slot | Only when `SKODUN_LEGACY_FG_LOCK=0` (default **1**/on) |
| Provider concurrency | Class `provider:<provider_id>`; default max_in_flight **1** |
| 429 handling | `unavailable` + `quota` → `provider_state` + reduce effective slots for P |
| Recovery | Next free chain entry, else bounded wait/retry, else untrustworthy fail |
| MCP | Unchanged refuse-if-busy per process |
| gate/trust | Unchanged |

## Resource classes

```text
review-fg          scope = git_common_dir
provider:xai       scope = "xai"   (provider id, not reviewer name)
provider:openai    scope = "openai"
...
```

Implement via existing `capacity_admissions.resource_class` + scope columns.
Provider class acquire/release must use the same reclaim rules (dead pid, stale
holders) as S3.

## Dual-hold (Phase A)

```text
LEGACY_FG_LOCK on  → acquire review-fg, then mkdir lock (S3 dual-hold)
LEGACY_FG_LOCK off → acquire review-fg only; N concurrent run_review allowed
```

Default **on** preserves tubescribes shadow safety.

## Admit + bind (Phase B)

```text
1. capacity.acquire review-fg (FIFO, telemetry)
2. for entry in finder_chain:
     if provider_state active for entry.provider: continue
     if !try_acquire provider:<id>: continue  # or wait if sole option
     run entry
     on success: break
     on quota/unavailable: release provider slot; shrink P; continue
     on degraded/timeout: existing chain policy (stop vs retry entry)
3. release provider slot; release review-fg
```

Do not hold `provider:*` across long idle waits without a bound.

### Pressure reduction

On quota for P:

- `mark_provider_unavailable` with TTL (existing).  
- **Effective max_in_flight(P) = 0** for that TTL (no new acquires). Derived from
  active `provider_state` (and optionally active admissions). Cross-process.

### Recovery

Normative order:

1. Next chain entry free and not in backoff → run it.  
2. Else wait (bounded) for any viable provider slot; re-select.  
3. Else untrustworthy failed — no silent pass.

## Queue UX (Phase C)

- Progress: repo and/or provider class position + remaining budget.  
- ETA: p50 of last K≤20 terminal `wait_ms` for same class+scope; omit if &lt;3 samples.  
- Pre-id wait: progress only. No new MCP tools required.  
- Both classes use `capacity_admissions` rows.

## Schema

Prefer **no** new table if `capacity_admissions` + `provider_state` suffice.
If effective caps need durability beyond env, additive columns or a small
`provider_limits` table with atomic schema bump (v7).

## Module plan

| Module | Change |
|---|---|
| `capacity.py` | Multi-class acquire; provider class helpers; optional ETA helper |
| `store.py` | Reuse admissions; reclaim for all classes; optional limits DDL |
| `pipeline.py` | Dual-hold gate on env; multi-slot path |
| `chain.py` | Acquire/release provider slot around entry; quota pressure hook |
| `config.py` | Optional per-provider max_in_flight |
| `services.py` | Status/providers diagnostics if needed |
| tests | Matrix T1–T9 from epic seed |

## Verification

- Hermetic tests T1–T9 from epic.  
- Manual/optional: two worktrees, dual-hold off, capacity 2, two fakes concurrent.  
- `git diff` empty on `gate.py` / `trust.py`.  
- Docs: concurrency fragment, integrate guide, epic checklist.  
- Merge to main + close issue = Done.

## Risks

Same as epic seed (legacy double-admit, slot leaks, misclassified 429). Defaults
conservative; multi-slot is **opt-in** via env after dual-hold off.
