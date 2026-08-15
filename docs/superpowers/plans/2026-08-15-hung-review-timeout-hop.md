# Hung Review Timeout Hop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a silent no-output timeout from occupying exclusive review-fg for a second same-provider wait, keep parseable finder evidence when a later extra pass times out, and point configured reviewers at grok-4.6 / gpt-5.6-luna / gemini-3.7-flash.

**Architecture:** Treat a watchdog timeout with `first_output_sec is None` as “this provider did not serve” so `run_chain` hops to the next configured fallback instead of consuming `timeout_retries` on the same hung CLI. A timeout that already printed is unchanged (existing emit-then-hang contract). Extra-pass merge still fail-closes when the pass produces nothing, but records the extra-pass reason and does not spend another full same-provider timeout-retry cycle. Model/effort tables are config-only.

**Tech Stack:** Python ≥ 3.12, stdlib-only runtime, pytest, shipped `run_review` / fake CLIs.

## Global Constraints

- Do not edit `gate.py` / `trust.py`.
- Do not parse timed-out stdout.
- Do not add a new store enum; reuse the existing hop-on-unavailable path.
- Do not hop on emit-then-hang / degraded-but-parsed answers.
- `gemini-3.7-flash` on agy is the suffixed id `gemini-3.7-flash-high` with no `effort` line.
- Junie on this host lists `gpt-5.6-luna` only among the requested ids; do not invent grok/gemini junie aliases.

---

### Task 1: No-output timeout hops (fixture A)

**Files:**
- Modify: `src/skodun/chain.py` timeout branch
- Test: `tests/test_fallback.py`, `tests/test_pipeline.py`

- [x] **Step 1: Write the failing tests**

Hermetic `run_review` with a silent-hang head (`sleep` / empty stdout) and a working fallback. Assert the hung provider is invoked once, the fallback answers, and `failure_reason` is not `timed out after 2 attempts` on the hung head. Second case: lone hung provider + `timeout_retries = 1` is invoked once.

- [x] **Step 2: Implement hop in `run_chain`**

If `result.timed_out` and `first_output_sec is None`: append the attempt, break to the next chain entry (or fail closed if none remain). Do not increment `timeouts_used`. Leave emit-then-hang on the existing retry-then-stop path.

- [x] **Step 3: Run the new tests and the existing emit-then-hang timeout tests**

---

### Task 2: Extra-pass timeout keeps finder evidence (fixture B)

**Files:**
- Modify: `src/skodun/pipeline.py` `_extra_pass`
- Test: `tests/test_pipeline.py`

- [x] **Step 1: Write the failing test**

Finder emits a clean envelope; the skeptic call hangs with empty stdout; `timeout_retries = 1`. Assert finder summary/findings remain, `extra_passes.skeptic.failed` is true, and the hung CLI is not given a second extra-pass timeout.

- [x] **Step 2: Prefix extra-pass timeout reasons; rely on Task 1 for no retry**

`merge_failed_extra_pass` already keeps summary/findings and demotes (fail closed). Prefix `failure_reason` so the record names the extra pass rather than looking like the finder never answered.

---

### Task 3: Reviewer model/effort tables

**Files:**
- Modify: `examples/multi-provider.toml`, `.skodun.toml`, `README.md` snippets, `~/.config/skodun/config.toml`
- Test: `tests/test_config.py`

Configured IDs: grok `grok-4.6` + `medium`; openai `gpt-5.6-luna` + `high`; google Gemini `gemini-3.7-flash-high` (no effort). Junie pool: `gpt-5.6-luna` + `high`. Host finder gets an acyclic fallback to `finder-gemini` so a silent grok hang can hop.

---

### Task 4: Verify, self-review, skodun-review, PR

Run focused then full pytest; `skodun providers` against the example config; persist scratch evidence; self-review; skodun-review loop; open PR and land.
