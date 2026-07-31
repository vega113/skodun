# skodun Phase 4 — Correctness Debt Design (repo scoping, stale-recovery scan)

Date: 2026-07-31. Status: approved (owner confirmed the four forks 2026-07-31);
**revised 2026-07-31 after an adversarial review** — see "Corrections to the
approved spec" below, which records what the first draft got wrong rather than
quietly absorbing the fix.
Prerequisite reading: `README.md` ("One store per repository", "Known
limitations"), the Phase 3 design spec (`2026-07-29-skodun-phase3-design.md`),
`docs/phase3-acceptance.md`.

## Corrections to the approved spec

Two of them, and the first is the phase itself.

1. **"Where the value is written" named two write paths and there are three.**
   The draft listed `store.reserve_prepush` and the foreground `run_review`
   record. It missed the **background worker's** record, built in
   `pipeline.run_prepush_review` (`pipeline.py:1740`) and persisted by
   `store.finalize_review`. `finalize_review` merges only `pid` and
   `superseded_by` back from the stored row and binds **every** other
   `_REVIEW_COLUMNS` value from the worker's dict (`store.py:1038-1045`), so a
   worker whose record carries no `repo` finalizes the row to `repo=NULL` —
   overwriting the value the reservation correctly wrote. Background rounds are
   the only kind `surface` delivers. Implemented as drafted, the phase's
   headline fix would have been silently inert on exactly the path the defect
   was reproduced on: every finished background round would land NULL, every
   scoped `surface` would return nothing, and the two-repository drill would
   have "passed" against reservations that no worker had yet finalized. Fixed in
   "Where the value is written" below.
2. **`log`'s repo scope had no surface to reach it from.** The draft scoped
   `list_reviews(branch=...)` but left `skodun log` with no `--repo` flag while
   assuming the MCP `log` tool would grow one. Resolved in "Reaching the scope
   from a transport" below.

## Scope

Phase 4 is the **correctness phase**, and it is deliberately small. Phases 1–3
each added a capability; this one fixes two things that can silently do the
wrong thing, and adds nothing.

1. **Repository scoping** — a `repo` column on `reviews` (store v5), consulted
   by the three queries that act on a repository's behalf.
2. **Stale-recovery scan** — stop decoding every artifact ever stored on the
   synchronous `git push` path.

**Cut from Phase 4, explicitly.** Worker-log pruning and the shim's unchecked
stdin buffering stay in the README's Known limitations. Churn attribution (R2)
and round context (R3) stay in epic #13. Scheduling, retention, `skodun doctor`,
generic adapters, local models, and the `junie` adapter are all still uncut from
earlier phases. A `junie` adapter in particular remains wanted and is a phase of
its own: the confinement it needs (empty capsule, Seatbelt profile, sanitized
environment, post-run mutation checks) is security-critical code, not a fifth
item on a correctness list.

## 1. Repository scoping

### The defect

`reviews` is keyed by branch. Two repositories sharing one store — which is the
default, since `SKODUN_DB` defaults to a single path under `$HOME` — collide on
any common branch name. Both halves were reproduced live during the Phase 3
final review:

* a push of repo A's `main` returned repo B's live reservation in
  `Reservation.superseded`, **with its pid**, so `signal_superseded` SIGTERMed
  an unrelated running worker (the `ps` guard passed — it *is* a skodun worker);
* a single `surface` call rendered *and acknowledged* both repositories' rounds,
  after which the other repository's session surfaced nothing.

The gate is unaffected: it is content-addressed and never consulted a branch.

### Repository identity

The value is `gitio.git_common_dir(repo)`, resolved to an absolute path.

This reuses the expression the foreground lock already scopes by, so "the same
repository" has exactly one definition in the codebase. Linked worktrees share a
common directory and are therefore correctly the same repository — which is the
behaviour skodun wants, since a worktree is where reviews are supposed to run.

The cost is stated rather than hidden: **moving or re-cloning a repository
changes the path, and every row recorded under the old one becomes invisible to
the scoped queries.** That is fail-closed (the consequence is redundant reviews
and undelivered history, never a wrong verdict), and it is consistent with the
NULL rule below. A remote URL would survive a move, but a repository may have no
remote, may have several, and the URL changes on a host migration; a generated
id stored inside `.git` would survive a move but requires writing there. Neither
trade is worth taking for a local-first tool whose store already lives beside
one machine's checkouts.

### Migration: v4 → v5

One `BEGIN IMMEDIATE` carrying the delta and the version stamp, the shape v3
established:

```sql
ALTER TABLE reviews ADD COLUMN repo TEXT;
```

No backfill and no table rebuild. Pre-v5 rows keep `repo IS NULL` permanently.
The future-version refusal is unchanged **as a rule**, but the *test* of it is
not: `test_a_store_stamped_v5_is_still_refused_untouched`
(`tests/test_store.py:1724`) stamps `user_version = 5` to build a "newer than
this build" store, and after this phase v5 is this build. That test moves to
v6 — the refusal it pins is `> SCHEMA_VERSION`, never the literal 5. The
existing v0/v2/v3/v4 migration tests extend to v5.

The delta is applied with the v3/v4 transactional discipline — a tuple of
statements inside ONE `BEGIN IMMEDIATE` carrying the version stamp — because
`ALTER TABLE ADD COLUMN` is not replay-idempotent: a store that applied the
`ALTER` and then crashed before the stamp comes back at v4 and replays it into
`duplicate column name`, bricked.

### NULL means "matches nothing"

A pre-v5 row is never superseded, never surfaced, never signalled, and never
listed by a repo-scoped query. `repo = ?` already excludes NULL in SQL, so this
needs no special case — but it must be *pinned by a test*, because it is a
decision rather than an accident of SQL semantics.

The failure mode is "an old row is invisible to the new queries", never "the
wrong repository's worker was killed". The price, stated in the migration note
and the README: **background rounds recorded before the upgrade are never
delivered by `surface`.** They remain in the store, readable by `log`, and the
gate can still match them by content.

### Where the value is written — THREE sites, not two

The first draft of this section said "both persistence paths" and named two. It
was wrong, and the way it was wrong is worth stating rather than deleting,
because the missing site is the one the whole phase is about.

| # | Site | What writes the row |
|---|---|---|
| 1 | `store.reserve_prepush` (`store.py:874-880`) | the reserved `running` row, from the dispatcher's repository |
| 2 | `pipeline.run_review`'s `common` (`pipeline.py:1240`) | the foreground record, via `save_review` |
| 3 | `pipeline.run_prepush_review`'s `common` (`pipeline.py:1740`) | **the background worker's finished record, via `finalize_review`** |

Site 3 is the one the draft missed. `finalize_review` (`store.py:1038-1045`)
merges exactly two fields back from the stored row — `pid` and `superseded_by`,
the database-owned ones — and binds every other column in `_REVIEW_COLUMNS`
from the worker's dict. A worker record with no `repo` key therefore writes
`repo=NULL` **over** the value site 1 correctly persisted, at the moment the
round becomes deliverable. And background rounds are the only kind `surface`
delivers, so patching sites 1 and 2 alone would have shipped a phase whose
headline fix does nothing: every finished background round NULL, every scoped
`surface` empty, and the two-repository drill green only because it asserts
against reservations rather than finalized rounds.

Site 3 takes its value from the reservation, not from a fresh git call:
`run_prepush_review` already reads `worst_runtime_sec` and `pid` off the
reserved row (`reserved = store.get_review(record_id) or {}`), and `repo` joins
them for the same reason — it is a fact about the reservation, and the worker
recomputing it could disagree with the row it is finalizing.

All three are written under the Phase 1 rule: the indexed column and the
artifact JSON are set from the same dict in the same statement
(`_review_values` serializes `json.dumps(rec)` beside the binds), so an index
row that disagrees with its artifact stays impossible.

Mechanically, `repo` reaches the column only if it is appended to **both**
`_REVIEW_COLUMNS` (`store.py:523-530`) and the hand-written positional tuple
`_review_values` returns (`store.py:532-553`). They are not derived from one
another: `_INSERT_REVIEW` and `_FINALIZE_REVIEW` size their placeholders from
the column list, so a column added to the list alone is 27 placeholders against
26 binds and a `ProgrammingError` on every write.

#### Why `finalize_review` does not make `repo` database-owned

The alternative to threading the value through site 3 is merging it back from
the stored row, as `pid` and `superseded_by` are — which would make the class of
bug above structurally impossible rather than merely fixed. It is deliberately
not taken, for one reason: **a value cannot be pinned by two mechanisms.** With
the merge in place, dropping `repo` from the worker's record would change
nothing, so no mutation could kill it and the threading would rot untested; with
the threading in place, a merge would be the dead half. One authoritative path,
one mutation that kills it (drop `repo=` from `run_prepush_review`'s `common`,
and the background-delivery test fails). `finalize_review`'s contract is
otherwise untouched by this phase, which is also worth something.

### Where the value is read — exactly three queries

| Site | Query | Harm today |
|---|---|---|
| `store.py` `reserve_prepush` | the same-branch supersede `SELECT` and `UPDATE` (two statements) | retires, and SIGTERMs the worker of, another repository's running review |
| `delivery.py` | `_ROUNDS_SELECT`, which is the base of BOTH the undelivered query and its `--include-delivered` replay sibling | `surface` delivers and acknowledges another repository's rounds |
| `store.py` `list_reviews` | `WHERE branch=?` | `log --branch` shows another repository's history |

Only the `--branch` form of `list_reviews` is scoped, because a branch name is
exactly the ambiguous key. `log` with no `--branch` is a human's "show me
everything" and stays unscoped.

### Reaching the scope from a transport

Scoping a query is half a fix; the other half is that both surfaces can aim it,
and today they cannot aim it the same way.

* **`surface`** already takes a repository on both surfaces — `--repo` on the
  CLI (`cli.py:250-253`) and a `repo` property on the MCP tool. Neither is the
  value the column stores, though: both are a *checkout path*, and the column
  holds `gitio.git_common_dir(...)`. Each transport converts, and a conversion
  that fails is a refusal (status 2), never a fall back to the cwd — reporting
  and permanently acknowledging a different repository's rounds because the
  named one could not be read is the exact damage this phase removes. The MCP
  `_repo_arg` helper (`mcpserver.py:281-303`) returns `Path | None` and does
  **not** convert; `_handle_surface` must.
* **`log`** takes one on neither. **Decision: `skodun log` gains `--repo`,
  matching `surface`'s shape exactly (`type=Path`, `default=None`, meaning
  "here"), and the MCP `log` tool gains the same optional `repo` property.**
  The alternative — leave MCP `log` unscoped — was rejected in one sentence: a
  scope the CLI cannot aim is a scope the user cannot inspect, and a `log` that
  is repo-scoped on one surface and global on the other makes "whose history is
  this" depend on which client you asked.

  `--repo` narrows `--branch` and does nothing without it, which is
  `list_reviews`'s own contract and is said so in the flag's help text. The repo
  is therefore resolved **only when `--branch` is given**: `gitio.git_common_dir`
  shells out to git and raises `GitError` outside a repository, and `skodun log`
  with no branch has always been runnable from anywhere. That must not change,
  and the resolution is wrapped so a failure is a refusal with a message rather
  than a traceback.

`recover_stale` is today the other `branch=None` caller, and §2 moves it off
`list_reviews` onto its own query. **That query is likewise unscoped by
repository**, and deliberately: a stale row is stale whichever repository
recorded it, and scoping the sweep would leave other repositories' abandoned
`running` rows to rot forever — including the pre-v5 rows that, by the NULL
rule, no scoped query can reach at all.

### Indexing

The shipped `ix_reviews_branch` is `(branch, reviewed_at)`. Two of the three
scoped queries now filter on `repo` as well, so the index no longer covers their
leading predicate. Widen it to `(repo, branch, reviewed_at)` in the same v5
delta — index changes are `CREATE INDEX IF NOT EXISTS`, replay-safe, and belong
with the column rather than in a later version. The Phase 1 additive rule holds:
the old index is kept, not dropped, so nothing that reads it changes behaviour.

Sizing note, so this is not cargo-culted: at the store sizes skodun sees
(thousands of rows) neither index decides correctness, and the measurable
latency problem is §2's artifact decoding, not index selection. The widened
index is cheap and correct; it is not the fix for anything.

### Deliberately NOT repo-scoped

Each of these is a decision, not an omission:

* **The gate.** It looks a review up by `diff_hash` alone. Identical diff bytes
  at the same base are the same change, and a review of them is a valid review
  wherever it happened — that is the property diff-identity exists to express.
  `gate.py` therefore stays **byte-identical into a fourth phase**, and the
  Phase 3 pledge and its sha256 pins carry over unchanged. (Phase 3 recorded
  "tighten the gate lookup to the current branch" as a named owner decision
  deliberately not taken; this spec records the repo variant of that question
  and takes the same answer, for the same reason.)
* **Dedup suppression.** Same content-addressed reasoning, and the lease already
  requires `base_sha` equality, which in practice differs across repositories.
* **`triage_events`.** Scoped by `branch` + `base_sha`. A dismissal is a
  judgement about a finding, not about a checkout.

## 2. Stale-recovery scan

`pipeline.recover_stale` iterates `store.list_reviews(None, _SCAN_ALL)`, and
`list_reviews` is `SELECT artifact_json ...` followed by `json.loads` per row.
It therefore **decodes every artifact ever stored** in order to find rows whose
`status` is already an indexed column — on the synchronous `git push` path,
inside the pre-push hook, where latency is felt directly. Measured: 0.31 s at
7,000 rows against 0.007 s for a targeted query, growing linearly with review
history forever.

The fix is a new store method that returns the three columns `recover_stale`
actually reads — `id`, `reviewed_at`, `worst_runtime_sec` — selected
`WHERE status='running'`, with no artifact decode. `status` is the PREDICATE,
not a returned key: every row the method yields is running by construction, so
returning the column would invite a caller to re-filter on it and drift from the
query. `list_reviews` keeps its current shape for its display callers.

(An earlier draft of this paragraph listed `status` among the returned columns,
which contradicted the plan and the shipped method. Corrected after the review
of PR #21 flagged the drift; the three-key shape is what shipped and what
`test_recover_stale_reads_no_artifacts` pins.)

Two properties must survive, both already pinned by Phase 3 tests and both
easy to break here:

* `recover_stale` **prefers the record's persisted `worst_runtime_sec`** over a
  recomputed ceiling, so a multi-batch record is not reclaimed at a
  single-review budget. The new query must return that column.
* A record whose `reviewed_at` will not parse is **left alone**. Age is the only
  evidence the sweep has and it does not act on evidence it lacks.

The new query also drops `list_reviews`'s `ORDER BY reviewed_at DESC`, and that
is harmless for a sweep: the loop acts on every row it is given, independently,
and never stops early. The ordering existed only so that the display callers
show newest first — which is why `list_reviews` keeps it — and the comment
attached to `_SCAN_ALL` (`pipeline.py:201-206`), which explains that the sweep
needs an unbounded LIMIT *because* the query is ordered newest-first, becomes
false the moment the sweep stops calling `list_reviews`. `_SCAN_ALL` has no
other caller and goes with it.

The sweep stays unscoped by repository (§1).

## Testing

The phase's own conformance obligations, beyond ordinary unit coverage:

* **A two-repository drill**, executable, as the phase's headline test: two real
  repositories sharing one store, same branch name in both, a running review in
  each. Assert that a push in A does not retire, signal, surface, or list B's
  review — the exact failure reproduced in Phase 3.
* **A finalized background round carries its repo.** Pinned separately from the
  drill and named here because it is correction 1: a round that has been through
  `finalize_review` — not merely reserved — must still have its `repo`, and must
  still be invisible to the other repository's `surface`. A drill that only ever
  looks at reserved rows cannot see this failure.
* **The NULL rule pinned** — a pre-v5 row is invisible to each of the three
  scoped queries, and still visible to the gate and to unscoped `log`.
* **Migration**: v4 → v5 extends the existing ladder tests; a mid-delta failure
  injection reopens cleanly at v4 and migrates on retry.
* **The scan**: `recover_stale` decodes no artifacts (assert by counting
  `json.loads` calls or by driving a store whose artifacts are deliberately
  unparseable), while still honouring the persisted budget and the unparseable
  timestamp rule.
* **The byte pin holds**: `gate.py` and `trust.py` sha256 unchanged, now across
  four phases.

Every task carries named mutations, killed by execution, as in Phase 3.

## Acceptance criteria

1. Full suite green with and without `SKODUN_ORACLE_DIR`, counts reconciled.
2. The two-repository drill passes, and fails when the repo predicate is removed
   from any one of the three queries.
3. A v4 store migrates to v5 in place, with its rows intact and invisible to the
   scoped queries; the gate still matches them by content.
4. `recover_stale` decodes zero artifacts and still honours both pinned
   properties — asserted by calling `recover_stale`, not `running_records`.
5. `gate.py`/`trust.py` byte-identical to their Phase 3 hashes.
6. **A background round that has been finalized carries a non-NULL `repo`**, and
   `surface` for the other repository does not deliver it. This is correction 1
   and it gets a criterion of its own: without it, criteria 2 and 3 can all pass
   against a store whose deliverable rounds are every one of them NULL.
7. `skodun log --branch` and the MCP `log` tool answer the same rows for the same
   store and the same repository, and `skodun log` with no `--branch` still runs
   outside a repository.
