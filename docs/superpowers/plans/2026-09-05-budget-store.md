# Execution-fenced budget persistence

**Goal:** Store #185 snapshots and actual capacity layers without letting an old
request execution overwrite current timing or apply new caps to old admissions.

- [x] Add failing real Store fixtures for snapshot/layer reads, ownership and
  execution fences, old/current history and invalid data.
- [x] Add budget_store.py with bounded strict JSON, canonical UTC, nonnegative
  finite numbers, private ownership guards and transaction-safe upserts.
- [x] Register additive migration 19 for per-execution snapshots and per-admission
  layers with indexed request/execution reads; preserve existing request data.
- [x] Extend explicit V19 object expectations and lifecycle inventory. Verify
  migration idempotence and focused Store/request/budget tests; self-review.
- [ ] Commit only this persistence slice for root to cherry-pick. No PR, provider,
  live-store, runtime or #188 source changes.

Self-review: scope is the shared Store connection and additive read/write doors.
Current reads select the latest request execution before looking for a snapshot,
so an old completed execution cannot masquerade as a current budget. Layers are
validated against real linked admissions and stay tied to original execution IDs.
Unknown current snapshots return None; malformed stored rows raise a typed error.
History is explicitly bounded and reports truncation. Tokens never enter snapshots
or public read projections.
