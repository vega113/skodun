# Cutover: legacy review scripts → skodun

**Date stamped:** 2026-08-02  
**Skodun readiness:** **superseded 2026-08-02**

---

## Supersession notice

External process audits written before 2026-08-02 (including multi-session
“four gated systems” coordination reports) often list skodun as not ready
because they still assume:

- open junie / R2–R3 / epic #23 work  
- missing doctor, retention, schedule  
- missing status/cancel  
- missing fair `review-fg` capacity  

**Those product items are shipped on skodun `main`.** Do **not** re-open closed
skodun epics (#23, S1 #41, S3 #42) from that class of report. Re-verify
against GitHub and this repo before planning skodun “readiness” work.

What those audits often still get **right** is **client coordination**: dual
review stacks, broad local gates, CI blocked on review convergence, and
agent polling. That work lives in the **client** repository and agent
policy, not in unfinished skodun epics.

| Claim class | Status as of 2026-08-02 |
|---|---|
| Skodun product spine (gate, store, adapters, MCP, dispatch) | Shipped |
| Junie adapter, R2/R3 presentation, doctor/retain/schedule | Shipped (#23 close-out) |
| Status + cancel (CLI/MCP) | Shipped (S1) |
| FIFO review-fg capacity + telemetry + dead-row reclaim | Shipped (S3) |
| Client still on `grok-review-*.sh` / Grok-only gate | Client cutover debt |
| Multi-review + poll loops + broad DB/browser gate | Orchestration / client policy |

---

## What skodun already provides (do not re-build)

- Fail-closed `skodun gate` on exact diff identity  
- Provider-neutral trustworthy reviews (configured finder chain)  
- `skodun review` + optional `--reviewer`; background `dispatch` / `surface`  
- `skodun review-status` / `review-cancel` (+ MCP tools)  
- FIFO `review-fg` admission, queue telemetry, preflight when whole chain is
  known unavailable  
- MCP: one long-running `review` per process; second call **refused** (not
  queued) by design  
- Integrate guide: [`integrate-external-project.md`](integrate-external-project.md)  
- Client agent paste: [`../examples/AGENTS.md`](../examples/AGENTS.md)

---

## Cutover checklist (client)

Work these **in order**. Each step is client-repo + policy unless noted.

### 1. Shadow

- [ ] Install skodun on the client machine; `skodun doctor --repo <client>` green  
- [ ] Configure `.skodun.toml` (or global config) with the intended finder
      fallback chain (not a single hard-coded provider)  
- [ ] On **10–20 real client changes**, run skodun in parallel with the legacy
      path: `skodun review` + `skodun gate`, and optionally
      `skodun shadow-compare` where a legacy archive exists  
- [ ] Record disagreements (gate code, trust axes, finding churn); fix client
      wiring or open a skodun issue only if skodun itself is wrong  
- [ ] Sample capacity telemetry under multi-agent load
      (`capacity_admissions` / wait progress), if contention is a goal metric  

### 2. Provider-neutral gate

- [ ] Client pre-push / `ci-local-gate` / agent “done” checks call
      **`skodun gate`**, not a Grok-only log under `.grok-reviews`  
- [ ] A trustworthy skodun row from **any** configured provider (exact
      diff hash, trustworthy, findings clean or triaged) is enough  
- [ ] AGENTS / MCP instructions: stop when `skodun gate` → `0`; pass absolute
      `repo` on MCP tools  
- [ ] Docs and scripts no longer require “Grok artifact” wording for pass  

### 3. Decommission legacy scripts

- [ ] One review pipeline per change by default:
      `skodun review` → (optional security/refuter) → `skodun gate`  
- [ ] Stop dual-running `grok-review-now.sh` (or equivalent) **and** skodun on
      the same tree (two locks / two stores = double wait)  
- [ ] Legacy scripts: read-only shadow or removed after a short soak (e.g. one
      week of stable gate parity)  
- [ ] If client-only fair locks were planned only for those scripts, cancel or
      narrow that work once scripts are gone (skodun S3 covers skodun FG)  

### 4. Agent policy

- [ ] **One** local finder chain by default; not every provider + every cloud
      bot on every low-risk change  
- [ ] Cloud bots (Codex/Gemini/CodeRabbit/…) stay **merge-boundary** checks,
      not a second full local review loop  
- [ ] Prefer `review-status` / `review-cancel` over 30–60s model-turn polling  
- [ ] Closing MCP session still cancels in-flight MCP `review`; do not leave
      abandoned waits on the FG lock without cancel or a human timeout  
- [ ] Optional later (client CI, not skodun core): tier full test gates;
      run cheap checks before review-convergence; resource-class locks  

---

## Explicit non-goals of this cutover doc

- Re-opening closed skodun epics from stale audits  
- Implementing host-wide fair queues for DB/Karma/Heroku inside skodun  
- Changing fail-closed gate/trust semantics  
- Making MCP queue a second `review` without fingerprint re-check  

---

## References

| Doc | Use |
|---|---|
| [`integrate-external-project.md`](integrate-external-project.md) | Install, MCP, gate wiring |
| [`epics/`](epics/) | Shipped epic seeds (S1/S3) |
| [`../AGENTS.md`](../AGENTS.md) | Maintainers working *on* skodun |
| [`../examples/AGENTS.md`](../examples/AGENTS.md) | Client agent paste |
| [`../examples/fragments/concurrency.md`](../examples/fragments/concurrency.md) | Multi-agent concurrency |
