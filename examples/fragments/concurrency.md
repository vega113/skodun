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
