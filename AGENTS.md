# AGENTS.md

Instructions for coding agents working **on** this repository (not for
products that *call* skodun — those use [`examples/AGENTS.md`](examples/AGENTS.md)).

Human docs: [`README.md`](README.md). Explicit user chat overrides this file.

---

## Project overview

**skodun** is a local, fail-closed code-review pipeline that drives AI **CLI
tools you already subscribe to** (grok / codex / agy / junie), not metered HTTP
API keys.

| Concern | Contract |
|---|---|
| Coverage | One review per **exact diff identity**; edits invalidate prior coverage |
| Trust | Untrustworthy runs never certify a push (`gate` exit `2`) |
| Gate | Exit `0` only when a trustworthy review covers the tree **and** open findings are triaged |
| Triage | Audited reasons (≥20 chars, no placeholders); `dismiss` / `defer` (filed ref required) / `reopen` |
| Surfaces | CLI and MCP share the same `services` layer for the review loop |

There is **no** `anthropic` / headless Claude adapter by design (metered billing
vs subscription-CLI premise).

Stack: Python ≥ 3.12, **stdlib-only** runtime, **pytest**. Code under
`src/skodun/`.

---

## Setup and commands

```bash
# From repository root — no install required for tests
python3 -m pytest
python3 -m pytest tests/test_pipeline.py -q --tb=line
python3 -m pytest tests/test_pipeline.py::test_name -vv --tb=short

# CLI without installing
PYTHONPATH=src python3 -m skodun --help
PYTHONPATH=src python3 -m skodun doctor
PYTHONPATH=src python3 -m skodun --version
```

`pyproject.toml` sets pytest `pythonpath` to `src` and `.`. Optional:
`pip install -e .` then use `skodun` on PATH.

Config: `~/.config/skodun/config.toml` and/or repo `.skodun.toml`.  
Store: SQLite (tests use tmp paths).

---

## Repository map

Load-bearing paths agents cannot safely guess:

| Path | Role |
|---|---|
| `src/skodun/pipeline.py` | Foreground review, FG lock, capacity dual-hold |
| `src/skodun/gate.py` | Push certification — change only with owner approval |
| `src/skodun/trust.py` | Trust axes / banner — change only with owner approval |
| `src/skodun/store.py` | SQLite + schema ladder (`SCHEMA_VERSION`) |
| `src/skodun/services.py` | Shared CLI + MCP service path |
| `src/skodun/mcpserver.py` | stdio MCP; one long-running `review` per process |
| `src/skodun/capacity.py` | FIFO `review-fg` admission + queue telemetry |
| `src/skodun/adapters/` | Providers: `xai`, `openai`, `google`, `junie` |
| `src/skodun/chain.py` | Fallback chain advances only on `unavailable` |
| `tests/` | Hermetic suite; drive **shipped** entry points |
| `docs/superpowers/specs/` | Designs for non-trivial work |
| `docs/epics/` | Product epic seeds (not a substitute for GitHub) |
| `docs/integrate-external-project.md` | Client MCP/gate wiring |
| `examples/AGENTS.md` | Client paste template (consumers of skodun) |
| `examples/fragments/` | Smaller client paste-ins |

---

## Hard invariants

1. **Fail closed.** Unexpected errors and missing coverage → gate `2`, never a silent pass.
2. **`gate.py` / `trust.py`:** do not edit without explicit owner approval. Prefer read-model status (e.g. cancelled over durable `failed` + reason) over new store enums that risk trust drift.
3. **CLI ↔ MCP parity.** Review-loop verbs go through `services.py` — same wording and status mapping on both surfaces.
4. **Store migrations.** Additive only; bump `SCHEMA_VERSION` via `_MIGRATIONS`. Never extend frozen Phase-1 `_SCHEMA` for new tables. Use transactional deltas for non-idempotent SQL.
5. **No `anthropic` adapter** unless the owner reopens that product premise.
6. **Tests prove the shipped path.** No hard-coded expected values that skip the unit under test; no re-implementation of production logic in tests; do not start past the code under test.
7. **Legacy FG lock** path/name and three-line `owner` format are interop-critical. Do not rename without a dual-hold bridge.
8. **Routing is OUTSIDE the fail-closed perimeter, deliberately.** `routing.py`
   degrades loudly to pre-S5 head selection when the store cannot answer —
   matching `chain._cached_unavailable`'s precedent for the same read — and
   must not be changed to fail closed. Invariant 1 is about **coverage and
   trust**: routing cannot touch either, since the model still reviews the real
   diff, the trust axes come from that run, and the gate reads the same record
   either way. Failing a review because the load optimiser hiccuped would spend
   a model call to report a store hiccup, and would let an optional feature
   take down the loop that works without it. (Decision recorded for epic #69 /
   #77; see `docs/superpowers/specs/2026-08-04-phase-b-weighted-routing.md` §6.)

---

## Code style (project-specific only)

- Prefer **module docstrings** that state load-bearing contracts.
- Frozen dataclasses; strict validation at config/store doors (`bool` is not an `int`).
- Store timestamps: canonical UTC `YYYY-MM-DDTHH:MM:SSZ` only.
- Pipeline does not write verdict banners to stdout (MCP owns the JSON-RPC stream); CLI renders from the returned record.
- Keep diffs focused — no drive-by reformats of unrelated modules.

---

## Testing instructions

- Full suite: `python3 -m pytest` from repo root.
- The root `tests/conftest.py` deletes every `SKODUN_*` variable from the
  environment before each test (allowlist: `SKODUN_ORACLE_DIR`). Tests set what
  they need themselves; nothing is inherited from the shell that ran pytest.
- Iterate with targeted modules (e.g. `tests/test_pipeline.py`, `tests/test_capacity.py`).
- Heavy store ResourceWarning sweep (deselect only when unrelated to store work):
  ```bash
  python3 -m pytest tests/test_store.py \
    --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error
  ```
- New code that calls `Store.open` must appear in `_STORE_TOUCHING_MODULES` or
  `_SWEEP_EXCLUDED` in `tests/test_store.py`.
- Behavior changes ship with hermetic tests in the same PR.

---

## Security boundaries

- Never weaken gate/trust to make a demo pass.
- Junie: macOS Seatbelt only; off-macOS → `unavailable`, never unconfined fallback.
- Context packing: reject unsafe paths (symlinks, FIFOs, etc.) — fail/omit, don’t hang.
- Do not log provider secrets or API keys.

---

## PR and completion rules

**Work is not done when tests pass on a branch.**

Done means:

1. Implementation + hermetic tests on the shipped path  
2. Branch pushed; PR against `main`  
3. Review feedback addressed  
4. Checks green (or non-blocking noise documented)  
5. **PR merged to `main`**  
6. **Related GitHub issues closed** with a link to the PR  

Until merge + issue close, say **implemented, not landed**.

Land path: `implement → tests → commit → push → PR → review → merge → close issues`.

- Commits: complete sentences; *why*; `refs #N` when applicable.  
- PRs: Summary + Test plan.  
- Never force-push `main`. Feature branches: `--force-with-lease` only after rebase when needed.

---

## Where to look things up

Do not turn this file into a ship log or issue index (IDs rot). Use:

| Need | Where |
|---|---|
| Designs | `docs/superpowers/specs/` |
| Epic seeds | `docs/epics/` |
| Client integrate | `docs/integrate-external-project.md` |
| Legacy → skodun cutover | `docs/cutover-from-legacy-review.md` |
| Client agent paste | `examples/AGENTS.md`, `examples/fragments/` |
| Operator/human detail | `README.md` |
| Live issue/PR status | GitHub |

---

## Do not invent

- Host-wide fair queue for non-review work  
- Parallel multi-provider voting as default  
- MCP second `review` queued behind a busy one without tree-fingerprint re-check at start (current policy: refuse if busy)  
- Scheduler / doctor / retain as MCP tools (CLI-only)  
- Softening fail-closed gate semantics for convenience  

When unsure, read the nearest design under `docs/superpowers/specs/` before coding.
