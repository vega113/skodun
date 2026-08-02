# Fragment: concurrency and multi-agent use (paste into AGENTS.md)

Use on machines with **several agents** or **several providers** (grok / agy /
junie / codex).

---

## skodun concurrency (read this)

### What is concurrent today

| Resource | Concurrency |
|---|---|
| Foreground `review` (CLI) | **One per repository** (repo lock). Others wait, then may exit `3`. |
| MCP `review` | **One per MCP server process.** Second call is **refused**, not queued. |
| Provider adapters | **Sequential fallback chain**, not parallel multi-provider voting. |
| Background pre-push workers | One **running** reservation per branch+repo; newer push **supersedes**. |

Having multiple providers configured does **not** mean N simultaneous reviews.

### Capacity work (epic S3 — when shipped)

Fair machine-local admission (FIFO waiters, explicit capacity, queue
telemetry) will replace blind multi-hour waits. Until then: **serialize
foreground reviews**; do not start competing full suites + reviews on the same
repo without coordination.

### Cancel / status (epic S1 — shipped)

| Surface | Verb |
|---|---|
| CLI | `skodun review-status [id] [--repo PATH]` |
| CLI | `skodun review-cancel <id>` |
| MCP | `review_status` (`review_id` and/or `repo`) |
| MCP | `review_cancel` (`review_id`) |

Status reports one of `queued|running|cancelled|failed|clean|findings` plus
age / provider / model when known. Cancel sets the in-process token (same MCP
process), signals a confirmed worker or FG process, and leaves a durable
untrustworthy terminal when the holder is gone — releasing the FG lock so
nothing stays forever-`running`. Closing the MCP session still cancels the
in-flight MCP `review`. Do not leave a second agent waiting on the same repo
lock without a human timeout; prefer cancel-by-id over abandon.

### Cost policy

Default: **one** skodun finder chain → gate.  
Optional: security/refuter when path-risky or churn marks say the loop is
chasing itself.  
Not default: skodun + legacy grok scripts + every cloud bot on every change.
