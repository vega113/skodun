"""HTTP runner for provider ``openai-api``: Chat Completions → contract JSON.

Spawned like the junie outer runner (``python -I -c`` bootstrap). Reads the
prompt file, calls OpenAI's HTTP API with the API key from the environment,
writes a contract-shaped JSON object on stdout, and emits a single machine
line on stderr for usage/cost accounting:

``SKODUN_API_USAGE {json}``

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
API_BASE_ENV = "SKODUN_OPENAI_API_BASE"  # override for proxies/tests
USAGE_PREFIX = "SKODUN_API_USAGE "


def _emit_usage(usage: dict) -> None:
    line = USAGE_PREFIX + json.dumps(usage, separators=(",", ":")) + "\n"
    sys.stderr.buffer.write(line.encode("utf-8"))
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

    key = os.environ.get(args.api_key_env) or os.environ.get(API_KEY_ENV) or ""
    if not str(key).strip():
        return _fail(
            f"missing API key: set {args.api_key_env} (or {API_KEY_ENV})",
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
    """Best-effort additionalProperties:false walk for OpenAI strict mode."""
    def walk(node: object) -> object:
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
            props = out.get("properties")
            if isinstance(props, dict):
                # strict mode often wants required = all property keys
                if "required" not in out:
                    out["required"] = list(props.keys())
        if out.get("type") == "array" and "items" in out:
            out["items"] = walk(out["items"])
        return out

    result = walk(schema)
    assert isinstance(result, dict)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
