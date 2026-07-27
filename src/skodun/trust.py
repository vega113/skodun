"""The single definition of the skodun trust invariant.

Every module that needs to know whether a review may suppress a re-review or
pass the gate imports :func:`is_trustworthy` from here. It is never re-derived.
"""

from __future__ import annotations


def is_trustworthy(parse_ok: bool, degraded: bool, diff_truncated: bool) -> bool:
    """trustworthy = parse_ok and not degraded and not diff_truncated."""
    return bool(parse_ok) and not degraded and not diff_truncated
