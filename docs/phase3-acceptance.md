# skodun Phase 3 — Live Acceptance Evidence

Date: 2026-07-30. Branch `claude/eager-kilby-676c3d` (epic #3), from Task 1
(`0cef05c`) through the Task 16 docs commit (`914b813`). Executed by the
orchestrating session directly, per the plan: real pushes to a disposable
remote, real detached workers, real model calls, a real installed MCP client.

## 0. Prerequisites, and the provider substitution this run had to make

Safety rules from the Phase 2 runbook were followed verbatim: `SKODUN_DB`,
`SKODUN_CONFIG`, and `GIT_CONFIG_GLOBAL` pinned to scratch paths for every
command; a throwaway repo and a local bare "remote"; no `auth login` flows.
Pinning `GIT_CONFIG_GLOBAL` matters twice over on this machine: the real
global config carries a leaked `core.hooksPath=/tmp/fake-global-hooks`
(pre-existing; flagged to the owner) which would otherwise swallow
`install-hooks`.

Provider availability, checked live immediately before the run:

| provider | CLI | version | status |
|---|---|---|---|
| `openai` | `codex` | codex-cli 0.144.5 | **works** — probe round-trip and every drill review below |
| `google` | `agy` | (responds) | **works** — `agy models` lists `gemini-3.6-flash-low` |
| `xai` | `grok` | grok 0.2.114 | **dead** — HTTP 402, usage balance exhausted (same as Phase 2) |
| `anthropic` | `claude` | 2.1.118 | headless `-p` **not logged in** (`Not logged in · Please run /login`) |

**SUBSTITUTION — recorded, not silent:** finder = `codex`
(`openai`/`gpt-5.4-mini`, effort low) with fallback `agy`
(`google`/`gemini-3.6-flash-low`). The MCP drill's live client is `codex`
(the `claude` CLI cannot run headless here; its real `initialize` handshake
was still captured for the Task 13 fixtures via a stub server —
`protocolVersion "2025-11-25"`, clientInfo `claude-code 2.1.118`).

One deliberate time compression, used once and marked where it happens (§4):
stale recovery fires on a persisted wall-clock budget of 5160 s; the drill
backdates the SIGKILLed record's `reviewed_at` instead of waiting 86 minutes.

## 1. Full suite, both modes, counts reconciled

At the Task 15 checkpoint commit (`59f3639`), controller-run:

```
$ SKODUN_ORACLE_DIR=<oracle> python3 -m pytest -q
2793 passed, 1 skipped in 632.46s (0:10:32)

$ python3 -m pytest -q          # oracle unset
2634 passed, 160 skipped in 806.93s (0:13:26)
```

(After the final-review fix commit `110e435`, which added `surface --repo`
and its tests, the with-oracle suite is **2803 passed, 1 skipped**; the
reconciliation below is unchanged in kind.)

Both modes collect 2794 tests. Without the oracle, 160 skip: 159
oracle-gated (they RUN with it: 2634 + 159 = 2793) plus the 1 skip present
in both modes — the seam matrix's single documented N/A cell
(`mcp × pipe_head`, reason string in `tests/test_seams.py`). The counts
reconcile exactly; no skip is silent. Phase 1/2 parity and conformance tests are
untouched (no shipped test file was edited this phase except the two edits
Tasks 8/14 declared in their commit messages).

## 2. Live push drill

A real `git push` of a feature branch through the installed shim; the record
below is the drill transcript, verbatim (ids and hashes are the real ones).

```
$ git push origin feature/div-guard
skodun: feature/div-guard: reviewing 1 file(s) vs origin/main as sk_20260730T142827Z_93162_fa0b7c45 in the background
 * [new branch]      feature/div-guard -> feature/div-guard
# push returned immediately; the store already held:
sk_20260730T142827Z_93162_fa0b7c45|feature/div-guard|running|5160|93223
#                                                    ^status  ^budget ^worker pid
# ~40s later, with no foreground process involved:
sk_20260730T142827Z_93162_fa0b7c45|feature/div-guard|clean|1|1|1|gpt-5.4-mini
```

`skodun surface` delivered it exactly once, then never again:

```
$ skodun surface --branch feature/div-guard
  - 2026-07-30T14:28:27Z sk_20260730T142827Z_93162_fa0b7c45 (head b0ab2a634): 1 finding(s) -- 0 high / 1 medium / 0 low
      [0] medium calc.py:7 [div-guard] `mean()` raises on empty input
$ skodun surface --branch feature/div-guard
skodun surface: no undelivered background review rounds on branch feature/div-guard
# deliveries ledger: sk_20260730T142827Z_93162_fa0b7c45|cli-text
```

Identity replay: the disposable remote's ref was deleted (an up-to-date push
sends the hook empty stdin, so replay needs a remote that lacks the ref) and
the identical content pushed again — dedup-suppressed inside the reservation
lease, with the audit row committed in the same transaction:

```
$ git -C remote.git update-ref -d refs/heads/feature/div-guard
$ git push origin feature/div-guard
skodun: feature/div-guard: diff bcd667eecc3f... is already covered by review sk_20260730T142827Z_93162_fa0b7c45; skipping
# dedup_events: 2026-07-30T14:29:14Z|feature/div-guard|sk_20260730T142827Z_93162_fa0b7c45
# reviews table: still exactly one row for that content
```

A further push after an edit reviewed again (`sk_20260730T142923Z_99590_fd675264`,
finalized `clean`, base = the existing remote branch oid per the Task 10
deviation).

## 3. Failure surfacing drill

A branch whose config names ONE finder with no fallback, pushed with that
finder's binary pointed at a nonexistent path (`SKODUN_CODEX_BIN`). The
worker ran, the chain exhausted, the record failed durably, and `surface`
says so in the reserved wording — never silence:

```
sk_20260730T142955Z_1773_9cda1f56|failure/dead-binary|failed|0
$ skodun surface --branch failure/dead-binary
  - 2026-07-30T14:29:55Z sk_20260730T142955Z_1773_9cda1f56 (head 7539a054e): NO REVIEW HAPPENED — this round reports nothing because it said nothing, not because it found nothing
      failure_reason: all providers unavailable: finder/openai: binary not found (rc 127)
```

## 4. Race and crash drills

**Supersede race** — two pushes of different content in quick succession
(the second forced while the first's worker was still reviewing):

```
$ git push origin race/two-pushes            # reserves sk_...143022Z_2647_deeab08d
$ git commit --amend ... && git push --force  # ~2s later
skodun: superseded an older running review (sk_20260730T143022Z_2647_deeab08d); signalled its worker (pid 2674)
# final state — exactly ONE non-superseded terminal review per content:
sk_20260730T143022Z_2647_deeab08d|superseded|sk_20260730T143024Z_2716_db4517f2|0
sk_20260730T143024Z_2716_db4517f2|clean||1
```

**SIGKILL recovery** — a live worker SIGKILLed mid-review left a `running`
row nothing would ever finish. Two facts, both live:

1. Backdating only the INDEXED `reviewed_at` did NOT trigger recovery
   (`recover_stale swept: 0`) — the artifact is the authority, tampering
   with the index alone changes nothing.
2. With `reviewed_at` backdated in both places (the recorded time
   compression: 5160 s persisted budget vs an 86-minute wait):

```
skodun: recovered stale review sk_20260730T143049Z_3887_59b012ca (older than 5160s) as failed
$ skodun gate --repo <repo>       # on that branch's content
SKODUN GATE: FAIL(2) no trustworthy review covers diff_hash=fc799d0a2a19 -- run a review before pushing
```

(Before the sweep the gate ALSO answered 2 — a `running` record certifies
nothing; recovery converts an open lie into a durable failure, not a pass
into a fail.)

## 5. Batching drill

All three legs live against `max_diff_bytes = 6000`, through the dispatcher.

**Clean over-budget diff** (21 KB, 8 files, hunk-sized changes): 8 batches +
the cross-file integration pass, ONE aggregated artifact at the FULL diff's
identity, trustworthy:

```
{'batched': True, 'batch_count': 8, 'parse_ok': True, 'degraded': False,
 'diff_truncated': False, 'trustworthy': True, 'findings_total': 2}
integration: {'ran': True, 'parse_ok': True, 'status': 'ran'}
```

Four batches carried `splitter_truncated=True` floors that still fit their
prompts whole and demoted NOTHING — the Task 8 prompt-level-truncation rule
observed live, exactly as recorded in the plan's Deviations.

**Seeded truncated batch** (an earlier 27 KB variant whose four generated
files were each ONE ~6.9 KB hunk — an accidental, and therefore honest,
seeding): every floor was also prompt-cut, and the aggregate failed closed:

```
sk_20260730T143223Z_8038_fbdba86d|failed|0
{'parse_ok': True, 'degraded': False, 'diff_truncated': True, ...
 'summary': 'batched review: 5 batch(es); + cross-file pass; 0 finding(s); truncated hunk(s)'}
$ skodun gate ... -> SKODUN GATE: FAIL(2)
```

**Seeded integration failure** (an `integrator`-role reviewer with a dead
binary and no fallback; batches on the live finder): all 4 batches answered,
the seam pass could not, and partial evidence is surfaced under the
incomplete warning — never the reserved line, never a pass:

```
{'batch_count': 4, 'parse_ok': False, 'trustworthy': False, 'usable_output': True,
 'failure_reason': 'one or more batches were not reviewed (integration); all providers unavailable: integrator/google: binary not found (rc 127)'}
batches parse_ok: [True, True, True, True]
$ skodun gate ... -> SKODUN GATE: FAIL(2)
$ skodun surface ... -> "INCOMPLETE REVIEW -- this round did not finish and cannot certify anything; ..."
```

## 6. MCP drill

A real installed client — `codex` 0.144.5 — connected to `skodun mcp`
(configured via `mcp_servers.skodun` with `command=python3`,
`args=["-m","skodun","mcp"]`), listed the tools, ran `gate` (status 1 with
the finding open) and `triage_list` (status 0), and performed ONE audited
single-finding dismissal (status 0). The CLI then showed the identical
ledger state:

```
$ skodun triage --list sk_20260730T142827Z_93162_fa0b7c45
[0] medium calc.py:7 [div-guard] `mean()` raises on empty input (DISMISSED 2026-07-30T14:38:53Z)
$ skodun gate --repo <repo>
SKODUN GATE: PASS 1 finding(s), all triaged on review sk_20260730T142827Z_93162_fa0b7c45 for diff_hash=bcd667eecc3f
# triage_events: 1|dismiss|guarded upstream: every caller validates non-empty input before calling mean
```

The server's exact tool list, captured over raw stdio in the same
environment (matches the pinned snapshot test):

```
['adopt_refuter', 'gate', 'log', 'review', 'surface', 'triage_dismiss', 'triage_list', 'triage_reopen']
```

Protocol suite: `tests/test_mcpserver.py` (67 tests) drives the server over
pipes with transcript fixtures — garbage bytes, unknown methods, malformed
params, batch arrays, pre-init calls, id-less request-only methods, a
runtime-generated 8 MiB oversized line — and asserts stdout parses
line-by-line as JSON-RPC with ZERO residue. Green in both suite modes above.
The version-negotiation fixtures are REAL captured handshakes
(claude-code 2.1.118: `2025-11-25`; codex 0.144.5: `2025-06-18`), provenance
in `tests/fixtures/mcp/README`.

## 7. Reopen drill

The adopted dismissal from §6 reversed with an audited reason; the gate
flipped 0 → 1; history keeps BOTH records in seq order:

```
$ skodun triage --reopen sk_20260730T142827Z_93162_fa0b7c45 0 "callers do not all validate: the batch importer feeds mean() straight from parsed CSV rows"
skodun triage: reopened finding 0 on review sk_20260730T142827Z_93162_fa0b7c45; it counts as open again
$ skodun gate --repo <repo>
SKODUN GATE: FAIL(1) 1 finding(s) open on review sk_20260730T142827Z_93162_fa0b7c45
$ skodun triage --list sk_20260730T142827Z_93162_fa0b7c45
[0] medium calc.py:7 [div-guard] `mean()` raises on empty input (REOPENED 2026-07-30T14:39:34Z, dismissed 2026-07-30T14:38:53Z)
# triage_events, in seq order:
# 1|dismiss|guarded upstream: every caller validates non-empty input before calling mean
# 2|reopen|callers do not all validate: the batch importer feeds mean() straight from parsed CSV rows
```

## Verdict

All seven acceptance criteria demonstrated live, none asserted from tests
alone. Every drill artifact (store, repo, remote) lived under a scratch
directory and is disposable; nothing touched `~/.local/share/skodun`,
`~/.grok`, or any real repository.
