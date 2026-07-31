"""Delivery: which background rounds a reader has been shown, and how they read.

A background review lands after `git push` has already returned. Nobody is
watching at that moment, so the round becomes a store record the developer never
saw -- and the failure that produced this module is not a missing feature, it is
a WRONG READING: a round that timed out records `findings_total: 0`, and a
surface that reported it as "0 findings" turned a review that never happened
into a clean bill of health.

So the two halves of this module are:

* **the presentations**, where "this round said nothing" is stated EXPLICITLY
  (:data:`NO_REVIEW_LINE`, verbatim) and is judged by an EXPLICIT PERSISTED
  SIGNAL -- `usable_output`, produced and validated upstream. A finding count is
  never the signal, in either direction. An untrustworthy round that DID answer
  renders its partial evidence under :data:`INCOMPLETE_WARNING` instead, because
  a no-review banner over three real batch reviews contradicts the artifact it
  claims to summarise.
* **the ledger**, `deliveries`, which records that a round reached a reader and
  on which channel. It is store STATE, not a marker file, and the ordering rule
  is the oracle's inverted to fail toward repetition: a round with content is
  acknowledged only AFTER the emit succeeds, so a crash between emit and ack
  re-delivers. Delivered-twice is the designed failure mode; delivered-never is
  unreachable short of losing the store.

This module is a pure CONSUMER of the review record. It computes no trust, mints
no ids, and writes nothing to `reviews`. The only SQL it owns is the two
statements below, over `reviews` (read) and `deliveries` (read/insert).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from .store import PREPUSH_MODE, SKODUN_SOURCE, Store, _iso_now, _require_ts
from .triage import shown_field
from .trust import coerce_count, one_line

#: Research decision 15's line, VERBATIM. A constant, never a format string:
#: its whole job is to be recognisable, by a human and by a grep, as the
#: statement that this round produced no answer at all. It is reserved for
#: exactly that case -- see `round_lines`.
NO_REVIEW_LINE = ("NO REVIEW HAPPENED — this round reports nothing because it "
                  "said nothing, not because it found nothing")

#: The header for a round that ANSWERED but cannot be trusted: a degraded round,
#: or a batched aggregate whose batches answered under a failed integration pass.
#: It must say "incomplete" and "cannot certify" in the same breath as the
#: evidence, because the evidence is real and the verdict is not.
INCOMPLETE_WARNING = (
    "INCOMPLETE REVIEW -- this round did not finish and cannot certify "
    "anything; what follows is partial evidence, not a clean bill of health")

#: The statuses a round may be delivered in. TERMINAL only: a `running` row is a
#: review still in flight, and acknowledging one would spend its single delivery
#: on a story that is not over. `running` is therefore not merely unrendered --
#: it is not eligible, so it cannot be acknowledged by any path.
TERMINAL_STATUSES = ("clean", "degraded", "failed", "superseded")

#: The delivery channel vocabulary, enforced by `acknowledge`. Closed on purpose:
#: `deliveries.channel` has no CHECK constraint, and the ledger is the only
#: record of HOW a round reached a reader -- a typo'd channel would quietly widen
#: the vocabulary and make that record unreadable.
CHANNELS = frozenset({"cli-text", "cli-claude", "mcp", "quiet"})

#: The two `--hook-format` values and the channel each one acknowledges under.
#: `text` is plain lines for a shell profile or any harness; `claude` is the
#: SessionStart JSON envelope.
TEXT = "text"
CLAUDE = "claude"
FORMATS = (TEXT, CLAUDE)
_CHANNEL_FOR_FORMAT = {TEXT: "cli-text", CLAUDE: "cli-claude"}

#: The channel a round with nothing to say is acknowledged under, immediately.
QUIET_CHANNEL = "quiet"

#: How many findings one round lists before it says "and N more". A round with
#: forty findings must not bury the other rounds beneath itself; the id is on the
#: line above, and `skodun triage --list <id>` shows the rest.
MAX_FINDINGS_SHOWN = 10

#: The `hookSpecificOutput.hookEventName` value the SessionStart envelope carries.
HOOK_EVENT = "SessionStart"


class SurfaceResult(NamedTuple):
    """What one surface pass produced. A `NamedTuple` deliberately: Task 14's
    `svc_surface` returns `(status, text, pending_acks)`, so this unpacks as that
    triple AND reads by name.

    * `status` -- 0. This layer decides nothing that can fail: a store it cannot
      read RAISES (the transport reports that, with its own diagnostics), and the
      only failures with an exit code of their own are the TRANSPORT's -- the
      emit that did not land, the ack that did not persist. Keeping the field
      means the CLI and the MCP tool answer the same shape.
    * `text` -- the whole payload, ending in a newline, or `""` when there is
      nothing to say. Empty is not an empty envelope: a hook that prints an empty
      report at every session start is noise, and a JSON envelope with an empty
      `additionalContext` is worse -- it injects a heading about nothing.
    * `pending_acks` -- the CONTENT-BEARING round ids, in render order, which the
      transport acknowledges with its own channel AFTER its write succeeds. Quiet
      rounds are already acknowledged and are never in here.
    """

    status: int
    text: str
    pending_acks: list[str]


# --- reading the ledger -----------------------------------------------------


def _conn(store: Store) -> sqlite3.Connection:
    """The store's connection.

    Reaching for the private attribute is deliberate and bounded. The v3 schema
    is installed COMPLETELY in Task 3 and `store.py` may only be CONSUMED from
    here on, so the delivery ledger's two statements live with the delivery
    semantics they implement rather than as two more methods on a class that
    knows nothing about presentation. Nothing here writes to `reviews`.
    """
    return store._c


#: The eligibility query, up to the delivery clause. A LEFT JOIN rather than a
#: `NOT IN (SELECT ...)`, because the replay below needs the ledger's own columns
#: to say when a round was delivered and on which channel.
_ROUNDS_SELECT = """
SELECT r.id AS row_id, r.status AS row_status, r.reviewed_at, r.branch, r.head,
       r.base_ref, r.base_sha, r.diff_hash, r.context_hash, r.mode, r.model,
       r.adapter, r.parse_ok, r.degraded, r.diff_truncated, r.trustworthy,
       r.stop_reason, r.findings_total, r.sev_high, r.sev_medium, r.sev_low,
       r.summary, r.source, r.superseded_by, r.artifact_json,
       d.delivered_at AS delivered_at, d.channel AS delivery_channel
FROM reviews r
LEFT JOIN deliveries d ON d.review_id = r.id
WHERE r.repo = ? AND r.branch = ? AND r.mode = ? AND r.source = ?
  AND r.status IN (%s)""" % ",".join("?" * len(TERMINAL_STATUSES))

#: `reviewed_at` then `id`: the store's timestamps have one-second resolution, so
#: two rounds from one push storm land in the same second routinely and the id is
#: what keeps the order from being arbitrary.
_ROUNDS_ORDER = "\nORDER BY r.reviewed_at, r.id"

#: The eligible-and-not-yet-delivered query and its replay sibling, built from ONE
#: select so `--include-delivered` cannot drift into a different notion of which
#: rounds exist.
_UNDELIVERED_SQL = _ROUNDS_SELECT + "\n  AND d.review_id IS NULL" + _ROUNDS_ORDER
_ALL_ROUNDS_SQL = _ROUNDS_SELECT + _ROUNDS_ORDER


def _index_record(row: sqlite3.Row) -> dict:
    """A record built from the INDEXED columns alone.

    The base every returned record is layered on, and the whole record when the
    artifact cannot be read. A corrupt artifact must cost that round its detail,
    never its delivery: skipping the row would leave it undelivered forever,
    which is the exact failure this module exists to remove.

    The three trust axes and `trustworthy` are stored as INTEGERs, so they are
    converted to real `bool`s here -- every reader downstream is strict about
    them, and an `int` would read as "not a bool, derive instead".
    """
    return dict(
        id=row["row_id"], status=row["row_status"], reviewed_at=row["reviewed_at"],
        branch=row["branch"], head=row["head"], base_ref=row["base_ref"],
        base_sha=row["base_sha"], diff_hash=row["diff_hash"],
        context_hash=row["context_hash"], mode=row["mode"], model=row["model"],
        adapter=row["adapter"], parse_ok=bool(row["parse_ok"]),
        degraded=bool(row["degraded"]), diff_truncated=bool(row["diff_truncated"]),
        trustworthy=bool(row["trustworthy"]), stop_reason=row["stop_reason"],
        findings_total=row["findings_total"], findings=[],
        severity={"high": row["sev_high"], "medium": row["sev_medium"],
                  "low": row["sev_low"]},
        summary=row["summary"], source=row["source"],
        superseded_by=row["superseded_by"],
    )


def _record(row: sqlite3.Row) -> dict:
    """One round, as the renderer reads it.

    The ARTIFACT is the record everywhere else in skodun, so it wins -- except
    for `id` and `status`, which are forced back to the INDEXED values because
    those two decided eligibility and the presentation branches on `status`. An
    index row that disagreed with its artifact would otherwise be rendered under
    one status and queried under another.

    A round already in the ledger carries a synthetic `delivery` annotation
    (`--include-delivered` prints it). It is NOT part of the persisted record and
    nothing writes it back.
    """
    rec = _index_record(row)
    raw = row["artifact_json"]
    artifact = None
    if isinstance(raw, str) and raw:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, Mapping):
            artifact = loaded
    if artifact is None:
        rec["artifact_unreadable"] = True
    else:
        rec.update(artifact)
        rec["id"] = row["row_id"]
        rec["status"] = row["row_status"]
    if row["delivered_at"] is not None or row["delivery_channel"] is not None:
        rec["delivery"] = {"delivered_at": row["delivered_at"],
                           "channel": row["delivery_channel"]}
    return rec


def _query(store: Store, branch: str, repo: str,
           include_delivered: bool) -> list[dict]:
    sql = _ALL_ROUNDS_SQL if include_delivered else _UNDELIVERED_SQL
    rows = _conn(store).execute(
        sql, (repo, branch, PREPUSH_MODE, SKODUN_SOURCE,
              *TERMINAL_STATUSES)).fetchall()
    return [_record(r) for r in rows]


def undelivered(store: Store, branch: str, repo: str) -> list[dict]:
    """The rounds on `branch` in `repo` a reader has never been shown, oldest
    first.

    Eligibility, and every clause of it is load-bearing:

    * `repo=<the git common dir>` -- two repositories sharing one store collide
      on any common branch name, and a `surface` that reached across them
      delivered AND permanently acknowledged the other's rounds. `repo IS NULL`
      (every pre-v5 row) matches nothing, deliberately.
    * `mode="prepush"` -- a foreground round was watched by the human who ran it.
    * `source="skodun"` -- a legacy-imported archive holds thousands of
      `mode=prepush` rows, and surfacing them would bury the first post-upgrade
      session under rounds from months ago. The ledger cannot un-flood it.
    * a TERMINAL status -- see `TERMINAL_STATUSES`.
    * no `deliveries` row.

    Ordered oldest-first (see `_ROUNDS_ORDER`), because that is the order the
    rounds happened in and a reader is being told a history.
    """
    return _query(store, branch, repo, include_delivered=False)


# --- the signal -------------------------------------------------------------


def has_usable_output(rec: Mapping) -> bool:
    """Whether ANY pass in this round produced a parseable answer.

    The EXPLICIT PERSISTED SIGNAL first: `usable_output` is produced by the
    review pipeline and validated as an exact `bool` at the persistence
    chokepoint for every `source="skodun"` `mode="prepush"` record, which is
    every record `undelivered` returns. Read it and believe it.

    Everything else falls back to the documented derivation `parse_ok or
    findings_total > 0`, for a record written before the field existed -- the v3
    migration adds columns and does not rewrite artifacts, so an upgraded store
    can still hold one -- or for a record whose artifact could not be read (the
    indexed columns carry both halves of the derivation).

    The `isinstance` check excludes `int`: `usable_output: 1` is not the bool the
    store validates, so it is not taken at its word either.

    A finding COUNT is never the signal, in either direction. A round whose
    passes all answered "nothing wrong" and a round that failed before any answer
    both have zero findings, and the difference between them is a clean review
    versus NO REVIEW HAPPENED.
    """
    value = rec.get("usable_output")
    if isinstance(value, bool):
        return value
    return bool(rec.get("parse_ok")) or coerce_count(rec.get("findings_total")) > 0


def _trustworthy(rec: Mapping) -> bool:
    """Strictly `trustworthy is True`.

    Deliberately NOT `shadow.effective_trustworthy`, whose absence-tolerant
    fallback exists for LEGACY archive rows -- the rows this module's `source`
    filter excludes by construction. Every record here was written by this
    build's store, which RECOMPUTES `trustworthy` from the axes on every write,
    so the field is always present and always agrees with them. Strictness
    therefore costs nothing and fails in the safe direction: a record that
    somehow lacks the field renders under a warning rather than as a clean round.
    """
    return rec.get("trustworthy") is True


# --- the presentations ------------------------------------------------------


def classify(rec: Mapping) -> str:
    """Which presentation this round gets. ONE decision, in one place.

    The order is the contract:

    1. `superseded` -- a newer push retired this round before it finished, and
       that newer round is the one a reader must read. It is reported in one
       line naming the replacement, and it deliberately does NOT get the reserved
       line: nothing is being hidden, the story simply continues in another
       record (and a force-push storm would otherwise print NO REVIEW HAPPENED
       once per superseded round).
    2. no usable output AND nothing to show -- the reserved line. This is the case
       the whole module exists for.
    3. usable output but not trustworthy -- the incomplete warning, with whatever
       findings exist. Partial evidence is surfaced, never hidden.
    4. trustworthy with findings -- rendered normally.
    5. trustworthy with nothing -- QUIET: no output, acknowledged immediately.

    2 sits above 5 on purpose: a round that is somehow marked trustworthy without
    having produced an answer is reported, never silently acknowledged as clean.

    The "AND nothing to show" half of 2 is a guard against a SELF-CONTRADICTING
    artifact, not a second signal. `usable_output=False` beside real findings is
    unreachable through any writer -- findings only come from a pass that
    answered -- so a record carrying both is one whose own fields disagree, and
    the reserved line over it would be a banner contradicting the record it
    summarises AND would drop the findings with it. The guard can only ever move a
    round TOWARD showing what it has: a zero-finding round can never be talked out
    of the reserved line, which is the false-clear direction. The COUNT is still
    never the signal for usability -- `has_usable_output` alone decides that.
    """
    if rec.get("status") == "superseded":
        return "superseded"
    if not has_usable_output(rec):
        return "incomplete" if _has_evidence(rec) else "no-output"
    if not _trustworthy(rec):
        return "incomplete"
    return "findings" if _has_evidence(rec) else "quiet"


def _has_evidence(rec: Mapping) -> bool:
    """Whether this round recorded anything a reader could act on.

    NOT a usability signal (see `has_usable_output` for that, and `classify` for
    why the distinction matters): purely "is there something here that a
    single-line summary would throw away".

    Deliberately NOT `coerce_count(...) > 0`, and this is the one place in the
    module where that rule is the wrong one. `coerce_count` renders an unusable
    value as `0` because a DISPLAY must not invent a number -- but here the answer
    decides whether a round is silently acknowledged as quiet, and a `0` derived
    from `findings_total: "3"` beside three real findings would acknowledge them
    unseen. So:

    * a non-empty `findings` LIST is evidence, whatever the count says. The stored
      count and the stored list are written from one record, but only the count is
      also an indexed column, so the list is the thing that cannot have been
      normalised out from under a reader;
    * a count that is present and not a plain `int` is evidence too -- "cannot
      tell" must never resolve to "nothing to see" (the oracle's own hard-won
      rule: an unguarded `int()` on a corrupt count once silenced its whole
      delivery pass);
    * absent or `None` is zero, matching every other reader.
    """
    findings = rec.get("findings")
    if isinstance(findings, list) and findings:
        return True
    total = rec.get("findings_total")
    if total is None:
        return False
    if isinstance(total, bool) or not isinstance(total, int):
        return True
    return total > 0


def _head_prefix(rec: Mapping) -> str:
    """`  - <when> <id> (head <9 chars>)`, plus the replay marker when there is
    one. Every interpolated value goes through `shown_field`: an id and a
    timestamp are ours, but a `head` label and a branch name are not necessarily,
    and this is a one-line-per-round listing."""
    prefix = (f"  - {shown_field(rec.get('reviewed_at'))} "
              f"{shown_field(rec.get('id'))} "
              f"(head {shown_field(rec.get('head'))[:9]})")
    seen = rec.get("delivery")
    if isinstance(seen, Mapping):
        prefix += (f" [delivered {shown_field(seen.get('delivered_at'))} via "
                   f"{shown_field(seen.get('channel'))}]")
    return prefix


def _reason_lines(rec: Mapping) -> list[str]:
    """The round's own words about why it did not finish, LABELLED.

    Both reason fields are printed when both are set, under their own names,
    because they answer different questions -- `degraded_reason` is why the round
    cannot be trusted, `failure_reason` is why it stopped -- and an operator
    reading a delivery needs the field name to know which one they have.
    """
    lines = []
    for name in ("degraded_reason", "failure_reason"):
        value = one_line(rec.get(name)).strip()
        if value:
            lines.append(f"      {name}: {shown_field(value)}")
    if not lines:
        lines.append("      reason: this round recorded no reason for not "
                     "finishing")
    return lines


def _findings_lines(rec: Mapping) -> list[str]:
    """The summary, up to `MAX_FINDINGS_SHOWN` findings, and the triage pointer.

    Every field on a finding line is untrusted model text reaching a terminal --
    and, in the `claude` envelope, an agent's context -- so all of it goes
    through `shown_field`, exactly as `triage --list` does. That is what stops a
    `title` carrying a newline from forging an extra round line.
    """
    lines = []
    summary = one_line(rec.get("summary")).strip()
    if summary:
        lines.append(f"      summary: {shown_field(summary)}")
    if rec.get("artifact_unreadable") is True:
        lines.append("      note: this round's stored artifact could not be "
                     "read, so only its indexed summary is shown")
    findings = rec.get("findings")
    findings = findings if isinstance(findings, list) else []
    for i, f in enumerate(findings[:MAX_FINDINGS_SHOWN]):
        f = f if isinstance(f, Mapping) else {}
        lines.append(f"      [{i}] {shown_field(f.get('severity'))} "
                     f"{shown_field(f.get('file'))}:{shown_field(f.get('line'))} "
                     f"{shown_field(f.get('title'))}")
    hidden = len(findings) - MAX_FINDINGS_SHOWN
    if hidden > 0:
        lines.append(f"      ... and {hidden} more finding(s) not shown")
    lines.append(f"      see: skodun triage --list {shown_field(rec.get('id'))}")
    return lines


def _severity_phrase(rec: Mapping) -> str:
    sev = rec.get("severity")
    sev = sev if isinstance(sev, Mapping) else {}
    return (f"{coerce_count(sev.get('high'))} high / "
            f"{coerce_count(sev.get('medium'))} medium / "
            f"{coerce_count(sev.get('low'))} low")


def round_lines(rec: Mapping) -> list[str]:
    """Every line this round contributes, or `[]` when it is quiet.

    An empty list is the ONLY thing that makes a round quiet, so
    "acknowledged immediately" and "rendered nothing" can never disagree.
    """
    kind = classify(rec)
    if kind == "quiet":
        return []
    prefix = _head_prefix(rec)
    if kind == "superseded":
        superseding = one_line(rec.get("superseded_by")).strip()
        # The PERSISTED field, written atomically by the reservation that retired
        # this row -- never inferred from the branch's other rounds or from
        # timestamps. With no id recorded, say so rather than name a guess.
        tail = (f"superseded by {shown_field(superseding)}" if superseding
                else "superseded by a newer push, which did not record its id")
        lines = [f"{prefix}: {tail}; read that round instead of this one"]
        if has_usable_output(rec) or _has_evidence(rec):
            # Unreachable through any writer -- the reservation supersedes only
            # `running` rows, and a `running` row has neither findings nor usable
            # output. Guarded anyway, and only in the direction that shows MORE:
            # the cost of being wrong about it is a real finding rendered as a
            # single line of bookkeeping and never seen again.
            lines += [f"      {INCOMPLETE_WARNING}", *_reason_lines(rec),
                      *_findings_lines(rec)]
        return lines
    if kind == "no-output":
        return [f"{prefix}: {NO_REVIEW_LINE}", *_reason_lines(rec)]
    if kind == "incomplete":
        return [f"{prefix}: {INCOMPLETE_WARNING}", *_reason_lines(rec),
                *_findings_lines(rec)]
    return [f"{prefix}: {_count_phrase(rec)} -- {_severity_phrase(rec)}",
            *_findings_lines(rec)]


def _count_phrase(rec: Mapping) -> str:
    """`N finding(s)`, or the honest answer when the stored count is unreadable.

    `coerce_count` would render `findings_total: "3"` as `0`, which is right for a
    banner whose format is a machine contract and wrong here: this line is the
    only thing a human reads before deciding whether to look, and "0 finding(s)"
    above a listed finding is the same false clear in miniature.
    """
    total = rec.get("findings_total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int)):
        return "an unreadable number of finding(s)"
    return f"{coerce_count(total)} finding(s)"


def _header(branch: str, count: int, include_delivered: bool) -> str:
    if include_delivered:
        return (f"skodun surface: {count} background review round(s) on branch "
                f"{shown_field(branch)}, previously delivered ones included")
    return (f"skodun surface: {count} undelivered background review round(s) on "
            f"branch {shown_field(branch)} -- each ran after `git push` returned, "
            f"so nobody has seen it")

#: The last line of every non-empty report. A delivery is history: it says what
#: earlier pushes produced, and says nothing whatever about the change in the
#: working tree right now. Without this line a clean-looking surface reads as a
#: green light, which is the same false-clear in a different costume.
FOOTER = ("skodun surface: nothing above certifies the current change -- run "
          "`skodun gate` for that")


def render_claude(body: str, branch: str, count: int) -> str:
    """The SessionStart envelope, one line, ASCII-only.

    `ensure_ascii=True` (the default, made explicit) because a hook's stdout is
    decoded by a process whose locale is not ours and the reserved line carries an
    em dash; escaping it to `\\u2014` keeps the envelope readable everywhere.
    ONE line, so a consumer reading line-by-line gets exactly one object.
    """
    return json.dumps(
        {"systemMessage": (f"skodun: {count} background review round(s) on "
                           f"{shown_field(branch)} that nobody has read"),
         "hookSpecificOutput": {"hookEventName": HOOK_EVENT,
                                "additionalContext": body}},
        ensure_ascii=True) + "\n"


# --- the ledger -------------------------------------------------------------


def channel_for_format(fmt: str) -> str:
    """`text` -> `cli-text`, `claude` -> `cli-claude`. Nothing else."""
    try:
        return _CHANNEL_FOR_FORMAT[fmt]
    except (KeyError, TypeError):
        raise ValueError(
            f"unknown surface format {fmt!r}; expected one of {list(FORMATS)}"
        ) from None


def acknowledge(store: Store, ids: Iterable[str], channel: str,
                *, now: str | None = None) -> int:
    """Record that these rounds reached a reader on `channel`. Returns the number
    of rounds that were not already in the ledger.

    IDEMPOTENT by `ON CONFLICT DO NOTHING`, not `DO UPDATE`: re-acknowledging is
    a no-op that keeps the FIRST delivery's timestamp and channel, because that
    is the fact the ledger records -- when a reader first saw this round. A
    re-delivery after a crash between emit and ack must not rewrite it.

    `channel` is validated against `CHANNELS` BEFORE anything is written, and an
    unusable id is refused the same way: this is the API Task 14's transports
    call, and a mistyped channel or a non-id would enter a ledger that has no
    constraint of its own to catch it.

    Each insert is its own statement in autocommit, so a store that stops
    accepting writes half-way leaves the remaining rounds undelivered -- they
    repeat, which is the designed direction.
    """
    if channel not in CHANNELS:
        raise ValueError(
            f"unknown delivery channel {channel!r}; expected one of "
            f"{sorted(CHANNELS)}")
    ids = list(ids)
    for review_id in ids:
        if not isinstance(review_id, str) or not review_id:
            raise ValueError(
                f"acknowledge: {review_id!r} is not a review id; refusing to "
                f"write it to the delivery ledger")
    if not ids:
        return 0
    at = _iso_now() if now is None else _require_ts("now", now)
    cur = _conn(store).executemany(
        "INSERT INTO deliveries (review_id, delivered_at, channel)"
        " VALUES (?,?,?) ON CONFLICT(review_id) DO NOTHING",
        [(review_id, at, channel) for review_id in ids])
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _acknowledge_quiet(store: Store, ids: list[str]) -> None:
    """Acknowledge rounds with nothing to say, immediately and BEST-EFFORT.

    Immediately, because they will never have anything to say and re-scanning
    them at every session start forever is pure waste -- nothing deliverable can
    be lost by marking them now.

    Best-effort, because a quiet ack is an optimisation while the report beside it
    is the product: a store that refuses this write must not also cost the
    content-bearing rounds their delivery.
    """
    if not ids:
        return
    try:
        acknowledge(store, ids, QUIET_CHANNEL)
    except BaseException:
        pass


# --- the transport-agnostic surface ----------------------------------------


def surface(store: Store, branch: str, repo: str, fmt: str = TEXT,
            include_delivered: bool = False) -> SurfaceResult:
    """Render one delivery pass. NO printing, no argparse, no exit codes.

    This is the whole surface, minus the transport: the CLI writes `text` to
    stdout and then acknowledges `pending_acks` with `cli-text`/`cli-claude`, and
    Task 14's MCP tool does the same with `mcp`. The split IS the ack discipline
    -- whoever performed the write is the only one who knows whether it landed.

    `fmt` is validated FIRST, before the store is touched: misuse must not
    acknowledge anything.

    `repo` is REQUIRED and has no default. Every row this pass renders it also
    ACKNOWLEDGES -- permanently, and a quiet round before the caller has written
    anything -- so a guessed repository would spend another repository's single
    delivery. See `undelivered` for the clause itself.
    """
    if fmt not in FORMATS:
        raise ValueError(
            f"unknown surface format {fmt!r}; expected one of {list(FORMATS)}")
    rounds = _query(store, branch, repo, include_delivered)
    quiet: list[str] = []
    pending: list[str] = []
    lines: list[str] = []
    for rec in rounds:
        rendered = round_lines(rec)
        review_id = rec.get("id")
        if not isinstance(review_id, str) or not review_id:
            # Unreachable through the store (`reviews.id` is the primary key);
            # a row that somehow has no id is rendered and never acknowledged,
            # rather than crashing the pass for every other round.
            lines.extend(rendered)
            continue
        if not rendered:
            quiet.append(review_id)
            continue
        pending.append(review_id)
        lines.extend(rendered)
    _acknowledge_quiet(store, quiet)
    if not lines:
        return SurfaceResult(0, "", [])
    body = "\n".join(
        [_header(branch, len(pending), include_delivered), *lines, FOOTER]) + "\n"
    text = body if fmt == TEXT else render_claude(body, branch, len(pending))
    return SurfaceResult(0, text, pending)
