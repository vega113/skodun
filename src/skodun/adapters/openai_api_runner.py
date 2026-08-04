"""HTTP runner for provider ``openai-api``: Chat Completions → contract JSON.

Spawned like the junie outer runner (``python -I -c`` bootstrap). Reads the
prompt file, calls OpenAI's HTTP API with the **client's** API key from the
process environment (bring-your-own-key), writes a contract-shaped JSON object
on stdout, and emits a single machine line on stderr for usage/cost accounting:

``SKODUN_API_USAGE {json}``

**API key (required, never from TOML):**

* ``OPENAI_API_KEY`` (preferred), or
* ``SKODUN_OPENAI_API_KEY`` (alias for MCP ``env`` blocks)

CLI: ``export OPENAI_API_KEY=…`` then ``skodun review --reviewer …``.  
MCP: put the same variable on the ``skodun mcp`` server ``env`` and restart the
session. See ``examples/fragments/openai-api.md``.

**Spend:** estimated cost is recorded by the parent process; the **daily**
(UTC) ceiling is enforced in ``skodun.spend`` (default $10/day per provider).

Never prints secrets. Failures exit non-zero with a short stderr reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from skodun import spend as spend_mod

API_URL_DEFAULT = "https://api.openai.com/v1/chat/completions"
API_KEY_ENV = "OPENAI_API_KEY"
API_KEY_ENV_ALT = "SKODUN_OPENAI_API_KEY"
API_BASE_ENV = "SKODUN_OPENAI_API_BASE"  # override for proxies/tests
USAGE_PREFIX = "SKODUN_API_USAGE "


def _emit_usage(usage: dict) -> None:
    line = USAGE_PREFIX + json.dumps(usage, separators=(",", ":")) + "\n"
    sys.stderr.buffer.write(line.encode("utf-8"))
    sys.stderr.buffer.flush()


#: The `finish_reason` that means the model said everything it meant to. Every
#: other value -- `length` at the token ceiling, `content_filter`, whatever the
#: API adds next -- means the answer stopped early, so this is an allowlist of
#: one rather than a list of the bad ones: a reason nobody here has heard of
#: must read as "incomplete", not as "fine".
COMPLETE_FINISH_REASON = "stop"

#: Wording the adapter's degradation table matches. Written HERE, by skodun,
#: which is what makes the signal verifiable -- the table it feeds used to
#: match `truncated` and `envelope refused`, neither of which anything in this
#: adapter's path ever wrote (issue #99).
INCOMPLETE_PREFIX = "openai-api response incomplete"


def _emit_incomplete(finish_reason: object) -> None:
    """Say so on stderr when the API stopped the answer early. No-op for `stop`.

    Also a no-op when the reason is absent: a response that never reached the
    `choices` array (a network error, an HTTP status) has its own message, and
    inventing "incomplete" from silence would be the inference-from-absence
    every adapter's degradation detection is written to avoid.
    """
    if not isinstance(finish_reason, str) or not finish_reason:
        return
    if finish_reason == COMPLETE_FINISH_REASON:
        return
    sys.stderr.buffer.write(
        f"{INCOMPLETE_PREFIX} (finish_reason={finish_reason})\n".encode("utf-8"))
    sys.stderr.buffer.flush()


def _fail(msg: str, rc: int = 2) -> int:
    sys.stderr.buffer.write((msg.rstrip() + "\n").encode("utf-8"))
    sys.stderr.buffer.flush()
    return rc


def call_chat_completions(
        *, api_key: str, model: str, prompt: str, schema: dict,
        timeout_sec: float, base_url: str,
        effort: str | None = None) -> tuple[int, dict | None, dict, str]:
    """Return (rc, payload_or_None, usage_dict, err_detail)."""
    body: dict = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a code-review assistant. Reply with JSON only "
                    "matching the response schema. No markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "skodun_contract",
                "strict": True,
                "schema": schema,
            },
        },
    }
    # Optional reasoning effort for models that accept it (ignored if unsupported).
    if effort and effort not in ("none",):
        body["reasoning_effort"] = effort

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "skodun-openai-api/0.4",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")[:2000]
        return e.code, None, {}, f"http {e.code}: {err_body}"
    except urllib.error.URLError as e:
        return 2, None, {}, f"network error: {e.reason!r}"
    except TimeoutError:
        return 2, None, {}, "request timed out"
    except Exception as e:  # noqa: BLE001 - total for runner
        return 2, None, {}, f"request failed: {e!r}"

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as e:
        return 2, None, {}, f"invalid json response: {e!r}"

    usage_raw = doc.get("usage") if isinstance(doc, dict) else None
    usage = {
        "prompt_tokens": int((usage_raw or {}).get("prompt_tokens") or 0),
        "completion_tokens": int((usage_raw or {}).get("completion_tokens") or 0),
        "total_tokens": int((usage_raw or {}).get("total_tokens") or 0),
        "model": model,
    }
    usage["cost_usd"] = spend_mod.estimate_cost_usd(
        model,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
    )
    if isinstance(doc, dict) and doc.get("id"):
        usage["request_id"] = str(doc["id"])

    if status != 200:
        return status, None, usage, f"http {status}"

    # BEFORE the content is touched, and that ordering is the point: the API's
    # own statement that it stopped early is most useful on exactly the runs
    # where the content is then unusable. `finish_reason: "length"` truncates
    # mid-JSON, so `json.loads` below raises and the reason would be lost with
    # it -- leaving "the model returned garbage" and "the answer was cut off at
    # the token ceiling" indistinguishable in the record.
    #
    # Carried on `usage` rather than in a wider return tuple because that dict
    # is already the runner's metadata channel (`request_id` rides on it too),
    # and `chain._record_api_usage` reads four named keys and ignores the rest.
    # Recorded ONLY when the API really said something. `finish_reason` is
    # nullable -- it is `null` on a response that is not finalized -- and
    # coercing that with `str()` produces `"None"`, a non-empty string that is
    # not `stop` and would therefore be reported as a truncation. That is
    # inference from absence, and it would demote usable output on the
    # strength of the API declining to answer.
    try:
        reason = doc["choices"][0]["finish_reason"]
    except (KeyError, IndexError, TypeError):
        reason = None
    if isinstance(reason, str) and reason:
        usage["finish_reason"] = reason

    try:
        choices = doc["choices"]
        content = choices[0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message content is not a string")
        payload = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return 2, None, usage, f"could not extract contract json: {e!r}"

    if not isinstance(payload, dict):
        return 2, None, usage, "contract json is not an object"
    return 0, payload, usage, ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="skodun.adapters.openai_api_runner")
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--schema", required=True, help="contract JSON Schema string")
    p.add_argument("--timeout-ms", type=int, required=True)
    p.add_argument("--effort", default="")
    p.add_argument("--api-key-env", default=API_KEY_ENV)
    args = p.parse_args(argv)

    key = (
        os.environ.get(args.api_key_env)
        or os.environ.get(API_KEY_ENV)
        or os.environ.get(API_KEY_ENV_ALT)
        or ""
    )
    if not str(key).strip():
        return _fail(
            f"missing API key: set {API_KEY_ENV} or {API_KEY_ENV_ALT} "
            f"(or {args.api_key_env}) in the process environment "
            f"(CLI export or MCP server env — never in repo TOML)",
            rc=2)

    try:
        schema = json.loads(args.schema)
    except ValueError as e:
        return _fail(f"invalid --schema json: {e!r}", rc=2)
    if not isinstance(schema, dict):
        return _fail("--schema must be a JSON object", rc=2)

    # OpenAI strict json_schema often requires additionalProperties: false.
    schema = _strictify_schema(schema)

    try:
        prompt = Path(args.prompt).read_text(encoding="utf-8")
    except OSError as e:
        return _fail(f"could not read prompt: {e!r}", rc=2)

    timeout_sec = max(1.0, int(args.timeout_ms) / 1000.0)
    base = (os.environ.get(API_BASE_ENV) or API_URL_DEFAULT).strip()
    effort = args.effort if args.effort and args.effort != "none" else None

    rc, payload, usage, err = call_chat_completions(
        api_key=key.strip(),
        model=args.model,
        prompt=prompt,
        schema=schema,
        timeout_sec=timeout_sec,
        base_url=base,
        effort=effort,
    )
    if usage:
        _emit_usage(usage)
    # BEFORE the failure branch below, so a truncation that ALSO broke the JSON
    # is still reported as a truncation. `_fail`'s own message would otherwise
    # be the only thing on stderr, and it describes the symptom (unparseable)
    # rather than the cause.
    _emit_incomplete(usage.get("finish_reason"))
    if rc != 0 or payload is None:
        # Surface rate limits clearly for classify().
        detail = err or "openai api call failed"
        low = detail.lower()
        if rc == 429 or "rate limit" in low or "429" in low:
            return _fail(f"rate limit: {detail}", rc=1)
        if rc in (401, 403) or "invalid api key" in low or "unauthorized" in low:
            return _fail(f"auth failure: {detail}", rc=1)
        if rc == 404 or "model" in low and ("not found" in low or "does not exist" in low):
            return _fail(f"model failure: {detail}", rc=1)
        return _fail(detail, rc=1 if rc else 2)

    out = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


def _strictify_schema(schema: dict) -> dict:
    """Best-effort additionalProperties:false walk for OpenAI strict mode.

    OpenAI requires every object under ``json_schema.strict`` to list
    ``required`` containing **all** property keys (nested ``items`` too).
    """
    def walk(node: object) -> object:
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object":
            out["additionalProperties"] = False
            props = out.get("properties")
            if isinstance(props, dict) and props:
                # Always overwrite: partial required arrays fail strict mode.
                out["required"] = list(props.keys())
        if out.get("type") == "array" and "items" in out:
            out["items"] = walk(out["items"])
        return out

    result = walk(schema)
    assert isinstance(result, dict)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
