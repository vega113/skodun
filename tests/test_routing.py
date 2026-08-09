"""Epic S5: provider auto-route — the pure scorer and its real store views.

Two halves, on purpose. The scoring tests drive `score_candidate` /
`pick_finder` with hand-built `ProviderLoad` views: that is the whole policy,
and it must be readable without a database. The load-view tests drive
`provider_loads` / `auto_route` against a REAL `Store` with real capacity
admissions, because "how busy is this provider" is a claim about the shipped
capacity tables and a fake would only prove the fake.
"""

from __future__ import annotations

import ast
import time

import pytest

import skodun
from skodun import capacity, routing
from skodun.config import Config, Defaults, Reviewer, Routing
from skodun.routing import (
    CLIENT_FAMILY_ENV,
    CROSS_MODEL_BONUS,
    FREE_SLOT_SCORE,
    QUEUE_DEPTH_PENALTY,
    ROUTE_FREE,
    ROUTE_FREE_CROSS,
    ROUTE_FREE_SHARE,
    ROUTE_WAIT,
    ROUTE_WAIT_CROSS,
    ROUTE_WAIT_SHARE,
    WEIGHT_SHARE_SCORE,
    ProviderLoad,
    ShareTarget,
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
    share_targets,
)
from skodun.store import Store, _TS_FORMAT


def _entry(name: str, provider: str, *, role: str = "finder",
           enabled: bool = True) -> Reviewer:
    return Reviewer(name=name, provider=provider, model="m", role=role,
                    enabled=enabled)


def _cfg(*reviewers: Reviewer, mode: str = "auto",
         pool: tuple[str, ...] = (), cross_model: bool = True,
         weights: tuple[tuple[str, float], ...] = (),
         weights_window_days: int = 7) -> Config:
    return Config(defaults=Defaults(), reviewers=tuple(reviewers),
                  routing=Routing(mode=mode, pool=pool,
                                  cross_model=cross_model, weights=weights,
                                  weights_window_days=weights_window_days))


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


def test_google_quota_pools_route_independently(store):
    gemini = Reviewer(name="gemini", provider="google",
                      model="gemini-3.6-flash", role="finder")
    claude = Reviewer(name="claude", provider="google",
                      model="claude-sonnet-4-6", role="finder")
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("google", "claude quota", "quota", until,
                                   quota_pool="google:claude-gpt")
    route = auto_route(_cfg(gemini, claude), store)
    assert route.reviewer.name == "gemini"


def test_google_quota_pools_share_one_provider_weight(store):
    gemini = Reviewer(name="gemini", provider="google",
                      model="gemini-3.6-flash", role="finder")
    claude = Reviewer(name="claude", provider="google",
                      model="claude-sonnet-4-6", role="finder")
    xai = _entry("xai", "xai")
    _seed_served(store, {"agy": 3, "grok": 1})
    cfg = _cfg(gemini, claude, xai,
               weights=(("google", 9), ("xai", 1)))

    shares = routing._shares_for(
        cfg, resolve_pool(cfg), provider_loads(store, resolve_pool(cfg)), store)

    assert set(shares) == {"google", "xai"}
    assert shares["google"].target == pytest.approx(0.9)
    assert shares["google"].actual == pytest.approx(0.75)


def test_auto_route_includes_confined_junie_when_feasible(monkeypatch, store,
                                                          tmp_path):
    monkeypatch.setattr("skodun.adapters.junie.sys.platform", "darwin")
    monkeypatch.setattr(
        "skodun.adapters.junie_sanitized.resolve_sandbox_exec",
        lambda: "/usr/bin/sandbox-exec")
    binary = tmp_path / "junie"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SKODUN_JUNIE_BIN", str(binary))
    junie = _entry("finder-junie-luna", "junie")
    fallback = _entry("finder-fallback", "xai")
    junie = Reviewer(**{**junie.__dict__, "fallbacks": (fallback.name,)})
    cfg = _cfg(junie, fallback)
    route = auto_route(cfg, store)
    assert route.reviewer.name == "finder-junie-luna"


def test_prompt_size_excludes_only_the_argv_bound_candidate(store):
    agy = _entry("finder-agy", "google")
    openai = _entry("finder-openai", "openai")
    route = auto_route(_cfg(agy, openai), store, prompt_size=200_000)
    assert route.reviewer.name == "finder-openai"


def test_cross_model_breaks_a_tie_between_two_equally_free_providers():
    pool = [_entry("same", "xai"), _entry("other", "openai")]
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=1)}
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("other", ROUTE_FREE_CROSS)
    # ...and the same picture with the preference off falls back to the order
    # the operator wrote, which here is the same-family entry.
    plain = pick_finder(pool, loads, client_family="xai", cross_model=False)
    assert (plain.reviewer.name, plain.reason) == ("same", ROUTE_FREE)


def test_a_cross_family_pick_that_would_have_won_anyway_is_not_labelled_cross():
    """`+cross` answers "is `cross_model` earning its keep?", so it has to be a
    counterfactual and not an observation that the winner is cross-family."""
    pool = [_entry("same", "xai"), _entry("other", "openai")]
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=3)}   # wins on slots alone
    route = pick_finder(pool, loads, client_family="xai")
    assert (route.reviewer.name, route.reason) == ("other", ROUTE_FREE)


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


def test_ties_break_by_the_order_the_operator_wrote():
    """Not alphabetically: two entries on ONE provider always score the same,
    and they can carry different models and fallbacks. A rename must not be
    what decides which model reviews."""
    loads = {"xai": ProviderLoad(free_slots=1),
             "openai": ProviderLoad(free_slots=1)}
    assert pick_finder([_entry("zzz", "xai"), _entry("aaa", "openai")],
                       loads).reviewer.name == "zzz"
    assert pick_finder([_entry("aaa", "openai"), _entry("zzz", "xai")],
                       loads).reviewer.name == "aaa"


def test_two_entries_on_one_provider_tie_to_the_first_listed():
    """The same load view by construction, so only the tie-break can choose."""
    loads = {"xai": ProviderLoad(free_slots=1)}
    pool = [_entry("finder-grok-high", "xai"), _entry("finder-grok-fast", "xai")]
    assert pick_finder(pool, loads).reviewer.name == "finder-grok-high"


def test_while_nothing_is_busy_auto_picks_what_mode_off_would():
    """The property that makes `auto` safe to switch on: it deviates only once
    load actually differs."""
    pool = [_entry("finder", "xai"), _entry("finder-codex", "openai")]
    idle = {"xai": ProviderLoad(free_slots=1),
            "openai": ProviderLoad(free_slots=1)}
    assert pick_finder(pool, idle).reviewer.name == "finder"
    busy = {"xai": ProviderLoad(free_slots=0, queue_depth=1),
            "openai": ProviderLoad(free_slots=1)}
    assert pick_finder(pool, busy).reviewer.name == "finder-codex"


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


def _seed_served(store: Store, per_adapter: dict[str, int]) -> None:
    """`n` reviews inside the window, credited to each ADAPTER name.

    Written through `save_review` so the rows are the ones `routing_counts`
    really reads -- source, adapter and timestamp all as the pipeline persists
    them. Keyed by adapter (`grok`, `codex`) rather than provider (`xai`,
    `openai`) because that is what a review record carries, and the gap between
    the two is exactly what `provider_served` has to bridge.
    """
    at = time.strftime(_TS_FORMAT, time.gmtime(time.time() - 3600))
    for adapter, n in per_adapter.items():
        for i in range(n):
            store.save_review({
                "id": f"{adapter}-{i}", "reviewed_at": at, "source": "skodun",
                "branch": "feat", "head": "a" * 40, "base_ref": "main",
                "base_sha": "b" * 40, "diff_hash": f"{adapter}{i}",
                "mode": "now", "model": "m", "adapter": adapter,
                "status": "clean", "parse_ok": True, "degraded": False,
                "diff_truncated": False, "trustworthy": True,
                "stop_reason": None, "findings": [], "findings_total": 0,
                "summary": "",
            })


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


def test_a_swallowed_routing_failure_is_reported_not_hidden(store, capsys):
    """Degrading to pre-S5 head selection looks exactly like a working install.

    A guard that fell back in silence would hide a real defect -- a broken store
    query, a bug in the blackout predicate -- behind reviews that keep
    succeeding, so every guard says what it swallowed on the progress stream the
    chain already reports quota outages on.
    """
    def boom(_store, _entry):
        raise RuntimeError("store is on fire")

    provider_loads(store, [_entry("a", "xai")], blackout_fn=boom)
    err = capsys.readouterr().err
    assert "routing: could not read load for provider xai" in err
    assert "store is on fire" in err


def test_a_failed_auto_route_says_it_fell_back(store, capsys, monkeypatch):
    """The other guard, and the one that decides the whole run's head."""
    monkeypatch.setattr("skodun.routing.provider_loads",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("scoring exploded")))
    cfg = _cfg(_entry("finder", "xai"))
    assert auto_route(cfg, store) is None
    err = capsys.readouterr().err
    assert "routing: auto-route failed" in err
    assert "scoring exploded" in err


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
    # With the preference off, the tie falls to the order the operator wrote
    # rather than to the other family.
    assert route.reviewer.name == "finder-a-xai"
    assert route.reason == ROUTE_FREE


# --- head resolution (pipeline seam) ----------------------------------------
# `pipeline.resolve_review_head` is the ONE place a head is chosen, so that the
# CLI and any number of stdio MCP processes apply the same rule. These drive it
# directly: they are about the DECISION, and a real model call would only make
# the same assertions slower.


def _head(cfg, store, **kw):
    from skodun.pipeline import resolve_review_head

    return resolve_review_head(cfg, store, **kw)


def test_a_pin_wins_over_a_free_provider_and_is_not_scored(store):
    """A caller asking a busy provider for a second opinion gets that provider."""
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    _hold(store, "xai", "h1")           # grok is busy, codex is idle
    head, meta = _head(cfg, store, requested="finder-grok")
    assert head.name == "finder-grok"
    assert meta == {"requested_reviewer": "finder-grok",
                    "routed_reviewer": "finder-grok",
                    "route_reason": "pinned", "client_family": None}


def test_mode_off_is_pre_s5_selection(store):
    """The first enabled finder, whatever the load says. The shipped default."""
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"),
               mode="off")
    _hold(store, "xai", "h1")
    head, meta = _head(cfg, store)
    assert head.name == "finder-grok"
    assert meta["route_reason"] == "config-finder"
    assert meta["requested_reviewer"] is None
    assert meta["routed_reviewer"] == "finder-grok"


def test_mode_auto_routes_off_the_busy_provider_and_records_why(store):
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    _hold(store, "xai", "h1")
    head, meta = _head(cfg, store)
    assert head.name == "finder-codex"
    assert meta == {"requested_reviewer": None,
                    "routed_reviewer": "finder-codex",
                    "route_reason": ROUTE_FREE, "client_family": None}


def test_head_resolution_forwards_prompt_size_to_auto_router(store, monkeypatch):
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    seen = {}

    def fake_auto_route(_cfg, _store, **kwargs):
        seen.update(kwargs)
        return routing.Route(cfg.reviewers[0], ROUTE_FREE)

    monkeypatch.setattr(routing, "auto_route", fake_auto_route)
    _head(cfg, store, prompt_size=12345)

    assert seen["prompt_size"] == 12345


def test_recovery_exclusion_does_not_fall_back_to_a_terminal_provider(store):
    from skodun.pipeline import PreflightRefused

    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    head, _meta = _head(cfg, store, avoid_providers={"xai"})
    assert head.provider == "openai"

    with pytest.raises(PreflightRefused, match="no alternative reviewer provider"):
        _head(cfg, store, avoid_providers={"xai", "openai"})


def test_mode_auto_records_the_declared_client_family_either_way(store):
    """The field describes the CALLER, so it is recorded whether or not it tipped."""
    cfg = _cfg(_entry("finder-grok", "xai"), _entry("finder-codex", "openai"))
    _, meta = _head(cfg, store, client_family="xai")
    assert meta["client_family"] == "xai"
    assert meta["route_reason"] == ROUTE_FREE_CROSS
    assert meta["routed_reviewer"] == "finder-codex"


def test_mode_auto_falls_back_to_the_config_finder_when_nothing_is_routable(store):
    """A blacked-out pool is still a runnable review — the chain fails fast in
    its own words, and a router that refused here would replace a diagnosis
    with a shrug."""
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("xai", "rate limited", "quota", until)
    cfg = _cfg(_entry("finder-grok", "xai"))
    head, meta = _head(cfg, store)
    assert head.name == "finder-grok"
    assert meta["route_reason"] == "auto:default-finder"


def test_a_config_with_no_finder_is_refused_in_both_modes(store):
    from skodun.pipeline import PreflightRefused

    for mode in ("off", "auto"):
        cfg = _cfg(_entry("r", "xai", role="refuter"), mode=mode)
        with pytest.raises(PreflightRefused,
                           match="no enabled reviewer with role 'finder'"):
            _head(cfg, store)


def test_a_pin_still_runs_when_the_config_names_no_finder_at_all(store):
    """The request supplies the one thing the role lookup exists to produce."""
    cfg = _cfg(_entry("second-opinion", "xai", role="refuter"), mode="auto")
    head, meta = _head(cfg, store, requested="second-opinion")
    assert head.name == "second-opinion"
    assert meta["route_reason"] == "pinned"


def test_an_explicit_pool_is_honoured_even_when_nothing_in_it_is_routable(store):
    """Leaving a finder OUT of the pool is how an operator excludes it from
    automatic selection. A "nothing routable" fallback that then picked it
    would be the one thing the pool exists to prevent."""
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("openai", "rate limited", "quota", until)
    cfg = _cfg(_entry("finder-pin-only", "xai"), _entry("finder-codex", "openai"),
               pool=("finder-codex",))
    head, meta = _head(cfg, store)
    assert head.name == "finder-codex"          # the pooled entry, blackout and all
    assert meta["route_reason"] == "auto:default-finder"


def test_without_an_explicit_pool_the_fallback_is_the_config_finder(store):
    """The implicit pool already contains it, so this is `_default_head`."""
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("xai", "rate limited", "quota", until)
    cfg = _cfg(_entry("finder", "xai"))
    head, meta = _head(cfg, store)
    assert (head.name, meta["route_reason"]) == ("finder",
                                                 "auto:default-finder")


def test_a_client_family_matching_no_configured_provider_cannot_misroute(store,
                                                                         capsys):
    """A typo adds the same bonus to every candidate, so the ORDER is untouched
    and the counterfactual correctly declines to call the pick `+cross`. What
    it does is nothing, silently, while the operator believes cross-model
    review is on -- so it says so."""
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"))
    typo = auto_route(cfg, store, client_family="opneai")
    plain = auto_route(cfg, store, client_family=None)
    assert (typo.reviewer.name, typo.reason) == (plain.reviewer.name,
                                                 plain.reason)
    err = capsys.readouterr().err
    assert "client_family 'opneai' matches no configured finder family" in err
    assert "openai, xai" in err


def test_a_family_that_does_match_is_not_warned_about(store, capsys):
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"))
    auto_route(cfg, store, client_family="xai")
    assert "matches no configured" not in capsys.readouterr().err


def test_no_inert_warning_when_the_operator_turned_cross_model_off(store,
                                                                   capsys):
    """Nothing is inert that was never switched on."""
    cfg = _cfg(_entry("finder-a", "xai"), cross_model=False)
    auto_route(cfg, store, client_family="opneai")
    assert "matches no configured" not in capsys.readouterr().err


# --- Phase B: declared share (#77) ------------------------------------------
# `[routing] weights` is the operator saying how reviews should be SPLIT across
# providers, measured against how many each actually served. Declared rather
# than derived: what weights express -- how much of a subscription a review
# consumes -- is not observable for a flat-rate CLI at any window length, so a
# router that inferred it would be acting on a number it made up. See
# `docs/superpowers/specs/2026-08-04-phase-b-weighted-routing.md` §1.


def test_no_weights_is_phase_a_exactly():
    """The default, and the property that makes the term safe to ship: an
    install that never sets weights scores byte-for-byte as it did before."""
    e = _entry("f", "xai")
    load = ProviderLoad(free_slots=1)
    assert (score_candidate(e, load, share=None)
            == score_candidate(e, load) == FREE_SLOT_SCORE)
    assert share_targets({}, ["xai", "openai"], {"xai": 10}) == {}


def test_an_unweighted_provider_counts_as_one():
    """So raising ONE provider does not mean listing every other."""
    got = share_targets({"xai": 3}, ["xai", "openai"], {})
    assert got["xai"].target == pytest.approx(0.75)
    assert got["openai"].target == pytest.approx(0.25)


def test_the_target_is_computed_over_this_run_s_candidates_only():
    """A weight for a provider that is not a candidate today must not shrink
    every real candidate's target -- the shares would stop summing to one and
    everybody would read as permanently owed work."""
    got = share_targets({"xai": 1, "google": 99}, ["xai", "openai"], {})
    assert got["xai"].target + got["openai"].target == pytest.approx(1.0)
    assert "google" not in got


def test_an_empty_window_leaves_everyone_at_zero_served():
    """The cold start: with no history, begin with the share that was asked
    for, so the highest-weighted provider goes first."""
    got = share_targets({"xai": 3, "openai": 1}, ["xai", "openai"], {})
    assert got["xai"].actual == got["openai"].actual == 0.0
    assert got["xai"].deficit > got["openai"].deficit


def test_a_provider_over_its_share_is_scored_below_one_under_it():
    """xai declared three quarters and served all ten of ten: it is over, and
    openai -- declared a quarter, served none -- is owed."""
    got = share_targets({"xai": 3}, ["xai", "openai"], {"xai": 10, "openai": 0})
    assert got["xai"].deficit < 0 < got["openai"].deficit
    over = score_candidate(_entry("a", "xai"), ProviderLoad(free_slots=1),
                           share=got["xai"])
    under = score_candidate(_entry("b", "openai"), ProviderLoad(free_slots=1),
                            share=got["openai"])
    assert under > over


def test_a_free_slot_still_beats_any_declared_share():
    """THE bound on this term, and the reason it is 24. A review that can start
    now finishes sooner than any prediction about a queue, so no weight may
    reorder providers that differ by a free slot -- the same guarantee
    `cross_model` has."""
    maximally_owed = ShareTarget(target=1.0, actual=0.0)
    maximally_over = ShareTarget(target=0.0, actual=1.0)
    starved_but_busy = score_candidate(
        _entry("a", "xai"), ProviderLoad(free_slots=0, queue_depth=0),
        share=maximally_owed)
    fed_and_free = score_candidate(
        _entry("b", "openai"), ProviderLoad(free_slots=1),
        share=maximally_over)
    assert fed_and_free > starved_but_busy
    # ...and the same at the tightest margin the scale allows: one free slot
    # against two, which is exactly `FREE_SLOT_SCORE` apart.
    one = score_candidate(_entry("a", "xai"), ProviderLoad(free_slots=1),
                          share=maximally_owed)
    two = score_candidate(_entry("b", "openai"), ProviderLoad(free_slots=2),
                          share=maximally_over)
    assert two > one
    assert 2 * WEIGHT_SHARE_SCORE < FREE_SLOT_SCORE


def test_a_wide_share_gap_outranks_cross_model_and_a_narrow_one_does_not():
    """`WEIGHT_SHARE_SCORE` is a COEFFICIENT, not a flat bonus, so comparing it
    with `CROSS_MODEL_BONUS` is not the whole story: a candidate's term is
    `24 * deficit`, and two providers 3:1 apart from a cold start are only 12
    apart -- less than the +20 a cross-family provider gets.

    That is intended. A marginal declared difference should be a marginal
    signal; a router that made 1.01:1 as decisive as 100:1 would be reading a
    preference as an ultimatum. What it means is that the honest precedence is
    "a wide share gap wins, a narrow one does not", with the crossover at a
    deficit spread of CROSS_MODEL_BONUS / WEIGHT_SHARE_SCORE.
    """
    def duel(target_a: float, target_b: float) -> str:
        """Two equally free providers, `b` cross-family, `a` the client's own."""
        a = score_candidate(
            _entry("a", "xai"), ProviderLoad(free_slots=1),
            share=ShareTarget(target=target_a, actual=0.0),
            client_family="xai")
        b = score_candidate(
            _entry("b", "openai"), ProviderLoad(free_slots=1),
            share=ShareTarget(target=target_b, actual=0.0),
            client_family="xai")
        return "a" if a > b else "b"

    # The crossover spread, stated as the arithmetic rather than as a magic
    # pair of numbers. Each side is taken with a margin rather than at the
    # boundary itself, because the term is `round()`ed to an integer and the
    # last fraction of a point is not a property worth pinning.
    crossover = CROSS_MODEL_BONUS / WEIGHT_SHARE_SCORE

    def spread(width: float) -> str:
        return duel(0.5 + width / 2, 0.5 - width / 2)

    # 3:1 from a cold start is a 0.5 spread, worth 12: it loses to the +20.
    assert duel(0.75, 0.25) == "b"
    assert spread(crossover - 0.3) == "b"
    assert spread(crossover + 0.15) == "a"
    assert duel(1.0, 0.0) == "a"


def test_the_share_label_is_causal_not_descriptive(store):
    """`auto:free+share` claims the weights are what SENT the review here. A
    provider that would have won on load alone records plain `auto:free`, or
    an operator reading the telemetry would believe their weights are earning
    their keep when they are not."""
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 3),))
    # Nothing served, nothing busy: xai is both first-listed AND the most owed,
    # so the share term changed nothing.
    route = auto_route(cfg, store)
    assert (route.reviewer.name, route.reason) == ("finder-a", ROUTE_FREE)


def test_a_declared_share_moves_the_head_and_says_so(store, monkeypatch):
    """The whole feature, end to end through the real store: xai has served
    everything in the window, so the review that would have gone to the
    first-listed entry goes to the one that is owed."""
    _seed_served(store, {"grok": 9, "codex": 1})
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 1), ("openai", 1)))

    route = auto_route(cfg, store)

    assert (route.reviewer.name, route.reason) == ("finder-b",
                                                   ROUTE_FREE_SHARE)


def test_the_same_store_without_weights_keeps_the_first_listed_head(store):
    """The control for the test above: the served counts are identical and it
    is the WEIGHTS that changed the answer, not the history."""
    _seed_served(store, {"grok": 9, "codex": 1})
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"))
    route = auto_route(cfg, store)
    assert (route.reviewer.name, route.reason) == ("finder-a", ROUTE_FREE)


def test_served_counts_are_read_by_provider_not_by_adapter_name(store):
    """Three of the five shipped adapters have a name that is not their
    provider id (`xai`/grok, `openai`/codex, `google`/agy). Reading `served`
    as if they matched scores every one of them as having served nothing --
    permanently owed the whole share, whatever it really did."""
    _seed_served(store, {"grok": 5})
    from skodun.routing import provider_served

    assert provider_served(store, ["xai", "openai"], window_days=7) == {
        "xai": 5, "openai": 0}


def test_a_store_that_cannot_answer_drops_the_term_rather_than_guessing(
        store, capsys):
    """A failed read knows NOTHING, which is not the same as an empty window:
    scoring it as empty would hand the whole share to the highest-weighted
    provider on the strength of a store error. Loud, like every other guard in
    this module."""
    class _Broken:
        def __getattr__(self, name):
            return getattr(store, name)

        def routing_counts(self, *a, **k):
            raise RuntimeError("no such column")

    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("openai", 99),))
    route = auto_route(cfg, _Broken())
    assert (route.reviewer.name, route.reason) == ("finder-a", ROUTE_FREE)
    assert "could not read served counts" in capsys.readouterr().err


def test_weights_steer_between_two_busy_providers(store):
    """Among providers that are ALL busy, steering by declared share is the
    entire job weights exist for."""
    _seed_served(store, {"grok": 10})
    _hold(store, "xai", "h1")
    _hold(store, "openai", "h2")
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 1), ("openai", 1)))

    route = auto_route(cfg, store)

    assert (route.reviewer.name, route.reason) == ("finder-b",
                                                   ROUTE_WAIT_SHARE)


def test_both_fractions_are_computed_over_the_same_candidate_set():
    """`target` and `actual` must share a denominator or their difference
    means nothing.

    A provider outside this run's pool -- another finder kept out of it, or a
    `role = "refuter"` entry nothing routes to -- may well have served reviews
    in the window. Counting those in the DENOMINATOR while the targets still
    sum to one would make every candidate read as owed work, which is not a
    decision this router can act on: it can only choose between the candidates
    it has, and a review some non-candidate served is not work they can
    rebalance.
    """
    got = share_targets({"xai": 1, "openai": 1}, ["xai", "openai"],
                        {"xai": 3, "openai": 1, "google": 1000})
    assert sum(t.target for t in got.values()) == pytest.approx(1.0)
    assert sum(t.actual for t in got.values()) == pytest.approx(1.0)
    assert got["xai"].actual == pytest.approx(0.75)
    assert "google" not in got


def test_a_blacked_out_provider_is_not_in_the_share_denominators(store):
    """`_argmax` skips an unavailable provider, so the share arithmetic must
    skip it too or the two disagree about what "the candidate set" is.

    Leaving one in dilutes every real candidate's target while its own served
    count inflates the denominator, so the deficits the scorer compares shrink.
    The winner often survives that; the MAGNITUDE does not, and magnitude is
    what decides whether the share term outranks the cross-model bonus.
    """
    until = time.strftime(_TS_FORMAT, time.gmtime(time.time() + 600))
    store.mark_provider_unavailable("google", "rate limited", "quota", until)
    _seed_served(store, {"grok": 10, "agy": 90})
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               _entry("finder-c", "google"),
               weights=(("xai", 1), ("openai", 1), ("google", 8)))

    shares = routing._shares_for(
        cfg, resolve_pool(cfg), provider_loads(store, resolve_pool(cfg)), store)

    assert set(shares) == {"xai", "openai"}
    # Both denominators are the two REMAINING candidates: equal weights, and
    # xai served everything they served between them.
    assert shares["xai"].target == shares["openai"].target == pytest.approx(0.5)
    assert shares["xai"].actual == pytest.approx(1.0)
    assert shares["openai"].actual == pytest.approx(0.0)
    # ...so the gap is the full 1.0 the two of them really differ by. Stated
    # against what counting the blacked-out provider WOULD have produced,
    # because the difference is the whole point and a bare 1.0 does not show
    # it: a tenth of the signal, which is under the crossover where the share
    # term stops outranking the cross-model bonus.
    with_blackout = share_targets(
        {"xai": 1, "openai": 1, "google": 8},
        ["xai", "openai", "google"],
        {"xai": 10, "openai": 0, "google": 90})
    assert (with_blackout["openai"].deficit
            - with_blackout["xai"].deficit) == pytest.approx(0.1)
    assert shares["openai"].deficit - shares["xai"].deficit == pytest.approx(1.0)


def test_the_served_mapping_uses_the_shipped_adapter_accessor(store):
    """Every registered provider, credited through `get_adapter` -- the same
    accessor `pipeline._adapter_for` and the `providers` listing use.

    Reading `.name` off the registry's CLASS works only while every adapter
    happens to declare it at class level. One that set it in `__init__` would
    raise inside `provider_served`, `auto_route` would catch it, and weights
    would go quietly inert while the config still said they were on -- the
    worst shape of failure for an optional feature.
    """
    from skodun.adapters import _REGISTRY, get_adapter
    from skodun.routing import provider_served

    providers = sorted(_REGISTRY)
    _seed_served(store, {get_adapter(p).name: i + 1
                         for i, p in enumerate(providers)})

    served = provider_served(store, providers, window_days=7)

    assert served == {p: i + 1 for i, p in enumerate(providers)}


def test_a_provider_with_no_adapter_is_skipped_rather_than_fatal(store):
    """`provider_loads` has already excluded it, so it is not a candidate and
    its share is nobody's -- but it must not take the whole term down with it,
    because `auto_route` would swallow that as "no weights today"."""
    from skodun.routing import provider_served

    _seed_served(store, {"grok": 3})
    assert provider_served(store, ["xai", "no-such-provider"],
                           window_days=7) == {"xai": 3, "no-such-provider": 0}


def test_a_share_that_would_have_decided_it_alone_is_still_credited(store):
    """The false negative that made the audit lie.

    Attributing each term by "does removing it change the winner" is the
    obvious rule and it is wrong when EITHER term alone would have been
    enough: remove the share and cross-model still produces the same head,
    remove cross-model and the share still does, so both questions answer "no"
    and the record says plain `auto:free` -- for a review that pure load would
    have sent somewhere else entirely. `skodun providers` then under-counts
    `auto:*+share` and makes configured weights look inert exactly when they
    are working.

    Here `finder-b` is BOTH the cross-family provider and the one owed the
    larger share, and each of those alone outranks first-listed `finder-a`.
    """
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 1), ("openai", 3)))

    route = auto_route(cfg, store, client_family="xai")

    assert route.reviewer.name == "finder-b"
    assert route.reason == ROUTE_FREE_SHARE


def test_cross_model_keeps_the_credit_when_it_is_the_only_explanation(store):
    """The other side of the same rule: weights configured, but flat, so the
    share term cannot be what moved this review."""
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 1), ("openai", 1)))

    route = auto_route(cfg, store, client_family="xai")

    assert route.reviewer.name == "finder-b"
    assert route.reason == ROUTE_FREE_CROSS


def test_pure_load_keeps_the_credit_when_no_soft_term_moved_anything(store):
    """And the base case: a head pure load would have picked anyway is neither
    `+share` nor `+cross`, however many soft terms are switched on."""
    _hold(store, "openai", "h1")
    cfg = _cfg(_entry("finder-a", "xai"), _entry("finder-b", "openai"),
               weights=(("xai", 1), ("openai", 9)))

    route = auto_route(cfg, store, client_family="xai")

    # openai is owed the share AND is cross-family, but it is busy and xai has
    # a free slot -- which no soft term may overturn.
    assert (route.reviewer.name, route.reason) == ("finder-a", ROUTE_FREE)


def _identifier_sites(tree: ast.AST, target: str) -> set[str]:
    """Every place `target` is mentioned, as `"<qualified scope>::<kind>"`.

    `def` is the definition itself; `ref` is any other mention -- a call, a
    bare name, an attribute access, either side of an import (`import x as
    target` binds the name just as `from x import target` does). Lumping them together is the
    point: the invariant below is about the identifier being MENTIONED, not
    about how, so an alias (`head_of = pipeline.resolve_review_head`) is a
    `ref` exactly like a direct call and cannot slip past a scan looking for
    one shape.

    An explicit descent, not `ast.walk` per function: `walk` also traverses
    nested defs, so a mention inside a closure would be attributed to the
    closure AND to every function around it -- a failure naming several
    functions when only one of them mentions anything.

    The scope is QUALIFIED (`Outer.method`, `outer.<locals>.inner`) rather than
    the innermost name alone. Two same-named functions in one file -- methods
    of different classes, most obviously -- would otherwise collapse to one
    label, so a second site could hide behind the first and the failure could
    not be located. Qualified rather than line-numbered because the expected
    set below must not churn every time something above it moves.
    """
    sites: set[str] = set()

    def descend(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and child.name == target:
                    sites.add(f"{where}::def")
                qualified = (child.name if where == "<module>"
                             else f"{where}.{child.name}")
                descend(child, qualified)   # the nested scope owns its body
                continue
            if ((isinstance(child, ast.Name) and child.id == target)
                    or (isinstance(child, ast.Attribute) and child.attr == target)
                    or (isinstance(child, ast.alias)
                        and target in (child.name, child.asname))):
                sites.add(f"{where}::ref")
            descend(child, where)

    descend(tree, "<module>")
    return sites


def test_run_review_is_the_only_production_caller_of_head_resolution():
    """The call-graph half of the Phase A scope note (issue #98).

    `resolve_review_head`'s docstring scopes auto-routing to the foreground
    loop, and the reviewer who raised it was right that a docstring is weak
    evidence for a claim about callers. The consequential half -- that the
    background pre-push worker does not route -- is pinned behaviourally in
    `tests/test_dispatch.py::test_the_background_worker_does_not_auto_route`,
    through the shipped entry point and against a config that WOULD route.

    This is the other half, and it catches something that one cannot: a NEW
    caller on some third surface, acquiring routing semantics nobody decided
    to give it. `run_prepush_review` picks its head with `_reviewer_for(cfg,
    "finder")` and `resolve_review_head` now duplicates that selection, so the
    two are a standing invitation to be collapsed -- and a failure here is the
    moment to have that conversation rather than to discover it from a
    background review that routed.

    The assertion is deliberately about the IDENTIFIER rather than about
    calls, and that is what makes it hard to walk past by accident. Counting
    call sites invites every alias to defeat it -- `from .pipeline import
    resolve_review_head as head_of`, or `head_of = pipeline.resolve_review_head`
    after an `import skodun.pipeline` -- and the way those arrive is somebody
    being tidy, not somebody evading a test. "The name is written in exactly
    two places" has no such surface: a reference of any shape, anywhere else,
    is a site.

    Read from the SOURCE rather than by monkeypatching, because the claim is
    about what is written: a caller on a branch this test never takes would be
    invisible to any dynamic check. The one residual gap is string reflection
    (`getattr(pipeline, "resolve_review_head")`), which no AST check can see
    and which nobody reaches for by accident.
    """
    from pathlib import Path

    src = Path(skodun.__file__).resolve().parent
    # BYTES, not decoded text: `ast.parse` honours a PEP 263 encoding
    # declaration itself, while `read_text(encoding="utf-8")` would raise on a
    # source file that declares another one.
    sites = {f"{path.relative_to(src)}::{site}"
             for path in sorted(src.rglob("*.py"))
             for site in _identifier_sites(
                 ast.parse(path.read_bytes(), filename=str(path)),
                 "resolve_review_head")}

    assert sites == {"pipeline.py::<module>::def",
                     "pipeline.py::_run_review::ref"}, (
        "`resolve_review_head` is written somewhere new. That is not "
        "automatically wrong -- it IS a decision about which surfaces "
        "auto-route, and Phase A scoped it to the foreground review loop. "
        "Widen this set deliberately, and say so in the function's scope note.")
