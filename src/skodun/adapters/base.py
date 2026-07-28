"""The provider-neutral half of an adapter: shapes, contracts, verdicts.

Everything here is true of *every* provider, so it lives above the individual
CLIs rather than being copied into each of them. Three ideas:

* **`ParseResult`** — what one attempt's output is worth. It is the only
  currency upstream code deals in; no caller ever sees a provider's envelope.
* **`ClassifyResult`** — the run-health verdict, on a separate axis from
  parsing. `degraded` requires *positive evidence* that this run's output was
  truncated or corrupted; `unavailable` means the provider could not serve at
  all. `category` is the cacheability axis: only `"quota"` is a property of the
  provider as a whole, so only `"quota"` may be remembered beyond one attempt.
  `auth`/`binary`/`model` are attempt-local — they describe this reviewer's
  configuration, and caching them would take down providers that are fine.
* **`OutputContract`** — the response shape a run is asked for. An adapter is
  contract-generic: it hands `contract.json_schema` to whatever schema
  mechanism its CLI offers, finds the envelope with `contract.eligible`, and
  believes the payload only if `contract.validate` says so. Adding a new
  response shape is then a new contract, not a new branch in every adapter.

`parse` and `classify` never raise, on any input. That is a trust property, not
politeness: an exception escaping into the gate path would be reported as an
unexpected error rather than as an untrustworthy review, and the two have very
different consequences. Totality is not one guard but two, in different places,
and neither alone is enough:

* `_ask` here covers the *contract predicates*, which are caller-supplied
  callables this module cannot vouch for.
* `_DECODE_FAILURES` here covers the *decoder*, which raises `RecursionError`
  — not a `ValueError` — on deeply nested untrusted output. It and the scan
  that uses it (`_first_eligible_object`) live in this module rather than in
  each adapter because that guard is the exact defect Task 1's review found:
  two copies of a totality guard is one fix away from a provider that still
  raises, and four adapters are planned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from ..config import Defaults, Reviewer

# The shell's command-not-found status. Every POSIX shell uses it, so it means
# "the binary named in the argv does not exist" for every provider CLI alike —
# which is why the conformance suite can assert it across the whole registry.
UNAVAILABLE_RC = 127

# Verbatim from the oracle's `GROK_REVIEW_SCHEMA` (pinned byte-for-byte by
# `test_schema_matches_oracle_verbatim`). Single line: it is one argv element.
_REVIEW_SCHEMA = (
    '{"type":"object","properties":{"summary":{"type":"string"},"findings":'
    '{"type":"array","items":{"type":"object","properties":{"file":{"type":'
    '"string"},"line":{"type":"integer"},"severity":{"type":"string","enum":'
    '["high","medium","low"]},"category":{"type":"string"},"title":{"type":'
    '"string"},"detail":{"type":"string"}},"required":["file","severity",'
    '"title","detail"]}}},"required":["summary","findings"]}'
)

# The refuter's response shape: one verdict per finding it was handed, keyed
# back to the input by `index`. Same single-line style as the review schema —
# it is one argv element too.
_REFUTER_SCHEMA = (
    '{"type":"object","properties":{"verdicts":{"type":"array","items":'
    '{"type":"object","properties":{"index":{"type":"integer"},"verdict":'
    '{"type":"string","enum":["confirmed","refuted","uncertain"]},"reasoning":'
    '{"type":"string"}},"required":["index","verdict","reasoning"]}}},'
    '"required":["verdicts"]}'
)

_SEVERITIES = frozenset({"high", "medium", "low"})

_VERDICTS = frozenset({"confirmed", "refuted", "uncertain"})


@dataclass(frozen=True)
class ClassifyResult:
    """How the *run* went, independent of whether its output parsed.

    `kind` is the decision Task 7's fallback chains switch on; `category` is
    only meaningful for `unavailable` and is empty otherwise. `detail` is for
    humans and logs and is never matched on.

    `ok` means "no positive evidence of ill health", NOT "produced usable
    output": a run with empty stdout and clean stderr is `ok` and yet worth
    nothing. Whether the attempt yielded anything is `ParseResult.parse_ok`, on
    the other axis, and a caller that needs both must check both.
    """

    kind: Literal["ok", "degraded", "unavailable"]
    category: str = ""     # for unavailable: quota|auth|binary|model|other
    detail: str = ""


@dataclass(frozen=True)
class ParseResult:
    """What one attempt's output is worth.

    `findings`/`summary` are only populated when `parse_ok` — a payload that
    failed validation must not leak half-shaped findings to a caller that
    checked the wrong flag. They are also, deliberately, the REVIEW_CONTRACT
    *projection*: under any other contract they stay `[]`/`""` so that a
    refuter response can never be mistaken for a review by a Phase 1 caller
    that only knows these two fields. Consumers of other contracts read
    `payload`, which is populated whenever `parse_ok`, for every contract,
    review included.
    """

    parse_ok: bool
    findings: list            # review-contract projection; [] for other contracts
    summary: str              # review-contract projection; "" for other contracts
    stop_reason: str | None
    degraded: bool
    degraded_reason: str
    payload: dict | None = None   # the contract-validated payload, verbatim


@dataclass(frozen=True)
class OutputContract:
    """The response shape a run is asked for.

    `eligible` is the envelope-extraction predicate — "does this JSON object
    look like the thing we asked for?" — and is applied identically at every
    fallback level of an adapter's extractor, so extraction needs no
    contract-conditionals. `validate` is the trust-critical check that decides
    `parse_ok`; it is always stricter than `eligible`.
    """

    name: str                          # "review" | "refuter"
    json_schema: str                   # single-line JSON Schema for the CLI flag
    eligible: Callable[[object], bool] # envelope-extraction predicate
    validate: Callable[[object], bool]


def _ask(pred: Callable[[object], bool], obj: object) -> bool:
    """Call a contract predicate and answer False if it misbehaves.

    Contracts carry caller-supplied callables. `parse`/`classify` promise never
    to raise, and that promise cannot be conditional on a third-party
    predicate being well-behaved. False is the fail-closed answer: an
    unanswerable payload is not a payload this program may act on.

    This covers the predicates and nothing else. The JSON decode that produces
    the object handed in here is guarded separately, by `_DECODE_FAILURES`
    below — so do not read this function as making the never-raise promise
    total on its own.
    """
    try:
        return bool(pred(obj))
    except Exception:  # noqa: BLE001 - deliberately total; see docstring
        return False


# --------------------------------------------------------------------------
# untrusted JSON: the decoder guard and the scan every adapter needs
# --------------------------------------------------------------------------

# Everything `json`'s decoder can throw at a hostile blob. `ValueError` is the
# documented one (`JSONDecodeError` subclasses it), but the C scanner signals
# "too deeply nested" with `RecursionError`, which is a `RuntimeError` and so
# sails past `except ValueError` untouched. Every decode site in every adapter
# sees untrusted model output — 64 KB of `[[[[` is a plausible thing for a
# confused model to emit and must be worth `parse_ok=False`, not an exception
# escaping into the gate path.
#
# It lives HERE, in one place, because it is the exact defect Task 1's review
# found in the grok adapter, and a fix applied to one copy of a guard is a fix
# that silently misses the others. Four adapters are planned; one tuple.
_DECODE_FAILURES = (ValueError, RecursionError)


def _first_eligible_object(
    text: str,
    eligible: Callable[[object], bool],
    transform: Callable[[object], object] | None = None,
) -> dict | None:
    """First eligible top-level JSON object in `text`, or None. Never raises.

    `eligible` is the requested contract's candidate predicate, applied
    identically to every candidate, so extraction needs no
    contract-conditionals. For `REVIEW_CONTRACT` it is `_review_eligible`, and
    two failure modes hang off that single rule:

    * An empty or hollow envelope slot (`{}`) is NOT eligible, so it falls
      through instead of masking a perfectly good payload elsewhere.
    * An individual *finding* (or *verdict*) object is not eligible, so a scan
      over a truncated envelope does not lock onto the first element of the
      array and record `parse_ok` with no real content.

    `raw_decode` from each `{` rather than `json.loads` on the whole string:
    every CLI observed so far sometimes wraps its answer in prose or a ```json
    fence, and grok sometimes emits the object twice. All of those make a bare
    `loads` die with "Extra data" and lose the answer entirely.

    `transform` is applied to each decoded candidate BEFORE `eligible` sees it,
    and is how an adapter translates its CLI's spelling of a payload into the
    contract's. The codex adapter passes `_strip_nulls` (OpenAI strict mode
    cannot express an absent key, only a null one); grok passes nothing,
    because its envelope needs no translation. `transform` is arbitrary
    adapter-supplied code fed decoded-but-untrusted JSON — a shape it does not
    control — so its call is guarded by its OWN `except Exception`, broader
    than `_DECODE_FAILURES`: nothing pins a transform to raising only
    `ValueError`/`RecursionError`, and this function's whole job is that
    NOTHING it does, decode or transform, escapes as an exception. A
    candidate the transform chokes on is treated exactly like one the decoder
    itself could not read: skipped, in favour of the next `{`.
    """
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except _DECODE_FAILURES:
            pos = text.find("{", pos + 1)
            continue
        if transform is not None:
            try:
                obj = transform(obj)
            except Exception:  # noqa: BLE001 - deliberately total; a
                # transform is caller-supplied code over untrusted-shaped
                # data, so it gets the same fail-closed treatment `_ask`
                # gives contract predicates, not just `_DECODE_FAILURES`.
                pos = text.find("{", pos + 1)
                continue
        if _ask(eligible, obj):
            return obj
        pos = text.find("{", pos + 1)
    return None


# --------------------------------------------------------------------------
# eligibility — the envelope-extraction predicates
# --------------------------------------------------------------------------


def _review_eligible(obj: object) -> bool:
    """The ONE review candidate predicate, applied identically at all levels.

    A candidate must carry `summary` or `findings`. Two failure modes hang off
    this single rule:

    * An empty or hollow `structuredOutput` (`{}`) is NOT eligible, so it falls
      through instead of masking a perfectly good payload sitting in `text`.
    * An individual *finding* object is not eligible, so a raw scan over a
      truncated envelope does not lock onto the first element of the findings
      array and record `parse_ok` with no real content.
    """
    return isinstance(obj, dict) and ("summary" in obj or "findings" in obj)


def _refuter_eligible(obj: object) -> bool:
    """The refuter counterpart: the envelope must carry `verdicts`.

    A single *verdict* object is not eligible, for the same reason a single
    finding is not: a raw scan over a truncated refuter envelope must not lock
    onto the first array element and call it the whole answer.
    """
    return isinstance(obj, dict) and "verdicts" in obj


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------


def _valid_payload(obj: object) -> bool:
    """True iff `obj` is a review this program can act on without guessing."""
    # `_review_eligible` already implies `isinstance(obj, dict)`, but this is
    # the trust-critical validator: the narrowing is spelled as a real check
    # rather than an `assert`, which `python -O` strips. Under -O a bare assert
    # would leave `obj.get` unguarded and turn a hostile payload into an
    # AttributeError inside the gate path instead of a clean `parse_ok=False`.
    if not isinstance(obj, dict) or not _review_eligible(obj):
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
        # `isinstance` FIRST, and not for tidiness: `x not in frozenset` calls
        # `hash(x)`, so `{"severity": {}}` — a plausible thing for a model to
        # emit where the schema asked for a string — raises TypeError out of a
        # validator whose whole job is to answer True or False. `_ask` contains
        # that today, but a validator on the trust path must not depend on its
        # caller to convert a shape error into an answer.
        severity = f.get("severity")
        if not isinstance(severity, str) or severity not in _SEVERITIES:
            return False
        if "line" in f:
            line = f["line"]
            # `bool` is a subclass of `int`, so `{"line": true}` would sail
            # through a bare isinstance check and later be formatted as "1".
            if isinstance(line, bool) or not isinstance(line, int):
                return False
    return True


def _valid_verdicts(obj: object) -> bool:
    """True iff `obj` is a refuter response every verdict of which is keyable.

    Mirrors `_valid_payload`'s strictness, and for the same reason: one
    malformed item fails the whole payload, so the run is retried rather than
    believed. A verdict whose `index` cannot be trusted cannot be merged back
    onto the finding it judges, and a merge that guesses is worse than a retry.

    Reasoning *length* is deliberately not checked here: how much justification
    a refutation owes is merge policy, not payload shape.
    """
    if not isinstance(obj, dict) or not _refuter_eligible(obj):
        return False
    verdicts = obj.get("verdicts")
    if not isinstance(verdicts, list):
        return False
    for v in verdicts:
        if not isinstance(v, dict):
            return False
        # `type(...) is int` rather than `isinstance`: `bool` subclasses `int`,
        # so `{"index": true}` would otherwise be accepted and then merged onto
        # finding number 1.
        if type(v.get("index")) is not int:
            return False
        # `isinstance` first, for the reason spelled at the severity check
        # above: membership in a frozenset hashes its left operand, and an
        # unhashable one turns a malformed verdict into a TypeError instead of
        # a `parse_ok=False`.
        verdict = v.get("verdict")
        if not isinstance(verdict, str) or verdict not in _VERDICTS:
            return False
        if not isinstance(v.get("reasoning"), str):
            return False
    return True


REVIEW_CONTRACT = OutputContract("review", _REVIEW_SCHEMA,
                                 _review_eligible, _valid_payload)
REFUTER_CONTRACT = OutputContract("refuter", _REFUTER_SCHEMA,
                                  _refuter_eligible, _valid_verdicts)


# --------------------------------------------------------------------------
# the protocol
# --------------------------------------------------------------------------


class Adapter(Protocol):
    """What every provider adapter must offer.

    `name` is the adapter (`"grok"`, `"codex"`); `provider` is the registry key
    it is reachable under (`"xai"`, `"openai"`). They differ because one
    provider may ship more than one CLI.
    """

    name: str
    provider: str
    # Set by adapters whose CLI reads the prompt from stdin rather than from a
    # `--prompt-file`-style flag. The prompt still travels as a FILE either
    # way; this only says who opens it. Task 7's runner honours it.
    stdin_from_prompt_file: bool = False

    def resolve_binary(self) -> str:
        """`SKODUN_<NAME>_BIN` -> the adapter's default path -> bare name."""
        ...

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults, cwd: Path,
                  contract: OutputContract = REVIEW_CONTRACT) -> list[str]:
        """Full argv for one attempt. The prompt travels as a file."""
        ...

    def parse(self, stdout: bytes, stderr: bytes,
              contract: OutputContract = REVIEW_CONTRACT) -> ParseResult:
        """Interpret one attempt's raw output. Never raises on garbage."""
        ...

    def classify(self, rc: int, stdout: bytes, stderr: bytes,
                 contract: OutputContract = REVIEW_CONTRACT) -> ClassifyResult:
        """Judge the run's health. Never raises on garbage.

        Takes the contract because usable-output precedence has to know which
        payload shape counts as usable: a perfectly valid refuter response must
        not be judged unusable merely because it is not a review.
        """
        ...

    def effort_map(self) -> dict[str, str]:
        """Canonical effort (`config.EFFORTS`) -> this CLI's spelling.

        A canonical value absent from the map is a **loud** `ValueError` in
        `build_cmd`, never a silently dropped flag: quietly reviewing at the
        CLI's default effort is exactly the kind of unnoticed downgrade the
        explicit-model rule exists to prevent.
        """
        ...
