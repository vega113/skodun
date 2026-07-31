# Bounding review rounds — where skodun should stop, and how clients should

Date: 2026-07-31. Status: proposal, not yet owner-approved.
R1 has since been implemented (issue #5, store v4) — see its section for what
shipped and where it differs from the recommendation below.
Prerequisite reading: `README.md` (the gate contract and the triage ledger),
`docs/phase3-acceptance.md`.
Supporting research: `sdd/review-cutoff-research.md` (sourced brief, with
evidence tiers).

## 1. The problem, measured rather than asserted

An automated review loop does not obviously terminate. Each round of fixes is
new code, the reviewer reviews it, and the loop can run indefinitely. This is
not hypothetical: a sibling project recorded a pull request that took **27
review rounds and surfaced 10 HIGH findings**, and a docs-only change that
churned across multiple heads without converging.

Phase 3 produced a controlled measurement of the same effect. skodun reviewed
its own Phase 3 branch, the findings were fixed, and the fixed branch was
re-reviewed:

| | round 1 | round 2 |
|---|---|---|
| findings | 11 | 6 |
| severity | 1 high / 6 med / 4 low | 1 high / 5 med / 0 low |
| identical `finding_key` carried over | — | **0** |
| findings located in code the previous round's fix wrote | — | **4 of 6** |

Two conclusions follow, and they point in opposite directions from the obvious
intuitions:

1. **Recurrence is not the pathology.** Not one finding repeated. A stopping
   rule built on "the reviewer keeps saying the same thing" would never fire.
2. **Fix-generated surface is the pathology.** Two thirds of round 2 was about
   code that existed only because of round 1. A rule of the form "review until
   the reviewer is quiet" cannot terminate against a target that grows every
   time you touch it.

Therefore: **any convergence-to-zero stopping rule is unsound for this
workload.** The rule has to be about the *consequence* of what is left, not
about whether anything is left.

## 2. What the outside evidence actually supports

Full sourcing and evidence tiers are in `sdd/review-cutoff-research.md`. The
load-bearing points:

- **Every documented stopping mechanism in mature practice is state-based, not
  counter-based.** Google's Engineering Practices is the most authoritative:
  approve once a change *definitely improves code health*, do not hold for
  perfection, mark optional points `Nit:` so the author may ignore them, and
  when author and reviewer stall, **escalate rather than iterate**. Nobody —
  not Google, not Chromium, not any AI-review vendor — publishes an
  evidence-backed round cap. (Strong.)
- **Severity-tier gating is the shipping pattern.** SonarQube's Clean-as-You-Code
  blocks on new defects only and routes the rest to a backlog; CodeRabbit blocks
  on Critical and leaves Warning/Info non-blocking. (Strong, as practice.)
- **Do not let the reviewer certify its own sufficiency.** Even a strong
  LLM-as-judge reaches only ~0.66 precision at deciding whether a review comment
  was useful, so "the bot says this round was enough" is a weak signal.
  (Moderate-to-strong, from a dedicated study.)
- **The famous numbers are thin.** "200–400 LOC / 60–90 minutes / 500 LOC per
  hour" all trace to a single unreplicated Cisco/SmartBear case study with no
  published raw data. Directionally credible, precisely unverifiable — they must
  not appear as calibrated thresholds in skodun's design rationale.

## 3. Where generic practice does NOT transfer to skodun

Two of the research's recommendations must be refused, and saying why is more
useful than adopting them.

**Re-review only the code changed since the last round.** This is the
best-evidenced efficiency measure in the brief, and it is incompatible with
skodun's trust contract. skodun's gate is content-addressed on the identity of
the **whole** outgoing diff: a review that examined only the delta cannot
certify the change, and recording it as though it had would be exactly the
silent partial-coverage failure the fail-closed design exists to prevent. The
benefit can still be had without the breakage — see R2, which *annotates*
findings by churn instead of *narrowing* the review.

**Gate on severity tier.** skodun already considered and rejected this: Phase 2
removed `severity_gate` and `confidence_threshold` from the config, leaving
migration messages that say the gate blocks on any open finding. That decision
should stand, for a reason the sibling project's own rule states plainly —
*severity labels lie in both directions*. A model-assigned "low" on a data-loss
bug is not a safe merge signal, and skodun would be handing its terminating
condition to the least reliable field in the artifact.

## 4. skodun's position: gate on triage completeness, not on severity

The resolution is already in the shipped gate and is worth naming as the
project's answer, because it is stronger than both alternatives:

> The gate passes on **clean OR every finding triaged** — not on findings=0, and
> not on "no criticals".

That terminates as reliably as severity gating, because triaging is an action
the human can always take, while being strictly harder to abuse: clearing a
finding requires a recorded decision that clears an audited floor (≥ 20
characters, placeholder set rejected), is appended to an immutable event stream,
and is reversible on the record via `triage --reopen`. Severity gating asks a
model to decide what may ship; triage gating asks a human, and keeps the
receipt.

Three further properties already hold and should be documented as the anti-loop
machinery they are, rather than left as incidental:

- **Convergence is a property of a frozen diff, and skodun enforces it
  structurally.** Editing during a review makes the gate answer 2, because the
  identity moved. This is not a convention that can be forgotten.
- **The refuter never dismisses anything.** A model's disagreement with a
  finding is an annotation; only a human adopting it by name clears the gate.
  This is the shipped form of "don't let the reviewer self-certify".
- **The dispatcher never re-reviews identical content.** Dedup suppression with
  a recorded audit row is convergence enforcement for the background path.

## 5. Recommendations

Ranked by value per unit of work. R1 is the one that matters.

### R1 — A first-class `defer` verb (needs a v4 migration) — **IMPLEMENTED**

**Status: shipped (issue #5), as store v4.** The recommendation as written is
below, unchanged; what was actually built and where it differs follows it.

Today there is one way to clear a finding: `dismiss`, which means *this is not a
defect*. The stopping rule needs a second: *this is a real defect, it is not
blast-radius for this change, and it is filed as X*. Overloading `dismiss` for
both makes the ledger lie about which findings the project still owes work on.

- `skodun triage --defer <review-id> <index> <tracking-ref> "<reason>"`, a third
  event verb beside `dismiss`/`reopen` in the existing stream.
- **The tracking reference is mandatory and validated**, exactly as the reason
  floor is: a deferral with no filed reference is refused. This makes the
  sibling project's rule — *"an unfiled deferral and an ignored finding are the
  same artifact"* — mechanically true rather than aspirational.
- The gate treats `defer` as non-blocking, like `dismiss`. That is the escape
  from the endless round.
- `triage --list` renders `DEFERRED → <ref>`; a listing of open deferrals across
  reviews (`skodun log --deferred`, or a `deferrals` subcommand) keeps them from
  rotting silently.

**What shipped**, with the decisions the recommendation left open:

- The CLI is exactly as specified, with `--reopen`'s exit contract (0 recorded /
  1 refused / 2 not found). The MCP tool is `triage_defer`, the ninth, behind
  `services.svc_triage_defer` so both surfaces refuse in the same words.
- **The reference lives in its own `tracking_ref` column**, not inside `reason`.
  Widening the v3 `CHECK(event IN ('dismiss','reopen'))` is a table rebuild
  either way — SQLite cannot alter a CHECK in place — so the column cost one
  more name in a column list, and it is what makes the reference *readable back*
  rather than recoverable only by guessing at prose.
- **The gate did not change by one byte.** `store.triage_for` returns the
  findings whose last event is in `CLEARING_EVENTS` (`dismiss` or `defer`), and
  `gate.py` already tested membership of that map and asked nothing further.
  Both files stay byte-identical to their Phase-3 pins.
- `triage --list` renders `DEFERRED -> <ref> <when>` (ASCII arrow, matching the
  rest of the CLI's output).
- The cross-review listing is **`skodun deferrals`**, a subcommand, not `log
  --deferred`: `log` lists reviews one row each while a deferral is a finding
  inside one, and the question "what has this project deferred" has no review to
  scope it to. It is deliberately CLI-only — reviewing the backlog is a human's
  periodic job, and an agent able to both file deferrals and mark them handled
  would hold both ends of the audit trail.
- **`reopen` was widened to overturn a deferral too.** Not in the
  recommendation, but a `defer` moves the gate to 0 exactly as a dismissal does,
  and §4 rests on every clearing decision being "reversible on the record".
- Reference validation is minimal and honest: non-empty, one token (no
  whitespace, no control characters), at least one alphanumeric, ≤200
  characters. No pattern to conform to — a scheme tight enough to "validate" an
  issue key would refuse whichever tracker skodun has never heard of, pushing
  its users back to burying the reference in prose.

### R2 — Churn attribution on findings (cheap, no schema change)

Annotate each finding with whether it lands in code that changed since the
previous review of this branch. Our measurement says this is the signal that
actually exists: 4 of 6 second-round findings were in first-round fix code.
Surfaced as a count — *"4 of 6 findings are in code written by the last fix
round"* — it is the loudest available "the loop is chasing its own tail,
escalate to a human" indicator, and it needs only the previous review's head
plus `git diff`.

### R3 — Round context in the review record and surfaces (cheap)

skodun cannot currently say *"this is review 3 of this branch, and 6 findings
were already triaged in earlier rounds"*, though the store holds everything
needed. A human deciding whether to keep going needs that line, and an agent
needs it more.

### R4 — Blast-radius guidance in the MCP prompts and README (cheap)

The `review-now` prompt correctly tells an agent to stop and not dismiss
anything, but gives no criteria, so it leaves the stopping question unanswered
at the exact moment it is asked. Ship the fix-now/defer table (§6) there and in
the README. **It cannot go in the model-facing review prompt** — that is
byte-pinned for oracle parity.

### R5 — Do NOT add a round cap

Recommended explicitly *against*, because it is the obvious idea. A hard cap is
unsafe: the sibling project's 27-round pull request carried a dead-on-arrival
schema overflow that a 3-round cap would have shipped. No published practice
supports a counter-based rule. If a bound is needed for cost control, bound
*spend* (model calls, wall clock) and surface it as a budget exhaustion that
demands a human decision — never as an automatic "good enough".

## 6. Instructions for skodun MCP clients

For agents driving `skodun mcp`, and for the humans supervising them. This is
the client-side protocol; it belongs in the README and in the `review-now`
prompt.

**Freeze the diff, then review.** Do not edit while a review is in flight. The
gate will catch it — the identity moves and the answer becomes 2 — but the round
is wasted. One review per head.

**Batch fixes into one commit, then review once.** A fix-per-finding cadence
multiplies rounds without improving the outcome.

**Classify every finding by consequence, never by its severity badge.** Record
each deferral with `skodun triage --defer <review-id> <index> <tracking-ref>
"<reason>"` — the reference is mandatory, and `skodun deferrals` is what stops
the backlog from rotting. Fix now only if the finding meets one of:

| Fix now | Defer, with a filed reference |
|---|---|
| The change does not work as described (dead on arrival, silent no-op) | Performance that is within bounds for the surface |
| A safety property the change or its docs explicitly promise is false | Style, naming, consistency |
| A wrong user-facing claim, or data corruption | Documentation drift (unless it is a one-line fix while you are there) |
| It needs a migration or manual repair to undo after merge | Message precision where the outcome is already correct |

Calibrate the bar to what the code promises: a change to a safety mechanism
legitimately holds "a stated guarantee is false" as blocking, where an ordinary
feature would defer the same class of finding.

**Stop when the gate says 0** — clean, or every finding triaged. That is the
terminating condition. It is not "the reviewer found nothing", which for a
non-trivial change may never happen.

**Escalate to a human instead of running another round when** any of these
holds:

- a round produces a blast-radius finding **in code the previous round's fix
  wrote** (the fix is riskier than the bug it fixed);
- the fix for a finding would be larger or touch more files than the change
  under review;
- two consecutive rounds produce only deferrable findings — take the deferrals
  and merge, per Google's "approve once it improves code health";
- the reviewer and the author disagree about whether a finding is real. Escalate;
  do not iterate. Model disagreement is what the refuter annotation is for, and
  it is deliberately not a dismissal.

**Never let the agent clear a finding on its own.** `triage_dismiss`,
`triage_defer` and `adopt_refuter` carry out a decision a human already made;
none of them is a way to tidy a report. This is the shipped policy and the research supports it: an
automated judge is only ~0.66 precise at deciding whether a review comment was
even useful.

## 7. What this does not solve

The deferral backlog is a liability transfer, not a fix: a project that defers
everything ships the same code as a project with no review, and only the filed
references make the difference visible. R1 makes deferral honest — and now does
so mechanically — but it cannot make it wise. Whether the backlog is worked is a human discipline no gate enforces —
which is the correct division of labour, and worth stating so nobody mistakes a
green gate for an absence of debt.
