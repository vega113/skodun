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
`effective_trustworthy` reuses the same recorded-field-wins-over-derived-axes
precedence rule as `legacy_import._recorded_denies_trust` / oracle
`is_trustworthy`, for the same reason: a legacy row predating the
`trustworthy` field must not read as untrustworthy just because the field is
absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .legacy_import import INDEX_NAME, _axis, _iter_records
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
    """Whether `row` counts as trustworthy, legacy back-compat rule included.

    A skodun-stored row always carries a real, recomputed `trustworthy` bool
    (`Store.save_review`'s invariant), so for those rows this is just
    `row["trustworthy"] is True`. A raw legacy index row may predate the field
    entirely, so the same precedence already used on import applies here: a
    row that *records* `trustworthy` is taken at its word (`is True`, so `1`
    or `"true"` is not trust); only a row where the field is absent or
    explicitly `null` falls back to deriving trust from the three axes.
    """
    if not row:
        return False
    recorded = row.get("trustworthy")
    if recorded is not None:
        return recorded is True
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
        l = legacy.get(dh)
        deltas = dict(
            findings_total=(_findings_total(s), _findings_total(l)),
            sev_high=(_sev(s, "high"), _sev(l, "high")),
            sev_medium=(_sev(s, "medium"), _sev(l, "medium")),
            sev_low=(_sev(s, "low"), _sev(l, "low")),
        )
        if s is not None and l is not None:
            match = (effective_trustworthy(s) == effective_trustworthy(l)
                      and (_findings_total(s) > 0) == (_findings_total(l) > 0))
        else:
            match = False
        out.append(Comparison(dh, s, l, match, deltas))
    return out
