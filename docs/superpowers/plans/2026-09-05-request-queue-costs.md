# Request queue and cost inspection plan

**Goal:** Provide one non-mutating CLI/MCP queue/request-cost view with explicit
ownership, namespaces, observation coverage, timing and usage semantics (#188).

**Architecture:** A bounded `queueview.py` reads request/execution/link records,
exact linked reviews and indexed capacity peers. It projects allowlisted data;
raw request result text, prompts, output, owner tokens and secrets never leave
the reader. CLI and MCP delegate to `svc_queue` and use read-only store opens.
No migration. #184/#185 supply additional persisted result/timing/limit facts;
missing contracts remain visible integration gaps until those slices land.

- [x] Add failing shipped service/CLI/MCP fixtures for four owners, expired waits,
  free admission and historical median labeling; validate read-only behavior.
- [x] Build bounded request/peer/link reads with explicit missing/orphan/truncated
  coverage. Preserve recovery and batch namespaces and current worktree scope;
  repository/host scope and explicit IDs must be visibly identified.
- [x] Aggregate unique attempts using actual call identity when present and
  conservative provenance otherwise. Do not count eligibility skips as calls,
  repeat nested telemetry, or sum overlapping queue/provider/execution intervals.
  Keep missing usage/spend unknown; distinguish per-call and aggregate bytes.
- [x] Expose shared queue text/JSON and metadata-rich stats timings. Label every
  latency summary with window, sample count, unit, denominator and method.
- [x] Add regression fixtures for resumed requests, duplicate attempt references,
  concurrent batches, missing links/usage, namespace collisions and 6.9 MB batch
  aggregate. Register the new Store-using tests in lifecycle inventory.
- [ ] Self-review and run focused tests, then push a PR without merging or claiming
  complete #188 acceptance until result/timing integration is verified.

Self-review: no gate/trust/process/routing behavior changes, no provider calls,
no live stores/configs. Root185 owns progress emission timing/historical label
runtime changes to avoid concurrent edits. Root183 owns shared scope controls;
mirror their worktree/repository/host vocabulary until that helper lands. Keep
query and output limits explicit; partial telemetry is not a complete total.

## Integration follow-up after #184/#183 landed

- Rebase onto current main; use control.scope_identity for queue selection.
- Fix the demonstrated CI failure: describe every queue MCP schema property.
- Preserve #184 incomplete attempt/cancellation accounting rather than inventing
  totals from missing extra-pass rows. Verify a real tracked executable-fixture
  review feeds its persisted attempt ID and input bytes into costs.
- Consume #185 phase, paused-review state, review_active_ms and exact historical
  admission layers. Keep absent/missing/failed getter states explicit and distinct.
- Add a real save/query fixture that activates after #185's migration/API lands;
  keep schema 19 out of this PR until root's runtime slice merges.

Self-review: no runtime budget hooks/progress placement edits in this follow-up;
no owner tokens or raw exception text enter read projections. Phase/timing
counters overlap and are never summed. Existing review threads were empty.
