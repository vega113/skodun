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


def test_current_branch_and_head_sha(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    assert current_branch(repo) == "feat"
    assert head_sha(repo) == _git(repo, "rev-parse", "HEAD")
