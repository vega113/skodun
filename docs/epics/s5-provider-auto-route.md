# Epic S5 — Provider auto-route (load balance across finders)

> **Live issue:** https://github.com/vega113/skodun/issues/69
> **Status:** design + plan reviewed; Phase A open via child issues.
> **Plan:** `docs/superpowers/plans/2026-08-03-s5-provider-auto-route.md`  
> **Depends on:** S4 provider slots + `provider_state` (shipped).  
> **Related:** [`s4-multi-slot-provider-concurrency.md`](s4-multi-slot-provider-concurrency.md),
> [`../superpowers/specs/2026-08-03-provider-auto-route-design.md`](../superpowers/specs/2026-08-03-provider-auto-route-design.md).

---

## Goal

Default review routing should **spread load across available finder providers**
on a machine (shared store), so idle subscriptions are not wasted while one
provider’s FIFO is deep — without requiring every agent to pick `--reviewer`.

Still support **explicit pin** (`--reviewer` / MCP `reviewer`) for second
opinions and forced providers.

Later phases may weight by **subscription quota % / daily credit budgets**;
MVP is **availability + short queue + cross-model preference**.

---

## Why (current gap)

Today:

* Capacity is **per-provider FIFO** (`provider:<id>`). Correct for 429.
* Head selection is **static**: first enabled `role=finder`, or explicit name.
* `fallbacks` hop on **failure/quota**, not on “Grok busy, Junie free.”

So N agents all defaulting to Grok pile onto one queue while Codex/Junie sit
idle. That is the load-balance hole S5 fills.

**Not solved by “one central MCP process”:** routing must live in
`pipeline` / `services` so CLI and every stdio MCP share the same policy and
store signals.

---

## Done when

### Phase A — MVP (ship first)

- [ ] Auto-route mode selectable: config and/or env (default **off** until
      proven, or **on** with safe pool = all enabled finders — decide in design).
- [ ] When auto-route is on and caller did **not** pass `reviewer`, pick a
      **finder entry** before first provider acquire using store-visible signals:
      free slots / queue depth / `provider_state` blackout / live holder count.
- [ ] Explicit `reviewer` / `--reviewer` **always wins** (no silent override).
- [ ] Record on the review artifact: `requested_reviewer`, `routed_reviewer`,
      `route_reason` (auditable).
- [ ] Optional **client family** hint: if client is Grok (or `client_family=xai`),
      prefer a non-xai finder when available (cross-model review preference).
- [ ] Same path for CLI and MCP; MCP may pass `client_family` from host metadata
      or env; CLI via env/`--client-family` optional.
- [ ] Docs: concurrency + AGENTS fragments; tests for pin vs auto vs cross-model.
- [ ] Gate/trust unchanged.

### Phase B — Weights (later)

- [ ] Per-provider weight or daily credit/spend share (openai-api already has
      spend; subscriptions may use soft quotas or operator %).
- [ ] Weighted least-loaded selection, not only free-slot.

### Phase C — (optional, non-goal for A)

- [ ] Mid-wait rebind to another provider (hard; cancel/re-admit). Out of MVP.
- [ ] Parallel multi-provider voting on one diff. Still non-goal.

---

## Non-goals

* Host-wide job OS / single MCP daemon as the only router  
* Replacing failure `fallbacks` (still used after a chosen head fails)  
* Auto-route of **extra passes** (skeptic/refuter keep role-based pick)  
* Guaranteeing equal quality across providers  

---

## Method

1. Land design spec under `docs/superpowers/specs/`.  
2. Implement Phase A behind a clear default; dogfood multi-agent.  
3. Phase B only after telemetry shows need.

---

## Open product decisions (resolve in PR 0 / design)

1. Default: auto-route **on** vs **opt-in** for one release.  
2. Pool: all enabled `role=finder` entries vs explicit `route_pool = [...]`.  
3. How client family is detected (MCP `clientInfo.name`, env, flag).  
4. Whether “prefer non-matching family” is hard constraint or soft score.
