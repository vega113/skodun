"""Git IO: base resolution, diff capture, blob-sha1 diff identity.

The legacy shell reviewer is the porting oracle for `diff_identity`: every stored
review and every legacy record is keyed on the hash it computes, so any deviation
makes the two archives unjoinable. The parity tests below shell out to the real
oracle and compare hashes; where this module and the oracle disagree, the oracle
is right. Parity tests skip when `$SKODUN_ORACLE_DIR` is unset (public-repo
hygiene: no local path may be hardcoded here).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skodun.gitio import (
    Base,
    blob_sha1,
    capture_diff,
    current_branch,
    diff_identity,
    git_common_dir,
    head_sha,
    is_primary_checkout,
    resolve_base,
)
from tests.conftest import oracle_dir

ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None
_NO_ORACLE = ORACLE is None or not ORACLE.exists()
requires_oracle = pytest.mark.skipif(
    _NO_ORACLE, reason="oracle checkout not present (set SKODUN_ORACLE_DIR)"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _mkrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c0")
    return repo


def _oracle(repo: Path, flag: str) -> str:
    """Run the oracle's `--diff-hash` / `--base-sha` seam in `repo`."""
    cp = subprocess.run(
        ["sh", str(ORACLE), flag], cwd=repo, capture_output=True, text=True
    )
    assert cp.returncode == 0, f"oracle {flag} rc={cp.returncode} stderr={cp.stderr}"
    out = cp.stdout.strip().splitlines()
    assert out, f"oracle {flag} printed nothing"
    return out[-1]


# --------------------------------------------------------------------------
# blob sha1 / diff identity
# --------------------------------------------------------------------------


def test_blob_sha1_matches_git(tmp_path):
    data = b"hello \xff diff bytes\n"
    expected = subprocess.run(
        ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True
    ).stdout.decode().strip()
    assert blob_sha1(data) == expected


def test_diff_identity_strips_trailing_newlines_like_shell():
    assert diff_identity(b"diff --git a b\n+x\n\n\n") == diff_identity(
        b"diff --git a b\n+x"
    )
    # ...and only TRAILING newlines: a leading newline (the untracked-only
    # shape) is part of the identity.
    assert diff_identity(b"\n+x") != diff_identity(b"+x")


def test_diff_identity_is_blob_sha1_of_stripped_bytes():
    assert diff_identity(b"payload\n\n") == blob_sha1(b"payload")


# --------------------------------------------------------------------------
# base resolution
# --------------------------------------------------------------------------


def test_base_resolves_to_merge_base_with_main(tmp_path):
    repo = _mkrepo(tmp_path)
    c0 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feat")
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    base = resolve_base(repo)
    assert base == Base(ref="main", sha=c0, warning=None)


def test_base_falls_back_with_warning(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    head_parent = _git(repo, "rev-parse", "HEAD^")
    _git(repo, "checkout", "-b", "feat")
    _git(repo, "branch", "-D", "main")
    base = resolve_base(repo)
    assert base.ref == "HEAD^"
    assert base.sha == head_parent
    assert base.warning is not None


def test_base_single_commit_repo_falls_back_to_head(tmp_path):
    repo = _mkrepo(tmp_path)  # exactly one commit; HEAD^ does not exist
    _git(repo, "checkout", "-b", "feat")
    _git(repo, "branch", "-D", "main")
    base = resolve_base(repo)
    assert base.ref == "HEAD"
    assert base.sha == _git(repo, "rev-parse", "HEAD")
    assert base.warning is not None


def _unrelated_history_repo(tmp_path: Path) -> Path:
    """github/main exists but shares no history with HEAD; main does share it.

    The oracle takes the FIRST candidate that merely *exists* and never falls
    through to the next one, so github/main's missing merge-base sends it to
    HEAD^ even though origin/main / main would have resolved.
    """
    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    _git(repo, "checkout", "--orphan", "orph")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "a.txt").unlink()
    (repo / "z.txt").write_text("z\n", encoding="utf-8")
    _git(repo, "add", "z.txt")
    _git(repo, "commit", "-m", "orphan")
    orphan = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/github/main", orphan)
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    return repo


def test_base_first_existing_candidate_wins_no_fallthrough(tmp_path):
    """ORACLE PARITY: candidate selection is existence-only, not merge-base-able."""
    repo = _unrelated_history_repo(tmp_path)
    base = resolve_base(repo)
    # main WOULD have produced a merge-base — proving the fallback is not vacuous.
    assert _git(repo, "merge-base", "main", "HEAD")
    assert base.ref == "HEAD^"
    assert base.sha == _git(repo, "rev-parse", "HEAD^")
    assert base.warning is not None


@requires_oracle
def test_base_sha_parity_with_oracle_unrelated_history(tmp_path):
    repo = _unrelated_history_repo(tmp_path)
    assert resolve_base(repo).sha == _oracle(repo, "--base-sha")


# --------------------------------------------------------------------------
# diff capture
# --------------------------------------------------------------------------


def test_diff_includes_untracked_and_is_stable(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")  # modified
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")  # untracked
    base = resolve_base(repo)
    d1 = capture_diff(repo, base.sha, untracked_max=100)
    d2 = capture_diff(repo, base.sha, untracked_max=100)
    assert d1.data == d2.data  # deterministic bytes
    assert "new.txt" in d1.files and "a.txt" in d1.files
    assert b"brand new" in d1.data
    assert d1.statuses["a.txt"] == "M"
    assert d1.statuses["new.txt"] == "A"
    assert d1.truncated_untracked is False


def test_untracked_only_diff_starts_with_newline(tmp_path):
    """ORACLE PARITY: sections join with one '\\n'; an empty tracked section
    leaves the leading separator in place."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "only-new.txt").write_text("nu\n", encoding="utf-8")
    d = capture_diff(repo, resolve_base(repo).sha, untracked_max=100)
    assert d.data.startswith(b"\ndiff --git ")
    assert not d.data.endswith(b"\n")  # every section is trailing-newline-stripped


def test_no_separator_appended_when_untracked_yields_no_diff(tmp_path):
    """ORACLE PARITY: the oracle skips untracked entries that are not regular
    files (`[ -f ]`) and appends the separator only when the section is
    non-empty. A dangling symlink must therefore leave the bytes untouched."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    base = resolve_base(repo)
    before = capture_diff(repo, base.sha, untracked_max=100).data
    (repo / "dangling.lnk").symlink_to("/definitely/missing/target")
    after = capture_diff(repo, base.sha, untracked_max=100)
    assert after.data == before
    assert "dangling.lnk" not in after.files


def test_deleted_and_added_statuses(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").unlink()
    (repo / "added.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    d = capture_diff(repo, resolve_base(repo).sha, untracked_max=100)
    assert d.statuses["a.txt"] == "D"
    assert d.statuses["added.txt"] == "A"


def test_rename_record_uses_new_path(tmp_path):
    """`R100\\0old\\0new` is a THREE-token record; the new name wins."""
    repo = _mkrepo(tmp_path)
    (repo / "old.txt").write_text("".join(f"line {i}\n" for i in range(40)), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    _git(repo, "checkout", "-b", "feat")
    _git(repo, "mv", "old.txt", "new.txt")
    (repo / "tail.txt").write_text("t\n", encoding="utf-8")  # 4th token must not desync
    _git(repo, "add", "tail.txt")
    d = capture_diff(repo, resolve_base(repo).sha, untracked_max=100)
    assert d.statuses.get("new.txt") == "R"
    assert "old.txt" not in d.statuses  # old name never leaks into the file list
    assert d.statuses.get("tail.txt") == "A"  # parser stayed in sync after 3 tokens


def test_nul_parsing_preserves_exotic_filenames(tmp_path):
    """Text-mode parsing would hand back git's quoted `"\\303\\244.txt"`, and
    `.strip()` would eat the real spaces — both open the wrong path later."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    tracked_odd = "ä-tracked.txt"
    spaced = " lead-and-trail .txt"
    (repo / tracked_odd).write_text("x\n", encoding="utf-8")
    (repo / spaced).write_text("y\n", encoding="utf-8")
    _git(repo, "add", "-A")
    untracked_odd = "ü-untracked.txt"
    (repo / untracked_odd).write_text("z\n", encoding="utf-8")
    d = capture_diff(repo, resolve_base(repo).sha, untracked_max=100)
    for name in (tracked_odd, spaced, untracked_odd):
        assert name in d.files, f"{name!r} missing from {d.files!r}"
        assert name in d.statuses
    assert not any('"' in f or "\\303" in f for f in d.files)  # no quotepath escapes
    assert d.statuses[untracked_odd] == "A"


def test_untracked_cap_truncates_and_flags(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    for i in range(5):
        (repo / f"u{i}.txt").write_text(f"{i}\n", encoding="utf-8")
    d = capture_diff(repo, resolve_base(repo).sha, untracked_max=2)
    assert d.truncated_untracked is True
    assert len([f for f in d.files if f.startswith("u")]) == 2
    assert b"u0.txt" in d.data and b"u4.txt" not in d.data
    d_all = capture_diff(repo, resolve_base(repo).sha, untracked_max=100)
    assert d_all.truncated_untracked is False
    assert b"u4.txt" in d_all.data


# --------------------------------------------------------------------------
# diff identity parity with the oracle
# --------------------------------------------------------------------------


@requires_oracle
def test_diff_identity_parity_with_oracle(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")  # tracked edit
    base = resolve_base(repo)
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle(
        repo, "--diff-hash"
    )
    (repo / "brand-new.txt").write_text("nu\n", encoding="utf-8")  # + untracked
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle(
        repo, "--diff-hash"
    )


@requires_oracle
def test_diff_identity_parity_untracked_only(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "only-new.txt").write_text("nu\n", encoding="utf-8")
    base = resolve_base(repo)  # oracle output starts "\n" + udiff
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle(
        repo, "--diff-hash"
    )


@requires_oracle
def test_diff_identity_parity_many_sections(tmp_path):
    """Several untracked sections, an empty file, a space-padded name, a
    non-ASCII TRACKED name, and a dangling symlink the oracle's `[ -f ]` drops."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "ä-tracked.txt").write_text("umlaut\n", encoding="utf-8")
    _git(repo, "add", "-A")
    (repo / "b-new.txt").write_text("nu\n", encoding="utf-8")
    (repo / "c-empty.txt").write_bytes(b"")
    (repo / " spaced name .txt").write_text("sp\n", encoding="utf-8")
    (repo / "z-dangling.lnk").symlink_to("/definitely/missing/target")
    base = resolve_base(repo)
    d = capture_diff(repo, base.sha, 100)
    assert diff_identity(d.data) == _oracle(repo, "--diff-hash")
    # not vacuous: all four real sections plus the tracked non-ASCII name are in
    assert d.statuses["ä-tracked.txt"] == "A"
    assert {"b-new.txt", "c-empty.txt", " spaced name .txt"} <= set(d.files)


@requires_oracle
def test_known_divergence_untracked_nonascii_name(tmp_path):
    """DELIBERATE DIVERGENCE from the oracle — pinned, not accidental.

    The oracle lists untracked files in TEXT mode, so `core.quotepath` renders a
    non-ASCII name as the literal `"\\303\\244-new.txt"`; its `[ -f "$_uf" ]`
    guard then fails on that quoted string and the file is dropped from the
    reviewed diff entirely. That is an oracle bug: a brand-new file with a
    non-ASCII name is never reviewed. skodun reads NUL-delimited names and
    includes the file, so the two hashes differ for exactly this input.

    Blast radius is one-directional and safe: a legacy record for such a diff
    simply fails to join and skodun asks for a fresh review — it never skips
    one. Reproducing the bug instead would require emulating git's
    `quote_c_style` in Python, a new and larger source of divergence.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "ä-new.txt").write_text("umlaut\n", encoding="utf-8")  # untracked
    d = capture_diff(repo, resolve_base(repo).sha, 100)
    assert b"umlaut" in d.data  # skodun reviews it...
    assert "ä-new.txt" in d.files
    assert diff_identity(d.data) != _oracle(repo, "--diff-hash")  # ...the oracle does not
    # and the divergence is exactly the dropped section, nothing else
    without = capture_diff(repo, resolve_base(repo).sha, 0)
    assert diff_identity(without.data) == _oracle(repo, "--diff-hash")


@requires_oracle
def test_diff_identity_parity_unrelated_history_base(tmp_path):
    repo = _unrelated_history_repo(tmp_path)
    base = resolve_base(repo)
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle(
        repo, "--diff-hash"
    )


@requires_oracle
def test_diff_identity_parity_rename(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / "old.txt").write_text("".join(f"line {i}\n" for i in range(40)), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    _git(repo, "checkout", "-b", "feat")
    _git(repo, "mv", "old.txt", "new.txt")
    base = resolve_base(repo)
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle(
        repo, "--diff-hash"
    )


# --------------------------------------------------------------------------
# repo introspection
# --------------------------------------------------------------------------


def test_primary_checkout_detection(tmp_path):
    repo = _mkrepo(tmp_path)
    assert is_primary_checkout(repo) is True
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "-b", "w1")
    assert is_primary_checkout(wt) is False


def test_primary_checkout_not_a_substring_test(tmp_path):
    """A repo living under a path that merely contains 'worktrees' is primary."""
    holder = tmp_path / "worktrees"
    holder.mkdir()
    repo = _mkrepo(holder)
    assert is_primary_checkout(repo) is True


def test_git_common_dir_shared_between_worktrees(tmp_path):
    repo = _mkrepo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "-b", "w1")
    assert git_common_dir(repo) == git_common_dir(wt)
    assert git_common_dir(repo) == (repo / ".git").resolve()


def test_current_branch_and_head_sha(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    assert current_branch(repo) == "feat"
    assert head_sha(repo) == _git(repo, "rev-parse", "HEAD")
