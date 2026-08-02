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
- **A gate you can trust** — exit `0` (clean or every finding triaged with an audited reason),
  `1` (findings open), `2` (no trustworthy review); every unexpected error is `2`, and every
  bypass is a recorded decision.
- **Triage ledger** — clearing a finding requires a ≥20-character reason; placeholder excuses
  ("false positive", "wontfix", …) are rejected; decisions survive review rounds but not rebases.
  There are three verbs: `dismiss` ("not a defect"), `defer` ("real, not for this change, filed
  as X" — with a **mandatory** tracking reference), and `reopen`, which overturns either. A
  refuter's annotation can be adopted as a dismissal reason with `triage --adopt-refuter`, one
  finding at a time — there is no bulk form.

## Status

**Phase 1 is implemented and running in shadow mode; Phase 2 adds multi-provider
review, quota-fallback chains, and cross-provider refutation on top of it; Phase 3
adds a pre-push dispatcher with background review, its delivery surface, and an MCP
server for agent harnesses.** Here is the honest scope:

- What exists: four registered provider adapters (`xai` driving `grok`, `openai`
  driving `codex`, `google` driving `agy`, `junie` driving the JetBrains `junie`
  CLI under macOS Seatbelt confinement), per-reviewer quota-fallback chains, an
  annotation-only refuter pass, the thirteen subcommands below, the SQLite store, the
  gate, the triage ledger, a one-shot importer for the archive of the previous
  implementation, a shadow comparison used to check the two against each other, a
  pre-push dispatcher with a detached background worker and a delivery ledger
  (`skodun surface`), and a stdio MCP server (`skodun mcp`).
- What does not: an `anthropic` adapter is declared nowhere in the registry — it is
  not shipped, so do not configure a reviewer with `provider = "anthropic"` and
  expect it to run (`skodun providers` will report it `FAILED` and exit `1`). This
  is a deliberate scope decision, not an unfinished task: driving the `claude` CLI
  headlessly bills as API usage rather than drawing on a Claude subscription, and
  the whole premise of skodun is reusing the CLI subscriptions you already pay for.
  An adapter that quietly moves a user onto metered API billing would work against
  that. See "Why there is no `anthropic` adapter" below. There is also still no
  scheduling.
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
| `skodun review [--repo DIR] [--reviewer NAME]` | Run one review of the outgoing change now, in the foreground, and record it. `--reviewer` heads this run's chain with a named `[[reviewers]]` entry instead of the config's `finder` — see "Choosing the reviewer for one review" below. | `0` trustworthy and clean · `1` trustworthy, findings open · `2` preflight refusal (nothing ran) · `3` gave up waiting for the lock · `4` no trustworthy review was recorded · `130` interrupted with Ctrl-C (stdout is left empty on purpose — an operator's own interruption, not a refusal). |
| `skodun gate [--repo DIR]` | Fail closed unless a trustworthy review already covers this exact diff; every decision is written to the audit log. Wire it into pre-push or CI. | `0` clean or every finding triaged · `1` findings open · `2` no trustworthy review covers this diff. Every unexpected exception maps to `2`, never `1`. |
| `skodun providers [--repo DIR]` | List every registered provider adapter, whether its CLI binary is resolvable and executable right now, and its cached availability state. Read-only; never gates anything. | `0` always, even with missing binaries — that is exactly what this command exists to report · `1` a reviewer in the loaded config (enabled or not) names a `provider` with no registered adapter · `2` `--repo`/config/store could not be read at all. |
| `skodun triage <review-id> <finding-index> "<reason>"` | Dismiss one finding with an audited reason — *this is not a defect*. Reasons under 20 characters and known placeholders are rejected. | `0` dismissed · `2` rejected (bad review id, bad index, reason too short or a placeholder). |
| `skodun triage --defer <review-id> <finding-index> <tracking-ref> "<reason>"` | Defer one finding to a **filed** tracking reference — *this is real, it is not blast-radius for this change, and the work is filed as X*. The reference is **mandatory** and validated (an issue number, a tracker key, or a URL — one token, not prose); the reason clears the same audit floor a dismissal's does. It clears the gate exactly as a dismissal does, and `skodun deferrals` is how the backlog it creates stays visible. | `0` recorded · `1` REFUSED (no usable tracking reference, or a reason that fails the audit floor) · `2` NOT FOUND (no such review/finding, invalid artifact, or misuse). |
| `skodun triage --list <review-id>` | List a review's findings, each marked `OPEN`, `DISMISSED <when>`, `DEFERRED -> <ref> <when>` or `REOPENED <when>, dismissed <when>`, with any refuter annotation shown alongside. | `0` / `2` (store or artifact could not be read). |
| `skodun triage --reopen <review-id> <finding-index> "<reason>"` | Overturn one finding's dismissal **or deferral**, with an audited reason of its own — it clears the same 20-character, no-placeholder floor a dismissal does, because it takes the gate from `0` back to `1`. Append-only: the decision it overturns and its reason (and, for a deferral, its filed reference) stay in the ledger, and the whole history of a finding (dismiss → reopen → defer → …) is preserved. | `0` recorded · `1` REFUSED (reason fails the audit floor, or the finding is neither dismissed nor deferred and there is nothing to overturn) · `2` NOT FOUND (no such review/finding, invalid artifact, or misuse). |
| `skodun triage --adopt-refuter <review-id> <finding-index>` | Dismiss ONE finding by adopting its refuter annotation as the audited reason. See "The refuter" below. | `0` recorded · `1` REFUSED (wrong verdict, thin reasoning, or reasoning that fails the audit floor) · `2` NOT FOUND (no such review/finding, invalid artifact, or misuse). |
| `skodun log [--branch B] [--repo DIR] [-n N]` | Recent reviews, newest first, one line each; untrustworthy rows are marked `!`. `--repo` narrows `--branch` to one repository (default: the current directory) and is ignored without one — an unscoped listing deliberately still crosses repositories. | `0` / `2` (bad `-n`, a `--repo` git cannot read while `--branch` is given, or the store could not be read). |
| `skodun deferrals [-n N]` | Every finding still standing as DEFERRED, across **all** reviews and branches, newest first: `<ref> \| <branch> \| <file>:<line> \| <severity> <title> \| deferred <when> \| review <id>`. Deliberately unscoped — a deferral filed on a branch nobody is looking at is exactly the one that rots. Nothing on stdout and a note on stderr when there are none. | `0` / `2` (bad `-n`, or the ledger could not be read). |
| `skodun import-legacy [--repo DIR] [--dir ARCHIVE]` | One-shot migration of a legacy `.grok-reviews` archive into the store. Idempotent. Anything it cannot fully verify is imported *demoted* rather than trusted, and every counter is printed. | `0` ok (including "nothing to import") · `2` the importer could not run or a store write failed partway. |
| `skodun shadow-compare [--dir ARCHIVE] [--diff-hash H] [--since TS]` | Compare skodun's verdicts against that archive's, hash by hash, and print a table plus a summary. `--since` restricts the comparison, on both sides, to rows reviewed at or after a canonical UTC timestamp — exactly `%Y-%m-%dT%H:%M:%SZ` (e.g. `2026-07-28T12:00:00Z`); any other shape is rejected before anything runs. | Observational: always `0`, **except** a malformed `--since`, which is a usage error and exits `2` before any comparison happens. |
| `skodun install-hooks [--repo DIR] [--force]` | Install (or re-install) the pre-push shim into this repository's real hooks directory, chaining any hook that was already there. See "Pre-push hooks and background review" below for what the shim does and what `--force` means. | `0` installed · `1` refused — a foreign hook is there and needs `--force` (or to be moved aside yourself) · `2` this is not a repository skodun can install into at all. |
| `skodun retain [--repo DIR] [--dry-run]` | Prune worker logs under `<db>.logs/` per the `[retention]` table (`worker_log_max_age_days`, `worker_log_max_count`; `0` disables an axis). Never deletes review artifacts or triage rows the gate needs. | `0` pass (including dry-run / nothing to do) · `2` config/store/log error or partial delete failure. |
| `skodun doctor [--repo DIR]` | Read-only install/MCP readiness: Python version, config load, store schema, registered adapters + binary resolve, MCP import. Does not mutate the store. | `0` all checks ok · `1` problems found · `2` doctor could not run. |
| `skodun schedule install [--repo DIR] [--dest DIR] [--force-platform]` | Write launchd plists from `[[schedule.jobs]]` (commands `retain` or `doctor`). Does **not** run a scheduler inside `skodun mcp`. macOS-only unless `--force-platform`. | `0` written (or nothing configured) · `2` refused/error. |
| `skodun dispatch [--repo DIR] [remote-name] [remote-url]` | Reserve and dispatch background reviews for a push. This is what the installed pre-push shim calls; nobody runs it by hand. It decides nothing about the push itself — every failure becomes a stderr warning and a durable `failed` review record, never a blocked push. | Always `0` — dispatching is not a verdict; `skodun gate` is. |
| `skodun worker --record-id ID --repo DIR --branch B --local-oid OID --base-sha SHA [--base-ref REF]` | The detached background review process `dispatch` spawns for one reservation. Internal: hidden from `--help` because its flags are reservation bookkeeping nobody types by hand, but it stays fully usable (and debuggable) by name. | `0` the reservation reached a terminal state (reviewed, cancelled, or already retired by a newer push) · `2` it could not do its job at all (no store, or no such reservation). Nothing in production reads this code. |
| `skodun surface [--repo DIR] [--branch B] [--hook-format text\|claude] [--include-delivered]` | Report background review rounds nobody has been shown yet, and record that they were delivered. Silence is never a verdict: a round that produced nothing usable says so explicitly (`NO REVIEW HAPPENED`) rather than reading as "0 findings". Certifies nothing about the change in the working tree right now — only `skodun gate` does that. See "Pre-push hooks and background review" below. | `0` reported (including "nothing to report") · `2` no store, no branch, no readable repository to scope the rounds to, an unwritable report, or an unrecordable delivery. |
| `skodun mcp` | Serve the review loop to agents over stdio (MCP JSON-RPC): the same `gate` / `review` / `log` / `surface` / `triage` decisions the CLI makes, exposed as tools and two prompts. No flags — every tool carries its own arguments. See "MCP server" below. | `0` the session ended (the client closed stdin, or disconnected) · `2` the server could not be loaded or started. |

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
provider = "xai"        # xai | openai | google | junie are registered -- run `skodun providers`
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
junie | adapter=junie | binary=junie (executable) | state=none
openai | adapter=codex | binary=codex (executable) | state=none
xai | adapter=grok | binary=/home/you/.grok/bin/grok (executable) | state=none
```

Four providers are registered: `xai` (the `grok` CLI), `openai` (the `codex`
CLI), `google` (the `agy` CLI), and `junie` (the JetBrains `junie` CLI, macOS
only — see below). **`anthropic` is not registered** — there is
no shipped adapter for it. A reviewer entry naming `provider = "anthropic"` (or
any other unregistered name) loads fine — `load_config` only requires `provider`
and `model` to be non-empty strings, it does not check the name against the
adapter registry — but `skodun providers` reports it `FAILED ... has no
registered adapter` and exits `1`, and an actual review or gate run against it
fails closed the same way any unresolvable provider does.

#### Why there is no `anthropic` adapter

Not an oversight, and not a half-finished task. skodun exists to reuse the
provider CLI subscriptions you already pay for — that is the reason it shells out
to installed binaries instead of linking a provider SDK. Driving the `claude` CLI
**headlessly** bills as API usage rather than drawing on a Claude subscription, so
an `anthropic` adapter would quietly move a user from flat-rate to metered billing
to run the same review. That trade is the project's premise inverted, so the
adapter is deliberately out of scope for now.

Nothing is stubbed out waiting for it: there is no `adapters/claude.py`, no
`anthropic` fixture directory, and no registry entry. The conformance suite is the
registration gate and would fail CI for an adapter registered without one, so the
absence is enforced rather than assumed. If the CLI's headless billing changes, the
adapter is a normal task — the contract it would implement is already the same one
`openai` and `google` satisfy.

Per-adapter binary overrides: `SKODUN_GROK_BIN`, `SKODUN_CODEX_BIN`,
`SKODUN_AGY_BIN`, `SKODUN_JUNIE_BIN` (a path, or a bare name resolved on `PATH`;
unset or empty is treated as "use the default"). `grok` additionally falls back
to `~/.grok/bin/grok` before `PATH` if that path is executable.

#### The `junie` adapter (macOS only)

`provider = "junie"` drives the JetBrains `junie` CLI as a prompt-only reviewer.
It does **not** point junie at your real worktree: every attempt runs in an empty
temporary capsule under a deny-by-default macOS Seatbelt profile, with operator
and foreign-provider credentials stripped from the environment, discovery
locations disabled, and post-run mutation checks before any envelope is trusted.
Off macOS — or when `/usr/bin/sandbox-exec` is missing — the adapter refuses
before inference (`unavailable`) rather than running unconfined. The prompt
travels as a file into the capsule and then on stdin (`--input-format text`),
never on argv. Effort values are `low` / `medium` / `high`; `max` is refused
loudly. Prefer a model id your junie install actually serves (for example
`gpt-5.6-luna`); the model is always explicit from the reviewer entry.

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

### Choosing the reviewer for one review

Normally the config decides: the chain is headed by the first enabled entry whose
`role` is `finder`. `--reviewer` overrides that **for one run**, by entry name:

```
skodun review --reviewer finder-openai
```

and the MCP `review` tool takes the same argument (`{"reviewer": "finder-openai"}`).
Use it when a provider is out of quota, when a change wants a second opinion from a
different model, or when an agent already knows which provider is healthy.

Three things it does *not* do:

- **It does not disable the chain.** The chosen entry's own `fallbacks` still
  apply, so this narrows where the chain *starts*, never whether it can recover.
- **It does not touch the extra passes.** The security, skeptic, refuter and
  integration passes still pick their reviewer by *role*; `--reviewer` selects the
  finder head only. (Where the config names no reviewer for a role, that pass falls
  back to whatever headed the chain, exactly as it always has.)
- **It does not fall back to the config's default.** A name that is not configured,
  is `enabled = false`, or sits on a provider with no registered adapter is refused
  in preflight — exit `2`, "no review ran", nothing spawned and nothing recorded —
  and the refusal lists the configured entries. Silently reviewing with the model
  you were trying to route around would be worse than not reviewing.

Selection is **by name, not by provider id**: two enabled entries may share a
provider, and choosing between them by an unstated rule would also choose their
model, effort, prompt budget and fallback chain. There is deliberately no
`--provider` flag.

The artifact records the request as a request: `requested_reviewer` is the name that
was asked for (`null` when nobody asked), while `adapter`/`model` and the `attempts`
provenance keep naming whoever actually *answered* — after a fallback those are two
different providers, and both facts are worth having.

`--reviewer` is a *foreground* flag. Background pre-push reviews use the configured
chain; `dispatch` and `worker` take no such argument.

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

### Prompt budgets are per provider

`[defaults] max_diff_bytes` is the prompt *envelope*: the diff plus whatever
packed file context fits after it. It is one number for the whole config, but it
is not the only thing that bounds a prompt — a CLI that carries its prompt in an
argv element rather than a file has a hard ceiling of its own (`agy`: ~120 KB,
the kernel's per-argument cap), and each adapter **declares** that ceiling.

The budget for one reviewer is therefore `min(that reviewer's own
max_diff_bytes or the global, its adapter's declared ceiling)`, and every prompt
build and batch plan is sized with it. Two consequences worth knowing:

* You do **not** have to shrink the global to fit the least capable provider in
  your config. An `agy` entry is budgeted to what it can carry while a `codex`
  or `grok` entry beside it keeps the whole envelope.
* A reviewer entry may carry its own `max_diff_bytes` (validated exactly like
  the `[defaults]` key: an integer, not a bool, at least 1). It *replaces* the
  global for that entry, in either direction, and is still capped by the
  adapter's ceiling — a ceiling is what the CLI can physically accept, and no
  configuration raises it.

A prompt that still exceeds an adapter's ceiling — a chain can span providers,
and one sized for a file-fed head may reach an argv-bound fallback — is
classified `unavailable` for that entry and **the chain advances** to the next
one, exactly as a quota outage would. This is still fail-closed: an exhausted
chain is a `failed`, untrustworthy record naming the prompt size and the
ceiling, and nothing becomes trustworthy that was not reviewed.

### `max_cost_usd`

A reviewer may set `max_cost_usd` (a finite, strictly positive number — `true`,
`0`, negative, `nan`, and `inf` are all rejected at load time, with a message
naming the reviewer). This validates today, but **no currently shipped adapter
(`xai`/`openai`/`google`) reads it** — it was added for an `anthropic` adapter that
is deliberately not shipped (see above), and is kept because a budget cap is a
provider-neutral idea any future metered adapter would want. Setting it today is
harmless and has no effect.

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

Phase 3 adds the pre-push hooks, background dispatcher, and MCP server documented
below; scheduling is still a later phase. See the research report for the roadmap.

## Pre-push hooks and background review

`skodun install-hooks` installs a small POSIX-sh shim as this repository's
`pre-push` hook (resolved through `git rev-parse --git-path hooks`, so it lands in
the right place for a linked worktree or a relocated `core.hooksPath` too, never a
hard-coded `.git/hooks`). What the shim does, in order:

1. **Buffers stdin once.** Git's pre-push protocol sends the list of updated refs
   on stdin, and it can only be read once — the shim tees it to a temp file so
   both the chained hook and skodun read the same bytes.
2. **Chains any hook that was already there**, with the original argv and the
   buffered stdin, and **propagates its refusal**: if that hook exits non-zero,
   the shim exits with the same code and the push fails exactly as it would have
   without skodun.
3. **Only then** runs `skodun dispatch` on the same buffered stdin. Nothing about
   review machinery may block the push at this point: every dispatcher failure —
   a bad config, a crash, the `skodun` executable being gone entirely — becomes a
   stderr warning and the shim still exits `0`.

Re-running `skodun install-hooks` replaces only a hook it recognizes as its own
(marked internally); anything else is a **foreign hook**, and installing over one
is **refused** unless you pass `--force`. `--force` backs the foreign hook up
beside the new one (`pre-push.pre-skodun`) and chains it — it is never discarded —
except that installing refuses even under `--force` if that backup name is already
holding a *different* hook than the one currently installed, so a second `--force`
run can never silently destroy the first backup.

### One store per repository, or one store for all of them (both work)

**Background review rounds carry the repository that recorded them.** `reviews`
has had a `repo` column since store v5, holding the repository's *git common
directory* — the same expression the foreground lock scopes by, so "the same
repository" has exactly one definition and a linked worktree is correctly the
same repository as its main checkout. The three queries that act on a
repository's behalf consult it:

- the supersede a push performs, so pushing repo A's `main` no longer retires
  repo B's in-flight round on the strength of a shared branch name — and no
  longer **signals repo B's live background worker to stop**;
- the undelivered-rounds query behind `skodun surface` (and its
  `--include-delivered` replay), so surfacing `main` in repo A no longer renders
  repo B's round, and no longer marks it delivered on repo B's behalf;
- `skodun log --branch`, which grew a `--repo` of its own to aim it. An
  *unscoped* `skodun log` still crosses repositories on purpose: a branch name is
  the ambiguous key, "show me everything" is not.

Two repositories can therefore share the default global store
(`~/.local/share/skodun/skodun.db`), which is what they do unless you say
otherwise.

**One caveat, and it is permanent: rows recorded before the upgrade have no
repository.** The v5 migration adds the column and backfills nothing, and
`repo = ?` never matches NULL — deliberately, because the alternative to an
invisible old row is guessing which tree it belonged to and killing that tree's
worker. What you can see: **background rounds recorded before the upgrade are
never delivered by `skodun surface`.** They are not lost — an unscoped `skodun
log` still lists them, `skodun triage` still reads them, and the gate still
matches them by content — they simply never appear in a session report again.
Everything recorded after the upgrade is unaffected.

The gate was never affected either way, and still is not: `skodun gate` is
content-addressed (`diff_hash` + `base_sha`) and consults no repository at all,
so it can never answer `0` for a change no trustworthy review covers. That it
*does* still match a review of the same content taken in a different checkout is
a decision, not an oversight — identical diff bytes at the same base are the same
change, which is the whole property diff-identity exists to express.

**You may still want one store per repository**, via `SKODUN_DB` — to keep one
project's review history off another's disk, or simply to keep `skodun log`
short. Point it at a path inside (or named after) each repository, from wherever
that repository's environment is set up — a shell profile:

```sh
# ~/.zshrc or ~/.bashrc
export SKODUN_DB="$HOME/.local/share/skodun/myproject.db"
```

or, per checkout, a `direnv` `.envrc` at the repository root:

```sh
# <repo>/.envrc  -- `direnv allow` once
export SKODUN_DB="$HOME/.local/share/skodun/$(basename "$PWD").db"
```

(keep the store *outside* the working tree — a database file inside it is
something a future `git add -A` will happily commit).

Whatever sets it must be in effect for `git push` as well as for your editor and
shell: the pre-push shim, the background worker, and `skodun surface` all read the
same variable, and they must all land on the same file. A half-configured split
is the failure to watch for: a worker writing rounds to one store while `skodun
surface` reads another reports nothing, and says nothing about why.

### Bypassing review for a push

Two independent switches, either one enough to skip review entirely for a push
(the chained foreign hook, if any, still runs and can still block the push on its
own terms):

- **`SKODUN_PREPUSH_SKIP=1 git push`** — skips review for that one push only.
- **`git config skodun.prepush false`** — a persistent, per-repository opt-out.

Both are checked *before* `.skodun.toml` is even read, so a broken config can
never make the bypass unavailable.

### `[dispatch]`

A `[dispatch]` table in `.skodun.toml` (or the global config) sizes the
*background* worker's budget — deliberately separate from `[defaults]`, which
sizes a *foreground* `skodun review` where a human is waiting:

| Key | Default | What it controls |
| --- | --- | --- |
| `enabled` | `true` | Config-level kill switch for background review, parallel to the `git config skodun.prepush false` bypass. `false` discards every pushed ref with one stderr note: no capture, no reservation, no worker, no record. |
| `timeout_sec` | `240` | The worker's per-attempt timeout, replacing `[defaults] timeout_sec` for background runs only. |
| `timeout_retries` | `0` | The worker's timeout-retry budget, replacing `[defaults] timeout_retries` for background runs only. |
| `dedup` | `true` | Whether a push whose diff a trustworthy review already covers may be suppressed without a model ever seeing it. `false` disables that suppression path entirely. |
| `large_prompt_bytes` | `80000` | Above this per-prompt size, a background attempt's timeout escalates to the *foreground* `[defaults] timeout_sec` — a prompt this large legitimately needs more time than the background budget allows. |

Every other setting a background review uses (diff size caps, checklist routing,
security-pass triggers, the extra passes) comes from `[defaults]` untouched.

### Reading background results: `skodun surface` and SessionStart hooks

A background review lands after `git push` has already returned, so nobody is
watching when it finishes — and the dangerous case is not the findings that go
unread, it is the rounds that *failed*: a timed-out review still records
`findings_total: 0`, and anything that reads that as "0 findings" turns a review
that never happened into a clean bill of health. `skodun surface` exists to close
that gap: it reports every background round on a branch nobody has been shown yet,
states plainly when a round produced no usable answer at all, and only marks a
round delivered once its report has actually reached a reader.

```
skodun surface [--repo DIR] [--branch B] [--hook-format text|claude] [--include-delivered]
```

- `--repo` picks the repository whose checked-out branch to report on, so the
  command works from any directory (the MCP `surface` tool's `repo` argument does
  the same thing). It moves *branch discovery only* — which store is read is
  still `SKODUN_DB`. `--branch` overrides the branch, but not the repository: the
  rounds are scoped to the repository that recorded them, so `surface` refuses
  with `2` when it cannot identify one, even with `--branch` given.
- `--branch` defaults to the checked-out branch.
- `--hook-format text` (the default) prints plain lines for a shell profile, a
  tmux hook, or a CI step; `--hook-format claude` prints exactly one JSON object —
  the Claude Code `SessionStart` hook envelope. Passing the flag at all says "a
  machine is calling", which changes exactly one thing: with nothing to report,
  the command is silent on **both** streams instead of noting on stderr that
  there was nothing undelivered. That note is for a human who typed the command
  and got silence; in a shell profile it is a line at every session start.
  Failures still go to stderr either way — a failure is *why* nothing appeared.
- `--include-delivered` replays rounds already recorded as delivered too, without
  affecting the ledger.

Two ready-to-use templates for wiring this into a session start live under
`examples/hooks/` — **skodun never installs either one into a repository or a
user's shell config; that is a choice for the person whose session it is, made by
following the instructions below, not something this tool writes for you.**

- **`examples/hooks/sessionstart-claude.sh`** — copy it somewhere of your own and
  register it as a Claude Code `SessionStart` hook:

  ```json
  {"hooks": {"SessionStart": [{"hooks": [
     {"type": "command", "command": "/path/to/sessionstart-claude.sh"}]}]}}
  ```

- **`examples/hooks/sessionstart-plain.sh`** — the same delivery in plain text, for
  any harness that just shows a human whatever a command prints; copy it
  somewhere of your own and call it from wherever a session begins, e.g. in
  `~/.bashrc` or `~/.zshrc`:

  ```sh
  [ -x "$HOME/bin/sessionstart-plain.sh" ] && "$HOME/bin/sessionstart-plain.sh"
  ```

Both scripts exit `0` on every path, including "skodun is not installed at all",
and both honor `SKODUN_BIN` to point at a specific binary instead of resolving
`skodun` on `PATH` or falling back to `python3 -m skodun`.

## Telling your coding agents how to use skodun

An agent that can run `skodun` will use it, and — without being told otherwise —
will keep fixing and re-reviewing until the reviewer goes quiet. That does not
converge: every round of fixes is new code for the next round to find fault
with. Measured on skodun's own Phase 3 branch, a second round repeated **none**
of the first round's eleven findings and put four of its six new ones in code
the fix commit had just written.

**`examples/AGENTS.md`** is a template to paste into your repository's own
`AGENTS.md` / `CLAUDE.md`. It covers the loop (freeze the diff, one review per
head, stop when `gate` exits 0 — not when findings reach zero), a fix-now vs
defer table judged on consequence rather than severity label, the conditions
that mean "escalate to a human instead of running another round", and the rule
that an agent never dismisses a finding by itself.

The reasoning, the outside evidence and the recommendations for skodun itself
are in `docs/superpowers/specs/2026-07-31-review-round-cutoff-design.md`.

### Deferring a finding honestly

The gate passes on **clean OR every finding triaged**, and `defer` is the verb
that makes "triaged" mean something other than "dismissed". Recording

```bash
skodun triage --defer sk_20260731_ab12 3 GH-412 \
  "in-bounds for this surface; the hot path is the batcher, filed for next sprint"
```

clears the finding for the gate while keeping it in the ledger as **outstanding
debt** rather than a rejected finding, so `skodun deferrals` can still list it
next month. The tracking reference is mandatory and validated; a deferral with
none is refused with the same force a placeholder reason is, because an unfiled
deferral and an ignored finding are the same artifact. File the issue first,
then record its reference.

This is a liability transfer, not a fix. A project that defers everything ships
the same code as a project with no review, and only the filed references make
the difference visible — whether the backlog is actually worked is a human
discipline no gate can enforce. What skodun guarantees is that the debt is
written down, attributable, and reversible on the record with `triage --reopen`.

## MCP server

`skodun mcp` serves the same review loop the CLI does — `gate`, `review`, `log`,
`surface`, and the `triage` operations — to any MCP client, over stdio, as **9
tools** and **2 prompts**. A tool's refusal is worded exactly like the CLI's,
because neither surface owns the words: `services.py` is the one implementation
both call.

Tools: `gate`, `review`, `log`, `surface`, `triage_list`, `triage_dismiss`,
`adopt_refuter`, `triage_reopen`, `triage_defer`. Prompts: `review-now` (run a
review and report findings without triaging any of them) and `gate-check` (ask
whether a trustworthy review covers the current change, and explain the verdict).

`triage_defer` takes a mandatory `tracking_ref` and refuses without a usable one,
in the CLI's words. There is deliberately no `deferrals` tool: reviewing the
deferral backlog is a human's periodic job, not a step in the loop an agent
drives, and an agent that could both file deferrals and mark them handled would be
holding both ends of the audit trail.

`review` takes an optional `reviewer` argument — the name of a configured
`[[reviewers]]` entry to head that one review's chain, exactly as the CLI's
`--reviewer` does, with the same refusals in the same words. An agent has no way to
enumerate the reviewer table (`providers` is deliberately not a tool), so the
refusal for a name it guessed lists the configured entries and their providers.

**`review` is long-running and only one may be in flight per server.** It runs
the whole foreground review pipeline — minutes, real model calls — on a background
thread so the server keeps answering other requests; a second `review` call while
one is running is refused outright (`"review already in flight"`), never queued,
because a queued review would run against a working tree that has since moved.
Closing the client session cancels a review still in progress rather than
abandoning it mid-write.

There is deliberately no `dispatch`, `worker`, `install-hooks`, `import-legacy`,
`shadow-compare`, or `providers` tool, and no bulk triage tool (no
`dismiss_all`/`adopt_all`): those are either machinery a human runs, or decisions
a human makes one finding at a time.

### Upgrading a running server

**A running server keeps serving the build it started with.** Every tool call
opens its own store connection, so data is always current — but the Python
modules were loaded once, at startup. Upgrade skodun (or edit it, in a source
checkout) and the server in your agent's config goes on running the old code
until the client restarts it, silently: no error, no warning, just an older
reviewer than you think you have.

So after upgrading, restart the client's MCP connection (in Claude Code, reload
the session or the server entry; in Codex, start a new `codex` run). To confirm
which build you actually reached, read `serverInfo.version` from the
`initialize` response — it is `skodun.__version__`, the same string
`skodun --version` prints, and a test pins it to the version declared in
`pyproject.toml` so the three can never disagree:

```
$ printf '%s\n' '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' | skodun mcp
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-06-18",...,"serverInfo":{"name":"skodun","version":"0.3.0"}}}
```

If that version is not the one you just installed, the client is still holding
the old process.

### Claude Code

```
claude mcp add skodun -- skodun mcp
```

(use `-- python3 -m skodun mcp` instead if you are running from a source checkout
rather than an installed console script). Or add it directly to a project's
`.mcp.json`:

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

### Codex CLI

In `~/.codex/config.toml`:

```toml
[mcp_servers.skodun]
command = "skodun"
args = ["mcp"]
```

## Known limitations

Recorded so each is a decision rather than a surprise. None of them can make the
gate answer `0` for a change no trustworthy review covers — that is the one
property everything here is arranged around — but each is a real rough edge.

- **Background rounds recorded before store v5 are never delivered.** v5 added
  the `repo` column and backfilled nothing, so every row written by an earlier
  build has `repo IS NULL` — and `repo = ?` matches NULL in no repository. Those
  rounds stay readable by an unscoped `skodun log` and by `skodun triage`, and the
  gate still matches them by content; they will simply never be reported by
  `skodun surface`. The alternative was to guess which tree an old row belonged
  to, and a wrong guess retires that tree's round and kills its worker. See "One
  store per repository, or one store for all of them" above.
- **Round context and churn attribution are presentation-only.** `skodun triage
  --list` and `skodun log` report which review round this is on the branch and
  whether findings land in files changed since the previous trustworthy review
  (R2/R3). That never narrows what the model reviews or what the gate
  certifies — the full outgoing diff is still the unit of trust.
- **The `junie` adapter is macOS-only.** Confinement uses `sandbox-exec`. On
  any other platform a junie reviewer classifies `unavailable` and the chain
  advances (or the review fails closed if junie is alone). There is no
  unconfined soft fallback.
- **Ops verbs `doctor` / `retain` / `schedule` are CLI-only.** The stdio MCP
  server exposes the review loop (`gate`, `review`, triage, `log`, `surface`)
  through the shared service path. Maintenance commands stay out of MCP so no
  scheduler runs inside the MCP process (see `docs/skill-decision-epic-23.md`).

Agent protocol template: `examples/AGENTS.md`. Pasteable fragments:
`examples/fragments/`. **Client MCP + gate wiring (start here for other
repos):** [`docs/integrate-external-project.md`](docs/integrate-external-project.md).
Epic close-out start prompt: `docs/agent-start-epic-23.md`. Post-#23 product
epics (status/cancel, fair capacity): `docs/epics/`.

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
