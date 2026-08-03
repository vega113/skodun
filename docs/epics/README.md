# Product epics (post #23)

## Done definition (agents)

**Epic / issue complete = merged to `main` + GitHub issues closed.**  
Not: tests pass only on a branch. See repository root [`AGENTS.md`](../../AGENTS.md).

## Status

| Epic | Issue | Seed | Status |
|---|---|---|---|
| **S1** Status + cancel | [#41](https://github.com/vega113/skodun/issues/41) | [s1-status-cancel.md](s1-status-cancel.md) | **Shipped** — [PR #49](https://github.com/vega113/skodun/pull/49) |
| **S3** Fair review capacity | [#42](https://github.com/vega113/skodun/issues/42) | [s3-fair-capacity.md](s3-fair-capacity.md) | **Shipped** — [PR #50](https://github.com/vega113/skodun/pull/50) |
| **S4** Multi-slot FG + per-provider concurrency + 429 backoff | [#56](https://github.com/vega113/skodun/issues/56) | [s4-multi-slot-provider-concurrency.md](s4-multi-slot-provider-concurrency.md) | **Shipped** — see design [S4 spec](../superpowers/specs/2026-08-02-s4-multi-slot-provider-concurrency-design.md) |
| **S5** Provider auto-route (load balance finders) | *(open issue)* | [s5-provider-auto-route.md](s5-provider-auto-route.md) | **Design** — [spec](../superpowers/specs/2026-08-03-provider-auto-route-design.md) |

Children (closed with parents):

| Issue | Parent |
|---|---|
| [#43](https://github.com/vega113/skodun/issues/43) status CLI | S1 |
| [#44](https://github.com/vega113/skodun/issues/44) cancel CLI/MCP | S1 |
| [#45](https://github.com/vega113/skodun/issues/45) capacity design | S3 |
| [#46](https://github.com/vega113/skodun/issues/46) admission into `run_review` | S3 |

S4 suggested children: design → Phase A multi-slot → Phase B provider slots/429 → Phase C queue UX (see seed).

Client wiring for external projects: [../integrate-external-project.md](../integrate-external-project.md).  
Legacy → skodun cutover (shadow → gate → decommission → policy): [../cutover-from-legacy-review.md](../cutover-from-legacy-review.md).  
Agent paste-ins: [../../examples/fragments/](../../examples/fragments/).
