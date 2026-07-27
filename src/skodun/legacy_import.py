"""Import the legacy `.grok-reviews` archive into the skodun store.

Two continuities are at stake, and they are the whole reason this module
exists:

  * **Gate continuity.** Content that was already reviewed by the legacy tool
    must keep satisfying the gate. Without it, every developer's first push
    after the migration is blocked on a re-review of work already reviewed.
  * **Ledger continuity.** A finding a human already dismissed, in writing,
    must stay dismissed. A dismissal that evaporates asks the same person to
    re-adjudicate the same finding, and the second answer is reliably lazier
    than the first.

THE CENTRAL RULE: **a trustworthy import requires the full artifact.**

An `index.jsonl` row is a *derived summary*. It carries counts (`findings_total`,
`severity`) but never `findings[]`. Storing such a row as trustworthy would be
the worst of both worlds: `Store.latest_trustworthy_for` would select it, and
then `triage.load_valid_artifact` -- the gate's fail-closed validator -- would
reject it for having no `findings`, so the gate returns 2. And it would keep
returning 2, because the store keeps handing back the same unusable record.
The gate would be stuck, and the "fix" would look like a store corruption
rather than an import bug.

So for every index row that would otherwise be trustworthy this module loads
`<id>.json`, validates it with the same validator the gate uses, and
cross-checks it against the summary. If the artifact is missing, unreadable,
invalid, or disagrees with its own index row, the row is imported **demoted**
(`parse_ok=False`, an explicit `failure_reason`) and counted. History is
preserved; trust is not. The cost of a demotion is one re-review; the cost of
a false trust is a jammed gate.

Nothing here ever aborts on bad input. The archive is appended to by concurrent
workers and a crashing writer leaves a half-written final line, so a corrupt
line costs one record and is counted in `skipped_lines` -- never the import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .store import Store
from .textnorm import ledger_key
from .triage import ArtifactError, load_valid_artifact
from .trust import is_trustworthy

INDEX_NAME = "index.jsonl"
LEDGER_NAME = "triage.jsonl"

DEMOTED_REASON = "legacy import: artifact missing/invalid"

# What a trust axis means when the archive carries something that is not a
# `bool` there. Legacy JSON is untrusted data: `1`, `"false"`, `[]` are all
# shapes a hand-edited or half-migrated archive can produce, and
# `Store.save_review` refuses to coerce them (`bool("false")` is `True`). Each
# axis is therefore read at its UNSAFE value, which is the value that denies
# trust: `parse_ok` false, `degraded` and `diff_truncated` true.
#
# DIVERGENCE FROM THE ORACLE, deliberate and fail-closed. The oracle's fallback
# (grok_review_triage.py:255-272) spells the last two checks `is not True`, so
# `degraded: 1` reads there as "not degraded" and the row is trustworthy. The
# project's trust invariant is stated verbatim as
# `parse_ok and not degraded and not diff_truncated`, i.e. by truthiness, under
# which `degraded: 1` denies trust. Where the two readings differ this module
# takes the stricter one; the worst case is one extra review.
_UNSAFE_AXIS = {"parse_ok": False, "degraded": True, "diff_truncated": True}


@dataclass(frozen=True)
class ImportStats:
    """What one import run did.

    `reviews` counts every index row persisted, trusted or demoted --
    `demoted_no_artifact` is a subset of it, not a separate bucket, so that
    `reviews` always answers "how much history was preserved".
    """

    reviews: int = 0
    triage: int = 0
    skipped_lines: int = 0
    demoted_no_artifact: int = 0


def _iter_records(path: Path):
    """Yield `(record_or_None)` for each non-blank line of a JSONL file.

    `None` means the line was unusable and should be counted as skipped. A
    missing file yields nothing at all: an archive without a ledger is normal,
    not an error.

    `errors="replace"` is deliberate and matches the oracle: these files are
    appended to by concurrent workers, so a partial write can leave an invalid
    byte mid-file. Strict decoding would make that one byte destroy the whole
    archive's readability.
    """
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        while True:
            # Iterating the handle can itself raise on a mid-read I/O error;
            # a truncated read must end the import quietly, not crash it.
            try:
                line = fh.readline()
            except (OSError, UnicodeDecodeError):
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue           # blank padding is not corruption
            try:
                rec = json.loads(line)
            except Exception:
                yield None         # truncated final line, garbage, half a write
                continue
            # Valid JSON that is not an object ("[]", "3", a bare string) would
            # make every `.get` below raise. One skipped record, not a crash.
            yield rec if isinstance(rec, dict) else None


def _axis(row: dict, name: str) -> bool:
    """One trust axis of a legacy row, as a real `bool`.

    Absent or `null` reads as `False`, which is the pre-field default and the
    oracle's reading: a row from before `diff_truncated` existed is not
    truncated, and a row with no `parse_ok` never claimed to have parsed. Any
    other non-`bool` reads at `_UNSAFE_AXIS[name]`.
    """
    value = row.get(name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return _UNSAFE_AXIS[name]


def _recorded_denies_trust(row: dict) -> bool:
    """Whether the row's own `trustworthy` field refuses trust.

    ORACLE PARITY on the precedence rule (`is_trustworthy`,
    grok_review_triage.py:255-272): a row that RECORDS `trustworthy` is taken
    at its word -- `is True`, so `1` or `"true"` is not trust -- and only a row
    where the field is absent or `null` (it was added 2026-07-14) falls back to
    deriving trust from the axes. Treating the field's absence as false would
    mark nearly every historical row untrustworthy, which is the error that
    inflated the original audit's failure rate from ~2% to 65%.

    The recorded verdict can only ever DENY trust, never grant it against the
    axes. `Store.save_review` recomputes `trustworthy` from the axes on every
    write and the gate re-asserts that the artifact's own field agrees with
    that recomputation, so a row spelling `trustworthy: true` while carrying
    `degraded: true` cannot be stored the way the oracle reads it -- the record
    would contradict itself and the gate would reject it as an inconsistent
    archive. It imports demoted instead. That is a second deliberate,
    fail-closed divergence, in the same direction and for the same reason as
    the one already documented in `gate.py`.
    """
    recorded = row.get("trustworthy")
    return recorded is not None and recorded is not True


def _demote(row: dict, axes: dict[str, bool], reason: str, *,
            force_reason: bool = False) -> dict:
    """Deny trust on the record itself, preserving what the archive claimed.

    The demotion is written into the AXES and not merely into the verdict,
    because `save_review` DERIVES the verdict from the axes -- a record whose
    stored `trustworthy` disagreed with its own `parse_ok` is exactly what the
    gate reads as a corrupt archive. `parse_ok` is the axis that carries it: it
    is the one that means "this run did not produce a usable result", which is
    true of every demotion here.

    Overwriting an axis loses information, so the values the archive actually
    recorded are kept verbatim under `legacy_trust`. That is what makes this a
    demotion rather than a rewrite: the history stays auditable, only the trust
    is withdrawn.
    """
    out = {**row, "legacy_trust": {k: row.get(k) for k in _UNSAFE_AXIS}
                                  | {"trustworthy": row.get("trustworthy")}}
    if force_reason or not out.get("failure_reason"):
        out["failure_reason"] = reason
    axes["parse_ok"] = False
    return out


def _load_artifact(archive: Path, row: dict, review_id: str) -> dict | None:
    """The full artifact for `review_id`, or `None` if it cannot be trusted.

    Rejects, all for the same reason -- a summary and an artifact that disagree
    describe two different reviews, and attesting to either one attests to
    content nobody reviewed:

      * an unreadable or non-JSON file;
      * anything `load_valid_artifact` rejects (the gate's own validator, so
        anything imported as trustworthy is guaranteed to survive the gate);
      * an artifact naming a different review `id` than the row that pointed
        at it -- it would be stored under the artifact's id and the index row's
        history would silently vanish;
      * a `diff_hash` disagreement -- the gate selects on `diff_hash`, so this
        is the exact field whose corruption would certify the wrong content;
      * a `findings_total` disagreement, including a row that fails to state
        one: unverifiable is not the same as verified.
    """
    # The `id` comes out of the archive's own JSON and is interpolated into a
    # filename, so it is untrusted path input. An id spelling `../../secrets`
    # would make the importer read a file outside the archive entirely. Only a
    # bare filename is accepted.
    name = f"{review_id}.json"
    if Path(name).name != name or "\\" in name:
        return None
    try:
        with open(archive / name, encoding="utf-8") as fh:
            art = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    try:
        art = load_valid_artifact(art)
    except ArtifactError:
        return None
    if art["id"] != review_id:
        return None
    if art["diff_hash"] != row.get("diff_hash"):
        return None
    total = row.get("findings_total")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    if total != art["findings_total"]:
        return None
    return art


def _import_index(store: Store, archive: Path, stats: dict) -> None:
    for row in _iter_records(archive / INDEX_NAME):
        if row is None:
            stats["skipped_lines"] += 1
            continue
        review_id = row.get("id")
        if not isinstance(review_id, str) or not review_id:
            # Without an id there is nothing to key the row on, and
            # `save_review` would raise. One skipped row.
            stats["skipped_lines"] += 1
            continue

        axes = {k: _axis(row, k) for k in _UNSAFE_AXIS}
        artifact = None
        if _recorded_denies_trust(row):
            # The archive already judged this review untrustworthy. Its axes
            # may still derive True (that is exactly the case the recorded
            # field was added to override), so the demotion has to be made
            # explicit or `save_review` would recompute the trust back.
            row = _demote(row, axes, "legacy import: archive recorded "
                                     "trustworthy=false")
        elif is_trustworthy(**axes):
            artifact = _load_artifact(archive, row, review_id)
            if artifact is None:
                row = _demote(row, axes, DEMOTED_REASON, force_reason=True)
                stats["demoted_no_artifact"] += 1

        # The artifact wins every field it defines -- it is the record the
        # review actually produced -- and the index row fills in anything it
        # omits. `source` is set last and explicitly: `save_review` defaults
        # the COLUMN to 'skodun' and dumps the record verbatim into
        # `artifact_json`, so a record that does not carry `source="legacy"`
        # itself would leave the column and the artifact disagreeing.
        rec = {**row, **(artifact or {})}
        rec.update(axes)
        rec["id"] = review_id
        rec["source"] = "legacy"
        try:
            store.save_review(rec)
        except Exception:
            stats["skipped_lines"] += 1
            continue
        stats["reviews"] += 1


def _import_ledger(store: Store, archive: Path, stats: dict) -> None:
    for rec in _iter_records(archive / LEDGER_NAME):
        if rec is None:
            stats["skipped_lines"] += 1
            continue
        # THE RECORDED KEY, NEVER A RECOMPUTED ONE. The ledger is the
        # authority: recomputing `finding_key` from the row's file/title would
        # silently re-file every dismissal under a different key if any part of
        # the key derivation ever drifted, and every previously-dismissed
        # finding would resurface as new -- which is precisely the failure this
        # import exists to prevent.
        fkey = rec.get("finding_key")
        branch = rec.get("branch")
        base_sha = rec.get("base_sha")
        if not (isinstance(fkey, str) and fkey
                and isinstance(branch, str) and isinstance(base_sha, str)):
            stats["skipped_lines"] += 1
            continue
        try:
            store.add_triage({**rec, "finding_key": fkey, "branch": branch,
                              "base_sha": base_sha,
                              "ledger_key": ledger_key(branch, base_sha, fkey)})
        except Exception:
            # `add_triage` raises KeyError when neither `review_id` nor `id` is
            # present, or when the audited reason is missing. An orphaned or
            # unauditable dismissal is not one worth honouring.
            stats["skipped_lines"] += 1
            continue
        stats["triage"] += 1


def import_legacy(store: Store, grok_reviews_dir: Path) -> ImportStats:
    """Import `grok_reviews_dir` into `store`. Idempotent, and never raises.

    Both underlying writes upsert on their primary key (`reviews.id`,
    `triage.ledger_key`), so importing the same archive twice produces the same
    store and the same stats. A demotion is therefore not a tombstone: if the
    missing artifact is later restored, a re-import upgrades the row.

    A missing archive directory returns zeros. Migration tooling runs on
    machines that never used the legacy tool, and "nothing to import" is an
    ordinary outcome there, not an error.
    """
    archive = Path(grok_reviews_dir)
    stats = dict(reviews=0, triage=0, skipped_lines=0, demoted_no_artifact=0)
    _import_index(store, archive, stats)
    _import_ledger(store, archive, stats)
    return ImportStats(**stats)
