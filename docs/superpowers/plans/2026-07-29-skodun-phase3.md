# skodun Phase 3 Implementation Plan — Dispatcher, MCP Server, Delivery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** skodun reviews pushes in the background (pre-push shim → dispatcher → detached workers with dedup, batching, oid context, supersede), delivers undelivered findings and failures to the next session, and serves agents over a hand-rolled stdlib-only MCP stdio server mirroring the CLI.

**Architecture:** Fixed by `docs/superpowers/specs/2026-07-29-skodun-phase3-design.md` (owner-approved, amended alongside this revision) — read it first; this plan implements it and does not re-litigate it. All background writes go through the existing store chokepoint; **no task modifies `gate.py` or `trust.py`**.

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest only.

## Global Constraints

- Everything in the Phase 1 and Phase 2 Global Constraints still binds, including the Phase 2 Deviations section (the agy argv exception and its guards stand unmodified).
- **`gate.py` and `trust.py` are byte-identical before and after this phase** — pinned by a test comparing their hashes against constants recorded at phase start (Task 15). The `_TS_FORMAT` literal in `gate.py` is explicitly deferred to a future owner decision; only `pipeline.py`'s literal is cleaned up. A named, deliberately-untaken owner decision (gate branch-scoping — spec §2) is recorded in the spec; do not implement it.
- **Extra passes (security/skeptic/refuter) remain `--now`-only — oracle parity.** Background prepush reviews are finder-only plus the batching integration pass. No task touches the mode predicates in `passes.py` except to pin this with tests.
- **The v3 store migration is installed COMPLETELY and atomically in Task 3** (triage events + dedup_events + deliveries + the `worst_runtime_sec` review column). The shipped ladder runs a delta only when `user_version < target`, so later tasks may only *consume* v3 state, never extend it. Any DDL discovered missing later is a plan defect: stop, amend Task 3, re-migrate test DBs.
- Oracle parity: dispatcher/dedup/batching/oid tasks port oracle semantics. Porting references: the research report's audit (features A6–A8, A2, A9 mode-scoping, A14.3–A14.4, A16) and the oracle sources under `SKODUN_ORACLE_DIR/scripts/` (`grok-prepush-review.sh` dispatcher 3421–3631, dedup 217–286 + 3496–3562, batching 828–945 + 2112–2752; `.claude/hooks/surface-grok-findings.sh`). Where this plan and observed oracle behavior disagree, the oracle wins; amend the plan in the same commit (Deviations section below).
- **Method requirements (binding on every task):**
  - Between-task review is **by execution and mutation, never inspection**; every task below lists named **Mutations** the reviewer applies — each must be killed by a test. `PYTHONDONTWRITEBYTECODE=1` is mandatory during mutation runs.
  - Subagents do NOT inherit `SKODUN_ORACLE_DIR` — pass it explicitly; always report ran-vs-skipped counts.
  - Tests never touch `~/.local/share/skodun/skodun.db` or `~/.grok`: pin `SKODUN_DB` and every `SKODUN_<X>_BIN` to tmp paths. Fixture provenance rule applies to every new fixture class, MCP transcripts included: each fixture directory carries a README ledger (captured-vs-synthesized, wording source).
  - Full suite ≈ 2.5–4 min: generous timeouts, foreground runs.
- **Seam matrix, mandatory for every new surface** (`dispatch`, `worker`, `surface`, `mcp`, `install-hooks`, `triage --reopen`): exit code × {normal, closed stdout, `| head` under pipefail, `python -m skodun`, console script}, plus misuse → message never traceback; `dispatch`/`worker`/`mcp` add no-terminal variants (stdin closed, stdout dead-reader pipe, no controlling tty).
- MCP stdout carries **only** newline-delimited JSON-RPC; diagnostics to stderr; one stray stdout byte is a test failure.
- Detached workers: `start_new_session=True`; environment built from an explicit **allowlist** — `PATH`, `HOME`, `LANG`/`LC_ALL` (forced to a UTF-8 locale), every `SKODUN_*` — nothing else inherited; worker stderr → per-review log file (Task 10's log-dir API).

## File Structure

```
src/skodun/
├── chain.py             # NEW (T2): chain executor extracted from pipeline.py
├── batching.py          # NEW (T6–T8): split, plan, aggregate
├── dispatch.py          # NEW (T9–T11): dedup probe, dispatcher, supersede, shim install
├── delivery.py          # NEW (T12): undelivered query + surface rendering + ack
├── services.py          # NEW (T14): shared CLI/MCP service functions -> (status, text)
├── mcpserver.py         # NEW (T13–T14): stdio JSON-RPC loop + tool registry
├── contextpack.py       # modified (T4): source="oid"
├── gitio.py             # modified (T4–T5): blob reads, ref-range diff
├── pipeline.py          # modified (T2, T8, T10): extraction, batching, prepush entry
├── store.py             # modified (T1, T3, T10): close(), v3 migration, conditional finalize, log dir
├── triage.py            # modified (T3): event-stream effective state
├── cli.py               # modified (T3, T10–T15): new subcommands, services rewiring
├── config.py            # modified (T10): [dispatch] table
└── passes.py            # modified (T7): integration-pass prompt
tests/  (new: test_chain.py test_batching.py test_dispatch.py test_delivery.py
         test_mcpserver.py + tests/fixtures/mcp/*.jsonl + README)
examples/hooks/sessionstart-claude.sh, sessionstart-plain.sh   # T12
docs/phase3-acceptance.md                                       # T16
```

---

### Task 1: Store lifetime — `close()`, context manager, warning-clean store suite

**Files:** Modify `src/skodun/store.py`, `src/skodun/cli.py` (context form), `src/skodun/pipeline.py` (`_TS_FORMAT` literal only), tests.

**Interfaces:** `Store.close()` idempotent; `Store.__enter__/__exit__`; using a closed store raises `sqlite3.ProgrammingError` (not swallowed). All CLI paths use `with Store.open(...) as store:`. A dedicated test runs the store-touching test modules under `-W error::ResourceWarning` in a subprocess pytest and asserts zero failures. `gate.py` is NOT touched (byte pledge; its `_TS_FORMAT` literal is deferred). Also deduplicate `cli._fmt_binary` onto `pipeline._binary_is_absent`'s path-vs-PATH split (one definition).

- [ ] Failing tests → implement → sweep consumers → full suite green.
- [ ] **Mutations:** (a) make `close()` a no-op → killed by the post-close `sqlite3.ProgrammingError` assertion (NOT by the warning run: sqlite3 connections do not reliably emit `ResourceWarning` on 3.12 — the `-W error` sub-run is a supplementary regression net, not the mutation-killer); (b) re-inline a divergent copy of the binary-format split in `cli.py` → the one-definition test (assert `cli` imports the pipeline helper) must fail.
- [ ] Commit: `feat: store close/context manager, ResourceWarning-clean store suite (refs EPIC)`

### Task 2: Extract `chain.py` (behavior-preserving)

**Files:** Create `src/skodun/chain.py`; Modify `src/skodun/pipeline.py`; `tests/test_chain.py` holds only relocated imports, no rewrites.

**Interfaces:** the executor moves verbatim and gains its public name: `chain.run_chain` (same signature as the shipped `_run_chain`); `pipeline._run_chain` remains as a one-line compatibility alias (existing monkeypatch tests target it); every existing test passes **unmodified** (the proof of preservation). Later tasks call `chain.run_chain`.

- [ ] Move; full suite green with zero test edits.
- [ ] **Mutations:** drop the quota-cache write; invert the degraded-retry decrement; skip the accepted-provenance update — each killed by an existing test.
- [ ] Commit: `refactor: extract chain executor to chain.py ahead of dispatcher growth (refs EPIC)`

### Task 3: Complete v3 migration + triage event stream + `--reopen`

**Files:** Modify `src/skodun/store.py`, `src/skodun/triage.py`, `src/skodun/cli.py`, tests.

**Interfaces:**
- **One atomic v3 migration** (2 → 3), installing ALL Phase 3 DDL:
  - `triage_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ledger_key TEXT, finding_key TEXT, event TEXT CHECK(event IN ('dismiss','reopen')), review_id TEXT, branch TEXT, base_sha TEXT, file TEXT, line INTEGER, severity TEXT, title TEXT, reason TEXT, at TEXT)` — **`finding_key` is a column**, because the shipped `open_findings` tests membership by `finding_key` and the shipped `triage_for` returns a `finding_key`-keyed map (verified: `triage.py`/`store.py`); the migration **seeds one `dismiss` event per existing `triage` row** (copying its `finding_key` and every recorded field); the legacy `triage` table stays in place, read-only from now on (additive rule).
  - `dedup_events(at TEXT, branch TEXT, diff_hash TEXT, matched_review_id TEXT)` (consumed by T9).
  - `deliveries(review_id TEXT PRIMARY KEY, delivered_at TEXT, channel TEXT)` (consumed by T12).
  - `ALTER TABLE reviews ADD COLUMN worst_runtime_sec INTEGER; ALTER TABLE reviews ADD COLUMN pid INTEGER; ALTER TABLE reviews ADD COLUMN superseded_by TEXT` (consumed by T8/T10: stale recovery, pid attach, supersede audit). Whenever `pid`/`superseded_by` are written, the same statement updates the artifact JSON via `json_set` — indexed columns and artifact never diverge (the Phase 1 rule).
  - **The entire v3 delta — all DDL, the dismissal seeding, and the `user_version` stamp — executes in ONE explicit transaction** (`BEGIN IMMEDIATE` … `COMMIT`; the shipped ladder is non-transactional and `ALTER TABLE ADD COLUMN` is not replay-idempotent, so a crash between column-add and stamp would brick the next open on a duplicate column). A failure-injection test kills the migration mid-delta (monkeypatched exception after the ALTER) and asserts the DB reopens cleanly at v2 and migrates successfully on retry.
  - Future-version refusal before any DDL (shipped rule; extend the true-v0/v2 migration tests to v3, asserting a v2 DB gains all four deltas).
- **Effective triage state is the LAST EVENT BY `seq`** per ledger key — a monotonic total order; timestamps are display-only (one-second resolution cannot order same-second dismiss/reopen). `store.add_triage` now appends a `dismiss` event (the old `INSERT OR REPLACE` path retired — re-dismiss no longer overwrites history); `store.triage_reopen(...)` appends a `reopen` event; `store.triage_for(branch, base_sha)` keeps its SHIPPED return shape — a **`finding_key`-keyed** map of the findings whose last event (by `seq`) is `dismiss` — so `open_findings` and the gate need **no change**; `ledger_key` groups history; `store.triage_history(ledger_key)` returns all events in seq order.
- CLI: `skodun triage --reopen <review-id> <finding-index> "<reason>"` (reason through `validate_reason`, unchanged); exits 0 recorded / 1 refused / 2 not-found; `--list` renders `DISMISSED`/`REOPENED` from the event stream with both timestamps.

- [ ] Failing tests: seeded migration preserves existing dismissals' effect on the gate; reopen flips gate 0→1; re-dismiss flips back and history shows all three events; same-second dismiss→reopen→dismiss resolves by seq; placeholder/short reopen reasons refused; v2→v3 delta test covers all four DDL items.
- [ ] **Mutations:** order by `at` instead of `seq` (same-second test must fail); seed migration skipped (gate-continuity test must fail); reopen validates with a no-op validator (refusal test must fail).
- [ ] Seam matrix for the new flag. Commit: `feat: complete v3 migration, append-only triage events, audited reopen (refs EPIC)`

### Task 4: `contextpack source="oid"` + gitio blob reads

**Files:** Modify `src/skodun/contextpack.py`, `src/skodun/gitio.py`, tests.

**Interfaces:** `gitio.blob_bytes(repo, oid, path) -> bytes | None` (`git cat-file blob <oid>:<path>`; None on any failure). `contextpack.pack(..., source="oid", oid=<commit>)` reads from the commit tree — always the tree the ref-range diff came from. Worktree-only hardening (symlink walk, O_NOFOLLOW, FIFO) is structurally moot for object reads; binary detection, caps, omission vocabulary unchanged. Replaces the `NotImplementedError` at `contextpack.py:334`.

- [ ] Failing tests: oid pack returns COMMITTED content while the worktree differs (the load-bearing property); deleted path → `missing`; binary blob → `binary`; bad oid → all omitted `missing`, never a raise; byte-determinism.
- [ ] **Mutations:** read from the worktree instead of the blob (committed-vs-worktree test must fail); swap `missing` → silent skip (omission-accounting test must fail).
- [ ] Commit: `feat: oid-sourced context packing for the dispatcher (refs EPIC)`

### Task 5: Ref-range diff scope

**Files:** Modify `src/skodun/gitio.py`, tests.

**Interfaces:** `capture_ref_diff(repo, base_sha, local_oid) -> Diff` — `git diff --no-ext-diff --no-textconv <base_sha> <local_oid>`: commits only, no untracked, no working tree. Same `-z` status parsing between two oids; `diff_identity` unchanged. Zero-OID handling is the caller's (T10 skips deletions first).

- [ ] Failing tests: excludes untracked and uncommitted worktree edits; same content pushed twice → identical identity; rename/copy status records parsed.
- [ ] **Mutations:** swap `local_oid` → `HEAD` — killed by a test whose pushed oid is deliberately an OLDER commit than the checked-out `HEAD` and asserts the OLDER commit's bytes (a same-as-HEAD fixture would not distinguish them); drop `--no-textconv` (add a textconv-attribute fixture repo test that must fail).
- [ ] Commit: `feat: pushed-ref diff scope for the dispatcher (refs EPIC)`

### Task 6: Batch split (deterministic, byte-level)

**Files:** Create `src/skodun/batching.py`, `tests/test_batching.py`.

**Interfaces:** `split(diff: bytes, budget: int) -> list[Batch]`, `Batch(data: bytes, files: list[str], truncated: bool)`. Oracle A8 rules: raw bytes throughout; split at `diff --git` boundaries; over-budget file splits at `@@` with the header repeated; over-budget single hunk → own batch `truncated=True`; greedy order-preserving packing; `len(diff) <= budget` → one identical un-truncated batch.

- [ ] Failing tests: determinism; header repetition; floor flagging; invalid-UTF-8 splits bit-identically; every batch ≤ budget except floors; `@@`-count conservation in vs out.
- [ ] **Mutations:** drop header repetition; drop the floor flag; reverse packing order — each killed.
- [ ] Commit: `feat: deterministic byte-level diff batching (refs EPIC)`

### Task 7: Integration pass (cross-file seams, aggregate-participating)

**Files:** Modify `src/skodun/passes.py`, `src/skodun/batching.py`, tests.

**Interfaces:**
- `integration_prompt(batch_summaries, ...) -> bytes` — generic, slot-free: per batch its file list, `diff --git`/`@@` header lines (bodies omitted, ≤ 120 header lines/batch), one-line summary, findings; instructs ONLY cross-file problems. `REVIEW_CONTRACT`.
- **Reviewer selection:** role `integrator` if configured and enabled, else the finder's reviewer (mirrors the shipped `_pass_reviewer` preference pattern); runs through `chain.run_chain`, so it gets fallback support.
- **The integration pass is a full participant in the aggregate** (unlike `--now` extra passes, it is not optional annotation — it is coverage): its parse/degraded/unavailable outcome joins Task 8's aggregation formulas, its attempts and provenance persist under `integration{}` in the artifact (oracle A8), and a failed or degraded integration pass makes the aggregate untrustworthy.
- Checklist: per-batch prompts select mode `"batch"` (never cross-file); the integration prompt selects mode `"integration"` (core + cross-file only) — both shipped Phase 1 modes, first consumers; each batch's selection persisted in its `batches[]` entry.

- [ ] Failing tests: headers-only prompt; single-batch runs skip the pass; `(integration) ` title-tagging follows extra-pass tagging rules; cross-file checklist rules appear in the integration prompt and in NO batch prompt; integrator-role reviewer preferred when configured.
- [ ] **Mutations:** include hunk bodies in the prompt (headers-only test fails); run the pass on single-batch (skip test fails); select `"full"` checklist mode per batch (cross-file-leak test fails).
- [ ] Commit: `feat: cross-file integration pass over batch seams (refs EPIC)`

### Task 8: Batched pipeline wiring (both modes)

**Files:** Modify `src/skodun/pipeline.py`, `src/skodun/batching.py`, tests.

**Interfaces:**
- **Ordering (two-stage, race-safe):** a pre-lock capture computes a batch-count ESTIMATE used only to size the lock's stale ceiling conservatively (budgets multiply by `(batch_count + 1)` on top of chain-width scaling). **Under the lock, the diff is recaptured and the authoritative batch plan rebuilt** — the shipped pipeline captures under the lock precisely so identity, context and checklists come from one tree state, and a long lock wait can change the worktree; if the recaptured identity differs from the estimate's, the ceiling is re-derived from the larger of the two plans (never shrunk). Everything the review persists comes from the under-lock capture. **The holder's budget must be visible to WAITERS** (shipped waiters reclaim with their OWN stale argument, so a small-diff waiter would reclaim a live large-batch holder): the holder writes a `budget` sidecar file inside the lock directory (`<lock>/budget`, one line, seconds) beside the byte-pinned `owner` file — additive, legacy scripts parse only `owner`; skodun waiters use `max(own_ceiling, holder_budget)` for reclaim decisions. Pinned by a large-holder-vs-small-waiter test. RECORDED LIMITATION: a coexisting LEGACY waiter honors only its fixed 2580 s ceiling, so a batched foreground run longer than that could be reclaimed by the legacy scripts during shadow coexistence — documented, accepted (coexistence is transitional), and noted in the runbook. The record persists its own `worst_runtime_sec` (v3 column, Task 3); `recover_stale` **prefers the record's persisted budget** over recomputation (the oracle's own `.meta` solution, A14.3) — a multi-batch record is never reclaimed on a single-review ceiling.
- When `len(diff) > max_diff_bytes`: route through the orchestrator — small diffs NEVER enter it (pinned by test), so the unbatched path is untouched, not "identical", and no parity assertion is needed. A one-batch orchestrator seam exists for tests only (`_orchestrate(batches=[whole])`) to compare prompt bytes with the unbatched builder.
- Each batch = sub-review through `chain.run_chain` (full retry/fallback; no index row, no banner, no delivery; scratch tags `{tag}.b{i}`); then the integration pass (T7) when ≥ 2 batches; then ONE aggregated record at the FULL `diff_hash`:
  with ≥ 2 batches: `parse_ok = all(batches) and integration.parse_ok`; `degraded = any(batches) or integration.degraded`; `diff_truncated = any(batch.truncated)`; `stop_reason` = first abnormal across sub-reviews in batch order **then integration**; `integration{}` persisted. **With exactly one batch (the floor case): the integration pass does not run and its terms are NEUTRAL** — `parse_ok = all(batches)`, `degraded = any(batches)`, `stop_reason` from batches alone, and `integration{}` is ABSENT from the artifact (readers tolerate absence, the Phase 2 rule). Findings merged with batch provenance (`batches[]`: files, bytes, attempts, trust axes, checklist selection); severity recounted.
- **Aggregate `context_hash` is `""` always** — batched aggregates are deliberately never dedup-suppressible (T9's `""` rule): the cost is a redundant re-review of rare oversized diffs; the alternative risks certifying unpacked context. Recorded as a deliberate conservative decision.
- Extra passes: `--now`-only predicates unchanged (Global Constraints) — a batched foreground run applies security/skeptic/refuter to the AGGREGATE per existing eligibility; a batched prepush run runs finder batches + integration only.

- [ ] Failing tests (fake CLIs): over-budget diff → one trustworthy aggregate, N batches recorded, provider invoked N+1 times; seeded `Cancelled` in batch 2 → aggregate degraded ⇒ gate 2, `stop_reason` is batch 2's; failed integration pass → aggregate `parse_ok=False` ⇒ gate 2; unavailable integration provider (dead binary, no fallback) → same; seeded floor batch → `diff_truncated` ⇒ gate 2; small diff never enters the orchestrator; one-batch seam prompt bytes == unbatched prompt bytes; persisted `worst_runtime_sec` respected by `recover_stale` (backdated multi-batch running record with a large persisted budget is NOT reclaimed at the single-review ceiling; without the column it would be).
- [ ] **Mutations:** `any` → `all` in degraded aggregation; first-abnormal → last-abnormal; drop integration from `parse_ok`; skip persisting `worst_runtime_sec` — each killed.
- [ ] Commit: `feat: batched review with aggregated full-identity artifact, both modes (refs EPIC)`

### Task 9: Dedup probe (dispatcher-only) + audited suppressions

**Files:** Create `src/skodun/dispatch.py` (probe), tests.

**Interfaces:** `dedup_probe(store, repo, diff: Diff, oid, branch: str) -> Suppression | None` (`branch` is the dispatcher's normalized pushed branch — NEVER derived from `current_branch(repo)`, which is wrong for a non-checked-out ref; it feeds the suppression event), oracle 3-way protocol with the SHIPPED `context_hash` semantics stated precisely (verified against `pipeline.py`: `""` is written when packing is disabled or the diff empty; a real pack writes the sha256 of the packed bytes, empty pack included; `NULL` exists only on legacy-imported rows):
- newest trustworthy record for `diff_identity(diff.data)`; none → review.
- record's ARTIFACT `context_hash` field **absent or JSON `null`** (legacy import — verified: the shipped importer omits the key from `artifact_json`, while `save_review` writes the SQL column as `""` for a missing key; the probe therefore reads the ARTIFACT returned by `latest_trustworthy_for`, never the column) → suppress without packing.
- record `context_hash` **non-empty** → pack the candidate's oid context with the same settings, compare hashes; equal → suppress; else review.
- record `context_hash` **`""`** → review, ALWAYS — shipped `""` is ambiguous between packing-disabled and nothing-packed, and an ambiguous match must not skip a review. (This also covers batched aggregates by construction, per T8.) Tests distinguish all three artifact states: key missing, key `null`, key `""`.
- ANY exception anywhere → review, logged to stderr; **no code path from error to suppression** (pinned by a monkeypatched-explosion test on every probe step).
- A suppression writes a `dedup_events` row (v3 table) `{at, branch, diff_hash, matched_review_id}`.
- **Foreground never dedups, proven behaviorally:** an end-to-end `--now` review with `dedup_probe` monkeypatched to explode runs to completion with the provider invoked and the probe never called (not a grep test).

- [ ] Failing tests: all four protocol branches; explosion-at-each-step → review; suppression recorded; foreground behavioral test.
- [ ] **Mutations:** treat `""` as NULL (branch test fails); swallow the pack-compare exception into suppress (explosion test fails); skip the event write (audit test fails).
- [ ] Commit: `feat: three-way dedup probe with audited suppressions (refs EPIC)`

### Task 10: Dispatcher, worker, shim, `[dispatch]` config

**Files:** Modify `src/skodun/dispatch.py`, `src/skodun/config.py`, `src/skodun/cli.py`, `src/skodun/pipeline.py`, `src/skodun/store.py` (conditional finalize + log dir), tests (`test_dispatch.py`).

**Interfaces:**
- **`Dispatch` dataclass** (`config.py`, own `[dispatch]` table, validated like `Defaults`): `enabled: bool = True`, `timeout_sec: int = 240`, `timeout_retries: int = 0`, `dedup: bool = True`, `large_prompt_bytes: int = 80_000`. Merge rule: the worker builds its effective `Defaults` as `replace(defaults, timeout_sec=dispatch.timeout_sec, timeout_retries=dispatch.timeout_retries)`; when any single built prompt exceeds `large_prompt_bytes` (per-prompt rule; in a batched run each batch prompt is measured individually), that attempt's background cap escalates to `defaults.timeout_sec` (the foreground cap) (oracle A14.7). **The reserved `worst_runtime_sec` is always computed from the ESCALATED cap** — reservation happens before any prompt exists, so the budget is unconditionally conservative; a generous stale ceiling only delays recovery, while an undersized one would reclaim a live worker. All other keys come from `[defaults]` untouched. (The spec's `large_prompt_escalation` name is superseded by `large_prompt_bytes` — spec amended.)
- **Atomic reservation lease (race-closing):** `skodun dispatch` reads pre-push ref lines from stdin (`<local ref> <local oid> <remote ref> <remote oid>`). **Accepted refs: `refs/heads/<name>` only, normalized to the short `<name>`** (that is the branch every downstream surface uses); tags and other ref classes are skipped with a stderr note. Per accepted ref: skip zero-OID deletions; resolve base (`remote oid` if non-zero else merge-base vs main refs); `capture_ref_diff`; dedup probe (when enabled, taking the normalized branch as a parameter); then `store.reserve_prepush(branch, head, base_sha, diff_hash, worst_runtime_sec) -> Reservation` — **one `BEGIN IMMEDIATE` transaction** that (a) **re-checks dedup INSIDE the lease**: if a trustworthy terminal record for `diff_hash` exists (the probe's own criteria — a racing dispatcher may have finalized between our probe and our reservation), no row is inserted and the result carries `suppressed_by=<id>` (the dispatcher records the dedup event); else (b) marks every still-`running` `mode="prepush"` row of the branch `superseded` with `superseded_by=<new id>`, and (c) inserts the new `running` row with `pid=NULL`. `Reservation = {record_id | None, suppressed_by | None, superseded: [{id, pid}]}` — the retired set is RETURNED by the transaction, never re-queried (a post-hoc query races). SQLite's write lock serializes racing dispatchers: whichever transaction commits second supersedes the first's row. Only then is the worker spawned; the dispatcher **conditionally attaches the pid** (`UPDATE … SET pid=? WHERE id=? AND status='running' AND pid IS NULL`) — if the attach reports no row (a racing dispatch superseded us between reserve and attach), the just-spawned child is terminated. The old-worker SIGTERM (below) happens after the transaction, outside it.
- **Conditional finalization (store API):** `store.finalize_review(record_id, rec) -> bool` — runs the SAME strict normalization as `save_review` (extract one shared `_normalize_record` routine: strict-bool axes validation, trust recomputed, caller-supplied `trustworthy` overwritten, indexed columns AND artifact JSON updated together), then applies it via a single `UPDATE ... WHERE id=? AND status='running'`; returns False (and changes nothing) if the record is no longer running. A worker cannot bypass the persistence chokepoint (pinned: non-bool axes raise; a `trustworthy=True` lie on a degraded record is overwritten) and a superseded/recovered record can never be overwritten by its late worker. `store.set_status` keeps its unconditional behavior for the recovery path only.
- **Worker:** hidden subcommand, exact argv: `skodun worker --record-id I --repo P --branch B --local-oid S --base-sha S --base-ref R`. It re-derives the diff via `capture_ref_diff`, **verifies `diff_identity` equals the reserved record's `diff_hash`** (mismatch → finalize failed, reason recorded — the push moved under us), runs the pipeline in `mode="prepush"` with oid context and the `[dispatch]` overlay, finalizes conditionally. Store located via `SKODUN_DB` (env allowlist passes it); worker stderr → `store.log_dir() / f"{record_id}.log"` where `store.log_dir()` is `<db-path>.logs/` (created lazily; API added this task).
- **Detach:** `subprocess.Popen([sys.executable, "-m", "skodun", "worker", ...], start_new_session=True, stdin=DEVNULL, stdout=DEVNULL, stderr=<log file>, env=<allowlist>)`.
- **Supersede signalling** (oracle A14.4): the reservation transaction already retired the rows (with `superseded_by` persisted atomically — Task 12 renders it) and RETURNED them in `Reservation.superseded`; afterwards, for each returned row with a non-NULL pid, SIGTERM only a pid whose `ps -o args=` still names the **skodun worker entrypoint** (pid-reuse guard); an unconfirmable pid gets no signal — and because finalization is conditional, a still-live unconfirmed worker that later finishes finds its record already terminal and its finalize returns False (pinned by a live-unconfirmable-worker test). `--now` records never touched.
- **Pre-record failures are durable:** any dispatch failure after ref parsing (config load, git failure, probe crash → probe already defaults to review; spawn failure) writes a fully-shaped `failed` review record — `mode="prepush"`, the branch, `diff_hash` if computed else `""`, `parse_ok=False`, `failure_reason` — so Task 12's delivery surfaces it. `dispatch` itself **always exits 0** to the push (a hook must never block on review machinery) except for its own usage errors; every failure is a loud record + stderr line, never silence, never a blocked push.
- **Shim (`skodun install-hooks`):** the hook directory is resolved via `git rev-parse --git-path hooks` — NEVER a hard-coded `.git/hooks` (linked worktrees have a `.git` file; `core.hooksPath` may point elsewhere; both tested). The written pre-push hook **tees stdin to a temp file** first; runs any pre-existing hook (preserved as `pre-push.pre-skodun` beside the resolved hook) with the original argv (`"$@"` — remote name and URL) and the buffered bytes on stdin, propagating a non-zero exit (the push fails as it would have); then pipes the same buffered bytes to `skodun dispatch "$@"` — and `dispatch` therefore accepts **two optional positional arguments** (remote name, remote URL — git's standard pre-push argv), recorded into failure notes but otherwise unused; without them argparse would reject the shim's call, and a usage error is the one dispatch path that exits non-zero, i.e. it would block the push (pinned by a test invoking dispatch exactly as the shim does). Idempotent: re-install detects the skodun marker line and replaces only its own shim. A foreign un-backed-up hook refuses without `--force`; `--force` backs it up and chains it (never discards it). Bypasses: `SKODUN_PREPUSH_SKIP=1` (one push, recorded to stderr), `git config skodun.prepush false` (disabled, recorded to stderr).
- `recover_stale` swept on every dispatch (reads persisted `worst_runtime_sec` per T8).

- [ ] Failing tests: ref parsing incl. deletions; reservation-then-spawn ordering (record exists before the worker process does); zero-delay double dispatch → exactly one terminal reviewed record, the other superseded; conditional finalize refuses on superseded/recovered records; worker verifies identity and records mismatch; pid-reuse guard; live-unconfirmable-worker cannot resurrect its record; shim tee feeds identical bytes and argv to both consumers and propagates the old hook's failure; both bypasses; spawn failure → durable failed record + dispatch exit 0; env allowlist (a poison env var is not inherited; `SKODUN_DB` is); seam matrices for `dispatch`/`worker`/`install-hooks`.
- [ ] **Mutations:** make `finalize_review` unconditional (supersede-overwrite test fails); drop the identity re-check in the worker (moved-push test fails); spawn before reserving (race test fails); skip the tee and run the old hook directly off live stdin (chained-hook-bytes test fails).
- [ ] Commit: `feat: pre-push dispatcher with reserved records, conditional finalize, chained shim (refs EPIC)`

### Task 11: Dispatcher trust-boundary drills (tests only)

**Files:** `tests/test_dispatch.py`.

Executable drills with fake CLIs in tmp repos: SIGKILL a worker mid-run → `recover_stale` marks failed (via persisted budget) → gate 2; zero-delay race → one terminal reviewed record per content; backdated running record → recovered; branch deleted then another dispatched → no interference; a worker finishing AFTER being superseded leaves the superseded status intact.

- [ ] Write; green. Commit: `test: dispatcher trust-boundary drills (refs EPIC)`

### Task 12: Delivery — undelivered query, `surface`, hook templates

**Files:** Create `src/skodun/delivery.py`, `examples/hooks/sessionstart-claude.sh`, `examples/hooks/sessionstart-plain.sh`; Modify `src/skodun/cli.py`, tests.

**Interfaces:**
- `undelivered(store, branch) -> list[dict]`: rows with `mode="prepush"`, **`source="skodun"`** (legacy-imported archive rows are never surfaced — pinned by an imported-prepush regression test), **terminal status only** (`{clean, degraded, failed, superseded}`; `running` rows are excluded and can never be acknowledged — a round is delivered only after its story is final), and no `deliveries` row.
- `skodun surface [--branch B] [--hook-format claude|text] [--include-delivered]`: findings rounds render normally; failed/degraded rounds render with the explicit line `NO REVIEW HAPPENED — this round reports nothing because it said nothing, not because it found nothing` plus `failure_reason`; superseded rounds render one line naming the superseding record via the persisted `superseded_by` field (written atomically by the reservation transaction — never inferred from branch/time). **Ack-after-emit:** quiet rounds (trustworthy, 0 findings) marked delivered immediately; content-bearing rounds marked ONLY after the full emit succeeds (flush + no exception) — a failed emit leaves the round undelivered (pinned by a failing-stream test); delivered-twice is the designed failure mode. `--hook-format claude` emits the SessionStart JSON envelope (`{"systemMessage": ..., "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}`); `text` plain lines. Hook templates call the command; installed by instruction (README), never written into a user repo by skodun.

- [ ] Failing tests: failure wording exact; running row invisible, then visible after finalization (transition test); legacy-source rows never surface; ack-after-emit both directions; quiet-round immediate ack; `--include-delivered` replays; claude JSON valid with both keys; seam matrix (closed stdout mid-emit ⇒ still undelivered, non-zero exit).
- [ ] **Mutations:** mark-then-emit ordering (failing-stream test fails); include `running` in eligibility (transition test fails); drop the `source` filter (legacy regression fails).
- [ ] Commit: `feat: delivery ledger and surface command — silence is never a verdict (refs EPIC)`

### Task 13: MCP stdio server core (hand-rolled JSON-RPC)

**Files:** Create `src/skodun/mcpserver.py`, `tests/test_mcpserver.py`, `tests/fixtures/mcp/*.jsonl` + `tests/fixtures/mcp/README`; Modify `src/skodun/cli.py` (`skodun mcp`).

**Interfaces:** newline-delimited JSON-RPC 2.0 over stdio, protocol decisions pinned NOW (not left to the implementer):
- Methods: `initialize` (pinned `MCP_PROTOCOL_VERSION` constant; echo the client's version when recognized, else ours; `capabilities: {tools: {}, prompts: {}}`; `serverInfo{name:"skodun", version}`), `notifications/initialized` (no reply), `ping` → `{}`, `tools/list`, `tools/call`, `prompts/list`, `prompts/get`.
- **JSON-RPC arrays (batch requests) are rejected** with `-32600` (MCP removed batching; one request per line). Unknown *methods* → `-32601`; malformed params → `-32602`; parse errors → `-32700` with `id: null`. **Unknown notifications are ignored silently.** Any request other than `initialize`/`ping` before initialization → `-32002` "server not initialized". Line length cap 8 MiB: an oversized line is drained, answered `-32700` with `id: null`, and the loop continues. EOF → clean exit 0; **EOF with a review in flight**: the server terminates the review's provider process group through the runner's existing kill path — the pipeline's `finally` downgrades the record to `failed` and releases the lock (the same machinery Ctrl-C uses) — then exits 0. Pinned by a slow-fake-CLI test: start review over MCP, close stdin, assert process exit, record `failed`, lock gone.
- Tool results (consumed by T14): `{content: [{type: "text", text: <cli text>}], isError: <status != 0>}` plus `structuredContent: {status: <int>}`.
- **Concurrency model:** the read loop handles every method inline EXCEPT `tools/call {name: "review"}`, which runs on a single background worker thread (capacity 1; a second review while busy → tool-level error result "review already in flight"). Every tool call opens its own `Store` (per-call connections — sqlite connections are thread-bound; pinned by a cross-thread test). Stdout writes go through one lock; a response is exactly one `write` + flush.
- **Transcript harness:** each `tests/fixtures/mcp/*.jsonl` holds request lines + expected-response predicates; cases include garbage bytes, unknown method, malformed params, a batch array, pre-init call, interleaved notifications, immediate EOF; the oversized-line case is **generated at test runtime**, not committed. Harness asserts stdout parses line-by-line as JSON-RPC with zero residue. Fixture README ledger: every transcript is synthesized BY CONSTRUCTION from the pinned protocol decisions above (recorded as such — the capture-provenance rule's documented exception, since no client emits attack transcripts), except the version-negotiation case, which SHOULD be captured: **probe step** — run the installed `claude`/`codex` CLI's MCP client against a stub echo server, capture the real `initialize` request into the fixtures (documented-skip: if neither CLI can act as a headless client, pin to the current published spec revision and record it in the README).
- Seam matrix incl. dead-reader stdout pipe → clean exit, no traceback; stdin closed immediately → exit 0.

- [ ] Failing tests (harness + predicates first) → implement loop + dispatch table.
- [ ] **Mutations:** drop the `id` echo on one method; swap `-32601`/`-32602`; emit one log line to stdout; run `review` inline (busy test must fail) — each killed.
- [ ] Commit: `feat: stdlib stdio MCP server core with transcript-pinned protocol (refs EPIC)`

### Task 14: Services extraction + MCP tools (the CLI mirror)

**Files:** Create `src/skodun/services.py`; Modify `src/skodun/cli.py`, `src/skodun/mcpserver.py`, `src/skodun/pipeline.py` (banner emission moves to callers), tests.

**Interfaces:**
- **Step 0 — extraction, behavior-preserving:** the logic inside the relevant `_cmd_*` bodies moves to `services.py` functions returning `(status: int, text: str)` with NO printing and NO argparse coupling: `svc_gate(repo)`, `svc_review(repo, ...)`, `svc_log(branch, limit)`, `svc_surface(branch, fmt, include_delivered) -> (status, text, pending_acks: list[review_id])` — the service acknowledges QUIET rounds itself, immediately (trustworthy, zero findings — nothing deliverable can be lost; Task 12's rule), and returns `pending_acks` holding ONLY the content-bearing rounds, which the service never acknowledges. **Each transport acknowledges only after its own real write succeeds**: the CLI emits the text through a variant of `_emit` that REPORTS write/flush failure (the shipped `_emit` swallows it — a delivery emit must not) and calls `delivery.acknowledge(store, ids)` only on success; the MCP server acknowledges after the JSON-RPC response line is written AND flushed (the tool handler returns the pending ids alongside the result; the write loop performs the ack post-flush). Buffering is never "emit success" anywhere, `svc_triage_list(review_id)`, `svc_triage_dismiss(review_id, index, reason)`, `svc_adopt_refuter(review_id, index)`, `svc_triage_reopen(review_id, index, reason)`. The CLI `_cmd_*` functions become thin: parse args → call service → `_emit(text)` → return status. **`run_review` stops printing but keeps its EXACT shipped signature and `-> dict` return** (least-disruptive option): banner emission is deleted from the pipeline (the record already contains everything `trust.banner` needs — `svc_review` derives the banner via the existing `trust.banner(record)`, one definition); progress notes go through an injectable sink parameter defaulting to stderr; the CLI prints the banner as its last stdout line (contract preserved); the MCP tool includes it in the result text — a process-global `redirect_stdout` is forbidden (it would corrupt concurrent JSON-RPC stdout). CLI tests pass unmodified (the proof for the extraction); pipeline tests that captured stdout are updated to read the returned banner — the ONLY test edits this task may make, listed in the commit message.
- MCP tools = exactly those services, listed in `tools/list` with explicit `inputSchema` (JSON Schema, required fields, types). **Tool-list snapshot test pins the exact list** — adding a tool is a reviewed decision. Dismissal-shaped tools return the SAME refusal text the CLI prints for the same bad input (pinned by parity tests comparing strings across both surfaces). `review` documented long-running, one in flight (T13). Prompts: `review-now`, `gate-check` (static).
- No bulk tool exists; nothing beyond the CLI's own surface.

- [ ] Failing tests: CLI suite unmodified and green after extraction; tool-list snapshot; refusal-string parity (placeholder reason, confirmed-verdict adopt, thin reasoning) CLI-vs-MCP; gate via MCP == gate via CLI on the same store; busy-review error; MCP surface ack parity: the ack happens only after the JSON-RPC response line's write+flush (a test transport whose flush raises leaves the rounds undelivered); no buffer completion ever acks.
- [ ] **Mutations:** reimplement one validator inside `mcpserver.py` with a subtly different message (parity test fails); divergent copy of `svc_gate` logic in `cli.py` (one-definition import test fails).
- [ ] Commit: `feat: shared services; MCP tools mirror the CLI exactly — nothing more (refs EPIC)`

### Task 15: Seam matrices + trust-boundary byte pin

**Files:** tests.

- Seam matrix parameterized over the six new surfaces (Global Constraints list); `gate.py`/`trust.py` byte-identity pin (sha256 constants recorded at phase start, asserted).
- [ ] Write; green. Commit: `test: seam matrices for phase 3 surfaces, trust-boundary byte pin (refs EPIC)`

### Task 16: Docs, examples, live acceptance

**Files:** Modify `README.md`, `examples/`; Create `docs/phase3-acceptance.md`.

- README: `install-hooks` setup, surface/hook wiring, MCP client config snippets (claude/codex CLIs), new subcommand rows.
- Live acceptance runbook — **prerequisites first** (each CLI responds, model ids valid, quota present), then the spec's seven criteria with pasted evidence each (suite counts reconciled both modes; live push → record → fresh-session surface → dedup on re-push with event → re-review after edit; failure round (a NO-FALLBACK reviewer with a dead binary — a chain head with a live fallback would recover, not fail) surfaces with exact wording; supersede race + SIGKILL recovery live; over-budget real diff batched + integration, seeded truncated batch gates 2, seeded integration failure gates 2; real MCP client end-to-end incl. one audited dismissal visible from the CLI, protocol suite zero-residue; reopen flips gate 0→1 with history).
- [ ] Execute; paste evidence; full suite both modes; commit: `docs: phase 3 live acceptance evidence (refs EPIC)`

---

## Self-Review Notes

- Spec coverage: T1 store lifetime; T2 extraction; T3 complete v3 + events + reopen; T4 oid; T5 ref scope; T6–T8 batching with integration as aggregate participant; T9 dedup with shipped `""` semantics; T10–T11 dispatcher with reservation/conditional-finalize/durable failures; T12 delivery with terminal-only, skodun-source-only eligibility; T13–T14 MCP with pinned protocol decisions and services extraction; T15 seams+pin; T16 acceptance. Owner decisions honored (stdlib MCP, both-mode batching, full mirror, append-only reopen — now as an event stream, which strengthens append-only rather than weakening it).
- Deliberate decisions restated: extra passes stay `--now`-only (oracle parity — the spec's earlier contrary sentence is amended); dispatch always exits 0 to the push, failures are durable records; reservation-before-spawn closes the dispatch race; conditional finalization makes terminal statuses immutable to late workers; batched aggregates carry `context_hash=""` and are never dedup-suppressible; probe errors always review; `--now` never dedups (behavioral test, not grep); delivery acks only terminal, skodun-source rounds, after emit; MCP rejects batch arrays, caps lines, and runs one review at a time on its own thread with per-call store connections; the gate branch-scoping question is a named, deliberately-untaken owner decision.
- Open risks named plainly: the MCP version probe (T13) and the shim's interaction with unknown existing hooks (T10) start with probes and have documented outcomes. Batching (T6–T8) is the largest port; its aggregation formulas and budget persistence are mutation-checked. The services extraction (T14 step 0) is the largest refactor of shipped code; its proof is the unmodified CLI test suite.

## Deviations recorded at implementation time

(None yet. When an installed binary or the oracle contradicts this plan, the implementer amends this section in the same commit — the Phase 2 pattern.)
