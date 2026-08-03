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
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import Defaults, Reviewer
from .base import (
    _REVIEW_SCHEMA,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    ClassifyResult,
    OutputContract,
    ParseResult,
    _ask,
    _DECODE_FAILURES,
    _first_eligible_object,
    _valid_payload,
)

# The review schema now lives in `base` (every adapter asks for the same
# response shape); this name is kept so the byte-for-byte oracle-parity test
# and every other Phase 1 caller keep working. Same object, not a copy.
SCHEMA = _REVIEW_SCHEMA

# `ParseResult` and `_valid_payload` above are imported for the same reason and
# are NOT unused: they moved to `base` and are re-exported from here so that
# every Phase 1 import site — `from skodun.adapters.grok import ParseResult` —
# keeps resolving to the one shared object rather than to a copy.

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

# The stopReason values that mean "completed normally". An ALLOWLIST is
# deliberate: a false positive costs one re-review, a false negative is the
# silent false all-clear this exists to prevent.
#
# Grok CLI ≥0.2.118 emits snake_case `end_turn` (live dogfood 2026-08-03);
# older builds emit PascalCase `EndTurn` (also the legacy tubescribes spelling
# after its #3672 normalization). Both are normal completion. `Cancelled` and
# unknown values stay degraded. The allowlist stays CLOSED — case folding of
# arbitrary strings is not done (e.g. `endturn` without underscore is still
# abnormal).
_STOP_REASON_OK: frozenset[str] = frozenset({"EndTurn", "end_turn"})

# --- unavailability tells (classify only; never inputs to `degraded`) ------
#
# All matched case-insensitively on stderr BYTES, and all consulted only when
# stdout carried no usable payload — the Phase 1 non-signal rule: noisy stderr
# alongside a healthy answer is noise, not a verdict. (`test_auth_noise_is_not_
# degraded` pins exactly that case for `Auth(AuthorizationRequired)`.)
#
# Checked in this order, and the order is a safety decision rather than
# alphabetical: `quota` is the ONLY provider-wide-cacheable category, so a
# false `quota` takes a working provider out of every later chain in the run,
# while a false `auth`/`model` costs one attempt. Anything that also looks like
# a more specific, attempt-local failure is therefore reported as that instead.
_AUTH_SIGNALS: tuple[bytes, ...] = (
    b"authorizationrequired",
    b"authorization required",
    b"unauthorized",
    b"authentication failed",
    b"invalid api key",
    b"not logged in",
)

_MODEL_SIGNALS: tuple[bytes, ...] = (
    b"unknown model",
    b"no such model",
    b"model not found",
    b"invalid model",
    b"unsupported model",
)

# No bare `429` or `402` here, deliberately: stderr can carry line numbers and
# byte offsets, and a numeric substring match would mint provider-wide quota
# outages out of arithmetic. That rejection is Task 1's and it still stands —
# what the last two entries change is the OTHER half of the same argument.
#
# The last two were added after Task 14's live acceptance run found that real
# xAI budget exhaustion matched nothing here. The installed CLI's actual
# message, rc 1 with empty stdout, is:
#
#     API error (status 402 Payment Required): <tier> usage balance exhausted
#
# `quota`, `rate limit` and `too many requests` all miss it, and so — this is
# the near miss worth naming — does `usage limit`, which reads as though it
# would match "usage balance exhausted" and does not. `classify` fell through
# to `ok`, the fallback chain never advanced, and nothing was cached: the
# headline feature defeated by its own most likely real-world trigger.
#
# Task 1's conservatism was about a BARE NUMBER being too easily present in
# unrelated stderr, and the safe failure direction being a missed `unavailable`
# rather than a false provider outage. A missed quota signal is exactly the
# failure being fixed here, so the two halves have to be weighed rather than
# one of them applied by reflex. Both additions are PHRASES, not numbers:
#
# * `payment required` is the IANA-registered reason phrase for HTTP 402 and
#   means one thing in every provider's vocabulary. It cannot arrive from
#   arithmetic, and a diagnostic that contains it is a diagnostic about
#   billing.
# * `balance exhausted` is the captured wording, and is specific in the way a
#   bare `exhausted` is not — CLIs routinely exhaust retries, iterators, token
#   budgets and context windows, and a bare `exhausted` would read all of those
#   as a provider outage.
#
# `402` itself stays out for precisely Task 1's reason, and
# `test_a_bare_402_is_not_a_quota_signal` pins that it does.
#
# Captured live on 2026-07-28 from grok 0.2.112; the capture is
# `tests/fixtures/adapters/xai/unavailable_quota.txt`.
_QUOTA_SIGNALS: tuple[bytes, ...] = (
    b"quota",
    b"rate limit",
    b"rate_limit",
    b"ratelimit",
    b"too many requests",
    b"usage limit",
    b"insufficient credit",
    b"out of credits",
    b"payment required",
    b"balance exhausted",
)

# Canonical effort (`config.EFFORTS`) -> grok's spelling. Pass-through today;
# it exists so that a provider whose CLI spells these differently is a table
# edit rather than a branch. `"none"` is absent on purpose: it is the opt-out
# handled by `_EFFORT_OFF` before any lookup happens, not a CLI value.
_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
}

# Models that reject `--effort`. Prefix match, so `grok-build-fast-1` and any
# future `grok-build*` build are covered without another table edit.
_NO_EFFORT_PREFIXES = ("grok-build",)

# `effort` values that mean "do not pass the flag at all". `None` is unset;
# `"none"` is the user's explicit opt-out. Those are the only two: `config`
# rejects every other spelling (`""` included — it is not in `config.EFFORTS`)
# before a `Reviewer` can reach this module, so an empty-string branch here
# would be dead code that quietly implies a third, undocumented opt-out.
_EFFORT_OFF = (None, "none")


def resolve_grok_bin() -> str:
    """`SKODUN_GROK_BIN` -> `~/.grok/bin/grok` if executable -> `grok` on PATH.

    There is deliberately no legacy `-p` re-shell fallback (global constraint):
    a run with empty stdout is simply a failed attempt (`parse_ok=False`), and
    the prompt is never re-invoked as a CLI argument. It is not retried either
    -- `pipeline._run_chain` retries only a `timed_out` attempt or a
    `degraded` one, and empty stdout with a clean stderr is neither. Nor does
    it advance a fallback chain: that is reserved for `classify` reporting
    `unavailable`, and empty output with clean stderr is `ok`.
    """
    override = os.environ.get("SKODUN_GROK_BIN")
    if override:
        return override
    default = Path.home() / ".grok" / "bin" / "grok"
    return str(default) if os.access(default, os.X_OK) else "grok"


class GrokAdapter:
    """Invocation + output interpretation for the xAI grok CLI."""

    name = "grok"
    provider = "xai"
    # grok takes the prompt via `--prompt-file`, so nothing goes on stdin.
    stdin_from_prompt_file = False

    def resolve_binary(self) -> str:
        """The protocol's spelling of the existing `resolve_grok_bin`."""
        return resolve_grok_bin()

    def effort_map(self) -> dict[str, str]:
        """Canonical effort -> grok's `--effort` value. A copy, so that a
        caller inspecting the table cannot mutate this adapter's behaviour."""
        return dict(_EFFORT_MAP)

    def prompt_limit(self) -> int | None:
        """No ceiling: the prompt travels as `--prompt-file`, not as argv.

        Nothing in this invocation grows with the prompt, so the kernel's
        per-argument cap never applies to it and this adapter has no size it
        must refuse. Declaring a number here would shrink every batch this
        provider reviews to fit a limit it does not have.
        """
        return None

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path,
                  contract: OutputContract = REVIEW_CONTRACT) -> list[str]:
        """The full argv for one review attempt.

        The prompt travels as a FILE, never as a shell-interpolated string, and
        the model is always explicit (`-m`) rather than inherited from the
        binary's own settings file. The response shape comes from `contract`,
        so the same invocation serves a review or a refuter pass.
        """
        effort = None if r.effort in _EFFORT_OFF else r.effort
        if effort is not None and r.model.startswith(_NO_EFFORT_PREFIXES):
            raise ValueError(
                f"model {r.model!r} does not support effort (configured "
                f"{r.effort!r}) — remove it, set effort = \"none\", or "
                f"choose a model that supports it")
        cli_effort = None
        if effort is not None:
            mapping = self.effort_map()
            if effort not in mapping:
                # LOUD, never a dropped flag: silently reviewing at the CLI's
                # own default effort is an unnoticed downgrade, and an
                # unnoticed downgrade is how a weak review passes for a strong
                # one.
                raise ValueError(
                    f"adapter {self.name!r} has no CLI value for effort "
                    f"{effort!r} (known: {sorted(mapping)})")
            cli_effort = mapping[effort]
        cmd = [
            resolve_grok_bin(),
            "--prompt-file", str(prompt_file),
            "--json-schema", contract.json_schema,
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
        if cli_effort is not None:
            cmd += ["--effort", cli_effort]
        return cmd

    def parse(self, stdout: bytes, stderr: bytes,
              contract: OutputContract = REVIEW_CONTRACT) -> ParseResult:
        payload, stop_reason = _extract_review(stdout, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        degraded, reason = _detect_degraded(stdout, stderr, stop_reason)
        # The `findings`/`summary` projection is REVIEW_CONTRACT's alone. Under
        # any other contract those two stay empty rather than being filled from
        # a foreign payload, so a Phase 1 caller that only knows them can never
        # read a refuter response as a review; such callers get `payload`.
        review = parse_ok and contract is REVIEW_CONTRACT
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if review else [],
            summary=payload["summary"] if review else "",
            stop_reason=stop_reason,
            degraded=degraded,
            degraded_reason=reason,
            payload=payload if parse_ok else None,
        )

    def classify(self, rc: int, stdout: bytes, stderr: bytes,
                 contract: OutputContract = REVIEW_CONTRACT) -> ClassifyResult:
        """Run health, on its own axis from parsing. Never raises.

        Precedence, and every step of it is a fail-safe choice:

        1. `rc 127` — the binary is not there, so nothing else in the output
           means anything.
        2. Usable output wins over noisy stderr. This is the Phase 1 non-signal
           rule: a run that produced a valid payload is not "unavailable"
           because its harness grumbled on the way. Usability is judged by
           `contract.validate`, so a valid refuter response counts as usable.
        3. Unavailability tells, attempt-local ones first (see the tables).
        4. `_detect_degraded`'s truncation evidence, byte-for-byte the same
           signals `parse` reports.
        5. Otherwise `ok` — including a run with empty stdout and clean stderr,
           which is a failed attempt (`parse_ok=False`) but carries no positive
           evidence of anything, and inventing a category for it would be the
           inference-from-absence this module refuses to make.
        """
        if rc == UNAVAILABLE_RC:
            return ClassifyResult(
                "unavailable", "binary",
                f"binary not found (rc {UNAVAILABLE_RC})")
        payload, stop_reason = _extract_review(stdout, contract.eligible)
        if not _ask(contract.validate, payload):
            err_lower = stderr.lower()
            for category, signals in (("auth", _AUTH_SIGNALS),
                                      ("model", _MODEL_SIGNALS),
                                      ("quota", _QUOTA_SIGNALS)):
                for sig in signals:
                    if sig in err_lower:
                        return ClassifyResult(
                            "unavailable", category,
                            f"{category} failure in stderr ({sig.decode()}) "
                            f"with no usable {contract.name} payload")
        degraded, reason = _detect_degraded(stdout, stderr, stop_reason)
        if degraded:
            return ClassifyResult("degraded", "", reason)
        return ClassifyResult("ok", "", "")


# --------------------------------------------------------------------------
# envelope extraction
# --------------------------------------------------------------------------


def _first_review_object(text: str,
                         eligible: Callable[[object], bool]) -> dict | None:
    """`base._first_eligible_object`, with no payload translation.

    The scan itself is shared — every adapter needs the same "find the first
    object the contract will accept, in prose that may wrap or repeat it" — and
    lives in `base` so the decoder guard around it exists once rather than once
    per provider. grok passes no `transform`: its envelope carries the payload
    in the contract's own spelling, and nothing has to be translated.

    The name is kept because the three-level fallback below reads better for
    it, and because this is the level Phase 1's tests talk about.
    """
    return _first_eligible_object(text, eligible)


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
    except _DECODE_FAILURES:
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


def _extract_review(
    stdout: bytes,
    eligible: Callable[[object], bool],
) -> tuple[dict | None, str | None]:
    """Three-level fallback: `structuredOutput` -> `text` -> raw scan.

    `eligible` is the requested contract's candidate predicate and is the ONLY
    contract-dependent thing here: the envelope is grok's regardless of what
    shape we asked it to put inside.

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
        if _ask(eligible, so):
            return so, stop_reason
        inner = root.get("text")
        if isinstance(inner, str) and inner.strip():
            found = _first_review_object(inner, eligible)
            if found is not None:
                return found, stop_reason

    return _first_review_object(text, eligible), stop_reason


if TYPE_CHECKING:  # pragma: no cover - static conformance, no runtime cost
    # `GrokAdapter` explicitly declares that it satisfies the package's
    # `Adapter` protocol. A type checker fails HERE, at the definition site, if
    # any member ever drifts from the protocol — which matters more once four
    # adapters exist and only one of them is exercised by a given config.
    from .base import Adapter

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
    if stop_reason is not None and stop_reason not in _STOP_REASON_OK:
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
