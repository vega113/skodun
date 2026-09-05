# Compatible continuation core implementation plan

Goal: expose an explicit compatible continuation policy for batched reviews, preserving usable exact evidence while retrying failed/dependent passes; refs #186. Runtime-budget integration is verified against schema-19 main; root owns final full-suite and merge checks.

Architecture: consumed source orchestrations stay immutable. A continuation creates a child generation whose optional `continuation_source` identity field names the source; absent fields remain omitted so existing canonical identity hashes do not change. Exact content identity comparison still requires all existing repository/worktree/head/base/diff/context/checklist/reviewer/config/policy/planner/boundary/pass fields. The source field distinguishes generations, not content compatibility. Child creation and usable checkpoint seeding are one transaction, and the existing per-pass claims plus atomic publication remain authoritative.

- [x] Add shipped service tests that first produce interrupted or consumed-untrustworthy batch runs, then request `continue_compatible=True`: reuse usable batches; retry failed batches/integration; rerun integration whenever any batch changes.
- [x] Add a strict usable-payload predicate (parsed, non-degraded, complete diff, no failure) using CheckpointPayload validation. Extend OrchestrationIdentity with the optional source field while preserving old canonical bytes.
- [x] Add consumed-untrustworthy candidate selection and transactional generation creation/seeding in Store. Require current logical request ownership, source linkage, exact identity, unexpired source, and no live source claims; never clone a trustworthy consumed aggregate. Existing `create_orchestration` row insertion becomes a shared private helper, keeping creation+seeding atomic.
- [x] Add explicit shared service/CLI/MCP continuation option. Reject incompatible fresh/reuse/key combinations. Preserve default fresh-recovery behavior; explicit compatible recovery may continue between attempts. Request adoption includes consumed-untrustworthy candidates and preserves logical request identity while retaining ownership/actor checks.
- [x] Wire the prepared-plan boundary to generation creation and use the child's exact digest for finalization. Record bounded continuation source/action/mismatch metadata without replacing termination/timing. Reused checkpoint payloads keep original attempt IDs; new calls keep fresh IDs. The #194 guard remains authoritative and never admits/spawns an unchanged impossible fallback.
- [x] Test changed identities/boundaries and intentional fresh opinions, two racing continuers, stale fences, failed integration retry, source immutability, and skipped transport with capable retry. Add new Store-using test module to sweep inventory.
- [x] Run focused shipped tests, self-review and push an independent PR with refs #186. Do not close the issue or merge until #185 integration and coordinated final checks pass.

Self-review: cloning immutable generations avoids stale publishers finalizing against rearmed source rows. The optional identity source yields a unique exact child digest for concurrent dedup without a migration or an overloaded prose field. Only usable source batches can seed a child; integration seeds only when all source batches are reused and its own payload is usable. Changed content fails exact comparison before seeding. No adaptive replan or truncation is introduced: changed plans invalidate reuse and require explicit new work. Request ownership prevents an active original execution and a continuation from both spending calls. No provider-wide failure cache, paid route, gate/trust change, or extra-pass persistence is included. Owner expedited exception permits one self-review pass without external-review waiting.

## Core validation and self-review

- Initial four shipped service regressions failed on the missing continuation policy. The implementation now passes them and the expanded 16-case continuation suite.
- Continuation + checkpoint + inventory under ResourceWarning-as-error: 70 passed in 20.49 seconds.
- Continuation + checkpoints + MCP schema: 70 passed in 21.66 seconds.
- Shared requests/services/results + MCP schema: 152 passed in 26.45 seconds.
- Broader continuation/batched/checkpoint run: 144 passed, 5 skipped in 82.70 seconds. Earlier combined checkpoint/request/service/MCP coverage: 254 passed.
- A real finding regression proved that the source's newly indexed findings changed its own advisory context; preserving the source lineage cutoff fixed it while retaining exact rebuilt hashes. An ancestor-selection regression proved that completed children could leave historical parents selectable; generation-aware selection now excludes those ancestors without mutating source evidence.
- Self-review verified source identity digest/pass-row equality, usable payload validation, owner/lease checks, atomic child creation + seeding + linkage, unchanged source fences, immutable consumed parents, strict boundary mismatch refusal, and normal fresh/recover compatibility. No new migration or gate/trust edits. Existing tests account for the new advisory cutoff as execution metadata when comparing equivalent artifacts.
- This is an independently reviewable core slice. #185 runtime budget integration and final full-suite/lifecycle verification remain required before landing or closing #186; no external review loop was run under the owner's expedited exception.


## Schema-19 budget integration

Rebased onto 99a3f94 (#185), retaining all runtime-limit arguments, budget controller
context, persisted timing/termination hooks, MCP fields and lifecycle registrations.
The combined strict run passed 136 tests (continuation, checkpoints, budget execution,
budget store, budget model, inventory) in 36.33 seconds. Shared request/service/result/
scoped-control and MCP-schema integration passed 202 tests in 42.02 seconds.

A new shipped scenario expires the review budget after three usable checkpoints,
then continues the same logical request with a new execution sequence and larger
runtime allowance. Only batch 4 and integration execute; the new measured review
wall time is 200ms. Existing request-budget snapshots remain per execution.

One independent read-only dependency-seam pass found a receipt validation gap.
Four negative receipt regressions failed before tightening; kind-specific indices,
closed pass fields, uniqueness, action totals and truncation consistency now pass.
That was the one useful independent review pass; no external provider review was
requested. Source compatibility, ownership/fence checks and integration dependencies
had no demonstrated defect in that pass. Final diff check/self-review passed.
