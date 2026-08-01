# Prompt: continue skodun at Phase 5

> Hand this to Grok (or any capable coding agent). It is self-contained apart from
> the repository itself. Written 2026-08-01, handing off from a Claude Code session
> that ran Phases 3 and 4 and is out of budget.

---

You are continuing work on **skodun**, at Phase 5. Four phases are shipped, merged and
closed. Your job is to plan and implement the next one. Nothing here is a fresh start —
read before you write.

## What skodun is

A local, provider-neutral **code-review server**: a Python CLI that runs a diff through an
AI coding CLI you are already subscribed to (rather than an API key), persists the review
to SQLite, and **gates pushes on the result**. It is a port of a mature shell+Python review
system — "the oracle" — that still runs in a separate repository.

Repository: `github.com/vega113/skodun`, public, Apache-2.0. All paths below are relative
to its root. Working checkout: `/Users/vega/devroot/skodun`, on `main` at `c313e34`.

**The oracle is located ONLY via the `SKODUN_ORACLE_DIR` environment variable.** Never
hardcode a path to it — not in code, not in a test, not in a plan. Locally:
`export SKODUN_ORACLE_DIR=/Users/vega/devroot/tubescribes`. Subagents do not inherit it;
pass it explicitly and always report ran-vs-skipped test counts, because oracle-gated tests
silently skip without it.

## Read these first, in this order

1. `README.md` — the shipped surface: thirteen subcommands, the gate contract, the triage
   ledger, the MCP server, "One store per repository", and **"Known limitations"**, which
   is an honest list of what is still wrong.
2. `docs/2026-07-27-review-server-research.md` — the original architecture decision and the
   phased roadmap this project has been following.
3. `docs/superpowers/specs/2026-07-31-review-round-cutoff-design.md` — **read this one
   properly.** It is the project's policy on when a review loop should stop, it is backed by
   measurement on skodun's own code, and R2/R3 in it are live Phase 5 candidates.
4. `examples/AGENTS.md` — the client-facing protocol. It tells an agent how to drive skodun
   and, more importantly, when to stop reviewing. You are such an agent; it applies to you.
5. The phase plans in `docs/superpowers/plans/`, especially each one's **Global Constraints**
   and its **"Deviations recorded at implementation time"** section. Those deviations are
   load-bearing decisions, most pinned by tests that assert both what the oracle does and
   what skodun does. Do not weaken one without an explicit owner decision.
6. `src/skodun/` — particularly `store.py`, `pipeline.py`, `chain.py`, `dispatch.py`,
   `adapters/`, and `mcpserver.py`.
7. Closed epics #1, #2, #3, #13 and their closing comments, for what was actually proven
   rather than merely built.

## Where the work is

**Epic #23 — Phase 5.** It lists four candidate groups: the `junie` adapter (probably the
spine), R2/R3 review-loop ergonomics, operational debt, and what remains uncut from earlier
phases. Read it. **Deciding the scope is part of your job** — say what you cut and why, the
way the earlier phases did.

## How this project works — non-negotiable

These are not style preferences. Each exists because something went wrong once.

- **`src/skodun/gate.py` and `src/skodun/trust.py` are byte-identical across four phases**,
  pinned by sha256 constants in `tests/test_seams.py`. No task changes either. If a design
  seems to require it, stop and ask the owner — twice now the right answer was to route
  around it (the gate is content-addressed on `diff_hash` and never consults a branch or a
  repository, deliberately).
- **Fail closed, always.** Exit `0` clean or every finding triaged · `1` findings open ·
  `2` no trustworthy review covers this content. Every unexpected exception maps to `2`,
  never `1`. A failed review is not a passed one.
- **Runtime is stdlib-only.** Python ≥ 3.12. `pytest` is the only dev dependency. The MCP
  server is hand-rolled JSON-RPC — no SDK. Do not add a dependency.
- **Committed code is fully generic.** No machine paths, no one project's private details
  in prompt text or config defaults. Repo-specific tables live in config with generic
  defaults; the oracle's own tables ship as `examples/scala-angular-monorepo.toml`.
- **The store is forward-only.** v5 is shipped. The ladder runs a delta only while
  `user_version < target`, so **a phase's entire schema change must land in ONE atomic v6
  delta** — later tasks may consume it, never extend it. An older build refuses a newer
  store, by design. `ALTER TABLE ADD COLUMN` is not replay-idempotent, so the delta runs
  inside one `BEGIN IMMEDIATE` carrying its own version stamp, with a mid-delta
  failure-injection test. Copy the v3/v4/v5 shape exactly.
- **MCP stdout carries only newline-delimited JSON-RPC.** Diagnostics go to stderr. One
  stray byte is a test failure.
- **Tests never touch the real store or a real provider.** Pin `SKODUN_DB`,
  `GIT_CONFIG_GLOBAL` and every `SKODUN_<X>_BIN` to tmp paths. The suite drives fake CLIs.

## Method — this is the part that catches things

Phases 1–4 used **subagent-driven development**: a fresh subagent per plan task, then a
between-task review **by execution and mutation, never inspection**. Every task names its
mutations; each must be killed by a test, and the task states *which* test kills it.

Hard-won specifics, all from real failures in this project:

- **A mutation nothing kills is worse than no mutation** — it makes the gate pass while
  proving nothing. In Phase 4, **six** planned mutations could not be killed as written, and
  one survived into a merged task before review caught it. Verify each mutation actually
  bites; when one survives, say so plainly rather than quietly strengthening the test.
- **Commit before mutating.** The revert step (`git checkout -- <file>`) takes an
  uncommitted fix with it. This destroyed work three times in one session.
- **Run tests in the foreground**, or as a background command you poll — the full suite is
  ~11–13 minutes. Use `PYTHONDONTWRITEBYTECODE=1` for mutation runs.
- **Plans get adversarially reviewed before execution.** Phase 3's went 17 rounds; Phase 4's
  first draft had **8 Critical defects**, two of which would have shipped a phase that was
  silently inert. The review changes designs, not just prose. Budget for it.
- **Verify a review finding against the code before acting on it.** In Phase 4, of the
  external bots' findings, some were valid and Critical, and one was refuted by the
  codebase's own documented contract. Both outcomes are fine; guessing is not.

## The review-loop stopping rule — it applies to your own work

Each round of fixes is new code the next round will review; that does not converge. Measured
here: round 1 found 11 findings, round 2 found 6 — with **zero** repeats and **four of six**
in code the first round's fix had just written.

So: judge every finding by its **consequence**, never its severity label. Fix now only for a
change that does not work as described, a false safety promise, a wrong user-facing claim,
data corruption, or something needing a migration to undo. Everything else is
`skodun triage --defer <review-id> <n> <tracking-ref> "<reason>"` — the reference is
**mandatory**, because an unfiled deferral and an ignored finding are the same artifact.

**Stop when `skodun gate` exits 0** — clean *or every finding triaged*. Not "the reviewer
found nothing", which for a real change may never happen. Escalate to the owner instead of
running another round when a round raises a must-fix finding in the previous fix's own code.

## Environment as of the handoff

- `skodun 0.3.0` is installed as an **editable pipx app** from `/Users/vega/devroot/skodun`
  and is on `PATH`. `git pull` there updates it — but a running MCP server holds the build
  it started with, so **restart the client** after upgrading and confirm with
  `serverInfo.version`.
- It is wired as an MCP server in **Codex** (`~/.codex/config.toml`), **Claude Code**
  (`~/.claude.json`, user scope) and **Grok** (`~/.grok/config.toml`) — all pointing at that
  one install. `grok mcp doctor` is the best diagnostic: it checks the command resolves, the
  process starts, the handshake completes, and counts tools. Nine tools: `gate`, `review`,
  `log`, `surface`, `triage_list`, `triage_dismiss`, `triage_defer`, `adopt_refuter`,
  `triage_reopen`, plus prompts `review-now` and `gate-check`.
- **Providers:** `agy` (google) works and is the configured finder in `.skodun.toml`.
  `codex` (openai) is **out of credits until 2026-08-05**. `grok` (xai) returns HTTP 402.
  Note `agy` passes its prompt as an argv word and refuses above ~120 KB, so a large change
  reviewed through it splits into more, smaller batches — that is per-provider budget
  machinery working, not a fault.
- The real store `~/.local/share/skodun/skodun.db` is at **v5**, 5261 reviews, 4765
  trustworthy, 257 legacy dismissals seeded into `triage_events`. Pre-v5 rows carry
  `repo IS NULL` and are invisible to the repo-scoped queries — expected, so `skodun
  surface` will not deliver anything recorded before 2026-08-01.
- Baseline: **3089 tests pass / 1 skipped** with the oracle; without it ~2928 pass / 160
  skip, and the counts must reconcile (the delta is exactly the oracle-gated set).

## What to produce

Follow the shape the earlier phases used:

1. **A design spec** at `docs/superpowers/specs/YYYY-MM-DD-skodun-phase5-design.md` —
   owner-approved before planning. Record the decisions *and what you rejected*, including
   anything you got wrong and corrected; the Phase 4 spec has a "Corrections to the approved
   spec" section for exactly that reason.
2. **An implementation plan** at `docs/superpowers/plans/YYYY-MM-DD-skodun-phase5.md` —
   task-by-task, each with the failing test written out, the exact code, the exact commands,
   and named mutations. Every code block must be transcribable verbatim: a helper or
   signature that does not exist as written is a plan defect. Have it adversarially reviewed
   against the shipped source before executing.
3. **The implementation**, task by task, commit per task (`refs #23`), full suite green in
   both oracle modes with the counts reconciled, and a PR per coherent chunk.

Ask the owner rather than guessing when a decision is theirs: scope cuts, anything touching
the byte-pinned files, a schema change, or a policy that would let something ship
unreviewed.
