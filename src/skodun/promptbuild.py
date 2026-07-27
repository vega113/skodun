"""Assemble the review prompt — the exact bytes handed to the model.

PARITY-CRITICAL. The instruction text is a byte-for-byte port of the oracle's
`write_prompt` (`scripts/grok-prepush-review.sh`). It was lifted from the
oracle's own `--write-prompt` output and emitted as the literals below by a
script; not one character was retyped. Reword it and every review silently gets
worse — the JSON example in particular is a schema the model is asked to match,
so a stray comma there costs more than a whole class of bugs here.
`tests/test_promptbuild.py` pins both an offline golden copy and full byte
parity against the live oracle.

Layout
------
1. reviewer instructions (plus, only when context packing is on, five extra
   lines about how to read the FILE CONTEXT sections);
2. the JSON response contract;
3. the branch/base/head block;
4. `----- BEGIN REPO RULES (path-scoped) -----` … `----- END REPO RULES -----`,
   omitted entirely when the checklist selection contributed no body;
5. `----- BEGIN DIFF -----` … `----- END DIFF -----`, with the diff capped at
   `max_diff_bytes` and a `----- DIFF TRUNCATED at N bytes -----` marker when
   the cap bit;
6. the packed FILE CONTEXT sections, when there are any.

Everything is `bytes`. Diffs are not decodable in general (they carry whatever
the files carry), so they are spliced in verbatim rather than round-tripped
through `str`.

Ported quirks
-------------
Two behaviours look like accidents of shell but are load-bearing for parity,
and the oracle wins where it and intuition disagree:

* **A blank line always precedes `----- END DIFF -----`.** The oracle's
  file-form diff path emits an unconditional `echo ""` there so that the marker
  starts on its own line even when the diff has no trailing newline. Real
  diffs do end in a newline, so in practice this shows up as a blank line.
  (This module ports the *file* form — `head -c "$MAX_DIFF_BYTES" "$_diff_file"`
  — which is the path the oracle uses whenever the diff arrives as a file, and
  the only one that preserves diff bytes exactly. The oracle's older string
  form differs: shell command substitution strips the diff's trailing newlines
  before it is ever written, so that form caps `diff + "\\n"` rather than the
  raw diff.)

* **Repo-rules body: all trailing newlines collapse to exactly one.** The
  oracle captures the checklist body through `$(...)`, which strips every
  trailing newline, then re-adds one with `printf '%s\\n'`. A body that is
  nothing but newlines therefore reads as empty and the whole section is
  dropped.

The head label is whatever the caller passes: `--now` mode passes
`"<oid> (working tree)"`, matching the oracle's own call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from skodun.checklist import Selection

# --- ORACLE TEXT BEGIN ---
#: Reviewer instructions, verbatim from the oracle's `write_prompt`.
_INTRO = (
    b'You are a senior code reviewer reviewing a pull request BEFORE it is pushed.\n'
    b'Review ONLY the unified diff below. Report real, concrete problems:\n'
    b'bugs, security issues, broken error handling, concurrency hazards, data\n'
    b'loss, and clear regressions. Be precise and conservative -- do not invent\n'
    b'issues or flag pure style. Do NOT modify files or run commands.\n'
    b'Additionally check the diff against the repo rules below; cite the rule id\n'
    b'in the finding title when one is violated (e.g. "[no-blocking-handler] ...").\n'
)

#: Emitted only when context packing is on (oracle: `_ctx_on = 1`).
_CONTEXT_INSTRUCTIONS = (
    b'When FILE CONTEXT sections are present after the diff, treat them as\n'
    b'read-only reference for resolving declarations and references in the\n'
    b'changed files. Findings must still anchor to changed lines in the DIFF.\n'
    b'If a referenced symbol is not visible in the diff or the file context,\n'
    b'do NOT assume it is missing or wrong.\n'
)

#: The JSON response contract, including the oracle's own example.
_RESPONSE_CONTRACT = (
    b'\n'
    b'Respond with ONLY a single JSON object (no prose, no markdown fences):\n'
    b'{"summary":"one-line overall assessment","findings":[{"file":"path","line":0,"severity":"high|medium|low","category":"bug|security|perf|correctness|other","title":"short title","detail":"why it matters"}]}\n'
    b'If there are no real issues, return an empty findings array.\n'
    b'\n'
)
# --- ORACLE TEXT END ---

RULES_BEGIN = b"----- BEGIN REPO RULES (path-scoped) -----\n"
RULES_END = b"----- END REPO RULES -----\n"
DIFF_BEGIN = b"----- BEGIN DIFF -----\n"
DIFF_END = b"----- END DIFF -----\n"
#: `n` is the *budget*, not the number of diff bytes actually written.
DIFF_TRUNCATED = "----- DIFF TRUNCATED at {n} bytes -----\n"


@dataclass(frozen=True)
class Prompt:
    """The rendered prompt plus the two facts callers need about it.

    `diff_truncated` feeds the trust invariant directly — a truncated diff can
    never back a trustworthy review, because the model did not see the whole
    change. `prompt_bytes` is `len(text)` and exists so callers can record and
    budget the prompt size without re-measuring.
    """

    text: bytes
    diff_truncated: bool
    prompt_bytes: int


def build(
    branch: str,
    base_ref: str,
    base_sha: str,
    head: str,
    diff: bytes,
    max_diff_bytes: int,
    selection: Selection | None,
    pack_body: bytes | None,
) -> Prompt:
    """Render the review prompt.

    `selection` may be None, or carry an empty body, to mean "no path-scoped
    rules" — the section is then omitted entirely rather than emitted empty.

    `pack_body` distinguishes three states, mirroring the oracle exactly:

    * `None` — context packing is off. No FILE CONTEXT instructions, no
      sections.
    * `b""` — packing was attempted but produced nothing (no eligible files, or
      the packer failed soft). The instructions are still emitted, because the
      oracle gates them on packing being *enabled*, not on the pack being
      non-empty; no sections are appended.
    * non-empty — instructions and sections both.

    Raises `ValueError` for a non-positive `max_diff_bytes`: a zero budget would
    silently ship a prompt containing no diff at all, which reads to the model
    as "nothing changed" rather than as an error.
    """
    if max_diff_bytes < 1:
        raise ValueError(f"max_diff_bytes must be >= 1, got {max_diff_bytes}")

    out = bytearray()
    out += _INTRO
    if pack_body is not None:
        out += _CONTEXT_INSTRUCTIONS
    out += _RESPONSE_CONTRACT

    out += f"Branch: {branch}\n".encode("utf-8")
    out += f"Base:   {base_ref} ({base_sha})\n".encode("utf-8")
    out += f"Head:   {head}\n".encode("utf-8")

    # Oracle: `$(...)` capture strips every trailing newline, `printf '%s\n'`
    # re-adds exactly one, and `[ -n "$_cl_body" ]` tests the stripped value.
    rules = selection.body.rstrip("\n") if selection is not None else ""
    if rules:
        out += b"\n" + RULES_BEGIN + rules.encode("utf-8") + b"\n" + RULES_END

    out += b"\n" + DIFF_BEGIN
    diff_truncated = len(diff) > max_diff_bytes
    out += diff[:max_diff_bytes]
    if diff_truncated:
        out += b"\n" + DIFF_TRUNCATED.format(n=max_diff_bytes).encode("utf-8")
    out += b"\n"  # unconditional; see "Ported quirks" above
    out += DIFF_END

    if pack_body:
        out += b"\n" + pack_body

    text = bytes(out)
    return Prompt(text=text, diff_truncated=diff_truncated,
                  prompt_bytes=len(text))
