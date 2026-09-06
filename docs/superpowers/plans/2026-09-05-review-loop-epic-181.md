# Review-loop epic 181 implementation plan

**Goal:** Deliver all thirteen implementation issues in epic #181 through merged PRs, with request ownership, bounded execution, compatible continuation, independent triage, measurable costs, and validated concurrency.

**Architecture:** Extend the shared services and existing SQLite admission/checkpoint records. Request identity is separate from review coverage; gate.py and trust.py remain protected. Adapter eligibility, lineage, and refuter policy can be developed independently in isolated worktrees. Merge those branches sequentially, rebase dependents, and retain exact-identity checks at execution and finalization.

**Delivery policy:** The owner explicitly authorizes self-review, at most one useful external review pass per slice, skipping redundant/unavailable reviews and waiting gates when justified, and expedited merges. Record actual validation and omitted checks in PRs; never call an incomplete check green. Do not force-push main. Use normal merges when available and admin merge only when justified by observed blocking requirements. Behavior still needs hermetic regression tests and merged-main smoke proof. Full-suite/lifecycle validation should run once per coherent integration boundary, rather than rerun overlapping long suites in every independent worker.

## Independent slices

- [ ] #194: chain.py/adapters — run authoritative transport checks before provider admission; preserve fatal configuration/encoding errors, exact bytes, prompt-specific eligibility, and capable fallback. Regression: oversized prompt requests zero slots and zero providers; boundary/Unicode/fatal settings remain correct.
- [ ] #191: fingerprint.py/store.py/pipeline lineage — exact indexed retrieval before bounded fallback, relevant prompt context, separate truncation telemetry; no inherited automatic dismissal. Regression: older match beyond 200 unrelated records, bounded query work and safe paths.
- [ ] #189: pipeline.py/triage.py/services.py — contributor-aware independent refuter selection and refusal before adoption write, preserving optional annotation semantics and explicit audited manual triage. Regression: mixed providers/fallbacks and missing legacy provenance cannot evade policy.

## Request and execution slices

- [ ] #182: add additive request-store lifecycle and shared request context before readiness/admission; link tickets/attempts/reviews, snapshot requested identity, revalidate under admission, return request ID even for no-review results, and fence duplicate active requests. Add tests/test_requests.py and shared service integration tests.
- [ ] #184: versioned result schema and CLI JSON/MCP parity for all outcomes, reason codes, explicit IDs, unknown identity/usage, no copied prior-attempt fields.
- [ ] #183: worktree-default status, explicit broader list views, guarded explicit-ID triage/cancel, queued cancellation, durable actor/cause/outcome events and finalization diagnosis.
- [ ] #185: distinct queue/provider/execution/total budgets with uniform propagation and bounded request leases; polling does not cancel live requests; true expiry terminates tickets fairly.
- [ ] #186: explicit compatible continuation versus fresh second opinion, classify usable and failed checkpoints, consumed-aggregate continuation, no repeat impossible input, fenced retry of failed/dependent work.
- [ ] #187: extend exact checkpoint dependencies to required extra passes; optional refuter stays optional.
- [ ] #188: shared queue/request read model and per-request timing/call/usage accounting, exact denominators and namespace joins, unknown dollars/tokens, historical-wait wording.
- [ ] #190: planner preview uses shipped planner, head/base provenance, per-call/aggregate bytes, transport topology and measured operating targets frozen into identity.
- [ ] #192: reconcile PR #180/legacy participants; prove one-versus-two concurrency on deterministic workload then a bounded existing-provider sample. Publish actual outcomes and rollback; do not infer speedups or change defaults without evidence.
- [ ] #193: opt-in bounded independent batch concurrency with safe per-worker store handles, fenced claims, dependency barriers, fairness, deterministic aggregation and measured comparison.

## Verification and completion

- [ ] Each slice records regression-before-fix evidence, focused pass counts, self-review of every acceptance criterion, and any known baseline failures.
- [ ] Review and merge live PR heads in dependency order, inspect actionable feedback once, and document any skipped wait/review under the owner's exception.
- [ ] Run integrated full suite and separate store/process ResourceWarning sweep; isolate demonstrated failures without calling interrupted/partial runs green.
- [ ] Fetch merged main, run focused shipped-path smoke checks, close each fulfilled child with its PR/evidence link, and update the epic checklist.
- [ ] Audit all thirteen issues and measurement deliverables against current main before closing the epic or marking the thread goal complete.

Self-review: the full epic remains in scope; independent work is isolated; no product trust weakening is used to accelerate delivery. PR #180 is coordination input, not assumed landed. The existing adapter-conformance baseline failure must be classified/fixed where necessary for meaningful integrated validation.
