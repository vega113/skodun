"""Provider auto-route (epic S5): which finder entry heads an un-pinned review.

Design: `docs/superpowers/specs/2026-08-03-provider-auto-route-design.md`.

The problem this solves is UTILISATION, not scheduling. S4 gave every provider
its own `provider:<id>` FIFO with its own `SKODUN_PROVIDER_MAX_IN_FLIGHT`, but
head selection stayed sticky: every un-pinned review started at the config's
first enabled `finder`, so independent reviews piled into one provider's queue
while the others sat idle. The router only decides **which queue to join**;
admission, the dual hold, the head entry's own `fallbacks` and the refuse-if-busy
MCP policy are all untouched.

Three properties this module is built around, and each one is load-bearing:

* **Pure scoring.** `score_candidate` / `pick_finder` take VIEWS and return a
  choice. All store reads live in `provider_loads`, so the policy can be tested
  without a database and the same policy runs from the CLI and from any number
  of stdio MCP processes -- none of which can see each other except through the
  store.
* **Scored once.** The choice is made at head resolution and never revisited
  while waiting for a slot. Re-scoring every poll would make two peers chase
  each other between queues and would invalidate the entry-specific budget,
  model and chain the pipeline has already resolved.
* **Soft cross-model.** A finder whose provider family differs from the calling
  client's gets a BONUS, never an exclusion. Preferring a second opinion from
  another model family must not be able to leave a single-family install with
  no reviewer at all.

A pin (`--reviewer` / the MCP `reviewer` argument) never reaches this module:
it is an absolute request, and answering it with a different provider would
hand the caller back the very model they were routing around.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .config import Config, Reviewer

#: The caller's own model family, when it cares to say. Lowest-priority source
#: after an explicit CLI flag / MCP argument (see `resolve_client_family`).
CLIENT_FAMILY_ENV = "SKODUN_CLIENT_FAMILY"

#: provider id -> family, for the cross-model preference ONLY. A family is a
#: "would this be a second opinion?" bucket, which is why `openai` (the codex
#: subscription CLI) and `openai-api` (metered HTTP) share one: they are two
#: doors to the same models, so routing from one to the other is not a second
#: opinion even though they are different providers with different quotas.
#: A provider absent from this table is its own family -- a new adapter is
#: never silently merged into an existing bucket.
_PROVIDER_FAMILIES: dict[str, str] = {
    "xai": "xai",
    "openai": "openai",
    "openai-api": "openai",
    "google": "google",
    "junie": "junie",
}

#: substring -> family, for the MCP `initialize` `clientInfo.name` heuristic.
#: Ordered, first match wins, matched against the lower-cased client name. A
#: HEURISTIC on purpose and documented as one: the value is only ever worth a
#: +20 tie-break, and a client this table has never heard of degrades to "no
#: declared family", which is the availability-only scoring everyone gets by
#: default. Clients that want certainty pass `client_family` explicitly.
_CLIENT_NAME_FAMILIES: tuple[tuple[str, str], ...] = (
    ("grok", "xai"),
    ("codex", "openai"),
    ("openai", "openai"),
    ("gemini", "google"),
    ("junie", "junie"),
    ("jetbrains", "junie"),
)

# --- route reasons ----------------------------------------------------------
# Recorded on the artifact so a run can be explained after the fact ("why did
# this review go to grok?"). Stable strings, because they are read by humans
# reading old records and by the docs that name them.

#: The caller named the head. No scoring ran.
ROUTE_PINNED = "pinned"
#: `mode = "off"`: the config's first enabled finder, i.e. pre-S5 selection.
ROUTE_CONFIG_FINDER = "config-finder"
#: `mode = "auto"` but nothing was routable (empty pool, every candidate
#: blacked out, or the store could not be read) -- the config's first enabled
#: finder heads the run and its own `fallbacks` still apply. Deliberately NOT a
#: refusal: the chain-level `_finder_chain_unavailable` short-circuit already
#: fails fast when every entry really is unavailable, and it says so in words
#: an operator can act on.
ROUTE_DEFAULT_FINDER = "auto:default-finder"
#: Routed to a provider with a free slot.
ROUTE_FREE = "auto:free"
#: Routed to a provider with a free slot, and the cross-model bonus decided it.
ROUTE_FREE_CROSS = "auto:free+cross"
#: Every candidate is busy; routed to the shortest queue.
ROUTE_WAIT = "auto:wait"
#: Every candidate is busy; the cross-model bonus applied to the pick.
ROUTE_WAIT_CROSS = "auto:wait+cross"

# --- scoring weights --------------------------------------------------------
# The MVP numbers from the design, named rather than inlined so the tests that
# pin the ordering read as statements about the POLICY and not about arithmetic.
# Their relative sizes are the whole policy:
#   * a free slot beats any amount of queue (100 >> 10 x depth), because a
#     review that can start now finishes sooner than any prediction about a
#     queue that may or may not drain;
#   * cross-model (20) can reorder two providers with the SAME free-slot count
#     but can never outrank one free slot against none -- the second opinion is
#     worth waiting a tie for, not worth waiting a queue for.

#: Per free slot, when the provider has at least one.
FREE_SLOT_SCORE = 100
#: Per queued waiter (plus one for this run) when the provider has none free.
#: Negative, so "busy" always sorts below "free" whatever the depths are.
QUEUE_DEPTH_PENALTY = 10
#: Flat bonus for a provider family that differs from the client's.
CROSS_MODEL_BONUS = 20


def provider_family(provider_id: str) -> str:
    """The cross-model family of `provider_id` (itself, when unmapped)."""
    pid = str(provider_id or "").strip()
    return _PROVIDER_FAMILIES.get(pid, pid)


def normalize_family(value: object) -> str | None:
    """A declared family as this module compares them, or None for "unknown".

    Lower-cased and stripped, because the value arrives from a shell env var
    and from a JSON tool argument typed by an agent, and `"XAI "` and `"xai"`
    are the same claim. Anything that is not a non-empty string is None rather
    than an error: a family is a HINT worth +20, and refusing a review because
    a client sent `client_family: 42` would trade a whole review for a tie-break
    nobody needs.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def family_for_client_name(name: object) -> str | None:
    """The family an MCP `clientInfo.name` suggests, or None. See the table."""
    if not isinstance(name, str):
        return None
    lowered = name.strip().lower()
    if not lowered:
        return None
    for needle, family in _CLIENT_NAME_FAMILIES:
        if needle in lowered:
            return family
    return None


def resolve_client_family(explicit: object = None,
                          env: Mapping[str, str] | None = None,
                          client_name: object = None) -> str | None:
    """The family to score with: explicit, else env, else the client-name hint.

    Priority is the design's, and it is priority by SPECIFICITY: an argument on
    this call describes this call, an env var describes this machine, and the
    client name is a guess about a handshake. The first source that yields a
    family wins -- an explicit value that normalizes to None (e.g. `""`) falls
    through rather than pinning "unknown", because an empty string is a caller
    declining to answer, not a caller declaring an empty family.
    """
    env = os.environ if env is None else env
    for candidate in (explicit, env.get(CLIENT_FAMILY_ENV),
                      family_for_client_name(client_name)):
        family = normalize_family(candidate)
        if family is not None:
            return family
    return None


@dataclass(frozen=True)
class ProviderLoad:
    """What the STORE can see about one provider, right now.

    `free_slots` is `max_in_flight - holders`, floored at 0; `queue_depth` is
    the number of waiters already queued for that provider's FIFO. `unavailable`
    folds together every reason a candidate cannot be routed to at all -- quota
    blackout (`provider_state`), a metered provider out of daily budget, a
    provider with no adapter -- because the scorer treats them identically:
    excluded, not merely penalised.
    """

    free_slots: int = 0
    queue_depth: int = 0
    unavailable: bool = False


@dataclass(frozen=True)
class Route:
    """A routing decision: the entry to head the chain, and why."""

    reviewer: Reviewer
    reason: str


def resolve_pool(cfg: Config) -> tuple[Reviewer, ...]:
    """The `[[reviewers]]` entries the router may choose between.

    An explicit `[routing] pool` in the order the operator wrote it (its names
    are validated against the reviewer table at config load, so every one of
    them resolves); otherwise every enabled `role = "finder"` entry, which is
    what a multi-provider config already spells out and saves the operator from
    maintaining the same list twice.

    Disabled entries never appear, in either branch: `enabled = false` is the
    one switch that means "not this one", and a router that honoured it only
    when the pool was implicit would make that switch depend on an unrelated
    table.
    """
    if cfg.routing.pool:
        by_name = {r.name: r for r in cfg.reviewers}
        return tuple(by_name[name] for name in cfg.routing.pool
                     if name in by_name and by_name[name].enabled)
    return tuple(r for r in cfg.reviewers if r.enabled and r.role == "finder")


def cross_bonus_applies(entry: Reviewer, client_family: str | None,
                        cross_model: bool) -> bool:
    """Whether `entry` earns the cross-model bonus against `client_family`."""
    if not cross_model or client_family is None:
        return False
    return provider_family(entry.provider) != client_family


def score_candidate(entry: Reviewer, load: ProviderLoad, *,
                    client_family: str | None = None,
                    cross_model: bool = True) -> int:
    """Score one candidate. Higher is better. Pure -- no store, no clock.

    Free capacity dominates: `FREE_SLOT_SCORE` per free slot, so a provider with
    two idle slots outranks one with a single idle slot, and both outrank
    anything that has to wait. A provider with nothing free is scored NEGATIVELY
    by how many waiters it already has (plus this run, which is why the depth is
    incremented) so that "least bad wait" is still an ordering rather than a
    coin-flip.

    An unavailable provider is scored here anyway rather than special-cased:
    `pick_finder` excludes it before scoring, and a scorer that quietly returned
    a sentinel for it would be a second, hidden exclusion rule.
    """
    if load.free_slots > 0:
        score = FREE_SLOT_SCORE * load.free_slots
    else:
        score = -QUEUE_DEPTH_PENALTY * (load.queue_depth + 1)
    if cross_bonus_applies(entry, client_family, cross_model):
        score += CROSS_MODEL_BONUS
    return score


def pick_finder(pool: Sequence[Reviewer],
                loads: Mapping[str, ProviderLoad], *,
                client_family: str | None = None,
                cross_model: bool = True) -> Route | None:
    """The best candidate in `pool`, or None when nothing is routable.

    Pure. `loads` is keyed by provider id; a candidate whose provider is ABSENT
    from it is excluded exactly as an `unavailable` one is -- the caller builds
    `loads` for the providers it could actually reach, so a missing key is a
    provider this run must not join the queue of.

    Ties break by NAME ascending, not by config order, and that is deliberate:
    two peers scoring the same picture must reach the same answer, and config
    order is the one input two installs of the same reviewer table can disagree
    about after an edit. Deterministic ties are not fairness -- spreading load is
    the free-slot term's job, and once one peer is admitted the next one sees a
    different picture.

    None means "the caller decides", which for the pipeline is today's first
    enabled finder recorded as `auto:default-finder`. It is NOT a refusal: an
    empty pool and a fully blacked-out one both still deserve the ordinary
    fail-fast the finder chain already performs, in its own words.
    """
    best: tuple[int, str] | None = None
    chosen: Reviewer | None = None
    chosen_load: ProviderLoad | None = None
    for entry in pool:
        load = loads.get(entry.provider)
        if load is None or load.unavailable:
            continue
        score = score_candidate(entry, load, client_family=client_family,
                                cross_model=cross_model)
        # Ascending name breaks a score tie, so the comparison key negates the
        # score and compares names forward: a LOWER key wins.
        key = (-score, entry.name)
        if best is None or key < best:
            best, chosen, chosen_load = key, entry, load
    if chosen is None or chosen_load is None:
        return None
    cross = cross_bonus_applies(chosen, client_family, cross_model)
    if chosen_load.free_slots > 0:
        reason = ROUTE_FREE_CROSS if cross else ROUTE_FREE
    else:
        reason = ROUTE_WAIT_CROSS if cross else ROUTE_WAIT
    return Route(reviewer=chosen, reason=reason)


# --- store views ------------------------------------------------------------
# The ONLY I/O in this module. Kept here rather than in `store.py` because
# every one of these reads already exists as a shipped store API -- this is a
# projection of the capacity tables into the shape the scorer wants, not new
# state, and it needs no schema.


def _provider_blackout(store, entry: Reviewer) -> bool:
    """Whether `entry`'s provider cannot serve a review right now.

    Both halves of `chain`'s own "this attempt cannot produce inference" test,
    imported from `chain` rather than re-spelled so the router and the executor
    can never disagree about which providers are usable:

    * an active `provider_state` quota blackout -- the same TTL row
      `chain._effective_provider_capacity` reads to force effective capacity 0;
    * a metered provider (`openai-api`) that has spent its daily budget, which
      `chain` refuses per attempt.

    Routing to either is a head that is guaranteed to fall through to its own
    fallbacks, i.e. a slower path to the same answer, chosen on purpose.

    Imported lazily: `chain` reaches back into `pipeline` for its own helpers,
    and this module must stay importable by a test that only wants the scorer.
    """
    from . import chain

    if chain._cached_unavailable(store, entry.provider) is not None:
        return True
    return chain._api_spend_blocked(store, entry)


def provider_loads(store, pool: Sequence[Reviewer], *,
                   max_in_flight: int | None = None,
                   blackout_fn: Callable[[object, Reviewer], bool] | None = None,
                   ) -> dict[str, ProviderLoad]:
    """Store-visible load for every provider named in `pool`.

    One entry per DISTINCT provider: two reviewer entries on the same provider
    share one `provider:<id>` FIFO, so they share one picture of it, and
    counting it twice would make a provider look busier the more ways an
    operator configured it.

    Best-effort per provider, and that posture is the point: a store that cannot
    answer about one provider must not fail a review. Such a provider is marked
    `unavailable` -- routing AWAY from a provider whose load is unknown is the
    conservative direction, and if that leaves nothing routable the caller falls
    back to the config's finder, which is what a pre-S5 install would have done
    anyway.
    """
    from . import capacity
    from .adapters import _REGISTRY

    if max_in_flight is None:
        max_in_flight = capacity.provider_max_in_flight_from_env()
    blackout = _provider_blackout if blackout_fn is None else blackout_fn

    out: dict[str, ProviderLoad] = {}
    for entry in pool:
        provider = entry.provider
        if provider in out:
            continue
        if provider not in _REGISTRY:
            # A config error, and one the pipeline's own preflight refuses in
            # its own words. Excluded here so the router cannot PICK it and turn
            # a refusal that names the misconfigured entry into one that names
            # an entry the caller never asked for.
            out[provider] = ProviderLoad(unavailable=True)
            continue
        try:
            if blackout(store, entry):
                out[provider] = ProviderLoad(unavailable=True)
                continue
            resource_class = capacity.provider_resource_class(provider)
            holders = store.capacity_holder_count(resource_class, provider)
            views = store.capacity_active_views(resource_class, provider)
            queued = sum(1 for v in views
                         if v.status == capacity.STATUS_QUEUED)
            out[provider] = ProviderLoad(
                free_slots=max(0, int(max_in_flight) - int(holders)),
                queue_depth=queued,
            )
        except Exception:   # pragma: no cover - defensive; see the docstring
            out[provider] = ProviderLoad(unavailable=True)
    return out


def auto_route(cfg: Config, store, *,
               client_family: str | None = None) -> Route | None:
    """Score the configured pool against live load. None -> caller decides.

    The one call the pipeline makes. Guarded end to end for the reason
    `provider_loads` gives: auto-routing is an optimisation over the shipped
    head selection, and an optimisation that can fail a review is worse than no
    optimisation.
    """
    try:
        pool = resolve_pool(cfg)
        if not pool:
            return None
        loads = provider_loads(store, pool)
        return pick_finder(pool, loads, client_family=client_family,
                           cross_model=cfg.routing.cross_model)
    except Exception:       # pragma: no cover - defensive
        return None
