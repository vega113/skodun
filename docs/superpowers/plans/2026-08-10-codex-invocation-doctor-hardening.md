# Codex Invocation Diagnostics and Doctor Smoke Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex native-spawn failures such as `ENOENT` and `EACCES` advance fallback with a bounded invocation diagnostic, and make `skodun doctor` execute `codex --version` so broken native installations are visible before a review.

**Architecture:** Keep classification provider-specific in `CodexAdapter`: extend its stderr signal table and expose a small `version_probe()` that launches the resolved binary with `--version`. Keep doctor read-only and generic: when an adapter exposes a probe, record both success and failure instead of silently dropping probe exceptions. Do not change trust, gate, routing, runner process-group behavior, or store schema.

**Tech Stack:** Python 3.12 standard library, pytest, subprocess, existing adapter and doctor APIs.

---

### Task 1: Pin native-spawn diagnostics in the Codex classifier

**Files:**
- Modify: `src/skodun/adapters/codex.py`
- Test: `tests/test_adapter_codex.py`

- [x] **Step 1: Write the failing regression test**

Add a parametrized test beside the existing Codex classification tests. For each native-spawn marker (`ENOENT`, `EACCES`, `EPERM`, and `exec format error`), call `CodexAdapter().classify(1, b"", b"codex: spawn ... <marker>\n", REVIEW_CONTRACT)` and assert `kind == "unavailable"`, `category == "invocation"`, and the marker is retained in the bounded detail.

- [x] **Step 2: Run the focused test and verify it fails for the missing signal**

Run:

```bash
python3 -m pytest tests/test_adapter_codex.py -k native_spawn -q
```

Expected: the test fails because the current `_INVOCATION_SIGNALS` table does not contain the native errno spellings.

- [x] **Step 3: Add the minimal invocation signals**

Extend `_INVOCATION_SIGNALS` with lowercase byte markers for `enoent`, `eacces`, `eperm`, and `exec format error`. Keep the existing no-events/no-stdout guard, so these signals only classify a nonzero empty run as unavailable and never inspect model-authored text.

- [x] **Step 4: Run the focused test and the full Codex adapter suite**

Run:

```bash
python3 -m pytest tests/test_adapter_codex.py -k native_spawn -q
python3 -m pytest tests/test_adapter_codex.py tests/test_openai_api.py -q
```

Expected: both commands pass. The Codex module alone has a pre-existing
conformance-loader ordering check that requires the OpenAI API sibling module
to be collected as well.

### Task 2: Add a real Codex version probe and surface failures in doctor

**Files:**
- Modify: `src/skodun/adapters/codex.py`
- Modify: `src/skodun/doctor.py`
- Test: `tests/test_doctor.py`

- [x] **Step 1: Write the failing doctor smoke-check tests**

Add tests that create executable temporary scripts selected through `SKODUN_CODEX_BIN`. A successful script must verify its first argument is `--version`, write a version string, and exit zero; assert the `adapter:openai:version` check is present, successful, and contains that version. A failing script must write `spawn ... ENOENT` to stderr and exit nonzero; assert the same check is present, failed, and preserves the diagnostic. These tests exercise `run_doctor`, not a mocked subprocess.

- [x] **Step 2: Run the focused doctor tests and verify they fail**

Run:

```bash
python3 -m pytest tests/test_doctor.py -k 'codex or version' -q
```

Expected: the tests fail because `CodexAdapter` has no `version_probe()` and doctor does not emit a failed check for probe errors.

- [x] **Step 3: Implement the Codex probe**

Import `subprocess` in `codex.py` and add `CodexAdapter.version_probe()`. It must invoke `[self.resolve_binary(), "--version"]` with captured byte output, a bounded timeout, and `check=False`; raise a concise error for a nonzero exit or empty output, and return the first non-empty output line for a successful probe. Preserve `FileNotFoundError`/permission errors for doctor to report.

- [x] **Step 4: Make doctor report probe failures**

Replace doctor’s silent `except Exception: pass` around `adapter.version_probe()` with a failed `adapter:<provider>:version` report line containing the exception representation. Keep missing/non-executable binaries on their existing adapter status path and preserve the read-only behavior.

- [x] **Step 5: Run focused tests and the full doctor suite**

Run:

```bash
python3 -m pytest tests/test_doctor.py -q
```

Expected: all doctor tests pass, including both real subprocess smoke cases.

### Task 3: Review and verify the complete change

**Files:**
- Review: `src/skodun/adapters/codex.py`, `src/skodun/doctor.py`, `tests/test_adapter_codex.py`, `tests/test_doctor.py`

- [x] **Step 1: Inspect the diff and whitespace**

Run `git diff --check` and inspect `git diff` for scope, bounded diagnostics, no secret leakage, and no changes to gate/trust/routing/store behavior.

- [x] **Step 2: Run the complete test suite**

Run `python3 -m pytest` from the repository root. Expected: exit code 0 with no failures.

- [x] **Step 3: Run the shipped CLI smoke commands**

Run `PYTHONPATH=src python3 -m skodun --version`, `PYTHONPATH=src python3 -m skodun doctor --repo .`, and `PYTHONPATH=src python3 -m skodun providers --repo .`. Record the actual local Codex binary state; a broken installed Codex should make its doctor version check fail rather than be mistaken for a healthy install.
