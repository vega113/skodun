# Recommendations A–I Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Waves 0–2, close #141/#142/#164/#147–#149/#165–#167, then upgrade the installed authority and migrate the shared store.

**Architecture:** Serialize schema/install first (#165). Then S6 (#166) and S8 (#167) with shared-seam serialization. Then S7 in order #147 → #148 → #149. One immutable wheel from merged main is the only production migrator.

**Tech Stack:** Python ≥ 3.12, stdlib-only runtime, pytest, hatchling wheel metadata, SQLite additive `_MIGRATIONS`.

---

## Issue / PR boundaries

| Wave | Issue | PR files (primary) | Schema bump? | Depends on |
|---|---|---|---|---|
| 0 | #165 under #164 | `provenance.py`, `store.py`, `cli.py`, `doctor.py`, `mcpserver.py`, `pyproject.toml`, `hatch_build.py`, `tests/test_schema_lifecycle.py`, `tests/test_provenance.py` | No (stay at 14) | none |
| 1a | #166 under #141 | `stack.py`, `promptbuild.py`, `budget.py`, `fingerprint.py`, `dispatch.py`; serialized `pipeline.py`, `services.py`, `store.py` | Yes, only if lineage index is added (15) | #165 |
| 1b | #167 under #164 | `readmodel.py`, `telemetry.py`, `checkpoints.py`; serialized `pipeline.py`, `services.py` | No unless #166 already took 15 | #165 |
| 2a | #147 under #142 | new receipt module, `store.py` `_MIGRATIONS`, `services.py`, CLI/MCP projections | Yes (next unused) | #165 |
| 2b | #148 | proof runner + tests | No unless storage needed | #147 |
| 2c | #149 | capability profiles + Scala corpus + ingestion | maybe compact evidence table | #147/#148 |

If #166 and #167 both need `pipeline.py`/`services.py`, serialize those files on one branch at a time. Do not merge overlapping shared-seam PRs without rebase onto refreshed main.

## Migration sequencing

1. #165 lands on schema 14. Still do **not** migrate the shared schema-13 store.
2. #166 may add `ix_finding_lineage_repo_created_review` as schema 15.
3. #147 adds receipt tables as the next unused version.
4. Shared-store apply happens once, from the final immutable wheel, after all of the above are on main.

## Test gates (every PR)

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest <focused> -q --tb=short
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short
# if store/process/schema lifecycle:
python3 -m pytest tests/test_store.py \
  --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error
```

`git diff --check` on `src/skodun/gate.py src/skodun/trust.py` must be empty.
Source smoke uses `SKODUN_DB` pointing at a disposable file. Never the default
shared database.

---

### Task 1: Wave 0 spec already written

**Files:**
- Create: `docs/superpowers/specs/2026-08-13-recommendations-completion-design.md`
- Create: `docs/superpowers/plans/2026-08-13-recommendations-completion.md`

- [x] **Step 1: Design and plan exist on this branch**

---

### Task 2: Distinguishable package version and embedded wheel identity

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/skodun/__init__.py`
- Create: `hatch_build.py`
- Modify: `src/skodun/provenance.py`
- Modify: `tests/test_provenance.py`
- Modify: `src/skodun/doctor.py`
- Modify: `src/skodun/mcpserver.py`

- [ ] **Step 1: Write the failing provenance tests**

```python
def test_embedded_wheel_identity_is_preferred_over_git(monkeypatch):
    monkeypatch.setattr(provenance, "_embedded_identity",
                        lambda: {"skodun_commit": "a" * 40, "source": "wheel"})
    monkeypatch.setattr(provenance, "_read_commit", lambda root: "b" * 40)
    got = provenance.code_provenance()
    assert got["skodun_commit"] == "a" * 40

def test_frozen_install_without_embed_still_reports_none(monkeypatch, tmp_path):
    monkeypatch.setattr(provenance, "_package_root", lambda: tmp_path)
    monkeypatch.setattr(provenance, "_embedded_identity", lambda: None)
    assert provenance.code_provenance()["skodun_commit"] is None
```

Update `test_a_frozen_install_reports_no_commit` so a wheel **with** embed
reports the embed, and a wheel **without** embed still reports `None`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_provenance.py -q --tb=short`
Expected: FAIL on missing `_embedded_identity`.

- [ ] **Step 3: Implement version bump and hatch hook**

`pyproject.toml` / `__init__.py`: version `0.5.0`.

`hatch_build.py` writes only into the wheel (never the source tree):

```python
COMMIT = "<40 hex or 40 hex-dirty>"
SCHEMA_VERSION = 14
VERSION = "0.5.0"
```

Refuse to build a wheel if git HEAD cannot be read. Dirty trees embed
`<sha>-dirty` so migration still refuses them.

- [ ] **Step 4: Implement `provenance._embedded_identity` and handshake**

`code_provenance()` uses embed first. MCP `initialize` includes `commit` from
embed even when the git cache is cold. Doctor package line always prints
`commit=` (literal `null` if absent) plus `schema_v=`.

- [ ] **Step 5: Re-run provenance/doctor/mcp tests; they pass**

---

### Task 3: Authenticate migration from installed identity

**Files:**
- Modify: `src/skodun/cli.py` (`_cmd_store`)
- Modify: `src/skodun/store.py` (`migrate_existing`)
- Modify: `tests/test_schema_lifecycle.py`

- [ ] **Step 1: Write failing tests**

```python
def test_cli_wheel_apply_uses_embedded_commit(tmp_path, capsys, monkeypatch):
    db = _authority_db(tmp_path)
    with Store.open(db):
        pass
    _downgrade(db)
    monkeypatch.setattr("skodun.provenance.code_provenance",
                        lambda: {"skodun_commit": "c" * 40})
    assert main(["store", "migrate", "--apply", "--db", str(db)]) == 0
    assert inspect_schema(db).state == "current"

def test_cli_apply_rejects_mismatched_build_commit(tmp_path, monkeypatch):
    ...
    assert main(["store", "migrate", "--apply", "--db", str(db),
                 "--build-commit", "d" * 40]) == 2
```

Also: dirty embed refused; default-shared-db + source dirty refused; disposable
`SKODUN_DB` + clean checkout allowed.

- [ ] **Step 2: Run to verify fail**

- [ ] **Step 3: CLI uses provenance identity; `--build-commit` is match-only**

Remove any path that lets a caller-asserted hash become the authority when
provenance is `None`. Wheel `commit` present → apply. Provenance `None` →
`build_identity_required`.

- [ ] **Step 4: Tests pass**

---

### Task 4: Harden inspection and readonly opens

**Files:**
- Modify: `src/skodun/store.py` (`inspect_schema`, `open_readonly`, `Store.close`)
- Modify: `tests/test_schema_lifecycle.py`

- [ ] **Step 1: Write failing tests**

FIFO path returns `not_a_file` in bounded time.
Symlink returns `symlink`.
Unreadable WAL/copy failure returns `snapshot_failed` / `unreadable_sidecar`
without traceback.
`open_readonly` on WAL-without-SHM does not create original `-shm`.
Doctor/readiness remain byte-stable.

- [ ] **Step 2: Implement descriptor-safe inspect + snapshot-backed readonly**

`os.open(..., O_RDONLY | O_NOFOLLOW | O_NONBLOCK)`, `fstat` must be regular
file. Copy failures become `SchemaInfo("invalid", reason_code=...)`.
`Store` may hold a `TemporaryDirectory` cleaned in `close()`.

- [ ] **Step 3: Tests pass**

---

### Task 5: Liveness-aware blockers, independent FG lock, retryable artifacts

**Files:**
- Modify: `src/skodun/store.py` (`migration_blockers`, `migrate_existing`, `Store.open`)
- Modify: `tests/test_schema_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Dead capacity PID is not `active_capacity_admission`.
Expired checkpoint lease is not `active_checkpoint_claim`.
Legacy lock at git-common-dir blocks even with zero capacity rows.
Dead lock owner is not a forever-blocker.
v0–v2 store without `reviews.pid` still migrates.
Failed apply (injected backup error) is retryable; receipt is
`migration-receipt-v{SCHEMA_VERSION}.json`.
Connect failure after `.init.lock` does not leak the lock.
Concurrent migrators: second is `migration_busy`.

- [ ] **Step 2: Implement**

Use `capacity.should_reclaim_admission` read-only.
Checkpoint blocker: `state='running' AND lease_expires_at > now`.
Discover lock scopes from all `capacity_admissions.scope` values plus any
`reviews` repo/common-dir we can read; check `LOCK_NAME` independently.
On apply failure before commit: unlink unverified receipt and backup.
Wrap `sqlite3.connect` in the init-lock `try`/`finally`.
PRAGMA `table_info` before selecting `pid`.

- [ ] **Step 3: Tests pass**

---

### Task 6: Wave 0 verification, commit, PR, merge

- [ ] Focused: `tests/test_schema_lifecycle.py tests/test_provenance.py tests/test_doctor.py tests/test_mcpserver.py`
- [ ] Full suite + store sweep
- [ ] gate/trust byte pin
- [ ] Commit with complete sentence + `refs #165`
- [ ] Push, PR, review threads to 0, merge, close #165 only after merged-main smoke
- [ ] **Do not migrate the shared store yet**

---

### Task 7: Wave 1 lane 1 (#166)

Implement the S6 contract in the design §6.1. Prefer `stack.py` /
`promptbuild.py` / `budget.py` first; then serialized `pipeline.py` /
`services.py` / `store.py`. Include TubeScribes reproduction tests. Schema 15
only for the lineage index. PR, merge, keep #141 open.

---

### Task 8: Wave 1 lane 2 (#167)

Implement the S8 contract in the design §6.2. Re-run 3-of-4 timeout/resume.
If #166 is in flight on shared seams, wait and rebase. PR, merge, keep #164
open until install.

---

### Task 9: Combined S6/S8 pins on refreshed main

Rebase leftover work. Run combined stacked-PR + 3-of-4 + lineage + coverage
tests.

---

### Task 10: #147 trusted receipts

New module + additive store + services projections. Advisory only. No new
trust axis. No second migration-receipt format.

---

### Task 11: #148 mutation proofs

Proof state machine and reject corpus through bounded runner.

---

### Task 12: #149 Scala / capability pilot

Protected profiles, Scala 3 corpus, real compiler/harness, receipt ingestion,
prompt-size reduction evidence, offline cleanup.

---

### Task 13: Close implementation issues

Close #147/#148/#149/#142/#166/#167/#141 only after merged-main acceptance.
Leave #164 open until install/migration.

---

### Task 14: Final install and shared-store migration

Only from the exact clean merged commit. Rehearse on a copy. Apply to shared
store. Install the same wheel. Restart MCP through supported mechanisms.
Compare CLI/MCP identity. Client smoke without source fallback.

---

## Self-review

- Spec §5–§10 each have tasks 2–14.
- No TBD/placeholder steps in Wave 0; later waves are issue-scoped because
  their code depends on Wave 0 landing.
- Names (`_embedded_identity`, `migration-receipt-v{N}.json`, schema 14 stay)
  are consistent with the spec.
