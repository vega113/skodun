"""OpenAI HTTP API adapter (metered): Chat Completions, not the codex CLI.

Provider id: ``openai-api`` (distinct from ``openai`` / Codex subscription CLI).

Requires an API key in the **process environment** (never in repo TOML):

* ``OPENAI_API_KEY`` (standard), or
* ``SKODUN_OPENAI_API_KEY`` (skodun-namespaced alias, useful in MCP ``env`` blocks)

Clients bring their own key via shell export or MCP server ``env`` (BYOK).

Optional:

* ``SKODUN_OPENAI_API_BASE`` — endpoint override (tests / proxies)
* ``SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY`` — USD ceiling **per UTC day**
  (default 10; not lifetime). Alias: ``SKODUN_OPENAI_API_SPEND_LIMIT_USD``
* ``SKODUN_OPENAI_API_INPUT_USD_PER_1M`` / ``_OUTPUT_`` — rate overrides

Any model id the OpenAI API accepts may be set on the reviewer entry.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..config import Defaults, Reviewer
from .openai_api_runner import INCOMPLETE_PREFIX
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

PROVIDER_ID = "openai-api"
API_KEY_ENV = "OPENAI_API_KEY"
#: Alias for hosts that prefer a skodun-prefixed secret name in MCP env.
API_KEY_ENV_ALT = "SKODUN_OPENAI_API_KEY"
USAGE_PREFIX = "SKODUN_API_USAGE "

_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}
_EFFORT_OFF = (None, "none")

_AUTH_SIGNALS = (
    b"auth failure",
    b"invalid api key",
    b"unauthorized",
    b"authentication",
    b"missing api key",
)
_QUOTA_SIGNALS = (
    b"rate limit",
    b"rate_limit",
    b"too many requests",
    b"quota",
    b"insufficient_quota",
    b"billing",
)
_MODEL_SIGNALS = (
    b"model failure",
    b"model_not_found",
    b"does not exist",
    b"invalid model",
    b"unknown model",
)

#: Positive evidence that THIS run's answer stopped early: the runner's own
#: line, written when the API reports a `finish_reason` other than `stop`.
#:
#: One entry, and it is the adapter's own wording rather than a guess at the
#: provider's. What was here before -- `truncated` and `envelope refused` --
#: matched nothing this adapter can emit: `envelope refused` belongs to
#: `junie_runner`, and `openai-api` has no envelope-staging wrapper at all, so
#: the degradation axis was dead on both signals and a truncated answer that
#: happened to parse was recorded as a complete, trustworthy review (#99).
#:
#: Matched at the START OF A LINE, not anywhere in stderr, and that is not
#: fastidiousness: on a non-2xx response the runner puts up to 2000 characters
#: of the PROVIDER'S error body on stderr (`http {code}: {body}`), so this
#: stream is not skodun's alone. An unanchored substring would let an error
#: body that merely mentions the phrase read as a truncation. The runner writes
#: the marker as a line of its own, so anchoring costs nothing and takes the
#: accidental collision off the table.
_DEGRADED_STDERR_SIGNALS: tuple[bytes, ...] = (
    INCOMPLETE_PREFIX.lower().encode("utf-8"),
)


def _detect_degraded(stderr: bytes) -> tuple[bool, str]:
    """Whether this run's answer was cut short, and the reason to record."""
    for line in stderr.lower().splitlines():
        for sig in _DEGRADED_STDERR_SIGNALS:
            if line.strip().startswith(sig):
                return True, (
                    "the openai-api response stopped before the model "
                    "finished; the review may be incomplete and an empty "
                    "result cannot be trusted")
    return False, ""


def parse_usage_line(stderr: bytes) -> dict | None:
    """Extract the machine usage line from runner stderr, if present."""
    text = stderr.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith(USAGE_PREFIX):
            raw = line[len(USAGE_PREFIX):].strip()
            try:
                doc = json.loads(raw)
            except ValueError:
                return None
            if isinstance(doc, dict):
                return doc
    return None


def _import_root() -> str:
    pkg_dir = Path(__file__).resolve().parents[1]
    return str(pkg_dir.parent)


def _isolated_runner_argv(flags: list[str]) -> list[str]:
    root = _import_root()
    bootstrap = (
        "import runpy, sys; "
        f"sys.path.insert(0, {root!r}); "
        "sys.argv = ['skodun.adapters.openai_api_runner'] + sys.argv[1:]; "
        "runpy.run_module('skodun.adapters.openai_api_runner', "
        "run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", bootstrap, *flags]


class OpenAIAPIAdapter:
    """Metered OpenAI Chat Completions adapter."""

    name = "openai-api"
    provider = PROVIDER_ID
    stdin_from_prompt_file = False

    def resolve_binary(self) -> str:
        # HTTP has no CLI binary; chain's "binary present" check uses existence
        # of this path. The runner is the current interpreter.
        return sys.executable

    def effort_map(self) -> dict[str, str]:
        return dict(_EFFORT_MAP)

    def prompt_limit(self) -> int | None:
        return None

    def build_cmd(
            self,
            prompt_file: Path,
            r: Reviewer,
            d: Defaults,
            cwd: Path,
            contract: OutputContract = REVIEW_CONTRACT,
    ) -> list[str]:
        if not r.model:
            raise ValueError(f"adapter {self.name!r}: model is required")
        # Missing OPENAI_API_KEY is left to the runner so classify → unavailable
        # and the chain can hop (same as a missing remote model), not a fatal
        # build_cmd config error for the whole review.
        timeout_ms = max(1, int(d.timeout_sec) * 1000)
        flags = [
            "--prompt", str(prompt_file),
            "--model", r.model,
            "--schema", contract.json_schema,
            "--timeout-ms", str(timeout_ms),
            "--api-key-env", API_KEY_ENV,
        ]
        if r.effort not in _EFFORT_OFF:
            mapping = self.effort_map()
            if r.effort not in mapping:
                raise ValueError(
                    f"adapter {self.name!r} has no value for effort "
                    f"{r.effort!r} (known: {sorted(mapping)})")
            flags.extend(["--effort", mapping[r.effort]])
        return _isolated_runner_argv(flags)

    def parse(
            self,
            stdout: bytes,
            stderr: bytes,
            contract: OutputContract = REVIEW_CONTRACT,
    ) -> ParseResult:
        payload = _extract(stdout, contract.eligible)
        parse_ok = _ask(contract.validate, payload)
        degraded, degraded_reason = _detect_degraded(stderr)
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"])
            if parse_ok and contract is REVIEW_CONTRACT else [],
            summary=payload["summary"]
            if parse_ok and contract is REVIEW_CONTRACT else "",
            stop_reason=None,
            # NOT hardcoded any more. A `finish_reason: "length"` answer that
            # happens to close its JSON cleanly parses fine, so `parse_ok`
            # alone cannot catch it -- this is the axis that keeps a review the
            # model stopped writing from being recorded as a complete one.
            degraded=degraded,
            degraded_reason=degraded_reason,
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
                "unavailable", "binary",
                f"binary not found (rc {UNAVAILABLE_RC})")
        payload = _extract(stdout, contract.eligible)
        usable = _ask(contract.validate, payload)
        if not usable:
            diagnostics = stderr.lower()
            for category, signals in (
                ("auth", _AUTH_SIGNALS),
                ("quota", _QUOTA_SIGNALS),
                ("model", _MODEL_SIGNALS),
            ):
                for sig in signals:
                    if sig in diagnostics:
                        return ClassifyResult(
                            "unavailable", category,
                            f"{category} failure in the run's diagnostics "
                            f"({sig.decode()}) with no usable "
                            f"{contract.name} payload")
        # AFTER the unavailability tables, so a 429 whose error body happens to
        # carry the truncation marker keeps its `quota` verdict: `quota` is the
        # one category cached provider-wide and the one a fallback chain hops
        # on, and reading it as `degraded` would stop the chain on a provider
        # that is simply out of budget.
        #
        # But BEFORE the usable-payload return below, which is the whole point:
        # a payload that validates still cannot be trusted when the API says it
        # stopped writing early, and that is the one truncation `parse_ok`
        # cannot see. A valid payload means a 200, so there is no error body on
        # stderr to collide with on that path.
        degraded, reason = _detect_degraded(stderr)
        if degraded:
            return ClassifyResult("degraded", "", reason)
        # No diagnostic signal: ill-formed payload is parse_ok=False, not a
        # provider outage (model text must not take the provider offline).
        return ClassifyResult("ok", "", "")


def _extract(stdout: bytes, eligible) -> dict | None:
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
