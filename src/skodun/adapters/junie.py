"""The junie adapter: confined invocation, envelope parse, run classification.

Junie is an agentic coding CLI. This adapter never points it at the real
worktree: `build_cmd` stages an empty capsule and returns an argv that runs
`skodun.adapters.junie_runner`, which applies macOS Seatbelt confinement,
sanitizes the environment, and only then prints a REVIEW_CONTRACT-shaped
payload on stdout. Off macOS the runner refuses before inference.

Classification never reads model-authored prose for a verdict. `parse` and
`classify` never raise on garbage.

One split is worth stating here because it decides whether a review survives a
broken install: a refusal from the OUTER RUNNER (no envelope, an envelope that
will not read or normalize, a stderr capture that escaped the capsule) is
`unavailable`/`harness`, while evidence that JUNIE's own answer was cut short
is `degraded`. The chain advances only on `unavailable`, so the first class
lets a fallback provider serve the review and the second correctly keeps it
with the reviewer that answered badly.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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

# Canonical effort -> junie --effort. Pass-through for the three levels the
# CLI documents (`junie --help`: low, medium, high). `"max"` is absent on
# purpose: inventing a mapping is an unnoticed downgrade.
_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}
_EFFORT_OFF = (None, "none")

# --- harness tells (stderr of the outer runner) --------------------------
# THE OUTER RUNNER's own refusals: the envelope file is missing, or it exists
# and could not be read, JSON-decoded or normalized, or the confined stderr
# capture escaped the capsule. Every one of them is a fault in skodun's own
# harness -- junie was never asked a question it could answer badly.
#
# These used to be `degraded`, and that classification cost a whole review.
# `chain.run_chain` advances a fallback chain ONLY on `unavailable`; `degraded`
# means "this reviewer answered badly", which correctly STOPS the chain rather
# than asking a second provider to re-answer a question the first one did
# answer. So a structurally broken adapter spent both of its entry's attempts
# in ~1.5s each and returned `trustworthy=false findings=0` with three other
# providers configured and idle (issue #92). An envelope that cannot be read is
# not a review outcome, and the entry that could not produce one has not served
# at all -- which is exactly what `unavailable` means.
_HARNESS_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"envelope refused",
    b"did not produce a json output envelope",
    b"stderr capture escaped",
)

# --- degradation tells (stderr of the outer runner) ----------------------
# What is left is evidence about the REVIEW rather than about the harness: an
# answer that may be incomplete. `result is missing` is deliberately NOT here
# any more -- the only thing that emits that phrase is `normalize_envelope`,
# whose exception the runner reports as `envelope refused: junie result is
# missing`, so it is matched above and an entry here could only ever be dead
# weight (see `test_every_degraded_signal_is_individually_load_bearing`).
_DEGRADED_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"truncated",
)

# --- unavailability tells -----------------------------------------------
_AUTH_SIGNALS: tuple[bytes, ...] = (
    b"authentication",
    b"unauthorized",
    b"invalid api key",
    b"not logged in",
    b"auth token",
)

_MODEL_SIGNALS: tuple[bytes, ...] = (
    b"unknown model",
    b"model not found",
    b"unsupported model",
    b"invalid model",
)

# Binary-sourced / documented-style quota wording. Provenance is recorded in
# tests/fixtures/adapters/junie/README.md. Prefer specific phrases over bare
# "quota" so a review that discusses quotas cannot take the provider down via
# model prose — these match stderr only.
_QUOTA_SIGNALS: tuple[bytes, ...] = (
    b"quota exceeded",
    b"rate limit",
    b"rate_limit",
    b"too many requests",
    b"payment required",
    b"out of credits",
    b"usage limit",
)

_PLATFORM_SIGNALS: tuple[bytes, ...] = (
    b"requires macos",
    b"sandbox-exec is unavailable",
    b"confinement requires macos",
)

_INVOCATION_SIGNALS: tuple[bytes, ...] = (
    b"binary not found",
    b"not an executable absolute path",
    b"managed junie shim",
    b"could not write sandbox profile",
    b"spawn failed",
    # Outer runner is `python -I -m skodun.adapters.junie_runner`. When clients
    # run skodun only via PYTHONPATH (no install), -I hides PYTHONPATH and the
    # child fails immediately — must not look like a clean empty parse.
    b"no module named 'skodun'",
    b"no module named skodun",
    b"modulenotfounderror",
)


def resolve_junie_bin() -> str:
    """`SKODUN_JUNIE_BIN` -> `junie` on PATH.

    An exported-but-EMPTY variable is treated as unset — `""` as argv material
    is not a path anyone meant.
    """
    return os.environ.get("SKODUN_JUNIE_BIN") or "junie"


def _import_root_for_isolated_runner() -> str:
    """Directory to put on ``sys.path`` so ``import skodun`` works under ``-I``.

    ``…/src`` for a source checkout / editable layout; the parent of the
    ``skodun`` package for a normal install (site-packages).
    """
    # adapters/junie.py → package dir → parent on sys.path
    pkg_dir = Path(__file__).resolve().parents[1]
    return str(pkg_dir.parent)


def _isolated_runner_argv(runner_flags: list[str]) -> list[str]:
    """``python -I`` + bootstrap that imports ``junie_runner`` with a fixed path.

    Avoids plain ``-m skodun.adapters.junie_runner`` failing when the parent
    process only had skodun via ``PYTHONPATH`` (which ``-I`` discards).
    """
    root = _import_root_for_isolated_runner()
    # sys.argv after -c is ['-c', ...flags]; rewrite before runpy so argparse
    # in junie_runner sees a normal module argv.
    bootstrap = (
        "import runpy, sys; "
        f"sys.path.insert(0, {root!r}); "
        "sys.argv = ['skodun.adapters.junie_runner'] + sys.argv[1:]; "
        "runpy.run_module('skodun.adapters.junie_runner', run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", bootstrap, *runner_flags]


class JunieAdapter:
    """Invocation + output interpretation for the JetBrains junie CLI."""

    name = "junie"
    provider = "junie"
    # The outer runner opens the capsule prompt itself; the chain must not
    # also open the original prompt as the child's stdin.
    stdin_from_prompt_file = False

    def resolve_binary(self) -> str:
        return resolve_junie_bin()

    def effort_map(self) -> dict[str, str]:
        return dict(_EFFORT_MAP)

    def prompt_limit(self) -> int | None:
        """No ceiling: the prompt travels as a file into the capsule, then stdin."""
        return None

    def routing_eligibility(self) -> tuple[bool, str]:
        """Cheap auto-route check that never offers an unconfined fallback."""
        if sys.platform != "darwin":
            return False, "junie confinement requires macOS"
        try:
            from .junie_sanitized import resolve_sandbox_exec
            resolve_sandbox_exec()
        except Exception as exc:  # noqa: BLE001 - routing must stay best effort
            return False, str(exc)
        binary = self.resolve_binary()
        present = (os.path.exists(binary) if os.path.sep in binary
                   else shutil.which(binary) is not None)
        if not present:
            return False, f"junie binary is unavailable: {binary}"
        return True, ""

    def build_cmd(
        self,
        prompt_file: Path,
        r: Reviewer,
        d: Defaults,
        cwd: Path,
        contract: OutputContract = REVIEW_CONTRACT,
    ) -> list[str]:
        """Argv for the outer confined runner. Does not put the prompt on argv.

        The prompt path is passed as a flag value (a path string), never as the
        prompt body. Capsule staging and Seatbelt work happen inside the
        runner process so a failure there classifies rather than crashing the
        chain mid-build.
        """
        cli_effort = None
        if r.effort not in _EFFORT_OFF:
            mapping = self.effort_map()
            if r.effort not in mapping:
                raise ValueError(
                    f"adapter {self.name!r} has no CLI value for effort "
                    f"{r.effort!r} (known: {sorted(mapping)})"
                )
            cli_effort = mapping[r.effort]

        if not r.model:
            raise ValueError(f"adapter {self.name!r}: model is required")

        timeout_ms = max(1, int(d.timeout_sec) * 1000)
        # Isolated interpreter (-I) so user-site / ambient PYTHONPATH cannot
        # leak into the confined runner. -I also *ignores* PYTHONPATH, so a
        # client that only set PYTHONPATH=src (no install) would fail with
        # ModuleNotFoundError unless we re-inject the import root that loaded
        # this adapter. When skodun is properly installed, that root is already
        # on the isolated interpreter's site path and the inject is harmless.
        runner_flags = [
            "--prompt",
            str(prompt_file),
            "--binary",
            self.resolve_binary(),
            "--model",
            r.model,
            "--timeout-ms",
            str(timeout_ms),
            "--schema",
            contract.json_schema,
        ]
        if cli_effort is not None:
            runner_flags.extend(["--effort", cli_effort])
        return _isolated_runner_argv(runner_flags)

    def parse(
        self,
        stdout: bytes,
        stderr: bytes,
        contract: OutputContract = REVIEW_CONTRACT,
    ) -> ParseResult:
        payload = _extract(stdout, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        degraded, reason = _detect_degraded(stderr, parse_ok=parse_ok)
        review = parse_ok and contract is REVIEW_CONTRACT
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if review else [],
            summary=payload["summary"] if review else "",
            stop_reason=None,
            degraded=degraded,
            degraded_reason=reason,
            payload=payload if parse_ok else None,
        )

    def classify(
        self,
        rc: int,
        stdout: bytes,
        stderr: bytes,
        contract: OutputContract = REVIEW_CONTRACT,
    ) -> ClassifyResult:
        if rc == UNAVAILABLE_RC:
            return ClassifyResult(
                "unavailable",
                "binary",
                f"binary not found (rc {UNAVAILABLE_RC})",
            )
        payload = _extract(stdout, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        if not parse_ok:
            diagnostics = stderr.lower()
            for category, signals in (
                ("auth", _AUTH_SIGNALS),
                ("model", _MODEL_SIGNALS),
                ("quota", _QUOTA_SIGNALS),
                ("other", _PLATFORM_SIGNALS + _INVOCATION_SIGNALS),
            ):
                for sig in signals:
                    if sig in diagnostics:
                        return ClassifyResult(
                            "unavailable",
                            category,
                            f"{category} failure in the run's diagnostics "
                            f"({sig.decode()}) with no usable "
                            f"{contract.name} payload",
                        )
            # AFTER the four above, and the order is the policy: a provider
            # that is out of quota (or unauthenticated, or asked for a model it
            # does not have) may ALSO fail to write an envelope, and the record
            # has to name the cause rather than the symptom -- `quota` above
            # all, because it is the one category cached provider-wide.
            #
            # The detail quotes the runner's own LINE rather than the signal it
            # matched. That line is `junie envelope refused: {e}`, where `{e}`
            # is the exception `normalize_envelope` or the JSON decoder raised
            # -- the root cause, which nothing used to persist: the rendered
            # `degraded_reason` kept only the signal name, no worker log was
            # written for a run that never reached the model, and diagnosing
            # #92 meant inferring from attempt timings and reproducing by hand.
            for sig in _HARNESS_STDERR_SIGNALS:
                if sig in diagnostics:
                    return ClassifyResult(
                        "unavailable",
                        "harness",
                        f"skodun's junie harness produced no {contract.name} "
                        f"envelope: {_evidence(stderr, sig)}",
                    )
        # Degradation (a truncated answer) is not unavailability — check before
        # inventing an empty-rc unavailable.
        degraded, reason = _detect_degraded(stderr, parse_ok=parse_ok)
        if degraded:
            return ClassifyResult("degraded", "", reason)
        if (not parse_ok and rc != 0
                and not (stdout or b"").strip()):
            # Hard fail with silence (e.g. old ModuleNotFoundError paths with
            # empty stderr capture): hop/fail closed, do not look "ok".
            return ClassifyResult(
                "unavailable",
                "other",
                f"junie runner exited rc={rc} with no review payload "
                f"(install skodun into this Python, or see "
                f"examples/fragments/review-troubleshooting.md)",
            )
        return ClassifyResult("ok", "", "")


def _extract(stdout: bytes, eligible) -> dict | None:
    """First eligible payload from the outer runner's stdout. Never raises."""
    text = stdout.decode("utf-8", "replace")
    stripped = text.lstrip()
    if stripped:
        try:
            root, _ = json.JSONDecoder().raw_decode(stripped, 0)
        except _DECODE_FAILURES:
            root = None
        if _ask(eligible, root):
            return root
    return _first_eligible_object(text, eligible)


#: How much of a runner line `_evidence` keeps. Long enough for a decoder
#: message with a line/column and a path, short enough that a classification
#: detail stays a sentence in a record and on the progress stream.
_EVIDENCE_MAX = 240


def _evidence(stderr: bytes, sig: bytes) -> str:
    """The runner's own LINE carrying `sig`, made safe to persist and print.

    The search runs per LINE of the original text, each lower-cased for the
    comparison only, so the line that is returned keeps its real casing and
    paths. Deliberately not offset arithmetic over one lower-cased copy:
    `str.lower()` is not length-preserving in Unicode (`"İ".lower()` is two
    code points), so an offset taken from the lowered text and applied to the
    original slices the wrong characters -- one such character anywhere earlier
    in stderr and the quoted cause comes out mangled.

    Sanitized rather than passed through, because this string reaches two
    places that neither quote nor escape it: `attempts[].classification.detail`
    in the artifact, and `chain`'s progress line on stderr. Control characters
    are dropped (an ESC sequence would rewrite rows already printed on an
    operator's terminal, and a newline would forge a second progress line), and
    the result is capped. Falls back to the signal itself if anything about the
    line is unusable -- rendering must never be what fails a classification.
    """
    needle = sig.decode()
    line = ""
    for candidate in stderr.decode("utf-8", "replace").splitlines():
        if needle in candidate.lower():
            line = candidate
            break
    cleaned = "".join(c for c in line if c.isprintable()).strip()
    if not cleaned:
        return sig.decode()
    if len(cleaned) > _EVIDENCE_MAX:
        cleaned = cleaned[:_EVIDENCE_MAX] + "..."
    return cleaned


def _detect_degraded(stderr: bytes, *, parse_ok: bool) -> tuple[bool, str]:
    err_lower = stderr.lower()
    for sig in _DEGRADED_STDERR_SIGNALS:
        if sig in err_lower:
            # NOT "harness failure": every signal still in this table is junie
            # saying its OWN answer was cut short, and the harness refusals
            # that used to share this wording are `unavailable`/`harness` now.
            # An operator reading `degraded_reason` should be sent to the model
            # run, not to skodun's wrapper.
            return True, (
                f"junie reported its output was cut short in stderr "
                f"({sig.decode()}); the review may be incomplete and an empty "
                f"result cannot be trusted"
            )
    # No completion signal equivalent to EndTurn/SUCCESS. A usable payload is
    # accepted as non-degraded; absence of payload with clean stderr is
    # parse_ok=False without inventing degradation from silence.
    del parse_ok  # documented; not used for inference-from-absence
    return False, ""


if TYPE_CHECKING:  # pragma: no cover
    from .base import Adapter

    _CONFORMS: type[Adapter] = JunieAdapter
