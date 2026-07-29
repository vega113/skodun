# skodun Phase 3 — Surfaces Design (MCP server, dispatcher, delivery)

Date: 2026-07-29. Status: approved (owner confirmed the four open forks 2026-07-29).
Prerequisite reading: `README.md`, the research report (decisions 13/15),
the Phase 2 design spec's "Cut from Phase 2" paragraph, the Phase 2 plan's
"Deviations recorded at implementation time", `docs/phase2-acceptance.md`.

## Scope

Phase 3 is the surfaces phase: skodun stops being only a foreground CLI.

1. **Pre-push shim + background dispatcher** — and the machinery Phases 1–2 deferred
   *to* it: the dedup probe, `contextpack source="oid"`, batching of oversized diffs,
   the pushed-ref diff scope, same-branch supersede, worktree binding.
2. **MCP server** — hand-rolled stdio JSON-RPC, stdlib-only (owner decision), exposing
   a full CLI mirror (owner decision) to agent harnesses.
3. **Delivery** — a store-backed undelivered-findings ledger, a `skodun surface`
   command, and a SessionStart hook template. Research decision 15 binds: "no review
   happened" is stated explicitly, never read as silence.
4. **Debt folded in** (owner decisions + triage): `Store.close()`/context manager
   (a long-lived dispatcher and server make connection lifetime real), append-only
   `triage --reopen`, the `_TS_FORMAT` literal stragglers, the
   `_fmt_binary`/`_binary_is_absent` duplication, and the pre-growth extraction of the
   chain executor into `chain.py` before the dispatcher adds to `pipeline.py` (1557
   lines already).

**Cut from Phase 3** (unchanged from the roadmap): scheduling, retention/pruning, full
`skodun doctor`, generic `openai-compatible`/`custom-command` adapters, local models,
cloud-bot embed generation, macOS notifications, rules-registry authoring/sync.
Also cut: the quadratic JSON-scan cleanup (no new consumer makes it hotter; Phase 4).

## Resolutions of the design tensions

### 1. MCP dependency — hand-rolled, stdlib-only (owner decision)

The runtime stays stdlib-only. The server implements the MCP stdio transport directly:
newline-delimited JSON-RPC 2.0, the `initialize`/`initialized` handshake, `ping`,
`tools/list`, `tools/call`, `prompts/list`, `prompts/get`, and clean shutdown — the
oldest, most stable subset of the protocol, and all skodun needs. The declared
`protocolVersion` is pinned as a constant with the negotiation rule (echo the client's
if we support it, else our own). Everything else (resources, sampling, notifications,
HTTP transports) is explicitly out of scope; if a later phase needs them, the
dependency question reopens with evidence.

Correctness is mechanical, not conventional: a protocol test-suite drives the server
over a pipe with recorded request/response transcripts (garbage bytes, unknown
methods, malformed params, batch-vs-single, oversized lines) — the server must never
crash and never emit a non-JSON-RPC line on stdout (diagnostics go to stderr). Live
acceptance connects a real installed MCP client (`claude` and/or `codex` CLI) to the
server and exercises a tool round-trip.

### 2. The dispatcher's trust boundary

The dispatcher writes through the **same store chokepoint** as the foreground: trust
is computed on write from strict-bool axes, and the gate re-asserts artifacts exactly
as today. `gate.py` and `trust.py` remain untouched (byte-identical through three
phases). The background failure modes map to existing fail-closed machinery:

- **Crash mid-review** → the record stays `running`; `recover_stale` (shipped in
  Phase 1, swept on every dispatch) marks it `failed` once its own worst-runtime
  budget expires. A failed record can never certify anything.
- **Machine sleep** → wall-clock ceilings mean an overdue `running` record is treated
  as dead and marked `failed`: the cost is a redundant re-review, never a stale trust.
- **Two pushes racing** → same-branch supersede: before launching, the dispatcher
  retires still-`running` *prepush-mode* workers of the same branch (foreground runs
  are never touched), with the oracle's pid-reuse guard — only a pid whose command
  line still names the skodun dispatcher entrypoint is signalled; an unconfirmable pid
  is retired terminally without a signal.
- **Worker outliving its branch** → supersede on the next push plus stale recovery;
  a worker's record for a vanished branch simply never matches a gate query.
- The dispatcher itself is a thin `skodun dispatch` subcommand reading the pre-push
  ref lines from stdin; workers are detached (`start_new_session`) so a closed
  terminal cannot kill a review (the oracle's `nohup` discipline).

### 3. Dedup vs fail-closed

Dedup exists **only** in the dispatcher (`--now` never dedups — oracle parity, pinned
since Phase 1). The probe is the oracle's 3-way protocol, ported:

- Suppress only when a stored record matches the exact `diff_hash` AND is
  `trustworthy` (which by construction already excludes `parse_ok=False`, `degraded`,
  `diff_truncated`) AND its context matches: a record with a non-empty `context_hash`
  suppresses only after packing the candidate's context and matching the dual hash; a
  legacy record with **absent** context (`NULL`) suppresses without packing; a record
  with **explicit-empty** context (`""` — pack attempted and failed) NEVER suppresses.
  The store has preserved the NULL-vs-`""` distinction since Phase 1 for exactly this.
- **Any probe error, any ambiguity, any partial state ⇒ review.** The probe's only
  failure mode is a redundant review; there is no code path from probe failure to
  suppression. Suppressions are recorded (a `dedup` event with the matched review id)
  so a skipped review is an auditable decision, not silence.

### 4. Batching and diff identity — one path, both modes (owner decision)

`diff_hash` is untouched: it is always the identity of the **full** diff bytes,
oracle-parity-pinned. Batching is internal decomposition:

- Deterministic byte-level split at `diff --git` boundaries; a single over-budget file
  splits at `@@` hunk boundaries with the file header repeated; a single over-budget
  hunk becomes its own batch flagged truncated — the irreducible floor is surfaced,
  never hidden.
- Batches run as **sub-reviews**: full reliability machinery, but no own index rows,
  no delivery entries, no verdict banners — the orchestrator owns exactly one terminal
  status keyed to the full `diff_hash`.
- With ≥ 2 batches, a **cross-file integration pass** reviews the seams: each batch's
  file list, its changed-region headers, one-line summary and findings — prompt asks
  only for cross-file problems. Checklist selection runs in `batch` mode per batch
  (never cross-file) and `integration` mode for the seam pass — modes shipped in
  Phase 1 and consumed for the first time here.
- Aggregation: `parse_ok = all parsed`, `degraded = any degraded`,
  `diff_truncated = any truncated`, `stop_reason` = the **first abnormal** value —
  a truncated batch can never hide behind healthy siblings. The gate reasons only
  about the one aggregated record; partial coverage is untrustworthy by construction.
- Both modes share the one implementation. Foreground over-budget diffs, which today
  fail closed as `diff_truncated`, become reviewable; the fail-closed path remains for
  the irreducible floor. Runtime and lock budgets scale by batch count exactly as they
  scale by chain width today.

### 5. Delivery semantics

"Undelivered" is store state, not a marker file: a `deliveries` ledger keyed by review
id records `delivered_at` + channel. `skodun surface` prints, for the current branch,
every background round not yet delivered — **findings AND failures**: a failed round
prints an explicit "NO REVIEW HAPPENED — this round reports nothing because it said
nothing, not because it found nothing" line (research decision 15, verbatim spirit).

The acknowledgement discipline is the oracle's, inverted to fail toward repetition:
quiet rounds are marked delivered immediately; rounds with content are marked **only
after the emit succeeds**. A crash between emit and mark re-delivers next time —
delivered-twice is the designed failure mode, delivered-never is unreachable short of
store loss. `--include-delivered` replays history. The SessionStart hook is a thin
template (shipped under `examples/hooks/`, installed by instruction, not by force)
calling `skodun surface --hook-format claude|text`; the same command serves any
harness or a bare shell profile.

### 6. Foreground vs dispatcher config

One config, two execution modes, explicit mode table:

- `[dispatch]` table: `enabled` (default true once the shim is installed),
  `timeout_sec` (default 240 — oracle parity vs 420 foreground), `timeout_retries`
  (default 0 — a force-push storm must not accumulate workers), `dedup` (default
  true), `large_prompt_escalation` (bg cap raised for large prompts, oracle parity).
  Everything absent falls back to `[defaults]`.
- Reviewer selection, chains, and extra-pass semantics are identical in both modes
  (the security pass's fail-closed hold applies to background reviews too).
- The dispatcher does **not** take the foreground lock (oracle parity: only `--now`
  contends it); background concurrency is bounded by supersede + budgets.
- Worktree binding carries over as shipped: `review --now` still refuses the primary
  checkout unless overridden; the dispatcher reviews whatever ref is pushed. The
  oracle's "dispatcher runs main's script copy" rule is satisfied structurally —
  skodun is one installed package, so there are no divergent per-worktree copies to
  bind (the problem that rule existed to paper over is the one skodun was built to
  delete).

## MCP surface — full CLI mirror (owner decision)

Tools mirror subcommands 1:1, through the same validators, with the same granularity:
`gate`, `review` (documented long-running; one in-flight review per server — the
foreground lock already serializes the backend), `log`, `surface`, `triage_list`,
`triage_dismiss` (single finding, audited ≥ 20-char reason), `adopt_refuter` (single
finding, refuted + non-thin only), `triage_reopen` (single finding, audited reason).
Prompts: `review-now`, `gate-check`. **Nothing more**: no bulk dismissal, no
auto-adopt, no tool the CLI does not have — an agent gains no path the human lacks.
Tool results carry the same text the CLI prints plus a small structured envelope
(exit-code-equivalent status); the verdict-banner contract is preserved inside it.

## Debt resolutions

- **`Store.close()` + context manager** — folded in (owner-triaged "carry" now
  load-bearing): every internal consumer uses the context form; the store-touching
  test modules run `ResourceWarning`-clean, pinned by a dedicated `-W error` test
  runner covering the store suite (whole-suite `-W error` remains Phase 4).
- **`triage --reopen`** — folded in (owner decision): append-only reversal records in
  the ledger (same ≥ 20-char audited-reason validator); `open_findings` treats a
  finding with a reversal newer than its dismissal as open again; full history
  preserved; surfaced in `triage --list`. No bulk form.
- **`_TS_FORMAT` literals** in `pipeline.py`/`gate.py` and the
  `_fmt_binary`/`_binary_is_absent` duplication — folded in as mechanical cleanups.
- **`chain.py` extraction** — folded in, before dispatcher work grows `pipeline.py`:
  behavior-preserving move of the chain executor, suite green, mutation-checked.
- **Quadratic JSON scan** — deferred to Phase 4 (documented; no new hot consumer).

## Constraints carried forward, verbatim

Fail-closed trust invariant and gate contract untouched — `gate.py`/`trust.py` stay
byte-identical unless an owner decision says otherwise. Oracle parity surfaces (diff
identity, triage keys, prompt bytes) unchanged; oracle located solely via
`SKODUN_ORACLE_DIR`. Python ≥ 3.12, stdlib-only runtime (reaffirmed by the MCP
decision), pytest the only dev dependency. Public-repo hygiene including prompt text;
repo-specific tables in config with generic defaults. Text I/O `encoding="utf-8"`;
prompts/diffs travel as files (the recorded agy argv exception stands, with its
guards). Model selection always explicit. The refuter stays annotation-only. The
conformance-suite pattern extends: the MCP protocol suite and the seam matrices are
mechanical gates, not conventions.

## Acceptance criteria (must be demonstrated, not asserted)

1. Full suite green with and without `SKODUN_ORACLE_DIR`, ran-vs-skipped counts
   reported and reconciled; Phase 1/2 parity and conformance tests untouched.
2. **Live push drill**: a real `git push` in a linked worktree triggers the shim; the
   background review lands as a store record; a fresh `skodun surface` delivers it
   once (marked, not re-delivered); a second identical push is dedup-suppressed with
   a recorded dedup event; a third push after an edit reviews again.
3. **Failure surfacing drill**: a background round forced to fail (dead binary at the
   head of a real chain) surfaces via `skodun surface` with the explicit
   "NO REVIEW HAPPENED" wording — never silence.
4. **Race and crash drills**: two pushes in quick succession → the older worker is
   superseded (recorded), exactly one terminal record per content; a SIGKILLed worker
   → `recover_stale` marks it failed and the gate answers 2 for that content.
5. **Batching drill**: an over-budget real diff reviews as N batches + integration
   pass, one aggregated trustworthy artifact at the full diff_hash; a seeded
   truncated batch makes the aggregate untrustworthy (gate 2), never a silent pass.
6. **MCP drill**: a real installed MCP client (claude and/or codex CLI) connects to
   `skodun mcp`, lists tools, runs `gate` and `triage_list`, performs one audited
   single-finding dismissal, and the CLI shows the identical ledger state; the
   protocol suite (garbage, unknown methods, oversized lines) passes with zero
   non-JSON-RPC stdout bytes.
7. **Reopen drill**: an adopted dismissal is reversed via `triage --reopen` with an
   audited reason; the gate flips 0 → 1; history shows both records.
