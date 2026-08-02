# Integrate skodun into an external project (CLI + MCP)

This guide is for **client repositories** (e.g. TubeScribes, app monorepos) that
want skodun as the **local review backend**. skodun itself lives in a separate
checkout; the client only needs the CLI on `PATH`, optional MCP wiring, and
agent instructions.

**Related product epics (post #23):**

- Status + cancel (no orphan in-flight reviews) — epic **S1** [#41](https://github.com/vega113/skodun/issues/41)
- Fair review capacity (FIFO admission, telemetry) — epic **S3** [#42](https://github.com/vega113/skodun/issues/42)

Until those land, use the **current** concurrency rules in
[`examples/AGENTS.md`](../examples/AGENTS.md) and the fragments under
[`examples/fragments/`](../examples/fragments/).

---

## What skodun is (and is not) for a client

| Is | Is not |
|---|---|
| Review + triage + gate on **exact diff identity** | Your full test suite / CI matrix |
| Provider-neutral (grok, codex, agy, junie, …) | Hardcoded “must be Grok” |
| CLI and stdio MCP with the **same** service semantics | A multi-agent OS or host job scheduler |
| Optional background pre-push + `surface` later | A push that waits for model inference |

Client projects should treat **`skodun gate` exit 0** as “review coverage OK”
and keep expensive certification (backend/frontend/e2e) in **their** gate/CI.

---

## 1. Install and verify

```bash
# From the skodun checkout (or your packaging path):
pip install -e /path/to/skodun   # or: python -m pip install ...

skodun --version
skodun doctor --repo /path/to/your/project
skodun providers --repo /path/to/your/project
```

Configure reviewers in:

- global: `~/.config/skodun/config.toml` (or `SKODUN_CONFIG`)
- project: `/path/to/your/project/.skodun.toml` (project wins per key)

See `examples/multi-provider.toml` and `examples/scala-angular-monorepo.toml`.

Store location: `SKODUN_DB` or default under XDG (`~/.local/share/skodun/…`).
**Never** point tests or CI at a shared developer store without isolation.

---

## 2. MCP wiring (Claude Code, Cursor, Codex, etc.)

skodun speaks **stdio MCP** (`skodun mcp`). One process per client is normal.

### Claude Code / similar JSON config

```json
{
  "mcpServers": {
    "skodun": {
      "command": "skodun",
      "args": ["mcp"]
    }
  }
}
```

Optional env for a dedicated store:

```json
{
  "mcpServers": {
    "skodun": {
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "SKODUN_DB": "/path/to/project/.skodun/skodun.db"
      }
    }
  }
}
```

Prefer **one store per product** (or per machine with `repo` scoping) so
`surface` / triage stay coherent. See README “One store per repository…”.

### After upgrade

Restart the MCP client. Confirm `skodun --version` matches what the server
reports in initialize/`serverInfo` if your client shows it.

### Tools agents get (today)

| MCP tool | Same idea as CLI |
|---|---|
| `gate` | `skodun gate` |
| `review` | `skodun review` (long-running; **one in flight per server**) |
| `log` | `skodun log` |
| `surface` | `skodun surface` |
| `triage_list` / `triage_dismiss` / `triage_defer` / `triage_reopen` / `adopt_refuter` | triage verbs |

**Not** MCP tools (shell out / human ops): `doctor`, `retain`, `schedule`,
`install-hooks`, `providers`, `dispatch`, bulk triage.

Prompts: `review-now`, `gate-check` (static policy text).

### Current concurrency rules (agents must know these)

1. **One foreground review per repository** (repo lock). A second CLI review
   waits then may exit `3`.
2. **One MCP `review` per server process.** A second call returns
   `"review already in flight"` — **not** a queue.
3. **Do not poll** with full agent turns every 30s. Prefer: start review → work
   elsewhere or wait outside the model → `gate` / `log` / `surface`.
4. **Providers are a fallback chain**, not parallel slots. Prefer one finder
   chain; do not run skodun + oracle scripts + every cloud bot for every change.

Epic **S1** adds first-class `status` / `cancel`. Epic **S3** adds fair capacity
and queue telemetry. Update this section when those ship.

---

## 3. Wire the client gate (provider-neutral)

Client pre-push or `ci-local-gate` should:

```bash
skodun gate --repo "$ROOT"
# 0 → review coverage OK for this exact tree
# 1 → findings open (triage or fix)
# 2 → no trustworthy review (run skodun review, then gate again)
```

**Do not** require a Grok-only artifact if skodun already recorded a trustworthy
review from junie/codex/agy. That is a client cutover bug, not a skodun gate bug.

Background optional:

```bash
skodun install-hooks --repo "$ROOT"   # may need --force to chain existing hook
# push returns; later:
skodun surface --repo "$ROOT"
```

---

## 4. Agent instructions for the client repo

Paste one of:

| Artifact | Use when |
|---|---|
| [`examples/AGENTS.md`](../examples/AGENTS.md) | Full template (recommended first paste) |
| [`examples/fragments/mcp-loop.md`](../examples/fragments/mcp-loop.md) | Project already has AGENTS; add MCP loop only |
| [`examples/fragments/concurrency.md`](../examples/fragments/concurrency.md) | Multi-agent / multi-provider machines |
| [`examples/fragments/mcp-server-config.md`](../examples/fragments/mcp-server-config.md) | Operator doc for MCP JSON only |

Edit bracketed project-specific bits (tracker URL, branch defaults).

---

## 5. Recommended default loop (cost-aware)

```text
freeze the diff
→ skodun gate   (if 0: stop — already covered)
→ skodun review (or MCP review) once
→ triage only with human decisions (defer needs a filed tracking ref)
→ skodun gate until 0
```

Optional security/refuter when path-risky or R2 churn marks say the loop is
chasing its own tail. **Not** default: every local provider + every cloud
reviewer for every low-risk change.

---

## 6. Smoke checklist for a new client

- [ ] `skodun doctor --repo .` clean enough to review  
- [ ] `skodun providers` lists intended adapters; binaries executable  
- [ ] MCP tools list includes `gate` + `review`  
- [ ] Agent AGENTS section present; stopping rule is `gate → 0`  
- [ ] Client gate invokes `skodun gate` without provider name hardcoding  
- [ ] (Optional) `install-hooks`; `surface` after a push  

---

## 7. Out of scope for client integration

- Host-wide fair queue for all work (DB suites, Karma, Heroku) — client gate  
- TubeScribes cutover of `grok-review-*.sh` — separate client epic  
- Anthropic adapter, severity-tier gating — not required for MCP integration  
