# S8.4 — explicit, isolated, recoverable schema lifecycle

## Contract

SQLite schema compatibility, inspection, and migration are separate policies.
Opening an existing store for a diagnostic or ordinary writable operation must
never advance `user_version`, enable WAL, create DDL, or create sibling
directories as a side effect. A newer schema remains a fail-closed refusal.
An older schema is reported as `migration_required` until an explicit CLI-only
maintenance command applies the ordered migration ladder.

The migration command is the only writer allowed to upgrade an existing store.
It requires an exact clean build identity for the installed authority, takes an
exclusive maintenance lock, refuses active review/checkpoint work, creates and
verifies a restrictive SQLite backup, applies the existing additive migration
ladder with replay-idempotent string deltas applied directly and
non-idempotent deltas applied transactionally, verifies integrity and schema
objects, and records a
bounded receipt. Failure leaves either the untouched old store or a verified
backup with recovery instructions; it never reports a partial success.

## State machine

```text
missing --(ordinary open)--> initialized at current schema
existing/current --(read-only or ordinary open)--> usable, byte-stable
existing/older --(inspection/read-only)--> migration_required, byte-stable
existing/older --(explicit plan)--> planned, no writes
planned --(apply + lock + clean build + backup)--> migrating
migrating --(commit + verify + receipt)--> current
migrating --(failure)--> refused, old store or verified backup recoverable
existing/newer --(any non-maintenance open)--> schema_too_new, byte-stable
```

## API boundaries

* `inspect_schema(path)` uses SQLite URI read-only mode when the file exists;
  it never creates a parent, enables WAL, runs `_SCHEMA`, or changes PRAGMAs.
  Missing paths return `missing` without creating anything.
* `Store.open(path)` initializes a missing database, but an existing database
  must already equal `SCHEMA_VERSION`; it refuses older and newer schemas with
  stable reason codes. `Store.open_readonly(path)` is byte-stable and exact.
* `Store.migrate_existing(path, *, build_commit, receipt_path)` is private to
  the CLI migration command and is never called by MCP or services.
* `skodun store migrate --plan` is read-only. `--apply` is the sole upgrade
  action and emits a receipt path and digest.
* Diagnostics (`doctor`, `review-readiness`, `providers`, `stats`, `log`, and
  `gate`) use read-only opens where they only project state. A missing database
  remains an empty, disposable authority; an older database is a bounded
  refusal, not an implicit migration.

## Safety decisions

* `gate.py` and `trust.py` remain byte-identical.
* The Phase-1 `_SCHEMA` stays frozen; schema changes remain additive entries in
  `_MIGRATIONS`.
* No automatic downgrade or two-schema dual authority is supported.
* Build identity is version plus exact commit; dirty/unknown source builds may
  inspect disposable stores but cannot migrate the default shared authority.
* Backups use SQLite's backup API, restrictive permissions, integrity checks,
  and a digest recorded in a bounded receipt. Receipt writes are bounded and
  do not contain prompts, transcripts, environment values, or secrets.
* MCP initialization never opens or migrates the store. Hosts must restart and
  compare version, commit, and schema after maintenance.

## Verification

Hermetic tests snapshot database bytes, WAL/SHM siblings, directory entries,
and `user_version` before and after every read-only command. Migration tests
cover missing/current/older/newer stores, clean/dirty build identity, active
foreground/checkpoint claims, lock contention, backup permissions and digest,
integrity verification, failure rollback, bounded receipts, CLI/MCP parity,
and unchanged gate/trust hashes.
