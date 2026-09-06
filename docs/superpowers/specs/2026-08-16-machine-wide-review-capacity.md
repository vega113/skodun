# Machine-wide review capacity (follow-up to S3/S4)

Date: 2026-08-16. Status: **implement** (semantics fixed by owner; remaining
1-vs-2 default is a conservative pick, not a product fork).
Parent incident: one machine-wide SQLite store plus many `skodun mcp` / CLI
reviewers. S3 left “full host multi-MCP fair queue” out of scope; this
follow-up puts **reviews only** in scope. `gate.py` / `trust.py` unchanged.

## Goal

One number for this Mac that bounds concurrent **reviews** across every repo
that shares the default store, while today’s per-repo `review-fg` stays an
**inner** limit. Same store; `SKODUN_DB` remains the isolation hatch and
**opts out** of machine protection.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Default store | Still one file per machine (`~/.local/share/skodun/skodun.db`) | Fragmenting per repo would defeat the cap |
| Outer resource | `review-machine` in `capacity_admissions`, scope `*` | Existing admission table; does not mix with per-repo `review-fg` rows |
| Outer default | **1** (`SKODUN_REVIEW_MACHINE_CAPACITY`) | Conservative; owner can raise. Junk / missing → 1 |
| Inner limit | Today’s `review-fg` per `git_common_dir` | S3/S4 and the legacy FG lock stay intact |
| Binding rule | `effective_fg = min(machine, repo_fg)` and the outer ticket still binds | A repo env `SKODUN_REVIEW_FG_CAPACITY=8` cannot exceed the machine cap |
| Acquire order | Machine ticket first, then per-repo `review-fg` (then legacy lock) | One order everywhere; reverse on release |
| Cross-MCP | Store FIFO, not in-process memory | Two MCP processes must see each other |
| Same-server MCP | Keep refuse-if-busy | Tree-moved hazard from S3 |
| Provider caps | Unchanged, already machine-wide **on the shared store** | `SKODUN_DB=/tmp/other.db` opts out of provider and review caps |
| Config | Env + optional `[capacity]` in `~/.config/skodun/config.toml`; repo `.skodun.toml` may only **tighten** | Global / env set the machine ceiling |
| Surfaces | CLI `review`, pre-push `dispatch`, every `skodun mcp` via `run_review` → `acquire_for_fg` | One admit path |
| Diagnostics | `skodun stats` and `skodun doctor` show machine cap, holders by repo, holders by provider | Operator-visible |
| Schema | Additive v21 ownership and holder-limit columns | Detect recycled PIDs and retain tighter active caps |

## Knobs

```text
SKODUN_REVIEW_MACHINE_CAPACITY=1          # outer; default 1
SKODUN_REVIEW_FG_CAPACITY=1               # inner per git_common_dir; min()'d
SKODUN_PROVIDER_MAX_IN_FLIGHT=2           # already machine-wide on shared DB
SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY=5
SKODUN_DB=/path/to/other.db               # opts out of machine protection
```

`~/.config/skodun/config.toml`:

```toml
[capacity]
machine = 1
```

Repo `.skodun.toml` (tighten only):

```toml
[capacity]
review_fg = 1
```

A repo value **above** the machine cap is clipped. A repo cannot raise the
machine cap — including when the global file has no `[capacity] machine`
key: the ceiling is then the shipped default of 1, not the repo's number.
`skodun doctor` / `skodun stats` print `resolved_machine_capacity` (env,
then file, then default), the same value `run_review` uses.

## Admit

```text
1. enqueue + FIFO-admit review-machine / scope=* / capacity=machine
2. enqueue + FIFO-admit review-fg / scope=git_common_dir / capacity=min(machine, repo)
3. optional legacy mkdir FG lock (unchanged dual-hold)
4. on any failure after (1): finish the machine ticket
5. on release: drop repo ticket, then machine ticket
```

MCP: a second `review` **on the same server** still refuses in-process. A
`review` on server B while server A holds the machine ticket waits or expires
via the store (same bounded `SKODUN_ADMISSION_WAIT_SECONDS` as today).

## Rejected

- Per-repo default databases (defeats the cap).
- Making the machine cap a new store enum / trust axis.
- Queuing non-review MCP tools.
- Default 2: owner can set 2; shipping 1 is the conservative pick.
- New resource tables: reuse `capacity_admissions`; v21 adds nullable process ownership.

## Verification

- Two different repo scopes sharing one store cannot both run when machine=1.
- Repo `SKODUN_REVIEW_FG_CAPACITY` higher than the machine cap still binds to
  the machine cap.
- Existing same-scope S3/S4 FIFO tests keep passing (inner rule unchanged).
- `SKODUN_DB` pointing at a second file is a separate admission universe.

## Delivery safety refinements

Machine admission retains a slot while its owner PID is live. Queue age and
execution age alone cannot prove that a cross-repo holder has stopped; dead
owners remain reclaimable. Machine and repo tickets both link to the durable
request and use the same foreground queue deadline.

Malformed recovery is restricted to the torn-WAL incident shape, serialized
with schema migration, and rechecked under the lifecycle lock. Private regular
file quarantine preserves original bytes. Replacement requires the complete
current declared schema and at least one review; older, partial, or failed
recoveries remain quarantined for manual restoration. Every observed failed
integrity check is invalid. Normal inspection for opens omits the full scan
unless the torn-WAL signature is present; doctor requests a full check.

The v21 migration adds nullable `owner_start` to admission rows. New machine
tickets record process birth identity; a live PID with a different observed
identity is reclaimable without signalling that process. Missing identity
evidence retains the ticket conservatively. Terminal cleanup retries transient
SQLite operational failures three times with bounded backoff.

Each admitted machine ticket persists its requested capacity limit. Admission
uses the minimum of the incoming limit and every active machine holder limit
inside the same write transaction. A repo's tighter limit remains binding until
its ticket is released; later clients cannot raise it while that holder runs.
