"""The codex adapter: build the invocation, scan the event stream, judge the run.

Written against codex-cli 0.144.5 and probed flag by flag before a line of it
existed; where this file and the plan disagreed, the installed binary won.
Three things about that CLI shape the whole module:

* **The prompt has no flag.** `codex exec` takes the prompt as a positional
  argument or, given the `-` marker, on stdin — and interpolating a prompt into
  an argv is forbidden outright. So the argv ends in `-`, `stdin_from_prompt_file`
  is True, and the runner opens the prompt FILE as the child's stdin. The prompt
  still travels as a file; this only says who opens it.

* **stdout is a JSONL event stream, not an envelope.** One JSON object per
  line: `thread.started`, `turn.started`, `item.started`/`item.updated`/
  `item.completed`, and a terminal `turn.completed` or `turn.failed`, plus
  stream-level `{"type": "error", "message": ...}` lines. The answer rides in
  the LAST `item.completed` whose `item.type` is `agent_message`, as a JSON
  string in `item.text` — a turn narrates before it answers, so "last", not
  "first". The `-o/--output-last-message` file would be a simpler payload
  source, but `parse` is handed bytes and never a path, and the stream has to
  be read anyway for the health verdict; one source of truth beats two that can
  disagree about the same run.

* **`--output-schema` is OpenAI structured outputs, which is STRICT.** The
  contract's own schema comes back a 400: strict mode demands
  `additionalProperties: false` on every object and a `required` naming every
  property. `build_cmd` therefore writes a strict PROJECTION of
  `contract.json_schema` (see `strict_schema`) rather than the schema verbatim.
  `build_cmd` OWNS that sidecar: it writes it as UTF-8, always overwriting, at
  `prompt_file.with_suffix(".schema.json")`, and names that path in the argv.
  The caller creates no schema file and cleans none up — the pipeline already
  owns the prompt file's directory.

The strict projection has a cost `parse` has to pay back. Strict mode cannot
say "this key may be absent", only "this key may be null", so the contract's
optional properties are spelled nullable-and-required and the model answers
`line: null` where the contract would have omitted `line` — which
`base._valid_payload` rejects outright. `parse` therefore strips JSON nulls
before validating, translating the CLI's spelling of absence back into the
contract's. That translation can only ever REMOVE keys, so it cannot rescue a
payload that is malformed for any other reason: a null `summary` still fails,
a null verdict `index` still fails.

Classification never reads the model's own words. It looks at the exit code,
at stderr, at event `type` fields, and at the `message` of stream-level `error`
events and of `turn.failed` — all of them written by the harness. `agent_message`
text is never consulted, so a review that quotes a 401 or discusses a dropped
stream cannot take the provider down (conformance rule 6).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import Defaults, Reviewer
from .base import (
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    ClassifyResult,
    OutputContract,
    ParseResult,
    _ask,
    _DECODE_FAILURES,
    _first_eligible_object,
)


# The v2 exec event types this adapter reasons about. A turn ends in exactly
# one of these, or in nothing at all.
_TURN_COMPLETED = "turn.completed"
_TURN_FAILED = "turn.failed"
_TERMINAL_TYPES = (_TURN_COMPLETED, _TURN_FAILED)

# The one item type that carries the model's answer. `command_execution`
# items carry `aggregated_output` — whatever the model READ — and a reasoning
# item carries its notes; treating either as the answer would report a file's
# contents as a review.
_ANSWER_ITEM = "agent_message"

# --- degradation tells (stderr only) ---------------------------------------
#
# Matched case-insensitively on stderr BYTES and on nothing else. Every string
# here is present verbatim in the installed codex binary, so these are the
# CLI's own words rather than guesses about what it might say. stdout is never
# searched for them: a review of retry code would otherwise flag itself.
_DEGRADED_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"stream disconnected",
    b"stream closed before response.completed",
    b"stream error",
    b"reached the retry limit",
)

# --- unavailability tells (classify only; never inputs to `degraded`) ------
#
# Consulted only when the stream carried no usable payload — the Phase 1
# non-signal rule: noisy diagnostics alongside a healthy answer are noise, not
# a verdict.
#
# Checked in the order below, and the order is a safety decision rather than
# alphabetical: `quota` is the ONLY provider-wide-cacheable category, so a
# false `quota` takes a working provider out of every later chain in the run,
# while a false `auth`/`model` costs one attempt. Anything that also looks like
# a more specific, attempt-local failure is therefore reported as that instead.
_AUTH_SIGNALS: tuple[bytes, ...] = (
    b"unauthorized",
    b"missing bearer",
    b"not logged in",
    b"sign in again",
    b"invalid api key",
    b"authentication failed",
)

_MODEL_SIGNALS: tuple[bytes, ...] = (
    b"model is not supported",
    b"unknown model",
    b"no such model",
    b"model not found",
    b"invalid model",
    b"unsupported model",
)

# No bare `429` or `402` here, deliberately: diagnostics carry byte offsets and
# request counters, and a numeric substring match would mint provider-wide
# quota outages out of arithmetic. Pinned by
# `test_a_bare_402_is_not_a_quota_signal`.
#
# The last three were added by the Task 14 audit, after a live capture showed
# grok's table missing real budget exhaustion. This table had the same shape of
# gap: it enumerated the phrases its author expected rather than the ones this
# CLI emits. Provenance, stated per entry rather than for the table, because
# the earlier agy draft's blanket claim was wrong for two of its entries:
#
# * `credit limit` — verbatim in the installed codex binary
#   ("You've reached your workspace credit limit"). OBSERVED IN THE BINARY,
#   not in a live failure: the balance to exhaust was xAI's, not OpenAI's.
# * `spend cap` — verbatim in the installed codex binary ("You hit your spend
#   cap set in your workspace. Increase your spend cap to continue."). Same
#   provenance and same caveat.
# * `payment required` — the IANA-registered reason phrase for HTTP 402. It is
#   present in this binary only inside the HTTP crate's canonical reason-phrase
#   table, so it is reachable wherever the CLI renders a status line, and it is
#   the exact wording xAI's CLI was live-captured emitting. SPECULATIVE for
#   THIS provider: no codex run has been observed emitting it.
#
# `balance exhausted` — grok's other new signal — is deliberately NOT here:
# `strings` finds zero occurrences of it in this binary, and adding a signal
# that can never fire while describing it as this CLI's vocabulary is exactly
# the mistake the agy table's comment records and undoes.
_QUOTA_SIGNALS: tuple[bytes, ...] = (
    b"quota",
    b"rate limit",
    b"rate_limit",
    b"ratelimit",
    b"too many requests",
    b"usage limit",
    b"insufficient credit",
    b"out of credits",
    b"credit limit",
    b"spend cap",
    b"payment required",
)

# Canonical effort (`config.EFFORTS`) -> the CLI's `model_reasoning_effort`.
#
# TOTAL, including `"none"`, and that is a deviation from the grok adapter
# worth stating: there `"none"` means "pass no flag", because grok has no such
# value. The OpenAI API does — its enum is `none, minimal, low, medium, high,
# xhigh, max` — so `"none"` here is a real, explicitly requested setting rather
# than a silent fallback to whatever the CLI would have picked.
#
# Two entries are not the identity for reasons the probe established:
#
# * `minimal` is in the API enum but NO model this CLI offers accepts it
#   ("Unsupported value: 'minimal' is not supported with the '...' model", and
#   separately "The following tools cannot be used with reasoning.effort
#   'minimal': web_search"). So canonical `none` maps to the API's own `none`.
# * canonical `max` maps DOWN to `xhigh`, which every listed model supports;
#   the API's `max` is offered by only some of them, so mapping to it would
#   make the effort table depend on the model.
_EFFORT_MAP: dict[str, str] = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


def resolve_codex_bin() -> str:
    """`SKODUN_CODEX_BIN` -> `codex` on PATH.

    No `~`-relative default, unlike grok: the codex CLI is installed on PATH by
    its package manager and `$CODEX_HOME` holds credentials and session state,
    not the executable. An exported-but-EMPTY variable is treated as unset —
    `""` as argv[0] is not a path anyone meant.
    """
    return os.environ.get("SKODUN_CODEX_BIN") or "codex"


# --------------------------------------------------------------------------
# the strict-mode schema projection
# --------------------------------------------------------------------------


def _nullable(sub: object) -> object:
    """Widen a sub-schema so it also accepts `null`.

    Strict mode requires every property to be listed in `required`, so this is
    the only way left to say "the model may not be able to answer this one".
    """
    if not isinstance(sub, dict):
        return sub
    out = dict(sub)
    kind = out.get("type")
    if isinstance(kind, str) and kind != "null":
        out["type"] = [kind, "null"]
    elif isinstance(kind, list) and "null" not in kind:
        out["type"] = list(kind) + ["null"]
    enum = out.get("enum")
    # An enum has to admit the new value too, or the widened type is
    # unsatisfiable and the request is rejected as an invalid schema.
    if isinstance(enum, list) and None not in enum:
        out["enum"] = list(enum) + [None]
    return out


def _strict_node(node: object) -> object:
    """One schema node, projected into OpenAI strict mode.

    `properties`, `required` and `items` are handled by NAME rather than by a
    blind recursive walk: a schema is JSON, and a JSON object may legitimately
    have a property called `required`. Walking generically would rewrite it.
    """
    if not isinstance(node, dict):
        return node
    out = dict(node)
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _strict_node(items)
    props = out.get("properties")
    if not isinstance(props, dict):
        return out
    declared = out.get("required")
    required = ([k for k in declared if isinstance(k, str)]
                if isinstance(declared, list) else [])
    projected: dict[str, object] = {}
    for key, sub in props.items():
        sub = _strict_node(sub)
        if key not in required:
            sub = _nullable(sub)
        projected[key] = sub
    out["properties"] = projected
    out["required"] = list(projected)
    out["additionalProperties"] = False
    return out


def strict_schema(json_schema: str) -> str:
    """`contract.json_schema` as the CLI's `--output-schema` will accept it.

    OpenAI structured outputs are strict: every object must close with
    `additionalProperties: false` and must list every one of its properties in
    `required`. Handing the contract's schema over unprojected is a 400 before
    a single token is spent — which is exactly what
    `tests/fixtures/adapters/openai/degraded_turn_failed.txt` captured.

    Single line out, as it goes in: the file is written for a machine, and a
    one-line schema keeps the fixture and the test diffs readable.
    """
    return json.dumps(_strict_node(json.loads(json_schema)),
                      separators=(",", ":"))


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------


class CodexAdapter:
    """Invocation + output interpretation for the OpenAI codex CLI."""

    name = "codex"
    provider = "openai"
    # `codex exec` has no input-file flag: the prompt is a positional argument
    # or, with the `-` marker, stdin. Interpolating it into the argv is
    # forbidden, so the runner opens the prompt FILE and feeds it in.
    stdin_from_prompt_file = True

    def resolve_binary(self) -> str:
        """The protocol's spelling of `resolve_codex_bin`."""
        return resolve_codex_bin()

    def effort_map(self) -> dict[str, str]:
        """Canonical effort -> `model_reasoning_effort`. A copy, so that a
        caller inspecting the table cannot mutate this adapter's behaviour."""
        return dict(_EFFORT_MAP)

    def prompt_limit(self) -> int | None:
        """No ceiling: the prompt is fed in on STDIN, never as an argv element.

        `stdin_from_prompt_file` is the same fact stated for the runner; this
        states it for the planner. Declaring a number here would shrink every
        batch this provider reviews to fit a limit it does not have.
        """
        return None

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path,
                  contract: OutputContract = REVIEW_CONTRACT) -> list[str]:
        """The full argv for one attempt, and the schema sidecar it names.

        Every flag here was accepted by codex-cli 0.144.5 during the probe.
        The load-bearing ones:

        * `--ignore-user-config` — "Do not load `$CODEX_HOME/config.toml`; auth
          still uses `CODEX_HOME`". Model and effort are then this program's
          decision and cannot be silently overridden by the developer's own
          settings file, which is the same reason grok is run without relying
          on `.grok/settings.json`.
        * `-m` — the model is ALWAYS explicit, never inherited.
        * `-s read-only` — the reviewer reads; it does not edit or run builds.
        * `-c web_search=disabled` — the review is of the diff in front of it.
          `disabled` and not `false` or `off`: the key is a string enum, and
          the other spellings are rejected while loading the config.
        * `--ephemeral` — no session file is left behind for a review.
        * `-` last — the stdin marker; see `stdin_from_prompt_file`.

        `d.max_turns` and `d.deny_tools` have no counterpart on `codex exec`
        (there is no turn cap and no per-tool deny list); the read-only sandbox
        is what stands in for the latter.
        """
        cli_effort = None
        if r.effort is not None:
            mapping = self.effort_map()
            if r.effort not in mapping:
                # LOUD, never a dropped flag: silently reviewing at the CLI's
                # own default effort is an unnoticed downgrade, and an
                # unnoticed downgrade is how a weak review passes for a strong
                # one.
                raise ValueError(
                    f"adapter {self.name!r} has no CLI value for effort "
                    f"{r.effort!r} (known: {sorted(mapping)})")
            cli_effort = mapping[r.effort]

        # `build_cmd` owns the sidecar: it is written here, as UTF-8, always
        # overwriting, beside the prompt whose directory the pipeline already
        # owns. The caller creates nothing and cleans nothing up. Overwriting
        # rather than writing-if-absent is the trust-critical half: two
        # attempts sharing a prompt directory must not have the first one's
        # response shape imposed on the second.
        schema_file = prompt_file.with_suffix(".schema.json")
        schema_file.write_text(strict_schema(contract.json_schema),
                               encoding="utf-8")

        cmd = [
            resolve_codex_bin(), "exec",
            "--json",
            "--output-schema", str(schema_file),
            "--color", "never",
            "-s", "read-only",
            "-m", r.model,
            "-c", "web_search=disabled",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "-C", str(cwd),
        ]
        if cli_effort is not None:
            cmd += ["-c", f"model_reasoning_effort={cli_effort}"]
        cmd.append("-")
        return cmd

    def parse(self, stdout: bytes, stderr: bytes,
              contract: OutputContract = REVIEW_CONTRACT) -> ParseResult:
        events = _events(stdout)
        payload, terminal = _extract(events, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        degraded, reason = _detect_degraded(events, terminal, stderr)
        # The `findings`/`summary` projection is REVIEW_CONTRACT's alone. Under
        # any other contract those two stay empty rather than being filled from
        # a foreign payload, so a Phase 1 caller that only knows them can never
        # read a refuter response as a review; such callers get `payload`.
        review = parse_ok and contract is REVIEW_CONTRACT
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if review else [],
            summary=payload["summary"] if review else "",
            stop_reason=terminal,
            degraded=degraded,
            degraded_reason=reason,
            payload=payload if parse_ok else None,
        )

    def classify(self, rc: int, stdout: bytes, stderr: bytes,
                 contract: OutputContract = REVIEW_CONTRACT) -> ClassifyResult:
        """Run health, on its own axis from parsing. Never raises.

        Precedence, and every step of it is a fail-safe choice:

        1. `rc 127` — the binary is not there, so nothing else means anything.
        2. Usable output wins over noisy diagnostics, on the AVAILABILITY axis
           only. A run that produced a valid payload is not "unavailable"
           because the harness grumbled on the way. Usability is judged by
           `contract.validate`, so a valid refuter response counts as usable.
        3. Unavailability tells, attempt-local ones first (see the tables),
           read from stderr and from harness-authored error events — never
           from the model's own message text.
        4. Degradation evidence, byte-for-byte the same signals `parse`
           reports. This is checked AFTER step 2 on purpose: "the provider
           served" is proved by the payload, "the answer is complete" is not,
           and conflating them is the Phase 1 silent false all-clear.
        5. Otherwise `ok` — including a run with empty stdout and clean stderr,
           which is a failed attempt (`parse_ok=False`) but carries no positive
           evidence of anything. Inventing a category for it would be the
           inference-from-absence this module refuses to make.
        """
        if rc == UNAVAILABLE_RC:
            return ClassifyResult(
                "unavailable", "binary",
                f"binary not found (rc {UNAVAILABLE_RC})")
        events = _events(stdout)
        payload, terminal = _extract(events, contract.eligible)
        if not _ask(contract.validate, payload):
            diagnostics = stderr.lower() + b"\n" + _diagnostics(events)
            for category, signals in (("auth", _AUTH_SIGNALS),
                                      ("model", _MODEL_SIGNALS),
                                      ("quota", _QUOTA_SIGNALS)):
                for sig in signals:
                    if sig in diagnostics:
                        return ClassifyResult(
                            "unavailable", category,
                            f"{category} failure in the run's diagnostics "
                            f"({sig.decode()}) with no usable "
                            f"{contract.name} payload")
        degraded, reason = _detect_degraded(events, terminal, stderr)
        if degraded:
            return ClassifyResult("degraded", "", reason)
        return ClassifyResult("ok", "", "")


# --------------------------------------------------------------------------
# the event stream
# --------------------------------------------------------------------------


def _events(stdout: bytes) -> list[dict]:
    """Every well-formed event line, in order. Never raises.

    `errors="replace"` rather than a strict decode: in exactly the truncated
    runs that matter, the last line is cut mid-codepoint, and a
    `UnicodeDecodeError` here would blind the degradation check on the very
    runs it exists to catch.

    The split is on `\\n` BYTES, before decoding, and that is a correctness
    requirement rather than a micro-optimisation. `str.splitlines()` also
    breaks on U+2028, U+2029 and U+0085; serde_json — which writes this stream
    — escapes none of the three, so a finding whose text quotes one of them
    reaches the wire raw and a `splitlines()` split cuts the `item.completed`
    event in two. Neither half decodes, the event disappears, and the run
    reports `parse_ok=False, degraded=False` — no payload AND no degradation
    signal — on a turn that completed. Fail-closed, but silently, and a review
    of source containing U+2028 would fail that way every single time.

    A line that does not decode is skipped rather than fatal — a half-written
    final line is what a killed process actually leaves behind, and the events
    before it are still evidence. An object with no string `type` is not an
    event: this stream is line-delimited JSON written by the harness, and
    "some JSON appeared on stdout" must not be mistaken for "the turn ran".
    """
    out: list[dict] = []
    for raw in stdout.split(b"\n"):
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except _DECODE_FAILURES:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            out.append(obj)
    return out


def _strip_nulls(obj: object) -> object:
    """Drop every JSON `null` VALUE, recursively.

    The CLI's strict schema mode cannot express an absent key, only a null one,
    so the contract's optional properties come back as `line: null` rather than
    not at all — and `base._valid_payload` rejects a non-int `line`. This
    translates the CLI's spelling of absence back into the contract's.

    It can only ever REMOVE keys, which is what makes it safe in a fail-closed
    path: a payload that is malformed for any other reason stays malformed. A
    null `summary` is still a missing `summary`; a null verdict `index` is
    still an unkeyable verdict.
    """
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def _first_payload(text: str, eligible: Callable[[object], bool]) -> dict | None:
    """`base._first_eligible_object`, with the strict-mode null translation.

    The scan is shared — the "prose or a ```json fence around the answer" case
    is not codex's alone — and `_strip_nulls` rides in as its `transform`, so
    every candidate is translated out of the CLI's spelling of absence before
    the contract's eligibility rule ever sees it. That ordering is the point:
    an object whose only defect is strict mode's `null`s must be recognised as
    the payload, not skipped over in favour of the next `{` in the stream.
    """
    return _first_eligible_object(text, eligible, _strip_nulls)


def _extract(events: list[dict],
             eligible: Callable[[object], bool]) -> tuple[dict | None, str | None]:
    """The answer and the terminal event type, from one event stream.

    The LAST eligible `agent_message` wins, not the first: a codex turn
    narrates as it works ("I'm checking X first, then I'll answer") and each
    of those is an `agent_message` too. Taking the first would record the
    model's opening remark as the review.
    """
    payload: dict | None = None
    terminal: str | None = None
    for ev in events:
        kind = ev.get("type")
        if kind in _TERMINAL_TYPES:
            terminal = kind
            continue
        if kind != "item.completed":
            continue
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != _ANSWER_ITEM:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            found = _first_payload(text, eligible)
            if found is not None:
                payload = found
    return payload, terminal


def _diagnostics(events: list[dict]) -> bytes:
    """The harness's own words about why a run went wrong, lowercased.

    Stream-level `error` events and `turn.failed`'s `error.message` ONLY.
    Deliberately not the model's `agent_message` text, and deliberately not
    item-level `error` items either: those carry advisory notices ("Skill
    descriptions were shortened to fit the context budget", "Model metadata
    ... Defaulting to fallback metadata") on runs that then succeed, and a
    classification table fed by advisories is a table that fires at random.
    """
    parts: list[str] = []
    for ev in events:
        kind = ev.get("type")
        if kind == "error":
            message = ev.get("message")
        elif kind == _TURN_FAILED:
            err = ev.get("error")
            message = err.get("message") if isinstance(err, dict) else None
        else:
            continue
        if isinstance(message, str):
            parts.append(message)
    return "\n".join(parts).lower().encode("utf-8", "replace")


# --------------------------------------------------------------------------
# degraded detection
# --------------------------------------------------------------------------


def _detect_degraded(events: list[dict], terminal: str | None,
                     stderr: bytes) -> tuple[bool, str]:
    """Positive evidence that the run was cut short. Never inferred from absence.

    Returns `(degraded, reason)`; `reason` is empty exactly when not degraded.

    The three signals, each on its own:

    1. stderr carrying the CLI's transport-failure wording — the harness
       telling us it lost the stream while the turn was still going.
    2. a terminal `turn.failed` — the turn ended, and not well.
    3. events, but no terminal event at all — the process died or was killed
       with the turn still open. This is the sharp one: the answer may already
       be complete and schema-valid on the wire, and only the missing terminal
       event says the run never finished. It is codex's spelling of the
       `stopReason: Cancelled` runs that recorded clean, with zero findings,
       116 times in the Phase 1 corpus.

    An EMPTY stream is not degraded. A run with no output and clean stderr is a
    failed attempt (`parse_ok=False`), but nothing about it is evidence of
    truncation, and manufacturing a signal from silence is the inference from
    absence this module refuses to make.
    """
    err_lower = stderr.lower()
    for sig in _DEGRADED_STDERR_SIGNALS:
        if sig in err_lower:
            return True, (
                f"transport failure in stderr ({sig.decode()}); the review may "
                f"be truncated and an empty result cannot be trusted")
    if terminal == _TURN_FAILED:
        return True, (
            "the codex turn failed (turn.failed); the review was cut off "
            "mid-investigation and an empty result cannot be trusted")
    if events and terminal is None:
        return True, (
            "the codex event stream carried no terminal turn event; the run "
            "did not complete and an empty result cannot be trusted")
    return False, ""


if TYPE_CHECKING:  # pragma: no cover - static conformance, no runtime cost
    # `CodexAdapter` explicitly declares that it satisfies the package's
    # `Adapter` protocol. A type checker fails HERE, at the definition site, if
    # any member ever drifts from the protocol — which matters more once four
    # adapters exist and only one of them is exercised by a given config.
    from .base import Adapter

    _CONFORMS: type[Adapter] = CodexAdapter
