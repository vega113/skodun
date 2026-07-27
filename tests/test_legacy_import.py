"""Importing the legacy `.grok-reviews` archive.

Two things must survive the migration or the migration is not worth doing:

  * a change that was already reviewed must keep satisfying the gate, and
  * a finding a human already dismissed, with a reason, must stay dismissed.

The tests here are written to fail loudly on the specific way this import can
go wrong: importing an index row -- a DERIVED SUMMARY with no `findings[]` --
as trustworthy. Such a row satisfies `latest_trustworthy_for`, and then the
gate's own artifact validation rejects it and returns 2. The gate is then
stuck at 2 forever, because the store keeps handing it the same unusable
record. Every trust assertion below therefore checks the *artifact*, not just
the verdict.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from skodun import gitio
from skodun.config import load_config
from skodun.gate import run_gate
from skodun.legacy_import import ImportStats, import_legacy
from skodun.store import Store
from skodun.textnorm import ledger_key
from skodun.triage import load_valid_artifact, validate_reason
from tests.conftest import oracle_dir
from tests.test_gitio import _git, _mkrepo

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

ROW = dict(id="loop_1", reviewed_at="2026-07-01T00:00:00Z", branch="b",
           head="h" * 40, base_sha="s" * 40, diff_hash="d" * 40, mode="prepush",
           parse_ok=True, degraded=False, diff_truncated=False,
           findings_total=0, severity={"high": 0, "medium": 0, "low": 0})

TRIAGE_ROW = dict(
    finding_key="ab" * 8, id="loop_0", head="h" * 40, branch="b",
    base_sha="s" * 40, file="a.py", line=1, severity="high", title="T",
    dismissed_reason="verified: handler checks None on entry, see PR #1",
    dismissed_at="2026-07-01T00:00:00Z")


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    """The CLI seam falls back to `~/.local/share/skodun/skodun.db`.

    Pinning `SKODUN_DB` inside `tmp_path` for every test in this module is the
    floor: forgetting to set it in one test cannot reach the developer's real
    store. `SKODUN_CONFIG` is pinned at a path that does not exist for the same
    reason -- the gate's identity work must not depend on a developer's global
    config.
    """
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))


def _archive(tmp_path, *, rows=(ROW,), artifacts=None, triage_rows=(TRIAGE_ROW,),
             index_tail="\n{corrupt", name=".grok-reviews"):
    """Write a legacy archive. `artifacts` maps review id -> artifact dict."""
    d = tmp_path / name
    d.mkdir()
    body = "\n".join(json.dumps(r) for r in rows) + index_tail
    (d / "index.jsonl").write_text(body, encoding="utf-8")
    for rid, art in (artifacts or {}).items():
        (d / f"{rid}.json").write_text(
            art if isinstance(art, str) else json.dumps(art), encoding="utf-8")
    if triage_rows is not None:
        (d / "triage.jsonl").write_text(
            "".join(json.dumps(t) + "\n" for t in triage_rows), encoding="utf-8")
    return d


def _artifact(row=ROW, **over):
    return {**row, "summary": "ok", "findings": [], **over}


def _store(tmp_path) -> Store:
    return Store.open(tmp_path / "s.db")


def _disk_full(rec):
    """A store that has stopped accepting writes.

    `sqlite3.OperationalError` is what sqlite actually raises for "database or
    disk is full" and for an I/O error, and it is a `sqlite3.DatabaseError`,
    which is the family the importer classifies as the STORE failing rather
    than the record being unusable.
    """
    raise sqlite3.OperationalError("database or disk is full")


# --------------------------------------------------------------------------
# The brief's two cases: trustworthy needs the artifact, summary alone does not
# --------------------------------------------------------------------------


def test_import_full_artifact_backcompat_trust_and_corrupt_line(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact()}))
    assert (stats.reviews, stats.triage, stats.skipped_lines) == (1, 1, 1)
    assert stats.demoted_no_artifact == 0
    imported = st.latest_trustworthy_for("d" * 40)
    assert imported is not None, "back-compat trust rule did not fire"
    assert imported["source"] == "legacy" and imported["findings"] == []
    # The whole point: what was imported is a REAL artifact, not a summary.
    load_valid_artifact(imported)
    assert "ab" * 8 in st.triage_for("b", "s" * 40)


def test_index_row_without_artifact_is_imported_demoted(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path))
    assert stats.demoted_no_artifact == 1 and stats.reviews == 1
    assert st.latest_trustworthy_for("d" * 40) is None   # never gate-eligible
    kept = st.get_review("loop_1")
    assert kept["source"] == "legacy"                    # history kept
    assert kept["parse_ok"] is False and kept["trustworthy"] is False
    assert kept["failure_reason"] == "legacy import: artifact missing/invalid"


def test_an_artifact_missing_diff_hash_demotes_its_row_and_spares_the_import(
        tmp_path):
    """ONE malformed artifact must not destroy the whole migration.

    `triage.load_valid_artifact` validates `id`/`branch`/`base_sha`/`findings`/
    `findings_total` but NOT `diff_hash`, so an artifact that omits only that
    key passes the validator and reaches the identity cross-check. Reading it
    with a subscript raised `KeyError` straight out of `import_legacy` -- a
    function documented to never raise and to abort on nothing -- so
    `skodun import-legacy` exited 2, every index row after the bad one was
    lost, and because `_import_ledger` runs AFTER `_import_index`, not a single
    dismissal was imported. Ledger continuity is half of what this module is
    for. The import is idempotent, so a re-run hit the same line and the
    operator had no way forward.

    Three rows, only the middle artifact malformed: the import completes, the
    middle row is demoted (one re-review), the rows on either side keep their
    trust, and the ledger is imported.
    """
    rows = [dict(ROW, id=f"loop_{i}", diff_hash=dh * 40)
            for i, dh in enumerate("abc", start=1)]
    artifacts = {r["id"]: _artifact(r) for r in rows}
    del artifacts["loop_2"]["diff_hash"]        # the ONLY thing wrong with it

    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, rows=rows, artifacts=artifacts,
                                       index_tail="\n"))

    assert stats.reviews == 3                    # nothing was lost
    assert stats.demoted_no_artifact == 1        # exactly the bad one
    assert stats.store_failures == 0
    demoted = st.get_review("loop_2")
    assert demoted["parse_ok"] is False and demoted["trustworthy"] is False
    assert demoted["failure_reason"] == "legacy import: artifact missing/invalid"
    assert st.latest_trustworthy_for("b" * 40) is None   # never gate-eligible
    # Its neighbours are untouched, and importable as real artifacts.
    for dh in ("a" * 40, "c" * 40):
        load_valid_artifact(st.latest_trustworthy_for(dh))
    # ...and the ledger, which only runs once the index is through.
    assert stats.triage == 1
    assert "ab" * 8 in st.triage_for("b", "s" * 40)


def test_an_artifact_missing_diff_hash_does_not_abort_the_cli(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """The same failure at the seam an operator actually runs: exit 0, not 2."""
    from skodun.cli import main

    rows = [dict(ROW, id=f"loop_{i}", diff_hash=dh * 40)
            for i, dh in enumerate("abc", start=1)]
    artifacts = {r["id"]: _artifact(r) for r in rows}
    del artifacts["loop_2"]["diff_hash"]
    d = _archive(tmp_path, rows=rows, artifacts=artifacts, index_tail="\n")
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli" / "s.db"))

    assert main(["import-legacy", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "KeyError" not in out
    assert "reviews=3" in out and "triage=1" in out
    assert "demoted_no_artifact=1" in out


# --------------------------------------------------------------------------
# Corrupt input is counted, never fatal
# --------------------------------------------------------------------------


def test_corrupt_and_truncated_lines_are_counted_not_fatal(tmp_path):
    """A crashing writer leaves a half-written final line. That is normal."""
    d = tmp_path / ".grok-reviews"
    d.mkdir()
    (d / "index.jsonl").write_bytes(
        json.dumps(ROW).encode()
        + b"\n[]"                                  # valid JSON, not an object
        + b"\n3"                                   # valid JSON, not an object
        + b'\n{"id": "no_end", "branch": "b"'      # truncated final line
    )
    (d / "loop_1.json").write_text(json.dumps(_artifact()), encoding="utf-8")
    st = _store(tmp_path)
    stats = import_legacy(st, d)
    assert stats.reviews == 1 and stats.skipped_lines == 3
    assert st.latest_trustworthy_for("d" * 40) is not None


def test_undecodable_bytes_do_not_abort_the_import(tmp_path):
    """`errors="replace"`: one bad byte must cost one row, not the archive."""
    d = tmp_path / ".grok-reviews"
    d.mkdir()
    (d / "index.jsonl").write_bytes(
        b'{"id": "bad", "\xff\xfe": 1}\n' + json.dumps(ROW).encode() + b"\n")
    (d / "loop_1.json").write_text(json.dumps(_artifact()), encoding="utf-8")
    st = _store(tmp_path)
    stats = import_legacy(st, d)
    # The mangled line is still valid JSON after replacement, so it imports as
    # a row with no trust axes; what matters is that the GOOD row survived it.
    assert stats.skipped_lines + stats.reviews == 2
    assert st.latest_trustworthy_for("d" * 40) is not None


def test_row_without_a_usable_id_is_skipped(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[{**ROW, "id": None}, {**ROW, "id": 7}, ROW],
        artifacts={"loop_1": _artifact()}, index_tail=""))
    assert stats.reviews == 1 and stats.skipped_lines == 2


# --------------------------------------------------------------------------
# Artifact / index disagreement demotes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("over, why", [
    (dict(diff_hash="e" * 40), "diff_hash"),
    (dict(id="somebody_else"), "id"),
])
def test_artifact_disagreeing_with_the_index_row_is_demoted(tmp_path, over, why):
    """A summary and an artifact that disagree describe two different reviews.

    Importing either one as trustworthy attests to content nobody reviewed.
    """
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact(**over)}))
    assert stats.demoted_no_artifact == 1, why
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.get_review("loop_1")["parse_ok"] is False


def test_corrupt_artifact_file_is_demoted(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": "{not json"}))
    assert stats.demoted_no_artifact == 1
    assert st.latest_trustworthy_for("d" * 40) is None


def test_artifact_failing_strict_validation_is_demoted(tmp_path):
    """No `findings` key at all: the shape `load_valid_artifact` exists to reject."""
    art = _artifact()
    del art["findings"]
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": art}))
    assert stats.demoted_no_artifact == 1
    assert st.latest_trustworthy_for("d" * 40) is None


def test_review_id_cannot_escape_the_archive_directory(tmp_path):
    """The `id` is untrusted JSON interpolated into a filename.

    The decoy below is a perfectly valid artifact whose `id` matches the row's,
    sitting one directory ABOVE the archive. If the importer resolved
    `../decoy.json` it would import it as trustworthy -- reading, and then
    attesting to, a file the archive does not contain.
    """
    d = tmp_path / "arch"
    d.mkdir()
    rid = "../decoy"
    row = {**ROW, "id": rid}
    (d / "index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (tmp_path / "decoy.json").write_text(
        json.dumps(_artifact(row)), encoding="utf-8")
    st = _store(tmp_path)
    stats = import_legacy(st, d)
    assert stats.demoted_no_artifact == 1
    assert st.latest_trustworthy_for("d" * 40) is None


def test_artifact_with_findings_imports_them_all(tmp_path):
    finds = [dict(file="a.py", line=1, severity="high", title="T1"),
             dict(file="b.py", line=2, severity="low", title="T2")]
    art = _artifact(findings=finds, findings_total=2)
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[{**ROW, "findings_total": 2}],
        artifacts={"loop_1": art}))
    assert stats.demoted_no_artifact == 0 and stats.reviews == 1
    assert stats.findings_reconciled == 0, "index and artifact agreed"
    imported = st.latest_trustworthy_for("d" * 40)
    assert [f["title"] for f in imported["findings"]] == ["T1", "T2"]
    assert imported["findings_total"] == 2


# --------------------------------------------------------------------------
# The findings-count check is ASYMMETRIC, and both directions are pinned here
# --------------------------------------------------------------------------


def test_artifact_reporting_more_findings_than_the_index_row_is_trusted(tmp_path):
    """A stale index summary does not invalidate the artifact it points at.

    The legacy writer appended the index row before later passes merged their
    findings in, so `findings_total: 0` beside a two-finding artifact is the
    single commonest shape in the real archive. The artifact is what gets
    imported and what the gate then reads, `load_valid_artifact` has already
    proved it self-consistent, and its extra findings can only make the gate
    STRICTER. Demoting it would discard already-reviewed history to guard
    against a direction of error that cannot loosen the gate.
    """
    finds = [dict(file="a.py", line=1, severity="high", title="T1"),
             dict(file="b.py", line=2, severity="low", title="T2")]
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(          # ROW still says findings_total=0
        tmp_path, artifacts={"loop_1": _artifact(findings=finds,
                                                 findings_total=2)}))
    assert stats.reviews == 1
    assert stats.demoted_no_artifact == 0, "a stale summary is not a demotion"
    assert stats.findings_reconciled == 1
    imported = st.latest_trustworthy_for("d" * 40)
    assert imported is not None, "gate-eligible: the artifact is trustworthy"
    load_valid_artifact(imported)                # a real artifact, not a summary
    assert imported["parse_ok"] is True and imported["trustworthy"] is True
    # The ARTIFACT's count wins, and every finding survives to be triaged.
    assert imported["findings_total"] == 2
    assert [f["title"] for f in imported["findings"]] == ["T1", "T2"]


def test_artifact_out_reporting_a_NONZERO_index_count_is_trusted(tmp_path):
    """The relaxed check is `artifact < row`, not `row == 0`.

    A summary appended after the first pass found one finding, with two more
    merged in later, is the same shape as the 0 -> 2 case above and must be
    read the same way. Pinned separately because a mutation narrowing the rule
    to "only when the row said zero" passes the 0 -> 2 test untouched.
    """
    finds = [dict(file="a.py", line=1, severity="high", title="T1"),
             dict(file="b.py", line=2, severity="low", title="T2"),
             dict(file="c.py", line=3, severity="low", title="T3")]
    row = {**ROW, "findings_total": 1,
           "severity": {"high": 1, "medium": 0, "low": 0}}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row],
        artifacts={"loop_1": _artifact(row, findings=finds, findings_total=3)}))
    assert stats.reviews == 1 and stats.demoted_no_artifact == 0
    assert stats.findings_reconciled == 1
    imported = st.latest_trustworthy_for("d" * 40)
    assert imported is not None
    load_valid_artifact(imported)
    assert imported["findings_total"] == 3
    assert [f["title"] for f in imported["findings"]] == ["T1", "T2", "T3"]


def test_reconciled_and_demoted_are_disjoint_buckets(tmp_path):
    """One archive, one of each shape, and the counters must not double-book.

    The smoke test can only assert the inequality `demoted + reconciled <=
    reviews`, which a double-booked row can satisfy by accident. Here the exact
    values are known, so a row counted in both buckets is visible.
    """
    reconciled_row = {**ROW, "id": "recon"}
    demoted_row = {**ROW, "id": "demo"}                  # no artifact on disk
    agreeing_row = {**ROW, "id": "agree"}
    denied_row = {**ROW, "id": "denied"}
    finds = [dict(file="a.py", line=1, severity="high", title="T1")]
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, index_tail="", triage_rows=None,
        rows=[reconciled_row, demoted_row, agreeing_row, denied_row],
        artifacts={
            "recon": _artifact(reconciled_row, findings=finds, findings_total=1),
            "agree": _artifact(agreeing_row),
            "denied": _artifact(denied_row, degraded=True)}))
    assert stats.reviews == 4, "every row is still preserved as history"
    assert stats.findings_reconciled == 1
    assert stats.demoted_no_artifact == 1
    assert stats.demoted_untrustworthy == 1
    assert stats.skipped_lines == 0 and stats.store_failures == 0
    # ... and the buckets sum to strictly less than `reviews`: the fourth row
    # ("agree") is in none of them.
    assert (stats.findings_reconciled + stats.demoted_no_artifact
            + stats.demoted_untrustworthy) == 3


def test_artifact_reporting_fewer_findings_than_the_index_row_is_demoted(tmp_path):
    """The direction the check exists for: an artifact that HIDES findings.

    The index recorded two; the artifact admits none. Trusting it would let the
    gate PASS on a review whose findings were never carried over -- the exact
    false all-clear this module is built to refuse.
    """
    row = {**ROW, "findings_total": 2,
           "severity": {"high": 1, "medium": 1, "low": 0}}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row],
        artifacts={"loop_1": _artifact(row, findings=[], findings_total=0)}))
    assert stats.reviews == 1 and stats.demoted_no_artifact == 1
    assert stats.findings_reconciled == 0, "an under-report is never reconciled"
    assert st.latest_trustworthy_for("d" * 40) is None   # never gate-eligible
    kept = st.get_review("loop_1")
    assert kept["parse_ok"] is False and kept["trustworthy"] is False
    assert kept["failure_reason"] == "legacy import: artifact missing/invalid"


# --------------------------------------------------------------------------
# Trust axes: legacy JSON is untrusted data, so non-booleans fail closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("axes", [
    dict(parse_ok=1, degraded=0, diff_truncated=0),
    dict(parse_ok="true", degraded="false", diff_truncated="false"),
    dict(parse_ok=True, degraded=1, diff_truncated=False),
    dict(parse_ok=True, degraded=False, diff_truncated="no"),
])
def test_non_boolean_trust_axes_fail_closed(tmp_path, axes):
    """`save_review` refuses non-bool axes; we must not launder them into True.

    Note the divergence this pins: the oracle's fallback spells the middle two
    checks `is not True`, so `degraded: 1` reads as NOT degraded and the row
    would be trustworthy. Here every non-boolean axis is read at its unsafe
    value.
    """
    row = {**ROW, **axes}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row], artifacts={"loop_1": _artifact(row)}))
    assert stats.reviews == 1
    assert stats.demoted_no_artifact == 0, "not an artifact problem"
    assert st.latest_trustworthy_for("d" * 40) is None
    kept = st.get_review("loop_1")
    assert kept["trustworthy"] is False
    for k in ("parse_ok", "degraded", "diff_truncated"):
        assert isinstance(kept[k], bool), f"{k} stored as {kept[k]!r}"


def test_missing_axes_default_to_the_pre_field_reading(tmp_path):
    """Absent `parse_ok` is not True, so the row is untrustworthy -- not a crash."""
    row = {k: v for k, v in ROW.items() if k != "parse_ok"}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row], artifacts={"loop_1": _artifact(row)}))
    assert stats.reviews == 1
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.get_review("loop_1")["parse_ok"] is False


def test_missing_diff_truncated_still_trusts_a_clean_row(tmp_path):
    """`diff_truncated` postdates most of the archive. Absent means False.

    If absent read as "unsafe" instead, essentially every historical row would
    import demoted and the migration would deliver no continuity at all.
    """
    row = {k: v for k, v in ROW.items() if k != "diff_truncated"}
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, rows=[row],
                               artifacts={"loop_1": _artifact(row)}))
    assert st.latest_trustworthy_for("d" * 40) is not None


# --------------------------------------------------------------------------
# The recorded `trustworthy` field, and the oracle's precedence rule
# --------------------------------------------------------------------------


def test_recorded_trustworthy_false_is_used_as_is_not_re_derived(tmp_path):
    """ORACLE PARITY: `if row.get("trustworthy") is not None` short-circuits.

    The axes here would derive True. The recorded verdict says otherwise and
    wins -- re-deriving would resurrect a row the legacy writer had already
    judged untrustworthy.
    """
    row = {**ROW, "trustworthy": False}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row], artifacts={"loop_1": _artifact(row)}))
    assert stats.reviews == 1 and stats.demoted_no_artifact == 0
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.get_review("loop_1")["trustworthy"] is False


def test_recorded_trustworthy_true_is_used_as_is(tmp_path):
    row = {**ROW, "trustworthy": True}
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, rows=[row],
                               artifacts={"loop_1": _artifact(row)}))
    assert st.latest_trustworthy_for("d" * 40) is not None


@pytest.mark.parametrize("value", [1, "true", "yes", [1]])
def test_non_boolean_recorded_trustworthy_is_not_true(tmp_path, value):
    """ORACLE PARITY: the oracle's test is `is True`, so `1` is not trust."""
    row = {**ROW, "trustworthy": value}
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, rows=[row],
                               artifacts={"loop_1": _artifact(row)}))
    assert st.latest_trustworthy_for("d" * 40) is None


def test_recorded_trustworthy_true_cannot_override_a_degraded_axis(tmp_path):
    """DOCUMENTED DIVERGENCE, in the fail-closed direction.

    The oracle would trust this row on the strength of its `trustworthy` field
    alone. skodun cannot: `Store.save_review` recomputes the verdict from the
    axes, and `gate` re-asserts that the artifact's own field agrees with the
    recomputation. Storing it as the oracle reads it would produce a record
    that contradicts itself and jams the gate at 2. So a self-contradictory
    legacy row imports demoted.
    """
    row = {**ROW, "trustworthy": True, "degraded": True}
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, rows=[row],
                               artifacts={"loop_1": _artifact(row)}))
    assert st.latest_trustworthy_for("d" * 40) is None
    kept = st.get_review("loop_1")
    assert kept["trustworthy"] is False and kept["degraded"] is True


def test_trustworthy_null_falls_back_to_the_axes(tmp_path):
    """ORACLE PARITY: the oracle tests `is not None`, so an explicit null falls
    through to the pre-2026-07-14 derivation rather than reading as false."""
    row = {**ROW, "trustworthy": None}
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, rows=[row],
                               artifacts={"loop_1": _artifact(row)}))
    assert st.latest_trustworthy_for("d" * 40) is not None


def test_a_row_demoted_by_its_recorded_verdict_is_counted(tmp_path):
    """A demotion nobody can see in the stats is a demotion nobody audits."""
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[{**ROW, "trustworthy": False}],
        artifacts={"loop_1": _artifact()}))
    assert stats.reviews == 1
    assert stats.demoted_untrustworthy == 1
    assert stats.demoted_no_artifact == 0, "the artifact was fine; the row was not"
    assert stats.skipped_lines == 1, "only the corrupt index tail"


# --------------------------------------------------------------------------
# THE ARTIFACT'S OWN TRUST DENIAL CANNOT BE OVERRIDDEN BY ITS INDEX SUMMARY
#
# The index row is a derived summary; the artifact is the record the review
# actually produced, and the module's stated merge rule is that the artifact
# wins every field it defines. Trust axes are not an exception to that rule --
# they are the fields where breaking it costs the most, because a clean-reading
# summary laundering an artifact's own `degraded: true` into a stored
# `trustworthy=1` is a gate PASS on a review the archive itself disowned.
#
# This is not a contrived shape. Rows written before `diff_truncated` and
# `trustworthy` existed carry neither field, so "the row reads clean" is the
# ordinary case across a large part of a real archive.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("denial", [
    dict(degraded=True),
    dict(parse_ok=False),
    dict(diff_truncated=True),
    dict(trustworthy=False),
])
def test_artifact_denying_its_own_trust_is_never_imported_trustworthy(tmp_path,
                                                                      denial):
    """Each of the four shapes an artifact can use to disown itself.

    The index ROW is clean in every case, which is exactly the situation where
    deriving trust from the row and stapling it onto the merged record would
    produce a trustworthy store row -- and `run_gate` would then pass on it.
    """
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact(**denial)}))
    assert stats.reviews == 1, "history is still preserved"
    assert stats.demoted_untrustworthy == 1
    assert stats.demoted_no_artifact == 0, "the artifact loaded fine"
    assert st.latest_trustworthy_for("d" * 40) is None, (
        f"an artifact recording {denial} imported as trustworthy")
    kept = st.get_review("loop_1")
    assert kept["trustworthy"] is False and kept["parse_ok"] is False
    assert kept["source"] == "legacy"
    # The demotion is a demotion, not a rewrite: what the artifact claimed is
    # still on the record for an auditor to read.
    assert kept["legacy_trust"] == {
        "parse_ok": denial.get("parse_ok", True),
        "degraded": denial.get("degraded", False),
        "diff_truncated": denial.get("diff_truncated", False),
        "trustworthy": denial.get("trustworthy")}


def test_artifact_denial_survives_a_row_that_predates_the_trust_fields(tmp_path):
    """The reachable shape: the row has no `diff_truncated`, no `trustworthy`.

    An absent axis reads False and an absent verdict falls back to the axes, so
    the row derives trustworthy on its own. Only the artifact knows better.
    """
    row = {k: v for k, v in ROW.items() if k != "diff_truncated"}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row],
        artifacts={"loop_1": _artifact(row, diff_truncated=True)}))
    assert stats.reviews == 1 and stats.demoted_untrustworthy == 1
    assert st.latest_trustworthy_for("d" * 40) is None


@pytest.mark.parametrize("denial", [
    dict(degraded=1),
    dict(diff_truncated="yes"),
    dict(parse_ok="false"),
])
def test_non_boolean_axes_on_the_artifact_also_fail_closed(tmp_path, denial):
    """The coercion is applied to the merged record, not only to the row.

    `bool("false")` is True, so reading an artifact's axis without the
    `_UNSAFE_AXIS` coercion would launder these into trust.
    """
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact(**denial)}))
    assert st.latest_trustworthy_for("d" * 40) is None
    kept = st.get_review("loop_1")
    assert kept["trustworthy"] is False
    for k in ("parse_ok", "degraded", "diff_truncated"):
        assert isinstance(kept[k], bool), f"{k} stored as {kept[k]!r}"


@pytest.mark.parametrize("value", [1, "true", "yes"])
def test_non_boolean_trustworthy_on_the_artifact_is_not_trust(tmp_path, value):
    """ORACLE PARITY, applied to the artifact: the test is `is True`."""
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path,
                               artifacts={"loop_1": _artifact(trustworthy=value)}))
    assert st.latest_trustworthy_for("d" * 40) is None


def test_a_clean_artifact_beside_a_clean_row_is_still_trusted(tmp_path):
    """The control: re-reading trust off the merged record must not tighten
    anything that was legitimately trustworthy before."""
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact(trustworthy=True)}))
    assert stats.demoted_untrustworthy == 0
    assert st.latest_trustworthy_for("d" * 40) is not None


def test_an_artifact_denial_beats_a_stale_summary_reconciliation(tmp_path):
    """A demoted row is never also counted as reconciled.

    The artifact out-reports its index row AND disowns itself. Its count is not
    the one anything will act on, so `findings_reconciled` -- which exists to
    say "this row was imported on the artifact's word" -- must not claim it.
    """
    finds = [dict(file="a.py", line=1, severity="high", title="T1")]
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact(findings=finds, findings_total=1,
                                                 degraded=True)}))
    assert stats.reviews == 1
    assert stats.demoted_untrustworthy == 1
    assert stats.findings_reconciled == 0
    assert st.latest_trustworthy_for("d" * 40) is None


# --------------------------------------------------------------------------
# The triage ledger
# --------------------------------------------------------------------------


def test_triage_uses_the_recorded_finding_key_never_a_recomputed_one(tmp_path):
    """The ledger is the authority.

    `finding_key` here is deliberately NOT sha256(file+title): if the importer
    recomputed it, the dismissal would land under a different key and every
    previously-dismissed finding would resurface as new.
    """
    st = _store(tmp_path)
    import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact()}))
    rows = st.triage_for("b", "s" * 40)
    assert set(rows) == {"ab" * 8}
    rec = rows["ab" * 8]
    assert rec["ledger_key"] == ledger_key("b", "s" * 40, "ab" * 8)
    assert rec["review_id"] == "loop_0"      # the legacy `id` spelling
    assert rec["dismissed_reason"].startswith("verified:")
    assert rec["file"] == "a.py" and rec["severity"] == "high"


@pytest.mark.parametrize("bad", [
    {k: v for k, v in TRIAGE_ROW.items() if k != "finding_key"},
    {**TRIAGE_ROW, "finding_key": ""},
    {**TRIAGE_ROW, "finding_key": None},
    {k: v for k, v in TRIAGE_ROW.items() if k != "id"},          # no review id
    {k: v for k, v in TRIAGE_ROW.items() if k != "branch"},
])
def test_unusable_triage_rows_are_skipped_and_counted(tmp_path, bad):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, triage_rows=[bad],
                                       artifacts={"loop_1": _artifact()}))
    assert stats.triage == 0
    assert stats.skipped_lines == 2      # the corrupt index tail + this row
    assert stats.triage_unauditable == 0, "unusable, not merely unauditable"
    assert st.triage_for("b", "s" * 40) == {}


# --------------------------------------------------------------------------
# THE AUDIT FLOOR APPLIES TO IMPORTED DISMISSALS
#
# `Store.add_triage` only requires `dismissed_reason` to be PRESENT. The rules
# that make a dismissal auditable -- MIN_REASON_CHARS and PLACEHOLDER_REASONS,
# enforced by `triage.validate_reason` on every dismissal skodun records itself
# -- live one layer above it, so without an explicit call the import path would
# be the one way into the ledger that has no floor at all. A row dismissing a
# finding as "fp" would then move the gate from 1 to 0.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason, why", [
    ("fp", "placeholder"),
    ("false positive", "placeholder"),
    ("Not A Bug", "placeholder, normalized"),
    ("too short", "under MIN_REASON_CHARS"),
    ("", "empty"),
    ("   \n  ", "whitespace only"),
    (None, "absent (explicit null)"),
    (17, "not even a string"),
])
def test_imported_dismissals_must_clear_the_audit_floor(tmp_path, reason, why):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact()},
        triage_rows=[{**TRIAGE_ROW, "dismissed_reason": reason}]))
    assert stats.triage == 0, why
    assert stats.triage_unauditable == 1
    assert stats.skipped_lines == 1, "the corrupt index tail only"
    assert st.triage_for("b", "s" * 40) == {}, (
        f"a {why} reason was honoured as a dismissal")


def test_a_missing_dismissed_reason_is_unauditable_not_merely_unusable(tmp_path):
    bad = {k: v for k, v in TRIAGE_ROW.items() if k != "dismissed_reason"}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, triage_rows=[bad],
                                       artifacts={"loop_1": _artifact()}))
    assert (stats.triage, stats.triage_unauditable) == (0, 1)
    assert st.triage_for("b", "s" * 40) == {}


def test_a_real_reason_still_imports(tmp_path):
    """The floor must not eat the dismissals the migration exists to preserve."""
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact()}))
    assert stats.triage == 1 and stats.triage_unauditable == 0
    assert set(st.triage_for("b", "s" * 40)) == {"ab" * 8}


def test_one_unauditable_dismissal_does_not_lose_the_auditable_ones(tmp_path):
    good = {**TRIAGE_ROW, "finding_key": "cd" * 8}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact()},
        triage_rows=[{**TRIAGE_ROW, "dismissed_reason": "fp"}, good]))
    assert (stats.triage, stats.triage_unauditable) == (1, 1)
    assert set(st.triage_for("b", "s" * 40)) == {"cd" * 8}


def test_e2e_a_rubber_stamp_dismissal_cannot_clear_a_finding(tmp_path):
    """END TO END, the reason this matters: gate 1 must not become gate 0.

    The finding is real and open. A legacy ledger row dismissing it as "fp"
    says nothing a human could audit; honouring it on import would silently
    clear the finding and hand the push a PASS.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    branch = gitio.current_branch(repo)
    from skodun.textnorm import finding_key
    finding = dict(file="a.txt", line=1, severity="high", title="T")
    row = dict(id="legacy_3", reviewed_at="2026-07-01T00:00:00Z", branch=branch,
               head=gitio.head_sha(repo), base_ref=base.ref, base_sha=base.sha,
               diff_hash=gitio.diff_identity(diff.data), mode="prepush",
               parse_ok=True, degraded=False, diff_truncated=False,
               findings_total=1, severity={"high": 1, "medium": 0, "low": 0})
    art = {**row, "summary": "s", "findings": [finding]}
    tri = dict(finding_key=finding_key("a.txt", "T"), id="legacy_3", branch=branch,
               base_sha=base.sha, file="a.txt", line=1, severity="high", title="T",
               dismissed_reason="fp", dismissed_at="2026-07-01T00:00:00Z")
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, rows=[row], artifacts={"legacy_3": art}, triage_rows=[tri],
        index_tail="", name="arch-stamp"))
    assert (stats.triage, stats.triage_unauditable) == (0, 1)
    r = run_gate(st, repo, load_config(repo))
    assert r.code == 1, f"a rubber stamp cleared a finding: {r.message}"
    assert "1 finding(s) open" in r.message


def test_one_bad_triage_row_does_not_lose_the_good_ones(tmp_path):
    good = {**TRIAGE_ROW, "finding_key": "cd" * 8}
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact()},
        triage_rows=[{**TRIAGE_ROW, "finding_key": ""}, good]))
    assert stats.triage == 1
    assert set(st.triage_for("b", "s" * 40)) == {"cd" * 8}


# --------------------------------------------------------------------------
# Absent / empty archives
# --------------------------------------------------------------------------


def test_missing_archive_directory_is_zeros_not_an_error(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, tmp_path / "nope")
    assert stats == ImportStats(0, 0, 0, 0)


def test_empty_archive_directory_is_zeros_not_an_error(tmp_path):
    d = tmp_path / ".grok-reviews"
    d.mkdir()
    st = _store(tmp_path)
    assert import_legacy(st, d) == ImportStats(0, 0, 0, 0)


def test_index_present_without_a_ledger(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, triage_rows=None,
                                       artifacts={"loop_1": _artifact()}))
    assert stats.reviews == 1 and stats.triage == 0


def test_blank_lines_are_not_counted_as_corruption(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, artifacts={"loop_1": _artifact()}, index_tail="\n\n   \n"))
    assert stats.reviews == 1 and stats.skipped_lines == 0


# --------------------------------------------------------------------------
# What the counters actually count
# --------------------------------------------------------------------------


def test_reviews_counts_index_lines_not_distinct_ids(tmp_path):
    """`reviews` is a count of LINES, and `ImportStats` says so in those words.

    A real archive repeats an id once per re-review of the same loop, and
    `save_review` upserts on `reviews.id`, so the store legitimately holds
    fewer rows than `reviews` reports. Pinned here because the counter reads
    like a count of preserved history and an operator will compare it against
    `SELECT COUNT(*)`.
    """
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, index_tail="", triage_rows=None,
        rows=[ROW, {**ROW, "reviewed_at": "2026-07-02T00:00:00Z"}],
        artifacts={"loop_1": _artifact()}))
    assert stats.reviews == 2, "two lines were read and persisted"
    assert st._c.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1
    # The docstring is the operator-facing half of this fix, so it is pinned
    # too: it must not go back to promising a count of preserved history.
    assert "upper bound" in ImportStats.__doc__
    assert "always answers" not in ImportStats.__doc__


def test_triage_counts_ledger_lines_not_distinct_ledger_keys(tmp_path):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, index_tail="", artifacts={"loop_1": _artifact()},
        triage_rows=[TRIAGE_ROW,
                     {**TRIAGE_ROW, "dismissed_at": "2026-07-02T00:00:00Z"}]))
    assert stats.triage == 2
    assert st._c.execute("SELECT COUNT(*) c FROM triage").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# A store that stops accepting writes is not a corrupt input line
# --------------------------------------------------------------------------


def test_a_store_write_failure_is_counted_apart_from_a_bad_line(tmp_path,
                                                                monkeypatch):
    """A full disk mid-import must not read as "the archive was garbage".

    Counted as `skipped_lines`, a disk failure produces `reviews=0
    skipped_lines=N` and an exit 0 -- a migration script is told the archive
    was junk and that the run succeeded, when in fact nothing was preserved.
    """
    st = _store(tmp_path)
    monkeypatch.setattr(st, "save_review", _disk_full)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact()}))
    assert stats.reviews == 0
    assert stats.store_failures == 1
    assert stats.skipped_lines == 1, "the corrupt index tail, and nothing else"


def test_a_ledger_write_failure_is_counted_apart_from_a_bad_line(tmp_path,
                                                                 monkeypatch):
    st = _store(tmp_path)
    monkeypatch.setattr(st, "add_triage", _disk_full)
    stats = import_legacy(st, _archive(tmp_path, artifacts={"loop_1": _artifact()}))
    assert stats.triage == 0 and stats.store_failures == 1
    assert stats.reviews == 1, "the index half still went in"


def test_an_unbindable_record_is_a_skipped_line_not_a_store_failure(tmp_path):
    """The other side of the split, so it cannot degenerate into "any error".

    A `branch` that is a list makes sqlite refuse the BINDING -- sqlite's own
    "the caller handed me something I cannot bind" error, which is the record
    being unusable, not the store being broken. Counting it as a store failure
    would make the CLI exit nonzero -- i.e. report a failed migration -- over
    one malformed line, which is the same confusion this split exists to end,
    only pointing the other way.
    """
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(
        tmp_path, index_tail="", triage_rows=None,
        rows=[{**ROW, "branch": ["not", "a", "string"]}]))
    assert stats.reviews == 0
    assert stats.store_failures == 0
    assert stats.skipped_lines == 1


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_importing_twice_changes_nothing(tmp_path):
    d = _archive(tmp_path, artifacts={"loop_1": _artifact()})
    st = _store(tmp_path)
    first = import_legacy(st, d)
    second = import_legacy(st, d)
    assert first == second
    assert st._c.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1
    assert st._c.execute("SELECT COUNT(*) c FROM triage").fetchone()["c"] == 1
    assert st.latest_trustworthy_for("d" * 40)["findings"] == []
    assert set(st.triage_for("b", "s" * 40)) == {"ab" * 8}


def test_re_import_after_the_artifact_appears_upgrades_the_row(tmp_path):
    """The demotion is not a tombstone: a repaired archive re-imports as trusted."""
    d = _archive(tmp_path)
    st = _store(tmp_path)
    assert import_legacy(st, d).demoted_no_artifact == 1
    (d / "loop_1.json").write_text(json.dumps(_artifact()), encoding="utf-8")
    again = import_legacy(st, d)
    assert again.demoted_no_artifact == 0 and again.reviews == 1
    assert st.latest_trustworthy_for("d" * 40) is not None
    assert st._c.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# END TO END: the continuity this task exists to deliver
# --------------------------------------------------------------------------


def _legacy_archive_for(repo, tmp_path, *, with_artifact=True, review_id="legacy_1",
                        artifact_findings=()):
    """A legacy archive describing the repo's real outgoing change.

    `artifact_findings` go into the ARTIFACT only; the index row keeps
    `findings_total: 0`, which is how the legacy writer left the rows whose
    later passes merged findings in after the summary was appended.
    """
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    row = dict(id=review_id, reviewed_at="2026-07-01T00:00:00Z",
               branch=gitio.current_branch(repo), head=gitio.head_sha(repo),
               base_ref=base.ref, base_sha=base.sha,
               diff_hash=gitio.diff_identity(diff.data), mode="prepush",
               model="grok-legacy", parse_ok=True, degraded=False,
               diff_truncated=False, findings_total=0,
               severity={"high": 0, "medium": 0, "low": 0})
    arts = {review_id: {**row, "summary": "s", "stop_reason": "EndTurn",
                        "findings": list(artifact_findings),
                        "findings_total": len(artifact_findings)}
            } if with_artifact else {}
    return _archive(tmp_path, rows=[row], artifacts=arts, triage_rows=None,
                    index_tail="", name=f"arch-{review_id}"), row


def test_e2e_imported_legacy_review_satisfies_the_gate(tmp_path):
    """THE point of this task: already-reviewed content is not re-reviewed.

    Without the artifact-backed import, `latest_trustworthy_for` would hand the
    gate a summary row, `load_valid_artifact` would reject it, and the gate
    would sit at 2 with no way for a fresh review to be selected ahead of it.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = _store(tmp_path)
    assert run_gate(st, repo, load_config(repo)).code == 2, "precondition"

    d, row = _legacy_archive_for(repo, tmp_path)
    assert import_legacy(st, d).reviews == 1
    result = run_gate(st, repo, load_config(repo))
    assert result.code == 0, result.message
    assert result.diff_hash == row["diff_hash"]
    assert "legacy_1" in result.message


def test_e2e_artifact_out_reporting_its_index_row_gates_at_one(tmp_path):
    """THE justification for the asymmetry, demonstrated end to end.

    The index row says `findings_total: 0`; the artifact carries two findings.
    Importing it on the artifact's word cannot produce a false all-clear -- the
    gate reads the artifact, finds two untriaged findings, and returns 1 ("go
    triage these"), not 0. Demoting the row instead would have returned 2 and
    forced a re-review of content already reviewed, which is the cost this
    module exists to avoid.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = _store(tmp_path)
    finds = [dict(file="a.txt", line=1, severity="high", title="T1"),
             dict(file="a.txt", line=2, severity="low", title="T2")]
    d, row = _legacy_archive_for(repo, tmp_path, artifact_findings=finds)

    stats = import_legacy(st, d)
    assert (stats.reviews, stats.demoted_no_artifact) == (1, 0)
    assert stats.findings_reconciled == 1

    result = run_gate(st, repo, load_config(repo))
    assert result.code == 1, result.message      # not 0 (false pass)...
    assert "2 finding(s) open" in result.message  # ...and not 2 (re-review)
    assert result.diff_hash == row["diff_hash"]


def test_e2e_demoted_legacy_row_is_never_gate_eligible(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = _store(tmp_path)
    d, row = _legacy_archive_for(repo, tmp_path, with_artifact=False)
    assert import_legacy(st, d).demoted_no_artifact == 1
    result = run_gate(st, repo, load_config(repo))
    assert result.code == 2, result.message
    assert "no trustworthy review" in result.message
    # ... and the history is still there to explain why.
    assert st.get_review("legacy_1")["source"] == "legacy"


def test_e2e_imported_dismissal_closes_an_open_finding(tmp_path):
    """A dismissal recorded by the legacy tool keeps the gate at 0 after the
    migration -- the second half of the continuity promise."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    branch = gitio.current_branch(repo)
    finding = dict(file="a.txt", line=1, severity="high", title="T")
    # The recorded key, exactly as the legacy ledger holds it.
    from skodun.textnorm import finding_key
    fkey = finding_key("a.txt", "T")
    row = dict(id="legacy_2", reviewed_at="2026-07-01T00:00:00Z", branch=branch,
               head=gitio.head_sha(repo), base_ref=base.ref, base_sha=base.sha,
               diff_hash=gitio.diff_identity(diff.data), mode="prepush",
               parse_ok=True, degraded=False, diff_truncated=False,
               findings_total=1, severity={"high": 1, "medium": 0, "low": 0})
    art = {**row, "summary": "s", "findings": [finding]}
    tri = dict(finding_key=fkey, id="legacy_2", branch=branch, base_sha=base.sha,
               file="a.txt", line=1, severity="high", title="T",
               dismissed_reason="the guard already lives in validate_input",
               dismissed_at="2026-07-01T00:00:00Z")
    st = _store(tmp_path)

    d_open = _archive(tmp_path, rows=[row], artifacts={"legacy_2": art},
                      triage_rows=None, index_tail="", name="arch-open")
    import_legacy(st, d_open)
    assert run_gate(st, repo, load_config(repo)).code == 1, "finding must be open"

    d_tri = _archive(tmp_path, rows=[row], artifacts={"legacy_2": art},
                     triage_rows=[tri], index_tail="", name="arch-triaged")
    assert import_legacy(st, d_tri).triage == 1
    r = run_gate(st, repo, load_config(repo))
    assert r.code == 0, r.message


# --------------------------------------------------------------------------
# The CLI seam
# --------------------------------------------------------------------------


def test_cli_import_legacy(tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli.db"))
    d = _archive(tmp_path, artifacts={"loop_1": _artifact()})
    assert main(["import-legacy", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "reviews=1" in out and "triage=1" in out and "skipped_lines=1" in out
    st = Store.open(tmp_path / "cli.db")
    assert st.latest_trustworthy_for("d" * 40) is not None


def test_cli_import_legacy_missing_dir_is_not_a_crash(tmp_path, monkeypatch, capsys):
    from skodun.cli import main
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli.db"))
    assert main(["import-legacy", "--dir", str(tmp_path / "nope")]) == 0
    assert "reviews=0" in capsys.readouterr().out


def test_cli_import_legacy_defaults_to_the_repo_archive(tmp_path, monkeypatch,
                                                        capsys):
    from skodun.cli import main
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli.db"))
    _archive(tmp_path, artifacts={"loop_1": _artifact()})
    assert main(["import-legacy", "--repo", str(tmp_path)]) == 0
    assert "reviews=1" in capsys.readouterr().out


def test_cli_import_legacy_reports_a_store_failure_as_nonzero(tmp_path, monkeypatch,
                                                              capsys):
    from skodun.cli import main
    # A path whose parent is a FILE: `Store.open` cannot mkdir there.
    (tmp_path / "blocker").write_text("x", encoding="utf-8")
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "blocker" / "s.db"))
    assert main(["import-legacy", "--dir", str(tmp_path)]) == 2


def test_cli_import_legacy_prints_every_counter(tmp_path, monkeypatch, capsys):
    """The CLI is the only operator-facing seam, so a counter it omits is one
    nobody will ever read.

    `findings_reconciled` exists precisely so that "imported on the artifact's
    word rather than the index's" is visible; printing the demotion counters
    but not that one shows the operator the losses and hides the overrides.
    """
    from skodun.cli import main
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli.db"))
    finds = [dict(file="a.py", line=1, severity="high", title="T1")]
    d = _archive(tmp_path, index_tail="",
                 rows=[ROW, {**ROW, "id": "denied"}],
                 artifacts={"loop_1": _artifact(findings=finds, findings_total=1),
                            "denied": _artifact({**ROW, "id": "denied"},
                                                degraded=True)},
                 triage_rows=[TRIAGE_ROW,
                              {**TRIAGE_ROW, "finding_key": "cd" * 8,
                               "dismissed_reason": "fp"}])
    assert main(["import-legacy", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    for expected in ("reviews=2", "triage=1", "skipped_lines=0",
                     "demoted_no_artifact=0", "demoted_untrustworthy=1",
                     "findings_reconciled=1", "triage_unauditable=1",
                     "store_failures=0"):
        assert expected in out, f"{expected!r} missing from {out!r}"


def test_cli_import_legacy_exits_nonzero_when_the_store_refused_a_write(
        tmp_path, monkeypatch, capsys):
    """`import_legacy` never raises, so a half-written import comes back as an
    ordinary result object. Exiting 0 on it would tell a migration script that
    history it does not have was preserved."""
    from skodun.cli import main
    from skodun.store import Store as _Store
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli.db"))
    monkeypatch.setattr(_Store, "save_review", lambda self, rec: _disk_full(rec))
    d = _archive(tmp_path, artifacts={"loop_1": _artifact()})
    assert main(["import-legacy", "--dir", str(d)]) == 2
    out = capsys.readouterr().out
    assert "store_failures=1" in out and "reviews=0" in out
    assert "FAILED" in out


# --------------------------------------------------------------------------
# Oracle-gated smoke test against the real archive
# --------------------------------------------------------------------------


_SMOKE_ROWS = 200


def _copy_real_archive(src, dst, limit=_SMOKE_ROWS):
    """Copy a bounded prefix of a real archive. NEVER mutates the source."""
    dst.mkdir(parents=True, exist_ok=True)
    kept = []
    with open(src / "index.jsonl", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if len(kept) >= limit:
                break
            kept.append(line)
            try:
                rid = json.loads(line).get("id")
            except Exception:
                continue
            art = src / f"{rid}.json"
            if isinstance(rid, str) and art.is_file():
                shutil.copyfile(art, dst / f"{rid}.json")
    (dst / "index.jsonl").write_text("".join(kept), encoding="utf-8")
    if (src / "triage.jsonl").is_file():
        shutil.copyfile(src / "triage.jsonl", dst / "triage.jsonl")
    return len(kept)


def test_real_archive_smoke(tmp_path):
    """Import a copy of a real legacy archive and check it is self-consistent.

    Skips cleanly when `$SKODUN_ORACLE_DIR` is unset or carries no archive.
    """
    od = oracle_dir()
    if od is None or not (od / ".grok-reviews" / "index.jsonl").is_file():
        pytest.skip("no legacy archive at $SKODUN_ORACLE_DIR/.grok-reviews")
    d = tmp_path / "copy"
    n = _copy_real_archive(od / ".grok-reviews", d)
    assert n > 0

    st = _store(tmp_path)
    stats = import_legacy(st, d)
    assert stats.reviews + stats.skipped_lines >= n
    assert stats.reviews > 0, "a real archive imported nothing"
    assert 0 <= stats.demoted_no_artifact <= stats.reviews
    assert 0 <= stats.findings_reconciled <= stats.reviews
    # Disjoint buckets: a row is either demoted or imported, never counted as
    # both, so the subsets cannot exceed the whole. (The exact-value version of
    # this, on synthetic data where a double-booking would be visible rather
    # than merely possible, is `test_reconciled_and_demoted_are_disjoint_buckets`.)
    assert (stats.demoted_no_artifact + stats.findings_reconciled
            + stats.demoted_untrustworthy) <= stats.reviews
    assert stats.triage > 0, "a real ledger imported no dismissals"
    # Nothing was lost to the STORE. A real archive exercising this path with a
    # nonzero count here means the import silently failed to preserve history.
    assert stats.store_failures == 0

    # The audit floor costs a real archive nothing: every dismissal a human
    # actually wrote clears it, so `triage` is not being propped up by rows
    # that would be rubber stamps.
    for row in st._c.execute("SELECT dismissed_reason FROM triage").fetchall():
        validate_reason(row["dismissed_reason"])

    # Every row the import called trustworthy must be a real, valid artifact --
    # this is the property whose violation jams the gate at 2.
    trusted = st._c.execute(
        "SELECT artifact_json FROM reviews WHERE trustworthy=1").fetchall()
    assert trusted, "no row survived as trustworthy"
    for r in trusted:
        art = json.loads(r["artifact_json"])
        load_valid_artifact(art)
        assert art["source"] == "legacy"
        assert art["trustworthy"] is True
        assert art["parse_ok"] is True and art["degraded"] is False
        assert art["diff_truncated"] is False
        assert st.latest_trustworthy_for(art["diff_hash"]) is not None

    assert import_legacy(st, d) == stats     # idempotent on real data too
