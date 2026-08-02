# Plan: S3 fair review capacity

Date: 2026-08-02. Parent epic #42. Design:
`docs/superpowers/specs/2026-08-02-s3-fair-capacity-design.md`.

## Tasks

1. **Schema v6** — `capacity_admissions` table + index; bump `SCHEMA_VERSION`;
   fix store tests that pin version `5`.
2. **`capacity.py`** — pure FIFO helpers; enqueue / try_admit / finish;
   acquire loop with progress callback; env knobs.
3. **Store methods** — transactional enqueue, try_admit, mark_started, finish,
   get, position.
4. **Pipeline wire** — provider-chain preflight short-circuit; capacity wait
   before/with FG lock dual-hold; release in `finally`.
5. **MCP** — keep refuse-if-busy; align tool description with S3 docs.
6. **Tests** — `tests/test_capacity.py` (+ store migration pins).
7. **Docs** — epic seed, concurrency fragment, integrate guide.

## Verification

- `python -m pytest tests/test_capacity.py tests/test_store.py tests/test_pipeline.py tests/test_mcpserver.py tests/test_s1_status_cancel.py -q --tb=line`
- Broader suite if time permits
- Confirm `git diff -- src/skodun/gate.py src/skodun/trust.py` empty
