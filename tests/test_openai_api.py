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
from skodun.adapters import get_adapter
from tests.adapter_conformance import (  # noqa: F401 - collected below
    AdapterConformance,
    load_fixture,
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
    for k in (
        "SKODUN_OPENAI_API_SPEND_LIMIT_USD",
        "SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY",
        "SKODUN_API_SPEND_LIMIT_USD",
        "SKODUN_API_SPEND_LIMIT_USD_PER_DAY",
    ):
        monkeypatch.delenv(k, raising=False)
    assert spend.spend_limit_usd(PROVIDER_ID) == 10.0
    monkeypatch.setenv("SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY", "2.5")
    assert spend.spend_limit_usd(PROVIDER_ID) == 2.5
    cost = spend.estimate_cost_usd(
        MODEL, prompt_tokens=1_000_000, completion_tokens=0)
    assert cost > 0


def test_spend_ledger_is_per_utc_day_not_lifetime(tmp_path, monkeypatch):
    """Limit compares only today's rows; yesterday does not burn the budget."""
    monkeypatch.setenv("SKODUN_OPENAI_API_SPEND_LIMIT_USD_PER_DAY", "0.01")
    st = Store.open(tmp_path / "s.db")
    assert SCHEMA_VERSION == 8
    with st:
        # Yesterday's heavy spend must not count.
        spend.record_usage(
            st, provider=PROVIDER_ID, model=MODEL,
            prompt_tokens=100, completion_tokens=100, cost_usd=9.99,
            at="2020-01-01T12:00:00Z")
        assert spend.spent_today_usd(st, PROVIDER_ID) == 0.0
        assert not spend.would_exceed_limit(
            st, PROVIDER_ID, additional_usd=0.0)
        # Today's spend does.
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
    monkeypatch.delenv("SKODUN_OPENAI_API_KEY", raising=False)
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


def test_runner_accepts_skodun_openai_api_key_alias(tmp_path, monkeypatch, capsys):
    """BYOK alias for MCP env blocks that prefer a skodun-prefixed name."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SKODUN_OPENAI_API_KEY", "sk-test-alias-not-used-live")

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({
                "id": "chatcmpl-alias",
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "summary": "alias key worked",
                            "findings": [],
                        }),
                    },
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                          "total_tokens": 5},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "skodun.adapters.openai_api_runner.urllib.request.urlopen",
        lambda *a, **k: _Resp())
    p = tmp_path / "p.txt"
    p.write_text("review", encoding="utf-8")
    schema = json.dumps({
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "findings": {"type": "array"},
        },
        "required": ["summary", "findings"],
    })
    rc = runner_main([
        "--prompt", str(p),
        "--model", MODEL,
        "--schema", schema,
        "--timeout-ms", "5000",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alias key worked" in out


# --------------------------------------------------------------------------
# degradation (#99): the axis this adapter did not have
# --------------------------------------------------------------------------


def test_a_finish_reason_other_than_stop_is_reported_on_stderr():
    """The signal at its source. `finish_reason` is the API's own statement
    that it stopped writing early, and the runner read the content without
    ever looking at it -- so a truncated answer and a garbage one were
    indistinguishable in the record."""
    from skodun.adapters.openai_api_runner import _emit_incomplete
    import io
    import sys as _sys

    for reason, expected in (("length", True), ("content_filter", True),
                             ("stop", False), ("", False), (None, False)):
        buf = io.BytesIO()
        real = _sys.stderr
        class _Cap:
            buffer = buf
        _sys.stderr = _Cap()
        try:
            _emit_incomplete(reason)
        finally:
            _sys.stderr = real
        got = buf.getvalue().decode()
        assert bool(got) is expected, (reason, got)
        if expected:
            assert f"finish_reason={reason}" in got


def test_an_unknown_finish_reason_reads_as_incomplete():
    """An allowlist of one, not a denylist: a reason the API adds after this
    was written must read as "stopped early", never as "fine"."""
    from skodun.adapters.openai_api_runner import COMPLETE_FINISH_REASON

    assert COMPLETE_FINISH_REASON == "stop"


def test_the_runner_records_finish_reason_even_when_the_json_is_unusable(
        monkeypatch):
    """Truncation at the token ceiling cuts the JSON mid-string, so the
    extraction below it fails -- and the reason would be lost with it. It is
    captured BEFORE the content is touched for exactly that run."""
    class _Resp:
        status = 200

        def read(self):
            return json.dumps({
                "id": "chatcmpl-cut",
                "choices": [{"finish_reason": "length",
                             "message": {"content": '{"summary": "half'}}],
                "usage": {"prompt_tokens": 9100, "completion_tokens": 4096,
                          "total_tokens": 13196},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    rc, payload, usage, err = call_chat_completions(
        api_key="sk-test", model=MODEL, prompt="review",
        schema={"type": "object",
                "properties": {"summary": {"type": "string"},
                               "findings": {"type": "array"}},
                "required": ["summary", "findings"]},
        timeout_sec=5.0,
        base_url="https://example.invalid/v1/chat/completions")

    assert rc != 0 and payload is None       # the JSON really is unusable...
    assert usage["finish_reason"] == "length"   # ...and the cause survives


def test_every_degraded_signal_is_individually_load_bearing():
    """The `_QUOTA_SIGNALS` rule, applied to the degradation table -- the rule
    whose absence let this adapter ship two signals it cannot emit."""
    from skodun.adapters.openai_api import _DEGRADED_STDERR_SIGNALS

    a = get_adapter("openai-api")
    for sig in _DEGRADED_STDERR_SIGNALS:
        # At the start of a line, because that is where the runner writes it
        # and where `_detect_degraded` looks -- see
        # `test_the_marker_only_counts_at_the_start_of_a_line`.
        v = a.classify(2, b"", sig + b" (finish_reason=length)\n")
        assert v.kind == "degraded", sig


def test_a_truncated_answer_that_parses_is_still_degraded():
    """The case `parse_ok` cannot catch, and the reason `parse` no longer
    hardcodes `degraded=False`: a length-truncated response whose JSON happens
    to close cleanly used to be recorded as a complete, trustworthy review."""
    f = load_fixture(FIXTURES / "degraded_truncated_answer.txt")
    a = get_adapter("openai-api")

    res = a.parse(f.stdout, f.stderr)

    assert res.parse_ok is True and res.degraded is True and res.degraded_reason
    assert a.classify(f.rc, f.stdout, f.stderr).kind == "degraded"


def test_a_healthy_run_with_noisy_stderr_is_still_not_degraded():
    """The other side: the degradation check runs BEFORE the usable-payload
    short-circuit, so it has to be a signal only the runner can write. Noise
    that merely mentions failure must not demote a complete review."""
    f = load_fixture(FIXTURES / "healthy_noisy_stderr.txt")
    a = get_adapter("openai-api")

    assert a.parse(f.stdout, f.stderr).degraded is False
    assert a.classify(f.rc, f.stdout, f.stderr).kind == "ok"


def test_every_degraded_signal_is_wording_this_adapter_can_actually_emit():
    """The check that would have caught #99, and the one the load-bearing
    rule cannot.

    Feeding each table entry back into `classify` proves the table fires on
    itself -- perfectly green while the strings match nothing production
    writes, which is exactly how `truncated` and `envelope refused` survived
    here. The non-circular question is whether the RUNNER emits the wording.

    String LITERALS, not the raw source. The first version of this test
    grepped the file and passed against the dead signals, because the comment
    explaining that they are dead quotes both of them -- a scan that reads
    prose about a string as evidence of writing it. Comments are absent from
    the AST, and docstrings are skipped explicitly, so what is left is text
    the module can put on a stream.
    """
    import ast as _ast
    from pathlib import Path as _Path

    from skodun.adapters import openai_api_runner
    from skodun.adapters.openai_api import _DEGRADED_STDERR_SIGNALS

    tree = _ast.parse(
        _Path(openai_api_runner.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in _ast.walk(tree)
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                             _ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], _ast.Expr)
        and isinstance(node.body[0].value, _ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = " ".join(
        node.value.lower() for node in _ast.walk(tree)
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings)

    for sig in _DEGRADED_STDERR_SIGNALS:
        assert sig.decode() in literals, (
            f"{sig!r} is in the degradation table but appears in no string "
            f"openai_api_runner can write: a signal the adapter cannot "
            f"produce is a claim it cannot back")


def test_a_provider_error_body_cannot_spoof_the_truncation_signal():
    """The stream this signal is read from is NOT skodun's alone.

    On a non-2xx the runner puts up to 2000 characters of the provider's own
    error body on stderr (`http {code}: {body}`), so an unanchored substring
    match would let an error body that merely mentions the phrase read as a
    truncation -- and a 429 read as `degraded` instead of `quota` stops the
    fallback chain on a provider that is only out of budget, and skips the one
    verdict that is cached provider-wide.

    Two defences, and the test covers both: the unavailability tables are
    consulted first, and the marker only counts at the start of a line.
    """
    a = get_adapter("openai-api")
    body = (b'http 429: {"error":{"message":"Rate limit reached. '
            b'openai-api response incomplete (finish_reason=length)"}}\n')

    v = a.classify(1, b"", body)

    assert v.kind == "unavailable" and v.category == "quota"


def test_the_marker_only_counts_at_the_start_of_a_line():
    """The anchor on its own, with no competing signal to hide behind."""
    a = get_adapter("openai-api")
    quoted = b'http 500: {"error":"openai-api response incomplete was logged"}\n'
    real = b"openai-api response incomplete (finish_reason=length)\n"

    assert a.classify(1, b"", quoted).kind != "degraded"
    assert a.classify(1, b"", real).kind == "degraded"


@pytest.mark.parametrize("reason, recorded", [
    ("length", "length"),
    ("stop", "stop"),
    (None, None),          # nullable, and NOT the string "None"
    (123, None),
    ("", None),
])
def test_a_null_finish_reason_is_not_a_truncation(monkeypatch, reason,
                                                  recorded):
    """`finish_reason` is nullable -- `null` on a response that is not
    finalized -- and `str(None)` is `"None"`: a non-empty string that is not
    `stop`, so a coerced null would be reported as a truncation and demote
    usable output on the strength of the API declining to answer."""
    class _Resp:
        status = 200

        def read(self):
            choice = {"message": {"content": json.dumps(
                {"summary": "ok", "findings": []})}}
            if reason is not None or True:
                choice["finish_reason"] = reason
            return json.dumps({
                "id": "chatcmpl-null",
                "choices": [choice],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    _, payload, usage, _ = call_chat_completions(
        api_key="sk-test", model=MODEL, prompt="review",
        schema={"type": "object",
                "properties": {"summary": {"type": "string"},
                               "findings": {"type": "array"}},
                "required": ["summary", "findings"]},
        timeout_sec=5.0,
        base_url="https://example.invalid/v1/chat/completions")

    assert payload is not None
    assert usage.get("finish_reason") == recorded
