"""Deterministic byte-level batch splitting of an over-budget unified diff.

`skodun.batching.split` is a port of the oracle's `split_diff_into_batches`
(`scripts/grok-prepush-review.sh`, the embedded Python at lines 842-959, reachable
standalone through the script's `--split-diff` seam at lines 968-971). The oracle
is the authority on every observable here: a batch's bytes are what a reviewer
actually saw, so a split that drifts makes this tool's batched reviews
incomparable with the archive of legacy ones.

The parity tests below therefore do not check a reimplementation against itself:
they run the *real* oracle seam over the same diff bytes and the same budget and
compare batch-for-batch, byte-for-byte. They skip when `$SKODUN_ORACLE_DIR` is
unset (public-repo hygiene: no local path may be hardcoded here).

Everything is RAW BYTES. A diff may contain invalid UTF-8 (a source file in a
legacy encoding, a binary patch); the split must be bit-identical anyway, which
is why no test here ever decodes the diff.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skodun.batching import Batch, split
from tests.conftest import oracle_dir

ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None
_NO_ORACLE = ORACLE is None or not ORACLE.exists()
requires_oracle = pytest.mark.skipif(
    _NO_ORACLE, reason="oracle checkout not present (set SKODUN_ORACLE_DIR)"
)


# --------------------------------------------------------------------------
# diff builders / measurements
# --------------------------------------------------------------------------


def _header(path: str) -> bytes:
    """The `diff --git` + index + `---`/`+++` preamble of one file section."""
    p = path.encode("utf-8")
    return (
        b"diff --git a/" + p + b" b/" + p + b"\n"
        b"index 1111111..2222222 100644\n"
        b"--- a/" + p + b"\n"
        b"+++ b/" + p + b"\n"
    )


def _hunk(start: int, count: int, fill: bytes = b"body") -> bytes:
    """One `@@` hunk with `count` added lines of `fill`."""
    head = b"@@ -%d,%d +%d,%d @@\n" % (start, count, start, count)
    return head + b"".join(b"+" + fill + b"\n" for _ in range(count))


def _section(path: str, hunks: list[bytes]) -> bytes:
    return _header(path) + b"".join(hunks)


def _widest_section(diff: bytes) -> int:
    """Size of the largest `diff --git` section in `diff`.

    Used only to CHOOSE budgets, never to assert behaviour: a budget at or above
    this leaves every section whole, which is exactly the regime where no header
    is repeated and the batches must therefore re-join to the input. Tests that
    hardcoded a number here would silently drift into the hunk-split regime the
    moment a fixture grew a byte.
    """
    sizes: list[int] = []
    cur = 0
    for ln in diff.splitlines(keepends=True):
        if ln.startswith(b"diff --git ") and cur:
            sizes.append(cur)
            cur = 0
        cur += len(ln)
    if cur:
        sizes.append(cur)
    return max(sizes) if sizes else 0


def _hunk_starts(data: bytes) -> int:
    """Count of hunk-START lines in `data`.

    A repeated file header cannot inflate this count: the repeated header is by
    construction exactly the section bytes BEFORE its first `@@` line, so it
    contains no line that starts with `@@`. That is what makes plain
    `startswith(b"@@")` a sound conservation counter across a hunk split.
    """
    return sum(1 for ln in data.splitlines() if ln.startswith(b"@@"))


TWO_FILES = _section("one.txt", [_hunk(1, 2), _hunk(40, 2)]) + _section(
    "two.txt", [_hunk(1, 1)]
)

MANY_HUNKS = _section("wide.py", [_hunk(10 + 40 * i, 3) for i in range(8)])

BIG_HUNK = _section("huge.py", [_hunk(1, 200, b"a" * 40)])

PREAMBLE = b"warning: some git chatter\n" + TWO_FILES

BINARY = (
    b"diff --git a/img.png b/img.png\n"
    b"index 1111111..2222222 100644\n"
    b"Binary files a/img.png and b/img.png differ\n"
) + b"".join(b"# padding to push this section over a small budget\n" for _ in range(6))

RENAME = (
    b"diff --git a/old/name.py b/new/name.py\n"
    b"similarity index 88%\n"
    b"rename from old/name.py\n"
    b"rename to new/name.py\n"
) + _hunk(1, 4)

QUOTED = (
    b'diff --git "a/dir/with space.py" "b/dir/with space.py"\n'
    b"index 1111111..2222222 100644\n"
    b'--- "a/dir/with space.py"\n'
    b'+++ "b/dir/with space.py"\n'
) + _hunk(1, 3)

# Invalid UTF-8 in the hunk body AND in a path: a latin-1 source file and a
# `core.quotepath=false` checkout both produce diffs like this.
INVALID_UTF8 = (
    b"diff --git a/caf\xe9.txt b/caf\xe9.txt\n"
    b"index 1111111..2222222 100644\n"
    b"--- a/caf\xe9.txt\n"
    b"+++ b/caf\xe9.txt\n"
    b"@@ -1,3 +1,3 @@\n"
    b"-na\xefve \xff\xfe bytes\n"
    b"+na\xefve \x80\x81 bytes\n"
    b"@@ -40,2 +40,2 @@\n"
    b"-\xc3\x28 not utf-8\n"
    b"+\xa0\xa1 not utf-8 either\n"
) + _section("plain.txt", [_hunk(1, 2)])

CRLF = _section("crlf.txt", [_hunk(10, 2), _hunk(40, 2)]).replace(b"\n", b"\r\n")

#: Every byte `str.splitlines` treats as a line break but `bytes.splitlines` does
#: not: \v, \f, the information separators, and the UTF-8 encodings of U+0085,
#: U+2028 and U+2029. A splitter that decoded first would cut a hunk body here.
_EXOTIC = b"\x0b\x0c\x1c\x1d\x1e\xc2\x85\xe2\x80\xa8\xe2\x80\xa9"

EXOTIC_SEPARATORS = _section(
    "exotic.txt", [_hunk(10, 2, _EXOTIC), _hunk(40, 2, _EXOTIC)]
)

HUNKS_ONLY = b"".join(_hunk(10 + 10 * i, 2) for i in range(4))

PREAMBLE_WITH_HUNKS = b"chatter with no file header\n" + HUNKS_ONLY

NO_TRAILING_NEWLINE = TWO_FILES.rstrip(b"\n")


# --------------------------------------------------------------------------
# within budget: one identical, un-truncated batch
# --------------------------------------------------------------------------


def test_within_budget_returns_one_identical_untruncated_batch():
    got = split(TWO_FILES, 10_000)
    assert len(got) == 1
    assert got[0].data == TWO_FILES  # byte-identical, not merely equivalent
    assert got[0].truncated is False
    assert got[0].files == ["one.txt", "two.txt"]


def test_exactly_at_budget_is_one_untruncated_batch():
    got = split(TWO_FILES, len(TWO_FILES))
    assert [b.data for b in got] == [TWO_FILES]
    assert got[0].truncated is False


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 37, 120, 400, 10_000])
def test_determinism_same_input_yields_byte_identical_batches_twice(budget):
    diff = PREAMBLE + MANY_HUNKS + BIG_HUNK + INVALID_UTF8
    first = split(diff, budget)
    second = split(diff, budget)
    assert first, "nothing was split -- the comparison below would be vacuous"
    assert [(b.data, b.files, b.truncated) for b in first] == [
        (b.data, b.files, b.truncated) for b in second
    ]


# --------------------------------------------------------------------------
# `diff --git` boundaries + greedy order-preserving packing
# --------------------------------------------------------------------------


def test_splits_at_diff_git_boundaries_and_preserves_order():
    a = _section("a.txt", [_hunk(1, 2)])
    b = _section("b.txt", [_hunk(1, 2)])
    c = _section("c.txt", [_hunk(1, 2)])
    # A budget that fits two sections but not three.
    budget = len(a) + len(b)
    got = split(a + b + c, budget)
    assert [x.data for x in got] == [a + b, c]
    assert [x.files for x in got] == [["a.txt", "b.txt"], ["c.txt"]]
    assert [x.truncated for x in got] == [False, False]


def test_no_hunk_split_batches_concatenate_back_to_the_input():
    """Order preservation, stated as reconstruction.

    When no section is over budget nothing is repeated, so the batches are a
    partition of the input in input order and must re-join to exactly it. This
    is what a reversed packing order would break.
    """
    sections = [_section("f%d.txt" % i, [_hunk(1, 2 + i)]) for i in range(6)]
    diff = b"".join(sections)
    for budget in (max(len(s) for s in sections), 200, 333, len(diff) - 1):
        got = split(diff, budget)
        assert b"".join(x.data for x in got) == diff
        assert [f for x in got for f in x.files] == [
            "f%d.txt" % i for i in range(6)
        ]


def test_preamble_before_the_first_header_is_its_own_leading_section():
    # Big enough that no section is hunk-split, small enough that the preamble
    # cannot ride along with the first file section.
    got = split(PREAMBLE, _widest_section(PREAMBLE))
    assert got[0].data == b"warning: some git chatter\n"
    assert got[0].files == []  # a preamble names no file
    assert got[0].truncated is False
    assert b"".join(x.data for x in got) == PREAMBLE


# --------------------------------------------------------------------------
# header repetition on hunk-split continuations
# --------------------------------------------------------------------------


def test_oversized_file_splits_at_hunks_with_the_header_repeated():
    # Equal-width hunk starts, so every `header + hunk` unit is the same size and
    # a budget of exactly that size admits all of them.
    hunks = [_hunk(10, 3), _hunk(40, 3), _hunk(80, 3)]
    header = _header("wide.py")
    diff = _section("wide.py", hunks)
    # Room for the header plus exactly one hunk, never two.
    budget = len(header) + len(hunks[0])
    assert len({len(h) for h in hunks}) == 1  # premise
    got = split(diff, budget)
    assert [x.data for x in got] == [header + h for h in hunks]
    assert all(x.files == ["wide.py"] for x in got)
    assert all(x.truncated is False for x in got)
    # Every continuation carries the full file header, not just the first batch.
    assert all(x.data.startswith(b"diff --git a/wide.py b/wide.py\n") for x in got)
    assert all(b"+++ b/wide.py\n" in x.data for x in got)


def test_hunk_split_continuations_pack_greedily_two_per_batch():
    hunks = [_hunk(10 + 10 * i, 3) for i in range(6)]
    header = _header("wide.py")
    diff = _section("wide.py", hunks)
    budget = 2 * (len(header) + len(hunks[0]))
    # Premises: equal-sized units, and the section as a whole really is over
    # budget (repeating the header is what makes 6 hunks cost more than 250 here
    # -- an unsplit section carries ONE header and would have fit).
    assert len({len(h) for h in hunks}) == 1
    assert len(diff) > budget
    got = split(diff, budget)
    assert [x.data for x in got] == [
        header + hunks[0] + header + hunks[1],
        header + hunks[2] + header + hunks[3],
        header + hunks[4] + header + hunks[5],
    ]
    # The file is named ONCE per batch even though its header appears twice.
    assert [x.files for x in got] == [["wide.py"]] * 3
    assert all(x.truncated is False for x in got)


def test_a_split_file_keeps_the_neighbouring_sections_in_order():
    before = _section("before.txt", [_hunk(10, 1)])
    hunks = [_hunk(10, 3), _hunk(40, 3)]
    header = _header("wide.py")
    after = _section("after.txt", [_hunk(10, 1)])
    diff = before + _section("wide.py", hunks) + after
    budget = len(header) + len(hunks[0])
    # Premises: only `wide.py` is over budget, and its two units are equal-sized.
    assert len(before) <= budget and len(after) <= budget
    assert len({len(h) for h in hunks}) == 1
    got = split(diff, budget)
    datas = [x.data for x in got]
    assert datas[0] == before
    assert datas[1] == header + hunks[0]
    assert datas[2] == header + hunks[1]
    assert datas[3] == after
    assert [x.files for x in got] == [
        ["before.txt"],
        ["wide.py"],
        ["wide.py"],
        ["after.txt"],
    ]


# --------------------------------------------------------------------------
# the irreducible floor: an over-budget single hunk
# --------------------------------------------------------------------------


def test_oversized_single_hunk_becomes_its_own_flagged_batch():
    big = _hunk(1, 40, b"z" * 40)
    small = _hunk(500, 1)
    header = _header("huge.py")
    diff = _section("huge.py", [big, small])
    budget = len(header) + len(small) + 10
    assert len(header) + len(big) > budget  # premise: the big hunk cannot fit
    got = split(diff, budget)
    assert [x.data for x in got] == [header + big, header + small]
    assert [x.truncated for x in got] == [True, False]
    assert len(got[0].data) > budget  # surfaced, not hidden
    assert got[0].files == ["huge.py"]


def test_unsplittable_oversized_section_without_hunks_is_one_flagged_batch():
    """A binary / rename-only section has no `@@` to split at."""
    got = split(BINARY, 40)
    assert [x.data for x in got] == [BINARY]
    assert got[0].truncated is True
    assert got[0].files == ["img.png"]


def test_a_batch_is_flagged_only_when_it_alone_exceeds_the_budget():
    diff = PREAMBLE + MANY_HUNKS + BIG_HUNK
    flagged = 0
    for budget in (25, 60, 150, 400, 900):
        got = split(diff, budget)
        assert got
        for b in got:
            assert b.truncated == (len(b.data) > budget)
            flagged += b.truncated
    assert flagged, "premise: these budgets must produce at least one floor"


@pytest.mark.parametrize("budget", [1, 20, 60, 150, 400, 900, 5000])
def test_every_batch_is_within_budget_except_flagged_floors(budget):
    diff = PREAMBLE + MANY_HUNKS + BIG_HUNK + INVALID_UTF8 + BINARY
    got = split(diff, budget)
    assert got
    for b in got:
        assert len(b.data) <= budget or b.truncated, (
            "over-budget batch must be flagged: %d > %d" % (len(b.data), budget)
        )


# --------------------------------------------------------------------------
# coverage: every hunk lands in exactly one batch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 20, 60, 150, 400, 900, 5000, 10**6])
def test_hunk_count_is_conserved_in_versus_out(budget):
    diff = PREAMBLE + MANY_HUNKS + BIG_HUNK + INVALID_UTF8 + RENAME + QUOTED
    got = split(diff, budget)
    assert _hunk_starts(diff) > 0
    assert sum(_hunk_starts(b.data) for b in got) == _hunk_starts(diff)


@pytest.mark.parametrize("budget", [1, 20, 60, 150, 400, 900, 5000])
def test_every_hunk_body_survives_verbatim_in_some_batch(budget):
    hunks = [_hunk(1 + 40 * i, 3, b"payload%d" % i) for i in range(8)]
    diff = _section("wide.py", hunks)
    got = split(diff, budget)
    for h in hunks:
        assert sum(1 for b in got if h in b.data) == 1


# --------------------------------------------------------------------------
# raw bytes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [1, 30, 90, 200, 10_000])
def test_invalid_utf8_diff_splits_bit_identically(budget):
    got = split(INVALID_UTF8, budget)
    twice = split(INVALID_UTF8, budget)
    assert [b.data for b in got] == [b.data for b in twice]
    # No re-encoding anywhere: the raw bytes are still present, and the batches
    # of a diff with no hunk repetition re-join to the input exactly.
    assert b"\xc3\x28 not utf-8\n" in b"".join(b.data for b in got)
    assert all(isinstance(b.data, bytes) for b in got)


def test_undecodable_path_is_named_with_replacement_not_raised():
    got = split(INVALID_UTF8, 10_000)
    assert got[0].files[0].startswith("caf")  # U+FFFD for the \xe9, no exception
    assert "plain.txt" in got[0].files


def test_crlf_line_endings_are_preserved_and_split_cleanly():
    """A CRLF diff must be cut BETWEEN lines, never between the CR and the LF.

    `bytes.splitlines(keepends=True)` treats a lone `\\r` as a break too, so a
    naive cut could leave a batch ending in a bare `\\r` and the next one opening
    with a bare `\\n`.
    """
    got = split(CRLF, 10_000)
    assert [b.data for b in got] == [CRLF]

    got = split(CRLF, 100)  # forces the two-hunk section apart
    assert len(got) == 2
    assert sum(_hunk_starts(b.data) for b in got) == 2
    for b in got:
        assert b.data.endswith(b"\r\n")
        assert not b.data.startswith(b"\n")
        for ln in b.data.splitlines(keepends=True):
            assert ln.endswith(b"\r\n"), "line cut inside a CRLF pair: %r" % ln


def test_diff_without_a_trailing_newline_is_not_padded():
    assert not NO_TRAILING_NEWLINE.endswith(b"\n")  # premise
    got = split(NO_TRAILING_NEWLINE, 10_000)
    assert [b.data for b in got] == [NO_TRAILING_NEWLINE]
    # Split into per-section batches (no header repetition at this budget), so
    # the pieces must re-join to exactly the input -- no synthesized newline.
    got = split(NO_TRAILING_NEWLINE, _widest_section(NO_TRAILING_NEWLINE))
    assert len(got) == 2
    assert b"".join(b.data for b in got) == NO_TRAILING_NEWLINE
    assert not got[-1].data.endswith(b"\n")


# --------------------------------------------------------------------------
# file naming
# --------------------------------------------------------------------------


def test_exotic_line_separator_bytes_are_not_cut_points():
    r"""Bit-exactness: only \r, \n and \r\n may end a line.

    `str.splitlines` also breaks on \v, \f, \x1c-\x1e and U+0085/U+2028/U+2029.
    A splitter that decoded the diff would therefore cut inside a hunk body that
    contains those bytes and hand the reviewer a corrupted fragment. This pins
    that they are ordinary payload: the two hunks come out whole.
    """
    hunks = [_hunk(10, 2, _EXOTIC), _hunk(40, 2, _EXOTIC)]
    header = _header("exotic.txt")
    assert len(EXOTIC_SEPARATORS) > len(header) + len(hunks[0])  # premise
    got = split(EXOTIC_SEPARATORS, len(header) + len(hunks[0]))
    assert [b.data for b in got] == [header + hunks[0], header + hunks[1]]
    assert all(_EXOTIC in b.data for b in got)


def test_rename_section_is_named_by_its_new_path():
    got = split(RENAME, 10_000)
    assert got[0].files == ["new/name.py"]


def test_quoted_path_with_a_space_is_unquoted():
    got = split(QUOTED, 10_000)
    assert got[0].files == ["dir/with space.py"]


def test_files_are_deduplicated_in_first_appearance_order():
    """One name per file per batch, however many headers carry it.

    Two ways the same path can appear twice inside one batch: two `diff --git`
    sections for it (a mode change plus a content change), and a hunk split that
    repeats its header. Both must collapse to a single entry -- a caller uses
    this list to pack per-batch context and would otherwise pack the file twice.
    """
    twice = _section("dup.py", [_hunk(10, 2)]) + _section("dup.py", [_hunk(40, 2)])
    diff = twice + _section("other.py", [_hunk(10, 2)])
    got = split(diff, 10_000)
    assert len(got) == 1
    assert got[0].files == ["dup.py", "other.py"]
    assert got[0].data.count(b"diff --git a/dup.py") == 2  # two headers, one name


# --------------------------------------------------------------------------
# degenerate inputs
# --------------------------------------------------------------------------


def test_empty_diff_yields_no_batches():
    """ORACLE PARITY: an empty diff produces ZERO batches, not one empty one.

    The oracle's caller treats a zero-batch split as a terminal failure
    ("diff batching produced no batches", grok-prepush-review.sh:3325-3341), so
    the emptiness must reach the caller as an empty list rather than as a batch
    with no content. This is a documented divergence from the Phase 3 brief,
    which reads `len(diff) <= budget` -> one identical batch; pinned against the
    real oracle in the parity table below.
    """
    assert split(b"", 100) == []
    assert split(b"", 1) == []


@pytest.mark.parametrize("budget", [0, -1, -10**6])
def test_budget_below_one_is_clamped_to_one(budget):
    """ORACLE PARITY: `if budget < 1: budget = 1` (grok-prepush-review.sh:847-848)."""
    got = split(TWO_FILES, budget)
    assert len(got) == 3  # two hunks of one.txt + all of two.txt, each a floor
    assert [b.data for b in got] == [b.data for b in split(TWO_FILES, 1)]
    assert all(b.truncated for b in got)


def test_diff_of_only_hunks_without_any_git_header():
    got = split(HUNKS_ONLY, 60)
    assert len(got) > 1
    assert b"".join(b.data for b in got) == HUNKS_ONLY
    assert all(b.files == [] for b in got)
    assert sum(_hunk_starts(b.data) for b in got) == 4


def test_oversized_preamble_with_hunks_repeats_the_preamble_as_its_header():
    """ORACLE PARITY: the "header" repeated on a split is whatever preceded the
    section's first `@@`, even when that is untitled preamble rather than a
    `diff --git` block. Odd-looking, and exactly what the oracle does."""
    lead = b"chatter with no file header\n"
    got = split(PREAMBLE_WITH_HUNKS, 60)
    assert len(got) > 1
    assert all(b.data.startswith(lead) for b in got)
    assert all(b.files == [] for b in got)
    assert sum(_hunk_starts(b.data) for b in got) == 4


def test_batch_is_a_value_with_the_documented_shape():
    b = Batch(data=b"x", files=["a"], truncated=True)
    assert (b.data, b.files, b.truncated) == (b"x", ["a"], True)
    assert Batch(data=b"x", files=["a"], truncated=True) == b


# --------------------------------------------------------------------------
# oracle parity
# --------------------------------------------------------------------------


def _oracle_split(work: Path, diff: bytes, budget: int) -> list[Batch]:
    """Run the oracle's `--split-diff` seam and read back its batches."""
    work.mkdir(parents=True, exist_ok=True)
    src = work / "in.diff"
    src.write_bytes(diff)
    cp = subprocess.run(
        [
            "sh",
            str(ORACLE),
            "--split-diff",
            str(src),
            str(work / "out"),
            str(budget),
        ],
        capture_output=True,
    )
    assert cp.returncode == 0, "oracle rc=%d stderr=%r" % (cp.returncode, cp.stderr)
    lines = [ln for ln in cp.stdout.decode("utf-8").splitlines() if ln.strip()]
    assert lines, "oracle --split-diff printed no manifest"
    manifest = json.loads(lines[-1])
    out = []
    for entry in manifest["batches"]:
        data = Path(entry["file"]).read_bytes()
        assert entry["bytes"] == len(data)
        out.append(
            Batch(data=data, files=list(entry["files"]), truncated=entry["truncated"])
        )
    assert manifest["batch_count"] == len(out)
    return out


PARITY_CASES = [
    ("empty", b"", 100),
    ("within-budget", TWO_FILES, 10_000),
    ("two-files-tight", TWO_FILES, 100),
    ("two-files-exact", TWO_FILES, len(TWO_FILES)),
    ("many-hunks", MANY_HUNKS, 150),
    ("many-hunks-floor", MANY_HUNKS, 20),
    ("big-hunk", BIG_HUNK, 300),
    ("preamble", PREAMBLE, 60),
    ("binary", BINARY, 40),
    ("rename", RENAME, 60),
    ("quoted-path", QUOTED, 60),
    ("invalid-utf8", INVALID_UTF8, 90),
    ("invalid-utf8-wide", INVALID_UTF8, 10_000),
    ("crlf", CRLF, 60),
    ("crlf-wide", CRLF, 10_000),
    ("exotic-separators", EXOTIC_SEPARATORS, 130),
    ("hunks-only", HUNKS_ONLY, 60),
    ("preamble-with-hunks", PREAMBLE_WITH_HUNKS, 60),
    ("no-trailing-newline", NO_TRAILING_NEWLINE, 90),
    ("budget-zero", TWO_FILES, 0),
    ("budget-negative", TWO_FILES, -5),
    ("mixed", PREAMBLE + MANY_HUNKS + BIG_HUNK + INVALID_UTF8 + BINARY, 250),
]


@requires_oracle
@pytest.mark.parametrize(
    "diff,budget", [c[1:] for c in PARITY_CASES], ids=[c[0] for c in PARITY_CASES]
)
def test_parity_with_oracle_split_diff(tmp_path, diff, budget):
    expected = _oracle_split(tmp_path / "o", diff, budget)
    got = split(diff, budget)
    assert len(got) == len(expected), "batch count: %d != %d" % (
        len(got),
        len(expected),
    )
    for i, (g, e) in enumerate(zip(got, expected)):
        assert g.data == e.data, "batch %d bytes differ" % (i + 1)
        assert g.files == e.files, "batch %d files differ" % (i + 1)
        assert g.truncated == e.truncated, "batch %d truncated flag differs" % (i + 1)


@requires_oracle
@pytest.mark.parametrize("budget", [1, 17, 64, 199, 512, 4096])
def test_parity_across_budgets_on_one_wide_diff(tmp_path, budget):
    diff = (
        b"".join(
            _section(
                "pkg/mod%02d.py" % i,
                [
                    _hunk(10 + 40 * j, 2 + j, b"p%d%d" % (i, j))
                    for j in range(1 + i % 3)
                ],
            )
            for i in range(12)
        )
        + BIG_HUNK
    )
    expected = _oracle_split(tmp_path / ("o%d" % budget), diff, budget)
    got = split(diff, budget)
    assert [(b.data, b.files, b.truncated) for b in got] == [
        (b.data, b.files, b.truncated) for b in expected
    ]


@requires_oracle
def test_parity_on_a_real_repo_diff(tmp_path):
    """Parity over a diff git actually produced, not a synthetic one."""
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    for i in range(6):
        (repo / ("f%d.py" % i)).write_bytes(
            b"".join(b"line %d of file %d\n" % (n, i) for n in range(30))
        )
    git("add", ".")
    git("commit", "-m", "c0")
    for i in range(6):
        p = repo / ("f%d.py" % i)
        body = p.read_bytes().split(b"\n")
        body[2] = b"CHANGED near the top of file %d" % i
        body[25] = b"CHANGED near the bottom of file %d" % i
        p.write_bytes(b"\n".join(body))
    (repo / "latin1.txt").write_bytes(b"caf\xe9 na\xefve\n")
    git("add", ".")
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "--no-pager",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--cached",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert diff
    for budget in (1, 100, 400, 1500, len(diff)):
        expected = _oracle_split(tmp_path / ("o%d" % budget), diff, budget)
        got = split(diff, budget)
        assert [(b.data, b.files, b.truncated) for b in got] == [
            (b.data, b.files, b.truncated) for b in expected
        ]
