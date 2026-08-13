# PR #169 Lineage Review Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every batched and detached pre-push reviewer receives the bounded stack/lineage context that identifies its prompt, and persist/expose the resulting lineage-context telemetry.

**Architecture:** Keep stack and lineage context advisory and bounded. `passes.integration_prompt` will render the same byte inputs used by batch prompts; `pipeline._orchestrate` will receive and thread them through preparation and integration; pre-push will load the context once from the reserved review identity. Artifact telemetry remains additive JSON fields, surfaced through the existing shared status/read-model paths without touching `gate.py` or `trust.py`.

**Tech Stack:** Python 3.12 stdlib runtime, pytest, SQLite-backed artifact JSON, shipped pipeline/CLI/MCP service surfaces.

---

### Task 1: Prove integration and pre-push context propagation

**Files:**
- Modify: `tests/test_integration_pass.py`
- Modify: `tests/test_batched_review.py`
- Modify: `tests/test_dispatch.py`

- [ ] **Step 1: Write the failing integration-prompt test**

Add a test beside the existing integration prompt tests that passes bounded `stack_context` and `lineage_context` bytes and asserts both appear in the rendered prompt and are reported by the returned prompt metadata.

- [ ] **Step 2: Run the focused test and verify it fails for the missing API**

Run: `python3 -m pytest tests/test_integration_pass.py -q --tb=short -k context`

Expected: FAIL because `integration_prompt` does not accept the context arguments.

- [ ] **Step 3: Add a batched orchestration regression test**

Exercise `_orchestrate` with two batches and explicit stack/lineage contexts, capture the integration provider prompt, and assert both context blocks are present and the integration pass identity differs from a no-context plan.

- [ ] **Step 4: Add a detached pre-push regression test**

Stub the existing `_lineage_prompt_context` seam, run the shipped pre-push pipeline helper, and assert the provider prompt contains the lineage block while the returned record carries its byte count and truncation flag.

- [ ] **Step 5: Run the new tests to establish the remaining red cases**

Run: `python3 -m pytest tests/test_integration_pass.py tests/test_batched_review.py tests/test_dispatch.py -q --tb=short -k 'context or prepush'`

Expected: the new propagation assertions fail while unrelated existing tests remain green.

### Task 2: Thread context through prompts and checkpoint identity

**Files:**
- Modify: `src/skodun/passes.py:980-1059`
- Modify: `src/skodun/pipeline.py:2297-2616, 3260-3500, 3630-3690`

- [ ] **Step 1: Extend `passes.integration_prompt` minimally**

Accept optional stack/lineage bytes and truncation flags, append the bounded blocks before batch summaries, preserve UTF-8-safe whole-prompt truncation, and return the two context telemetry values in `promptbuild.Prompt`.

- [ ] **Step 2: Thread the contexts into pre-push**

Load `_lineage_prompt_context` after the reserved repository identity is known. Store its byte count/truncation in `common`, pass it to `_prepare_batch_plan`, `_orchestrate`, and `_single_shot`, and keep the detached path’s object-database context source unchanged.

- [ ] **Step 3: Thread the contexts through orchestration**

Add optional context parameters to `_orchestrate`; pass them when it creates a prepared plan and when it builds the integration prompt. Keep existing callers source-compatible with empty defaults.

- [ ] **Step 4: Confirm identity coverage**

Ensure batch prompt hashes include the context-rendered bytes and integration prompt hashes include the integration context. Preserve the existing exact-diff, reviewer, checklist, and checkpoint identity fields.

- [ ] **Step 5: Run the focused red tests until green**

Run: `python3 -m pytest tests/test_integration_pass.py tests/test_batched_review.py tests/test_dispatch.py -q --tb=short -k 'context or prepush'`

Expected: PASS.

### Task 3: Expose telemetry and self-review the frozen diff

**Files:**
- Modify: `src/skodun/services.py:1441-1550`
- Modify: `tests/test_services.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Add read-model/status assertions first**

Assert `lineage_context_bytes` and `lineage_context_truncated` appear in the shared status line and JSON projection when present.

- [ ] **Step 2: Add the artifact schema assertion**

Assert a normal foreground record and a pre-push record retain the lineage telemetry fields after persistence/finalization.

- [ ] **Step 3: Implement the shared projection update**

Add both fields next to the existing stack-context fields in status, surface, and JSON read-model projections, using the existing omission behavior for absent legacy fields.

- [ ] **Step 4: Run focused verification and inspect the diff**

Run:

```bash
python3 -m pytest tests/test_integration_pass.py tests/test_batched_review.py tests/test_dispatch.py tests/test_pipeline.py tests/test_services.py -q --tb=short
git diff --check
```

Expected: PASS with no whitespace errors; `gate.py` and `trust.py` remain byte-identical to `origin/main`.

### Task 4: Deliver PR #169

**Files:**
- Modify: only the files listed above

- [ ] **Step 1: Run the full required suite**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`

- [ ] **Step 2: Commit and push the focused fix**

Use a complete-sentence commit explaining that PR #169 now carries lineage context into integration and pre-push prompts.

- [ ] **Step 3: Re-query exact-head GitHub state**

Inspect PR #169 checks and GraphQL `reviewThreads`; reply to each actionable thread with the exact fix/test evidence, and resolve only after the current head contains the fix.

- [ ] **Step 4: Merge and verify post-merge**

Merge only with a current head, green required checks, and zero unresolved threads; refresh `origin/main`, run the focused S6 smoke tests there, and close #166/#141 only if their live acceptance criteria are fully satisfied.

## Self-review checklist

- [ ] No provider receives an unbounded or nondeterministic context block.
- [ ] Integration and pre-push paths use the same lineage identity source as foreground review.
- [ ] Prompt/checkpoint identity changes when context bytes or truncation state changes.
- [ ] Legacy records without the new telemetry remain readable.
- [ ] Full-diff certification, trust axes, gate behavior, and `finding_key` are untouched.
- [ ] No `gate.py` or `trust.py` edits are introduced.
