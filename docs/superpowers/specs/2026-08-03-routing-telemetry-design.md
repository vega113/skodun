# Design: Routing telemetry on `skodun providers`

**Date:** 2026-08-03
**Epic:** [S5 #69](https://github.com/vega113/skodun/issues/69) — prerequisite for Phase B (#77)
**Depends on:** S5 Phase A (shipped, PR #83)
**Status:** approved

---

## Problem

Phase A routes un-pinned reviews and records the decision on every artifact
(`requested_reviewer`, `routed_reviewer`, `route_reason`, `client_family`).
Nothing can read those in aggregate. They live only inside `artifact_json`,
there is no indexed column, no query, and neither `providers` nor `doctor`
mentions routing at all.

So an operator cannot answer **"over the last week, how did routing actually
distribute?"** — which is the question that decides whether Phase B's weights
are needed, and the only way to tell afterwards whether they worked. The S5
design gates Phase B on exactly this: *"Ship Phase A telemetry (`route_reason`,
holder counts) before weights."* That gate is currently unmet.

## Goal

One read-only diagnostic that answers "which provider is carrying the load, and
did routing put it there". Nothing else. This is not a scheduler, not a report
format anything parses, and not a gate.

## Non-goals

* Any new persisted state, column, or `SCHEMA_VERSION` bump.
* An MCP tool. `providers` is CLI-only by existing decision (AGENTS.md), and a
  diagnostic that spends no model calls has no reason to become agent surface.
* Weights, shares, or any Phase B mechanic. This measures; it does not steer.
* Changing `providers`' exit-code contract.

---

## `served` vs `routed`: the distinction the surface is built on

The `reviews` table carries two different provider facts, and they **diverge
after a fallback**:

| Field | Means |
|---|---|
| `adapter` | who **actually served** — `_apply` rewrites it to whoever answered |
| `routed_reviewer` | the entry the router **chose as head** |

A routed grok head that fails over to codex leaves `adapter=codex,
routed_reviewer=finder-grok`.

The per-provider count keys on **`adapter`**, and is labelled `served=`, because
the question Phase B needs answered is who actually burned the subscription. The
footer's per-entry counts key on `routed_reviewer`, i.e. what the router
decided. The two deliberately do not add up, and the gap between them is the
fallback rate — a signal worth having, not a discrepancy to reconcile.

---

## Output

```
routing: mode=auto pool=all-enabled-finders cross_model=on window=7d
xai        | adapter=grok  | binary=… | state=none | holders=0 | served=12/40 (auto 8, pinned 3, unrouted 1)
openai     | adapter=codex | binary=… | state=none | holders=0 | served=28/40 (auto 24, config 2, unrouted 2)
…
routing decisions (7d): auto:free 26, auto:wait 4, auto:free+cross 2, pinned 3, config-finder 2, unrouted 3
routed head (7d):       finder-codex 24, finder-grok 11, finder-agy 2
```

A header line for the effective routing config, the existing per-provider lines
with one bit appended, and two footer lines.

The numbers above are internally consistent, and the way they reconcile is the
point: 40 reviews in the window; `served=` splits them by provider (12 + 28);
`routing decisions` splits the same 40 by reason; `routed head` totals 37,
because the 3 `unrouted` records have no chosen entry to attribute. That
`routed head` does not match `served=` per provider is expected — the gap is
the fallback rate.

`pool=` shows `all-enabled-finders` when `[routing] pool` is empty, and the
configured names otherwise.

Output is **ASCII only**, separators included. `cli._emit` exists partly to keep
a `UnicodeEncodeError` from an ASCII-only locale meeting a non-ASCII message
from turning an exit code into the interpreter's 1, and no other `providers`
output uses non-ASCII.

An empty window is not reported as `served=0/0` on every line: the per-line bit
is omitted entirely and the footer says so once.

### Buckets on the provider line

| Bucket | `route_reason` |
|---|---|
| `auto` | any `auto:*` |
| `pinned` | `pinned` |
| `config` | `config-finder` |
| `unrouted` | absent |

`unrouted` covers two populations on purpose: records written before S5, and
background pre-push reviews (the worker does not route). Both consumed a
provider slot, so both belong in the denominator; neither was a routing
decision, so neither belongs in `auto`.

### Footer

`routing decisions` reports exact `route_reason` values rather than buckets —
`auto:free` and `auto:wait` are the difference between "spreading works" and
"everything is saturated", and `auto:free+cross` is the only way to see whether
`cross_model` is earning its keep. `routed head` reports `routed_reviewer`
counts, which is where two entries sharing one provider become visible.

---

## Data path

One new read-only `Store` method:

```python
def routing_counts(self, *, since_iso: str) -> list[dict]
```

Returns `(adapter, route_reason, routed_reviewer, n)` rows from a single
grouped query, extracting the routing fields with SQLite's `json_extract` over
`artifact_json`. JSON1 is available (SQLite 3.51.2 on the current toolchain).

No schema change: the fields are already persisted. Grouping and the window
filter both happen in SQL, so no artifact is decoded in Python — unlike
`list_reviews`, which decodes every row it returns.

The query is scoped to `source = 'skodun'`. This was found by running the
surface against a real store rather than reasoned about up front, and it is
load-bearing: a store that has run `import-legacy` holds the old grok-reviews
archive, which on the author's machine outnumbers skodun's own reviews five to
one and carries no adapter at all. Unscoped, the denominator was 1126 against
191 attributable reviews, and grok's real 28% share printed as 5% — the exact
number this surface exists to get right.

The window filter is `reviewed_at >= since_iso`, correct as a string comparison
because store timestamps are fixed-width canonical UTC. `reviewed_at` is not
indexed on its own (only `(branch, reviewed_at)`), so this is a table scan. That
is the right trade for a diagnostic at these row counts; the docstring says so
rather than the schema gaining an index for it.

### CLI

`skodun providers --since-days N`, default `7`, integer ≥ 1.

---

## Error handling

The `holders=` precedent, exactly: a guarded read whose failure **omits the
bit** rather than failing the command. An operator running a diagnostic because
something is wrong must not be refused an answer about the parts that still
work.

`providers`' exit contract is unchanged — `0` normally, `1` only for a config
naming a provider with no registered adapter, `2` for a `--repo`, config, or
store that could not be read at all.

---

## Testing

**Store** (`tests/test_store.py`): window boundary (a review exactly at the
cutoff, one before it), `route_reason` absent → its own group, grouping across
adapter × reason, and an empty store.

**CLI** (`tests/test_cli.py`, beside the existing `providers` tests): bucket
arithmetic on the provider line, the footer's exact-reason and per-entry counts,
`--since-days` validation, degradation to an omitted bit when the query raises,
and that none of it moves the exit codes.

---

## What this unblocks

Run it for a week on a real multi-provider machine. The distribution it reports
answers the question Phase B cannot currently ask: whether one provider is
carrying a disproportionate share, and whether that is routing's doing or
agents pinning by habit. Phase B's own open question — what unit "usage" is
measured in for flat-rate subscription CLIs, where no token or cost signal
exists — is a decision that should be made against those numbers rather than
in the abstract.
