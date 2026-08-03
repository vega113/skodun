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
SECRETS="${SKODUN_SECRETS_FILE:-${HOME:-}/.secrets/.env}"
# ABSOLUTE, for the same reason `command` below must be: the host that spawns
# this often has a minimal PATH, and `exec skodun` would reintroduce exactly
# the dependency the absolute `command` was there to avoid.
SKODUN_BIN="${SKODUN_BIN:-${HOME:-}/.local/bin/skodun}"
# APPENDED, never prepended: a host may spawn this with a minimal PATH -- the
# same reason SKODUN_BIN is absolute below -- and `sed`/`head` read the key with
# no `set -e` to catch their absence, so a missing utility would yield an empty
# key silently. Appending guarantees they resolve without changing precedence
# for anything already on PATH; prepending could shadow a provider CLI that
# skodun goes on to spawn by bare name.
# `:+` not `:-`: an EMPTY element in PATH means the current directory, so
# `"${PATH:-}:/usr/bin"` on a stripped environment would silently put CWD on
# the search path of every provider CLI skodun spawns.
PATH="${PATH:+$PATH:}/usr/bin:/bin"
export PATH

# An unexpanded "${OPENAI_API_KEY}" -- what a host that does not expand its env
# block leaves behind -- is NOT a key, and it is not merely useless: skodun
# reads OPENAI_API_KEY BEFORE the alias, so a leftover literal would beat the
# real key this script exports. Cleared, per variable, before anything else
# looks at them.
case "${OPENAI_API_KEY:-}" in '${'*) unset OPENAI_API_KEY ;; esac
case "${SKODUN_OPENAI_API_KEY:-}" in '${'*) unset SKODUN_OPENAI_API_KEY ;; esac

# Only consult the file when neither variable survived that.
# `-f` as well as `-r`: a readable DIRECTORY passes `-r`, extraction then yields
# an empty key, and skodun execs without a secret -- auth failing with no hint
# that the path was never a file.
if [ -z "${SKODUN_OPENAI_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] \
        && [ -f "$SECRETS" ] && [ -r "$SECRETS" ]; then
    # BOTH documented names -- a secrets file may well use the preferred
    # OPENAI_API_KEY, and looking only for the alias exports nothing.
    for name in SKODUN_OPENAI_API_KEY OPENAI_API_KEY; do
        key=$(sed -n "s/^${name}=//p" "$SECRETS" | head -n 1 \
              | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
        [ -n "$key" ] && export SKODUN_OPENAI_API_KEY="$key" && break
    done
    unset key name
fi
exec "$SKODUN_BIN" "$@"
```

```json
"skodun": { "type": "stdio",
            "command": "/absolute/path/to/skodun-with-secrets",
            "args": ["mcp"], "env": {} }
```

**Leave the MCP `env` block empty.** Migrating from the `"${OPENAI_API_KEY}"`
recipe above? Delete that entry. On a host that does not expand it the literal
is still *non-empty*, so a launcher checking only for emptiness would skip the
secrets file and hand skodun a useless value — auth fails with the real key
sitting unread. The gate above treats a leftover `${...}` as unset for exactly
that reason, but removing the entry is clearer than relying on the guard.

**Extract the one variable; do not source the file.** A secrets file usually
holds credentials for unrelated systems — databases, cloud accounts — and
skodun spawns third-party provider CLIs that inherit its environment. Sourcing
the whole file hands every one of those secrets to every model subprocess.
`SKODUN_OPENAI_API_KEY` is the only one that is skodun's business.

The same launcher is the right home for the spend ceiling, since it applies
however skodun was started. **These two lines go in the script above**, on the
line before `exec` -- not in a file of their own, and not in the MCP `env`
block, which the launcher deliberately leaves empty:

```sh
# ...key extraction above...

# Your ceiling. `:-` so an explicit value from the caller still wins. Pick the
# number deliberately -- skodun's own default is 10, so a launcher that sets
# anything else means `skodun` started through it and `skodun` started directly
# do not agree.
case "${SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY:-}" in
    '\${'*) unset SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY ;;
esac
SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY="${SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY:-10}"
export SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY

exec "$SKODUN_BIN" "$@"
```

**What that extraction handles:** a plain `NAME=value` line, optionally wrapped
in single or double quotes. It is not a dotenv parser -- a trailing inline
comment (`NAME=value # note`) ends up in the key and authentication fails with
the secret sitting right there in the file. Keep the line plain, or extend the
`sed` if your file needs more.

A shell rc is not equivalent, and neither is a shell *env* file. Know what each
one actually covers before relying on it:

| Where | Applies to |
|---|---|
| the launcher | every `skodun` started through it, **whatever the shell, or none** |
| `~/.zshenv` | every **zsh**, interactive or not — but not bash, and not a process spawned directly |
| `~/.zshrc` | **interactive zsh only** — a script, a CI step or an agent tool silently gets the default ceiling |

Only the launcher is shell-independent, because it sets the variable itself
rather than relying on something having sourced a file first. A shell file is a
useful complement for `skodun` typed at a prompt; it is not a substitute.

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
3. Get the key into the skodun **process**: the user shell for CLI use, and for
   MCP either an `env` block your host really expands **or** the launcher above
   with `env: {}`. Do not leave a half-migrated `"${OPENAI_API_KEY}"` behind —
   see the launcher section for why a leftover literal is worse than nothing.
4. Restart MCP; call `review` with absolute `repo` + `reviewer` name.
5. Watch spend notes on stderr / store; raise or lower the daily limit as needed.
