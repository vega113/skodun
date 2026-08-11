# Review Readiness Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cheap, read-only readiness report that identifies known-impossible trustworthy review topologies before foreground capacity or provider invocation, then expose the same result through the CLI and MCP.

**Architecture:** `readiness.py` will own the topology/readiness read model and reuse the existing config, routing, adapter, Git, pass, and budget primitives without changing gate/trust or review execution. `services.py` will be the shared transport-neutral seam; CLI and MCP will validate only transport arguments and render the same structured result. Unknown live provider health remains eligible and is reported as unknown rather than as a false failure.

**Tech Stack:** Python 3.12 stdlib, SQLite store, argparse, stdio MCP, pytest.

---

### Task 1: Define the readiness read model and provider/topology checks

**Files:**
- Create: `src/skodun/readiness.py`
- Test: `tests/test_readiness.py`

- [x] **Step 1: Write failing tests for known-impossible and potentially-available reports**

Cover these shipped-path behaviors with real `Config`, `Reviewer`, `Defaults`, and temporary `Store` objects:

```python
def test_all_finder_chain_entries_in_active_quota_blackout_fail_fast(tmp_path):
    store, repo, cfg = make_store_repo_and_config(tmp_path, finder_chain=True)
    mark_all_chain_pools_unavailable(store, cfg)
    report = readiness.check(store, repo, cfg, requested=None)
    assert report.ready is False
    assert report.reason_code == "finder_chain_unavailable"
    assert report.estimated_attempts == 0

def test_missing_binary_is_reported_without_capacity_or_provider_call(tmp_path, monkeypatch):
    store, repo, cfg = make_store_repo_and_config(tmp_path, finder_chain=False)
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "missing-grok"))
    report = readiness.check(store, repo, cfg, requested=None)
    assert report.ready is False
    assert report.reason_code == "binary_unavailable"

def test_unknown_binary_health_is_eligible_and_reports_budget(tmp_path):
    store, repo, cfg = make_store_repo_and_config(tmp_path, finder_chain=False)
    report = readiness.check(store, repo, cfg, requested=None)
    assert report.ready is True
    assert report.state == "potentially_available"
    assert report.reason_code == "health_unknown"
    assert report.estimated_worst_runtime_sec > 0

def test_required_security_pass_without_eligible_reviewer_is_impossible(tmp_path):
    store, risky_repo, cfg_without_security = make_risky_repo_without_security_reviewer(tmp_path)
    report = readiness.check(store, risky_repo, cfg_without_security, requested=None)
    assert report.ready is False
    assert report.reason_code == "required_pass_unavailable"
```

- [x] **Step 2: Run the focused tests and verify they fail because the module/API is absent**

Run: `python3 -m pytest tests/test_readiness.py -q --tb=short`

Expected: collection or assertion failures naming the missing `skodun.readiness.check` behavior, not an environment failure.

- [x] **Step 3: Implement the immutable report and deterministic check**

Implement a frozen `ReadinessReport` with `ready`, `state`, stable `reason_code`, human `reason`, selected finder, per-entry topology details, prompt/batch facts, and `estimated_attempts`, `estimated_worst_runtime_sec`, and `estimated_lock_budget_sec`. The check must:

1. resolve the requested/config/auto head using the same routing rules;
2. walk the selected finder chain, auto pool when applicable, scheduled security/refuter/skeptic/integration reviewers, and each reachable fallback;
3. reject unknown adapters and missing binaries before any capacity call;
4. read active quota/provider blackout state using the same quota-pool key as `chain.py`, treating an unreadable store as unknown rather than impossible;
5. capture the current diff read-only, calculate the selected head’s prompt budget and batch count, and reject an empty or unbatchable plan;
6. mark required security/integration paths unavailable when the current diff schedules them and no eligible reviewer/adapter path can serve them; keep the optional annotation-only refuter’s absence explicit but non-clean only when its trust policy actually schedules it;
7. leave live provider health as `unknown`, never probe a model, never acquire review-fg/provider capacity, and never mutate the store;
8. calculate the worst-case attempt count and budget from the existing chain width, batch count, and budget helpers.

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m pytest tests/test_readiness.py -q --tb=short`

Expected: all readiness tests pass.

### Task 2: Add the shared services seam and CLI surface

**Files:**
- Modify: `src/skodun/services.py`
- Modify: `src/skodun/cli.py`
- Modify: `src/skodun/pipeline.py`
- Modify: `README.md`
- Test: `tests/test_services.py`
- Test: `tests/test_cli.py`

- [x] **Step 1: Write failing service/CLI parity tests**

Assert `svc_review_readiness(store, repo, reviewer=...)` returns `(0, text, metadata)` for a potentially available topology and `(2, text, metadata)` for a known-impossible one, with JSON metadata matching the rendered report. Add parser/dispatch tests for `skodun review-readiness --repo PATH [--reviewer NAME] [--json]` and verify the command never calls `services.svc_review` or acquires capacity.

- [x] **Step 2: Run the focused tests and verify the new surface fails**

Run: `python3 -m pytest tests/test_services.py tests/test_cli.py -q --tb=short -k 'readiness'`

Expected: failures for the missing service and parser/handler.

- [x] **Step 3: Implement the service, CLI command, and documentation**

Add `svc_review_readiness` to the services layer with the repository’s existing `(status, text, metadata)` contract. The CLI should render human output or stable JSON from the same report, return `0` only for `potentially_available`, and return `2` for known-impossible/config/read errors. Document that it is read-only, does not health-probe providers, and does not certify gate/trust.

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m pytest tests/test_services.py tests/test_cli.py -q --tb=short -k 'readiness'`

Expected: all readiness service/CLI tests pass.

### Task 3: Mirror readiness through MCP and preserve registry contracts

**Files:**
- Modify: `src/skodun/mcpserver.py`
- Test: `tests/test_mcpserver.py`
- Test: `tests/test_services.py`

- [x] **Step 1: Write failing MCP tests**

Add a non-long-running `review_readiness` handler test covering structured metadata, status `2` on known impossibility, exact argument validation, and registry ordering/schema exposure. Assert it calls `services.svc_review_readiness` and never `svc_review`.

- [x] **Step 2: Run the focused MCP tests and verify the new tool is absent/failing**

Run: `python3 -m pytest tests/test_mcpserver.py -q --tb=short -k 'readiness'`

Expected: failure because the registry does not yet expose the handler.

- [x] **Step 3: Implement the MCP handler and schema**

Add the same `repo`, optional `reviewer`, and optional `client_family` arguments as the CLI; MCP’s structured result makes `json` unnecessary. Return the service status/text/metadata via the existing `HandlerResult` and `_review_result` conventions. Keep the tool read-only and non-long-running.

- [x] **Step 4: Run focused parity tests**

Run: `python3 -m pytest tests/test_mcpserver.py tests/test_services.py -q --tb=short -k 'readiness'`

Expected: all readiness MCP/service tests pass, including registry snapshots updated only for this reviewed tool addition.

### Task 4: Integrate static refusal before review capacity

**Files:**
- Modify: `src/skodun/pipeline.py`
- Test: `tests/test_capacity.py`
- Test: `tests/test_pipeline.py`

- [x] **Step 1: Write the failing admission-order regression test**

Configure a linked worktree with one finder whose overridden binary path does not exist, replace `capacity.acquire_for_fg` with a function that raises if called, and assert `run_review` raises `PreflightRefused` with `binary_unavailable` while the admission function call count remains zero.

- [x] **Step 2: Run the regression test and verify it fails because admission currently precedes binary diagnosis**

Run: `python3 -m pytest tests/test_capacity.py::test_missing_finder_binary_is_refused_before_review_fg_admission -q --tb=short`

Expected: the test reaches the guard that raises `review-fg admission must not be acquired`.

- [x] **Step 3: Call the static readiness check before stale recovery and capacity**

After existing config/adapter and cached finder-chain preflight, check the current snapshot and raise `PreflightRefused` with the stable readiness reason code when it is known impossible. Check cancellation before the snapshot so an already-cancelled MCP review still returns `review cancelled`. Leave the existing under-lock authoritative capture, fallback chain, and gate/trust logic unchanged.

- [x] **Step 4: Run the regression and compatibility tests**

Run: `python3 -m pytest tests/test_capacity.py::test_missing_finder_binary_is_refused_before_review_fg_admission tests/test_pipeline.py::test_a_missing_reviewer_binary_is_refused_before_any_review_record -q --tb=short`

Expected: both tests pass and no provider process or durable review row is created.

### Task 5: Self-review and repository verification

**Files:**
- Modify: `docs/superpowers/plans/2026-08-11-review-readiness.md`

- [x] **Step 1: Review the diff for invariant violations**

Run: `git diff --check` and inspect that `src/skodun/gate.py` and `src/skodun/trust.py` are byte-identical to `origin/main`, readiness has no capacity/provider invocation, and no provider secret is logged.

- [x] **Step 2: Run changed-module tests**

Run: `python3 -m pytest tests/test_readiness.py tests/test_services.py tests/test_cli.py tests/test_mcpserver.py -q --tb=short`

Expected: exit `0` with no failures.

- [x] **Step 3: Run the full hermetic suite and store warning sweep**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short` and the repository’s `tests/test_store.py` ResourceWarning sweep.

Expected: both commands exit `0`; retain exact counts in the delivery notes.

- [x] **Step 4: Verify the shipped CLI/MCP surfaces manually**

Run `PYTHONPATH=src python3 -m skodun review-readiness --help` and a temporary-config readiness check, then exercise the MCP registry test or stdio request. Confirm output and structured metadata agree.

- [x] **Step 5: Commit the focused implementation**

```bash
git add src/skodun/readiness.py src/skodun/services.py src/skodun/cli.py src/skodun/mcpserver.py tests/test_readiness.py tests/test_services.py tests/test_cli.py tests/test_mcpserver.py README.md docs/superpowers/plans/2026-08-11-review-readiness.md
git commit -m "feat: add review readiness preflight refs #121"
```
