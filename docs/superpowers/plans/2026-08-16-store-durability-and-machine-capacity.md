# Plan — store durability + machine-wide review capacity

1. Slice A in `store.py` / `doctor.py`: distinguish `SQLITE_BUSY` from
   malformed; apply WAL durability PRAGMAs (`synchronous=FULL`); on
   WAL-header + missing/empty `-wal` that fails integrity, quarantine
   `*.malformed-<utc>` (never delete) and fail closed unless `.recover`
   produces a verified replacement. Do not change the default to per-repo
   DBs; do not flip a healthy `journal_mode=DELETE` store back to WAL.
2. Doctor store line prints `journal_mode`, `-wal`/`-shm` presence or size,
   and `integrity_check`.
3. Slice B spec under `docs/superpowers/specs/`; implement the machine-wide
   outer cap in `capacity_admissions` only if the remaining 1-vs-2 default
   is a conservative pick (default 1), not an owner product fork.
4. Hermetic tests in `tests/test_store_durability.py` (and doctor/capacity
   shipped-path tests). Never open the live `~/.local/share/skodun/skodun.db`.
5. `gate.py` / `trust.py` stay byte-identical.
