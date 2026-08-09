# Exact-Diff Trusted Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in CLI and MCP reuse of a trustworthy review only when the current repository, diff, context, checklist, tree, and caller intent match exactly.

**Architecture:** Capture the current foreground identity before provider capacity acquisition. A dedicated reuse module probes trustworthy stored artifacts using a strict predicate and returns a read-only projection whose findings are recalculated from current triage. Every opt-in probe is appended to a separate audit ledger; any miss, error, ambiguity, explicit fresh request, or tree movement falls through to the existing review path.

**Tech Stack:** Python 3.12 standard library, SQLite additive migration, pytest, existing `gitio`, `checklist`, `contextpack`, `triage`, `services`, CLI, and MCP layers.

---

## Task 1: Add the persisted reuse audit and tree fingerprint primitives

**Files:**
- Modify: `src/skodun/store.py`
- Modify: `src/skodun/gitio.py`
- Test: `tests/test_store.py`
- Test: `tests/test_gitio.py`

- [x] **Step 1: Write failing tests** for schema v10, append-only reuse event rows, and a tree fingerprint that changes for dirty edits while HEAD remains unchanged.
- [x] **Step 2: Run the focused tests and confirm they fail because the migration, store method, and fingerprint function do not exist.**
- [x] **Step 3: Implement the additive v10 migration, validated append-only `reuse_events` methods, and a SHA-256 fingerprint over HEAD, NUL-delimited porcelain status, and changed-file content.
- [x] **Step 4: Run the focused store and git tests and confirm they pass.**

## Task 2: Define the exact reuse probe and read-only result projection

**Files:**
- Create: `src/skodun/reuse.py`
- Test: `tests/test_reuse.py`

- [x] **Step 1: Write failing tests** covering full predicate hits, each identity mismatch, missing/corrupt/untrustworthy records, current triage changes, and tree movement after the probe.
- [x] **Step 2: Run the focused tests and confirm they fail at the new probe boundary.**
- [x] **Step 3: Implement current identity capture, checklist/context identity calculation using the existing dispatcher context rules, strict candidate validation, the tree recheck, and the banner/status projection without saving or mutating the candidate artifact.
- [x] **Step 4: Run the focused reuse tests and confirm they pass.**

## Task 3: Wire opt-in reuse through the shared service and both transports

**Files:**
- Modify: `src/skodun/pipeline.py`
- Modify: `src/skodun/services.py`
- Modify: `src/skodun/cli.py`
- Modify: `src/skodun/mcpserver.py`
- Modify: `README.md`
- Test: `tests/test_services.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_mcpserver.py`
- Test: `tests/test_mcptools.py`

- [x] **Step 1: Write failing service, CLI parser, MCP handler, and schema tests** for `--reuse-trusted`, `--fresh`, `reuse_trusted`, and `fresh`, including no provider invocation/capacity acquisition on a hit and current-triage status on a hit.
- [x] **Step 2: Run the focused tests and confirm they fail because the flags and service seam are absent.**
- [x] **Step 3: Add `tree_fingerprint` and deterministic batched checklist/context identities to foreground artifacts, add the opt-in service probe before `_svc_review_once`, append hit/miss/bypass/error audit rows, and preserve the existing fresh path and wording. Return the reused review ID in CLI/MCP text and structured metadata.
- [x] **Step 4: Run the focused service, pipeline, CLI, and MCP tests and confirm they pass.**

## Task 4: Add telemetry coverage and perform delivery verification

**Files:**
- Modify: `src/skodun/stats.py`
- Modify: `src/skodun/store.py`
- Modify: `tests/test_stats.py`
- Modify: `tests/test_store.py`

- [x] **Step 1: Write failing tests** for reuse hit/miss counts and the invariant that reused artifacts do not increment provider-served counts.
- [x] **Step 2: Implement the read-only stats projection and run the targeted stats tests.**
- [x] **Step 3: Run the complete pytest suite, the store ResourceWarning sweep, verify `gate.py` and `trust.py` are byte-identical, self-review the diff, commit, push, open the PR, and monitor checks/review threads until clean.
- [ ] **Step 4: Merge the PR, comment and close issue #117, and record that issue #115 remains open pending the separate TubeScribes canary/owner decision.**
