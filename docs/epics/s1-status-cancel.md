# Epic S1 — Review status + cancellation (first-class)

> **Live issue:** https://github.com/vega113/skodun/issues/41  
> This file is the in-repo design seed and agent handoff.

## Goal

Agents and humans can **observe** and **cancel** any in-flight skodun review
without orphaning provider processes or leaving durable `running` rows forever.

## Why

Multi-agent load leaves reviews “stuck” when a provider hangs or a session dies.
MCP today: one `review` in flight; disconnect cancels, but there is no
cross-session cancel-by-id or uniform status surface. Background prepush already
has SIGTERM → cancel token; FG/MCP must match that product contract.

## Done when

- [x] CLI: `skodun review-status` (or `skodun status`) by `review-id` and/or
      “current for `--repo`”
- [x] CLI: `skodun review-cancel <id>` (or `skodun cancel`)
- [x] MCP tools: `review_status`, `review_cancel` (read/mutate only in-flight /
      terminal metadata — not a second gate)
- [x] Cancel: sets cancel token, terminates provider process group, durable
      terminal record (`cancelled` / failed with reason), releases FG lock
- [x] Status reports: `queued|running|cancelled|failed|clean|findings` (+ ages,
      provider, model when known)
- [x] Stale recovery for FG `running` rows aligned with prepush `recover_stale`
- [x] Tests: hermetic cancel mid-fake-provider; status after terminal; MCP parity
      via `services`
- [x] Docs: `examples/AGENTS.md` + `examples/fragments/concurrency.md` +
      `docs/integrate-external-project.md` updated for shipped verbs
- [x] `gate.py` / `trust.py` unchanged unless owner-approved pin change

## Non-goals

- Fair multi-waiter FIFO capacity (epic **S3**)
- Host-wide queue for non-review work
- Unlimited MCP queue without tree fingerprint

## Suggested children

1. Status read model (store fields + CLI/MCP)
2. Cancel path for FG + MCP long-running slot
3. Docs + agent fragments

## Method

Investigate → short design under `docs/superpowers/specs/` if schema changes →
implement with `SKODUN_DB` tests only → PR `refs` this epic.
