# Prompt: complete epic #23 — finish skodun MCP as designed

> Hand this to Grok (or any capable coding agent). It is self-contained apart from
> the repository itself. Written 2026-08-01. Phases 1–4 are merged; Phase 5 spine
> (junie) and R2/R3 exist as **open PRs** and may need land, rebase, or re-implementation
> against current `main`. Your job is to **close epic #23** by shipping everything the
> epic lists as required for “skodun MCP as designed.” Investigate first, then plan,
> then implement. Do not declare victory while #23 is open.

---

You are finishing **skodun** epic **#23**. The goal is not “ship one more feature.”
The goal is: **epic #23 is closed, and the designed local review server + MCP surface
is complete for daily agent use**, per the original research roadmap and the
acceptance criteria on the epic.

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

### A. Land the Phase 5 work already started

| Item | Status at handoff | Your job |
|---|---|---|
| **Junie adapter** | Open PR **#24** (`phase5-junie-adapter`) | Merge if still correct and CI-green after rebase onto current `main`; otherwise re-implement from the design/plan in that PR and the oracle port sources. Must ship: provider `junie`, empty-capsule + macOS Seatbelt confinement, fail-closed off macOS, conformance suite, no `gate.py`/`trust.py` edits. |
| **R2 churn + R3 round context** | Open PR **#25** (`phase5b-r2-r3`) | Same: merge or re-implement. Must ship: presentation-only annotations on triage list / log / surface (and MCP via `services`); gate exit contract unchanged; no schema change required for R2/R3 alone. |

After both land on `main`, confirm `skodun providers` lists `junie` and `skodun triage --list` / `log` show round/churn lines.

### B. Operational debt (README Known limitations that are still defects)

1. **Worker-log pruning** — `<db>.logs/<record-id>.log` must not grow unbounded. Ship a
   retention policy (config + implementation + tests): age and/or count bounds, safe
   deletion (nothing re-reads logs for trust). Prefer tying this to a general retention
   story (see C) rather than a one-off `rm`.
2. **Pre-push shim stdin-buffer success check** — if teeing git’s ref list to a temp file
   fails (e.g. full disk), do **not** hand a truncated list to a chained foreign hook.
   Fail closed in the direction that does not invent push decisions from incomplete stdin.
3. **Quadratic JSON scan** — the deferred Phase 3 performance defect: eliminate or bound
   the worst-case scan on the hot path; pin with a test that would fail if the quadratic
   behavior returned.

### C. Design Phase 4 — required for “MCP as designed”

From `docs/2026-07-27-review-server-research.md` §Suggested roadmap Phase 4 and
§Proposed architecture. These are **in scope for closing #23**, not optional polish:

1. **`skodun doctor`** — diagnose install/MCP readiness: registered adapters, binary
   resolve/executable, optional version probe, config load errors, store open/version,
   MCP handshake sanity (or clear instructions). Agents and humans use this when
   `skodun mcp` “doesn’t work.” Wire a read-only MCP tool **or** document why doctor
   stays CLI-only (human diagnostic); either way the CLI command must exist and be tested.
2. **Retention** — prune durable junk without deleting review/triage identity the gate
   needs: worker logs (B.1), and any raw prompt/stdout blobs if present; keep artifact
   rows / triage ledger. Config-driven TTLs; dry-run or report counts; tests.
3. **Scheduling** — `skodun schedule install` (or equivalent) generating **launchd** (macOS)
   plists for configured jobs from TOML `[schedule]` (shape in the research doc). No
   scheduler thread inside the stdio MCP process. Off-macOS: explicit refuse or documented
   no-op with doctor visibility. At least one integration test with a fixture plist/dir.

### D. MCP product completeness (parity with the design intent)

Shipped today (verify on `main` after merges): tools roughly
`gate`, `review`, `log`, `surface`, `triage_list`, `triage_dismiss`, `adopt_refuter`,
`triage_reopen`, `triage_defer`; prompts `review-now`, `gate-check`.

For epic close you must:

1. **Keep CLI ↔ MCP word-for-word parity** through `services` for every tool that mirrors
   a CLI verb. New CLI surfaces (doctor, schedule, retention report) either get MCP tools
   or an explicit epic comment why they are CLI-only.
2. **Update MCP prompts / `examples/AGENTS.md`** so agents see R2/R3, junie macOS-only,
   doctor, and the gate stopping rule — no stale “round context does not exist” prose.
3. **README Known limitations** — remove or rewrite every limitation you fixed; do not
   leave “never pruned” text after pruning ships.
4. **MCP stdout purity** — still only newline-delimited JSON-RPC on stdout; diagnostics
   on stderr. One stray byte is a test failure.
5. **Skill (if missing or stale)** — thin `SKILL.md` / agent skill for the review loop
   policy, pointing at MCP tools and the stopping rule (research §Skill). Skip only with
   a filed follow-up that does **not** block #23 if the owner already treats
   `examples/AGENTS.md` + MCP prompts as sufficient — **record that decision on the epic**.

### E. Explicitly out of scope for #23 (do not reopen without owner)

These remain **later epics** unless the owner expands #23 in writing:

- Anthropic/`claude` adapter (out of scope by product premise — metered billing)
- Generic `openai-compatible` / `custom-command` adapters as full products
- Local-model productization beyond what doctor reports
- Cloud-bot embed generation / rules-registry authoring sync (research: GH-side out of scope)
- macOS notification center productization
- Changing `gate.py` / `trust.py` byte pins without explicit owner approval
- Severity-tier gating or re-reviewing only the delta (rejected by cutoff design)

## Read these first, in this order

1. **This prompt** and **live epic #23** (body + comments — the epic is the contract).
2. `README.md` — gate contract, MCP, Known limitations, providers.
3. `docs/2026-07-27-review-server-research.md` — architecture + Phase 4 roadmap (doctor,
   schedule, retention).
4. `docs/superpowers/specs/2026-07-31-review-round-cutoff-design.md` — R2/R3 (if #25 not
   merged yet).
5. `docs/superpowers/specs/2026-08-01-skodun-phase5-design.md` and
   `docs/superpowers/plans/2026-08-01-skodun-phase5.md` — junie design/plan (if #24 not
   merged).
6. Open PRs **#24** and **#25** — diff, CI, review threads.
7. `examples/AGENTS.md` — client protocol; keep it true.
8. Phase plans under `docs/superpowers/plans/` — Global Constraints + Deviations.
9. `src/skodun/` — especially `store.py`, `dispatch.py`, `mcpserver.py`, `services.py`,
   `adapters/`, pre-push shim sources.
10. Closed epics #1–#3, #13 for proven contracts.

## Method (non-negotiable)

These exist because skodun paid for them in Phases 1–5:

- **Investigate → success criteria → short plan → implement minimum verified change.**
- **Plan before large code.** For the remaining epic work, write:
  - design notes under `docs/superpowers/specs/` when behavior or schema changes;
  - an implementation plan under `docs/superpowers/plans/` with task-level tests and
    named mutations where security or gate-adjacent code is involved.
- **`gate.py` and `trust.py` stay byte-identical** unless the owner approves a change
  (sha256 pins in `tests/test_seams.py`). Route around them.
- **Fail closed.** Exit `0` clean or all triaged · `1` findings open · `2` no trustworthy
  review. Unexpected exceptions → `2`, never `1`.
- **Runtime stdlib-only**, Python ≥ 3.12; pytest only as dev dependency. No MCP SDK.
- **Store forward-only.** If you need schema, **one atomic v6** (or next version) delta
  for the whole epic slice that needs DDL — same `BEGIN IMMEDIATE` + mid-delta
  failure-injection discipline as v3–v5. Do not drip migrations.
- **Generic committed code.** No machine paths; no one project’s private surfaces in
  defaults. Oracle only via `SKODUN_ORACLE_DIR`.
- **Tests never touch the real store or real providers.** Pin `SKODUN_DB`,
  `GIT_CONFIG_GLOBAL`, `SKODUN_*_BIN` to tmp.
- **Commit before mutating.** Revert must not destroy uncommitted fixes.
- **Review-loop stopping rule:** stop when `skodun gate` exits 0; triage by consequence;
  `triage --defer` requires a filed tracking ref. Do not infinite-loop on your own PRs.
- **Batch PR work:** land #24/#25 first if possible (or restack), then ops debt, then
  doctor/retention/schedule as coherent PR chunks. Each PR: `refs #23`, suite green
  both oracle modes with reconciled pass/skip counts.

## Suggested work order

1. **Inventory** — `git fetch`; state of `main`, #24, #25, CI, epic body. Note drift.
2. **Close the open PR gap** — rebase/merge or re-ship junie + R2/R3 onto `main`.
3. **Ops debt** — shim stdin, log pruning (or as part of retention), quadratic scan.
4. **Doctor** — CLI (+ MCP policy decision).
5. **Retention** — config + implementation + tests.
6. **Schedule install** — launchd generation + tests.
7. **Docs/MCP prompts/AGENTS** — make the agent-facing surface match reality.
8. **Epic close** — comment on #23 with evidence links (PRs, suite counts, seam hashes,
   doctor/MCP smoke); only then close the issue.

## Environment notes (handoff-time; re-verify)

- skodun was installed as editable pipx from this checkout; restart MCP clients after
  upgrade; check `serverInfo.version` / `skodun --version`.
- Providers: `agy` often works; `codex`/`grok` may be quota-blocked — tests use fakes.
- Store at `~/.local/share/skodun/skodun.db` is real user data — never point tests at it.
- Full suite ~11–40 minutes depending on the resource-warning sweep; run with
  `PYTHONDONTWRITEBYTECODE=1`; report with- and without-oracle counts.

## What to produce

1. Updated epic comments as you complete slices (evidence, not vibes).
2. Design/plan docs for any non-trivial slice (doctor, retention, schedule, schema).
3. Merged (or merge-ready) PRs covering A–D above.
4. Final #23 close comment: checklist of acceptance criteria with PR URLs and suite
   evidence; confirmation that Known limitations no longer list fixed items; confirmation
   gate/trust pins unchanged (or owner-approved).

**If blocked:** stop and ask the owner — do not silently shrink “MCP as designed” to
“merged the easy PR.” The epic is open until the close criteria are met.
