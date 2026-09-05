# Required Follow-up Checkpoints Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan task by task after the dependency lands. Do not dispatch further agents unless the coordinator authorizes them.

**Goal:** Continue an exact batched foreground review at its first missing required follow-up, preserving usable upstream evidence and existing conditional trust semantics.

**Architecture:** Extend the existing orchestration with security/skeptic checkpoint rows, runtime dependency bindings, and immutable continuation candidates. Claims and atomic final publication validate those bindings through the existing Store authority; refuter remains optional annotation.

**Tech Stack:** Python 3.12+, stdlib SQLite/JSON/hashlib, hermetic pytest.

Status: implementation authorized by the coordinator after #186 / PR #208 landed. This worktree was rebased onto confirmed main `8e1ebff16aa46b539e5993e2c15c2408fbdf9880`. Schema 20 is reserved for this slice. The coordinator accepted all scope/compatibility/crash-guarantee decisions below.

Design checkout: `a2f06777a1f735395d2da783af3847cfd8cb9fa7`.
Initial design evidence on 2026-09-05: issue #187 was OPEN; PR #208 was OPEN at `51afcdaf47c5e50adb418bbffca1750803c7f751`. Before implementation, live GitHub confirmed #208 MERGED at the implementation base above and #187 still OPEN. Its continuation worktree was inspected read-only at `/Users/vega/.codex/worktrees/ep181-continuation/skodun`. That dependency has schema 19 and no additional migration. This plan does not freeze an unmerged implementation. An existing untracked draft in this assigned worktree was preserved and tightened against that evidence.

## Goal and scope

An explicit compatible continuation of a batched foreground review must retain usable batches/integration and retry only an unusable required security/skeptic pass and any affected dependents. A successful security pass must survive a later skeptic failure when its exact inputs remain unchanged.

This slice checkpoints the existing security and skeptic roles. It adds no generic mandatory-role configuration. Unbatched execution remains unchanged. Refuter execution remains the existing optional annotation path, including contributor-family filtering and guarded adoption; it is not checkpointed in this slice. Optional refuter reruns remain visible as actual calls and are not described as zero-cost continuation.

Protected boundaries: do not edit `src/skodun/gate.py` or `src/skodun/trust.py`; do not change routing, legacy lock ownership, provider process cleanup, configured providers, live configuration, or any live store. Temporary test stores and the additive migration definition are the only storage changes authorized by this design.

Keep #186's explicit `continue_compatible` / CLI `--continue` semantics. Plain recovery still requests fresh opinions; `--fresh` and deliberate second-opinion exclusions retain their existing meaning. Keep full configuration/policy equality. A changed configuration or pass policy therefore refuses the whole incompatible continuation with the existing first-mismatch reason; this plan does not promise selective reuse across changed global configuration. Within a compatible configuration, changed upstream evidence invalidates dependent follow-up candidates.

## Landed and proposed seams

- `checkpoints.PassIdentity` currently accepts batch/integration only; integration already permits a deferred `prompt_hash`.
- `pipeline._checkpointed_sub` owns fenced claim, usable sub-result transport, completion, and release-on-interruption. Required extra passes use the same review output contract as `_Sub` / `CheckpointPayload`.
- `pipeline._extra_pass` owns security/skeptic merge behavior. Preserve `passes.merge_extra_pass` / failed-pass demotion instead of inventing a parallel merge implementation.
- `passes.should_run_security` uses mode, path risk and its kill switch. `passes.should_run_skeptic` uses the post-security merged review's trust and zero-finding condition. `passes.refuter_decision` uses the pre-security finder snapshot.
- #186 adds `checkpoints.usable_payload`: validated `parse_ok=True`, `degraded=False`, `diff_truncated=False`, and empty `failure_reason`. A serialized returned failure is not reusable evidence.
- #186 `Store.fork_continuation(source_id, identity, *, request_id, owner_token, created_at, expires_at)` creates/deduplicates a child generation, validates request ownership/source link and exact content identity, and seeds usable source evidence. Source generations remain immutable; live source claims refuse continuation.
- #186 `OrchestrationIdentity.continuation_source` namespaces child generation ownership. It is included in the digest and omitted when absent to preserve old identity bytes; content comparison still checks all original content fields.
- #186 receipts retain original attempt IDs and expose bounded reused/executed/failed pass actions. Extend that existing receipt and its strict decoder for required follow-ups.

## Exact dependency graph

| Checkpoint | Scheduling input | Bound evidence | Publication requirement |
|---|---|---|---|
| Batch / integration | Existing #186 planner | Existing exact identities and integration dependencies | Unchanged |
| Security | Existing foreground/path-risk/kill-switch decision | Exact finder aggregate assembled from ordered batch/integration results; full diff; security prompt/slots; reviewer/fallback configuration and policy | Required only when scheduled |
| Skeptic | Existing condition on the post-security aggregate | Exact finder aggregate plus the security result or its explicit not-scheduled decision; merged trust axes and ordered findings; skeptic prompt; selected finder chain and policy | Required only when scheduled |
| Refuter | Existing pre-security finder snapshot decision | Existing finding numbering and complete actual contributor-provider set | Optional annotation; no checkpoint barrier |

Both security and skeptic currently render the full diff, branch/base/head labels, and role lead; neither renders the aggregate findings or generic checklist/context pack. Their dependency bindings deliberately include the aggregate and inherited checklist/context identities as required by #187, without changing the actual prompts. Security also consumes configured security prompt slots. Skeptic cannot inherit a stale decision made before a failed security pass is successfully retried. Refuter must not start consuming security/skeptic findings merely because those passes acquire checkpoints.

Define `followup-input/v1` as a deterministic semantic digest over:

1. Ordered upstream pass keys and their normalized output digests: parsed/degraded/truncated axes, stop/failure state, summary and ordered findings.
2. Actual accepted provider/model/effort and available sanitized adapter attribution for those outputs. Include every actual finder/integration contributor, not just the final aggregate's selected head. Explicit null model/effort or unavailable version is a real value; require known accepted provider attribution for reusable usable evidence, rather than making every optional provenance field mandatory. Unknown attribution refuses reuse; configured head identity cannot replace an unknown actual contributor.
3. The exact aggregate snapshot consumed for the decision, including trust axes and finding numbering.
4. The conditional decision, decision reason, pass role/version, mode, relevant path-risk result and effective kill-switch policy.
5. Full diff identity and exact rendered prompt digest/byte count/truncation flag, plus inherited context/checklist/stack/lineage identities and the unchanged reviewer/config/policy/planner identities.

Exclude orchestration/request IDs, wall-clock timestamps, queue durations, new execution-budget counters, and #186's `continuation_source` / `continuation_action` annotations from the semantic output digest. They are operational provenance, not changed input evidence. Original invocation IDs remain in telemetry and are not regenerated on reuse. A receipt transport annotation alone must not invalidate an otherwise unchanged security checkpoint.

Use separate stored dependency and rendered-prompt digests so refusal diagnostics can distinguish changed upstream evidence, scheduling, and prompt/context. Store hashes and bounded identity metadata, never prompt bodies or transcripts.

## Storage and identity changes

Use one existing `review_orchestrations` authority, one Store connection, existing request links, and the existing fenced checkpoint methods. Do not introduce another orchestration database or independent scheduler.

The current `review_checkpoints.pass_kind` CHECK allows only batch/integration. To keep the migration strictly additive, add a `review_followup_checkpoints` relation in the same database, referencing the same orchestration ID with cascading deletion. Do not rebuild or weaken the existing table or edit frozen schema definitions.

The new relation contains the existing claim/result fields and conventions:

- Key: `(orchestration_id, pass_kind, pass_index)`, with security/skeptic only and index 0.
- Existing checkpoint state vocabulary, claim owner/token, fence, claim/lease timestamps, payload, prompt/diff/boundary identities, completion timestamp and failure reason.
- Nullable bounded `decision_json`, `dependency_json`, and `binding_hash` for the runtime binding. `binding_hash` covers the dependency manifest, decision and rendered-prompt identity together.
- Separate nullable `candidate_json`, carrying bounded source identity, source binding, source payload and original completion timestamp. Keep candidate bytes out of `payload_json` until validated promotion; generic payload readers must not mistake candidate data for child evidence.

Allocate the next migration only after the coordinator confirms the post-#186 schema version. Preserve all existing rows/indexes; test fresh installation, explicit migration, rollback, and read-only older-schema refusal. No review status enum or trust axis changes.

Extend `PassIdentity` with security/skeptic kinds, index 0, and deferred prompt hashes. For batched foreground (`now`) orchestration, include both deterministic candidate rows, even when a kill switch or path policy will decline one. Their boundary hash binds the static follow-up policy, including exact switch values already covered by `reuse.security_policy_identity`. A runtime decision is required before final publication even when the decision is not to run the pass. Prepush plans retain batch/integration only.

Use a distinct follow-up planner version when these candidates are present. A legacy source without that follow-up plan refuses continuation with an explicit planner/plan mismatch; retain its evidence unchanged. Do not silently certify missing legacy required-pass provenance or silently switch an explicit continuation to a fresh review.

Route all four kinds through shared claim/completion/release internals using a fixed validated kind-to-table map. `list_checkpoints` returns a normalized union in batch, integration, security, skeptic order. This avoids a second implementation of fencing while preserving the additive table boundary. Extend `expire_orchestrations` to clear both payload and candidate bytes under the existing expiry policy; cascading retention/deletion must cover the new relation. Inspection uses the existing migration-free schema doors.

## Runtime binding and continuation

1. Build/claim/reuse batches and integration through #186 unchanged. Construct the finder snapshot with the existing aggregation code.
2. Compute the security decision and dependency/prompt identity. Transactionally bind the candidate under the existing orchestration/request ownership checks.
3. If scheduled, use the shared fenced invocation lifecycle and existing extra-pass merge. If not scheduled, store a bound non-required decision without a provider payload or invocation. Project it as not planned; do not manufacture a clean review payload.
4. Recompute the skeptic decision from the actual post-security merge, then bind/run/reuse it the same way.
5. Run the existing optional refuter path with its original finder snapshot and contributor policy.
6. Revalidate complete repository/content/plan identity and publish through the existing atomic finalization path.

Proposed shared Store binding seam:

`bind_followup_checkpoint(orchestration_id, pass_identity, *, request_id, owner_token, dependency_manifest, decision, now)`

The binding transaction validates the current request link/owner, allowed pass kind, declared row, exact diff/policy boundary, and referenced upstream checkpoint identities/digests. It never resets a live claim. A conflicting decision or dependency on an already-validated current-generation result refuses; it does not overwrite active work.

Return the validated `binding_hash` and require it on follow-up calls to `claim_checkpoint` and `complete_checkpoint` (optional keyword default `None` preserves existing batch/integration call sites; it is mandatory for follow-ups). Claim verifies that exact binding is scheduled; completion checks the binding as well as owner/token/fence. A stale binder therefore cannot claim or complete against another runtime decision. Finalization independently reconstructs the referenced upstream digests under its existing write transaction rather than trusting caller-supplied aggregate flags.

Extend #186 child seeding as follows:

- Seed batches/integration exactly as #186 does today.
- Copy a usable source security/skeptic payload only as a candidate in a pending child row, retaining its source dependency/prompt identities. Do not mark it complete before rebuilding the child aggregate and validating its runtime binding.
- On an exact binding match, atomically promote that candidate to reusable complete evidence without a provider call.
- On a candidate mismatch, clear only the child's candidate payload/bindings, retain the immutable source, record the stable mismatch, and invoke the newly bound pass under a fresh fence.
- Never carry a source's not-scheduled decision forward as a target decision. A retried security pass can make skeptic newly required or newly unnecessary.
- Returned unusable required-pass payloads remain diagnostic source evidence and are not seeded as usable child results.

A bound not-scheduled decision retains the provider row's pending state; it has no provider work to claim. `claim_checkpoint` refuses that row while its decision is not scheduled, the read model reports not planned, and publication treats the bound decision as satisfied rather than as evidence. No new stored state enum is needed. The pure decision schema has `scheduled: bool`, `required: bool` (equal to scheduled for these two roles), and a closed reason code; it never fabricates a provider output.

Prompt preparation failures need a terminal local-failure binding with the same upstream/scheduling identity, `prompt_hash=None`, and an explicit preparation-failure code. Persist an unusable diagnostic payload with empty attempts through the fenced follow-up path; this preserves the existing demoted final review and makes continuation retry preparation. Do not mark this case not scheduled or fabricate prompt bytes. Cancellation and persistence/claim failures propagate; provider/preparation failures alone use existing extra-pass demotion semantics.

Keep `_extra_pass` as the merge adapter. Its checkpointed branch runs `_checkpointed_sub`, then converts `_Sub` into the existing `passes.merge_extra_pass` / `merge_failed_extra_pass` contract. Its unbatched branch stays unchanged. Catch only ordinary provider/preparation errors: `CheckpointInFlight`, `CheckpointClaimLost`, `PersistenceFailed` and cancellation must escape to the existing interruption path, never become a mergeable provider failure. Preserve `extra pass security` / `extra pass skeptic` cause wording.

## Publication and observability barriers

Update the existing publication validator inside the same transaction that writes the final review and consumes the orchestration:

- All static candidate decisions must be bound and match the exact validated upstream generation.
- Every scheduled required follow-up must have a matching fenced terminal payload. A bound not-scheduled decision satisfies only scheduling, never provider evidence.
- A trustworthy final artifact additionally requires usable base and required follow-up payloads. Pending, stale, unknown, or unusable required evidence cannot be replaced with green aggregate flags.
- A terminal untrustworthy artifact may retain returned failures for diagnostics and #186 child continuation. Preserve the current interrupted/pending behavior; partial work never produces gate coverage.
- Refuter failure/absence does not enter this barrier and does not demote otherwise complete required coverage.

Validate follow-up requirements only for plans that declare them. An old completed review remains readable under its original plan and old trust contract; this migration does not retroactively withdraw or manufacture coverage. Explicit continuation across old/new plan versions refuses before admission.

Extend shared read models and #186's bounded continuation receipt with security/skeptic actions and stable reasons such as `followup_upstream_changed`, `followup_schedule_changed`, `followup_prompt_changed`, and `followup_candidate_unusable`. Record candidate invalidation reasons in the child row/read model; do not repurpose a successful continuation receipt's `first_mismatch`, which #186 requires to be null. Keep required pass state distinct from optional refuter annotation state. Counts must not double-count source attempts as new calls. Update the closed receipt decoder and kind-specific index rules together with rendering.

`readmodel.project_review` currently maps every non-batch checkpoint to integration and counts all complete rows. Replace this with explicit role mapping and only count scheduled required rows; unbound candidates are pending requirements, not completed evidence, and bound skips are excluded from counts/next pass. `review_results` reconstructs partial evidence from checkpoints and also needs explicit extra-pass reconstruction. Preserve ordinal `next_resumable_pass` compatibility (batch ordinals, integration then security then skeptic), using the existing role state map to identify the role.

Checkpointed extras retain their attempts in `extra_passes[name]` metadata so `review_results.observation` can distinguish actual new calls from original reused attempts. The current `_extra_pass` comment explicitly notes that `passes._merge` drops attempts; add a narrow metadata field there rather than assuming `_with_provenance` makes attempts visible. Retain original attempt IDs and annotate reuse, keeping counts and missing-attempt-scope reporting honest. Optional refuter behavior remains as today.

## Implementation slices and verification

- [x] **1. Confirm the landed base.** After #186 lands, fetch current `origin/main`, preserve this draft, and update the isolated branch from that base. Record the landed SHA and schema version here. Recheck #187 live state and the exact `fork_continuation`, receipt, budget and finalization APIs. Do not cherry-pick a stale dependency head.
- [x] **2. Add failing lifecycle and semantic identity tests.** Extend `tests/test_checkpoints.py` and `tests/test_schema_lifecycle.py`; create `tests/test_followup_checkpoints.py`. Cover index-0 validation, bounded/closed binding and candidate schemas, old-schema inspection without mutation, rollback, source immutability, candidate promotion and corruption rejection. Run those tests to establish the missing behavior before source edits.
- [x] **3. Add storage and pure identity support.** Modify `src/skodun/checkpoints.py` and `src/skodun/store.py`; create `src/skodun/followups.py` for semantic projection/binding construction. Add the next migration (20 only if 19 is still current), shared kind routing, binding-aware claims/completion, normalized reads and expiry. Do not add a Store opener in the helper. Re-run the focused schema/checkpoint tests and commit this coherent seam with `refs #187`.
- [x] **4. Add shipped continuation regressions.** Build `tests/test_followup_checkpoints.py` on `tests/test_continuation.py`'s temporary repo/Store and fake `runner.run_with_watchdog` setup. Explicitly enable security/skeptic in each relevant fixture because `_ready_repo` disables them. Add a risky path under `auth/`, configure prompt limits so the full extra-pass diff fits while four batches remain planned, and recognize prompt filenames with `security.`/`skeptic.` prefixes before the batch naming fallback. Assert fake runner launches, returned service results and persisted source/child rows, not a mock of the new binder.
- [x] **5. Connect required extra passes.** Modify `src/skodun/pipeline.py` at `_orchestration_identity`, the finder snapshot/extra-pass sequence, `_extra_pass`, `_checkpointed_sub` and finalization. Use the pure helper and existing merges; add follow-up attempts metadata narrowly in `src/skodun/passes.py`. Bind each decision before provider admission; persist terminal preparation failures and propagate cancellation/persistence failures. Exercise changed scheduling, usable-security/failed-skeptic and upstream-invalidation cases, then commit.
- [x] **6. Extend immutable continuation and atomic publication.** Extend `Store.fork_continuation` to seed follow-up candidates, never complete target evidence before runtime validation. Extend `_require_complete_orchestration` to check declared decisions, upstream digests and required payload usability for a trustworthy final artifact inside `save_checkpointed_review`'s transaction. Add source-plan corruption, stale bind/claim, two-continuers, late completion, cancellation and rollback tests, then commit.
- [x] **7. Connect shared reporting.** Modify `src/skodun/readmodel.py`, `src/skodun/continuation.py` and `src/skodun/review_results.py`; touch `src/skodun/services.py` only if required to pass existing shared metadata. Extend `tests/test_readmodel.py`, `tests/test_review_results.py`, `tests/test_continuation.py` and service/MCP parity coverage. Update `README.md`'s continuation section with required-pass retry and optional refuter semantics. Keep the existing CLI/MCP request controls unchanged.
- [ ] **8. Verify and hand off.** Run the focused commands below, then self-review the frozen diff and request one useful independent pass through the coordinator. The coordinator owns integrated full-suite/merge proof unless reassigned. No live provider pilot is part of #187. Commit, push, PR, review, merge and issue closure remain delivery requirements when implementation is authorized.

Suggested pure helper contracts (implementation may use frozen dataclasses with these exact responsibilities):

| Helper | Contract |
|---|---|
| `semantic_payload(payload: CheckpointPayload) -> dict` | Project an explicit allowlist of result axes/summary/ordered findings, accepted provider/model/effort and sanitized adapter identity. Exclude attempts, timings, generation IDs and reuse annotations. |
| `build_followup_binding(*, kind, upstream_rows, aggregate, decision, prompt_identity, orchestration_identity) -> dict` | Validate closed bounded input; project ordered semantic dependencies; return version, kind, dependencies, aggregate hash, decision, prompt identity, content identity digest and binding hash. |

These signatures describe design boundaries, not source implementation. The helper must use an explicit field allowlist, not recursively drop arbitrary keys named `id` or `timestamp` from findings: such keys may be substantive finding data. Use the source checkpoint's semantic finding list before run-specific aggregate annotations; bind the aggregate's stable ordered finding view separately. Missing expected usable provenance yields a stable reuse refusal, never configured-provider substitution.

Required hermetic acceptance cases:

- Four usable batches + usable integration + failed security: continuation invokes security only, then only whatever its new result actually schedules.
- Usable security + failed skeptic: continuation reuses base/security and invokes skeptic only.
- Failed security suppresses skeptic; successful retry with no findings makes skeptic required. A retry that adds findings keeps skeptic unscheduled.
- Changed batch/integration result or actual accepted provider invalidates security and affected skeptic candidates. Changed security evidence invalidates skeptic. Reuse-only telemetry annotations do not invalidate either.
- Changed global config/policy/prompt or missing legacy plan/provenance refuses or invalidates with the precise documented reason; no silent fresh run.
- Optional refuter unavailable, same-family-ineligible, or unparseable: otherwise complete required coverage stays trustworthy; adoption policy and original finding numbering remain intact.
- Cancellation while a required pass is running, after its result but before checkpoint completion, and during final publication: no concurrently owned duplicate calls or partial certification. A result durably completed before cancellation is reused; a result lost before completion is retried only after existing ownership/lease rules permit it.
- Two simultaneous continuers share the child generation; one claim owns each provider call. Late completion from a reclaimed claim is rejected.
- Stale candidate binding cannot be promoted or counted as complete; an interrupted binder cannot skip a required pass.
- Fresh and continued equivalent inputs produce the same trust/gate decision and findings, allowing only documented execution telemetry differences.
- CLI/MCP receipt parity, closed-schema validation, required/optional counts, explicit not-planned decisions, and bounded output.

Start with focused shipped-path tests, then run affected store lifecycle tests under ResourceWarning separately. The coordinator owns integrated full-suite/merge proof unless reassigned. No live provider pilot is part of #187.

Concrete verification commands after implementation:

```bash
python3 -m pytest tests/test_followup_checkpoints.py tests/test_continuation.py tests/test_checkpoints.py -q --tb=short
python3 -m pytest tests/test_readmodel.py tests/test_review_results.py tests/test_services.py tests/test_refuter.py tests/test_cancellation.py -q --tb=short
python3 -m pytest tests/test_schema_lifecycle.py tests/test_store.py --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error -q --tb=short
python3 -W error::ResourceWarning -m pytest tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error -q --tb=short
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short
git diff --check
```

Report actual counts and any incomplete sweep. The implementation includes executable behavior and is undergoing the focused checks and separate lifecycle sweep above. The coordinator owns the combined full-suite/independent-review/merge proof.

## Decisions for the coordinator

1. **Recommended scope:** persist required security/skeptic for existing batched foreground orchestration only; keep refuter optional and uncheckpointed. #187 names configured mandatory follow-ups, but no generic mandatory-role registry exists in this inspected pipeline. Adding one or adding unbatched orchestration would be a separate feature.
2. **Recommended compatibility:** retain full config/policy equality from #186; changed global policy refuses continuation with its first mismatch, while changed runtime evidence invalidates only dependent child candidates. Selective reuse across differing configuration would weaken the existing identity premise and is not included.
3. **Crash guarantee:** no concurrent duplicate provider calls under the existing lock/request/claim ownership rules; no reuse of missing durable output. Exactly-once historical model calls across a crash between provider completion and SQLite persistence are not implementable with the current CLI providers and no idempotent provider receipt API. Do not claim otherwise in acceptance evidence. Lost-output retry remains a new measured execution.
4. **Schema choice:** an additive relation under `review_orchestrations` preserves the frozen table CHECK and one orchestration authority. Rebuilding `review_checkpoints` would violate the current additive-only rule; storing required evidence outside the orchestration would create the second authority #187 forbids.

## Self-review and start condition

The plan covers all #187 acceptance criteria while preserving #186's ownership/generation model and strict configuration identity. It deliberately does not persist the incompatible refuter output contract, broaden unbatched orchestration, or introduce a new mandatory-role configuration. The additive relation preserves existing checkpoint data and uses the same fences and publication transaction.

Self-review against live #187 and inspected #208 specifically checked: conditional skip persistence, upstream/provider identity, no premature candidate promotion, required local-preparation failure, claims bound to runtime identity, terminal untrustworthy diagnostics, legacy plan compatibility, optional refuter isolation, expiry, honest attempts and CLI/MCP read-model mapping. This design has not received a new independent review; the coordinator can use its one useful independent pass at implementation readiness without repeating the #186 review.

Implementation start condition is satisfied. Implementation uses `followups.py` for bounded pure identity and `followup_store.py` for schema 20/runtime binding under the existing Store authority. `_required_followup` wraps the unchanged unbatched `_extra_pass` path and uses `_checkpointed_sub` for batched foreground work. Required metadata retains attempts through `_with_provenance`, so no `passes.py` merge rewrite was necessary.

Coordinator-approved refinement: security/skeptic capped prompts preserve the existing non-demoting `partial_coverage` policy. Their role-specific usability requires parseable/nondegraded/no-failure output plus exact capped prompt identity; batch/integration usability remains unchanged. Publication additionally checks the final artifact's semantic follow-up output hash against the stored payload. Direct pipeline recovery after a closed request remains compatible; active linked requests still require the owner token.

Checkpoint for continuation: 24 new follow-up tests passed, including both retry paths, changing upstream evidence, preparation failure, durable cancellation, exact binding/fence rejection, candidate isolation, output/prompt corruption, optional refuter, explicit v19-to-v20 migration and racing continuers. Earlier focused continuation/checkpoint/read-model run passed 168 tests; store/schema checks passed 312 with the separate heavy test deselected. The final expanded focused run passed 340 tests with 5 skips in 146.74s. Supplementary continuation/schema/read-model checks passed 160 tests. Coordinator-requested queue reuse counting and next-required-pass reporting were added after the core freeze: 76 strict ResourceWarning follow-up/queue/read-model tests passed, and the separate degraded-follow-up regression passed. The first heavy store ResourceWarning sweep finished with 2 failed, 2679 passed, 25 skipped, 1 deselected and 889 warnings in 594.14s. Both failures were remaining historical migration fixtures (request downgrade and v19 budget upgrade expectation); those fixtures were corrected and their strict focused tests passed 2/2. The corrected heavy sweep is being rerun. No full-suite success or landed status is claimed here.
