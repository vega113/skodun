"""Git IO: base resolution, diff capture, object reads, and the diff identity hash.

Two kinds of read live here and they answer different questions. `capture_diff`
reads the WORKING TREE, because a foreground review is of what the developer has
in front of them; `blob_bytes`/`blob_size` read the OBJECT STORE, because a
background review is of a ref that has already been pushed and the working tree
may since have moved on. Neither is a substitute for the other.
`capture_ref_diff`/`resolve_ref_base` are the object-store-only counterparts of
`capture_diff`/`resolve_base`: the same diff-capture and base-resolution jobs,
done between two given oids instead of against the working tree and the
checked-out `HEAD`, for exactly the same reason `blob_bytes` avoids the
working tree.

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
and the gate would enforce an identity nobody else can reproduce. This is
the ONLY guarantee those two flags buy: no external-diff or textconv driver
can alter the hashed bytes. It is NOT a guarantee that the hashed bytes are
independent of local git config in general — `core.quotepath` still moves
them for a diff touching a non-ASCII path, because `git diff --no-index`
renders the filename in the diff header under quotepath rules regardless of
`--no-ext-diff --no-textconv`. That sensitivity is fail-safe, not a bug: a
differing hash means a failed legacy-record join and one extra review, never
a wrong gate PASS. See `test_quotepath_changes_diff_identity_for_nonascii_path`
in `tests/test_gitio.py`.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Mandatory on every diff: keeps no external-diff or textconv driver able to
# alter the hashed bytes. Does NOT make the hashed bytes config-independent in
# general — see the module docstring's core.quotepath note.
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


def _worktree_root(repo: Path) -> Path:
    """The worktree root containing `repo`.

    ORACLE PARITY: the oracle opens both its `--diff-hash` and `--base-sha`
    seams with `WORKTREE="$(git rev-parse --show-toplevel)"; cd "$WORKTREE"`.
    That is load-bearing, not tidiness — see `capture_diff`.
    """
    return Path(_out(repo, "rev-parse", "--show-toplevel"))


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


_REF_FALLBACK_WARNING = (
    "no main ref (github/main|origin/main|main) with a merge-base found; "
    "reviewing {ref}..{oid} only -- a multi-commit branch may be "
    "under-reviewed"
)


def resolve_ref_base(repo: Path, local_oid: str) -> Base:
    """Resolve the base for a PUSHED ref that need not be checked out at all.

    This is the dispatcher's base resolution for a NEW remote branch.
    Candidate order and existence-only selection — the first of
    `github/main`/`origin/main`/`main` that merely resolves, `break`ing
    immediately with no fall-through to the next candidate even if it has no
    merge-base — mirror `resolve_base` exactly; see its own docstring for why
    fall-through would silently pick a different base_sha.

    The one deliberate difference: `resolve_base` computes the outgoing
    FOREGROUND change, whose tip is always the checked-out `HEAD`. A
    background dispatch reviews a ref that was PUSHED and may never be
    checked out — the checkout's actual `HEAD` can be a different branch
    entirely, or simply stale — so `local_oid`, not `HEAD`, plays the "tip of
    the range" role everywhere `resolve_base` hard-codes `HEAD`: the
    merge-base call itself, and both of its no-merge-base fallbacks
    (`local_oid^`, then `local_oid` for a single-commit branch). Using `HEAD`
    for any of those would silently resolve — and therefore review — the
    wrong range whenever the pushed branch and the checkout have diverged.
    """
    base_ref = ""
    for cand in ("github/main", "origin/main", "main"):
        if _run(repo, "rev-parse", "--verify", "-q", cand, ok_codes=(0, 1)).returncode == 0:
            base_ref = cand
            break

    if base_ref:
        mb = _run(repo, "merge-base", base_ref, local_oid, ok_codes=(0, 1, 128))
        sha = mb.stdout.decode("utf-8", "replace").strip() if mb.returncode == 0 else ""
        if sha:
            return Base(ref=base_ref, sha=sha)

    # No main ref, or one that shares no history with local_oid: fall back to
    # its previous commit, reviewing only local_oid^..local_oid — warn loudly
    # rather than silently reviewing a subset.
    parent_rev = f"{local_oid}^"
    parent = _run(repo, "rev-parse", "--verify", "-q", parent_rev, ok_codes=(0, 1, 128))
    if parent.returncode == 0:
        return Base(
            ref=parent_rev,
            sha=parent.stdout.decode("utf-8", "replace").strip(),
            warning=_REF_FALLBACK_WARNING.format(ref=parent_rev, oid=local_oid),
        )
    # Single-commit branch: local_oid^ does not exist.
    return Base(
        ref=local_oid,
        sha=_out(repo, "rev-parse", local_oid),
        warning=_REF_FALLBACK_WARNING.format(ref=local_oid, oid=local_oid),
    )


@dataclass(frozen=True)
class Diff:
    data: bytes
    files: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    truncated_untracked: bool = False


def _tracked_statuses(repo: Path, base_sha: str, *other: str) -> dict[str, str]:
    """path -> one-letter status from `git diff --name-status -z`.

    NUL-delimited, never text-mode + `.strip()`: under default `core.quotepath`
    git renders non-ASCII names as `"\\303\\244.txt"` in text mode, and stripping
    would eat filenames' real leading/trailing spaces. Records are `X\0path\0`
    except rename AND copy (`R`/`C`), which carry `old\0new` — the new name wins.

    `*other` is empty for `capture_diff` (base_sha vs the working tree/index)
    and is `(local_oid,)` for `capture_ref_diff` (base_sha vs a second oid,
    commits only) — one parser shared between both callers, per the module's
    "reuse the shipped -z name-status parsing" contract.
    """
    toks = _paths(_run(repo, "diff", *_DIFF_FLAGS, "--name-status", "-z", base_sha, *other).stdout)
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

    `repo` may be any path inside the worktree; it is normalised to the
    worktree root before any git call. That is required, not a courtesy:
    `git diff` emits worktree-root-relative paths while
    `git ls-files --others` emits paths relative to the cwd, so running the
    two from a subdirectory would mix two path bases into one `files` list
    (untracked entries unopenable from the root) and into one hash. The
    oracle avoids this the same way — `cd "$(git rev-parse --show-toplevel)"`
    before `resolve_outgoing_change`.

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
    repo = _worktree_root(repo)
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


def capture_ref_diff(repo: Path, base_sha: str, local_oid: str) -> Diff:
    """`base_sha..local_oid` — commits only. No untracked files, no working
    tree, no index: everything here comes from the two given oids.

    This is the background dispatcher's diff scope: it reviews a ref that has
    been PUSHED, and that ref need not be checked out at all — its working
    tree, if one even exists, may since have moved on to something else
    entirely. `capture_diff` exists for the opposite reason: a FOREGROUND
    review is of what the developer has in front of them right now. Reuses
    `_tracked_statuses`'s `-z` name-status parsing (rename/copy records
    included) between the two given oids; `diff_identity` is unchanged and
    applies to the returned `Diff` exactly as it does to `capture_diff`'s.

    Unlike `capture_diff`, `repo` is NOT normalised to the worktree root here,
    and deliberately so: `git diff <rev> <rev>` with no pathspec always emits
    paths relative to the repo TOP regardless of the invoking cwd (it is only
    a working-tree/index diff, or `ls-files`, that are cwd-relative — the
    exact mismatch `capture_diff` normalises away). There is also no
    untracked-file listing here to keep path-consistent with, since a
    ref-range diff between two commits has no untracked files by definition.
    """
    tracked = _run(repo, "--no-pager", "diff", *_DIFF_FLAGS, base_sha, local_oid).stdout
    statuses = _tracked_statuses(repo, base_sha, local_oid)
    files = list(statuses)
    return Diff(data=tracked.rstrip(b"\n"), files=files, statuses=statuses)


def _blob_rev(oid: str, path: str) -> str | None:
    """The `<oid>:<path>` argument for a blob read, or None if either is unsafe.

    Every rejection here closes a way for the argument to name something other
    than "this path in that commit's tree":

      * **An empty oid is the INDEX.** `git cat-file blob :a.txt` prints the
        STAGED bytes and exits 0 — it does not fail. An object read exists
        precisely to avoid the not-yet-pushed states of a repo, so the one input
        that silently serves one has to be refused rather than passed on.
        Pinned by `test_blob_bytes_refuses_an_empty_rev_rather_than_reading_the_index`.
      * **A colon in the oid re-splits the argument.** git splits `<rev>:<path>`
        at the FIRST colon, so `oid=":0"` yields `:0:a.txt` — the index again,
        and a colon anywhere else silently renames the path being read.
      * **A leading `-` is an option**, not a revision.
      * **A `.` or `..` first component is cwd-relative**: `<rev>:../a.txt`
        resolves against the process's directory inside the repo, not the tree
        root, so the same argument names different blobs from different
        directories. Everything else in `<rev>:<path>` IS tree-root-relative,
        which is why these functions — unlike `capture_diff` — need no
        worktree-root normalisation.

    A NUL byte would make `subprocess` raise `ValueError` (not `OSError`); it is
    rejected here so the callers' promise of None-on-any-failure does not rest
    on one exception class.
    """
    if not oid or not path:
        return None
    if oid.startswith("-") or ":" in oid or "\0" in oid:
        return None
    if path.startswith(("/", "\\")) or "\0" in path:
        return None
    if path.replace("\\", "/").split("/", 1)[0] in (".", ".."):
        return None
    return f"{oid}:{path}"


def _cat_file(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`git cat-file ...` with no exception surface at all: None if it could not
    be run (git absent, un-encodable argument), otherwise the result."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "cat-file", *args], capture_output=True
        )
    except (OSError, ValueError):
        return None


def blob_size(repo: Path, oid: str, path: str) -> int | None:
    """Byte size of `path` in commit `oid`'s tree, or None on ANY failure.

    CAVEAT, relied upon by `contextpack` and shared with the oracle: `cat-file
    -s` answers for any object type, so a DIRECTORY has a size here. Only the
    blob read rejects a tree, so a directory among the candidates is classified
    on its size and refused later — see `test_a_tree_path_has_a_size_but_no_
    blob_bytes`.
    """
    rev = _blob_rev(oid, path)
    if rev is None:
        return None
    cp = _cat_file(repo, "-s", rev)
    if cp is None or cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.strip() or b"0")
    except ValueError:
        return None


def blob_bytes(
    repo: Path, oid: str, path: str, *, max_bytes: int | None = None
) -> bytes | None:
    """Content of `path` in commit `oid`'s tree, or None on ANY failure.

    This is how the background dispatcher reads code: it reviews a ref that has
    been PUSHED, so the reviewed content must come from the pushed commit's
    tree. The working tree is a different thing that may have moved on, and a
    review of it would be a review of code nobody pushed.

    `max_bytes` reads only that prefix, without buffering the rest: a peek for
    binary detection must not pull a committed 200 MB video into memory, and
    such a file is one `git diff --name-only` entry like any other. The read is
    a `Popen` rather than a `run` for exactly that reason.

    No filter is applied — no `--filters`, no `--textconv`. The bytes are the
    object's own, so what gets reviewed cannot depend on the developer's
    `.gitattributes` or config, the same guarantee `_DIFF_FLAGS` buys for diffs.

    Two properties worth naming because callers lean on them:

      * **An empty blob comes back as `b""`, never None.** The packer reports
        None as `missing` and packs `b""`; conflating them would report a
        legitimately empty committed file as absent.
      * **A symlink reads as its target *path*, not its target's content.** git
        stores a symlink as a blob holding the target string, so an object read
        cannot traverse one. That is why the working-tree packer's symlink
        hardening has no analogue on this side.
    """
    rev = _blob_rev(oid, path)
    if rev is None:
        return None
    if max_bytes is None:
        cp = _cat_file(repo, "blob", rev)
        if cp is None or cp.returncode != 0:
            return None
        return cp.stdout
    if max_bytes <= 0:
        # A zero-length peek cannot tell an empty blob from a missing one, so
        # let the size probe answer the existence question.
        return b"" if blob_size(repo, oid, path) is not None else None
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "blob", rev],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return None
    try:
        data = proc.stdout.read(max_bytes) if proc.stdout else b""
    except OSError:
        data = b""
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.kill()  # a blob longer than the peek leaves git still writing
        proc.wait()
    if data:
        return data
    # Nothing on stdout: an empty blob, a missing path and a tree all look the
    # same from here, and the kill may have pre-empted git's real exit code, so
    # the exit code cannot decide it. The size probe can.
    return b"" if blob_size(repo, oid, path) is not None else None


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
