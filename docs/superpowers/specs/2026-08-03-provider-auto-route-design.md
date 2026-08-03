# Design: Provider auto-route (S5)

**Date:** 2026-08-03  
**Epic:** [`docs/epics/s5-provider-auto-route.md`](../../epics/s5-provider-auto-route.md)  
**Status:** proposed

---

## Problem

Operators configure several providers (e.g. Grok, Codex, Junie), each with
`SKODUN_PROVIDER_MAX_IN_FLIGHT` ≥ 1. Independent reviews *could* use free slots
on different providers, but **head selection is sticky**: everyone uses the
configured finder (or the same default), so load piles onto one `provider:<id>`
FIFO while others stay empty.

Failure **fallbacks** only hop after quota/unavailable/hard failure — not when
the preferred provider is merely **busy**.

Agents also often want **cross-model review**: if the coding client is Grok,
prefer a non-Grok reviewer when available.

---

## Goals

1. **Default path:** no `reviewer` argument → router picks a finder entry.  
2. **Pin path:** `reviewer` / `--reviewer` → unchanged absolute override.  
3. **Signals (MVP):** free capacity, active holders, queued depth,  
   `provider_state` blackout (effective capacity 0).  
4. **Client family (MVP soft rule):** prefer finder whose provider family ≠  
   client family when alternatives exist.  
5. **One policy for CLI + MCP** (shared `services` / `pipeline`).  
6. **Auditable:** record what was requested vs routed and why.

## Non-goals (MVP)

* Mid-flight re-route while already waiting on a provider slot.  
* Parallel multi-provider voting on one diff.  
* Full credit/% economics (Phase B).  
* MCP-process-only scheduler (stdio multi-process must share store rules).

---

## Architecture placement

```text
CLI / MCP review call
        │
        ▼
  svc_review(reviewer=?, client_family=?)
        │
        ▼
  run_review → resolve_head(cfg, store, …)
        │         ├─ pin if reviewer set
        │         └─ else auto_route(pool, store, client_family)
        ▼
  acquire review-fg → acquire provider:<picked> → chain (fallbacks on failure)
```

**Not** “the MCP server allocates.” Any stdio MCP and the CLI must call the
same resolver so two processes do not apply different rules.

Store remains the **authority** for load signals (`capacity_admissions`,
`provider_state`).

---

## Configuration (proposed)

### Mode

```toml
[routing]
# off | auto  (MVP)
mode = "auto"

# Optional explicit pool of [[reviewers]] names (role finder, enabled).
# Empty / omit = all enabled reviewers with role = "finder".
pool = ["finder-grok", "finder-codex", "finder-junie"]

# Soft preference: avoid provider families matching the client.
# true (default when mode=auto): prefer cross-family when any candidate free.
cross_model = true
```

Env overrides (operator-friendly):

| Env | Meaning |
|---|---|
| `SKODUN_ROUTING_MODE=off\|auto` | Overrides config mode |
| `SKODUN_CLIENT_FAMILY=xai\|openai\|google\|junie\|…` | Declared client family |

### Provider family map

Map adapter/provider ids to a **family** for cross-model:

| provider id | family |
|---|---|
| `xai` | `xai` |
| `openai`, `openai-api` | `openai` |
| `google` | `google` |
| `junie` | `junie` (or `jetbrains` if we want one bucket) |

Client family uses the same vocabulary.

---

## Selection algorithm (Phase A)

Input:

* `pool`: list of enabled finder `Reviewer` entries  
* `store`: live holder counts + optional queue depths per `provider:<id>`  
* `client_family`: optional string  
* `cross_model`: bool  

For each candidate entry `r`:

1. **Hard exclude** if provider has no adapter or is not registered.  
2. **Hard exclude** if `provider_state` says unavailable (effective capacity 0).  
3. Score (higher better), deterministic ties by `(score, name)`:

| Signal | Score contribution (MVP sketch) |
|---|---|
| Free slots (`max_in_flight - holders > 0`) | +100 × free_slots |
| No free slots (must wait) | −10 × (queue_depth + 1) |
| Cross-model: `family(r) != client_family` | +20 if `cross_model` and client_family set |
| Same-family as client | +0 (still eligible) |
| Stable name order | tie-break only |

Pick **argmax**. If pool empty after excludes → fall back to today’s  
“first enabled finder” or refuse with a clear preflight (prefer refuse if  
mode=auto and pool configured but all blacked out — fail closed fast).

**Important:** scoring runs **at head resolution**, once per review start.  
It does not re-score every poll while waiting (avoids thrash). Failure still  
uses the chosen entry’s **`fallbacks`** chain as today.

### Pin path

If `reviewer` is non-empty → `_requested_head` only; no auto-route.  
Record `route_reason = "pinned"`.

### Default when `mode = off`

Identical to today: first enabled `role=finder` (or pin).

---

## Client family sources

Priority (first wins):

1. Explicit CLI `--client-family` / MCP tool arg `client_family` (optional).  
2. Env `SKODUN_CLIENT_FAMILY`.  
3. MCP only: map `initialize` `clientInfo.name` heuristics  
   (`grok`, `claude`, `codex`, `cursor`, …) → family or `unknown`.  
4. `unknown` → cross-model bonus disabled (availability-only scoring).

Do not invent a hard dependency on a particular host.

---

## Artifact fields (additive)

On the review record / JSON artifact:

| Field | Meaning |
|---|---|
| `requested_reviewer` | Pin name or null |
| `routed_reviewer` | Entry name that headed the chain |
| `route_reason` | `pinned` \| `auto:<brief>` e.g. `auto:free+cross` |
| `client_family` | Resolved family or null |

No change to trust axes or gate identity (still content-hash based).

---

## Concurrency interaction

| Layer | Unchanged? |
|---|---|
| `review-fg` FIFO per repo | Yes |
| `provider:<id>` FIFO store-wide | Yes — router only **chooses which queue to join** |
| MCP refuse-if-busy | Yes |
| Failure fallbacks | Yes — after a routed head fails |

Auto-route **improves utilization** of the existing S4 slots; it does not replace
them.

Example with max_in_flight=2 per provider, three finders, six independent
reviews, `mode=auto`, no pins:

* First picks fill free slots across providers (spread).  
* Later picks join the shortest/least-loaded queues.  
* Cross-model biases Grok-client reviews toward non-xai when free.

---

## Phase B (later): credits / quota share

Not MVP. Sketch only:

* Config `[[routing.weights]] provider = "xai" weight = 50` (percent or relative).  
* Or daily spend already tracked for `openai-api`; soft caps for subscriptions  
  via operator-declared “effective daily slots.”  
* Score penalty when usage_share > weight_share.

Ship Phase A telemetry (`route_reason`, holder counts) before weights.

---

## API / MCP surface

| Surface | Change |
|---|---|
| CLI `review` | Optional `--client-family`; routing via config/env |
| MCP `review` | Optional `client_family`; keep `reviewer` pin |
| MCP `initialize` | May stash `clientInfo` on the server for default family |
| `providers` / `doctor` | Optional: show routing mode + pool |

Tool description: document that omitting `reviewer` uses auto-route when enabled.

---

## Testing plan (Phase A)

1. Unit: score function pure — free slot beats busy; blackout excluded;  
   cross-model prefers other family when free.  
2. Unit: pin ignores scores.  
3. Integration: two providers, max 1 each; three sequential auto reviews  
   without pin → not all three head the same provider if both free at start  
   (control time with fake holders).  
4. MCP: omit reviewer with mode=auto → `routed_reviewer` set; with reviewer  
   → pin.  
5. Regression: mode=off matches pre-S5 head selection.

---

## Risks

| Risk | Mitigation |
|---|---|
| Surprise model quality | Default mode off for one release **or** require explicit pool |
| Flapping choice | Score once at start only |
| Stale holder counts | Same store path as capacity; reclaim already exists |
| Agents still pin Grok | Docs: prefer omit `reviewer`; pin only for second opinion |
| Cross-model too aggressive | Soft bonus, never hard-exclude last available family |

---

## Implementation sketch (files)

| Area | Touch |
|---|---|
| `config.py` | `[routing]` table |
| `pipeline.py` or new `routing.py` | `resolve_head` / `auto_route` |
| `services.py` / `cli.py` / `mcpserver.py` | Pass `client_family` |
| `store.py` | Optional helper: holder count + queued count by provider (may already exist via capacity APIs) |
| Docs fragments | concurrency, mcp-loop, AGENTS |
| Tests | `test_routing.py` + pipeline/MCP hooks |

Schema bump: **only if** new persisted columns are required outside artifact JSON.  
Prefer artifact-only first (no `SCHEMA_VERSION` bump).

---

## Rollout recommendation

1. Land design + epic (this doc).  
2. Implement Phase A with `mode = "off"` default; dogfood `mode = "auto"` in  
   multi-provider.toml example.  
3. After dogfood, flip example/docs default to `auto` for multi-provider  
   installs; keep pin for power users.  
4. Phase B weights when operators ask for credit fairness.

---

## Decision log (to fill at implement time)

* [ ] Default mode on first merge: off / auto  
* [ ] Pool default: all finders / require explicit list  
* [ ] Cross-model: soft only (proposed)  
* [ ] GitHub issue number for S5  
