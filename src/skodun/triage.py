"""Human triage of review findings, with an audited reason -- dismissal,
deferral, and the un-dismissal that overturns either -- plus the fail-closed
artifact validation that keeps a corrupt or hand-edited review from ever
satisfying the gate.

Every decision is APPENDED to the store's triage event stream: `dismiss`,
`defer` and `reopen` (see below and `store.Store.triage_state`). Nothing is
edited or deleted, so an overturned dismissal keeps its reason and the whole
history of a finding reads back in order. All three verbs clear the same audit
floor on their reason, from the same `validate_reason`.

`defer` is skodun's own and is the newest of the three. `dismiss` says *not a
defect*; `defer` says *a defect, not blast-radius for this change, filed as X*,
and it clears the gate just as a dismissal does -- which is the escape from a
review loop that need not terminate, since every round of fixes is new code the
reviewer will review. What keeps it from being an auto-dismissal with better
manners is `validate_tracking_ref`: the filed reference is MANDATORY and is
refused on exactly the terms a placeholder reason is, because an unfiled
deferral and an ignored finding are the same artifact.

PARITY-CRITICAL: ported from the oracle's `grok_review_triage.py`. Where this
module's *review semantics* and the oracle's disagree, the oracle wins — the
placeholder set, the reason floor, and the accept/reject verdict of
`validate_reason` are pinned to it by oracle-loaded tests.

`load_valid_artifact` is the one deliberate, documented exception: it is
strictly *stronger* than the oracle's `load_review`. See the comment block
above that function.

`adopt_refuter` and the two display helpers around it are skodun's own; the
oracle had no refuter pass. They are the ONLY path by which a refuter verdict
can ever dismiss a finding, and that path is explicit and per-finding on
purpose: a `refuted` annotation changes no count, no severity and no trust
axis, and a review whose only finding is annotated `refuted` still gates 1
until a human adopts it by name. There is deliberately no "adopt all", and
nothing anywhere adopts by itself.
"""

from __future__ import annotations

import re

from .store import Store
from .textnorm import collapse_ws, finding_key, ledger_key, norm
from .trust import one_line


class TriageError(ValueError):
    """A dismissal reason or finding index failed validation."""


class FindingNotFound(TriageError):
    """The named finding index does not resolve to a finding on this review.

    A `TriageError` so that every existing caller keeps catching every index
    failure exactly as before, and a distinct type so the CLI can tell the two
    halves of the adoption contract apart: `2` is "the thing you named does
    not exist", `1` is "it exists and the dismissal was refused".
    """


class ArtifactError(ValueError):
    """A review artifact is self-inconsistent and must not be trusted."""


# PARITY-CRITICAL: copied verbatim from the oracle's `PLACEHOLDER_REASONS`
# (grok_review_triage.py:58-63) — the parity test in tests/test_triage.py
# asserts exact set equality against the real oracle module.
PLACEHOLDER_REASONS = {
    "false positive", "fp", "not a bug", "wontfix", "won't fix", "no", "nope",
    "n/a", "na", "none", "ignore", "ignored", "skip", "skipped", "ok", "fine",
    "invalid", "wrong", "incorrect", "disagree", "not an issue", "no issue",
    "already fixed", "by design", "intentional", "known", "irrelevant",
}
MIN_REASON_CHARS = 20

#: How long a tracking reference may be. It is an IDENTIFIER to look a deferral
#: up by -- an issue number, a tracker key, a URL -- not the explanation, which
#: is what `reason` is for. Generous enough for a long self-hosted URL and short
#: enough that the reference cannot become a second, unvalidated reason. It also
#: bounds what `skodun deferrals` prints per row.
MAX_TRACKING_REF_CHARS = 200

#: The key a refuter annotation lives under on a finding, and the name of the
#: pass in `extra_passes`. Spelled here rather than imported from `passes`
#: because that module already imports THIS one (`MIN_REASON_CHARS`), and a
#: cycle is worse than a duplicated four-letter string.
#: `test_the_annotation_key_matches_the_pass_that_writes_it` pins the two
#: spellings together: drift would make every annotation invisible to adoption
#: while every hand-built fixture kept passing.
REFUTER_KEY = "refuter"

#: The one refuter verdict a human may adopt as a dismissal. `confirmed` and
#: `uncertain` are refused by name — adopting either would mean recording a
#: dismissal whose own stated grounds do not dismiss anything.
ADOPTABLE_VERDICT = "refuted"

#: How much of one annotation field `triage --list` prints. The reasoning is
#: arbitrary model text and the listing is one line per item, so it is
#: newline-flattened and then truncated; `provider`, `model` and `verdict` go
#: through the same rule, because a 10,000-character provider name drowns the
#: listing just as thoroughly as a 10,000-character reasoning would. This is a
#: DISPLAY rule only: the artifact and any adopted ledger reason keep the
#: original, untruncated string.
MAX_ANNOTATION_DISPLAY_CHARS = 120


def validate_reason(reason: str) -> None:
    """Raise TriageError unless `reason` clears the audit floor.

    A dismissal that says nothing is the failure mode this ledger exists to
    prevent. PARITY-CRITICAL, in the oracle's order (grok_review_triage.py:
    93-107) — empty, then placeholder, then length:

    * an empty (or whitespace-only) reason is rejected first;
    * a reason whose fully normalized form is one of PLACEHOLDER_REASONS is
      rejected as a *placeholder*, with the oracle's actionable message.
      This check must precede the length check: every placeholder is shorter
      than MIN_REASON_CHARS, so checking length first would make this branch
      unreachable and replace the actionable message with "too short";
    * the length floor is measured on the whitespace-collapsed but NOT
      lowercased form, exactly as the oracle measures it. See
      `textnorm.collapse_ws`.
    """
    cleaned = collapse_ws(reason)
    if not cleaned:
        raise TriageError("a dismissal reason is required (it was empty)")
    if norm(cleaned) in PLACEHOLDER_REASONS:
        raise TriageError(
            f"{cleaned!r} is a placeholder, not a reason. Say WHY the finding "
            "is wrong -- what the reviewer missed, or where the guard actually "
            "lives.")
    if len(cleaned) < MIN_REASON_CHARS:
        raise TriageError(
            f"reason is {len(cleaned)} chars; at least {MIN_REASON_CHARS} are "
            "required. A dismissal nobody can audit later is indistinguishable "
            "from ignoring the finding.")


def validate_tracking_ref(ref) -> str:
    """Return the canonical reference, or raise TriageError. THE one floor.

    The deferral floor, standing beside `validate_reason` rather than inside it:
    a `defer` clears the gate, and the only thing separating "we filed this and
    will do it" from "we stopped looking at it" is a reference somebody can
    actually open. *An unfiled deferral and an ignored finding are the same
    artifact* -- so a missing reference is refused exactly as a placeholder
    reason is, and skodun's own name for that rule is this function.

    Validation is DELIBERATELY MINIMAL, and the restraint is the design. There
    is no pattern to conform to: an issue number (`#412`), a bare number, a
    tracker key (`SKO-7`), a repo-qualified issue (`owner/repo#5`) and a URL are
    all references, and a scheme tight enough to "validate" them would refuse
    the tracker this project has never heard of -- pushing its users back to
    burying the reference in prose, which is the failure being fixed. What is
    checked is only what makes a value unusable AS a reference:

    * empty or whitespace-only -- there is nothing filed;
    * longer than `MAX_TRACKING_REF_CHARS` -- that is a reason, not an
      identifier, and the reason field is right there;
    * containing whitespace or a control character -- a reference is ONE token
      nobody has to interpret. `I will file it later` is a promise, not a
      filing; and this value is printed on a one-line-per-item listing, so a
      newline could forge a row and an ESC could rewrite what the terminal
      already showed. (`shown_field` bounds it again at display time; refusing
      it at the door means the ledger never stores one in the first place.)
    * carrying no letter or digit -- `---` and `#` name nothing that can be
      looked up.

    Returns the STRIPPED value, which is what gets stored and printed: the
    surrounding whitespace of a pasted reference is a copy-paste artefact, not
    part of the identifier.
    """
    if not isinstance(ref, str):
        raise TriageError(
            f"a tracking reference must be text ({ref!r}); a deferral has to "
            "name where the work is filed")
    cleaned = ref.strip()
    if not cleaned:
        raise TriageError(
            "a tracking reference is required (it was empty). A deferral says "
            "the finding is real and the work is filed somewhere -- name the "
            "issue, ticket or URL. An unfiled deferral and an ignored finding "
            "are the same artifact.")
    if len(cleaned) > MAX_TRACKING_REF_CHARS:
        raise TriageError(
            f"tracking reference is {len(cleaned)} chars; at most "
            f"{MAX_TRACKING_REF_CHARS} are allowed. It is the identifier to "
            "look the deferral up by, not the explanation -- put that in the "
            "reason.")
    if any(c.isspace() for c in cleaned) or _CONTROL_CHARS.search(cleaned):
        raise TriageError(
            f"{cleaned!r} is prose, not a filed reference. A tracking "
            "reference is one token nobody has to interpret: an issue number "
            "(#412), a tracker key (SKO-7), or a URL.")
    if not any(c.isalnum() for c in cleaned):
        raise TriageError(
            f"{cleaned!r} names nothing that can be looked up; a tracking "
            "reference must carry at least one letter or digit.")
    return cleaned


# DELIBERATE, DOCUMENTED DIVERGENCE FROM THE ORACLE — read before "fixing"
# this back to match `grok_review_triage.py:176-230`.
#
# The oracle's `load_review` is LENIENT about absent keys: a missing or None
# `findings` is silently coerced to `[]`, the `findings_total` type and
# `findings_total != len(findings)` checks are skipped entirely when
# `findings_total` is missing or None (it validates the count only when the
# artifact actually asserts one), and `id` / `branch` / `base_sha` are never
# checked at all. That leniency is safe at the oracle's OWN call sites, which
# re-derive `review.get("findings") or []` for display.
#
# It is NOT safe here. In skodun this function is the fail-closed validator the
# GATE (Task 7) runs before a stored review is allowed to certify a push, and
# the check the LEGACY IMPORTER (Task 16) explicitly leans on: a legacy index
# row is a derived summary *without* `findings[]`, and storing it as
# trustworthy "would let it satisfy the gate, whose artifact validation
# (Task 6) then rejects it". Under the lenient rule an artifact carrying no
# `findings` key reads as "zero findings" — i.e. clean — and the gate can PASS
# on a review whose findings were never recorded. That inverts the project's
# central fail-closed posture.
#
# "The oracle wins" exists to keep keys and review semantics byte-compatible
# with the legacy archive; it does not require importing the oracle's weaker
# validation into skodun's gate path. Being STRICTER is safe in this direction:
# the worst case is that a malformed artifact forces a fresh review.
def load_valid_artifact(rec) -> dict:
    """Return `rec` if it is a self-consistent review artifact, else raise.

    Rejects, each with an `ArtifactError` naming the specific problem: a
    non-object artifact; a missing or non-string `id`, `branch`, or
    `base_sha`; a missing `findings`; a `findings` that is not a list; any
    non-dict member of `findings`; a missing `findings_total`; a
    `findings_total` that is not a plain int (bool/float/str all rejected —
    `isinstance(True, int)` is True in Python, so the bool check must be
    explicit and must come first); and `findings_total != len(findings)`.

    The identity trio is validated here rather than at the call site because
    `dismiss` indexes `review["id"]`, `review["branch"]`, and
    `review["base_sha"]` straight after this function "passes" — and the
    ledger key is built from `branch` + `base_sha`, so a non-string there
    would silently scope a dismissal to the wrong review loop. Validating
    only what a caller does not consume is a validator in name only.

    Stricter than the oracle on the missing-key and identity cases; see the
    comment block above for why that divergence is deliberate and fail-safe.
    """
    if not isinstance(rec, dict):
        raise ArtifactError("artifact is not an object")
    for field in ("id", "branch", "base_sha"):
        if field not in rec:
            raise ArtifactError(f"{field} is missing")
        if not isinstance(rec[field], str):
            raise ArtifactError(f"{field} is not a string ({rec[field]!r})")
    if "findings" not in rec:
        raise ArtifactError("findings is missing")
    findings = rec["findings"]
    if not isinstance(findings, list):
        raise ArtifactError(f"findings is not a list ({findings!r})")
    if any(not isinstance(f, dict) for f in findings):
        raise ArtifactError("findings contains a non-object entry")
    if "findings_total" not in rec:
        raise ArtifactError("findings_total is missing")
    total = rec["findings_total"]
    if isinstance(total, bool) or not isinstance(total, int):
        raise ArtifactError(f"findings_total is not an integer ({total!r})")
    if total != len(findings):
        raise ArtifactError(
            f"findings_total={total} != len(findings)={len(findings)} "
            "(truncated or hand-edited artifact)")
    return rec


def _finding_at(findings: list, index) -> dict:
    """`findings[index]`, or raise `FindingNotFound`.

    ONE definition, used by `dismiss` and by `adopt_refuter`, so the two paths
    cannot drift on what counts as a nameable finding.

    `isinstance(True, int)` is True in Python, so an unguarded bool indexes
    the list: `dismiss(..., True, ...)` would dismiss findings[1] — a
    different finding than the caller named. Any other non-int would leak a
    raw TypeError past this module's TriageError contract.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        raise FindingNotFound(f"finding index must be an int ({index!r})")
    if not (0 <= index < len(findings)):   # negative indexes must not
        raise FindingNotFound(f"finding index {index} out of range")  # alias
    return findings[index]


def dismiss(store: Store, review: dict, index: int, reason: str, now: str) -> dict:
    """Record an audited dismissal of one finding and return the ledger row.

    Appends a `dismiss` event (v3). A finding that was dismissed, reopened and
    then dismissed again goes through this same function every time -- there is
    no separate "re-dismiss" verb, and no previous decision is overwritten.
    """
    review = load_valid_artifact(review)
    validate_reason(reason)
    # load_valid_artifact guarantees `findings` is present and a list of dicts.
    f, fkey, lkey = _keys_for(review, index)
    rec = dict(ledger_key=lkey, finding_key=fkey, id=review["id"],
               branch=review["branch"], base_sha=review["base_sha"],
               file=f.get("file"), line=f.get("line"),
               severity=f.get("severity"), title=f.get("title"),
               dismissed_reason=reason, dismissed_at=now)
    store.add_triage(rec)
    return rec


def defer(store: Store, review: dict, index: int, tracking_ref: str, reason: str,
          now: str) -> dict:
    """Record an audited deferral of one finding and return the ledger row.

    Appends a `defer` event (v4): *this finding is real, it is not blast-radius
    for this change, and the work is filed as `tracking_ref`*. It CLEARS the
    gate exactly as a dismissal does -- see `store.Store.triage_for` -- which is
    what lets a review loop terminate on a non-trivial change without anybody
    pretending a real defect is a false positive.

    Two floors, and both are the shared ones rather than new copies: the reason
    clears `validate_reason` (the same function a dismissal and a reopen clear,
    placeholder set included -- "filed as GH-1" plus a reason of `wontfix` is a
    dismissal wearing a ticket number), and the reference clears
    `validate_tracking_ref`.

    THE ORDER OF THE CHECKS IS THE EXIT CONTRACT, and it matches `reopen`'s.
    `FindingNotFound` -- which the CLI reports as 2, "the thing you named does
    not exist" -- is raised before either floor is applied, because a reference
    cannot be refused on behalf of no finding. Only then can a `TriageError`
    (reported as 1, "it exists and the deferral was declined") arise.

    The REFERENCE is judged before the reason: it is what distinguishes this
    verb from `dismiss`, so an agent or a human who left it out should be told
    that, not lectured about prose they did write.

    There is no state precondition. Deferring an already-dismissed finding, or
    re-filing a deferral under a different reference, is another event on the
    stream exactly as a re-dismissal is -- nothing is edited, and the earlier
    reference and its reason stay readable in `triage_history`.
    """
    review = load_valid_artifact(review)
    f, fkey, lkey = _keys_for(review, index)
    tracking_ref = validate_tracking_ref(tracking_ref)
    validate_reason(reason)
    rec = dict(ledger_key=lkey, finding_key=fkey, id=review["id"],
               branch=review["branch"], base_sha=review["base_sha"],
               file=f.get("file"), line=f.get("line"), severity=f.get("severity"),
               title=f.get("title"), reason=reason, tracking_ref=tracking_ref,
               at=now)
    store.triage_defer(rec)
    return rec


def open_findings(review: dict, triaged: dict[str, dict]) -> list[dict]:
    """Findings from `review` with no matching entry in `triaged`."""
    out = []
    for f in load_valid_artifact(review)["findings"]:
        if finding_key(f.get("file", ""), f.get("title", "")) not in triaged:
            out.append(f)
    return out


def _keys_for(review: dict, index) -> tuple[dict, str, str]:
    """`(finding, finding_key, ledger_key)` for one named finding of `review`.

    ONE derivation, shared by `dismiss` and `reopen`, so the two cannot end up
    writing events under different keys for the same finding -- which would
    make a reopen silently fail to overturn anything.
    """
    f = _finding_at(review["findings"], index)
    fkey = finding_key(f.get("file", ""), f.get("title", ""))
    return f, fkey, ledger_key(review["branch"], review["base_sha"], fkey)


def _effective_event(store: Store, review: dict, fkey: str) -> str | None:
    """The last event recorded for one finding of `review`, or None.

    Read through the store's own effective-state definition rather than by
    re-deriving "the latest row" here: the gate and this module must never
    disagree about whether a finding is dismissed.
    """
    state = store.triage_state(review["branch"], review["base_sha"]).get(fkey)
    return None if state is None else state.get("event")


def reopen(store: Store, review: dict, index: int, reason: str, now: str) -> dict:
    """Overturn one finding's dismissal OR deferral, with an audited reason.

    A cleared finding is not cleared for all time: a fix regresses, a base
    moves, a reason turns out to have been wrong, a deferral turns out to have
    been the wrong call. Reopening is therefore a first-class decision recorded
    in the same stream, and it is subject to the SAME audit floor a dismissal
    clears -- `validate_reason`, the identical function, so the floors cannot
    drift. A reopen moves the gate from 0 to 1, which is exactly the kind of
    change nobody should be able to make without saying why.

    It overturns whichever CLEARING verb is in force (`Store.CLEARING_EVENTS`),
    not `dismiss` alone. That is not a convenience: `defer` moves the gate to 0
    just as `dismiss` does, so a deferral with no audited undo would be the one
    decision in this ledger that cannot be taken back on the record.

    Nothing is edited or deleted: the dismissal or deferral stays in the stream
    with its own reason and its filed reference, readable by
    `store.triage_history`, and the reopen is appended after it.

    THE ORDER OF THE CHECKS IS THE EXIT CONTRACT. `FindingNotFound` (which the
    CLI reports as 2, "the thing you named does not exist") is raised before
    the reason is judged and before the ledger is consulted, because a reason
    cannot be refused on behalf of no finding. Only then does a `TriageError`
    (reported as 1, "it exists and the reopen was declined") become possible:
    an unauditable reason, or a finding that is not cleared in the first
    place -- for which there is nothing to overturn, and an event claiming
    otherwise would be a reopen with nothing behind it.
    """
    review = load_valid_artifact(review)
    f, fkey, lkey = _keys_for(review, index)
    validate_reason(reason)
    state = _effective_event(store, review, fkey)
    if state not in Store.CLEARING_EVENTS:
        raise TriageError(
            f"finding {index} is not dismissed or deferred"
            + (" (it was already reopened)" if state == Store.EVENT_REOPEN else "")
            + "; there is nothing to reopen")
    rec = dict(ledger_key=lkey, finding_key=fkey, id=review["id"],
               branch=review["branch"], base_sha=review["base_sha"],
               file=f.get("file"), line=f.get("line"), severity=f.get("severity"),
               title=f.get("title"), reason=reason, at=now)
    store.triage_reopen(rec)
    return rec


def status_token(state) -> str:
    """The `(...)` status `triage --list` prints for one finding.

    `OPEN`, `DISMISSED <when>`, `REOPENED <when>, dismissed <when>` -- both
    timestamps whenever both decisions exist, because "reopened today" is only
    meaningful next to the dismissal it overturned -- or `DEFERRED -> <ref>
    <when>`.

    The deferral token carries its REFERENCE and not its counterpart timestamp,
    and that asymmetry is the point of the verb: what a reader of this listing
    needs from a deferred finding is where the work is filed, which is the only
    thing distinguishing outstanding debt from a rejected finding.

    `state` is one value from `store.triage_state`, or None for a finding with
    no events. Every field goes through `shown_field`: a seeded legacy
    `dismissed_at` is whatever the archive happened to contain, a `tracking_ref`
    could have been hand-edited into the store, and both print on the same
    terminal line as the finding's own status -- the exact exposure that rule
    exists for. An unknown event verb renders `OPEN`, the safe direction: a
    finding shown as open is one a human still has to look at.

    Rendering may never be the thing that crashes a listing, so nothing here
    raises for any input.
    """
    event = state.get("event") if isinstance(state, dict) else None
    if event == Store.EVENT_DEFER:
        # A deferral always has a reference (the store's door enforces it), so
        # the empty case is a hand-edited or seeded row -- rendered as a bare
        # DEFERRED rather than as `DEFERRED -> `, which would read as a filing
        # whose name merely failed to print.
        out = "DEFERRED"
        ref = shown_field(state.get("tracking_ref"))
        if ref:
            out += f" -> {ref}"
        when = shown_field(state.get("deferred_at"))
        return f"{out} {when}" if when else out
    if event == Store.EVENT_DISMISS:
        label, own, other, other_word = ("DISMISSED", "dismissed_at",
                                         "reopened_at", "reopened")
    elif event == Store.EVENT_REOPEN:
        label, own, other, other_word = ("REOPENED", "reopened_at",
                                         "dismissed_at", "dismissed")
    else:
        return "OPEN"
    out = label
    when = shown_field(state.get(own))
    if when:
        out += f" {when}"
    before = shown_field(state.get(other))
    if before:
        out += f", {other_word} {before}"
    return out


# ---------------------------------------------------------------------------
# Refuter annotations: reading them, showing them, and adopting one
# ---------------------------------------------------------------------------
#
# An annotation is model output that has been through `passes.merge_refuter_
# pass` and then through JSON and SQLite. Everything below therefore treats
# `verdict`, `reasoning`, `provider` and `model` as untrusted DATA: types are
# checked, and no field's *content* steers control flow beyond the explicit
# checks written here. The display helpers additionally may never raise —
# rendering a listing must not be the thing that crashes it.


def refuter_annotation(finding) -> dict | None:
    """The refuter annotation on `finding`, or None if there is nothing usable.

    None for a finding with no annotation at all AND for one whose annotation
    is not an object: `refuter: "refuted"` is a shape no reader can interpret,
    and inventing a verdict from it is exactly the kind of guess this module
    does not make.
    """
    if not isinstance(finding, dict):
        return None
    annotation = finding.get(REFUTER_KEY)
    return annotation if isinstance(annotation, dict) else None


#: C0 controls (0x00-0x1F) other than the two `trust.one_line` already turns
#: into spaces, plus DEL (0x7F) and the C1 controls (0x80-0x9F). ESC (0x1B)
#: is the one that matters most in practice — it is how a terminal is told to
#: move the cursor and erase what is already on screen, and the reviewer
#: demonstrated a complete, deterministic rewrite of a finding's OPEN/
#: DISMISSED status this way, from a refuter's free-text `reasoning` alone.
#: But every character in these ranges is a display-plane instruction, not
#: content, for a listing meant to be read as plain text — so all of them go,
#: not just the one used in the demonstrated exploit.
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")


def shown_field(value) -> str:
    """One display field, bounded and safe for a one-line-per-item listing.

    Three rules, in order: `trust.one_line` — the project's single "this
    value may not break out of a single-line record" rule, which replaces
    CR/LF with spaces and does nothing else; then every remaining C0/C1
    control character (ESC and friends — CR/LF are already gone by this
    point) is REMOVED, not escaped into some other visible marker, because
    escaping would just put a different attacker-controlled string on the
    line; then a hard length cap. All three are needed: the flattening stops
    a value with a raw newline in it from forging an extra row, the control
    strip stops one from rewriting what the terminal already printed, and the
    cap stops a 10,000-character field from burying the rest of the listing
    under itself.

    Used for every refuter annotation field (see `refuter_line`) AND for a
    finding's TITLE in the CLI listing: both are untrusted model text
    reaching the same one-line-per-item display, so both go through the same
    rule.
    """
    return _CONTROL_CHARS.sub("", one_line(value))[:MAX_ANNOTATION_DISPLAY_CHARS]


def refuter_line(annotation) -> str:
    """The one extra `triage --list` line for a refuter annotation.

    `refuter(<provider>/<model>): <verdict> — <reasoning>`, every field
    bounded by `shown_field`. A missing field renders as the empty string
    rather than the literal "None", the same convention `trust.one_line`
    already uses everywhere else.
    """
    if not isinstance(annotation, dict):
        annotation = {}
    return ("refuter(%s/%s): %s — %s"
            % (shown_field(annotation.get("provider")),
               shown_field(annotation.get("model")),
               shown_field(annotation.get("verdict")),
               shown_field(annotation.get("reasoning"))))


def refuter_pass_ran(review) -> bool:
    """Whether a refuter pass actually EXECUTED on this review record.

    THE AUTHENTICATION CHECK for the annotation channel, and it is not
    theoretical — read this before relaxing it.

    Task 8 hardened `merge_refuter_pass` and `skipped_refuter_pass` to strip
    any `refuter` key already present on a finding, because a finder's parsed
    findings are untrusted model output that reaches those functions verbatim
    and the adapter's payload validator checks the required keys without
    removing extra ones. That hardening covers the paths where a refuter pass
    was SCHEDULED. It cannot cover the paths where one was not: when
    `refuter_decision` declines (the `SKODUN_REFUTER_PASS` kill switch, a mode
    other than `now`, an untrustworthy finder), NEITHER merge function runs,
    and a `refuter` key the finder wrote about its own finding — verdict,
    reasoning, and a provider and model naming a vendor that was never
    invoked — rides straight into the stored artifact.

    So `refuter.verdict == "refuted"` on a finding is, on its own, not
    evidence that any refuter said anything. `extra_passes` is the other half:
    the pipeline builds it, a model's payload cannot contribute to it, and it
    is `{}` on exactly the records where no pass ran. Where a pass DID run,
    every annotation on the record went through the stripping merge and is
    therefore the pass's own.

    `ran is True` — never truthiness — because the value survives a JSON and
    SQLite round trip, and the string `"false"` is truthy.
    """
    if not isinstance(review, dict):
        return False
    extras = review.get("extra_passes")
    if not isinstance(extras, dict):
        return False
    meta = extras.get(REFUTER_KEY)
    return isinstance(meta, dict) and meta.get("ran") is True


def refuter_same_provider_as_finder(review) -> bool:
    """Whether the refuter pass answered from the FINDER's own provider.

    Read off `extra_passes.refuter`, which the pass writes and a finder cannot
    forge (`passes._strip_finder_refuter_keys` drops any incoming annotation,
    so the pass's own counters are the record's truth). Strictly `is True`, and
    defended at every level: the value comes out of stored JSON, and a
    truthiness test would read the string `"false"` as yes.
    """
    if not isinstance(review, dict):
        return False
    extras = review.get("extra_passes")
    if not isinstance(extras, dict):
        return False
    meta = extras.get(REFUTER_KEY)
    return isinstance(meta, dict) and meta.get("same_provider_as_finder") is True


def _attribution(annotation: dict, field: str) -> str:
    """One half of the `refuter(<provider>/<model>)` prefix, or raise.

    The prefix exists to say WHOSE verdict is being adopted. `refuter(None/
    None): ...` is attribution theatre, and it would be written into an audit
    ledger and read back years later, so an annotation that cannot say who
    answered is refused rather than rendered with a hole in it. Flattened with
    `one_line` for the same reason every other single-line record is: a
    provider carrying a newline must not be able to forge a second line inside
    one ledger reason.
    """
    value = annotation.get(field)
    if not isinstance(value, str) or not collapse_ws(value):
        raise TriageError(
            f"the refuter annotation's {field} is missing or unusable "
            f"({value!r}); a dismissal cannot be attributed to nobody")
    return one_line(value)


def adopt_refuter(store: Store, review: dict, index: int, now: str) -> dict:
    """Dismiss one finding on the strength of its refuter annotation.

    THE ONLY path by which a refuter verdict becomes a dismissal, and it is
    driven by a human naming one finding. Nothing here is automatic and there
    is deliberately no bulk form.

    Refuses, each as a `TriageError`: missing or overlapping contributor
    provenance; mismatched pass/annotation attribution; a finding with no usable
    annotation; a
    verdict that is not `refuted` (named in the message, because "your model
    said `confirmed`" is the actionable fact); an annotation the pass marked
    `thin_reasoning`; a `reasoning` that is not a string; and an annotation
    that cannot say which provider and model answered. A finding index that
    does not resolve raises `FindingNotFound`, and a self-inconsistent
    artifact raises `ArtifactError` — before anything is written, in every
    case.

    VALIDATION HAPPENS TWICE, DELIBERATELY
    --------------------------------------
    The RAW reasoning is validated first and ALONE. Only then is the
    synthesized `refuter(<provider>/<model>): <reasoning>` string validated
    and persisted through the ordinary `dismiss` path.

    The order is the whole point. `refuter(openai/some-model): race` is 29
    characters and clears the 20-character audit floor comfortably — on the
    strength of a provider name. Validating only the synthesized string would
    let a one-word refutation buy its way past the floor with attribution,
    which is precisely the failure the floor exists to prevent: a finding
    dismissed because a well-known model's name was long. Validating the raw
    reasoning first makes the prefix unable to rescue anything, and it puts
    the *whole* of `validate_reason` — including the placeholder set — on the
    model's own words, so a refuter that answered "false positive" is refused
    with the placeholder message.

    The second pass is not decorative either: the string that is actually
    persisted is the string that was actually validated, so no future change
    to the prefix can quietly put an unvalidated reason in the ledger.

    Trustworthiness is deliberately NOT checked, exactly as `dismiss` has
    never checked it: the gate re-asserts trust against the artifact itself
    and never even reaches an untrustworthy review, so a check here would be a
    second, implicit policy that changes nothing the gate decides.
    """
    review = load_valid_artifact(review)
    finding = _finding_at(review["findings"], index)

    # BEFORE the annotation is read at all: an annotation on a record where no
    # refuter pass ran has no provenance and may simply be something the FINDER
    # wrote about its own finding. See `refuter_pass_ran`.
    if not refuter_pass_ran(review):
        raise TriageError(
            "no refuter pass ran on this review, so any refuter annotation on "
            "it is unattributed and cannot be adopted; re-review with a "
            "reviewer in role 'refuter', or dismiss the finding with your own "
            "reason")

    annotation = refuter_annotation(finding)
    if annotation is None:
        raise TriageError(
            f"finding {index} carries no refuter annotation, so there is no "
            "verdict to adopt; dismiss it with your own reason instead")

    verdict = annotation.get("verdict")
    if not isinstance(verdict, str):
        raise TriageError(
            f"the refuter annotation on finding {index} carries no verdict "
            f"({verdict!r})")
    if verdict != ADOPTABLE_VERDICT:
        raise TriageError(
            f"the refuter's verdict on finding {index} is {verdict!r}, not "
            f"{ADOPTABLE_VERDICT!r}; only a refutation can be adopted as a "
            "dismissal")

    # The pass marks this when the reasoning is under MIN_REASON_CHARS,
    # measured with `textnorm.collapse_ws` — the same helper `validate_reason`
    # measures its floor with. Refused on the flag AND, independently, by the
    # raw validation below: the flag is written by the pass and could be
    # absent from a hand-edited artifact, so it may not be the only thing
    # standing here.
    if annotation.get("thin_reasoning") is True:
        raise TriageError(
            f"the refuter's reasoning on finding {index} is marked "
            "thin_reasoning and cannot be adopted; dismiss it with your own "
            "reason if you agree with it")

    reasoning = annotation.get("reasoning")
    if not isinstance(reasoning, str):
        # `validate_reason` would stringify anything (`collapse_ws` calls
        # `str()`), so `{'r': 'x'}` would become a 12-character "reason" and a
        # list of words a perfectly auditable-looking one.
        raise TriageError(
            f"the refuter's reasoning on finding {index} is not text "
            f"({reasoning!r})")

    validate_reason(reasoning)          # FIRST, and on the raw words alone

    reason = "%s(%s/%s): %s" % (REFUTER_KEY, _attribution(annotation, "provider"),
                                _attribution(annotation, "model"), reasoning)
    validate_reason(reason)             # then on exactly what gets persisted
    from .refuter_policy import adoption_refusal

    refusal = adoption_refusal(review["extra_passes"][REFUTER_KEY], annotation)
    if refusal is not None:
        raise TriageError(refusal)
    return dismiss(store, review, index, reason, now)
