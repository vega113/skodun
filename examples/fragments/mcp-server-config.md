# Fragment: MCP server config (operators)

Paste into the client project’s MCP docs or runbook. Adjust the binary path if
`skodun` is not on the default `PATH`.

## Claude Code (CLI)

```bash
claude mcp add skodun -- skodun mcp
# From a skodun source tree without an installed console script:
# claude mcp add skodun -- python3 -m skodun mcp
```

## JSON-shaped hosts (Claude Code `.mcp.json`, Cursor-style, etc.)

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"]
    }
  }
}
```

### Optional: project-local store

Use a **real absolute path** unless your host expands workspace placeholders:

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "SKODUN_DB": "/absolute/path/to/project/.skodun/skodun.db"
      }
    }
  }
}
```

### Optional: bring-your-own OpenAI API key (metered `openai-api`)

skodun does **not** store API keys in TOML. Put the key on the **MCP process**
so tool `review` with `reviewer` → `provider = "openai-api"` can bill that
client’s key (BYOK). Prefer host secret expansion, not a committed literal:

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
        "SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY": "10"
      }
    }
  }
}
```

Alias accepted: `SKODUN_OPENAI_API_KEY` (same value). If the host already
inherits the user environment and `OPENAI_API_KEY` is set there, an explicit
`env` block is optional. **Restart MCP** after changing env.

Reviewer entry still lives in config TOML (`provider = "openai-api"`, any
model id). Full notes: [`openai-api.md`](openai-api.md).

### After installing or upgrading skodun — restart MCP sessions

stdio servers **do not hot-reload** code or env. Long-lived processes keep the
**old** tool list and schema ladder until the host restarts them.

**When to restart every client that runs `skodun mcp`:**

| Trigger | Symptom if you skip restart |
|---|---|
| Upgrade / reinstall skodun | Old code; missing tools; wrong behaviour |
| CLI upgraded store (`SCHEMA_VERSION`) | `store schema vN is newer than this skodun` |
| Changed MCP `command` / `args` / `env` | Old env (e.g. missing `OPENAI_API_KEY`) |
| Host shows &lt; **13** tools | Stale `tools/list` from pre-S1/feedback builds |

**Steps:**

1. **Restart** the MCP connection / agent session (Claude Code: reload MCP or
   window; Cursor: restart MCP; Codex: new run; Grok: new session). Prefer
   graceful reload over `kill -9` mid-review.
2. Point MCP at the **same** `skodun` binary as the shell CLI.
3. `skodun doctor --repo <project>` and `skodun --version`.
4. Confirm **all 13 tools**:
   `gate`, `review`, `log`, `surface`, `review_status`, `review_cancel`,
   `triage_list`, `triage_dismiss`, `adopt_refuter`, `triage_reopen`,
   `triage_defer`, `feedback_add`, `feedback_list`
   (+ prompts `review-now`, `gate-check`).
5. Prefer agents pass absolute `repo` on tool calls (server cwd may differ).
6. For metered reviews: confirm the MCP process can see `OPENAI_API_KEY`
   (or `SKODUN_OPENAI_API_KEY`) without printing it.

Agents must **not** switch to shell `skodun review` as a permanent workaround
for schema-behind or a short tool list — restart MCP instead.

### Topology (operators)

- One `skodun mcp` process is **not** “owned by” one repository: each tool call
  may pass a different absolute `repo` (any worktree).
- That process still allows only **one** in-flight `review` (refuse-if-busy).
- Parallel agents → parallel MCP sessions (or CLI), not a queue inside one
  server. Full definitions: [`mcp-review-topology.md`](mcp-review-topology.md).

### What is *not* an MCP tool

Shell out if needed: `skodun doctor`, `skodun providers`, `skodun retain`,
`skodun schedule install`, `skodun install-hooks`.
