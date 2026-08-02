# Fragment: agent loop via MCP (paste into AGENTS.md)

Use when the project already has agent instructions and only needs the skodun
MCP review loop.

---

## Code review with skodun (MCP)

This project uses **skodun** as the local review backend over MCP. The gate
keys on **exact diff content**, not commit SHAs.

### Tools

- `gate` — does a trustworthy review cover **this** tree? **Stop when status is 0.**
- `review` — run one foreground review (minutes, model cost). Optional
  `reviewer` = configured `[[reviewers]]` name.
- `triage_list` / `triage_dismiss` / `triage_defer` / `triage_reopen` /
  `adopt_refuter` — audited human decisions only; never bulk-clear findings.
- `log` / `surface` — history and undelivered background rounds; **not** a gate.

CLI-only when needed: `skodun doctor`, `skodun providers` (shell).

### Loop

1. Finish edits; freeze the tree.
2. Call `gate`. If `0`, **stop** (already covered).
3. Call `review` **once**. Do not start a second `review` while one is in flight
   (server refuses with “review already in flight”).
4. Summarize findings for a human. Do **not** dismiss/defer unless the human
   decided; `triage_defer` requires a **filed** tracking ref.
5. Call `gate` again. **Stop at 0.** Not at “reviewer found nothing.”

### Escalation (do not iterate forever)

Stop and ask a human when a round raises a must-fix finding in code the
previous fix wrote, when the fix would exceed the change under review, or when
you disagree with a finding.

### Must not

- Never treat `surface` as coverage of the current tree.
- Never invent a second review system (oracle scripts, ad-hoc CLI) if skodun is
  the configured backend.
- Never poll every 30s with a full agent turn “to see if review finished” —
  wait outside the model, then call `gate` / `log`.
