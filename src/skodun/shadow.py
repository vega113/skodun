"""Shadow comparison: skodun's verdicts against the legacy `.grok-reviews` archive.

This is an observational check, not a gate: it never blocks anything and it
never raises. Its whole job is to answer one question for a human -- "does the
new reviewer agree with the old one, on the same content?" -- without ever
being in a position to make that answer wrong in the dangerous direction.

**The union, not just skodun's rows.** `compare` iterates every `diff_hash`
present on *either* side. Iterating only skodun's store rows could never
surface a hash the legacy archive reviewed but skodun never touched --
`legacy-only` would be a bucket that can never be nonzero, and the summary
line would quietly lie about coverage.

**`match` has exactly one definition**, and it is intentionally coarse: both
sides present, both agree on `trustworthy`, and both agree on cleanliness
(`findings_total == 0` vs `> 0`). Two independent LLM runs over the same diff
are not expected to count the same findings or tally the same severities --
that would be a false failure of the model, not of skodun -- so exact counts
never enter `match`. They still matter to a human deciding whether the new
reviewer is *worse*, so they are carried in `deltas` for eyes only.

**Legacy `index.jsonl` is untrusted, crash-prone data.** A concurrent legacy
writer can leave a half-written final line, so parsing reuses
`legacy_import._iter_records`, the same corrupt/truncated-line-tolerant reader
the importer uses -- matching its posture is the point, not an accident.
`effective_trustworthy` calls `legacy_import._recorded_denies_trust` and then
`trust.is_trustworthy` -- the importer's own two-step precedence, in the
importer's own order -- so a legacy row reads here exactly as the importer
reads it. Two reasons, one per half of the rule: a row predating the
`trustworthy` field must not read as untrustworthy just because the field is
absent, and a row that *records* trust its axes deny must not read as
trustworthy just because it said so. (What the importer would ultimately
STORE can still be stricter than this: it also demotes a row whose full
artifact is missing or invalid. That is a statement about what may pass a
GATE, not about what the legacy tool concluded, and this module is comparing
conclusions.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .legacy_import import INDEX_NAME, _axis, _iter_records, _recorded_denies_trust
from .store import Store
from .trust import is_trustworthy

_AXES = ("parse_ok", "degraded", "diff_truncated")

# Large enough to be "all rows" for any real store, small enough to stay a
# plain Python/SQLite int without any special-casing of "no limit".
_ALL_ROWS = 10**9


@dataclass(frozen=True)
class Comparison:
    """One `diff_hash`'s two sides, and the human-facing deltas between them.

    `skodun` / `legacy` are the newest-by-`reviewed_at` record from each side,
    or `None` if that side never reviewed this content at all. `match` is
    `False` whenever a side is missing -- there is nothing to agree on.
    """

    diff_hash: str
    skodun: dict | None
    legacy: dict | None
    match: bool
    deltas: dict


def effective_trustworthy(row: dict | None) -> bool:
    """Whether `row` counts as trustworthy, by the importer's exact precedence.

    This CALLS `legacy_import`'s rule rather than restating it, because the
    verdict this module reports has to be the verdict the same row gets on
    import -- otherwise `compare` can report agreement between a skodun row
    and a legacy row that the importer read the other way, which is the one
    direction the module docstring says it must never get wrong.

    The rule has two halves and both are load-bearing:

      * A row that RECORDS `trustworthy` is taken at its word, but only in the
        denying direction (`_recorded_denies_trust`: not `None` and not
        `True`, so `1` or `"true"` is a denial, not trust). A recorded verdict
        can never GRANT trust against the axes -- `Store.save_review`
        recomputes trust from the axes on every write, so a row spelling
        `trustworthy: true` beside `degraded: true` is stored untrustworthy
        and must read untrustworthy here too.
      * A row where the field is absent or `null` -- it was added late, and it
        is absent on a large fraction of any real archive -- derives trust
        from the three axes. Reading its absence as `false` is the error that
        once inflated an audit's failure rate from ~2% to 65%.
    """
    if not row:
        return False
    if _recorded_denies_trust(row):
        return False
    axes = {k: _axis(row, k) for k in _AXES}
    return is_trustworthy(**axes)


def _int(value: object) -> int:
    """A persisted count, coerced to `int`, or `0` for anything unusable.

    Both sides here are untrusted data by the time this module sees them: a
    legacy row is unvalidated JSON, and even a skodun row could in principle
    have been hand-edited on disk. `bool` is rejected explicitly because
    `isinstance(True, int)` is `True` in Python.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _findings_total(row: dict | None) -> int:
    return _int(row.get("findings_total")) if row else 0


def _sev(row: dict | None, key: str) -> int:
    if not row:
        return 0
    sev = row.get("severity")
    if not isinstance(sev, dict):
        return 0
    return _int(sev.get(key))


def _newest(rows: list[dict]) -> dict:
    """The row with the lexicographically-greatest `reviewed_at`.

    ISO-8601 UTC timestamps (`Store`'s own convention, and the legacy
    archive's) sort correctly as plain strings, so no parsing is needed. A
    missing/non-string `reviewed_at` sorts as the empty string -- oldest, not
    an error -- so one malformed timestamp cannot hide a real one.
    """
    def key(r: dict) -> str:
        v = r.get("reviewed_at")
        return v if isinstance(v, str) else ""
    return max(rows, key=key)


def _legacy_rows(grok_reviews_dir: Path) -> dict[str, dict]:
    """`diff_hash -> newest legacy row`, tolerating corrupt/truncated lines.

    A missing archive directory or a missing `index.jsonl` yields no rows at
    all via `_iter_records`, never an exception -- a machine that never ran
    the legacy tool, or a shadow run before the archive exists, is normal.
    """
    by_hash: dict[str, list[dict]] = {}
    for rec in _iter_records(Path(grok_reviews_dir) / INDEX_NAME):
        if rec is None:            # corrupt/truncated/non-object line
            continue
        dh = rec.get("diff_hash")
        if isinstance(dh, str) and dh:
            by_hash.setdefault(dh, []).append(rec)
    return {dh: _newest(rows) for dh, rows in by_hash.items()}


def _skodun_rows(store: Store) -> dict[str, dict]:
    """`diff_hash -> newest skodun row`, across every branch.

    `Store` exposes no direct "every row" query, so this reads every review
    through the existing `list_reviews(branch=None, limit=...)` API with a
    limit large enough to mean "all of them" for any real store, rather than
    reaching into the store's private connection.
    """
    by_hash: dict[str, list[dict]] = {}
    for rec in store.list_reviews(None, _ALL_ROWS):
        dh = rec.get("diff_hash")
        if isinstance(dh, str) and dh:
            by_hash.setdefault(dh, []).append(rec)
    return {dh: _newest(rows) for dh, rows in by_hash.items()}


def compare(store: Store, grok_reviews_dir: Path,
            diff_hash: str | None) -> list[Comparison]:
    """Compare skodun's verdicts to the legacy archive's, hash by hash.

    Iterates the union of `diff_hash`es seen on either side, or just
    `diff_hash` when one is given (and it appears on at least one side --
    otherwise there is nothing to report and the result is empty). See the
    module docstring for why the union, and for the exact, single definition
    of `match`.
    """
    legacy = _legacy_rows(Path(grok_reviews_dir))
    skodun = _skodun_rows(store)

    if diff_hash is not None:
        hashes = [diff_hash] if (diff_hash in legacy or diff_hash in skodun) else []
    else:
        hashes = sorted(set(legacy) | set(skodun))

    out: list[Comparison] = []
    for dh in hashes:
        s = skodun.get(dh)
        g = legacy.get(dh)
        deltas = dict(
            findings_total=(_findings_total(s), _findings_total(g)),
            sev_high=(_sev(s, "high"), _sev(g, "high")),
            sev_medium=(_sev(s, "medium"), _sev(g, "medium")),
            sev_low=(_sev(s, "low"), _sev(g, "low")),
        )
        if s is not None and g is not None:
            match = (effective_trustworthy(s) == effective_trustworthy(g)
                      and (_findings_total(s) > 0) == (_findings_total(g) > 0))
        else:
            match = False
        out.append(Comparison(dh, s, g, match, deltas))
    return out
