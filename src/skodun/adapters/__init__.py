"""Provider adapters: build one model CLI's invocation, interpret its output.

An adapter owns everything provider-specific about a single review attempt —
the argv, the response envelope's shape, and which harness misbehaviours make
that response untrustworthy. Everything upstream of it works in terms of
`ParseResult`/`ClassifyResult` and never in terms of one CLI's flags.

The provider-neutral half of that bargain — the result shapes, the `Adapter`
protocol, and the `OutputContract`s that say which response shape a run is
asking for — lives in `base`. This module is the registry and the re-export
surface; `get_adapter` raises rather than falling back to a default, because
silently reviewing with the wrong model is worse than not reviewing at all.
"""

from __future__ import annotations

from .base import (
    PROMPT_TOO_LARGE_CATEGORY,
    REFUTER_CONTRACT,
    REFUTER_VERDICTS,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    Adapter,
    ClassifyResult,
    OutputContract,
    ParseResult,
    PromptTooLarge,
)
from .agy import _STATUS_OK as _AGY_NORMAL_STOP
from .agy import AgyAdapter
from .codex import _TURN_COMPLETED as _CODEX_NORMAL_STOP
from .codex import CodexAdapter
from .grok import _STOP_REASON_OK as _GROK_NORMAL_STOP
from .grok import GrokAdapter

__all__ = [
    "Adapter",
    "AgyAdapter",
    "ClassifyResult",
    "CodexAdapter",
    "GrokAdapter",
    "NORMAL_STOP_REASONS",
    "OutputContract",
    "PROMPT_TOO_LARGE_CATEGORY",
    "ParseResult",
    "PromptTooLarge",
    "REFUTER_CONTRACT",
    "REFUTER_VERDICTS",
    "REVIEW_CONTRACT",
    "UNAVAILABLE_RC",
    "get_adapter",
]

#: Every value a `ParseResult.stop_reason` can carry for a run that ENDED
#: NORMALLY, across all three adapters — grok's `EndTurn`, agy's `SUCCESS`,
#: codex's `turn.completed`.
#:
#: Assembled from each adapter's OWN constant rather than re-spelled, because a
#: second list of these words is a list that drifts — and it already had. The
#: batched aggregate measured "abnormal" as `!= "EndTurn"`, one adapter's
#: vocabulary applied to all of them, so agy's normal terminal status was
#: promoted to the top of a record as though it were a truncation signal (and
#: read, in the verdict banner, as a success). This is a REPORTING vocabulary
#: only: no trust axis is computed from it. An adapter still judges its OWN
#: run's health itself, on its own terms, in `parse`/`classify` — this is the
#: one place that has to compare terminal words ACROSS adapters, because a
#: single batched review can be answered by several of them.
NORMAL_STOP_REASONS: frozenset[str] = frozenset(
    {_GROK_NORMAL_STOP, _AGY_NORMAL_STOP, _CODEX_NORMAL_STOP})


# Keyed by PROVIDER, not by adapter name: config names a provider, and one
# provider may ship more than one CLI. Typed as `type[Adapter]`, not bare
# `type`: registering a class that does not satisfy the protocol is then a type
# error at the table, not a surprise at the first `parse()` of a real review.
_REGISTRY: dict[str, type[Adapter]] = {
    "xai": GrokAdapter,
    "openai": CodexAdapter,
    "google": AgyAdapter,
}


def get_adapter(provider: str) -> Adapter:
    try:
        return _REGISTRY[provider]()
    except KeyError:
        raise ValueError(
            f"no adapter for provider {provider!r} "
            f"(known: {sorted(_REGISTRY)})") from None
