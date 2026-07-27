"""Tests for `skodun.promptbuild` — the exact bytes sent to the reviewer.

The instruction header is a byte-exact port of the oracle's `write_prompt`.
Two layers guard it:

* `GOLDEN_HEADER_OFF` / `GOLDEN_HEADER_ON` below are the oracle's own output,
  extracted mechanically (never retyped) and pinned here so the suite fails
  offline if the instruction text is reworded, reordered, or emptied.
* `test_prompt_parity_with_oracle` regenerates the oracle's prompt from the
  real `--write-prompt` seam and asserts full byte equality, so the goldens
  themselves cannot silently drift away from the oracle.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess

import pytest

from skodun.checklist import Selection
from skodun.promptbuild import Prompt, build
from tests.conftest import oracle_dir

SEL = Selection(sections=["core"], bytes_total=10, over_budget=False,
                dropped=[], body="## core\n- r\n")
EMPTY_SEL = Selection(sections=[], bytes_total=0, over_budget=False,
                      dropped=[], body="")

# --- GOLDEN BEGIN ---
_CONTRACT_LEN = 340

#: The oracle's instruction header, extracted from its own `--write-prompt`
#: output (see the task report for the exact command). Never retyped.
GOLDEN_HEADER_OFF = (
    b'You are a senior code reviewer reviewing a pull request BEFORE it is pushed.\n'
    b'Review ONLY the unified diff below. Report real, concrete problems:\n'
    b'bugs, security issues, broken error handling, concurrency hazards, data\n'
    b'loss, and clear regressions. Be precise and conservative -- do not invent\n'
    b'issues or flag pure style. Do NOT modify files or run commands.\n'
    b'Additionally check the diff against the repo rules below; cite the rule id\n'
    b'in the finding title when one is violated (e.g. "[no-blocking-handler] ...").\n'
    b'\n'
    b'Respond with ONLY a single JSON object (no prose, no markdown fences):\n'
    b'{"summary":"one-line overall assessment","findings":[{"file":"path","line":0,"severity":"high|medium|low","category":"bug|security|perf|correctness|other","title":"short title","detail":"why it matters"}]}\n'
    b'If there are no real issues, return an empty findings array.\n'
    b'\n'
)

GOLDEN_CONTEXT_BLOCK = (
    b'When FILE CONTEXT sections are present after the diff, treat them as\n'
    b'read-only reference for resolving declarations and references in the\n'
    b'changed files. Findings must still anchor to changed lines in the DIFF.\n'
    b'If a referenced symbol is not visible in the diff or the file context,\n'
    b'do NOT assume it is missing or wrong.\n'
)

GOLDEN_HEADER_ON = (
    GOLDEN_HEADER_OFF[:len(GOLDEN_HEADER_OFF) - _CONTRACT_LEN]
    + GOLDEN_CONTEXT_BLOCK
    + GOLDEN_HEADER_OFF[len(GOLDEN_HEADER_OFF) - _CONTRACT_LEN:]
)
# --- GOLDEN END ---


# --------------------------------------------------------------------------
# Instruction text: byte-exact, and not vacuously present
# --------------------------------------------------------------------------

def test_instruction_header_is_oracle_text_verbatim_packing_off():
    p = build("b", "origin/main", "s" * 40, "h" * 40, b"d", 400_000, SEL, None)
    assert p.text.startswith(GOLDEN_HEADER_OFF)
    # The header must be substantial — an emptied or stubbed instruction block
    # would still satisfy a naive `startswith(b"")`.
    assert len(GOLDEN_HEADER_OFF) > 600
    assert p.text[len(GOLDEN_HEADER_OFF):].startswith(b"Branch: b\n")


def test_instruction_header_is_oracle_text_verbatim_packing_on():
    p = build("b", "origin/main", "s" * 40, "h" * 40, b"d", 400_000, SEL, b"CTX\n")
    assert p.text.startswith(GOLDEN_HEADER_ON)
    assert len(GOLDEN_HEADER_ON) > len(GOLDEN_HEADER_OFF)
    assert p.text[len(GOLDEN_HEADER_ON):].startswith(b"Branch: b\n")


def test_context_instruction_block_only_when_pack_body_is_not_none():
    marker = b"When FILE CONTEXT sections are present after the diff"
    off = build("b", "o/m", "s", "h", b"d", 400_000, SEL, None).text
    on = build("b", "o/m", "s", "h", b"d", 400_000, SEL, b"CTX\n").text
    assert marker not in off
    assert marker in on
    # An empty (but attempted) pack still carries the instructions, exactly as
    # the oracle does: `_ctx_on` tracks "packing enabled", while the FILE
    # CONTEXT sections themselves are gated on a non-empty body.
    empty = build("b", "o/m", "s", "h", b"d", 400_000, SEL, b"").text
    assert marker in empty
    assert empty.rstrip(b"\n").endswith(b"----- END DIFF -----")


def _only_difference(off: bytes, on: bytes) -> bytes:
    """The contiguous run present in `on` and absent from `off`."""
    n = 0
    while n < len(off) and off[n] == on[n]:
        n += 1
    delta = len(on) - len(off)
    extra = on[n:n + delta]
    assert on[:n] + on[n + delta:] == off, "difference is not one contiguous run"
    return extra


def test_context_instruction_block_is_the_oracles_five_lines():
    off = build("b", "o/m", "s", "h", b"d", 400_000, SEL, None).text
    on = build("b", "o/m", "s", "h", b"d", 400_000, SEL, b"").text
    assert _only_difference(off, on) == GOLDEN_CONTEXT_BLOCK
    assert _only_difference(GOLDEN_HEADER_OFF, GOLDEN_HEADER_ON) \
        == GOLDEN_CONTEXT_BLOCK
    assert GOLDEN_CONTEXT_BLOCK.count(b"\n") == 5
    assert GOLDEN_CONTEXT_BLOCK.endswith(b"\n")


def test_context_block_sits_between_reviewer_intro_and_json_contract():
    on = build("b", "o/m", "s", "h", b"d", 400_000, SEL, b"CTX\n").text
    i_intro = on.index(b"You are a senior code reviewer")
    i_ctx = on.index(b"When FILE CONTEXT sections are present")
    i_json = on.index(b"Respond with ONLY a single JSON object")
    assert i_intro < i_ctx < i_json


def test_embedded_json_example_is_valid_json():
    text = build("b", "o/m", "s", "h", b"d", 400_000, SEL, None).text
    lines = [ln for ln in text.split(b"\n") if ln.startswith(b'{"summary"')]
    assert len(lines) == 1, "exactly one JSON contract example expected"
    obj = json.loads(lines[0].decode("utf-8"))
    assert set(obj) == {"summary", "findings"}
    assert isinstance(obj["summary"], str) and obj["summary"]
    assert isinstance(obj["findings"], list) and len(obj["findings"]) == 1
    assert set(obj["findings"][0]) == {
        "file", "line", "severity", "category", "title", "detail",
    }
    assert obj["findings"][0]["line"] == 0


# --------------------------------------------------------------------------
# Layout and ordering
# --------------------------------------------------------------------------

def test_layout_and_truncation():
    diff = b"diff --git a/a b/a\n" + b"x" * 100
    p = build("feat", "origin/main", "s" * 40, "h" * 40, diff,
              max_diff_bytes=50, selection=SEL, pack_body=b"CTX")
    t = p.text
    assert p.diff_truncated is True
    assert b"----- DIFF TRUNCATED at 50 bytes -----" in t
    assert t.index(b"BEGIN REPO RULES") < t.index(b"BEGIN DIFF")
    assert t.index(b"END DIFF") < t.index(b"CTX")


def test_section_ordering_is_head_rules_diff_context():
    p = build("feat", "origin/main", "s" * 40, "h" * 40, b"dd", 400_000,
              SEL, b"----- BEGIN FILE CONTEXT: a -----\n")
    t = p.text
    assert (t.index(b"Branch: feat")
            < t.index(b"----- BEGIN REPO RULES (path-scoped) -----")
            < t.index(b"----- END REPO RULES -----")
            < t.index(b"----- BEGIN DIFF -----")
            < t.index(b"----- END DIFF -----")
            < t.index(b"----- BEGIN FILE CONTEXT: a -----"))


def test_branch_base_head_block_format():
    p = build("feat/x", "origin/main", "a" * 40, "b" * 40, b"d", 400_000,
              SEL, None)
    assert (b"Branch: feat/x\n"
            b"Base:   origin/main (" + b"a" * 40 + b")\n"
            b"Head:   " + b"b" * 40 + b"\n") in p.text


def test_head_may_carry_the_now_mode_working_tree_label():
    head = "c" * 40 + " (working tree)"
    p = build("feat", "origin/main", "a" * 40, head, b"d", 400_000, SEL, None)
    assert b"Head:   " + head.encode("utf-8") + b"\n" in p.text


def test_repo_rules_section_absent_when_selection_body_empty():
    p = build("b", "o/m", "s", "h", b"d", 400_000, EMPTY_SEL, None)
    assert b"REPO RULES" not in p.text
    assert b"Head:   h\n\n----- BEGIN DIFF -----\n" in p.text


def test_repo_rules_section_absent_when_selection_is_none():
    p = build("b", "o/m", "s", "h", b"d", 400_000, None, None)
    assert b"REPO RULES" not in p.text


def test_repo_rules_body_trailing_newlines_normalised_to_one():
    """The oracle captures the body through `$(...)` (strips ALL trailing
    newlines) then re-adds exactly one via `printf '%s\\n'`."""
    sel = Selection(sections=["core"], bytes_total=4, over_budget=False,
                    dropped=[], body="## core\n\n\n\n")
    p = build("b", "o/m", "s", "h", b"d", 400_000, sel, None)
    assert (b"----- BEGIN REPO RULES (path-scoped) -----\n"
            b"## core\n"
            b"----- END REPO RULES -----\n") in p.text


def test_repo_rules_section_absent_when_body_is_only_newlines():
    sel = Selection(sections=["core"], bytes_total=0, over_budget=False,
                    dropped=[], body="\n\n")
    p = build("b", "o/m", "s", "h", b"d", 400_000, sel, None)
    assert b"REPO RULES" not in p.text


def test_repo_rules_body_appears_verbatim():
    p = build("b", "o/m", "s", "h", b"d", 400_000, SEL, None)
    assert b"----- BEGIN REPO RULES (path-scoped) -----\n## core\n- r\n" \
           b"----- END REPO RULES -----\n" in p.text


# --------------------------------------------------------------------------
# Diff body and truncation
# --------------------------------------------------------------------------

def test_no_truncation_when_within_budget():
    p = build("b", "origin/main", "s" * 40, "h" * 40, b"small", 400_000, SEL,
              None)
    assert p.diff_truncated is False and b"TRUNCATED" not in p.text


def test_truncation_fires_exactly_at_the_budget_boundary():
    diff = b"y" * 100
    at = build("b", "o/m", "s", "h", diff, 100, SEL, None)
    assert at.diff_truncated is False
    assert b"TRUNCATED" not in at.text
    assert b"----- BEGIN DIFF -----\n" + diff + b"\n----- END DIFF -----\n" \
        in at.text

    over = build("b", "o/m", "s", "h", diff, 99, SEL, None)
    assert over.diff_truncated is True
    assert b"----- BEGIN DIFF -----\n" + b"y" * 99 + \
        b"\n----- DIFF TRUNCATED at 99 bytes -----\n\n----- END DIFF -----\n" \
        in over.text


def test_truncation_marker_text_is_exact():
    p = build("b", "o/m", "s", "h", b"z" * 10, 4, SEL, None)
    assert b"\n----- DIFF TRUNCATED at 4 bytes -----\n" in p.text
    # Not "4 bytes-----", not "DIFF TRUNCATED AT", not a byte count of the
    # written prefix plus framing.
    assert b"----- DIFF TRUNCATED at 10 bytes -----" not in p.text


def test_diff_is_kept_byte_for_byte_including_non_utf8():
    diff = b"diff --git a/x b/x\n+\xff\xfe\x00binary-ish\n"
    p = build("b", "o/m", "s", "h", diff, 400_000, SEL, None)
    assert b"----- BEGIN DIFF -----\n" + diff + b"\n----- END DIFF -----\n" \
        in p.text
    assert p.diff_truncated is False


def test_blank_line_always_precedes_end_diff_marker():
    """Oracle file-form emits an unconditional `echo ""` so END DIFF starts on
    its own line even when the diff lacks a trailing newline."""
    no_nl = build("b", "o/m", "s", "h", b"abc", 400_000, SEL, None).text
    assert b"\nabc\n----- END DIFF -----\n" in no_nl
    with_nl = build("b", "o/m", "s", "h", b"abc\n", 400_000, SEL, None).text
    assert b"\nabc\n\n----- END DIFF -----\n" in with_nl


def test_empty_diff_still_produces_well_formed_markers():
    p = build("b", "o/m", "s", "h", b"", 400_000, SEL, None)
    assert b"----- BEGIN DIFF -----\n\n----- END DIFF -----\n" in p.text
    assert p.diff_truncated is False


def test_non_positive_max_diff_bytes_rejected():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            build("b", "o/m", "s", "h", b"d", bad, SEL, None)


# --------------------------------------------------------------------------
# Context sections
# --------------------------------------------------------------------------

def test_context_body_appended_after_blank_line():
    body = b"----- BEGIN FILE CONTEXT: a.py -----\nx = 1\n" \
           b"----- END FILE CONTEXT -----\n"
    p = build("b", "o/m", "s", "h", b"d", 400_000, SEL, body)
    assert p.text.endswith(b"----- END DIFF -----\n\n" + body)


def test_empty_pack_body_appends_nothing():
    p = build("b", "o/m", "s", "h", b"d", 400_000, SEL, b"")
    assert p.text.endswith(b"----- END DIFF -----\n")


# --------------------------------------------------------------------------
# Prompt accounting
# --------------------------------------------------------------------------

def test_prompt_bytes_equals_len_text():
    for pack in (None, b"", b"CTX\n"):
        for diff, cap in ((b"d" * 10, 400_000), (b"d" * 10, 3)):
            p = build("b", "o/m", "s", "h", diff, cap, SEL, pack)
            assert p.prompt_bytes == len(p.text)
            assert p.prompt_bytes > 0


def test_prompt_is_a_frozen_dataclass_of_the_declared_shape():
    p = build("b", "o/m", "s", "h", b"d", 400_000, SEL, None)
    assert isinstance(p, Prompt)
    assert isinstance(p.text, bytes)
    assert isinstance(p.diff_truncated, bool)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.text = b"nope"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Parity with the oracle
# --------------------------------------------------------------------------

ORACLE_SCRIPT = "scripts/grok-prepush-review.sh"


def _run_oracle(root, out, diff_file, *, branch, base_ref, base_sha, head,
                context, max_diff_bytes, file_list=None):
    """Drive the oracle's `--write-prompt` seam and return the prompt bytes.

    Seam contract (oracle source, just below `write_prompt`):
        --write-prompt PROMPT BRANCH BASE_REF BASE_SHA HEAD [DIFF_FILE]
    With DIFF_FILE the diff is streamed byte-for-byte (the form skodun ports);
    without it the diff is read from stdin as a shell string. Context meta is
    printed as one JSON line on stdout.
    """
    env = dict(os.environ)
    env["GROK_REVIEW_CONTEXT"] = "1" if context else "0"
    env["GROK_MAX_DIFF_BYTES"] = str(max_diff_bytes)
    env["GR_WORKTREE"] = str(root)
    env["GR_FILE_LIST"] = str(file_list or "")
    env["GR_CONTEXT_SOURCE"] = "wt"
    subprocess.run(
        ["sh", ORACLE_SCRIPT, "--write-prompt", str(out), branch, base_ref,
         base_sha, head, str(diff_file)],
        cwd=str(root), env=env, check=True, capture_output=True,
    )
    return out.read_bytes()


def _split_oracle_prompt(raw):
    """Recover (rules_body, pack_body) from an oracle-generated prompt.

    Both come from oracle-side subsystems ported in other tasks (checklist
    selection, context packing); feeding the oracle's own output back into
    `build` isolates this task's concern — prompt assembly — while still
    asserting the full prompt byte-for-byte.
    """
    rules = ""
    begin = b"----- BEGIN REPO RULES (path-scoped) -----\n"
    end = b"----- END REPO RULES -----\n"
    if begin in raw:
        i = raw.index(begin) + len(begin)
        j = raw.index(end, i)
        rules = raw[i:j].decode("utf-8")
    tail = raw.split(b"----- END DIFF -----\n", 1)[1]
    pack = tail[1:] if tail.startswith(b"\n") else tail
    return rules, pack


@pytest.mark.parametrize("context,max_diff_bytes", [
    (False, 400_000),
    (True, 400_000),
    (False, 60),
])
def test_prompt_parity_with_oracle(tmp_path, context, max_diff_bytes):
    root = oracle_dir()
    if root is None or not (root / ORACLE_SCRIPT).is_file():
        pytest.skip("SKODUN_ORACLE_DIR unset or oracle script absent")

    diff = (b"diff --git a/noop.js b/noop.js\n"
            b"--- a/noop.js\n+++ b/noop.js\n@@ -1 +1 @@\n-x\n+y\n")
    diff_file = tmp_path / "fix.diff"
    diff_file.write_bytes(diff)
    file_list = tmp_path / "files.txt"
    file_list.write_text("noop.js\n", encoding="utf-8")

    branch, base_ref = "feat/parity", "origin/main"
    base_sha, head = "a" * 40, "b" * 40 + " (working tree)"

    raw = _run_oracle(root, tmp_path / "oracle.txt", diff_file,
                      branch=branch, base_ref=base_ref, base_sha=base_sha,
                      head=head, context=context,
                      max_diff_bytes=max_diff_bytes, file_list=file_list)
    rules, pack = _split_oracle_prompt(raw)
    sel = Selection(sections=["core"], bytes_total=len(rules.encode("utf-8")),
                    over_budget=False, dropped=[], body=rules)

    mine = build(branch, base_ref, base_sha, head, diff, max_diff_bytes,
                 sel, pack if context else None).text

    # Required assertion: the instruction header (everything above the
    # branch/base/head block) matches byte-for-byte.
    cut = raw.index(b"Branch: ")
    assert mine[:cut] == raw[:cut]
    assert len(raw[:cut]) > 600  # never vacuously equal
    # Stronger: the whole prompt matches byte-for-byte.
    assert mine == raw


def test_parity_fixture_exercises_the_paths_it_claims_to(tmp_path):
    """Guard the parity test itself: the oracle prompt for the packing-on and
    truncating variants must actually contain a context section / a truncation
    marker, otherwise those parametrisations would assert nothing extra."""
    root = oracle_dir()
    if root is None or not (root / ORACLE_SCRIPT).is_file():
        pytest.skip("SKODUN_ORACLE_DIR unset or oracle script absent")

    diff = (b"diff --git a/noop.js b/noop.js\n"
            b"--- a/noop.js\n+++ b/noop.js\n@@ -1 +1 @@\n-x\n+y\n")
    diff_file = tmp_path / "fix.diff"
    diff_file.write_bytes(diff)
    file_list = tmp_path / "files.txt"
    file_list.write_text("noop.js\n", encoding="utf-8")
    kw = dict(branch="b", base_ref="origin/main", base_sha="a" * 40, head="h")

    on = _run_oracle(root, tmp_path / "on.txt", diff_file, context=True,
                     max_diff_bytes=400_000, file_list=file_list, **kw)
    assert b"----- BEGIN FILE CONTEXT: noop.js -----" in on

    trunc = _run_oracle(root, tmp_path / "t.txt", diff_file, context=False,
                        max_diff_bytes=60, file_list=file_list, **kw)
    assert b"----- DIFF TRUNCATED at 60 bytes -----" in trunc
