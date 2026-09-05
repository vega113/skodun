# Queue and request-cost observations

`skodun queue --json` inspects requests for the current worktree. Use
`--scope repository` for linked worktrees, or `--scope host` for the host's bounded
request window. An explicit `skodun queue REQUEST-ID --json` targets that request
and labels its identity even when it belongs to another worktree. The MCP
`queue` tool uses the same service, scope vocabulary and data as the CLI.
Inspection uses `Store.open_readonly`; a missing or older store is refused rather
than initialized or migrated. No provider or configuration probes run.

Each request reports owner worktree/branch/head, links, actual admission waits,
queue position among waiters, holder identities and a historical median wait
with sample count. Position 1 alone is not evidence of contention. Missing
limits are unknown. Historical wait excludes provider run time: the legacy
`wait_ms` column is total admission lifetime and is not a queue-only sample.
Missing admission timestamps on released/started holders stay unknown.

The #185 budget interface is `Store.request_budget(request_id)`. Its allowlisted
projection keeps time caps (`max_queue_seconds`, `max_review_seconds`,
`max_provider_wait_seconds`, `max_wall_seconds`), queue/review/total/provider-wait
deadlines, execution sequence, measured timing and capacity layers. Each layer
is joined by admission ID as well as resource class and scope, so a resumed
request's latest limit cannot rewrite older admission history. Configured
capacity, effective capacity and legacy dual-hold remain separate facts. No
machine-wide layer is invented while PR #180 remains unlanded. Current execution
phase and `review_paused_for_queue` are reported directly. `review_active_ms`
measures charged review allowance; `review_wall_ms` is literal time since first
provider launch. They overlap and must not be added. The review deadline remains
null while review allowance is paused for foreground readmission. An absent
budget getter is explicitly unavailable; a supported getter without a current
snapshot is missing. Getter failures report only their error category. Bounded
history can supply older admissions' exact caps without replacing current timing.

Cost counts use original artifact request ownership. A review linked for reuse
is historical coverage, not new provider execution. The #184 `attempt_id`
deduplicates raw/telemetry/checkpoint copies; old rows without stable IDs retain
explicitly incomplete identity coverage. `candidate_skips` counts all candidates
that did not launch; `eligibility_skips` counts only confirmed deterministic input
refusals. Missing binaries, cached quota and expired admission waits are candidate
skips, not transport-ineligibility evidence. Neither category is a launched call. Retry counts mean repeated launches of the same provider/model
within the same pass namespace. Recovery orchestration IDs and batch
orchestration IDs are never joined interchangeably.

Known subtotals are named `reported_*`. Full launched-call/token/prompt-byte
counts remain unknown while a request is active or linked observations are
missing or ambiguous. Prompt bytes are not billed tokens. Per-call input bytes,
maximum per-call bytes, total launched input bytes and recorded batch/integration
aggregate bytes are separate. A 6.9 MB aggregate across ten calls is not a
6.9 MB provider request. Optional input/output/cache/reasoning/total token usage
is preserved when actually reported.

Metered spend joins only owned review IDs. `api_spend_events.request_id` belongs
to the provider API namespace, not the Skodun request namespace. An empty
attributable ledger means unknown spend, not zero dollars. Subscription costs
remain unknown. Queue/provider/execution elapsed durations use unions of real
start/end intervals so overlapping holds and concurrent batches are not summed
as wall time. Missing timing remains visible. External local-gate lock waits are
a separate, unobserved metric; they cannot prove zero Skodun queue wait.

`stats` adds explicit request/execution/review-mode/recovery/batch denominators
and bounded attempt observations. Every latency aggregate identifies its window,
sample count, unit, denominator and method; statistics use nearest-rank quantiles.
Small-sample historical queue medians are labeled, never presented as forecasts.

Reads are bounded: at most 100 selected requests (default 50), 200 links per
request, 100 executions, 200 peers per resource/scope, 100 distinct resource/scope
reads shared across requests, 20 historical wait samples, 2000 unique attempt
observations from at most 6000 raw attempt references, and 2000 recent metered
ledger rows. Host/repository selection scans at most 1000 recent request rows;
scoping after that window can miss older rows and reports truncation. Request
output is bounded at 2 MiB; oversized holder projections are omitted with explicit
truncation. Missing links, malformed artifacts, incomplete identities and
truncation are coverage facts, not zeros.

## Integration status

The reader is rebased onto the shipped #184 result/attempt-ID implementation,
and a tracked review with an executable fixture verifies that its actual IDs and
input sizes reach request costs. Missing extra-pass attempts and incomplete
cancelled observations keep full totals unknown. Complete #188 acceptance still
requires #185's persisted getter/timing and free-admission progress placement.
The real save/query integration fixture activates when that API lands; it is
explicitly skipped while the dependency is absent. Interface fixtures alone do
not certify that dependency. The core reader can land separately, but #188
remains open until the integrated API and runtime checks pass.
