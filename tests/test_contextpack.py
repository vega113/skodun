"""Tests for `skodun.contextpack` — hardened working-tree context packing.

Two kinds of assertion live here and they carry different weight:

  * Byte-level fixtures (`test_exact_section_bytes*`) pin the prompt format.
    The markers are part of the prompt the model reads; a stray space or a
    renamed marker changes every review's input and silently invalidates the
    stored reviews it is compared against.
  * Hardening tests pin the *failure* modes: a path that escapes the worktree,
    a symlink anywhere in the path, and a FIFO must all degrade to an omission
    reason. A FIFO in particular must never block — a hung packer hangs the
    whole review, so that test is guarded by a real timeout rather than by
    trusting the implementation not to block. Nor may any input *raise*: a
    non-UTF-8 path and a path with an embedded NUL both reach `os.*` calls
    that fail with `ValueError` rather than `OSError`, and both must come back
    as an omission reason.

Several tests here exist to pin a constant or a layer that outcome-only
assertions cannot distinguish — an exact threshold, a check that must run
before a syscall rather than after it. Each says in its docstring what would
otherwise go unnoticed, because a fixture whose numbers look arbitrary is the
first thing a later edit will "simplify".

Oracle parity tests drive `scripts/grok-context-pack.py` through its env
interface and compare body bytes, hash, inclusions and omissions.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from skodun.contextpack import REASONS, Pack, pack
from tests.conftest import oracle_dir

ORACLE_SCRIPT = "scripts/grok-context-pack.py"

# The brief's status map: every path used by the small unit tests below.
M = {"big.py": "M", "small.py": "M", "link.txt": "M", "b.bin": "M",
     "../etc/passwd": "M", "/etc/passwd": "M", "new.py": "A", "gone.py": "D"}


def _reasons(p: Pack) -> dict[str, str]:
    return dict(p.omitted)


def test_reason_vocabulary_is_exactly_the_documented_set():
    # The reasons are rendered into the prompt header and read back by
    # callers, so the vocabulary is an interface, not an implementation note.
    assert REASONS == ("deleted", "binary", "already-in-diff", "missing",
                       "over-file-cap", "over-headroom")


def test_every_emitted_reason_is_in_the_vocabulary(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00" * 10)
    (tmp_path / "big.py").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "new.py").write_text("tiny", encoding="utf-8")
    (tmp_path / "capped.py").write_text("y" * 400, encoding="utf-8")
    # Under the per-file cap but over the headroom: the only path to the
    # sixth reason once the cap has claimed the bigger files.
    (tmp_path / "medium.py").write_text("z" * 320, encoding="utf-8")
    p = pack(tmp_path, ["b.bin", "big.py", "new.py", "gone.py", "capped.py",
                        "medium.py", "nope.py"], M,
             headroom=300, per_file_cap=350)
    got = {r for _, r in p.omitted}
    assert got <= set(REASONS)
    # All six reachable at once, so this is not a one-reason smoke test.
    assert got == set(REASONS)


# --------------------------------------------------------------------------
# Selection, all-or-nothing, determinism
# --------------------------------------------------------------------------

def test_all_or_nothing_and_deterministic(tmp_path):
    (tmp_path / "big.py").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "small.py").write_text("y" * 100, encoding="utf-8")
    p1 = pack(tmp_path, ["big.py", "small.py"], M, headroom=600)
    assert p1.included == ["small.py"]  # big can't fit whole -> skipped whole
    assert ("big.py", "over-headroom") in p1.omitted
    # Nothing was truncated: the small file is present in full.
    assert b"y" * 100 in p1.body
    assert b"x" * 5000 not in p1.body
    p2 = pack(tmp_path, ["big.py", "small.py"], M, headroom=600)
    assert p1.body == p2.body
    assert p1.sha256 == p2.sha256 == hashlib.sha256(p1.body).hexdigest()
    assert p1.bytes_total == len(p1.body) > 0


def test_added_and_deleted_classified(tmp_path):
    (tmp_path / "new.py").write_text("tiny added file", encoding="utf-8")
    p = pack(tmp_path, ["new.py", "gone.py"], M, headroom=10_000)
    assert ("new.py", "already-in-diff") in p.omitted  # A + <16KiB
    assert ("gone.py", "deleted") in p.omitted
    assert p.included == []


def test_large_added_file_is_packed(tmp_path):
    # >=16 KiB: a single-shot diff may not carry the whole file, so pack it.
    (tmp_path / "new.py").write_text("z" * 20_000, encoding="utf-8")
    p = pack(tmp_path, ["new.py"], {"new.py": "A"}, headroom=100_000)
    assert p.included == ["new.py"]
    assert b"z" * 20_000 in p.body


def test_already_in_diff_threshold_is_exactly_16384(tmp_path):
    """Pins `ALREADY_IN_DIFF_MAX` to the value, not to a band around it.

    One byte either side of 16 KiB, so 16383, 16385 and a `<`-to-`<=` slip all
    change an outcome asserted here.
    """
    (tmp_path / "under.py").write_text("u" * 16_383, encoding="utf-8")
    (tmp_path / "at.py").write_text("a" * 16_384, encoding="utf-8")
    st = {"under.py": "A", "at.py": "A"}
    p = pack(tmp_path, ["under.py", "at.py"], st, headroom=100_000)
    assert ("under.py", "already-in-diff") in p.omitted
    assert p.included == ["at.py"]


def test_pack_large_added_off_leaves_headroom_for_the_modified_files(tmp_path):
    """The opt-out exists because size-descending selection has a bad case.

    A big added file sorts first and can consume the whole envelope, evicting
    the modified files the review is actually about. On a single-shot review
    the diff already carries that added file whole, so packing it buys nothing;
    the default stays on because a batched diff may not carry it.
    """
    (tmp_path / "new.py").write_text("z" * 30_000, encoding="utf-8")
    (tmp_path / "mod.py").write_text("m" * 2_000, encoding="utf-8")
    files = ["new.py", "mod.py"]
    statuses = {"new.py": "A", "mod.py": "M"}

    on = pack(tmp_path, files, statuses, headroom=31_000)
    assert on.included == ["new.py"]
    assert ("mod.py", "over-headroom") in on.omitted

    off = pack(tmp_path, files, statuses, headroom=31_000, pack_large_added=False)
    assert off.included == ["mod.py"]
    assert ("new.py", "already-in-diff") in off.omitted
    assert b"m" * 2_000 in off.body
    assert b"z" * 30_000 not in off.body


def test_pack_large_added_off_still_packs_a_large_modified_file(tmp_path):
    # The flag is about `A` only: it must not touch anything else.
    (tmp_path / "mod.py").write_text("m" * 30_000, encoding="utf-8")
    p = pack(tmp_path, ["mod.py"], {"mod.py": "M"}, headroom=100_000,
             pack_large_added=False)
    assert p.included == ["mod.py"]


def test_missing_added_file_is_missing_not_already_in_diff(tmp_path):
    # Size is resolved before the added/16KiB branch, so an unreadable added
    # path reports `missing` rather than `already-in-diff`.
    p = pack(tmp_path, ["new.py"], {"new.py": "A"}, headroom=10_000)
    assert _reasons(p) == {"new.py": "missing"}


def test_absent_from_statuses_is_treated_as_modified(tmp_path):
    (tmp_path / "a.txt") .write_text("hello\n", encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=10_000)
    assert p.included == ["a.txt"]


def test_ordering_is_size_descending_path_ascending(tmp_path):
    # b/a tie at 300 bytes pins the tie-break; c is bigger and must come first.
    (tmp_path / "c.txt").write_text("c" * 900, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 300, encoding="utf-8")
    (tmp_path / "a.txt").write_text("a" * 300, encoding="utf-8")
    # Input order deliberately unsorted and not size-ordered.
    p = pack(tmp_path, ["b.txt", "a.txt", "c.txt"], {}, headroom=10_000)
    assert p.included == ["c.txt", "a.txt", "b.txt"]
    # And the body sections follow the same order.
    assert p.body.index(b"c" * 900) < p.body.index(b"a" * 300) < p.body.index(b"b" * 300)


def test_tie_break_decides_which_file_fits(tmp_path):
    # Only one of the two equal-size files fits: the ascending path wins.
    # Headroom leaves room for exactly one 371-byte section plus the 42-byte
    # omission header, but nowhere near a second section.
    (tmp_path / "b.txt").write_text("b" * 300, encoding="utf-8")
    (tmp_path / "a.txt").write_text("a" * 300, encoding="utf-8")
    p = pack(tmp_path, ["b.txt", "a.txt"], {}, headroom=450)
    assert p.included == ["a.txt"]
    assert ("b.txt", "over-headroom") in p.omitted
    assert p.body.startswith(b"Context omitted for: b.txt (over-headroom)\n")


# --------------------------------------------------------------------------
# Hardening
# --------------------------------------------------------------------------

def test_symlink_rejected(tmp_path):
    (tmp_path / "real.txt").write_text("secret", encoding="utf-8")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    p = pack(tmp_path, ["link.txt"], M, headroom=10_000)
    assert p.included == []
    assert p.omitted[0][0] == "link.txt"
    assert b"secret" not in p.body


def test_symlink_rejected_at_non_final_component(tmp_path):
    # The leaf is an ordinary file; the *directory* component is the symlink.
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "file.txt").write_text("secret", encoding="utf-8")
    os.symlink(real_dir, tmp_path / "link_dir")
    # Sanity: the path really does resolve to a readable regular file.
    assert (tmp_path / "link_dir" / "file.txt").read_text(encoding="utf-8") == "secret"
    p = pack(tmp_path, ["link_dir/file.txt"], {}, headroom=10_000)
    assert p.included == []
    assert _reasons(p) == {"link_dir/file.txt": "missing"}
    assert b"secret" not in p.body


def test_symlink_escaping_the_worktree_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    os.symlink(outside / "passwd", repo / "escape.txt")
    p = pack(repo, ["escape.txt"], {}, headroom=10_000)
    assert p.included == []
    assert b"root:x:0:0" not in p.body


def test_symlink_swapped_in_after_the_component_walk(tmp_path, monkeypatch):
    """`O_NOFOLLOW` closes the component walk's check-then-use window.

    Both earlier layers are made to lie, exactly as an attacker who wins the
    race would make them lie: the component walk reports "not a symlink" and
    the preflight `lstat` reports a regular file. The link points *inside* the
    worktree, so the resolved-under-root check cannot help either, and
    `O_NOFOLLOW` on the open is the only layer left. Without it the packer
    would emit `real.txt`'s contents labelled as `link.txt`.
    """
    (tmp_path / "real.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("regular\n", encoding="utf-8")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    real_lstat = os.lstat

    def lying_lstat(path, *a, **kw):
        if str(path).endswith("link.txt"):
            return real_lstat(decoy)
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", lying_lstat)
    p = pack(tmp_path, ["link.txt", "ok.txt"], {}, headroom=10_000)
    assert p.included == ["ok.txt"]
    assert ("link.txt", "missing") in p.omitted
    assert b"secret" not in p.body


@pytest.mark.parametrize("bad", [
    "../etc/passwd", "/etc/passwd", "\\etc\\passwd", "C:/Windows/win.ini",
    "sub/../../etc/passwd", "", ".", "./..",
])
def test_traversal_rejected(tmp_path, bad):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd").write_text("decoy", encoding="utf-8")
    p = pack(tmp_path, [bad], M, headroom=10_000)
    assert p.included == []
    assert b"decoy" not in p.body


def test_traversal_rejected_brief_case(tmp_path):
    p = pack(tmp_path, ["../etc/passwd", "/etc/passwd"], M, headroom=10_000)
    assert p.included == []
    assert _reasons(p) == {"../etc/passwd": "missing", "/etc/passwd": "missing"}


def test_windows_drive_prefix_rejected_when_the_path_really_exists(tmp_path):
    """The drive-prefix rejection, pinned non-vacuously.

    `C:/Windows/win.ini` in the parametrised case above proves nothing on
    POSIX — the path simply does not exist, so every rejection layer is
    unreachable and deleting the `path[1] == ':'` check keeps the suite green.
    Here `C:` is a real directory (a legal POSIX name) holding a real file, so
    only that check stands between the packer and its contents.
    """
    win = tmp_path / "C:" / "Windows"
    win.mkdir(parents=True)
    (win / "win.ini").write_text("drive-secret", encoding="utf-8")
    # Sanity: without the prefix check this path resolves to a readable file.
    assert (tmp_path / "C:" / "Windows" / "win.ini").is_file()

    p = pack(tmp_path, ["C:/Windows/win.ini"], {}, headroom=10_000)
    assert p.included == []
    assert _reasons(p) == {"C:/Windows/win.ini": "missing"}
    assert b"drive-secret" not in p.body
    # The backslash spelling of the same path is rejected too.
    assert pack(tmp_path, ["C:\\Windows\\win.ini"], {}, headroom=10_000).included == []


def test_embedded_nul_in_a_path_degrades_to_missing(tmp_path):
    """A NUL in a path raises `ValueError` from `os.*`, not `OSError`.

    That is the blind spot the resolve-under-root block and the `ValueError`
    arms on the `lstat`/`open` calls both cover; catching only `OSError` lets
    the exception escape `pack()` and kill the review. This pins the contract
    (degrade to a reason) rather than which layer catches it.
    """
    p = pack(tmp_path, ["a\x00b.txt"], {}, headroom=10_000)
    assert p.included == []
    assert _reasons(p) == {"a\x00b.txt": "missing"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_non_regular_paths_are_rejected_before_any_open(tmp_path, monkeypatch):
    """The preflight `S_ISREG(lstat)` must reject *before* `os.open` runs.

    The post-open `fstat` also rejects a FIFO, so outcome-only assertions
    cannot tell the two layers apart and deleting the preflight looks free.
    It is not: opening a FIFO is observable from outside — it releases a writer
    blocked in `open(2)` — and the packer must not perturb the tree it reads.
    So this asserts on the syscall, which is the thing the layer exists to
    avoid.
    """
    os.mkfifo(tmp_path / "pipe.fifo")
    (tmp_path / "adir").mkdir()
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")

    opened: list[str] = []
    real_open = os.open

    def spy_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    p = pack(tmp_path, ["pipe.fifo", "adir", "ok.txt"], {}, headroom=10_000)

    assert not any(o.endswith("pipe.fifo") for o in opened), opened
    assert not any(o.endswith("adir") for o in opened), opened
    # Not vacuous: the regular file *was* opened, so the spy really was live.
    assert any(o.endswith("ok.txt") for o in opened), opened
    assert p.included == ["ok.txt"]
    assert _reasons(p) == {"pipe.fifo": "missing", "adir": "missing"}


def test_binary_omitted(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02" * 100)
    p = pack(tmp_path, ["b.bin"], M, headroom=10_000)
    assert ("b.bin", "binary") in p.omitted
    assert p.included == []


def test_binary_by_nontext_ratio_without_nul(tmp_path):
    # No NUL byte at all: >30% control bytes must still read as binary.
    (tmp_path / "b.bin").write_bytes(b"\x01\x02\x03\x04" * 200 + b"abcdef" * 50)
    p = pack(tmp_path, ["b.bin"], M, headroom=10_000)
    assert ("b.bin", "binary") in p.omitted


def test_nontext_ratio_threshold_is_exactly_thirty_percent(tmp_path):
    """Pins `BINARY_NONTEXT_RATIO` to 0.30 and to `>` rather than `>=`.

    Two 1000-byte NUL-free files straddling the ratio by a single byte, so the
    verdict flips for any other cut point and for an inclusive comparison.
    """
    (tmp_path / "at.txt").write_bytes(b"\x01" * 300 + b"a" * 700)     # == 0.30
    (tmp_path / "over.bin").write_bytes(b"\x01" * 301 + b"a" * 699)   # > 0.30
    p = pack(tmp_path, ["at.txt", "over.bin"], {}, headroom=100_000)
    assert p.included == ["at.txt"]
    assert ("over.bin", "binary") in p.omitted


def test_del_byte_counts_as_non_text(tmp_path):
    # 0x7F only: no NUL and no C0 control byte, so the file reads as text
    # unless `b == 127` is in the non-text class. 400/1000 = 0.40 > 0.30.
    (tmp_path / "del.bin").write_bytes(b"\x7f" * 400 + b"a" * 600)
    p = pack(tmp_path, ["del.bin"], {}, headroom=100_000)
    assert ("del.bin", "binary") in p.omitted
    assert p.included == []


def test_binary_peek_window_is_exactly_8192_bytes(tmp_path):
    """Pins `BINARY_PEEK_BYTES`: non-text bytes are front-loaded, so the
    verdict depends on how much of the file the sample covers.

      * `wide_win.bin` is 8500 bytes with 2500 non-text at the front:
        2500/8192 = 0.305 -> binary, but a *larger* window sees the whole file
        (2500/8500 = 0.294) and calls it text.
      * `narrow_win.txt` is 9000 bytes with 2000 non-text at the front:
        2000/8192 = 0.244 -> text, but a *smaller* window (2000/4096 = 0.49,
        1024/1024 = 1.0) calls it binary.

    Together they bracket 8192 from both sides.
    """
    (tmp_path / "wide_win.bin").write_bytes(b"\x01" * 2500 + b"a" * 6000)
    (tmp_path / "narrow_win.txt").write_bytes(b"\x01" * 2000 + b"a" * 7000)
    p = pack(tmp_path, ["wide_win.bin", "narrow_win.txt"], {}, headroom=100_000)
    assert ("wide_win.bin", "binary") in p.omitted
    assert p.included == ["narrow_win.txt"]


def test_nul_after_the_peek_window_still_omitted(tmp_path):
    # Clean first 8 KiB, NUL beyond it: the full-content check must catch it.
    (tmp_path / "b.bin").write_bytes(b"a" * 9000 + b"\x00" + b"b" * 10)
    p = pack(tmp_path, ["b.bin"], {}, headroom=100_000)
    assert ("b.bin", "binary") in p.omitted
    assert p.included == []


def test_missing_file(tmp_path):
    p = pack(tmp_path, ["nope.py"], {}, headroom=10_000)
    assert _reasons(p) == {"nope.py": "missing"}


def test_directory_is_not_packable(tmp_path):
    (tmp_path / "adir").mkdir()
    p = pack(tmp_path, ["adir"], {}, headroom=10_000)
    assert _reasons(p) == {"adir": "missing"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_fifo_does_not_block(tmp_path):
    """A named pipe among the candidates must be omitted, never opened-and-read.

    Reading a FIFO with no writer blocks forever; a packer that did so would
    hang the review process. The timeout is the assertion.
    """
    os.mkfifo(tmp_path / "pipe.fifo")
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    box: dict[str, Pack] = {}

    def run() -> None:
        box["p"] = pack(tmp_path, ["pipe.fifo", "ok.txt"], {}, headroom=10_000)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "pack() blocked on a FIFO"
    p = box["p"]
    assert ("pipe.fifo", "missing") in p.omitted
    assert p.included == ["ok.txt"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_fifo_swapped_in_after_the_preflight_stat(tmp_path, monkeypatch):
    """Simulate the TOCTOU race the post-open `fstat` exists to close.

    `os.lstat` is made to lie about the FIFO (reporting the mode of a real
    regular file), so only the post-open `S_ISREG(os.fstat(fd))` check can
    reject it — and `O_NONBLOCK` is what keeps the `os.open` itself from
    hanging while it does.
    """
    os.mkfifo(tmp_path / "pipe.fifo")
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("regular\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")

    real_lstat = os.lstat

    def lying_lstat(path, *a, **kw):
        if str(path).endswith("pipe.fifo"):
            return real_lstat(decoy)
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(os, "lstat", lying_lstat)
    box: dict[str, Pack] = {}

    def run() -> None:
        box["p"] = pack(tmp_path, ["pipe.fifo", "ok.txt"], {}, headroom=10_000)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "pack() blocked opening a FIFO past the preflight"
    p = box["p"]
    assert ("pipe.fifo", "missing") in p.omitted
    assert p.included == ["ok.txt"]


# --------------------------------------------------------------------------
# Caps, headroom, body assembly
# --------------------------------------------------------------------------

def test_per_file_cap(tmp_path):
    (tmp_path / "a.txt").write_text("a" * 100, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 10, encoding="utf-8")
    p = pack(tmp_path, ["a.txt", "b.txt"], {}, headroom=10_000, per_file_cap=50)
    assert ("a.txt", "over-file-cap") in p.omitted
    assert p.included == ["b.txt"]


def test_per_file_cap_is_a_maximum_not_an_exclusive_bound(tmp_path):
    # A file of exactly the cap fits; one byte more does not. Pins `>` against
    # a slip to `>=`, which would silently cost every caller one byte of cap.
    (tmp_path / "at.txt").write_text("a" * 50, encoding="utf-8")
    (tmp_path / "over.txt").write_text("b" * 51, encoding="utf-8")
    p = pack(tmp_path, ["at.txt", "over.txt"], {}, headroom=10_000, per_file_cap=50)
    assert p.included == ["at.txt"]
    assert ("over.txt", "over-file-cap") in p.omitted


def test_second_fit_check_catches_a_decode_that_widens(tmp_path):
    """The post-encode re-check is load-bearing, not a paranoid duplicate.

    `errors="replace"` widens invalid UTF-8, so bytes budgeted at `size + 1`
    can render far larger. 1000 bytes of `0xE9` carry no NUL and no control
    byte, so they classify as text and are budgeted at 1001 — but each byte is
    an invalid sequence and decodes to U+FFFD, three bytes each, so the section
    body is ~3000. The pre-check passes on the stale estimate and only the
    re-check against the bytes actually produced keeps the body inside
    headroom. Truncating here instead of omitting would overrun it ~3x.

    No race and no filesystem timing is involved: the file never changes.
    """
    (tmp_path / "wide.txt").write_bytes(b"\xe9" * 1000)
    overhead = len(b"----- BEGIN FILE CONTEXT: wide.txt -----\n"
                   b"----- END FILE CONTEXT -----\n")
    # Exactly what the pre-check budgets: `overhead + size + 1`. So the
    # pre-check passes by a hair and the re-check is the only gate left.
    headroom = overhead + 1000 + 1
    p = pack(tmp_path, ["wide.txt"], {}, headroom=headroom)

    assert p.included == []
    assert _reasons(p) == {"wide.txt": "over-headroom"}
    assert len(p.body) <= headroom
    assert p.body == b"Context omitted for: wide.txt (over-headroom)\n"
    # Nothing of the widened section leaked in, whole or truncated.
    assert b"BEGIN FILE CONTEXT" not in p.body
    assert "�".encode("utf-8") not in p.body
    # Not vacuous: enough headroom for the *rendered* size and it is included.
    roomy = pack(tmp_path, ["wide.txt"], {}, headroom=100_000)
    assert roomy.included == ["wide.txt"]
    assert len(roomy.body) > headroom


def test_per_file_cap_below_one_is_ignored(tmp_path):
    (tmp_path / "a.txt").write_text("a" * 100, encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=10_000, per_file_cap=0)
    assert p.included == ["a.txt"]


def test_zero_headroom_packs_nothing_but_reports(tmp_path):
    (tmp_path / "a.txt").write_text("a" * 100, encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=0)
    assert p.body == b""
    assert p.bytes_total == 0
    assert p.included == []
    assert ("a.txt", "over-headroom") in p.omitted
    assert p.sha256 == hashlib.sha256(b"").hexdigest()


def test_body_never_exceeds_headroom(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("x" * (100 * (i + 1)), encoding="utf-8")
    names = [f"f{i}.txt" for i in range(6)]
    for headroom in range(1, 900, 37):
        p = pack(tmp_path, names, {}, headroom=headroom)
        assert len(p.body) <= headroom, (headroom, len(p.body))


def test_header_dropped_when_it_cannot_fit(tmp_path):
    # One section fits exactly; the omission header cannot. Sections are
    # dropped until the header fits, and if nothing fits the header goes too.
    (tmp_path / "a.txt").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 4000, encoding="utf-8")
    exact = pack(tmp_path, ["a.txt"], {}, headroom=100_000)
    p = pack(tmp_path, ["a.txt", "b.txt"], {}, headroom=len(exact.body))
    # No room for `Context omitted for: b.txt (over-headroom)\n` alongside it,
    # so a.txt is dropped and the header (now listing both) is emitted alone.
    assert p.included == []
    assert set(p.omitted) == {("a.txt", "over-headroom"), ("b.txt", "over-headroom")}
    assert p.body == b"Context omitted for: a.txt (over-headroom), b.txt (over-headroom)\n"


def test_budget_reserves_a_newline_even_when_the_file_has_one(tmp_path):
    """The fit test budgets `size + 1` before knowing if a newline is needed.

    For a file that already ends in a newline that reservation is one byte
    too many, so a section that would have fitted *exactly* is omitted. That
    conservatism is the oracle's, and it is deliberate: the pre-check runs
    against `st_size`, before the bytes are read, and a file that grew or
    lacked its final newline must not be able to overshoot the envelope.
    """
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    exact = len(b"----- BEGIN FILE CONTEXT: a.txt -----\nhello\n"
                b"----- END FILE CONTEXT -----\n")
    p = pack(tmp_path, ["a.txt"], {}, headroom=exact)
    assert p.included == []
    assert p.body == b"Context omitted for: a.txt (over-headroom)\n"
    # One more byte of headroom and it fits.
    assert pack(tmp_path, ["a.txt"], {}, headroom=exact + 1).included == ["a.txt"]


def test_source_oid_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        pack(tmp_path, [], {}, headroom=10, source="oid")


def test_duplicate_paths_collapse(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    p = pack(tmp_path, ["a.txt", "a.txt"], {}, headroom=10_000)
    assert p.included == ["a.txt"]
    assert p.body.count(b"BEGIN FILE CONTEXT") == 1


# --------------------------------------------------------------------------
# Byte-level prompt-format fixtures — these pin prompt parity
# --------------------------------------------------------------------------

def test_exact_section_bytes(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=10_000)
    assert p.body == (
        b"----- BEGIN FILE CONTEXT: a.txt -----\n"
        b"hello\n"
        b"----- END FILE CONTEXT -----\n"
    )
    assert p.bytes_total == len(p.body)
    assert p.sha256 == hashlib.sha256(p.body).hexdigest()


def test_exact_bytes_with_omission_header_and_two_files(tmp_path):
    (tmp_path / "a.txt").write_text("aaa\n", encoding="utf-8")
    (tmp_path / "z.txt").write_text("zzzzzzzz\n", encoding="utf-8")
    p = pack(tmp_path, ["a.txt", "gone.py", "z.txt"], {"gone.py": "D"},
             headroom=10_000)
    assert p.body == (
        b"Context omitted for: gone.py (deleted)\n"
        b"----- BEGIN FILE CONTEXT: z.txt -----\n"
        b"zzzzzzzz\n"
        b"----- END FILE CONTEXT -----\n"
        b"----- BEGIN FILE CONTEXT: a.txt -----\n"
        b"aaa\n"
        b"----- END FILE CONTEXT -----\n"
    )


def test_missing_trailing_newline_is_added(tmp_path):
    (tmp_path / "a.txt").write_text("no-newline", encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=10_000)
    assert p.body == (
        b"----- BEGIN FILE CONTEXT: a.txt -----\n"
        b"no-newline\n"
        b"----- END FILE CONTEXT -----\n"
    )


def test_empty_file_section(tmp_path):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    p = pack(tmp_path, ["a.txt"], {}, headroom=10_000)
    assert p.body == (
        b"----- BEGIN FILE CONTEXT: a.txt -----\n"
        b"\n"
        b"----- END FILE CONTEXT -----\n"
    )


def test_omission_order_follows_input_order(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00" * 10)
    p = pack(tmp_path, ["gone.py", "b.bin", "nope.py"],
             {"gone.py": "D"}, headroom=10_000)
    assert p.omitted == [("gone.py", "deleted"), ("b.bin", "binary"),
                         ("nope.py", "missing")]
    assert p.body == (
        b"Context omitted for: gone.py (deleted), b.bin (binary), "
        b"nope.py (missing)\n"
    )


# --------------------------------------------------------------------------
# Non-UTF-8 (surrogate-escaped) paths
#
# `gitio._paths` decodes git's path stream with `errors="surrogateescape"` on
# purpose — `replace` would mangle a non-UTF-8 filename and open the wrong
# file — so `Diff.files` and `Diff.statuses` legitimately carry lone
# surrogates. Every one of these paths must degrade like any other candidate.
# The bytes emitted for such a path are the bytes git gave us, which is the
# only rendering that names the real file.
# --------------------------------------------------------------------------

# What `b"caf\xe9.txt"` becomes after gitio's decode: latin-1 'é', not UTF-8.
SURROGATE_NAME = b"caf\xe9.txt".decode("utf-8", "surrogateescape")


def _redirect(monkeypatch, name: str, target: Path) -> None:
    """Make `os.lstat`/`os.open` treat a path ending in `name` as `target`.

    Several filesystems (APFS among them) refuse to create a file whose name
    is not valid UTF-8, so a surrogate name cannot simply be written to
    `tmp_path` and still run everywhere. Redirecting the two syscalls that
    reach the filesystem exercises the whole packer against such a path
    without needing the filesystem to store one.
    """
    real_lstat, real_open = os.lstat, os.open

    def lstat(path, *a, **kw):
        return real_lstat(target if str(path).endswith(name) else path, *a, **kw)

    def opn(path, *a, **kw):
        return real_open(target if str(path).endswith(name) else path, *a, **kw)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "open", opn)


def test_surrogate_path_in_the_omitted_position(tmp_path):
    # Nothing on disk: the path is reported, and reporting it is what used to
    # raise `UnicodeEncodeError` out of `pack()` and take the review with it.
    p = pack(tmp_path, [SURROGATE_NAME], {}, headroom=10_000)
    assert p.included == []
    assert _reasons(p) == {SURROGATE_NAME: "missing"}
    assert p.body == b"Context omitted for: caf\xe9.txt (missing)\n"


def test_surrogate_path_in_the_included_position(tmp_path, monkeypatch):
    (tmp_path / "real.txt").write_text("body-bytes\n", encoding="utf-8")
    _redirect(monkeypatch, SURROGATE_NAME, tmp_path / "real.txt")

    expected = (b"----- BEGIN FILE CONTEXT: caf\xe9.txt -----\n"
                b"body-bytes\n"
                b"----- END FILE CONTEXT -----\n")
    p = pack(tmp_path, [SURROGATE_NAME], {}, headroom=10_000)
    assert p.included == [SURROGATE_NAME]
    assert p.body == expected
    assert p.sha256 == hashlib.sha256(expected).hexdigest()

    # The headroom budget must cost the path the same one byte the marker
    # actually spends on it. If `_section_overhead` encoded the path any other
    # way, the budget and the emitted bytes would disagree and this boundary
    # (omit at exactly the section length, include one byte past it — see
    # `test_budget_reserves_a_newline_even_when_the_file_has_one`) would move.
    assert pack(tmp_path, [SURROGATE_NAME], {}, headroom=len(expected)).included == []
    tight = pack(tmp_path, [SURROGATE_NAME], {}, headroom=len(expected) + 1)
    assert tight.included == [SURROGATE_NAME]


def test_surrogate_path_from_a_gitio_style_status_stream(tmp_path):
    """End to end from the bytes git actually emits, not a hand-built string.

    This is the shape `gitio._tracked_statuses` produces: a NUL-delimited
    `--name-status -z` stream decoded with `surrogateescape`. It is the real
    route by which a surrogate reaches `pack()`.
    """
    raw = b"M\x00caf\xe9.txt\x00D\x00d\xffel.py\x00A\x00ok.txt\x00"
    toks = raw.decode("utf-8", "surrogateescape").split("\0")
    statuses: dict[str, str] = {}
    i = 0
    while i < len(toks) and toks[i]:
        statuses[toks[i + 1]] = toks[i][:1]
        i += 2
    files = list(statuses)
    assert files[0] == SURROGATE_NAME  # the decode really did produce one
    (tmp_path / "ok.txt").write_text("hello\n", encoding="utf-8")

    p = pack(tmp_path, files, statuses, headroom=10_000)
    assert p.included == []  # ok.txt is a small add -> already-in-diff
    assert p.omitted == [(SURROGATE_NAME, "missing"),
                         ("d\udcffel.py", "deleted"),
                         ("ok.txt", "already-in-diff")]
    assert p.body == (b"Context omitted for: caf\xe9.txt (missing), "
                      b"d\xffel.py (deleted), ok.txt (already-in-diff)\n")


def test_non_roundtrippable_surrogate_is_escaped_not_raised(tmp_path):
    # U+D800 cannot come from a `surrogateescape` decode (that only produces
    # U+DC80..U+DCFF), so it has no byte form to restore. It must still not
    # raise: the fallback renders it visibly instead.
    name = "bad\ud800.txt"
    p = pack(tmp_path, [name], {}, headroom=10_000)
    assert _reasons(p) == {name: "missing"}
    assert p.body == b"Context omitted for: bad\\ud800.txt (missing)\n"


# --------------------------------------------------------------------------
# Oracle parity
# --------------------------------------------------------------------------

def _build_fixture(root: Path) -> tuple[list[str], dict[str, str], str]:
    """A tree exercising every classification branch at once.

    Returns (paths, statuses, synthetic_diff_text). The oracle derives its
    statuses by parsing a unified diff, skodun takes them from
    `gitio.Diff.statuses`; the synthetic diff encodes the same three facts
    (added / added-large / deleted) in the oracle's own input language.
    """
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "big.py").write_text("x" * 5000, encoding="utf-8")
    (root / "mid.py").write_text("m" * 2000, encoding="utf-8")
    (root / "tie_a.txt").write_text("a" * 300, encoding="utf-8")
    (root / "tie_b.txt").write_text("b" * 300, encoding="utf-8")
    (root / "empty.txt").write_text("", encoding="utf-8")
    (root / "no_eol.txt").write_text("tail-without-newline", encoding="utf-8")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    # NUL-free but >30% control bytes: pins the ratio branch under parity,
    # which `bin.dat` alone cannot (its NUL decides before the ratio is read).
    (root / "ctrl.dat").write_bytes(b"\x01\x02\x03\x04" * 200 + b"abcdef" * 50)
    # Control bytes only at the very front: text under an 8 KiB sample, binary
    # under a smaller one. Pins the size of the peek window.
    (root / "ctrl_head.txt").write_bytes(b"\x01\x02\x03\x04" + b"a" * 9000 + b"\n")
    # Clean for the whole peek window, NUL after it: only the full-content
    # re-check on load can catch this one.
    (root / "late_nul.dat").write_bytes(b"a" * 9000 + b"\x00" + b"b" * 10)
    (root / "sub" / "nested.py").write_text("n" * 400, encoding="utf-8")
    (root / "new_small.py").write_text("tiny added file", encoding="utf-8")
    # Straddles the 16 KiB already-in-diff threshold from below: still
    # already-in-diff, and would flip if the threshold moved.
    (root / "new_mid.py").write_text("w" * 12_000, encoding="utf-8")
    (root / "new_big.py").write_text("z" * 20_000, encoding="utf-8")
    (root / "real.txt").write_text("secret", encoding="utf-8")
    if not (root / "link.txt").exists():
        os.symlink(root / "real.txt", root / "link.txt")
    if hasattr(os, "mkfifo") and not (root / "pipe.fifo").exists():
        os.mkfifo(root / "pipe.fifo")

    paths = [
        "big.py", "mid.py", "tie_b.txt", "tie_a.txt", "empty.txt",
        "no_eol.txt", "bin.dat", "ctrl.dat", "ctrl_head.txt", "late_nul.dat",
        "sub/nested.py", "new_small.py", "new_mid.py", "new_big.py",
        "gone.py", "link.txt", "pipe.fifo", "absent.py",
        "../etc/passwd", "/etc/passwd",
    ]
    if not hasattr(os, "mkfifo"):
        paths.remove("pipe.fifo")
    statuses = {"new_small.py": "A", "new_mid.py": "A", "new_big.py": "A",
                "gone.py": "D"}
    diff_text = (
        "diff --git a/new_small.py b/new_small.py\n"
        "new file mode 100644\n"
        "diff --git a/new_mid.py b/new_mid.py\n"
        "new file mode 100644\n"
        "diff --git a/new_big.py b/new_big.py\n"
        "new file mode 100644\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
    )
    return paths, statuses, diff_text


def _run_oracle(oracle: Path, worktree: Path, paths: list[str], diff_text: str,
                headroom: int, per_file_cap: int | None,
                tmp: Path) -> tuple[bytes, str, list[str], list[tuple[str, str]]]:
    """Drive `scripts/grok-context-pack.py` through its env interface."""
    script = oracle / ORACLE_SCRIPT
    assert script.is_file(), f"oracle script not found: {script}"
    lst = tmp / "files.txt"
    lst.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    dpath = tmp / "diff.txt"
    dpath.write_text(diff_text, encoding="utf-8")
    env = {
        **os.environ,
        "GR_WORKTREE": str(worktree),
        "GR_FILE_LIST": str(lst),
        "GR_DIFF_FILE": str(dpath),
        "GR_CONTEXT_SOURCE": "wt",
        "GR_CONTEXT_HEADROOM": str(headroom),
        # skodun always packs large adds (>=16KiB); that is the oracle's
        # opt-in branch, so the parity run opts in.
        "GR_CONTEXT_PACK_LARGE_ADDED": "1",
        "GROK_REVIEW_CONTEXT": "1",
    }
    env.pop("GROK_CONTEXT_FILE_BYTES", None)
    if per_file_cap is not None:
        env["GROK_CONTEXT_FILE_BYTES"] = str(per_file_cap)
    out = subprocess.run([sys.executable, str(script)], env=env, check=True,
                         capture_output=True, timeout=120).stdout
    parts = out.split(b"\n", 5)
    assert len(parts) == 6, f"oracle output unparseable: {out[:200]!r}"
    n, files_s, omit_s, h, meta, body = parts
    assert meta == b"----- BEGIN CONTEXT META END -----", meta
    assert int(n) == len(body), (int(n), len(body))
    included = json.loads(files_s) if files_s else []
    omitted = [(p, r) for p, r in (json.loads(omit_s) if omit_s else [])]
    return body, h.decode("ascii"), included, omitted


ORACLE_HEADROOMS = (0, 400, 1000, 5200, 6000, 30_000, 100_000)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
@pytest.mark.parametrize("headroom", ORACLE_HEADROOMS)
def test_oracle_parity(tmp_path, headroom):
    root = tmp_path / "wt"
    root.mkdir()
    paths, statuses, diff_text = _build_fixture(root)
    want_body, want_hash, want_included, want_omitted = _run_oracle(
        oracle_dir(), root, paths, diff_text, headroom, None, tmp_path)
    got = pack(root, paths, statuses, headroom=headroom)
    assert got.body == want_body
    assert got.sha256 == want_hash
    assert got.included == want_included
    assert got.omitted == want_omitted
    assert got.bytes_total == len(want_body)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_oracle_parity_with_per_file_cap(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    paths, statuses, diff_text = _build_fixture(root)
    want_body, want_hash, want_included, want_omitted = _run_oracle(
        oracle_dir(), root, paths, diff_text, 100_000, 1000, tmp_path)
    got = pack(root, paths, statuses, headroom=100_000, per_file_cap=1000)
    assert got.body == want_body
    assert got.sha256 == want_hash
    assert got.included == want_included
    assert got.omitted == want_omitted
    # Guard against a vacuous pass: the cap must actually have bitten.
    assert ("big.py", "over-file-cap") in got.omitted


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
@pytest.mark.parametrize("delta", [-1, 0, 1, 2])
def test_oracle_parity_at_the_exact_section_boundary(tmp_path, delta):
    """Parity for the off-by-one-sensitive `size + 1` fit test.

    A newline-terminated file at a headroom of exactly its section length is
    where the packer's conservative one-byte reservation is observable, so it
    is where the oracle and skodun would first disagree about it.
    """
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    exact = len(b"----- BEGIN FILE CONTEXT: a.txt -----\nhello\n"
                b"----- END FILE CONTEXT -----\n")
    headroom = exact + delta
    want_body, want_hash, want_included, want_omitted = _run_oracle(
        oracle_dir(), root, ["a.txt"], "", headroom, None, tmp_path)
    got = pack(root, ["a.txt"], {}, headroom=headroom)
    assert got.body == want_body
    assert got.sha256 == want_hash
    assert got.included == want_included
    assert got.omitted == want_omitted
    # Not vacuous: the boundary really does flip inclusion across this range.
    assert bool(got.included) == (delta >= 1)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_oracle_parity_cases_are_not_vacuous(tmp_path):
    """Every omission reason and both header outcomes really occur above."""
    root = tmp_path / "wt"
    root.mkdir()
    paths, statuses, _ = _build_fixture(root)
    seen: set[str] = set()
    bodies: set[bytes] = set()
    header_only = full = 0
    for headroom in ORACLE_HEADROOMS:
        p = pack(root, paths, statuses, headroom=headroom)
        seen.update(r for _, r in p.omitted)
        bodies.add(p.body)
        if p.body and not p.included:
            header_only += 1
        if not any(r == "over-headroom" for _, r in p.omitted) and p.included:
            full += 1
    # Every reason reachable from a working tree occurs (`over-file-cap` has
    # its own parity case; it needs a cap to trigger).
    assert seen == {"deleted", "binary", "already-in-diff", "missing",
                    "over-headroom"}
    # The headrooms span the three distinct shapes of the algorithm: an empty
    # body, a header-only body (everything dropped so the header fits), and a
    # run where every packable file is included.
    assert b"" in bodies
    assert header_only >= 1
    assert full >= 1
    # ...and they are not all the same partial pack.
    assert len(bodies) >= 5
