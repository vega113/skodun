"""Refuter independence is a conservative provider-family comparison.

This is annotation/adoption policy, never a trust axis. Provenance must come
from accepted pipeline outcomes, not configured reviewers or model payloads.
Provider-family difference is only a proxy for independent reasoning; the two
OpenAI adapters are one family, and other adapters retain their provider ID.
"""

from __future__ import annotations


def provider_family(value) -> str | None:
    if not isinstance(value, str):
        return None
    provider = value.lower()
    if provider == "openai-api":
        return "openai"
    return provider if provider in {"openai", "xai", "google", "junie"} else None


def contributor_families(providers) -> frozenset[str] | None:
    """Unknown or incomplete lists cannot establish independence."""
    if not isinstance(providers, (list, tuple)) or not providers:
        return None
    families = [provider_family(provider) for provider in providers]
    if any(family is None for family in families):
        return None
    return frozenset(families)


def adoption_refusal(meta: dict, annotation: dict) -> str | None:
    """Validate stored pass evidence before an annotation can write triage."""
    contributors = contributor_families(meta.get("contributing_providers"))
    provider = provider_family(meta.get("provider"))
    if contributors is None or provider is None:
        return ("independent refuter provenance is missing or unknown; re-review "
                "with an independent provider or dismiss with your own audited reason")
    if meta.get("same_provider_as_finder") is True or provider in contributors:
        return ("the refuter used the same provider family as a finding contributor; "
                "independent refutation is required, or dismiss with your own audited reason")
    if (provider_family(annotation.get("provider")) != provider
            or annotation["provider"].lower() != meta["provider"].lower()
            or annotation.get("model") != meta.get("model")):
        return ("the refuter annotation provider/model does not match the pass "
                "provenance; dismiss with your own audited reason")
    return None
