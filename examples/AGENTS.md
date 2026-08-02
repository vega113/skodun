# Using skodun — a section to paste into your own AGENTS.md

This file is a **template for the repository that uses skodun**, not for skodun
itself. Copy the section below into your project's `AGENTS.md` (or `CLAUDE.md`,
`GEMINI.md`, `.cursorrules` — whatever your agents read) and edit the bracketed
parts. It tells a coding agent how to drive skodun, and — the part agents get
wrong without being told — **when to stop reviewing**.

Everything here is behaviour skodun already enforces or that its design
documents; nothing needs a plugin.

**Smaller paste-ins** (MCP-only loop, MCP topology, concurrency, operator MCP
JSON, OpenAI API BYOK): [`fragments/`](fragments/) — start with
[`fragments/mcp-review-topology.md`](fragments/mcp-review-topology.md) if agents
confuse MCP process, repository, and worktree; use
[`fragments/openai-api.md`](fragments/openai-api.md) for metered OpenAI HTTP
(`OPENAI_API_KEY` / MCP `env`, daily spend cap).  
**Full client integration** (install, MCP, gate wiring):  
[`../docs/integrate-external-project.md`](../docs/integrate-external-project.md).

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
- `skodun triage --list <review-id>` — the findings, with their triage state
  (`OPEN`, `DISMISSED`, `DEFERRED -> <ref>`, `REOPENED`).
- `skodun triage --defer <review-id> <n> <tracking-ref> "<reason>"` — record that
  a finding is real, is not blast-radius for this change, and is FILED as
  `<tracking-ref>`. It clears the gate; the reference is mandatory and a deferral
  without one is refused. See "A deferral must be filed" below.
- `skodun deferrals` — every finding still standing as deferred, across all
  reviews. This is the backlog the deferrals above created; it is a human's to
  review.
- `skodun surface` — background rounds nobody has seen yet (from the pre-push
  hook). Reports history; it never certifies the current change.
- `skodun review-status [REVIEW-ID] [--repo PATH]` — observe a review's
  lifecycle (`queued|running|cancelled|failed|clean|findings`) plus age,
  provider, and model when known. By id, or current for `--repo`. **Not a
  gate** — use `gate` for coverage of the current change.
- `skodun review-cancel <REVIEW-ID>` — cancel an in-flight review (token and/or
  process signal, durable untrustworthy terminal, FG lock released). Refuses
  missing ids and already-terminal rows.
- `skodun doctor` — install/MCP readiness (config, store schema, adapters,
  binaries). Read-only; does not move the gate. **CLI-only** (not an MCP tool).
- `skodun retain [--dry-run]` — prune worker logs per `[retention]`. Never
  deletes gate artifacts. **CLI-only.**
- `skodun schedule install` — write launchd plists from `[[schedule.jobs]]`.
  No scheduler runs inside `skodun mcp`. **CLI-only** (macOS launchd).

If skodun is wired in over MCP, the review-loop operations are the `gate`,
`review`, `log`, `surface`, `triage_list`, `triage_dismiss`, `adopt_refuter`,
`triage_reopen`, `triage_defer`, `review_status`, and `review_cancel` tools,
with identical wording and identical refusals via the shared service path.
Pass **`repo` as an absolute project path** on `gate` / `review` / `log` /
`surface` / `review_status` when the MCP server’s cwd may not be this
repository. Optional `reviewer` on `review` is a configured entry **name**,
not a provider id. There is no `deferrals`, `doctor`, `retain`, or `schedule`
tool: backlog review is a human's job, and ops verbs are shell-out CLI commands
so the stdio MCP server stays free of schedulers and mutators that are not part
of the agent review loop.

Setup for external projects: [`../docs/integrate-external-project.md`](../docs/integrate-external-project.md).

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

| Fix now | Defer (`triage --defer`), with a filed issue |
|---|---|
| The change does not work as described (dead on arrival, silent no-op) | Performance that is within bounds for this surface |
| A safety property the change or its docs explicitly promise is false | Style, naming, consistency |
| A wrong user-facing claim, or data corruption | Documentation drift (unless it is one line while you are there) |
| Undoing it after merge needs a migration or manual repair | Message precision where the outcome is already right |

Calibrate to what the code promises: a change to a safety mechanism legitimately
treats "a stated guarantee is false" as blocking, where an ordinary feature
defers the same class of finding.

**A deferral must be filed, and skodun enforces it.** Open an issue in [<your
tracker>] FIRST, then record the deferral against its reference:

```bash
skodun triage --defer <review-id> <n> <tracking-ref> "<why it can wait>"
```

`<tracking-ref>` is an issue number, a tracker key or a URL — one token, not
prose — and it is **mandatory**: a deferral without a usable reference is refused
exactly as a placeholder reason is, because an unfiled deferral and an ignored
finding are the same artifact. Do NOT dismiss a real finding and mention the
issue in the reason; `dismiss` means "not a defect", and using it for a deferral
makes the ledger stop distinguishing outstanding debt from rejected findings.

The deferral clears the gate. It does not clear the work: `skodun deferrals`
lists every one that is still open, across every review and branch.

### When to stop and ask a human instead of running another round

Each round of fixes is new code, and the reviewer will review it. That does not
converge on its own. Escalate rather than iterate when:

- a round raises a fix-now finding **in code the previous round's fix wrote** —
  the fix is riskier than the bug it fixed;
- fixing a finding would touch more code than the change under review;
- two rounds in a row produce only deferrable findings — file them and merge;
- you disagree with a finding. Record **non-gate** feedback
  (`skodun feedback add --kind finding_judgment …` or MCP `feedback_add`) with
  a substantive reason, tell the human, and stop — do not argue with the
  reviewer by rewriting code. Do **not** `triage_dismiss` on your own (that
  clears the gate). If you hit a **skodun product bug**, record
  `skodun feedback add --kind product_bug …` so maintainers can inspect later
  (see [`fragments/feedback.md`](fragments/feedback.md)).

### What you must not do

- **Never clear a finding on your own.** `skodun triage <id> <n> "<reason>"`,
  `--defer` and `--adopt-refuter` all record a *human's* decision in an audit
  ledger and all move the gate; none of them is a way to tidy a report. Present
  the findings and let a human decide — including which ones may be deferred and
  under what reference. You **may** use `feedback add` / `feedback_add` to
  record your judgment or a skodun product bug without moving the gate.
- **Never push with `SKODUN_PREPUSH_SKIP=1`** or by disabling the hook to get a
  green run. If the gate refuses, that is the product working.
- **Never treat `surface` output as a verdict.** It reports history. Only `gate`
  answers whether the current change is covered.

### Providers, R2/R3 presentation, ops

- Registered providers include `xai` (grok), `openai` (codex), `google` (agy),
  and **`junie`** (macOS-only, confined empty capsule + Seatbelt). Off macOS a
  junie reviewer is `unavailable` and the chain advances.
- Multiple providers are a **fallback chain**, not parallel review slots. Prefer
  **one** finder chain → gate. Do not also run legacy grok-review scripts and
  every cloud bot for ordinary changes unless policy says so.
- `triage --list` / `log` / `surface` may show **round ordinal** and **churn**
  marks (findings in files changed since the previous trustworthy review). That
  is presentation only — it never narrows the model prompt or the gate unit of
  trust (still the full outgoing diff).
- If install looks broken, run `skodun doctor` before inventing a second review
  system. For disk growth of worker logs, use `skodun retain` (or a launchd job
  from `skodun schedule install`).

### Concurrency (today)

- **One** foreground review per repository (repo lock). CLI waiters may exit `3`.
- **One** MCP `review` per server process; a second call is refused
  (`review already in flight`), not queued.
- **Status / cancel (S1):** use `review-status` / `review-cancel` (MCP:
  `review_status` / `review_cancel`) instead of abandoning a hung provider.
  Closing the MCP session still cancels the in-flight MCP `review`.
- Do **not** burn agent turns polling every 30–60s. Wait outside the model, then
  call `review-status` / `gate` / `log` / `surface`.
- Deeper notes: [`fragments/concurrency.md`](fragments/concurrency.md).

### If skodun is unavailable

A missing or unauthenticated model CLI makes reviews fail, and a failed review
is not a passed one — the gate answers `2` and the change is not covered. Run
`skodun doctor` to see which adapter/binary/config is wrong. Say so and stop;
do not work around it.
