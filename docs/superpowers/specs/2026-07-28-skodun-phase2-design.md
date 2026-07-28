# skodun Phase 2 — Multi-Provider Design

Date: 2026-07-28. Status: approved (owner confirmed the three open forks 2026-07-28).
Prerequisite reading: `README.md`, the research report, the Phase 1 plan's Global
Constraints, `docs/shadow-mode.md`.

## Scope

Phase 2 makes skodun genuinely multi-provider:

1. **Adapter contract hardening** — a provider-neutral `ParseResult`/`Adapter` contract
   with a conformance suite every registered adapter must pass.
2. **Three new adapters** — `openai` (codex CLI), `anthropic` (claude CLI), `google`
   (agy CLI, *contingent* — explicit documented-skip path if its headless mode proves
   unusable at implementation time).
3. **Cross-provider refuter pass** — annotate-and-explicit-adopt (owner decision).
4. **Quota fallback chains** — per-reviewer ordered fallback, fail-closed when exhausted.
5. **Deferred-decision cleanups** — remove `severity_gate`/`confidence_threshold`
   (owner decision), `KeyboardInterrupt` exit honesty, `shadow-compare --since`.

**Cut from Phase 2** (and why): MCP server, pre-push shim, SessionStart hook → Phase 3
(surfaces; nothing here blocks them). Scheduling, retention, full `skodun doctor` →
Phase 4 (a minimal `skodun providers` listing ships now because fallback chains need
binary-resolution introspection anyway). Generic `openai-compatible`/`custom-command`
adapters → later (YAGNI; the three named CLIs exercise the contract). Batching and the
`contextpack` `source="oid"` seam → stay with the dispatcher (Phase 3). Local models →
per the research roadmap, later.

## Resolutions of the six design tensions

### 1. The adapter contract

`ParseResult` and the `Adapter` protocol move to `adapters/base.py` (grok module
re-exports for compatibility; `ParseResult` keeps its six Phase 1 fields
backward-compatible and adds `payload` — the contract-validated payload verbatim,
with `findings`/`summary` demoted to a review-contract-only projection). The runs a
pipeline requests are described by an **`OutputContract`** (`name`, single-line JSON
schema, eligibility + validation callables); two ship: `REVIEW_CONTRACT` (Phase 1
review shape) and `REFUTER_CONTRACT` (the refuter's verdicts shape). The contract
guarantees:

- `build_cmd(prompt_file, reviewer, defaults, cwd, contract) -> list[str]` — full argv;
  prompt always travels as a file; **raises `ValueError` loudly** for an effort the
  (provider, model) pair cannot honor — never a silent downgrade. Each adapter owns an
  explicit canonical-effort → CLI-flag mapping table.
- `parse(stdout, stderr, contract) -> ParseResult` — never raises on garbage;
  `parse_ok` requires a contract-valid payload (review shape keeps grok's Phase 1
  finding-shape validation).
- `classify(rc, stdout, stderr, contract) -> ok | degraded | unavailable (+ category
  + detail)` — NEW. `degraded` means, provider-neutrally: *positive evidence this
  run's output was truncated or corrupted by the harness* (each adapter defines its
  own signals from its CLI's failure vocabulary). `unavailable` means: *the provider
  could not serve at all* — quota exhausted, auth expired, binary missing (rc 127),
  model id unknown — with a `category` (`quota|auth|binary|model|other`) that decides
  cacheability. Usable contract-valid terminal output always classifies `ok`
  regardless of stderr noise. The kind drives fallback: `unavailable` triggers the
  chain; `degraded` triggers the existing same-reviewer retry.
- **Conformance suite** (`tests/adapter_conformance.py`, with a registry-parameterized
  coverage gate): every adapter must (a) return `parse_ok=False` without raising on
  garbage/empty/truncated input under both contracts, (b) ship ≥2 degradation
  fixtures (healthy vs degraded envelope pairs) and detect them — captured from real
  CLI output wherever a real capture can exist, synthesized otherwise, with
  capture-vs-synthesized provenance recorded per fixture, (c) ship ≥1
  `unavailable` fixture with a category (incl. the rc-127 binary-missing case),
  (d) prove one loud effort rejection or declare full effort support, (e) never mark
  `degraded` or `unavailable` from finding-text content or from stderr noise beside
  usable output, (f) request, classify and parse a valid `REFUTER_CONTRACT` fixture.
  An adapter that cannot recognise its CLI failing is worse than no adapter — the
  suite is the registration gate.

### 2. Trust across a provider fallback

A fallback is a **fresh attempt by a different reviewer entry**, never a continuation.
Each entry in `attempts[]` gains `{provider, model, effort}` and a persisted
`classification`: the full `ClassifyResult` (`{kind, category, detail}`) for every
completed attempt, synthetic `unavailable` objects for missing-binary and
quota-cache-skip entries, and `null` for timed-out attempts (whose output was
discarded — there is nothing to classify). The review's trust axes come solely from
the single accepted attempt; the invariant and its store-chokepoint enforcement are
untouched. Outputs of two providers are never merged into one payload.

### 3. Per-pass provenance

The indexed `reviews.model`/`reviews.adapter` columns keep their Phase 1 meaning: the
**primary (finder) attempt that produced the accepted payload**. Per-pass provenance is
additive artifact-JSON: every `extra_passes.<name>` object and every `attempts[]` entry
carries `{provider, model, effort}`. No destructive migration of the live store (6k+
imported rows): absent fields mean "single-provider Phase 1 record". `PRAGMA
user_version` is introduced (0→2) with a migration runner so future schema changes have
a home; the only v2 DDL is the new `provider_state` table.

### 4. Refuter semantics (owner-approved: annotate + explicit adopt)

New extra pass, role `refuter`, runs when the finder result is trustworthy with
`findings_total > 0`. Cross-provider by configuration (the existing role-based reviewer
selection already routes it; the example config pairs an `xai` finder with an `openai`
refuter). Output: per-finding verdicts `confirmed | refuted | uncertain` plus reasoning,
validated against a JSON schema keyed by finding index. Merge is **annotation-only**:
verdicts attach to findings (`finding["refuter"] = {...}` with provider attribution);
counts, severity, trust axes, and the gate are untouched — pinned by test. A failed or
degraded refuter records `extra_passes.refuter.status` with a reason and **does not
demote the primary** — provider-B being unavailable is an absent annotation, not a
broken review. (Contrast: the security pass keeps its Phase 1 fail-closed demotion
regardless of which provider ran it — role semantics decide demotion, never provider
identity.) `triage --list` shows annotations; `skodun triage --adopt-refuter <id> <n>`
dismisses one finding recording the refuter's reasoning + attribution as the audited
reason (must still pass `validate_reason`). No auto-dismissal, ever.

### 5. Effort per provider

Canonical enum unchanged (`none|low|medium|high|max`). Per-adapter mapping tables:
grok → `--effort` (unchanged); codex → `-c model_reasoning_effort=` with
`{none→minimal, low→low, medium→medium, high→high, max→xhigh}`; claude → `--effort`
with `{low, medium, high, max→max}` and **loud rejection of `none`** (no such level);
agy → decided at implementation against the installed CLI, same loud-rejection rule.
Every mapping is pinned by the conformance suite.

### 6. Quota fallback chains

`[[reviewers]]` gains `fallbacks = ["<reviewer-name>", ...]` — an ordered chain of
other reviewer entries. Validation: referenced entries exist, are enabled, no cycles,
chain length ≤ 3. Execution: when an attempt classifies `unavailable`, the pipeline
moves to the next chain entry (fresh attempt, that entry's own adapter/model/effort;
the per-entry timeout budget applies). `degraded`/timeout retries stay within the
current entry as today. A `provider_state` table caches `unavailable_until` per
provider (written on quota-style unavailability with a conservative TTL, consulted to
skip a known-dead provider, atomically updated, always bypassable with
`SKODUN_IGNORE_PROVIDER_STATE=1`). **Exhausted chain = explicit `failed` record**
(`failure_reason` naming every entry and its classification), `trustworthy=false`,
banner emitted. The failed record never erases older coverage, under the store's
newest-wins selection: the gate reads the NEWEST trustworthy row for the diff_hash,
and that row must pass the gate's artifact checks (`base_sha` match included) — the
diff-identity invariant working, fail-closed when the newest row's base has moved.
Where no trustworthy coverage exists (fresh content), the gate answers exit 2. Never
a pass minted by the failure itself.

## Deferred-decision resolutions

- **`severity_gate`/`confidence_threshold`: removed** (owner decision). Loading a config
  that still sets either key fails with a targeted message ("removed in Phase 2; the
  gate blocks on any open finding by design — delete the key"). The Phase 1 no-effect
  pin tests are replaced by removal-message tests.
- **Ctrl-C honesty:** `KeyboardInterrupt` escapes `_cmd_review` only and exits 130
  after the existing `finally` cleanup (record downgraded, lock released). `_cmd_gate`
  keeps mapping every exception to 2.
- **`shadow-compare --since <ISO>`:** optional window filter on both sides'
  `reviewed_at`, so the legacy-only count stops drifting upward as the legacy system
  keeps running. Snapshot semantics documented.
- **`contextpack` `source="oid"`:** stays `NotImplementedError`; dispatcher phase.

## Constraints carried forward, verbatim

Fail-closed trust invariant and gate contract untouched. Stdlib-only runtime, pytest
only. Public-repo hygiene: no machine paths, no upstream project names in `src/`
(including prompt text — the refuter prompt is generic and slot-free; unlike the
security prompt it names no repo-specific concepts, so it needs no slot interface). Oracle parity surfaces (diff identity, triage keys, prompt bytes) unchanged —
Phase 2 adds no oracle-ported code, so no new parity tests; existing ones must stay
green. Every file read/written passes `encoding="utf-8"`; prompts travel as files;
model selection always explicit.

## Acceptance criteria (must be demonstrated, not asserted)

1. Full suite green with and without `SKODUN_ORACLE_DIR`; all Phase 1 parity tests
   untouched and passing.
2. Conformance: every adapter in the registry passes the shared conformance suite.
3. **Live cross-provider run** on a real change-set: finder on one provider, refuter on
   another; annotations visible in `triage --list`; one `--adopt-refuter` dismissal
   flips the gate 1→0 (on a store copy); recorded artifact shows per-pass provenance.
4. **Fallback drill**: chain `[dead-binary reviewer → real reviewer]` yields a
   trustworthy review whose `attempts[]` shows `unavailable` then success on the
   fallback provider. Exhausted chain `[dead → dead]` on fresh content yields a
   `failed` record, banner `trustworthy=false`, gate exit 2 (where the newest
   trustworthy row for those bytes matches the current base, it legitimately keeps
   gating — newest-wins, pinned).
   The provider-state cache is exercised with a quota-category failure (dead
   binaries are deliberately uncached).
5. **No regression**: whole-archive shadow comparison against the legacy archive
   reproduces Phase 1 counts (modulo documented classes and the `--since` window).
