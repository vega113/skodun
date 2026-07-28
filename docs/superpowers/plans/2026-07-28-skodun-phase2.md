# skodun Phase 2 Implementation Plan — Multi-Provider, Refuter, Fallback Chains

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** skodun reviews through any of four provider CLIs (grok, codex, claude, agy), with a cross-provider refuter that annotates findings, and per-reviewer quota-fallback chains that fail closed when exhausted.

**Architecture:** The design is fixed by `docs/superpowers/specs/2026-07-28-skodun-phase2-design.md` (owner-approved) — read it first; this plan implements it and does not re-litigate it. Provider-neutral adapter contract in `adapters/base.py` + a conformance suite as the registration gate; fallback = fresh attempt by a different reviewer entry; refuter = annotation-only extra pass; store changes are additive (artifact JSON + one new table via a `user_version` migration runner).

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest only.

## Global Constraints

- Everything in the Phase 1 plan's Global Constraints still binds: fail-closed trust invariant (single definition, store-enforced strict-bool axes), gate 0/1/2 with every unexpected exception → 2, `encoding="utf-8"` everywhere, prompts/diffs travel as files, explicit model selection, public-repo hygiene (no machine paths, no upstream project names or repo-layout literals in `src/` — including prompt text), oracle located only via `SKODUN_ORACLE_DIR`.
- Phase 1 parity surfaces (diff identity, triage keys, prompt bytes) are untouched. This phase adds **no** oracle-ported code; all existing parity tests must remain green and unmodified (except the two `severity_gate` no-effect pin tests, which Task 3 replaces by design).
- Adapter fixtures are **captured from the real CLIs** during implementation (each adapter task starts with a probe step) and committed under `tests/fixtures/adapters/<provider>/` — sanitized: no tokens, no usernames, no machine paths inside fixture bytes.
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
- Produces: `base.ParseResult` (moved from `grok.py`, field-identical), `base.UNAVAILABLE_RC = 127`,
  `Classification = Literal["ok", "degraded", "unavailable"]`,
  `class Adapter(Protocol)` with `name: str`, `provider: str`,
  `build_cmd(prompt_file, r: Reviewer, d: Defaults, cwd) -> list[str]`,
  `parse(stdout: bytes, stderr: bytes) -> ParseResult`,
  `classify(rc: int, stdout: bytes, stderr: bytes) -> Classification`,
  `effort_map() -> dict[str, str]` (canonical effort → CLI value; a canonical value absent from the map is a **loud** `ValueError` in `build_cmd`).
- `grok.py` imports `ParseResult` from `base` and re-exports it (`from .base import ParseResult`) so existing imports keep working; `adapters/__init__` re-exports `ParseResult`, `Classification`, `Adapter`, `get_adapter`.
- Semantics (from the spec, binding): `degraded` = positive evidence the harness truncated/corrupted this run's output; `unavailable` = the provider could not serve at all (quota, auth, unknown model id, binary missing → rc 127). `classify` never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_base.py
from skodun.adapters import Adapter, Classification, ParseResult, get_adapter
from skodun.adapters.base import UNAVAILABLE_RC

def test_parse_result_importable_from_base_and_grok():
    from skodun.adapters.base import ParseResult as base_pr
    from skodun.adapters.grok import ParseResult as grok_pr
    assert base_pr is grok_pr          # one class, re-exported — not a copy

def test_grok_adapter_satisfies_protocol():
    a = get_adapter("xai")
    assert a.provider == "xai"
    assert callable(a.classify) and callable(a.effort_map)

def test_rc_127_is_unavailable_for_every_registered_adapter():
    from skodun.adapters import _REGISTRY
    for cls in _REGISTRY.values():
        assert cls().classify(UNAVAILABLE_RC, b"", b"command not found") == "unavailable"
```

- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/test_adapter_base.py -v`
- [ ] **Step 3: Implement** — create `base.py` with the dataclass moved verbatim plus:

```python
# src/skodun/adapters/base.py (excerpt — full docstrings in the style of adapters/__init__)
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from ..config import Defaults, Reviewer

Classification = Literal["ok", "degraded", "unavailable"]
UNAVAILABLE_RC = 127   # shell's command-not-found: the binary itself is absent

@dataclass(frozen=True)
class ParseResult:
    parse_ok: bool
    findings: list
    summary: str
    stop_reason: str | None
    degraded: bool
    degraded_reason: str

class Adapter(Protocol):
    name: str
    provider: str
    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path) -> list[str]: ...
    def parse(self, stdout: bytes, stderr: bytes) -> ParseResult: ...
    def classify(self, rc: int, stdout: bytes, stderr: bytes) -> Classification: ...
    def effort_map(self) -> dict[str, str]: ...
```

Give `GrokAdapter` `provider = "xai"`, an `effort_map()` returning its current pass-through table, and a `classify()` built from its existing `_detect_degraded` signals plus unavailability signals (rc 127; `authorizationrequired`-style auth-fatal stderr **only when stdout carried no usable envelope** — the Phase 1 non-signal rule stands when output is healthy; unknown-model-id stderr). Keep `_detect_degraded`'s behavior byte-for-byte (its tests must not change).

- [ ] **Step 4: Run to verify PASS** — plus full suite: `python3 -m pytest -q` (no regressions).
- [ ] **Step 5: Commit** — `git commit -am "feat: provider-neutral adapter contract in adapters/base (refs EPIC)"`

---

### Task 2: Conformance suite + grok retrofit

**Files:**
- Create: `tests/adapter_conformance.py`, `tests/fixtures/adapters/xai/{healthy.txt,degraded_stopreason.txt,degraded_stderr.txt,unavailable_auth.txt}`
- Modify: `tests/test_adapter_grok.py` (add the mixin subclass)

**Interfaces:**
- Produces: `class AdapterConformance` — a mixin; each adapter's test module subclasses it and supplies `adapter()`, `fixture_dir`, and `effort_reject_case() -> tuple[Reviewer, str] | None` (None = full effort support, must then prove every canonical value maps). The mixin asserts, for the supplied adapter:
  1. `parse(garbage)` for garbage in `{b"", b"{", b"\x00\xff" * 512, b"[]"}` → `parse_ok=False`, never raises;
  2. every `*healthy*` fixture → `classify(0, ...) == "ok"` and `parse(...).parse_ok is True`;
  3. every `*degraded*` fixture → `degraded` from parse or `classify` — and ≥ 2 such fixtures exist;
  4. every `*unavailable*` fixture → `classify(...) == "unavailable"` — and ≥ 1 exists, plus the rc-127 case;
  5. the effort contract: either one loud `ValueError` case or a total mapping over `config.EFFORTS`;
  6. `degraded` is never triggered by finding-text content: a healthy envelope whose finding titles contain the adapter's own stderr signal words still classifies `ok`.
- Fixture file format: first line `rc=<int>`, then `--- stdout ---` / `--- stderr ---` sections, raw bytes, UTF-8. A tiny loader in the mixin parses it.
- Grok fixtures are synthesized from the Phase 1 test envelopes (no live call needed — the shapes are already pinned by `test_adapter_grok.py`).

- [ ] **Step 1: Write the mixin + grok subclass; run to verify FAIL** (missing fixtures/classify cases fail loudly).
- [ ] **Step 2: Create the four xai fixtures** from the known envelope shapes (structuredOutput healthy; `stopReason: Cancelled`; `tool_error` stderr; auth-fatal stderr with empty stdout + rc 1).
- [ ] **Step 3: Run to verify PASS** — `python3 -m pytest tests/test_adapter_grok.py -v` then full suite.
- [ ] **Step 4: Commit** — `git commit -am "test: adapter conformance suite; grok is the first conforming adapter (refs EPIC)"`

---

### Task 3: Config — key removal + fallback chains

**Files:**
- Modify: `src/skodun/config.py`, `tests/test_config.py`, `examples/scala-angular-monorepo.toml` (drop the removed keys if present)

**Interfaces:**
- `Defaults` loses `severity_gate` and `confidence_threshold` (fields, minimums entry, docstrings). A config still setting either raises `ValueError` with the migration message: `"[defaults] severity_gate was removed in Phase 2: the gate blocks on any open finding by design — delete the key"` (same shape for `confidence_threshold`). This must fire from the *removed-keys check*, not the generic unknown-key error — the generic message would read as a typo, not a decision.
- `Reviewer` gains `fallbacks: tuple[str, ...] = ()`. Validation (in `load_config`, after all reviewers merge): every referenced name exists **after merging**, is `enabled`, is not the reviewer itself, no duplicates, chain length ≤ 3, and no cycles across chains (walk each chain transitively; a chain member's own `fallbacks` are NOT followed at runtime — document that runtime uses only the head reviewer's list — but cycle validation still rejects mutual references to keep configs comprehensible).
- The two Phase 1 no-effect pin tests (`test_severity_gate_high_still_blocks_on_a_low_finding` in `tests/test_gate.py` and its config sibling) are **replaced** by removal-message tests; the gate behavior they pinned (any open finding blocks) is re-pinned without the config key.

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
- `Store.open` runs a migration ladder keyed on `PRAGMA user_version`: version 0 (any existing Phase 1 DB — its tables already match, so migration is a no-op stamp) → 2. Each migration is a function in an ordered list; `executescript` of `_SCHEMA` stays (idempotent `IF NOT EXISTS`), the runner only applies deltas and stamps the version. Opening a DB with a **higher** version than the code knows raises (`"store schema v{n} is newer than this skodun"`) — never operate on a future schema.
- New table: `provider_state(provider TEXT PRIMARY KEY, unavailable_until TEXT, reason TEXT, recorded_at TEXT)`.
- New API: `mark_provider_unavailable(provider, reason, until_iso)`; `provider_unavailable_reason(provider, now_iso, env=os.environ) -> str | None` — returns the reason only when `now < unavailable_until` and `SKODUN_IGNORE_PROVIDER_STATE` is unset/`"0"`; expired or bypassed rows return None. Writes are atomic (single UPSERT).

- [ ] **Step 1: Failing tests** — fresh DB lands at version 2; a v0 Phase-1-shaped DB opens and stamps 2 with rows intact; future version raises; provider_state honors TTL and env bypass.

```python
def test_migration_stamps_and_preserves(tmp_path):
    db = tmp_path / "s.db"
    st = Store.open(db); st.save_review(REC)          # Phase-1-style record
    st._c.execute("PRAGMA user_version = 0")          # simulate pre-migration
    st2 = Store.open(db)
    assert st2._c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert st2.get_review("r1")["summary"] == "ok"

def test_future_schema_refused(tmp_path):
    db = tmp_path / "s.db"
    Store.open(db)._c.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="newer"):
        Store.open(db)

def test_provider_state_ttl_and_bypass(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.mark_provider_unavailable("openai", "quota", "2026-07-28T12:00:00Z")
    assert st.provider_unavailable_reason("openai", "2026-07-28T11:00:00Z", env={}) == "quota"
    assert st.provider_unavailable_reason("openai", "2026-07-28T13:00:00Z", env={}) is None
    assert st.provider_unavailable_reason(
        "openai", "2026-07-28T11:00:00Z",
        env={"SKODUN_IGNORE_PROVIDER_STATE": "1"}) is None
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
- `build_cmd`: prompt via file redirect is not available as a flag — codex takes the prompt as an argument or stdin; since the Adapter contract passes `prompt_file`, use `["bash", "-c", ...]` **never** — instead pass the literal argv `[bin, "exec", "-", ...flags]` and have the pipeline feed stdin? NO — the runner's contract is argv + files only, stdin is `DEVNULL`. Therefore pass the prompt as `codex exec "$(cat file)"`-style argv is also forbidden (shell interpolation). **Resolution: `build_cmd` returns `[bin, "exec", "--input-file", str(prompt_file), ...]` if the installed CLI supports an input-file flag; otherwise the adapter declares `stdin_from_prompt_file = True` and Task 7 (pipeline) honors it by opening the prompt file as the child's stdin.** The conformance suite does not care; the pipeline change is 3 lines and tested in Task 7. Decide from the probe; document the choice in the adapter docstring.
- `effort_map()`: `{"none": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "xhigh"}` — total, no rejection case (pass `effort_reject_case() -> None` and prove totality).
- `parse`: stdout is a JSONL event stream; scan for the final agent-message item (`item.completed` with the message payload, or the `-o`-style last message if the probe shows a simpler shape), then apply the same `_eligible`/`_valid_payload` discipline as grok (extract those two helpers into `base.py` as part of this task — they are provider-neutral payload rules, and both grok and codex must import them from one place).
- `classify`: `unavailable` on rc 127, on stderr auth/login signals (`login`, `unauthorized`, `401`), quota signals (`rate limit`, `quota`), unknown-model errors; `degraded` on a stream with events but **no** terminal turn-completion event, or an explicit stream-error event; else `ok`. All matched case-insensitively on stderr and on event `type` fields — never on message text content (conformance rule 6).

- [ ] **Step 1: probe + fixtures** (live, one-time; sanitize) — then failing conformance subclass + unit tests for the event-stream parse (healthy fixture parses; stream missing turn-completion → degraded; auth fixture → unavailable).
- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: codex adapter (openai) with captured-envelope fixtures (refs EPIC)"`

---

### Task 6: Claude adapter (provider "anthropic")

**Files:**
- Create: `src/skodun/adapters/claude.py`, `tests/test_adapter_claude.py`, `tests/fixtures/adapters/anthropic/*`
- Modify: `src/skodun/adapters/__init__.py` (register `"anthropic": ClaudeAdapter`)

**Interfaces:**
- Binary: `SKODUN_CLAUDE_BIN` → `claude` on PATH. **Step 0 probe** as in Task 5; starting shape (verify against `claude --help`):
  `claude -p --output-format json --json-schema <schema> --model <m> --effort <e> --tools "" --bare --no-session-persistence`
  The review prompt goes through `--input-file`-equivalent if available, else the stdin route from Task 5's resolution (same mechanism, same pipeline support).
- `effort_map()`: `{"low": "low", "medium": "medium", "high": "high", "max": "max"}`; canonical `"none"` **loudly rejected** (`effort_reject_case()` returns it).
- `max_cost_usd`: when `Reviewer.max_cost_usd` is set, append `--max-budget-usd <v>`; this is the first adapter to consume that Phase 1 field — add the flag to `build_cmd` tests.
- `parse`: single JSON envelope on stdout (`{"type": "result", ...}` shape per probe); payload from its structured-output/result field via the shared `base` helpers; tolerate prose around it with the raw-decoder scan.
- `classify`: `unavailable` on rc 127, auth/credit signals (`credit`, `billing`, `login`, `401`), model-not-found; `degraded` on `is_error: true` / error subtypes (`error_max_turns`, `error_during_execution`) when stdout still parses, and on truncated envelopes; else `ok`.

- [ ] Steps 1–5 as Task 5. Commit: `git commit -am "feat: claude adapter (anthropic) with budget cap support (refs EPIC)"`

---

### Task 7: Pipeline — stdin support, classification loop, fallback chains

**Files:**
- Modify: `src/skodun/pipeline.py`, `tests/test_pipeline.py`; Create: `tests/test_fallback.py`

**Interfaces:**
- `runner.run_with_watchdog` gains `stdin_path: Path | None = None` (opened read-only as the child's stdin when set; `DEVNULL` otherwise) — honoring adapters that set `stdin_from_prompt_file`.
- `_run_reviewer` becomes chain-aware: `_run_chain(head: Reviewer, cfg, d, prompt, cwd, store) -> _Outcome`:
  1. Build the ordered list `[head] + [reviewer-by-name for each head.fallbacks]` (names validated at load).
  2. For each entry: if `store.provider_unavailable_reason(entry.provider, now)` → record a synthetic attempt `{provider, model, skipped: "provider marked unavailable: <reason>"}` and continue to the next entry.
  3. Otherwise run attempts exactly as Phase 1 (timeout retries, degraded retries — all within this entry), with every attempt dict gaining `provider`, `model`, `effort`.
  4. After the entry's attempts are exhausted, `classify` the last attempt: `unavailable` → `store.mark_provider_unavailable(provider, reason, now + TTL)` (TTL: 30 min, constant `PROVIDER_UNAVAILABLE_TTL_SEC = 1800`) and move to the next entry; anything else (degraded/failed) → **stop the chain** and return the failure — a provider that *answered badly* is not a quota event, and hopping providers on a degraded answer would mask real harness bugs (spec: unavailability is a different class from misbehaving).
  5. Chain exhausted → `_Outcome(None, attempts, "all providers unavailable or failed: <per-entry summary>")` → `failed` record, `trustworthy=false` (existing machinery), banner, exit 4. Never a pass.
- The accepted attempt's `provider`/`model` become the record's indexed `adapter`/`model` (spec: those columns mean "the attempt that produced the accepted payload").
- `unavailable` classification on the FIRST attempt of an entry short-circuits that entry's remaining retries (retrying a missing binary is noise).

- [ ] **Step 1: Failing tests** — with fake binaries on PATH:

```python
def test_fallback_chain_recovers(tmp_path, monkeypatch):
    # finder's binary is absent (rc 127 path), fallback's fake binary succeeds
    _fake_cli(tmp_path, "fake-good", HEALTHY_ENVELOPE)
    cfg = _cfg_with_chain(primary_bin="/nonexistent/skodun-dead-bin",
                          fallback_provider="xai", fallback_bin="fake-good")
    rec = run_review(repo, cfg, store)
    assert rec["trustworthy"] is True
    assert rec["attempts"][0]["provider"] != rec["attempts"][-1]["provider"]
    assert rec["adapter"] != "dead"          # indexed columns follow the accepted attempt

def test_exhausted_chain_fails_closed(tmp_path):
    cfg = _cfg_with_chain(primary_bin="/nonexistent/a", fallback_bin="/nonexistent/b")
    rec = run_review(repo, cfg, store)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert "unavailable" in rec["failure_reason"]
    assert run_gate(store, repo, cfg).code == 2

def test_degraded_does_not_hop_providers(tmp_path):
    # primary returns a Cancelled stopReason (degraded) — chain must NOT advance
    ...
    assert all(a["provider"] == "xai" for a in rec["attempts"])

def test_provider_state_skips_known_dead_provider(tmp_path):
    store.mark_provider_unavailable("openai", "quota", _iso(now + 999))
    rec = run_review(repo, cfg_openai_then_xai, store)
    assert rec["attempts"][0].get("skipped", "").startswith("provider marked unavailable")
```

(Fake CLI helpers extend Phase 1's `_fake_grok`; each fake logs invocations for the never-dedup-style assertions.)

- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `git commit -am "feat: quota fallback chains with fail-closed exhaustion (refs EPIC)"`

---

### Task 8: Refuter pass

**Files:**
- Modify: `src/skodun/passes.py`, `src/skodun/pipeline.py`, `tests/test_passes.py`; Create: `tests/test_refuter.py`

**Interfaces:**
- `should_run_refuter(mode, trustworthy, findings_total, cfg, env) -> bool` — `mode == "now"`, trustworthy, `findings_total > 0`, a reviewer with role `refuter` is configured and enabled (no refuter configured = pass silently skipped with a note, not an error), kill switch `SKODUN_REFUTER_PASS=0`.
- `refuter_prompt(findings, diff_file, ...) -> bytes` — generic, slot-free: presents the diff plus the finder's findings as a numbered list and instructs adversarial re-examination. Response contract (JSON schema, same enforcement style as the review schema): `{"verdicts": [{"index": int, "verdict": "confirmed|refuted|uncertain", "reasoning": str}]}` — `reasoning` required, minimum length enforced at merge (≥ 20 chars after whitespace collapse, reusing `textnorm.norm`), so an adopted reason can always pass `validate_reason`.
- `merge_refuter_pass(primary, refuter_result, provenance) -> dict` — **annotation only**: for each valid verdict whose `index` is in range, set `primary["findings"][i]["refuter"] = {"verdict", "reasoning", "provider", "model"}`. Out-of-range/duplicate indexes are dropped with a note. Counts, severity, trust axes untouched — pinned:

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

- Pipeline wiring: refuter runs after security/skeptic merges, through `_run_chain` (it gets fallback support for free), reviewer selected by role `refuter` (existing `_reviewer_for`); no fail-closed hold — a pending refuter never delays publishing (unlike security). `extra_passes.refuter` records `{provider, model, effort, status, note}`.

- [ ] Steps: failing tests → FAIL → implement → PASS + full suite → Commit `git commit -am "feat: cross-provider refuter pass, annotation-only by design (refs EPIC)"`

---

### Task 9: Triage — surface annotations + adopt-refuter

**Files:**
- Modify: `src/skodun/triage.py`, `src/skodun/cli.py`, `tests/test_triage.py`, `tests/test_cli.py`

**Interfaces:**
- `triage --list <id>`: findings with a `refuter` annotation show one extra line: `refuter(<provider>/<model>): <verdict> — <reasoning first 120 chars>`.
- `skodun triage --adopt-refuter <review-id> <finding-index>`: requires the finding to carry `refuter.verdict == "refuted"` (adopting a `confirmed` or `uncertain` verdict is an error naming the verdict — adopting a confirmation as a dismissal is nonsense and must say so). Synthesizes the reason `refuter(<provider>/<model>): <reasoning>`, runs it through the **existing** `validate_reason` (no bypass — if the refuter's reasoning is too thin to audit, adoption fails and says why), then records the dismissal through the existing `dismiss` path. Exit codes: 0 recorded, 1 refused (validation/verdict), 2 review/finding not found.
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
- `KeyboardInterrupt` during `review` propagates past the command handler (the pipeline `finally` has already downgraded the record and released the lock — pin that with a test that interrupts a fake-CLI review via SIGINT to the process group) and exits **130**; `gate` keeps mapping every exception, `KeyboardInterrupt` included, to 2. `main()`'s `BaseException → 2` catch-all gains the one carve-out, scoped to the `review` command only.
- `skodun providers`: for each registered adapter — provider id, adapter name, resolved binary path (or `NOT FOUND`), `provider_state` status (available / unavailable-until + reason). Read-only, exit 0 even when binaries are missing (it is a diagnostic listing, not a gate) — but exit 1 when a *configured* reviewer references a provider with no registered adapter (that is a config error worth failing loudly in CI).

- [ ] Steps: failing tests → FAIL → implement → PASS → Commit `git commit -am "feat: skodun providers listing; Ctrl-C exits 130 from review only (refs EPIC)"`

---

### Task 12: shadow-compare --since

**Files:**
- Modify: `src/skodun/shadow.py`, `src/skodun/cli.py`, `tests/test_shadow.py`, `docs/shadow-mode.md`

**Interfaces:** `compare(..., since: str | None)` — when set (ISO-8601, validated), rows on **both** sides with `reviewed_at < since` are excluded before the union join; summary line gains `since=<value>`. Docs: one paragraph replacing the point-in-time caveat with the windowed usage.

- [ ] Steps: failing test (a legacy-only row older than `since` disappears from the report; an invalid `--since` is a usage error, exit 2) → implement → PASS → Commit `git commit -am "feat: shadow-compare --since window (refs EPIC)"`

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
2. **Fallback drill:** reviewer with `SKODUN_<X>_BIN=/nonexistent` and a real fallback → trustworthy review, `attempts[]` shows the unavailable classification then the fallback provider; then both entries dead → `failed` record, banner `trustworthy=false`, gate exit 2, `providers` shows the cached unavailable state, `SKODUN_IGNORE_PROVIDER_STATE=1` bypasses it.
3. **No-regression:** whole-archive `shadow-compare` (and `--since` windowed) against the legacy archive; counts consistent with the Phase 1 log modulo documented classes.
4. Paste every command + output into the evidence log; each of the epic's acceptance criteria gets a ✅ with a pointer.

- [ ] Steps: write runbook → execute → paste evidence → full suite both modes one final time → Commit `git commit -am "docs: phase 2 live acceptance evidence (refs EPIC)"`

---

## Self-Review Notes

- Spec coverage: adapter contract + conformance (T1–T2), three adapters with real-fixture discipline (T5, T6, T10-contingent), fallback chains + provider_state + fail-closed exhaustion (T4, T7), refuter annotate-and-adopt (T8, T9), key removal (T3), CLI honesty + providers (T11), shadow window (T12), docs (T13), demonstrable acceptance (T14). Cuts and contingencies match the spec.
- Deliberate decisions restated: degraded never hops providers (only `unavailable` does); indexed `model`/`adapter` columns follow the accepted attempt; refuter failure never demotes; security-pass demotion is role-based and provider-blind; no `--adopt-all`.
- Open risk named plainly: Tasks 5/6/10 depend on the *installed* CLI versions' flags and envelope shapes — that is why each starts with a probe step and captured fixtures instead of trusting this plan's command sketches, and why agy carries an explicit skip path.
