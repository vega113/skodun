# Multi-Provider Local Code-Review Server — Research Report & Recommendation

Date: 2026-07-27
Scope: replace per-project grok-review scripts (tubescribes) with one reusable, locally-running,
multi-provider review server that reuses existing CLI subscriptions (codex, claude, grok, gemini/agy),
with configurable reviewers (provider + model + effort), full feature parity with the tubescribes
system, and a skill/MCP interface for any harness.

---

## TL;DR — Recommendation

**Build our own server ("skodun"): a local stdio MCP server + CLI that ports the tubescribes review
pipeline into a provider-neutral core, with thin subprocess adapters over the installed CLI harnesses.
Do not adopt or fork an existing project as the base — but borrow deliberately from several.**

Why:

1. **The hard 80% is already ours.** The genuinely difficult, valuable machinery — the fail-closed
   trust model, gate exit contract, triage/dismissal ledger, diff-identity dedup, batching with
   integration pass, context packing, checklist registry — exists only in tubescribes, hardened by
   ~9 months of incident-driven fixes (a 4,020-run corpus informs the degraded-detection heuristics
   alone). No surveyed project has anything comparable. Porting this into someone else's young
   codebase is more work and more risk than building around it.
2. **The 20% that existing projects offer (CLI adapters, config, MCP scaffolding) is the easy part** —
   every one of the four CLIs supports headless prompt-in/JSON-out with per-run model and (mostly)
   effort selection, verified against the locally installed binaries.
3. **Chorus (chorus-codes/chorus) is the one project built on our exact premise** — local daemon,
   BYO-subscription CLI adapters, YAML reviewer slots, MCP server, Apache-2.0, actively developed.
   But it is 3 months old, lacks precisely our differentiators (scheduling, pre-push gating,
   per-reviewer effort, adversarial refute pipeline — the last is on its roadmap, i.e. will be
   shaped by upstream, not us), and has none of the tubescribes trust/gate semantics. It is the
   best *reference*, not the right *base*. Track it; steal its adapter and persona-config ideas;
   revisit adoption at its v1.0 if our build stalls.
4. **PAL MCP (ex-Zen), the mature multi-model MCP server, is API-key-first and stale since
   Dec 2025.** Ideas-only (its `clink` CLI-bridge presets and `consensus` tool are worth reading).

---

## Part 1 — What we have: the tubescribes grok-review system

Full inventory is in the appendix-level detail below; this section is the parity contract.

### Scale and maturity

- `scripts/grok-prepush-review.sh`: 3,631 lines, backed by 1,956 lines of tests.
- Python helpers: `grok_review_triage.py` (620), `grok-context-pack.py` (467),
  `grok-extra-passes.py`, `grok-checklist-select.py` (136) — these are lift-and-shift portable.
- Review archive `.grok-reviews/`: 24,541 files / 212 MB, 5,897 index rows, 253 triage dismissals.
- Nearly every design decision carries an inline postmortem citing an issue/PR number.

### The pipeline (three paths)

1. **Foreground loop (required pre-PR):** `grok-review-now.sh` → worktree check → cross-worktree
   lock → diff vs merge-base(github/main) incl. untracked → checklist selection → context packing →
   grok call (watchdog, retries) → security pass (path-triggered, fail-closed hold) → skeptic pass
   (only on clean results) → artifact + `GROK VERDICT` banner. Then fix-or-triage until clean;
   `ci-local-gate.sh` runs the gate (exit 0 clean/triaged, 1 open findings, 2 no trustworthy review).
2. **Passive pre-push (never blocks):** git pre-push hook → dedup probe by diff-hash → background
   worker (nohup) → artifact; findings delivered by a Claude Code SessionStart hook
   ("said nothing ≠ found nothing" discipline).
3. **Cloud/merge boundary:** J-Bot GitHub Action (Grok, 3 passes, reasoningEffort high) +
   codex/copilot/coderabbit/qodo/gemini bots, all fed rule embeds generated from the same registry;
   convergence watcher + a deliberately off-round GitHub cron (`7,22,37,52 * * * *`).

### Feature inventory (the parity checklist)

| # | Feature | Essence |
|---|---------|---------|
| 1 | **Diff identity & dedup** | `diff_hash` = git hash-object of the exact diff bytes (`--no-ext-diff --no-textconv` load-bearing); dual-hash (diff + context) 3-way dedup probe; only `trustworthy` reviews suppress re-review |
| 2 | **Two diff scopes** | Foreground: working tree vs merge-base incl. untracked (cap 100); dispatcher: pushed-ref range only |
| 3 | **Prompt construction** | Conservative reviewer prompt + path-scoped repo rules + JSON-only response contract + diff + packed file context |
| 4 | **Rules registry & checklists** | `code-rules.json` (33 rules; rationale must cite an incident; self-pruning: a rule leaves the prompt once it gains a static check) → generated checklists (≤2,560 B each), AGENTS.md embeds, and cloud-bot config embeds; byte-for-byte sync check in CI |
| 5 | **Per-change checklist selection** | Longest-prefix path→section mapping, 18 KiB budget, priority drop order, fail-soft |
| 6 | **Context packing** | Full changed-file contents in leftover budget; deterministic selection; symlink/traversal-hardened; omission reasons recorded |
| 7 | **Batched review** | Byte-level deterministic split at file/hunk boundaries; per-batch sub-reviews; cross-file integration pass; single aggregated artifact; first-abnormal `stop_reason` |
| 8 | **Adversarial passes** | (a) Skeptic clean-check when a review would clear the gate with 0 findings; (b) path-triggered security pass with fail-closed hold (clean row never published first); (c) triage ledger as the adversarial check on the *reviewer* |
| 9 | **Trust model** | `trustworthy = parse_ok && !degraded && !diff_truncated`; degraded detection from measured signals (stderr markers, leaked control tokens, `stopReason != EndTurn`, stderr-only turn-limit) with documented non-signals; verdict banner always last line of stdout |
| 10 | **Gate** | Exit contract 0/1/2; fails closed at every step (identity, base-sha, index↔artifact agreement, rebase detection via base_sha); every unexpected exception → exit 2; bypasses recorded as decisions (`GROK_GATE_SKIP=1` → `outcome:"skipped"` in evidence) |
| 11 | **Triage ledger** | `finding_key = sha256(file + title)` (line-number deliberately excluded); ledger scoped to branch + base_sha (rebase re-opens dismissals); ≥20-char reasons, 28 placeholder phrases rejected; append-only JSONL |
| 12 | **Reliability** | Process-group watchdog kill; two independent retry axes (timeout / degraded), always fresh sessions; stale-run recovery; same-branch supersede with pid-reuse guards; cross-worktree foreground lock; index write lock that demotes on failure; `nohup` survival |
| 13 | **Worktree binding** | Refuse primary checkout; `--now` runs this worktree's script, dispatcher runs main's (fix propagation) |
| 14 | **Artifacts & history** | Per-review JSON artifact (rich schema incl. attempts, checklist/context telemetry, `rule_ids` extracted from finding titles), index.jsonl, failures.log, status files, log viewer that imports the triage module rather than re-deriving keys |
| 15 | **Delivery** | SessionStart hook surfaces undelivered background findings AND failures ("no review happened" stated explicitly); macOS notifications (argv-safe osascript) |
| 16 | **Scheduling** | GitHub-side only (PR-advance cron, J-Bot on PR events, watch/poll loops). **No local scheduler exists** — pre-push is the local trigger |
| 17 | **Multi-provider** | **Grok only today.** An unimplemented plan doc (`docs/implementation/2026-07-25-grok-review-agy-fallback.md`) specifies an Antigravity (`agy`) quota-fallback — the clearest statement of multi-provider intent, and a useful requirements list (fence stripping, gate-contract preservation, lock retention during fallback, atomic quota-state writes) |

### Known debts the replacement should fix (not just match)

- **14 divergent copies** of the main script across ~125 linked worktrees — the strongest argument
  for a central server.
- **Model selection is implicit** (`.grok/settings.json`, never a CLI flag) — a portability trap.
- **No retention/cost controls:** 212 MB unbounded archive; 6.2 MB index linearly rescanned by
  dedup, gate, viewer, and hook.
- **Single provider, no fallback**; same-model skeptic pass (cross-provider refutation is
  demonstrably better — see Part 3).
- Everything constrained by macOS bash 3.2 (no flock, BSD stat/wc quirks, empty-array guards).

---

## Part 2 — What exists (survey, verified July 2026)

### Top candidates

| Tool | What it is | Subscription reuse | Per-reviewer model/effort | Adversarial | Scheduling | Gating | License / activity | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Chorus** ([chorus-codes/chorus](https://github.com/chorus-codes/chorus), [chorus.codes](https://chorus.codes)) | Local daemon (Fastify :7707) + web UI + MCP server + CLI; drives claude/codex/gemini/grok-build/kimi/opencode CLIs; YAML reviewer templates with lineage+model+persona slots; quorum/consensus; SQLite; personas as editable markdown | **Yes — core design** | Model yes, **effort no** | Quorum now; multi-stage write→review→fix→re-review on roadmap (v0.9); red-green info-asymmetric template | **No** | **No** | Apache-2.0 (SaaS layer proprietary); created 2026-04, pushed today, 525★ | **Best reference; not the base.** Track for v1.0 |
| **PAL MCP, ex-Zen** ([BeehiveInnovations](https://github.com/BeehiveInnovations/zen-mcp-server)) | Multi-model MCP server (Python): codereview/consensus/precommit/challenge; `clink` bridges gemini/claude/codex CLIs | Partial (clink only; presets hardcode models) | Model per call; no effort | Consensus + challenge | No | `precommit` tool (agent-invoked, not a hook) | 11.7k★ but **last push 2025-12-15** | Ideas-only (stale, API-first) |
| **religa/multi_mcp** | Tiny MCP server mixing API models and gemini/codex/claude CLI models; codereview/compare/debate | Yes (mixed) | Models in YAML | debate = independent answers + critique | No | No | MIT, 33★, active | Ideas-only; closest small-scale architecture |
| **PR-Agent** ([The-PR-Agent](https://github.com/The-PR-Agent/pr-agent)) | CI/CLI PR reviewer, LiteLLM (API keys) | No | Not per-slot | No | CI-driven | No | MIT, 12.3k★, active (community-owned since Qodo donated it) | Ideas-only (prompt/severity design) |
| **Kodus** | Self-hosted PR-review platform (Docker/Helm) | No (API keys) | Global | AST+LLM pipeline | Webhook | Paid tiers | **AGPLv3** | Irrelevant (heavy, SaaS-shaped) |
| **CodeRabbit CLI** | Cloud-backed CLI | No (their servers) | No | Their pipeline | No | pre-commit usable | Proprietary | Irrelevant (violates local + BYO) |
| **llm-council** (karpathy) | 3-stage: parallel answers → **anonymized peer ranking** → chairman synthesis | No (OpenRouter) | Config list | Yes — the pattern | No | No | 23.3k★, abandoned by design | Ideas-only — best adversarial protocol surveyed |
| CLI-wrapper MCPs (gemini-mcp-tool, codex-mcp, claude-code-mcp, ai-cli-mcp) | One CLI exposed over MCP | Yes (single provider each) | Partial | No | No | No | Mostly obsoleted by native `codex mcp-server` / `claude mcp serve` | Ideas-only (adapter/background-process patterns) |
| Adversarial plugins (heym Optimizer-vs-Skeptic action, agent-review-panel, gemini-plugin-cc) | Review-protocol experiments | Partial | n/a | **Yes — the whole point** | No | Action = merge gate | Small, 2026-active | Ideas-only (refute-pass prompt designs) |

Notes:
- "Chorus" ≠ chorus.sh (Melty Labs' Mac multi-model *chat* app — unrelated to code review).
- Claude's MCP connector registry has no matching code-review servers; community registries list
  many small ones, all API-key based.

### Harness landscape shifts that constrain the design

- **Gemini CLI was retired 2026-06-18** in favor of Google's closed-source **Antigravity CLI
  (`agy`)** — one of our four harnesses died mid-2026. Adapters must be pluggable and
  version-detected, never hardcoded. (The tubescribes fallback plan doc already targets `agy`.)
- **Grok CLI is now "Grok Build"**: MCP client, reads `.mcp.json`, discovers Claude-format skills
  (`~/.claude/skills`, `.agents/skills/`), has a `leader` shared-backend pattern.
- Native review primitives appeared: `codex review --base <branch>` (purpose-built, non-interactive),
  Claude Code `/code-review` (with effort levels; cannot be scheduled), `codex mcp-server` and
  `claude mcp serve` (both CLIs are MCP servers natively).

---

## Part 3 — Constraints and best practices (verified against installed binaries)

### CLI capability matrix (codex 0.144.5, claude 2.1.118, gemini 0.51.0, grok 0.2.112)

| Capability | codex | claude | gemini | grok |
|---|---|---|---|---|
| Headless | `codex exec` / `codex review` | `claude -p` | `gemini -p` | `grok -p` / `--prompt-file` / `--prompt-json` |
| JSON out | `--json`, `--output-schema <file>`, `-o <file>` (last msg) | `--output-format json`, `--json-schema` | `-o json` (no schema — validate yourself) | `--output-format json`, `--json-schema` |
| Model | `-m` | `--model` | `-m` | `-m` (tubescribes today: implicit via `.grok/settings.json`) |
| Effort | `-c model_reasoning_effort=...` (minimal→xhigh), profiles | `--effort low\|medium\|high\|xhigh\|max` | No flag — model aliases with `thinkingLevel` in settings | `--effort none→max` — **but grok-build 400s on it**; per-model capability table needed |
| Read-only sandbox | `-s read-only` | `--tools <read-only set>` / `--permission-mode plan`; `--bare` for determinism | `--approval-mode plan` | `--disallowed-tools`, `--sandbox` |
| Cost caps | — (subscription) | `--max-budget-usd` | quotas | `--max-turns` |
| MCP server | **yes** (`codex mcp-server`) | **yes** (`claude mcp serve`) | no | no (own WS protocol; leader pattern) |
| MCP prompts → slash commands | yes (experimental) | yes | yes | undocumented |

### Economics (changed materially in 2026)

- **Claude headless is no longer flat-rate:** since 2026-06-15, all programmatic use (`claude -p`,
  Agent SDK, Actions) draws from a separate monthly credit pool billed at API rates (Pro $20 /
  Max $100–$200 of credits), then pay-as-you-go. Use Claude for on-demand/gating passes, capped
  with `--max-budget-usd` — not for high-volume scheduled sweeps.
- **Codex is currently the best flat-rate lane** (included in ChatGPT plans; OpenAI docs accept
  reusing ChatGPT auth on trusted local runners, recommend API keys only for CI/shared automation).
- **Grok**: SuperGrok/Premium+ quotas; headless OAuth (`grok login --device-auth`); the tubescribes
  quota-fallback plan exists precisely because these quotas bite.
- **Gemini/agy**: free-tier quotas suit cheap triage passes; closed-source CLI = churn risk.

### MCP server vs skill: both, but the skill is thin

- Anthropic's framing: **MCP = what the agent can reach; skills = how to do the work.** MCP tool
  descriptions suffice for "call this tool correctly"; MCP **prompts** surface as
  `/mcp__server__prompt` slash commands in claude, codex, and gemini — so "run a review now" needs
  no skill at all.
- What tool descriptions can't carry is multi-step policy: run finder → refuter → gate; how to read
  the findings schema; when to triage vs fix. That's a **thin SKILL.md**, shipped once in the
  vendor-neutral **`.agents/skills/`** tree — codex and grok scan it natively, claude via
  `~/.claude/skills`, gemini via `gemini skills link`. One skill file serves all four harnesses.

### Scheduling

- **launchd over cron on macOS** (missed jobs run on wake; cron silently skips during sleep).
- Harness-native schedulers are dead ends for a provider-neutral server (Claude Code scheduled
  tasks require the desktop app and draw metered credits; `/code-review` is explicitly
  non-schedulable; codex has no local scheduler).
- Best practice: **OS-level trigger (generated launchd plist) invoking a plain CLI entrypoint**
  (`skodun run --scheduled`), never a scheduler thread inside a stdio MCP server (which has no
  guaranteed lifetime). Scheduling data lives in config; plists are generated/installed from it.
- **stdio + shared SQLite (WAL) beats an HTTP daemon** for a single-user local tool: zero network
  surface, per-project cwd for free, N instances share one history DB. If cross-project queueing /
  quota accounting later demands one warm process, grok's **leader pattern** (stdio shim per client
  + one daemon on a unix socket) is the proven middle path. (HTTP+SSE is deprecated in the MCP
  spec — don't build on it.)

### Review-pipeline evidence worth encoding

- **Line-annotated hunks + anchor validation** (findings must cite lines that exist in the diff —
  classic hallucination filter): OpenAI's own Codex-SDK code-review cookbook implements exactly
  this.
- **Confidence as a separate axis from severity**, with a drop threshold: Anthropic's
  claude-code-security-review scores each finding and drops below 8/10.
- **Justify-before-flag** (explicit reasoning logs) cut false positives 51% without recall loss
  at cubic.dev.
- **One capable agentic reviewer beats parallel voting**: Cursor's Bugbot rewrite (single reviewer
  deciding its own investigation depth) beat their 8-parallel-passes + majority-voting v1 —
  70%+ vs 52% resolution. Parallelism belongs across *roles* (finder/refuter/security), not as
  redundant copies of one role.
- **Cross-provider refutation** counters same-model agreeableness bias — the refuter should be a
  different provider than the finder (improvement over tubescribes' same-model skeptic).
- Dedup on (file, overlapping range, category) + judge-merge; gate on severity≥high AND
  confidence≥threshold; incremental review via stored per-hunk digests.

---

## Recommendation in full

### Decision: build "skodun", borrow deliberately

| Option | Assessment |
|---|---|
| **Adopt Chorus as-is** | No. Missing scheduling, gating, effort, triage, trust model — i.e. most of the parity contract. Its quorum-on-a-task model is not our gate-centric pipeline. |
| **Extend/fork Chorus** | No (for now). Young codebase moving fast under upstream's roadmap (multi-stage review lands in *their* v0.9, shaped by them), proprietary SaaS layer, TypeScript re-implementation of our hardened Python/bash logic anyway. Contributing adapters upstream later is fine; basing the gate on it is not. Re-evaluate at their v1.0. |
| **Extend PAL/Zen** | No. Stale 7 months, API-key-first, clink presets hardcode models. Read its `consensus`/`clink` code for ideas. |
| **Build own** | **Yes.** We keep the crown jewels (port ~1,800 lines of already-hardened Python nearly verbatim), replace the 3,631-line bash monolith with a maintainable core, and add exactly the missing pieces: provider adapters, scheduling, SQLite, MCP surface. |

### What to borrow from whom

| Source | Take |
|---|---|
| tubescribes | The entire pipeline semantics (parity table above); the four Python helpers as near-verbatim ports; the test-suite discipline (1,956 lines of tests, including the awk trick of testing the *real* prompt-writer) |
| Chorus | Reviewer-slot config shape (`lineage`/`model`/`persona`), personas as editable markdown files, red-green info-asymmetry idea, SQLite-in-`~/.tool` layout |
| llm-council | Anonymized peer ranking for the refute/consensus stage (strips provider-loyalty bias) |
| Anthropic security-review | Per-finding confidence scoring with a hard drop threshold |
| Codex cookbook | Line-annotated hunk format + anchor validation against the diff |
| PAL/Zen clink | Role-preset config layout for CLI bridges (`default`/`planner`/`codereviewer`) |
| grok CLI | Leader pattern, if a shared warm daemon is ever needed |
| Cursor Bugbot / cubic.dev | Agentic single finder over parallel voting; justify-before-flag prompting |

### Proposed architecture

**Language: Python** (direct reuse of the hardened triage/pack/extra-pass modules; official MCP
Python SDK; no bash-3.2 constraints ever again). TypeScript is defensible (Chorus as reference,
npm distribution) but forfeits the code reuse — the single strongest argument decides it.

Components:

1. **Core library + CLI** — `skodun review [--now|--staged|--base <ref>]`, `skodun gate`,
   `skodun triage <id> <n> "<reason>"`, `skodun log`, `skodun install-hooks`,
   `skodun schedule install`, `skodun doctor` (adapter/auth/version detection).
2. **stdio MCP server** (`skodun mcp`) — tools: `review_diff`, `review_status`, `gate`,
   `triage_finding`, `list_findings`, `review_log`; MCP prompts: `/review-now`, `/review-gate` —
   these become slash commands in claude/codex/gemini for free.
3. **Adapters** (subprocess, per-CLI): codex, claude, grok, agy, plus `openai-compatible` (local
   models later, mirroring Chorus v1.0's plan) and `custom-command` escape hatch. Each adapter owns:
   invocation flags, canonical-effort translation (`low|medium|high|max` → codex config key /
   claude `--effort` / grok `--effort` / gemini model alias), **per-model capability table**
   (e.g. grok-build rejects reasoningEffort), read-only sandboxing, envelope parsing, and
   **per-CLI degraded-signal detection** (the tubescribes signals are grok-specific; each adapter
   must define its own, with the same conservative "positive evidence only" philosophy).
4. **Store: SQLite (WAL)** at `~/.local/share/skodun/skodun.db` — reviews, attempts, findings,
   triage ledger, quota state, delivery markers. Fixes the 212 MB / linear-rescan debt; add
   retention (prune raw prompt/stdout blobs after N days, keep artifact rows). Raw per-review
   blobs on disk beside it, TTL'd.
5. **Config: TOML, layered** — global `~/.config/skodun/config.toml` + per-project `.skodun.toml`
   (deep-merged, project wins, reviewers merged by name), `-c key=value` overrides:

```toml
[defaults]
severity_gate = "high"          # block on >= this AND confidence >= threshold
confidence_threshold = 7
max_diff_bytes = 400_000
timeout_sec = 420

[[reviewers]]
name       = "finder"
provider   = "openai"           # adapter id: codex|claude|grok|agy|openai-compatible|custom
model      = "gpt-5.6"
effort     = "medium"
role       = "finder"           # finder | refuter | security | triager | integrator
dimensions = ["bugs", "security", "correctness"]
persona    = "personas/finder.md"

[[reviewers]]
name = "refuter"
provider = "xai"
model = "grok-4.20-0309-reasoning"
effort = "high"
role = "refuter"                # cross-provider vs finder — deliberate

[[reviewers]]
name = "security"
provider = "anthropic"
model = "claude-sonnet-5"
effort = "high"
role = "security"
max_cost_usd = 0.50             # Claude lane is metered — cap it

[schedule]
  [[schedule.jobs]]
  repo = "~/devroot/tubescribes"
  cron = "0 7 * * 1-5"          # skodun generates+installs the launchd plist
  scope = "branch"              # what to review on schedule
```

6. **Pipeline (ported semantics, generalized roles):** diff identity (`diff_hash` +
   `context_hash`), dedup, batching + integration pass, checklist registry + per-change selection
   (keep the registry format and the self-pruning rule; make the generator emit cloud-bot embeds
   as today), context packing, then role passes: finder → (security if path-risky, fail-closed
   hold) → refuter (on all findings, not just clean runs — upgraded skeptic) → triager
   (dedup/merge/confidence) → gate. Trust model, verdict banner, exit contract 0/1/2, rebase
   detection, recorded bypasses: identical semantics to tubescribes.
7. **Triggers:** `skodun install-hooks` (pre-push shim per repo — dispatcher semantics ported),
   launchd plists for scheduled runs, `skodun review --now` for the foreground loop, SessionStart
   hook (shipped as part of the skill install) for delivery.
8. **Skill:** one thin SKILL.md in `.agents/skills/skodun/` teaching the loop policy (review →
   verify each finding → fix or triage with an audited reason → gate; never trust a missing
   verdict), plus per-harness install one-liners. MCP prompts cover the "how do I invoke it" case.

### Parity map (tubescribes feature → skodun)

| tubescribes | skodun |
|---|---|
| diff_hash/context_hash dedup, trustworthy-only suppress | Same algorithm, SQLite-indexed instead of JSONL scan |
| Foreground `--now` + fg lock; background pre-push dispatcher | `skodun review --now` + per-repo lock rows in SQLite; installed pre-push shim calling `skodun dispatch` |
| Batching + integration pass | Ported as-is (byte-level split logic) |
| Rules registry + generation + sync-check + selection budget | Ported; registry stays per-repo (`docs/review/code-rules.json`), server consumes it |
| Context packing (hardened) | Port `grok-context-pack.py` near-verbatim |
| Security pass w/ fail-closed hold; skeptic pass | Generalized role passes; refuter runs cross-provider and on non-clean runs too |
| Trust model + degraded detection | Same invariant; detection signals per-adapter |
| Gate 0/1/2, rebase detection, recorded bypass | Identical contract (so `ci-local-gate.sh`-style callers keep working) |
| Triage ledger (finding_key, reason validation) | Port `grok_review_triage.py`; same keys so existing dismissals can be imported |
| Watchdog/retries/stale-recovery/supersede | Ported to Python process management (os.setsid, process groups) |
| Worktree binding + main-copy dispatch rule | Central server makes the 14-divergent-copies problem disappear; keep the primary-checkout refusal |
| Artifacts, index, viewer, verdict banner | SQLite + `skodun log`; banner format preserved |
| SessionStart delivery + notifications | Hook shipped with the skill; notification adapter (osascript, argv-safe) |
| GH-side scheduling / cloud bots | Unchanged (out of scope); registry keeps generating bot embeds |
| agy fallback plan doc | Becomes real: quota state in SQLite, provider-fallback chains per role |

### Risks / open questions

1. **Claude lane economics** — metered credits since June 2026; mitigate with role assignment
   (codex/grok for volume, claude for gating passes) and `--max-budget-usd`.
2. **CLI churn** — gemini already died mid-2026; `skodun doctor` must detect versions/capabilities,
   adapters must be replaceable without touching the core.
3. **ToS** — programmatic subscription use is explicitly accommodated (codex: local trusted runner
   OK; claude: the metered pool *is* the sanctioned path; grok: headless OAuth documented). Avoid
   shared-runner/CI use of subscription auth.
4. **Porting the bash monolith** — the 3,631 lines shrink a lot in Python, but the reliability
   semantics (pgid kills, publish ordering, lock reclaim races) are subtle; port the existing
   test suites as the oracle, and shadow-run against the grok scripts on tubescribes before cutover.
5. **Effort semantics differ per provider** — canonical enum + per-model capability table; refuse
   loudly (not silently) when a model rejects an effort setting.

### Suggested roadmap

1. **Phase 1 — core + one adapter (grok):** CLI + SQLite + ported pipeline, shadow-mode on
   tubescribes (run beside existing scripts, diff the verdicts). Import existing triage ledger.
2. **Phase 2 — multi-provider:** codex + claude + agy adapters, roles config, cross-provider
   refuter, quota fallback chains (implements the agy plan doc).
3. **Phase 3 — surfaces:** stdio MCP server + MCP prompts + SKILL.md in `.agents/skills/`;
   pre-push shim installer; SessionStart delivery hook.
4. **Phase 4 — scheduling + retention:** launchd plist generation, archive TTLs, `skodun doctor`.
5. **Cutover:** replace tubescribes scripts with thin shims calling skodun; keep the gate exit
   contract byte-compatible so `ci-local-gate.sh` needs only the command swapped.

---

*Sources: local audit of /Users/vega/devroot/tubescribes (scripts, docs, .grok-reviews archive,
.claude hooks, workflows); --help output of installed codex 0.144.5, claude 2.1.118, gemini 0.51.0,
grok 0.2.112; github.com/chorus-codes/chorus; chorus.codes; github.com/BeehiveInnovations
(zen/pal-mcp-server, clink docs); The-PR-Agent/pr-agent; karpathy/llm-council;
anthropics/claude-code-security-review; cursor.com/blog/building-bugbot; cubic.dev blog;
OpenAI Codex SDK code-review cookbook; code.claude.com headless & scheduled-tasks docs;
Anthropic "Equipping agents for the real world with Agent Skills"; Apple launchd scheduling docs;
MCP transport spec discussions (2026).*
