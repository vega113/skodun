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


def coerce_count(value: object) -> int:
    """THE rule for reading a count off an already-persisted record. Total.

    ONE definition, imported by every renderer -- `banner` here,
    `cli._cmd_log`, `cli._fmt_side`, and `shadow`. There used to be three, and
    they disagreed: `banner` parsed `"3"` as 3 while `log` and `shadow` read it
    as 0, so the same stored row printed `findings=3 severity=2/0/0` from one
    command and `0-0-0` from another. A count is a count wherever it is read.

    The rule is `triage.load_valid_artifact`'s: a count is a plain, non-`bool`
    `int`, and everything else is not a count and renders `0`. That is
    deliberate agreement with the gate's own validator -- an artifact whose
    `findings_total` is a string, a float or a bool cannot certify anything, so
    a display that invented a number from one would be showing a figure no
    other part of the system accepts. `bool` needs the explicit guard because
    `isinstance(True, int)` is `True` in Python, and reading `findings_total:
    true` as ONE finding would make `shadow.compare` report a MISMATCH that
    never happened.

    `0` rather than a visible sentinel (e.g. `findings=?`): the banner's format
    is a fixed contract downstream tooling parses as `field=<int>`, the same
    convention already used for a missing/`None` field (see `banner`'s
    docstring), so an unusable count folds into that same "absent means zero"
    convention instead of inventing a second, non-numeric shape for consumers
    to special-case. Rendering must never be the thing that crashes a run, so
    this cannot raise for any input.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def one_line(value: object) -> str:
    """Stringify a field so it cannot break out of a single-line record.

    ONE definition, imported by `banner`/`banner_failure` here and by
    `cli._cmd_log`. The banner is always the LAST line of stdout, and the log
    is one line per review, so nothing either interpolates may contain a
    newline -- a `summary`, `reason`, or `stop_reason` field that happens to
    carry `\n`/`\r` (whether from a misbehaving reviewer or a hand-edited
    record) must not be able to split the line or forge a second banner
    beneath it. Every field is coerced to `str` and has `\r`/`\n` replaced
    with a space before it reaches the format string; `None` and missing
    fields become the empty string rather than raising or printing the
    literal ``"None"``.

    Note the difference from `flatten_lines`: that one JOINS a multi-line
    message with a visible separator because every line of it is wanted in the
    record; this one substitutes spaces in a value that was never supposed to
    be multi-line in the first place.
    """
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")


def flatten_lines(message: str) -> str:
    """One line, with every line of `message` kept and visibly separated.

    ONE definition, imported by `cli._record_setup_failure` and by
    `gate.run_gate._record`. Both write the same `gate_events.note` column and
    must write it the same way.

    The recorded note used to be the LAST line only, which silently dropped
    the `identity note:` lines the gate prefixes to its verdict -- so an
    auditor reading `gate_events` could not see that the decision was made
    against an under-scoped identity, which is precisely what those notes
    exist to make loud. `gate_events.note` is a single-line column by
    convention, so the lines are joined rather than truncated.
    """
    return " | ".join(message.splitlines())


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
    head = one_line(review.get("head"))[:9]
    line = (
        "SKODUN VERDICT: "
        f"trustworthy={_lower_bool(review.get('trustworthy'))} "
        f"findings={coerce_count(review.get('findings_total'))} "
        f"degraded={_lower_bool(review.get('degraded'))} "
        f"stop_reason={one_line(review.get('stop_reason'))} "
        f"head={head} "
        f"id={one_line(review.get('id'))} "
        f"severity={coerce_count(sev.get('high'))}/{coerce_count(sev.get('medium'))}"
        f"/{coerce_count(sev.get('low'))}"
    )
    return line


def banner_failure(reason: str) -> str:
    """Render the verdict banner for a path where no record was ever persisted.

    `banner` needs a stored review to read back; this is the fallback for
    every path that never got that far (no trustworthy review found, setup
    failed, etc.) so a run always ends with a verdict line, never silence.
    """
    return f"SKODUN VERDICT: trustworthy=false reason={one_line(reason)}"
