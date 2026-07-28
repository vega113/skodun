# skodun Phase 2 Implementation Plan — Multi-Provider, Refuter, Fallback Chains

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** skodun reviews through any of four provider CLIs (grok, codex, claude, agy), with a cross-provider refuter that annotates findings, and per-reviewer quota-fallback chains that fail closed when exhausted.

**Architecture:** The design is fixed by `docs/superpowers/specs/2026-07-28-skodun-phase2-design.md` (owner-approved) — read it first; this plan implements it and does not re-litigate it. Provider-neutral adapter contract in `adapters/base.py` + a conformance suite as the registration gate; fallback = fresh attempt by a different reviewer entry; refuter = annotation-only extra pass; store changes are additive (artifact JSON + one new table via a `user_version` migration runner).

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest only.

## Global Constraints

- Everything in the Phase 1 plan's Global Constraints still binds: fail-closed trust invariant (single definition, store-enforced strict-bool axes), gate 0/1/2 with every unexpected exception → 2, `encoding="utf-8"` everywhere, prompts/diffs travel as files, explicit model selection, public-repo hygiene (no machine paths, no upstream project names or repo-layout literals in `src/` — including prompt text), oracle located only via `SKODUN_ORACLE_DIR`.
- Phase 1 parity surfaces (diff identity, triage keys, prompt bytes) are untouched. This phase adds **no** oracle-ported code; all existing parity tests must remain green and unmodified (except `tests/test_gate.py::test_severity_gate_high_still_blocks_on_a_low_finding` — rewritten without the removed key — and the generic config parametrizations that shrink when the fields go, per Task 3).
- Adapter fixtures are **captured from real CLI output**, committed under `tests/fixtures/adapters/<provider>/`, and sanitized (no tokens, no usernames, no machine paths inside fixture bytes). New adapters capture theirs in their probe step; grok's come from real archived envelopes in the legacy archive (Task 2), with any fixture that had to be synthesized (no real capture exists, e.g. stderr signals) explicitly labeled as such in the fixture directory's README.
- `attempts[]` entries and `extra_passes.<name>` objects always carry `{provider, model, effort}` from this phase on; absence of those fields means "Phase 1 record" and every reader must tolerate absence.
- New CLI surface (`providers`, `triage --adopt-refuter`, `shadow-compare --since`) follows the existing pattern: every path ends in a defined exit code, observational commands survive a closed stdout, and no command exits 0 by accident.
- Live-CLI tests (anything invoking a real provider binary) are opt-in via env (`SKODUN_LIVE_<PROVIDER>=1`) and skip cleanly otherwise; CI runs fixture-driven tests only.

## File Structure

```
src/skodun/
├── adapters/
│   ├── __init__.py      # registry + re-exports (modified)
│   ├── base.py          # NEW: ParseResult, Classification, Adapter protocol
│   ├── grok.py          # modified: classify(), fixtures, base imports
│   ├── codex.py         # NEW: openai provider via codex CLI
│   ├── claude.py        # NEW: anthropic provider via claude CLI
│   └── agy.py           # NEW (contingent): google provider via agy CLI
├── config.py            # modified: key removal + fallbacks validation
├── store.py             # modified: migration runner, provider_state
├── pipeline.py          # modified: classification loop, fallback chains, refuter wiring
├── runner.py            # modified: stdin_path support
├── passes.py            # modified: refuter prompt/schema/merge
├── triage.py            # modified: adopt-refuter reason synthesis
├── cli.py               # modified: KeyboardInterrupt, providers, adopt-refuter, --since
└── shadow.py            # modified: --since window
tests/
├── adapter_conformance.py   # NEW: shared conformance mixin
├── fixtures/adapters/<provider>/*.txt   # NEW: captured envelopes
└── test_adapter_{codex,claude,agy}.py, test_fallback.py, test_refuter.py  # NEW
docs/phase2-acceptance.md    # NEW: live acceptance runbook + evidence log
examples/multi-provider.toml # NEW
```

---

### Task 1: Adapter base contract

**Files:**
- Create: `src/skodun/adapters/base.py`, `tests/test_adapter_base.py`
- Modify: `src/skodun/adapters/__init__.py`, `src/skodun/adapters/grok.py` (imports only)

**Interfaces:**
- Produces, in `base.py`:
  - `ParseResult` (moved from `grok.py`, field-identical) and the provider-neutral payload helpers `_eligible`/`_valid_payload` **moved here from `grok.py`** (both re-exported from `grok.py` so its tests keep passing unchanged).
  - `UNAVAILABLE_RC = 127`.
  - `ClassifyResult(kind: Literal["ok", "degraded", "unavailable"], category: str, detail: str)` — `category` is the cacheability axis for `unavailable`: one of `"quota" | "auth" | "binary" | "model" | "other"` (empty for ok/degraded). Only `"quota"` is provider-wide-cacheable (Task 7); auth/binary/model failures stay attempt-local.
  - `OutputContract(name: str, json_schema: str, validate: Callable[[object], bool])` — the response contract a run is asked for. Two instances live here: `REVIEW_CONTRACT` (the Phase 1 review schema + `_valid_payload`) and `REFUTER_CONTRACT` (Task 8's verdicts schema + its validator). Adapters are contract-generic: they pass `contract.json_schema` to their CLI's schema mechanism and validate payloads with `contract.validate`.
  - `class Adapter(Protocol)`: `name: str` (adapter name, e.g. `"codex"`), `provider: str` (e.g. `"openai"`), `stdin_from_prompt_file: bool` (class attr, default `False` — set by adapters whose CLI takes the prompt on stdin; Task 7 honors it),
    `resolve_binary() -> str` (env override `SKODUN_<NAME>_BIN` → adapter default → bare name on PATH; grok's wraps the existing `resolve_grok_bin`),
    `build_cmd(prompt_file, r: Reviewer, d: Defaults, cwd, contract: OutputContract = REVIEW_CONTRACT) -> list[str]`,
    `parse(stdout: bytes, stderr: bytes, contract: OutputContract = REVIEW_CONTRACT) -> ParseResult`,
    `classify(rc: int, stdout: bytes, stderr: bytes) -> ClassifyResult`,
    `effort_map() -> dict[str, str]` (canonical effort → CLI value; a canonical value absent from the map is a **loud** `ValueError` in `build_cmd`).
- `adapters/__init__` re-exports `ParseResult`, `ClassifyResult`, `OutputContract`, `REVIEW_CONTRACT`, `REFUTER_CONTRACT`, `Adapter`, `get_adapter`.
- Semantics (from the spec, binding): `degraded` = positive evidence the harness truncated/corrupted this run's output; `unavailable` = the provider could not serve at all. `classify` and `parse` never raise, on any input.
- `GrokAdapter.parse` becomes contract-parametric: it validates the extracted payload with `contract.validate`, sets `payload` on success, and populates the `findings`/`summary` projection **only when `contract is REVIEW_CONTRACT`** (behavior under `REVIEW_CONTRACT` is byte-identical; its existing tests must pass unmodified). The envelope-extraction eligibility predicate also becomes contract-aware: for the refuter contract, an object with `verdicts` is eligible (extend `_eligible` to take the contract, or give `OutputContract` an `eligible: Callable` field — pick one and apply it to all three fallback levels).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_base.py
from skodun.adapters import ParseResult, REVIEW_CONTRACT, REFUTER_CONTRACT, get_adapter
from skodun.adapters.base import UNAVAILABLE_RC

def test_parse_result_importable_from_base_and_grok():
    from skodun.adapters.base import ParseResult as base_pr
    from skodun.adapters.grok import ParseResult as grok_pr
    assert base_pr is grok_pr          # one class, re-exported — not a copy

def test_grok_adapter_satisfies_protocol():
    a = get_adapter("xai")
    assert a.provider == "xai" and a.name == "grok"
    assert a.stdin_from_prompt_file is False
    assert callable(a.classify) and callable(a.effort_map)
    assert a.resolve_binary()          # non-empty string, env override honored

def test_rc_127_is_unavailable_binary_for_every_registered_adapter():
    from skodun.adapters import _REGISTRY
    for cls in _REGISTRY.values():
        r = cls().classify(UNAVAILABLE_RC, b"", b"command not found")
        assert r.kind == "unavailable" and r.category == "binary"

def test_contracts_validate_their_own_shapes():
    assert REVIEW_CONTRACT.validate({"summary": "s", "findings": []})
    assert not REVIEW_CONTRACT.validate({"verdicts": []})
    assert REFUTER_CONTRACT.validate(
        {"verdicts": [{"index": 0, "verdict": "refuted",
                       "reasoning": "the guard on entry already handles the None case"}]})
    assert not REFUTER_CONTRACT.validate({"summary": "s", "findings": []})
```

- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/test_adapter_base.py -v`
- [ ] **Step 3: Implement** — create `base.py` with the dataclass moved verbatim plus:

```python
# src/skodun/adapters/base.py (excerpt — full docstrings in the style of adapters/__init__)
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol
from ..config import Defaults, Reviewer

UNAVAILABLE_RC = 127   # shell's command-not-found: the binary itself is absent

@dataclass(frozen=True)
class ClassifyResult:
    kind: Literal["ok", "degraded", "unavailable"]
    category: str = ""     # for unavailable: quota|auth|binary|model|other
    detail: str = ""

@dataclass(frozen=True)
class ParseResult:
    parse_ok: bool
    findings: list            # review-contract projection; [] for other contracts
    summary: str              # review-contract projection; "" for other contracts
    stop_reason: str | None
    degraded: bool
    degraded_reason: str
    payload: dict | None = None   # NEW: the contract-validated payload, verbatim.
    # `findings`/`summary` stay as the REVIEW_CONTRACT projection so every
    # existing caller keeps working; consumers of other contracts (the refuter
    # merge reads payload["verdicts"]) use `payload`. `payload` is populated
    # whenever parse_ok, for every contract, review included.

@dataclass(frozen=True)
class OutputContract:
    name: str                          # "review" | "refuter"
    json_schema: str                   # single-line JSON Schema for the CLI flag
    validate: Callable[[object], bool]

REVIEW_CONTRACT = OutputContract("review", _REVIEW_SCHEMA, _valid_payload)
REFUTER_CONTRACT = OutputContract("refuter", _REFUTER_SCHEMA, _valid_verdicts)
# _REVIEW_SCHEMA moves here from grok.SCHEMA (re-exported there);
# _REFUTER_SCHEMA/_valid_verdicts are defined in full in this task — see below.

class Adapter(Protocol):
    name: str
    provider: str
    stdin_from_prompt_file: bool = False
    def resolve_binary(self) -> str: ...
    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults, cwd: Path,
                  contract: OutputContract = REVIEW_CONTRACT) -> list[str]: ...
    def parse(self, stdout: bytes, stderr: bytes,
              contract: OutputContract = REVIEW_CONTRACT) -> ParseResult: ...
    def classify(self, rc: int, stdout: bytes, stderr: bytes) -> ClassifyResult: ...
    def effort_map(self) -> dict[str, str]: ...
```

`_REFUTER_SCHEMA` (single line, same style as the review schema): object with required `verdicts` array of objects `{index: integer, verdict: enum[confirmed|refuted|uncertain], reasoning: string}` all three required. `_valid_verdicts` mirrors `_valid_payload`'s strictness: top level must have a list `verdicts`; every item a dict with `type(index) is int` (bool excluded), `verdict` in the enum, `reasoning` a str. Reasoning *length* is not validated here — that is merge policy (Task 8), not payload shape.

Give `GrokAdapter` `provider = "xai"`, `name = "grok"`, `resolve_binary()` delegating to the existing `resolve_grok_bin`, an `effort_map()` returning its current pass-through table, and a `classify()` built from its existing `_detect_degraded` signals plus unavailability signals (rc 127 → `binary`; `authorizationrequired`-style auth-fatal stderr **only when stdout carried no usable envelope** — the Phase 1 non-signal rule stands when output is healthy → `auth`; unknown-model-id stderr → `model`; quota/rate-limit stderr → `quota`). Keep `_detect_degraded`'s behavior byte-for-byte (its tests must not change).

- [ ] **Step 4: Run to verify PASS** — plus full suite: `python3 -m pytest -q` (no regressions).
- [ ] **Step 5: Commit** — `git commit -am "feat: provider-neutral adapter contract in adapters/base (refs EPIC)"`

---

### Task 2: Conformance suite + grok retrofit

**Files:**
- Create: `tests/adapter_conformance.py`, `tests/fixtures/adapters/xai/{healthy.txt,degraded_stopreason.txt,degraded_stderr.txt,unavailable_auth.txt}`
- Modify: `tests/test_adapter_grok.py` (add the mixin subclass)

**Interfaces:**
- Produces: `class AdapterConformance` — a mixin; each adapter's test module subclasses it and supplies `adapter()`, `fixture_dir`, and `effort_reject_case() -> tuple[Reviewer, str] | None` (None = full effort support, must then prove every canonical value maps). The mixin asserts, for the supplied adapter:
  1. `parse(garbage, contract)` for garbage in `{b"", b"{", b"\x00\xff" * 512, b"[]"}` × both contracts → `parse_ok=False`, never raises; `classify(rc, garbage, garbage)` for rc in `{0, 1, 127}` never raises;
  1b. a `*refuter_healthy*` fixture exists and `parse(..., REFUTER_CONTRACT)` on it yields `parse_ok=True` with `payload["verdicts"]` a non-empty list — every adapter must prove it can request AND parse the refuter shape, or Task 8 breaks only at runtime;
  2. every `*healthy*` fixture → `classify(0, ...).kind == "ok"` and `parse(...).parse_ok is True`;
  3. every `*degraded*` fixture → `degraded` from parse or `classify` — and ≥ 2 such fixtures exist;
  4. every `*unavailable*` fixture → `classify(...).kind == "unavailable"` with a non-empty `category` — and ≥ 1 exists, plus the rc-127 → `binary` case;
  5. the effort contract: either one loud `ValueError` case or a total mapping over `config.EFFORTS`;
  6. `degraded` is never triggered by finding-text content: a healthy envelope whose finding titles contain the adapter's own stderr signal words still classifies `ok`;
  7. **usable output wins over stderr noise**: a healthy, schema-valid envelope on stdout accompanied by stderr containing the adapter's own auth/quota signal words still classifies `ok` — `unavailable` means the provider could not serve, and it demonstrably did. (This generalizes grok's Phase 1 auth-noise non-signal rule to every adapter; each supplies a `*healthy_noisy_stderr*` fixture.)
- **The registry is the gate, mechanically:** a registry-parameterized test (`test_every_registered_adapter_has_conformance_coverage`) asserts that for every provider in `_REGISTRY` there exists a collected `AdapterConformance` subclass bound to that provider (discoverable via a `provider_id` class attr on each subclass). Registering an adapter without a conformance subclass fails CI by construction, not by convention.
- Fixture file format: first line `rc=<int>`, then `--- stdout ---` / `--- stderr ---` sections, raw bytes, UTF-8. A tiny loader in the mixin parses it.
- **Grok fixtures are captured from real archived envelopes**, not synthesized: the legacy archive at `Path(SKODUN_ORACLE_DIR) / ".grok-reviews"` (the same location the existing parity infrastructure and `test_real_archive_smoke` use) holds real `<id>.grok.txt` stdout envelopes from live runs — copy one healthy and one degraded (`stopReason: Cancelled`) envelope, sanitize (strip any repo paths/branch names from summaries and finding text, keep the structure byte-faithful), and commit. The stderr-signal and auth fixtures may be synthesized where the archive holds no stderr captures — note which fixtures are captured vs synthesized in a `fixtures/adapters/xai/README` line each. (Global Constraints' capture rule applies in full to the three NEW adapters, whose probe steps produce their fixtures.)

- [ ] **Step 1: Write the mixin + registry-coverage test + grok subclass; run to verify FAIL** (missing fixtures/classify cases fail loudly).
- [ ] **Step 2: Create the four xai fixtures** per the capture rule above (healthy + Cancelled from the archive; `tool_error` stderr and auth-fatal + rc 1 synthesized and labeled).
- [ ] **Step 3: Run to verify PASS** — `python3 -m pytest tests/test_adapter_grok.py -v` then full suite.
- [ ] **Step 4: Commit** — `git commit -am "test: adapter conformance suite; grok is the first conforming adapter (refs EPIC)"`

---

### Task 3: Config — key removal + fallback chains

**Files:**
- Modify: `src/skodun/config.py`, `tests/test_config.py`, `examples/scala-angular-monorepo.toml` (drop the removed keys if present)

**Interfaces:**
- `Defaults` loses `severity_gate` and `confidence_threshold` (fields, minimums entry, docstrings). A config still setting either raises `ValueError` with the migration message: `"[defaults] severity_gate was removed in Phase 2: the gate blocks on any open finding by design — delete the key"` (same shape for `confidence_threshold`). This must fire from the *removed-keys check*, not the generic unknown-key error — the generic message would read as a typo, not a decision.
- `Reviewer` gains `fallbacks: tuple[str, ...] = ()`. Validation (in `load_config`, after all reviewers merge): every referenced name exists **after merging**, is `enabled`, is not the reviewer itself, no duplicates, chain length ≤ 3, and no cycles across chains (walk each chain transitively; a chain member's own `fallbacks` are NOT followed at runtime — document that runtime uses only the head reviewer's list — but cycle validation still rejects mutual references to keep configs comprehensible).
- Existing-test impact, precisely: `tests/test_gate.py::test_severity_gate_high_still_blocks_on_a_low_finding` is rewritten to pin the same behavior (any open finding blocks) without setting the removed key; `tests/test_config.py` has **no dedicated pin test** for these keys — they participate in the generic numeric-field parametrizations (`_DEFAULTS_MINIMUMS` coverage), so removing `confidence_threshold` from `Defaults` and `_DEFAULTS_MINIMUMS` updates those parametrized cases as a side effect. Add two new targeted migration-message tests (one per removed key).

- [ ] **Step 1: Failing tests** — removal message for each key (exact-text match on the "was removed in Phase 2" phrase); `fallbacks` happy path; each invalid shape (unknown name, self-reference, disabled target, cycle, length 4) raises naming the reviewer and the problem.

```python
def test_removed_keys_get_migration_message(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text("[defaults]\nseverity_gate = 'high'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="removed in Phase 2"):
        load_config(None, global_path=p)

def test_fallback_chain_validated(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text("""
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["backup"]
[[reviewers]]
name = "backup"
provider = "openai"
model = "n"
""", encoding="utf-8")
    cfg = load_config(None, global_path=p)
    assert cfg.reviewers[0].fallbacks == ("backup",)

def test_fallback_cycle_rejected(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text("""
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
fallbacks = ["b"]
[[reviewers]]
name = "b"
provider = "openai"
model = "n"
fallbacks = ["a"]
""", encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        load_config(None, global_path=p)
```

- [ ] **Step 2: Run to verify FAIL** → **Step 3: Implement** (`_REMOVED_DEFAULTS = {"severity_gate": "...msg...", ...}` checked before the unknown-keys check; `_validate_fallbacks(reviewers)` helper) → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: fallback chains in config; severity_gate/confidence_threshold removed with migration errors (refs EPIC)"`

---

### Task 4: Store — migration runner + provider_state

**Files:**
- Modify: `src/skodun/store.py`, `tests/test_store.py`

**Interfaces:**
- `Store.open` runs a migration ladder keyed on `PRAGMA user_version`. **Order is load-bearing:** (1) read `user_version`; (2) if it exceeds the code's `SCHEMA_VERSION = 2`, raise `ValueError("store schema v{n} is newer than this skodun")` **before any DDL executes** — never touch a future schema; (3) apply the ordered migration deltas above the current version (v0→2: create `provider_state`; the Phase 1 tables already match, so nothing else changes); (4) stamp `user_version = 2`. The idempotent `executescript(_SCHEMA)` runs only after the future-version check.
- New table: `provider_state(provider TEXT PRIMARY KEY, unavailable_until TEXT, reason TEXT, category TEXT, recorded_at TEXT)`.
- New API: `mark_provider_unavailable(provider, reason, category, until_iso)`; `provider_unavailable_reason(provider, now_iso, env=os.environ) -> str | None` — returns the reason only when `now < unavailable_until` and `SKODUN_IGNORE_PROVIDER_STATE` is unset/`"0"`; expired or bypassed rows return None; `provider_state_rows(now_iso) -> list[dict]` — `{provider, unavailable_until, reason, category, active: bool}` for every row, for `skodun providers` (Task 11). Writes are atomic (single UPSERT).

- [ ] **Step 1: Failing tests** — fresh DB lands at version 2; a v0 Phase-1-shaped DB opens and stamps 2 with rows intact; future version raises; provider_state honors TTL and env bypass.

```python
# PHASE1_SCHEMA below is the Phase 1 _SCHEMA DDL copied VERBATIM into the test
# (a true v0 database, not "new schema with the version reset") — the migration
# must prove it creates provider_state on a DB that has never had it.
def test_migration_from_true_phase1_db(tmp_path):
    import sqlite3, json
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.executescript(PHASE1_SCHEMA)                  # verbatim Phase 1 DDL, v0
    raw.execute("INSERT INTO reviews (id, diff_hash, trustworthy, artifact_json)"
                " VALUES (?, ?, 1, ?)",
                ("r1", "d" * 40, json.dumps({"id": "r1", "summary": "ok"})))
    raw.commit(); raw.close()
    st = Store.open(db)
    assert st._c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert st.get_review("r1")["summary"] == "ok"     # rows preserved
    st.mark_provider_unavailable("openai", "quota", "quota",
                                 "2026-07-28T12:00:00Z")  # new table exists

def test_future_schema_refused_before_any_ddl(tmp_path):
    import sqlite3
    db = tmp_path / "s.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99"); raw.commit(); raw.close()
    with pytest.raises(ValueError, match="newer"):
        Store.open(db)
    raw = sqlite3.connect(db)                         # and it really ran no DDL:
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "reviews" not in tables and "provider_state" not in tables

def test_provider_state_ttl_bypass_and_rows(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "rate limited", "quota",
                                 "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z",
                                          env={}) == "rate limited"
    assert st.provider_unavailable_reason("openai", "2026-07-28T13:00:00Z",
                                          env={}) is None
    assert st.provider_unavailable_reason(
        "openai", "2026-07-28T11:00:00Z",
        env={"SKODUN_IGNORE_PROVIDER_STATE": "1"}) is None
    rows = st.provider_state_rows("2026-07-28T11:00:00Z")
    assert rows[0]["active"] is True and rows[0]["category"] == "quota"
```

- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: store migration runner and provider_state cache (refs EPIC)"`

---

### Task 5: Codex adapter (provider "openai")

**Files:**
- Create: `src/skodun/adapters/codex.py`, `tests/test_adapter_codex.py`, `tests/fixtures/adapters/openai/*`
- Modify: `src/skodun/adapters/__init__.py` (register `"openai": CodexAdapter`)

**Interfaces:**
- `CodexAdapter.name = "codex"`, `provider = "openai"`. Binary resolution: `SKODUN_CODEX_BIN` → `codex` on PATH.
- **Step 0 (probe, before any code):** run the installed CLI headlessly against a trivial prompt and capture real envelopes to fixtures — a healthy run, a nonexistent-model run, and (if reproducible) an auth-failed run. Command shape to start from (verify every flag against `codex exec --help` — do NOT trust this plan over the binary):
  `codex exec - --json --output-schema <schema.json> -s read-only -m <model> -c model_reasoning_effort=<v> --skip-git-repo-check --ephemeral < prompt.txt`
  Record in the task's commit message which flags the installed version actually accepted.
- `build_cmd`: shell interpolation of the prompt is forbidden, always. If the probe shows the installed CLI has an input-file flag, use it; otherwise set the class attr `stdin_from_prompt_file = True` (now part of the Task 1 protocol) and return argv ending in the CLI's stdin marker (`[bin, "exec", "-", ...flags]`) — Task 7's runner change feeds the prompt file as the child's stdin. Decide from the probe; document the choice in the adapter docstring.
- Binary: `resolve_binary()` per the Task 1 convention — `SKODUN_CODEX_BIN` → `codex` on PATH.
- `effort_map()`: `{"none": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "xhigh"}` — total, no rejection case (pass `effort_reject_case() -> None` and prove totality).
- `parse(stdout, stderr, contract)`: stdout is a JSONL event stream; scan for the final agent-message item (`item.completed` with the message payload, or the `-o`-style last message if the probe shows a simpler shape), then validate via `contract.validate` and the shared `base` helpers (moved there in Task 1). The `--output-schema` file content is `contract.json_schema`: **`build_cmd` itself is responsible for writing it** (UTF-8, always overwritten) to `prompt_file.with_suffix(".schema.json")` and referencing that path in the returned argv — the caller creates no schema file. The pipeline owns the prompt file's directory, so the sidecar needs no extra cleanup. Document this in the adapter docstring.
- `classify -> ClassifyResult`: **usable terminal output wins** — when the stream carries a schema-valid final message, classify `ok` regardless of stderr noise (conformance rule 7; grok's Phase 1 auth-noise rule, generalized). Otherwise: `unavailable` on rc 127 (`binary`), stderr auth/login signals (`login`, `unauthorized`, `401`) (`auth`), quota signals (`rate limit`, `quota`, `429`) (`quota`), unknown-model errors (`model`); `degraded` on a stream with events but **no** terminal turn-completion event, or an explicit stream-error event; else `ok`. All matched case-insensitively on stderr and on event `type` fields — never on message text content (conformance rule 6).

- [ ] **Step 1: probe + fixtures** (live, one-time; sanitize) — then failing conformance subclass + unit tests for the event-stream parse (healthy fixture parses; stream missing turn-completion → degraded; auth fixture → unavailable).
- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: codex adapter (openai) with captured-envelope fixtures (refs EPIC)"`

---

### Task 6: Claude adapter (provider "anthropic")

**Files:**
- Create: `src/skodun/adapters/claude.py`, `tests/test_adapter_claude.py`, `tests/fixtures/adapters/anthropic/*`
- Modify: `src/skodun/adapters/__init__.py` (register `"anthropic": ClaudeAdapter`)

**Interfaces:**
- Binary: `resolve_binary()` — `SKODUN_CLAUDE_BIN` → `claude` on PATH. **Step 0 probe** as in Task 5; starting shape (verify against `claude --help`):
  `claude -p --output-format json --json-schema <schema> --model <m> --effort <e> --tools "" --bare --no-session-persistence`
  The review prompt goes through `--input-file`-equivalent if available, else the stdin route from Task 5's resolution (same mechanism, same pipeline support).
- `effort_map()`: `{"low": "low", "medium": "medium", "high": "high", "max": "max"}`; canonical `"none"` **loudly rejected** (`effort_reject_case()` returns it).
- `max_cost_usd`: when `Reviewer.max_cost_usd` is set, append `--max-budget-usd <v>`; this is the first adapter to consume that Phase 1 field. Because consumption starts here, **validation starts here too**: extend `config._validate` to require `max_cost_usd`, when present, to be a finite positive number and not a `bool` (TOML happily supplies `true`, `-1`, `nan` — each must be a loud ValueError naming the reviewer, with tests in `tests/test_config.py`). Add the flag to `build_cmd` tests.
- `parse`: single JSON envelope on stdout (`{"type": "result", ...}` shape per probe); payload from its structured-output/result field via the shared `base` helpers; tolerate prose around it with the raw-decoder scan.
- `classify`: **usable terminal output wins** — a schema-valid envelope on stdout classifies `ok` regardless of stderr noise (conformance rule 7), unless the envelope itself reports a fatal provider error. Otherwise: `unavailable` on rc 127, auth/credit signals (`credit`, `billing`, `login`, `401`), model-not-found; `degraded` on `is_error: true` / error subtypes (`error_max_turns`, `error_during_execution`), and on truncated envelopes; else `ok`.

- [ ] Steps 1–5 as Task 5. Commit: `git commit -am "feat: claude adapter (anthropic) with budget cap support (refs EPIC)"`

---

### Task 7: Pipeline — stdin support, classification loop, fallback chains

**Files:**
- Modify: `src/skodun/pipeline.py`, `src/skodun/runner.py`, `tests/test_pipeline.py`, `tests/test_runner.py`; Create: `tests/test_fallback.py`

**Interfaces:**
- `runner.run_with_watchdog` gains `stdin_path: Path | None = None` — opened read-only (`encoding` irrelevant, binary fd) as the child's stdin when set, `DEVNULL` otherwise; the fd is closed in all paths. `tests/test_runner.py` gains: stdin content reaches the child; default remains `DEVNULL` (child reading stdin gets EOF immediately).
- `_run_reviewer` becomes chain-aware: `_run_chain(head: Reviewer, cfg, d, prompt, cwd, store, scratch: Path, tag: str, contract=REVIEW_CONTRACT) -> _Outcome` — `scratch`/`tag` keep the shipped `_run_reviewer` contract (it creates prompt/stdout/stderr files from them); per-entry, per-attempt filenames extend the tag: `f"{tag}.{entry.name}.{n}"`, collision-free across chain entries, and the codex/claude schema sidecars (Task 5) land in the same directory. Chain behavior:
  1. Build the ordered list `[head] + [reviewer-by-name for each head.fallbacks]` (names validated at load).
  2. **Preflight extends to the whole graph:** the existing resolve-every-adapter-before-locking step now traverses every configured head *and* fallback entry for the finder and every extra-pass role — an unknown provider anywhere refuses the run before the lock, before any record, before any model call (test below).
  3. For each entry: if `store.provider_unavailable_reason(entry.provider, now)` → record a synthetic attempt `{n, provider, model, effort, skipped: "provider marked unavailable: <reason>"}` and continue to the next entry.
  4. Otherwise run attempts within the entry. **Every completed attempt is classified before its output is considered:** `classify(rc, stdout, stderr)` first — `kind == "unavailable"` → stop this entry immediately (no retries against a dead provider) and advance the chain, after `store.mark_provider_unavailable(provider, reason, category, now + TTL)` **only when `category == "quota"`** (TTL 30 min, `PROVIDER_UNAVAILABLE_TTL_SEC = 1800`; auth/binary/model failures are attempt-local — caching them would let one bad model id black-hole a whole provider); `kind == "degraded"` → consumes the entry's degraded-retry budget exactly like a `ParseResult.degraded` (the two degraded signals are OR-ed — a truncated codex stream must not bypass the retry that a truncated grok envelope gets); only `kind == "ok"` may proceed to `parse` and acceptance. Timeouts keep their own retry budget as in Phase 1.
  5. A degraded/failed entry (retries exhausted, not unavailable) → **stop the chain** and return the failure — a provider that *answered badly* is not a quota event, and hopping providers on a degraded answer would mask real harness bugs.
  6. Chain exhausted (every entry unavailable/skipped) → `_Outcome(None, attempts, "all providers unavailable: <per-entry summary>")` → `failed` record, `trustworthy=false` (existing machinery), banner, exit 4. Never a pass. **Gate interaction, stated precisely:** the failed record does not erase older coverage — if a trustworthy review of the *same diff_hash* already exists **and it passes the gate's existing artifact checks (`base_sha` match included — a rebase still invalidates it)**, the gate keeps answering from it (the diff-identity invariant: identical bytes at the same base remain covered). For content with no prior trustworthy coverage — the acceptance drill's case — the gate answers 2. Both directions pinned in `test_fallback.py`; the seeded-coverage test seeds with the CURRENT base_sha so it exercises the pass path, plus a rebased-base variant asserting 2.
- `_Outcome` gains `accepted: dict | None` = `{adapter_name, provider, model, effort}` of the attempt whose payload was accepted; `run_review` updates the record's indexed `adapter`/`model` fields from it (**adapter NAME, e.g. `"codex"`, in the `adapter` column — provider ids like `"openai"` live in `attempts[]`/provenance**, per the spec). The record is still *initialized* with the finder's identity; the post-run update is what makes the columns mean "the accepted attempt".
- Every `extra_passes.<name>` object (security and skeptic included — retrofit) gains `{provider, model, effort}` provenance, defined precisely: the ACCEPTED attempt's triplet when one exists; else the TERMINAL attempt's (the last that actually executed); else — every entry cache-skipped, nothing ran — explicit `null`s plus a `note` naming why. Per-attempt provenance in `attempts[]` always carries the full chain either way.
- **Runtime budgets scale with the chain (lock safety):** `worst_runtime_sec` and the lock stale ceiling currently budget one reviewer per pass; a chain can run up to 4 entries (head + ≤3 fallbacks) per pass. Both calculations multiply by the *configured* maximum chain width across all roles (`max(1 + len(r.fallbacks))`), pinned by a test asserting the ceiling for a 4-entry chain config is ≥ 4× the single-entry ceiling. Without this, a waiting foreground lock could reclaim a live long chain and run two reviews concurrently against one inference backend.

- [ ] **Step 1: Failing tests** — with fake binaries on PATH:

Binary control in tests uses the per-adapter env overrides, never PATH tricks (grok prefers `~/.grok/bin/grok` over PATH, so a PATH fake is unreliable): `monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/dead")`, `monkeypatch.setenv("SKODUN_GROK_BIN", str(fake_ok))`, etc.

```python
def test_fallback_chain_recovers(tmp_path, monkeypatch, repo, store):
    # head reviewer: openai with a dead binary; fallback: xai with a fake that succeeds
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/skodun-dead")
    monkeypatch.setenv("SKODUN_GROK_BIN", str(_fake_cli(tmp_path, HEALTHY_ENVELOPE)))
    rec = run_review(repo, CFG_OPENAI_THEN_XAI, store)
    assert rec["trustworthy"] is True
    kinds = [(a.get("provider"), "skipped" in a or a.get("rc") == 127)
             for a in rec["attempts"]]
    assert kinds[0][0] == "openai" and kinds[0][1]          # unavailable recorded
    assert rec["adapter"] == "grok" and rec["model"] == FAKE_XAI_MODEL
    # exact accepted identity, not a negative assertion

def test_exhausted_chain_fails_closed_and_gate_semantics(tmp_path, monkeypatch, repo, store):
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/a")
    monkeypatch.setenv("SKODUN_GROK_BIN", "/nonexistent/b")
    rec = run_review(repo, CFG_OPENAI_THEN_XAI, store)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "unavailable" in rec["failure_reason"]
    assert run_gate(store, repo, CFG_OPENAI_THEN_XAI).code == 2   # no prior coverage
    # and the invariant direction: identical content with OLDER trustworthy
    # coverage stays covered — a quota outage cannot un-review reviewed bytes
    _seed_trustworthy_review_for_current_diff(store, repo)
    assert run_gate(store, repo, CFG_OPENAI_THEN_XAI).code == 0

def test_degraded_does_not_hop_providers(tmp_path, monkeypatch, repo, store):
    monkeypatch.setenv("SKODUN_GROK_BIN",
                       str(_fake_cli(tmp_path, CANCELLED_ENVELOPE)))
    rec = run_review(repo, CFG_XAI_THEN_OPENAI, store)
    assert all(a["provider"] == "xai" for a in rec["attempts"])   # never advanced
    assert rec["trustworthy"] is False

def test_provider_state_skips_known_dead_provider_when_quota(tmp_path, repo, store):
    store.mark_provider_unavailable("openai", "rate limited", "quota", FUTURE_ISO)
    rec = run_review(repo, CFG_OPENAI_THEN_XAI, store)
    assert rec["attempts"][0]["skipped"].startswith("provider marked unavailable")
    assert rec["attempts"][0]["effort"] is not None or "effort" in rec["attempts"][0]

def test_non_quota_unavailability_is_not_cached(tmp_path, monkeypatch, repo, store):
    monkeypatch.setenv("SKODUN_CODEX_BIN", "/nonexistent/a")      # binary, not quota
    run_review(repo, CFG_OPENAI_THEN_XAI_WITH_FAKE_XAI, store)
    assert store.provider_unavailable_reason("openai", NOW_ISO, env={}) is None

def test_unknown_fallback_provider_refused_in_preflight(tmp_path, repo, store):
    rec_count_before = len(store.list_reviews(None, 1000))
    with pytest.raises(SystemExit) as e:   # via CLI path; or PreflightRefused via API
        _cli_review(repo, CFG_WITH_UNKNOWN_FALLBACK_PROVIDER)
    assert len(store.list_reviews(None, 1000)) == rec_count_before  # nothing ran

def test_lock_ceiling_scales_with_chain_width():
    d = Defaults()
    single = worst_runtime_sec(d, max_chain_width=1)
    assert worst_runtime_sec(d, max_chain_width=4) >= 4 * single
```

(Fake CLI helpers extend Phase 1's `_fake_grok`; each fake logs invocations. Config constants — `CFG_OPENAI_THEN_XAI` etc. — are module-level TOML snippets with the chain under test.)

- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: quota fallback chains with fail-closed exhaustion (refs EPIC)"`

---

### Task 8: Refuter pass

**Files:**
- Modify: `src/skodun/passes.py`, `src/skodun/pipeline.py`, `tests/test_passes.py`; Create: `tests/test_refuter.py`

**Interfaces:**
- **Eligibility and inputs come from a finder snapshot, not the merged record.** The pipeline snapshots `(finder_trustworthy, finder_findings)` immediately after the finder's parse, *before* any security/skeptic merge: security/skeptic findings must not trigger a refuter the finder didn't earn, a security demotion must not suppress one the finder did earn, and verdict indexes refer to the finder's own numbering. (Finder findings keep indexes `0..n-1` in the merged list because extra-pass merges append — pinned by a test.)
- `should_run_refuter(mode, finder_trustworthy, finder_findings_total, cfg, env) -> bool` — `mode == "now"`, finder trustworthy, `finder_findings_total > 0`, a reviewer with role `refuter` is configured and enabled (no refuter configured = pass silently skipped with a note, not an error), kill switch `SKODUN_REFUTER_PASS=0`.
- `refuter_prompt(finder_findings, diff: bytes, ...) -> bytes` — takes the diff BYTES like the existing security/skeptic prompt builders (`diff.data` is already in scope in the pipeline; no diff file exists and none is introduced): generic, slot-free, presents the diff plus the finder's findings as a numbered list and instructs adversarial re-examination. The response contract is `base.REFUTER_CONTRACT` (defined in Task 1); the refuter runs through `_run_chain(..., contract=REFUTER_CONTRACT)`, so every adapter can request and validate the verdicts shape, and the refuter gets fallback support for free.
- `merge_refuter_pass(primary, refuter_result, provenance) -> dict` — **annotation only**: for each valid verdict whose `index` is within the finder snapshot's range, set `primary["findings"][i]["refuter"] = {"verdict", "reasoning", "provider", "model"}`. Out-of-range/duplicate indexes are dropped with a note. **Reasoning floor at merge:** a verdict whose reasoning, measured by the SAME collapse `triage.validate_reason` uses (import its collapse helper; do NOT use `textnorm.norm`, which lowercases/casefolds and can change length), is under `MIN_REASON_CHARS` is stored with `"thin_reasoning": true` — annotation kept for the human, adoption later refused. Counts, severity, trust axes untouched — pinned:

```python
def test_refuter_never_touches_trust_or_counts():
    out = merge_refuter_pass(_primary_with_findings(2), REFUTER_OK, PROV)
    assert out["parse_ok"] is True and out["degraded"] is False
    assert out["findings_total"] == 2
    assert out["findings"][0]["refuter"]["verdict"] == "refuted"

def test_failed_refuter_is_a_note_not_a_demotion():
    out = merge_refuter_pass(_primary_with_findings(1), None, PROV)
    assert out["parse_ok"] is True                       # unlike security/skeptic
    assert out["extra_passes"]["refuter"]["status"] == "failed"

def test_gate_ignores_refuter_annotations(tmp_path):
    # a review whose only finding is marked "refuted" still gates 1
    ...
    assert run_gate(store, repo, cfg).code == 1
```

- Pipeline wiring: the refuter *executes* after security/skeptic merges (so the published record is complete in one write) but its eligibility, prompt content, and index mapping use the finder snapshot per above; reviewer selected by role `refuter` (existing `_reviewer_for`); no fail-closed hold: refuter failure never demotes trust — though as a synchronous pass it does extend wall-clock before the single final write (an async post-publication design was considered and rejected for Phase 2: one write, one banner). `extra_passes.refuter` records `{provider, model, effort, status, note}`.

- [ ] Steps: failing tests → FAIL → implement → PASS + full suite → Commit `git commit -am "feat: cross-provider refuter pass, annotation-only by design (refs EPIC)"`

---

### Task 9: Triage — surface annotations + adopt-refuter

**Files:**
- Modify: `src/skodun/triage.py`, `src/skodun/cli.py`, `tests/test_triage.py`, `tests/test_cli.py`

**Interfaces:**
- `triage --list <id>`: findings with a `refuter` annotation show one extra line: `refuter(<provider>/<model>): <verdict> — <reasoning first 120 chars>`.
- `skodun triage --adopt-refuter <review-id> <finding-index>`: requires the finding to carry `refuter.verdict == "refuted"` (adopting a `confirmed` or `uncertain` verdict is an error naming the verdict) and not `thin_reasoning`. **Validation happens twice, deliberately:** first the RAW reasoning alone must pass `validate_reason` (the `refuter(provider/model): ` prefix must never be what pushes a one-word reasoning over the 20-char floor — that would launder thin reasoning through attribution), then the synthesized `refuter(<provider>/<model>): <reasoning>` string is validated and persisted through the existing `dismiss` path. Exit codes: 0 recorded, 1 refused (validation/verdict/thin), 2 review/finding not found.
- This is an explicit per-finding action; there is deliberately no `--adopt-all`.

- [ ] Steps: failing tests (adopt happy path flips `open_findings` empty; adopting `confirmed` refused; thin reasoning refused by `validate_reason`; missing annotation refused) → FAIL → implement → PASS → Commit `git commit -am "feat: refuter annotations in triage, explicit adopt-refuter dismissal (refs EPIC)"`

---

### Task 10: Agy adapter (provider "google") — CONTINGENT

**Files:**
- Create: `src/skodun/adapters/agy.py`, `tests/test_adapter_agy.py`, `tests/fixtures/adapters/google/*`; Modify: `adapters/__init__.py`

**Interfaces:** same contract as Tasks 5–6; binary `SKODUN_AGY_BIN` → `agy`. **Step 0 probe decides the task's fate:** if the installed CLI offers a usable headless prompt-in/JSON-out mode, implement exactly as the codex/claude pattern (effort mapping decided from the probe, loud-reject anything unmappable). If it does not — no headless mode, no parseable output, or auth cannot run non-interactively — **skip the task**: commit a `docs/adapters-agy-status.md` recording the probe transcript, the blocking gap, and the re-evaluation trigger, and remove `google` from the epic's acceptance wording in the same commit. The skip is a first-class outcome, not a failure; what is not acceptable is a half-adapter that cannot pass conformance.

- [ ] Steps: probe → decide → (implement + conformance) or (document skip) → Commit.

---

### Task 11: CLI honesty + providers listing

**Files:**
- Modify: `src/skodun/cli.py`, `tests/test_cli.py`

**Interfaces:**
- `KeyboardInterrupt` honesty needs BOTH layers changed, because `_cmd_review` itself catches `BaseException` at several points (imports/store-open/config-load wrappers and around `run_review`) and would convert Ctrl-C to 2 or 4 before `main()` ever sees it: add `except KeyboardInterrupt: raise` immediately before **every** `BaseException` handler inside `_cmd_review` (and only there), then scope `main()`'s carve-out to the parsed `review` dispatch → exit **130**. The pipeline `finally` has already downgraded the record and released the lock — pin with a test that launches skodun ITSELF as a subprocess (`sys.executable -m skodun review ...` with a fake slow CLI) and sends SIGINT to the skodun process — NOT to the provider child's group: `run_with_watchdog` starts the child with `start_new_session=True`, so signalling the fake CLI's group would never reach the skodun parent and the test would prove nothing. Assert: skodun exits 130, the record's status is `failed`, the foreground lock is gone, and the isolated provider group is dead. `_cmd_gate` keeps mapping every exception, `KeyboardInterrupt` included, to 2 (pinned).
- `skodun providers`: for each registered adapter — provider id, adapter name, `resolve_binary()` result + whether it exists/executable (or `NOT FOUND`), and the row from `store.provider_state_rows(now)` (active/until/reason/category). Read-only, exit 0 even when binaries are missing (it is a diagnostic listing, not a gate) — but exit 1 when a *configured* reviewer references a provider with no registered adapter (that is a config error worth failing loudly in CI).

- [ ] Steps: failing tests → FAIL → implement → PASS → Commit `git commit -am "feat: skodun providers listing; Ctrl-C exits 130 from review only (refs EPIC)"`

---

### Task 12: shadow-compare --since

**Files:**
- Modify: `src/skodun/shadow.py`, `src/skodun/cli.py`, `tests/test_shadow.py`, `docs/shadow-mode.md`

**Interfaces:** `compare(..., since: str | None) -> CompareResult(comparisons: list[Comparison], excluded_unparseable: int)` — a result object, because the CLI summary must print the excluded count and duplicating the filter in `cli.py` would be a second implementation of the window; update every existing caller of `compare` (cli, tests) to unpack it. `since` must match the store's own canonical timestamp format **exactly**: `%Y-%m-%dT%H:%M:%SZ` (UTC). Anything else — offsets like `+02:00`, date-only, prose — is a usage error (exit 2, message naming the required format). With that constraint, plain lexicographic comparison against stored `reviewed_at` values is correct. Rows whose stored `reviewed_at` is missing or does not match the canonical format are **excluded from a windowed compare and counted** in the summary (`n unparseable-timestamp rows excluded`) — never crashed on, never silently included. Summary line gains `since=<value>`. Docs: one paragraph replacing the point-in-time caveat with the windowed usage.

- [ ] Steps: failing tests (a legacy-only row older than `since` disappears; `--since 2026-07-28T00:00:00+02:00` is a usage error; a malformed stored timestamp is excluded and counted) → implement → PASS → Commit `git commit -am "feat: shadow-compare --since window (refs EPIC)"`

---

### Task 13: Docs + examples

**Files:**
- Modify: `README.md` (multi-provider status, new commands, config example with a fallback chain + refuter), `examples/scala-angular-monorepo.toml`; Create: `examples/multi-provider.toml` (commented: xai finder with openai fallback, openai refuter, anthropic security with `max_cost_usd`)

- [ ] Steps: write → self-check every documented flag against `--help` output of the shipped CLI (`python3 -m skodun --help` etc.) → Commit `git commit -am "docs: multi-provider README and example configs (refs EPIC)"`

---

### Task 14: Live acceptance runbook

**Files:**
- Create: `docs/phase2-acceptance.md` (procedure + evidence log)

Procedure (run on a real repository with real outgoing changes; store copies for anything destructive):

1. **Cross-provider run:** finder = grok, refuter = codex (or claude). `skodun review` on a change-set with ≥ 1 real finding → `triage --list` shows the annotation with provider attribution → `--adopt-refuter` on one refuted finding → `skodun gate` flips 1→0 → artifact JSON shows per-pass `{provider, model, effort}`.
2. **Fallback drill (two parts, because cacheability is category-scoped):** (a) availability: reviewer with `SKODUN_<X>_BIN=/nonexistent` and a real fallback → trustworthy review, `attempts[]` shows the `binary` unavailable classification then the fallback provider; both entries dead → `failed` record, banner `trustworthy=false`, gate exit 2 (fresh content, no prior coverage). Dead binaries are `category=binary` and deliberately NOT cached. (b) cache: point a reviewer at a fake CLI script that replays a captured quota-failure envelope (`category=quota`) → the chain advances, `providers` shows the cached unavailable-until state, and `SKODUN_IGNORE_PROVIDER_STATE=1` bypasses it on the next run.
3. **No-regression:** whole-archive `shadow-compare` (and `--since` windowed) against the legacy archive; counts consistent with the Phase 1 log modulo documented classes.
4. Paste every command + output into the evidence log; each of the epic's acceptance criteria gets a ✅ with a pointer.

- [ ] Steps: write runbook → execute → paste evidence → full suite both modes one final time → Commit `git commit -am "docs: phase 2 live acceptance evidence (refs EPIC)"`

---

## Self-Review Notes

- Spec coverage: adapter contract + contracts/conformance (T1–T2), three adapters with real-fixture discipline (T5, T6, T10-contingent), fallback chains + provider_state + fail-closed exhaustion + budget scaling (T4, T7), refuter annotate-and-adopt on a finder snapshot (T8, T9), key removal (T3), CLI honesty + providers (T11), shadow window (T12), docs (T13), demonstrable acceptance (T14). Cuts and contingencies match the spec.
- Deliberate decisions restated: classification runs on **every** attempt (a truncated stream gets the same retry a truncated envelope does); only `unavailable` advances the chain, and only `category == "quota"` is cached provider-wide; indexed `adapter` column carries the adapter *name* of the accepted attempt (providers live in provenance); an exhausted chain's failed record never erases older coverage of identical bytes (diff-identity invariant), and gates 2 only where no trustworthy coverage exists; refuter eligibility/indexes come from the finder snapshot; refuter failure never demotes; adoption validates the raw reasoning before the attributed string; no `--adopt-all`.
- Open risk named plainly: Tasks 5/6/10 depend on the *installed* CLI versions' flags and envelope shapes — that is why each starts with a probe step and captured fixtures instead of trusting this plan's command sketches, and why agy carries an explicit skip path.
- Adversarially reviewed by codex (gpt-5.6-sol, high reasoning effort) against the shipped Phase 1 source; all 18 round-1 findings incorporated (incl. per-attempt classification, refuter output contract, chain-scaled lock budgets, gate-vs-exhaustion semantics, cacheability categories, preflight over the full chain graph, Ctrl-C handler layering, true-v0 migration testing).
