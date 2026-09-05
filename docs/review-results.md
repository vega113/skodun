# Versioned review results

`skodun review --json` emits one JSON object on stdout. Progress remains on
stderr. MCP `review` returns the same object at `structuredContent.result`,
alongside its existing text, status and metadata. Default CLI banners and exit
codes remain compatible. The current schema identifier is `review-result/v1`.

| Field | Meaning |
|---|---|
| `ids` | Logical request, observed review and its original request, recovery orchestration, and batch orchestration IDs; missing IDs are null. |
| `identity.requested` / `identity.observed` | Separate exact repository/worktree/head/base/diff identities; no latest-review lookup fills missing observations. |
| `execution` | Terminal state, stable reason code, compatible exit code, request durability, replay/reuse flags, and nullable retry/continuation eligibility. Null eligibility means not established; it is not authorization to retry. |
| `coverage` | Observed artifact trust, parse status, and partial-batch evidence. Null means no artifact observation. |
| `findings` | Observed finding total; open triage count remains null because this projection does not evaluate triage. |
| `gate` | Always `evaluated=false, exit_code=null`. A review result never substitutes for the authoritative gate. |
| `timing` | Explicit timing scope and known fields; absent measurements remain null. Attempt durations and queue waits stay on their own rows. |
| `bytes` | `scope=review_aggregate`: the artifact's prompt-byte aggregate, never an individual invocation's input size. |
| `counts` | `scope=observed_review` and review ID: candidate rows and actual process launches in that observed artifact. Historical replay/reuse counts describe the observed review, not new work. |
| `orchestration` | Separately scoped recovery attempt/review IDs and batch count; never combined with provider-process counts. |
| `attempts` | Up to 128 bounded rows with scope, stable attempt ID, ordinal, provider/model, launched flag, reason code, input bytes, transport ceiling and timing. |
| `causes` | Structured attempt causes retained even if a configured fallback later succeeds. |

Each attempt's `input_scope=provider_input` names the complete encoded payload
offered to that candidate. `launched=false` means no provider process started;
input bytes then describe the rejected/offered payload, not transmitted work.
Even a launched process is not proof of a model API call or charge. Historical
artifacts lacking `input_bytes` report null, even when aggregate bytes exist.
Attempt IDs survive persisted checkpoint copying; ordinals alone are not unique
across chains or resumed executions.

`attempts_truncated=true` marks the 128-row display bound. Counts and cause codes
still include every candidate in the observed artifact. The request ledger stores
only a compact observation, not whole artifacts, findings, prompts or outputs.
`review-status` / artifacts remain the detailed evidence surfaces.

Reason codes come from typed exceptions, explicit execution branches, request
state and provider classification fields. Scripts must not parse banner prose.
Core codes include:

| Situation | Reason code |
|---|---|
| Trustworthy clean / findings | `review_clean` / `review_findings` |
| Trusted historical reuse | `trusted_reuse` |
| Typed preflight, invalid repository/config/options | `preflight_refused`, `repository_invalid`, `configuration_invalid`, `invalid_input` |
| Startup/store/publication failure | `pipeline_unavailable`, `persistence_failed`, `request_persistence_failed` |
| Admission expiry | `admission_expired` |
| Requested cancellation / wall budget / attempt budget | `requested_cancel`, `budget_expired`, `recovery_attempts_exhausted` |
| No available compatible transport route | `no_compatible_route`; causes retain `transport_ineligible` and any upstream timeout/quota |
| Provider quota / timeout / unusable review | `provider_quota`, `provider_timeout`, `review_untrustworthy` |
| Partial batch evidence | `review_partial` (or a more specific terminal cause), with `coverage.partial=true` |
| Idempotent observation/refusal | Existing stable `request_*` codes; a malformed stored result is `request_result_invalid` |

A successful fallback still reports `review_clean`/`review_findings`; earlier
failures remain in `causes` and scoped attempt rows. A no-review failure keeps
`ids.review_id` and observed coverage null instead of copying an earlier recovery
attempt. Result projection is additive and needs no store schema migration.
