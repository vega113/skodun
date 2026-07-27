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
    MIN_REASON_CHARS,
    PLACEHOLDER_REASONS,
    ArtifactError,
    TriageError,
    dismiss,
    load_valid_artifact,
    open_findings,
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
