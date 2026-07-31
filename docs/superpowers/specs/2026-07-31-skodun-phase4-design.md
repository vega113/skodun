# skodun Phase 4 — Correctness Debt Design (repo scoping, stale-recovery scan)

Date: 2026-07-31. Status: approved (owner confirmed the four forks 2026-07-31).
Prerequisite reading: `README.md` ("Known limitations"), the Phase 3 design spec
(`2026-07-29-skodun-phase3-design.md`), `docs/phase3-acceptance.md`.

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
The future-version refusal is unchanged, and the existing v0/v2/v3/v4 migration
tests extend to v5.

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

### Where the value is written

Both persistence paths, at the moment the record is created:

* `store.reserve_prepush(...)` — the dispatcher has the repository already;
* the foreground `run_review` path — likewise.

Written under the Phase 1 rule: the indexed column and the artifact JSON are set
in the same statement, so an index row that disagrees with its artifact stays
impossible.

### Where the value is read — exactly three queries

| Site | Query | Harm today |
|---|---|---|
| `store.py` `reserve_prepush` | the same-branch supersede `SELECT` and `UPDATE` (two statements) | retires, and SIGTERMs the worker of, another repository's running review |
| `delivery.py` | the undelivered query | `surface` delivers and acknowledges another repository's rounds |
| `store.py` `list_reviews` | `WHERE branch=?` | `log --branch` shows another repository's history |

Only the `--branch` form of `list_reviews` is scoped, because a branch name is
exactly the ambiguous key. `log` with no `--branch` is a human's "show me
everything" and stays unscoped.

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
`list_reviews` is `SELECT artifact_json ... ` followed by `json.loads` per row.
It therefore **decodes every artifact ever stored** in order to find rows whose
`status` is already an indexed column — on the synchronous `git push` path,
inside the pre-push hook, where latency is felt directly. Measured: 0.31 s at
7,000 rows against 0.007 s for a targeted query, growing linearly with review
history forever.

The fix is a new store method that selects the indexed columns
`recover_stale` actually reads — id, status, `reviewed_at`, `worst_runtime_sec`
— `WHERE status='running'`, with no artifact decode. `list_reviews` keeps its
current shape for its display callers.

Two properties must survive, both already pinned by Phase 3 tests and both
easy to break here:

* `recover_stale` **prefers the record's persisted `worst_runtime_sec`** over a
  recomputed ceiling, so a multi-batch record is not reclaimed at a
  single-review budget. The new query must return that column.
* A record whose `reviewed_at` will not parse is **left alone**. Age is the only
  evidence the sweep has and it does not act on evidence it lacks.

The sweep stays unscoped by repository (§1).

## Testing

The phase's own conformance obligations, beyond ordinary unit coverage:

* **A two-repository drill**, executable, as the phase's headline test: two real
  repositories sharing one store, same branch name in both, a running review in
  each. Assert that a push in A does not retire, signal, surface, or list B's
  review — the exact failure reproduced in Phase 3.
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
   properties.
5. `gate.py`/`trust.py` byte-identical to their Phase 3 hashes.
