# Prompt: complete epic #23 — finish skodun as local review backend

> Deeper companion to [`docs/agent-start-epic-23.md`](agent-start-epic-23.md).  
> Hand either file to a coding agent; prefer **agent-start** for session zero.  
> Written 2026-08-01; child issues + ready-as-backend definition 2026-08-02.  
> Phases 1–4 are merged; Phase 5 spine (junie) and R2/R3 exist as **open PRs**
> and may need land, rebase, or re-implementation against current `main`.
> Your job is to **close epic #23**. Investigate first, then plan, then implement.

---

You are finishing **skodun** epic **#23**. The goal is not “ship one more feature.”
The goal is: **epic #23 is closed, and skodun is ready to serve as the local review
backend** for projects on the machine (including TubeScribes cutover *starting*
after this epic), per the acceptance criteria on the live issue.

## What skodun is

A local, provider-neutral **code-review server**: a Python CLI that runs a diff through an
AI coding CLI you are already subscribed to (rather than an API key), persists the review
to SQLite, and **gates pushes on the result**. Agents drive it over **stdio MCP**
(`skodun mcp`) with the same semantics as the CLI.

Repository: `github.com/vega113/skodun`, public, Apache-2.0. Working checkout:
`/Users/vega/devroot/skodun`. Paths below are relative to the repo root.

**The oracle is located ONLY via the `SKODUN_ORACLE_DIR` environment variable.** Never
hardcode a path to it — not in code, tests, plans, or this document. Subagents do not
inherit it; pass it explicitly and always report ran-vs-skipped test counts.

## Done looks like this (epic close criteria)

You may close #23 only when **all** of the following are true. If you cut any of them,
the cut must be recorded on the epic with an owner-approved reason **and** a follow-up
issue that still leaves #23 open — **cuts do not close #23**.

### Ready-as-backend (product outcome)

When closed, skodun can be the review system of record for a project without the
tubescribes grok-script archive:

- review + gate + triage + surface via CLI/MCP
- background pre-push dispatch with durable failure records
- doctor + retention + schedule install for multi-week operation
- agent protocol docs that match shipped tools

TubeScribes wiring (`ci-local-gate` accepting skodun artifacts, retiring
`grok-review-now.sh`) is **out of scope** here — separate tubescribes epic that
**starts after** #23 closes.

### A. Land the Phase 5 work already started

| Item | Status at handoff | Your job |
|---|---|---|
| **Junie adapter** | Open PR **#24** | Merge if still correct and CI-green after rebase onto current `main`; otherwise re-implement. Must ship: provider `junie`, empty-capsule + macOS Seatbelt confinement, fail-closed off macOS, conformance suite, no `gate.py`/`trust.py` edits. |
| **R2 churn + R3 round context** | Open PR **#25** | Same: merge or re-implement. Presentation-only on triage list / log / surface (+ MCP via `services`); gate exit contract unchanged. |

After both land on `main`, confirm `skodun providers` lists `junie` and `skodun triage --list` / `log` show round/churn lines.

### B. Operational debt (README Known limitations that are still defects)

Tracked as children:

1. **Worker-log pruning** — issue **#26** (prefer with retention #30).
2. **Pre-push shim stdin-buffer success check** — issue **#27**.
3. **Quadratic JSON scan** — issue **#28**.

### C. Design Phase 4 — required for “local review backend”

1. **`skodun doctor`** — issue **#29**.
2. **Retention** — issue **#30**.
3. **Scheduling** — issue **#31**.

### D. MCP product completeness

Issue **#32** plus:

1. CLI ↔ MCP parity through `services` for mirrored verbs.
2. MCP prompts / `examples/AGENTS.md` updated (junie, R2/R3, doctor, stopping rule).
3. README Known limitations rewritten for fixed items.
4. MCP stdout purity (JSON-RPC only on stdout).
5. Skill **or** recorded decision that AGENTS+prompts suffice.

### E. Explicitly out of scope for #23

- Anthropic/`claude` adapter
- Generic `openai-compatible` / `custom-command` product adapters
- Local-model productization beyond doctor visibility
- Cloud-bot embed generation / rules-registry authoring sync
- macOS notification productization
- Changing `gate.py` / `trust.py` byte pins without owner approval
- Severity-tier gating or re-reviewing only the delta
- TubeScribes cutover
- Host-wide multi-MCP fair queue (beyond one FG review per server process)

## Read these first, in this order

1. `docs/agent-start-epic-23.md` and **live epic #23**.
2. `README.md`.
3. This prompt.
4. `docs/2026-07-27-review-server-research.md`.
5. Open PRs **#24** and **#25**.
6. `examples/AGENTS.md`.
7. Phase plans under `docs/superpowers/plans/`.
8. `src/skodun/`.
9. Closed epics #1–#3, #13 for proven contracts.

## Method (non-negotiable)

- Investigate → success criteria → short plan → implement minimum verified change.
- Design/plan docs for doctor, retention, schedule, schema.
- `gate.py` and `trust.py` stay byte-identical unless owner-approved.
- Fail closed. Runtime stdlib-only. Store forward-only; one atomic DDL bump if needed.
- Generic committed code. Oracle only via `SKODUN_ORACLE_DIR`.
- Tests pin `SKODUN_DB` / fakes; never the real user store.
- Commit before mutating. Batch: land #24/#25 first, then children in order.

## Suggested work order

1. Inventory `main` + #24 + #25.
2. Close PR gap (junie + R2/R3).
3. #27 stdin, #28 scan.
4. #26+#30 retention (includes log pruning).
5. #29 doctor.
6. #31 schedule install.
7. #32 docs/MCP/skill.
8. Epic close comment with evidence; then close #23.

## What to produce

1. Epic/child comments with PR + suite evidence.
2. Design/plan docs for non-trivial slices.
3. Final #23 close comment with ready-as-backend checklist.

**If blocked:** stop and ask the owner — do not silently shrink the epic.
