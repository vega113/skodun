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

### Cancel / status (epic S1 — when shipped)

Use `review_status` / `review_cancel` (CLI + MCP) instead of abandoning an
in-flight provider. **Today:** closing the MCP session cancels the in-flight
MCP `review`; there is no cancel-by-id from another session yet. Do not leave a
second agent waiting on the same repo lock without a human timeout.

### Cost policy

Default: **one** skodun finder chain → gate.  
Optional: security/refuter when path-risky or churn marks say the loop is
chasing itself.  
Not default: skodun + legacy grok scripts + every cloud bot on every change.
