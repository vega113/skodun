# Controlled foreground concurrency pilot

A foreground capacity of two is useful only when every tighter limiter permits
it. The legacy lock and provider/quota-pool admission still apply. Keep the
production defaults until a bounded real-provider comparison supports a change.
This guide and harness are the #192 preparation; real-pilot acceptance is tracked
in the GitHub issue.

## Reproduce the hermetic workload

From a clean Skodun checkout, use a new output directory:

```sh
PYTHONPATH=src python3 -m benchmarks.foreground_concurrency \
  --output /tmp/skodun-fg-fixture-20260905 \
  --delay-seconds 2 --with-limiter-controls
```

The harness creates a benign repository and four frozen worktrees, then invokes
the shipped review CLI and gate. A generated local executable is the only
provider. It never reads or changes the installed authority or calls a real
subscription or API provider. The output directory must not already exist.

The four profiles use the same fixture authority, workload, required-pass policy,
and explicit queue/review/provider/total budgets:

| Profile | Foreground capacity | Provider capacity | Legacy lock | Expected limiting layer |
| --- | ---: | ---: | --- | --- |
| Baseline | 1 | 2 | Off | Foreground |
| Two foreground reviews | 2 | 2 | Off | Foreground/provider |
| Legacy control | 2 | 2 | On | Legacy lock |
| Provider control | 2 | 1 | Off | Provider |

`report.json` records actual elapsed trial time, unique request/diff counts,
trustworthy completion, process overlap, real persisted capacity layers, gate
results, child CPU observations, database size and raw artifact locations.
`queue.json` and each CLI result retain stage timing, ownership and exact
identities. Provider events use monotonic nanoseconds for overlap. Every trial
contains four request samples but only one trial-wall-time observation; it is
not four measurements of total workload latency. No request-level p90 or fixed
speedup is inferred from one comparison. A separately identified machine layer
is taken from actual persisted observations when present, never invented from
an assumed PR state.

The interval data should show bounded overlap only in the capacity-two profile;
both serialization controls must stay serial. All four reviews must finalize
once at the frozen diff identity, pass the gate and release their admissions.
Timeouts and failures stay in the report. Incomplete interval or result data is
not a successful concurrency proof. Tokens, subscription dollars, external
local-gate waits, host peak memory and SQLite busy counts remain unknown where
there is no attributable measurement.

## Prepare an opt-in live profile

Before using real providers, inventory the installed build/schema, active review
and checkpoint leases, foreground legacy holders, actual provider/quota pools,
and the current status of PR #180. Enumerating old wrappers in worktrees finds
potential participants, not necessarily running workers. A live MCP PID alone
is not proof that a specific review still runs.

Use the installed authority's read-only commands where its schema supports them:

```sh
skodun store migrate --plan
skodun queue --scope host --json
skodun review-status --scope host --json
```

Older installations may not have `queue`. A source checkout can plan an upgrade
read-only, but cannot apply it to the default shared authority. Install an
immutable release wheel with the expected build identity and coordinate the
required MCP restart and explicit migration in a safe maintenance window.
The maintenance path must refuse active reviews/claims/admissions and legacy
locks; do not remove its blockers or migrate under active old workers.

For a proven current-only participant set, the opt-in foreground comparison
uses `SKODUN_REVIEW_FG_CAPACITY=1` then `2`, with the legacy lock disabled only
for that controlled repository/profile. Keep existing provider/quota limits and
all other settings unchanged. The provider cap used by the hermetic fixture is
not a recommendation to raise a real provider limit. A present machine cap is
part of the effective bound and must be recorded. Every real call uses the
existing shared database; a different SQLite file cannot bypass these limits.

Pin an explicitly selected configured subscription reviewer. Check its full
fallback chain before the trial; no metered adapter may be introduced silently.
Use the same frozen workload and explicit finite budgets for both profiles.
Capture request IDs and results even when the trial is refused, cancelled,
quota-limited or untrustworthy. Zero attributable spend rows do not establish
zero token usage or zero total cost.

## Acceptance and rollback

Publish workload/build/configuration, raw sanitized receipts, sample windows and
counts, actual queue and total timing, completion/failure rates, observed
limiting layers and process/store pressure. Distinguish a real-provider outcome
from the fixture experiment. A safe but constrained or slower result is valid;
an unperformed real trial is not completion of #192.

Restore the previous opt-in profile if trustworthy completion declines,
ownership/finalization becomes ambiguous, a limit is exceeded, provider pressure
rises without benefit, or an old participant appears. Do not silently downgrade
an authority that acquired new records: schema recovery follows its explicit
backup/receipt procedure. Leave production defaults unchanged unless measured
results and a separate rollout decision support changing them.
