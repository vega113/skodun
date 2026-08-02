# AGENTS.md

> **README for coding agents** working *on* this repository.  
> Human overview, install, and operator docs: [`README.md`](README.md).  
> Client projects that *use* skodun paste from [`examples/AGENTS.md`](examples/AGENTS.md) — not this file.

This file follows the open [AGENTS.md](https://agents.md/) convention (vendor-neutral Markdown for AI coding agents). Closest `AGENTS.md` wins if nested files appear later; explicit user chat overrides everything here.

---

## Project overview

**skodun** is a local, fail-closed code-review pipeline that drives AI **CLI tools you already subscribe to** (grok / codex / agy / junie), not metered HTTP API keys.

Product spine:

| Concern | Contract |
|---|---|
| Coverage | One review per **exact diff identity**; edits invalidate prior coverage |
| Trust | Untrustworthy runs never certify a push (`gate` exit `2`) |
| Gate | Exit `0` only when a trustworthy review covers the tree **and** open findings are triaged |
| Triage | Audited reasons (≥20 chars, no placeholders); `dismiss` / `defer` (filed ref required) / `reopen` |
| Surfaces | CLI and MCP share the same `services` layer for the review loop |

Premise: reusing subscription CLIs. There is **no** `anthropic` / headless Claude adapter by design (metered billing).

Python ≥ 3.12, **stdlib-only** runtime; **pytest** for tests. Package lives under `src/skodun/`.

---

## Setup and commands

```bash
# From repository root — no install required for tests
python3 -m pytest              # full suite (pythonpath = src + . via pyproject)
python3 -m pytest tests/test_X.py -q --tb=line
python3 -m pytest tests/test_X.py::test_name -vv --tb=short

# Run the CLI without installing
PYTHONPATH=src python3 -m skodun --help
PYTHONPATH=src python3 -m skodun doctor
PYTHONPATH=src python3 -m skodun --version
```

Optional editable install: `pip install -e .` then `skodun` on PATH.

Config: global `~/.config/skodun/config.toml` and/or repo `.skodun.toml`.  
Store: SQLite (path from env/config; tests use tmp paths).

---

## Repository map

| Path | Role |
|---|---|
| `src/skodun/pipeline.py` | Foreground review orchestration, FG lock, capacity dual-hold |
| `src/skodun/gate.py` | Push certification — **do not change lightly** |
| `src/skodun/trust.py` | Trust axes / banner — **do not change lightly** |
| `src/skodun/store.py` | SQLite + schema ladder (`SCHEMA_VERSION`) |
| `src/skodun/services.py` | Shared CLI + MCP service path |
| `src/skodun/mcpserver.py` | stdio MCP; long-running `review` capacity 1 |
| `src/skodun/capacity.py` | FIFO `review-fg` admission + telemetry (epic S3) |
| `src/skodun/adapters/` | Provider adapters (`xai`, `openai`, `google`, `junie`) |
| `src/skodun/chain.py` | Fallback chain on `unavailable` only |
| `tests/` | Hermetic suite; drive **shipped** entry points |
| `docs/epics/` | Product epic seeds + Done status |
| `docs/superpowers/specs/` | Design docs for non-trivial work |
| `docs/integrate-external-project.md` | Client MCP/gate wiring |
| `examples/AGENTS.md` | **Client** paste template (consumers of skodun) |
| `examples/fragments/` | Smaller client paste-ins (concurrency, MCP loop) |

---

## Hard invariants (do not violate)

1. **Fail closed.** Unexpected errors and missing coverage map to gate `2`, never a silent pass.
2. **`gate.py` / `trust.py`:** leave **byte-identical** unless the owner explicitly approves a change (product epics pin this). Prefer reporting cancelled/status as a read-model over durable `failed` + reason rather than new store enums that risk trust drift.
3. **CLI ↔ MCP parity.** Review-loop verbs go through `services.py`. Same wording, same exit/status mapping. Do not implement behavior only in `mcpserver` or only in `cli`.
4. **Store migrations.** Additive only; bump `SCHEMA_VERSION` with a ladder entry in `store.py`. Never edit frozen Phase-1 `_SCHEMA` for new tables — use `_MIGRATIONS`. Transactional deltas for non-idempotent SQL.
5. **No anthropic adapter** unless the owner reopens that product premise.
6. **Tests must prove the shipped path.** No hard-coded expected values that bypass the unit under test, no re-implementation of production logic inside tests, no “start after the interesting code.”
7. **Legacy FG lock path** (`grok-reviews-foreground.lock` + three-line `owner`) is interop-critical for shadow coexistence. Do not rename without a dual-hold bridge.

---

## Code style (project-specific)

- Prefer **module docstrings** that state load-bearing contracts (this codebase relies on them).
- Frozen dataclasses / explicit validation at store and config doors (`bool` is not an `int`).
- Timestamps in the store: canonical UTC `YYYY-MM-DDTHH:MM:SSZ` only.
- Progress / banners: pipeline does not write verdicts to stdout (MCP JSON-RPC ownership); CLI renders from the returned record.
- Keep diffs focused; do not “drive-by” reformat unrelated modules.

---

## Testing instructions

- Default: `python3 -m pytest` from repo root.
- Prefer targeted modules while iterating (`tests/test_pipeline.py`, `tests/test_capacity.py`, …).
- The store suite includes a heavy ResourceWarning subprocess sweep; deselect only when iterating on unrelated store work:
  `python3 -m pytest tests/test_store.py --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error`
- New modules that call `Store.open` must be listed in `_STORE_TOUCHING_MODULES` or `_SWEEP_EXCLUDED` in `tests/test_store.py`.
- After behavior changes: add or update hermetic tests in the same PR.

---

## Security and trust boundaries

- Never weaken gate/trust to make a demo pass.
- Junie runs only under macOS Seatbelt confinement; off-macOS must classify `unavailable`, never unconfined soft-fallback.
- Context packing must not follow unsafe paths (symlinks, FIFOs, etc.) — fail/omit, don’t hang.
- Provider secrets stay in the environment / CLI configs; skodun must not log API keys.

---

## Goal / epic completion (non-negotiable)

**A goal, epic, or feature issue is NOT complete when tests pass locally.**

**Done means all of:**

1. Implementation + hermetic tests on the shipped path  
2. Branch pushed; PR opened against `main`  
3. Review feedback addressed (no open blocking threads you own)  
4. Checks green, or non-blocking failures documented (e.g. bot rate-limit)  
5. **PR merged to `main`**  
6. **GitHub issue(s) closed** with a comment linking the PR/merge  

Until (5) and (6), report status as **implemented, not landed** — never “complete” or “epic done.”  
Local pytest green, a design-only diff, or a harness verification plan alone do **not** close product work.

### Preferred land path

```text
implement → tests → commit → push → PR → review fixes → merge to main → close issues
```

Keep `main` green. Prefer short-lived branches over long-lived feature branches without a merge PR.

### Commit / PR habits

- Commit messages: complete sentences; explain *why*; `refs #N` for issues.
- PR body: Summary + Test plan; link epic/issue.
- Do not force-push shared `main`. Use `--force-with-lease` only on your own feature branch after rebase when needed.

---

## Product intent pointers

| Kind | Path |
|---|---|
| Epic seeds + ship status | [`docs/epics/`](docs/epics/) |
| Designs | [`docs/superpowers/specs/`](docs/superpowers/specs/) |
| Client integrate guide | [`docs/integrate-external-project.md`](docs/integrate-external-project.md) |
| Client agent paste | [`examples/AGENTS.md`](examples/AGENTS.md), [`examples/fragments/`](examples/fragments/) |
| Known limitations | [`README.md`](README.md) § Known limitations |

Shipped post-#23 epics (do not re-open without owner intent):

- **S1** status + cancel — PR #49  
- **S3** fair `review-fg` capacity — PR #50  

---

## What not to invent

- Host-wide fair queue for non-review work (DB suites, Karma, etc.)  
- Parallel multi-provider voting as default  
- MCP queue-behind-busy without fingerprint re-check (S3 chose refuse-if-busy)  
- Scheduler / doctor / retain as MCP tools (CLI-only by design)  
- Softening fail-closed gate semantics for convenience  

When unsure, read the nearest design under `docs/superpowers/specs/` or the epic seed before coding.
