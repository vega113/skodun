# Review plan preview and measured target design

**Status:** Investigation/design only. No implementation authorized yet.
**Issue:** #190, https://github.com/vega113/skodun/issues/190.
**Inspected main:** `7ce8baee3e9791637455c74ea463e8d402792bed` (#203 core).
**Dependencies observed:** #194 closed; #188 open. Root requires #185/#188
integration acceptance before authorizing this implementation.

**Goal:** Explain the actual full-scope review call plan and support an explicit,
source-backed operational target below existing hard envelopes, with no provider
launches and no invented latency claims.

**Recommended narrow product contract:** `review-plan` / MCP `review_plan` is a
read-only preview. Default selection retains the existing configured target.
An opt-in measured selection can propose a target and render the resulting plan,
then returns the exact existing `--batch-target-bytes N` execution argument.
Execution consumes that explicit frozen value through its existing path. This
avoids introducing changing telemetry into routing/request acceptance or silently
replanning queued work. Root should approve this explicit-application contract
before implementation; automatic telemetry-based execution selection would need
an additional request/continuation design and is not assumed here.

## Live code findings and clean seam

1. `budget.prompt_budget` is the existing configured/reviewer/adapter envelope
   authority. `pipeline._batch_budget`, `_effective_batch_budget`, and
   `batch_plan` decide the diff-only target and deterministic split. With context
   packing, diff budget is half the prompt envelope. A byte ceiling is neither a
   token/context guarantee nor a latency objective.
2. `pipeline._prepare_batch_plan` already builds exact batch prompts, checklist
   selections, context packs, boundary digests and pass identities without
   launching a provider. Call this helper from preview; do not call `_run_review`,
   which also performs stale recovery, capacity admission and persistence.
3. The small-diff branch has its own builder path. Extract only its prompt-input
   preparation into a helper called by both execution and preview; preserve the
   existing invariant that small diffs never enter the batch orchestrator.
4. Security and skeptic prompt bodies depend on the known diff, so their sizes
   can be exact before execution even when their scheduling is conditional.
   Integration needs future batch summaries/findings. Refuter needs future
   finder findings and actual contributor families. Their exact bytes/eligibility
   are unknown until those inputs exist; do not substitute fabricated findings.
5. `passes.should_run_security`, `should_run_skeptic`, `refuter_decision`, and
   `should_run_integration` are the branch predicates. Skeptic and refuter are
   not simultaneously mandatory. Reuse the policy functions for scenario labels.
6. #194's `adapter.prompt_limit()` / `validate_prompt(prompt, reviewer)` and
   transport/capability metadata are the eligibility authority. A head-sized batch does not imply each fallback can carry it.
   Preview must examine the actual rendered prompt separately for each fallback.
   Do not call `build_cmd` just to probe eligibility: adapters may write support
   files there. An absent capability/validator is unknown, not a latency promise.
7. `dispatch.resolve_dispatch_base` uses the remote OID for an existing remote
   branch; new branches use `gitio.resolve_ref_base`. `gitio.resolve_base` is the
   foreground path. A stack manifest's certification base is validated against
   the selected base; it does not currently override execution's resolver.
8. Checkpoints already freeze `batch_budget`, boundary/pass hashes and effective
   defaults in their identity. Trusted reuse separately reconstructs context and
   checklist identities. A target change can leave boundaries unchanged, so a
   versioned planning-policy discriminator is needed for the new target contract.
   Keep it in checkpoint/reuse compatibility, not `security_policy_identity`
   (which also participates in gate behavior).

## Proposed surfaces and output

- CLI `review-plan --repo PATH [--reviewer NAME] [--batch-target-bytes N] --json`;
  MCP `review_plan` delegates to the same service and uses the same read model.
- Measured mode is explicit, for example `--target-source measured` with a declared
  per-call latency objective. Without a stated objective, show eligible measured
  cohorts/candidate targets but retain configured planning rather than inventing
  what 'optimal' means. Existing explicit bytes take precedence over suggestions.
  Require a stable head (explicit reviewer or routing off) for one selected
  measured target. Unpinned auto-routing gets per-head cohorts and an explicit
  route-conditional result; do not silently turn an observed route into a pin.
- Foreground mode captures working tree + HEAD using the shipped resolver.
- Prepush mode accepts the same four ref fields as `dispatch.Ref` and calls
  `resolve_dispatch_base` plus the shipped ref-diff capture; never substitute the
  currently checked-out HEAD or main for the pushed ref's real base.
- A historical review-ID inspection may explain stored mode/base/head/breadth,
  scoped to the supplied matching repository. Missing Git objects or task/stack
  intent are explicit unknowns; there is no automatic engine-defect conclusion.
- Return requested mode/base inputs, resolver source, resolved base ref/SHA/head,
  repository/worktree identity, manifest certification base and validation state,
  full changed-file/diff-byte counts, content hash and plan digest.
- Return each primary/batch prompt's exact bytes, diff bytes, covered paths and
  boundary/hash; preserve full aggregate counts when a display list is bounded.
  Distinguish exact known per-call totals/maxima from aggregate prompt sums.
- Return a call graph with required, conditional and result-dependent passes.
  Candidate evaluation/retry upper bounds are not expected provider launches.
  Expected conditional-call counts require measured branch frequencies; otherwise
  retain the conditions instead of inventing probabilities.
- Each provider path reports configured envelope, adapter transport capability,
  exact-input eligibility or `pending_result_input`, and the source/version of
  those facts. Deterministic input refusal remains separate from cached quota or
  binary availability. No availability/cost/latency guarantee follows from size.
- Include `snapshot_only` and explicit change/missing-data/truncation flags.
  Preview is not a durable request, queue admission, checkpoint claim or gate.

## Conditional integration coverage

For integration, report required participation of every planned batch, selected
integrator/fallback graph, known structural input floor and hard envelopes.
Exact final bytes stay unknown because summaries/findings do not yet exist.
If even the known structural floor cannot fit the integrator, do not recommend a
finer target as a complete solution. Preserve the existing complete integration
builder and its fail-closed truncation behavior at execution. Do not invent a
new hierarchical integration algorithm or drop batches/regions to improve timing.
Refuter eligibility additionally depends on the actual contributing families;
show that dependency rather than assuming the configured head was the only one.

## Measured-target evidence and selection

No real provider measurements were collected in this design turn. The supplied
6,914,679-byte / 18-batch / 459-file artifact is an aggregate example, with a
406,186-byte maximum batch prompt and prepush/branch-specific scope. It supplies
no universal optimum and does not justify treating 15,000 bytes as a default.

Use bounded historical rows through the #188 attempt/provenance machinery:

- Sample exact launched attempt IDs once. Keep actual provider/model/effort,
  mode, pass kind, planner/capability provenance, paired batch-diff/input bytes,
  context bytes, duration, timeout cap and terminal classification. Skip
  transport-ineligible candidates as launches; label old/missing identities.
- Group primary/batch work separately from integration/refuter. Preserve model,
  mode and context differences instead of combining unrelated lanes. Do not
  filter only trustworthy successes: retain timeout/unusable outcomes in the
  reliability denominator and label right-censored timeout durations separately.
- Proposed initial advisory qualification: 20 unique launched samples from at
  least 5 distinct requests in a named 30-day window. These are policy thresholds,
  not statistical guarantees. One 18-batch incident is still one request.
- Every cohort reports scanned/matched counts, request count, sample IDs/digest,
  window, missing/censored/failure counts, units and quantile method. Missing
  executable version remains an explicit provenance limitation, not a guessed
  version. Do not expose prompt bodies, transcripts or secrets.
- Candidate targets come from observed diff-byte ranges with paired actual input
  sizes and are clamped by the same shipped head envelope/context rules. Select
  the largest qualified candidate meeting the declared per-call latency goal
  and documented failure/censoring guardrail, then prepare/check the entire
  candidate plan (including integration feasibility). Defaults/explicit overrides
  win when evidence is insufficient, mismatched or unavailable.
- The exact confidence/failure guardrail and CLI wording are product parameters
  for root approval. Avoid claiming causality or a global speed optimum from
  observational cohorts. No automatic live probing or configuration writes.
- Show per-call historical ranges only for qualified matching cohorts. A sum of
  per-call p90 values is not a request p90. Whole-request ranges require enough
  comparable request-level interval-union observations from #188; otherwise the
  request range is unknown. Queue contention remains a separate unknown.

## Target and identity handling

Persist a versioned planning-policy projection with selected effective target,
source and planner/boundary identity on the review/checkpoint path. Continue
using the existing explicit batch-target override so request configuration and
keyed intent remain frozen before admission. Execution must not resample history
or change the target halfway through a request.

Add a separate planning-policy compatibility check for trusted reuse, including
when a new target happens to produce identical boundaries. Old unknown planning
identity or changed selected target receives an explicit refusal/mismatch reason.
Keep the gate's full-diff/trust semantics and security policy hash unchanged.
Checkpoint mismatches must identify target/policy changes explicitly before any
reuse; an intentional new plan invalidates affected checkpoint work and keeps
all diff bytes and required integration coverage.

## Implementation slices after authorization

1. **Shared preparation and preview core:** new `plan_preview.py`, small prompt
   preparation helper extraction in `pipeline.py`, shared `svc_review_plan`, CLI
   parser/handler and MCP schema/handler. Use existing splitting/builders and
   read-only Store snapshots. Missing/older history cannot cause migration; it
   yields configured planning plus an explicit history-unavailable reason.
2. **Calibration policy:** new `operational_targets.py` with pure cohort/selection
   functions over bounded scalar telemetry. Reuse the existing attempt-ID and
   namespace machinery, extending one shared safe projection if paired batch
   bytes are missing. No new persistence table/index unless query plans prove a
   specific need.
3. **Identity and execution parity:** additive planning projection, target-aware
   checkpoint/reuse compatibility and explicit mismatch reasons. Keep execution's
   captured base/diff/target authoritative; a preview invalidated by tree changes
   cannot silently substitute a different scope.
4. **Docs and hermetic acceptance:** explain the explicit preview-to-execution
   override contract, conditional branches, history limitations, base provenance
   and lack of speed guarantees. Register any new Store-using tests in the
   lifecycle inventory. Root owns integration/merge/issue closure.

## Acceptance fixtures and verification ladder

- Small and large real Git diffs: spy on shipped prepared prompts in execution
  and compare to preview bytes/boundaries for identical captured inputs/target.
  Assert no provider/watchdog, capacity enqueue, request creation or writes occur
  during preview; do not merely start after the code under test.
- Boundary, irreducible large-file, rename, Unicode and unsafe-path fixtures:
  concatenated split bytes equal the authoritative diff, hashes are deterministic,
  any actual prompt truncation is visible and cannot certify a complete plan.
- Explicit stack certification bases (matching and mismatching), existing/new
  prepush refs, broad branch histories and a changed working tree: execution and
  preview use the same resolver/full diff; broad scope is explained, not narrowed.
- Synthetic 18-batch fixture: separate aggregate from per-call maximum and expose
  one fallback's exact transport refusal before execution. Integration remains
  required, and future-result prompts remain labeled unknown.
- Conditional graph fixtures: clean finder + clean security enables skeptic;
  trusted dirty finder may enable independent refuter; failed/degraded or
  security-added findings do not turn both into mandatory calls.
- Same bytes with changed operational target, even identical boundaries: explicit
  checkpoint/reuse mismatch; gate/trust files remain byte-identical.
- Empty, sparse, stale, mixed-mode/model, duplicate-ID, timeout-censored and
  malformed telemetry: configured fallback with exact sample/coverage reasons.
- Controlled offline tradeoff fixture: synthetic labeled historical cohorts and
  the real splitter/builders compare several observed byte targets on one diff.
  Show smaller calls versus greater batch/integration overhead. This is a policy
  consistency demonstration, not measured provider performance or a promised
  speedup. Any real pilot requires separate authorization; none runs here.
- Run focused preview/planner/batching/budget/reuse/checkpoint/CLI/MCP tests first;
  then root's integrated full suite and separate lifecycle sweep before closure.

## Self-review and decisions for authorization

- All #190 acceptance areas map to a fixture above; the existing full-scope and
  trust invariants remain intact. No source code or configuration was changed.
- Approval decision: use the narrow explicit-application measured target contract
  or separately design automatic runtime target selection. The latter cannot be
  slipped into this patch: route selection, request config identity and compatible
  continuation would need one frozen calibration decision.
- Approval decision: choose the advisory sample/failure guardrail and declared
  latency-goal interface. Current evidence cannot validate a numerical optimum.
- Integration blocker remains #185/#188 acceptance, not #194. Re-read main and
  those closures before editing implementation files.
