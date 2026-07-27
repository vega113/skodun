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

import pytest

from skodun import gitio
from skodun.config import load_config
from skodun.gate import run_gate
from skodun.legacy_import import ImportStats, import_legacy
from skodun.store import Store
from skodun.textnorm import ledger_key
from skodun.triage import load_valid_artifact
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
    (dict(findings_total=3, findings=[dict(file="a", title="t")] * 3),
     "findings_total"),
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
    imported = st.latest_trustworthy_for("d" * 40)
    assert [f["title"] for f in imported["findings"]] == ["T1", "T2"]
    assert imported["findings_total"] == 2


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
    {k: v for k, v in TRIAGE_ROW.items() if k != "dismissed_reason"},
    {k: v for k, v in TRIAGE_ROW.items() if k != "branch"},
])
def test_unusable_triage_rows_are_skipped_and_counted(tmp_path, bad):
    st = _store(tmp_path)
    stats = import_legacy(st, _archive(tmp_path, triage_rows=[bad],
                                       artifacts={"loop_1": _artifact()}))
    assert stats.triage == 0
    assert stats.skipped_lines == 2      # the corrupt index tail + this row
    assert st.triage_for("b", "s" * 40) == {}


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


def _legacy_archive_for(repo, tmp_path, *, with_artifact=True, review_id="legacy_1"):
    """A legacy archive describing the repo's real outgoing change."""
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
                        "findings": []}} if with_artifact else {}
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
    assert stats.triage > 0, "a real ledger imported no dismissals"

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
