# skodun Phase 4 Implementation Plan — Repository Scoping and the Stale-Recovery Scan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two repositories sharing one store stop colliding on branch names, and `recover_stale` stops decoding every stored artifact on the `git push` path.

**Architecture:** Fixed by `docs/superpowers/specs/2026-07-31-skodun-phase4-design.md` (owner-approved) — read it first; this plan implements it and does not re-litigate it. A `repo TEXT` column on `reviews` (store v5), written by both persistence paths, read by exactly three queries. `NULL` matches nothing. The gate, dedup, and triage are deliberately NOT scoped.

**Tech Stack:** unchanged — Python ≥ 3.12, stdlib-only runtime, pytest the only dev dependency.

## Global Constraints

- Everything in the Phase 1–3 Global Constraints still binds.
- **`gate.py` and `trust.py` stay byte-identical**, now across four phases, pinned by the sha256 constants already in `tests/test_seams.py`:
  `gate.py` = `62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f`,
  `trust.py` = `8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1`.
  No task modifies either file. If a task appears to need to, stop and surface it.
- **The v5 migration is installed COMPLETELY and atomically in Task 1.** The ladder runs a delta only while `user_version < target`, so later tasks may only *consume* v5 state, never extend it. Any DDL discovered missing later is a plan defect: stop, amend Task 1, re-migrate test DBs.
- Python ≥ 3.12, stdlib-only runtime, pytest only.
- Committed code fully generic: no machine paths. The oracle is reachable only via `SKODUN_ORACLE_DIR`; subagents do NOT inherit it — pass it explicitly and always report ran-vs-skipped counts.
- Tests never touch `~/.local/share/skodun/skodun.db` or `~/.grok`: pin `SKODUN_DB`, `GIT_CONFIG_GLOBAL`, and every `SKODUN_<X>_BIN` to tmp paths.
- **Method requirement, binding on every task:** between-task review is by **execution and mutation**, never inspection. Every task below lists named **Mutations**; each must be killed by a test. `PYTHONDONTWRITEBYTECODE=1` is mandatory during mutation runs. Commit before mutating — the revert step takes an uncommitted fix with it.
- Full suite ≈ 11–13 min: run it as a background command and poll its output file; never a monitor. Baseline at plan start: **3062 passed, 1 skipped** with the oracle.
- Commit per task (`refs #13`), push periodically.
- The model CLIs may be unavailable (codex credits, xai 402). The suite drives fake CLIs and needs none of them.

## File Structure

```
src/skodun/
├── store.py       # modified (T1, T2, T4): v5 delta, repo column, scoped supersede,
│                  #   list_reviews scoping, running_records() for the sweep
├── pipeline.py    # modified (T2, T4): repo on the foreground record; recover_stale
│                  #   reads indexed columns instead of artifacts
├── dispatch.py    # modified (T2): repo passed into reserve_prepush
├── delivery.py    # modified (T3): repo in the undelivered/replay query
├── cli.py         # modified (T3): surface/log pass the repo
└── services.py    # modified (T3): svc_surface/svc_log take the repo through
tests/  (new: tests/test_repo_scoping.py — the two-repository drill)
```

---

### Task 1: Store v5 — the `repo` column and the widened index

**Files:** Modify `src/skodun/store.py`; `tests/test_store.py`.

**Interfaces:**
- Produces: `SCHEMA_VERSION = 5`; `_MIGRATION_V5: tuple[str, ...]`; `reviews.repo TEXT` (NULL on every pre-v5 row); index `ix_reviews_repo_branch` on `(repo, branch, reviewed_at)`; `"repo"` appended to `_REVIEW_COLUMNS`.
- Consumes: the v4 ladder shape in `_MIGRATIONS` and `_apply_atomic`.

Mirror the v3/v4 delta discipline exactly: a tuple of statements applied in ONE `BEGIN IMMEDIATE` with the version stamp, because `ALTER TABLE ADD COLUMN` is not replay-idempotent.

- [ ] **Step 1: Write the failing migration test**

```python
def test_v4_store_gains_the_repo_column_and_its_index(tmp_path):
    """v5 is additive: the column arrives NULL on every existing row and the
    old index is kept, not replaced."""
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review(_clean_record(id="sk_old", branch="main"))
        st._c.execute("PRAGMA user_version = 4")
        st._c.commit()

    with Store.open(db) as st:
        assert st._c.execute("PRAGMA user_version").fetchone()[0] == 5
        cols = {r["name"] for r in st._c.execute("PRAGMA table_info(reviews)")}
        assert "repo" in cols
        row = st._c.execute(
            "SELECT repo FROM reviews WHERE id='sk_old'").fetchone()
        assert row["repo"] is None, "a pre-v5 row must not be backfilled"
        idx = {r["name"] for r in st._c.execute("PRAGMA index_list(reviews)")}
        assert "ix_reviews_repo_branch" in idx
        assert "ix_reviews_branch" in idx, "the shipped index is kept, not dropped"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q -k "gains_the_repo_column"`
Expected: FAIL — `user_version` is 4, `repo` not in `cols`.

- [ ] **Step 3: Add the v5 delta**

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

- [ ] **Step 4: Add `repo` to the persisted columns**

In `_REVIEW_COLUMNS`, append `"repo"` after `"superseded_by"`. This is what makes both write paths persist it: every record dict already flows through `_normalize_record` and `_review_values`, so a record carrying `repo` is written to the indexed column and serialized into the artifact from the same dict.

- [ ] **Step 5: Run the test and the rest of the store suite**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 6: Extend the ladder and failure-injection tests to v5**

Find the existing v0/v2/v3/v4 ladder tests and the v4 mid-delta failure-injection test in `tests/test_store.py`; extend each to cover v5 in the same shape. The injection test must assert the store reopens cleanly at v4 and migrates on retry.

- [ ] **Step 7: Full suite, then commit**

```bash
git add src/skodun/store.py tests/test_store.py
git commit -m "feat: store v5 adds the repo column and its index (refs #13)"
```

- [ ] **Mutations:** (a) drop the `CREATE INDEX` from the delta → the index assertion fails; (b) backfill the column in the delta (`UPDATE reviews SET repo='x'`) → the `repo IS NULL` assertion fails; (c) put the two statements outside the transaction (apply them non-atomically) → the mid-delta injection test must fail.

---

### Task 2: Write the repo, and scope the supersede

**Files:** Modify `src/skodun/store.py`, `src/skodun/pipeline.py`, `src/skodun/dispatch.py`; `tests/test_store.py`, `tests/test_dispatch.py`.

**Interfaces:**
- Consumes: `reviews.repo` and `_REVIEW_COLUMNS` from Task 1.
- Produces: `Store.reserve_prepush(..., repo: str, ...)` — a required keyword argument, so no caller can reserve a record without one; the foreground record dict carries `repo`; both supersede statements in `reserve_prepush` filter `AND repo=?`.

`repo` is the string form of `gitio.git_common_dir(repo_path)`. Compute it once per call site and pass it down; do not re-derive it inside the store (the store must not shell out to git).

- [ ] **Step 1: Write the failing supersede test**

```python
def test_supersede_does_not_retire_another_repositorys_running_review(tmp_path):
    """The exact defect: two repositories, one store, the same branch name.
    Reserving in A must not touch B's running row, and must not return it for
    signalling -- returning it is what SIGTERMed an unrelated worker."""
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(_running_prepush(id="sk_b", branch="main", repo="/repos/b"))
        res = st.reserve_prepush(
            "main", "head1", "origin/main", "base1", "hash1", 100,
            _no_dedup_evidence(), repo="/repos/a")

        assert res.record_id is not None
        assert res.superseded == (), "another repository's row was retired"
        other = st.get_review("sk_b")
        assert other["status"] == "running"
        assert other["superseded_by"] is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q -k "another_repositorys_running"`
Expected: FAIL — `reserve_prepush()` has no `repo` argument (TypeError), and once added, `res.superseded` contains `sk_b`.

- [ ] **Step 3: Add the parameter and scope both statements**

In `reserve_prepush`, add a required keyword-only `repo: str` parameter, and change both supersede statements (around `store.py:853` and `store.py:860`):

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

and add `repo=repo` to the reserved record dict built a few lines below (beside `mode=PREPUSH_MODE`).

- [ ] **Step 4: Pass it from the dispatcher**

In `src/skodun/dispatch.py`, where `store.reserve_prepush(...)` is called, pass `repo=str(gitio.git_common_dir(repo))`. `dispatch` already imports `gitio` and holds the repository path.

- [ ] **Step 5: Set it on the foreground record**

In `src/skodun/pipeline.py` around line 1243, the foreground record dict is built with `diff_hash=diff_hash, mode=mode, model=finder.model, ...`. Add `repo=str(gitio.git_common_dir(repo))` to it. Use the same expression, so both paths write the same value for the same checkout.

- [ ] **Step 6: Run the store and dispatch suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py tests/test_dispatch.py tests/test_pipeline.py -q`
Expected: PASS. Existing `reserve_prepush` callers in tests will need the new argument — that is expected and is the point of making it required.

- [ ] **Step 7: Commit**

```bash
git add src/skodun/store.py src/skodun/pipeline.py src/skodun/dispatch.py tests/
git commit -m "feat: records carry their repository, and supersede is scoped to it (refs #13)"
```

- [ ] **Mutations:** (a) drop `repo=?` from the `SELECT` only → the supersede test fails (the row is returned for signalling); (b) drop it from the `UPDATE` only → the test's `other["status"] == "running"` assertion fails; (c) make `repo` optional with a default of `None` → add a test asserting a reservation without a repo raises, and watch it fail.

---

### Task 3: Scope delivery and `log --branch`

**Files:** Modify `src/skodun/delivery.py`, `src/skodun/services.py`, `src/skodun/cli.py`, `src/skodun/store.py`; `tests/test_delivery.py`, `tests/test_cli.py`.

**Interfaces:**
- Consumes: `reviews.repo` from Tasks 1–2.
- Produces: `delivery.undelivered(store, branch, repo)`; `delivery.surface(store, branch, repo, fmt=..., include_delivered=...)`; `services.svc_surface(store, branch, repo, fmt, include_delivered)`; `Store.list_reviews(branch, limit, repo=None)` — `repo` filters only when `branch` is not None.

- [ ] **Step 1: Write the failing delivery test**

```python
def test_surface_never_delivers_another_repositorys_rounds(tmp_path):
    """One store, two repositories, the same branch. Surfacing A must not
    render B's round -- and must not ACKNOWLEDGE it, which is what left the
    other repository's session with nothing to show."""
    with Store.open(tmp_path / "s.db") as st:
        st.save_review(_delivered_candidate(id="sk_b", branch="main",
                                            repo="/repos/b"))
        rounds = delivery.undelivered(st, "main", "/repos/a")
        assert rounds == []

        status, text, pending = delivery.surface(st, "main", "/repos/a")
        assert "sk_b" not in text
        assert pending == []
        assert st._c.execute(
            "SELECT COUNT(*) FROM deliveries WHERE review_id='sk_b'"
        ).fetchone()[0] == 0, "another repository's round was acknowledged"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_delivery.py -q -k "another_repositorys_rounds"`
Expected: FAIL — `undelivered()` takes two arguments; once the parameter exists, `sk_b` is returned.

- [ ] **Step 3: Scope the delivery SQL**

In `src/skodun/delivery.py`, `_ROUNDS_SELECT` ends with:

```sql
WHERE r.branch = ? AND r.mode = ? AND r.source = ?
```

Change it to `WHERE r.repo = ? AND r.branch = ? AND r.mode = ? AND r.source = ?` and add the repo to the bind tuple as its FIRST parameter, matching the new placeholder order. Thread `repo: str` through `undelivered`, `surface`, and any helper between them.

- [ ] **Step 4: Scope `list_reviews`, for `--branch` only**

In `src/skodun/store.py:1336`:

```python
    def list_reviews(self, branch: str | None, limit: int = 30,
                     repo: str | None = None) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch is not None:
            # Scoped ONLY with a branch: a branch name is the ambiguous key.
            # `branch=None` is a human's "show me everything" (and, before
            # Task 4, the stale sweep) and stays unscoped across repositories.
            q += " WHERE branch=?"
            args = (branch,)
            if repo is not None:
                q += " AND repo=?"
                args += (repo,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
```

- [ ] **Step 5: Pass the repo from the transports**

`services.svc_surface` and `svc_log` take `repo` and pass it down; `cli._cmd_surface` and `_cmd_log` compute `str(gitio.git_common_dir(Path(".")))` — the same expression as Task 2 — and pass it.

On the MCP side the two tools are NOT symmetrical today, and this is the one place the task has real work rather than threading:

* **`surface` already has it.** `_handle_surface` calls `_repo_arg(params, "surface")` and passes the result to `services.resolve_surface_branch`. Reuse that value; add nothing.
* **`log` does not.** `_handle_log` reads only `branch` and `limit`, and its `input_schema` (around `mcpserver.py:651`) declares only those two. Add a `repo` property to that schema and resolve it with `_repo_arg(params, "log")`, exactly as `surface` does — otherwise `log --branch` is repo-scoped on the CLI and unscoped over MCP, which breaks the one-implementation rule the parity tests enforce.

Adding a schema property changes the served tool list, so **the tool-list snapshot test in `tests/test_mcptools.py` must be updated deliberately** — that test exists to make a surface change a reviewed decision, not an accident. Add a CLI-vs-MCP parity test asserting `log --branch` returns the same rows on both surfaces for the same store.

- [ ] **Step 6: Run the affected suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_delivery.py tests/test_cli.py tests/test_mcptools.py tests/test_services.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/skodun/delivery.py src/skodun/services.py src/skodun/cli.py src/skodun/store.py tests/
git commit -m "feat: surface and log --branch are scoped to their repository (refs #13)"
```

- [ ] **Mutations:** (a) drop `r.repo = ?` from the delivery SQL → the delivery test fails; (b) scope `list_reviews` even when `branch is None` → add a test asserting an unscoped listing still returns another repository's rows, and watch it fail; (c) acknowledge before filtering (move the ack above the repo predicate) → the `deliveries` count assertion fails.

---

### Task 4: The stale-recovery scan stops decoding artifacts

**Files:** Modify `src/skodun/store.py`, `src/skodun/pipeline.py`; `tests/test_store.py`, `tests/test_pipeline.py`.

**Interfaces:**
- Produces: `Store.running_records() -> list[dict]` — every row with `status='running'`, as dicts of the INDEXED columns only: `id`, `reviewed_at`, `worst_runtime_sec`. No artifact is read or decoded.
- Consumes: nothing from Tasks 1–3. **Unscoped by repository**, deliberately: a stale row is stale whichever repository recorded it, and the pre-v5 rows are unreachable to every scoped query.

- [ ] **Step 1: Write the failing test**

```python
def test_recover_stale_reads_no_artifacts(tmp_path, monkeypatch):
    """The sweep runs on the synchronous `git push` path and used to decode
    EVERY stored artifact to read a status that is an indexed column. The
    unparseable artifact is the proof: it would raise if anything decoded it."""
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        st.save_review(_running_prepush(id="sk_old", branch="main",
                                        repo="/repos/a"))
        # Corrupt the artifact AFTER a valid write: any decode now raises.
        st._c.execute("UPDATE reviews SET artifact_json='{not json' "
                      "WHERE id='sk_old'")
        st._c.commit()

    with Store.open(db) as st:
        rows = st.running_records()
        assert [r["id"] for r in rows] == ["sk_old"]
        assert set(rows[0]) == {"id", "reviewed_at", "worst_runtime_sec"}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py -q -k "reads_no_artifacts"`
Expected: FAIL — `Store` has no attribute `running_records`.

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

- [ ] **Step 4: Point `recover_stale` at it**

In `src/skodun/pipeline.py`, `recover_stale` currently does `for rec in store.list_reviews(None, _SCAN_ALL):` and then filters on status. Change it to `for rec in store.running_records():` and delete the now-dead status filter. **Both existing properties must survive unchanged**: the record's persisted `worst_runtime_sec` is still preferred over a recomputed ceiling, and a record whose `reviewed_at` will not parse is still left alone.

- [ ] **Step 5: Run the pipeline and store suites**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_store.py tests/test_pipeline.py tests/test_dispatch.py tests/test_batched_review.py -q`
Expected: PASS, including the shipped tests that pin the persisted-budget preference and the unparseable-timestamp rule.

- [ ] **Step 6: Full suite in the background, then commit**

```bash
git add src/skodun/store.py src/skodun/pipeline.py tests/
git commit -m "perf: the stale sweep reads indexed columns, not every artifact (refs #13)"
```

- [ ] **Mutations:** (a) drop `worst_runtime_sec` from the selected columns → the shipped persisted-budget test fails; (b) scope `running_records` by repo → the sweep test must be extended with a second repository's stale row and fail; (c) revert `recover_stale` to `list_reviews(None, _SCAN_ALL)` → the unparseable-artifact test fails.

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
```

- [ ] **Step 2: Run it**

Run: `SKODUN_ORACLE_DIR=<oracle> PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_repo_scoping.py -q`
Expected: PASS on the finished Tasks 1–4.

- [ ] **Step 3: Update the README**

Remove "Background rounds have no repo dimension" from Known limitations; remove the `recover_stale` full-table-scan entry. Add, in its place, a short note that pre-v5 rows are invisible to the scoped queries and that background rounds recorded before the upgrade are never delivered — this is a real user-visible consequence and belongs where the limitations were.

- [ ] **Step 4: Full suite both modes, then commit**

Run with and without `SKODUN_ORACLE_DIR`; reconcile the counts.

```bash
git add tests/test_repo_scoping.py README.md
git commit -m "test: the two-repository drill, and the docs it retires (refs #13)"
```

- [ ] **Mutations:** (a) remove the repo predicate from any ONE of the three scoped queries → the drill must fail, naming which; (b) make the gate repo-scoped → the drill's assertion 4 must fail (this pins the decision NOT to scope it).

---

## Self-Review Notes

- **Spec coverage:** §1 identity → T2 (one expression, both paths); §1 migration → T1; §1 NULL rule → T1 (no backfill) + T5 (assertion 5); §1 three queries → T2 (supersede) and T3 (delivery, `list_reviews`); §1 indexing → T1; §1 not-scoped → T4 (sweep) and T5 assertion 4 (gate), with dedup and triage untouched by any task; §2 scan → T4; Testing section → T1/T4/T5; acceptance criteria 1–5 → T5 plus the standing byte pin.
- **Deliberate decisions restated:** `repo` is a required keyword argument on `reserve_prepush`, so no caller can reserve without one; `list_reviews` scopes only when a branch is given; the stale sweep stays unscoped; the gate stays content-addressed and byte-identical into a fourth phase.
- **Task 2 will break existing test callers** of `reserve_prepush` by making the argument required. That is intended and is cheaper than a default that silently means "match everything".

## Deviations recorded at implementation time

(None yet. When the shipped source contradicts this plan, the implementer amends this section in the same commit — the Phase 2/3 pattern.)
