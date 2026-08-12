"""Git IO: base resolution, diff capture, blob-sha1 diff identity.

The legacy shell reviewer is the porting oracle for `diff_identity`: every stored
review and every legacy record is keyed on the hash it computes, so any deviation
makes the two archives unjoinable. The parity tests below shell out to the real
oracle and compare hashes; where this module and the oracle disagree, the oracle
is right. Parity tests skip when `$SKODUN_ORACLE_DIR` is unset (public-repo
hygiene: no local path may be hardcoded here).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skodun.gitio import (
    Base,
    GitError,
    blob_bytes,
    blob_sha1,
    blob_size,
    canonical_repository_identity,
    capture_diff,
    capture_ref_diff,
    current_branch,
    diff_identity,
    exact_commit_exists,
    git_common_dir,
    head_sha,
    is_primary_checkout,
    is_ancestor,
    tree_fingerprint,
    resolve_base,
    resolve_ref_base,
)
from tests.conftest import oracle_dir

ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None
_NO_ORACLE = ORACLE is None or not ORACLE.exists()
requires_oracle = pytest.mark.skipif(
    _NO_ORACLE, reason="oracle checkout not present (set SKODUN_ORACLE_DIR)"
)


@pytest.fixture(autouse=True)
def _neutralise_ambient_git_config(monkeypatch):
    """Drop `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` from the env.

    Those entries rank with `git -c`, i.e. ABOVE repo-local config, so a runner
    that exports them would override the settings `_mkrepo` pins (notably
    `core.quotepath`) for skodun's git calls and the oracle's alike, and the
    documented divergence below would silently stop being the thing it claims to
    document. Everything else (`~/.gitconfig`, system config) ranks below
    repo-local and is already neutralised by the pin itself.
    """
    for key in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
        if key == "GIT_CONFIG_COUNT" or key.split("_")[-1].isdigit():
            monkeypatch.delenv(key, raising=False)


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
    # Pinned, not inherited: `core.quotepath` decides whether the oracle's
    # text-mode untracked listing hands its `[ -f ]` guard a quoted name, which
    # is exactly what test_known_divergence_untracked_nonascii_name documents.
    # Leaving it to ambient config would make that test's result a property of
    # the developer's machine.
    _git(repo, "config", "core.quotepath", "true")
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


def test_tree_fingerprint_changes_for_dirty_worktree_without_head_change(tmp_path):
    repo = _mkrepo(tmp_path)
    before = tree_fingerprint(repo)
    head = head_sha(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    after = tree_fingerprint(repo)
    assert head_sha(repo) == head
    assert before != after


def test_tree_fingerprint_changes_when_the_same_dirty_file_changes_again(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    first_dirty = tree_fingerprint(repo)
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    second_dirty = tree_fingerprint(repo)
    assert first_dirty != second_dirty


def test_tree_fingerprint_changes_when_the_same_submodule_file_changes_again(
        tmp_path):
    (tmp_path / "inner").mkdir()
    (tmp_path / "outer").mkdir()
    inner = _mkrepo(tmp_path / "inner")
    outer = _mkrepo(tmp_path / "outer")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add",
         str(inner), "mod")
    _git(outer, "commit", "-m", "add submodule")
    (outer / "mod" / "a.txt").write_text("two\n", encoding="utf-8")
    first_dirty = tree_fingerprint(outer)
    (outer / "mod" / "a.txt").write_text("three\n", encoding="utf-8")
    second_dirty = tree_fingerprint(outer)
    assert first_dirty != second_dirty


def test_tree_fingerprint_can_be_limited_to_reviewed_paths(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    before = tree_fingerprint(repo, paths=["a.txt"])
    (repo / "unreviewed.txt").write_text("outside the captured diff\n",
                                          encoding="utf-8")
    after = tree_fingerprint(repo, paths=["a.txt"])
    assert before == after


def test_tree_fingerprint_rejects_a_replaced_path(tmp_path, monkeypatch):
    import errno

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")

    def replaced_path(*args, **kwargs):
        raise OSError(errno.ELOOP, "too many symbolic links")

    monkeypatch.setattr(os, "open", replaced_path)
    with pytest.raises(GitError, match="could not safely read"):
        tree_fingerprint(repo)


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
    assert before.startswith(b"diff --git ")  # not vacuous: `before` is a real diff
    (repo / "dangling.lnk").symlink_to("/definitely/missing/target")
    # the untracked LIST is non-empty; only the untracked DIFF section is
    assert "dangling.lnk" in _git(repo, "ls-files", "--others", "--exclude-standard")
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


def test_quotepath_changes_diff_identity_for_nonascii_path(tmp_path):
    """DOCUMENTS a known, fail-safe config sensitivity -- not a bug to fix.

    `Diff.files`/`statuses` are correct either way (NUL parsing is immune to
    `core.quotepath`), but `git diff --no-index` still renders the untracked
    file's name in the diff HEADER under quotepath rules, and that header text
    is part of the hashed bytes. So `diff_identity` for a diff touching a
    non-ASCII path is itself a function of `core.quotepath`, independent of
    `--no-ext-diff --no-textconv` (those only suppress external/textconv
    drivers -- see the `gitio` module docstring). The direction is safe: a
    differing hash only means a failed legacy-record join and one extra
    review, never a wrong gate PASS.

    The autouse `_neutralise_ambient_git_config` fixture strips
    `GIT_CONFIG_*` env entries for the whole module so the runner's ambient
    config can't shadow this; the config is pinned per-repo below so the
    result is a property of that config, not of the machine running the test.
    """
    repo = _mkrepo(tmp_path)  # _mkrepo already pins core.quotepath=true
    _git(repo, "checkout", "-b", "feat")
    (repo / "ä-new.txt").write_text("umlaut\n", encoding="utf-8")  # untracked, non-ASCII
    base = resolve_base(repo)

    assert _git(repo, "config", "--get", "core.quotepath") == "true"
    quoted = diff_identity(capture_diff(repo, base.sha, 100).data)

    _git(repo, "config", "core.quotepath", "false")
    unquoted = diff_identity(capture_diff(repo, base.sha, 100).data)

    # not vacuous: both hashes are well-formed 40-char hex digests...
    for h in (quoted, unquoted):
        assert len(h) == 40
        int(h, 16)
    # ...and they differ, which is the whole point of this test.
    assert quoted != unquoted


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


def _exec_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_diff_flags_suppress_textconv_and_external_drivers(tmp_path):
    """`--no-ext-diff --no-textconv` are load-bearing, so pin them with a test.

    Without them the hashed bytes are a function of the developer's
    `.gitattributes` + `diff.<driver>.*` config: the gate would enforce an
    identity nobody else can reproduce, and the reviewer would be shown a
    transformed view of the change rather than the change. Both drivers are
    asserted LIVE first (raw `git diff` output shows them), so this test cannot
    pass by the drivers simply never firing.
    """
    repo = _mkrepo(tmp_path)
    # .gitattributes lands in the BASE commit so it is not itself part of the diff
    (repo / ".gitattributes").write_text(
        "tc.txt diff=tcdrv\next.txt diff=extdrv\nu-new.txt diff=tcdrv\n", encoding="utf-8"
    )
    (repo / "tc.txt").write_text("textconv target\n", encoding="utf-8")
    (repo / "ext.txt").write_text("external target\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "attrs")
    _git(repo, "checkout", "-b", "feat")
    (repo / "tc.txt").write_text("textconv target EDITED\n", encoding="utf-8")
    (repo / "ext.txt").write_text("external target EDITED\n", encoding="utf-8")
    (repo / "u-new.txt").write_text("untracked\n", encoding="utf-8")  # --no-index path
    base = resolve_base(repo)
    before = capture_diff(repo, base.sha, untracked_max=100)
    assert before.data.startswith(b"diff --git ")

    # Scripts live outside the repo so they are not themselves untracked content.
    tc = _exec_script(tmp_path / "textconv.sh", '#!/bin/sh\nsed "s/^/TEXTCONV-/" "$1"\n')
    ext = _exec_script(tmp_path / "extdiff.sh", "#!/bin/sh\necho EXTERNAL-DIFF-RAN\n")
    _git(repo, "config", "diff.tcdrv.textconv", str(tc))
    _git(repo, "config", "diff.extdrv.command", str(ext))

    # Both drivers are live: git's own output changes once they are configured.
    raw = subprocess.run(
        ["git", "-C", str(repo), "--no-pager", "diff", base.sha], capture_output=True
    ).stdout
    assert b"TEXTCONV-" in raw, "textconv driver did not fire; test would be vacuous"
    assert b"EXTERNAL-DIFF-RAN" in raw, "external diff did not fire; test would be vacuous"
    raw_untracked = subprocess.run(
        ["git", "-C", str(repo), "--no-pager", "diff", "--no-index", "--", "/dev/null", "u-new.txt"],
        capture_output=True,
    ).stdout
    assert b"TEXTCONV-" in raw_untracked, "textconv did not fire on --no-index"

    after = capture_diff(repo, base.sha, untracked_max=100)
    assert after.data == before.data
    assert diff_identity(after.data) == diff_identity(before.data)
    assert b"TEXTCONV-" not in after.data
    assert b"EXTERNAL-DIFF-RAN" not in after.data
    assert b"textconv target EDITED" in after.data  # the real content survived


def _subdir_repo(tmp_path: Path) -> Path:
    """A repo whose change spans the root and a subdirectory, incl. untracked."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "sub").mkdir()
    (repo / "sub" / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    _git(repo, "checkout", "-b", "feat2")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")  # tracked, at the root
    (repo / "sub" / "kept.txt").write_text("kept edited\n", encoding="utf-8")  # tracked, in sub
    (repo / "sub" / "new.txt").write_text("nu\n", encoding="utf-8")  # untracked, in sub
    return repo


def test_capture_diff_normalises_subdirectory_to_worktree_root(tmp_path):
    """`git diff` paths are root-relative, `ls-files --others` paths are
    cwd-relative — capturing from a subdirectory must not mix the two bases."""
    repo = _subdir_repo(tmp_path)
    base = resolve_base(repo)
    from_root = capture_diff(repo, base.sha, untracked_max=100)
    from_sub = capture_diff(repo / "sub", base.sha, untracked_max=100)
    assert from_sub.data == from_root.data
    assert from_sub.files == from_root.files
    assert from_sub.statuses == from_root.statuses
    assert diff_identity(from_sub.data) == diff_identity(from_root.data)
    # not vacuous: the untracked entry is the one whose base would have differed
    assert "sub/new.txt" in from_sub.files
    # every listed path opens relative to the worktree root
    for f in from_sub.files:
        assert (repo / f).exists(), f"{f!r} is not openable from the worktree root"


# --------------------------------------------------------------------------
# ref-range diff scope (Task 5: the background dispatcher's reads)
#
# `capture_ref_diff`/`resolve_ref_base` are the PUSHED-ref analogues of
# `capture_diff`/`resolve_base`: a background review is of `base_sha..
# local_oid` -- commits only -- because the ref being reviewed need not be
# checked out at all, and its working tree (if any) may since have moved on.
# --------------------------------------------------------------------------


def test_ref_diff_excludes_untracked_and_worktree_edits(tmp_path):
    repo = _mkrepo(tmp_path)  # c0: a.txt == "one\n"
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c1")
    local_oid = _git(repo, "rev-parse", "HEAD")

    # Dirty the worktree AFTER local_oid is committed: an untracked file plus
    # a further uncommitted edit to the tracked file. Neither was pushed.
    (repo / "a.txt").write_text("DIRTY WORKTREE\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("should never appear\n", encoding="utf-8")

    d = capture_ref_diff(repo, base_sha, local_oid)
    assert d.statuses == {"a.txt": "M"}
    assert d.files == ["a.txt"]
    assert b"two" in d.data  # the committed content is there...
    assert b"DIRTY WORKTREE" not in d.data  # ...the worktree edit is not...
    assert b"should never appear" not in d.data  # ...and neither is the untracked file
    assert "untracked.txt" not in d.files
    assert d.truncated_untracked is False


def test_ref_diff_same_content_pushed_twice_gives_identical_identity(tmp_path):
    """Two distinct commits (different oids, e.g. a re-push after a rebase)
    carrying the SAME net content diff against the same base must hash the
    same -- the identity is a function of diff bytes, not of the oid."""
    repo = _mkrepo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c1")
    first_oid = _git(repo, "rev-parse", "HEAD")

    _git(repo, "reset", "--hard", base_sha)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c1-repushed")
    second_oid = _git(repo, "rev-parse", "HEAD")
    assert first_oid != second_oid  # not vacuous: genuinely different oids

    d1 = capture_ref_diff(repo, base_sha, first_oid)
    d2 = capture_ref_diff(repo, base_sha, second_oid)
    assert diff_identity(d1.data) == diff_identity(d2.data)


def test_ref_diff_rename_record_parsed_between_two_oids(tmp_path):
    """`R100\\0old\\0new` between two committed oids -- no working tree or
    index involved at all -- is still a three-token record; new name wins."""
    repo = _mkrepo(tmp_path)
    (repo / "old.txt").write_text("".join(f"line {i}\n" for i in range(40)), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    base_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "mv", "old.txt", "new.txt")
    (repo / "tail.txt").write_text("t\n", encoding="utf-8")  # 4th token must not desync
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2-rename")
    local_oid = _git(repo, "rev-parse", "HEAD")

    d = capture_ref_diff(repo, base_sha, local_oid)
    assert d.statuses.get("new.txt") == "R"
    assert "old.txt" not in d.statuses  # old name never leaks into the file list
    assert d.statuses.get("tail.txt") == "A"  # parser stayed in sync after 3 tokens


def test_ref_diff_uses_local_oid_not_checked_out_head(tmp_path):
    """The pushed oid is deliberately OLDER than the checked-out HEAD: a
    mutant that swapped `local_oid` for `HEAD` would diff against the wrong
    (newer) commit, and this test would see the newer commit's bytes instead
    of the older, actually-pushed one. A same-as-HEAD fixture could not tell
    the two apart."""
    repo = _mkrepo(tmp_path)  # c0: a.txt == "one\n"
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("OLDER PUSHED VALUE\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c1-pushed")
    older_oid = _git(repo, "rev-parse", "HEAD")  # the oid that was actually pushed

    # Checked-out HEAD moves further, past the pushed oid.
    (repo / "a.txt").write_text("NEWER CHECKED OUT VALUE\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c2-local-only")
    checked_out_head = _git(repo, "rev-parse", "HEAD")
    assert checked_out_head != older_oid  # not vacuous

    d = capture_ref_diff(repo, base_sha, older_oid)
    assert b"OLDER PUSHED VALUE" in d.data
    assert b"NEWER CHECKED OUT VALUE" not in d.data


def test_ref_diff_no_textconv_suppresses_textconv_driver(tmp_path):
    """`--no-textconv` is load-bearing for `capture_ref_diff` too: without it
    the hashed pushed-ref diff would depend on the repo's `.gitattributes` +
    `diff.<driver>.textconv` config, not the pushed bytes. The driver is
    asserted LIVE first (raw `git diff` output shows it), so this cannot pass
    by the driver simply never firing."""
    repo = _mkrepo(tmp_path)
    # .gitattributes lands in the BASE commit so it is not itself part of the diff
    (repo / ".gitattributes").write_text("tc.txt diff=tcdrv\n", encoding="utf-8")
    (repo / "tc.txt").write_text("textconv target\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "attrs")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "tc.txt").write_text("textconv target EDITED\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "edit")
    local_oid = _git(repo, "rev-parse", "HEAD")

    tc = _exec_script(tmp_path / "textconv.sh", '#!/bin/sh\nsed "s/^/TEXTCONV-/" "$1"\n')
    _git(repo, "config", "diff.tcdrv.textconv", str(tc))

    raw = subprocess.run(
        ["git", "-C", str(repo), "--no-pager", "diff", base_sha, local_oid],
        capture_output=True,
    ).stdout
    assert b"TEXTCONV-" in raw, "textconv driver did not fire; test would be vacuous"

    d = capture_ref_diff(repo, base_sha, local_oid)
    assert b"TEXTCONV-" not in d.data
    assert b"textconv target EDITED" in d.data  # the real content survived


def test_ref_base_merge_base_uses_local_oid_not_checked_out_head(tmp_path):
    """`resolve_ref_base` merge-bases against `local_oid`, never the checked-
    out `HEAD` -- pinned by a fixture where the two give DIFFERENT merge-bases
    with `main`, so a mutant swapping one for the other returns the wrong sha
    rather than merely a coincidentally-matching one."""
    repo = _mkrepo(tmp_path)  # c0 on main
    c0 = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("m1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main-c1")
    c1 = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("m2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main-c2")  # main tip now c2

    # "feat": the PUSHED branch (never checked out again after this) --
    # branches off main at c1.
    _git(repo, "checkout", c1)
    _git(repo, "checkout", "-b", "feat")
    (repo / "feat.txt").write_text("f\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat-1")
    local_oid = _git(repo, "rev-parse", "HEAD")

    # The checked-out HEAD ends up somewhere else entirely: "other" branches
    # off main's ROOT commit c0, so ITS merge-base with main is c0, not c1.
    _git(repo, "checkout", c0)
    _git(repo, "checkout", "-b", "other")
    (repo / "other.txt").write_text("o\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "other-1")
    assert _git(repo, "rev-parse", "HEAD") != local_oid  # not vacuous

    # Precondition: the two merge-bases really do differ.
    assert _git(repo, "merge-base", "main", local_oid) == c1
    assert _git(repo, "merge-base", "main", "HEAD") == c0

    base = resolve_ref_base(repo, local_oid)
    assert base == Base(ref="main", sha=c1)


def test_ref_base_falls_back_with_warning_relative_to_local_oid(tmp_path):
    """No main ref resolves: the fallback is `local_oid^`, never `HEAD^` --
    pinned by making the checked-out HEAD's parent differ from local_oid's."""
    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("m1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main-c1")
    _git(repo, "checkout", "-b", "feat")
    (repo / "feat.txt").write_text("f\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat-1")
    local_oid = _git(repo, "rev-parse", "HEAD")
    local_oid_parent = _git(repo, "rev-parse", f"{local_oid}^")

    # Checked-out HEAD moves on past local_oid, so HEAD^ != local_oid^.
    (repo / "feat.txt").write_text("f2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat-2")
    assert _git(repo, "rev-parse", "HEAD^") != local_oid_parent  # not vacuous

    _git(repo, "branch", "-D", "main")  # no candidate main ref resolves

    base = resolve_ref_base(repo, local_oid)
    assert base.ref == f"{local_oid}^"
    assert base.sha == local_oid_parent
    assert base.warning is not None


def test_ref_base_single_commit_repo_falls_back_to_local_oid_itself(tmp_path):
    repo = _mkrepo(tmp_path)  # exactly one commit; local_oid^ does not exist
    local_oid = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feat")  # so "main" is not the checked-out branch
    _git(repo, "branch", "-D", "main")
    base = resolve_ref_base(repo, local_oid)
    assert base.ref == local_oid
    assert base.sha == local_oid
    assert base.warning is not None


def test_ref_base_first_existing_candidate_wins_no_fallthrough(tmp_path):
    """ORACLE PARITY (mirrored): candidate selection is existence-only, not
    merge-base-able -- `github/main` exists but shares no history, so
    `resolve_ref_base` falls straight to `local_oid^` without ever trying
    `origin/main`/`main`, exactly as `resolve_base` does for `HEAD`."""
    repo = _unrelated_history_repo(tmp_path)
    local_oid = _git(repo, "rev-parse", "HEAD")
    base = resolve_ref_base(repo, local_oid)
    # main WOULD have produced a merge-base -- proving the fallback is not vacuous.
    assert _git(repo, "merge-base", "main", local_oid)
    assert base.ref == f"{local_oid}^"
    assert base.sha == _git(repo, "rev-parse", f"{local_oid}^")
    assert base.warning is not None


# --------------------------------------------------------------------------
# error surface
# --------------------------------------------------------------------------


def test_git_failure_raises_giterror_carrying_the_command(tmp_path):
    """A failing git invocation must surface as `GitError`, not a silent empty
    result that a caller could mistake for "no change"."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    precheck = subprocess.run(
        ["git", "-C", str(plain), "rev-parse", "--git-dir"], capture_output=True
    )
    assert precheck.returncode != 0, "precondition: the directory must not be a git repo"
    with pytest.raises(GitError) as excinfo:
        resolve_base(plain)
    assert "rev-parse" in str(excinfo.value)  # the failing command is named
    assert "rc=" in str(excinfo.value)


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

    The divergence exists under `core.quotepath=true` (git's default), which
    `_mkrepo` pins repo-locally — so this test asserts a property of THAT
    configuration rather than of whatever the runner's git happens to be set to.
    Under `core.quotepath=false` the oracle would see the raw name, its `[ -f ]`
    guard would pass, and the hashes would agree.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "ä-new.txt").write_text("umlaut\n", encoding="utf-8")  # untracked
    # The precondition, asserted behaviourally rather than by reading config:
    # the text-mode listing the oracle consumes really does quote the name.
    assert _git(repo, "config", "--get", "core.quotepath") == "true"
    assert '"\\303\\244-new.txt"' in _git(repo, "ls-files", "--others", "--exclude-standard")
    d = capture_diff(repo, resolve_base(repo).sha, 100)
    assert b"umlaut" in d.data  # skodun reviews it...
    assert "ä-new.txt" in d.files
    assert diff_identity(d.data) != _oracle(repo, "--diff-hash")  # ...the oracle does not
    # and the divergence is exactly the dropped section, nothing else
    without = capture_diff(repo, resolve_base(repo).sha, 0)
    assert diff_identity(without.data) == _oracle(repo, "--diff-hash")


@requires_oracle
def test_diff_identity_parity_untracked_all_skipped(tmp_path):
    """The pure-separator branch: the untracked LIST is non-empty but every
    entry fails `[ -f ]`, so no separator is appended. The oracle is the
    authority that a bare `\\n` must not be joined on here."""
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "z-dangling.lnk").symlink_to("/definitely/missing/target")
    listing = _git(repo, "ls-files", "--others", "--exclude-standard")
    assert listing.splitlines() == ["z-dangling.lnk"]  # non-empty, and ONLY the symlink
    d = capture_diff(repo, resolve_base(repo).sha, 100)
    assert "z-dangling.lnk" not in d.files
    assert not d.data.endswith(b"\n")
    assert diff_identity(d.data) == _oracle(repo, "--diff-hash")


@requires_oracle
def test_diff_identity_parity_from_subdirectory(tmp_path):
    """The oracle self-normalises with `cd "$(git rev-parse --show-toplevel)"`;
    capturing from a subdirectory must land on the same hash it does."""
    repo = _subdir_repo(tmp_path)
    base = resolve_base(repo)
    from_sub = capture_diff(repo / "sub", base.sha, 100)
    assert diff_identity(from_sub.data) == _oracle(repo / "sub", "--diff-hash")
    assert diff_identity(from_sub.data) == _oracle(repo, "--diff-hash")


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


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/Acme/Project.git", "github.com/Acme/Project"),
    ("https://github.com:443/Acme/Project.git", "github.com/Acme/Project"),
    ("HTTPS://github.com:443/Acme/Project.git", "github.com/Acme/Project"),
    ("git@github.com:Acme/Project.git", "github.com/Acme/Project"),
    ("ssh://git@github.com/Acme/Project.git", "github.com/Acme/Project"),
    ("ssh://git@github.com:22/Acme/Project.git", "github.com/Acme/Project"),
    ("SSH://git@github.com:22/Acme/Project.git", "github.com/Acme/Project"),
])
def test_canonical_repository_identity_normalizes_supported_remotes(
        tmp_path, url, expected):
    repo = _mkrepo(tmp_path)
    _git(repo, "remote", "add", "origin", url)

    assert canonical_repository_identity(repo) == expected


@pytest.mark.parametrize("url", [
    "/tmp/project.git",
    "file:///tmp/project.git",
    "https://user:secret@example.com/acme/project.git",
    "user:secret@example.com:acme/project.git",
    "https://example.com/acme/../project.git",
    "https://example.com/acme/project.git?token=secret",
    "https://example.com/acme/project.git#fragment",
    "https://example.com/acme/control\x01project.git",
])
def test_canonical_repository_identity_refuses_unportable_or_unsafe_remotes(
        tmp_path, url):
    repo = _mkrepo(tmp_path)
    _git(repo, "remote", "add", "origin", url)

    assert canonical_repository_identity(repo) is None


def test_canonical_repository_identity_never_guesses_without_origin(tmp_path):
    repo = _mkrepo(tmp_path)

    assert canonical_repository_identity(repo) is None


def test_exact_commit_exists_accepts_only_full_commit_object_ids(tmp_path):
    repo = _mkrepo(tmp_path)
    commit = head_sha(repo)
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=b"blob\n", capture_output=True, check=True,
    ).stdout.decode().strip()

    assert exact_commit_exists(repo, commit) is True
    assert exact_commit_exists(repo, blob) is False
    assert exact_commit_exists(repo, commit[:12]) is False
    assert exact_commit_exists(repo, "f" * 40) is False
    assert exact_commit_exists(repo, "--help") is False


def test_is_ancestor_accepts_only_ordered_full_commit_object_ids(tmp_path):
    repo = _mkrepo(tmp_path)
    older = head_sha(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "c1")
    newer = head_sha(repo)

    assert is_ancestor(repo, older, newer) is True
    assert is_ancestor(repo, newer, older) is False
    # Git treats an object as its own ancestor; callers needing a strict edge
    # must compare the ids separately, as stack.validate does.
    assert is_ancestor(repo, older, older) is True
    assert is_ancestor(repo, older[:12], newer) is False
    assert is_ancestor(repo, "f" * 40, newer) is False


def test_current_branch_and_head_sha(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    assert current_branch(repo) == "feat"
    assert head_sha(repo) == _git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# object-store blob reads
#
# These are the background dispatcher's reads. It reviews a ref that has been
# PUSHED, so the content it packs must come from the pushed commit's tree; the
# working tree is a different thing that may have moved on, and reading it
# would review code nobody pushed. Every failure here degrades to None so the
# packer can turn it into an omission reason instead of an exception.
# --------------------------------------------------------------------------


def test_blob_bytes_reads_the_committed_bytes_not_the_worktree(tmp_path):
    repo = _mkrepo(tmp_path)  # a.txt == "one\n", committed
    oid = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("WORKTREE\n", encoding="utf-8")

    assert blob_bytes(repo, oid, "a.txt") == b"one\n"
    assert blob_size(repo, oid, "a.txt") == 4
    # Not vacuous: the working tree really does hold something else.
    assert (repo / "a.txt").read_bytes() == b"WORKTREE\n"
    # ...and staging the edit does not move the answer either.
    _git(repo, "add", "a.txt")
    assert blob_bytes(repo, oid, "a.txt") == b"one\n"


def test_blob_bytes_returns_none_for_every_failure(tmp_path):
    repo = _mkrepo(tmp_path)
    oid = _git(repo, "rev-parse", "HEAD")

    assert blob_bytes(repo, oid, "nope.txt") is None       # not in that tree
    assert blob_size(repo, oid, "nope.txt") is None
    assert blob_bytes(repo, "0" * 40, "a.txt") is None     # well-formed, absent
    assert blob_bytes(repo, "not-an-oid", "a.txt") is None  # unresolvable
    assert blob_size(repo, "0" * 40, "a.txt") is None
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert blob_bytes(plain, oid, "a.txt") is None         # no repo at all
    # An embedded NUL makes `subprocess` raise `ValueError`, which is NOT an
    # `OSError`: catching only `OSError` would let it escape and kill a review
    # over one bad filename.
    assert blob_bytes(repo, oid, "a\x00b.txt") is None
    assert blob_size(repo, oid, "a\x00b.txt") is None


def test_blob_bytes_refuses_an_empty_rev_rather_than_reading_the_index(tmp_path):
    """`<oid>:<path>` with an empty oid is `:<path>` — the INDEX, not a tree.

    This is why the guard exists rather than being left to git: an empty (or
    colon-carrying) oid does not fail, it silently answers from the staged
    state, which is exactly the not-yet-pushed content an object read exists to
    avoid. The precondition assertion below drives the real git to prove it.
    """
    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("STAGED\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    for arg in (":a.txt", ":0:a.txt"):
        probe = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", arg], capture_output=True
        )
        assert probe.returncode == 0 and probe.stdout == b"STAGED\n", arg

    for bad in ("", ":", ":0", "-x", "--version"):
        assert blob_bytes(repo, bad, "a.txt") is None, bad
        assert blob_size(repo, bad, "a.txt") is None, bad


def test_blob_bytes_prefix_read_stops_at_max_bytes(tmp_path):
    """A peek must not buffer the whole blob: a committed 200 MB video is one
    `git diff --name-only` entry like any other."""
    repo = _mkrepo(tmp_path)
    (repo / "big.txt").write_text("x" * 5000, encoding="utf-8")
    (repo / "empty.txt").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    oid = _git(repo, "rev-parse", "HEAD")

    assert blob_bytes(repo, oid, "big.txt", max_bytes=10) == b"x" * 10
    assert blob_size(repo, oid, "big.txt") == 5000
    assert len(blob_bytes(repo, oid, "big.txt")) == 5000
    # An empty blob must come back as b"", distinguishable from a missing one:
    # the packer reports the second as `missing` and packs the first.
    assert blob_bytes(repo, oid, "empty.txt", max_bytes=10) == b""
    assert blob_bytes(repo, oid, "empty.txt") == b""
    assert blob_size(repo, oid, "empty.txt") == 0
    assert blob_bytes(repo, oid, "nope.txt", max_bytes=10) is None


def test_blob_reads_are_tree_root_relative_and_refuse_cwd_relative_paths(tmp_path):
    """`<rev>:<path>` is tree-root-relative — EXCEPT for a `./` or `../` path,
    which git resolves against the cwd and which therefore names a different
    blob depending on which directory of the repo the call was made from. Such
    a path is refused rather than silently resolved, so `repo` never has to be
    normalised to the worktree root the way `capture_diff` must normalise it.
    """
    repo = _mkrepo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    oid = _git(repo, "rev-parse", "HEAD")

    # Same answer from the root and from a subdirectory.
    assert blob_bytes(repo, oid, "sub/nested.txt") == b"nested\n"
    assert blob_bytes(repo / "sub", oid, "sub/nested.txt") == b"nested\n"
    assert blob_bytes(repo / "sub", oid, "a.txt") == b"one\n"
    # `../a.txt` from `sub/` is the one git WOULD resolve (to `a.txt`).
    for bad in ("../a.txt", "./a.txt", "/a.txt", "", ".", ".."):
        assert blob_bytes(repo / "sub", oid, bad) is None, bad
        assert blob_size(repo / "sub", oid, bad) is None, bad


def test_blob_bytes_reads_a_non_utf8_path(tmp_path):
    """A path git hands us as surrogate-escaped text must still name its blob.

    The tree entry is built through the index rather than the filesystem: APFS
    and others refuse a filename that is not valid UTF-8, so a test that wrote
    one would pass on some machines and skip on others. `gitio._paths` decodes
    git's path stream with `surrogateescape` precisely so the bytes round-trip
    back out through `subprocess`, and this is where that round-trip pays off.
    """
    repo = _mkrepo(tmp_path)
    name = b"caf\xe9.txt".decode("utf-8", "surrogateescape")
    sha = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=b"nonascii-blob\n", capture_output=True, check=True,
    ).stdout.decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{name}")
    tree = _git(repo, "write-tree")
    oid = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "nonascii")

    assert blob_bytes(repo, oid, name) == b"nonascii-blob\n"
    assert blob_size(repo, oid, name) == len(b"nonascii-blob\n")


def test_blob_bytes_of_a_symlink_is_its_target_path_not_the_target_file(tmp_path):
    """An object read cannot traverse a symlink: git stores one as a blob whose
    content is the target *string*, so what comes back is path text, never the
    bytes of whatever it points at. That is why the working-tree packer's
    symlink hardening has no analogue on the object side.
    """
    repo = _mkrepo(tmp_path)
    (repo / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    os.symlink(repo / "secret.txt", repo / "link.txt")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    oid = _git(repo, "rev-parse", "HEAD")

    got = blob_bytes(repo, oid, "link.txt")
    assert got == str(repo / "secret.txt").encode("utf-8")
    assert b"SECRET" not in got


def test_a_tree_path_has_a_size_but_no_blob_bytes(tmp_path):
    """`cat-file -s` answers for ANY object type, a directory included, while
    only a blob has blob bytes. Pinned because the packer's classify step
    trusts the size probe: a directory therefore reaches a blob read, and that
    read is what rejects it. The oracle takes the same route (its own size probe
    answers for the tree too), so the packed bytes still match — see
    `test_oracle_parity_oid_source`.

    BOTH read forms reject it, and that consistency is the point. `b""` from a
    prefix read does not mean "not a blob" anywhere in this module — it means
    "an empty committed file", which the packer packs. A limited read that
    answered `b""` for a directory would hand a caller a phantom empty file
    whose only defence was that a second, unlimited read happened to run later.
    """
    repo = _mkrepo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c1")
    oid = _git(repo, "rev-parse", "HEAD")

    assert blob_size(repo, oid, "sub") is not None
    assert blob_bytes(repo, oid, "sub") is None
    assert blob_bytes(repo, oid, "sub", max_bytes=8192) is None
    # The zero-length peek takes its own code path and must agree.
    assert blob_bytes(repo, oid, "sub", max_bytes=0) is None
    # ...while an EMPTY BLOB still reads as b"" on every one of those forms:
    # this rejection must not be "anything with no bytes is absent".
    (repo / "empty.txt").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c2")
    oid2 = _git(repo, "rev-parse", "HEAD")
    assert blob_bytes(repo, oid2, "empty.txt") == b""
    assert blob_bytes(repo, oid2, "empty.txt", max_bytes=8192) == b""
    assert blob_bytes(repo, oid2, "empty.txt", max_bytes=0) == b""


def test_a_submodule_path_is_not_a_blob_on_either_read_form(tmp_path):
    """A gitlink names a COMMIT object, so where that object is present
    `cat-file -s` sizes it too. Same rule as a directory: not a blob, so
    neither read form invents bytes for it.

    The "is present" is a real qualifier, not pedantry: `submodule add` puts
    the inner repo's objects under the outer repo's `.git/modules/`, NOT in
    the outer repo's own object database, so `cat-file -s HEAD:mod` there
    normally fails and `blob_size` correctly answers None. The `fetch` below
    is what genuinely puts that commit in the outer ODB, and it is what makes
    the size assertion true BY CONSTRUCTION. Without it the assertion passed
    only by accident, and only sometimes: `_mkrepo` gives both repos identical
    content, author, committer and message, so their initial commits get the
    same sha whenever the two `git commit` calls land in the same clock second
    — and the gitlink then happens to name an object the outer repo already
    had. Straddle a second boundary and the shas differ and it fails (~2 runs
    in 10). The three `blob_bytes` assertions never depended on any of this:
    a gitlink is not a blob whether or not its object is reachable.
    """
    (tmp_path / "inner").mkdir()
    (tmp_path / "outer").mkdir()
    inner = _mkrepo(tmp_path / "inner")
    repo = _mkrepo(tmp_path / "outer")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add",
         str(inner), "mod")
    _git(repo, "commit", "-m", "add submodule")
    oid = _git(repo, "rev-parse", "HEAD")
    # See docstring: this is what puts HEAD:mod in THIS repo's ODB. The
    # `protocol.file.allow` pin is for the same reason as the one above, but
    # not for the same default: a direct fetch from a local path is allowed
    # under git's default `user` policy (only submodule-spawned child
    # processes are refused there, which is what forces the pin on `submodule
    # add`). Pinned anyway so an ambient `never` — the one setting that does
    # refuse this fetch — cannot make the test a property of the machine.
    _git(repo, "-c", "protocol.file.allow=always", "fetch", str(inner), "HEAD")
    assert _git(repo, "cat-file", "-t", f"{oid}:mod") == "commit"

    assert blob_size(repo, oid, "mod") is not None
    assert blob_bytes(repo, oid, "mod") is None
    assert blob_bytes(repo, oid, "mod", max_bytes=8192) is None
    assert blob_bytes(repo, oid, "mod", max_bytes=0) is None


def test_blob_reads_accept_a_ref_not_only_a_sha(tmp_path):
    """The dispatcher passes a sha, but nothing in the contract narrows the rev
    to one: `HEAD`, a branch and a tag all resolve, which is what lets tests
    and future callers name a commit the way git does."""
    repo = _mkrepo(tmp_path)
    for rev in ("HEAD", "main", _git(repo, "rev-parse", "HEAD")[:8]):
        assert blob_bytes(repo, rev, "a.txt") == b"one\n", rev
