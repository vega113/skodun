"""The grok adapter: build the invocation, parse the envelope, judge the run.

Ported from the oracle's grok invocation (`run_grok_attempt`), its envelope
extractor (`_extract_review` / `_first_json_object`) and `detect_degraded`.

Two of the three jobs here are about *trust*, not convenience:

* `parse_ok` says the output is a review this program understands. It is
  deliberately STRICTER than the oracle, which accepted any dict with a list
  `findings` and merely filtered out non-dict items. That laxity mints a
  `parse_ok=True` record whose findings the triage module cannot key — a
  "trustworthy" record with nothing in it, which strands the gate at exit 2
  with no way to progress. Here a single malformed item fails the whole
  payload, so the run is retried instead of being believed.
* `degraded` says the harness misbehaved even though the output looks fine.
  It is **positive evidence only**: absence of a signal is never taken as
  proof of health, and no signal is inferred from a *lack* of something.

`parse_ok` and `degraded` are independent axes. A run can parse perfectly and
still be degraded (that is precisely the silent-false-all-clear this guards:
in the oracle's corpus, 116 `Cancelled` runs recorded clean with zero
findings), and the trust invariant requires both to be right.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Defaults, Reviewer

# Verbatim from the oracle's `GROK_REVIEW_SCHEMA` (pinned byte-for-byte by
# `test_schema_matches_oracle_verbatim`). Single line: it is one argv element.
SCHEMA = (
    '{"type":"object","properties":{"summary":{"type":"string"},"findings":'
    '{"type":"array","items":{"type":"object","properties":{"file":{"type":'
    '"string"},"line":{"type":"integer"},"severity":{"type":"string","enum":'
    '["high","medium","low"]},"category":{"type":"string"},"title":{"type":'
    '"string"},"detail":{"type":"string"}},"required":["file","severity",'
    '"title","detail"]}}},"required":["summary","findings"]}'
)

# stderr tells of a harness-side failure. Matched case-insensitively: the
# oracle greps these with `grep -Eiq`, so `Tool_Error` must not slip through.
_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"tool_error",
    b"execution_failure",
    b"dropped the response channel",
    b"harness-side bug",
    b"harness side bug",  # the oracle's regex is `harness[- ]side bug`
)

# grok's tool-call control token (U+2581 LOWER ONE EIGHTH BLOCK). It never
# appears in a well-formed review, so its presence in stdout means the answer
# was cut off mid tool-use. Matched on raw UTF-8 BYTES, exactly as the oracle's
# `LC_ALL=C grep -F` does, so output containing invalid multibyte sequences
# still matches instead of blowing up a decoder.
_LEAKED_TOKEN = "tool▁call".encode("utf-8")

# stderr tell of turn-limit exhaustion. stderr ONLY, deliberately: a review
# whose FINDINGS discuss turn limits would trip a stdout substring search. In
# the oracle's 4020-run corpus this string appears in 151 stderr files and zero
# stdout files.
_MAX_TURNS = b"max turns reached"

# The only stopReason value that means "completed normally". An ALLOWLIST is
# deliberate: a false positive costs one re-review, a false negative is the
# silent false all-clear this exists to prevent.
_STOP_REASON_OK = "EndTurn"

_SEVERITIES = frozenset({"high", "medium", "low"})

# Models that reject `--effort`. Prefix match, so `grok-build-fast-1` and any
# future `grok-build*` build are covered without another table edit.
_NO_EFFORT_PREFIXES = ("grok-build",)

# `effort` values that mean "do not pass the flag at all". `None` is unset;
# `"none"` is the user's explicit opt-out. Those are the only two: `config`
# rejects every other spelling (`""` included — it is not in `config.EFFORTS`)
# before a `Reviewer` can reach this module, so an empty-string branch here
# would be dead code that quietly implies a third, undocumented opt-out.
_EFFORT_OFF = (None, "none")


@dataclass(frozen=True)
class ParseResult:
    """What one attempt's output is worth.

    `findings`/`summary` are only populated when `parse_ok` — a payload that
    failed validation must not leak half-shaped findings to a caller that
    checked the wrong flag.
    """

    parse_ok: bool
    findings: list
    summary: str
    stop_reason: str | None
    degraded: bool
    degraded_reason: str


def resolve_grok_bin() -> str:
    """`SKODUN_GROK_BIN` -> `~/.grok/bin/grok` if executable -> `grok` on PATH.

    There is deliberately no legacy `-p` re-shell fallback (global constraint):
    a run with empty stdout is a failed attempt and is retried as a fresh
    session, never re-invoked with the prompt as a CLI argument.
    """
    override = os.environ.get("SKODUN_GROK_BIN")
    if override:
        return override
    default = Path.home() / ".grok" / "bin" / "grok"
    return str(default) if os.access(default, os.X_OK) else "grok"


class GrokAdapter:
    """Invocation + output interpretation for the xAI grok CLI."""

    name = "grok"

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path) -> list[str]:
        """The full argv for one review attempt.

        The prompt travels as a FILE, never as a shell-interpolated string, and
        the model is always explicit (`-m`) rather than inherited from the
        binary's own settings file.
        """
        effort = None if r.effort in _EFFORT_OFF else r.effort
        if effort is not None and r.model.startswith(_NO_EFFORT_PREFIXES):
            raise ValueError(
                f"model {r.model!r} does not support effort (configured "
                f"{r.effort!r}) — remove it, set effort = \"none\", or "
                f"choose a model that supports it")
        cmd = [
            resolve_grok_bin(),
            "--prompt-file", str(prompt_file),
            "--json-schema", SCHEMA,
            "-m", r.model,
            "--disable-web-search",
            "--no-subagents",
            "--no-memory",
            "--no-plan",
            "--max-turns", str(d.max_turns),
            "--verbatim",
            "--disallowed-tools", d.deny_tools,
            "--cwd", str(cwd),
        ]
        if effort is not None:
            cmd += ["--effort", effort]
        return cmd

    def parse(self, stdout: bytes, stderr: bytes) -> ParseResult:
        payload, stop_reason = _extract_review(stdout)
        parse_ok = _valid_payload(payload)
        degraded, reason = _detect_degraded(stdout, stderr, stop_reason)
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if parse_ok else [],
            summary=payload["summary"] if parse_ok else "",
            stop_reason=stop_reason,
            degraded=degraded,
            degraded_reason=reason,
        )


# --------------------------------------------------------------------------
# envelope extraction
# --------------------------------------------------------------------------


def _eligible(obj: object) -> bool:
    """The ONE candidate predicate, applied identically at all three levels.

    A candidate must carry `summary` or `findings`. Two failure modes hang off
    this single rule:

    * An empty or hollow `structuredOutput` (`{}`) is NOT eligible, so it falls
      through instead of masking a perfectly good payload sitting in `text`.
    * An individual *finding* object is not eligible, so a raw scan over a
      truncated envelope does not lock onto the first element of the findings
      array and record `parse_ok` with no real content.
    """
    return isinstance(obj, dict) and ("summary" in obj or "findings" in obj)


def _first_review_object(text: str) -> dict | None:
    """First eligible top-level JSON object in `text`, or None.

    `raw_decode` from each `{` rather than `json.loads` on the whole blob:
    grok wraps its answer in prose or ```json fences and sometimes emits the
    object twice, all of which make a bare `loads` die with "Extra data" and
    lose the review entirely.
    """
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if _eligible(obj):
            return obj
        pos = text.find("{", pos + 1)
    return None


def _root_envelope(text: str) -> object | None:
    """The envelope ROOT value, or None if `text` does not open with one.

    `raw_decode` rather than `json.loads`, and this is a trust fix rather than
    a tidy-up: `loads` raises "Extra data" on ANY trailing byte, while the
    level-3 scan below is deliberately built to survive exactly that (grok
    wraps its answer in prose and *sometimes emits the final object twice*).
    With `loads` here the two disagreed, and the disagreement was silent and
    one-directional: stdout of a good envelope carrying `"stopReason":
    "Cancelled"` plus one trailing line parsed fine at level 3 but produced NO
    root, so the stopReason check was skipped and the run recorded
    `parse_ok=True, degraded=False` — the exact combination that certifies a
    review, on a run the model cancelled.

    Deliberate divergence from the oracle, in the FAIL-SAFE direction: the
    oracle's `grok_stop_reason` uses `json.load` and goes silent on the same
    input, so skodun now flags degraded where the oracle stayed quiet. A false
    positive costs one re-review; a false negative is a silent false all-clear.
    Pinned by `test_oracle_misses_stop_reason_after_trailing_data`.
    """
    # `raw_decode` does not skip leading whitespace (`loads` does); strip it so
    # a pretty-printed envelope is still recognised as an envelope.
    stripped = text.lstrip()
    if not stripped:
        return None
    try:
        root, _ = json.JSONDecoder().raw_decode(stripped, 0)
    except ValueError:
        return None
    return root


def _root_stop_reason(root: object) -> str | None:
    """Root `stopReason`, read as a STRING or not at all.

    Matches the oracle's `grok_stop_reason`, which prints the value only when
    `isinstance(v, str)` and whose caller then treats an empty result as
    absent. A non-string or empty `stopReason` yields no signal — the same
    "absent/unparseable means no signal" rule that keeps plain-output builds
    and prose answers clean.
    """
    if not isinstance(root, dict):
        return None
    value = root.get("stopReason")
    return value if isinstance(value, str) and value else None


def _extract_review(stdout: bytes) -> tuple[dict | None, str | None]:
    """Three-level fallback: `structuredOutput` -> `text` -> raw scan.

    `errors="replace"` rather than a strict decode: grok's output routinely
    carries non-ASCII, and in exactly the truncated runs that matter it carries
    invalid multibyte sequences. A `UnicodeDecodeError` here would skip the
    stopReason check on the very runs it exists to catch.

    That is the second deliberate divergence from the oracle, again in the
    FAIL-SAFE direction: the oracle's `grok_stop_reason` opens the file with a
    strict `encoding="utf-8"` and lets `except Exception` swallow the
    `UnicodeDecodeError`, so an envelope carrying one bad byte alongside
    `"stopReason": "Cancelled"` yields no signal there and a `degraded` flag
    here. Pinned by `test_oracle_misses_stop_reason_in_invalid_utf8_envelope`.
    """
    text = stdout.decode("utf-8", "replace")
    root = _root_envelope(text)

    stop_reason = _root_stop_reason(root)

    # The envelope is detected by its OWN keys (structuredOutput/text), not by
    # the absence of summary/findings at the root — so a review that happens to
    # echo those keys at the envelope level cannot divert us away from the
    # authoritative structuredOutput.
    if isinstance(root, dict):
        so = root.get("structuredOutput")
        if _eligible(so):
            return so, stop_reason
        inner = root.get("text")
        if isinstance(inner, str) and inner.strip():
            found = _first_review_object(inner)
            if found is not None:
                return found, stop_reason

    return _first_review_object(text), stop_reason


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------


def _valid_payload(obj: object) -> bool:
    """True iff `obj` is a review this program can act on without guessing."""
    # `_eligible` already implies `isinstance(obj, dict)`, but this is the
    # trust-critical validator: the narrowing is spelled as a real check rather
    # than an `assert`, which `python -O` strips. Under -O a bare assert would
    # leave `obj.get` unguarded and turn a hostile payload into an
    # AttributeError inside the gate path instead of a clean `parse_ok=False`.
    if not isinstance(obj, dict) or not _eligible(obj):
        return False
    if not isinstance(obj.get("summary"), str):
        return False
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return False
    for f in findings:
        if not isinstance(f, dict):
            return False
        for key in ("file", "title", "detail"):
            v = f.get(key)
            # `isinstance(True, str)` is False already, but spelling the bool
            # guard here keeps the rule uniform with the `line` check below.
            if not isinstance(v, str):
                return False
        if f.get("severity") not in _SEVERITIES:
            return False
        if "line" in f:
            line = f["line"]
            # `bool` is a subclass of `int`, so `{"line": true}` would sail
            # through a bare isinstance check and later be formatted as "1".
            if isinstance(line, bool) or not isinstance(line, int):
                return False
    return True


if TYPE_CHECKING:  # pragma: no cover - static conformance, no runtime cost
    # `GrokAdapter` explicitly declares that it satisfies the package's
    # `Adapter` protocol. Under TYPE_CHECKING only, because the protocol lives
    # in the package `__init__` that imports this module — a runtime import
    # would be a cycle. A type checker fails HERE, at the definition site, if
    # `build_cmd`/`parse` ever drift from the protocol.
    from . import Adapter

    _CONFORMS: type[Adapter] = GrokAdapter


# --------------------------------------------------------------------------
# degraded detection
# --------------------------------------------------------------------------


def _detect_degraded(stdout: bytes, stderr: bytes,
                     stop_reason: str | None) -> tuple[bool, str]:
    """Positive evidence that the run was cut short. Order follows the oracle.

    Returns `(degraded, reason)`; `reason` is empty exactly when not degraded.
    """
    err_lower = stderr.lower()
    for sig in _STDERR_SIGNALS:
        if sig in err_lower:
            return True, (
                f"harness failure in stderr ({sig.decode()}); the review may be "
                f"truncated and an empty result cannot be trusted")
    if _LEAKED_TOKEN in stdout:
        return True, (
            "grok leaked tool-call control tokens; the review was cut off mid "
            "tool-use and an empty result cannot be trusted")
    if stop_reason is not None and stop_reason != _STOP_REASON_OK:
        return True, (
            f"grok run did not complete normally (stopReason: {stop_reason}); "
            f"the review was cut off mid-investigation and an empty result "
            f"cannot be trusted")
    # Deliberate divergence from the oracle, in the fail-safe direction: the
    # oracle greps this with `grep -Fq` (case-SENSITIVE). Matching
    # case-insensitively is a strict superset — everything the oracle flags,
    # plus mixed-case spellings a future grok build might emit. A false
    # positive costs one re-review; a false negative is a silent false
    # all-clear. Pinned by the parity tests.
    if _MAX_TURNS in err_lower:
        return True, (
            "grok hit its turn limit (max turns reached); the review ran out "
            "of turns mid-investigation and an empty result cannot be trusted")
    return False, ""
