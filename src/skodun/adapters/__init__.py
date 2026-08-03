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
from .junie import JunieAdapter
from .openai_api import OpenAIAPIAdapter

__all__ = [
    "Adapter",
    "AgyAdapter",
    "ClassifyResult",
    "CodexAdapter",
    "GrokAdapter",
    "JunieAdapter",
    "OpenAIAPIAdapter",
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
#: NORMALLY, across adapters that expose one — grok's `EndTurn`, agy's
#: `SUCCESS`, codex's `turn.completed`.
#:
#: Assembled from each adapter's OWN constant rather than re-spelled, because a
#: second list of these words is a list that drifts — and it already had. The
#: batched aggregate measured "abnormal" as `!= "EndTurn"`, one adapter's
#: vocabulary applied to all of them, so agy's normal terminal status was
#: promoted to the top of a record as though it were a truncation signal (and
#: read, in the verdict banner, as a success). This is a REPORTING vocabulary
#: only: no trust axis is computed from it. An adapter still judges its OWN
#: run's health itself, on its own terms, in `parse`/`classify` — this is the
def _normal_words(value: "str | frozenset[str]") -> frozenset[str]:
    """One adapter's normal terminal word(s), as a flat set either way.

    A bare `str` is iterable, so `frozenset(value)` on one would silently
    explode it into its LETTERS -- which is the failure this helper exists to
    make impossible, not a hypothetical: it would put `E`, `n`, `d`... in the
    cross-adapter set and quietly accept nonsense as a normal terminal word.
    """
    return frozenset({value}) if isinstance(value, str) else frozenset(value)


#: one place that has to compare terminal words ACROSS adapters, because a
#: single batched review can be answered by several of them.
#:
#: Junie is deliberately absent: its outer runner emits a contract payload
#: with no harness completion word equivalent to EndTurn/SUCCESS, so
#: `stop_reason` stays None rather than inventing a token the CLI does not
#: produce.
#: Each adapter contributes EVERY word it calls normal, flattened. An adapter
#: may export one word or a set of them -- grok accepts both `EndTurn` and the
#: `end_turn` newer CLIs emit -- and the two shapes have to end up as peers
#: here. Building this with a set literal put grok's set INSIDE as a single
#: element, so `"EndTurn" in NORMAL_STOP_REASONS` was False and a perfectly
#: normal grok batch published its own clean terminal word as the round's first
#: ABNORMAL one. `_normal_words` is what keeps a future adapter switching from
#: a word to a set from re-introducing that silently.
NORMAL_STOP_REASONS: frozenset[str] = frozenset().union(
    *(_normal_words(v) for v in
      (_GROK_NORMAL_STOP, _AGY_NORMAL_STOP, _CODEX_NORMAL_STOP)))


# Keyed by PROVIDER, not by adapter name: config names a provider, and one
# provider may ship more than one CLI. Typed as `type[Adapter]`, not bare
# `type`: registering a class that does not satisfy the protocol is then a type
# error at the table, not a surprise at the first `parse()` of a real review.
_REGISTRY: dict[str, type[Adapter]] = {
    "xai": GrokAdapter,
    "openai": CodexAdapter,
    "google": AgyAdapter,
    "junie": JunieAdapter,
    # Metered HTTP (not the codex subscription CLI). Requires OPENAI_API_KEY.
    "openai-api": OpenAIAPIAdapter,
}


def get_adapter(provider: str) -> Adapter:
    try:
        return _REGISTRY[provider]()
    except KeyError:
        raise ValueError(
            f"no adapter for provider {provider!r} "
            f"(known: {sorted(_REGISTRY)})") from None
