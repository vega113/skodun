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
invalid, or disagrees with its own index row in a direction that could hide
findings, the row is imported **demoted** (`parse_ok=False`, an explicit
`failure_reason`) and counted. History is preserved; trust is not. The cost of
a demotion is one re-review; the cost of a false trust is a jammed gate. The
one disagreement that does NOT demote is an artifact carrying MORE findings
than its index row: see `_load_artifact`, where the asymmetry is argued.

Trust is read off the record that is actually STORED, never off the summary
alone. The index row and the artifact each carry their own `parse_ok`,
`degraded`, `diff_truncated` and `trustworthy` fields, and the artifact wins
every field it defines -- so an artifact recording `degraded: true` beside an
index row that happens to read clean is untrustworthy, and imports demoted.
Deriving trust from the row and then stapling it onto the merged record would
let the summary launder the artifact's own denial into a PASS.

The ledger gets the same treatment: an imported dismissal must clear the very
audit floor (`triage.validate_reason`) that every dismissal skodun records
itself has to clear. A legacy row dismissing a finding as "fp" is a rubber
stamp, and honouring it would silently clear a finding on import.

Nothing here ever aborts on bad input. The archive is appended to by concurrent
workers and a crashing writer leaves a half-written final line, so a corrupt
line costs one record and is counted in `skipped_lines` -- never the import.
A failure of the STORE is different in kind and is counted separately, in
`store_failures`, so that a full disk cannot be reported as a clean run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import Store
from .textnorm import ledger_key
from .triage import ArtifactError, TriageError, load_valid_artifact, validate_reason
from .trust import is_trustworthy

INDEX_NAME = "index.jsonl"
LEDGER_NAME = "triage.jsonl"

DEMOTED_REASON = "legacy import: artifact missing/invalid"
RECORDED_DENIAL_REASON = "legacy import: archive recorded trustworthy=false"
ARTIFACT_DENIAL_REASON = "legacy import: the artifact denies its own trust"

# "The STORE could not accept a well-formed record", as opposed to "this record
# was unusable". The difference matters to an operator: a full disk mid-import
# silently reported as `skipped_lines` is indistinguishable from a few corrupt
# lines, and the run still claims success while preserving nothing.
#
# The split is by exception type, and the order below is the classification:
#
#   * `_RECORD_UNUSABLE` is sqlite's own name for "the caller handed me
#     something I cannot bind" -- a legacy row carrying a list where a string
#     belongs raises `ProgrammingError("Error binding parameter ...")` on 3.12,
#     and `InterfaceError` on older builds. That is one bad line, not a broken
#     store. (`ProgrammingError` also covers "no such table", which would be a
#     store problem; it cannot arise here, because `Store.open` creates the
#     schema before any import can start.)
#   * `_STORE_BROKEN` is everything else in the `DatabaseError` family -- disk
#     full, disk I/O error, locked database, malformed image, a read-only
#     file -- plus the failures that never reach sqlite at all.
#
# Checked in that order, so the narrower rule wins.
_RECORD_UNUSABLE = (sqlite3.ProgrammingError, sqlite3.InterfaceError)
_STORE_BROKEN = (sqlite3.DatabaseError, OSError, MemoryError)

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

# "The artifact did not carry this key at all", distinguishable from every JSON
# value it could have carried -- including `null`. See `_load_artifact`.
_MISSING = object()


@dataclass(frozen=True)
class ImportStats:
    """What one import run did.

    Every counter counts LINES OF THE ARCHIVE, not rows of the store. `reviews`
    is the number of index rows persisted, trusted or demoted, and `triage` the
    number of ledger rows persisted; both writes upsert on their primary key
    (`reviews.id`, `triage.ledger_key`), so an archive that repeats an id --
    which real archives do, once per re-review of the same loop -- persists
    that id once while contributing to the count each time. `reviews` is
    therefore an upper bound on the number of distinct reviews preserved, not
    an exact count of them, and `SELECT COUNT(*) FROM reviews` may legitimately
    be smaller. Counting distinct ids instead was considered and rejected: the
    subset counters below count events, so mixing the two units would let
    `demoted_no_artifact` exceed `reviews` for an id demoted on two lines.

    `demoted_no_artifact` counts rows demoted because the full artifact could
    not be had (missing, unreadable, invalid, or disagreeing with its row in a
    direction that could hide findings). `demoted_untrustworthy` counts rows
    demoted because the archive ITSELF denied trust -- a recorded
    `trustworthy: false` on the row, or an artifact whose own trust axes or
    `trustworthy` field deny it. Both are subsets of `reviews` and disjoint
    from each other: a row takes at most one demotion path.

    `findings_reconciled` counts the rows imported on the ARTIFACT's word
    against a stale index summary: the artifact reported more findings than the
    row did, and (see `_load_artifact`) that is trusted rather than demoted.
    It is a subset of `reviews` and disjoint from both demotion counters -- a
    row that ends up demoted is never reconciled, because its count did not
    survive as the trusted one. It is reported separately because "imported,
    but its count came from somewhere other than the index" is a thing an
    operator should be able to see without it being mistaken for a demotion.

    `skipped_lines` counts lines the archive could not supply a usable record
    for. `triage_unauditable` is split out of it rather than folded into it: a
    dismissal dropped for failing the audit floor is not corruption, it is a
    finding that will reappear at the next gate run, and an operator reading
    the numbers should be able to tell those apart. `store_failures` counts
    records that were well-formed but could not be WRITTEN; it is the one
    counter whose being nonzero means the import did not do what it claimed,
    and the CLI exits nonzero on it.
    """

    reviews: int = 0
    triage: int = 0
    skipped_lines: int = 0
    demoted_no_artifact: int = 0
    findings_reconciled: int = 0
    demoted_untrustworthy: int = 0
    triage_unauditable: int = 0
    store_failures: int = 0


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
    """One trust axis of a legacy record, as a real `bool`.

    Absent or `null` reads as `False`, which is the pre-field default and the
    oracle's reading: a row from before `diff_truncated` existed is not
    truncated, and a row with no `parse_ok` never claimed to have parsed. Any
    other non-`bool` reads at `_UNSAFE_AXIS[name]`.

    Applied to an index row on its own, and then again to the MERGED
    row-plus-artifact record -- an artifact's own axis has to be read through
    the same coercion, or a `degraded: 1` written into the artifact would read
    as clean.
    """
    value = row.get(name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return _UNSAFE_AXIS[name]


def _recorded_denies_trust(row: dict) -> bool:
    """Whether the record's own `trustworthy` field refuses trust.

    Asked of the index row, and then again of the merged row-plus-artifact
    record: an artifact spelling `trustworthy: false` has recorded a verdict
    about itself just as authoritatively as an index row does, and the summary
    it happens to sit beside cannot talk it out of that.

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
      * a `diff_hash` disagreement, an artifact that omits `diff_hash`
        included -- the gate selects on `diff_hash`, so this is the exact
        field whose corruption would certify the wrong content;
      * an artifact reporting FEWER findings than its index row, including a
        row that fails to state a count at all: unverifiable is not the same as
        verified. An artifact reporting MORE is accepted -- see below.
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
    # A SENTINEL LOOKUP, never a subscript. `load_valid_artifact` validates
    # `id`, `branch`, `base_sha`, `findings` and `findings_total`; `diff_hash`
    # is deliberately NOT in its schema (the gate re-asserts it separately,
    # against the hash it has just computed), so an artifact that simply omits
    # the key reaches here. Subscripting it raised `KeyError` straight out of
    # `import_legacy` -- a function whose contract is that it never raises and
    # that nothing aborts on bad input -- so ONE malformed file destroyed the
    # whole migration: every later index row was lost, and `_import_ledger`
    # runs after `_import_index`, so not a single dismissal was imported
    # either. Ledger continuity is half this module's purpose.
    #
    # The sentinel rather than a plain `.get()` keeps the demote-on-mismatch
    # semantics exact: `None` is a value a row can legitimately carry, and an
    # artifact that omits the field entirely must not be able to "agree" with
    # it. A missing `diff_hash` is therefore a disagreement like any other --
    # the row imports demoted, costing one re-review, which is precisely what
    # the rest of this function already does with an artifact whose identity
    # cannot be confirmed.
    if art.get("diff_hash", _MISSING) != row.get("diff_hash"):
        return None
    total = row.get("findings_total")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    # ASYMMETRIC ON PURPOSE, and the asymmetry is the whole check.
    #
    # This comparison exists to stop an artifact that *under*-reports findings
    # relative to its index row: hidden open findings are the only thing here
    # that could produce a false gate PASS, so an artifact claiming fewer than
    # the summary recorded is rejected and the row imports demoted.
    #
    # An artifact carrying MORE findings than a stale index summary cannot do
    # that. The ARTIFACT is what gets imported and what the gate then reads, so
    # its extra findings can only make the gate STRICTER -- exit 1 with those
    # findings open, never exit 0. And it is not unverified: the artifact has
    # already been proved self-consistent by `load_valid_artifact` above
    # (`findings_total == len(findings)`) before this comparison runs. The
    # disagreement is a property of the summary, not of the artifact -- the
    # legacy writer appends the index row when the first pass finishes, before
    # later passes merge their findings in, so this is the single commonest
    # shape of index/artifact disagreement in a real archive. Demoting on it
    # would discard already-reviewed findings to guard against a direction of
    # error that cannot loosen the gate.
    if art["findings_total"] < total:
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
        reconciled = False
        if _recorded_denies_trust(row):
            # The archive already judged this review untrustworthy. Its axes
            # may still derive True (that is exactly the case the recorded
            # field was added to override), so the demotion has to be made
            # explicit or `save_review` would recompute the trust back.
            row = _demote(row, axes, RECORDED_DENIAL_REASON)
            stats["demoted_untrustworthy"] += 1
        elif is_trustworthy(**axes):
            artifact = _load_artifact(archive, row, review_id)
            if artifact is None:
                row = _demote(row, axes, DEMOTED_REASON, force_reason=True)
                stats["demoted_no_artifact"] += 1
            else:
                # `_load_artifact` only returns on `artifact >= row`, and it
                # rejects a row whose count is not a plain int, so any surviving
                # inequality is the accepted direction: the artifact out-reports
                # a stale summary. Counted, not demoted -- but only if the row
                # survives the trust re-read below as trusted, since a demoted
                # row's count is not the one anything will act on.
                reconciled = artifact["findings_total"] != row.get("findings_total")

        # The artifact wins every field it defines -- it is the record the
        # review actually produced -- and the index row fills in anything it
        # omits. `source` is set last and explicitly: `save_review` defaults
        # the COLUMN to 'skodun' and dumps the record verbatim into
        # `artifact_json`, so a record that does not carry `source="legacy"`
        # itself would leave the column and the artifact disagreeing.
        rec = {**row, **(artifact or {})}

        # TRUST IS A PROPERTY OF THE MERGED RECORD, NOT OF THE SUMMARY.
        #
        # `axes` above was derived from the index row alone. Writing it onto
        # the merged record unconditionally would let the summary OVERRIDE the
        # artifact on exactly the four fields that decide whether the gate may
        # pass: an artifact recording `degraded: true`, `parse_ok: false`,
        # `diff_truncated: true` or `trustworthy: false` would import as
        # trustworthy whenever its row happened to read clean -- and rows in a
        # real archive routinely omit `diff_truncated` and `trustworthy`
        # entirely, so "reads clean" is the common case, not a contrived one.
        # That both contradicts the merge rule stated just above (the artifact
        # wins every field it defines) and inverts the rule below it (a
        # recorded denial cannot be talked out of).
        #
        # So the axes and the recorded verdict are re-read from `rec`. Doing it
        # on the merged record rather than by comparing row against artifact is
        # the simpler rule to hold: there is one record, it is the one being
        # stored, and its trust is read off it -- no notion of "disagreement"
        # to define, and no second precedence order to keep straight. It also
        # cannot LOOSEN anything, since this branch is only reachable when the
        # row's own axes already derived trustworthy; the re-read can only
        # discover a denial the row did not carry.
        if artifact is not None:
            axes = {k: _axis(rec, k) for k in _UNSAFE_AXIS}
            if _recorded_denies_trust(rec) or not is_trustworthy(**axes):
                rec = _demote(rec, axes, ARTIFACT_DENIAL_REASON)
                stats["demoted_untrustworthy"] += 1
                reconciled = False

        rec.update(axes)
        rec["id"] = review_id
        rec["source"] = "legacy"
        try:
            store.save_review(rec)
        except _RECORD_UNUSABLE:
            stats["skipped_lines"] += 1
            continue
        except _STORE_BROKEN:
            # The record was fine; the store could not take it. Counting this
            # as a skipped LINE would let a full disk mid-import look like a
            # handful of corrupt lines and still exit 0.
            stats["store_failures"] += 1
            continue
        except Exception:
            stats["skipped_lines"] += 1
            continue
        stats["reviews"] += 1
        if reconciled:
            stats["findings_reconciled"] += 1


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

        # THE AUDIT FLOOR APPLIES TO IMPORTED DISMISSALS TOO.
        #
        # `store.add_triage` only requires `dismissed_reason` to be PRESENT, so
        # without this call the `MIN_REASON_CHARS` and `PLACEHOLDER_REASONS`
        # rules that `validate_reason` enforces on every dismissal skodun
        # records itself would simply not exist on the import path. A legacy
        # row dismissing a finding as "fp" would import clean and move the gate
        # from 1 to 0 -- a rubber stamp silently clearing a finding, which is
        # the precise failure the ledger's audit floor was built to prevent.
        # Importing history is not a reason to honour a dismissal nobody could
        # audit; the cost of dropping one is that the finding is presented
        # again for a real judgement.
        #
        # `validate_reason` is the same function the interactive path calls, so
        # the floor cannot drift between the two. It is given the raw value:
        # `collapse_ws` stringifies, so a non-string reason fails the floor
        # rather than raising.
        try:
            validate_reason(rec.get("dismissed_reason"))
        except TriageError:
            stats["triage_unauditable"] += 1
            continue
        except Exception:
            # Nothing else should come out of it, and if something ever does,
            # an unvalidatable reason is still an unauditable dismissal.
            stats["triage_unauditable"] += 1
            continue

        try:
            store.add_triage({**rec, "finding_key": fkey, "branch": branch,
                              "base_sha": base_sha,
                              "ledger_key": ledger_key(branch, base_sha, fkey)})
        except _RECORD_UNUSABLE:
            stats["skipped_lines"] += 1
            continue
        except _STORE_BROKEN:
            stats["store_failures"] += 1
            continue
        except Exception:
            # `add_triage` raises KeyError when neither `review_id` nor `id` is
            # present. An orphaned dismissal is not one worth honouring.
            stats["skipped_lines"] += 1
            continue
        stats["triage"] += 1


def import_legacy(store: Store, grok_reviews_dir: Path) -> ImportStats:
    """Import `grok_reviews_dir` into `store`. Idempotent, and never raises.

    Idempotent in the sense that matters: re-importing the same archive
    produces the same stats and the same store as the GATE reads it. The index
    write upserts on `reviews.id`. Dismissals are appended to the store's
    triage event stream (v3), which is append-only by design, so a second
    import appends a second, identical `dismiss` event rather than upserting a
    row -- the finding stays dismissed for the same reason, and no decision a
    human made in between is ever overwritten. A demotion is therefore not a
    tombstone either: if the missing artifact is later restored, a re-import
    upgrades the row.

    A missing archive directory returns zeros. Migration tooling runs on
    machines that never used the legacy tool, and "nothing to import" is an
    ordinary outcome there, not an error.

    "Never raises" is not "always succeeded": a nonzero `store_failures` means
    records were lost to the store, and the caller is expected to report that
    as a failure even though nothing was thrown.
    """
    archive = Path(grok_reviews_dir)
    stats = dict(reviews=0, triage=0, skipped_lines=0, demoted_no_artifact=0,
                 findings_reconciled=0, demoted_untrustworthy=0,
                 triage_unauditable=0, store_failures=0)
    _import_index(store, archive, stats)
    _import_ledger(store, archive, stats)
    return ImportStats(**stats)
