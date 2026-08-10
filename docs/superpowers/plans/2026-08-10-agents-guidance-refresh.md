# Agent Guidance and Process Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Skodun's maintainer and client-agent guidance up to date with the merged provider-routing/Codex-hardening work and the observed review/PR workflow.

**Architecture:** Documentation-only change. Keep the root file focused on repository-specific constraints and delivery gates, keep `examples/AGENTS.md` focused on the client review loop, and record historical memory corrections in an append-only note outside the repository.

**Tech Stack:** Markdown, GitHub CLI, pytest for shipped-path smoke verification.

---

## Task 1: Capture the current repository contract in root guidance

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update provider scope and load-bearing map**

Replace the outdated subscription-only overview with the current contract: subscription CLIs are the primary path, while `openai-api` is an optional metered BYOK adapter with a daily cap. Add `openai-api` to the adapter map and add `runner.py` to the process-lifecycle map.

- [ ] **Step 2: Add evidence-first workspace and GitHub rules**

Document that implementation starts from a fresh `origin/main` worktree, that the live issue/PR state must be checked before planning, and that merged/closed work is not patched in place. Require a concrete plan before edits for non-trivial work and distinguish observed evidence from historical memory.

- [ ] **Step 3: Add staged verification rules**

Document the fast focused-test gate, the separate full-suite/resource-warning sweep, and the rule that an interrupted or stalled command is not a pass. Require exact ran/passed/skipped/interrupted counts and the concrete blocker in the PR when a long sweep cannot finish.

- [ ] **Step 4: Add the observed review/merge loop**

Clarify that the current PR head is the review unit, unresolved review threads must be zero before merge, actionable findings are fixed in a narrow follow-up commit/PR, and rapid follow-up PRs should remain one-cause and independently testable. Add the post-merge fresh-main verification requirement.

- [ ] **Step 5: Add process-cleanup safety guidance**

Record the recent runner hardening contract: cleanup may signal only a proven owned process group; PID/PGID/session races or inconclusive `ps` evidence fail closed, and output must not be trusted after descendant cleanup. Do not weaken this to make a provider appear healthy.

## Task 2: Refresh the client-facing agent template

**Files:**
- Modify: `examples/AGENTS.md`

- [ ] **Step 1: Explain automatic-pool scope and quota diagnostics**

State that an explicit non-empty `[routing].pool` limits automatic head selection, while an omitted or empty pool considers all enabled finders; explain that fallbacks may still reach configured reviewers outside the automatic head pool. Also state that `skodun providers` reports provider and quota-pool state, and that AGY Gemini and Claude/GPT pools are independent when configured with their resolved pool identities.

- [ ] **Step 2: Add install/schema freshness recovery**

Make `doctor`, `providers`, and MCP restart the first response to schema-behind, missing-tool, or no-output symptoms. Explicitly warn against mixing a stale source checkout with a newer installed MCP process.

- [ ] **Step 3: Tighten the unavailable-provider decision path**

Require checking the actual provider state and bounded diagnostics before retrying, preserve fail-closed behavior for unknown failures, and keep the existing rule that only `gate` certifies the current frozen diff.

## Task 3: Self-review and verify the documentation change

**Files:**
- Test: `tests/` via existing shipped entry points; no production code changes expected.

- [ ] **Step 1: Inspect the diff for stale contradictions**

Run `rg -n -i 'not metered|openai-api|full suite|origin/main|review thread|quota pool|schema-behind' AGENTS.md examples/AGENTS.md` and reconcile every changed claim with `README.md`, `src/skodun/`, and the live merged PRs.

- [ ] **Step 2: Run focused shipped-path checks**

Run:

```bash
python3 -m pytest tests/test_cli.py tests/test_doctor.py tests/test_config.py tests/test_routing.py -q --tb=short
git diff --check
```

Expected: focused tests pass and `git diff --check` is clean. The PR will rely on hosted full-suite checks for the unchanged runtime.

- [ ] **Step 3: Review the plan and final diff**

Confirm that only `AGENTS.md`, `examples/AGENTS.md`, and this plan changed; `src/skodun/gate.py` and `src/skodun/trust.py` remain byte-identical; no issue index or historical ship log was added.

## Task 4: Deliver and merge

- [ ] **Step 1: Commit the focused documentation update**

```bash
git add AGENTS.md examples/AGENTS.md docs/superpowers/plans/2026-08-10-agents-guidance-refresh.md
git commit -m "docs: refresh agent delivery guidance"
```

- [ ] **Step 2: Push and open the PR**

Push `codex/agents-guidance-refresh` and open a PR against `main` with the evidence from Tasks 1–3 and no issue closure claim for already-closed work.

- [ ] **Step 3: Monitor checks and review threads**

Use the live PR head, inspect every actionable review comment, fix only in-scope findings, and verify zero unresolved review threads plus green required checks.

- [ ] **Step 4: Merge and verify main**

Merge once the PR is current and green, then fetch `origin/main`, inspect the merge commit, and run the focused documentation-adjacent smoke checks from the merged checkout.

## Self-review

- Coverage: provider scope, stale-memory/process evidence, current AGENTS.md practices, client routing diagnostics, staged verification, review-thread/merge workflow, and runner safety are each assigned above.
- Scope: no runtime behavior, gate/trust semantics, issue closure, or provider configuration is changed by this PR.
- Freshness: claims are grounded in `origin/main`, PRs #119–#137, open issue #121, README/integration docs, and the current AGENTS.md standard.
- Verification: the plan names exact commands and treats interrupted full-suite runs as incomplete rather than passing.
