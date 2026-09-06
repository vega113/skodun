# Controlled foreground concurrency pilot — September 6, 2026

Keep the production default at one foreground review. Two slots allowed real overlap without exceeding provider limits, but the two small comparisons moved in opposite directions. This validates a bounded opt-in profile; it does not establish a general speedup or justify raising provider capacity.

## Results

Each row is one trial of four requested reviews across the same four frozen worktrees. All 16 requests produced trustworthy exact-diff coverage, with one execution and one final review record each. One review returned a finding and its gate stayed blocked; no findings were dismissed for this experiment.

| Series / build | Foreground slots | Trial wall time | Trustworthy completions | Actual CLI launches | Gate exits |
| --- | ---: | ---: | ---: | --- | --- |
| Initial / `7f97e84` | 1 | 193.39s | 4/4 | 4 known; total unknown | 0, 0, 0, 0 |
| Initial / `7f97e84` | 2 | 216.58s | 4/4 | 4 known; total unknown | 0, 0, 0, 0 |
| Attribution fixed / `cc631b8` | 1 | 256.16s | 4/4 | 8, complete | 0, 0, 0, 1 |
| Attribution fixed / `cc631b8` | 2 | 178.52s | 4/4 | 8, complete | 0, 0, 0, 0 |

The initial run exposed dropped unbatched follow-up attempt arrays. #216 fixed that observation gap; #215 was closed after installed live confirmation. The initial records remain incomplete rather than receiving invented historical counts. The corrected pair reports all 16 distinct provider invocation IDs, including required skeptic calls, with no retries or skipped candidates in that pair. A provider invocation means a CLI launch, not every model/API turn the CLI may perform internally.

Sampled foreground peaks were exactly 1 and 2 under their respective profiles. Every observed provider/quota-pool peak stayed at 1. One xAI admission timed out in the initial two-slot trial and the configured fallback completed; that is retained as a constraint, not hidden as success at the first provider. Every admission is terminal, there are no remaining active admissions, and no admission-timestamp FIFO inversion was observed. The same-second and legacy-limit invariants also have the shipped hermetic regressions from #211/#214.

## Workload and configuration

The workload is four small Python additions, one per worktree, implementing an ordered unique-tag normalizer with explicit input validation and limits 10/20/30/40. It is synthetic acceptance code, not a representative production-scale TubeScribes diff. Each initial primary prompt was about 2.3 KB. The frozen head/base/diff/config identities and per-request receipts are in [the sanitized evidence](2026-09-06-foreground-concurrency-pilot.json). Each profile has four unique diffs; all four profiles together still contain only four unique diffs and sixteen explicitly fresh requests.

Lanes 1/3 pin the existing `finder-codex` configuration (`openai`, `gpt-5.6-luna`, high effort); lanes 2/4 pin `finder` (`xai`, `grok-4.6`, medium effort). Existing subscription fallback graphs and pass policy are retained. The reachable configured paths were checked to exclude the metered adapter, and Codex CLI reported ChatGPT authentication. No provider limit, credential, metered route or production default was enabled by the experiment.

Both profiles use the actual shared authority and provider capacity 1 per resource scope. Only the controlled repository's foreground capacity changes from 1 to 2; its legacy dual hold is explicitly off after confirming that all participants are current. PR #180 remains open and no machine-cap layer exists in these measured builds. The legacy-on and provider-cap-one serialization controls are covered by the separate hermetic harness; no other database was used for real-provider traffic.

Per-request budgets are identical: queue 600s, review 240s, provider admission 60s, total 900s. Existing polling, context packing, model configuration and conditional-pass policy remain unchanged. Initial CLI prompt bytes are not billed tokens. Provider usage and subscription dollars remain unknown where no attributable observation exists; zero API-spend rows do not establish zero cost.

## Reproduce and interpret

After an authorized shared-authority maintenance/measurement window, freeze four independent worktrees and run four concurrent CLI requests per profile. Use the same selected reviewer per lane and the same inputs/settings in both profiles:

```sh
SKODUN_REVIEW_FG_CAPACITY=1 SKODUN_PROVIDER_MAX_IN_FLIGHT=1 \
SKODUN_LEGACY_FG_LOCK=0 skodun review --repo <frozen-worktree> \
  --reviewer <finder-codex-or-finder> --fresh --json \
  --request-key <unique-key> --max-queue-seconds 600 \
  --max-review-seconds 240 --max-provider-wait-seconds 60 \
  --max-wall-seconds 900
```

Repeat with foreground capacity 2 and new request keys. Inspect each request through `skodun queue <id> --json` and verify its current diff with `skodun gate`. Keep failed/expired/finding outcomes in the dataset. The retained operator driver and complete private receipts are under the audit host's `skodun-ep181-live-pilot-20260906` artifact directory; the JSON companion publishes sanitized per-request evidence without prompts, transcripts or credentials.

Trial time is measured with the driver's monotonic clock and includes CLI startup and up to one second of completion-detection lag. Admission observations are sampled once per second; their peaks are observed lower bounds, not a continuous process census. Request queue/review/total timings come from the engine's scoped observations and are never summed to impersonate overlapping trial wall time. Host/network/provider load was not isolated. There is one trial per configuration in each series, so no p90, fixed speedup, causal attribution or production recommendation follows. Recorded lineage context bytes were zero in all sixteen requests.

## Upgrade, safety and rollback

The installed schema-16 runtime was drained before the shipped migration guard applied schema 20. All original values across sixteen tables, including 10,541 reviews, were preserved and SQLite integrity checked. The command created its backup and receipt; no active review was interrupted to force migration. #216 subsequently changed only attempt observation persistence, with no further schema migration.

Stop an opt-in profile on ownership ambiguity, stale certification, cap/fairness violations or failure-rate regression. Restore foreground capacity 1 for future requests; never change an active request's frozen execution policy. Code-only rollback must use a schema-compatible wheel. Restoring the schema-16 backup after new records exist requires preserving those records and an explicit recovery decision; reinstalling an old wheel alone is not a database rollback.

During the pilot, the configured Codex UI MCP connection required a host refresh; a newly started MCP process from the same installed authority passed legacy-status and queue checks. Fresh CLI/MCP processes were used for validation. Consumer coordinators paused new submissions for the measurement window; final client-refresh and release status is tracked in #181.
