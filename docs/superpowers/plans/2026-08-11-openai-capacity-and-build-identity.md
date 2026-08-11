# OpenAI Capacity and Build Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an OpenAI/Codex-routed review use the same selected finder chain for its skeptic pass instead of accidentally requiring the refuter provider, and make MCP review/readiness responses identify the exact Skodun build that answered.

**Architecture:** The cross-provider refuter remains a separate annotation-only policy. Skeptic selection will use the selected finder entry and its configured fallbacks, so a Google refuter quota outage cannot demote a clean Codex review. MCP response metadata will add the cached runtime commit beside the existing package version; the local machine config will disable the optional cross-model tie-break so an `openai` client family prefers the available Codex pool by capacity/order.

**Tech Stack:** Python 3.12+ stdlib, pytest, TOML configuration, stdio MCP structured responses.

---

### Task 1: Separate skeptic and refuter reviewer selection

**Files:**
- Modify: `src/skodun/pipeline.py:989-1007`
- Modify: `tests/test_pipeline.py` near the extra-pass selection tests
- Modify: `README.md` reviewer-selection and skeptic/refuter sections
- Modify: `examples/fragments/review-troubleshooting.md`
- Modify: `examples/multi-provider.toml` comments describing extra-pass chains
- Modify: `docs/integrate-external-project.md` skeptic/quota guidance

- [ ] **Step 1: Write the failing regression test**

Add a test that constructs a finder entry on `xai` and a configured `refuter` entry on `google`, then asserts the production selector returns the finder for `skeptic` and the Google entry for `refuter`:

```python
def test_skeptic_reuses_selected_finder_chain_not_refuter_role():
    finder = Reviewer(name="finder", provider="xai", model="grok", role="finder")
    refuter = Reviewer(name="refuter", provider="google", model="claude", role="refuter")
    cfg = Config(defaults=Defaults(), reviewers=(finder, refuter))

    assert pipeline._pass_reviewer(cfg, "skeptic", finder) is finder
    assert pipeline._pass_reviewer(cfg, "refuter", finder) is refuter
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run `python3 -m pytest tests/test_pipeline.py -k skeptic_reuses_selected_finder_chain -q`. It must fail because the current selector maps `skeptic` to the `refuter` role.

- [ ] **Step 3: Implement the minimal selection change**

Change `_EXTRA_PASS_ROLES` so the skeptic maps to `finder`, while the refuter continues to map to `refuter`:

```python
_EXTRA_PASS_ROLES = {"security": "security", "skeptic": "finder",
                     "refuter": "refuter",
                     passes.INTEGRATION_PASS: passes.INTEGRATION_ROLE}
```

Update the surrounding comments/docstrings to state that the skeptic inherits the selected finder's chain and that only the annotation refuter uses the `refuter` role. Do not change skeptic demotion, refuter annotation, trust, or gate semantics.

- [ ] **Step 4: Verify the focused pipeline behavior**

Run `python3 -m pytest tests/test_pipeline.py -k 'skeptic or extra_pass' -q`. The existing clean-review, skeptic-failure, and refuter tests must remain green.

- [ ] **Step 5: Update operator documentation**

Replace wording that says a `role = "refuter"` reviewer serves the skeptic pass. Document that a clean review's skeptic uses the selected finder/fallback chain, while `role = "refuter"` remains the separate, annotation-only cross-provider pass. Add the explicit operational example: `--reviewer finder-codex` or an auto route with `cross_model = false` uses Codex for both finder and skeptic when its chain is available.

### Task 2: Return exact build identity in MCP results

**Files:**
- Modify: `src/skodun/mcpserver.py:_review_result`
- Modify: `tests/test_mcpserver.py` and `tests/test_mcptools.py`
- Modify: `README.md` MCP/version guidance if the response shape needs operator wording

- [ ] **Step 1: Write the failing metadata test**

Add a test that patches `skodun.provenance.cached_provenance` to return a known commit, calls `_review_result(0, "ok")`, and asserts its metadata includes both the existing `skodun_version` and the exact `skodun_commit`. Assert that `tool_result(result)["structuredContent"]` carries the same fields.

- [ ] **Step 2: Run the focused test and verify it fails**

Run `python3 -m pytest tests/test_mcpserver.py -k 'build_identity or review_result' -q`. It must fail because review/readiness responses currently expose only `skodun_version`.

- [ ] **Step 3: Add cached commit metadata**

Make `_review_result` lazily read `provenance.cached_provenance()` and add `skodun_commit` to the existing metadata. If no commit is available, include `None` rather than guessing; preserve all caller metadata and the existing version field.

- [ ] **Step 4: Verify MCP response compatibility**

Run `python3 -m pytest tests/test_mcpserver.py tests/test_mcptools.py -q`. Update exact metadata assertions to include the new field while preserving every existing review/readiness status and text assertion.

### Task 3: Apply and verify the local Codex routing policy

**Files:**
- Modify: `/Users/vega/.config/skodun/config.toml` under `[routing]`

- [ ] Set `cross_model = false` with a comment explaining that this machine has an available Codex subscription and wants availability/order scoring rather than a cross-family tie-break. Keep the explicit `pool` and fallback graph unchanged; explicit `--reviewer` pins remain available for deliberate second opinions.
- [ ] Read the merged config and live store without starting a model; assert `routing.auto_route(..., client_family="openai")` selects `finder-codex` under the current provider state.
- [ ] Run `/Users/vega/.local/bin/skodun --version`, `/Users/vega/.local/bin/skodun doctor --repo /Users/vega/.codex/worktrees/98fe/skodun`, and the MCP initialize handshake. Record the installed version, schema, and build-identity visibility.

### Task 4: Full verification and handoff

**Files:**
- Review: all changed source, tests, docs, and the local configuration diff

- [ ] Run `git diff --check` and inspect the complete diff for accidental gate/trust changes, altered refuter semantics, or leaked provider credentials.
- [ ] Run focused suites for pipeline, MCP, config, and routing, then `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`.
- [ ] Run the required store ResourceWarning sweep:

```bash
python3 -m pytest tests/test_store.py \
  --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error
```

- [ ] Re-check `git status`, commit only repository files, push the branch, open a PR against `main`, and monitor checks/review threads through merge. Report the separate local config change and the exact provider-readiness evidence used to unblock the TubeScribes review.
