# skodun

**A local, multi-provider AI code-review server that reuses the CLI subscriptions you already have.**

skodun runs code review pipelines through the AI coding CLIs installed on your machine — Codex
(ChatGPT plan), Claude Code, Grok, Antigravity — instead of burning API keys. Reviewers are
declared in a TOML config (provider, model, effort, role), and the pipeline is built around
fail-closed trust semantics battle-tested over ~9 months of daily use in a private project:

- **Diff-identity review tracking** — one review per exact content hash; a rebase or edit
  invalidates coverage, never silently.
- **Adversarial passes** — a skeptic pass that attacks clean results, a security pass for risky
  paths, and (Phase 2) a cross-provider refuter for every finding.
- **A gate you can trust** — exit `0` (clean or every finding dismissed with an audited reason),
  `1` (findings open), `2` (no trustworthy review); every unexpected error is `2`, and every
  bypass is a recorded decision.
- **Triage ledger** — dismissing a finding requires a ≥20-character reason; placeholder excuses
  ("false positive", "wontfix", …) are rejected; dismissals survive review rounds but not rebases.

## Status

**Pre-alpha — design phase.** Phase 1 (core pipeline + grok adapter, shadow-validated against the
legacy implementation) is specified and about to start:

- [Research report & architecture decision](docs/2026-07-27-review-server-research.md)
- [Phase 1 implementation plan](docs/superpowers/plans/2026-07-27-skodun-phase1.md)

## Planned shape

```toml
# ~/.config/skodun/config.toml
[[reviewers]]
name     = "finder"
provider = "openai"      # adapter: codex | claude | grok | agy | openai-compatible
model    = "gpt-5.6"
effort   = "medium"
role     = "finder"      # finder | refuter | security | triager

[[reviewers]]
name     = "refuter"
provider = "xai"
model    = "grok-4.20-0309-reasoning"
effort   = "high"
role     = "refuter"     # cross-provider refutation, deliberately
```

```bash
skodun review          # foreground review of your outgoing change
skodun gate            # 0/1/2 — wire it into pre-push or CI
skodun triage <id> <n> "why this finding is wrong (audited, ≥20 chars)"
skodun log
```

Later phases add an MCP server (so any agent harness can call it), a portable skill,
launchd scheduling, and pre-push hooks. See the research report for the full roadmap.

## Requirements

- Python ≥ 3.12 (runtime deps: stdlib only; dev: pytest)
- At least one supported AI CLI installed and authenticated (Phase 1: `grok`)

## License

[Apache-2.0](LICENSE)
