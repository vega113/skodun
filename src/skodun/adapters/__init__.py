"""Provider adapters: build one model CLI's invocation, interpret its output.

An adapter owns everything provider-specific about a single review attempt —
the argv, the response envelope's shape, and which harness misbehaviours make
that response untrustworthy. Everything upstream of it works in terms of
`ParseResult` and never in terms of one CLI's flags.

Phase 1 ships one provider (`xai`). `get_adapter` raises rather than falling
back to a default: silently reviewing with the wrong model is worse than not
reviewing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Defaults, Reviewer
from .grok import GrokAdapter, ParseResult

__all__ = ["Adapter", "ParseResult", "GrokAdapter", "get_adapter"]


class Adapter(Protocol):
    """What every provider adapter must offer."""

    name: str

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path) -> list[str]:
        """Full argv for one attempt. The prompt travels as a file."""
        ...

    def parse(self, stdout: bytes, stderr: bytes) -> ParseResult:
        """Interpret one attempt's raw output. Never raises on garbage."""
        ...


# Typed as `type[Adapter]`, not bare `type`: registering a class that does not
# satisfy the protocol is then a type error at the table, not a surprise at the
# first `parse()` of a real review.
_REGISTRY: dict[str, type[Adapter]] = {"xai": GrokAdapter}


def get_adapter(provider: str) -> Adapter:
    try:
        return _REGISTRY[provider]()
    except KeyError:
        raise ValueError(
            f"no adapter for provider {provider!r} "
            f"(known: {sorted(_REGISTRY)})") from None
