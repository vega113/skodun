# Quota-Pool-Aware Auto Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep independent provider quota pools independently routable, make Junie and prompt-size eligibility candidate-local, and advance safely on genuine Codex empty-output failures.

**Architecture:** Add a validated `Reviewer.quota_pool` dimension with adapter/model-derived defaults for existing configs. Persist quota blackouts by `(provider, quota_pool)` through an additive SQLite migration while recognizing legacy provider-wide rows. Use the same pool key for routing load/capacity views and provider diagnostics, and add cheap adapter eligibility before auto selection. Extend Codex classification with bounded sanitized diagnostics for nonzero empty streams; retain fail-closed trust semantics and existing fallback behavior.

**Tech Stack:** Python 3.12 stdlib, SQLite migration ladder, pytest hermetic tests.

---

### Task 1: Define and validate quota-pool identity

**Files:**
- Modify: `src/skodun/config.py` (`Reviewer`, validation, config loading)
- Modify: `src/skodun/adapters/base.py` (provider-neutral pool helper/protocol surface)
- Modify: `src/skodun/adapters/agy.py` and `src/skodun/adapters/junie.py` only where adapter-owned eligibility/pool defaults are needed
- Test: `tests/test_config.py`, `tests/test_adapter_agy.py`, `tests/test_adapter_junie.py`

- [ ] Add an optional, non-empty `quota_pool` reviewer field and validate it as a string when supplied; preserve existing config files by deriving `google:gemini` for Gemini models, `google:claude-gpt` for AGY Claude/GPT models, and the provider id for other providers.
- [ ] Expose one helper that returns the resolved pool for a reviewer so routing, chain, capacity, and diagnostics do not each infer it independently.
- [ ] Add tests for explicit pool values, legacy-derived AGY pools, invalid values, and unchanged non-Google defaults.

### Task 2: Migrate provider state and make capacity/load routing pool-aware

**Files:**
- Modify: `src/skodun/store.py` (schema version 10 migration and provider-state APIs)
- Modify: `src/skodun/capacity.py` (pool-aware resource key while retaining old provider-only calls)
- Modify: `src/skodun/chain.py` (pool-aware cache, quota writes, effective capacity, and slot acquisition)
- Modify: `src/skodun/routing.py` (pool-keyed `ProviderLoad`, blackout/load grouping, route audit)
- Modify: `src/skodun/cli.py` (provider/pool state and holder diagnostics)
- Test: `tests/test_store.py`, `tests/test_capacity.py`, `tests/test_chain.py`, `tests/test_routing.py`, `tests/test_cli.py`

- [ ] Add a transactional additive migration that permits multiple state rows for one provider, copies legacy rows into a legacy provider-wide pool marker, and leaves expired/healthy pools routable.
- [ ] Keep old method call shapes valid, but allow `mark_provider_unavailable`, `provider_unavailable_reason`, and `provider_state_rows` to accept/report a pool; exact pool reads must also honor an active legacy provider-wide row.
- [ ] Use resolved pool keys for provider capacity scope/resource grouping and effective quota capacity; keep `provider` and adapter attribution unchanged in attempt/review records.
- [ ] Key routing loads, served-count grouping, and blackout checks by quota pool; include the selected pool in route metadata and `providers` output without exposing secrets.
- [ ] Prove AGY Claude/GPT blackout does not exclude Gemini, Gemini blackout does not exclude Claude/GPT, same-pool entries still share load, and legacy state remains safe.

### Task 3: Add safe automatic candidate eligibility

**Files:**
- Modify: `src/skodun/adapters/base.py` (optional eligibility hook/default)
- Modify: `src/skodun/adapters/junie.py` and `src/skodun/adapters/junie_sanitized.py` (macOS Seatbelt/binary readiness predicate)
- Modify: `src/skodun/routing.py` (eligibility filtering and optional prompt-size check)
- Modify: `src/skodun/pipeline.py` (pass the available prompt/budget feasibility signal and preserve fallback metadata)
- Test: `tests/test_routing.py`, `tests/test_pipeline.py`, `tests/test_adapter_junie.py`

- [ ] Exclude missing binaries, non-macOS Junie, unavailable Seatbelt, and prompt sizes above a candidate adapter’s declared limit before choosing an automatic head; report candidate-local reasons and preserve explicit pins.
- [ ] Keep Junie confined-only: no unconfined fallback. A Junie candidate without a safe configured fallback is not selected when its execution would leave no safe recovery path.
- [ ] Ensure an oversized prompt can only make that candidate unavailable and the existing chain advances to its configured fallback; no quota-pool state is written for prompt size.
- [ ] Test feasible Junie selection, oversized candidate exclusion/fallback, non-macOS refusal, and pin behavior.

### Task 4: Classify Codex nonzero empty-output failures with bounded diagnostics

**Files:**
- Modify: `src/skodun/adapters/codex.py` (classification categories, diagnostic sanitizer/bounds)
- Modify: `src/skodun/chain.py` (persist the bounded diagnostic in attempt artifact and advance on availability/configuration categories)
- Test: `tests/test_adapter_codex.py`, `tests/test_chain.py`
- Add: `tests/fixtures/adapters/openai/unavailable_empty_output.txt`

- [ ] Add a strict nonzero/no-usable-payload path: recognized auth, quota, model/config, invocation, transport, and harness diagnostics become `unavailable` with a specific category; unknown failures remain `ok` plus `parse_ok=false` and therefore untrustworthy.
- [ ] Sanitize control characters, redact obvious credential/token material, and cap diagnostic length before storing or displaying it; never include prompts or unrestricted provider output.
- [ ] Assert `rc=1`, empty stdout, sub-second-style stderr is retained as bounded evidence, the fallback chain advances for recognized availability failures, and trust/gate axes remain unchanged.

### Task 5: Self-review and verification

**Files:**
- Modify: documentation/examples only if the shipped config/diagnostic contract needs operator wording.

- [ ] Run focused red-green tests for each new behavior, then the full pytest suite and the store ResourceWarning sweep required by `AGENTS.md`.
- [ ] Run `git diff --check`, inspect the complete diff for gate/trust edits, migration safety, secret leakage, and legacy API compatibility.
- [ ] Run the CLI smoke paths for `providers` and `--help`; record implementation/test status and any external delivery blocker separately.
