# Fragment: agent loop via MCP (paste into AGENTS.md)

Use when the project already has agent instructions and only needs the skodun
MCP review loop.

---

## Code review with skodun (MCP)

This project uses **skodun** as the local review backend over MCP. The gate
keys on **exact diff content**, not commit SHAs.

### Tools (13 — full catalog)

Pass **`repo`** as an **absolute path** to this project root on tools that
accept it (if omitted, skodun uses the MCP server cwd, which may not be this
repo).

**MCP process ≠ one repo.** One `skodun mcp` can act on any worktree you pass as
`repo`, but only **one** `review` runs at a time in that process (second call →
`review already in flight`). Multi-worktree / multi-repo parallel agents need
separate MCP processes or CLI — definitions in
[`mcp-review-topology.md`](mcp-review-topology.md).

| Tool | Use |
|---|---|
| `gate` | Does a trustworthy review cover **this** tree? Status **0** = stop |
| `review` | One foreground review (minutes). **Omit `reviewer`** unless you want a specific entry — see below. Optional `client_family` declares your own model family |
| `log` | Recent history (not a gate) |
| `surface` | Undelivered background rounds (not a gate) |
| `review_status` | Observe in-flight / terminal lifecycle (not a gate) |
| `review_cancel` | Cancel by review id |
| `triage_list` | Findings + state for one review |
| `triage_dismiss` / `triage_defer` / `triage_reopen` / `adopt_refuter` | Audited **human** gate decisions only; never bulk-clear. `triage_defer` needs a **filed** `tracking_ref` |
| `feedback_add` / `feedback_list` | Non-gate judgment / product bugs — see [`feedback.md`](feedback.md) |

If your host shows **fewer than 13** tools, the MCP process is an **old build** —
**restart the MCP session** (see below). Do not invent missing tools via shell.

CLI-only when needed: `skodun doctor`, `skodun providers` (shell).

### Restart MCP after upgrade / missing tools

stdio MCP does **not** hot-reload. After `pip install` / upgrade / env change,
or if tools are missing / `store schema … newer than this skodun`:

1. Restart the host’s **skodun MCP** connection (or the whole agent session).
2. Confirm `skodun --version` and that tools/list includes all 13 names.
3. Prefer MCP tools again — do **not** permanently fall back to CLI for the loop.

Operator detail: [`mcp-server-config.md`](mcp-server-config.md).

### Loop

1. Finish edits; freeze the tree.
2. Call `gate` with `repo`. If status `0`, **stop** (already covered).
3. Call `review` **once** with `repo`, normally with **no `reviewer`**. Do not
   start a second `review` while one is in flight (refused: `review already in
   flight`).
4. Summarize findings for a human. Do **not** dismiss/defer unless the human
   decided.
5. Call `gate` again. **Stop at 0** — not at “reviewer found nothing.”

### Which reviewer runs (omit `reviewer` by default)

When the operator has `[routing] mode = "auto"`, **omitting `reviewer` lets
skodun pick a finder whose provider has a free slot** instead of piling onto a
busy one. Pinning by habit defeats that for every other agent on the machine.

- **Omit `reviewer`** — the normal case.
- **Pin `reviewer`** only for a deliberate second opinion, or a provider you
  know is healthy when skodun does not. A pin is absolute in every mode.
- **`client_family`** (optional, e.g. `"xai"` if you are Grok) asks for a
  finder from a *different* model family when one is equally free. Soft: it
  never blocks a review. Most hosts can skip it — skodun already guesses from
  the MCP handshake, and `SKODUN_CLIENT_FAMILY` covers the rest.

With routing `off` (the default), omitting `reviewer` simply uses the
configured finder, exactly as before.

### Escalation (do not iterate forever)

Stop and ask a human when a round raises a must-fix finding in code the
previous fix wrote, when the fix would exceed the change under review, or when
you disagree with a finding.

### Must not

- Never treat `surface` as coverage of the current tree.
- Never invent a second review system if skodun is the configured backend.
- Never poll every 30–60s with a full agent turn — wait outside the model, then
  call `review_status` / `gate` / `log`.
- Never replace MCP with shell `skodun review` just because tools look
  incomplete or schema-behind — **restart MCP** first.
