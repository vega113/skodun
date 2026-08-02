"""OpenAI HTTP adapter (openai-api) + spend limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skodun import spend
from skodun.adapters.openai_api import (
    PROVIDER_ID,
    OpenAIAPIAdapter,
    parse_usage_line,
)
from skodun.adapters.openai_api_runner import (
    call_chat_completions,
    main as runner_main,
)
from skodun.config import Defaults, Reviewer
from skodun.store import SCHEMA_VERSION, Store
from tests.adapter_conformance import (  # noqa: F401 - collected below
    AdapterConformance,
    test_coverage_gate_fails_without_a_conformance_subclass,
    test_every_registered_adapter_has_conformance_coverage,
    test_load_fixture_rejects_a_malformed_rc,
)

MODEL = "gpt-5.6-luna"
R = Reviewer(name="f", provider=PROVIDER_ID, model=MODEL,
             role="finder", effort="medium")
D = Defaults(timeout_sec=30)
FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "openai_api"


class TestOpenAIAPIConformance(AdapterConformance):
    provider_id = PROVIDER_ID
    fixture_dir = FIXTURES

    def adapter(self):
        return OpenAIAPIAdapter()

    def effort_reject_case(self):
        r = Reviewer(name="f", provider=PROVIDER_ID, model=MODEL,
                     role="finder", effort="max")
        return r, "effort"


def test_registry_has_openai_api():
    from skodun.adapters import _REGISTRY, get_adapter
    assert PROVIDER_ID in _REGISTRY
    a = get_adapter(PROVIDER_ID)
    assert a.name == "openai-api"


def test_build_cmd_isolated_bootstrap(tmp_path):
    p = tmp_path / "p.txt"
    p.write_text("review me", encoding="utf-8")
    cmd = OpenAIAPIAdapter().build_cmd(p, R, D, tmp_path)
    assert cmd[0]
    assert "-I" in cmd
    assert "-c" in cmd
    assert "openai_api_runner" in cmd[cmd.index("-c") + 1]
    assert "--model" in cmd and MODEL in cmd
    assert "review me" not in " ".join(cmd)


def test_parse_usage_line():
    stderr = (
        b"noise\n"
        b'SKODUN_API_USAGE {"prompt_tokens":10,"completion_tokens":5,'
        b'"total_tokens":15,"cost_usd":0.001,"model":"gpt-5.6-luna"}\n'
    )
    u = parse_usage_line(stderr)
    assert u is not None
    assert u["prompt_tokens"] == 10
    assert u["cost_usd"] == 0.001


def test_estimate_cost_and_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("SKODUN_OPENAI_API_SPEND_LIMIT_USD", raising=False)
    monkeypatch.delenv("SKODUN_API_SPEND_LIMIT_USD", raising=False)
    assert spend.spend_limit_usd(PROVIDER_ID) == 10.0
    monkeypatch.setenv("SKODUN_OPENAI_API_SPEND_LIMIT_USD", "2.5")
    assert spend.spend_limit_usd(PROVIDER_ID) == 2.5
    cost = spend.estimate_cost_usd(
        MODEL, prompt_tokens=1_000_000, completion_tokens=0)
    assert cost > 0


def test_spend_ledger_and_block(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_OPENAI_API_SPEND_LIMIT_USD", "0.01")
    st = Store.open(tmp_path / "s.db")
    assert SCHEMA_VERSION == 8
    with st:
        spend.record_usage(
            st, provider=PROVIDER_ID, model=MODEL,
            prompt_tokens=100, completion_tokens=100, cost_usd=0.02)
        assert spend.spent_today_usd(st, PROVIDER_ID) >= 0.02
        assert spend.would_exceed_limit(st, PROVIDER_ID, additional_usd=0.0)


def test_classify_rate_limit():
    v = OpenAIAPIAdapter().classify(
        1, b"", b"rate limit: http 429: too many requests\n")
    assert v.kind == "unavailable"
    assert v.category == "quota"


def test_classify_auth():
    v = OpenAIAPIAdapter().classify(
        1, b"", b"auth failure: invalid api key\n")
    assert v.kind == "unavailable"
    assert v.category == "auth"


def test_parse_healthy_payload():
    body = json.dumps({
        "summary": "ok",
        "findings": [],
    }).encode()
    res = OpenAIAPIAdapter().parse(body, b"")
    assert res.parse_ok
    assert res.findings == []


def test_call_chat_completions_mocked(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return json.dumps({
                "id": "chatcmpl-test",
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "summary": "clean",
                            "findings": [],
                        }),
                    },
                }],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _Resp())
    rc, payload, usage, err = call_chat_completions(
        api_key="sk-test",
        model=MODEL,
        prompt="review",
        schema={"type": "object",
                "properties": {"summary": {"type": "string"},
                               "findings": {"type": "array"}},
                "required": ["summary", "findings"]},
        timeout_sec=5.0,
        base_url="https://example.invalid/v1/chat/completions",
    )
    assert rc == 0 and err == ""
    assert payload["summary"] == "clean"
    assert usage["prompt_tokens"] == 11
    assert usage["cost_usd"] >= 0


def test_runner_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = tmp_path / "p.txt"
    p.write_text("x", encoding="utf-8")
    rc = runner_main([
        "--prompt", str(p),
        "--model", MODEL,
        "--schema", '{"type":"object","properties":{}}',
        "--timeout-ms", "1000",
    ])
    assert rc != 0
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err or "api key" in err.lower()
