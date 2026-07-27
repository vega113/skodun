"""Shadow comparison against the legacy `.grok-reviews` archive.

`compare` is an observational join, not a gate: the tests below pin its one
rule (`match` is trustworthy-agreement plus clean-vs-dirty agreement, and
nothing finer), that the union of hashes -- not just skodun's -- is what gets
iterated, that legacy corruption is tolerated exactly like the importer
tolerates it, and that "newest by reviewed_at" is what wins on each side.
"""

import json

import pytest

from skodun.shadow import compare
from skodun.store import Store
from tests.test_store import REC


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
    out = compare(st, d, None)
    assert len(out) == 1 and out[0].match is True


def test_mismatch_when_legacy_found_findings(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"; d.mkdir()
    legacy = dict(id="loop_9", diff_hash="d"*40, trustworthy=True,
                  findings_total=2, severity={"high": 1, "medium": 1, "low": 0},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    out = compare(st, d, None)
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
    out = {c.diff_hash: c for c in compare(st, d, None)}
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
    out = {c.diff_hash: c for c in compare(st, d, None)}
    # the corrupt line contributed no comparison at all -- only the two real
    # hashes (skodun's "d"*40 and legacy's "e"*40) appear
    assert set(out) == {"d"*40, "e"*40}
    assert out["e"*40].legacy["id"] == "loop_1"


def test_missing_grok_reviews_directory_does_not_crash(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    out = compare(st, tmp_path / "nope-no-such-dir", None)
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
    out = compare(st, d, None)
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
    out = compare(st, d, None)
    assert out[0].match is False


def test_equal_cleanliness_different_severity_tallies_is_still_a_match(tmp_path):
    """Two independent model runs are not expected to agree on exact counts.

    Both sides are trustworthy and both are DIRTY (findings_total > 0), so
    they must match on the one thing `match` cares about -- even though the
    severity tallies (visible only in `deltas`) disagree substantially.
    """
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "findings_total": 3,
                    "severity": {"high": 1, "medium": 2, "low": 0}})
    d = tmp_path / ".grok-reviews"
    legacy = dict(id="loop_1", diff_hash="d"*40, trustworthy=True,
                  findings_total=7, severity={"high": 0, "medium": 1, "low": 6},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    _write_index(d, legacy)
    out = compare(st, d, None)
    assert len(out) == 1
    assert out[0].match is True
    assert out[0].deltas["findings_total"] == (3, 7)
    assert out[0].deltas["sev_high"] == (1, 0)
    assert out[0].deltas["sev_medium"] == (2, 1)
    assert out[0].deltas["sev_low"] == (0, 6)


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

    out = compare(st, d, "d"*40)
    assert len(out) == 1
    assert out[0].diff_hash == "d"*40
    assert out[0].legacy is None            # legacy never reviewed this one


def test_single_hash_filter_absent_on_both_sides_is_empty(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    out = compare(st, tmp_path / ".grok-reviews", "z"*40)
    assert out == []


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


def test_cli_shadow_compare_missing_archive_still_exits_0(tmp_path, monkeypatch,
                                                          capsys):
    from skodun.cli import main
    dbpath = tmp_path / "cli.db"
    monkeypatch.setenv("SKODUN_DB", str(dbpath))
    Store.open(dbpath).save_review(REC)
    assert main(["shadow-compare", "--dir", str(tmp_path / "nope")]) == 0
    assert "shadow: 1 compared" in capsys.readouterr().out


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
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2


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
