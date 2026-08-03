# Plan: S5 provider auto-route (load balance finders)

Date: 2026-08-03. Parent epic: **S5** (GitHub issue opened with this plan).  
Design: `docs/superpowers/specs/2026-08-03-provider-auto-route-design.md`.  
Epic seed: `docs/epics/s5-provider-auto-route.md`.

## Goal

When callers omit `reviewer` / `--reviewer`, pick a **finder entry** using
store-visible load (free slots, queue depth, `provider_state`) plus optional
**cross-model** preference from `client_family`. Explicit pin always wins.
Same path for CLI and MCP. No schema bump if routing metadata fits the
existing review artifact JSON.

## Decisions for implementers (locked unless product revises)

| Decision | Choice |
|---|---|
| Default `routing.mode` first merge | **`off`** (opt-in dogfood; flip examples to `auto`) |
| Pool when empty/omit | All enabled `role=finder` reviewers |
| Cross-model | Soft score only (never hard-exclude last available family) |
| Score timing | Once at head resolution (not every provider poll) |
| Failure after pick | Existing head `fallbacks` chain unchanged |
| Extra passes | Unchanged role-based pick (not auto-routed) |
| SCHEMA_VERSION | No bump for Phase A (artifact fields only) |

## Tasks (map 1:1 to child issues)

### Task 1 — Config: `[routing]` table

**Files:** `src/skodun/config.py`, `tests/test_config.py`

- Add frozen `Routing` dataclass: `mode: str` (`off`|`auto`),  
  `pool: tuple[str, ...]` (entry names), `cross_model: bool` (default True).
- Parse `[routing]` from TOML; unknown keys loud; bad `mode` loud.
- Env override: `SKODUN_ROUTING_MODE=off|auto` (wins over config when set).
- Wire onto `Config` (same merge posture as other tables: repo over global).
- Default when table absent: `mode=off`, empty pool, `cross_model=True`.

**Done when:** unit tests for defaults, parse, env override, invalid mode.

### Task 2 — Pure router: `routing.py`

**Files:** `src/skodun/routing.py` (new), `tests/test_routing.py` (new)

- `provider_family(provider_id: str) -> str` map (`xai`, `openai`+`openai-api`,
  `google`, `junie`, else provider id).
- `ClientSignals` / inputs: pool reviewers, holder counts, queued counts,
  unavailable set, `client_family`, `cross_model`.
- `score_candidate(...) -> int` pure; `pick_finder(...) -> Reviewer` pure given
  views (no I/O).
- Scoring (locked sketch from design):
  - exclude: blacked-out / no adapter / disabled / not in pool resolution
  - `+100 * free_slots` if free_slots > 0
  - else `-10 * (queue_depth + 1)`
  - `+20` if cross_model and client_family set and family(provider) != client_family
  - tie-break: higher score, then name ascending
- `route_reason` strings: `pinned`, `auto:free`, `auto:free+cross`,
  `auto:wait`, `auto:wait+cross`, `auto:default-finder` (fallback).

**Done when:** pure unit tests cover free vs busy, blackout, cross-model soft,
tie-break, empty pool → documented error type or None for caller.

### Task 3 — Store load views for routing

**Files:** `src/skodun/store.py` and/or thin wrappers in `routing.py`,  
`tests/test_routing.py` / store tests if new methods

- Read-only helpers (prefer reuse of `capacity_holder_count` + active queued
  views for `provider:<id>` scope):
  - holders per provider
  - queued depth per provider
- Use existing `provider_state` / same effective-unavailable notion as
  `chain._effective_provider_capacity` (import carefully to avoid cycles;
  duplicate the “is blackout?” predicate next to chain if needed).

**Done when:** tests with synthetic admissions + unavailable row drive
`pick_finder` integration with real Store (tmp db).

### Task 4 — Pipeline head resolution + artifact fields

**Files:** `src/skodun/pipeline.py`, `tests/test_pipeline.py` (or routing
integration), any record builder that sets `requested_reviewer`

- Replace “first enabled finder” path with `resolve_review_head(cfg, store,
  requested=None, client_family=None) -> tuple[Reviewer, route_meta]`.
- Pin path: existing `_requested_head`; `route_reason=pinned`.
- Auto path only if `routing.mode == auto` and no pin.
- Mode off: today’s first enabled finder; reason `auto:default-finder` or
  `config-finder` for clarity when mode off.
- Persist on artifact (and index if already mirrored):  
  `requested_reviewer`, `routed_reviewer`, `route_reason`, `client_family`.
- Do **not** change gate/trust modules.

**Done when:** pipeline tests pin vs auto; artifact fields present;  
`git diff -- src/skodun/gate.py src/skodun/trust.py` empty.

### Task 5 — CLI + services surface

**Files:** `src/skodun/cli.py`, `src/skodun/services.py`, `tests/test_cli.py`

- `skodun review --client-family <fam>` optional; pass through `svc_review`.
- Env `SKODUN_CLIENT_FAMILY` when flag omitted.
- `svc_review(..., client_family=None)` → `run_review`.
- Optional: `skodun providers` or `doctor` line for routing mode (nice-to-have
  in this task if cheap; else Task 7).

**Done when:** CLI help + test flag/env reach route path (mock or unit).

### Task 6 — MCP surface

**Files:** `src/skodun/mcpserver.py`, `tests/test_mcptools.py`

- Optional tool arg `client_family` on `review` (string).
- Optionally stash `clientInfo.name` from `initialize` as default family
  heuristic (map grok/claude/codex/cursor → family or leave unknown).
- Update tool description: omit `reviewer` → auto when mode=auto; pin still
  absolute.
- Keep refuse-if-busy unchanged.

**Done when:** schema snapshot/tests for new property; handler passes family.

### Task 7 — Docs + examples

**Files:**

- `examples/fragments/concurrency.md`
- `examples/multi-provider.toml` (`[routing] mode = "auto"` + pool)
- `examples/AGENTS.md` / `mcp-loop.md` (prefer omit reviewer; pin for 2nd opinion)
- `docs/integrate-external-project.md`
- `docs/epics/s5-provider-auto-route.md` checkboxes
- README short note if routing is user-visible

**Done when:** docs state mode default off, pin wins, cross-model soft.

### Task 8 — Phase B stub only (issue, not implement in A)

Open tracking issue only: weighted / credit-based routing. No code in Phase A.

## Verification (Phase A complete)

```bash
python -m pytest tests/test_routing.py tests/test_config.py \
  tests/test_pipeline.py tests/test_cli.py tests/test_mcptools.py \
  tests/test_chain.py tests/test_capacity.py -q --tb=line
# full suite before merge
python -m pytest -q --tb=line
# gate/trust untouched
git diff -- src/skodun/gate.py src/skodun/trust.py
```

Dogfood: multi-provider config, `mode=auto`, two agents omit reviewer, confirm
different `routed_reviewer` / providers when both free (log/artifact).

## Out of scope (do not implement in child issues of Phase A)

- Mid-wait rebind to another provider  
- Parallel multi-provider voting  
- Host-wide single MCP daemon as router  
- SCHEMA_VERSION bump unless artifact-only proves impossible  
- Changing skeptic/refuter role resolution  

## Suggested PR stack

1. Config + pure `routing.py` + store views + tests (Tasks 1–3)  
2. Pipeline + CLI + MCP wire (Tasks 4–6)  
3. Docs + example (Task 7)  

Or one PR if small enough; prefer 1–2 PRs for reviewability.
