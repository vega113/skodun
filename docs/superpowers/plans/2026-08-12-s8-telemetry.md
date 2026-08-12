# S8.3 deterministic slicing and batch telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers choose a smaller deterministic batch target and inspect bounded per-batch execution telemetry without changing schema or trust semantics.

**Architecture:** Add one validated defaults field and one per-call service override. Keep planning pure in `pipeline.batch_plan`, with the override clamped against the reviewer budget. Add a focused telemetry projection in `pipeline` from existing planner/attempt data, and expose it through the existing artifact/status paths without persisting sensitive process output.

**Tech Stack:** Python 3.12 stdlib, frozen dataclasses, SQLite JSON artifacts, pytest.

---

## Task 1: Lock planner/config behavior with tests

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_batched_review.py`

- [ ] **Step 1: Add config validation tests** for default zero, positive integer, boolean rejection, and negative rejection of `batch_target_bytes`.
- [ ] **Step 2: Add planner tests** proving a target below the provider envelope splits a diff, identical input/config yields identical bytes/files, and a target above the provider budget cannot widen the plan.
- [ ] **Step 3: Run the focused tests** and confirm they fail because the field and override do not exist.

## Task 2: Implement the bounded planner override

**Files:**
- Modify: `src/skodun/config.py`
- Modify: `src/skodun/pipeline.py`

- [ ] **Step 1:** Add `Defaults.batch_target_bytes: int = 0` and register its `>= 0` validator.
- [ ] **Step 2:** Add an effective batch-budget helper that returns `min(provider_budget, configured_target)` when the target is positive, otherwise the existing budget.
- [ ] **Step 3:** Make `batch_plan` use the smaller threshold and split budget while preserving the unchanged default path.
- [ ] **Step 4:** Run Task 1 tests and the existing batching tests.

## Task 3: Thread the validated override through CLI/MCP/services

**Files:**
- Modify: `src/skodun/services.py`
- Modify: `src/skodun/cli.py`
- Modify: `src/skodun/mcpserver.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_mcptools.py`

- [ ] **Step 1:** Validate the optional positive integer at the service boundary, preserving `None` as config-selected behavior.
- [ ] **Step 2:** Add matching CLI `--batch-target-bytes` and MCP `batch_target_bytes` schema/handler fields.
- [ ] **Step 3:** Pass the value through `services.py` to `pipeline.run_review`, where an override creates an effective frozen `Defaults` copy for this invocation and therefore participates in checkpoint identity.
- [ ] **Step 4:** Add parity tests asserting CLI and MCP reject the same invalid values and forward the same valid value.

## Task 4: Add bounded batch/attempt telemetry

**Files:**
- Modify: `src/skodun/pipeline.py`
- Modify: `src/skodun/chain.py`
- Create: `tests/test_telemetry.py`

- [ ] **Step 1:** Add a pure allowlisted telemetry helper that maps planner identity, byte counts, attempt timing/retry/category fields, and optional adapter usage; missing token values remain `None`.
- [ ] **Step 2:** Add sanitized executable identity fields only from adapter-resolved values; leave version/build unknown during review (doctor/providers owns explicit probes), and never include PATH, environment, prompt, stdout, or stderr.
- [ ] **Step 3:** Attach the helper output to each batch and integration metadata object, leaving the aggregate attempt list empty and adding no S7 receipt envelope.
- [ ] **Step 4:** Test deterministic telemetry, unknown token behavior, sanitized provenance, timeout/fallback fields, and opaque receipt extension absence.

## Task 5: Expose and verify the read model

**Files:**
- Modify: `src/skodun/readmodel.py`
- Modify: `src/skodun/services.py`
- Modify: `tests/test_readmodel.py`

- [ ] **Step 1:** Add optional telemetry summary fields to the existing coverage projection without changing gate eligibility.
- [ ] **Step 2:** Ensure text and JSON status expose batch count, prompt bytes, completed/failed batch counts, and planner digest when present.
- [ ] **Step 3:** Run focused tests, the complete suite, and the required store ResourceWarning sweep.

## Task 6: Freeze, review, land, and close

**Files:**
- Modify: PR metadata only.

- [ ] **Step 1:** Self-review the frozen diff for schema, trust, secret, and receipt-envelope regressions.
- [ ] **Step 2:** Commit, push, open the issue-narrow PR with Summary, safety decisions, test plan, compatibility notes, and `Refs #152`.
- [ ] **Step 3:** Wait for exact-head CI/review, address only actionable findings, merge, refresh `origin/main`, smoke-test the merged path, and close #152.
- [ ] **Step 4:** Verify #143 acceptance across #150/#151/#152 and close the epic with linked evidence.

## Self-review

- Deterministic slicing and changed target identity: Tasks 1–2 and 3.
- Per-batch bytes, timing, retry/fallback, timeout, tokens, and provenance:
  Task 4.
- CLI/MCP/config parity and JSON/status visibility: Tasks 3 and 5.
- Schema/trust/receipt safety and full verification: Tasks 4–6.
- Global wall/cancellation behavior remains owned by existing checkpoint and
  runner code; this plan only surfaces its completed attempt/checkpoint data.
