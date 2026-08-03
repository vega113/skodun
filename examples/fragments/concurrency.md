# Fragment: concurrency and multi-agent use (paste into AGENTS.md)

Use on machines with **several agents** or **several providers** (grok / agy /
junie / codex).

**Definitions first** (MCP process vs repo vs worktree):  
[`mcp-review-topology.md`](mcp-review-topology.md). Capacity below is **not**
“slots on this MCP server.”

---

## skodun concurrency (read this)

### Three layers (do not conflate)

| Layer | Scope | Limit |
|---|---|---|
| MCP process | One `skodun mcp` process | **1** in-flight `review` tool call (**refuse-if-busy**, not queued) |
| `review-fg` | **Per repository** (`git_common_dir`; all worktrees of that clone share it) | `SKODUN_REVIEW_FG_CAPACITY` (default **1**) |
| `provider:<id>` | **Store-wide** (all repos on that DB) | `SKODUN_PROVIDER_MAX_IN_FLIGHT` (default **1** per provider) |

Env knobs are process-wide **defaults**; **counting** for FG is per repo, for
providers is global to the store. “3 providers × 2 = 6 concurrent reviews” is
**not** how skodun multiplies slots.

### What is concurrent today

| Resource | Concurrency |
|---|---|
| Foreground `review` (CLI) | **FIFO `review-fg` capacity** (default **1** per repository). Default **dual-hold** also takes the legacy `grok-reviews-foreground.lock` (effective single physical mutex with tubescribes). Waiters are ordered; bounded wait then exit `3`. Progress reports **queue position**, **remaining wait budget**, and **ETA** (`eta≈Xs`) when ≥3 terminal samples exist. |
| MCP `review` | **One per MCP server process.** Second call is **refused** (`"review already in flight"`), not queued (S3 choice: stale tree risk). Same process can still target many repos/worktrees **sequentially** via `repo`. |
| Provider adapters | **Sequential fallback chain** + per-provider **`provider:<id>` max_in_flight** (default **1**). Not parallel multi-provider voting on one diff. |
| Background pre-push workers | One **running** reservation per branch+repo; newer push **supersedes**. |

Having multiple providers configured does **not** mean N simultaneous reviews
of the same diff. Multi-slot FG is for **independent** reviews (separate
processes / worktrees), subject to the layers above.

### Fair capacity (epic S3 — shipped) + multi-slot / providers (S4)

| Knob | Default | Meaning |
|---|---|---|
| `SKODUN_REVIEW_FG_CAPACITY` | `1` | Max concurrent admitted+running `review-fg` holders per repo (store). |
| `SKODUN_LEGACY_FG_LOCK` | on (`1`) | **on** (unset/empty/`1`/junk): dual-hold store + mkdir lock. **`0` only**: store capacity only (true multi-slot when capacity ≥2). Do **not** turn off until legacy scripts no longer share the repo (see cutover doc). |
| `SKODUN_PROVIDER_MAX_IN_FLIGHT` | `1` | Max concurrent inference holders per `provider:<id>` (global default). |
| `SKODUN_ADMISSION_WAIT_SECONDS` | same as lock wait | Shared budget for repo admit + provider-slot waits |
| `SKODUN_LOCK_WAIT_SECONDS` | stale ceiling | Dual-hold lock wait (interop) |
| `SKODUN_LOCK_POLL_SECONDS` | `10` | Poll cadence |
| `SKODUN_LOCK_STALE_SECONDS` | ceiling | Waiter reclaim ceiling |

**Enable multi-slot FG (after legacy decommissioned):**

```bash
export SKODUN_LEGACY_FG_LOCK=0
export SKODUN_REVIEW_FG_CAPACITY=2   # or higher
```

**Provider slots:** each chain entry acquires `provider:<id>` before the
provider process starts and releases on success, hop, cancel, or fail. On
quota/429 the provider is marked in `provider_state` and effective
max_in_flight becomes **0** for the TTL; the chain hops to the next free
entry (or fails closed if none remain).

Telemetry for each attempt is **persisted** in the store (`capacity_admissions`)
for both `review-fg` and `provider:<id>`: `queued_at`, `admitted_at`,
`started_at`, `ended_at`, `wait_ms`, and `expire_reason` when the attempt
expired or was rejected. Expiry is durable — there is no blind infinite
requeue of the same attempt id.

**Diagnostics:** `skodun providers` shows active `provider_state` backoff and
active holder counts for each `provider:<id>`.

**Preflight:** if every provider in the finder fallback chain is known
unavailable via cached `provider_state`, the run **fails fast** (exit 2) without
spending the full admission wait budget.

### Auto-route the finder (epic S5 — shipped, default off)

Provider slots let several reviews run at once; they do **not** decide *which*
provider a review joins. Without routing, every un-pinned review starts at the
first enabled `finder`, so N agents pile into one `provider:<id>` FIFO while
the others idle. `[routing]` picks the queue instead.

```toml
[routing]
mode        = "auto"    # off (default) | auto
pool        = []        # reviewer NAMES; empty = every enabled role=finder
cross_model = true      # soft preference for a different provider family
```

| Knob | Default | Meaning |
|---|---|---|
| `[routing] mode` | `off` | `auto` scores the pool at head resolution |
| `SKODUN_ROUTING_MODE` | unset | `off`\|`auto`; **overrides both config layers** |
| `--client-family` / MCP `client_family` | unset | the CALLER's family (`xai`, `openai`, `google`, `junie`) |
| `SKODUN_CLIENT_FAMILY` | unset | same, per machine |

**Scoring** (once, at the start of the run — never re-scored while waiting):

| Signal | Contribution |
|---|---|
| Free slots (`max_in_flight − holders`) | `+100 × free_slots` |
| No free slot | `−10 × (queue_depth + 1)` |
| Different family from `client_family` | `+20`, only when `cross_model` |
| Tie | reviewer name ascending (two peers must agree) |

Excluded outright: `provider_state` quota blackout, a metered provider out of
daily budget, a provider with no adapter, anything outside the pool.

**Rules that do not bend:**

- **A pin always wins.** `--reviewer NAME` / `{"reviewer": "NAME"}` is absolute
  in every mode — use it for a deliberate second opinion.
- **Cross-model is soft.** `+20` breaks a tie between two equally free
  providers. It never outranks a free slot, and never excludes the last
  available family: a single-provider machine still gets reviewed.
- **Failure handling is unchanged.** After a head is chosen, that entry's own
  `fallbacks` chain runs exactly as before.
- **Extra passes are not routed.** Security / skeptic / refuter / integration
  still pick by ROLE.
- **Background pre-push reviews are not routed** in this phase; they use the
  configured finder.

**Audit.** Every review records `requested_reviewer`, `routed_reviewer`,
`route_reason` (`pinned`, `config-finder`, `auto:free`, `auto:free+cross`,
`auto:wait`, `auto:wait+cross`, `auto:default-finder`) and `client_family` on
its artifact. `auto:default-finder` means auto was on but nothing was routable
— an empty pool, or every candidate blacked out.

**Agents: prefer omitting `reviewer`.** Auto-routing can only spread load over
callers that let it choose. Pin for a second opinion, not by habit.

### Cancel / status (epic S1 — shipped)

| Surface | Verb |
|---|---|
| CLI | `skodun review-status [id] [--repo PATH]` |
| CLI | `skodun review-cancel <id>` |
| MCP | `review_status` (`review_id` and/or `repo`) |
| MCP | `review_cancel` (`review_id`) |

Status reports one of `queued|running|cancelled|failed|clean|findings` plus
age / provider / model when known. Cancel sets the in-process token (same MCP
process) or signals a confirmed worker or FG process; that live holder then
demotes the row and releases the FG lock. If the holder is already gone,
cancel writes a durable untrustworthy terminal so nothing stays forever-
`running`, and the stale sweep reclaims the FG lock. Closing the MCP session
still cancels the in-flight MCP `review`. Prefer cancel-by-id over abandon.

### Cost policy

Default: **one** skodun finder chain → gate.  
Optional: security/refuter when path-risky or churn marks say the loop is
chasing itself.  
Not default: skodun + legacy grok scripts + every cloud bot on every change.
