# skodun

**A local code-review pipeline that runs through an AI coding CLI you are already
subscribed to, instead of an API key.**

Reviewers are declared in a TOML config (provider, model, effort, role), and the
pipeline is built around fail-closed trust semantics:

- **Diff-identity review tracking** — one review per exact content hash; a rebase or edit
  invalidates coverage, never silently.
- **Adversarial passes** — a skeptic pass that attacks clean results, and a security pass for
  risky paths.
- **A gate you can trust** — exit `0` (clean or every finding dismissed with an audited reason),
  `1` (findings open), `2` (no trustworthy review); every unexpected error is `2`, and every
  bypass is a recorded decision.
- **Triage ledger** — dismissing a finding requires a ≥20-character reason; placeholder excuses
  ("false positive", "wontfix", …) are rejected; dismissals survive review rounds but not rebases.

## Status

**Phase 1 is implemented, and it is running in shadow mode.** That is a real but
narrow thing, so here is the honest scope:

- What exists: the six subcommands below, the SQLite store, the gate, the triage
  ledger, a one-shot importer for the archive of the previous implementation, and
  a shadow comparison used to check the two against each other.
- What does not: one provider adapter only (`xai`, driving the `grok` CLI) — no
  cross-provider refutation, no MCP server, no scheduling, no git hooks.
- Shadow mode means exactly what it says: `shadow-compare` is observational, always
  exits `0`, and blocks nothing. skodun is being watched against the tool it is
  meant to replace, not yet trusted in its place.

Background:

- [Research report & architecture decision](docs/2026-07-27-review-server-research.md)
- [Phase 1 implementation plan](docs/superpowers/plans/2026-07-27-skodun-phase1.md)

## Commands

| Command | What it does |
| --- | --- |
| `skodun review [--repo DIR]` | Run one review of the outgoing change now, in the foreground, and record it. `0` trustworthy and clean, `1` findings open, `2` preflight refusal (nothing ran), `3` gave up waiting for the lock, `4` no trustworthy review was recorded. |
| `skodun gate [--repo DIR]` | Fail closed unless a trustworthy review already covers this exact diff. `0` / `1` / `2`, as above; every decision is written to the audit log. Wire it into pre-push or CI. |
| `skodun triage <review-id> <finding-index> "<reason>"` | Dismiss one finding with an audited reason. Reasons under 20 characters and known placeholders are rejected. |
| `skodun triage --list <review-id>` | List a review's findings, each marked `OPEN` or `DISMISSED`. |
| `skodun log [--branch B] [-n N]` | Recent reviews, newest first, one line each; untrustworthy rows are marked `!`. |
| `skodun import-legacy [--repo DIR] [--dir ARCHIVE]` | One-shot migration of a legacy `.grok-reviews` archive into the store. Idempotent. Anything it cannot fully verify is imported *demoted* rather than trusted, and every counter is printed. |
| `skodun shadow-compare [--dir ARCHIVE]` | Compare skodun's verdicts against that archive's, hash by hash, and print a table plus a summary. Observational: always exits `0`. |

`skodun` with no subcommand is a usage error, not a `0` — a silent success is
indistinguishable from a PASS to whatever consumes the exit code.

## Configuration

Global config at `~/.config/skodun/config.toml` (override with `SKODUN_CONFIG`),
per-repository overrides in `<repo>/.skodun.toml`. The store lives at
`~/.local/share/skodun/skodun.db` (override with `SKODUN_DB`).

```toml
# ~/.config/skodun/config.toml
[[reviewers]]
name     = "finder"
provider = "xai"        # Phase 1 ships this one adapter, driving the `grok` CLI
model    = "grok-4.20-0309-reasoning"
effort   = "high"
role     = "finder"     # finder | refuter | security | triager | integrator
```

`examples/scala-angular-monorepo.toml` is a commented, drop-in `.skodun.toml` for a
mixed-language repository — checklist routing, test-path patterns, security paths.

Later phases add more provider adapters (and with them cross-provider refutation of
every finding), an MCP server so any agent harness can call it, scheduling, and
pre-push hooks. See the research report for the roadmap.

## Requirements

- Python ≥ 3.12. Runtime dependencies: **the standard library only**. Dev
  dependency: **pytest**, and nothing else.
- One supported AI CLI installed and authenticated (Phase 1: `grok`).

## Running the tests

```bash
python3 -m pytest              # from the repository root
```

No install step: `pyproject.toml` puts `src` and the repository root on
`pythonpath` for pytest.

Some tests are **parity tests** against the previous implementation this project
ports from. They load that implementation from a local checkout and assert skodun
either matches it exactly or diverges only in the documented, fail-closed
direction. They are skipped unless `SKODUN_ORACLE_DIR` points at such a checkout:

```bash
SKODUN_ORACLE_DIR=/path/to/that/checkout python3 -m pytest
```

The suite must pass both ways. `SKODUN_ORACLE_DIR` has no default, so no machine's
directory layout is baked into the tests.

## License

[Apache-2.0](LICENSE)
