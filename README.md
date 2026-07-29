# skodun

**A local code-review pipeline that runs through an AI coding CLI you are already
subscribed to, instead of an API key.**

Reviewers are declared in a TOML config (provider, model, effort, role, and an
optional quota-fallback chain), and the pipeline is built around fail-closed trust
semantics:

- **Diff-identity review tracking** — one review per exact content hash; a rebase or edit
  invalidates coverage, never silently.
- **Adversarial passes** — a skeptic pass that attacks clean results, a security pass for
  risky paths, and an optional refuter pass where a *different* provider re-examines the
  finder's findings.
- **A gate you can trust** — exit `0` (clean or every finding dismissed with an audited reason),
  `1` (findings open), `2` (no trustworthy review); every unexpected error is `2`, and every
  bypass is a recorded decision.
- **Triage ledger** — dismissing a finding requires a ≥20-character reason; placeholder excuses
  ("false positive", "wontfix", …) are rejected; dismissals survive review rounds but not rebases.
  A refuter's annotation can be adopted as that reason with `triage --adopt-refuter`, one finding
  at a time — there is no bulk form.

## Status

**Phase 1 is implemented and running in shadow mode; Phase 2 adds multi-provider
review, quota-fallback chains, and cross-provider refutation on top of it.** Here is
the honest scope:

- What exists: three registered provider adapters (`xai` driving `grok`, `openai`
  driving `codex`, `google` driving `agy`), per-reviewer quota-fallback chains, an
  annotation-only refuter pass, the seven subcommands below, the SQLite store, the
  gate, the triage ledger, a one-shot importer for the archive of the previous
  implementation, and a shadow comparison used to check the two against each other.
- What does not: an `anthropic` adapter is declared nowhere in the registry — it is
  not shipped, so do not configure a reviewer with `provider = "anthropic"` and
  expect it to run (`skodun providers` will report it `FAILED` and exit `1`). There
  is also still no MCP server, no scheduling, and no git hooks.
- Shadow mode means exactly what it says: `shadow-compare` is observational, always
  exits `0`, and blocks nothing. skodun is being watched against the tool it is
  meant to replace, not yet trusted in its place.

First shadow run: eight real change-sets, gate agreement on seven, and identical
diff identity across 205 repository states. The results — including the one
disagreement and why it is not a porting defect — are in
[the shadow-mode runbook](docs/shadow-mode.md).

Background:

- [Research report & architecture decision](docs/2026-07-27-review-server-research.md)
- [Phase 1 implementation plan](docs/superpowers/plans/2026-07-27-skodun-phase1.md)

## Commands

| Command | What it does | Exit codes |
| --- | --- | --- |
| `skodun review [--repo DIR]` | Run one review of the outgoing change now, in the foreground, and record it. | `0` trustworthy and clean · `1` trustworthy, findings open · `2` preflight refusal (nothing ran) · `3` gave up waiting for the lock · `4` no trustworthy review was recorded · `130` interrupted with Ctrl-C (stdout is left empty on purpose — an operator's own interruption, not a refusal). |
| `skodun gate [--repo DIR]` | Fail closed unless a trustworthy review already covers this exact diff; every decision is written to the audit log. Wire it into pre-push or CI. | `0` clean or every finding triaged · `1` findings open · `2` no trustworthy review covers this diff. Every unexpected exception maps to `2`, never `1`. |
| `skodun providers [--repo DIR]` | List every registered provider adapter, whether its CLI binary is resolvable and executable right now, and its cached availability state. Read-only; never gates anything. | `0` always, even with missing binaries — that is exactly what this command exists to report · `1` a reviewer in the loaded config (enabled or not) names a `provider` with no registered adapter · `2` `--repo`/config/store could not be read at all. |
| `skodun triage <review-id> <finding-index> "<reason>"` | Dismiss one finding with an audited reason. Reasons under 20 characters and known placeholders are rejected. | `0` dismissed · `2` rejected (bad review id, bad index, reason too short or a placeholder). |
| `skodun triage --list <review-id>` | List a review's findings, each marked `OPEN` or `DISMISSED`, with any refuter annotation shown alongside. | `0` / `2` (store or artifact could not be read). |
| `skodun triage --adopt-refuter <review-id> <finding-index>` | Dismiss ONE finding by adopting its refuter annotation as the audited reason. See "The refuter" below. | `0` recorded · `1` REFUSED (wrong verdict, thin reasoning, or reasoning that fails the audit floor) · `2` NOT FOUND (no such review/finding, invalid artifact, or misuse). |
| `skodun log [--branch B] [-n N]` | Recent reviews, newest first, one line each; untrustworthy rows are marked `!`. | `0` / `2` (bad `-n`, or the store could not be read). |
| `skodun import-legacy [--repo DIR] [--dir ARCHIVE]` | One-shot migration of a legacy `.grok-reviews` archive into the store. Idempotent. Anything it cannot fully verify is imported *demoted* rather than trusted, and every counter is printed. | `0` ok (including "nothing to import") · `2` the importer could not run or a store write failed partway. |
| `skodun shadow-compare [--dir ARCHIVE] [--diff-hash H] [--since TS]` | Compare skodun's verdicts against that archive's, hash by hash, and print a table plus a summary. `--since` restricts the comparison, on both sides, to rows reviewed at or after a canonical UTC timestamp — exactly `%Y-%m-%dT%H:%M:%SZ` (e.g. `2026-07-28T12:00:00Z`); any other shape is rejected before anything runs. | Observational: always `0`, **except** a malformed `--since`, which is a usage error and exits `2` before any comparison happens. |

`skodun` with no subcommand is a usage error, not a `0` — a silent success is
indistinguishable from a PASS to whatever consumes the exit code. `python -m skodun`
behaves identically to the installed `skodun` console script.

Run `skodun <command> --help` for the exact flags; the table above is a summary, not
a substitute.

## Configuration

Global config at `~/.config/skodun/config.toml` (override with `SKODUN_CONFIG`),
per-repository overrides in `<repo>/.skodun.toml` (layers merge; a repo-layer value
wins per key, reviewers merge by `name`). The store lives at
`~/.local/share/skodun/skodun.db` (override with `SKODUN_DB`).

```toml
# ~/.config/skodun/config.toml
[[reviewers]]
name     = "finder"
provider = "xai"        # xai | openai | google are registered -- run `skodun providers`
model    = "grok-4.5"   # must be an id your CLI offers -- run `grok models`
effort   = "high"
role     = "finder"     # finder | refuter | security | triager | integrator
```

`model` is always passed explicitly and is never inherited from the provider
CLI's own settings file. Provider model ids change; if the configured id is not
one the installed CLI offers, the run fails closed — an explicit `failed` record
and `trustworthy=false`, never a silent pass. Check the current ids with each
CLI's own listing command before writing one into a config: `grok models` for
`xai`, `agy models` for `google`. The `codex` (`openai`) CLI has no equivalent
subcommand as of codex-cli 0.144.5 — its `--help` output lists no model-listing
command — so check available ids through the interactive `codex` session's own
model picker or your OpenAI account's documentation instead.

`effort` is one of `none | low | medium | high | max`, and those are skodun's own
names rather than any CLI's: each adapter translates them into whatever its binary
spells them as, and an effort an adapter cannot translate is a loud error, never a
quietly dropped flag. `"none"` means **the lowest reasoning that provider offers**,
which for a CLI with no such setting is *no effort flag at all* and for one with a
real lowest level is that level, requested explicitly. Omitting `effort` is a
different thing: unset passes no flag on any provider. `"max"` is the ceiling and
may map to the highest level every model that adapter serves accepts, rather than
to one only some of them offer.

### Registered providers

Run `skodun providers` to see, for this machine, each registered provider's
adapter, whether its CLI binary resolves and is executable, and its cached
availability state:

```
$ skodun providers
google | adapter=agy | binary=agy (executable) | state=none
openai | adapter=codex | binary=codex (executable) | state=none
xai | adapter=grok | binary=/home/you/.grok/bin/grok (executable) | state=none
```

Three providers are registered: `xai` (the `grok` CLI), `openai` (the `codex`
CLI), and `google` (the `agy` CLI). **`anthropic` is not registered** — there is
no shipped adapter for it. A reviewer entry naming `provider = "anthropic"` (or
any other unregistered name) loads fine — `load_config` only requires `provider`
and `model` to be non-empty strings, it does not check the name against the
adapter registry — but `skodun providers` reports it `FAILED ... has no
registered adapter` and exits `1`, and an actual review or gate run against it
fails closed the same way any unresolvable provider does.

Per-adapter binary overrides: `SKODUN_GROK_BIN`, `SKODUN_CODEX_BIN`,
`SKODUN_AGY_BIN` (a path, or a bare name resolved on `PATH`; unset or empty is
treated as "use the default"). `grok` additionally falls back to
`~/.grok/bin/grok` before `PATH` if that path is executable.

### Quota-fallback chains

A reviewer may name an ordered chain of other reviewer entries to try, by name, if
its own attempt classifies `unavailable` (binary missing, auth failure, unknown
model, or a cached provider-wide quota outage):

```toml
[[reviewers]]
name      = "finder"
provider  = "xai"
model     = "grok-4.5"
effort    = "high"
role      = "finder"
fallbacks = ["finder-openai"]   # tried, in order, only when "finder"'s own attempt is unavailable

[[reviewers]]
name     = "finder-openai"
provider = "openai"
model    = "gpt-5.4-mini"
effort   = "medium"
role     = "finder"
```

Validated at load time, not discovered mid-run: every name in `fallbacks` must
exist in the merged reviewer set, must be `enabled`, must not be the reviewer
itself, must not repeat within one chain, and the chain (head + fallbacks) may
hold at most 4 entries total (head + up to 3 fallbacks). A cycle anywhere in the
fallback graph — direct or through several reviewers — is also a load-time error.
A fallback member's own `fallbacks` are never followed while it is standing in for
another reviewer; only the head's list is walked.

### The refuter

A reviewer with `role = "refuter"` adds a pass, scheduled only when the finder
came back trustworthy *with* findings, where a **different provider** re-examines
each one and returns a verdict of `confirmed`, `refuted`, or `uncertain` with its
own reasoning.

**The refuter is annotation-only.** It never dismisses a finding, never changes a
finding count, and the gate never reads its annotations — `gate.open_findings`
blocks on any untriaged finding regardless of what a refuter said about it. The
only way a refuter's verdict becomes a dismissal is an explicit, per-finding
`skodun triage --adopt-refuter <review-id> <finding-index>` — there is
deliberately no `--adopt-all` or other bulk form. Adoption itself still goes
through the ordinary ≥20-character, non-placeholder reason floor: the finding's
raw refuter reasoning is what gets validated, so a refuter that answered "false
positive" is refused exactly like a human typing the same words, and a verdict
other than `refuted` (`confirmed`, `uncertain`) or reasoning the pass already
flagged as too thin is refused before anything is written.

The refuter pass, like the security and skeptic passes, has an env kill switch:
`SKODUN_SECURITY_PASS=0`, `SKODUN_SKEPTIC_PASS=0`, `SKODUN_REFUTER_PASS=0`
disable the respective pass (any other value, or leaving the variable unset,
leaves it enabled — only the exact string `"0"`, after stripping whitespace,
turns a pass off).

### `max_cost_usd`

A reviewer may set `max_cost_usd` (a finite, strictly positive number — `true`,
`0`, negative, `nan`, and `inf` are all rejected at load time, with a message
naming the reviewer). This validates today, but **no currently shipped adapter
(`xai`/`openai`/`google`) reads it** — it exists for a future `anthropic` adapter
that is not registered yet.

### Provider-state cache

A provider that answers a `quota` failure is cached unavailable for 30 minutes so
a bad config or a real outage does not burn every reviewer's attempt budget on a
provider that is already known to be down. `SKODUN_IGNORE_PROVIDER_STATE`
bypasses that cache — unset, blank, or exactly `"0"` means "the cache applies as
usual"; any other value means "ignore it, try every provider anyway". `skodun
providers` reports when this is set and notes that the state rows it prints are
then informational only.

### Removed keys

`[defaults] severity_gate` and `[defaults] confidence_threshold` were Phase 1
forward-looking stubs that nothing ever read — the gate blocks on *any* open
finding regardless of severity, by design. Phase 2 removed both rather than ever
implement a severity filter that would quietly weaken the gate. A config that still
sets either one fails to load with a message naming the key and explaining why,
before the generic "unknown `[defaults]` key" check ever runs.

### Example configs

- `examples/scala-angular-monorepo.toml` — a commented, drop-in `.skodun.toml` for
  a mixed-language repository: checklist routing, test-path patterns, security
  paths. Copy it to `<your repo>/.skodun.toml` and edit the paths to match your
  tree; it has no `[[reviewers]]` of its own, so pair it with a reviewer config
  (see the next file, or the snippets above).
- `examples/multi-provider.toml` — a commented reference for the reviewer side:
  a cross-provider finder with a fallback, a refuter, and a stack-agnostic
  security-pass shape, including the one provider (`google`/`agy`) whose CLI has
  sharp edges worth knowing about before you configure it.

Later phases add an MCP server so any agent harness can call skodun, scheduling,
and pre-push hooks. See the research report for the roadmap.

## Requirements

- Python ≥ 3.12. Runtime dependencies: **the standard library only**. Dev
  dependency: **pytest**, and nothing else.
- At least one supported AI CLI installed and authenticated: `grok` (`xai`),
  `codex` (`openai`), or `agy` (`google`).

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
