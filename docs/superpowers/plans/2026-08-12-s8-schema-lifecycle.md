# Plan — #155 explicit schema lifecycle

1. Add schema inspection and exact read-only/open policies in `store.py`.
   Preserve fail-closed newer-schema behavior and make existing older schemas
   refuse ordinary writable opens without any mutation.
2. Add migration plan/apply primitives: maintenance lock, active-work refusal,
   SQLite backup/verification, ordered migration application, integrity checks,
   and bounded receipt persistence.
3. Add CLI-only `store migrate --plan|--apply` wiring and stable diagnostics;
   route read-only CLI projections through non-mutating opens.
4. Add source/editable/default-store isolation and immutable build identity
   checks; keep MCP startup/tool calls migration-free.
5. Add shipped-path tests for byte stability, migration safety/failure, CLI
   behavior, MCP parity, receipts, and unchanged gate/trust hashes.
6. Run focused tests, the prescribed store ResourceWarning sweep, then the
   full suite with the known heavy sweep result recorded precisely. Freeze the
   diff for exact-head review and merge only after current-head CI/review state
   is clean.
