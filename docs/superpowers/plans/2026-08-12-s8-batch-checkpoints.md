# S8.1 Batch Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist exact-identity batched sub-reviews outside coverage-bearing review rows, resume completed passes safely, and atomically finalize the existing aggregate artifact.

**Architecture:** A focused `checkpoints.py` module owns canonical identity and strict payload validation; additive v13 store tables own transactional claim/fencing and final consumption. The existing pipeline prepares deterministic pass inputs and reuses or runs each pass through that controller, while the existing `fresh` service/CLI/MCP flag bypasses resume. `gate.py` and `trust.py` remain unchanged.

**Tech Stack:** Python 3.12 stdlib, SQLite, pytest, existing CLI and stdio MCP services.

---

### Task 1: Define canonical identities and bounded checkpoint payloads

**Files:**
- Create: `src/skodun/checkpoints.py`
- Create: `tests/test_checkpoints.py`

- [x] **Step 1: Write failing pure-unit tests**

Add tests that build a four-batch plan and assert identical inputs produce the same orchestration/pass identities; changing repo, worktree, branch, head, base ref/SHA, full diff, tree fingerprint, context, checklist, reviewer graph, config, policy, planner, batch budget, ordered boundaries, or one prompt changes the first named mismatch. Add payload tests that accept a normalized `_Sub` shape and refuse unknown keys, non-bool axes, oversized text/JSON, malformed findings/attempts, and prompt/transcript/environment/PATH fields.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_checkpoints.py -q --tb=short`

Expected: collection fails because `skodun.checkpoints` does not exist.

- [x] **Step 3: Implement the frozen identity/payload types**

Implement `OrchestrationIdentity`, `PassIdentity`, `CheckpointPayload`, canonical JSON/SHA-256 helpers, fixed-order `first_mismatch`, `PLANNER_VERSION = "skodun-batch-v1"`, strict text/list/JSON bounds, and `payload_from_sub` / `sub_fields_from_payload`. Persist only sanitized existing result fields.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_checkpoints.py -q --tb=short`

Expected: all checkpoint identity/payload tests pass.

### Task 2: Add v13 orchestration/checkpoint storage and fencing

**Files:**
- Modify: `src/skodun/store.py`
- Modify: `tests/test_store.py`
- Test: `tests/test_checkpoints.py`

- [x] **Step 1: Write failing migration and transaction tests**

Add tests for fresh v13, v12 upgrade, frozen `_SCHEMA`, orchestration insert/find/mismatch recording, ordered checkpoint rows, one winning live claim, in-flight contender, expired claim reclaim with incremented fence, late-owner completion refusal, immutable complete payloads, release-to-pending, expiry/pruning, and no checkpoint visibility through `list_reviews`, reuse candidates, finding keys, triage, or gate inputs.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_store.py tests/test_checkpoints.py -q --tb=short -k 'orchestration or checkpoint or schema_version'`

Expected: failures for missing v13 tables/APIs and `SCHEMA_VERSION == 12`.

- [x] **Step 3: Implement additive schema and store APIs**

Bump `SCHEMA_VERSION` to 13 through `_MIGRATIONS` only. Add `create_orchestration`, `find_resume_candidate`, `record_orchestration_mismatch`, `claim_checkpoint`, `complete_checkpoint`, `release_checkpoint`, `list_checkpoints`, and bounded expiry/prune APIs. Keep consumption private to the atomic review-finalization transaction so no caller can consume checkpoints without publishing their review. Use `BEGIN IMMEDIATE` for every multi-statement non-idempotent state transition and conditional token/fence predicates for completion.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_store.py tests/test_checkpoints.py -q --tb=short -k 'orchestration or checkpoint or schema_version'`

Expected: all new store/checkpoint tests pass.

### Task 3: Prepare deterministic batch pass inputs before execution

**Files:**
- Modify: `src/skodun/pipeline.py`
- Modify: `src/skodun/reuse.py`
- Modify: `tests/test_batched_review.py`
- Test: `tests/test_reuse.py`

- [x] **Step 1: Write failing planner/identity tests**

Assert the pipeline can prepare every batch prompt, checklist/context identity, boundary digest, and reviewer/config/policy identity without invoking `_run_chain`; the prompt bytes match the existing `_orchestrate` path exactly. Assert foreground working-tree and pre-push OID context produce stable but distinct identities when their packed bytes differ.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_batched_review.py tests/test_reuse.py -q --tb=short -k 'checkpoint or orchestration_identity or prepared_batch'`

Expected: failures for missing preparation/identity seams.

- [x] **Step 3: Extract preparation without changing aggregation**

Introduce focused frozen prepared-pass structures and helpers that reuse `batch_plan`, checklist selection, context packing, prompt building, `aggregate_context_identity`, `aggregate_checklist_identity`, and `security_policy_identity`. Keep the existing batch bytes and integration scheduling rules unchanged; do not edit gate/trust.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_batched_review.py tests/test_reuse.py -q --tb=short -k 'checkpoint or orchestration_identity or prepared_batch'`

Expected: all preparation and identity tests pass.

### Task 4: Resume batches and integration with transactional claims

**Files:**
- Modify: `src/skodun/pipeline.py`
- Modify: `src/skodun/checkpoints.py`
- Modify: `tests/test_batched_review.py`
- Modify: `tests/test_cancellation.py`

- [x] **Step 1: Write failing shipped-path resume tests**

Create a deterministic four-batch fixture whose cancellation fires before batch 4. Assert batches 1-3 are complete checkpoints, no checkpoint is a review, the resumed run invokes only batch 4 plus integration, and the final aggregate matches a fresh uninterrupted aggregate except permitted orchestration metadata. Add exact-mismatch refusal, live racing resumer, expired-lease reclaim, and cancellation-during-integration tests.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_batched_review.py tests/test_cancellation.py -q --tb=short -k 'resume or checkpoint or racing_resumer'`

Expected: repeated provider calls or missing checkpoint APIs demonstrate the absent feature.

- [x] **Step 3: Integrate claim/reuse/complete into `_orchestrate`**

For each prepared pass, reuse a strictly validated complete payload or claim and run it once. On live in-flight observation, fail closed without a provider call. Release the caller's claim on cancellation/exception, retain completed payloads, and build the aggregate through the existing field/ordering logic. Revalidate full identity before final persistence.

- [x] **Step 4: Make final review persistence and orchestration consumption atomic**

Add a store transaction that verifies all planned checkpoints complete, conditionally finalizes the existing foreground/pre-push running row through the same normalization/identity rules, and marks the orchestration consumed. A missing/changed checkpoint or moved identity rolls back the whole transaction.

- [x] **Step 5: Run the focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_batched_review.py tests/test_cancellation.py tests/test_checkpoints.py -q --tb=short -k 'resume or checkpoint or racing_resumer'`

Expected: all resume, fencing, cancellation, and aggregate-equivalence tests pass.

### Task 5: Wire fresh bypass and shared CLI/MCP behavior

**Files:**
- Modify: `src/skodun/services.py`
- Modify: `src/skodun/pipeline.py`
- Modify: `src/skodun/cli.py`
- Modify: `src/skodun/mcpserver.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_mcpserver.py`
- Modify: `README.md`

- [x] **Step 1: Write failing parity tests**

Assert `fresh=True` reaches the pipeline as `resume=False` through `services.py`, CLI `--fresh`, and MCP `fresh`; default behavior resumes an exact incomplete orchestration. Assert human and MCP progress name reused batches and first mismatch consistently, without exposing checkpoint payloads or prompts.

- [x] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest tests/test_services.py tests/test_cli.py tests/test_mcpserver.py -q --tb=short -k 'fresh or resume or checkpoint'`

Expected: the existing flag affects trusted-review reuse only and does not yet control checkpoint resume.

- [x] **Step 3: Thread the shared resume intent**

Extend the existing `svc_review_detailed` path so `fresh` disables both trusted artifact reuse and checkpoint resume. Keep CLI/MCP argument schemas unchanged, preserve one MCP review in flight, and document exact resume, mismatch, in-flight, expiry, and fresh behavior.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_services.py tests/test_cli.py tests/test_mcpserver.py -q --tb=short -k 'fresh or resume or checkpoint'`

Expected: CLI/MCP/service parity tests pass.

### Task 6: Verify safety, migrations, and shipped paths

**Files:**
- Modify: `docs/superpowers/plans/2026-08-12-s8-batch-checkpoints.md`

- [ ] **Step 1: Self-review exact acceptance coverage**

Inspect the diff and map each #150 acceptance criterion to a test. Run `git diff --check`; compare `git hash-object src/skodun/gate.py src/skodun/trust.py` to `origin/main`; search checkpoint tables out of gate, trust, triage, delivery, and reuse code.

- [ ] **Step 2: Run focused shipped-path verification**

Run: `python3 -m pytest tests/test_checkpoints.py tests/test_batched_review.py tests/test_cancellation.py tests/test_store.py tests/test_services.py tests/test_cli.py tests/test_mcpserver.py -q --tb=short`

Expected: exit 0 with no failures.

- [ ] **Step 3: Run the full suite and prescribed ResourceWarning sweep**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`

Run: `python3 -m pytest tests/test_store.py --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error`

Expected: both complete with exit 0; retain exact passed/skipped/deselected counts.

- [ ] **Step 4: Exercise real CLI/MCP help and a hermetic resume smoke**

Run `PYTHONPATH=src python3 -m skodun review --help`, inspect MCP tool schema via its registry test, and run the four-batch fake-provider timeout/resume smoke through the shipped service entrypoint.

- [ ] **Step 5: Commit the narrow issue implementation**

Stage only #150 files and commit with a complete-sentence message explaining the fail-closed checkpoint boundary and `refs #150`.
