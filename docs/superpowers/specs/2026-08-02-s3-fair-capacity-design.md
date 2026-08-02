# skodun S3 — fair review capacity (FIFO admission + telemetry)

Date: 2026-08-02. Status: design for epic #42 (children #45 design, #46 wire).
Prerequisite reading: `docs/epics/s3-fair-capacity.md`,
`examples/fragments/concurrency.md`, `docs/integrate-external-project.md` §2,
and the foreground lock protocol in `src/skodun/pipeline.py` (module docstring).

## Scope

Replace multi-hour “wait for the repo lock with no inference” starvation with:

1. Explicit **review-fg** capacity admission (default capacity **1**).
2. **FIFO** waiters with a **bounded** admission timeout and durable
   `expired` / `rejected` outcomes (no blind infinite requeue).
3. **Persisted** queue telemetry: `queued_at`, `admitted_at`, `started_at`,
   `ended_at`, `wait_ms`, and expire/reject reason when applicable.
4. CLI surfaces **queue position** and **remaining wait budget** while waiting.
5. Preflight: if the **entire finder provider chain** is known unavailable via
   existing `provider_state`, refuse **before** spending the admission wait.
6. MCP policy: **refuse-if-busy** (keep current posture); document why.
7. **Legacy lock bridge**: dual-hold with `grok-reviews-foreground.lock` so
   tubescribes / legacy scripts still serialize with skodun.

**Out of scope (epic non-goals):** full host multi-MCP fair queue, TubeScribes
Karma/DB locks, parallel multi-provider voting, scheduler jobs inside
`skodun mcp`, required `review-bg` / `provider:<id>` classes (optional stubs
only), any change to `gate.py` / `trust.py`.

## Explicit decisions

| Topic | Decision | Why |
|---|---|---|
| MCP under contention | **Refuse-if-busy** (status 2, `"review already in flight"`) | A second MCP `review` queued behind the first would run against a tree that has usually moved; fingerprint admit is the alternative and is **not** chosen for this epic. |
| Physical mutual exclusion | Keep **legacy mkdir lock** path + owner format | Shadow cutover with tubescribes/legacy scripts is load-bearing; renaming or dropping the lock reintroduces dual-backend contention. |
| Cross-process FIFO | **SQLite total order** (`capacity_admissions.queued_at`, `id`) | In-process queues cannot order multi-process waiters; mkdir alone is a race, not FIFO. |
| Capacity default | **1** (`SKODUN_REVIEW_FG_CAPACITY`) | Matches one shared inference backend and the legacy lock’s effective concurrency. |
| Dual-hold | Store slot **and** legacy lock for every admitted FG review | Store gives fair order + telemetry; lock keeps legacy interop. |
| Capacity > 1 | Store admits up to N; **legacy lock still serializes physical runs to 1** until the bridge is retired | Raising capacity is supported for the admission layer and future bridge removal; operators who raise it today still do not double-admit against legacy holders. |
| Expire outcome | Durable `expired` row + `LockTimeout` (CLI exit **3**) | Same operator-visible exit as today’s lock wait; telemetry records the reason. |
| Preflight chain | Finder chain only (head + configured fallbacks) | That is the chain that would spend the wait budget before any useful inference; extra-pass roles are not required for the short-circuit. |

## Rejected alternatives

### A. Replace the mkdir lock with store-only admission

Rejected for shadow coexistence: legacy scripts only understand
`grok-reviews-foreground.lock`. A skodun-only store gate would double-admit
against a live legacy holder.

### B. MCP admit-with-queue (no fingerprint)

Rejected: documented stale-tree hazard in `mcpserver` and the concurrency
fragment. If a future epic chooses admit, fingerprint re-check at start with
cancel-on-move is mandatory.

### C. In-process FIFO only

Rejected: multi-agent / multi-worktree load is multi-process. An in-process
queue claims FIFO while peers overtake via independent processes.

### D. Infinite requeue after admission timeout

Rejected by the epic: expiry must be durable and terminal for that attempt.
The operator re-runs explicitly.

## Architecture

### Resource class

```text
review-fg   — foreground CLI / pipeline run_review (this epic)
review-bg   — optional stub name only; not wired
provider:<id> — out of scope
```

### Module layout

```text
src/skodun/capacity.py   — pure FIFO decision helpers + acquire/release loop
src/skodun/store.py      — v6 `capacity_admissions` + transactional admit/finish
src/skodun/pipeline.py   — preflight short-circuit + wire acquire around FG lock
src/skodun/mcpserver.py  — refuse-if-busy unchanged; docs/tool text cite S3 choice
```

`gate.py` and `trust.py` stay **byte-identical**.

### Schema (v6)

One atomic ladder rung. Replay-safe `CREATE TABLE IF NOT EXISTS`:

```sql
CREATE TABLE IF NOT EXISTS capacity_admissions (
  id TEXT PRIMARY KEY,
  resource_class TEXT NOT NULL,
  scope TEXT NOT NULL,          -- git common dir (same scope as FG lock)
  status TEXT NOT NULL,         -- queued|admitted|running|released|expired|rejected
  queued_at TEXT NOT NULL,      -- canonical store UTC Z timestamp
  admitted_at TEXT,
  started_at TEXT,
  ended_at TEXT,
  wait_ms INTEGER,
  expire_reason TEXT,
  pid INTEGER,
  review_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_capacity_scope_status
  ON capacity_admissions(resource_class, scope, status, queued_at, id);
```

`SCHEMA_VERSION` becomes **6**. No backfill; empty table on upgrade.

### FIFO admit rule (pure)

Given active rows for `(resource_class, scope)` with status in
`{queued, admitted, running}`:

1. `holders = count(status in {admitted, running})`
2. If `holders >= capacity` → not eligible
3. Among `status=queued`, sort by `(queued_at, id)` ascending
4. Only the **first** queued id is eligible to transition to `admitted`

Expire is terminal: `status=expired`, `expire_reason` set, `ended_at` /
`wait_ms` filled. That id never returns to `queued`.

### Dead / stale reclaim (multi-process)

A SIGKILLed waiter never reaches `finish`, so its row can sit as `queued` or
`running` forever and poison FIFO (dead head never calls `try_lock`; dead
holder blocks capacity forever even when the legacy lock is free).

Before every FIFO decide / `try_lock` attempt, peers reclaim active rows for
the same `(resource_class, scope)` when:

* `pid` is known and not alive → `rejected` / `stale_pid_dead` (immediate);
* status is a **holder** (`admitted` / `running`) and age since `queued_at`
  exceeds the FG lock stale ceiling → `rejected` / `stale_age`;
* `queued` with no usable pid and age past the same ceiling → same.

Live `queued` waiters are **not** age-reclaimed (admission timeout bounds them).

### Pipeline wire

Order inside `_run_review` after adapter preflight:

1. **Provider-chain preflight** — if every finder-chain provider has an active
   `provider_state` row, raise `PreflightRefused` (exit 2). No enqueue, no
   lock wait budget.
2. `recover_stale` (unchanged).
3. Size lock wait / stale ceiling (unchanged knobs).
4. **Capacity enqueue** for `review-fg` scoped to `git_common_dir`.
5. Wait loop: report position + remaining budget on stderr / `progress_sink`;
   when FIFO-eligible, attempt legacy `_acquire_fg_lock` with a short slice of
   the remaining budget (reclaim rules unchanged).
6. On lock success: mark `admitted` + `started`, proceed with today’s body.
7. `finally`: release store ticket (`released` / `rejected` on cancel path)
   and release the FG lock (ABA guard unchanged).

Admission wait budget defaults to the same value as today’s
`SKODUN_LOCK_WAIT_SECONDS` / stale ceiling so operators keep one mental model.
Optional override: `SKODUN_ADMISSION_WAIT_SECONDS`.

Capacity: `SKODUN_REVIEW_FG_CAPACITY` (integer ≥ 1, default 1). Junk degrades
to default 1 (same style as lock env knobs).

### Telemetry lifecycle

| Field | When set |
|---|---|
| `queued_at` | enqueue |
| `admitted_at` | FIFO slot taken (paired with successful lock for dual-hold) |
| `started_at` | review body begins under the lock (may equal `admitted_at`) |
| `ended_at` | release / expire / reject |
| `wait_ms` | `ended_at - queued_at` in ms (integer) |
| `expire_reason` | e.g. `admission_timeout`, `cancelled` |

### CLI visibility

While waiting, progress lines include at least:

```text
skodun: review-fg queue position 2; wait budget 180s remaining
```

Not silent spin-only. Position is 1-based among non-terminal rows for the
scope ordered by `queued_at`.

### MCP policy (chosen: refuse-if-busy)

Unchanged runtime behaviour:

- One long-running `review` per MCP server process.
- Second call → `BUSY_STATUS=2`, `BUSY_TEXT="review already in flight"`.
- Closing the session still cancels the in-flight review (S1).

Documented in concurrency fragment + integrate guide as the **S3 choice**,
not a temporary gap.

### Legacy lock bridge

```text
skodun waiter:  enqueue(store) → FIFO head → mkdir(legacy lock) → run → release both
legacy script:  mkdir(legacy lock) only
```

Skodun never renames `LOCK_NAME` or the three-line `owner` format. The store
queue is additive. A live legacy holder prevents mkdir; the FIFO head keeps
polling until reclaim rules fire, the holder finishes, or admission expires.

### Preflight unavailable short-circuit

Uses `Store.provider_unavailable_reason` (respects
`SKODUN_IGNORE_PROVIDER_STATE`). Only **active** TTLs count. If any chain
entry is free of an active row, admission proceeds normally.

## Config / env knobs

| Knob | Default | Meaning |
|---|---|---|
| `SKODUN_REVIEW_FG_CAPACITY` | `1` | Max concurrent admitted+running `review-fg` holders per scope in the store |
| `SKODUN_ADMISSION_WAIT_SECONDS` | same as lock wait | Bounded admission budget |
| `SKODUN_LOCK_WAIT_SECONDS` | stale ceiling | Legacy lock wait (still used for dual-hold slices) |
| `SKODUN_LOCK_POLL_SECONDS` | `10` | Poll cadence |
| `SKODUN_LOCK_STALE_SECONDS` | ceiling | Waiter reclaim ceiling |

## Tests (hermetic)

1. Pure FIFO: later waiter never overtakes earlier when capacity=1.
2. Bounded expire: durable `expired` + reason; no requeue of same id.
3. Telemetry fields present after admit/release and after expire.
4. Preflight: full chain unavailable → refuse without consuming full wait.
5. MCP: existing busy-refusal tests remain the policy pin.
6. Migration: fresh store at v6; v5 store gains `capacity_admissions`.
7. `gate.py` / `trust.py` content unchanged (suite / `git diff` check).

## Docs to update when shipping

- `docs/epics/s3-fair-capacity.md` checklist
- `examples/fragments/concurrency.md`
- `docs/integrate-external-project.md` § concurrency
- This design + plan under `docs/superpowers/`

## Success

Epic #42 closes only when wire (#46) and tests/docs match this design — a
design-only diff is not done.
