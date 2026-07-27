"""Reason validation, ledger dismissal, and fail-closed artifact validation.

Parity with the oracle (`grok_review_triage.py`) matters here because the
whole point of the triage ledger is that a dismissal or an artifact-shape
rule silently drifting from the legacy tool would either resurrect an
already-litigated finding or let a corrupt artifact satisfy the gate.
`test_placeholder_set_matches_legacy` and `test_validate_reason_parity_with_legacy_module`
load the *actual* oracle module from `$SKODUN_ORACLE_DIR` and assert
agreement directly; they skip (not xfail, not silently pass) when the oracle
checkout is absent.
"""

import importlib.util
import sys

import pytest

from skodun.store import Store
from skodun.textnorm import finding_key
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
    with pytest.raises(TriageError):
        validate_reason("  FALSE   POSITIVE  ")
    with pytest.raises(TriageError):
        validate_reason("Wontfix")


def test_reason_length_boundary_exact():
    # Exactly MIN_REASON_CHARS normalized chars must be accepted; one under
    # must be rejected. Both strings are plain lowercase ASCII already, so
    # normalization does not change their length.
    exactly_20 = "a" * 20
    assert len(exactly_20) == MIN_REASON_CHARS
    validate_reason(exactly_20)  # must not raise
    with pytest.raises(TriageError):
        validate_reason("a" * (MIN_REASON_CHARS - 1))


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


def test_artifact_missing_findings_defaults_to_empty_list():
    # PARITY: the oracle treats a missing/None `findings` as an empty list,
    # not corruption (grok_review_triage.py:196-202) -- an artifact that
    # simply has nothing to report must not be indistinguishable from a
    # truncated one.
    rec = dict(id="r1", branch="feat", base_sha="s" * 40)
    out = load_valid_artifact(rec)
    assert out is rec
    assert open_findings(rec, {}) == []


def test_artifact_missing_findings_total_skips_total_check():
    # PARITY: the oracle only validates findings_total when the artifact
    # actually asserts one (grok_review_triage.py:216-229); a well-formed
    # artifact that omits the count entirely must not be rejected just
    # because there is nothing to compare against len(findings).
    rec = dict(GOOD)
    del rec["findings_total"]
    out = load_valid_artifact(rec)
    assert out["findings"] == GOOD["findings"]


def test_artifact_null_findings_total_also_skips_check():
    rec = dict(GOOD, findings_total=None)
    load_valid_artifact(rec)  # must not raise


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


def test_load_valid_artifact_parity_with_legacy_load_review(tmp_path):
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    import json
    legacy = _load_legacy()

    cases = [
        GOOD,
        dict(GOOD, findings_total=2),
        dict(GOOD, findings="oops"),
        dict(GOOD, findings=[1]),
        dict(GOOD, findings_total=True),
        dict(GOOD, findings_total=1.0),
        dict(id="r2", branch="feat", base_sha="s" * 40),  # missing findings entirely
        dict(GOOD, findings_total=None),
    ]
    for i, rec in enumerate(cases):
        skodun_ok = True
        try:
            load_valid_artifact(rec)
        except ArtifactError:
            skodun_ok = False

        # Legacy's load_review reads from a file on disk keyed by review id;
        # give it the same shape via a temp artifact file.
        review_id = f"case{i}"
        (tmp_path / f"{review_id}.json").write_text(json.dumps(rec), encoding="utf-8")
        legacy_ok = True
        try:
            legacy.load_review(str(tmp_path), review_id)
        except ValueError:
            legacy_ok = False

        assert skodun_ok == legacy_ok, f"disagreement on case {i}: {rec!r}"
