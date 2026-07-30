"""Deterministic full-file context packing for the review prompt.

The diff tells the model what changed; this module tells it what the changed
files now *look like*. It packs whole file contents into whatever prompt
headroom the diff left over, and it must do so under three constraints that
shape every decision below:

  * **Deterministic.** The same tree, the same headroom and the same statuses
    must yield the same bytes and the same `sha256`, because that hash is part
    of what identifies the reviewed content. Hence: candidates ordered by
    descending size with the path as an ascending tie-break, never by dict or
    filesystem order.
  * **All-or-nothing per file.** A file that does not fit whole is omitted
    whole. Half a file is worse than no file: the model cannot tell a truncated
    tail from a real one and will report findings about code that does not
    exist.
  * **Never blocks, never raises, never follows a link out.** Path lists reach
    this module from `git diff --name-only` on a tree an attacker may control.
    A hostile or merely unlucky entry must degrade to an omission reason, never
    to a symlinked read outside the worktree, never to a hang, and never to an
    exception. See `_safe_open` and `_encode_prompt_text`.

THREE RESIDUAL WEAKNESSES, all inherited from the oracle and all unfixable
while the packed bytes must match it. They are documented here rather than
fixed, so that the guarantee above is not read as more than it is:

  * A **hard link** inside the worktree whose inode also lives outside it is
    packed in full. No `lstat` or `realpath` check can tell it from an ordinary
    file — indistinguishability is what a hard link *is* — so the symlink
    layers below do not help. Exposure is small: git cannot check a hard link
    out, so this needs a local actor who already has worktree write access.
  * **File content can forge the markers.** A file whose text contains a
    literal `----- BEGIN FILE CONTEXT: x -----` line lands in the prompt
    verbatim, so content can impersonate a section boundary. The markers are a
    prompt convention the model reads, not framing a parser enforces.
  * **A path containing `,` or a newline corrupts the omission header**, whose
    entries are joined with `", "` and terminated by `"\n"`. Only the prose
    header is affected; `Pack.omitted` is a list of pairs and stays exact, and
    it is what callers should read.

ORACLE PARITY. The body format is a prompt format: the markers below are read
by the model and are part of the input that stored reviews were produced from.
They are reproduced from `scripts/grok-context-pack.py` byte-for-byte and are
pinned by fixture tests in `tests/test_contextpack.py` plus parity tests that
run the oracle over the same tree.

Two deliberate interface differences from the oracle, neither observable in the
packed bytes (both are pinned by the parity tests, which feed the oracle the
equivalent input):

  1. The oracle recovers each path's status by *parsing a unified diff*
     (`added`/`deleted`/`binary`/`modified`); skodun takes `statuses` from
     `gitio.Diff.statuses`, i.e. git's own one-letter codes from
     `git diff --name-status`. A path absent from `statuses` is `M`. The
     oracle's `binary` status has no git-letter equivalent — git reports a
     binary file as `M` — so such a file reaches the binary *content* check
     instead and is omitted for the same reason, `binary`. It differs only for
     a path that is both binary-in-the-diff and unreadable now, which the
     oracle calls `binary` and skodun calls `missing`.
  2. The oracle packs a large added file only under an opt-in env flag
     (`GR_CONTEXT_PACK_LARGE_ADDED`); skodun's `pack_large_added` parameter
     defaults to on. A single-shot diff carries a small added file's full
     content already (hence `already-in-diff`), but at >= 16 KiB the diff may
     be batched or split, so by default the file is packed. Parity runs set
     the oracle's flag to match the default. See `pack()` for when a caller
     should turn it off.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from . import gitio

# --- prompt format (byte-for-byte from the oracle; do not "tidy") -----------
BEGIN_MARKER: Final = "----- BEGIN FILE CONTEXT: {path} -----\n"
END_MARKER: Final = b"----- END FILE CONTEXT -----\n"
OMITTED_PREFIX: Final = "Context omitted for: "

# --- classification constants ----------------------------------------------
# An added file smaller than this has its full content in the diff already.
ALREADY_IN_DIFF_MAX: Final = 16384
# Binary detection looks at this much of the file, and calls it binary above
# this fraction of non-text bytes (a NUL anywhere decides it outright).
BINARY_PEEK_BYTES: Final = 8192
BINARY_NONTEXT_RATIO: Final = 0.30

#: The complete omission vocabulary. Reasons are a stable interface: they are
#: rendered into the prompt header and read back by the caller.
REASONS: Final = (
    "deleted",
    "binary",
    "already-in-diff",
    "missing",
    "over-file-cap",
    "over-headroom",
)


@dataclass(frozen=True)
class Pack:
    """A packed context body plus the accounting needed to explain it."""

    body: bytes
    bytes_total: int = 0
    included: list[str] = field(default_factory=list)
    omitted: list[tuple[str, str]] = field(default_factory=list)
    sha256: str = ""


def _safe_rel(path: str) -> str | None:
    """Normalise a repo-relative path, or return None if it is not one.

    Rejects absolute paths (POSIX and backslash-rooted), Windows drive/UNC
    prefixes, and any `..` component. Empty and `.` components are dropped, so
    `./a//b` normalises to `a/b`. The *original* string is still what appears
    in the prompt markers — only the lookup uses this normal form.
    """
    if not path or path.startswith(("/", "\\")):
        return None
    if len(path) >= 2 and path[1] == ":":  # C:\... or C:/...
        return None
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _safe_open(repo: Path, rel: str) -> BinaryIO | None:
    """Open a normalised relative path under `repo`, or return None.

    The hardening here is layered because each layer closes a different hole:

      * **No symlink at any component.** Not just the leaf: `link_dir/f.txt`
        with a symlinked `link_dir` reaches outside just as effectively. A
        symlink is never followed for working-tree packing, so a hostile tree
        cannot inject a secret from elsewhere on the machine into the prompt.
      * **Resolved path still under the worktree.** Guards against anything the
        component walk missed (mount tricks, `..` smuggled in by a
        normalisation bug).
      * **`ValueError` alongside `OSError`, everywhere.** Not a stylistic
        widening: a path with an embedded NUL makes `resolve()`, `os.lstat`
        and `os.open` raise `ValueError`, which is *not* a subclass of
        `OSError`, so catching only `OSError` lets it escape `pack()` and kill
        the review over one bad filename. Two layers now catch it — the
        `resolve` block above and the `os.*` block below — so removing either
        alone still degrades to `missing`. That redundancy is deliberate, and
        `test_embedded_nul_in_a_path_degrades_to_missing` pins the contract
        rather than either layer.
      * **`O_NOFOLLOW`.** The component walk is a check-then-use; a symlink
        swapped in between the two would win without this.
      * **Preflight `S_ISREG(lstat)`.** A FIFO, device or directory is rejected
        *before* any `open` touches it. The post-open `fstat` would catch it
        too, but only after the open had already happened — and opening a FIFO
        is itself observable: it releases a writer blocked in `open(2)`. The
        packer must not perturb the tree it is reading.
      * **`O_NONBLOCK` + post-open `S_ISREG(fstat)`.** The preflight `lstat`
        rejects a FIFO or device, but an attacker who swaps one in after it can
        make `open()` block forever on a pipe with no writer — a hung review,
        not a wrong one, which no later check would ever get to run.
        `O_NONBLOCK` makes that `open` return instead of hanging, `fstat` on
        the *fd* (not the path) then rejects what was actually opened, and
        blocking mode is restored for the regular files that survive.
    """
    try:
        root = repo.resolve()
    except (OSError, ValueError):
        return None
    cur = root
    for part in rel.split("/"):
        cur = cur / part
        try:
            if cur.is_symlink():
                return None
        except (OSError, ValueError):
            return None
    try:
        cur.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.lstat(cur).st_mode):
            return None
        fd = os.open(cur, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
    except OSError:
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def _probe(repo: Path, path: str) -> tuple[int, bytes] | None:
    """Size plus a binary-detection peek, without loading the whole file."""
    rel = _safe_rel(path)
    if rel is None:
        return None
    f = _safe_open(repo, rel)
    if f is None:
        return None
    try:
        return os.fstat(f.fileno()).st_size, f.read(BINARY_PEEK_BYTES)
    except OSError:
        return None
    finally:
        f.close()


def _read_all(repo: Path, path: str) -> bytes | None:
    rel = _safe_rel(path)
    if rel is None:
        return None
    f = _safe_open(repo, rel)
    if f is None:
        return None
    try:
        return f.read()
    except OSError:
        return None
    finally:
        f.close()


def _oid_probe(repo: Path, oid: str, path: str) -> tuple[int, bytes] | None:
    """`_probe` for a commit tree: size plus a peek, without loading the blob.

    Two git calls rather than one, for the same reason the working-tree probe
    takes a size and an 8 KiB read rather than the whole file: a committed
    200 MB video is one `git diff --name-only` entry like any other, and
    classification must not buffer it.

    ORACLE PARITY, one narrow divergence shared with `_oid_read_all`: the
    normalised path is what reaches git here, while the oracle passes the RAW
    candidate and uses its normal form only as a safety test. So a path written
    with a redundant `//` (which `<oid>:a//b` does not resolve) is `missing`
    there and packed here. Unreachable from real input — git's own path streams
    never contain one — and the direction is more context, never other content.
    """
    rel = _safe_rel(path)
    if rel is None:
        return None
    size = gitio.blob_size(repo, oid, rel)
    if size is None:
        return None
    peek = gitio.blob_bytes(repo, oid, rel, max_bytes=BINARY_PEEK_BYTES)
    if peek is None:
        return None
    return size, peek


def _oid_read_all(repo: Path, oid: str, path: str) -> bytes | None:
    rel = _safe_rel(path)
    if rel is None:
        return None
    return gitio.blob_bytes(repo, oid, rel)


def _readers(
    repo: Path, source: str, oid: str | None
) -> tuple[Callable[[str], tuple[int, bytes] | None], Callable[[str], bytes | None]]:
    """The (classify-probe, full-load) pair for `source`.

    The selection happens exactly once, here, so the packing loop below cannot
    read one source while reporting the other — the failure mode that would put
    working-tree code in a prompt whose identity says "this commit".
    """
    if source == "oid":
        # `pack` has already refused an empty oid, and one that got here anyway
        # would still fail closed: `gitio` refuses an empty rev rather than
        # letting `git cat-file blob :path` answer from the index.
        rev = oid or ""
        return (lambda p: _oid_probe(repo, rev, p),
                lambda p: _oid_read_all(repo, rev, p))
    return (lambda p: _probe(repo, p), lambda p: _read_all(repo, p))


def _is_binary(data: bytes) -> bool:
    """A NUL anywhere in `data`, or >30% non-text bytes in the first 8 KiB."""
    if b"\0" in data:
        return True
    sample = data[:BINARY_PEEK_BYTES]
    if not sample:
        return False
    nontext = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    return (nontext / len(sample)) > BINARY_NONTEXT_RATIO


def _encode_prompt_text(text: str) -> bytes:
    """Encode prompt text that embeds a caller-supplied path. Cannot raise.

    Paths arrive from `gitio`, which decodes git's NUL-delimited path stream
    with `errors="surrogateescape"` — deliberately, because `replace` would
    mangle a non-UTF-8 filename into U+FFFD and then open the wrong file. So
    `Diff.files` and `Diff.statuses` legitimately carry lone surrogates, and a
    strict `.encode("utf-8")` here would raise `UnicodeEncodeError` on one and
    take the whole review down with it — for a path this module was only ever
    going to report as an omission.

    `surrogateescape` on the way out is the exact mirror of that decode: the
    bytes emitted into the prompt are byte-for-byte the bytes git handed us,
    which is the only rendering that names the real file. A surrogate outside
    U+DC80..U+DCFF cannot have come from such a decode and does not round-trip,
    so it is escaped visibly instead — ugly, but bounded, deterministic, and
    still not an exception.

    Every byte-length accounting of a marker goes through here too, so the
    headroom budget and the emitted bytes can never disagree about a path's
    cost.
    """
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace")


def _begin_marker(path: str) -> bytes:
    return _encode_prompt_text(BEGIN_MARKER.format(path=path))


def _section_overhead(path: str) -> int:
    """Byte cost of a section's markers, as they will actually be encoded."""
    return len(_begin_marker(path)) + len(END_MARKER)


def _omission_header(omitted: list[tuple[str, str]]) -> bytes:
    parts = ", ".join(f"{p} ({r})" for p, r in omitted)
    return _encode_prompt_text(OMITTED_PREFIX + parts + "\n")


def pack(
    repo: Path,
    files: list[str],
    statuses: dict[str, str],
    headroom: int,
    source: str = "wt",
    oid: str | None = None,
    per_file_cap: int | None = None,
    pack_large_added: bool = True,
) -> Pack:
    """Pack full file contents for `files` into at most `headroom` bytes.

    `source` decides WHERE the content is read from, and the two answers are not
    interchangeable:

      * `"wt"` — the working tree under `repo`. What a foreground review is of:
        the code the developer has in front of them, uncommitted edits included.
      * `"oid"` — the tree of commit `oid` (required, and refused if empty).
        What a background review is of: the dispatcher reviews a ref that has
        been PUSHED, and the working tree may since have moved on — a different
        branch, a rebase, a half-finished edit. Reading it would review code
        nobody pushed while the record says the push was reviewed. The bytes
        come from `gitio.blob_bytes`, i.e. from the object store, so they are
        also immune to a `.gitattributes` filter and to a concurrent edit.
        `repo` then only locates the repository: `<oid>:<path>` is
        tree-root-relative, so any directory inside it does.

    Passing an `oid` with `source="wt"` is refused rather than ignored: it is the
    one caller mistake whose consequence is silent (a working-tree pack that
    every downstream field labels as the commit's).

    Everything else is the same on both sources: binary detection, the caps,
    selection order, the section format and the omission vocabulary, all pinned
    against the oracle in each mode. Only the classification STEP at which a
    non-blob path is caught differs — the working-tree reader rejects a directory
    or a symlink up front, while an object read has a size for a directory
    (`git cat-file -s` answers for a tree) and refuses it at the full read, and
    reads a symlink as the target *path text* git stored in the blob. Neither is
    a real candidate: `git diff --name-only` lists blobs.

    The oid source does NOT replicate the working-tree hardening above
    (`_safe_open`'s symlink walk, `O_NOFOLLOW`, the FIFO layers), because it is
    structurally moot rather than merely unnecessary: an object read cannot
    traverse a link out of the tree and cannot block on a pipe. `_safe_rel` is
    still applied, so an absolute or `..` path is an omission on both sources.

    `statuses` is `gitio.Diff.statuses` (path -> git's one-letter code); a path
    absent from it is treated as `M`. `headroom` is what the diff left over —
    diff content always wins the envelope, this packer only ever sees the
    remainder, so `headroom <= 0` is a normal outcome and yields an empty body
    with every candidate reported as `over-headroom`.

    `per_file_cap` has no Phase 1 caller and is kept deliberately: it is the
    oracle's `GROK_CONTEXT_FILE_BYTES` knob, and `test_oracle_parity_with_
    per_file_cap` drives the real oracle with that variable set to assert the
    packed bytes still match. Removing the parameter would remove a parity
    assertion, not dead weight. `None` (the default, and anything below 1)
    means no per-file limit, so the shipped behaviour is unaffected.

    `pack_large_added` decides what happens to an added (`A`) file of at least
    `ALREADY_IN_DIFF_MAX` bytes. Smaller adds are always `already-in-diff`: the
    diff carries their full content, so packing them spends headroom to say the
    same thing twice. For larger ones the right answer depends on the caller:

      * **On (the default, and what the oracle's `GR_CONTEXT_PACK_LARGE_ADDED`
        opts into).** Correct when the diff may be batched or split, so the
        model may never see the whole new file.
      * **Off.** Correct for a *single-shot* review, where the diff already
        carries every added file whole. Selection is size-descending, so a big
        added file is packed first and can crowd out the modified files whose
        surrounding context the reviewer actually needs — the diff shows what
        changed in them, and only this packer can show what they now look like.

    Never raises for bad input: an unreadable, escaping, non-regular or
    non-UTF-8 path becomes an omission with a reason, and so does an oid that
    does not resolve (every candidate `missing`, no exception). The exceptions
    are an unimplemented `source` and an incoherent `source`/`oid` pair, which
    are programming errors, not input.
    """
    if source not in ("wt", "oid"):
        raise NotImplementedError(
            f"context source {source!r} is not implemented; only 'wt' "
            "(working tree) and 'oid' (commit tree) are supported"
        )
    if source == "oid" and not oid:
        # Not degraded to an empty pack: a caller with no oid to read cannot be
        # served, and an empty oid is `git cat-file blob :path` — the INDEX.
        raise ValueError("context source 'oid' requires a non-empty oid")
    if source == "wt" and oid is not None:
        raise ValueError(
            f"oid {oid!r} passed with context source 'wt'; pass source='oid' to "
            "read that commit's tree, or drop the oid to read the working tree"
        )
    probe_one, load_one = _readers(repo, source, oid)
    if per_file_cap is not None and per_file_cap < 1:
        per_file_cap = None

    # De-duplicate while preserving caller order: the order fixes the omission
    # header's order, and a repeated path must not be packed twice.
    paths: list[str] = []
    seen: set[str] = set()
    for p in files:
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)

    # Classify everything first — these reasons do not depend on headroom.
    # Only sizes are collected here; content is loaded solely for files that
    # are actually going to be included.
    packable: list[tuple[int, str]] = []
    early_omit: list[tuple[str, str]] = []

    for path in paths:
        st = statuses.get(path, "M")
        if st == "D":
            early_omit.append((path, "deleted"))
            continue
        probed = probe_one(path)
        if probed is None:
            early_omit.append((path, "missing"))
            continue
        size, peek = probed
        if st == "A" and (not pack_large_added or size < ALREADY_IN_DIFF_MAX):
            # The whole new file is in the diff already; packing it again would
            # spend headroom to say the same thing twice.
            early_omit.append((path, "already-in-diff"))
            continue
        if _is_binary(peek):
            early_omit.append((path, "binary"))
            continue
        if per_file_cap is not None and size > per_file_cap:
            early_omit.append((path, "over-file-cap"))
            continue
        packable.append((size, path))

    # Descending size, path as ascending tie-break — deterministic, and it
    # prefers the files a reviewer most needs to see in full.
    packable.sort(key=lambda t: (-t[0], t[1]))

    included: list[str] = []
    headroom_omit: list[tuple[str, str]] = []
    sections: list[bytes] = []
    used = 0

    if headroom <= 0:
        # The diff filled the envelope: skip all file I/O, just report.
        headroom_omit.extend((path, "over-headroom") for _size, path in packable)
    else:
        for size, path in packable:
            # Budget the raw size plus the newline normalisation may add, so
            # all-or-nothing can never overshoot after the fact.
            if used + _section_overhead(path) + size + 1 > headroom:
                headroom_omit.append((path, "over-headroom"))
                continue
            data = load_one(path)
            if data is None:  # vanished or swapped between classify and load
                headroom_omit.append((path, "missing"))
                continue
            if _is_binary(data):  # a NUL past the 8 KiB peek window
                headroom_omit.append((path, "binary"))
                continue
            text = data.decode("utf-8", errors="replace")
            if not text.endswith("\n"):
                text = text + "\n"
            # `text` came from a `replace` decode, so it holds no surrogates and
            # encodes strictly; only the path needs the tolerant encoder.
            section = _begin_marker(path) + text.encode("utf-8") + END_MARKER
            # Re-check against the bytes actually produced: the file may have
            # grown since the size probe, and `replace` can widen a decode.
            if used + len(section) > headroom:
                headroom_omit.append((path, "over-headroom"))
                continue
            sections.append(section)
            used += len(section)
            included.append(path)

    omitted = early_omit + headroom_omit
    order = {p: i for i, p in enumerate(paths)}
    omitted.sort(key=lambda t: (order.get(t[0], 10**9), t[0]))

    header = b""
    if omitted and headroom > 0:
        header = _omission_header(omitted)
        # The header must fit inside the same headroom as the sections, and it
        # grows each time a section is dropped (the drop adds another entry),
        # so recompute rather than reserve. Trailing sections go first: they
        # are the smallest candidates, so this drops the least context.
        while sections and used + len(header) > headroom:
            used -= len(sections.pop())
            if included:
                omitted.append((included.pop(), "over-headroom"))
                omitted.sort(key=lambda t: (order.get(t[0], 10**9), t[0]))
                header = _omission_header(omitted)
        if used + len(header) > headroom:
            # Not even the bare header fits: the note is worth less than the
            # guarantee that the body never exceeds headroom.
            header = b""

    body = header + b"".join(sections) if headroom > 0 else b""
    return Pack(
        body=body,
        bytes_total=len(body),
        included=included,
        omitted=omitted,
        sha256=hashlib.sha256(body).hexdigest(),
    )
