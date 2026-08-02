# Agent start: close skodun epic #23 (local review backend)

> **Hand this file to any capable coding agent to begin work.**  
> It is self-contained apart from the repository. Updated 2026-08-02.  
> Live contract: [github.com/vega113/skodun/issues/23](https://github.com/vega113/skodun/issues/23)

---

## Mission

Close **epic #23** so **skodun is ready to serve as the local review backend** for real projects (TubeScribes and other checkouts on the machine).

That means: agents and humans can **review → triage → gate → surface** through skodun CLI/MCP, with doctor, retention, and safe hooks — **without** depending on the tubescribes `scripts/grok-review-*.sh` stack as the source of truth.

**Closing #23 is not the tubescribes cutover.** Cutover (provider-neutral gate, shim replacement, Agents.md in tubescribes) is a **separate tubescribes epic**. When #23 is done, that cutover is *allowed to start*.

---

## Repository

| | |
|---|---|
| Remote | `https://github.com/vega113/skodun` |
| Typical checkout | `/Users/vega/devroot/skodun` |
| Runtime | Python ≥ 3.12, **stdlib only** (pytest = dev) |
| Epic | https://github.com/vega113/skodun/issues/23 |

**Oracle (tubescribes review scripts) is located ONLY via `SKODUN_ORACLE_DIR`.** Never hardcode a path. Subagents do not inherit it — pass it explicitly and always report ran-vs-skipped test counts.

---

## Done when (epic close)

You may close #23 only when **all** acceptance checkboxes on the live issue are done, with PR URLs + suite evidence on a closing comment. **Cuts do not close the epic** without an owner comment + follow-up issue.

### Ready-as-backend checklist (must be true)

1. `skodun review` / MCP `review` works with registered providers (junie on macOS when #24 landed).
2. `skodun gate` fail-closed exact-diff contract (exit 0/1/2) still holds.
3. Triage ledger (dismiss / defer / reopen) works; MCP parity for triage verbs.
4. `install-hooks` + background `dispatch` + `surface` usable.
5. `skodun doctor` diagnoses install/MCP readiness.
6. Retention bounds worker logs / durable junk; gate artifacts kept.
7. Pre-push stdin tee fails closed; quadratic scan bounded or gone.
8. README Known limitations and `examples/AGENTS.md` match reality.
9. Full test suite green **with and without** `SKODUN_ORACLE_DIR` (reconcile pass/skip).

### Explicitly out of scope (do not expand #23)

- TubeScribes cutover / rewriting `ci-local-gate` to require skodun
- Host-wide multi-MCP fair queue beyond one FG review per MCP process
- Anthropic/`claude` adapter, generic openai-compatible product adapters
- Weakening gate/trust; editing `gate.py` / `trust.py` without owner approval
- Severity-tier gating or “re-review only the last fix delta”

---

## Child work queue (implement these)

| Order | Item | Link | Notes |
|---:|---|---|---|
| 1 | Junie adapter | PR **#24** | Merge or re-implement after rebase onto current `main` |
| 2 | R2/R3 presentation | PR **#25** | Merge or re-implement; no gate contract change |
| 3 | Pre-push stdin fail-closed | Issue **#27** | Small, independent |
| 4 | Quadratic JSON scan | Issue **#28** | Bound or eliminate; regression test |
| 5 | Worker-log pruning + retention | Issues **#26**, **#30** | Prefer one retention design for both |
| 6 | `skodun doctor` | Issue **#29** | CLI required; MCP optional with policy note |
| 7 | Schedule install (launchd) | Issue **#31** | No scheduler inside stdio MCP |
| 8 | Docs / MCP parity / skill decision | Issue **#32** | Last; then close #23 |

**One coherent PR per child (or tightly coupled pair #26+#30).** Every PR: `refs #23` and the child issue number.

---

## Read first (in order)

1. **This file** and the **live epic #23** body + comments.
2. `README.md` — gate contract, MCP, Known limitations, providers.
3. `docs/epic-23-completion-handoff-prompt.md` — deeper acceptance text.
4. `docs/2026-07-27-review-server-research.md` — architecture + Phase 4 (doctor/retention/schedule).
5. Open PRs **#24** and **#25** (diff, CI, threads).
6. Designs/plans under `docs/superpowers/` for junie / R2-R3 if those PRs are not merged.
7. `examples/AGENTS.md` — keep it true.
8. `src/skodun/` — especially `store.py`, `dispatch.py`, `mcpserver.py`, `services.py`, `adapters/`, pre-push shim sources.
9. `tests/test_seams.py` — `gate.py` / `trust.py` sha256 pins.

---

## Method (non-negotiable)

- **Investigate → short plan → minimum verified change.** For non-trivial slices (doctor, retention, schedule, schema), write design under `docs/superpowers/specs/` and plan under `docs/superpowers/plans/`.
- **`gate.py` and `trust.py` stay byte-identical** unless the owner approves a pin change.
- **Fail closed.** Gate: `0` clean or all triaged · `1` findings open · `2` no trustworthy review. Unexpected → `2`, never `1`.
- **Runtime stdlib-only.** No MCP SDK. Store migrations **forward-only**; if DDL is needed, **one atomic version bump** for the whole epic DDL chunk.
- **Generic code only.** No machine-local paths. Oracle only via `SKODUN_ORACLE_DIR`.
- **Tests never touch the real user store** (`~/.local/share/skodun/…`) or real provider CLIs — pin `SKODUN_DB`, `GIT_CONFIG_GLOBAL`, `SKODUN_*_BIN` to tmp.
- **Commit before risky mutations.** Prefer small PRs that each leave `main` green.
- **Stopping rule for your own review loop:** stop when `skodun gate` exits 0; triage by consequence; `triage --defer` requires a filed tracking ref.

---

## First actions (session 0)

```text
1. gh issue view 23 -R vega113/skodun
2. gh pr view 24 -R vega113/skodun ; gh pr view 25 -R vega113/skodun
3. git fetch origin && git checkout main && git pull --ff-only
4. Inventory drift: are #24/#25 mergeable onto current main? CI green?
5. Write a short ordered plan comment on #23 (which child you take first).
6. Start with #24 land/rebase unless blocked — then #25 — then #27.
```

Do **not** start retention/doctor until the open spine PRs are merged or explicitly re-implemented.

---

## Environment notes

- Install/dev: editable install from checkout is fine; restart MCP clients after upgrade; confirm `skodun --version` matches `serverInfo.version`.
- Real store at `~/.local/share/skodun/skodun.db` is user data — never point tests at it.
- Providers may be quota-blocked; tests use fakes. Live smoke is optional evidence, not a substitute for the suite.
- Full suite can take ~11–40 minutes; use `PYTHONDONTWRITEBYTECODE=1`; report oracle and non-oracle counts.

---

## What to produce

1. Comments on #23 and the child issue as each slice lands (PR URL + suite evidence).
2. Design/plan docs for non-trivial slices.
3. Merged (or merge-ready) PRs covering A–D.
4. Final #23 close comment:
   - checklist A–E with links
   - ready-as-backend checklist
   - confirmation Known limitations no longer list fixed items
   - confirmation gate/trust pins unchanged (or owner-approved)
   - note: tubescribes cutover is a separate epic and may now start

---

## If blocked

Stop and ask the owner. Do **not** silently shrink “ready as local review backend” to “merged the easy PR.”

Further detail: `docs/epic-23-completion-handoff-prompt.md`.
