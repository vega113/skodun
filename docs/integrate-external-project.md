# Integrate skodun into an external project (CLI + MCP)

This guide is for **client repositories** (TubeScribes, app monorepos, etc.) that
want skodun as the **local review backend**. skodun is a separate install; the
client needs the CLI on `PATH` (or an absolute command), optional MCP wiring,
config, and agent instructions.

**Related product epics (post #23):**

- Status + cancel — **S1** [#41](https://github.com/vega113/skodun/issues/41)
- Fair review capacity — **S3** [#42](https://github.com/vega113/skodun/issues/42)

Until those land, treat concurrency rules below as **current product behaviour**.

| More detail | Path |
|---|---|
| Full agent template | [`examples/AGENTS.md`](../examples/AGENTS.md) |
| Pasteable fragments | [`examples/fragments/`](../examples/fragments/) |
| MCP deep dive (tools, upgrade) | [README — MCP server](../README.md#mcp-server) |
| Epic seeds | [`docs/epics/`](epics/) |

---

## What skodun is (and is not) for a client

| Is | Is not |
|---|---|
| Review + triage + gate on **exact diff identity** | Your full test suite / CI matrix |
| Provider-neutral (configured adapters: grok, codex, agy, junie, …) | Hardcoded “must be Grok” |
| CLI and stdio MCP with the **same** service path (`services.py`) | A multi-agent OS or host job scheduler |
| Optional background pre-push + later `surface` | A push that waits for model inference |

**`skodun gate` exit `0`** means: a trustworthy review covers this exact tree
**and** there are no open findings (clean **or** all findings triaged with an
audited reason). Keep expensive certification (backend/frontend/e2e) in the
**client** gate/CI.

---

## 1. Prerequisites and install

- **Python ≥ 3.12** (runtime is **stdlib-only**; pytest is dev-only).
- At least one configured provider CLI installed and authenticated (`grok`,
  `codex`, `agy`, and/or `junie` on macOS — see `skodun providers`).

### From a skodun source checkout

```bash
cd /path/to/skodun
python3 -m pip install -e .          # installs the `skodun` console script
skodun --version                     # should match pyproject.toml (e.g. 0.4.0)
```

Without an install, you can still run:

```bash
cd /path/to/skodun
PYTHONPATH=src python3 -m skodun --version
# MCP from source:
#   claude mcp add skodun -- python3 -m skodun mcp
# with cwd/env so `python3 -m skodun` resolves (see README MCP section).
```

There is no requirement that skodun live *inside* the client monorepo.

### Verify against the client tree

```bash
skodun doctor --repo /path/to/your/project
skodun providers --repo /path/to/your/project
```

`doctor` is read-only. Fix missing binaries / config before expecting `review`
to succeed.

### Config

| Layer | Path |
|---|---|
| Global | `~/.config/skodun/config.toml` or `SKODUN_CONFIG` |
| Project | `<project>/.skodun.toml` (wins per-key over global) |

Minimal shape (edit models to what your CLIs actually serve):

```toml
[[reviewers]]
name = "finder"
provider = "xai"          # or openai | google | junie
model = "grok-4.20-0309-reasoning"
role = "finder"
effort = "medium"
```

Worked examples: `examples/multi-provider.toml`,
`examples/scala-angular-monorepo.toml`.

### Store

| | |
|---|---|
| Default | under XDG, typically `~/.local/share/skodun/skodun.db` |
| Override | `SKODUN_DB=/absolute/path/to.db` |

Prefer **one durable store per machine or per product**, with reviews scoped by
`repo` (see README “One store per repository…”). **Never** point automated
tests at a shared human store without isolation.

---

## 2. MCP wiring

skodun serves **stdio MCP** only: `skodun mcp` (no network port, no SDK).

### Claude Code (quick)

```bash
claude mcp add skodun -- skodun mcp
# source checkout without console script:
# claude mcp add skodun -- python3 -m skodun mcp
```

### Project `.mcp.json` / JSON-shaped hosts

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

Optional project-local store (use a **real absolute path** if the host does not
expand `${workspaceFolder}`):

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

Operator fragment: [`examples/fragments/mcp-server-config.md`](../examples/fragments/mcp-server-config.md).

### `repo` argument (easy to get wrong)

Most tools accept optional `repo`: a path **inside** the git worktree.

- **Absent** → skodun uses the **MCP server process cwd** (often *not* your
  project if the client started the server elsewhere).
- **Wrong type** (array, number, blank) → refused (`repo must be a path…`),
  never silently remapped to cwd.
- **Best practice for external projects:** always pass an absolute project root
  as `repo` on `gate`, `review`, `log`, and `surface`.

### Tools (today) — 9 tools, 2 prompts

| MCP tool | CLI analogue | Notes |
|---|---|---|
| `gate` | `skodun gate` | Status 0/1/2 as CLI |
| `review` | `skodun review` | Long-running; optional `reviewer` **name** (not provider id) |
| `log` | `skodun log` | Optional `branch`, `limit` |
| `surface` | `skodun surface` | History only — **not** a gate |
| `triage_list` | `triage --list` | Needs `review_id` |
| `triage_dismiss` | default triage dismiss | `review_id`, `index`, `reason` |
| `triage_defer` | `triage --defer` | + mandatory `tracking_ref` |
| `triage_reopen` | `triage --reopen` | |
| `adopt_refuter` | `triage --adopt-refuter` | |

**Not** MCP tools (shell / human ops): `doctor`, `providers`, `retain`,
`schedule`, `install-hooks`, `dispatch`, `worker`, `import-legacy`,
`shadow-compare`, bulk triage, `deferrals`.

Prompts: `review-now`, `gate-check` (static policy text).

### After upgrade

A running MCP process keeps the **old** Python build until the client restarts
it. After upgrading skodun:

1. Restart the MCP connection/session.
2. Confirm version: `skodun --version` and `serverInfo.version` from
   `initialize` (pinned to `pyproject.toml`).

Smoke initialize (optional):

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  | skodun mcp
```

### Concurrency (agents must know)

1. **One foreground review per repository** (repo lock). A second CLI review
   waits, then may exit **`3`**.
2. **One MCP `review` per server process.** A second call returns
   `"review already in flight"` — **not** queued (a queue would run against a
   moved tree).
3. Closing the MCP session **cancels** the in-flight MCP review (today).
   Cross-session cancel-by-id is epic **S1**.
4. **Do not poll** with full agent turns every 30–60s. Wait outside the model,
   then call `gate` / `log` / `surface`.
5. Providers are a **fallback chain**, not parallel slots.

Epic **S3** will add fair capacity + telemetry; update this section when it ships.

---

## 3. Wire the client gate (provider-neutral)

Client pre-push or `ci-local-gate` should treat skodun as coverage of the
**exact working tree** (or the tree you intentionally review):

```bash
ROOT=/path/to/your/project
skodun gate --repo "$ROOT"
# 0 → trustworthy coverage; no open findings (clean or all triaged)
# 1 → findings still open → fix or audited triage, then gate again
# 2 → no trustworthy review for this content → skodun review, then gate
```

**Do not** require a Grok-only log/artifact if skodun already stored a
trustworthy review from junie/codex/agy. That is a **client cutover** defect.

### Optional background pre-push

```bash
skodun install-hooks --repo "$ROOT"   # --force chains a foreign pre-push
# git push returns without waiting for the model; later:
skodun surface --repo "$ROOT"
```

`surface` never certifies the current tree — only `gate` does.

---

## 4. Agent instructions for the client repo

| Artifact | Use when |
|---|---|
| [`examples/AGENTS.md`](../examples/AGENTS.md) | Full template (first paste) |
| [`examples/fragments/mcp-loop.md`](../examples/fragments/mcp-loop.md) | MCP loop only |
| [`examples/fragments/concurrency.md`](../examples/fragments/concurrency.md) | Multi-agent / multi-provider |
| [`examples/fragments/mcp-server-config.md`](../examples/fragments/mcp-server-config.md) | Operator MCP JSON |

Edit bracketed project bits (tracker URL for deferrals, etc.).

---

## 5. Recommended default loop (cost-aware)

```text
freeze the diff
→ gate (if 0: stop — already covered)
→ review once (CLI or MCP; pass absolute repo)
→ human triage only (defer requires a filed tracking ref)
→ gate until 0
```

Optional security/refuter when path-risky or R2 churn marks say the loop is
chasing its own tail. **Not** default: skodun + legacy oracle scripts + every
cloud bot for every low-risk change.

---

## 6. Smoke checklist for a new client

- [ ] Python ≥ 3.12; `skodun --version` matches the intended build  
- [ ] `skodun doctor --repo <project>` usable (config + store + adapters)  
- [ ] `skodun providers --repo <project>` shows intended adapters executable  
- [ ] MCP tools list includes `gate` + `review` (+ triage family)  
- [ ] Agents pass absolute `repo` (or MCP cwd is the project)  
- [ ] AGENTS section present; **stop when `gate` → 0**  
- [ ] Client gate calls `skodun gate` without hardcoding a provider name  
- [ ] (Optional) `install-hooks`; `surface` after a push  
- [ ] After upgrade: MCP client restarted; versions agree  

---

## 7. Out of scope for this integration

- Host-wide fair queue for non-review work (DB suites, Karma, Heroku)  
- TubeScribes cutover of `grok-review-*.sh` (client epic)  
- Anthropic/`claude` adapter (deliberately unshipped; see README)  
- Severity-tier gating or re-review-only-last-delta  
- Changing `gate.py` / `trust.py` semantics  

---

## Self-review notes (maintainers)

Last reviewed against skodun **0.4.x** / post-epic-#23 main:

| Check | Result |
|---|---|
| Tool list matches `default_tools()` | 9 tools + 2 prompts |
| MCP busy string | `"review already in flight"` |
| Gate 0 meaning | clean **or** all findings triaged |
| `repo` optional, defaults to server cwd | documented; recommend absolute path |
| `reviewer` is config **name**, not provider id | documented |
| Console script + `python3 -m skodun` | both documented |
| Upgrade requires MCP restart | documented |
| CLI-only ops list | doctor, providers, retain, schedule, install-hooks, … |
| Links to #41 / #42 | present |

When S1/S3 land, update §2 concurrency and the fragments in the same PR as the
feature.
