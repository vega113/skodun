"""Epic S5: provider auto-route — the pure scorer and its real store views.

Two halves, on purpose. The scoring tests drive `score_candidate` /
`pick_finder` with hand-built `ProviderLoad` views: that is the whole policy,
and it must be readable without a database. The load-view tests drive
`provider_loads` / `auto_route` against a REAL `Store` with real capacity
admissions, because "how busy is this provider" is a claim about the shipped
capacity tables and a fake would only prove the fake.
"""

from __future__ import annotations

import time

import pytest

from skodun import capacity
from skodun.config import Config, Defaults, Reviewer, Routing
from skodun.routing import (
    CLIENT_FAMILY_ENV,
    CROSS_MODEL_BONUS,
    FREE_SLOT_SCORE,
    QUEUE_DEPTH_PENALTY,
    ROUTE_FREE,
    ROUTE_FREE_CROSS,
    ROUTE_WAIT,
    ROUTE_WAIT_CROSS,
    ProviderLoad,
    auto_route,
    cross_bonus_applies,
    family_for_client_name,
    normalize_family,
    pick_finder,
    provider_family,
    provider_loads,
    resolve_client_family,
    resolve_pool,
    score_candidate,
)
from skodun.store import Store, _TS_FORMAT


def _entry(name: str, provider: str, *, role: str = "finder",
           enabled: bool = True) -> Reviewer:
    return Reviewer(name=name, provider=provider, model="m", role=role,
                    enabled=enabled)


def _cfg(*reviewers: Reviewer, mode: str = "auto",
         pool: tuple[str, ...] = (), cross_model: bool = True) -> Config:
    return Config(defaults=Defaults(), reviewers=tuple(reviewers),
                  routing=Routing(mode=mode, pool=pool,
                                  cross_model=cross_model))


@pytest.fixture(autouse=True)
def _no_client_family_env(monkeypatch):
    """No test here inherits the developer's own declared client family."""
    monkeypatch.delenv(CLIENT_FAMILY_ENV, raising=False)


# --- families ---------------------------------------------------------------


def test_the_two_openai_doors_share_one_family():
    """`openai-api` is metered HTTP to the same models the codex CLI serves.

    Routing between them is not a second opinion, so cross-model must not pay a
    bonus for the hop.
    """
    assert provider_family("openai") == provider_family("openai-api") == "openai"


@pytest.mark.parametrize("provider, family", [
    ("xai", "xai"), ("google", "google"), ("junie", "junie"),
])
def test_known_providers_map_to_their_own_family(provider, family):
    assert provider_family(provider) == family


def test_an_unmapped_provider_is_its_own_family():
    """A new adapter is never silently merged into an existing bucket."""
    assert provider_family("some-new-provider") == "some-new-provider"


@pytest.mark.parametrize("raw, expected", [
    ("XAI ", "xai"), ("openai", "openai"), ("", None), ("   ", None),
    (None, None), (42, None), (True, None),
])
def test_normalize_family_accepts_only_a_non_empty_string(raw, expected):
    assert normalize_family(raw) == expected


@pytest.mark.parametrize("name, family", [
    ("Grok CLI", "xai"), ("codex", "openai"), ("gemini-cli", "google"),
    ("JetBrains Junie", "junie"), ("something-nobody-mapped", None), (None, None),
])
def test_client_name_heuristics(name, family):
    assert family_for_client_name(name) == family


def test_client_family_prefers_the_explicit_argument(monkeypatch):
    monkeypatch.setenv(CLIENT_FAMILY_ENV, "google")
    assert resolve_client_family("xai", client_name="codex") == "xai"


def test_client_family_falls_back_to_env_then_to_the_client_name(monkeypatch):
    monkeypatch.setenv(CLIENT_FAMILY_ENV, "google")
    assert resolve_client_family(None, client_name="codex") == "google"
    monkeypatch.delenv(CLIENT_FAMILY_ENV)
    assert resolve_client_family(None, client_name="codex") == "openai"
    assert resolve_client_family(None, client_name="nobody") is None


def test_an_empty_explicit_family_falls_through_rather_than_pinning_unknown(
        monkeypatch):
    """`--client-family ""` is declining to answer, not declaring a family."""
    monkeypatch.setenv(CLIENT_FAMILY_ENV, "google")
    assert resolve_client_family("", client_name=None) == "google"


# --- the pool ---------------------------------------------------------------


def test_an_empty_pool_means_every_enabled_finder():
    cfg = _cfg(_entry("a", "xai"), _entry("r", "openai", role="refuter"),
               _entry("b", "google"), _entry("off", "junie", enabled=False))
    assert [r.name for r in resolve_pool(cfg)] == ["a", "b"]


def test_an_explicit_pool_is_taken_in_the_order_it_was_written():
    cfg = _cfg(_entry("a", "xai"), _entry("b", "openai"), _entry("c", "google"),
               pool=("c", "a"))
    assert [r.name for r in resolve_pool(cfg)] == ["c", "a"]


# --- scoring ----------------------------------------------------------------


def test_free_slots_score_and_a_busy_provider_scores_negative():
    e = _entry("a", "xai")
    assert score_candidate(e, ProviderLoad(free_slots=2)) == 2 * FREE_SLOT_SCORE
    # No free slot: this run would be the (depth + 1)-th waiter.
    assert score_candidate(e, ProviderLoad(free_slots=0, queue_depth=3)) == (
        -QUEUE_DEPTH_PENALTY * 4)


def test_the_cross_model_bonus_applies_only_to_a_different_family():
    e = _entry("a", "xai")
    free = ProviderLoad(free_slots=1)
    assert score_candidate(e, free, client_family="openai") == (
        FREE_SLOT_SCORE + CROSS_MODEL_BONUS)
    assert score_candidate(e, free, client_family="xai") == FREE_SLOT_SCORE
    # ...and not at all when the operator turned it off, or nobody declared one.
    assert score_candidate(e, free, client_family="openai",
                           cross_model=False) == FREE_SLOT_SCORE
    assert score_candidate(e, free, client_family=None) == FREE_SLOT_SCORE


def test_cross_bonus_predicate_agrees_with_the_score():
    e = _entry("a", "xai")
    assert cross_bonus_applies(e, "openai", True) is True
    assert cross_bonus_applies(e, "xai", True) is False
    assert cross_bonus_applies(e, "openai", False) is False
    assert cross_bonus_applies(e, None, True) is False


# --- picking ----------------------------------------------------------------


def test_a_free_provider_beats_a_busy_one_however_long_the_queue():
    pool = [_entry("busy", "xai"), _entry("free", "openai")]
    loads = {"xai": ProviderLoad(free_slots=0, queue_depth=0),
             "openai": ProviderLoad(free_slots=1)}
    route = pick_finder(pool, loads)
    assert (route.reviewer.name, route.reason) == ("free", ROUTE_FREE)


def test_more_free_slots_wins():
    pool = [_entry("one", "xai"), _entry("two", "openai")]
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=2)}
    assert pick_finder(pool, loads).reviewer.name == "two"


def test_when_everything_is_busy_the_shortest_queue_wins():
    pool = [_entry("deep", "xai"), _entry("shallow", "openai")]
    loads = {"xai": ProviderLoad(free_slots=0, queue_depth=4),
             "openai": ProviderLoad(free_slots=0, queue_depth=1)}
    route = pick_finder(pool, loads)
    assert (route.reviewer.name, route.reason) == ("shallow", ROUTE_WAIT)


def test_a_blacked_out_provider_is_excluded_even_when_it_is_the_idle_one():
    pool = [_entry("out", "xai"), _entry("busy", "openai")]
    loads = {"xai": ProviderLoad(free_slots=4, unavailable=True),
             "openai": ProviderLoad(free_slots=0, queue_depth=9)}
    assert pick_finder(pool, loads).reviewer.name == "busy"


def test_a_provider_with_no_load_view_at_all_is_excluded():
    """A missing key is a provider the caller could not read; do not route to it."""
    pool = [_entry("unknown", "nope"), _entry("known", "openai")]
    loads = {"openai": ProviderLoad(free_slots=1)}
    assert pick_finder(pool, loads).reviewer.name == "known"


def test_cross_model_breaks_a_tie_between_two_equally_free_providers():
    pool = [_entry("same", "xai"), _entry("other", "openai")]
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=1)}
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("other", ROUTE_FREE_CROSS)
    # ...and the same picture with the preference off falls back to name order.
    plain = pick_finder(pool, loads, client_family="xai", cross_model=False)
    assert (plain.reviewer.name, plain.reason) == ("other", ROUTE_FREE)


def test_cross_model_never_outranks_a_free_slot():
    """+20 must not send a review into a queue to avoid the client's own family."""
    pool = [_entry("same-free", "xai"), _entry("other-busy", "openai")]
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=0, queue_depth=0)}
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("same-free", ROUTE_FREE)


def test_cross_model_never_excludes_the_last_available_family():
    """A single-family install still gets a reviewer, bonus or no bonus."""
    pool = [_entry("only", "xai")]
    loads = {"xai": ProviderLoad(free_slots=1)}
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("only", ROUTE_FREE)


def test_a_busy_cross_family_pick_says_so_in_its_reason():
    pool = [_entry("same", "xai"), _entry("other", "openai")]
    loads = {"xai": ProviderLoad(free_slots=0, queue_depth=1),
             "openai": ProviderLoad(free_slots=0, queue_depth=1)}
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("other", ROUTE_WAIT_CROSS)


def test_ties_break_by_name_ascending_not_by_config_order():
    """Two peers scoring the same picture must reach the same answer."""
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=1)}
    forward = pick_finder([_entry("aaa", "xai"), _entry("zzz", "openai")], loads)
    reverse = pick_finder([_entry("zzz", "openai"), _entry("aaa", "xai")], loads)
    assert forward.reviewer.name == reverse.reviewer.name == "aaa"


def test_an_empty_pool_and_a_fully_excluded_one_both_hand_the_choice_back():
    assert pick_finder([], {}) is None
    assert pick_finder([_entry("out", "xai")],
                       {"xai": ProviderLoad(unavailable=True)}) is None


# --- store views ------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    st = Store.open(tmp_path / "routing.db")
    try:
        yield st
    finally:
        st.close()


def _hold(store: Store, provider: str, admission_id: str) -> None:
    """One admitted holder on `provider:<id>` — a real capacity admission."""
    store.capacity_enqueue(admission_id=admission_id,
                           resource_class=capacity.provider_resource_class(provider),
                           scope=provider)
    store.capacity_force_admit(admission_id)


def _queue(store: Store, provider: str, admission_id: str) -> None:
    """One waiter parked on `provider:<id>`, never admitted."""
    store.capacity_enqueue(admission_id=admission_id,
                           resource_class=capacity.provider_resource_class(provider),
                           scope=provider)


def test_provider_loads_counts_real_holders_and_real_waiters(store):
    _hold(store, "xai", "h1")
    _queue(store, "xai", "q1")
    _queue(store, "xai", "q2")
    loads = provider_loads(store, [_entry("a", "xai")], max_in_flight=2)
    assert loads["xai"] == ProviderLoad(free_slots=1, queue_depth=2,
                                        unavailable=False)


def test_free_slots_never_go_negative_when_holders_outnumber_capacity(store):
    _hold(store, "xai", "h1")
    _hold(store, "xai", "h2")
    loads = provider_loads(store, [_entry("a", "xai")], max_in_flight=1)
    assert loads["xai"].free_slots == 0


def test_two_entries_on_one_provider_share_one_load_view(store):
    """A provider must not look busier for being configured twice."""
    _hold(store, "xai", "h1")
    loads = provider_loads(store, [_entry("a", "xai"), _entry("b", "xai")],
                           max_in_flight=2)
    assert list(loads) == ["xai"]
    assert loads["xai"].free_slots == 1


def test_a_quota_blackout_makes_a_provider_unavailable(store):
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("xai", "rate limited", "quota", until)
    loads = provider_loads(store, [_entry("a", "xai"), _entry("b", "openai")],
                           max_in_flight=1)
    assert loads["xai"].unavailable is True
    assert loads["openai"].unavailable is False


def test_a_provider_with_no_adapter_is_unavailable(store):
    """The pipeline refuses it by name; the router must not pick it first."""
    loads = provider_loads(store, [_entry("a", "not-a-provider")])
    assert loads["not-a-provider"] == ProviderLoad(unavailable=True)


def test_a_store_that_cannot_answer_marks_the_provider_unavailable(store):
    """Unknown load routes AWAY, and the caller falls back to the config finder."""
    def boom(_store, _entry):
        raise RuntimeError("store is on fire")

    loads = provider_loads(store, [_entry("a", "xai")], blackout_fn=boom)
    assert loads["xai"].unavailable is True


# --- end to end against a real store ----------------------------------------


def test_auto_route_spreads_off_a_busy_provider(store):
    """The whole point: a busy provider loses the head to an idle peer."""
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    _hold(store, "xai", "h1")
    route = auto_route(cfg, store)
    assert (route.reviewer.name, route.reason) == ("finder-codex", ROUTE_FREE)


def test_auto_route_honours_an_explicit_pool(store):
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"),
               pool=("finder-grok",))
    _hold(store, "xai", "h1")
    route = auto_route(cfg, store)
    # The pool is the whole candidate set: an idle provider outside it is not a
    # candidate, so the busy one still heads the run.
    assert route.reviewer.name == "finder-grok"
    assert route.reason == ROUTE_WAIT


def test_auto_route_returns_none_when_the_config_has_no_finder(store):
    cfg = _cfg(_entry("r", "xai", role="refuter"))
    assert auto_route(cfg, store) is None


def test_auto_route_returns_none_when_every_candidate_is_blacked_out(store):
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    for provider in ("xai", "openai"):
        store.mark_provider_unavailable(provider, "rate limited", "quota", until)
    cfg = _cfg(_entry("a", "xai"), _entry("b", "openai"))
    assert auto_route(cfg, store) is None


def test_auto_route_passes_the_operators_cross_model_switch_through(store):
    cfg = _cfg(_entry("finder-a-xai", "xai"), _entry("finder-b-openai", "openai"),
               cross_model=False)
    route = auto_route(cfg, store, client_family="xai")
    # With the preference off, the tie falls to name order rather than to the
    # other family.
    assert route.reviewer.name == "finder-a-xai"
    assert route.reason == ROUTE_FREE
