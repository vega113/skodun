r"""Deterministic byte-level splitting of an over-budget diff into review batches.

A diff bigger than one prompt's diff budget used to fail closed as
`diff_truncated`: unreviewable, and therefore ungateable. This module is the
first half of the answer -- it cuts the diff into size-bounded pieces that each
fit a prompt, so every hunk can be reviewed *somewhere* instead of the whole
push being refused.

Three properties are load-bearing, and every decision below follows from them:

  * **Deterministic.** The same diff bytes and the same budget must yield the
    same batches, always. Batch reviews are aggregated under the full diff's
    identity, so a split that wandered between runs would make two runs over
    identical content produce non-comparable results. Hence: sections in input
    order, greedy packing, no size sorting, no dict iteration.
  * **Total coverage, never silent.** Every `@@` hunk in the input lands in
    exactly one batch. A hunk that alone exceeds the budget cannot be made to
    fit by any splitting -- that is the irreducible floor -- so it becomes its
    own batch flagged `truncated=True`. The flag is the whole point: it travels
    up to the trust invariant (`trustworthy = parse_ok and not degraded and not
    diff_truncated`) and demotes the run. Dropping the hunk, or keeping it and
    dropping the flag, would turn an unreviewable region into a silent pass.
  * **Raw bytes throughout.** The diff's identity is a git blob SHA-1 over its
    exact bytes, and a diff can legitimately contain invalid UTF-8 (a file in a
    legacy encoding, a `core.quotepath=false` path). Nothing here decodes the
    diff. `files` is decoded, because it is a label -- with
    `errors="replace"`, never raising on the bytes it was given.

    Bit-exactness rests on one stdlib property: `bytes.splitlines(keepends=True)`
    breaks on `\r`, `\n` and `\r\n` and nothing else, so re-joining its output
    always reproduces the input. (`str.splitlines` would also break on `\v`,
    `\f`, `\x1c`-`\x1e` and U+0085/U+2028/U+2029 -- decoding first would
    therefore invent cut points inside a hunk body and corrupt it.) Pinned by
    `test_exotic_line_separator_bytes_are_not_cut_points`.

`changed_regions` is the module's second export and the same kind of thing: a
pure, byte-level read of a diff's STRUCTURE. It lives here rather than beside
the prompt that consumes it because "what is a header line" is decided by the
two markers the splitter already cuts on -- one definition, so a reader of a
batch's change summary and the splitter that produced the batch can never
disagree about where a file or a hunk begins.

Note what this module deliberately does NOT do: it does not know about prompts,
budgets-from-config, stores, or passes. It is a pure function over bytes, so the
splitting rules can be pinned against the oracle directly. `changed_regions`
keeps that boundary: it reports THAT its cap bit, and the caller owns the words
that say so in a prompt.

ORACLE PARITY. Ported from the oracle's `split_diff_into_batches`
(`scripts/grok-prepush-review.sh` lines 842-959 -- an embedded Python heredoc,
so this is a vendor-and-adapt port of real code, not a re-derivation from
prose). `tests/test_batching.py` runs the oracle's own `--split-diff` seam
(lines 968-971) over the same inputs and compares batch-for-batch,
byte-for-byte. Two adaptations, neither observable in the batch bytes:

  * The oracle writes `<prefix>.batchN.diff` files and prints a manifest JSON;
    this returns `Batch` values. The caller decides whether bytes ever hit disk.
  * The oracle's manifest carries a per-batch `bytes` field; here that is just
    `len(batch.data)` and is not stored twice.

One observable divergence from the Phase 3 plan text, where the ORACLE WINS: an
**empty diff yields zero batches**, not one empty batch. The plan reads
`len(diff) <= budget` -> one identical batch, which for `b""` would be a batch
with no content. The oracle produces none (`splitlines` of `b""` is empty, so
there are no sections), and its caller relies on that: zero batches is a
terminal failure, "diff batching produced no batches"
(grok-prepush-review.sh:3325-3341). Pinned by
`test_parity_with_oracle_split_diff[empty]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

#: Section boundary. The trailing space is part of the marker: it is what git
#: emits, and matching without it would also match a hunk body line beginning
#: `diff --git` at column 0, which cannot happen in a unified diff (bodies are
#: prefixed) but could in the rare preamble.
_DIFF_GIT = b"diff --git "

#: Hunk boundary. Body lines are prefixed with ' ', '+' or '-', so a line
#: starting with `@@` at column 0 is a hunk header.
_HUNK = b"@@"

#: `diff --git "a/x y" "b/x y"` -- git quotes paths containing spaces, a `"`, a
#: `\`, a control character or, under `core.quotepath` (its default), any
#: non-ASCII byte.
#:
#: `(?:[^"\\]|\\.)*` rather than `[^"]*`, because the quoting is C-STYLE: a `"`
#: inside the path arrives as `\"`, and a matcher that stopped at the first bare
#: `"` matched nothing at all for such a header -- `_file_of` then fell through
#: to its last resort and reported the ENTIRE two-path tail
#: (`"a/we\"ird.py" "b/we\"ird.py"`) as the filename. The alternation consumes
#: escape PAIRS, so `\\` at the end of a component cannot swallow the closing
#: quote either.
_QUOTED_PATHS = re.compile(rb'"a/((?:[^"\\]|\\.)*)" "b/((?:[^"\\]|\\.)*)"$')

#: One C escape inside a quoted path: `\ooo` (octal byte) or `\<char>`.
_C_ESCAPE = re.compile(rb"\\([0-7]{1,3}|.)", re.DOTALL)

#: The single-character C escapes git emits, per `quote_c_style` in quote.c.
_C_ESCAPES = {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n",
              b"r": b"\r", b"t": b"\t", b"v": b"\v",
              b"\\": b"\\", b'"': b'"'}

#: `diff --git a/old b/new`. Group 1 is greedy so that a path containing the
#: literal " b/" splits at its LAST occurrence, which is what git's own
#: unquoted form implies. Group 2 (the post-image path) is what we report: for a
#: rename, the new name is the one a reviewer will look for.
_PLAIN_PATHS = re.compile(rb"a/(.*) b/(.*?)$")

#: How many `diff --git` / `@@` header lines `changed_regions` reports for one
#: batch before it stops reading. The oracle's own `changed_regions(max_lines=120)`
#: default: enough to map a large batch's changed regions, bounded so that a
#: thousand-hunk batch cannot crowd every other batch out of one prompt.
MAX_REGION_LINES = 120


@dataclass(frozen=True)
class Batch:
    """One prompt-sized piece of a diff.

    `data` is a byte-exact slice of the input, except that a batch produced by
    splitting one file at its hunk boundaries repeats that file's header (see
    `_units`), so the concatenation of all batches equals the input only when no
    such split occurred.

    `files` names the changed files this batch actually carries, in first-
    appearance order, taken from the `diff --git` headers inside `data` -- so a
    caller can pack per-batch context for exactly the files the batch shows. A
    batch of pure preamble names nothing and gets `[]`.

    `truncated` marks the irreducible floor: this batch alone exceeds the budget
    because a single hunk (or an unsplittable binary/rename-only section) does.
    It must be propagated, never swallowed -- it is the input to
    `diff_truncated` and therefore to the trust invariant.
    """

    data: bytes
    files: list[str] = field(default_factory=list)
    truncated: bool = False


class _Unit(NamedTuple):
    """An atomic, indivisible piece of the diff: lines plus the files they name."""

    lines: list[bytes]
    files: list[str]


def _blen(lines: list[bytes]) -> int:
    return sum(len(ln) for ln in lines)


def _unquote_c(raw: bytes) -> bytes:
    """The real bytes behind one C-quoted path body (the part between the
    quotes), with every git escape undone.

    Worth doing rather than reporting the escaped text: under `core.quotepath`
    -- git's DEFAULT -- every non-ASCII byte is emitted as `\\ooo`, so an
    undecoded label names `caf\\303\\251.py`, a path that exists nowhere. A
    caller packing per-batch context looks that up and finds nothing.

    Total, never raising: an unrecognised escape keeps its own character (which
    is what git's own unquoting does for anything not in its table), so the
    worst case is a label that is merely no better than the raw text. `files` is
    a label, never a key.
    """
    def sub(m: "re.Match[bytes]") -> bytes:
        body = m.group(1)
        if len(body) <= 3 and all(0x30 <= c <= 0x37 for c in body):
            return bytes([int(body, 8) & 0xFF])
        return _C_ESCAPES.get(body, body)

    return _C_ESCAPE.sub(sub, raw)


def _file_of(section: list[bytes]) -> str:
    """Best-effort changed-file name from a section's first `diff --git` line.

    A label for the manifest and for context packing, so it is decoded -- with
    `errors="replace"`, because a path is bytes and need not be UTF-8. A section
    with no recognisable header (a preamble) yields `""`.
    """
    for ln in section:
        if not ln.startswith(_DIFF_GIT):
            continue
        rest = ln[len(_DIFF_GIT) :].rstrip(b"\r\n")
        m = _QUOTED_PATHS.match(rest)
        if m:
            return _unquote_c(m.group(2)).decode("utf-8", "replace")
        m = _PLAIN_PATHS.match(rest)
        if m:
            return m.group(2).strip().strip(b'"').decode("utf-8", "replace")
        return rest.strip().decode("utf-8", "replace")
    return ""


def _sections(lines: list[bytes]) -> list[list[bytes]]:
    """Cut `lines` into per-file sections at `diff --git` boundaries.

    Any preamble before the first header (rare -- git chatter, a mailbox-style
    lead-in) becomes its own leading section rather than being attached to the
    first file, which would mislabel it.
    """
    sections: list[list[bytes]] = []
    cur: list[bytes] = []
    for ln in lines:
        if ln.startswith(_DIFF_GIT) and cur:
            sections.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append(cur)
    return sections


def _units(section: list[bytes], budget: int) -> list[_Unit]:
    """Turn one section into the smallest indivisible pieces it has.

    A section within budget is one unit. An oversized one is cut at its `@@`
    boundaries with the file header **repeated in front of every hunk**: a
    continuation batch that arrived as bare hunk bodies would not tell the
    reviewer which file it is looking at, or which lines -- the repetition is
    what makes each piece reviewable in isolation, and it costs only the header
    bytes. A section with no `@@` at all (binary, rename-only, mode change) has
    no smaller piece to cut to and stays one unit however large it is.
    """
    name = _file_of(section)
    files = [name] if name else []
    if _blen(section) <= budget:
        return [_Unit(section, files)]

    header: list[bytes] = []
    hunks: list[list[bytes]] = []
    cur: list[bytes] | None = None
    for ln in section:
        if ln.startswith(_HUNK):
            if cur is not None:
                hunks.append(cur)
            cur = [ln]
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        hunks.append(cur)

    if not hunks:
        return [_Unit(section, files)]
    return [_Unit(header + h, files) for h in hunks]


def split(diff: bytes, budget: int) -> list[Batch]:
    """Split `diff` into deterministic batches of at most `budget` bytes each.

    Returns batches in input order. `len(diff) <= budget` returns exactly one
    batch whose `data is` byte-identical to `diff` and whose `truncated` is
    False; `b""` returns `[]` (see the module docstring's divergence note).
    A batch exceeds `budget` only when it is a single irreducible unit, and then
    it carries `truncated=True`.

    `budget < 1` is clamped to 1 rather than rejected: a caller computing a
    budget by arithmetic (envelope minus overhead, halved for context headroom)
    can legitimately arrive at zero or below, and refusing to split is worse
    than splitting maximally and flagging every floor -- the flag already says
    the result is untrustworthy.
    """
    if budget < 1:
        budget = 1
    lines = diff.splitlines(keepends=True)

    units: list[_Unit] = []
    for section in _sections(lines):
        units.extend(_units(section, budget))

    # Greedy, order-preserving packing. Order preservation is not cosmetic:
    # reviewers read a diff in file order, and the aggregation step keys batch
    # findings back to positions in the full diff.
    batches: list[Batch] = []
    cur_lines: list[bytes] = []
    cur_files: list[str] = []
    cur_bytes = 0
    cur_truncated = False

    def flush() -> None:
        nonlocal cur_lines, cur_files, cur_bytes, cur_truncated
        if cur_lines:
            batches.append(
                Batch(
                    data=b"".join(cur_lines),
                    files=cur_files,
                    truncated=cur_truncated,
                )
            )
        cur_lines, cur_files, cur_bytes, cur_truncated = [], [], 0, False

    for unit in units:
        ub = _blen(unit.lines)
        if cur_lines and cur_bytes + ub > budget:
            flush()
        cur_lines.extend(unit.lines)
        for name in unit.files:
            if name not in cur_files:
                cur_files.append(name)
        cur_bytes += ub
        if ub > budget:
            # An over-budget unit is always alone in its batch: either the batch
            # was empty when it arrived, or the check above just flushed it. So
            # the flag describes exactly this unit, and no reviewable neighbour
            # gets tarred with a truncation it does not have.
            cur_truncated = True
            flush()
    flush()

    return batches


def changed_regions(
    diff: bytes, max_lines: int = MAX_REGION_LINES,
) -> tuple[list[str], bool]:
    r"""The `diff --git` / `@@` HEADER lines of `diff`, and whether the cap bit.

    A compact map of WHICH files and -- through git's function context on the
    `@@` line -- which regions and enclosing symbols a diff touches, with every
    hunk BODY left out. That omission is the whole point: a diff gets batched
    precisely because its bodies do not fit one prompt, so a cross-batch summary
    that carried them would be the oversized prompt batching exists to avoid.
    What is left is still real change signal, which is what lets a reader (the
    integration pass) spot a cross-file break in a batch that reported nothing.

    Returns `(lines, capped)`. `lines` are decoded with `errors="replace"` and
    stripped of their line ending -- they are a LABEL, like `Batch.files`, and a
    path or a function name need not be UTF-8. `capped` is True when reading
    stopped at `max_lines`, so the list may be incomplete; the caller owns
    whatever it wants to say about that. The flag is deliberately not a sentinel
    line in the list: a marker string mixed in among real header lines is
    indistinguishable from a diff that happens to contain one.

    Lines are cut with `bytes.splitlines()` -- the same `\r`/`\n`/`\r\n` rule
    `split` relies on, so this function and the splitter agree byte for byte
    about where a line begins. (The oracle iterates the batch FILE, which breaks
    on `\n` alone and can therefore keep a bare `\r` inside a reported header;
    unobservable on git's own output, and stricter here.)

    `max_lines < 1` is clamped to 1 rather than rejected, exactly as `split`
    clamps a non-positive budget: a caller computing a cap by arithmetic can
    land on zero, and reporting one region with `capped=True` says strictly more
    than refusing to look.
    """
    if max_lines < 1:
        max_lines = 1
    out: list[str] = []
    for ln in diff.splitlines():
        if not (ln.startswith(_DIFF_GIT) or ln.startswith(_HUNK)):
            continue
        out.append(ln.rstrip(b"\r\n").decode("utf-8", "replace"))
        if len(out) >= max_lines:
            # ORACLE BEHAVIOR: the cap is checked AFTER the append and without
            # looking ahead, so a diff with exactly `max_lines` headers reports
            # `capped` even though nothing was actually dropped. Kept rather
            # than corrected -- it is the oracle's observable behaviour, and it
            # errs toward telling the reader the map may be partial.
            return out, True
    return out, False
