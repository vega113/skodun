# Epic S5 — Provider auto-route (load balance across finders)

> **Live issue:** https://github.com/vega113/skodun/issues/69
> **Status:** Shipped. Phase A (PR #83, default `mode = "off"`), routing
> telemetry (PR #85) and Phase B weights (PR #97) are all on `main` and
> #69 is closed. Phase C is closed as not warranted rather than as done —
> see the Phase C section below.
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

- [x] Auto-route mode selectable: `[routing] mode` plus `SKODUN_ROUTING_MODE`.
      Default **off**; pool defaults to all enabled finders.
- [x] When auto-route is on and caller did **not** pass `reviewer`, pick a
      **finder entry** before first provider acquire using store-visible signals:
      free slots / queue depth / `provider_state` blackout / live holder count.
      Scored once, at head resolution (`pipeline.resolve_review_head`).
- [x] Explicit `reviewer` / `--reviewer` **always wins** (no silent override).
- [x] Record on the review artifact: `requested_reviewer`, `routed_reviewer`,
      `route_reason`, `client_family` (artifact fields only — no schema bump).
- [x] Optional **client family** hint: soft `+20` for a different family; never
      a hard exclusion, so the last available family still reviews.
- [x] Same path for CLI and MCP (`services.svc_review` → `run_review`). MCP
      resolves `client_family` from the tool argument, then
      `SKODUN_CLIENT_FAMILY`, then the handshake `clientInfo.name`; CLI has
      `--client-family` and the same env.
- [x] Docs: README, concurrency + mcp-loop + AGENTS fragments,
      `examples/multi-provider.toml`; tests for pin vs auto vs cross-model.
- [x] Gate/trust unchanged.

**Not routed in Phase A** (deliberate, recorded here so it is not mistaken for
an oversight): the background pre-push worker. It is a different surface with a
reserved, identity-pinned record; the design's diagram covers the foreground
loop.

### Phase B — Weights (shipped, PR #97, tracked as #77)

- [x] Per-provider weight or daily credit/spend share (openai-api already has
      spend; subscriptions may use soft quotas or operator %).
- [x] Weighted least-loaded selection, not only free-slot.

Weights are **declared, never inferred**: what they express — how much of a
subscription a review consumes — is not observable to skodun for a flat-rate
CLI, so a router that inferred one would be acting on a number it made up.

### Phase C — (optional, non-goal for A)

- [x] Mid-wait rebind to another provider (hard; cancel/re-admit). **Closed as
      not warranted** (#109), on the measurement that issue named rather than
      on a claim that the collision cannot happen: across 6623 stored artifacts
      and 205 auto decisions, `auto:wait` has never once been recorded. The
      counter is live and pinned end to end, so the zero is a real zero.
      `skodun providers --since-days N` is how to re-check if concurrency
      rises.
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

## Product decisions (resolved)

1. Default: **opt-in** — `mode = "off"` on first merge; `examples/multi-provider.toml`
   ships `auto` because that is the config auto-routing exists for.
2. Pool: **all enabled `role=finder`** by default; `[routing] pool` narrows it
   by name, validated against the merged reviewer table.
3. Client family: explicit flag/argument → `SKODUN_CLIENT_FAMILY` → MCP
   `clientInfo.name` heuristic → undeclared (availability-only scoring).
4. Cross-model is a **soft score** (`+20`), never a hard constraint.
