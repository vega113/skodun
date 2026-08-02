# skodun Phase 5 — the `junie` adapter (vendor-and-adapt)

Date: 2026-08-01. Status: design for epic #23; scope decisions recorded
below so a later cut is a deliberate amendment, not an unstated drift.
Prerequisite reading: `README.md` (Known limitations, gate contract),
`docs/phase5-handoff-prompt.md`, the Phase 4 design
(`2026-07-31-skodun-phase4-design.md`), the review-round-cutoff design
(`2026-07-31-review-round-cutoff-design.md`), and — via `$SKODUN_ORACLE_DIR`
only — the oracle junie design
`docs/superpowers/specs/2026-07-28-junie-review-fallback-design.md` §§4, 6, 7,
12.

## Scope

Phase 5 is the **junie adapter phase**. One capability, security-critical,
deliberately deferred twice:

1. Register a provider-neutral **`junie` adapter** under the existing
   `Adapter` protocol and conformance suite.
2. **Vendor-and-adapt** the oracle's production containment rather than
   redesign it: empty review capsule, deny-by-default macOS Seatbelt profile,
   sanitized environment (12 operator/provider keys stripped; only
   `JUNIE_API_KEY` optionally forwarded), managed-install binary resolution,
   descriptor-confined reads of capsule artifacts, post-run mutation checks
   before any envelope is trusted.
3. Fail closed off macOS and whenever confinement cannot be established —
   never run junie unconfined as a soft fallback.
4. Document the new provider in `examples/multi-provider.toml` and the README
   provider surface; leave the model-facing review prompt byte-pinned.

**No store schema change.** v5 remains current. There is no v6 in this phase.

**`gate.py` and `trust.py` stay byte-identical** to the Phase 3/4 pins:

| file | sha256 |
|---|---|
| `src/skodun/gate.py` | `62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f` |
| `src/skodun/trust.py` | `8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1` |

## Explicit cuts (and why)

| Candidate | Decision | Why |
|---|---|---|
| **R2 churn attribution** | Cut to a follow-up issue | Cheap, no schema, high ergonomic value — but orthogonal to the security surface and easy to ship alone. Bundling it with Seatbelt code inflates review blast radius without coupling benefit. |
| **R3 round context** | Cut to a follow-up issue | Same reason as R2; both live in the review-cutoff design and need only store reads + git. |
| **Worker-log pruning** | Cut | Operational debt; does not block a provider. README Known limitations stays honest. |
| **Pre-push shim stdin-buffer check** | Cut | Correctness polish on a rare full-disk path; not coupled to junie. |
| **Quadratic JSON scan** | Cut | Deferred since Phase 3; still not the phase's value. |
| **Scheduling, retention, doctor, generic openai-compatible / custom-command adapters, local models, cloud-bot embeds, macOS notifications, rules-registry sync** | Cut | Explicitly still-uncut from earlier phases; none is required to land a fourth provider. |

These cuts are part of Phase 5 success, not a deferral of the epic's spine.
Epic #23 remains open for the cut items; this phase owns only the junie row.

## Rejected alternatives

### A. Junie's built-in `--review` mode

Rejected by the oracle design (§4.1) and rejected again here: target selection
is derived from repository Git state, there is no explicit base/head, and the
JSON envelope is Markdown rather than `GROK_REVIEW_SCHEMA`. Exact prompt
parity and deterministic normalization are harder to prove.

### B. Generic junie task in the real worktree

Rejected by the oracle (§4.2): junie is an agentic coding tool without agy's
hard `--disallowed-tools`. A prompt-only review must not receive a mutable
production checkout.

### C. Re-derive containment from first principles in skodun

Rejected: the oracle containment is production-hardened (including #3514
Seatbelt hardening) and its landmine checklist (§12) is the list of ways
*this exact containment* was got wrong the first time. A re-derivation
recreates those failure modes. Port is vendor-and-adapt against skodun's
`Adapter` protocol, same method as grok/codex/agy.

### D. Extend `runner.run_with_watchdog` / `chain.run_chain` with junie-only hooks

Rejected: the chain is provider-neutral. Junie-specific capsule lifecycle,
env sanitization, and post-run mutation checks live inside the adapter's
invocation surface (a thin outer runner process that the chain already
knows how to spawn, watch, and read). `gate.py` / `trust.py` / the runner's
watchdog contract stay untouched.

### E. Soft-fallback to unconfined junie on non-macOS

Rejected: skodun ships cross-platform, and the whole point of this adapter is
the confinement. Off-macOS (or missing `sandbox-exec`) the adapter refuses
before inference with a classified `unavailable` / clear detail — never a
quiet unconfined run.

### F. Put the packed prompt on argv

Rejected (oracle landmine): junie takes the prompt on **stdin**
(`--input-format text`) specifically to avoid `ARG_MAX`. `prompt_limit()`
is therefore `None` (file/stdin path), matching codex.

## Architecture

### Registry

```text
provider id:  "junie"
adapter name: "junie"
env override: SKODUN_JUNIE_BIN
default bin:  "junie" on PATH
```

Added to `adapters._REGISTRY` and to `NORMAL_STOP_REASONS` only if junie
exposes a stable normal-completion word; if the envelope has **no** completion
signal equivalent to codex's `turn.completed` / grok's `EndTurn` / agy's
`SUCCESS`, `stop_reason` stays `None` and `classify` **fails closed to
`degraded`** whenever a payload is accepted without positive run-health
evidence — matching the epic's probe note. Prefer positive evidence
(`llmUsage` present with the configured model, rc 0, capsule clean) over
inventing a stop word the CLI does not emit.

### Module layout

```text
src/skodun/adapters/
  junie_confined_io.py   # descriptor-confined reads (oracle port)
  junie_sanitized.py     # Seatbelt profile, env, binary resolve, exec helpers
  junie_runner.py        # outer process: capsule stage, spawn, normalize → stdout
  junie.py               # Adapter: build_cmd / parse / classify / effort_map
```

Genericity: no machine paths, no tubescribes/oracle private surfaces in
defaults. Capsule marker prefix is `skodun-junie-review-capsule-v1:` (not the
oracle's project-specific marker). Prompt append for schema direction uses
the same generic `REVIEW_CONTRACT` schema skodun already owns — not an
oracle-private constant.

### Invocation contract (pinned)

Mirrored from oracle §6, adapted to skodun's runner:

1. `build_cmd` stages an empty capsule under the system temp dir
   (`skodun-junie-review.XXXXXX/capsule/...`), copies the prompt file into
   the capsule, writes `{"brave": false}` config, returns an argv whose
   `argv[0]` is `sys.executable` running `skodun.adapters.junie_runner` under
   `-I` (isolated mode).
2. `stdin_from_prompt_file = False` — the runner child opens the capsule
   prompt itself (the chain must not also open the original prompt as
   stdin).
3. The outer runner:
   - refuses non-darwin / missing `sandbox-exec` before spawn;
   - builds the Seatbelt profile into the capsule;
   - spawns `sandbox-exec -f <profile> <resolved-junie> …` with a sanitized
     env (only PATH, HOME=real account home, JUNIE_*, TMPDIR, JAVA_TOOL_OPTIONS,
     LANG/LC_ALL; optional `JUNIE_API_KEY` if present in the parent env;
     never OPENAI/ANTHROPIC/GOOGLE/GEMINI/XAI/GROK/OPENROUTER/LITELLM/GH/
     GITHUB/HEROKU/EMAILIT keys);
   - uses `--input-format text`, `--output-format json`,
     `--json-output-file` inside the capsule, empty `--project`, model and
     effort from the reviewer entry, timeout from defaults, every discovery
     location disabled or redirected into the capsule, `--skip-update-check`;
   - on child exit, descriptor-confined-reads the envelope and optional
     `project/review.json`, applies the trust contract (§ below);
   - on acceptance, writes a single JSON object on stdout that is
     `REVIEW_CONTRACT`-shaped (`summary` + `findings`) so `parse` stays a
     normal envelope extract — no chain special case;
   - on refusal, writes nothing usable on stdout, a sanitized reason on
     stderr, and a non-zero rc;
   - always deletes the capsule root on the way out (best-effort; a failed
     delete never upgrades a refusal to acceptance).
4. `parse` / `classify` operate only on the outer runner's stdout/stderr/rc,
   same totality rules as the other adapters. Classification never reads
   model-authored prose for a verdict.

### Trust contract for acceptance (oracle §7, projected)

The outer runner accepts an envelope only when **all** hold:

1. child exited 0 within the watchdog (the chain's watchdog still bounds the
   outer process; a hang truncates stdout as today);
2. envelope is valid JSON object, confined-read from the capsule;
3. either (a) exactly one regular non-symlink single-link `project/review.json`
   whose content validates against `REVIEW_CONTRACT`, with `changes`
   reporting only that file, or (b) no `review.json`, empty `changes`, and
   `result` normalizes to a `REVIEW_CONTRACT` object (direct JSON, fenced
   JSON, or the structured Markdown form the oracle already accepts);
4. when `llmUsage` is present it is a non-empty array of objects with
   non-empty `model` strings, at least one model evidences the configured
   model (normalized token match), and no entry names gemini/grok;
5. the project tree contains no symlinks and only `review.json` plus
   disposable `.junie/**` metadata;
6. capsule paths never contain whitespace (Seatbelt / argv safety).

Anything else is refusal → outer rc ≠ 0 → `classify` lands `unavailable` or
`degraded` from stderr signals / empty payload, never a trustworthy clean
review.

### Effort and model

- Canonical efforts map: `low|medium|high` pass through; `max` is loud
  `ValueError` in `build_cmd` (same pattern as agy) unless a live probe of
  the installed junie proves a distinct spelling — the plan's first probe
  step records which. `none` omits `--effort`.
- Model is always explicit from the reviewer entry (`-m` / `--model`); no
  silent default to Luna in config defaults. Examples may show
  `gpt-5.6-luna` as a known-good id; committed code does not hardcode a
  single project's preferred model as the only allowed value. Usage
  evidence in the trust contract is checked against the **configured**
  model for that attempt.

### Conformance

`tests/test_adapter_junie.py` subclasses `AdapterConformance` with provider
`"junie"` and a full fixture set (`healthy`, ≥2 `degraded` incl. stderr,
`unavailable` + `unavailable_quota`, `healthy_noisy_stderr`,
`refuter*healthy*`). Quota fixture wording is binary-sourced or
documented-synthesis per the conformance rule's provenance requirement;
README in the fixture directory records which.

Oracle-gated parity tests (if any) skip without `SKODUN_ORACLE_DIR` and
reconcile in the suite counts.

### Config / docs surfaces

- `examples/multi-provider.toml`: a commented `[[reviewers]]` block for
  `provider = "junie"` with the empty-capsule / macOS-only caveats in prose
  next to it (mirror the google footgun section's style).
- README: providers list and Known limitations — add "junie requires macOS
  Seatbelt confinement; off-macOS the adapter refuses" and remove junie from
  any "still uncut" wording if present.
- Version: bump to `0.4.0` (new provider is a minor capability).

## Non-negotiables restated

- Runtime stdlib-only, Python ≥ 3.12; pytest the only dev dependency.
- Fail closed: unexpected exceptions in the review path map to exit 2
  semantics upstream; a failed junie attempt is never a clean review.
- Committed code fully generic; oracle only via `SKODUN_ORACLE_DIR`.
- Tests pin `SKODUN_DB`, `GIT_CONFIG_GLOBAL`, every `SKODUN_<X>_BIN` to tmp.
- No edits to `gate.py` / `trust.py`.
- No store v6.

## Landmine checklist (ported and skodun-specific)

From oracle §12, plus skodun seams:

- [ ] Do not pass the real worktree as junie's `--project` or as the child's
      cwd for the model process (outer runner may start in the repo; the
      sandboxed junie chdirs into the capsule project).
- [ ] Do not put the packed prompt on argv.
- [ ] Disable every documented default discovery location explicitly.
- [ ] Do not inherit operator/provider credentials (the 12-key strip) in
      production or fixtures; never put `JUNIE_API_KEY` on argv or in logs.
- [ ] Require the trust contract before writing REVIEW_CONTRACT JSON to
      stdout; a validation failure must not emit a usable payload.
- [ ] Kill the junie process group on timeout (runner already does this for
      the outer process; outer runner must also reap its sandboxed child).
- [ ] Off-macOS / missing sandbox-exec: refuse before inference.
- [ ] Capsule marker and paths use the skodun-generic prefix; no
      tubescribes-private strings in shipped code.
- [ ] `build_cmd` failures that are config (unknown effort) stop the chain;
      capacity/platform refusal classifies unavailable and may advance a
      fallback chain.
- [ ] Conformance coverage gate must see a `TestJunieConformance` subclass.
- [ ] Do not touch `gate.py` / `trust.py`.
- [ ] Commit before any mutation experiment; name the killing test.

## Acceptance criteria

1. `get_adapter("junie")` returns `JunieAdapter`; `skodun providers` lists it.
2. Conformance suite green for `"junie"` with the required fixture set.
3. Unit tests kill: unconfined non-macOS path, credential inheritance,
   unexpected capsule file, wrong-model usage, symlink project entry,
   missing sandbox-exec, prompt-on-argv absence, discovery flags present.
4. Full suite green with and without `SKODUN_ORACLE_DIR`, pass/skip deltas
   reconcile.
5. Seam hashes for `gate.py` / `trust.py` unchanged.
6. Design cuts (R2/R3/ops debt) are filed or already tracked under #23, not
   silently forgotten.

## What this phase deliberately does not change

The gate contract, triage ledger, MCP tool set, store schema, pre-push
dispatch, and the model-facing review prompt. Junie is another provider
behind the same surfaces; clients configure it like any other reviewer.
