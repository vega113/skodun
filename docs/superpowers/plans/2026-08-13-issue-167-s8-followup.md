# S8 follow-up: coverage projections, checkpoint leases, and telemetry

## Goal

Complete issue #167 on the merged `origin/main` baseline without changing
`gate.py` or `trust.py`: make partial checkpoint evidence visible and
gate-ineligible, preserve explicit pass states and finder/refuter semantics,
prevent duplicate provider calls during admission waits, and make persisted
timing/prompt telemetry honest across CLI/MCP projections and checkpoint reuse.

## Acceptance slices

1. Add failing read-model tests for completed checkpoint payload decoding,
   pending-state projection, strict booleans, extra-pass status vocabulary,
   unbatched completion, and finder-only/refuter semantics. Implement the
   smallest pure projection change and exercise it through review-status CLI/MCP
   tests.
2. Add failing checkpoint/pipeline tests for admission-wait-aware leases,
   completed-checkpoint provenance reuse, unknown-vs-zero timing, aggregate
   integration prompt bytes, and one effective batch target feeding planning,
   identity, and execution. Implement only the shared checkpoint/orchestration
   seam required by those tests.
3. Add the shipped 3-of-4 timeout/resume regression: three completed batches
   remain visible, resume invokes only batch four plus integration, and the
   final review matches a fresh run except for allowed unknown provenance.
4. Run focused tests, the store ResourceWarning sweep, and the full suite;
   verify `gate.py` and `trust.py` remain byte-identical. Commit, push, create
   the PR, address exact-head review threads, and merge only with green checks.

## Constraints

- Additive store changes only; no new receipt schema (issue #147 owns it).
- Preserve exact-diff reuse, triage, R2/R3, routing, recovery, and refuter
  annotation semantics.
- Keep unknown timing/token/version data as `None`, never synthetic zeroes.
- Do not edit `gate.py` or `trust.py`.
