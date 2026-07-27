"""The single definition of the skodun trust invariant.

Every module that needs to know whether a review may suppress a re-review or
pass the gate imports :func:`is_trustworthy` from here. It is never re-derived.
"""

from __future__ import annotations


def is_trustworthy(parse_ok: bool, degraded: bool, diff_truncated: bool) -> bool:
    """trustworthy = parse_ok and not degraded and not diff_truncated.

    This is the single definition of the trust invariant, but it does no type
    checking of its own: it coerces its inputs with plain truthiness, so e.g.
    ``is_trustworthy("false", "", "")`` returns ``True`` rather than raising.
    Callers must pass real ``bool``s. The strict enforcement of that — a hard
    rejection of non-bool axis values — lives at the persistence chokepoint in
    :meth:`skodun.store.Store.save_review`, not here, because later code paths
    call this function on values derived from legacy JSON where ints may
    still appear and need to flow through untouched.
    """
    return bool(parse_ok) and not degraded and not diff_truncated
