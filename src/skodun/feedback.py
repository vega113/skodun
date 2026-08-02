"""Agent/human feedback ledger — judgment and product notes, not gate triage.

Triage (``dismiss`` / ``defer`` / ``reopen``) is the **human liability** path
that clears or reopens findings for ``gate``. This module is different on
purpose:

* **Agents and humans** may append feedback.
* Feedback is **append-only** and **never** changes open-finding state.
* Kinds cover finding-level judgment, whole-review quality, and **skodun
  product bugs** so maintainers can list notes later and open issues.

Use triage when a human decides the gate may pass. Use feedback when an agent
(or human) records a judgment or a product defect without silently clearing
the gate.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .store import _TS_FORMAT
from .textnorm import collapse_ws

if TYPE_CHECKING:
    from .store import Store

#: Who wrote the note. Agents should set ``actor=agent``.
ACTORS = frozenset({"agent", "human", "unknown"})

#: What the note is about.
KINDS = frozenset({
    # Judgment on one finding (agree / disagree / nuance) — does not clear gate.
    "finding_judgment",
    # Quality of a whole review (noisy, excellent, missed issue, …).
    "review_quality",
    # Suspected bug or defect *in skodun* for later human issue filing.
    "product_bug",
    # General product note (docs, UX, config) without claiming a bug.
    "product_note",
})

MIN_BODY_CHARS = 20


class FeedbackError(ValueError):
    """Validation refused this feedback (kind, actor, body, or fields)."""


def _iso_now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


def validate_actor(actor: str) -> str:
    cleaned = str(actor or "").strip().lower()
    if cleaned not in ACTORS:
        raise FeedbackError(
            f"actor must be one of {sorted(ACTORS)}, got {actor!r}")
    return cleaned


def validate_kind(kind: str) -> str:
    cleaned = str(kind or "").strip().lower()
    if cleaned not in KINDS:
        raise FeedbackError(
            f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    return cleaned


def validate_body(body: str) -> str:
    cleaned = collapse_ws(body)
    if not cleaned:
        raise FeedbackError("feedback body is required (it was empty)")
    if len(cleaned) < MIN_BODY_CHARS:
        raise FeedbackError(
            f"feedback body is {len(cleaned)} chars; at least "
            f"{MIN_BODY_CHARS} are required so later inspection has substance")
    return cleaned


def validate_fields(kind: str, *, review_id: str | None,
                    finding_index: int | None) -> None:
    """Kind-specific required fields."""
    if kind == "finding_judgment":
        if not review_id or not str(review_id).strip():
            raise FeedbackError(
                "finding_judgment requires review_id")
        if finding_index is None:
            raise FeedbackError(
                "finding_judgment requires finding_index (as triage_list [n])")
        if (not isinstance(finding_index, int)
                or isinstance(finding_index, bool)
                or finding_index < 0):
            raise FeedbackError(
                f"finding_index must be a non-negative int, "
                f"got {finding_index!r}")
    if kind == "review_quality":
        if not review_id or not str(review_id).strip():
            raise FeedbackError("review_quality requires review_id")


def record(
        store: "Store", *,
        kind: str,
        body: str,
        actor: str = "agent",
        review_id: str | None = None,
        finding_index: int | None = None,
        provider: str | None = None,
        repo: str | None = None,
        source: str | None = None,
        at: str | None = None) -> dict:
    """Validate and append one feedback event. Returns the stored row."""
    kind = validate_kind(kind)
    actor = validate_actor(actor)
    body = validate_body(body)
    rid = (str(review_id).strip() if review_id is not None
           and str(review_id).strip() else None)
    validate_fields(kind, review_id=rid, finding_index=finding_index)
    return store.feedback_append(
        at=at if at is not None else _iso_now(),
        actor=actor,
        kind=kind,
        body=body,
        review_id=rid,
        finding_index=finding_index,
        provider=(str(provider).strip() or None) if provider else None,
        repo=(str(repo).strip() or None) if repo else None,
        source=(str(source).strip() or None) if source else None,
    )


def list_feedback(
        store: "Store", *,
        kind: str | None = None,
        review_id: str | None = None,
        limit: int = 50) -> list[dict]:
    """Newest first. ``kind`` if set must be a known kind."""
    k = validate_kind(kind) if kind is not None and str(kind).strip() else None
    rid = (str(review_id).strip() if review_id is not None
           and str(review_id).strip() else None)
    return store.feedback_list(kind=k, review_id=rid, limit=limit)


def format_row(row: dict) -> str:
    """One human-readable line for CLI / MCP list."""
    seq = row.get("seq")
    at = row.get("at") or "?"
    actor = row.get("actor") or "?"
    kind = row.get("kind") or "?"
    bits = [f"#{seq}", at, actor, kind]
    if row.get("review_id"):
        bits.append(f"review={row['review_id']}")
    if row.get("finding_index") is not None:
        bits.append(f"finding=[{row['finding_index']}]")
    if row.get("provider"):
        bits.append(f"provider={row['provider']}")
    if row.get("repo"):
        bits.append(f"repo={row['repo']}")
    if row.get("source"):
        bits.append(f"source={row['source']}")
    body = collapse_ws(row.get("body") or "")
    if len(body) > 160:
        body = body[:157] + "..."
    return " | ".join(str(b) for b in bits) + f" | {body}"
