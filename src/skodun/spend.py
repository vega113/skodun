"""API spend tracking and limits for metered adapters (openai-api first).

CLI subscription providers do not use this module. HTTP adapters record each
call's token usage and an **estimated** USD cost, then refuse new calls when
a configurable **per-provider per-UTC-day** ceiling is reached.

The budget **resets every UTC midnight**. It is not a lifetime cap — users do
not need to raise the limit as spend accumulates over weeks.

Limits (first match wins):

* ``SKODUN_<PROVIDER>_SPEND_LIMIT_USD_PER_DAY`` (preferred), e.g.
  ``SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY``
* ``SKODUN_<PROVIDER>_SPEND_LIMIT_USD`` (alias, same meaning: **daily**)
* ``SKODUN_API_SPEND_LIMIT_USD_PER_DAY`` / ``SKODUN_API_SPEND_LIMIT_USD``
  (shared default for all API providers)
* built-in default **10.0** USD **per UTC day**

Costs are estimates from a rate table (USD per 1M tokens). Override rates via
``SKODUN_OPENAI_API_INPUT_USD_PER_1M`` / ``SKODUN_OPENAI_API_OUTPUT_USD_PER_1M``
(global fallback rates when a model is unknown).
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Mapping

from .store import _TS_FORMAT

if TYPE_CHECKING:
    from .store import Store

DEFAULT_SPEND_LIMIT_USD_PER_DAY = 10.0
# Preferred names say PER_DAY; unsuffixed aliases mean the same (daily, not lifetime).
API_SPEND_LIMIT_ENV_PER_DAY = "SKODUN_API_SPEND_LIMIT_USD_PER_DAY"
API_SPEND_LIMIT_ENV = "SKODUN_API_SPEND_LIMIT_USD"

# Approximate USD per 1_000_000 tokens (input, output). Conservative defaults
# for unknown models; override with env for dogfood accuracy.
_MODEL_RATES_USD_PER_1M: dict[str, tuple[float, float]] = {
    # Placeholders — operators should override via env when metering matters.
    "gpt-5.6-luna": (1.0, 4.0),
    "gpt-5.4": (2.0, 8.0),
    "gpt-5.4-mini": (0.5, 2.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o3": (10.0, 40.0),
    "o4-mini": (1.1, 4.4),
}
_DEFAULT_RATE_USD_PER_1M = (5.0, 15.0)


def _iso_now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


def _utc_day_prefix(now_iso: str | None = None) -> str:
    """``YYYY-MM-DD`` from a canonical store timestamp, or current UTC day."""
    if now_iso and len(now_iso) >= 10:
        return now_iso[:10]
    return time.strftime("%Y-%m-%d", time.gmtime())


def provider_limit_env_names(provider: str) -> tuple[str, str]:
    """``(…_PER_DAY, …_USD)`` env names for one provider (both mean daily)."""
    key = str(provider or "").strip().upper().replace("-", "_")
    base = f"SKODUN_{key}_SPEND_LIMIT_USD"
    return (f"{base}_PER_DAY", base)


def spend_limit_usd(provider: str,
                    env: Mapping[str, str] | None = None) -> float:
    """Per-provider **UTC-day** ceiling in USD (≥ 0). Junk → default 10/day."""
    env = os.environ if env is None else env
    names = (
        *provider_limit_env_names(provider),
        API_SPEND_LIMIT_ENV_PER_DAY,
        API_SPEND_LIMIT_ENV,
    )
    for name in names:
        raw = env.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = float(str(raw).strip())
        except ValueError:
            continue
        if value < 0 or value != value:  # NaN
            continue
        return value
    return DEFAULT_SPEND_LIMIT_USD_PER_DAY


def rates_for_model(model: str,
                    env: Mapping[str, str] | None = None) -> tuple[float, float]:
    """(input_usd_per_1m, output_usd_per_1m) for ``model``."""
    env = os.environ if env is None else env
    mid = str(model or "").strip()
    # Exact then prefix match (gpt-5.6-luna-2026-… → gpt-5.6-luna)
    if mid in _MODEL_RATES_USD_PER_1M:
        base = _MODEL_RATES_USD_PER_1M[mid]
    else:
        base = _DEFAULT_RATE_USD_PER_1M
        for key, rates in _MODEL_RATES_USD_PER_1M.items():
            if mid.startswith(key):
                base = rates
                break
    inp, out = base
    try:
        if env.get("SKODUN_OPENAI_API_INPUT_USD_PER_1M", "").strip():
            inp = float(env["SKODUN_OPENAI_API_INPUT_USD_PER_1M"].strip())
        if env.get("SKODUN_OPENAI_API_OUTPUT_USD_PER_1M", "").strip():
            out = float(env["SKODUN_OPENAI_API_OUTPUT_USD_PER_1M"].strip())
    except ValueError:
        pass
    return (max(0.0, inp), max(0.0, out))


def estimate_cost_usd(model: str, *, prompt_tokens: int,
                      completion_tokens: int,
                      env: Mapping[str, str] | None = None) -> float:
    inp_rate, out_rate = rates_for_model(model, env)
    pt = max(0, int(prompt_tokens))
    ct = max(0, int(completion_tokens))
    return (pt * inp_rate + ct * out_rate) / 1_000_000.0


def spent_today_usd(store: "Store", provider: str,
                    *, now_iso: str | None = None) -> float:
    day = _utc_day_prefix(now_iso)
    return store.api_spend_sum_usd(provider, day_prefix=day)


def remaining_budget_usd(store: "Store", provider: str,
                         *, now_iso: str | None = None,
                         env: Mapping[str, str] | None = None) -> float:
    limit = spend_limit_usd(provider, env)
    spent = spent_today_usd(store, provider, now_iso=now_iso)
    return max(0.0, limit - spent)


def would_exceed_limit(store: "Store", provider: str, *,
                       additional_usd: float = 0.0,
                       now_iso: str | None = None,
                       env: Mapping[str, str] | None = None) -> bool:
    """True if recording ``additional_usd`` would pass the daily ceiling."""
    limit = spend_limit_usd(provider, env)
    spent = spent_today_usd(store, provider, now_iso=now_iso)
    return (spent + max(0.0, float(additional_usd))) > limit + 1e-12


def record_usage(
        store: "Store", *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
        cost_usd: float | None = None,
        review_id: str | None = None,
        request_id: str | None = None,
        at: str | None = None,
        env: Mapping[str, str] | None = None) -> dict:
    """Persist one API spend row. Returns the stored row."""
    pt = max(0, int(prompt_tokens))
    ct = max(0, int(completion_tokens))
    tt = int(total_tokens) if total_tokens is not None else pt + ct
    if cost_usd is None:
        cost_usd = estimate_cost_usd(model, prompt_tokens=pt,
                                     completion_tokens=ct, env=env)
    return store.api_spend_append(
        at=at or _iso_now(),
        provider=provider,
        model=model,
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=max(0, tt),
        cost_usd=float(cost_usd),
        review_id=review_id,
        request_id=request_id,
    )
