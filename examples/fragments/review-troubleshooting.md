# Fragment: review troubleshooting (operators + agents)

Paste into client ops docs or AGENTS.md when reviews fail in confusing ways.
Long-form: [`../../docs/integrate-external-project.md`](../../docs/integrate-external-project.md).

---

## Quick triage

| Symptom | Likely cause | What to do |
|---|---|---|
| Review ends in **&lt;1s**, `trustworthy=false`, “no parseable review”, junie | Outer process used `PYTHONPATH=src` only; junie runner is `python -I` and could not import `skodun` | **Install** skodun into that Python (`pip install -e .` / pipx). Prefer the `skodun` console script. See § Junie. |
| Finder looks clean, then whole review **untrustworthy**, `extra_passes.skeptic` / refuter failed | Clean finder schedules **skeptic** (uses `role=refuter` provider). That provider **quota**/auth failed and **demoted** the review | Fix that provider, or temporarily `SKODUN_SKEPTIC_PASS=0` and/or `SKODUN_REFUTER_PASS=0` |
| `provider_state` / “marking provider … unavailable until …” | Quota/rate-limit cached in the store | Wait for TTL, use another provider/reviewer, or (debug only) `SKODUN_IGNORE_PROVIDER_STATE` |
| `openai-api` “missing API key” / auth failure | Key not in **this** process env (CLI shell or MCP server `env`) | `export OPENAI_API_KEY=…` or set MCP `env`; never TOML. Alias: `SKODUN_OPENAI_API_KEY`. Restart MCP after changing env. See [`openai-api.md`](openai-api.md). |
| `api daily spend limit reached … this UTC day` | Per-provider **daily** cap hit (default $10/UTC day) | Wait until UTC midnight, or raise `SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY` for that day only — not a lifetime total |
| Refused on **primary checkout** | skodun defaults to linked **worktrees** | `git worktree add …` or `SKODUN_ALLOW_MAIN=1` only if you accept that risk |
| Second MCP `review` while one runs | **refuse-if-busy** (one review per MCP process) | Wait, or second MCP process / CLI; see [`mcp-review-topology.md`](mcp-review-topology.md) |
| `review already in flight` / exit 3 | Capacity or legacy FG lock wait timed out | See [`concurrency.md`](concurrency.md); free slots, raise wait, multi-slot only after dual-hold off |
| Host shows **&lt;13** MCP tools (no `review_status` / `feedback_*`) | **Stale MCP process** after upgrade | **Restart MCP session**; confirm tools/list = 13. See [`mcp-server-config.md`](mcp-server-config.md) |
| `store schema vN is newer than this skodun` | CLI upgraded store; MCP still old build | **Restart MCP** (same install as CLI). Do not abandon MCP for shell review permanently |
| Finder clean then whole review untrustworthy (agy `status: ERROR`) | Often **provider quota** (e.g. Google “Individual quota reached”) | Check `skodun providers`; wait for TTL / upgrade sub; ensure skodun classifies as quota so fallbacks hop |

Diagnostics (CLI only — not MCP tools):

```bash
skodun doctor --repo /abs/path/to/worktree
skodun providers --repo /abs/path/to/worktree
skodun review-status --repo /abs/path/to/worktree
skodun log --repo /abs/path/to/worktree -n 5
```

---

## Install vs `PYTHONPATH` (especially junie)

**Prefer an install** so `skodun` and `sys.executable` share one environment:

```bash
pip install -e /path/to/skodun    # or pipx install / reinstall
skodun --version
skodun doctor --repo /path/to/client
```

Running only:

```bash
PYTHONPATH=src python3 -m skodun review …
```

can work for many providers, but **junie** spawns an isolated child (`python -I …`). Isolated mode **ignores `PYTHONPATH`**. Current skodun re-injects the package import root into that child; still install for MCP/hosts that pin a different Python.

If junie fails instantly with “No module named skodun” (older builds) or empty payload: reinstall skodun into the **same** interpreter that runs `skodun`.

---

## Skeptic / refuter demotion

On a **trustworthy clean** finder (`findings_total == 0`, mode `now`), skodun may run a **skeptic** pass. That pass prefers a configured `role = "refuter"` reviewer (often openai/codex). If that chain is **unavailable** (quota), the extra pass **fails closed** and demotes the primary — even when the finder was clean.

| Env | Effect |
|---|---|
| `SKODUN_SKEPTIC_PASS=0` | Do not run skeptic |
| `SKODUN_REFUTER_PASS=0` | Do not run refuter |
| `SKODUN_SECURITY_PASS=0` | Do not run security pass |

Use these for dogfood when the secondary provider is down; re-enable for full policy.

---

## Reviewers vs providers

| | **Providers** | **Reviewers** |
|---|---|---|
| What | Built-in adapters: `xai`, `openai`, `google`, `junie` | **Your** named `[[reviewers]]` entries |
| List | `skodun providers` | Read TOML; `doctor` shows **count**; bad `--reviewer` refusal lists **names** |
| Custom | Cannot add a new CLI type without a skodun adapter | **Yes** — any name/model/effort on a registered `provider` |

Config merge: `~/.config/skodun/config.toml` (or `SKODUN_CONFIG`) then `<repo>/.skodun.toml` (later wins per field, reviewers merge by `name`).

```toml
[[reviewers]]
name     = "finder-junie-luna"
provider = "junie"
model    = "gpt-5.6-luna"   # must exist for your junie install
effort   = "medium"
role     = "finder"
```

```bash
skodun review --repo /abs/worktree --reviewer finder-junie-luna
# MCP: { "reviewer": "finder-junie-luna", "repo": "/abs/worktree" }
```

---

## Capacity / multi-agent

See [`concurrency.md`](concurrency.md) and [`mcp-review-topology.md`](mcp-review-topology.md). Defaults stay serial; multi-slot needs `SKODUN_LEGACY_FG_LOCK=0` + capacity ≥2. MCP is still one in-flight `review` per process.

---

## Agent judgment without clearing the gate

Do not auto-`triage_dismiss`. Use **feedback** for agent judgment / product bugs: [`feedback.md`](feedback.md).
