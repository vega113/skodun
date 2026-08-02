# Fragment: OpenAI HTTP API (metered) + BYOK for MCP

Optional **metered** path: Chat Completions over HTTPS with **your** API key.
Distinct from `provider = "openai"` (Codex **subscription CLI**).

---

## When to use

| Use | Prefer |
|---|---|
| ChatGPT/Codex subscription CLI | `provider = "openai"` (codex) |
| Pay-per-token API, any OpenAI model id, spend caps | `provider = "openai-api"` |

---

## Bring your own key (BYOK)

The key is read from the **process environment** only:

| Variable | Role |
|---|---|
| `OPENAI_API_KEY` | Standard OpenAI key (preferred) |
| `SKODUN_OPENAI_API_KEY` | Skodun-namespaced alias (handy in MCP `env` blocks) |

**Never** put keys in `.skodun.toml`, git, or agent chat logs.

### Shell / CLI

```bash
export OPENAI_API_KEY=sk-...          # or SKODUN_OPENAI_API_KEY
export SKODUN_OPENAI_API_SPEND_LIMIT_USD=10   # optional; default 10

skodun review --repo /abs/worktree --reviewer finder-openai-api
```

### MCP server config (clients)

The MCP host must inject the key into the **skodun mcp** process. If the host
already inherits your login env, `OPENAI_API_KEY` alone is enough. Otherwise set
`env` on the server entry (use a **host secret / env expansion**, not a committed
literal):

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
        "SKODUN_OPENAI_API_SPEND_LIMIT_USD": "10"
      }
    }
  }
}
```

Some hosts want a skodun-only name:

```json
"env": {
  "SKODUN_OPENAI_API_KEY": "${OPENAI_API_KEY}"
}
```

After changing MCP env: **restart** the MCP connection (stdio servers do not
hot-reload env).

Agent tool call (absolute `repo`):

```json
{
  "name": "review",
  "arguments": {
    "repo": "/absolute/path/to/worktree",
    "reviewer": "finder-openai-api"
  }
}
```

---

## Reviewer config (shared by CLI and MCP)

```toml
# ~/.config/skodun/config.toml and/or <repo>/.skodun.toml
[[reviewers]]
name     = "finder-openai-api"
provider = "openai-api"
model    = "gpt-5.6-luna"   # any model id the OpenAI API accepts
effort   = "medium"
role     = "finder"
# max_cost_usd = 0.50
# fallbacks = ["finder"]    # hop to a CLI reviewer if API is down
```

MCP and CLI both load the same config merge (global + repo). The **key** is only
in the process env of whichever surface is running.

---

## Spend tracking

- Tokens + estimated $ → store `api_spend_events` + `attempts[].usage`
- Daily ceiling per provider (UTC), default **$10** (`SKODUN_OPENAI_API_SPEND_LIMIT_USD`)
- At the cap, `openai-api` is skipped so the chain can hop

Optional rate overrides (USD per 1M tokens):

```bash
export SKODUN_OPENAI_API_INPUT_USD_PER_1M=1.0
export SKODUN_OPENAI_API_OUTPUT_USD_PER_1M=4.0
```

---

## Checklist for client roll-out

1. Install skodun (`pip` / pipx); `skodun providers` lists `openai-api`.
2. Add `[[reviewers]]` with `provider = "openai-api"` and the model you want.
3. Put `OPENAI_API_KEY` (or alias) in **MCP server env** and/or the user shell.
4. Restart MCP; call `review` with absolute `repo` + `reviewer` name.
5. Watch spend notes on stderr / store; raise or lower the daily limit as needed.
