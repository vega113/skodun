# Fragment: concurrency and multi-agent use (paste into AGENTS.md)

Use on machines with **several agents** or **several providers** (grok / agy /
junie / codex).

---

## skodun concurrency (read this)

### What is concurrent today

| Resource | Concurrency |
|---|---|
| Foreground `review` (CLI) | **FIFO review-fg capacity** (default **1** per repository) + legacy `grok-reviews-foreground.lock` dual-hold. Waiters are ordered; bounded wait then exit `3`. Progress reports **queue position** and **remaining wait budget**. |
| MCP `review` | **One per MCP server process.** Second call is **refused** (`"review already in flight"`), not queued (S3 choice: stale tree risk). |
| Provider adapters | **Sequential fallback chain**, not parallel multi-provider voting. |
| Background pre-push workers | One **running** reservation per branch+repo; newer push **supersedes**. |

Having multiple providers configured does **not** mean N simultaneous reviews.

### Fair capacity (epic S3 — shipped)

| Knob | Default | Meaning |
|---|---|---|
| `SKODUN_REVIEW_FG_CAPACITY` | `1` | Max concurrent admitted+running `review-fg` holders per repo (store). Raising above 1 is allowed; the **legacy FG lock still serializes physical runs to 1** while tubescribes/legacy scripts coexist. |
| `SKODUN_ADMISSION_WAIT_SECONDS` | same as lock wait | Bounded FIFO admission budget |
| `SKODUN_LOCK_WAIT_SECONDS` | stale ceiling | Dual-hold lock wait (interop) |
| `SKODUN_LOCK_POLL_SECONDS` | `10` | Poll cadence |
| `SKODUN_LOCK_STALE_SECONDS` | ceiling | Waiter reclaim ceiling |

Telemetry for each attempt is **persisted** in the store (`capacity_admissions`):
`queued_at`, `admitted_at`, `started_at`, `ended_at`, `wait_ms`, and
`expire_reason` when the attempt expired or was rejected. Expiry is durable —
there is no blind infinite requeue of the same attempt id.

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
