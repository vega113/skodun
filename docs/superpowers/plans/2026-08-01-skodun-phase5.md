# skodun Phase 5 Implementation Plan — the `junie` adapter

> **For agentic workers:** implement task-by-task. Between-task review is by
> **execution and mutation**, never inspection. Commit per task (`refs #23`)
> before any mutation experiment.

**Goal:** Ship a production-safe `junie` provider adapter by vendor-and-adapt
port of the oracle containment, under skodun's existing `Adapter` protocol and
conformance suite. No schema change. No `gate.py` / `trust.py` edits.

**Architecture:** fixed by
`docs/superpowers/specs/2026-08-01-skodun-phase5-design.md`. Read it first,
including cuts and landmines. Outer runner process presents a normal
REVIEW_CONTRACT JSON on stdout so `chain` / `runner` need no special case.

**Tech stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest only.

## Global Constraints

- Phase 1–4 Global Constraints still bind.
- **`gate.py` / `trust.py` byte pins** (tests/test_seams.py):
  - gate: `62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f`
  - trust: `8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1`
- No store v6. SCHEMA_VERSION stays 5.
- Runtime stdlib-only. Committed code fully generic (capsule marker
  `skodun-junie-review-capsule-v1:`).
- Oracle only via `SKODUN_ORACLE_DIR`. Report ran-vs-skipped counts.
- Tests pin `SKODUN_DB`, `GIT_CONFIG_GLOBAL`, every `SKODUN_<X>_BIN` to tmp.
- Method: every task names Mutations; each must die under a named test.
  `PYTHONDONTWRITEBYTECODE=1` for mutation runs. **Commit before mutating.**
- Full suite ~11–13 min; run foreground or poll background. Baseline at handoff:
  ~3089 pass / 1 skip with oracle; ~2928 / 160 without.

## File structure

```text
src/skodun/adapters/
  junie_confined_io.py   # NEW T1
  junie_sanitized.py     # NEW T2  (profile, binary, env, spawn helpers)
  junie_runner.py        # NEW T3  (capsule stage, normalize, __main__)
  junie.py               # NEW T4  (Adapter)
  __init__.py            # MOD T4  (registry + NORMAL_STOP_REASONS)
tests/
  test_junie_confined_io.py
  test_junie_sanitized.py
  test_junie_runner.py
  test_adapter_junie.py
  fixtures/adapters/junie/  # fixtures + README
examples/multi-provider.toml  # MOD T5
README.md                     # MOD T5
pyproject.toml                # MOD T5 version 0.4.0
```

---

### Task 1: Descriptor-confined reads

**Files:** `src/skodun/adapters/junie_confined_io.py`, `tests/test_junie_confined_io.py`

Port `open_confined_text` from the oracle's `junie_confined_io.py` essentially
verbatim (stdlib only; no project strings).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_junie_confined_io.py
from __future__ import annotations
import os
from pathlib import Path
from unittest import mock
import pytest
from skodun.adapters.junie_confined_io import open_confined_text

def test_reads_single_link_regular_file_inside_root(tmp_path: Path):
    artifact = tmp_path / "project" / "review.json"
    artifact.parent.mkdir()
    artifact.write_text('{"findings":[]}\n', encoding="utf-8")
    with open_confined_text(str(artifact), str(tmp_path), "review") as h:
        assert h.read() == '{"findings":[]}\n'

def test_rejects_path_outside_root(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        with open_confined_text(str(outside), str(tmp_path), "out"):
            pass

def test_rejects_symlink_final(tmp_path: Path):
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    real.write_text("{}", encoding="utf-8")
    link.symlink_to(real)
    with pytest.raises(ValueError, match="single-link regular file"):
        with open_confined_text(str(link), str(tmp_path), "link"):
            pass

def test_rejects_hardlink(tmp_path: Path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}", encoding="utf-8")
    os.link(a, b)
    with pytest.raises(ValueError, match="single-link regular file"):
        with open_confined_text(str(b), str(tmp_path), "b"):
            pass
```

- [ ] **Step 2: Run — expect fail** `python3 -m pytest tests/test_junie_confined_io.py -q`
- [ ] **Step 3: Implement** the oracle port of `open_confined_text`.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Commit** `feat(junie): port descriptor-confined reads (refs #23)`

**Mutations**
1. Drop `O_NOFOLLOW` from file open → `test_rejects_symlink_final` dies.
2. Drop `st_nlink != 1` check → `test_rejects_hardlink` dies.

---

### Task 2: Seatbelt profile, binary resolve, sanitized env

**Files:** `src/skodun/adapters/junie_sanitized.py`, `tests/test_junie_sanitized.py`

Port the pure helpers from oracle `junie-sanitized-exec.py`:
`resolve_sandbox_exec`, `build_sandbox_profile`, `resolve_junie_binary`,
`_require_managed_junie_data`, env builder, stripped-key set. Do **not** put
oracle/project paths in defaults. `SANDBOX_EXEC = "/usr/bin/sandbox-exec"`.

- [ ] **Step 1: Failing tests** covering:
  - `resolve_sandbox_exec` raises on non-darwin (mock `sys.platform`)
  - `build_sandbox_profile` denies link/clone, allows only capsule write
  - `resolve_junie_binary` rejects relative paths; resolves managed shim
  - `build_sanitized_env` strips the 12 keys, keeps `JUNIE_API_KEY` only when set
  - profile string uses JSON-encoded paths (no raw interpolation of untrusted)

- [ ] **Step 2–4: implement, green, commit**
  `feat(junie): port seatbelt profile and sanitized env (refs #23)`

**Mutations**
1. Leave `OPENAI_API_KEY` in sanitized env → env test dies.
2. Soft-allow non-darwin (`return SANDBOX_EXEC` always) → platform test dies.
3. Omit `(deny file-link file-clone)` from profile → profile test dies.

---

### Task 3: Outer runner — capsule, spawn, normalize

**Files:** `src/skodun/adapters/junie_runner.py`, `tests/test_junie_runner.py`

Public functions (importable; `__main__` thin):

```python
CAPSULE_MARKER_PREFIX = "skodun-junie-review-capsule-v1:"
STRIPPED_ENV_KEYS = (...)  # the 12

def stage_capsule(prompt_bytes: bytes, *, tmp_root: Path | None = None) -> Path:
    """Create capsule root; return it. Marker file written."""

def normalize_envelope(
    envelope: dict,
    *,
    project: Path,
    capsule: Path,
    configured_model: str,
) -> dict:
    """Return REVIEW_CONTRACT-shaped {summary, findings} or raise ValueError."""

def run_confined_junie(
    *,
    prompt_file: Path,
    binary: str,
    model: str,
    effort: str | None,
    timeout_ms: int,
    contract_schema: str,
    capsule_root: Path | None = None,
) -> tuple[int, bytes, bytes]:
    """Stage (if needed), spawn sandboxed junie, normalize, return (rc, out, err)."""
```

Normalization implements design trust contract (review.json path OR result
normalization; llmUsage model evidence against `configured_model`; no
gemini/grok in usage; no unexpected project files).

`run_confined_junie` on non-darwin returns rc=2, empty stdout, stderr reason
without spawning.

Spawn uses `subprocess.Popen([sandbox_exec, "-f", profile, *junie_argv], env=...,
cwd=project, stdin=prompt, stdout=capsule_stdout, stderr=capsule_stderr,
start_new_session=True)` then wait; on timeout the caller is the chain
watchdog on the outer process — still reap child group in a `finally` if the
outer is signalled. For unit tests, inject a `spawner` callable.

- [ ] **Step 1: Failing tests** for normalize (happy review.json, happy
  fenced JSON result, reject extra project file, reject wrong model usage,
  reject gemini in usage, reject symlink) and for `run_confined_junie`
  non-darwin refusal (mock platform).
- [ ] **Step 2–4: implement, green, commit**
  `feat(junie): outer runner with capsule normalize (refs #23)`

**Mutations**
1. Accept envelope without model evidence when llmUsage present → wrong-model test dies.
2. Skip unexpected-file walk → extra project file test dies.
3. On non-darwin, still call spawner → platform refusal test dies.

---

### Task 4: `JunieAdapter` + registry + conformance

**Files:** `src/skodun/adapters/junie.py`, `__init__.py`,
`tests/test_adapter_junie.py`, `tests/fixtures/adapters/junie/*`

```python
class JunieAdapter:
    name = "junie"
    provider = "junie"
    stdin_from_prompt_file = False

    def resolve_binary(self) -> str: ...  # SKODUN_JUNIE_BIN or "junie"
    def effort_map(self) -> dict[str, str]:  # low/medium/high
    def prompt_limit(self) -> None: ...
    def build_cmd(...):  # stages via junie_runner; returns
        # [sys.executable, "-I", "-m", "skodun.adapters.junie_runner", ...]
    def parse(...):  # REVIEW_CONTRACT from stdout root JSON
    def classify(...):  # rc 127 binary; stderr signals; else ok/degraded
```

Outer-runner stderr signals (examples): `junie confinement requires macOS`,
`sandbox-exec is unavailable`, `authentication`, `quota`, `payment required`,
`rate limit`. Synthesize quota fixture from documented/binary wording; record
provenance in fixtures README.

Register in `_REGISTRY["junie"] = JunieAdapter`. Do **not** invent a
NORMAL_STOP_REASONS token unless a real harness word exists — outer runner
emits pure REVIEW_CONTRACT JSON without a status field; `stop_reason=None` and
healthy classify is `ok` only when parse_ok and no degraded stderr.

- [ ] **Step 1: fixtures + conformance subclass + build_cmd argv tests**
- [ ] **Step 2: implement adapter + registry**
- [ ] **Step 3: full adapter test file green**
- [ ] **Step 4: commit** `feat(junie): register junie adapter with conformance (refs #23)`

**Mutations**
1. Omit registry entry → conformance coverage gate dies.
2. Put prompt text in argv → build_cmd test (no prompt body in argv) dies.
3. `stdin_from_prompt_file = True` → dedicated test dies.
4. Drop a required fixture → conformance rule fails.

---

### Task 5: Docs, examples, version

**Files:** `examples/multi-provider.toml`, `README.md`, `pyproject.toml`
(version `0.4.0`), optional short note in Known limitations.

- [ ] Comment block for junie reviewer; version bump; README provider row.
- [ ] Commit `docs(junie): document junie provider and bump to 0.4.0 (refs #23)`

**Mutations:** none code-path; verify `python -m skodun providers` lists junie
with tmp store.

---

### Task 6: Full suite both oracle modes + seams

```bash
# without oracle
env -u SKODUN_ORACLE_DIR PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=no
# with oracle (if available)
SKODUN_ORACLE_DIR="$SKODUN_ORACLE_DIR" PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=no
# seams
python3 -c "..."  # compare to pins
python3 -m skodun providers
```

Record evidence under the goal scratch dir. Open a PR for the coherent chunk
when ready (`refs #23`).

## Adversarial review notes (pre-execution)

Checked against shipped source before coding:

1. **`build_cmd` side effects are already allowed** (codex writes a schema
   sidecar). Capsule staging beside a temp root is the same class of effect;
   the chain's scratch dir is for prompts, not necessarily for capsules —
   capsules use system temp and clean themselves.
2. **`chain` only opens stdin when `stdin_from_prompt_file`** — junie must be
   False or the chain double-feeds and the outer runner's own open races.
3. **`argv[0]` is `sys.executable`**, not the junie binary — `_binary_is_absent`
   still checks `adapter.resolve_binary()` first, so a missing junie is
   classified before `build_cmd`. The outer process binary (python) is always
   present; that is correct.
4. **No stop_reason from junie harness** — do not add a fake `EndTurn` to
   NORMAL_STOP_REASONS; reporting code already treats unknown stop reasons as
   non-normal for banner purposes only (not trust). Prefer `stop_reason=None`.
5. **Schema projection for refuter:** outer runner must honour contract —
   pass `contract.json_schema` / name so refuter attempts get the refuter
   shape. Normalization for refuter accepts `verdicts` the same way.
6. **Plan defect to avoid:** a mutation that only asserts string presence in
   source without driving the function — every mutation above maps to a
   behavioral test.
