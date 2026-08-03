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
# Daily ceiling (UTC day), NOT lifetime — resets every UTC midnight (default 10):
export SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY=10
# alias (same meaning): SKODUN_OPENAI_API_SPEND_LIMIT_USD=10

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
        "SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY": "10"
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

### When the host does not expand `${VARS}`

Some hosts (Claude Code among them) take the `env` block as **literals** — a
`"${OPENAI_API_KEY}"` there is passed through verbatim and skodun reports
`missing api key`. That leaves a bad choice: paste the key into a config file
that is plaintext on disk and often synced, or go without.

There is a third option — point `command` at a launcher that exports the key
and execs skodun. The key stays in whatever file already holds your secrets:

```sh
#!/bin/sh
# ~/.local/bin/skodun-with-secrets   (chmod 700)
set -u
SECRETS="${SKODUN_SECRETS_FILE:-$HOME/.secrets/.env}"
if [ -z "${SKODUN_OPENAI_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] \
        && [ -r "$SECRETS" ]; then
    key=$(sed -n 's/^SKODUN_OPENAI_API_KEY=//p' "$SECRETS" | head -n 1)
    [ -n "$key" ] && export SKODUN_OPENAI_API_KEY="$key"
    unset key
fi
exec skodun "$@"
```

```json
"skodun": { "type": "stdio",
            "command": "/absolute/path/to/skodun-with-secrets",
            "args": ["mcp"], "env": {} }
```

**Extract the one variable; do not source the file.** A secrets file usually
holds credentials for unrelated systems — databases, cloud accounts — and
skodun spawns third-party provider CLIs that inherit its environment. Sourcing
the whole file hands every one of those secrets to every model subprocess.
`SKODUN_OPENAI_API_KEY` is the only one that is skodun's business.

The same launcher is the right home for the spend ceiling, since it applies
however skodun was started:

```sh
SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY="${SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY:-5}"
export SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY
```

A shell rc is NOT equivalent for that: `~/.zshrc` is sourced only by
interactive shells, so a script, a CI step or an agent tool running `skodun`
in a non-interactive shell silently gets the default ceiling instead. Use
`~/.zshenv` (or the launcher) if you want one number everywhere.

After changing MCP env — or the launcher — **restart** the MCP connection
(stdio servers do not hot-reload env).

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
- **Per UTC day** ceiling per provider (default **$10**), via
  `SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY` (or alias without `_PER_DAY`)
- **Not a lifetime total** — the counter resets at UTC midnight; you only raise
  the limit if a *single day* needs more headroom
- At the daily cap, `openai-api` is skipped so the chain can hop

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
