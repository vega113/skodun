# S5 Phase B — weighted / credit-based routing (design amendment)

Amends `2026-08-03-provider-auto-route-design.md`. Closes the Phase B half of
epic #69, tracked as #77.

Phase A shipped `[routing] mode = "auto"`: an un-pinned review is headed by the
pooled finder with the best store-visible load — free `provider:<id>` slots
first, shortest queue second, a soft cross-model bonus as a tie-break. Phase B
is the question Phase A deliberately left open: **what happens when the
providers are not interchangeable.**

---

## 1. The question that blocked this, and its answer

The Phase B tracking issue was gated on telemetry (`skodun providers
--since-days N`, shipped in #85) settling one question:

> what unit "usage" is measured in. There is no token or cost signal for
> flat-rate subscription CLIs — `api_spend_events` is written for `openai-api`
> only — so the candidates are reviews served, provider wall-clock occupancy,
> or operator-declared effective daily slots.

Waiting for that sample cannot answer it, and this amendment records why.

The quantity an operator actually wants to balance is *how much of each
provider's allowance a review consumes*. For a flat-rate subscription CLI that
quantity is **not observable to skodun at any window length**: the provider
publishes no balance, the CLI reports no cost, and the same prompt costs a
different fraction of a different subscription tier. More weeks of `served=`
counts measure how load was *distributed*, never how much it *cost* — so the
sample can validate a policy but cannot supply the missing unit.

Of the three candidates:

| Candidate | Observable | Why not the unit |
|---|---|---|
| Provider wall-clock occupancy | yes | Measures how *slow* a provider is, not how much of it an operator may spend. A model that thinks for ten minutes would be rationed for being thorough. |
| Reviews served | yes | The only honest **denominator**, and it is what `routing_counts` already counts. Not a numerator: it says how work was split, not what the split should be. |
| Operator-declared share | no — *declared* | The operator is the only party that knows the tiers, the prices and the other things drawing on each subscription. |

**Decision: the operator declares the share; the store counts reviews served
to measure against it.** Weights are a statement of intent, not a measurement
skodun pretends to derive. This is also the smallest thing that can be wrong:
a weight nobody sets changes nothing, and a weight that is wrong is wrong in a
way its author can see in `skodun providers`.

## 2. Config

```toml
[routing]
mode                = "auto"
weights             = { xai = 3, google = 1 }   # provider id -> share
weights_window_days = 7                          # default
```

* Keyed by **provider id**, not reviewer name. Weights are about a
  subscription, and two `[[reviewers]]` entries on one provider draw on the
  same one. (`pool` is keyed by name for the opposite reason: it selects an
  entry, and an entry carries a model.)
* A provider absent from a non-empty `weights` table has weight **1**, so
  raising one provider does not require listing every other.
* `weights = {}` (the default) disables the term entirely. `mode = "auto"`
  without weights behaves exactly as Phase A did, byte for byte.
* Zero and negative weights are **refused** at config load. Zero reads as
  "never route here", which is what `pool` and `enabled = false` already say
  explicitly; accepting it would add a third, silent way to exclude a provider.
* A weight for a provider no configured reviewer uses is refused, for the
  reason `[routing] pool` refuses an unknown name: it is a typo that would
  otherwise do nothing, quietly.

## 3. Scoring

`share_targets` turns weights + served counts into, per provider:

```
target = weight / sum(weights over the pool's providers)
actual = served / served by those same providers in the window   (0 if none)
```

**Both denominators are this run's candidate set**, and that is load-bearing
rather than incidental: their difference is the whole signal, so a `target`
over the pool and an `actual` over every provider the install has would not be
comparable. Reviews served by a provider that is not a candidate today — a
finder kept out of an explicit `pool`, a `role = "refuter"` entry nothing
routes to — would then shrink every candidate's `actual` while the targets
still summed to one, so every candidate would read as owed work. That is not a
decision this router can act on: it chooses between the candidates it has, and
a review some non-candidate served is not work they can rebalance.

and `score_candidate` adds `WEIGHT_SHARE_SCORE * (target - actual)`, rounded.

`WEIGHT_SHARE_SCORE = 24`, and the number is the policy:

```
free capacity   100 per free slot
declared share  ±24            <- new
cross-model      20
queue depth     -10 per waiter
```

* **Share can never cross a free-slot boundary.** The deficit is in `[-1, 1]`,
  so the largest gap it can open between two candidates is 48, and one free
  slot is 100. A provider that can start *now* still wins, whatever the
  weights say — the same guarantee Phase A gives `cross_model`, for the same
  reason: a review that starts now finishes sooner than any prediction about a
  queue.
* **`24` is a coefficient, not a flat bonus, and the precedence over
  cross-model is therefore conditional.** A candidate's term is `24 × deficit`,
  so two providers weighted 3:1 with nothing served yet are 12 apart — less
  than the +20 a cross-family provider gets. A **wide** share gap outranks
  cross-model and a narrow one does not, with the crossover at a deficit spread
  of `CROSS_MODEL_BONUS / WEIGHT_SHARE_SCORE` (≈0.83, roughly a 10:1 declared
  split from a cold start). This is intended rather than tolerated: a marginal
  declared difference should be a marginal signal, and a router that made
  1.01:1 as decisive as 100:1 would be reading a preference as an ultimatum.
  Both terms are soft; neither is an instruction the other must obey.
* **Share can outrank up to two waiters of queue depth.** Among providers that
  are *all* busy, steering by declared share is exactly the job weights exist
  for; bounding it at two waiters keeps it from sending work to a queue that
  is meaningfully longer.

An empty window gives every provider `actual = 0`, so `deficit = target` and
the highest-weighted provider goes first — absent a cross-model preference
large enough to tip it, per the point above. That is the right cold start: with
no history, begin with the share the operator asked for.

## 4. Telemetry

Two new `route_reason` values, causal in the same sense as `auto:free+cross` —
recorded only when re-scoring *without* the share term picks someone else:

* `auto:free+share`
* `auto:wait+share`

When both the share term and the cross-model bonus would independently change
the winner, the label is `+share`: the operator's instruction outranks the
heuristic, and one label per decision keeps the vocabulary readable.

`skodun providers` prints the effective weights in its routing header, so "are
my weights on" is answerable without reading the config layers by hand. Its
`--since-days` also defaults to `weights_window_days` whenever weights are
configured, so the counts it shows are the ones the router actually scored
against rather than a seven-day window nobody asked for; an explicit flag
still wins.

## 5. What this does NOT do, and why

* **No automatic weights.** See §1: skodun cannot measure the thing weights
  express. A router that inferred them would be inventing a number and acting
  on it.
* **No spend-derived weights for `openai-api`.** It is the one metered
  provider and `api_spend_events` does exist for it, but its daily cap is
  already enforced per attempt (`chain._api_spend_blocked`) and a provider over
  budget is excluded from routing entirely. A second, softer spend signal on
  top of a hard one would only make the hard one harder to reason about.
* **Not a scheduler.** Unchanged from Phase A: routing chooses which queue to
  join. Admission, the dual hold, and the head entry's own `fallbacks` are
  untouched, and `gate.py` / `trust.py` are not read or written.
* **The score-once collision window stays open.** Two runs starting in the
  same instant read the same snapshot — including the same served counts — and
  can pick the same provider. Weights do not narrow it, and closing it needs
  mid-wait re-binding, which re-opens the entry-specific budget, model and
  chain the pipeline has already resolved. It remains Phase C, documented in
  `routing.py` and `examples/fragments/concurrency.md`.

## 6. Questions this amendment closes

Three were recorded against Phase A (#83) rather than triaged, and #77 was
where they were to be settled.

1. **A seventh `route_reason` for the pool-fallback case (`auto:pool-default`)?**
   No. `ROUTE_DEFAULT_FINDER` names the RULE — "nothing was routable, so the
   config's default head runs" — and which entry that resolved to is on the
   same artifact as `routed_reviewer`. A second value for the same rule under
   a different pool shape would split one fact across two labels.
2. **Does routing belong inside the fail-closed perimeter?** No, and it is now
   recorded in `AGENTS.md`. The fail-closed invariant is about COVERAGE and
   TRUST: a review that cannot be shown trustworthy must never certify a push.
   Routing cannot touch that — the model reviews the real diff, the trust axes
   come from that run, and the gate reads the same record either way. It
   degrades loudly to pre-S5 head selection on a store error, matching
   `chain._cached_unavailable`'s precedent for the very same read. Failing a
   review because the load optimiser hiccuped would spend a model call to
   report a store hiccup.
3. **The score-once collision window.** §5 above: Phase C, not Phase B.

## 7. Acceptance

* `weights = {}` is Phase A: same head, same `route_reason`, on every input.
* A declared share moves the head only when free slots are equal.
* `auto:*+share` appears only when the share term is what decided it.
* Config refuses zero, negative, non-finite, non-numeric and unknown-provider
  weights, each naming `[routing] weights`. Non-finite matters more than it
  looks: TOML has `inf` as a literal and `inf > 0` is true, so an accepted
  `inf` makes `target` an `inf / inf` NaN, the scorer's `round()` raises, and
  `auto_route`'s guard swallows it on every routed run — auto-routing silently
  off, from a config that loaded cleanly.
* A store that cannot answer the served counts disables the term for that run
  and says so, rather than scoring everyone as if the window were empty.
