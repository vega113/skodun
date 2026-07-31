"""The agy adapter: build the invocation, read the envelope, judge the run.

Written against agy 1.1.8 and probed flag by flag before a line of it existed;
where this file and the plan disagreed, the installed binary won. Four things
about that CLI shape the whole module, and the first is a deviation from a
global rule rather than a detail:

* **There is no way to hand this CLI a prompt FILE.** It has no `--prompt-file`
  flag, and it does not read stdin in print mode — probed twice: piping text in
  alongside `--print "<question>"` left the model answering `NONE` (it never
  saw the piped bytes), and `--print -` sent the literal one-character string
  `-`. The only channel the binary offers is the `--print` argv value. So
  `build_cmd` reads the prompt file and puts its TEXT in the argv. No shell is
  involved — the argv is a list handed to `subprocess` — so nothing is
  shell-interpolated and no quoting rule can be got wrong; what changes is that
  the bytes are in the process's argument vector, which has THREE
  preconditions the file (grok) and stdin (codex) routes never had: the text
  must decode as UTF-8 (a `str` is what argv needs), it must carry no embedded
  NUL (`subprocess.Popen` rejects one anywhere in argv), and it must fit under
  the kernel's per-argument cap. All three are guarded in `build_cmd`, in that
  order, and all three fail loudly with a `ValueError` rather than a
  `UnicodeDecodeError` or an unguarded `Popen` `ValueError` escaping as an
  unexpected exception. The first two STOP the chain (`chain.run_chain`:
  "could not build the invocation") because they are facts about the repo,
  which no other provider would fix. The THIRD raises `base.PromptTooLarge`
  and ADVANCES the chain instead: a prompt that does not fit is a statement
  about this provider's capacity, which is what a fallback chain is for, and
  an exhausted chain is still a failure. The size guard is
  `MAX_PROMPT_ARG_BYTES` below, and `prompt_limit()` is the same number
  declared upward so the planner sizes for it rather than discovering it here.
  (b) is a real, stated cost of this provider
  regardless of the guards: a reviewer's prompt contains the diff under
  review, and on a shared machine `ps` will show it for the life of the
  attempt. Providers whose CLI accepts a file (grok) or stdin (codex) do not
  pay any of this.

* **stdout is ONE JSON envelope**, written at the end of the run:
  `{"conversation_id", "status", "response", "structured_output"?, "error"?,
  "json_schema"?, "duration_seconds", "num_turns", "usage"}`. `structured_output`
  is the copy the CLI itself validated against `--json-schema` and is
  authoritative; `response` is the model's raw text, which the probe saw carry
  a ```json fence and, once, the payload twice at two indentations. Extraction
  is therefore the same three-level fallback the grok adapter uses.

* **`--json-schema` takes the contract's schema VERBATIM.** Unlike codex's
  OpenAI structured-outputs mode, nothing has to be projected: the probe handed
  `contract.json_schema` over byte-for-byte and got a `structured_output` back
  that validated. The flag accepts "a JSON schema string or path to a schema
  file"; this adapter passes the STRING. A sidecar file would buy nothing here
  (there is no projection to write) and would cost `build_cmd` its freedom from
  side effects and add a stale-file hazard when two attempts share a prompt
  directory — the exact hazard codex has to defend against by always
  overwriting.

* **`--effort` has exactly three levels**, `low|medium|high`, and it is
  entangled with the model id. `agy models` lists both base ids
  (`gemini-3.6-flash`) and effort-suffixed ones (`gemini-3.6-flash-low`); a
  base id REQUIRES `--effort`, and a suffixed id REFUSES any `--effort` that
  disagrees with its suffix. All of those refusals are rc 1 with an `error`
  opening `invalid model selection`, which is why one signal classifies the
  unknown id, the missing effort and the conflicting effort alike. Canonical
  `max` has no level and is refused loudly by `build_cmd`; canonical `none` is
  the opt-out and passes no flag, exactly as it does for grok.

  This is a configuration footgun, not just an implementation detail: `agy
  models` (the command a config author actually runs) prints ONLY the
  effort-suffixed ids, and picking one of THOSE alongside any non-matching
  `effort =` in a `[[reviewer]]` entry is a guaranteed rc-1 on every attempt,
  every time, for this provider. Only two shapes work — a suffixed id with
  `effort` unset or `"none"`, or a base id (not in `agy models`' own listing)
  with a matching `effort`. It fails closed and loudly (see above), so no
  review is ever silently weakened by it, but a config author who never reads
  this docstring can still burn every attempt on it before noticing.
  This two-shapes rule is restated in plain config-author language next
  to the `google` section of `examples/multi-provider.toml`, so the
  footgun is visible where a config is actually being written, not only
  here.

Classification never reads the model's own words. It looks at the exit code, at
stderr, and at the envelope's `status` and `error` — both written by the
harness. `response` and `structured_output` are never consulted for a verdict,
so a review that quotes an auth error or discusses a dropped stream cannot take
the provider down (conformance rule 6).

One captured failure shape drove the degradation table more than any other: a
run whose tool call was auto-denied (headless mode cannot prompt for
permission) exits **0** with **`status: SUCCESS`** and an **empty `response`**,
and says so only on stderr. That is a silent-false-all-clear generator, and
`no output produced` — the CLI's own words — is what stops it.
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
    PromptTooLarge,
    _ask,
    _DECODE_FAILURES,
    _first_eligible_object,
)

#: The largest prompt this adapter will place in an argv element.
#:
#: Linux caps a SINGLE argument at `MAX_ARG_STRLEN` = 32 pages = 131072 bytes,
#: regardless of how much room `ARG_MAX` leaves overall; macOS caps the whole
#: argv (`kern.argmax`, 1 MiB on the probe machine). The Linux per-argument cap
#: is the smaller and the less obvious of the two, so it is the one this guard
#: uses everywhere: a reviewer that works on one developer's machine and dies
#: on CI is worse than one that refuses the same prompt on both. The slack
#: leaves room for the flags and the inline schema that share the vector.
#:
#: `config.Defaults.max_diff_bytes` is 400_000, so a large diff COULD exceed
#: this -- but the planner no longer lets it: `budget.prompt_budget` reads this
#: number through `prompt_limit()` and sizes every prompt and batch for the
#: reviewer that will actually run, so a chain headed by this adapter is
#: budgeted to what it can carry rather than to the global envelope.
#:
#: The guard in `build_cmd` remains, because sizing is an estimate and a chain
#: can span providers: a prompt sized for a file-fed head may still reach an
#: argv-bound fallback. It fails closed, and it now fails SIDEWAYS first --
#: `PromptTooLarge` classifies `unavailable` and `chain.run_chain` advances to
#: the next entry, and only an exhausted chain is a failure. The alternative it
#: has always prevented is an `OSError` out of `subprocess`, which reaches the
#: gate as an unexpected exception instead of as a reviewer that could not
#: serve.
MAX_PROMPT_ARG_BYTES = 128 * 1024 - 8 * 1024

# The `status` value that means "the run reached its end normally". An
# ALLOWLIST, deliberately: a false positive costs one re-review, a false
# negative is the silent false all-clear this module exists to prevent. The
# binary carries `SUCCESS`, `ERROR`, `TIMEOUT` and `CANCELLED`; only the first
# two were observed in print mode, and the table does not depend on having seen
# them all.
_STATUS_OK = "SUCCESS"

# --- degradation tells (stderr only) ---------------------------------------
#
# Matched case-insensitively on stderr BYTES and on nothing else. Every string
# here is present verbatim in the installed agy binary, so these are the CLI's
# own words rather than guesses about what it might say. stdout is never
# searched for them: a review of retry code would otherwise flag itself.
_DEGRADED_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"no output produced",
    b"stream error",
    b"was interrupted",
    b"context deadline exceeded",
)

# --- unavailability tells (classify only; never inputs to `degraded`) ------
#
# Consulted only when the run carried no usable payload — the Phase 1
# non-signal rule: noisy diagnostics alongside a healthy answer are noise, not
# a verdict.
#
# Checked in the order below, and the order is a safety decision rather than
# alphabetical: `quota` is the ONLY provider-wide-cacheable category, so a
# false `quota` takes a working provider out of every later chain in the run,
# while a false `auth`/`model`/`other` costs one attempt. Anything that also
# looks like a more specific, attempt-local failure is therefore reported as
# that instead.
_AUTH_SIGNALS: tuple[bytes, ...] = (
    b"authentication required",
    b"authentication failed",
    b"authentication timed out",
    b"please visit the url to log in",
    b"not logged in",
    b"unauthorized",
    b"invalid api key",
    b"sign in again",
)

# `invalid model selection` is the CLI's umbrella prefix for EVERY model or
# effort rejection — unknown id, missing `--effort`, an `--effort` the model
# has no level for, an `--effort` that conflicts with an effort-suffixed id.
# All of them are attempt-local statements about this reviewer's configuration.
_MODEL_SIGNALS: tuple[bytes, ...] = (
    b"invalid model selection",
    b"is not recognized as a known model",
    b"unknown model",
    b"no such model",
    b"model not found",
    b"unsupported model",
)

# No bare `429` here, deliberately: diagnostics carry byte offsets and request
# counters, and a numeric substring match would mint provider-wide quota
# outages out of arithmetic.
#
# Every entry below is present verbatim in the installed agy 1.1.8 binary
# (`strings -a "$(which agy)" | grep -ic -- '<phrase>'`), same standard as
# every other signal table in this module. `"usage limit"` and `"insufficient
# credit"` were dropped from an earlier draft that listed them as if they were
# observed: a direct grep against the binary found zero hits for either, so
# the earlier report's claim that this table was "built from phrases present
# in it" was wrong for those two specifically. The direction was harmless
# (over-broad quota matching is the dangerous direction, and absent strings
# can never over-match), but a signal that can never fire is dead weight and
# its stated provenance was false, so it is gone rather than relabelled
# speculative.
# The last three were added by the Task 14 audit, after a live capture showed
# grok's table missing real budget exhaustion. This table's narrowness — it
# matches `quota exceeded`, never a bare `quota`, because the Google protos
# this binary embeds are dense with the word — is correct and is kept, but it
# is also what let the binary's OWN sentence fall through every table to `ok`.
# Provenance, per entry:
#
# * `exhausted your quota` — verbatim in the installed agy 1.1.8 binary
#   ("You have exhausted your quota on this model."). Matched by neither
#   `quota exceeded` nor `resource_exhausted`. OBSERVED IN THE BINARY, not in
#   a live failure: the balance to exhaust was xAI's, not Google's.
# * `quota exhausted` — verbatim in the installed binary, as the UI's own
#   status label ("Quota available" / "Quota exhausted"). Same caveat.
# * `payment required` — the IANA-registered reason phrase for HTTP 402. It is
#   present in this binary only inside Go's `net/http` status-text table, so it
#   is reachable wherever the CLI renders a status line, and it is the exact
#   wording xAI's CLI was live-captured emitting. SPECULATIVE for THIS
#   provider: no agy run has been observed emitting it.
#
# A bare `exhausted` was considered and rejected: this binary exhausts
# iterators, counters, read limits and retry budgets, and reading any of those
# as a provider outage is the false-`quota` direction that costs a healthy
# provider every later chain in the run. `balance exhausted` — grok's other new
# signal — is likewise absent: zero occurrences in this binary, and a signal
# that can never fire is the dead weight this comment's older half records.
_QUOTA_SIGNALS: tuple[bytes, ...] = (
    b"quota exceeded",
    b"resource_exhausted",
    b"resourceexhausted",
    b"rate limit",
    b"rate_limit",
    b"ratelimit",
    b"too many requests",
    b"out of credits",
    b"exhausted your quota",
    b"quota exhausted",
    b"payment required",
)

# The CLI refusing our own argv. Attempt-local and caches nothing, hence
# `other` — but it must be classified as SOMETHING: a rejected `--json-schema`
# exits 1 with empty stdout, which the tables above would otherwise read as a
# provider that simply said nothing.
_INVOCATION_SIGNALS: tuple[bytes, ...] = (
    b"invalid --json-schema",
    b"empty prompt",
    # PLURAL, verified against the installed binary rather than assumed:
    # `agy --nonexistent-flag` -> rc 2, stderr `flags provided but not
    # defined: -nonexistent-flag`. The installed CLI's wrapper always emits
    # the plural, even for exactly one undefined flag. Go stdlib's `flag`
    # package spells this singular ("flag provided but not defined:"), and
    # that spelling IS present in the binary too, but it is unreachable
    # through agy's own argument parser -- only the wrapper's plural ever
    # reaches stderr. A singular entry here would never match a real run,
    # so a rejected invocation (rc 2, no auth/model/quota wording) would fall
    # through every table unclassified and land on plain `ok` with no
    # explanation -- precisely the silent all-clear this table exists to
    # prevent.
    b"flags provided but not defined",
)

# Canonical effort (`config.EFFORTS`) -> the CLI's `--effort`. Pass-through for
# the three levels it has. `"max"` is ABSENT on purpose and is not an oversight
# the next reader should fix: `agy --effort max` is refused live with `invalid
# --effort "max" (valid: low, medium, high)`, so `build_cmd` raises rather than
# quietly sending `high` and reporting the result as `max`. `"none"` is absent
# for the different reason grok's is: it is the user's explicit opt-out,
# handled before any lookup happens, not a CLI value — the CLI rejects the
# literal string `none` too.
_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# `effort` values that mean "do not pass the flag at all". `None` is unset;
# `"none"` is the user's explicit opt-out. Those are the only two: `config`
# rejects every other spelling before a `Reviewer` can reach this module.
_EFFORT_OFF = (None, "none")


def resolve_agy_bin() -> str:
    """`SKODUN_AGY_BIN` -> `agy` on PATH.

    No `~`-relative default, unlike grok: agy is installed on PATH by its own
    installer and `~/.gemini` holds credentials, settings and conversation
    state rather than the executable. An exported-but-EMPTY variable is treated
    as unset — `""` as argv[0] is not a path anyone meant.
    """
    return os.environ.get("SKODUN_AGY_BIN") or "agy"


class AgyAdapter:
    """Invocation + output interpretation for the Google agy CLI."""

    name = "agy"
    provider = "google"
    # agy ignores stdin in print mode (probed twice; see the module docstring),
    # so the runner must NOT open the prompt file as the child's stdin. The
    # prompt reaches the CLI as the `--print` value instead.
    stdin_from_prompt_file = False

    def resolve_binary(self) -> str:
        """The protocol's spelling of `resolve_agy_bin`."""
        return resolve_agy_bin()

    def effort_map(self) -> dict[str, str]:
        """Canonical effort -> `--effort`. A copy, so that a caller inspecting
        the table cannot mutate this adapter's behaviour."""
        return dict(_EFFORT_MAP)

    def prompt_limit(self) -> int | None:
        """This CLI's argv ceiling — the SAME constant `build_cmd` enforces.

        Returned rather than re-spelled: a planner sizing batches against a
        number that has drifted from the guard's is a planner cutting batches
        this adapter will refuse (or, worse, needlessly small ones).
        """
        return MAX_PROMPT_ARG_BYTES

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path,
                  contract: OutputContract = REVIEW_CONTRACT) -> list[str]:
        """The full argv for one attempt. Writes nothing.

        Every flag here was accepted by agy 1.1.8 during the probe. The
        load-bearing ones:

        * `--print <text>` — the only prompt channel this CLI has. See the
          module docstring; the size guard below is the price.
        * `--output-format json` — one envelope on stdout instead of prose.
        * `--json-schema <string>` — the contract's schema, verbatim.
        * `--model` — always explicit, never inherited from the CLI's own
          settings file.
        * `--sandbox` — "run in a sandbox with terminal restrictions enabled".
          Paired with the ABSENCE of `--dangerously-skip-permissions`: a tool
          call that needs permission is then auto-denied, because a reviewer
          must not be able to execute anything.
        * `--print-timeout` — set to `d.timeout_sec`, the SAME deadline the
          watchdog (`runner.py`) enforces, and the watchdog starts counting
          first and always wins the race in practice: it is armed before
          `Popen` even returns, while the CLI's own `--print-timeout` clock
          starts later, after its own startup work. So this flag is NOT what
          gives a stalled run a chance to self-report before the watchdog
          kills it — at equal deadlines with a later start, it essentially
          never fires first. What it actually buys is real and different:
          agy's default `--print-timeout` is `5m0s` (300s, `agy --help`), well
          under `Defaults.timeout_sec` (420s), so WITHOUT this flag the CLI
          would self-terminate at five minutes while skodun still had two
          minutes of budget left. Passing the runner's own deadline raises
          agy's internal timeout to match skodun's, so the CLI does not give
          up on its own before the watchdog would.

        `cwd` takes no flag: agy has no `--cwd`/`-C`, and the runner already
        spawns the child in that directory. `d.max_turns` and `d.deny_tools`
        have no counterpart either — the sandbox plus auto-denied permissions
        is what stands in for the latter.
        """
        effort = None if r.effort in _EFFORT_OFF else r.effort
        cli_effort = None
        if effort is not None:
            mapping = self.effort_map()
            if effort not in mapping:
                # LOUD, never a dropped flag: silently reviewing at the CLI's
                # own default effort is an unnoticed downgrade, and an
                # unnoticed downgrade is how a weak review passes for a strong
                # one. `max` lands here by design — this CLI has no fourth
                # level, and mapping it down to `high` would report a weaker
                # review as the configured one.
                raise ValueError(
                    f"adapter {self.name!r} has no CLI value for effort "
                    f"{effort!r} (known: {sorted(mapping)})")
            cli_effort = mapping[effort]

        try:
            prompt = prompt_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            # STRICT, not `errors="replace"`. This is the SOURCE side of the
            # pipeline (the diff under review, read back from the file
            # `promptbuild.py` wrote), not the CLI's stdout: a lossy decode
            # here would let the model review bytes that differ from what is
            # actually in the repo -- for a REVIEW tool that is its own
            # hazard, since a finding could point at text that does not
            # exist. Refusing loudly costs a latin-1 repo this one provider;
            # decoding lossily would cost every repo the correctness of what
            # gets reviewed. Reachable on a normal path: a latin-1 source file
            # produces a `git diff` that is not UTF-8 decodable, and grok/codex
            # build fine on the same file because neither of them decodes the
            # prompt text at all (grok passes the path, codex streams the raw
            # bytes on stdin) -- this decode exists only because the argv
            # channel needs a `str`.
            raise ValueError(
                f"adapter {self.name!r}: prompt is not valid UTF-8 ({e}); "
                f"this CLI takes the prompt as an argv string, which must be "
                f"decodable text, and reviewing a lossy re-decode would risk "
                f"the model commenting on bytes that are not actually in the "
                f"diff. Review this change with another provider, or make the "
                f"source file UTF-8") from e

        if "\x00" in prompt:
            # `subprocess.Popen` raises `ValueError: embedded null byte` for a
            # NUL anywhere in argv -- and that raise happens in `runner.py`,
            # OUTSIDE this function, where `pipeline._run_chain` catches only
            # `FileNotFoundError` around the call. Left unguarded here the
            # same `ValueError` still fires, but as an unexpected exception
            # escaping `_run_chain` rather than as a reviewer that could not
            # be invoked. A NUL survives into the diff when it is past git's
            # first-8000-byte binary-file heuristic, so this is reachable on
            # a normal path, not just a synthetic input. Caught in the SAME
            # guard as the size check below, and failing the same way, so
            # every argv precondition this adapter has surfaces as one loud,
            # diagnosable `ValueError` from `build_cmd` instead of three
            # different failure shapes at three different layers.
            raise ValueError(
                f"adapter {self.name!r}: prompt contains an embedded NUL "
                f"byte, which a subprocess argv cannot carry; review this "
                f"change with another provider")

        size = len(prompt.encode("utf-8"))
        if size > MAX_PROMPT_ARG_BYTES:
            # BYTES, not characters: the kernel's per-argument cap counts
            # bytes, so a multibyte prompt that looks short in characters can
            # still be refused by `execve`.
            #
            # `PromptTooLarge`, not a bare `ValueError`, and the difference is
            # the whole point: this is a statement about THIS PROVIDER's
            # capacity, so `chain.run_chain` classifies it `unavailable` and
            # advances to the next entry -- an `agy`-headed chain with a
            # `codex` fallback reviews the change instead of dying on it. The
            # refusals ABOVE (an undecodable prompt, an embedded NUL) stay bare
            # `ValueError`s and stay fatal: those are facts about the repo, and
            # reviewing the same bytes elsewhere would hide them.
            raise PromptTooLarge(
                f"adapter {self.name!r}: prompt is too large to pass on the "
                f"command line ({size} bytes > {MAX_PROMPT_ARG_BYTES}); this "
                f"CLI has no prompt-file flag and ignores stdin, so lower "
                f"`max_diff_bytes` or review this change with another "
                f"provider",
                size=size, limit=MAX_PROMPT_ARG_BYTES)

        cmd = [
            resolve_agy_bin(),
            "--print", prompt,
            "--output-format", "json",
            "--json-schema", contract.json_schema,
            "--model", r.model,
            "--print-timeout", f"{d.timeout_sec}s",
            "--sandbox",
        ]
        if cli_effort is not None:
            cmd += ["--effort", cli_effort]
        return cmd

    def parse(self, stdout: bytes, stderr: bytes,
              contract: OutputContract = REVIEW_CONTRACT) -> ParseResult:
        payload, status, _ = _extract(stdout, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        degraded, reason = _detect_degraded(status, stderr)
        # The `findings`/`summary` projection is REVIEW_CONTRACT's alone. Under
        # any other contract those two stay empty rather than being filled from
        # a foreign payload, so a Phase 1 caller that only knows them can never
        # read a refuter response as a review; such callers get `payload`.
        review = parse_ok and contract is REVIEW_CONTRACT
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if review else [],
            summary=payload["summary"] if review else "",
            stop_reason=status,
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
           read from stderr and from the envelope's harness-authored `error` —
           never from `response` or `structured_output`.
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
        payload, status, error = _extract(stdout, contract.eligible)
        if not _ask(contract.validate, payload):
            diagnostics = stderr.lower() + b"\n" + error
            for category, signals in (("auth", _AUTH_SIGNALS),
                                      ("model", _MODEL_SIGNALS),
                                      ("other", _INVOCATION_SIGNALS),
                                      ("quota", _QUOTA_SIGNALS)):
                for sig in signals:
                    if sig in diagnostics:
                        return ClassifyResult(
                            "unavailable", category,
                            f"{category} failure in the run's diagnostics "
                            f"({sig.decode()}) with no usable "
                            f"{contract.name} payload")
        degraded, reason = _detect_degraded(status, stderr)
        if degraded:
            return ClassifyResult("degraded", "", reason)
        return ClassifyResult("ok", "", "")


# --------------------------------------------------------------------------
# the envelope
# --------------------------------------------------------------------------


def _root_envelope(text: str) -> object | None:
    """The envelope ROOT value, or None if `text` does not open with one.

    `raw_decode` rather than `json.loads`, for the reason the grok adapter
    spells out at length: `loads` raises "Extra data" on ANY trailing byte,
    and losing the root that way would silently skip the `status` check on
    exactly the malformed runs it exists to catch.
    """
    stripped = text.lstrip()
    if not stripped:
        return None
    try:
        root, _ = json.JSONDecoder().raw_decode(stripped, 0)
    except _DECODE_FAILURES:
        return None
    return root


def _extract(
    stdout: bytes,
    eligible: Callable[[object], bool],
) -> tuple[dict | None, str | None, bytes]:
    """`(payload, status, diagnostics)` from one run's stdout. Never raises.

    Three levels for the payload — `structured_output` -> `response` -> a raw
    scan — and `eligible` is the requested contract's candidate predicate,
    applied identically at every level so extraction needs no
    contract-conditionals.

    `structured_output` first because it is the copy the CLI validated against
    `--json-schema`; `response` behind it because a run without a schema (or
    one the CLI could not validate) still carries the model's answer there,
    sometimes inside a ```json fence. The raw scan is the last resort for
    output that is not this CLI's envelope at all.

    `diagnostics` is the harness's own words about why the run went wrong —
    the root `error` string, lowercased — and NOTHING else. Not `response`, not
    `structured_output`: those are the model's words, and a review that quotes
    a 401 must not be able to take the provider down.

    `errors="replace"` rather than a strict decode: in exactly the truncated
    runs that matter the output is cut mid-codepoint, and a
    `UnicodeDecodeError` here would blind the status check on the very runs it
    exists to catch.
    """
    text = stdout.decode("utf-8", "replace")
    root = _root_envelope(text)

    status: str | None = None
    diagnostics = b""
    payload: dict | None = None

    if isinstance(root, dict):
        value = root.get("status")
        if isinstance(value, str) and value:
            status = value
        err = root.get("error")
        if isinstance(err, str):
            diagnostics = err.lower().encode("utf-8", "replace")

        so = root.get("structured_output")
        if _ask(eligible, so):
            payload = so
        else:
            inner = root.get("response")
            if isinstance(inner, str) and inner.strip():
                payload = _first_eligible_object(inner, eligible)

    if payload is None:
        payload = _first_eligible_object(text, eligible)
    return payload, status, diagnostics


# --------------------------------------------------------------------------
# degraded detection
# --------------------------------------------------------------------------


def _detect_degraded(status: str | None,
                     stderr: bytes) -> tuple[bool, str]:
    """Positive evidence that the run was cut short. Never inferred from absence.

    Returns `(degraded, reason)`; `reason` is empty exactly when not degraded.

    The two signals, each on its own:

    1. stderr carrying the CLI's own failure wording. The sharp member is
       `no output produced`, which is the ONLY evidence in the captured
       auto-denied-tool run: rc 0, `status: SUCCESS`, empty `response`. Every
       other axis reports that run as healthy-but-empty.
    2. a terminal `status` that is present and is not `SUCCESS` — the harness
       saying the run ended, and not well. A print-mode timeout lands here
       (`status: ERROR`, `error: timeout waiting for response`), and so does
       any future `TIMEOUT`/`CANCELLED` spelling without a table edit, because
       the check is an allowlist of the one good value rather than a denylist
       of bad ones.

    An ABSENT status is not degraded. A run with no output and clean stderr is
    a failed attempt (`parse_ok=False`), but nothing about it is evidence of
    truncation, and manufacturing a signal from silence is the inference from
    absence this module refuses to make.
    """
    err_lower = stderr.lower()
    for sig in _DEGRADED_STDERR_SIGNALS:
        if sig in err_lower:
            return True, (
                f"harness failure in stderr ({sig.decode()}); the review may "
                f"be truncated and an empty result cannot be trusted")
    if status is not None and status != _STATUS_OK:
        return True, (
            f"the agy run did not complete normally (status: {status}); the "
            f"review was cut off mid-investigation and an empty result cannot "
            f"be trusted")
    return False, ""


if TYPE_CHECKING:  # pragma: no cover - static conformance, no runtime cost
    # `AgyAdapter` explicitly declares that it satisfies the package's
    # `Adapter` protocol. A type checker fails HERE, at the definition site, if
    # any member ever drifts from the protocol — which matters more once four
    # adapters exist and only one of them is exercised by a given config.
    from .base import Adapter

    _CONFORMS: type[Adapter] = AgyAdapter
