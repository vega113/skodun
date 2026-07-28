# Shadow-mode runbook

Shadow mode runs skodun *beside* an existing review system on the same
change-sets and compares the two verdicts. Nothing is cut over: skodun records
its own reviews, the legacy system keeps recording its own, and
`skodun shadow-compare` joins the two archives on `diff_hash`.

This is the Phase 1 acceptance procedure. It assumes a repository that already
uses the legacy shell-based review scripts and has a populated `.grok-reviews`
archive — that archive is the porting oracle, and the runbook's whole purpose is
to show skodun agreeing with it on real changes.

## Why both systems can run at once

Both acquire the **same foreground lock**, at
`<git-common-dir>/grok-reviews-foreground.lock`, using the same three-line owner
format (`pid=`, `started=`, `worktree=`). Each can therefore judge the other's
liveness, and the two never hit the inference backend concurrently. If one waits
on the other, that is the design working, not a bug.

## Prerequisites

- The `grok` CLI installed and authenticated. skodun resolves it via
  `SKODUN_GROK_BIN`, then `~/.grok/bin/grok`, then `grok` on `PATH`.
- A **linked worktree** with real outgoing changes. `skodun review` refuses to
  run in a primary checkout unless `SKODUN_ALLOW_MAIN=1` — review the branch you
  are about to push, not your main working copy.
- Python ≥ 3.12. The runtime has no third-party dependencies.

## Setup

1. **Import the legacy archive** (once; idempotent). This gives the gate
   continuity for already-reviewed content and carries existing dismissals
   across, so previously-triaged findings do not resurface:

   ```
   skodun import-legacy --dir "$(git rev-parse --git-common-dir)/../.grok-reviews"
   ```

   The command prints a stats line. `demoted_no_artifact` counts index rows whose
   full artifact was missing or invalid — those are imported as history but are
   never gate-eligible. `triage_unauditable` counts dismissals that failed the
   audit floor and were dropped; those findings will show as open again.

2. **Configure a reviewer** in `~/.config/skodun/config.toml`:

   ```toml
   [[reviewers]]
   name = "finder"
   provider = "xai"
   model = "grok-4.20-0309-reasoning"
   role = "finder"
   ```

   Use the same model the legacy scripts use, or the comparison measures the
   model change rather than the port.

3. **Optional — repo-layout tables.** skodun ships generic defaults: no
   path→checklist mapping, and a generic security-trigger set. To reproduce a
   specific stack's behavior, copy the example config into the repo:

   ```
   cp examples/scala-angular-monorepo.toml <repo>/.skodun.toml
   ```

   Without it, only the `core` checklist section is selected and the security
   pass triggers on the generic segment set. This is intentional: the committed
   code carries no project's directory layout.

## Per-change-set procedure

In a linked worktree with real outgoing changes:

```
sh scripts/grok-review-now.sh     # legacy first
skodun review                     # skodun second — waits on the shared lock
skodun gate                       # 0 clean/all-triaged, 1 findings open, 2 no trustworthy review
sh scripts/grok-review-now.sh --gate
```

The two gate exit codes must agree. Then:

```
skodun shadow-compare
```

`match` is deliberately narrow: both sides present, agreeing on `trustworthy`
and on cleanliness (zero findings vs some). Exact finding counts and severity
tallies between two independent model runs are *not* expected to be equal — they
appear in `deltas` for human eyes and never affect `match`.

## Acceptance bar

Over **at least 5 real change-sets with distinct `diff_hash` values**:

- no crash in either system;
- every skodun run produces either a trustworthy verdict or an explicit
  `degraded`/`failed` record — never a silent pass;
- `skodun gate` and the legacy gate return the same code on each;
- dismissing one finding with `skodun triage` flips that repo's gate from 1 to 0.

Record each comparison in the log table below.

## Known deliberate divergences

These are expected in a shadow run and are not defects. Each is fail-safe: it
can cost one extra review, never a silent all-clear.

- **`degraded` is reported more often.** skodun matches `max turns reached`
  case-insensitively (the legacy script's grep is case-sensitive), parses the
  response envelope with a resilient decoder so trailing bytes cannot hide a
  non-`EndTurn` `stopReason`, and decodes leniently where the legacy script is
  strict.
- **Trust is never short-circuited.** The legacy triage script trusts a stored
  `trustworthy` field over the axes it was derived from, as a back-compat path
  for rows predating that field. skodun always recomputes from
  `parse_ok and not degraded and not diff_truncated`.
- **Artifact validation is stricter.** skodun requires a persisted artifact to
  carry `findings`, `findings_total`, `id`, `branch` and `base_sha`; the legacy
  validator tolerates missing keys. A lenient reading would let an artifact with
  no recorded findings read as clean.
- **New files with non-ASCII names are reviewed.** Under git's default
  `core.quotepath`, the legacy script's untracked-file guard sees a quoted
  literal and silently skips such a file. skodun includes it, so the two compute
  different `diff_hash` values for that change — a missed join, never a wrong
  pass.
- **Repo-layout tables are configuration.** Checklist mapping, test-path
  patterns and security triggers live in `.skodun.toml`, not in code. Without
  `examples/scala-angular-monorepo.toml` copied in, selection and security
  triggering are deliberately narrower than the legacy scripts'.

## Comparison log

<!-- SHADOW-LOG-START -->

### Run 1 — 2026-07-27, 8 change-sets

Eight linked worktrees of the reference repository, each with real outgoing
changes, reviewed by skodun and compared against the legacy archive. Model
`grok-4.5` on both sides; the repo-layout tables from
`examples/scala-angular-monorepo.toml` loaded via the user-level config.

| # | `diff_hash` | files | skodun verdict | skodun gate | legacy gate | shadow |
|---|---|---|---|---|---|---|
| 1 | `c82ba3ce0662` | 1 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 2 | `a91ad91a7d75` | 8 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 3 | `399cf6762ed5` | 12 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 4 | `fbb7dbc5557a` | 21 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 5 | `e472ff4eadfc` | 3 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 6 | `cc9d21ffc9c5` | 11 | trustworthy, 0 findings | 0 | 0 | MATCH |
| 7 | `f534fdbe972c` | 5 | trustworthy, 2 medium | 1 | 0 | MISMATCH |
| 8 | `b13dc872f7f6` | 6 | trustworthy, 1 high 1 medium | 1 | 1 | MATCH |

Eight distinct `diff_hash` values, comfortably over the five-change-set bar.

**Result against the acceptance bar**

- **No crash on either side.** Every run completed; wall-clock per review ranged
  from 157 s to 1930 s.
- **Every skodun run produced an explicit verdict.** All eight were
  `trustworthy=true` with `stop_reason=EndTurn`. No silent pass anywhere.
- **Gate agreement: 7 of 8.** Row 7 is examined below — it is not a porting
  defect.
- **Triage flips the gate.** On row 7: `skodun gate` → 1; a placeholder reason
  (`"fp"`) was rejected by the audit floor with a message explaining what a real
  reason must contain; two substantive dismissals were accepted; `skodun gate` →
  0 (`PASS 2 finding(s), all triaged`). Run on a copy of the store, since the
  dismissals were a mechanism test rather than a code judgement.

**Row 7, the one gate disagreement.** Both gates behaved correctly on their own
inputs. skodun's newest trustworthy review of that content was the one taken
during this run, which reported two medium findings, so it returned 1. The
legacy gate matched its own stored review of the same content from ten days
earlier, which had reported zero findings, so it returned 0. Two independent
model runs over the same diff reached different conclusions; skodun was the
stricter of the two. Row 8 shows the same effect inside a single verdict class —
skodun found one more finding than the archive — and is still a MATCH, because
`match` is defined on trustworthiness and cleanliness, not on counts.

**Whole-archive comparison.** After importing the legacy archive (6116 reviews,
263 dismissals, 0 corrupt lines, idempotent on re-import):

```
shadow: 4792 compared, 4783 matched, 0 skodun-only, 3 legacy-only
```

**Diff-identity parity, measured separately.** Across **205 real repository
states** — every worktree with outgoing changes — skodun's `diff_identity` and
the legacy `--diff-hash` agreed **without a single mismatch**. This is the
property everything else rests on: had it drifted, no stored review would ever
join a legacy record.

**One real defect surfaced, in the environment rather than the port.** The first
pass used the model id recorded in the legacy settings file, which the installed
grok CLI no longer offers. Every review failed in about five seconds. skodun
recorded `status=failed`, `parse_ok=false`, and emitted
`trustworthy=false` — the fail-closed contract working on an unplanned fault.
This is the failure mode the "model selection is explicit, never inherited from
a settings file" rule exists to make visible.

### Run 2 — 2026-07-28, concurrent-execution proof

Run 1 exercised the two systems in sequence. This run exercised them
**overlapping**, to show the shared foreground lock actually serializes them.

Procedure: start a legacy review in the background; 20 s later, with the lock
held, start `skodun review` on the same worktree; sample the process table once
per second for the whole window, counting live reviewer processes.

```
lock: <git-common-dir>/grok-reviews-foreground.lock
owner file written by the legacy script, read by skodun:
    pid=48934
    started=1785215631
    worktree=<worktree>

05:13:51  legacy review starts, takes the lock
05:14:11  skodun review starts and reports:
          "another foreground review is running (pid=48934); waiting --
           serializing avoids the shared-inference timeout"
05:18:58  skodun completes (287 s wall, most of it waiting)
```

**Result: 277 samples, zero showing more than one reviewer process. Maximum
concurrency observed: 1.**

skodun parsed the legacy script's own three-line owner file, recognised a live
peer, and waited — the interop the byte-format compatibility exists for.

This run also produced the cleanest possible comparison, since both systems
reviewed the *same content at the same time* rather than days apart:

| | id | verdict |
|---|---|---|
| legacy | `loop_claude_apply-sync-floors-db-2565__101eec10b__20260728T051351Z_48944` | trustworthy, 0 findings |
| skodun | `sk_20260728T051611Z_50426_756b3b76` | trustworthy, 0 findings |

Same `head` (`101eec10b`), same verdict, same finding count.

### Imported dismissals remain effective

Checked against the real imported archive (263 dismissals) using the gate's own
decision function, `open_findings`, over `store.triage_for(branch, base_sha)`:
imported legacy dismissals close findings on imported legacy reviews, including
reviews where **every** finding is closed and the gate therefore returns `0`
rather than `1`. A dismissal recorded in the previous system stays honoured
after migration — no one has to re-triage.

<!-- SHADOW-LOG-END -->
