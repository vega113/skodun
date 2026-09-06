# Integrate skodun into an external project (CLI + MCP)

This guide is for **client repositories** (TubeScribes, app monorepos, etc.) that
want skodun as the **local review backend**. skodun is a separate install; the
client needs the CLI on `PATH` (or an absolute command), optional MCP wiring,
config, and agent instructions.

**Related product epics (post #23):**

- Status + cancel — **S1** [#41](https://github.com/vega113/skodun/issues/41)
- Fair review capacity — **S3** [#42](https://github.com/vega113/skodun/issues/42)
- Multi-slot FG + per-provider concurrency — **S4** [#56](https://github.com/vega113/skodun/issues/56)

S1, S3, and S4 are **shipped** — concurrency rules below match current product
behaviour.

| More detail | Path |
|---|---|
| Full agent template | [`examples/AGENTS.md`](../examples/AGENTS.md) |
| Pasteable fragments | [`examples/fragments/`](../examples/fragments/) |
| MCP deep dive (tools, upgrade) | [README — MCP server](../README.md#mcp-server) |
| Epic seeds | [`docs/epics/`](epics/) |
| Legacy → skodun cutover | [`cutover-from-legacy-review.md`](cutover-from-legacy-review.md) |

---

For consistent CLI/MCP capacity and installed-build checks across clients, see
[the shared host profile](shared-host-profile.md).

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
- At least one configured provider: subscription CLIs (`grok`, `codex`, `agy`,
  and/or `junie` on macOS) and/or optional metered **`openai-api`** with
  `OPENAI_API_KEY` — see `skodun providers` and
  [`examples/fragments/openai-api.md`](../examples/fragments/openai-api.md).

### From a skodun source checkout

```bash
cd /path/to/skodun
python3 -m pip install -e .          # installs the `skodun` console script
skodun --version                     # should match pyproject.toml (e.g. 0.4.0)
```

Without an install, you can still run for many commands:

```bash
cd /path/to/skodun
PYTHONPATH=src python3 -m skodun --version
# MCP from source:
#   claude mcp add skodun -- python3 -m skodun mcp
# with cwd/env so `python3 -m skodun` resolves (see README MCP section).
```

**Prefer `pip install -e .` (or pipx) for real reviews**, especially **junie**.
The junie path spawns an isolated Python child (`python -I`); that mode ignores
ambient `PYTHONPATH`. skodun re-injects its package root into that child, but
hosts that pin another interpreter still need skodun **installed** on that
interpreter. Instant junie failures with “no parseable review” almost always
mean install/import mismatch — see
[`examples/fragments/review-troubleshooting.md`](../examples/fragments/review-troubleshooting.md).

There is no requirement that skodun live *inside* the client monorepo.

### Verify against the client tree

```bash
skodun doctor --repo /path/to/your/project
skodun providers --repo /path/to/your/project
```

`doctor` is read-only. Fix missing binaries / config before expecting `review`
to succeed. **`providers` lists adapters** (xai/openai/google/junie), not your
named `[[reviewers]]` table — configure reviewers in TOML; pass
`--reviewer <name>` / MCP `reviewer` to select one, or omit it and let
`[routing] mode = "auto"` pick a finder with a free provider slot.

### Config

| Layer | Path |
|---|---|
| Global | `~/.config/skodun/config.toml` or `SKODUN_CONFIG` |
| Project | `<project>/.skodun.toml` (wins per-key over global) |

Minimal shape (edit models to what your CLIs actually serve):

```toml
[[reviewers]]
name = "finder"
provider = "xai"          # or openai | openai-api | google | junie
model = "grok-4.20-0309-reasoning"
role = "finder"
effort = "medium"
```

Worked examples: `examples/multi-provider.toml`,
`examples/scala-angular-monorepo.toml`.

### Optional: OpenAI HTTP API (client brings their own key)

Use when you want metered Chat Completions instead of (or as fallback to) the
Codex subscription CLI. Full fragment:
[`examples/fragments/openai-api.md`](../examples/fragments/openai-api.md).

| Piece | Where |
|---|---|
| Reviewer | TOML `provider = "openai-api"`, any OpenAI **model** id |
| API key | Process env only: `OPENAI_API_KEY` or `SKODUN_OPENAI_API_KEY` — **never** TOML/git |
| Daily spend cap | Env `SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY` (default **$10 per UTC day**, not lifetime) |
| MCP | Put the key (and optional daily cap) in the MCP server `env` block; restart MCP |

```toml
[[reviewers]]
name     = "finder-openai-api"
provider = "openai-api"
model    = "gpt-5.6-luna"
effort   = "medium"
role     = "finder"
```

```bash
export OPENAI_API_KEY=sk-...   # client secret; not committed
skodun review --repo /abs/worktree --reviewer finder-openai-api
```

MCP hosts that do not inherit the user env must inject the key (secret expansion
preferred over a committed literal) — see
[`examples/fragments/mcp-server-config.md`](../examples/fragments/mcp-server-config.md).

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
Topology (MCP vs repo vs worktree):
[`examples/fragments/mcp-review-topology.md`](../examples/fragments/mcp-review-topology.md).  
Metered OpenAI BYOK (API key in MCP `env`):
[`examples/fragments/openai-api.md`](../examples/fragments/openai-api.md).

### What an MCP server is

A skodun MCP server is **one long-lived stdio process** (`skodun mcp`):

| Property | Behaviour |
|---|---|
| Transport | stdin/stdout JSON-RPC only (no network port) |
| Role | Transport over the **same** `services` path as the CLI |
| Lifetime | One host MCP session/connection; restart client → new process |
| Default cwd | Whatever the host set when spawning the server |
| Store | Shared SQLite (default under home, or `SKODUN_DB`) — usually one per machine |

It is **not** a per-repository daemon and **not** a multi-review job queue.

### `repo` argument (easy to get wrong)

Most tools accept optional `repo`: a path **inside** the git worktree.

- **Absent** → skodun uses the **MCP server process cwd** (often *not* your
  project if the client started the server elsewhere).
- **Wrong type** (array, number, blank) → refused (`repo must be a path…`),
  never silently remapped to cwd.
- **Best practice for external projects:** always pass an absolute project root
  (or linked **worktree** root) as `repo` on `gate`, `review`, `log`, and
  `surface`.

Each `review` / `gate` call selects **one worktree’s** current tree for that
call. The same MCP process may target **different repos or worktrees on later
calls** by changing `repo`.

### Review requests: one process, many trees, one at a time

```text
Host agent  →  tools/call review { repo? }  →  skodun mcp
                 │
                 ├─ if a review is already running in this process
                 │     → refuse: "review already in flight"  (not queued)
                 └─ else → svc_review → run_review(that worktree)
```

| Situation | What happens |
|---|---|
| Second `review` while first is in flight (**same** MCP process) | Refused immediately |
| Sequential `review` for repo A, then repo B (same process) | OK |
| Two agents, two worktrees, **one** shared MCP | Second agent refused while first runs |
| Two agents, two worktrees, **two** MCP processes (or CLI) | Both may run if capacity allows |

**Linked worktrees** of the same clone share one `git_common_dir`. Reviews always
cover **that worktree’s** diff identity; FG capacity for those worktrees is
**pooled per repository** (see concurrency below). Full scenario table:
[`mcp-review-topology.md`](../examples/fragments/mcp-review-topology.md).

### Tools (today) — 13 tools, 2 prompts

| MCP tool | CLI analogue | Notes |
|---|---|---|
| `gate` | `skodun gate` | Status 0/1/2 as CLI |
| `review` | `skodun review` | Long-running; optional `reviewer` **name** (not provider id) |
| `log` | `skodun log` | Optional `branch`, `limit` |
| `surface` | `skodun surface` | History only — **not** a gate |
| `review_status` | `skodun review-status` | Lifecycle observe; not a gate |
| `review_cancel` | `skodun review-cancel` | Cancel in-flight by id |
| `triage_list` | `triage --list` | Needs `review_id` |
| `triage_dismiss` | default triage dismiss | `review_id`, `index`, `reason` — **human** gate decision |
| `triage_defer` | `triage --defer` | + mandatory `tracking_ref` |
| `triage_reopen` | `triage --reopen` | |
| `adopt_refuter` | `triage --adopt-refuter` | |
| `feedback_add` | `skodun feedback add` | Non-gate agent/human judgment or product bug note |
| `feedback_list` | `skodun feedback list` | Inspect feedback for later issues |

**Not** MCP tools (shell / human ops): `doctor`, `providers`, `retain`,
`schedule`, `install-hooks`, `dispatch`, `worker`, `import-legacy`,
`shadow-compare`, bulk triage, `deferrals`.

Prompts: `review-now`, `gate-check` (static policy text).

### After upgrade — restart every skodun MCP session

A running MCP process keeps the **old** Python build (and the **old tool
list**) until the client restarts it. After upgrading skodun (or whenever
agents see fewer than **13** tools, or `store schema … newer than this
skodun`):

1. **Restart** each host’s `skodun mcp` connection / agent session (graceful
   reload preferred; avoid `kill -9` mid-review).
2. Confirm CLI: `skodun --version` and `skodun doctor --repo <project>`.
3. Confirm MCP: `serverInfo.version` from `initialize` matches the CLI; host
   `tools/list` names all **13** tools in the table above (not an old 9-tool
   snapshot).

Smoke initialize (optional):

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  | skodun mcp
```

Do **not** treat a short tool list or schema-behind as “prefer CLI forever” —
restart MCP so agents keep using the intended transport.

### Concurrency (agents must know)

Do not conflate layers (see also
[`examples/fragments/concurrency.md`](../examples/fragments/concurrency.md) and
[`mcp-review-topology.md`](../examples/fragments/mcp-review-topology.md)):

| Layer | Scope | Limit |
|---|---|---|
| MCP process | One `skodun mcp` | **1** in-flight `review` (refuse-if-busy) |
| `review-fg` | Per repository (`git_common_dir`; all worktrees share it) | `SKODUN_REVIEW_FG_CAPACITY` (default 1) |
| `provider:<id>` | Whole store (all repos) | `SKODUN_PROVIDER_MAX_IN_FLIGHT` (default 1) |

**Which provider an un-pinned review joins** is a separate question from how
many may run: by default it is always the first enabled `finder`. Set
`[routing] mode = "auto"` (or `SKODUN_ROUTING_MODE=auto`) to have skodun pick a
finder with a free slot instead. A pin (`--reviewer` / MCP `reviewer`) still
wins, and agents should **omit** it so routing has something to choose. Full
knobs and scoring: [`examples/fragments/concurrency.md`](../examples/fragments/concurrency.md).

1. **CLI foreground reviews:** FIFO **review-fg** capacity (default **1** per
   repository). Default **dual-hold** also takes the legacy
   `grok-reviews-foreground.lock` (effective single physical mutex while
   tubescribes/legacy scripts coexist). Waiters are ordered; progress reports
   **queue position**, **remaining wait budget**, and **ETA** when enough
   samples exist; bounded wait then exit **`3`**. Raise capacity with
   `SKODUN_REVIEW_FG_CAPACITY`. For **true multi-slot** after legacy is gone:
   `SKODUN_LEGACY_FG_LOCK=0` (exact `0` only) plus capacity ≥2. Telemetry is
   persisted (`capacity_admissions`). Env is a global default; **counting** is
   still per repo.
2. **Provider concurrency (S4):** each chain entry acquires `provider:<id>`
   (default max_in_flight **1**, override `SKODUN_PROVIDER_MAX_IN_FLIGHT`)
   before inference and releases on every terminal. Quota/429 marks
   `provider_state` and shrinks that provider to effective **0** slots for the
   TTL; the chain hops or fails closed (never a silent trustworthy pass).
   Provider pools are **store-wide**, not per MCP and not per worktree.
3. **MCP `review`:** **One per server process.** A second call returns
   `"review already in flight"` — **not** queued (S3 policy: a queue would run
   against a moved tree). Multiple repos/worktrees on one MCP are fine
   **sequentially**; parallel agents need **separate** MCP processes or CLI.
4. **Status / cancel (S1):** `skodun review-status` / `skodun review-cancel`
   and MCP `review_status` / `review_cancel` observe or stop in-flight work
   without a second gate. Closing the MCP session still cancels the in-flight
   MCP review.
5. **Do not poll** with full agent turns every 30–60s. Wait outside the model,
   then call `review-status` / `gate` / `log` / `surface`.
6. Providers are a **fallback chain**, not parallel voting on one diff. If the
   **entire** finder chain is known unavailable via `provider_state`, the run
   fails fast (exit 2) without burning the full admission wait.
7. **Skeptic on a clean finder** uses the selected finder entry and its fallback
   chain. If that chain is on quota, the extra pass **demotes** an otherwise
   clean review. The `role = "refuter"` provider is separate and annotation-only;
   its outage does not demote the review. Temporarily set
   `SKODUN_SKEPTIC_PASS=0` when the selected finder chain is unavailable. Details:
   [`review-troubleshooting.md`](../examples/fragments/review-troubleshooting.md).

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
| [`examples/fragments/mcp-review-topology.md`](../examples/fragments/mcp-review-topology.md) | MCP process vs repo vs worktree |
| [`examples/fragments/concurrency.md`](../examples/fragments/concurrency.md) | Multi-agent / multi-provider capacity |
| [`examples/fragments/mcp-server-config.md`](../examples/fragments/mcp-server-config.md) | Operator MCP JSON |
| [`examples/fragments/feedback.md`](../examples/fragments/feedback.md) | Agent judgment + product-bug feedback (non-gate) |
| [`examples/fragments/review-troubleshooting.md`](../examples/fragments/review-troubleshooting.md) | Failed reviews, junie, skeptic/quota, reviewers vs providers |
| [`examples/fragments/openai-api.md`](../examples/fragments/openai-api.md) | Metered OpenAI HTTP: BYOK API key, MCP env, daily spend |

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
| Tool list matches `EXPECTED_TOOLS` | **13 tools** + 2 prompts (incl. status/cancel + feedback) |
| MCP busy string | `"review already in flight"` |
| Gate 0 meaning | clean **or** all findings triaged |
| `repo` optional, defaults to server cwd | documented; recommend absolute path |
| `reviewer` is config **name**, not provider id | documented |
| Console script + `python3 -m skodun` | both documented |
| Upgrade / missing tools → restart MCP sessions | documented (README + mcp-server-config + mcp-loop) |
| CLI-only ops list | doctor, providers, retain, schedule, install-hooks, … |
| Links to #41 / #42 / #56 | present |
| MCP topology fragment | `examples/fragments/mcp-review-topology.md` |

S1, S3, and S4 are shipped: §2 concurrency,
`examples/fragments/concurrency.md`, and
`examples/fragments/mcp-review-topology.md` match the product (FIFO review-fg,
optional multi-slot, provider max_in_flight, MCP refuse-if-busy; MCP process ≠
per-repo queue).
