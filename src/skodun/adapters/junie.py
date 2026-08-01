"""The junie adapter: confined invocation, envelope parse, run classification.

Junie is an agentic coding CLI. This adapter never points it at the real
worktree: `build_cmd` stages an empty capsule and returns an argv that runs
`skodun.adapters.junie_runner`, which applies macOS Seatbelt confinement,
sanitizes the environment, and only then prints a REVIEW_CONTRACT-shaped
payload on stdout. Off macOS the runner refuses before inference.

Classification never reads model-authored prose for a verdict. `parse` and
`classify` never raise on garbage.
"""

from __future__ import annotations

import json
import os
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

# --- degradation tells (stderr of the outer runner) ----------------------
_DEGRADED_STDERR_SIGNALS: tuple[bytes, ...] = (
    b"envelope refused",
    b"did not produce a json output envelope",
    b"result is missing",
    b"stderr capture escaped",
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
)


def resolve_junie_bin() -> str:
    """`SKODUN_JUNIE_BIN` -> `junie` on PATH.

    An exported-but-EMPTY variable is treated as unset — `""` as argv material
    is not a path anyone meant.
    """
    return os.environ.get("SKODUN_JUNIE_BIN") or "junie"


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
        cmd = [
            sys.executable,
            "-I",
            "-m",
            "skodun.adapters.junie_runner",
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
            cmd.extend(["--effort", cli_effort])
        return cmd

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
        if not _ask(contract.validate, payload):
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
        degraded, reason = _detect_degraded(
            stderr, parse_ok=_ask(contract.validate, payload)
        )
        if degraded:
            return ClassifyResult("degraded", "", reason)
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


def _detect_degraded(stderr: bytes, *, parse_ok: bool) -> tuple[bool, str]:
    err_lower = stderr.lower()
    for sig in _DEGRADED_STDERR_SIGNALS:
        if sig in err_lower:
            return True, (
                f"junie harness failure in stderr ({sig.decode()}); the "
                f"review may be truncated and an empty result cannot be trusted"
            )
    # No completion signal equivalent to EndTurn/SUCCESS. A usable payload is
    # accepted as non-degraded; absence of payload with clean stderr is
    # parse_ok=False without inventing degradation from silence.
    del parse_ok  # documented; not used for inference-from-absence
    return False, ""


if TYPE_CHECKING:  # pragma: no cover
    from .base import Adapter

    _CONFORMS: type[Adapter] = JunieAdapter
