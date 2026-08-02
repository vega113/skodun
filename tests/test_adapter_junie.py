"""Tests for the junie adapter: invocation, parse, classify, conformance."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from skodun.adapters import (
    REFUTER_CONTRACT,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    get_adapter,
)
from skodun.adapters.junie import (
    _QUOTA_SIGNALS,
    JunieAdapter,
    resolve_junie_bin,
)
from skodun.config import Defaults, Reviewer
from tests.adapter_conformance import (  # noqa: F401 - collected below
    AdapterConformance,
    load_fixture,
    test_coverage_gate_fails_without_a_conformance_subclass,
    test_every_registered_adapter_has_conformance_coverage,
    test_load_fixture_rejects_a_malformed_rc,
)

FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "junie"
MODEL = "gpt-5.6-luna"
R = Reviewer(name="f", provider="junie", model=MODEL, role="finder")
D = Defaults()


@pytest.fixture(autouse=True)
def pinned_junie_bin(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_JUNIE_BIN", str(tmp_path / "pinned" / "junie"))


def fx(name: str):
    return load_fixture(FIXTURES / f"{name}.txt")


# --------------------------------------------------------------------------
# binary / registry
# --------------------------------------------------------------------------


def test_binary_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_JUNIE_BIN", str(tmp_path / "elsewhere"))
    assert resolve_junie_bin() == str(tmp_path / "elsewhere")


def test_binary_falls_back_to_path_name(monkeypatch):
    monkeypatch.delenv("SKODUN_JUNIE_BIN", raising=False)
    assert resolve_junie_bin() == "junie"


def test_empty_override_is_not_an_override(monkeypatch):
    monkeypatch.setenv("SKODUN_JUNIE_BIN", "")
    assert resolve_junie_bin() == "junie"


def test_registry_serves_the_junie_adapter():
    a = get_adapter("junie")
    assert isinstance(a, JunieAdapter)
    assert a.name == "junie" and a.provider == "junie"


def test_prompt_does_not_travel_as_chain_stdin():
    assert JunieAdapter.stdin_from_prompt_file is False


def test_no_prompt_ceiling():
    assert JunieAdapter().prompt_limit() is None


# --------------------------------------------------------------------------
# build_cmd
# --------------------------------------------------------------------------


def test_build_cmd_uses_isolated_python_module_runner(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this change", encoding="utf-8")
    cmd = JunieAdapter().build_cmd(prompt, R, D, tmp_path)
    assert cmd[0] == sys.executable
    assert "-I" in cmd
    # Bootstrap re-injects the skodun import root under -I (which drops
    # PYTHONPATH). Still isolated; not a bare ambient -m without path fix.
    assert "-c" in cmd
    assert "junie_runner" in cmd[cmd.index("-c") + 1]
    assert "--prompt" in cmd
    assert str(prompt) in cmd
    assert "--model" in cmd and MODEL in cmd
    assert "--schema" in cmd
    # Prompt BODY must never appear on argv.
    assert "review this change" not in cmd
    joined = " ".join(cmd)
    assert "review this change" not in joined


def test_classify_modulenotfound_is_unavailable_not_ok():
    """PYTHONPATH-only parent + python -I child used to look like empty ok."""
    stderr = (
        b"Error while finding module specification for "
        b"'skodun.adapters.junie_runner' "
        b"(ModuleNotFoundError: No module named 'skodun')\n"
    )
    v = JunieAdapter().classify(1, b"", stderr)
    assert v.kind == "unavailable"
    assert v.category == "other"
    assert "skodun" in v.detail.lower()


def test_classify_empty_stdout_nonzero_rc_is_unavailable():
    v = JunieAdapter().classify(1, b"", b"")
    assert v.kind == "unavailable"
    assert "no review payload" in v.detail


def test_build_cmd_does_not_embed_prompt_body(tmp_path):
    """Mutation target: putting prompt text on argv must fail this test."""
    prompt = tmp_path / "prompt.txt"
    secret = "UNIQUE_PROMPT_BODY_MARKER_9f3a"
    prompt.write_text(secret, encoding="utf-8")
    cmd = JunieAdapter().build_cmd(prompt, R, D, tmp_path)
    assert secret not in cmd
    assert secret not in " ".join(cmd)


def test_build_cmd_passes_effort_when_set(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    r = Reviewer(
        name="f", provider="junie", model=MODEL, role="finder", effort="high"
    )
    cmd = JunieAdapter().build_cmd(prompt, r, D, tmp_path)
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_build_cmd_rejects_max_effort(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    r = Reviewer(
        name="f", provider="junie", model=MODEL, role="finder", effort="max"
    )
    with pytest.raises(ValueError, match="effort"):
        JunieAdapter().build_cmd(prompt, r, D, tmp_path)


def test_build_cmd_timeout_ms_from_defaults(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    d = Defaults(timeout_sec=90)
    cmd = JunieAdapter().build_cmd(prompt, R, d, tmp_path)
    assert cmd[cmd.index("--timeout-ms") + 1] == "90000"


# --------------------------------------------------------------------------
# parse / classify
# --------------------------------------------------------------------------


def test_parse_healthy_fixture():
    f = fx("healthy")
    res = JunieAdapter().parse(f.stdout, f.stderr)
    assert res.parse_ok
    assert res.summary
    assert len(res.findings) == 3
    assert res.stop_reason is None
    assert not res.degraded


def test_parse_refuter_fixture():
    f = fx("refuter_healthy")
    res = JunieAdapter().parse(f.stdout, f.stderr, REFUTER_CONTRACT)
    assert res.parse_ok
    assert res.payload is not None
    assert "verdicts" in res.payload
    assert res.findings == []  # review projection stays empty


def test_classify_healthy_is_ok():
    f = fx("healthy")
    v = JunieAdapter().classify(f.rc, f.stdout, f.stderr)
    assert v.kind == "ok"


def test_classify_rc_127_is_binary():
    v = JunieAdapter().classify(UNAVAILABLE_RC, b"", b"")
    assert v.kind == "unavailable" and v.category == "binary"


def test_classify_quota_fixture():
    f = fx("unavailable_quota")
    v = JunieAdapter().classify(f.rc, f.stdout, f.stderr)
    assert v.kind == "unavailable" and v.category == "quota"


def test_classify_platform_refusal_is_other():
    v = JunieAdapter().classify(
        2, b"", b"junie confinement requires macOS; refusing unconfined run\n"
    )
    assert v.kind == "unavailable" and v.category == "other"


def test_classify_usable_payload_wins_over_noisy_auth_stderr():
    f = fx("healthy_noisy_stderr")
    v = JunieAdapter().classify(f.rc, f.stdout, f.stderr)
    assert v.kind == "ok"


def test_every_quota_signal_is_individually_load_bearing():
    """Each table entry must fire on its own — no dead-weight signals."""
    a = JunieAdapter()
    for sig in _QUOTA_SIGNALS:
        v = a.classify(1, b"", b"error: " + sig + b"\n")
        assert v.kind == "unavailable" and v.category == "quota", sig


def test_degraded_stderr_fixture():
    f = fx("degraded_stderr")
    res = JunieAdapter().parse(f.stdout, f.stderr)
    assert not res.parse_ok
    assert res.degraded
    v = JunieAdapter().classify(f.rc, f.stdout, f.stderr)
    assert v.kind == "degraded"


# --------------------------------------------------------------------------
# conformance
# --------------------------------------------------------------------------


class TestJunieConformance(AdapterConformance):
    provider_id = "junie"
    fixture_dir = FIXTURES

    def adapter(self):
        return JunieAdapter()

    def effort_reject_case(self):
        r = Reviewer(
            name="f",
            provider="junie",
            model=MODEL,
            role="finder",
            effort="max",
        )
        return r, "effort"
