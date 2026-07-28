# Prompt: plan the skodun Phase 2 epic

> Hand this to a planning agent. It is self-contained apart from the repo itself.

---

Plan **Phase 2** of `skodun` and produce two deliverables: a GitHub epic issue and an
authoritative implementation plan file. Do not write implementation code.

## What skodun is

`skodun` is a local, provider-neutral code-review server: a Python CLI that runs a diff
through a model CLI, persists the review to SQLite, and gates pushes on the result. It is
a port of a mature shell+Python review system ("the oracle") that still runs in a separate
repository. You are working in the skodun repository itself (public, Apache-2.0,
`github.com/vega113/skodun`); paths below are relative to its root. The oracle checkout is
located only via the `SKODUN_ORACLE_DIR` environment variable — never hardcode a path to
it, in a plan or anywhere else.

Phase 1 is complete, merged to `main`, and closed as epic #1. Read these before planning
anything — in this order:

1. `README.md` — the shipped surface.
2. `docs/2026-07-27-review-server-research.md` — the architecture decision and the phased
   roadmap. Its "Suggested roadmap" section defines Phase 2's remit.
3. `docs/superpowers/plans/2026-07-27-skodun-phase1.md` — the Phase 1 plan. Read its
   **Global Constraints** and, especially, its **Self-Review Notes → "Known intentional
   deviations from legacy"** list. Those deviations are load-bearing safety decisions, each
   pinned by a test that asserts *both* what the oracle does and what skodun does. Phase 2
   must not weaken any of them.
4. `docs/shadow-mode.md` — the Phase 1 acceptance evidence and its honest caveats.
5. The source under `src/skodun/`, particularly `adapters/`, `config.py`, `passes.py`,
   `pipeline.py`, `store.py`, and `trust.py`.
6. GitHub issue #1 and its two closing comments, for what was actually proven.

## Phase 2 scope

From the roadmap: **multi-provider**. Concretely — additional subprocess adapters beside
the existing `xai`/grok one (the research report names codex, claude, and a
Gemini-family CLI), role-driven reviewer configuration, a **cross-provider refuter**, and
**quota fallback chains** when a provider is exhausted or unavailable.

Treat that as the starting point, not the finished scope. Your job includes deciding what
genuinely belongs in Phase 2 versus Phase 3 (surfaces: MCP server, pre-push shim,
SessionStart hook) and Phase 4 (scheduling, retention, `skodun doctor`). Say what you cut
and why.

## Design tensions you must resolve, not gloss

These are real and specific to this codebase. A plan that does not address them is not
ready.

- **The adapter contract is grok-shaped today.** `adapters/grok.py` owns envelope parsing
  with a three-level fallback, a schema-validity check that gates `parse_ok`, and a
  `degraded` detector built from that CLI's specific stderr signals, leaked control token,
  and `stopReason` semantics. Each new CLI has different output shapes and different
  failure vocabulary. Decide what the `Adapter` protocol actually guarantees, what
  `degraded` means provider-neutrally, and how a new adapter proves it detects its own
  degradation — an adapter that cannot recognise its CLI failing is worse than no adapter,
  because it mints trustworthy records from broken runs.
- **Trust across a provider fallback.** If a run falls back from provider A to provider B
  mid-attempt, what does the resulting record mean? The trust invariant
  (`trustworthy = parse_ok and not degraded and not diff_truncated`) has exactly one
  definition and is computed on write by the store. Decide whether a fallback is a fresh
  attempt, whether it is recorded as such in `attempts[]`, and whether trust is affected.
  Do not change the invariant itself.
- **Per-pass provenance.** `reviews` carries single `model` and `adapter` columns, but
  Phase 2 can have the finder, security pass and refuter on three different providers.
  Decide the schema change and the migration — the store is live and holds an imported
  archive of thousands of real reviews.
- **Cross-provider refuter changes merge semantics.** Phase 1's extra passes reuse the
  finder's adapter, and `passes.merge_extra_pass` / `merge_failed_extra_pass` demote the
  primary along two deliberately independent axes (a *failed* pass clears `parse_ok`; a
  *degraded* pass sets `degraded` and does not touch `parse_ok`). Decide whether a
  refuter on a *different* provider failing should demote the primary at all, or whether
  provider-B unavailability is a different class of event from provider-B misbehaving.
- **Effort is a canonical enum translated per adapter.** The research report is explicit
  that a model rejecting an effort setting must fail **loudly, not silently**. Phase 1
  already rejects effort for models that do not support it. Extend that per provider
  without inventing a silent-downgrade path.
- **Quota fallback is a policy, and policies need a fail-closed default.** Decide what
  happens when every provider in a chain is exhausted. The answer that fits this codebase
  is an explicit `failed` record and a gate that says "no trustworthy review", never a
  pass.

## Decisions Phase 1 deliberately deferred to you

- `severity_gate` and `confidence_threshold` are declared in `[defaults]`, validated and
  bounds-checked, and **read by nothing**. `gate.open_findings` returns every untriaged
  finding regardless of severity, so one `low` finding is exit 1 even under
  `severity_gate = "high"`. This is documented and pinned by
  `tests/test_gate.py::test_severity_gate_high_still_blocks_on_a_low_finding`. Decide:
  implement them, or remove them. Leaving a config key that implies gating behaviour it
  does not have is the one option to reject.
- `cli.main()` catches `BaseException` and returns 2, so Ctrl-C during a long `review`
  exits 2 — a code that nominally means "preflight refusal, nothing ran". It is safe (the
  `finally` has already downgraded the record and released the lock) but misleading. The
  clean fix is letting `KeyboardInterrupt` escape `_cmd_review` only, never `_cmd_gate`.
- `shadow-compare`'s summary is a point-in-time snapshot; legacy-only rows accumulate on
  their own as the other system keeps running. If shadow mode remains an ongoing signal,
  it needs a time window or the count drifts upward and reads as regression.
- `contextpack` raises `NotImplementedError` for `source="oid"`; that seam belongs to the
  dispatcher phase. Confirm it stays out of Phase 2.

## Hard constraints — inherited, non-negotiable

- **Fail closed.** The trust invariant has one definition in `trust.py`, is computed by the
  store on write from strictly-`bool` axes, and is re-asserted by the gate against the
  artifact. The gate contract is `0` clean-or-all-triaged, `1` findings open, `2` no
  trustworthy review, with **every unexpected exception mapping to 2, never 1**.
- **Public open-source hygiene.** No machine-specific paths, no upstream-project names, and
  no single project's repo-layout literals in `src/` — that includes prompt text, which is
  shipped data, not commentary. Repo-specific tables live in config with generic defaults;
  concrete tables ship as commented examples under `examples/`. Test code locates the
  porting oracle solely via `SKODUN_ORACLE_DIR` and skips cleanly when unset.
- **Python ≥ 3.12, stdlib-only runtime, pytest as the only dev dependency.** Phase 2 adding
  a provider SDK would break this; adapters are subprocess-based over installed CLIs
  precisely so existing subscriptions are reused.
- **Diff identity, triage keys, and prompt bytes are byte-compatible with the oracle** and
  pinned by oracle-gated parity tests. Do not plan changes that move them.
- Every text file read/written passes `encoding="utf-8"` explicitly; prompts and diffs
  travel as files, never shell-interpolated strings; model selection is always explicit and
  never inherited from a provider CLI's own settings file.

## Method

Use the `superpowers:brainstorming` skill first to explore intent and requirements, then
`superpowers:writing-plans` to produce the plan. The Phase 1 plan is your template for
depth and shape: numbered TDD tasks, each with the failing test first, the implementation,
the oracle reference where one exists, and a commit step. Match that standard.

Ground every claim about current behaviour by reading the code or running it — the Phase 1
suite (1047 tests) runs with `SKODUN_ORACLE_DIR` set for the parity tests and without it
for everything else. Do not describe behaviour you have not verified.

Have the finished plan adversarially reviewed before it is called done. The Phase 1 plan
went through five rounds and the findings were substantive — including a coercion bug, a
path-parsing defect, and an off-by-one in a byte budget. Expect the same.

## Deliverables

1. **A GitHub epic issue** on `vega113/skodun`, in the shape of epic #1: why, what the
   phase delivers, a task index table with the key constraint per task, and explicit
   acceptance criteria that can be *demonstrated* rather than asserted. Phase 1's criteria
   are a good model — they forced live evidence, and two of the four turned out not to have
   been proven until someone actually ran them.
2. **A plan file** at `docs/superpowers/plans/<date>-skodun-phase2.md`, authoritative and
   linked from the epic.

State plainly anything you could not determine, and any place where you think the Phase 1
design will resist what Phase 2 needs. A planning document that hides a known tension is
worse than one that names it and proposes two options.
