# Using skodun — a section to paste into your own AGENTS.md

This file is a **template for the repository that uses skodun**, not for skodun
itself. Copy the section below into your project's `AGENTS.md` (or `CLAUDE.md`,
`GEMINI.md`, `.cursorrules` — whatever your agents read) and edit the bracketed
parts. It tells a coding agent how to drive skodun, and — the part agents get
wrong without being told — **when to stop reviewing**.

Everything here is behaviour skodun already enforces or that its design
documents; nothing needs a plugin.

---

## Code review with skodun

This repository gates pushes on skodun. Before you finish a change, a
trustworthy review must cover it.

### The commands you will use

- `skodun gate` — does a trustworthy review cover the current change? This is
  the only command that answers that question. Exit `0` clean or all findings
  triaged, `1` findings still open, `2` nothing trustworthy covers this content.
- `skodun review` — run a review now, in the foreground. Minutes, and it spends
  model calls. Do not run two at once; they serialize on a lock. Add
  `--reviewer <name>` (MCP: `{"reviewer": "<name>"}`) to head this one review
  with a named reviewer entry — a second opinion, or a provider you know is
  healthy. A name that does not resolve is refused before anything runs, and the
  refusal lists the configured names; do not retry with a guess.
- `skodun triage --list <review-id>` — the findings, with their triage state.
- `skodun surface` — background rounds nobody has seen yet (from the pre-push
  hook). Reports history; it never certifies the current change.

If skodun is wired in over MCP, the same operations are the `gate`, `review`,
`log`, `surface`, `triage_list`, `triage_dismiss`, `adopt_refuter` and
`triage_reopen` tools, with identical wording and identical refusals.

### The loop

1. **Finish the change first, then review.** Do not review a tree you are still
   editing. skodun keys on the content, not the commit, so an edit during a
   review makes the result cover something that no longer exists and the gate
   will say `2`. A review round is only meaningful against a frozen diff.
2. **One review per head.** Fix every finding you are going to fix in ONE
   commit, then review once. A fix-per-finding cadence multiplies rounds
   without improving the result.
3. **Read the verdict line.** `trustworthy=false` means the review does not
   cover the change and nothing may be concluded from it — not "it found
   nothing".
4. **Stop when `skodun gate` exits 0.** That is the terminating condition. It is
   NOT "the reviewer found nothing", which for a real change may never happen.

### When to fix and when to defer

Judge every finding by its **consequence**, never by its severity label — labels
are wrong in both directions. Fix before merging only if the finding meets one
of these:

| Fix now | Defer, with a filed issue |
|---|---|
| The change does not work as described (dead on arrival, silent no-op) | Performance that is within bounds for this surface |
| A safety property the change or its docs explicitly promise is false | Style, naming, consistency |
| A wrong user-facing claim, or data corruption | Documentation drift (unless it is one line while you are there) |
| Undoing it after merge needs a migration or manual repair | Message precision where the outcome is already right |

Calibrate to what the code promises: a change to a safety mechanism legitimately
treats "a stated guarantee is false" as blocking, where an ordinary feature
defers the same class of finding.

**A deferral must be filed.** [Open an issue in <your tracker> and reference it
in the dismissal reason.] An unfiled deferral and an ignored finding are the
same artifact.

### When to stop and ask a human instead of running another round

Each round of fixes is new code, and the reviewer will review it. That does not
converge on its own. Escalate rather than iterate when:

- a round raises a fix-now finding **in code the previous round's fix wrote** —
  the fix is riskier than the bug it fixed;
- fixing a finding would touch more code than the change under review;
- two rounds in a row produce only deferrable findings — file them and merge;
- you disagree with a finding. Say so and stop; do not argue with the reviewer
  by rewriting code.

### What you must not do

- **Never dismiss a finding on your own.** `skodun triage <id> <n> "<reason>"`
  and `--adopt-refuter` record a *human's* decision in an audit ledger; they are
  not a way to tidy a report. Present the findings and let a human decide.
- **Never push with `SKODUN_PREPUSH_SKIP=1`** or by disabling the hook to get a
  green run. If the gate refuses, that is the product working.
- **Never treat `surface` output as a verdict.** It reports history. Only `gate`
  answers whether the current change is covered.

### If skodun is unavailable

A missing or unauthenticated model CLI makes reviews fail, and a failed review
is not a passed one — the gate answers `2` and the change is not covered. Say so
and stop; do not work around it.
