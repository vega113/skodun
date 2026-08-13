# Recommendations A–I completion design

Exact-main baseline at design time: `6baa7037d8781930c6aad78379c88be1d43d3873`
(schema 14). Installed authority at design time: pipx `skodun 0.4.0`, schema 13,
`skodun_commit=None`.

## 1. Purpose

Finish the original recommendations program and the schema/install authority
work that followed the schema-12/13 incident. Completion means merged main,
closed tracking issues, one installed CLI/MCP authority, an explicit shared-store
migration with backup and receipt, and passing reproductions. Implementation
alone is not completion.

## 2. Live disposition (re-checked)

Open at design time: #141, #142, #147, #148, #149, plus new #164–#167.
No open PRs. Latest exact-main CI green.

Closed children that still have confirmed post-merge defects must not be
reopened. Follow-ups:

| Follow-up | Parent | Scope |
|---|---|---|
| #164 epic | closed #143/#155 | schema/install authority completion |
| #165 | #164, PR #156 | Wave 0: identity, wheel migration, inspection/blockers |
| #166 | #141, PRs #153/#162/#163 | S6 prompt/lineage/surfaces |
| #167 | #164/#143, PRs #154/#157/#158 | S8 coverage/lease/telemetry |
| #147/#148/#149 | #142 | S7 receipts, mutation proofs, Scala pilot |

Unresolved threads on PRs #153, #154, #156–#160, #162, #163 were classified
against `6baa703` and resolved with evidence or a follow-up link. PRs #159 and
#160 had zero unresolved threads. Do not equate those merges with epic
acceptance.

## 3. Mapping A–I

| Rec | Meaning | Landed? | Remaining owner |
|---|---|---|---|
| A | Correct review context and attribution | Partial S6 | #166 then #141 |
| B | First-class stacked-PR awareness | Partial S6 | #166 then #141 |
| C | Stable semantic finding dedup/lineage | Partial S6.2 | #166 then #141 |
| D | Compiler-valid mutation evidence | No | #148 after #147 |
| E | Non-vacuous behavioral proof | No | #148/#149 |
| F | Explicit trustworthy/degraded/topology states | Partial S8 | #167; no new trust axis |
| G | Resumable, bounded, inspectable orchestration | Partial S8 | #165/#167 |
| H | Source-language capability profiles, Scala pilot | No | #149 |
| I | Repository evidence-receipt ingestion | No | #147 then #149 |
| Schema/install | Explicit migration + release identity | Partial #155 | #165 then #164 |

## 4. Invariants

- Fail closed for coverage and trust. Missing/malformed/stale/unverifiable
  evidence cannot increase trust. Caller stack or receipt data never clears
  the gate.
- Do not edit `gate.py` or `trust.py` without explicit owner approval.
- CLI/MCP review-loop verbs stay in `services.py`.
- Checkpoints stay separate from certifying review records until atomic
  finalization.
- Additive `_MIGRATIONS` only. Never extend frozen Phase-1 `_SCHEMA`.
- Preserve legacy `finding_key`, triage, gate, exact-diff reuse, R2/R3,
  routing, recovery, and refuter semantics.
- Runtime remains stdlib-only. Do not bundle Scala/SBT or a hand-written
  language parser as a correctness authority.
- Repository compiler/harness commands come from protected policy and run
  through bounded owned-process execution.
- Do not persist prompts, transcripts, secrets, API keys, full environments,
  or PATH.
- Never run source/editable smoke tests against the default shared database.
- Do not install feature branches or migrate the shared store from unmerged
  code.
- Never kill unknown MCP or worker processes.

Routing remains outside the fail-closed perimeter (AGENTS.md invariant 8).

## 5. Wave 0 — release identity and authority safety (#165)

Serialized. No later schema bump and no shared-store migration until this
merges.

### 5.1 Build identity

A wheel must carry an immutable identity independent of git:

- package version (bump `0.4.0` → `0.5.0` so schema-13 `0.4.0` and schema-14
  source are distinguishable);
- exact 40-character commit written at wheel build time;
- `SCHEMA_VERSION` of the code that produced the wheel.

`code_provenance()` prefers the embedded wheel identity. Checkouts without an
embedded file keep the existing git probe, including `-dirty` / `-unknown`.
`commit=None` remains valid only for an install that truly has neither.

CLI `doctor` and MCP `serverInfo` always expose `version`, `commit`, and
`schemaVersion`. Wheel identity is synchronous, so the MCP handshake must not
omit `commit` merely because the git warmer has not finished.

### 5.2 Migration authentication

`skodun store migrate --apply` authenticates the **installed** identity:

- wheel with a clean embedded commit may apply without a git checkout;
- `--build-commit`, if supplied, must match that identity exactly;
- dirty/unknown/source-without-clean-identity cannot migrate the default
  shared database;
- disposable `SKODUN_DB` remains the development isolation path.

### 5.3 Inspection

`inspect_schema` and blocker reads:

- open with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK` (or equivalent) and require a
  regular file before SQLite;
- never hang on FIFO/symlink/special-file TOCTOU;
- snapshot WAL/SHM onto a disposable copy when needed;
- classify copy/temp/unreadable-sidecar failures as `SchemaInfo(state=invalid)`
  with stable reason codes (`not_a_file`, `symlink`, `snapshot_failed`,
  `unreadable_sidecar`, `temp_unavailable`, `invalid_sqlite`);
- `open_readonly` must not reconnect to a WAL original in a way that creates
  `-shm` beside the authority.

### 5.4 Migration blockers

Classify live work using existing liveness/lease rules **without mutating**
during `--plan`:

- review rows: live PID, or null PID treated as live; missing `pid` column on
  pre-v3 stores is handled without raising;
- checkpoint claims: `state=running` **and** `now < lease_expires_at`;
- capacity admissions: `should_reclaim_admission` (PID/age); dead/stale rows
  are not blockers;
- legacy FG lock at `<git-common-dir>/grok-reviews-foreground.lock`, discovered
  from capacity scopes **and** independently of whether an active capacity row
  exists; dead/unparsable-past-grace locks are not forever-blockers.

`--apply` still refuses live work. It does not reclaim or kill processes.

### 5.5 Artifacts and retry

- Receipt path is schema-version-scoped:
  `<db>.migration-receipt-v{target}.json`.
- Backup remains `<db>.backup-before-v{target}`.
- Failed apply before schema commit removes unverified exclusive-create
  artifacts so retry is possible.
- Successful schema commit with a later receipt-write failure stays
  `receipt_pending`, never a false `migration_failed`.
- Init lock cleanup includes `sqlite3.connect` failure.
- Preserve backup API, integrity check, `0o600`, migration lock ownership,
  crash recovery, bounded receipts.

Ordinary review, gate, MCP startup, doctor, readiness, status, and diagnostics
remain non-migrating.

## 6. Wave 1 — S6 and S8 hardening

After #165 merges. Two lanes may proceed in parallel **only** when their files
do not overlap. Serialize `store.py`, schema migrations, `services.py`,
`pipeline.py`, `cli.py`, and `mcpserver.py`.

### 6.1 Lane 1 / #166 (S6)

- Pass validated stack context into `_prepare_batch_plan` / every
  `promptbuild.build` call, including integration if it builds a prompt.
- Include that block in exact checkpoint prompt identity.
- UTF-8-safe truncation; reserve max stack-context bytes in adapter overhead.
- Make `Prompt.stack_context_truncated` accurate or remove it.
- Bounded prior-fingerprint projection in the provider prompt.
- Mixed deletion-only + added hunks: retain deletion regions as uncertain
  rather than `ownership_unreachable` for the whole manifest.
- Status/log/surface/JSON/MCP expose `fingerprint_status`, candidate count,
  limit, truncation, and lineage summaries.
- Bound lineage by flattened finding candidates; `limit+1` instead of
  `COUNT(DISTINCT ...)`; add a supporting index via additive migration.
- Minimize writer-lock duration; keep lineage+review publication atomic.
- Replace `TypeError` substring fallback with signature/capability detection.
- Re-run the TubeScribes stacked-PR reproduction listed in #141/#166.

### 6.2 Lane 2 / #167 (S8)

- Decode completed checkpoint payloads when projecting usable evidence.
- Map `pending` to queued/pending, never failed.
- Project actual security/skeptic/refuter metadata shapes.
- Unbatched completed finder: `completed_passes=1`, `planned_passes=1`.
- Strict persisted booleans (`delivery.has_usable_output` semantics).
- Preserve finder-only and annotation-only refuter semantics.
- Include configured admission wait in checkpoint leases, or renew while
  waiting.
- Persist original queued/admitted/started/completed timestamps; reuse them
  on completed-checkpoint resume.
- Include integration prompt bytes in aggregates.
- Keep missing timing/token/version unknown, not zero.
- Effective `batch_target_bytes` already matches planning/reuse/orchestration
  identity on current main; keep that pin and cover it through shipped
  CLI/MCP paths.
- Re-run the 3-of-4 timeout/resume reproduction.

After both lanes merge, rebase onto refreshed main and run combined S6/S8
compatibility tests.

## 7. Wave 2 — Epic #142

Strict order: #147 → #148 → #149. Integrate with the landed schema lifecycle;
do not invent a second receipt schema beside S8 migration receipts.

### 7.1 #147 receipts

Canonical repository/base/head/diff/policy/command identity; bounded canonical
JSON and digests; protected producer policy; safe-file handling; replay and
duplicate handling; bounded redacted diagnostics; additive durable/read-model
storage; compact provider context; identical CLI/MCP projections; advisory-only
with no new trust axis.

### 7.2 #148 mutation proofs

State machine: prepare → uniquely select target → mutate intended bytes/symbol
→ compile/validate → execute controls → execute mutant → prove child/sentinel
delivery → prove old-fail/new-pass → restore → verify final tree identity.

Reject at least: absent/multiple/no-op targets; invalid compiler fixtures;
undefined decoys; deleted mutation commands; deleted/substituted fixtures;
child bypass or missing sentinel; missing positive/negative control; false
RED/GREEN; failed restoration; changed final tree; receipt replay on a
non-exact identity.

### 7.3 #149 language profiles / Scala pilot

Protected capability profiles mapping requirements to trusted command IDs;
explicit unavailable/version-mismatch/timeout/unsafe-command reasons; Scala 3
fixture corpus (significant indentation, `given … with`, anonymous/nested
classes, XML literals, backticks, imports/inheritance, root qualification,
quoted syntax, Unicode); real compiler or authoritative harness validation;
ingest preflight/full-gate/mutation/CI/review-thread receipts; compact evidence
selection vs the 127 KB prose case; offline operation and bounded cleanup.
Do not claim Scala correctness from lexical matching.

## 8. Shared seams and sequencing

```text
#165 (store, provenance, cli, doctor, mcpserver, hatch hook)
        |
        +-- #166 (stack, promptbuild, fingerprint, dispatch;
        |         serialized: store, pipeline, services)
        +-- #167 (readmodel, telemetry, checkpoints, reuse;
                  serialized: pipeline, services)
        |
        combined S6/S8 tests on refreshed main
        |
        #147 (receipt envelope; additive store/services)
        |
        #148 (proof runner; uses #147)
        |
        #149 (profiles + Scala corpus; uses #147/#148)
        |
        install immutable build → rehearse copy → migrate shared store
        → restart MCP hosts → client smoke
```

No Wave 2 schema bump until #165 has landed. #147 may add tables through
`_MIGRATIONS` only.

## 9. Test gates (every issue)

1. Focused hermetic shipped-path tests.
2. `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`.
3. Store ResourceWarning sweep whenever store/process/schema lifecycle is
   involved; new `Store.open` callers join `_STORE_TOUCHING_MODULES`.
4. `gate.py` / `trust.py` byte-identical unless owner approval.
5. Exact-head Skodun review/gate against a disposable `SKODUN_DB` for
   source-build smoke; never the shared production store.
6. After merge: focused merged-main smoke; close the issue with PR + evidence.

## 10. Final installation (only after all implementation PRs merge)

1. Build one immutable wheel from the exact clean merged commit.
2. Rehearse migration against a verified copy of the real schema-13 store.
3. Confirm no live review, claim, admission, worker, or migrator.
4. Apply supported migration to the shared store; verify backup and receipt.
5. Install that same wheel as the sole CLI/MCP authority.
6. Restart MCP/scheduler/worker through supported ownership mechanisms.
7. Compare CLI doctor and each MCP `serverInfo` for version/commit/schema.
8. Read-only gate/readiness/status smoke; real client smoke from Skodun and
   TubeScribes without source fallback.
9. Inconclusive → fail closed and preserve recovery instructions.

## 11. Terminal condition

Do not report completion until recommendations A–I have merged-main evidence,
#141/#142/#164/#147/#148/#149/#165/#166/#167 are closed, no related PR is
open, exact-main CI is green, required suites and pins pass, the three
reproductions pass, installed CLI and every MCP authority match, the shared
store was upgraded with backup and receipt, and no competing source authority
remains.
