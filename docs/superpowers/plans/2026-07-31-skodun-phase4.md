# skodun Phase 4 Implementation Plan — Repository Scoping and the Stale-Recovery Scan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two repositories sharing one store stop colliding on branch names, and `recover_stale` stops decoding every stored artifact on the `git push` path.

**Architecture:** Fixed by `docs/superpowers/specs/2026-07-31-skodun-phase4-design.md` (owner-approved, **revised 2026-07-31 after an adversarial review**) — read it first, including its "Corrections to the approved spec" section; this plan implements it and does not re-litigate it. A `repo TEXT` column on `reviews` (store v5), written by **three** persistence paths, read by exactly three queries. `NULL` matches nothing. The gate, dedup, and triage are deliberately NOT scoped.

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest the only dev dependency.

**Execution note (2026-08-01).** Two findings from the review of this plan's own PR (#21) were confirmed against the executed code and are recorded here rather than left to contradict what shipped:

* **The finalized-worker test in T2 as written could not kill its mutation.** It builds `rec` from `st.get_review(...)`, whose artifact already carries `repo`, so it passes even when `run_prepush_review` omits `repo=reserved.get("repo")` — the exact site the phase turns on. Found independently during execution and by the PR review. What shipped instead adds `repo` to `test_the_worker_preserves_every_reservation_owned_field`, which drives a real reserve → `run_worker` → finalize cycle; the mutation dies there and nowhere else (commit `0223358`).
* **The T3 CLI/MCP `log` parity test as written was metadata-only** — it compared parser and schema shape, so acceptance criterion 7 could fail while it stayed green. The shipped suite adds `test_the_scoped_log_renders_identically_on_both_surfaces`, which runs the scoped form on both surfaces against one store and compares status and bytes.

A third review finding — that the drill's delivery mutation could survive because `_ROUNDS_SELECT` returns terminal rows while the drill specifies running ones — was **refuted by execution**: the drill carries `b_round`, a finalized background round, and removing the delivery predicate does fail it. The plan text was the stale artifact there, not the implementation.

**Revision note (2026-07-31).** This plan was rewritten after an adversarial review found eight defects that would have produced broken code. The two that would have broken the *phase* rather than a task are called out where they land: T2 Step 6 (the background worker's record, without which the headline fix is inert) and T1 Step 5 (`_review_values` is hand-written, so appending to `_REVIEW_COLUMNS` alone is a `ProgrammingError` on every write). Every code block below has been checked against the shipped source and uses the test helpers that actually exist.

## Global Constraints

- Everything in the Phase 1–3 Global Constraints still binds.
- **`gate.py` and `trust.py` stay byte-identical**, now across four phases, pinned by the sha256 constants already in `tests/test_seams.py:97-98`:
  `gate.py` = `62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f`,
  `trust.py` = `8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1`.
  No task modifies either file. If a task appears to need to, stop and surface it.
- **The v5 migration is installed COMPLETELY and atomically in Task 1.** The ladder runs a delta only while `user_version < target`, so later tasks may only *consume* v5 state, never extend it. Any DDL discovered missing later is a plan defect: stop, amend Task 1, re-migrate test DBs.
- Python ≥ 3.12, stdlib-only runtime, pytest only.
- Committed code fully generic: no machine paths. The oracle is reachable only via `SKODUN_ORACLE_DIR`; subagents do NOT inherit it — pass it explicitly and always report ran-vs-skipped counts.
- Tests never touch `~/.local/share/skodun/skodun.db` or `~/.grok`: pin `SKODUN_DB`, `GIT_CONFIG_GLOBAL`, and every `SKODUN_<X>_BIN` to tmp paths.
- **Method requirement, binding on every task:** between-task review is by **execution and mutation**, never inspection. Every task below lists named **Mutations**; each must be killed by a test, and the task states *which* test kills it. A mutation nothing kills is worse than no mutation — if a listed mutation turns out to survive, that is a plan defect: fix the test, then re-run the mutation. `PYTHONDONTWRITEBYTECODE=1` is mandatory during mutation runs. Commit before mutating — the revert step takes an uncommitted fix with it.
- Full suite ≈ 11–13 min: run it as a background command and poll its output file; never a monitor. Baseline at plan start: **3062 passed, 1 skipped** with the oracle.
- Commit per task (`refs #13`), push periodically.
- The model CLIs may be unavailable (codex credits, xai 402). The suite drives fake CLIs and needs none of them.

## File Structure

```text
src/skodun/
├── store.py       # modified (T1, T2, T3, T4): v5 delta, repo in _REVIEW_COLUMNS
│                  #   AND _review_values, scoped supersede, list_reviews scoping,
│                  #   running_records() for the sweep
├── pipeline.py    # modified (T2, T4): repo on the foreground record AND on the
│                  #   background worker's record; recover_stale reads indexed
│                  #   columns instead of artifacts; _SCAN_ALL retired
├── dispatch.py    # modified (T2): repo passed into reserve_prepush
├── delivery.py    # modified (T3): repo in the undelivered/replay query
├── cli.py         # modified (T3): surface/log resolve and pass the repo;
│                  #   `log` gains --repo
├── services.py    # modified (T3): svc_surface/svc_log take the repo through
└── mcpserver.py   # modified (T3): both tools convert their repo argument to a
                   #   common dir; `log` gains a repo property
tests/  (new: tests/test_repo_scoping.py — the two-repository drill)
```

---

### Task 1: Store v5 — the `repo` column and the widened index

**Files:** Modify `src/skodun/store.py`; `tests/test_store.py`.

**Interfaces:**
- Produces: `SCHEMA_VERSION = 5`; `_MIGRATION_V5: tuple[str, ...]`; `reviews.repo TEXT` (NULL on every pre-v5 row); index `ix_reviews_repo_branch` on `(repo, branch, reviewed_at)`; `"repo"` appended to `_REVIEW_COLUMNS` **and** to `_review_values`'s return tuple.
- Consumes: the v4 ladder shape in `_MIGRATIONS` and `_apply_atomic`.

Mirror the v3/v4 delta discipline exactly: a tuple of statements applied in ONE `BEGIN IMMEDIATE` with the version stamp, because `ALTER TABLE ADD COLUMN` is not replay-idempotent.

- [ ] **Step 1: Add the v4 fixture the migration test needs**

The shipped ladder tests never hand-stamp a `user_version`, and the reason matters here: a fresh `Store.open` stamps the *current* `SCHEMA_VERSION` with the column already added, so setting `user_version = 4` and reopening replays the `ALTER` into `duplicate column name`. The pattern that works is `_pinned_at_v3()`/`_v3_db()` (`tests/test_store.py:1489,1561`) — build a real prior-version store by running the ladder with this build pinned to that version. Add the v4 pair beside them, in the same shape:

```python
def _pinned_at_v4():
    """A context manager that makes this build behave as the shipped v4 one."""
    import contextlib
    import unittest.mock as _mock

    from skodun import store as store_mod

    stack = contextlib.ExitStack()
    stack.enter_context(_mock.patch.object(store_mod, "SCHEMA_VERSION", 4))
    stack.enter_context(_mock.patch.object(
        store_mod, "_MIGRATIONS",
        tuple((t, d) for t, d in store_mod._MIGRATIONS if t <= 4)))
    return stack


def _v4_db(path, *, triage_rows=(LEGACY_TRIAGE,)):
    """A real v4-shaped store: the v3 fixture, migrated by the v4 delta ALONE.

    What a shipped v4 build left on a user's disk, which is what the v5 delta
    must upgrade. Carries `_v2_db`'s `r1` review row, which is the pre-v5 row
    the NULL rule is about.
    """
    _v3_db(path, triage_rows=triage_rows)
    with _pinned_at_v4():
        Store.open(path).close()
    assert _user_version(path) == 4
    assert "repo" not in _columns(path, "reviews")
    return path
```

- [ ] **Step 2: Write the failing migration test**

```python
def test_a_v4_store_gains_the_repo_column_and_its_index(tmp_path):
    """v5 is additive: the column arrives NULL on every existing row and the
    shipped index is kept, not replaced."""
    db = _v4_db(tmp_path / "s.db")
    before = _objects(db)

    st = Store.open(db)

    assert _user_version(db) == SCHEMA_VERSION == 5
    assert "repo" in _columns(db, "reviews")
    row = st._c.execute("SELECT repo FROM reviews WHERE id='r1'").fetchone()
    assert row["repo"] is None, "a pre-v5 row must not be backfilled"
    idx = {name for kind, name in _objects(db) if kind == "index"}
    assert "ix_reviews_repo_branch" in idx
    assert "ix_reviews_branch" in idx, "the shipped index is kept, not dropped"
    assert before < _objects(db), "the delta added nothing"
    st.close()
```

- [ ] **Step 3: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q -k "gains_the_repo_column"`
Expected: FAIL — `SCHEMA_VERSION` is 4, so `_v4_db`'s own `_user_version(path) == 4` assertion passes and the `== 5` assertion is what breaks.

- [ ] **Step 4: Add the v5 delta**

In `src/skodun/store.py`, beside `_MIGRATION_V4`:

```python
# --- v5: repository scoping -------------------------------------------------
#
# `reviews` was keyed by branch alone, so two repositories sharing one store
# collided on any common branch name: a push in one retired and SIGTERMed the
# other's running worker, and one `surface` call delivered AND acknowledged
# both repositories' rounds. The column carries `gitio.git_common_dir(repo)` --
# the same expression the foreground lock scopes by, so "the same repository"
# has one definition.
#
# NO BACKFILL. A pre-v5 row keeps `repo IS NULL` permanently and `repo = ?`
# excludes it from every scoped query, which is fail-closed: an old row goes
# invisible rather than the wrong repository's worker being killed. The
# accepted cost is that background rounds recorded before the upgrade are
# never delivered by `surface`.
_MIGRATION_V5: tuple[str, ...] = (
    "ALTER TABLE reviews ADD COLUMN repo TEXT",
    # The shipped `ix_reviews_branch` is kept (the Phase 1 additive rule); this
    # one leads with the column the scoped queries now filter on first.
    "CREATE INDEX IF NOT EXISTS ix_reviews_repo_branch"
    " ON reviews(repo, branch, reviewed_at)",
)
```

Then set `SCHEMA_VERSION = 5` and append `(5, _MIGRATION_V5)` to `_MIGRATIONS`.

- [ ] **Step 5: Add `repo` to the persisted columns — BOTH places**

**This is one of the two defects that would have broken the phase.** `_REVIEW_COLUMNS` (`store.py:523-530`) and `_review_values` (`store.py:532-553`) are *not* derived from one another: the column list is a tuple of names, and `_review_values` is a hand-written positional tuple that has to match it by hand. `_INSERT_REVIEW` and `_FINALIZE_REVIEW` (`store.py:556-577`) size their placeholders from the column list, so appending `"repo"` to the list alone gives 27 placeholders against 26 binds and a `sqlite3.ProgrammingError` on **every** review write in the project.

Both edits, in the same step:

1. In `_REVIEW_COLUMNS`, append `"repo"` after `"superseded_by"`.
2. In `_review_values`, append `rec.get("repo")` to the returned tuple, after `rec.get("superseded_by")`:

```python
        rec.get("summary"), rec.get("source", SKODUN_SOURCE),
        json.dumps(rec, ensure_ascii=False),
        _opt_positive_int(rec.get("worst_runtime_sec")),
        _opt_positive_int(rec.get("pid")), rec.get("superseded_by"),
        rec.get("repo"),
    )
```

`_normalize_record` copies the caller's dict wholesale and filters no keys, so a record carrying `repo` reaches both the indexed column and `artifact_json` from the same dict — the Phase 1 rule, unchanged.

- [ ] **Step 6: Run the test and the rest of the store suite**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q`
Expected: PASS for the new test; several shipped tests fail on the hardcoded version, which is Step 7.

- [ ] **Step 7: Retarget the version assertions and extend the ladder**

Four assertions spell the version as a literal `4` and must become `5`:

- `tests/test_store.py:977` — `assert SCHEMA_VERSION == 4`
- `tests/test_store.py:1006` — `assert st._c.execute("PRAGMA user_version").fetchone()[0] == 4`
- `tests/test_store.py:1473` — `assert _MIGRATIONS[-1][0] == SCHEMA_VERSION == 4`
- `tests/test_store.py:1511` — `assert _user_version(db) == SCHEMA_VERSION == 4`

And one test is not a literal to bump but a fixture to move:

- `tests/test_store.py:1724` — `test_a_store_stamped_v5_is_still_refused_untouched` builds a "newer than this build" store by stamping `user_version = 5`, which after this phase is *this* build. Retarget it to v6: rename to `..._stamped_v6_...`, stamp 6, and fix the intermediate `assert _user_version(db) == 4` at line 1729 to `== SCHEMA_VERSION`. The rule it pins is `> SCHEMA_VERSION` and was never the literal 5 — `test_a_future_version_store_is_refused` at 1126 already says so.

Then extend the ladder and failure-injection coverage to v5, in the shipped shapes:

- the v0/v2 whole-ladder test (`test_a_v0_and_a_v2_store_both_climb_the_whole_ladder_to_v4`, line 1573) asserts `_user_version(db) == SCHEMA_VERSION`; add `assert "repo" in _columns(db, "reviews")` to its loop and rename it to `..._to_v5`.
- add `_broken_v5_ladder(monkeypatch)` modelled on `_broken_v3_ladder` (line 1273) — the same delta with `"INSERT INTO no_such_table_boom (x) VALUES (1)"` injected after the last `ALTER TABLE` — and a `test_a_crash_mid_v5_delta_leaves_a_clean_v4_store_that_migrates_on_retry` modelled on the v3 drill (line 1295): the store must come back at v4 with no `repo` column and no `ix_reviews_repo_branch`, and migrate cleanly after `monkeypatch.undo()`.

- [ ] **Step 8: Full suite, then commit**

```bash
git add src/skodun/store.py tests/test_store.py
git commit -m "feat: store v5 adds the repo column and its index (refs #13)"
```

- [ ] **Mutations:**
  (a) drop the `CREATE INDEX` statement from `_MIGRATION_V5` → `test_a_v4_store_gains_the_repo_column_and_its_index` fails on `"ix_reviews_repo_branch" in idx`.
  (b) add `"UPDATE reviews SET repo='x'"` to the delta → the same test fails on `row["repo"] is None`.
  (c) drop `rec.get("repo")` from `_review_values` while leaving `"repo"` in `_REVIEW_COLUMNS` → *every* write raises `sqlite3.ProgrammingError`; the whole store suite goes red. Listed because it is the mistake the plan itself nearly shipped, and because "it crashes loudly everywhere" is the evidence that the two lists must be edited together.
  (d) convert `_MIGRATION_V5` from a tuple into a single `executescript` string (the non-transactional lane) → `test_no_non_transactional_delta_carries_a_non_idempotent_statement` (line 1458) fails on the `ALTER TABLE` check, and the new v5 crash drill fails because a half-applied delta is no longer rolled back.

---

### Task 2: Write the repo on all three paths, and scope the supersede

**Files:** Modify `src/skodun/store.py`, `src/skodun/pipeline.py`, `src/skodun/dispatch.py`; `tests/test_store.py`, `tests/test_dispatch.py`, `tests/test_delivery.py`.

**Interfaces:**
- Consumes: `reviews.repo`, `_REVIEW_COLUMNS` and `_review_values` from Task 1.
- Produces: `Store.reserve_prepush(..., repo: str, ...)` — a required keyword argument, so no caller can reserve a record without one; the foreground record dict AND the background worker's record dict both carry `repo`; both supersede statements in `reserve_prepush` filter `AND repo=?`.

`repo` is the string form of `gitio.git_common_dir(repo_path)`. Compute it once per call site and pass it down; do not re-derive it inside the store (the store must not shell out to git).

- [ ] **Step 1: Extend the reservation fixture, and its key-set assertion**

`tests/test_store.py`'s `_reserve` helper (line 2240) forwards `**kw` to `reserve_prepush`, so it needs a default for the new required argument. Give it one:

```python
def _reserve(st, **kw):
    args = dict(branch="b", head="h" * 40, base_ref="origin/main",
                base_sha="s" * 40, diff_hash="d" * 40, worst_runtime_sec=1234,
                evidence=_evidence(enabled=False), repo="/repos/a")
    args.update(kw)
    return st.reserve_prepush(**args)
```

Do the same to `tests/test_delivery.py`'s own `_reserve` (line 72), whose defaults are `worst_runtime_sec=99` and `evidence=_evidence()` — use the same `"/repos/a"` literal, which Task 3 promotes to a `REPO_A` constant in that file.

Both fixtures pass `evidence=_evidence(enabled=False)` / `_evidence()` with `enabled=False`, so `_suppression_candidate` returns `None` immediately (`store.py:927`) and repeated `_reserve` calls with identical content really do reserve rather than dedup. Step 2 depends on that.

`RESERVED_KEYS` (`tests/test_store.py:2222`) is an **exact** key-set assertion — `assert set(rec) == RESERVED_KEYS` at line 2269 — so putting `repo` on the reserved record breaks it until `"repo"` is added to the set. Add it beside `"superseded_by"`, and add `assert rec["repo"] == "/repos/a"` to `test_reserve_prepush_writes_the_documented_running_shape` so the value is pinned and not merely tolerated.

- [ ] **Step 2: Write the failing supersede test**

```python
def test_supersede_does_not_retire_another_repositorys_running_review(tmp_path):
    """The exact defect: two repositories, one store, the same branch name.
    Reserving in A must not touch B's running row, and must not return it for
    signalling -- returning it is what SIGTERMed an unrelated worker.

    BOTH repositories have a running row, and that is deliberate: with only B's
    row present the scoped SELECT returns nothing, `retired` is empty and the
    UPDATE is skipped entirely (`store.py:858`), so an UNSCOPED UPDATE would
    still leave B alone and the mutation that drops `repo=?` from it would
    survive. A's own row is what makes the UPDATE actually run.
    """
    with Store.open(tmp_path / "s.db") as st:
        in_b = _reserve(st, branch="main", repo="/repos/b")
        in_a = _reserve(st, branch="main", repo="/repos/a")

        newer = _reserve(st, branch="main", repo="/repos/a")

        assert newer.record_id is not None
        assert [r["id"] for r in newer.superseded] == [in_a.record_id], (
            "the supersede must return A's own row and nothing else")
        assert st.get_review(in_b.record_id)["status"] == "running"
        assert st.get_review(in_b.record_id)["superseded_by"] is None
        assert st.get_review(in_a.record_id)["status"] == "superseded"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q -k "another_repositorys_running"`
Expected: FAIL — `reserve_prepush()` got an unexpected keyword argument `repo`; once the parameter exists, `newer.superseded` contains B's id too and B's row reads `superseded`.

- [ ] **Step 4: Add the parameter and scope both statements**

In `reserve_prepush`, add a required keyword-only `repo: str` parameter after `evidence` (i.e. in the existing `*,` group, alongside `now` and `id_prefix`, but with no default), and change both supersede statements (`store.py:853` and `store.py:860`):

```python
            rows = self._c.execute(
                "SELECT id, pid FROM reviews"
                " WHERE repo=? AND branch=? AND mode=? AND status=?",
                (repo, branch, PREPUSH_MODE, RUNNING)).fetchall()
            retired = tuple({"id": r["id"], "pid": r["pid"]} for r in rows)
            if retired:
                self._c.execute(
                    """UPDATE reviews SET status='superseded', superseded_by=?,
                         artifact_json=json_set(artifact_json,
                           '$.status', 'superseded', '$.superseded_by', ?)
                       WHERE repo=? AND branch=? AND mode=? AND status=?""",
                    (record_id, record_id, repo, branch, PREPUSH_MODE, RUNNING))
```

and add `repo=repo,` to the reserved record dict a few lines below (beside `mode=PREPUSH_MODE`).

- [ ] **Step 5: Pass it from the dispatcher and the foreground**

In `src/skodun/dispatch.py:1011`:

```python
    reservation = store.reserve_prepush(
        ref.branch, ref.local_oid, base.ref, base.sha, diff_hash,
        reserved_budget(cfg, diff.data), evidence,
        repo=str(gitio.git_common_dir(repo)))
```

`dispatch` already imports `gitio` and holds `repo`.

In `src/skodun/pipeline.py:1240`, `run_review`'s `common` dict is built with `diff_hash=diff_hash, mode=mode, model=finder.model, ...`. Add, beside `worst_runtime_sec=...`:

```python
            repo=str(gitio.git_common_dir(repo)),
```

- [ ] **Step 6: Set it on the BACKGROUND worker's record — the one the draft plan missed**

**This is the second defect that would have broken the phase, and it is a spec correction too** (see the design spec's "Corrections to the approved spec", item 1). `store.finalize_review` (`store.py:1038-1045`) merges only `pid` and `superseded_by` back from the stored row and binds every other `_REVIEW_COLUMNS` value from the worker's dict — so a worker record with no `repo` writes `repo=NULL` over the value Step 4's reservation correctly persisted, at the exact moment the round becomes deliverable. Background rounds are the only kind `surface` delivers. Patch Steps 4–5 alone and the phase's headline fix is silently inert.

In `src/skodun/pipeline.py:1740`, `run_prepush_review`'s `common` already reads two database-owned values off `reserved`. `repo` joins them — taken from the reservation rather than recomputed, so the worker cannot disagree with the row it is finalizing:

```python
    common = dict(
        id=record_id, reviewed_at=reserved.get("reviewed_at") or _iso_now(),
        source="skodun", branch=branch, head=local_oid, base_ref=base.ref,
        base_sha=base.sha, diff_hash=diff_hash, mode="prepush",
        model=finder.model, adapter=adapter.name, timeout_seconds=d.timeout_sec,
        max_turns=d.max_turns,
        worst_runtime_sec=reserved.get("worst_runtime_sec"),
        pid=reserved.get("pid"),
        repo=reserved.get("repo"),
    )
```

- [ ] **Step 7: Pin it with a test that goes through `finalize_review`**

In `tests/test_store.py`, beside the other finalize tests. A test that only ever looks at *reserved* rows cannot see this failure:

```python
def test_a_finalized_record_keeps_the_reservations_repo(tmp_path):
    """`finalize_review` binds every column from the WORKER's dict and merges
    only `pid`/`superseded_by` back, so a worker record with no `repo` would
    write NULL over the reservation's value -- on the only rounds `surface`
    ever delivers."""
    with Store.open(tmp_path / "s.db") as st:
        res = _reserve(st, branch="main", repo="/repos/a")
        rec = dict(st.get_review(res.record_id), status="clean", parse_ok=True,
                   usable_output=True, summary="ok", findings=[],
                   findings_total=0)

        assert st.finalize_review(res.record_id, rec) is True

        assert st.get_review(res.record_id)["repo"] == "/repos/a"
        row = _raw_row(tmp_path / "s.db", res.record_id)
        assert row["repo"] == "/repos/a", "the INDEXED column, not just the JSON"
```

This test passes trivially the moment `_reserve` supplies `repo`, because `st.get_review` round-trips it through the artifact. That is fine and it is the point: it is the regression pin for Step 6, and the mutation below is what proves it bites.

- [ ] **Step 8: Fix the remaining `reserve_prepush` call sites**

Making the argument required breaks every existing caller, which is intended. The complete list, verified:

- source: `src/skodun/dispatch.py:1011` (Step 5)
- fixture helpers: `tests/test_store.py:2240` and `tests/test_delivery.py:72` (Step 1)
- direct test calls in `tests/test_dispatch.py`: **1367, 1411, 1507, 1710, 1909, 1932, 3124** — seven of them, each needing `repo=str(gitio.git_common_dir(repo))` for whatever repository the test already built, or a literal path where the test has none.

- [ ] **Step 9: Run the affected suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py tests/test_dispatch.py tests/test_pipeline.py tests/test_delivery.py tests/test_batched_review.py -q`
Expected: PASS. `tests/test_delivery.py` is in the list because its own `_reserve` is one of the two fixture helpers.

- [ ] **Step 10: Commit**

```bash
git add src/skodun/store.py src/skodun/pipeline.py src/skodun/dispatch.py tests/
git commit -m "feat: records carry their repository, and supersede is scoped to it (refs #13)"
```

- [ ] **Mutations:**
  (a) drop `repo=?` from the supersede `SELECT` only → `test_supersede_does_not_retire_another_repositorys_running_review` fails: `newer.superseded` returns B's row for signalling.
  (b) drop `repo=?` from the supersede `UPDATE` only → the same test fails on `st.get_review(in_b.record_id)["status"] == "running"`. It is killed **because both repositories have a running row**: with only B's, the scoped SELECT returns nothing, `retired` is empty and `if retired:` skips the UPDATE altogether, so an unscoped UPDATE would never execute and the mutation would survive.
  (c) give `repo` a default of `None` on `reserve_prepush` → add a one-line test asserting `st.reserve_prepush("b", "h" * 40, "origin/main", "s" * 40, "d" * 40, 1, _evidence(enabled=False))` raises `TypeError`, and watch it fail.
  (d) drop `repo=reserved.get("repo")` from `run_prepush_review`'s `common` → `test_a_finalized_record_keeps_the_reservations_repo` cannot see it (it calls `finalize_review` directly), so this mutation is killed in **Task 3** by the background-delivery test, and in Task 5 by the drill. Recorded here so the implementer knows it is deliberately deferred rather than unpinned — and if Task 3's test does not kill it, that is a plan defect to fix before Task 3 is committed.

---

### Task 3: Scope delivery and `log --branch`

**Files:** Modify `src/skodun/delivery.py`, `src/skodun/services.py`, `src/skodun/cli.py`, `src/skodun/mcpserver.py`, `src/skodun/store.py`; `tests/test_delivery.py`, `tests/test_cli.py`, `tests/test_services.py`, `tests/test_mcptools.py`.

**Interfaces:**
- Consumes: `reviews.repo` from Tasks 1–2.
- Produces: `delivery.undelivered(store, branch, repo)`; `delivery.surface(store, branch, repo, fmt=TEXT, include_delivered=False)`; `services.svc_surface(store, branch, repo, fmt="text", include_delivered=False)`; `services.svc_log(store, branch, limit, repo=None)`; `Store.list_reviews(branch, limit=30, repo=None)` — `repo` filters only when `branch` is not None; `skodun log --repo`; a `repo` property on the MCP `log` tool.

**Blast radius, read this before writing any code.** Task 2 warned about its callers; this task has a larger one, and it is not optional threading — once the query is scoped, every fixture record without a `repo` becomes invisible and the affected tests return `[]`.

- `tests/test_delivery.py`: **13** `delivery.undelivered`/`delivery.surface` call sites — lines 99, 107, 116, 124, 137, 144, 150, 157, 159, 377, 525, 901, 930 — plus the `_surface` helper at line 166. The `_rec` fixture (line 22) writes no `repo` at all, so *every* eligibility test in the file returns `[]` until it does.
- `tests/test_cli.py`: **20** `main(["surface", ...])` call sites, and most pass `--branch` from a cwd that is not a repository — which works today only because `resolve_surface_branch` short-circuits before any git call. Once `_cmd_surface` must also resolve a repository, those tests need a real repo and records stamped with it. The `_surface_db` fixture (line 2504) is the one place to add it.
- `tests/test_services.py`: **7** `svc_surface` call sites — lines 160, 199, 214, 225, 244, 255, 387 — each of which now needs the new required positional. The 6 `svc_log` sites (148, 149, 188, 278, 286, 386) all pass `branch=None` and `svc_log`'s new parameter is optional, so they compile unchanged; check that they still assert what they meant to. The `monkeypatch.setattr(store, "list_reviews", boom)` at line 431 must keep accepting whatever `svc_log` passes — `boom` is `def boom(*a, **kw)`, so verify rather than assume.
- **One fixture reaches three files.** `_round` (`tests/test_cli.py:2484`) is imported by `tests/test_services.py:33` and `tests/test_mcptools.py:49-50`, as is `_surface_db`. Stamping the repo there is the cheapest fix — but it cannot be a literal: the value has to be `git_common_dir` of a real repository the test built, because that is what the transport computes at run time. Give `_surface_db` a `repo` parameter that sets `rec["repo"]` on each record before saving, and let the tests pass the repo they already have.

`Store.list_reviews`'s new parameter is keyword-with-default, so the ~40 `list_reviews(branch, limit)` call sites across the suite are unaffected; only `svc_surface`'s signature is a hard break.

- [ ] **Step 1: Extend the delivery fixtures**

In `tests/test_delivery.py`, give `_rec` (line 22) a `repo` and `_surface` (line 166) a matching default, so the file has one definition of "this repository":

```python
REPO_A = "/repos/a"
REPO_B = "/repos/b"
```

Add `repo=REPO_A` to `_rec`'s dict (before `rec.update(kw)`), add `repo=REPO_A` to `_reserve`'s defaults if Task 2 left a different literal there, and rewrite `_surface`:

```python
def _surface(st, branch="b", repo=REPO_A, **kw):
    return delivery.surface(st, branch, repo, **kw)
```

Then update the 13 call sites to pass `REPO_A` positionally after the branch. They are mechanical — `delivery.undelivered(st, "b")` becomes `delivery.undelivered(st, "b", REPO_A)`.

- [ ] **Step 2: Write the failing delivery test**

```python
def test_surface_never_delivers_another_repositorys_rounds(tmp_path):
    """One store, two repositories, the same branch. Surfacing A must not
    render B's round -- and must not ACKNOWLEDGE it, which is what left the
    other repository's session with nothing to show."""
    with _store(tmp_path) as st:
        _save(st, id="sk_b", repo=REPO_B)

        assert delivery.undelivered(st, "b", REPO_A) == []

        status, text, pending = delivery.surface(st, "b", REPO_A)
        assert "sk_b" not in text
        assert pending == []
        assert _deliveries(tmp_path / "s.db") == [], (
            "another repository's round was acknowledged")

        # The REPLAY sibling is built from the same select and must be scoped
        # with it: `--include-delivered` reaching across repositories would
        # render B's history into A's session.
        assert delivery.surface(st, "b", REPO_A, include_delivered=True).text == ""
        assert _ids(delivery.undelivered(st, "b", REPO_B)) == ["sk_b"]
```

- [ ] **Step 3: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_delivery.py -q -k "another_repositorys_rounds"`
Expected: FAIL — `undelivered()` takes 2 positional arguments; once the parameter exists, `sk_b` comes back.

- [ ] **Step 4: Scope the delivery SQL**

In `src/skodun/delivery.py`, `_ROUNDS_SELECT` (line 131) ends with:

```sql
WHERE r.branch = ? AND r.mode = ? AND r.source = ?
  AND r.status IN (%s)
```

Change the first line to `WHERE r.repo = ? AND r.branch = ? AND r.mode = ? AND r.source = ?`. Both `_UNDELIVERED_SQL` and `_ALL_ROUNDS_SQL` are built from it, so both are scoped by the one edit — which is the reason they are built from one select. Then thread `repo` through the three functions, binding it **first** to match the new placeholder order:

```python
def _query(store: Store, branch: str, repo: str,
           include_delivered: bool) -> list[dict]:
    sql = _ALL_ROUNDS_SQL if include_delivered else _UNDELIVERED_SQL
    rows = _conn(store).execute(
        sql, (repo, branch, PREPUSH_MODE, SKODUN_SOURCE,
              *TERMINAL_STATUSES)).fetchall()
    return [_record(r) for r in rows]


def undelivered(store: Store, branch: str, repo: str) -> list[dict]:
```

and `def surface(store: Store, branch: str, repo: str, fmt: str = TEXT, include_delivered: bool = False) -> SurfaceResult:`, whose body calls `_query(store, branch, repo, include_delivered)`. Add to `undelivered`'s eligibility docstring, beside the `mode`/`source` clauses:

```text
    * `repo=<the git common dir>` -- two repositories sharing one store collide
      on any common branch name, and a `surface` that reached across them
      delivered AND permanently acknowledged the other's rounds. `repo IS NULL`
      (every pre-v5 row) matches nothing, deliberately.
```

- [ ] **Step 5: Scope `list_reviews`, for `--branch` only**

In `src/skodun/store.py:1336`:

```python
    def list_reviews(self, branch: str | None, limit: int = 30,
                     repo: str | None = None) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch is not None:
            # Scoped ONLY with a branch: a branch name is the ambiguous key.
            # `branch=None` is a human's "show me everything" and stays
            # unscoped across repositories -- so a `repo` handed in without a
            # branch is ignored, which is this method's published contract and
            # what `log --repo`'s help text says.
            q += " WHERE branch=?"
            args = (branch,)
            if repo is not None:
                q += " AND repo=?"
                args += (repo,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
        rows = self._c.execute(q, args + (limit,)).fetchall()
        return [json.loads(r["artifact_json"]) for r in rows]
```

Pin both halves of that contract in `tests/test_store.py`, beside `test_list_reviews_orders_and_limits` (line 215):

```python
def test_list_reviews_scopes_by_repo_only_when_a_branch_is_given(tmp_path):
    """`--branch` is the ambiguous key and the only thing `repo` narrows. An
    unscoped listing is a human's "show me everything" and must keep crossing
    repositories -- including the pre-v5 rows no scoped query can reach."""
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(dict(REC, id="in_a", branch="main", repo="/repos/a"))
        st.save_review(dict(REC, id="in_b", branch="main", repo="/repos/b"))
        st.save_review(dict(REC, id="pre_v5", branch="main"))

        assert [r["id"] for r in st.list_reviews("main", 30, "/repos/a")] == ["in_a"]
        assert sorted(r["id"] for r in st.list_reviews(None, 30, "/repos/a")) == [
            "in_a", "in_b", "pre_v5"], "an unscoped listing must not be filtered"
        assert st.list_reviews("main", 30, "/repos/nowhere") == []
```

- [ ] **Step 6: Thread it through the services**

```python
def svc_log(store, branch, limit, repo=None) -> tuple[int, str]:
```

with the store call becoming `rows = store.list_reviews(branch, rows_wanted, repo)`. Add to the docstring: `repo` narrows `branch` and is ignored without one (`list_reviews`'s contract).

```python
def svc_surface(store, branch, repo, fmt="text",
                include_delivered=False) -> tuple[int, str, list]:
```

with `delivery.surface(store, branch, repo, fmt, bool(include_delivered))`. `repo` is positional and required here, unlike `svc_log`'s: a `surface` that guessed the repository would deliver and permanently acknowledge rounds, and there is no safe default for that.

Then fix the callers listed in the blast radius: the 7 `svc_surface` sites in `tests/test_services.py` (160, 199, 214, 225, 244, 255, 387) gain the positional repo, their `_db(...)` records gain a matching one, and the 6 `svc_log` sites compile unchanged.

- [ ] **Step 7: `_cmd_surface` uses `args.repo`, never the cwd**

In `src/skodun/cli.py`, widen the lazy import to `from . import delivery, gitio, services`, and after the existing branch resolution add:

```python
    # The repo the ROWS are scoped by, resolved from the SAME argument
    # `resolve_surface_branch` just used. NEVER `Path(".")`: with a hardcoded
    # cwd, `skodun surface --repo /other` would deliver AND permanently
    # acknowledge the CWD repository's rounds -- a fresh instance of the defect
    # this phase closes. A repo git cannot read is a refusal, exactly as it
    # already is for the branch.
    try:
        repo = str(gitio.git_common_dir(
            args.repo if args.repo is not None else Path(".")))
    except BaseException as e:
        return _warn(f"skodun surface: could not resolve the repository to "
                     f"report on: {e!r}", 2)
```

and pass it: `services.svc_surface(store, branch, repo, fmt, bool(args.include_delivered))`.

Note the consequence, and do not paper over it: `surface --branch X` from a directory that is not a repository used to work and now exits 2. That is correct — the rows are repo-scoped and there is no repository to scope them to — but it is why most of `tests/test_cli.py`'s 20 surface call sites need a real repo. Add a `repo` parameter to `_surface_db` (line 2504) that stamps every record, and `monkeypatch.chdir(repo)` in the tests that do not already pass `--repo`.

- [ ] **Step 8: `log` gains `--repo`, and `_cmd_log` resolves it LAZILY**

The parity decision is the spec's ("Reaching the scope from a transport"): `log` gains `--repo` on both surfaces rather than MCP `log` being left unscoped, because a scope the CLI cannot aim is a scope the user cannot inspect. In `build_parser`, beside the existing `log` arguments (`cli.py:147-151`):

```python
    log.add_argument("--repo", type=Path, default=None,
                     help="narrow --branch to one repository (default: the "
                          "current directory); ignored without --branch")
```

In `_cmd_log` (`cli.py:1221-1231`) — and the laziness is the whole point, because `gitio._out` calls `_run`, which raises `GitError` outside a repository (`gitio.py:54-60`), while `_cmd_log` guards only `Store.open` and `skodun log` has always been runnable from anywhere:

```python
    from .services import svc_log

    repo = None
    if args.branch is not None:
        # ONLY with a branch, and wrapped. `git_common_dir` shells out to git
        # and raises outside a repository -- and `skodun log` with no branch
        # running from anywhere is this command's contract, not an accident.
        from . import gitio
        try:
            repo = str(gitio.git_common_dir(
                args.repo if args.repo is not None else Path(".")))
        except BaseException as e:
            return _emit(f"skodun log: could not resolve the repository for "
                         f"--branch: {e!r}", 2)
    try:
        from .store import Store
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun log: could not read the store: {e!r}", 2)
    with store:
        code, text = svc_log(store, args.branch, args.limit, repo)
    return _emit(text, code) if text else code
```

Pin the laziness, because it is the thing that is easy to lose:

```python
def test_log_without_a_branch_still_runs_outside_a_repository(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """`--repo` is resolved only for `--branch`. An unscoped `log` never shells
    out to git, and exiting 1 with a GitError traceback from a directory that
    is not a repository is not in this command's contract."""
    db = tmp_path / "s.db"
    Store.open(db).close()
    monkeypatch.setenv("SKODUN_DB", str(db))
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert main(["log"]) == 0
    assert capsys.readouterr().err == ""
```

- [ ] **Step 9: Both MCP handlers convert their repo argument**

`_repo_arg` (`mcpserver.py:281-303`) returns `Path | None` — a **checkout path**, which is what `resolve_surface_branch` wants and is *not* what the column stores. "`surface` already has it, add nothing" is false: the value needs converting, and the conversion can fail.

In `_handle_surface` (`mcpserver.py:485`), after the branch resolution:

```python
    try:
        scope = str(gitio.git_common_dir(repo))
    except Exception as e:
        return HandlerResult(
            status=2,
            text=f"skodun surface: could not resolve the repository to report "
                 f"on: {e!r}")
    with call.store_factory() as store:
        status, text, pending = services.svc_surface(
            store, branch, scope, fmt, include_delivered)
```

with `gitio` added to the handler's local import.

In `_handle_log`, the same shape, and lazily for the same reason as the CLI:

```python
    repo_path, refusal = _repo_arg(call.params, "log")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    branch = branch if isinstance(branch, str) and branch else None
    scope = None
    if branch is not None:
        try:
            scope = str(gitio.git_common_dir(repo_path))
        except Exception as e:
            return HandlerResult(
                status=2,
                text=f"skodun log: could not resolve the repository for "
                     f"branch: {e!r}")
    with call.store_factory() as store:
        status, text = services.svc_log(store, branch, limit, scope)
```

and add the shipped `_REPO_PROPERTY` to the `log` tool's `input_schema` (`mcpserver.py:651`):

```python
            input_schema=_schema({
                **_REPO_PROPERTY,
                "branch": {"type": "string",
                           "description": "restrict to one branch; defaults to "
                                          "every branch"},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "maximum rows, newest first "
                                         "(default 20)"},
            }),
```

**No snapshot needs updating.** `EXPECTED_TOOLS` (`tests/test_mcptools.py:59`, asserted at line 160) pins tool *names and order* only; an added property does not touch it. `test_the_required_arguments_are_the_ones_without_a_default` (line 199) asserts `required["log"] == set()`, and `repo` is optional, so it stays true. The two tests the new property must satisfy are already there and generic: the schema-shape loop at line 187 requires every property to carry a `type` and a non-empty `description` — which is why the edit reuses `_REPO_PROPERTY` rather than inlining a new dict.

Add the parity test beside the shipped `test_surface_repo_defaults_to_none_and_the_mcp_tool_takes_the_same_argument` (`tests/test_cli.py:2769`), in its shape:

```python
def test_log_repo_defaults_to_none_and_the_mcp_tool_takes_the_same_argument():
    """Neither surface may grow a repo argument the other lacks: a `log` that
    is repo-scoped on one and global on the other makes "whose history is
    this" depend on which client you asked."""
    import argparse

    from skodun.cli import build_parser
    from skodun.mcpserver import default_registry

    subs = [a for a in build_parser()._actions
            if isinstance(a, argparse._SubParsersAction)]
    repos = [a for a in subs[0].choices["log"]._actions if a.dest == "repo"]
    assert len(repos) == 1, repos
    assert repos[0].default is None
    assert repos[0].type is Path

    tool = [t for t in default_registry() if t.name == "log"]
    assert len(tool) == 1, tool
    assert "repo" in tool[0].input_schema["properties"]
```

- [ ] **Step 10: Run the affected suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_delivery.py tests/test_cli.py tests/test_mcptools.py tests/test_mcpserver.py tests/test_services.py tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/skodun/delivery.py src/skodun/services.py src/skodun/cli.py \
        src/skodun/mcpserver.py src/skodun/store.py tests/
git commit -m "feat: surface and log --branch are scoped to their repository (refs #13)"
```

- [ ] **Mutations:**
  (a) drop the `r.repo = ?` clause from `_ROUNDS_SELECT` **and** its bind from `_query` → `test_surface_never_delivers_another_repositorys_rounds` fails: `sk_b` comes back. (Dropping only one of the two is a `ProgrammingError`, which is a crash rather than a mutation; drop both.)
  (b) move `list_reviews`'s `if repo is not None:` block out of the `if branch is not None:` block, so an unscoped listing is filtered too → `test_list_reviews_scopes_by_repo_only_when_a_branch_is_given` fails on the `list_reviews(None, 30, "/repos/a")` assertion.
  (c) make `_cmd_surface` compute `str(gitio.git_common_dir(Path(".")))` instead of using `args.repo` → add a CLI test in which the cwd is repository A with its own undelivered round and the command is `surface --repo <B>`; the mutation delivers and acknowledges A's round, so the test's "A's round is still undelivered" assertion fails. This replaces the draft plan's "acknowledge before filtering" mutation, which is not expressible: the predicate is in SQL and `_acknowledge_quiet` already runs after `_query` (`delivery.py:626`), so there is no ordering to invert.
  (d) drop `repo=reserved.get("repo")` from `run_prepush_review`'s `common` (Task 2, Step 6) → a background round finalized by a worker lands `repo=NULL` and `surface` for its own repository returns nothing. Killed by an end-to-end test in this task: reserve in A, finalize through `store.finalize_review`, then assert `delivery.undelivered(st, branch, REPO_A)` returns that round. **Run this mutation explicitly** — it is the one that would have shipped the phase inert.

---

### Task 4: The stale-recovery scan stops decoding artifacts

**Files:** Modify `src/skodun/store.py`, `src/skodun/pipeline.py`; `tests/test_store.py`, `tests/test_pipeline.py`.

**Interfaces:**
- Produces: `Store.running_records() -> list[dict]` — every row with `status='running'`, as dicts of the INDEXED columns only: `id`, `reviewed_at`, `worst_runtime_sec`. No artifact is read or decoded.
- Consumes: nothing from Tasks 1–3. **Unscoped by repository**, deliberately: a stale row is stale whichever repository recorded it, and the pre-v5 rows are unreachable to every scoped query.

- [ ] **Step 1: Write the failing test — through `recover_stale`, not around it**

The draft plan's version called `running_records()` directly, which pins the query but not the wiring: reverting `recover_stale` to `list_reviews` would have left it green and acceptance criterion 4 pinned by nothing. The corrupt artifact is only evidence if the thing under test is the sweep. In `tests/test_pipeline.py`, beside the other `recover_stale` tests:

```python
def test_recover_stale_decodes_no_artifacts(tmp_path):
    """The sweep runs on the synchronous `git push` path and used to decode
    EVERY stored artifact to read a status that is an indexed column. The
    unparseable artifact is the proof: `list_reviews` would raise on it before
    the loop body ever ran.

    The corrupt row is deliberately FRESH, so the sweep reaches its age check
    and stops -- `fail_if_running` writes through `json_set`, which would
    itself refuse malformed JSON, and this test is about the read path.
    """
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)
    st = _store(tmp_path)
    _running(st, "sk_corrupt", 1)
    _running(st, "sk_old", 600)
    st._c.execute("UPDATE reviews SET artifact_json='{not json' "
                  "WHERE id='sk_corrupt'")

    assert pipeline.recover_stale(st, cfg) == 1

    assert st.get_review("sk_old")["status"] == "failed"
    assert st._c.execute(
        "SELECT status FROM reviews WHERE id='sk_corrupt'"
    ).fetchone()["status"] == "running"
```

Add the query's own unit test in `tests/test_store.py`, which pins the shape the sweep depends on:

```python
def test_running_records_returns_the_indexed_columns_and_only_running_rows(
        tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(dict(REC, id="done", status="clean"))
        res = _reserve(st, branch="main", repo="/repos/a")
        st.save_review(dict(REC, id="legacy", status="running", parse_ok=False,
                            trustworthy=False))

        rows = st.running_records()

        assert sorted(r["id"] for r in rows) == ["legacy", res.record_id]
        assert set(rows[0]) == {"id", "reviewed_at", "worst_runtime_sec"}
        by_id = {r["id"]: r for r in rows}
        assert by_id[res.record_id]["worst_runtime_sec"] == 1234
        assert by_id["legacy"]["worst_runtime_sec"] is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py tests/test_pipeline.py -q -k "running_records or decodes_no_artifacts"`
Expected: FAIL — `Store` has no attribute `running_records`, and `recover_stale` raises `json.JSONDecodeError` on the corrupt row.

- [ ] **Step 3: Add the query**

In `src/skodun/store.py`, beside `list_reviews`:

```python
    def running_records(self) -> list[dict]:
        """Every `running` row, as the INDEXED columns the stale sweep reads.

        `list_reviews` decodes `artifact_json` for every row it returns, and
        `recover_stale` called it with no branch on every push -- so the sweep
        decoded every artifact ever stored to read a status that is an indexed
        column, on the synchronous `git push` path. This reads three columns
        and decodes nothing.

        UNORDERED, unlike `list_reviews`: the sweep judges every row it is
        given, independently, and never stops early, so `ORDER BY reviewed_at
        DESC` bought it nothing. The ordering exists for the DISPLAY callers
        and stays on `list_reviews` for them.

        Deliberately UNSCOPED by repository: a stale row is stale whichever
        repository recorded it, and scoping the sweep would strand the pre-v5
        rows that `repo IS NULL` already hides from every scoped query.
        """
        rows = self._c.execute(
            "SELECT id, reviewed_at, worst_runtime_sec FROM reviews"
            " WHERE status=?", (RUNNING,)).fetchall()
        return [{"id": r["id"], "reviewed_at": r["reviewed_at"],
                 "worst_runtime_sec": r["worst_runtime_sec"]} for r in rows]
```

- [ ] **Step 4: Point `recover_stale` at it, and retire `_SCAN_ALL`**

In `src/skodun/pipeline.py:418`, replace

```python
    for rec in store.list_reviews(None, _SCAN_ALL):
        if not isinstance(rec, dict) or rec.get("status") != "running":
            continue
        rid = rec.get("id")
```

with

```python
    for rec in store.running_records():
        rid = rec.get("id")
```

**Both existing properties must survive unchanged**: `_record_budget(rec)` still prefers the record's persisted `worst_runtime_sec` over the computed ceiling, and a record whose `reviewed_at` will not parse is still left alone by the `started is None` guard.

Then delete `_SCAN_ALL` and its comment block (`pipeline.py:201-206`). It has no other caller, and its comment — "the query orders by `reviewed_at DESC`, so the stale records it exists to clean are the last ones it would reach" — describes a query the sweep no longer runs. Leaving it is dead code carrying a false explanation. `tests/test_pipeline.py:591`'s `test_recover_stale_scans_past_the_newest_reviews` keeps passing (there is now no limit at all to scan past) but its docstring says the same false thing; rewrite it to say the sweep is unbounded and unordered.

- [ ] **Step 5: Run the pipeline and store suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py tests/test_pipeline.py tests/test_dispatch.py tests/test_batched_review.py tests/test_fallback.py -q`
Expected: PASS, including the shipped tests that pin the persisted-budget preference (`tests/test_batched_review.py:1052`) and the unparseable-timestamp rule (`tests/test_pipeline.py:604`).

- [ ] **Step 6: Full suite in the background, then commit**

```bash
git add src/skodun/store.py src/skodun/pipeline.py tests/
git commit -m "perf: the stale sweep reads indexed columns, not every artifact (refs #13)"
```

- [ ] **Mutations:**
  (a) drop `worst_runtime_sec` from `running_records`'s SELECT **and** from the dicts it builds → `_record_budget` sees nothing, every batched row is judged at the single-review ceiling, and `test_recover_stale_prefers_the_budget_the_record_persisted` (`tests/test_batched_review.py:1052`) fails by sweeping `sk_batched`.
  (b) add `AND repo IS NOT NULL` to `running_records`'s WHERE — the plausible mistake, given the NULL rule two tasks away → `test_recover_stale_fails_old_running_records_and_leaves_fresh_ones` (`tests/test_pipeline.py:579`) fails, because its `_running` helper writes no repo and `sk_old` is never swept. This replaces the draft's "scope `running_records` by repo", which was not expressible: the method takes no repo argument to scope by.
  (c) revert `recover_stale` to `store.list_reviews(None, -1)` with its status filter → `test_recover_stale_decodes_no_artifacts` fails, because `list_reviews` decodes every row in its return comprehension and raises on `sk_corrupt` before the sweep's loop body runs. This is the mutation the draft plan could not kill, because its test called `running_records()` and never reached the wiring.

---

### Task 5: The two-repository drill, and the docs

**Files:** Create `tests/test_repo_scoping.py`; Modify `README.md`.

The phase's headline test: not a unit seam, but the whole defect, end to end, in one executable drill.

- [ ] **Step 1: Write the drill**

Two real git repositories in `tmp_path`, one store, the same branch name in both, a running prepush review in each. Then, driving the real entry points (`dispatch.run_dispatch` for A, `delivery.surface` for A, `cli.main(["log", "--branch", ...])` for A), assert every one of these:

```python
def test_two_repositories_sharing_one_store_do_not_collide(tmp_path):
    # ... build repo_a, repo_b, one SKODUN_DB, a running prepush row in each ...

    # 1. A push in A does not retire B's row, and does not return it for
    #    signalling (returning it is what SIGTERMed an unrelated worker).
    # 2. `surface` for A renders neither B's round nor acknowledges it.
    # 3. `log --branch main` in A lists only A's rows.
    # 4. The GATE still matches by content across both -- it is deliberately
    #    NOT repo-scoped, and this asserts that decision rather than leaving
    #    it to be broken silently later.
    # 5. A pre-v5 row (repo IS NULL) is invisible to 1-3 and still visible to
    #    the gate and to unscoped `log`.
    # 6. A round FINALIZED by the worker path -- not merely reserved -- still
    #    carries A's repo and is still invisible to B's `surface`. Without
    #    this, 1-3 all pass against a store whose deliverable rounds are every
    #    one of them NULL (design spec, correction 1).
```

Assertion 6 is not optional decoration: the drill without it is exactly the drill that would have certified a broken phase. Build it by reserving through `run_dispatch` (or `store.reserve_prepush`) and then finalizing through `store.finalize_review` with a record built the way `run_prepush_review` builds one.

- [ ] **Step 2: Run it**

Run: `SKODUN_ORACLE_DIR=<oracle> PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_repo_scoping.py -q`
Expected: PASS on the finished Tasks 1–4.

- [ ] **Step 3: Update the README — the main section first**

The text Phase 4 invalidates is not primarily in Known limitations. It is **§"One store per repository (required if you use skodun in more than one)"** at `README.md:367-379`, which states the defect as *current behaviour* and tells the reader to work around it with one store per repository. That is the passage that becomes false, and leaving it while editing only the limitations list would leave the README's loudest statement about this contradicting the code.

Rewrite it to say what is now true: the store carries a `repo` column, rounds are scoped to the repository that recorded them, sharing one store across repositories is supported — and the one remaining caveat, which is the NULL rule: **rows recorded before the upgrade have no repository and are never delivered by `surface`** (they stay readable by unscoped `log`, and the gate still matches them by content). Keep the paragraph noting the gate was never affected either way; it was true then and is true now.

Then the secondary edits in Known limitations:

- `README.md:641` — remove the "Background rounds have no repo dimension" entry.
- `README.md:648` — remove the "Stale-review recovery JSON-parses the whole `reviews` table on every push" entry.

Both are now fixed rather than limitations, and the surviving user-visible consequence lives in the rewritten §"One store per repository", not here.

- [ ] **Step 4: Full suite both modes, then commit**

Run with and without `SKODUN_ORACLE_DIR`; reconcile the counts.

```bash
git add tests/test_repo_scoping.py README.md
git commit -m "test: the two-repository drill, and the docs it retires (refs #13)"
```

- [ ] **Mutations:**
  (a) remove the repo predicate from any ONE of the three scoped queries — the supersede `SELECT`, `_ROUNDS_SELECT`, `list_reviews` — one at a time → the drill must fail, and its failure must name which assertion (1, 2 or 3) broke. Three separate runs, not one.
  (b) scope the gate by repository → **not by editing `gate.py`**, which the byte pin forbids and which would be killed by `tests/test_seams.py` rather than by the drill. The gate's lookup lives in the store: add `AND repo=?` to `Store.latest_trustworthy_for` (`store.py:716-721`) and thread a repo into it. Drill assertion 4 must fail. This is what makes "the gate is deliberately content-addressed" a pinned decision rather than a paragraph.
  (c) drop assertion 6's finalize and assert against the reserved row instead → the drill passes with Task 2 Step 6 reverted, which is the failure this drill exists to catch. Run it as a two-step mutation: revert Step 6, confirm the drill goes red; restore Step 6, weaken assertion 6, confirm the drill goes green with the bug present. The second half is the one that matters.

---

## Self-Review Notes

- **Spec coverage:** §1 identity → T2 (one expression, all three paths); §1 migration → T1; §1 NULL rule → T1 (no backfill) + T3 (`list_reviews` scoping test) + T5 (assertion 5); §1 three write sites → T2 Steps 5–7; §1 three read queries → T2 (supersede) and T3 (delivery, `list_reviews`); §1 reaching the scope from a transport → T3 Steps 7–9; §1 indexing → T1; §1 not-scoped → T4 (sweep) and T5 assertion 4 (gate), with dedup and triage untouched by any task; §2 scan → T4; Testing section → T1/T3/T4/T5; acceptance criteria 1–7 → T5 plus the standing byte pin, with criterion 6 (finalized background rounds) landing in T2 Step 7, T3 mutation (d) and T5 assertion 6.
- **Deliberate decisions restated:** `repo` is a required keyword argument on `reserve_prepush`, so no caller can reserve without one; `finalize_review` does NOT merge `repo` as database-owned, because a value pinned by two mechanisms is a value with one unkillable mutation (design spec); `list_reviews` scopes only when a branch is given; `log` gains `--repo` on both surfaces rather than MCP `log` staying unscoped; the stale sweep stays unscoped and unordered; the gate stays content-addressed and byte-identical into a fourth phase.
- **What this plan learned from its own review.** Two defects would have broken the phase rather than a task — `_review_values` not being derived from `_REVIEW_COLUMNS` (T1 Step 5), and the background worker's record being a third write site (T2 Step 6) — and both were invisible from the documents alone. The rule that follows: a plan's code block is a claim about the shipped source, and every claim in this one has been read back against it. Where a task changes a function's signature, its blast radius is enumerated by line, not gestured at.
- **Blast radii, by task.** T2 breaks 10 `reserve_prepush` call sites (1 source, 7 in `test_dispatch.py`, 2 fixture helpers) and the `RESERVED_KEYS` exact-key assertion. T3 is larger: 13 delivery call sites plus a helper, the `_rec` fixture that makes them all return `[]`, 20 CLI surface call sites that now need a real repository, 7 `svc_surface` call sites, and the `_round`/`_surface_db` fixtures that `test_services.py` and `test_mcptools.py` both import. T4 breaks nothing but retires `_SCAN_ALL` and two stale docstrings; T5 breaks nothing.
- **Mutation audit.** Every mutation in this plan was re-checked with the suspicion the review applied to T2(b) and T4(c), and four were replaced because nothing killed them: T2(b) needed a running row in *both* repositories before the scoped UPDATE ever executes; T3(c) named an ordering that does not exist (the predicate is SQL; `_acknowledge_quiet` already runs after `_query`); T4(b) named a parameter `running_records` does not have; T4(c) tested the query instead of the wiring. T5(b) was rewritten because the obvious way to perform it — editing `gate.py` — is forbidden by the byte pin and would be killed by the wrong test.

## Deviations recorded at implementation time

(None yet. When the shipped source contradicts this plan, the implementer amends this section in the same commit — the Phase 2/3 pattern.)
