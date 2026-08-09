from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from skodun import reuse
from skodun.config import load_config
from skodun.gitio import capture_diff, current_branch, resolve_base
from skodun.store import Store
from tests.test_gitio import _git, _mkrepo


def _identity(**changes):
    values = dict(
        repo_id="/repo/.git", worktree_root="/repo", branch="feature",
        head="h" * 40, base_sha="b" * 40, diff_hash="d" * 40,
        context_hash="c" * 64, checklist_hash="k" * 64,
        tree_fingerprint="t" * 64, security_policy_hash="p" * 64,
    )
    values.update(changes)
    return reuse.ReuseIdentity(**values)


def _record(identity: reuse.ReuseIdentity, **changes):
    record = dict(
        id="r1", reviewed_at="2026-08-09T00:00:00Z", branch=identity.branch,
        head=identity.head, base_ref="main", base_sha=identity.base_sha,
        diff_hash=identity.diff_hash, context_hash=identity.context_hash,
        checklist_hash=identity.checklist_hash,
        tree_fingerprint=identity.tree_fingerprint,
        security_policy_hash=identity.security_policy_hash,
        repo_id=identity.repo_id, worktree_root=identity.worktree_root,
        mode="now", source="skodun", status="clean", parse_ok=True,
        degraded=False, diff_truncated=False, findings=[], findings_total=0,
        summary="clean", severity={"high": 0, "medium": 0, "low": 0},
        requested_reviewer=None, client_family=None,
    )
    record.update(changes)
    return record


def test_real_probe_matches_current_context_checklist_and_tree(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "finder"\nprovider = "xai"\nmodel = "m"\n',
        encoding="utf-8")
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    cfg = load_config(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    identity = reuse._identity_for(
        repo, cfg, base, diff, branch=current_branch(repo),
        reviewer_name="finder")
    db = tmp_path / "probe.db"
    with Store.open(db) as store:
        store.save_review(_record(identity, routed_reviewer="finder"))
        result = reuse.probe(store, repo, cfg=cfg)
    assert result.candidate is not None
    assert result.candidate["id"] == "r1"


def test_tree_movement_during_probe_forces_a_miss(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "finder"\nprovider = "xai"\nmodel = "m"\n',
        encoding="utf-8")
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    cfg = load_config(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    identity = reuse._identity_for(
        repo, cfg, base, diff, branch=current_branch(repo),
        reviewer_name="finder")
    with Store.open(tmp_path / "probe.db") as store:
        store.save_review(_record(identity, routed_reviewer="finder"))
        calls = iter((identity.tree_fingerprint,
                      identity.tree_fingerprint,
                      "moved-" + identity.tree_fingerprint[6:]))
        monkeypatch.setattr(
            reuse.gitio, "tree_fingerprint",
            lambda repo, **kwargs: next(calls))
        result = reuse.probe(store, repo, cfg=cfg)
    assert result.candidate is None
    assert "tree moved" in result.reason


def test_exact_identity_candidate_is_selected_without_rewriting_it(tmp_path):
    identity = _identity()
    db = tmp_path / "reuse.db"
    with Store.open(db) as store:
        store.save_review(_record(identity))
        before = store.get_review("r1")
        found = reuse.find_exact_candidate(store, identity)
        after = store.get_review("r1")
    assert found == before
    assert after == before


def test_each_identity_mismatch_is_a_miss(tmp_path):
    identity = _identity()
    db = tmp_path / "reuse.db"
    with Store.open(db) as store:
        store.save_review(_record(identity))
        for field in ("repo_id", "base_sha", "diff_hash", "context_hash",
                      "checklist_hash", "tree_fingerprint",
                      "security_policy_hash"):
            changed = identity.__dict__.copy()
            changed[field] = "x" * len(changed[field])
            assert reuse.find_exact_candidate(
                store, reuse.ReuseIdentity(**changed)) is None, field


def test_untrustworthy_candidate_is_never_reused(tmp_path):
    identity = _identity()
    with Store.open(tmp_path / "reuse.db") as store:
        store.save_review(_record(identity, parse_ok=False, status="failed"))
        assert reuse.find_exact_candidate(store, identity) is None


def test_disabled_context_pack_can_match_without_a_context_hash(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "finder"\nprovider = "xai"\nmodel = "m"\n',
        encoding="utf-8")
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    cfg = load_config(repo)
    cfg = replace(cfg, defaults=replace(cfg.defaults, context_pack=False))
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    identity = reuse._identity_for(
        repo, cfg, base, diff, branch=current_branch(repo),
        reviewer_name="finder")
    assert identity.context_hash is None
    with Store.open(tmp_path / "reuse.db") as store:
        store.save_review(_record(identity, context_hash=""))
        assert reuse.find_exact_candidate(store, identity)["id"] == "r1"


def test_oversized_identity_has_deterministic_batched_hashes(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "finder"\nprovider = "xai"\nmodel = "m"\n',
        encoding="utf-8")
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    cfg = load_config(repo)
    cfg = replace(cfg, defaults=replace(cfg.defaults, max_diff_bytes=1000))
    base = resolve_base(repo)
    diff = SimpleNamespace(
        data=(b"diff --git a/a.txt b/a.txt\n@@ -1 +1 @@\n-" + b"x" * 1800
              + b"\n+two\n"),
        files=["a.txt"], statuses={"a.txt": "M"})
    first = reuse._identity_for(
        repo, cfg, base, diff, branch=current_branch(repo),
        reviewer_name="finder")
    second = reuse._identity_for(
        repo, cfg, base, diff, branch=current_branch(repo),
        reviewer_name="finder")
    assert first.checklist_hash and first.context_hash
    assert first.checklist_hash == second.checklist_hash
    assert first.context_hash == second.context_hash


def test_projection_recomputes_open_findings_from_current_triage(tmp_path):
    identity = _identity()
    finding = {
        "file": "a.py", "line": 3, "severity": "high", "category": "bug",
        "title": "known issue", "detail": "detail",
    }
    from skodun.textnorm import finding_key
    key = finding_key(finding["file"], finding["title"])
    with Store.open(tmp_path / "reuse.db") as store:
        store.save_review(_record(
            identity, findings=[finding], findings_total=1,
            severity={"high": 1, "medium": 0, "low": 0}))
        store.add_triage({
            "ledger_key": "feature\0" + identity.base_sha + "\0" + key,
            "finding_key": key, "review_id": "r1",
            "branch": "feature", "base_sha": identity.base_sha,
            "file": "a.py", "line": 3, "severity": "high",
            "title": "known issue", "dismissed_reason": "handled in this change",
            "dismissed_at": "2026-08-09T00:00:00Z",
        })
        status, text = reuse.project(
            store, store.get_review("r1"), branch="feature",
            base_sha=identity.base_sha)
    assert status == 0
    assert "findings=0" in text
