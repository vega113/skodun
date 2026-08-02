# Epic S4 — Multi-slot review-fg + per-provider concurrency + 429 backoff

> **Live issue:** https://github.com/vega113/skodun/issues/56  
> This file is the in-repo design seed and **AI agent implementation handoff**.

**Depends on:** S3 fair capacity (shipped, #42 / PR #50), S1 status/cancel
(shipped, #41 / PR #49).  
**Related:** [`../cutover-from-legacy-review.md`](../cutover-from-legacy-review.md)
(client dual-script cutover improves real-world multi-slot; not a hard code
blocker for optional dual-hold).

---

## Goal

Maximize **throughput of independent foreground reviews** (different trees /
attempts), not multi-model voting on one diff:

1. **Several reviews at once** per repository scope when configured (capacity N).
2. **Same provider may run multiple reviews** concurrently, up to a per-provider
   in-flight cap.
3. **429 / quota pressure** is detected, reduces that provider’s pressure, and
   the review **still completes** via hop and/or backoff-retry within budgets.
4. Operators and agents see **realistic queue signals** (position, wait budget,
   optional ETA from recent telemetry; status while waiting where applicable).

Fail-closed gate/trust semantics stay unchanged.

---

## Why

S3 delivered FIFO admission + telemetry under **effective concurrency 1**:
default `review-fg` capacity 1 **and** dual-hold of the legacy
`grok-reviews-foreground.lock`. That is correct for shadow coexistence with
legacy scripts, but it does **not** let multi-agent / multi-worktree load use
available provider capacity.

Operators need:

- More than one independent `run_review` making progress at a time.
- Safe multi-flight against one provider (risking 429) with automatic **pressure
  reduction** and **recovery**, not permanent stall or silent fail.
- No requirement that “three models agree” — only that **as many separate
  reviews as configured slots allow** can run.

---

## Product model (read carefully)

```text
                    ┌─ review-fg capacity N (FIFO, per git-common-dir) ─┐
 Independent ──────►│                                                    ├──► run
 reviews             └─ provider:<id> slots (max_in_flight, backoff) ────┘
                              ▲
                              │ 429/quota → classify unavailable + shrink
                              │ effective slots / TTL; hop or wait+retry
```

| Mode | In scope? |
|---|---|
| Concurrent reviews of **different** work / attempts | **Yes** |
| Same provider, multiple concurrent reviews (within cap) | **Yes** |
| Sequential fallback when provider unavailable/quota | **Yes** (extend) |
| Parallel multi-provider **vote on one diff** | **No** |
| Host-wide DB/Karma/browser locks | **No** |

---

## Done when (all required)

### Phase A — multi-slot repo admission (true N concurrent FG)

- [ ] **Optional legacy dual-hold.** Env (name bikeshed OK if documented), e.g.
      `SKODUN_LEGACY_FG_LOCK`:
  - default **`1` / on** — today’s dual-hold (store + mkdir lock); capacity N
    still serializes physical runs under the lock (S3 compatibility).
  - **`0` / off** — admit and run under store capacity only; **no** mkdir
    dual-hold. Required for true multi-slot concurrency.
- [ ] With dual-hold **off** and `SKODUN_REVIEW_FG_CAPACITY=N` (N≥2), hermetic
      tests prove **N concurrent** `run_review` (or admission+lock-free path)
      make progress without one waiter blocking forever behind a live peer that
      holds only a store slot.
- [ ] With dual-hold **on**, behavior remains dual-hold / effective 1 physical
      mutex with legacy path (regression pin).
- [ ] Defaults stay safe: capacity default **1**, dual-hold default **on**, so
      cutover clients are not surprised.
- [ ] Docs: concurrency fragment + integrate guide + this epic seed describe
      how to enable multi-slot and when to turn dual-hold off (after legacy
      scripts decommissioned — see cutover doc).

### Phase B — per-provider in-flight + 429 pressure

- [ ] Resource class **`provider:<id>`** (e.g. `provider:xai`) with configurable
      **max_in_flight** (default **1** per provider unless config/env raises).
- [ ] Starting inference for a chain entry **acquires** that provider’s slot;
      terminal paths **release** it (including cancel, fail, hop).
- [ ] Repo admit (`review-fg`) **and** provider slot both required before
      provider process starts (order documented; no double-count leaks).
- [ ] **429 / rate-limit / quota** from adapters continues to classify as
      `unavailable` + category **`quota`** (or equivalent); must **not** be
      treated as a clean stop that finalizes a trustworthy pass.
- [ ] On quota for provider P: update `provider_state` (existing TTL path) **and**
      **reduce pressure** on P for the TTL window by treating P as
      **max_in_flight effective = 0** (no new acquires) until TTL expires.
      (Simpler than partial decrement; document. Optional later: stepwise
      decrement.)
- [ ] **Recovery so the review still completes** within existing budgets:
  1. Prefer **next chain entry** with free slots and not in backoff, else
  2. **Wait** (bounded by admission/review wait budget) for any viable
      provider slot if no chain entry is free, then retry selection, else
  3. Fail closed with untrustworthy terminal (same fail-closed spirit as today).
  No infinite requeue of an **expired** `review-fg` admission ticket.
- [ ] Hermetic tests: concurrent acquires on same provider honor max_in_flight;
      simulated 429 frees slot, shrinks pressure, allows hop/retry path to
      succeed with a fake second provider.

### Phase C — queue realism + reschedule UX

- [ ] While waiting for **repo** and/or **provider** slots, **stderr progress**
      (and `progress_sink`) surfaces at least: **queue position** and
      **remaining wait budget**; when blocked on a provider, name the provider
      class (e.g. `provider:xai queue position 1; wait budget 30s remaining`).
- [ ] **ETA (required for Done):** p50 of last K (K=20 or fewer if not enough
      rows) completed `wait_ms` for the same `resource_class`+`scope` among
      terminal admissions; if fewer than 3 samples, omit ETA (do not invent).
      Surface as `eta≈Xs` on progress lines when present.
- [ ] **Pre-record wait** (before a review id exists): progress-only (stderr /
      sink). Do **not** require a store review row solely for queue display.
      After a `running` row exists, `review-status` continues to report
      lifecycle as today; no new required MCP tool.
- [ ] `skodun providers` lists active `provider_state` backoff (already) and,
      when cheap, count of active `provider:<id>` holders (admitted/running).
- [ ] Telemetry: **reuse** `capacity_admissions` for both `review-fg` and
      `provider:<id>` (same table, different `resource_class`). No second queue
      table unless design proves need.

### Cross-cutting (all phases)

- [ ] Design under `docs/superpowers/specs/` **before** large code (agent must
      not skip). One atomic store version bump if DDL required.
- [ ] Hermetic tests drive **shipped** `capacity` / `run_review` / chain paths
      (no test theater).
- [ ] `examples/fragments/concurrency.md` + `docs/integrate-external-project.md`
      + this seed checklist updated when shipping.
- [ ] **`gate.py` / `trust.py` unchanged** unless owner-approved.
- [ ] PR `refs` this epic issue; epic **Done** = merged to `main` + issue closed
      (see root `AGENTS.md`).

---

## Non-goals

- Parallel multi-provider **voting** / race-to-first on a **single** diff as
  default product behavior  
- Host-wide fair queue for non-review work (DB, Karma, Cypress, Heroku)  
- Changing fail-closed gate/trust semantics  
- MCP multi-queue of `review` without fingerprint re-check (keep refuse-if-busy
  unless a child explicitly implements fingerprint admit)  
- Required anthropic / metered HTTP API-key adapter (subscription-CLI premise;
  separate epic if ever wanted)  
- Replacing client full-gate tiers (TubeScribes)  

---

## Architecture notes (agent must implement against these)

### Resource classes

| Class | Scope key | Default max holders | Role |
|---|---|---|---|
| `review-fg` | `git_common_dir` string | `SKODUN_REVIEW_FG_CAPACITY` (1) | Repo-level concurrency of FG reviews |
| `provider:<id>` | provider id string (e.g. `xai`) | config/env (default 1) | Concurrent inference processes per provider |

Reuse S3 FIFO machinery where possible (`capacity.py` + `capacity_admissions`);
prefer **one table** with `resource_class` already present over a parallel
system.

### Dual-hold policy (Phase A)

| `SKODUN_LEGACY_FG_LOCK` | Store capacity | mkdir lock |
|---|---|---|
| on (default) | admit up to N | **always** dual-hold → physical ~1 with legacy |
| off | admit up to N | **not** taken by skodun |

Document: only turn dual-hold **off** when no legacy scripts share the repo, or
accept dual-backend risk.

### Admit order (Phase B) — **normative**

1. Enqueue / wait **review-fg** (FIFO) until admitted (or expire).  
2. Under the repo slot, select chain entries in order; skip providers with
   active `provider_state` or effective max_in_flight 0.  
3. For the chosen entry, acquire **provider:\<id\>** (FIFO if contended) with a
   **bounded** wait that does not exceed remaining admission/review budget.  
4. Start provider process. On hop: **release** previous provider slot **before**
   acquiring the next.  
5. On all terminal paths (success, fail, cancel): release provider slot then
   review-fg (or reverse order if needed for ABA safety — pin in design tests).

**Do not** acquire a provider slot before holding review-fg.  
**Do not** hold a provider slot while blocked only on review-fg.

### 429 / quota pressure (Phase B)

- Prefer existing `chain` + adapter `unavailable` / `quota` +
  `store.mark_provider_unavailable`.  
- Pressure reduction must be **cross-process** (store or equivalent), not only
  in-memory in one process.  
- Dead-pid / stale reclaim (S3) applies to new resource classes too.

### Recovery (Phase B+C)

```text
attempt entry E:
  if quota/429 → remember P, shrink P, release P slot
  if next entry E' free → run E'
  else if wait budget remains → wait for any viable slot (repo already held
       or re-check design for release-on-long-wait)
  else → untrustworthy failed (no silent pass)
```

Pin: a review that hit 429 on provider A and succeeded on B records provenance
on the artifact as today (`attempts[]` / answering provider).

---

## Suggested knobs (names indicative; document finals)

| Knob | Default | Phase |
|---|---|---|
| `SKODUN_REVIEW_FG_CAPACITY` | 1 | A (exists) |
| `SKODUN_LEGACY_FG_LOCK` | 1 (on) | A |
| `SKODUN_PROVIDER_MAX_IN_FLIGHT` or per-provider config | 1 | B |
| `SKODUN_PROVIDER_BACKOFF_*` / reuse quota TTL | align with `PROVIDER_UNAVAILABLE_TTL_SEC` | B |
| Admission wait envs | S3 defaults | A–C |

Config file shape for per-provider caps is preferred over only global env when
multiple providers differ (design decides).

---

## Implementation method (for AI agents)

1. **Design first** — write/update
   `docs/superpowers/specs/YYYY-MM-DD-s4-multi-slot-provider-concurrency-design.md`
   covering dual-hold off, provider class, 429 pressure, recovery, schema.  
2. **TDD / hermetic tests** — pure capacity helpers + store transactions +
   pipeline/chain integration with fakes; no real model calls.  
3. **Phase order A → B → C** (C may partially land with A for better messages).  
4. **One PR stack or clear phased PRs**, each green alone if possible.  
5. **Land path:** tests → PR → review → **merge to main** → close issues
   (`AGENTS.md` Done definition).  

### Likely touch set

| Area | Files (indicative) |
|---|---|
| Capacity | `src/skodun/capacity.py`, `src/skodun/store.py` (schema if needed) |
| Pipeline / chain | `src/skodun/pipeline.py`, `src/skodun/chain.py` |
| Config | `src/skodun/config.py` (optional per-provider caps) |
| Status | `src/skodun/services.py` (queue visibility) |
| Docs | concurrency fragment, integrate guide, this seed |
| Tests | `tests/test_capacity.py`, new provider-slot / 429 tests, pipeline |

### Test matrix (minimum)

| # | Scenario |
|---|---|
| T1 | Dual-hold on + capacity 2: still safe vs legacy mutex semantics |
| T2 | Dual-hold off + capacity 2: two waiters both become running (fakes) |
| T3 | Provider max_in_flight=1: second review on same provider waits/FIFO |
| T4 | Provider max_in_flight=2: two concurrent on same provider |
| T5 | Fake 429 on head → provider_state + pressure down + fallback succeeds |
| T6 | All providers quota → fail fast / no infinite spin; untrustworthy terminal |
| T7 | Cancel mid wait releases repo and provider slots |
| T8 | Progress/status includes position and wait budget under multi-slot wait |
| T9 | `gate.py` / `trust.py` byte-identical unless owner note |

---

## Suggested children

1. **Design** — multi-slot dual-hold policy + provider class + 429 pressure schema  
2. **Phase A** — optional dual-hold off + true `review-fg` N  
3. **Phase B** — `provider:<id>` slots + acquire in chain + backoff on quota  
4. **Phase C** — status/ETA/diagnostics + docs  
5. **Hardening** — reclaim, leak tests, multi-process stress if feasible hermetically  

---

## Risks

| Risk | Mitigation |
|---|---|
| Dual-hold off + legacy scripts → double inference | Default dual-hold **on**; docs + cutover checklist |
| Provider slot leak on cancel/exception | `finally` + reclaim by pid/age (S3 pattern) |
| 429 misclassified as degraded | Adapter conformance + explicit tests |
| Holding repo slot while waiting forever for provider | Bounded waits; design order; fail closed |
| Capacity N thrashes shared CLI accounts | Per-provider caps + backoff; defaults conservative |

---

## Out of scope reminders

Cutover of TubeScribes scripts and provider-neutral **client** gate remain
[`cutover-from-legacy-review.md`](../cutover-from-legacy-review.md). This epic
makes skodun **able** to run many concurrent reviews safely; clients must still
stop dual pipelines to benefit fully.
