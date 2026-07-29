"""Shadow comparison against the legacy `.grok-reviews` archive.

`compare` is an observational join, not a gate: the tests below pin its one
rule (`match` is trustworthy-agreement plus clean-vs-dirty agreement, and
nothing finer), that the union of hashes -- not just skodun's -- is what gets
iterated, that legacy corruption is tolerated exactly like the importer
tolerates it, and that "newest by reviewed_at" is what wins on each side.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import skodun
from skodun.legacy_import import import_legacy
from skodun.shadow import _int, compare, effective_trustworthy
from skodun.store import Store
from tests.test_store import REC

# `.../src`, so a subprocess started with `python -m skodun` imports the same
# package pytest is testing. In-process the ini's `pythonpath` handles this; a
# subprocess inherits nothing of it.
_SRC = str(Path(skodun.__file__).resolve().parents[1])


def _subprocess_env(db: Path) -> dict:
    env = dict(os.environ)          # carries the autouse SKODUN_CONFIG pin
    env["SKODUN_DB"] = str(db)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def _write_index(d, *rows):
    d.mkdir(exist_ok=True)
    (d / "index.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# The brief's three cases, verbatim
# ---------------------------------------------------------------------------

def test_compare_matches_on_trust_and_cleanliness(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)  # trustworthy, clean, diff_hash = "d"*40
    d = tmp_path / ".grok-reviews"; d.mkdir()
    legacy = dict(id="loop_9", diff_hash="d"*40, parse_ok=True, degraded=False,
                  diff_truncated=False, trustworthy=True, findings_total=0,
                  severity={"high": 0, "medium": 0, "low": 0}, branch="b",
                  reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    out = compare(st, d, None).comparisons
    assert len(out) == 1 and out[0].match is True


def test_mismatch_when_legacy_found_findings(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"; d.mkdir()
    legacy = dict(id="loop_9", diff_hash="d"*40, trustworthy=True,
                  findings_total=2, severity={"high": 1, "medium": 1, "low": 0},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    out = compare(st, d, None).comparisons
    assert out[0].match is False and out[0].deltas["findings_total"] == (0, 2)


def test_union_surfaces_one_sided_hashes_and_newest_row_wins(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                                   # skodun-only: "d"*40
    d = tmp_path / ".grok-reviews"; d.mkdir()
    older = dict(id="loop_1", diff_hash="e"*40, trustworthy=False, findings_total=0,
                 severity={"high":0,"medium":0,"low":0}, branch="b",
                 reviewed_at="2026-07-01T00:00:00Z")
    newer = dict(older, id="loop_2", trustworthy=True,
                 reviewed_at="2026-07-02T00:00:00Z")
    (d / "index.jsonl").write_text(
        json.dumps(older) + "\n" + json.dumps(newer) + "\n", encoding="utf-8")
    out = {c.diff_hash: c for c in compare(st, d, None).comparisons}
    assert out["d"*40].legacy is None and out["d"*40].match is False  # skodun-only
    assert out["e"*40].skodun is None                                  # legacy-only
    assert out["e"*40].legacy["id"] == "loop_2"                        # newest wins


# ---------------------------------------------------------------------------
# Corruption and absence must not crash the comparison
# ---------------------------------------------------------------------------

def test_corrupt_line_in_index_is_tolerated_not_fatal(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"; d.mkdir()
    good = dict(id="loop_1", diff_hash="e"*40, trustworthy=True, findings_total=0,
                severity={"high": 0, "medium": 0, "low": 0}, branch="b",
                reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_bytes(
        json.dumps(good).encode() + b"\n"
        + b'{"id": "truncated", "diff_hash": "f"'    # truncated final line
    )
    out = {c.diff_hash: c for c in compare(st, d, None).comparisons}
    # the corrupt line contributed no comparison at all -- only the two real
    # hashes (skodun's "d"*40 and legacy's "e"*40) appear
    assert set(out) == {"d"*40, "e"*40}
    assert out["e"*40].legacy["id"] == "loop_1"


def test_missing_grok_reviews_directory_does_not_crash(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    out = compare(st, tmp_path / "nope-no-such-dir", None).comparisons
    assert len(out) == 1
    assert out[0].diff_hash == "d"*40
    assert out[0].legacy is None and out[0].skodun is not None
    assert out[0].match is False


# ---------------------------------------------------------------------------
# The exact, single definition of `match`
# ---------------------------------------------------------------------------

def test_both_sides_untrustworthy_is_a_match(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "trustworthy": False, "degraded": True,
                    "status": "degraded"})
    d = tmp_path / ".grok-reviews"
    legacy = dict(id="loop_1", diff_hash="d"*40, trustworthy=False,
                  findings_total=0, severity={"high": 0, "medium": 0, "low": 0},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy)
    out = compare(st, d, None).comparisons
    assert len(out) == 1
    assert out[0].match is True, "both sides agree: neither is trustworthy"


def test_one_trustworthy_one_not_is_a_mismatch(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)  # trustworthy=True
    d = tmp_path / ".grok-reviews"
    legacy = dict(id="loop_1", diff_hash="d"*40, trustworthy=False,
                  findings_total=0, severity={"high": 0, "medium": 0, "low": 0},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy)
    out = compare(st, d, None).comparisons
    assert out[0].match is False


def test_equal_cleanliness_different_severity_tallies_is_still_a_match(tmp_path):
    """Two independent model runs are not expected to agree on exact counts.

    Both sides are trustworthy and both are DIRTY (findings_total > 0), so
    they must match on the one thing `match` cares about -- even though the
    severity tallies (visible only in `deltas`) disagree substantially.

    The legacy row spells its axes out: a recorded `trustworthy: true` alone
    does not make a row trustworthy (the axes decide, see the precedence tests
    above), and this test is about the COUNTS, so its trust has to be real.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "findings_total": 3,
                    "severity": {"high": 1, "medium": 2, "low": 0}})
    d = tmp_path / ".grok-reviews"
    legacy = dict(id="loop_1", diff_hash="d"*40, trustworthy=True,
                  parse_ok=True, degraded=False, diff_truncated=False,
                  findings_total=7, severity={"high": 0, "medium": 1, "low": 6},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy)
    out = compare(st, d, None).comparisons
    assert len(out) == 1
    assert out[0].match is True
    assert out[0].deltas["findings_total"] == (3, 7)
    assert out[0].deltas["sev_high"] == (1, 0)
    assert out[0].deltas["sev_medium"] == (2, 1)
    assert out[0].deltas["sev_low"] == (0, 6)


# ---------------------------------------------------------------------------
# `effective_trustworthy`: the importer's precedence, not a second reading
#
# This is the rule that decides every `t`/`f` printed and every `match`
# computed, and it has two halves that are easy to get backwards. A recorded
# `trustworthy` field can only ever DENY trust; trust itself is derived from
# the axes. Reading a recorded `true` as trust would report agreement for a
# row the importer stores as untrustworthy -- a FALSE MATCH, the one direction
# this module must never get wrong. Reading an ABSENT field as `false` is the
# opposite error, and it is the one that once turned a ~2% audit failure rate
# into 65%: the field was added late, and a large fraction of any real archive
# predates it.
# ---------------------------------------------------------------------------

def _legacy_row(**over):
    """A legacy index row with clean axes and NO `trustworthy` field at all."""
    row = dict(id="loop_1", diff_hash="d"*40, branch="b", base_sha="s"*40,
               parse_ok=True, degraded=False, diff_truncated=False,
               findings_total=0, severity={"high": 0, "medium": 0, "low": 0},
               reviewed_at="2026-07-01T00:00:00Z")
    return {**row, **over}


def test_absent_trustworthy_field_derives_trust_from_the_axes():
    """The 39%-of-a-real-archive case: no recorded verdict, clean axes."""
    row = _legacy_row()
    assert "trustworthy" not in row, "the point of this row is the ABSENT field"
    assert effective_trustworthy(row) is True


def test_absent_trustworthy_field_with_null_still_derives_from_the_axes():
    assert effective_trustworthy(_legacy_row(trustworthy=None)) is True


@pytest.mark.parametrize("axis, denying", [
    ("parse_ok", False), ("degraded", True), ("diff_truncated", True)])
def test_absent_trustworthy_field_each_axis_can_deny_alone(axis, denying):
    assert effective_trustworthy(_legacy_row(**{axis: denying})) is False


def test_recorded_false_denies_trust_even_with_clean_axes():
    assert effective_trustworthy(_legacy_row(trustworthy=False)) is False


@pytest.mark.parametrize("recorded", [True, None])
def test_recorded_verdict_cannot_grant_trust_against_a_denying_axis(recorded):
    """A recorded verdict DENIES or defers -- it never overrides the axes.

    `Store.save_review` recomputes `trustworthy` from the axes on every write,
    so this row is STORED untrustworthy. Reading it as trustworthy here would
    make `compare` report `match=True` for a pair that actually disagrees.
    """
    row = _legacy_row(trustworthy=recorded, degraded=True)
    assert effective_trustworthy(row) is False


@pytest.mark.parametrize("recorded", [1, 0, "true", "false", "", [], {}])
def test_a_non_bool_recorded_verdict_is_never_trust(recorded):
    """`is not True`, not truthiness: `1` and `"true"` are denials, not trust.

    The axes are clean on every row here, so anything that read the recorded
    field by truthiness would let `1` and `"true"` through as trust.
    """
    assert effective_trustworthy(_legacy_row(trustworthy=recorded)) is False


@pytest.mark.parametrize("axes", [
    {"parse_ok": 1}, {"parse_ok": "true"}, {"degraded": 0}, {"degraded": "false"},
    {"diff_truncated": 0}, {"diff_truncated": "no"}])
def test_a_non_bool_axis_reads_at_its_unsafe_value_and_denies(axes):
    """Legacy JSON is untrusted: a non-`bool` axis denies trust, every time."""
    assert effective_trustworthy(_legacy_row(**axes)) is False


def test_a_missing_row_is_not_trustworthy():
    assert effective_trustworthy(None) is False
    assert effective_trustworthy({}) is False


# The rows above, checked against what the IMPORTER actually stores for them.
# Two readings of one rule drift; this pins them together.
_PRECEDENCE_ROWS = [
    {},                                          # field absent, clean axes
    {"trustworthy": None},                       # explicitly null
    {"trustworthy": True},                       # recorded, and supported
    {"trustworthy": False},                      # recorded denial
    {"trustworthy": 1},                          # non-bool: a denial, not trust
    {"trustworthy": "true"},
    {"parse_ok": False},
    {"degraded": True},
    {"diff_truncated": True},
    {"trustworthy": True, "degraded": True},     # claims trust the axes deny
    {"trustworthy": True, "parse_ok": False},
    {"parse_ok": 1},                             # non-bool axis
    {"degraded": 0},
]


@pytest.mark.parametrize("over", _PRECEDENCE_ROWS)
def test_effective_trustworthy_agrees_with_what_the_importer_stores(tmp_path, over):
    """The verdict shown must be the verdict the importer would persist.

    Shadow mode's whole claim is "skodun and the legacy archive agree on this
    content". If this helper read a legacy row differently from the importer,
    the table would be comparing skodun against a legacy verdict that exists
    nowhere -- and the disagreement would show up as agreement.

    A valid artifact is written beside each row so that the importer's OTHER
    demotion rule (no full artifact, no trust) cannot be what decides the
    outcome here; the axes/recorded precedence is the only variable.
    """
    row = _legacy_row(**over)
    archive = tmp_path / ".grok-reviews"
    _write_index(archive, row)
    (archive / "loop_1.json").write_text(json.dumps(dict(
        id="loop_1", branch="b", base_sha="s"*40, diff_hash="d"*40,
        findings=[], findings_total=0)), encoding="utf-8")

    st = Store.open(tmp_path / "s.db")
    assert import_legacy(st, archive).reviews == 1
    stored = st.get_review("loop_1")

    assert effective_trustworthy(row) is (stored["trustworthy"] is True), (
        f"row {over!r}: shadow says {effective_trustworthy(row)}, "
        f"the importer stored {stored['trustworthy']!r}")


def test_the_precedence_table_exercises_both_outcomes(tmp_path):
    """Guards the parametrisation above from passing by being all-one-answer."""
    assert {effective_trustworthy(_legacy_row(**o)) for o in _PRECEDENCE_ROWS} == {
        True, False}


def test_recorded_true_against_a_denying_axis_is_a_mismatch_not_a_match(tmp_path):
    """Finding 1, end to end: the false agreement must not be reachable.

    skodun's row is genuinely trustworthy. The legacy row CLAIMS trust while
    carrying `degraded: true`, so the importer would store it untrustworthy.
    The two disagree, and `compare` has to say so.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                                   # trustworthy, clean
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True, degraded=True))
    out = compare(st, d, None).comparisons
    assert len(out) == 1
    assert out[0].match is False


# ---------------------------------------------------------------------------
# Counts off untrusted data
# ---------------------------------------------------------------------------

def test_bool_counts_are_rejected_rather_than_counted_as_one(tmp_path):
    """`isinstance(True, int)` is True, so `bool` needs an explicit guard.

    Without it a legacy row carrying `findings_total: true` would read as ONE
    finding: the row would count as dirty, and a clean skodun row beside it
    would be reported as a MISMATCH that never happened.
    """
    assert _int(True) == 0 and _int(False) == 0
    assert _int(3) == 3 and _int("3") == 0 and _int(None) == 0

    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                                   # clean: 0 findings
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True, findings_total=True,
                                severity={"high": True, "medium": 0, "low": 0}))
    out = compare(st, d, None).comparisons
    assert out[0].deltas["findings_total"] == (0, 0)
    assert out[0].deltas["sev_high"] == (0, 0)
    assert out[0].match is True


# ---------------------------------------------------------------------------
# The single-hash filter form
# ---------------------------------------------------------------------------

def test_single_hash_filter_returns_only_that_hash(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                       # "d"*40
    st.save_review({**REC, "id": "other", "diff_hash": "c"*40})
    d = tmp_path / ".grok-reviews"
    legacy_c = dict(id="loop_1", diff_hash="c"*40, trustworthy=True,
                    findings_total=0, severity={"high": 0, "medium": 0, "low": 0},
                    branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy_c)

    out = compare(st, d, "d"*40).comparisons
    assert len(out) == 1
    assert out[0].diff_hash == "d"*40
    assert out[0].legacy is None            # legacy never reviewed this one


def test_single_hash_filter_absent_on_both_sides_is_empty(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    out = compare(st, tmp_path / ".grok-reviews", "z"*40).comparisons
    assert out == []


# ---------------------------------------------------------------------------
# `since`: bounds both sides to the same window. Rows whose stored
# `reviewed_at` cannot be read in the canonical form are excluded from a
# windowed compare and counted -- never crashed on, never silently included.
# ---------------------------------------------------------------------------

def test_compare_returns_a_result_object_with_comparisons_and_excluded_count(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    result = compare(st, tmp_path / ".grok-reviews", None)
    assert isinstance(result.comparisons, list)
    assert result.comparisons[0].diff_hash == "d"*40
    assert result.excluded_unparseable == 0


def test_since_excludes_a_legacy_only_row_older_than_since(tmp_path):
    """The brief's case 1: a legacy-only row strictly before `since` vanishes."""
    st = Store.open(tmp_path / "s.db")
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(reviewed_at="2026-07-01T00:00:00Z"))
    result = compare(st, d, None, since="2026-07-15T00:00:00Z")
    assert result.comparisons == []
    assert result.excluded_unparseable == 0


def test_since_malformed_offset_is_a_usage_error(tmp_path):
    """The brief's case 2, at the `compare` boundary: an offset is not the
    canonical form, so `since` is rejected rather than silently misread.
    The message reuses `store._require_ts`'s own wording, naming the format.
    """
    st = Store.open(tmp_path / "s.db")
    d = tmp_path / ".grok-reviews"
    with pytest.raises(ValueError, match="2026-07-28T12:00:00Z"):
        compare(st, d, None, since="2026-07-28T00:00:00+02:00")


@pytest.mark.parametrize("bad_since", [
    "", "   ", "2026-07-28", "not-a-timestamp", "2026-7-8T1:2:3Z",
    "2026-07-28T00:00:00", "2026-07-28 00:00:00Z",
    "２０２６-01-01T00:00:00Z", "٢٠٢٦-01-01T00:00:00Z"])
def test_since_any_non_canonical_value_is_a_usage_error(tmp_path, bad_since):
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):
        compare(st, tmp_path / ".grok-reviews", None, since=bad_since)


def test_a_unicode_digit_since_cannot_silently_empty_the_window(tmp_path):
    """The failure this guards is quiet, not loud, which is why it needs its
    own test rather than one more parametrize case.

    Python's `\\d` is Unicode-aware and `time.strptime` accepts Unicode decimal
    digits, so `２０２６-01-01T00:00:00Z` used to pass the canonical check. Those
    codepoints sort ABOVE every ASCII digit, so the window then matched nothing
    and the run reported a clean `0 compared, 0 unparseable rows excluded` --
    an answer indistinguishable from "the archive really is empty in that
    window". A usage error is the only safe outcome.
    """
    st = Store.open(tmp_path / "s.db")
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(reviewed_at="2026-07-20T00:00:00Z"))

    ascii_since = compare(st, d, None, since="2026-01-01T00:00:00Z")
    assert len(ascii_since.comparisons) == 1        # the row is genuinely in window

    with pytest.raises(ValueError):
        compare(st, d, None, since="２０２６-01-01T00:00:00Z")


def test_since_excludes_and_counts_a_malformed_stored_timestamp(tmp_path):
    """The brief's case 3: a row that cannot be windowed is excluded, not
    crashed on and not silently included -- and the exclusion is counted."""
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "reviewed_at": ""})               # skodun: blank ts
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(diff_hash="e"*40, reviewed_at="not-a-timestamp"))
    result = compare(st, d, None, since="2000-01-01T00:00:00Z")
    assert result.comparisons == []
    assert result.excluded_unparseable == 2


@pytest.mark.parametrize("bad_ts", ["", None, 42, "2026-7-8T1:2:3Z"])
def test_since_excludes_rows_with_unparseable_reviewed_at_on_skodun_side(
        tmp_path, bad_ts):
    """Empty string, missing entirely, a non-string, and a canonical-looking
    value of the wrong width all count as unparseable -- none of them may be
    guessed at or silently kept in the window."""
    st = Store.open(tmp_path / "s.db")
    rec = {k: v for k, v in REC.items() if k != "reviewed_at"}
    if bad_ts is not None:
        rec["reviewed_at"] = bad_ts
    st.save_review(rec)
    result = compare(st, tmp_path / "nope-no-such-dir", None,
                      since="2000-01-01T00:00:00Z")
    assert result.comparisons == []
    assert result.excluded_unparseable == 1


def test_since_in_the_future_excludes_everything_and_counts_nothing(tmp_path):
    """Every stored timestamp is canonical here, just outside the window --
    that is ordinary windowing, not an unparseable-timestamp exclusion."""
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(diff_hash="e"*40))
    result = compare(st, d, None, since="2099-01-01T00:00:00Z")
    assert result.comparisons == []
    assert result.excluded_unparseable == 0


def test_since_older_than_every_row_matches_the_unwindowed_result(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(diff_hash="e"*40))
    unwindowed = compare(st, d, None)
    windowed = compare(st, d, None, since="2000-01-01T00:00:00Z")
    assert ([c.diff_hash for c in windowed.comparisons]
            == [c.diff_hash for c in unwindowed.comparisons])
    assert windowed.excluded_unparseable == 0


def test_since_windows_both_sides_independently(tmp_path):
    """A hash present on both sides but with one side's newest row outside
    the window must not silently keep the excluded side's verdict."""
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "diff_hash": "a"*40, "reviewed_at": "2026-08-01T00:00:00Z"})
    d = tmp_path / ".grok-reviews"
    _write_index(
        d,
        _legacy_row(diff_hash="a"*40, reviewed_at="2026-01-01T00:00:00Z"),  # too old
        _legacy_row(diff_hash="b"*40, reviewed_at="2026-08-01T00:00:00Z"),  # in window
    )
    result = compare(st, d, None, since="2026-07-01T00:00:00Z")
    out = {c.diff_hash: c for c in result.comparisons}
    assert set(out) == {"a"*40, "b"*40}
    assert out["a"*40].legacy is None and out["a"*40].skodun is not None
    assert out["b"*40].skodun is None and out["b"*40].legacy is not None
    assert result.excluded_unparseable == 0


# ---------------------------------------------------------------------------
# CLI: shadow-compare, log, triage
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse" / "skodun.db"))


def test_cli_shadow_compare_summary_line_and_exit_0_on_mismatch(tmp_path, monkeypatch,
                                                                capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review(REC)                                    # trustworthy, clean
    st.save_review({**REC, "id": "r2", "diff_hash": "b"*40, "trustworthy": False,
                    "degraded": True, "status": "degraded"})
    d = tmp_path / ".grok-reviews"
    legacy_mismatch = dict(id="loop_1", diff_hash="d"*40, trustworthy=False,
                           findings_total=0, severity={"high": 0, "medium": 0, "low": 0},
                           branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy_mismatch)

    assert main(["shadow-compare", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "shadow: 2 compared, 0 matched, 1 skodun-only, 0 legacy-only" in out
    assert "MISMATCH" in out
    assert "SKODUN-ONLY" in out
    assert ("d"*40)[:12] in out


def test_cli_shadow_compare_exits_0_even_when_everything_mismatches(tmp_path,
                                                                    monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review(REC)  # trustworthy=True, clean
    d = tmp_path / ".grok-reviews"
    legacy = dict(id="loop_1", diff_hash="d"*40, trustworthy=False,
                  findings_total=5, severity={"high": 1, "medium": 1, "low": 3},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy)

    assert main(["shadow-compare", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "shadow: 1 compared, 0 matched, 0 skodun-only, 0 legacy-only" in out


def test_cli_shadow_compare_diff_hash_restricts_the_table(tmp_path, monkeypatch,
                                                          capsys):
    """`compare`'s `diff_hash` filter reaches the CLI, which is what it is for.

    It was specified in the plan's Task 17 interface and implemented, but the
    CLI always passed `None`, so no invocation could ever use it -- and the
    obvious question during a shadow run ("what did the two sides say about
    THIS change?") had no answer short of grepping the table.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review(REC)                                   # diff_hash = "d"*40
    st.save_review({**REC, "id": "r2", "diff_hash": "e"*40})
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True))

    assert main(["shadow-compare", "--dir", str(d), "--diff-hash", "d"*40]) == 0
    out = capsys.readouterr().out
    assert "shadow: 1 compared, 1 matched, 0 skodun-only, 0 legacy-only" in out
    assert ("e"*40)[:12] not in out

    # A hash on neither side is not an error and not an empty row: it is
    # nothing to report, and the summary says exactly that.
    assert main(["shadow-compare", "--dir", str(d), "--diff-hash", "z"*40]) == 0
    assert ("shadow: 0 compared, 0 matched, 0 skodun-only, 0 legacy-only"
            in capsys.readouterr().out)


def test_cli_shadow_compare_summary_always_carries_since_and_excluded_count(
        tmp_path, monkeypatch, capsys):
    """`since=` and the excluded count are part of the summary schema, present
    on every run -- including the ordinary, unwindowed one -- so a reader (or
    a script, per Task 14's runbook) never has to special-case their absence.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True))

    assert main(["shadow-compare", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "since=none, 0 unparseable-timestamp rows excluded" in out

    assert main(["shadow-compare", "--dir", str(d),
                 "--since", "2026-01-01T00:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "since=2026-01-01T00:00:00Z, 0 unparseable-timestamp rows excluded" in out


def test_cli_shadow_compare_since_windows_the_table_and_the_summary(
        tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review({**REC, "reviewed_at": "2026-08-01T00:00:00Z"})           # "d"*40
    st.save_review({**REC, "id": "old", "diff_hash": "e"*40,
                    "reviewed_at": "2026-01-01T00:00:00Z"})                  # too old
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True, reviewed_at="2026-08-01T00:00:00Z"))

    rc = main(["shadow-compare", "--dir", str(d), "--diff-hash", "d"*40,
               "--since", "2026-07-01T00:00:00Z"])
    assert rc == 0
    out = capsys.readouterr().out
    assert ("e"*40)[:12] not in out
    assert "shadow: 1 compared" in out
    assert "since=2026-07-01T00:00:00Z" in out


def test_cli_shadow_compare_since_in_the_future_excludes_everything_cleanly(
        tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(diff_hash="e"*40))

    assert main(["shadow-compare", "--dir", str(d),
                 "--since", "2099-01-01T00:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert ("shadow: 0 compared, 0 matched, 0 skodun-only, 0 legacy-only, "
            "since=2099-01-01T00:00:00Z, 0 unparseable-timestamp rows excluded"
            in out)


@pytest.mark.parametrize("bad_since", [
    "", "   ", "2026-07-28", "2026-07-28T00:00:00+02:00", "not-a-timestamp",
    "2026-7-8T1:2:3Z"])
def test_cli_shadow_compare_since_usage_error_exits_2_names_the_format(
        tmp_path, monkeypatch, capsys, bad_since):
    """Task 11's `providers --repo <nonexistent>` lesson, applied here: a
    malformed `--since` must never exit 0 and silently disable the window,
    and the refusal must be readable, not a traceback."""
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)

    rc = main(["shadow-compare", "--since", bad_since])
    assert rc == 2
    out = capsys.readouterr().out
    assert "--since" in out
    assert "2026-07-28T12:00:00Z" in out          # names the required format
    assert "shadow:" not in out                    # never got to a comparison


def test_python_dash_m_shadow_compare_since_usage_error_matches_console_script(
        tmp_path):
    db = tmp_path / "sub" / "s.db"
    Store.open(db).save_review(REC)
    p = subprocess.run(
        [sys.executable, "-m", "skodun", "shadow-compare", "--since", "bad"],
        capture_output=True, text=True, env=_subprocess_env(db))
    assert p.returncode == 2
    assert "--since" in p.stdout


def test_cli_shadow_compare_prints_no_deltas_for_an_agreeing_row(
        tmp_path, monkeypatch, capsys):
    """Deltas are for the rows where they can mean something.

    `match` is coarse on purpose -- two LLM runs over one diff are not expected
    to tally the same counts -- so the counts are printed exactly where a human
    needs them (a MISMATCH) and suppressed where they would be noise.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    d = tmp_path / ".grok-reviews"
    _write_index(d, _legacy_row(trustworthy=True))

    assert main(["shadow-compare", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "MATCH" in out and "deltas" not in out


def test_cli_shadow_compare_table_column_order_is_skodun_then_legacy(
        tmp_path, monkeypatch, capsys):
    """The two sides are interchangeable-looking; the ORDER is the meaning.

    Swapping the columns would turn "skodun found a high, legacy found three
    lows" into its exact opposite while every count on screen stayed the same,
    so the row is pinned verbatim -- hash, skodun, legacy, verdict.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(
        {**REC, "findings_total": 1, "status": "findings",
         "severity": {"high": 1, "medium": 0, "low": 0}})
    d = tmp_path / ".grok-reviews"
    _write_index(d, dict(id="loop_1", diff_hash="d"*40, trustworthy=False,
                         findings_total=3, branch="b",
                         severity={"high": 0, "medium": 0, "low": 3},
                         reviewed_at="2026-07-01T00:00:00Z"))

    assert main(["shadow-compare", "--dir", str(d)]) == 0
    lines = [x for x in capsys.readouterr().out.splitlines() if "|" in x]
    assert lines == [
        f"{('d'*40)[:12]} | t/1-0-0 | f/0-0-3 | MISMATCH",
        # The counts `match` deliberately ignores, printed for the human the
        # comparison is for -- and in the SAME order as the columns above.
        "             | deltas (skodun vs legacy): findings_total=1/3, "
        "sev_high=1/0, sev_medium=0/0, sev_low=0/3",
    ]


def test_cli_shadow_compare_missing_archive_says_so_and_still_exits_0(
        tmp_path, monkeypatch, capsys):
    """Exit 0 is the contract; SILENCE is not.

    With no archive found every skodun row prints as SKODUN-ONLY and the
    summary states that with full confidence. `--dir` defaults to a RELATIVE
    path, so the ordinary way to get here is running from the wrong directory
    -- and the output has to name the path it looked for, or a wrong working
    directory is indistinguishable from a genuine "legacy never saw this".
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    missing = tmp_path / "nope"
    assert main(["shadow-compare", "--dir", str(missing)]) == 0
    out = capsys.readouterr().out
    assert "shadow: 1 compared" in out          # still reports what it saw
    assert str(missing) in out                  # ...and names what it did not find
    assert "no archive directory" in out


def test_cli_shadow_compare_archive_without_an_index_says_so_too(
        tmp_path, monkeypatch, capsys):
    """Same wrong answer, one directory level down: the dir exists, empty."""
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    d = tmp_path / ".grok-reviews"; d.mkdir()
    assert main(["shadow-compare", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "index.jsonl" in out and str(d) in out
    assert "shadow: 1 compared" in out


def test_cli_log_prints_columns_newest_first_with_bang_on_untrustworthy(
        tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review({**REC, "id": "old", "reviewed_at": "2026-07-27T09:00:00Z",
                    "branch": "feat", "files_changed": ["a.py"],
                    "status": "clean", "summary": "all good"})
    st.save_review({**REC, "id": "new", "reviewed_at": "2026-07-27T12:00:00Z",
                    "branch": "feat", "files_changed": ["a.py", "b.py", "c.py"],
                    "trustworthy": False, "degraded": True, "status": "degraded",
                    "summary": "the reviewer stalled", "findings_total": 0,
                    "severity": {"high": 0, "medium": 0, "low": 0}})
    assert main(["log", "--branch", "feat"]) == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    # newest first
    assert "2026-07-27T12:00:00Z" in lines[0]
    assert "2026-07-27T09:00:00Z" in lines[1]
    # the untrustworthy row (newest) is marked; the trustworthy one is not
    assert lines[0].lstrip().startswith("!")
    assert not lines[1].lstrip().startswith("!")
    assert "feat" in lines[0] and "degraded" in lines[0]
    assert "the reviewer stalled" in lines[0]
    assert "3" in lines[0]     # three files_changed
    assert "1" in lines[1]     # one file_changed
    assert "all good" in lines[1]


def test_cli_log_limit(tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    for i in range(5):
        st.save_review({**REC, "id": f"r{i}", "reviewed_at": f"2026-07-27T1{i}:00:00Z"})
    assert main(["log", "-n", "2"]) == 0
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 2


@pytest.mark.parametrize("bad", ["-1", "0"])
def test_cli_log_rejects_a_non_positive_limit(tmp_path, monkeypatch, capsys, bad):
    """`-n` becomes SQLite's LIMIT, where a NEGATIVE value means UNLIMITED.

    So `log -n -1` would dump the entire store while reading like a request
    for fewer rows than the default -- the opposite of what was asked for, on
    output nobody re-counts.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    for i in range(3):
        st.save_review({**REC, "id": f"r{i}", "reviewed_at": f"2026-07-27T1{i}:00:00Z"})
    assert main(["log", "-n", bad]) == 2
    out = capsys.readouterr().out
    assert "positive" in out
    assert "2026-07-27" not in out, "a rejected limit must print no rows at all"


def test_cli_log_flattens_newlines_so_a_summary_cannot_forge_a_row(
        tmp_path, monkeypatch, capsys):
    """`log` is one line per review, and a summary is reviewer-authored text.

    A summary carrying a newline would print as a second line indistinguishable
    from a real review -- a reviewer (or a hand-edited record) could spell out
    a clean row for a review that does not exist. Both `\\n` and `\\r` are
    flattened, and no content is dropped in the process.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(
        {**REC, "summary": "real summary\n2026-07-27T10:00:00Z | b | 0 | 0-0-0 "
                           "| clean | forged\rtail"})
    assert main(["log"]) == 0
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 1, lines
    for fragment in ("real summary", "forged", "tail"):
        assert fragment in lines[0]   # flattened onto one line, never truncated


# --- triage --------------------------------------------------------------

def _artifact_with_one_finding(review_id, branch, base_sha, diff_hash):
    return dict(id=review_id, branch=branch, base_sha=base_sha, diff_hash=diff_hash,
                reviewed_at="2026-07-27T10:00:00Z", head="h"*20, base_ref="origin/main",
                context_hash="", mode="now", model="m", adapter="grok",
                status="findings", parse_ok=True, degraded=False,
                diff_truncated=False, trustworthy=True, stop_reason="EndTurn",
                summary="1 finding", findings_total=1,
                severity={"high": 1, "medium": 0, "low": 0},
                findings=[dict(file="a.py", line=3, severity="high",
                               category="bug", title="NPE", detail="boom")])


def test_cli_triage_rejects_placeholder_reason(tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    art = _artifact_with_one_finding("rev1", "feat", "s"*40, "d"*40)
    st.save_review(art)

    rc = main(["triage", "rev1", "0", "fp"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "placeholder" in out
    assert st.triage_for("feat", "s"*40) == {}


def test_cli_triage_list(tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    art = _artifact_with_one_finding("rev1", "feat", "s"*40, "d"*40)
    st.save_review(art)

    assert main(["triage", "--list", "rev1"]) == 0
    out = capsys.readouterr().out
    assert "NPE" in out and "a.py" in out
    assert "[0]" in out


def test_cli_triage_list_with_a_dismissal_is_rejected_not_half_honoured(
        tmp_path, monkeypatch, capsys):
    """`--list` and a dismissal are two commands sharing one parser.

    `triage --list <id> <index> "<reason>"` parses cleanly and then throws the
    index and the reason away: the caller typed an audited reason, saw a
    listing and an exit 0, and would have every right to believe the finding
    was dismissed. It was not. Reject the mixture rather than silently picking
    one of its two meanings.
    """
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    st = Store.open(dbpath)
    st.save_review(_artifact_with_one_finding("rev1", "feat", "s"*40, "d"*40))

    rc = main(["triage", "--list", "rev1", "0",
               "verified: handler already checks None on entry, see PR #1"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "--list" in out
    assert "NPE" not in out, "the listing must not be printed as if it were the ask"
    assert st.triage_for("feat", "s"*40) == {}, "and nothing may be dismissed"


def test_cli_triage_dismissal_flips_the_gate_from_1_to_0(tmp_path, monkeypatch,
                                                          capsys):
    """The end-to-end point of the ledger: a real, audited dismissal must be
    what moves the gate, not the CLI reporting success on its own say-so."""
    from skodun import gitio
    from skodun.cli import main
    from skodun.config import load_config
    from skodun.gate import run_gate
    from tests.test_gitio import _git, _mkrepo

    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))

    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.py").write_text("two\n", encoding="utf-8")
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    branch = gitio.current_branch(repo)
    dh = gitio.diff_identity(diff.data)

    st = Store.open(dbpath)
    art = _artifact_with_one_finding("rev1", branch, base.sha, dh)
    st.save_review(art)

    assert run_gate(st, repo, load_config(repo)).code == 1, "finding must be open"

    rc = main(["triage", "rev1", "0",
              "verified: handler already checks None on entry, see PR #1"])
    assert rc == 0
    capsys.readouterr()

    result = run_gate(st, repo, load_config(repo))
    assert result.code == 0, result.message


# --------------------------------------------------------------------------
# The process boundary — a closed stdout must never change these exit codes
# --------------------------------------------------------------------------
#
# `shadow-compare`, `log`, and `triage --list` are all observational, and all
# three are ordinary things to pipe into `head` or `grep -q`. Piping closes
# stdout's read end before the child has written everything it means to, so
# the write raises `BrokenPipeError` deterministically. Only a real subprocess
# exercises that: in-process, `capsys` never lets a print actually fail.
# Escaping, that exception would leave the interpreter's own exit code of 1 —
# turning "nothing to report" or "here is your listing" into "findings remain
# open" about a review that was never even consulted.


def test_cli_shadow_compare_exit_code_survives_a_closed_stdout(tmp_path):
    db = tmp_path / "sub" / "s.db"
    Store.open(db).save_review(REC)   # at least one row, so the table has to print
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "skodun", "shadow-compare"],
            stdout=w_fd, stderr=subprocess.PIPE, text=True, env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == 0, f"stderr={p.stderr!r}"


def test_cli_shadow_compare_since_exit_code_survives_a_closed_stdout(tmp_path):
    """The new summary fields (`since=`, the excluded count) are printed on
    the same `_emit` path as everything else -- a closed stdout must not turn
    THIS line's failure into the interpreter's exit code of 1 either."""
    db = tmp_path / "sub" / "s.db"
    Store.open(db).save_review(REC)
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "skodun", "shadow-compare",
             "--since", "2026-01-01T00:00:00Z"],
            stdout=w_fd, stderr=subprocess.PIPE, text=True, env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == 0, f"stderr={p.stderr!r}"


def test_cli_log_exit_code_survives_a_closed_stdout(tmp_path):
    db = tmp_path / "sub" / "s.db"
    st = Store.open(db)
    for i in range(5):
        st.save_review({**REC, "id": f"r{i}", "reviewed_at": f"2026-07-27T1{i}:00:00Z"})
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "skodun", "log"],
            stdout=w_fd, stderr=subprocess.PIPE, text=True, env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == 0, f"stderr={p.stderr!r}"


def test_cli_triage_list_exit_code_survives_a_closed_stdout(tmp_path):
    db = tmp_path / "sub" / "s.db"
    st = Store.open(db)
    st.save_review(_artifact_with_one_finding("rev1", "feat", "s"*40, "d"*40))
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "skodun", "triage", "--list", "rev1"],
            stdout=w_fd, stderr=subprocess.PIPE, text=True, env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == 0, f"stderr={p.stderr!r}"


def test_cli_triage_dismissal_exit_code_survives_a_closed_stdout(tmp_path):
    """The dismissal's own success line -- printed last, after the write already
    landed -- must not turn a real `0` into a pipe error either."""
    db = tmp_path / "sub" / "s.db"
    st = Store.open(db)
    st.save_review(_artifact_with_one_finding("rev1", "feat", "s"*40, "d"*40))
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "skodun", "triage", "rev1", "0",
             "verified: handler already checks None on entry, see PR #1"],
            stdout=w_fd, stderr=subprocess.PIPE, text=True, env=_subprocess_env(db))
    finally:
        os.close(w_fd)
    assert p.returncode == 0, f"stderr={p.stderr!r}"
