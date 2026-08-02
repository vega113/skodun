# Fragment: agent loop via MCP (paste into AGENTS.md)

Use when the project already has agent instructions and only needs the skodun
MCP review loop.

---

## Code review with skodun (MCP)

This project uses **skodun** as the local review backend over MCP. The gate
keys on **exact diff content**, not commit SHAs.

### Tools

Pass **`repo`** as an **absolute path** to this project root on `gate`,
`review`, `log`, and `surface` (if omitted, skodun uses the MCP server cwd,
which may not be this repo).

**MCP process ≠ one repo.** One `skodun mcp` can act on any worktree you pass as
`repo`, but only **one** `review` runs at a time in that process (second call →
`review already in flight`). Multi-worktree / multi-repo parallel agents need
separate MCP processes or CLI — definitions in
[`mcp-review-topology.md`](mcp-review-topology.md).

- `gate` — does a trustworthy review cover **this** tree? Status **0** = clean
  or all findings triaged. **Stop when status is 0.**
- `review` — one foreground review (minutes, model cost). Optional `reviewer` =
  configured `[[reviewers]]` **entry name** (not provider id).
- `triage_list` / `triage_dismiss` / `triage_defer` / `triage_reopen` /
  `adopt_refuter` — audited human decisions only; never bulk-clear.
  `triage_defer` requires a **filed** `tracking_ref`.
- `log` / `surface` — history / undelivered background rounds; **not** a gate.
- `feedback_add` / `feedback_list` — **non-gate** agent judgment or skodun
  product-bug notes for later human inspection (does **not** clear the gate).
  See [`feedback.md`](feedback.md).

CLI-only when needed: `skodun doctor`, `skodun providers` (shell).

### Loop

1. Finish edits; freeze the tree.
2. Call `gate` with `repo`. If status `0`, **stop** (already covered).
3. Call `review` **once** with `repo`. Do not start a second `review` while one
   is in flight (refused: `review already in flight`).
4. Summarize findings for a human. Do **not** dismiss/defer unless the human
   decided.
5. Call `gate` again. **Stop at 0** — not at “reviewer found nothing.”

### Escalation (do not iterate forever)

Stop and ask a human when a round raises a must-fix finding in code the
previous fix wrote, when the fix would exceed the change under review, or when
you disagree with a finding.

### Must not

- Never treat `surface` as coverage of the current tree.
- Never invent a second review system if skodun is the configured backend.
- Never poll every 30–60s with a full agent turn — wait outside the model, then
  call `gate` / `log`.
