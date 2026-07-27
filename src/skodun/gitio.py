"""Git IO: base resolution, diff capture, and the diff identity hash.

`diff_identity` is the join key between every review skodun stores and every
record the legacy shell reviewer already wrote. It is defined by the oracle's
observable behaviour, not by convenience: the oracle builds the diff in shell
`$(...)` command substitutions, and every capture that passes through one loses
ALL of its trailing newlines. Reproducing that byte-for-byte is the whole point
of this module — see the ORACLE PARITY notes below, each pinned by a test in
`tests/test_gitio.py` that shells out to the real oracle.

`--no-ext-diff --no-textconv` are mandatory on every diff invocation: `git diff`
otherwise honours `diff.external` and per-driver `textconv` from config and
`.gitattributes`, so the hashed bytes would depend on the developer's machine
and the gate would enforce an identity nobody else can reproduce.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Mandatory on every diff: keeps the hashed bytes independent of local config.
_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")


class GitError(RuntimeError):
    """A git invocation exited with a code the caller did not allow."""


def _run(repo: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if cp.returncode not in ok_codes:
        raise GitError(
            f"git {' '.join(args)}: rc={cp.returncode} "
            f"{cp.stderr.decode('utf-8', 'replace').strip()}"
        )
    return cp


def _out(repo: Path, *args: str) -> str:
    return _run(repo, *args).stdout.decode("utf-8", "replace").strip()


def _paths(raw: bytes) -> list[str]:
    """Split a NUL-delimited git path stream into tokens.

    `surrogateescape` (not `replace`) so a path that is not valid UTF-8 still
    round-trips back through `subprocess` unchanged; `replace` would turn it
    into U+FFFD and we would then diff — or open — the wrong file.
    """
    return raw.decode("utf-8", "surrogateescape").split("\0")


def blob_sha1(data: bytes) -> str:
    """git's blob object id for `data` (== `git hash-object --stdin`)."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def diff_identity(data: bytes) -> str:
    """THE diff-hash function. Never call `blob_sha1` on diff bytes directly.

    ORACLE PARITY: the oracle hashes `printf '%s' "$DIFF"` where `$DIFF` came
    out of a `$(...)` capture, which strips ALL trailing newlines. Hashing raw
    captured bytes yields a hash the legacy archive has never seen, breaking
    legacy import joins and shadow-compare.
    """
    return blob_sha1(data.rstrip(b"\n"))


@dataclass(frozen=True)
class Base:
    ref: str
    sha: str
    warning: str | None = None


_FALLBACK_WARNING = (
    "no main ref (github/main|origin/main|main) with a merge-base found; "
    "reviewing {ref}..HEAD + local edits only -- a multi-commit branch may be "
    "under-reviewed"
)


def resolve_base(repo: Path) -> Base:
    """Resolve the base the outgoing change is computed against.

    ORACLE PARITY: candidate selection is *existence-only*. The oracle takes the
    first of `github/main`/`origin/main`/`main` that resolves and `break`s; if
    that ref has no merge-base with HEAD it falls straight through to `HEAD^`
    rather than trying the next candidate. Skipping to the next candidate would
    pick a different base_sha — and therefore a different diff and a different
    diff_hash — than the oracle for any repo with an unrelated `github/main`.
    """
    base_ref = ""
    for cand in ("github/main", "origin/main", "main"):
        if _run(repo, "rev-parse", "--verify", "-q", cand, ok_codes=(0, 1)).returncode == 0:
            base_ref = cand
            break

    if base_ref:
        mb = _run(repo, "merge-base", base_ref, "HEAD", ok_codes=(0, 1, 128))
        sha = mb.stdout.decode("utf-8", "replace").strip() if mb.returncode == 0 else ""
        if sha:
            return Base(ref=base_ref, sha=sha)

    # No main ref, or one that shares no history: fall back to the previous
    # commit. That reviews only HEAD^..HEAD + local edits, so warn loudly rather
    # than silently reviewing a subset.
    parent = _run(repo, "rev-parse", "--verify", "-q", "HEAD^", ok_codes=(0, 1, 128))
    if parent.returncode == 0:
        return Base(
            ref="HEAD^",
            sha=parent.stdout.decode("utf-8", "replace").strip(),
            warning=_FALLBACK_WARNING.format(ref="HEAD^"),
        )
    # Single-commit repo: HEAD^ does not exist.
    return Base(
        ref="HEAD",
        sha=_out(repo, "rev-parse", "HEAD"),
        warning=_FALLBACK_WARNING.format(ref="HEAD"),
    )


@dataclass(frozen=True)
class Diff:
    data: bytes
    files: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    truncated_untracked: bool = False


def _tracked_statuses(repo: Path, base_sha: str) -> dict[str, str]:
    """path -> one-letter status from `git diff --name-status -z`.

    NUL-delimited, never text-mode + `.strip()`: under default `core.quotepath`
    git renders non-ASCII names as `"\\303\\244.txt"` in text mode, and stripping
    would eat filenames' real leading/trailing spaces. Records are `X\0path\0`
    except rename AND copy (`R`/`C`), which carry `old\0new` — the new name wins.
    """
    toks = _paths(_run(repo, "diff", *_DIFF_FLAGS, "--name-status", "-z", base_sha).stdout)
    statuses: dict[str, str] = {}
    i = 0
    while i < len(toks) and toks[i]:
        code = toks[i][:1]
        two_path = code in ("R", "C")
        path = toks[i + 2] if two_path else toks[i + 1]
        statuses[path] = code
        i += 3 if two_path else 2
    return statuses


def capture_diff(repo: Path, base_sha: str, untracked_max: int) -> Diff:
    """Working tree vs `base_sha`, including untracked files, capped.

    ORACLE PARITY, three separate points:
      1. Each `$(...)` capture strips trailing newlines, so the tracked section
         and the *whole* untracked section are each right-stripped.
      2. The untracked section is the RAW concatenation of the per-file
         `--no-index` diffs (the oracle's `while` loop writes to one capture);
         it is joined to the tracked section with exactly one `\\n`, and only
         when it is non-empty. An untracked-only change therefore starts with a
         leading `\\n` (empty tracked section), while a tracked-only change gains
         no trailing separator.
      3. The oracle guards each untracked entry with `[ -f "$_uf" ]`, so
         dangling symlinks (and anything not a regular file) contribute nothing
         — even though `git diff --no-index` would happily emit a section for
         them. Such entries are left out of `files`/`statuses` too: they carry
         no diff bytes, and Task 9's context packer could not open them.

    ONE DELIBERATE DIVERGENCE from the oracle, pinned by
    `test_known_divergence_untracked_nonascii_name`: the oracle lists untracked
    files in TEXT mode, so `core.quotepath` hands its `[ -f ]` guard the literal
    string `"\\303\\244.txt"`, the test fails, and an untracked file with a
    non-ASCII name is dropped from the reviewed diff entirely — never reviewed.
    skodun reads NUL-delimited names and includes it, so the hashes differ for
    exactly that input. The direction is safe (a legacy record for such a diff
    fails to join and skodun asks for a fresh review; it never skips one), and
    reproducing the bug would mean emulating git's `quote_c_style` in Python —
    a new and larger source of divergence.
    Untracked order is git's own (`ls-files` output order, then `head -n MAX`) —
    not re-sorted in Python, so the capped subset can never diverge from the
    oracle's on a locale or code-point-vs-byte ordering difference.
    """
    tracked = _run(repo, "--no-pager", "diff", *_DIFF_FLAGS, base_sha).stdout
    statuses = _tracked_statuses(repo, base_sha)
    files = list(statuses)

    all_untracked = [
        f
        for f in _paths(_run(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout)
        if f
    ]
    truncated = len(all_untracked) > untracked_max

    udiff = b""
    for f in all_untracked[:untracked_max]:
        if not (repo / f).is_file():  # `[ -f ]`: follows symlinks, rejects dangling
            continue
        cp = _run(
            repo, "--no-pager", "diff", *_DIFF_FLAGS, "--no-index", "--", "/dev/null", f,
            ok_codes=(0, 1),
        )
        if not cp.stdout:
            continue
        udiff += cp.stdout
        files.append(f)
        statuses[f] = "A"

    data = tracked.rstrip(b"\n")
    udiff = udiff.rstrip(b"\n")
    if udiff:
        data = data + b"\n" + udiff
    return Diff(data=data, files=files, statuses=statuses, truncated_untracked=truncated)


def git_common_dir(repo: Path) -> Path:
    """The shared git dir — identical for a repo and all of its worktrees."""
    return Path(_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def current_branch(repo: Path) -> str:
    return _out(repo, "rev-parse", "--abbrev-ref", "HEAD")


def head_sha(repo: Path) -> str:
    return _out(repo, "rev-parse", "HEAD")


def is_primary_checkout(repo: Path) -> bool:
    """True iff `repo` is the primary checkout rather than a linked worktree.

    Compares the resolved `--git-dir` against the resolved `--git-common-dir`;
    they differ exactly for linked worktrees. A substring test for `worktrees`
    in the path would misclassify any repo that merely lives under such a
    directory.
    """
    git_dir = Path(_out(repo, "rev-parse", "--absolute-git-dir")).resolve()
    return git_dir == git_common_dir(repo)
