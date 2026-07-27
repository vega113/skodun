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


def _lower_bool(value: object) -> str:
    """Render a value as `true`/`false`, defaulting falsy/missing to `false`."""
    return "true" if value else "false"


def _clean(value: object) -> str:
    """Stringify a field for interpolation into the single-line banner.

    The banner is always the last line of stdout, so nothing it interpolates
    may contain a newline -- a `summary`, `reason`, or `stop_reason` field
    that happens to carry `\n`/`\r` (whether from a misbehaving reviewer or a
    hand-edited record) must not be able to split the line or forge a second
    banner beneath it. Every field is coerced to `str` and has `\r`/`\n`
    replaced with a space before it reaches the format string; `None` and
    missing fields become the empty string rather than raising or printing
    the literal ``"None"``.
    """
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")


def banner(review: dict) -> str:
    """Render the verdict banner from an already-persisted review record.

    Every value comes straight off `review` -- never recomputed -- so the
    banner can never disagree with the record the gate later reads back. A
    record missing a field (or carrying `None` for it) renders that field as
    its zero value instead of raising: the banner must never be the thing
    that crashes a run.
    """
    sev = review.get("severity") or {}
    head = _clean(review.get("head"))[:9]
    line = (
        "SKODUN VERDICT: "
        f"trustworthy={_lower_bool(review.get('trustworthy'))} "
        f"findings={int(review.get('findings_total') or 0)} "
        f"degraded={_lower_bool(review.get('degraded'))} "
        f"stop_reason={_clean(review.get('stop_reason'))} "
        f"head={head} "
        f"id={_clean(review.get('id'))} "
        f"severity={int(sev.get('high') or 0)}/{int(sev.get('medium') or 0)}"
        f"/{int(sev.get('low') or 0)}"
    )
    return line


def banner_failure(reason: str) -> str:
    """Render the verdict banner for a path where no record was ever persisted.

    `banner` needs a stored review to read back; this is the fallback for
    every path that never got that far (no trustworthy review found, setup
    failed, etc.) so a run always ends with a verdict line, never silence.
    """
    return f"SKODUN VERDICT: trustworthy=false reason={_clean(reason)}"
