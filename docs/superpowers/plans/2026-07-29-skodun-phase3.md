# skodun Phase 3 Implementation Plan — Dispatcher, MCP Server, Delivery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** skodun reviews pushes in the background (pre-push shim → dispatcher → detached workers with dedup, batching, oid context, supersede), delivers undelivered findings and failures to the next session, and serves agents over a hand-rolled stdlib-only MCP stdio server mirroring the CLI.

**Architecture:** Fixed by `docs/superpowers/specs/2026-07-29-skodun-phase3-design.md` (owner-approved) — read it first; this plan implements it and does not re-litigate it. All background writes go through the existing store chokepoint; `gate.py` and `trust.py` are not modified by any task.

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest only.

## Global Constraints

- Everything in the Phase 1 and Phase 2 Global Constraints still binds, including the Phase 2 Deviations section (the agy argv exception and its guards stand unmodified).
- **`gate.py` and `trust.py` are byte-identical before and after this phase** — pinned by a test comparing their hashes against committed constants recorded at phase start. If a task appears to need a change there, STOP and surface it as an owner decision.
- Oracle parity: the dispatcher/dedup/batching/oid tasks port oracle semantics. Their porting references are the research report's audit (Part 1, features A6–A8, A2 dispatcher scope, A14 items 3–4, A16) and the oracle sources under `SKODUN_ORACLE_DIR/scripts/` (`grok-prepush-review.sh` dispatcher lines 3421–3631, dedup 217–286 + 3496–3562, batching 828–945 + 2112–2752; `.claude/hooks/surface-grok-findings.sh`). Where this plan and observed oracle behavior disagree, the oracle wins; amend the plan in the same commit (Deviations section at the end of this file).
- **Method requirements, from the Phase 1–2 postmortems (binding on every task):**
  - Between-task review is **by execution and mutation, never inspection**: run the suite, mutate the new code at named points, confirm a test fails per mutation. `PYTHONDONTWRITEBYTECODE=1` is mandatory during mutation runs (stale-`.pyc` false results otherwise).
  - Subagents do NOT inherit `SKODUN_ORACLE_DIR` — pass it explicitly; always report ran-vs-skipped counts (a skipped parity test is not evidence of parity).
  - Tests never touch `~/.local/share/skodun/skodun.db` or `~/.grok`: every test pins `SKODUN_DB` and every `SKODUN_<X>_BIN` to tmp paths; new fixtures follow the provenance rule (real capture wherever a capture can exist; synthesized wording only from strings verifiably present in the installed binary or documented provider text, labeled in the fixture README).
  - Full suite ≈ 2.5–4 min: generous timeouts, run in the foreground.
- **Seam matrix, mandatory for every new subcommand/flag/entrypoint** (`dispatch`, `surface`, `mcp`, `install-hooks`, `triage --reopen`): exit code × {normal, closed stdout, `| head` under pipefail, `python -m skodun`, console script}, plus ordinary misuse → message, never a traceback. The dispatcher and MCP server additionally get the no-terminal variants: stdin closed, stdout a pipe to a dead reader, no controlling tty.
- MCP server hygiene: stdout carries **only** newline-delimited JSON-RPC; every diagnostic goes to stderr; a single non-conforming stdout byte is a test failure.
- New store state (deliveries, reopen records, dedup events) arrives via the existing migration ladder (`user_version` 2 → 3), additive only; a future-version DB is refused before any DDL (shipped rule).
- Detached workers: `start_new_session=True`, environment scrubbed to what the pipeline needs, never inheriting the dispatcher's stdin/stdout; worker stderr goes to a per-review log file in the store's blob directory.

## File Structure

```
src/skodun/
├── chain.py             # NEW (Task 2): chain executor extracted from pipeline.py
├── batching.py          # NEW (Tasks 6–8): split, plan, aggregate
├── dispatch.py          # NEW (Tasks 9–11): dedup probe, dispatcher, supersede
├── delivery.py          # NEW (Task 12): deliveries ledger + surface rendering
├── mcpserver.py         # NEW (Tasks 13–14): stdio JSON-RPC loop + tool registry
├── contextpack.py       # modified (Task 4): source="oid"
├── gitio.py             # modified (Tasks 4–5): blob reads, ref-range diff
├── pipeline.py          # modified (Tasks 2, 8, 10): extraction, batching, prepush mode
├── store.py             # modified (Tasks 1, 3, 9, 12): close(), v3 migration, events
├── triage.py            # modified (Task 3): reopen records
├── cli.py               # modified (Tasks 3, 10–15): new subcommands
├── config.py            # modified (Task 10): [dispatch] table
└── passes.py            # modified (Task 7): integration-pass prompt
tests/
├── test_chain.py, test_batching.py, test_dispatch.py, test_delivery.py,
├── test_mcpserver.py (+ tests/fixtures/mcp/*.jsonl transcripts)
└── (existing modules extended)
examples/hooks/sessionstart-claude.sh, sessionstart-plain.sh   # NEW (Task 12)
docs/phase3-acceptance.md                                       # NEW (Task 16)
```

---

### Task 1: Store lifetime — `close()`, context manager, warning-clean store suite

**Files:** Modify `src/skodun/store.py`, `tests/test_store.py`; touch every internal consumer that opens a store (`cli.py`, tests) to the context form.

**Interfaces:** `Store.close()` (idempotent); `Store.__enter__/__exit__` (closes); `Store.open` unchanged. All CLI paths use `with Store.open(...) as store:`. A dedicated test runs the store-touching test modules under `-W error::ResourceWarning` via a subprocess pytest invocation and asserts zero failures (whole-suite `-W error` stays Phase 4).

- [ ] Failing tests: double-close is a no-op; using a closed store raises `sqlite3.ProgrammingError` (not swallowed); the `-W error::ResourceWarning` sub-run passes.
- [ ] Implement; sweep internal consumers; full suite green.
- [ ] Mechanical cleanups folded here: replace remaining `_TS_FORMAT` literals in `pipeline.py`/`gate.py` — **note:** the `gate.py` change is EXEMPT from the byte-identity pin only if the owner approves; otherwise leave `gate.py`'s literal in place and record it (default: LEAVE IT — byte-identity wins over cleanup). Deduplicate `cli._fmt_binary` onto `pipeline._binary_is_absent`'s split.
- [ ] Commit: `feat: store close/context manager, ResourceWarning-clean store suite (refs EPIC)`

### Task 2: Extract `chain.py` (behavior-preserving)

**Files:** Create `src/skodun/chain.py`, `tests/test_chain.py`; Modify `src/skodun/pipeline.py` (imports + removal).

**Interfaces:** `chain.run_chain(...)` — the existing `_run_chain` moved verbatim with its private helpers (`_Outcome`, attempt classification loop, provider-state consult); `pipeline` re-exports nothing new; every existing test passes unmodified. `test_chain.py` holds only NEW coverage relocated by import, not rewrites.

- [ ] Move; full suite green with zero test edits (the proof of behavior preservation).
- [ ] Mutation check: 3 named mutations inside the moved executor (drop the quota-cache write; invert the degraded-retry decrement; skip the accepted-provenance update) — each must be killed by an existing test.
- [ ] Commit: `refactor: extract chain executor to chain.py ahead of dispatcher growth (refs EPIC)`

### Task 3: Triage `--reopen` (append-only reversal)

**Files:** Modify `src/skodun/store.py` (v3 migration part 1: `triage_reopen` table), `src/skodun/triage.py`, `src/skodun/cli.py`, tests.

**Interfaces:** `triage_reopen(ledger_key, reason, reopened_at)` records appended, never deleting dismissals; `store.triage_for` returns each finding's EFFECTIVE state: dismissed iff the newest of {dismissal, reopen} for its ledger key is the dismissal. `open_findings` needs no change (it consumes `triage_for`'s map — a reopened finding simply drops out of the map). `validate_reason` applies to reopen reasons unchanged. CLI: `skodun triage --reopen <review-id> <finding-index> "<reason>"`, exits 0 recorded / 1 refused / 2 not-found; `--list` shows `DISMISSED` / `REOPENED` with both timestamps. MCP mirrors it later (Task 14).

- [ ] Failing tests: reopen flips gate 0→1 on a fully-triaged review; re-dismiss after reopen flips back (newest wins); placeholder/short reopen reasons refused; history query returns both records; migration test extends the true-v0/v2 pattern to v3.
- [ ] Implement; seam matrix for the new flag; full suite; mutation check (drop the newest-wins comparison → a test must fail).
- [ ] Commit: `feat: append-only triage reopen with audited reasons (refs EPIC)`

### Task 4: `contextpack source="oid"` + gitio blob reads

**Files:** Modify `src/skodun/contextpack.py`, `src/skodun/gitio.py`, tests.

**Interfaces:** `gitio.blob_bytes(repo, oid, path) -> bytes | None` (`git cat-file blob <oid>:<path>`, None on any failure — missing path, bad oid, binary handled downstream); `contextpack.pack(..., source="oid", oid=<commit>)` reads file content from the commit's tree instead of the worktree — always the same tree the ref-range diff came from. The worktree hardening that has no oid analogue (symlink walk, O_NOFOLLOW, FIFO) is replaced by its blob-side equivalent: `cat-file` reads object content, so path traversal/symlink swaps are structurally impossible; binary detection and size caps apply unchanged to the returned bytes. Omission vocabulary unchanged.

- [ ] Failing tests: oid pack returns committed content while the worktree differs (the load-bearing property — dispatcher context must match the pushed commit, not the current tree); deleted-in-commit path → `missing`; binary blob → `binary`; bad oid → every file omitted with `missing`, never a raise; determinism byte-for-byte.
- [ ] Implement (`NotImplementedError` at `contextpack.py:334` replaced); full suite; oracle note: `GR_CONTEXT_SOURCE=oid` semantics per audit A6.
- [ ] Commit: `feat: oid-sourced context packing for the dispatcher (refs EPIC)`

### Task 5: Ref-range diff scope

**Files:** Modify `src/skodun/gitio.py`, tests.

**Interfaces:** `capture_ref_diff(repo, base_sha, local_oid, ) -> Diff` — `git diff --no-ext-diff --no-textconv <base_sha> <local_oid>`: commits only, **no untracked files, no working tree** (the dispatcher's scope, audit A2). Same NUL-delimited `--name-status` parsing between the two oids; `diff_identity` applies unchanged (same trailing-newline rule — parity test against the oracle's dispatcher hash path where the seam permits, else against `diff_identity`'s own pinned behavior with a recorded note). Zero-OID (branch deletion) is the caller's concern (Task 10 skips it before calling).

- [ ] Failing tests: ref diff excludes untracked and uncommitted edits present in the worktree; identical content pushed twice yields identical identity; statuses map correct across rename/copy records.
- [ ] Implement; full suite. Commit: `feat: pushed-ref diff scope for the dispatcher (refs EPIC)`

### Task 6: Batch split + plan (deterministic, byte-level)

**Files:** Create `src/skodun/batching.py`, `tests/test_batching.py`.

**Interfaces:** `split(diff: bytes, budget: int) -> list[Batch]` — `Batch(data: bytes, files: list[str], truncated: bool)`. Rules (oracle A8, ported): operate on raw bytes (invalid UTF-8 splits bit-identically); split at `diff --git` boundaries; an over-budget single file splits at `@@` hunk boundaries with the file header repeated so each piece reviews alone; a single over-budget hunk becomes its own batch with `truncated=True` (the irreducible floor, surfaced); greedy packing preserving input order (deterministic); `split(d, b)` with `len(d) <= b` returns one un-truncated batch identical to `d`.

- [ ] Failing tests: determinism (same input → same batches, byte-for-byte); header repetition on hunk splits; irreducible-floor flagging; invalid-UTF-8 diff splits without error; every batch ≤ budget except flagged floors; concatenating batches' hunks loses no hunk (coverage completeness — count `@@` occurrences in vs out).
- [ ] Implement; mutation check (drop header repetition; drop the floor flag — each killed).
- [ ] Commit: `feat: deterministic byte-level diff batching (refs EPIC)`

### Task 7: Integration pass (cross-file seams)

**Files:** Modify `src/skodun/passes.py`, `src/skodun/batching.py`, tests.

**Interfaces:** `integration_prompt(batch_summaries, ...) -> bytes` — generic, slot-free: per batch its file list, `diff --git`/`@@` changed-region header lines (bodies omitted, max 120 header lines per batch), one-line summary, and findings; instructs ONLY cross-file problems (broken callers, inconsistent contracts, removed symbols still used, schema change unmatched). Uses `REVIEW_CONTRACT` (findings shape unchanged). Checklist selection runs in `integration` mode (core + cross-file only — shipped Phase 1 machinery, first consumer). Runs only when ≥ 2 batches.

- [ ] Failing tests: prompt contains headers but no hunk bodies; single-batch runs skip the pass; findings from the pass merge with `(integration) ` title-tagging following the extra-pass tagging rules (rule-id titles keep their prefix, detail gets the tag).
- [ ] Implement; commit: `feat: cross-file integration pass over batch seams (refs EPIC)`

### Task 8: Batched pipeline wiring (both modes)

**Files:** Modify `src/skodun/pipeline.py`, `src/skodun/batching.py`, tests.

**Interfaces:** When `len(diff) > max_diff_bytes`, the pipeline routes through the batch orchestrator instead of setting `diff_truncated`: each batch is a **sub-review** through `chain.run_chain` (full retry/fallback machinery; no index row, no banner, no delivery — orchestrator-owned scratch tags `{tag}.b{i}`); then the integration pass; then ONE aggregated record at the FULL `diff_hash`: `parse_ok = all`, `degraded = any`, `diff_truncated = any batch truncated`, `stop_reason` = first abnormal across sub-reviews in batch order, findings merged with batch provenance in the artifact (`batches[]`: per-batch files, bytes, attempts, trust axes), severity recounted. Security/skeptic/refuter passes run on the AGGREGATE per their existing eligibility (security hold decided before any batch runs — unchanged ordering guarantee). Budgets: `worst_runtime_sec` and lock ceiling multiply by `(batch_count + 1)`; foreground keeps the lock for the whole batched run.

- [ ] Failing tests (fake CLIs): an over-budget diff yields one trustworthy aggregated record with N batches recorded and grok invoked N+1 times (batches + integration); a seeded `Cancelled` envelope in batch 2 makes the aggregate degraded ⇒ untrustworthy ⇒ gate 2 (first-abnormal stop_reason is batch 2's); a seeded irreducible-floor batch makes `diff_truncated=True` ⇒ gate 2; single-batch path is byte-identical to the unbatched path (no behavioral fork for small diffs); budget scaling pinned.
- [ ] Implement; mutation check (aggregate `any degraded` → `all degraded` must be killed; first-abnormal → last-abnormal must be killed).
- [ ] Commit: `feat: batched review with aggregated full-identity artifact, both modes (refs EPIC)`

### Task 9: Dedup probe (dispatcher-only) + dedup events

**Files:** Create `src/skodun/dispatch.py` (probe part), Modify `src/skodun/store.py` (v3 part 2: `dedup_events`), tests.

**Interfaces:** `dedup_probe(store, repo, diff: Diff, oid) -> Suppression | None`. Port of the oracle 3-way protocol (audit A7): find newest trustworthy record for `diff_identity(diff.data)`; none → review (`None`). Found with `context_hash` **NULL/absent** (legacy) → suppress without packing. Found with non-empty `context_hash` → pack the candidate's oid context, compare; match → suppress, mismatch → review. Found with **explicit-empty** `context_hash` (`""`) → review, always. ANY exception anywhere in the probe → review (`None`), logged to stderr — no path from error to suppression (pinned by a monkeypatched-explosion test). A suppression writes a `dedup_events` row `{at, branch, diff_hash, matched_review_id}` — a skipped review is an auditable decision.

- [ ] Failing tests: all four protocol branches; probe explosion → review; suppression recorded; `--now` path has no call site (grep-pinned test asserting `pipeline.run_review` never imports the probe).
- [ ] Implement; commit: `feat: three-way dedup probe with audited suppressions (refs EPIC)`

### Task 10: Dispatcher + pre-push shim + `[dispatch]` config

**Files:** Modify `src/skodun/dispatch.py`, `src/skodun/config.py`, `src/skodun/cli.py`, `src/skodun/pipeline.py` (mode="prepush" entry), tests (`test_dispatch.py`).

**Interfaces:**
- `[dispatch]` config table: `enabled=true, timeout_sec=240, timeout_retries=0, dedup=true, large_prompt_bytes=80_000` (escalates the bg cap to the fg cap for large prompts — oracle A14.7); validation follows `[defaults]` rules; absent keys fall back to `[defaults]`.
- `skodun dispatch` reads pre-push ref lines from stdin (`<local ref> <local oid> <remote ref> <remote oid>`), per ref: skip zero-OID deletions; resolve base (`remote oid` when non-zero, else merge-base against the default main refs); `capture_ref_diff`; dedup probe (when `dedup=true`); same-branch supersede (below); then **detach a worker**: `subprocess.Popen([sys.executable, "-m", "skodun", "worker", ...], start_new_session=True, stdin=DEVNULL, stdout=DEVNULL, stderr=<per-review log file>)` — `skodun worker` is a hidden subcommand running the pipeline with `mode="prepush"`, oid context, `[dispatch]` budgets, no fg lock. `dispatch` itself always exits 0 fast (a hook must never block a push on review latency) except on its own usage errors; failures inside dispatch are recorded (`failures` note in the store + stderr) — fail toward the push proceeding with a loud record, never toward blocking or silence.
- Supersede (oracle A14.4): before launching, retire still-`running` `mode="prepush"` records of the same branch — SIGTERM only a pid whose `ps -o args=` still names the skodun worker entrypoint (pid-reuse guard); unconfirmable pid → record retired terminally (`superseded`) without a signal. `--now` records never touched.
- `skodun install-hooks` writes a pre-push shim into the repo's hook path that CHAINS to any existing pre-push hook (runs it first, propagates its failure), honors `SKODUN_PREPUSH_SKIP=1` (one push) and `git config skodun.prepush false` (disable, recorded to stderr), and pipes the ref lines to `skodun dispatch`. Idempotent; refuses to overwrite a non-skodun hook without `--force` (prints the diff it would make).
- `recover_stale` swept at every dispatch (shipped machinery, new call site).

- [ ] Failing tests: ref parsing incl. deletions; worker detaches and its record lands with `mode="prepush"` + oid context source; supersede retires exactly the prepush-mode sibling and records it; pid-reuse guard (a recycled pid running something else is not signalled); shim chains and propagates an existing hook's failure; both bypasses; dispatch exits 0 with a dead worker binary (loud record, push proceeds); seam matrix for `dispatch`/`install-hooks`/`worker`.
- [ ] Implement; mutation checks (drop the pid-guard `ps` match; drop supersede's prepush-mode filter — each killed).
- [ ] Commit: `feat: pre-push dispatcher with detached workers, supersede, chained shim (refs EPIC)`

### Task 11: Dispatcher hardening drills (tests only)

**Files:** tests (`test_dispatch.py`).

Scripted end-to-end drills with fake CLIs in tmp repos: crash (SIGKILL a worker mid-run → recover_stale marks failed → gate 2); race (two dispatches 100 ms apart → one terminal record per content, older superseded); sleep simulation (backdate a running record past its budget → recovered); worker outlives branch (delete the branch, dispatch another → no interference). These are the spec's trust-boundary claims turned into executable evidence before the live drill repeats them for real.

- [ ] Write drills; all green; commit: `test: dispatcher trust-boundary drills (refs EPIC)`

### Task 12: Delivery ledger + `skodun surface` + hook templates

**Files:** Create `src/skodun/delivery.py`, `examples/hooks/sessionstart-claude.sh`, `examples/hooks/sessionstart-plain.sh`; Modify `src/skodun/store.py` (v3 part 3: `deliveries`), `src/skodun/cli.py`, tests.

**Interfaces:** `deliveries(review_id PRIMARY KEY, delivered_at, channel)`; `undelivered(store, branch) -> list[dict]` — background rounds (`mode="prepush"`) for the branch without a deliveries row, **including failed/degraded rounds**. `skodun surface [--branch B] [--hook-format claude|text] [--include-delivered]`: renders findings rounds normally; renders failure rounds with the explicit line `NO REVIEW HAPPENED — this round reports nothing because it said nothing, not because it found nothing` plus the failure reason; **ack discipline:** quiet rounds (trustworthy, 0 findings) marked delivered immediately; content-bearing rounds marked ONLY after the full emit succeeds (flush + no exception) — a crash between emit and mark re-delivers (delivered-twice is the designed failure mode; a mark-then-emit ordering is the bug this exists to prevent, pinned by a test that fails the emit and asserts the round is still undelivered). `--hook-format claude` emits the SessionStart JSON envelope (`{"systemMessage": ..., "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}`); `text` is plain lines. Hook templates call the command and are installed by instruction (README), never written into a user repo by skodun.

- [ ] Failing tests: failed round surfaces with the exact wording; ack-after-emit (failed emit ⇒ still undelivered; successful emit ⇒ marked; second surface ⇒ silent); quiet-round immediate ack; `--include-delivered` replays; claude JSON is valid JSON with both keys; seam matrix (closed stdout mid-emit ⇒ round stays undelivered, exit non-zero).
- [ ] Implement; commit: `feat: delivery ledger and surface command — silence is never a verdict (refs EPIC)`

### Task 13: MCP stdio server core (hand-rolled JSON-RPC)

**Files:** Create `src/skodun/mcpserver.py`, `tests/test_mcpserver.py`, `tests/fixtures/mcp/*.jsonl`; Modify `src/skodun/cli.py` (`skodun mcp`).

**Interfaces:** newline-delimited JSON-RPC 2.0 over stdio. Implements: `initialize` (declares `protocolVersion` pinned constant `MCP_PROTOCOL_VERSION`; negotiation rule: echo the client's requested version when recognized, else answer with ours; `capabilities: {tools: {}, prompts: {}}`; `serverInfo`), `notifications/initialized` (accepted, no reply), `ping`, `tools/list`, `tools/call`, `prompts/list`, `prompts/get`; unknown method → JSON-RPC error `-32601`; parse error → `-32700` with `id: null`; requests before `initialize` → error, never crash. EOF on stdin → clean exit 0. **Stdout discipline:** every stdout line is exactly one JSON-RPC message; all logging to stderr; pinned by a harness that runs the server over pipes, feeds each `tests/fixtures/mcp/*.jsonl` transcript (recorded request lines + expected response predicates — including garbage bytes, a 10 MB line, interleaved notifications, and an immediate EOF), and asserts stdout parses line-by-line as JSON-RPC with zero residue. **Probe step:** before finalizing `MCP_PROTOCOL_VERSION`, run the installed `claude`/`codex` CLI's MCP client against a stub echo server to capture the version(s) real clients send; record them in the fixture README (documented-skip: if neither CLI can act as a client headlessly, pin to the current published spec revision and note it).

- [ ] Failing tests first (transcript harness with hand-written expected predicates); implement loop + dispatch table; seam matrix (dead-reader stdout pipe → clean exit, no traceback; stdin closed immediately → exit 0).
- [ ] Mutation checks: drop the `id` echo on one method; swap error codes; emit a log line to stdout — each killed by the harness.
- [ ] Commit: `feat: stdlib stdio MCP server core with transcript-pinned protocol (refs EPIC)`

### Task 14: MCP tools — the CLI mirror

**Files:** Modify `src/skodun/mcpserver.py`, tests.

**Interfaces:** Tools, each a thin wrapper over the SAME functions the CLI calls (never a reimplementation — one definition of every behavior): `gate`, `review` (documented long-running; the server allows ONE in-flight review, second call → busy error), `log`, `surface`, `triage_list`, `triage_dismiss(review_id, finding_index, reason)`, `adopt_refuter(review_id, finding_index)`, `triage_reopen(review_id, finding_index, reason)`. Every dismissal-shaped tool goes through `validate_reason`/verdict checks identically to the CLI — pinned by tests that assert the SAME refusal strings for the same bad inputs on both surfaces. Tool results: `{status: <cli-exit-equivalent int>, text: <what the CLI would print>}` in MCP content. Prompts: `review-now`, `gate-check` (static text). **No bulk tool exists; a `tools/list` snapshot test pins the exact tool list** so an added tool is a reviewed decision, not drift.

- [ ] Failing tests: tool-list snapshot; dismissal parity (placeholder reason refused with identical message via CLI and MCP); adopt-refuter of a `confirmed` verdict refused; gate over MCP equals gate via CLI on the same store; busy-review error.
- [ ] Implement; commit: `feat: MCP tools mirror the CLI exactly — nothing more (refs EPIC)`

### Task 15: New-surface seam matrices + byte-identity pin

**Files:** tests.

- The seam matrix parameterized over the five new surfaces (Global Constraints list); plus the `gate.py`/`trust.py` byte-identity pin test (hash constants recorded now, asserted always — with the Task 1 `_TS_FORMAT` caveat resolved per its default: gate literal left in place).
- [ ] Write; green; commit: `test: seam matrices for phase 3 surfaces, trust-boundary byte pin (refs EPIC)`

### Task 16: Docs, examples, live acceptance

**Files:** Modify `README.md`, `examples/`; Create `docs/phase3-acceptance.md`.

- README: dispatcher setup (`install-hooks`), surface/hook wiring, MCP usage (client config snippets for claude/codex CLIs), new subcommands table rows.
- **Live acceptance runbook** — prerequisites first (each CLI responds, model ids valid, quota present — Phase 2's check caught two dead credentials before they wasted runs), then the spec's seven criteria with pasted evidence each: (1) suite counts reconciled both modes; (2) live push → background record → fresh-session surface → dedup on identical re-push (event recorded) → re-review after edit; (3) failure surfacing with the exact wording; (4) supersede race + SIGKILL recovery live; (5) over-budget real diff batched with integration pass, seeded truncated batch gates 2; (6) real MCP client end-to-end incl. one audited dismissal visible from the CLI, protocol suite zero-residue; (7) reopen flips gate 0→1 with history shown.
- [ ] Execute; paste evidence; full suite both modes one final time; commit: `docs: phase 3 live acceptance evidence (refs EPIC)`

---

## Self-Review Notes

- Spec coverage: every spec section maps to tasks (store lifetime T1; chain extraction T2; reopen T3; oid T4; ref scope T5; batching T6–T8; dedup T9; dispatcher/shim/config T10–T11; delivery T12; MCP T13–T14; seams/pin T15; docs+acceptance T16). Cuts match the spec. Owner decisions honored: stdlib MCP, both-mode batching, full CLI mirror, append-only reopen.
- Deliberate decisions restated: dispatch exits 0 to the push except usage errors (loud record over blocked push); probe errors always review; `--now` never dedups (grep-pinned); sub-reviews own no index rows or deliveries; first-abnormal aggregation; ack-after-emit delivery; one in-flight MCP review; tool-list snapshot as drift gate; `gate.py`/`trust.py` byte-pinned.
- Open risks named plainly: the MCP protocol-version probe (T13) and the shim's interaction with unknown existing hooks (T10) are the two places an installed binary or a user's repo can contradict this plan — both start with probes and both have documented outcomes for the contradiction. Batching is the largest port; its oracle references are line-precise and its aggregation rules are mutation-checked.

## Deviations recorded at implementation time

(None yet. When an installed binary or the oracle contradicts this plan, the implementer amends this section in the same commit — the Phase 2 pattern.)
