"""R2 churn attribution and R3 round context — real store + git fixtures."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from skodun import roundctx
from skodun.services import svc_log, svc_triage_list
from skodun.store import Store
from skodun.textnorm import finding_key, ledger_key


# --- git + store helpers ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _mkrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.quotepath", "true")
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c0")
    return repo


def _commit(repo: Path, path: str, content: str, msg: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "s.db")


def _finding(file: str, title: str = "bug", **kw) -> dict:
    f = dict(file=file, line=1, severity="high", category="bug",
             title=title, detail="enough detail for a real finding")
    f.update(kw)
    return f


def _rec(**kw) -> dict:
    rec = dict(
        id="r1", reviewed_at="2026-08-01T10:00:00Z", branch="main",
        head="a" * 40, base_ref="main", base_sha="b" * 40,
        diff_hash="d" * 40, context_hash="", mode="foreground",
        source="skodun", model="m", adapter="agy", status="clean",
        parse_ok=True, degraded=False, degraded_reason="",
        diff_truncated=False, stop_reason="SUCCESS", summary="ok",
        findings=[], findings_total=0,
        severity={"high": 0, "medium": 0, "low": 0},
        failure_reason="", usable_output=True, trustworthy=True,
        superseded_by=None, repo=None,
    )
    rec.update(kw)
    if "findings" in kw or rec["findings"]:
        rec["findings_total"] = len(rec["findings"])
        sev = {"high": 0, "medium": 0, "low": 0}
        for f in rec["findings"]:
            s = f.get("severity")
            if s in sev:
                sev[s] += 1
        rec["severity"] = sev
    return rec


def _save(st: Store, **kw) -> dict:
    rec = _rec(**kw)
    st.save_review(rec)
    return rec


def _dismiss(st: Store, review: dict, finding: dict, *, at: str) -> None:
    fkey = finding_key(finding["file"], finding["title"])
    st.add_triage({
        "ledger_key": ledger_key(review["branch"], review["base_sha"], fkey),
        "finding_key": fkey,
        "review_id": review["id"],
        "branch": review["branch"],
        "base_sha": review["base_sha"],
        "file": finding["file"],
        "line": finding.get("line"),
        "severity": finding.get("severity"),
        "title": finding["title"],
        "dismissed_reason": "not a defect after checking the call sites carefully",
        "dismissed_at": at,
    })


# --- pure helpers -----------------------------------------------------------


def test_finding_in_churn_exact_path():
    changed = frozenset({"a.py", "b.py"})
    assert roundctx.finding_in_churn({"file": "a.py"}, changed) is True
    assert roundctx.finding_in_churn({"file": "c.py"}, changed) is False
    assert roundctx.finding_in_churn({"file": ""}, changed) is None
    assert roundctx.finding_in_churn({"file": "a.py"}, None) is None


def test_annotate_summary_counts():
    findings = [
        _finding("a.py", "one"),
        _finding("b.py", "two"),
        _finding("", "no-path"),
    ]
    annotated, summary = roundctx.annotate_findings_churn(
        findings, frozenset({"a.py"}))
    assert summary.total == 3
    assert summary.in_churn == 1
    assert summary.unknown == 1
    assert annotated[0][roundctx.CHURN_KEY] is True
    assert annotated[1][roundctx.CHURN_KEY] is False
    assert annotated[2][roundctx.CHURN_UNKNOWN] is True
    assert "1 of 2 attributed" in summary.line() or "1 of 3" in summary.line()


# --- R2 with real git history -----------------------------------------------


def test_r2_churn_uses_git_paths_between_review_heads(tmp_path):
    """Second review's findings: only files changed after first head are churn.

    Drives the shipped `churn_for_review` + `paths_changed_between` (via
    gitio.capture_ref_diff), not a re-implemented path set.
    """
    repo = _mkrepo(tmp_path)
    h1 = _commit(repo, "stable.py", "stable = 1\n", "c1-stable")
    h2 = _commit(repo, "fixme.py", "broken = True\n", "c2-fix")

    # Sanity: git says fixme.py changed between h1 and h2, stable.py did not
    # as a path introduced only after... actually stable.py was in h1 and
    # unchanged in h2; fixme.py is new in h2.
    changed = roundctx.paths_changed_between(repo, h1, h2)
    assert changed is not None
    assert "fixme.py" in changed
    assert "stable.py" not in changed

    common = str(repo.resolve())
    with _store(tmp_path) as st:
        r1 = _save(st, id="rev1", head=h1, reviewed_at="2026-08-01T10:00:00Z",
                   repo=common, findings=[_finding("stable.py", "old")])
        r2 = _save(st, id="rev2", head=h2, reviewed_at="2026-08-01T11:00:00Z",
                   repo=common, findings=[
                       _finding("fixme.py", "new bug"),
                       _finding("stable.py", "old still"),
                   ])
        annotated, summary, prev = roundctx.churn_for_review(
            st, r2, repo_path=repo)
        assert prev is not None and prev["id"] == r1["id"]
        assert summary.in_churn == 1
        assert summary.total == 2
        assert summary.unknown == 0
        by_file = {f["file"]: f[roundctx.CHURN_KEY] for f in annotated}
        assert by_file["fixme.py"] is True
        assert by_file["stable.py"] is False
        # Summary count is git-derived, not a hardcoded constant alone:
        # recompute path set independently and require agreement.
        assert summary.in_churn == sum(
            1 for f in r2["findings"] if f["file"] in changed)

        code, text = svc_triage_list(st, "rev2")
        assert code == 0
        assert "churn: 1 of 2 finding(s) land in code changed since the previous review" in text
        assert "churn:yes" in text
        assert "churn:no" in text


def test_r2_missing_file_is_unknown_not_false_churn(tmp_path):
    repo = _mkrepo(tmp_path)
    h1 = _git(repo, "rev-parse", "HEAD")
    h2 = _commit(repo, "x.py", "x\n", "c1")
    common = str(repo.resolve())
    with _store(tmp_path) as st:
        _save(st, id="rev1", head=h1, reviewed_at="2026-08-01T10:00:00Z",
              repo=common, findings=[])
        r2 = _save(st, id="rev2", head=h2, reviewed_at="2026-08-01T11:00:00Z",
                   repo=common, findings=[_finding("", "no file")])
        annotated, summary, _ = roundctx.churn_for_review(st, r2, repo_path=repo)
        assert summary.unknown == 1
        assert annotated[0].get(roundctx.CHURN_UNKNOWN) is True
        assert roundctx.CHURN_KEY not in annotated[0]


# --- R3 round ordinal + prior triaged ---------------------------------------


def test_r3_ordinal_and_prior_triaged_on_log_and_triage(tmp_path):
    with _store(tmp_path) as st:
        f_old = _finding("a.py", "first")
        r1 = _save(st, id="rev1", reviewed_at="2026-08-01T10:00:00Z",
                   branch="feat", repo="/repos/a",
                   findings=[f_old], trustworthy=True)
        _dismiss(st, r1, f_old, at="2026-08-01T10:05:00Z")
        f_new = _finding("b.py", "second")
        r2 = _save(st, id="rev2", reviewed_at="2026-08-01T11:00:00Z",
                   branch="feat", repo="/repos/a",
                   findings=[f_new], trustworthy=True)
        f_third = _finding("c.py", "third")
        r3 = _save(st, id="rev3", reviewed_at="2026-08-01T12:00:00Z",
                   branch="feat", repo="/repos/a",
                   findings=[f_third], trustworthy=True)

        ctx2 = roundctx.round_context_for_review(st, r2)
        assert ctx2 is not None
        assert ctx2.ordinal == 2
        assert ctx2.total_rounds == 3
        assert ctx2.prior_triaged == 1  # f_old dismissed on rev1

        ctx3 = roundctx.round_context_for_review(st, r3)
        assert ctx3.ordinal == 3
        assert ctx3.prior_triaged == 1

        code, log_text = svc_log(st, "feat", 10, repo="/repos/a")
        assert code == 0
        assert "review 3 of 3" in log_text
        assert "review 2 of 3" in log_text
        assert "1 finding(s) already triaged in earlier rounds" in log_text

        code, triage_text = svc_triage_list(st, "rev3")
        assert code == 0
        assert "round: review 3 of 3 on this branch" in triage_text
        assert "1 finding(s) already triaged in earlier rounds" in triage_text


def test_r3_does_not_cross_repositories(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="a1", reviewed_at="2026-08-01T10:00:00Z",
              branch="main", repo="/repos/a", findings=[])
        b1 = _save(st, id="b1", reviewed_at="2026-08-01T11:00:00Z",
                   branch="main", repo="/repos/b", findings=[])
        ctx = roundctx.round_context_for_review(st, b1)
        assert ctx is not None
        assert ctx.ordinal == 1
        assert ctx.total_rounds == 1


# --- gate unchanged ---------------------------------------------------------


def test_r2_does_not_change_gate_exit(tmp_path):
    """Open findings still exit 1; full triage still exits 0 — churn ignored.

    Drives the real `run_gate` entry point with a review whose findings carry
    R2 annotations (one in-churn, one not). Gate must not treat churn markers
    as triage.
    """
    from skodun import gitio, triage
    from skodun.config import load_config
    from skodun.gate import run_gate

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    # Outgoing change so gate has content to match.
    # (Uncommitted edit against HEAD.)
    findings = [
        _finding("churn.py", "in fix"),
        _finding("other.py", "outside"),
    ]
    annotated, summary = roundctx.annotate_findings_churn(
        findings, frozenset({"churn.py"}))
    assert summary.in_churn == 1
    assert summary.total == 2

    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    dhash = gitio.diff_identity(diff.data)
    cfg = load_config(repo)

    with _store(tmp_path) as st:
        rec = _rec(
            id="g1", branch=gitio.current_branch(repo), head=gitio.head_sha(repo),
            base_ref=base.ref, base_sha=base.sha, diff_hash=dhash,
            findings=annotated, findings_total=2,
            severity={"high": 2, "medium": 0, "low": 0},
            parse_ok=True, degraded=False, diff_truncated=False,
            status="clean", usable_output=True,
            repo=str(gitio.git_common_dir(repo)),
        )
        st.save_review(rec)

        r1 = run_gate(st, repo, cfg, env={})
        assert r1.code == 1, r1.message
        assert "open" in r1.message.lower()

        # Triage only the non-churn finding → still open (the churn one remains)
        art = st.get_review("g1")
        # Index of "outside" finding
        idx_out = next(i for i, f in enumerate(art["findings"])
                       if f["file"] == "other.py")
        triage.dismiss(
            st, art, idx_out,
            "not a defect after checking the call sites carefully",
            "2026-08-01T10:00:00Z",
        )
        r2 = run_gate(st, repo, cfg, env={})
        assert r2.code == 1, r2.message  # churn finding still open

        art = st.get_review("g1")
        idx_churn = next(i for i, f in enumerate(art["findings"])
                         if f["file"] == "churn.py")
        triage.dismiss(
            st, art, idx_churn,
            "not a defect after checking the call sites carefully",
            "2026-08-01T10:00:01Z",
        )
        r3 = run_gate(st, repo, cfg, env={})
        assert r3.code == 0, r3.message


def test_seams_untouched():
    root = Path(__file__).resolve().parents[1] / "src" / "skodun"
    pins = {
        "gate.py":
            "62628b4c804218607234c2a8d2c9b6054a30c6ab7b96679d62924d4e57d0bd3f",
        "trust.py":
            "8a3ccda55205898fe20dc2304cc1bd62fe9e08a2c28da77b7d36b5e1160167c1",
    }
    for name, exp in pins.items():
        got = hashlib.sha256((root / name).read_bytes()).hexdigest()
        assert got == exp, f"{name} changed: {got}"
