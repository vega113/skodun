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


def _coerce_int(value: object) -> int:
    """Coerce a persisted count to `int`, returning `0` when it cannot be.

    `findings_total` and the `severity` sub-counts come off an
    already-persisted record this module does not control -- a corrupted or
    hand-edited entry can carry a non-numeric string (or some other
    unrelated type) where a count belongs. Plain `int(...)` raises on those,
    which would make the count the thing that crashes the banner. We choose
    to render `0` silently rather than a visible sentinel (e.g. `findings=?`):
    the banner's format is a fixed contract downstream tooling parses as
    `field=<int>`, the same convention already used for a missing/`None`
    field (see `banner`'s docstring), so an unparseable count is folded into
    that same "absent means zero" convention instead of inventing a second,
    non-numeric shape for consumers to special-case.
    """
    if isinstance(value, (bool, int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


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
    sev = review.get("severity")
    if not isinstance(sev, dict):
        # `severity` guards only falsy values with `or {}`; a truthy
        # non-dict (a list, str, or int) would survive that and then
        # `.get()` would raise `AttributeError`. Fall back to `{}` for any
        # shape that isn't a dict, not just falsy ones.
        sev = {}
    head = _clean(review.get("head"))[:9]
    line = (
        "SKODUN VERDICT: "
        f"trustworthy={_lower_bool(review.get('trustworthy'))} "
        f"findings={_coerce_int(review.get('findings_total'))} "
        f"degraded={_lower_bool(review.get('degraded'))} "
        f"stop_reason={_clean(review.get('stop_reason'))} "
        f"head={head} "
        f"id={_clean(review.get('id'))} "
        f"severity={_coerce_int(sev.get('high'))}/{_coerce_int(sev.get('medium'))}"
        f"/{_coerce_int(sev.get('low'))}"
    )
    return line


def banner_failure(reason: str) -> str:
    """Render the verdict banner for a path where no record was ever persisted.

    `banner` needs a stored review to read back; this is the fallback for
    every path that never got that far (no trustworthy review found, setup
    failed, etc.) so a run always ends with a verdict line, never silence.
    """
    return f"SKODUN VERDICT: trustworthy=false reason={_clean(reason)}"
