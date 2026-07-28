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
    REFUTER_CONTRACT,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    Adapter,
    ClassifyResult,
    OutputContract,
    ParseResult,
)
from .grok import GrokAdapter

__all__ = [
    "Adapter",
    "ClassifyResult",
    "GrokAdapter",
    "OutputContract",
    "ParseResult",
    "REFUTER_CONTRACT",
    "REVIEW_CONTRACT",
    "UNAVAILABLE_RC",
    "get_adapter",
]


# Keyed by PROVIDER, not by adapter name: config names a provider, and one
# provider may ship more than one CLI. Typed as `type[Adapter]`, not bare
# `type`: registering a class that does not satisfy the protocol is then a type
# error at the table, not a surprise at the first `parse()` of a real review.
_REGISTRY: dict[str, type[Adapter]] = {"xai": GrokAdapter}


def get_adapter(provider: str) -> Adapter:
    try:
        return _REGISTRY[provider]()
    except KeyError:
        raise ValueError(
            f"no adapter for provider {provider!r} "
            f"(known: {sorted(_REGISTRY)})") from None
