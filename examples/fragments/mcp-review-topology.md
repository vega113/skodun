# Fragment: MCP server, review requests, repos, worktrees

Paste into operator runbooks or AGENTS.md when agents/humans confuse **MCP
process**, **repository**, and **worktree**. This is product behaviour on
current skodun — not a wish list.

**Related:** [`mcp-loop.md`](mcp-loop.md) (agent loop),
[`mcp-server-config.md`](mcp-server-config.md) (how to start MCP),
[`concurrency.md`](concurrency.md) (capacity knobs),
[`../../docs/integrate-external-project.md`](../../docs/integrate-external-project.md).

---

## Vocabulary

| Term | Means in skodun |
|---|---|
| **MCP server / process** | One long-lived `skodun mcp` (stdio JSON-RPC). Spawned by the host (Claude Code, Cursor, …). No network port. |
| **MCP session** | That connection. Closing it cancels the in-flight MCP `review` (if any). |
| **`review` (tool / CLI)** | One request to run the **foreground** pipeline on **one worktree’s** current outgoing change. Same pipeline as `skodun review`. |
| **`repo` argument** | Path **inside** a git worktree. Absent → MCP process **cwd**. Wrong type/blank → refused (not remapped to cwd). Prefer **absolute** paths. |
| **Worktree** | One checkout directory (primary or linked). Reviews always attach to a worktree’s content. |
| **Repository / git identity** | `git_common_dir` — shared by all worktrees of the same clone. |
| **Store** | SQLite DB of reviews/capacity (default under home, or `SKODUN_DB`). Usually **one per machine**, not one per MCP. |
| **refuse-if-busy** | Second concurrent MCP `review` in the **same process** → tool error `"review already in flight"` (**not** queued). |

---

## What an MCP server is (and is not)

```text
Host agent  ──stdio──►  skodun mcp process  ──►  services.py  ──►  run_review
```

| Is | Is not |
|---|---|
| A **transport** over the same services as the CLI | A per-repo daemon |
| Able to act on **any** worktree you pass as `repo` | Bound to one repository for its lifetime |
| **One** in-flight `review` tool call per process | A multi-review queue or job scheduler |
| Shares the machine store with CLI / other MCPs | Its own isolated capacity universe (except refuse-if-busy) |

Tools are the curated review loop (`gate`, `review`, `log`, `surface`, triage,
status/cancel, …). Shell ops stay CLI-only (`doctor`, `providers`, hooks, …).

---

## What a “request to review” is

| Surface | Call | Pipeline |
|---|---|---|
| MCP | tool `review` `{ "repo"?: path, "reviewer"?: name }` | `svc_review` → `run_review` |
| CLI | `skodun review --repo PATH` | `run_review` |

Always:

1. Resolve **which worktree** (`repo` or cwd).  
2. Capture **that worktree’s** outgoing diff (exact content identity).  
3. Admit capacity / locks (see concurrency fragment).  
4. Run the finder chain (provider CLIs).  
5. Persist a record in the **shared store**.

A review is “cover **this tree’s current bytes**,” not “queue a logical repo forever.”

---

## One MCP process × concurrent `review` calls

```text
review #1 running  → OK
review #2 while #1 busy → refused: "review already in flight"
```

| | |
|---|---|
| Limit | **1** in-flight `review` **per MCP process** |
| Queue? | **No** (stale-tree risk if deferred) |
| Multi-repo / multi-worktree? | Still **one at a time** in that process |
| Parallel MCP reviews | Need **separate MCP processes** (or CLI processes) |

---

## Several repositories

One MCP can review **different repos sequentially** by changing `repo`:

```text
review(repo="/work/projA-wt1")   → finishes
review(repo="/work/projB")      → OK next
```

| Concern | Scope |
|---|---|
| Which tree | Per call (`repo` / cwd) |
| Store | Usually one DB for all repos |
| MCP concurrency | Still 1 `review` per process |
| `review-fg` capacity | **Per `git_common_dir`** (repo A and B are separate pools) |
| `provider:<id>` slots | **Store-wide** (all repos compete for the same provider pool) |

---

## One repository × multiple worktrees

```text
/repos/proj/.git              ← git_common_dir (shared)
/repos/proj                   ← primary (often refused for review)
/repos/proj-wt-agent1         ← linked worktree
/repos/proj-wt-agent2         ← linked worktree
```

| Concern | Behavior |
|---|---|
| Review target | Always **one** worktree’s diff (agent1 ≠ agent2 content) |
| Gate / identity | Exact content hash of **that** worktree |
| `review-fg` | **Shared** across all worktrees of this repo (`scope = git_common_dir`) |
| Legacy FG lock | Under common dir when dual-hold is on (repo-wide mutex) |
| One shared MCP | Still only **one** `review` at a time, even for different worktrees |

**Intended multi-agent pattern:** each agent has its **own worktree** and preferably its **own MCP process** (or uses CLI). Then capacity knobs can allow more than one FG review for that repo when multi-slot is enabled — see [`concurrency.md`](concurrency.md).

---

## Scenarios

| Setup | Result |
|---|---|
| One agent, one MCP, one worktree | One `review` runs; second while busy → refused |
| One agent, one MCP, switches worktrees/repos | Sequential OK; never two at once in that process |
| Two agents, two worktrees, **same** MCP | Second agent refused while first `review` runs |
| Two agents, two worktrees, **two** MCPs (or CLIs), same repo | Both may run if `review-fg` / provider capacity allow (shared FG pool for the repo) |
| Two agents, two repos, two MCPs | Separate FG pools; provider slots still shared if same provider |

---

## Capacity (one paragraph — details in concurrency.md)

Three **different** limits:

1. **MCP process** — refuse-if-busy (1 `review`). Not a capacity env knob.  
2. **`review-fg`** — env `SKODUN_REVIEW_FG_CAPACITY`, counted **per repository** (`git_common_dir`). Multi-slot needs `SKODUN_LEGACY_FG_LOCK=0`.  
3. **`provider:<id>`** — env `SKODUN_PROVIDER_MAX_IN_FLIGHT`, counted **per provider across the whole store** (all repos).

“3 providers × 2 slots = 6 concurrent reviews” is **not** how skodun multiplies concurrency. See [`concurrency.md`](concurrency.md).

---

## Agent musts

- Pass **absolute** `repo` on every `gate` / `review` / `log` / `surface`.  
- Do **not** start a second MCP `review` while one is in flight on the same server.  
- For parallel agents: **separate worktrees** + **separate MCP processes** (or CLI), then respect capacity.  
- Prefer `review_status` / `review_cancel` over abandon or 30–60s poll loops.
