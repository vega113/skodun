"""Reason validation, ledger dismissal, and fail-closed artifact validation.

Parity with the oracle (`grok_review_triage.py`) matters here because the
whole point of the triage ledger is that a dismissal or an artifact-shape
rule silently drifting from the legacy tool would either resurrect an
already-litigated finding or let a corrupt artifact satisfy the gate.
`test_placeholder_set_matches_legacy` and `test_validate_reason_parity_with_legacy_module`
load the *actual* oracle module from `$SKODUN_ORACLE_DIR` and assert
agreement directly; they skip (not xfail, not silently pass) when the oracle
checkout is absent.

Artifact validation is the one place skodun is deliberately STRICTER than the
oracle, because it guards the gate's fail-closed contract rather than a
display path. That divergence is documented in `triage.py` and pinned from
both sides by `test_load_valid_artifact_divergence_from_legacy_is_deliberate`.
"""

import importlib.util
import sys

import pytest

from skodun.store import Store
from skodun.textnorm import collapse_ws, finding_key, norm
from skodun.triage import (
    ADOPTABLE_VERDICT,
    MAX_ANNOTATION_DISPLAY_CHARS,
    MIN_REASON_CHARS,
    PLACEHOLDER_REASONS,
    REFUTER_KEY,
    ArtifactError,
    FindingNotFound,
    TriageError,
    adopt_refuter,
    dismiss,
    load_valid_artifact,
    open_findings,
    refuter_annotation,
    refuter_line,
    refuter_pass_ran,
    refuter_same_provider_as_finder,
    validate_reason,
)

from tests.conftest import oracle_dir

LEGACY = (oracle_dir() / "scripts" / "grok_review_triage.py") if oracle_dir() else None

GOOD = dict(id="r1", branch="feat", base_sha="s" * 40, findings_total=1,
            findings=[dict(file="a.py", line=3, severity="high",
                           category="bug", title="NPE", detail="boom")])


def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_triage", LEGACY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_triage"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# validate_reason
# ---------------------------------------------------------------------------

def test_reason_rules():
    with pytest.raises(TriageError):
        validate_reason("false positive")
    with pytest.raises(TriageError):
        validate_reason("short")
    validate_reason("download-artifact@v4 already extracts to the target dir, see README")


def test_reason_placeholder_case_and_whitespace_insensitive():
    # PLACEHOLDER_REASONS is matched against the normalized (lowercased,
    # whitespace-collapsed) reason, so shouting or padding it must not help.
    #
    # Each assertion checks the MESSAGE, not merely that something raised:
    # every placeholder is shorter than MIN_REASON_CHARS, so a "too short"
    # rejection would satisfy `pytest.raises` while the placeholder branch sat
    # unreachable and its actionable wording ("say WHY") was never emitted.
    for reason in ("  FALSE   POSITIVE  ", "Wontfix", "false positive"):
        with pytest.raises(TriageError) as exc:
            validate_reason(reason)
        assert "placeholder" in str(exc.value), reason
        assert "chars" not in str(exc.value), reason


def test_reason_placeholder_padded_past_the_length_floor_still_placeholder():
    # 34 raw chars -- comfortably over MIN_REASON_CHARS before normalization --
    # but the padding collapses away and the reason is still nothing but a
    # placeholder. The oracle rejects this with its placeholder message (not
    # its length message), and so must skodun.
    padded = "false positive" + " " * 20
    assert len(padded) >= MIN_REASON_CHARS
    with pytest.raises(TriageError) as exc:
        validate_reason(padded)
    assert "placeholder" in str(exc.value)


@pytest.mark.parametrize("reason", sorted(PLACEHOLDER_REASONS))
def test_every_placeholder_is_rejected_as_a_placeholder(reason):
    # The whole set is live, not decorative: if the length check ran first,
    # all 27 would be rejected with the wrong (and unactionable) reason.
    with pytest.raises(TriageError) as exc:
        validate_reason(reason)
    assert "placeholder" in str(exc.value)


def test_reason_length_boundary_exact():
    # Exactly MIN_REASON_CHARS normalized chars must be accepted; one under
    # must be rejected. Both strings are plain lowercase ASCII already, so
    # normalization does not change their length.
    exactly_20 = "a" * 20
    assert len(exactly_20) == MIN_REASON_CHARS
    validate_reason(exactly_20)  # must not raise
    with pytest.raises(TriageError):
        validate_reason("a" * (MIN_REASON_CHARS - 1))


@pytest.mark.parametrize("reason", ["İ" * 10, "ABCDEFGHIJİABCDEFGH"])
def test_reason_length_measured_before_lowercasing(reason):
    # U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE) lowercases to TWO
    # codepoints, so `len(norm(reason))` overcounts: "İ"*10 is 10 characters
    # but normalizes to 20. Measuring the floor on the lowercased form would
    # let a 10-character reason clear a 20-character audit bar. The floor is
    # measured on the collapsed-not-lowercased form, exactly as the oracle
    # measures it, so both of these must be rejected.
    assert len(norm(reason)) >= MIN_REASON_CHARS   # lenient form would accept
    assert len(collapse_ws(reason)) < MIN_REASON_CHARS
    with pytest.raises(TriageError):
        validate_reason(reason)


def test_reason_long_enough_raw_but_collapses_under_normalization():
    # "a" + 30 spaces + "b" is 32 raw chars (>= MIN_REASON_CHARS) but
    # collapses to "a b" (3 chars) once whitespace is normalized to a single
    # space. Validation must operate on the normalized form, not the raw
    # string, so this must still be rejected.
    raw = "a" + (" " * 30) + "b"
    assert len(raw) >= MIN_REASON_CHARS
    with pytest.raises(TriageError):
        validate_reason(raw)


def test_reason_empty_and_whitespace_only_rejected():
    with pytest.raises(TriageError):
        validate_reason("")
    with pytest.raises(TriageError):
        validate_reason("    \t\n   ")


# ---------------------------------------------------------------------------
# load_valid_artifact
# ---------------------------------------------------------------------------

def test_artifact_validation_fails_closed():
    for bad in [dict(GOOD, findings_total=True),
                dict(GOOD, findings_total=2),
                dict(GOOD, findings="oops"),
                dict(GOOD, findings=[1]),
                "not a dict"]:
        with pytest.raises(ArtifactError):
            load_valid_artifact(bad)


def test_artifact_findings_total_float_rejected():
    with pytest.raises(ArtifactError):
        load_valid_artifact(dict(GOOD, findings_total=1.0))


def test_artifact_findings_total_string_rejected():
    with pytest.raises(ArtifactError):
        load_valid_artifact(dict(GOOD, findings_total="1"))


def test_artifact_findings_non_dict_members_rejected():
    with pytest.raises(ArtifactError):
        load_valid_artifact(dict(GOOD, findings=[None], findings_total=1))
    with pytest.raises(ArtifactError):
        load_valid_artifact(dict(GOOD, findings=["x", "y"], findings_total=2))


IDENTITY_FIELDS = ("id", "branch", "base_sha")


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_artifact_missing_identity_field_rejected(field):
    # `dismiss` reads all three straight after validation "passes", and builds
    # the ledger key from branch + base_sha. A validator that skips the fields
    # its own caller consumes just relocates the failure to a bare KeyError.
    rec = {k: v for k, v in GOOD.items() if k != field}
    with pytest.raises(ArtifactError, match=field):
        load_valid_artifact(rec)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
@pytest.mark.parametrize("value", [None, 1, ["x"], {"a": 1}])
def test_artifact_non_string_identity_field_rejected(field, value):
    with pytest.raises(ArtifactError, match=field):
        load_valid_artifact(dict(GOOD, **{field: value}))


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_dismiss_raises_artifact_error_not_keyerror_on_missing_identity(tmp_path, field):
    st = Store.open(tmp_path / "s.db")
    rec = {k: v for k, v in GOOD.items() if k != field}
    with pytest.raises(ArtifactError):
        dismiss(st, rec, 0, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")


def test_artifact_empty_but_complete_is_accepted():
    # The fail-closed rule is about *asserted* shape, not about forbidding a
    # clean review: an artifact that explicitly records zero findings and a
    # matching zero total is well-formed and must pass.
    rec = dict(id="r1", branch="feat", base_sha="s" * 40,
               findings=[], findings_total=0)
    assert load_valid_artifact(rec) is rec
    assert open_findings(rec, {}) == []


# --- Deliberate divergence from the oracle -------------------------------
#
# The oracle's `load_review` ACCEPTS every artifact below: it coerces a
# missing/None `findings` to `[]`, skips the `findings_total` check entirely
# when that key is missing/None, and never inspects `id`/`branch`/`base_sha`.
# skodun rejects all of them, on purpose. `load_valid_artifact` guards the gate's fail-closed contract --
# under the lenient rule an artifact with no recorded `findings` reads as
# "zero findings", i.e. clean, and the gate could PASS a review whose
# findings were never stored. It is also the check the legacy importer
# relies on to stop a findings-less index row from certifying a push.
# Being stricter than the oracle is fail-safe in this direction: the worst
# case is that a malformed artifact forces one fresh review.
# `test_load_valid_artifact_divergence_from_legacy_is_deliberate` pins the
# other half of this claim -- that the oracle really does accept them.

DIVERGENT = {
    "missing findings": dict(id="r2", branch="feat", base_sha="s" * 40,
                             findings_total=0),
    "null findings": dict(id="r2", branch="feat", base_sha="s" * 40,
                          findings=None, findings_total=0),
    "missing findings_total": {k: v for k, v in GOOD.items()
                               if k != "findings_total"},
    "null findings_total": dict(GOOD, findings_total=None),
    # The oracle never looks at the identity trio at all, so it accepts these
    # too -- and then `dismiss` would read review["branch"] and raise a bare
    # KeyError, or build a ledger key from a non-string and scope the
    # dismissal to the wrong review loop.
    "missing branch": {k: v for k, v in GOOD.items() if k != "branch"},
    "non-string base_sha": dict(GOOD, base_sha=40),
}


@pytest.mark.parametrize("label", sorted(DIVERGENT))
def test_artifact_missing_or_null_keys_rejected_stricter_than_oracle(label):
    with pytest.raises(ArtifactError):
        load_valid_artifact(DIVERGENT[label])


def test_dismiss_and_open_findings_reject_the_divergent_shapes_too(tmp_path):
    # The strictness has to hold on the paths that actually reach the gate,
    # not just on the validator in isolation -- neither entry point may fall
    # back to "no findings key means nothing to report".
    st = Store.open(tmp_path / "s.db")
    for rec in DIVERGENT.values():
        with pytest.raises(ArtifactError):
            dismiss(st, rec, 0, "a perfectly valid twenty-plus character reason here",
                    now="2026-07-27T10:00:00Z")
        with pytest.raises(ArtifactError):
            open_findings(rec, {})


# ---------------------------------------------------------------------------
# dismiss / open_findings
# ---------------------------------------------------------------------------

def test_negative_index_rejected(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        dismiss(st, GOOD, -1, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")


def test_index_equal_to_length_rejected(tmp_path):
    # len(findings) == 1 for GOOD, so index 1 is one-past-the-end and must
    # be rejected exactly like any other out-of-range index.
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        dismiss(st, GOOD, 1, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")


@pytest.mark.parametrize("index", [True, False, 0.0, "0", None, [0]])
def test_non_int_index_rejected(tmp_path, index):
    # `isinstance(True, int)` is True, so an unguarded bool INDEXES the list:
    # True would dismiss findings[1] -- a different finding than the caller
    # named -- and False would silently mean 0. Every other non-int must
    # surface as TriageError rather than a raw TypeError/AttributeError.
    two = dict(GOOD, findings_total=2,
               findings=GOOD["findings"] + [dict(file="b.py", line=9, severity="low",
                                                 category="style", title="unused import",
                                                 detail="meh")])
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        dismiss(st, two, index, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")
    assert st.triage_for("feat", "s" * 40) == {}


def test_dismiss_rejects_invalid_reason_before_writing(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        dismiss(st, GOOD, 0, "fp", now="2026-07-27T10:00:00Z")
    assert st.triage_for("feat", "s" * 40) == {}


def test_dismiss_rejects_corrupt_artifact_before_writing(tmp_path):
    st = Store.open(tmp_path / "s.db")
    bad = dict(GOOD, findings_total=2)
    with pytest.raises(ArtifactError):
        dismiss(st, bad, 0, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")
    assert st.triage_for("feat", "s" * 40) == {}


def test_dismiss_and_open(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = dismiss(st, GOOD, 0, "line numbers drift; verified handler checks None on entry",
                  now="2026-07-27T10:00:00Z")
    assert rec["finding_key"] == finding_key("a.py", "NPE")
    triaged = st.triage_for("feat", "s" * 40)
    assert open_findings(GOOD, triaged) == []


def test_open_findings_leaves_undismissed_findings_open(tmp_path):
    two = dict(GOOD, findings_total=2,
               findings=GOOD["findings"] + [dict(file="b.py", line=9, severity="low",
                                                  category="style", title="unused import",
                                                  detail="meh")])
    st = Store.open(tmp_path / "s.db")
    dismiss(st, two, 0, "line numbers drift; verified handler checks None on entry",
            now="2026-07-27T10:00:00Z")
    triaged = st.triage_for("feat", "s" * 40)
    remaining = open_findings(two, triaged)
    assert len(remaining) == 1
    assert remaining[0]["title"] == "unused import"


# ---------------------------------------------------------------------------
# Parity with the oracle
# ---------------------------------------------------------------------------

def test_placeholder_set_matches_legacy():
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()
    assert PLACEHOLDER_REASONS == legacy.PLACEHOLDER_REASONS
    assert MIN_REASON_CHARS == legacy.MIN_REASON_CHARS


def test_validate_reason_parity_with_legacy_module():
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    cases = [
        "false positive",
        "  FALSE   POSITIVE  ",
        "fp",
        "short",
        "",
        "    \t\n   ",
        "a" * 20,
        "a" * 19,
        "a" + (" " * 30) + "b",
        "download-artifact@v4 already extracts to the target dir, see README",
        "line numbers drift; verified handler checks None on entry",
        "wontfix",
        "won't fix",
        "already fixed",
        "not an issue",
        "this is a perfectly legitimate reason with plenty of detail",
        "ok",
        "known",
        # Non-ASCII, where `str.lower()` LENGTHENS the string: U+0130
        # lowercases to two codepoints. Measuring the floor on the lowercased
        # form accepts both of these while the oracle rejects both, so they
        # are the exact inputs that catch that divergence.
        "İ" * 10,
        "ABCDEFGHIJİABCDEFGH",
        "İ" * 20,
        # A placeholder padded past the raw length floor: both sides must
        # reject on the placeholder rule, which they only reach if the
        # placeholder check precedes the length check.
        "false positive" + " " * 20,
    ]
    for reason in cases:
        skodun_ok = True
        try:
            validate_reason(reason)
        except TriageError:
            skodun_ok = False

        legacy_ok = True
        try:
            legacy.validate_reason(reason)
        except ValueError:
            legacy_ok = False

        assert skodun_ok == legacy_ok, f"disagreement on {reason!r}"


def _legacy_accepts(legacy, tmp_path, review_id: str, rec) -> bool:
    """True if the oracle's `load_review` accepts `rec`.

    It reads from a file on disk keyed by review id, so each case has to be
    materialized as a temp artifact file first.
    """
    import json
    (tmp_path / f"{review_id}.json").write_text(json.dumps(rec), encoding="utf-8")
    try:
        legacy.load_review(str(tmp_path), review_id)
        return True
    except ValueError:
        return False


def test_load_valid_artifact_parity_with_legacy_on_fully_specified_artifacts(tmp_path):
    # Where the artifact actually asserts both keys, skodun and the oracle
    # must agree exactly -- the divergence below is confined to the
    # missing/None cases and must not have leaked anywhere else.
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    cases = [
        GOOD,
        dict(id="r3", branch="feat", base_sha="s" * 40, findings=[], findings_total=0),
        dict(GOOD, findings_total=2),
        dict(GOOD, findings="oops"),
        dict(GOOD, findings=[1]),
        dict(GOOD, findings_total=True),
        dict(GOOD, findings_total=1.0),
        dict(GOOD, findings_total="1"),
    ]
    for i, rec in enumerate(cases):
        skodun_ok = True
        try:
            load_valid_artifact(rec)
        except ArtifactError:
            skodun_ok = False
        legacy_ok = _legacy_accepts(legacy, tmp_path, f"case{i}", rec)
        assert skodun_ok == legacy_ok, f"disagreement on case {i}: {rec!r}"


def test_load_valid_artifact_divergence_from_legacy_is_deliberate(tmp_path):
    # Pins BOTH halves of the documented divergence against the real oracle:
    # the oracle accepts each of these findings-less / total-less artifacts,
    # and skodun rejects each. The direction is intentional and fail-safe --
    # `load_valid_artifact` guards the gate's fail-closed contract, and the
    # oracle's leniency would let an artifact with no recorded findings read
    # as clean and certify a push. If this test starts failing because the
    # oracle grew stricter, the fix is to delete the divergence note, never
    # to loosen skodun.
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    for i, (label, rec) in enumerate(sorted(DIVERGENT.items())):
        assert _legacy_accepts(legacy, tmp_path, f"div{i}", rec), \
            f"oracle unexpectedly rejects {label}: {rec!r}"
        with pytest.raises(ArtifactError):
            load_valid_artifact(rec)


# ---------------------------------------------------------------------------
# Refuter annotations: display, and the ONE path by which a verdict may dismiss
# ---------------------------------------------------------------------------
#
# A refuter verdict is an annotation and nothing else: it changes no count, no
# severity, no trust axis, and a review whose only finding is marked `refuted`
# still gates 1. `adopt_refuter` is the only path by which that verdict can
# ever become a dismissal, and it is explicit and per-finding on purpose --
# there is deliberately no `--adopt-all` and no auto-adoption anywhere.
#
# The annotation itself originates in model output, so every field below is
# treated as untrusted data: types are checked, and nothing in a `verdict`,
# `reasoning`, `provider` or `model` steers control flow beyond the checks
# spelled out here.

#: A reasoning that clears the audit floor on its own, with no help from the
#: attribution prefix.
REASONING = "the guard at line 12 already rejects a None handler before this runs"

#: The synthesized ledger reason for `_annotated()`'s defaults.
SYNTHESIZED = f"refuter(openai/model-x): {REASONING}"


#: What `passes.merge_refuter_pass` writes into `extra_passes` for a pass that
#: ran. `ran: True` is what authenticates every annotation on the record: the
#: pipeline writes this object and a model's payload cannot contribute to it,
#: and where a pass ran, the merge stripped any annotation the FINDER had
#: forged before writing its own. See `triage.refuter_pass_ran`.
RAN = {"pass": REFUTER_KEY, "ran": True, "status": "ran", "degraded": False,
       "verdicts_total": 1, "annotated": 1, "dropped": 0,
       "provider": "openai", "model": "model-x", "effort": None, "note": ""}


def _annotated(verdict=ADOPTABLE_VERDICT, reasoning=REASONING, provider="openai",
               model="model-x", _drop=(), _meta=RAN, **extra):
    """`GOOD`, with a refuter annotation on its single finding."""
    ann = {"verdict": verdict, "reasoning": reasoning,
           "provider": provider, "model": model}
    ann.update(extra)
    for key in _drop:
        ann.pop(key, None)
    art = dict(GOOD, findings=[dict(GOOD["findings"][0], **{REFUTER_KEY: ann})])
    if _meta is not None:
        art["extra_passes"] = {REFUTER_KEY: dict(_meta)}
    return art


def _triaged(st):
    return st.triage_for(GOOD["branch"], GOOD["base_sha"])


def test_the_annotation_key_matches_the_pass_that_writes_it():
    # `triage` cannot import `passes` (that module already imports this one),
    # so the spelling is duplicated -- and a duplicated constant that drifts
    # would make every annotation invisible to adoption while every test that
    # builds its own fixture kept passing.
    from skodun.passes import REFUTER_PASS

    assert REFUTER_KEY == REFUTER_PASS


def test_only_refuted_is_adoptable():
    from skodun.adapters import REFUTER_VERDICTS

    assert ADOPTABLE_VERDICT == "refuted"
    assert ADOPTABLE_VERDICT in REFUTER_VERDICTS


# --- adoption, happy path -------------------------------------------------

def test_adopt_refuter_records_the_dismissal_and_closes_the_finding(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = adopt_refuter(st, _annotated(), 0, now="2026-07-27T10:00:00Z")

    assert rec["finding_key"] == finding_key("a.py", "NPE")
    # The ledger keeps the attribution AND the untruncated reasoning.
    assert rec["dismissed_reason"] == SYNTHESIZED
    triaged = _triaged(st)
    assert open_findings(_annotated(), triaged) == []


def test_the_adopted_reason_is_persisted_verbatim(tmp_path):
    st = Store.open(tmp_path / "s.db")
    adopt_refuter(st, _annotated(), 0, now="2026-07-27T10:00:00Z")
    row = _triaged(st)[finding_key("a.py", "NPE")]
    assert row["dismissed_reason"] == SYNTHESIZED


def test_a_long_reasoning_reaches_the_ledger_untruncated(tmp_path):
    long = "the handler is unreachable because " + ("x" * 400)
    st = Store.open(tmp_path / "s.db")
    rec = adopt_refuter(st, _annotated(reasoning=long), 0,
                        now="2026-07-27T10:00:00Z")
    assert rec["dismissed_reason"].endswith(long)
    assert len(rec["dismissed_reason"]) > MAX_ANNOTATION_DISPLAY_CHARS


def test_adopting_the_same_finding_twice_is_idempotent(tmp_path):
    st = Store.open(tmp_path / "s.db")
    adopt_refuter(st, _annotated(), 0, now="2026-07-27T10:00:00Z")
    adopt_refuter(st, _annotated(), 0, now="2026-07-27T11:00:00Z")
    triaged = _triaged(st)
    assert len(triaged) == 1
    assert triaged[finding_key("a.py", "NPE")]["dismissed_at"] == "2026-07-27T11:00:00Z"


def test_adoption_is_per_finding_and_leaves_the_others_open(tmp_path):
    second = dict(file="b.py", line=9, severity="low", category="style",
                  title="unused import", detail="meh",
                  **{REFUTER_KEY: {"verdict": ADOPTABLE_VERDICT,
                                   "reasoning": REASONING,
                                   "provider": "openai", "model": "model-x"}})
    art = dict(_annotated(), findings_total=2)
    art["findings"] = art["findings"] + [second]
    st = Store.open(tmp_path / "s.db")
    adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    remaining = open_findings(art, _triaged(st))
    assert [f["title"] for f in remaining] == ["unused import"]


def test_adoption_does_not_require_a_trustworthy_review(tmp_path):
    # Deliberate parity with `dismiss`, which has never checked this: the gate
    # re-asserts trust against the artifact itself and never even reaches an
    # untrustworthy review, so a trust check here would be a second, implicit
    # policy that changes nothing the gate decides.
    st = Store.open(tmp_path / "s.db")
    art = dict(_annotated(), trustworthy=False, degraded=True)
    adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    assert len(_triaged(st)) == 1


# --- adoption, refusals ---------------------------------------------------

@pytest.mark.parametrize("verdict", ["confirmed", "uncertain"])
def test_adopting_a_non_refuted_verdict_is_refused_naming_the_verdict(tmp_path,
                                                                      verdict):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, _annotated(verdict=verdict), 0, now="2026-07-27T10:00:00Z")
    assert verdict in str(exc.value)
    assert _triaged(st) == {}


def test_a_thin_reasoning_annotation_is_refused(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, _annotated(reasoning="nope.", thin_reasoning=True), 0,
                      now="2026-07-27T10:00:00Z")
    assert "thin" in str(exc.value)
    assert _triaged(st) == {}


def test_the_attribution_prefix_can_never_be_what_clears_the_floor(tmp_path):
    """The reason validation runs TWICE, and this is why.

    `refuter(openai/model-x): race` clears the 20-char floor comfortably --
    on the strength of a provider name. The RAW reasoning is validated first
    and alone, so a one-word refutation is refused however long the
    attribution happens to be. Deliberately built WITHOUT `thin_reasoning`:
    that flag is written by the pass and could be absent from a hand-edited
    artifact, so it may not be the only thing standing here.
    """
    st = Store.open(tmp_path / "s.db")
    art = _annotated(reasoning="race")
    assert REFUTER_KEY in art["findings"][0]
    assert "thin_reasoning" not in art["findings"][0][REFUTER_KEY]
    assert len(f"refuter(openai/model-x): race") >= MIN_REASON_CHARS

    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    assert "chars" in str(exc.value)
    assert _triaged(st) == {}


def test_a_placeholder_reasoning_is_refused_as_a_placeholder(tmp_path):
    # The raw reasoning goes through the WHOLE of `validate_reason`, not just
    # its length floor: a model that answered "false positive" is refused with
    # the placeholder message even though the synthesized string would clear
    # every length check.
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, _annotated(reasoning="false positive"), 0,
                      now="2026-07-27T10:00:00Z")
    assert "placeholder" in str(exc.value)
    assert _triaged(st) == {}


def test_a_finding_with_no_refuter_annotation_is_refused(tmp_path):
    st = Store.open(tmp_path / "s.db")
    bare = dict(GOOD, extra_passes={REFUTER_KEY: dict(RAN, annotated=0)})
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, bare, 0, now="2026-07-27T10:00:00Z")
    assert "no refuter" in str(exc.value).lower()
    assert _triaged(st) == {}
    # ...and it is a refusal, not a "finding not found": the finding is right
    # there, and the CLI maps the two to different exit codes.
    assert not isinstance(exc.value, FindingNotFound)


@pytest.mark.parametrize("annotation", ["refuted", None, 5, ["refuted"], True])
def test_a_non_object_annotation_is_refused(tmp_path, annotation):
    st = Store.open(tmp_path / "s.db")
    art = dict(GOOD, extra_passes={REFUTER_KEY: dict(RAN)},
               findings=[dict(GOOD["findings"][0], **{REFUTER_KEY: annotation})])
    with pytest.raises(TriageError):
        adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


@pytest.mark.parametrize("verdict", [None, 5, ["refuted"], True, {"v": "refuted"}])
def test_a_non_string_verdict_is_refused(tmp_path, verdict):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        adopt_refuter(st, _annotated(verdict=verdict), 0, now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


#: `str()` of these two clears MIN_REASON_CHARS and matches no placeholder,
#: unlike every OTHER fixture below -- `str(None)`, `str(5)`, `str(["ok"])`,
#: `str(True)` and `str({"r": "x"})` are all short enough to be rejected by
#: the length floor alone, which is why deleting the `isinstance(reasoning,
#: str)` guard at `triage.py` survives the whole suite without these two:
#: every existing fixture is refused for being too short either way, so the
#: type check itself is never exercised. These make the type check load-
#: bearing: `str(_LONG_LIST)` / `str(_LONG_DICT)` are long, unplaceholdered,
#: and would sail through `validate_reason` if it were ever handed the
#: stringified form instead of being refused for not being a string at all.
_LONG_LIST_REASONING = [
    "the guard at line 12 already rejects a None handler before this runs"]
_LONG_DICT_REASONING = {
    "r": "the guard at line 12 already rejects a None handler before this runs"}


@pytest.mark.parametrize("reasoning", [None, 5, ["ok"], True, {"r": "x"},
                                        _LONG_LIST_REASONING, _LONG_DICT_REASONING])
def test_a_non_string_reasoning_is_refused(tmp_path, reasoning):
    # `validate_reason` would happily stringify these (`collapse_ws` calls
    # `str()`), so `{'r': 'x'}` would become a 12-character "reason" and a list
    # of words a perfectly auditable-looking one. Types are checked first.
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        adopt_refuter(st, _annotated(reasoning=reasoning), 0,
                      now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("value", [None, "", "   ", 5, ["openai"], True])
def test_an_unusable_attribution_is_refused(tmp_path, field, value):
    # The prefix exists to say WHOSE verdict this is. `refuter(None/None): ...`
    # is attribution theatre, and it would be written into an audit ledger.
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, _annotated(**{field: value}), 0,
                      now="2026-07-27T10:00:00Z")
    assert field in str(exc.value)
    assert _triaged(st) == {}


@pytest.mark.parametrize("field", ["provider", "model"])
def test_a_missing_attribution_field_is_refused(tmp_path, field):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        adopt_refuter(st, _annotated(_drop=(field,)), 0, now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


def test_an_attribution_carrying_newlines_cannot_forge_a_second_record(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = adopt_refuter(st, _annotated(provider="openai\nrefuter(anthropic",
                                       model="m\r\n2"), 0,
                        now="2026-07-27T10:00:00Z")
    assert "\n" not in rec["dismissed_reason"]
    assert "\r" not in rec["dismissed_reason"]


def test_a_huge_attribution_still_only_ever_adds_to_the_reason(tmp_path):
    # 10k characters of provider name is a nuisance, not a bypass: the RAW
    # reasoning was already validated on its own before any of it was seen.
    st = Store.open(tmp_path / "s.db")
    rec = adopt_refuter(st, _annotated(provider="p" * 10_000), 0,
                        now="2026-07-27T10:00:00Z")
    assert rec["dismissed_reason"].endswith(REASONING)


def test_the_attribution_prefix_preserves_case(tmp_path):
    """Every fixture provider/model in this module is already lowercase, so a
    mutant that case-folds `_attribution` (`one_line` -> `norm`) would survive
    the whole suite undetected -- and this is a PERMANENT AUDIT LEDGER, where
    a provider name silently case-folding is exactly the kind of regression
    nobody notices until they go looking for it. `_attribution` uses
    `one_line`, which does not touch case; pin that directly."""
    st = Store.open(tmp_path / "s.db")
    rec = adopt_refuter(st, _annotated(provider="OpenAI", model="GPT-Something"), 0,
                        now="2026-07-27T10:00:00Z")
    assert rec["dismissed_reason"] == f"refuter(OpenAI/GPT-Something): {REASONING}"


@pytest.mark.parametrize("index", [-1, 1, 2, True, False, 0.0, "0", None, [0]])
def test_an_unusable_index_is_a_finding_not_found(tmp_path, index):
    # Same rule `dismiss` enforces -- `isinstance(True, int)` is True, so an
    # unguarded bool would adopt a verdict about a DIFFERENT finding -- but
    # raised as the subclass the CLI maps to exit 2 rather than to a refusal.
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(FindingNotFound):
        adopt_refuter(st, _annotated(), index, now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


def test_finding_not_found_is_a_triage_error():
    # The CLI's plain-dismissal path catches `TriageError` and must keep
    # catching every index failure, subclass or not.
    assert issubclass(FindingNotFound, TriageError)


@pytest.mark.parametrize("label", sorted(DIVERGENT))
def test_adopt_rejects_a_corrupt_artifact_before_writing(tmp_path, label):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ArtifactError):
        adopt_refuter(st, DIVERGENT[label], 0, now="2026-07-27T10:00:00Z")
    assert st.triage_for("feat", "s" * 40) == {}


def test_adopt_rejects_a_findings_total_mismatch_before_writing(tmp_path):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ArtifactError):
        adopt_refuter(st, dict(_annotated(), findings_total=2), 0,
                      now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


# --- display --------------------------------------------------------------

def test_refuter_annotation_returns_none_for_anything_unusable():
    assert refuter_annotation(GOOD["findings"][0]) is None
    for bad in ["refuted", None, 5, ["x"], True]:
        f = dict(GOOD["findings"][0], **{REFUTER_KEY: bad})
        assert refuter_annotation(f) is None, bad
    good = _annotated()["findings"][0]
    assert refuter_annotation(good) == good[REFUTER_KEY]


def test_refuter_line_format():
    line = refuter_line(_annotated()["findings"][0][REFUTER_KEY])
    assert line == f"refuter(openai/model-x): refuted — {REASONING}"


def test_refuter_line_flattens_newlines_into_spaces():
    art = _annotated(reasoning="first line\r\nsecond line\nthird")
    ann = art["findings"][0][REFUTER_KEY]
    line = refuter_line(ann)
    assert "\n" not in line and "\r" not in line
    assert "first line" in line and "third" in line


def test_refuter_line_truncates_the_reasoning_but_the_artifact_keeps_it():
    long = "u" * 500
    art = _annotated(reasoning=long)
    ann = art["findings"][0][REFUTER_KEY]
    line = refuter_line(ann)
    assert line.endswith("u" * MAX_ANNOTATION_DISPLAY_CHARS)
    assert not line.endswith("u" * (MAX_ANNOTATION_DISPLAY_CHARS + 1))
    # The annotation itself is untouched -- the truncation is a display rule,
    # and the artifact (and any adopted ledger reason) keeps the original.
    assert ann["reasoning"] == long
    assert art["findings"][0][REFUTER_KEY]["reasoning"] == long


@pytest.mark.parametrize("field", ["provider", "model", "verdict"])
def test_every_annotation_field_is_bounded_in_the_listing(field):
    # `--list` is one line per finding plus at most one line per annotation. A
    # 10,000-character provider name or a newline in a model id would drown or
    # split that listing; all three fields go through the same rule the
    # reasoning does.
    ann = _annotated(**{field: "z" * 5000})["findings"][0][REFUTER_KEY]
    assert len(refuter_line(ann)) < 4 * MAX_ANNOTATION_DISPLAY_CHARS
    ann = _annotated(**{field: "a\nb"})["findings"][0][REFUTER_KEY]
    assert "\n" not in refuter_line(ann)


def test_refuter_line_never_raises_on_a_malformed_annotation():
    # Rendering may never be the thing that crashes a listing.
    for ann in [{}, {"verdict": None, "reasoning": None},
                {"verdict": 5, "reasoning": ["x"], "provider": {}, "model": 1.5}]:
        assert isinstance(refuter_line(ann), str)


def test_refuter_line_strips_ansi_cursor_control_from_the_reasoning():
    """The live exploit: a `reasoning` carrying `\\x1b[1A\\x1b[2K\\x1b[G`
    (cursor up, erase line, column 0) rewrites whatever the terminal already
    printed just above -- which for `--list` is always the finding's own
    OPEN/DISMISSED status line, because the annotation line immediately
    follows it. Suppressing an unauthenticated annotation for this exact
    reason while printing an authenticated one with live cursor control would
    be internally inconsistent with the feature's own threat model."""
    rewrite = "\x1b[1A\x1b[2K\x1b[G[0] high a0.py:3 NPE 0 (DISMISSED)\x1b[1B\x1b[G"
    art = _annotated(reasoning="before " + rewrite + " after")
    ann = art["findings"][0][REFUTER_KEY]
    line = refuter_line(ann)
    assert "\x1b" not in line
    assert "before" in line and "after" in line


@pytest.mark.parametrize("field", ["provider", "model", "verdict"])
def test_refuter_line_strips_control_characters_from_every_field(field):
    ann = _annotated(**{field: "x\x1b[2Jy\x07z"})["findings"][0][REFUTER_KEY]
    line = refuter_line(ann)
    assert "\x1b" not in line and "\x07" not in line
    assert "x" in line and "y" in line and "z" in line


def test_shown_field_strips_c0_and_c1_controls_but_keeps_ordinary_text():
    from skodun.triage import shown_field

    # Only the ESC (0x1B) and the C1 control (0x9B) are removed -- the
    # printable "[1A" that follows the ESC in a real cursor-control sequence
    # is ordinary text to this function and stays.
    assert shown_field("a\x1bb\x9bc") == "abc"
    # `one_line` replaces `\r` and `\n` independently, so `\r\n` becomes two
    # spaces, not one -- consistent with its own behaviour elsewhere.
    assert shown_field("line1\nline2\r\nline3") == "line1 line2  line3"
    assert shown_field(None) == ""


# --- same_provider_as_finder ----------------------------------------------

def test_same_provider_as_finder_is_read_off_the_pass_and_defended():
    assert refuter_same_provider_as_finder(GOOD) is False
    assert refuter_same_provider_as_finder(
        dict(GOOD, extra_passes={"refuter": {"same_provider_as_finder": True}})) is True
    for extras in [None, "x", 5, [], {"refuter": None}, {"refuter": "x"},
                   {"refuter": {}},
                   {"refuter": {"same_provider_as_finder": "true"}},
                   {"refuter": {"same_provider_as_finder": 1}}]:
        assert refuter_same_provider_as_finder(
            dict(GOOD, extra_passes=extras)) is False, extras


def test_same_provider_as_finder_never_raises_on_a_non_dict_review():
    assert refuter_same_provider_as_finder("not a review") is False
    assert refuter_same_provider_as_finder(None) is False


# --- the annotation channel is authenticated ------------------------------
#
# Task 8 hardened `merge_refuter_pass`/`skipped_refuter_pass` to strip any
# `refuter` key a finder shipped on its own finding. That covers the paths
# where a refuter pass was SCHEDULED. Where `refuter_decision` declines -- the
# `SKODUN_REFUTER_PASS` kill switch, a mode other than `now`, an untrustworthy
# finder -- neither merge runs, and the forged key rides into the artifact
# verbatim (the adapter's payload validator checks the required keys and does
# not remove extra ones). `refuter_pass_ran` is the consuming end's answer.

FORGED = _annotated(provider="openai", model="a-model-that-never-ran", _meta=None)


def test_a_forged_annotation_with_no_pass_behind_it_cannot_be_adopted(tmp_path):
    st = Store.open(tmp_path / "s.db")
    # Everything the happy path needs EXCEPT a pass: verdict `refuted`, a
    # reasoning that clears the audit floor on its own, a plausible provider.
    assert FORGED["findings"][0][REFUTER_KEY]["verdict"] == ADOPTABLE_VERDICT
    assert "extra_passes" not in FORGED
    with pytest.raises(TriageError) as exc:
        adopt_refuter(st, FORGED, 0, now="2026-07-27T10:00:00Z")
    assert "no refuter pass ran" in str(exc.value)
    assert _triaged(st) == {}


@pytest.mark.parametrize("extras", [
    {},                                            # no pass ran at all
    {"security": {"ran": True}},                   # a DIFFERENT pass ran
    {"refuter": {"ran": False, "status": "skipped"}},
    {"refuter": {"ran": False, "status": "failed"}},
    {"refuter": {"status": "ran"}},                # no `ran` key
    {"refuter": {"ran": "true"}},                  # truthy, not True
    {"refuter": {"ran": 1}},
    {"refuter": "ran"},
    {"refuter": None},
])
def test_every_shape_that_is_not_a_pass_that_ran_refuses_adoption(tmp_path, extras):
    st = Store.open(tmp_path / "s.db")
    art = dict(FORGED, extra_passes=extras)
    assert refuter_pass_ran(art) is False
    with pytest.raises(TriageError):
        adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    assert _triaged(st) == {}


def test_a_degraded_refuter_pass_still_ran_and_its_verdicts_are_adoptable(tmp_path):
    # `degraded` describes the RUN, not the verdict, and the merge wrote these
    # annotations itself. Refusing here would be a policy the brief does not
    # ask for and the pass does not imply.
    st = Store.open(tmp_path / "s.db")
    art = _annotated(_meta=dict(RAN, status="degraded", degraded=True))
    adopt_refuter(st, art, 0, now="2026-07-27T10:00:00Z")
    assert len(_triaged(st)) == 1


def test_refuter_pass_ran_never_raises_on_junk():
    for review in [None, "x", 5, [], {}, {"extra_passes": "x"},
                   {"extra_passes": ["refuter"]}]:
        assert refuter_pass_ran(review) is False, review


# ---------------------------------------------------------------------------
# reopen: an audited un-dismissal, appended to the same event stream
# ---------------------------------------------------------------------------
#
# A dismissal is not a verdict for all time -- a fix regresses, a base moves,
# a reason turns out to have been wrong. Reopening is therefore a first-class,
# AUDITED decision recorded with the same floor `validate_reason` puts on a
# dismissal, and it is APPENDED: nothing in the ledger is ever edited or
# deleted, so the history of a finding reads dismiss -> reopen -> dismiss and
# every reason survives.

GOOD_REASON = "line numbers drift; verified the handler checks None on entry"
REOPEN_REASON = "the null check was removed again in the refactor; it crashes"


def _lkey(review=None) -> str:
    from skodun.textnorm import ledger_key

    review = review or GOOD
    f = review["findings"][0]
    return ledger_key(review["branch"], review["base_sha"],
                      finding_key(f["file"], f["title"]))


def _dismissed(st, review=None):
    review = review or GOOD
    return dismiss(st, review, 0, GOOD_REASON, now="2026-07-27T10:00:00Z")


def test_reopen_records_an_audited_reopen_and_reopens_the_finding(tmp_path):
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    assert open_findings(GOOD, _triaged(st)) == []

    rec = reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")

    assert rec["finding_key"] == finding_key("a.py", "NPE")
    assert rec["ledger_key"] == _lkey()
    assert rec["reason"] == REOPEN_REASON
    assert rec["at"] == "2026-07-27T12:00:00Z"
    # The gate's view: the finding is open again.
    assert _triaged(st) == {}
    assert len(open_findings(GOOD, _triaged(st))) == 1
    assert [h["event"] for h in st.triage_history(_lkey())] == ["dismiss", "reopen"]


def test_reopen_preserves_the_dismissal_reason_it_overturns(tmp_path):
    """Append-only is the point: the reason the finding was dismissed with must
    still be readable after it is reopened, or the ledger cannot be audited."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    history = st.triage_history(_lkey())
    assert [h["reason"] for h in history] == [GOOD_REASON, REOPEN_REASON]


@pytest.mark.parametrize("reason", ["fp", "false positive", "wontfix", "",
                                    "   ", "too short"])
def test_reopen_refuses_an_unauditable_reason_and_writes_nothing(tmp_path, reason):
    """The SAME floor a dismissal clears -- `validate_reason`, unchanged. A
    reopen nobody can audit later is indistinguishable from re-opening a
    finding out of spite, and it moves the gate from 0 to 1."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    with pytest.raises(TriageError):
        reopen(st, GOOD, 0, reason, now="2026-07-27T12:00:00Z")
    assert set(_triaged(st)) == {finding_key("a.py", "NPE")}, "still dismissed"
    assert [h["event"] for h in st.triage_history(_lkey())] == ["dismiss"]


def test_reopen_refuses_a_finding_that_is_not_dismissed(tmp_path):
    """There is nothing to overturn, and an event saying otherwise would be a
    reopen with no dismissal behind it in the audit stream."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError) as e:
        reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    assert "not dismissed" in str(e.value).lower()
    assert st.triage_history(_lkey()) == []


def test_reopen_refuses_a_finding_that_is_already_reopened(tmp_path):
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    with pytest.raises(TriageError):
        reopen(st, GOOD, 0, "and again, for a second unrelated reason entirely",
               now="2026-07-27T13:00:00Z")
    assert [h["event"] for h in st.triage_history(_lkey())] == ["dismiss", "reopen"]


def test_a_dismissal_after_a_reopen_is_an_ordinary_dismissal(tmp_path):
    """No special "re-dismiss" verb: the stream is dismiss/reopen and `dismiss`
    is the same function it always was."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    dismiss(st, GOOD, 0, "fixed in the follow-up commit; the guard is back",
            now="2026-07-27T14:00:00Z")
    assert open_findings(GOOD, _triaged(st)) == []
    assert [h["event"] for h in st.triage_history(_lkey())] == \
        ["dismiss", "reopen", "dismiss"]


@pytest.mark.parametrize("index", [1, -1, 99, True, "0", None, 1.0])
def test_reopen_raises_finding_not_found_for_an_unresolvable_index(tmp_path, index):
    """`FindingNotFound`, not `TriageError`, because the CLI maps the two to
    DIFFERENT exit codes: 2 for "the thing you named does not exist", 1 for "it
    exists and the reopen was refused"."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    with pytest.raises(FindingNotFound):
        reopen(st, GOOD, index, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    assert [h["event"] for h in st.triage_history(_lkey())] == ["dismiss"]


def test_reopen_reports_a_bad_index_before_it_judges_the_reason(tmp_path):
    """Both are wrong; the honest answer is the one about the thing that does
    not exist, because a reason cannot be refused on behalf of no finding."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    with pytest.raises(FindingNotFound):
        reopen(st, GOOD, 99, "fp", now="2026-07-27T12:00:00Z")


def test_reopen_rejects_a_corrupt_artifact_before_writing(tmp_path):
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    bad = dict(GOOD, findings_total=2)
    with pytest.raises(ArtifactError):
        reopen(st, bad, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    assert st.triage_history(_lkey()) == []


def test_reopen_rejects_a_non_canonical_timestamp(tmp_path):
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    _dismissed(st)
    with pytest.raises(ValueError):
        reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27 12:00:00")
    assert [h["event"] for h in st.triage_history(_lkey())] == ["dismiss"]


def test_reopen_is_scoped_to_the_reviews_own_branch_and_base(tmp_path):
    """A dismissal recorded against another base must not be reopenable from
    here, and vice versa: the ledger key carries branch and base_sha."""
    from skodun.triage import reopen

    st = Store.open(tmp_path / "s.db")
    other = dict(GOOD, base_sha="0" * 40)
    _dismissed(st, other)                       # dismissed under a DIFFERENT base
    with pytest.raises(TriageError):
        reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    assert st.triage_history(_lkey(other))[-1]["event"] == "dismiss"


# --- the status token the listing prints ----------------------------------

def test_status_token_renders_open_dismissed_and_reopened(tmp_path):
    from skodun.triage import reopen, status_token

    st = Store.open(tmp_path / "s.db")
    fkey = finding_key("a.py", "NPE")

    def token():
        return status_token(st.triage_state(GOOD["branch"], GOOD["base_sha"]).get(fkey))

    assert token() == "OPEN"
    _dismissed(st)
    assert token() == "DISMISSED 2026-07-27T10:00:00Z"
    reopen(st, GOOD, 0, REOPEN_REASON, now="2026-07-27T12:00:00Z")
    # BOTH timestamps: the reopen is the state, the dismissal is its history.
    assert token() == "REOPENED 2026-07-27T12:00:00Z, dismissed 2026-07-27T10:00:00Z"
    dismiss(st, GOOD, 0, "fixed in the follow-up commit; the guard is back",
            now="2026-07-27T14:00:00Z")
    assert token() == "DISMISSED 2026-07-27T14:00:00Z, reopened 2026-07-27T12:00:00Z"


def test_status_token_never_raises_and_never_trusts_a_stored_timestamp():
    """A seeded legacy `dismissed_at` is whatever the archive contained, so the
    listing treats it as untrusted display text like every other field on the
    line: no raw ESC, no forged second row, bounded length."""
    from skodun.triage import status_token

    assert status_token(None) == "OPEN"
    assert status_token({}) == "OPEN"
    assert status_token({"event": "deleted"}) == "OPEN"
    assert status_token({"event": "dismiss", "dismissed_at": None}) == "DISMISSED"
    hostile = status_token({"event": "dismiss",
                            "dismissed_at": "2026\x1b[2K\n(OPEN) " + "z" * 5000})
    assert "\x1b" not in hostile and "\n" not in hostile
    assert len(hostile) < 200, len(hostile)
    for junk in [{"event": 5}, {"event": None}, [], "x", 7,
                 {"event": "reopen", "reopened_at": ["x"], "dismissed_at": {}}]:
        assert isinstance(status_token(junk), str), junk
