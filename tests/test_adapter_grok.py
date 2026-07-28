"""Tests for the grok adapter: invocation, envelope parsing, degraded detection.

Three axes are under test and they are deliberately independent:

* **build_cmd** — the flag list is a contract with an external binary. It is
  asserted on the argv *list*, not a joined string, so a value containing a
  space can never masquerade as two arguments.
* **parse** — the three-level envelope fallback and the schema validation that
  decides `parse_ok`. A `parse_ok=True` record is, by the trust invariant, a
  record the gate is allowed to believe; anything that would let malformed
  findings through is tested here.
* **degraded** — positive evidence only. Every signal is exercised on its own
  (so a passing test cannot be riding another signal), and both documented
  non-signals are pinned negative.

Parity with the porting oracle's `detect_degraded` is asserted by extracting
that shell function (and its `grok_stop_reason` helper) verbatim and running it
against the same fixtures. Those tests skip when `$SKODUN_ORACLE_DIR` is unset.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from skodun.adapters import ParseResult, get_adapter
from skodun.adapters.grok import (
    SCHEMA,
    GrokAdapter,
    resolve_grok_bin,
)
from skodun.config import Defaults, Reviewer
from tests.adapter_conformance import (  # noqa: F401 - see below
    AdapterConformance,
    test_coverage_gate_fails_without_a_conformance_subclass,
    test_every_registered_adapter_has_conformance_coverage,
    test_load_fixture_rejects_a_malformed_rc,
)
from tests.conftest import oracle_dir

# The two `test_*` functions above are imported, not re-declared: they are the
# registry coverage gate, defined once next to the suite it gates. Importing
# them into a collected module is what makes pytest run them —
# `adapter_conformance.py` is a mixin module and is deliberately not collected.

R = Reviewer(name="f", provider="xai", model="grok-4.20-0309-reasoning", role="finder")
D = Defaults()

GOOD = {"summary": "ok", "findings": []}

# The leaked tool-call control token, spelled from its codepoint so the literal
# in this file cannot be silently normalized by an editor into a plain space.
LEAKED = "tool▁call"


# A whole envelope whose `text` carries the control token as RAW UTF-8 bytes,
# the way the oracle's corpus records it (18 of 4020 runs).
TOKEN_ENVELOPE = json.dumps(
    {"structuredOutput": GOOD, "stopReason": "EndTurn",
     "text": f"<{LEAKED}>search"}, ensure_ascii=False).encode("utf-8")


@pytest.fixture(autouse=True)
def pinned_grok_bin(monkeypatch, tmp_path):
    """Never touch the developer's real `~/.grok`.

    `resolve_grok_bin` falls back to `os.access(Path.home()/".grok"/...)`, so
    every `build_cmd` test that left `SKODUN_GROK_BIN` unset was stat-ing the
    real home directory and letting `argv[0]` vary by machine. Nothing is
    created or modified there, but the constraint is absolute and a
    machine-dependent argv is a latent flake. The five dedicated
    binary-resolution tests below override this fixture explicitly (they set or
    delete the variable themselves), which is the point: resolution order is
    tested only where it is the subject.
    """
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "pinned" / "grok"))


def env(**kw) -> bytes:
    """A realistic grok envelope: the real key set observed on the wire."""
    e = {
        "text": "",
        "stopReason": "EndTurn",
        "sessionId": "019f46cc-0c68-7ab0-a14d-4fa13edacabf",
        "requestId": "37e7f24e-6c69-4cca-aa8c-2cd571a76bcf",
        "thought": "The user wants a review. Let me analyze the diff.",
        "structuredOutput": None,
    }
    e.update(kw)
    return json.dumps(e).encode("utf-8")


# --------------------------------------------------------------------------
# build_cmd
# --------------------------------------------------------------------------


def test_cmd_flag_list_is_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_GROK_BIN", "/opt/grok")
    p = tmp_path / "p.txt"
    cmd = GrokAdapter().build_cmd(p, R, D, tmp_path)
    assert cmd == [
        "/opt/grok",
        "--prompt-file", str(p),
        "--json-schema", SCHEMA,
        "-m", "grok-4.20-0309-reasoning",
        "--disable-web-search",
        "--no-subagents",
        "--no-memory",
        "--no-plan",
        "--max-turns", "40",
        "--verbatim",
        "--disallowed-tools", "bash,read,write,edit,web_search,web_fetch",
        "--cwd", str(tmp_path),
    ]


def test_cmd_has_explicit_model_and_denies_tools(tmp_path):
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    s = " ".join(cmd)
    assert "-m grok-4.20-0309-reasoning" in s
    assert "--disallowed-tools bash,read,write,edit,web_search,web_fetch" in s
    assert "--json-schema" in s and "--max-turns 40" in s


def test_cmd_never_uses_legacy_prompt_arg(tmp_path):
    """No `-p` re-shell fallback exists (global constraint); always --prompt-file."""
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    assert "-p" not in cmd
    assert "--prompt-file" in cmd


def test_cmd_honours_non_default_defaults(tmp_path):
    d = Defaults(max_turns=7, deny_tools="bash,read")
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, d, tmp_path)
    assert cmd[cmd.index("--max-turns") + 1] == "7"
    assert cmd[cmd.index("--disallowed-tools") + 1] == "bash,read"


def test_schema_is_passed_through_unmodified_and_is_valid_json(tmp_path):
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    passed = cmd[cmd.index("--json-schema") + 1]
    assert passed == SCHEMA
    assert "\n" not in SCHEMA  # single line: it is one argv element
    parsed = json.loads(SCHEMA)
    assert parsed["required"] == ["summary", "findings"]
    item = parsed["properties"]["findings"]["items"]
    assert item["properties"]["severity"]["enum"] == ["high", "medium", "low"]
    assert item["required"] == ["file", "severity", "title", "detail"]
    assert item["properties"]["line"]["type"] == "integer"


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
def test_schema_matches_oracle_verbatim():
    """The schema string is byte-identical to the oracle's GROK_REVIEW_SCHEMA."""
    src = (oracle_dir() / "scripts" / "grok-prepush-review.sh").read_text(
        encoding="utf-8")
    hits = [ln for ln in src.splitlines() if ln.startswith("GROK_REVIEW_SCHEMA=")]
    assert len(hits) == 1, hits
    oracle_schema = hits[0][len("GROK_REVIEW_SCHEMA="):].strip()
    assert oracle_schema[0] == "'" and oracle_schema[-1] == "'"
    assert oracle_schema[1:-1] == SCHEMA


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_effort_appended_when_set(tmp_path, effort):
    r = Reviewer(name="f", provider="xai", model="grok-4-fast", role="finder",
                 effort=effort)
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)
    assert cmd[-2:] == ["--effort", effort]


def test_effort_absent_when_unset(tmp_path):
    assert "--effort" not in GrokAdapter().build_cmd(
        tmp_path / "p.txt", R, D, tmp_path)


def test_effort_none_is_an_opt_out_not_a_flag(tmp_path):
    r = Reviewer(name="f", provider="xai", model="grok-4-fast", role="finder",
                 effort="none")
    assert "--effort" not in GrokAdapter().build_cmd(
        tmp_path / "p.txt", r, D, tmp_path)


def test_effort_rejected_for_grok_build(tmp_path):
    r = Reviewer(name="f", provider="xai", model="grok-build", role="finder",
                 effort="high")
    with pytest.raises(ValueError, match="effort"):
        GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)


def test_effort_rejected_for_grok_build_variants(tmp_path):
    r = Reviewer(name="f", provider="xai", model="grok-build-fast-1",
                 role="finder", effort="low")
    with pytest.raises(ValueError, match="grok-build-fast-1"):
        GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)


def test_grok_build_without_effort_is_fine(tmp_path):
    r = Reviewer(name="f", provider="xai", model="grok-build", role="finder")
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)
    assert "--effort" not in cmd and cmd[cmd.index("-m") + 1] == "grok-build"


def test_grok_build_with_effort_none_is_fine(tmp_path):
    """`effort = "none"` is the documented opt-out; no flag is emitted, so
    there is nothing for the model to reject."""
    r = Reviewer(name="f", provider="xai", model="grok-build", role="finder",
                 effort="none")
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)
    assert "--effort" not in cmd


# --------------------------------------------------------------------------
# binary resolution
# --------------------------------------------------------------------------


def test_resolve_bin_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_GROK_BIN", "/custom/grok")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_grok_bin() == "/custom/grok"


def test_resolve_bin_uses_home_grok_when_executable(monkeypatch, tmp_path):
    monkeypatch.delenv("SKODUN_GROK_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    binpath = tmp_path / ".grok" / "bin" / "grok"
    binpath.parent.mkdir(parents=True)
    binpath.write_text("#!/bin/sh\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IXUSR)
    assert resolve_grok_bin() == str(binpath)


def test_resolve_bin_falls_back_to_path_when_home_grok_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("SKODUN_GROK_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert not (tmp_path / ".grok").exists()
    assert resolve_grok_bin() == "grok"


def test_resolve_bin_falls_back_when_home_grok_not_executable(monkeypatch, tmp_path):
    monkeypatch.delenv("SKODUN_GROK_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    binpath = tmp_path / ".grok" / "bin" / "grok"
    binpath.parent.mkdir(parents=True)
    binpath.write_text("not executable", encoding="utf-8")
    binpath.chmod(0o644)
    assert resolve_grok_bin() == "grok"


def test_resolve_bin_ignores_empty_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_GROK_BIN", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert resolve_grok_bin() == "grok"


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_registry_returns_grok_for_xai():
    a = get_adapter("xai")
    assert isinstance(a, GrokAdapter) and a.name == "grok"


def test_registry_rejects_unknown_provider():
    with pytest.raises(ValueError, match="anthropic"):
        get_adapter("anthropic")


# --------------------------------------------------------------------------
# envelope parse — the three fallback levels
# --------------------------------------------------------------------------


def test_parse_structured_output():
    p = GrokAdapter().parse(env(structuredOutput=GOOD), b"")
    assert p.parse_ok and not p.degraded and p.stop_reason == "EndTurn"
    assert p.summary == "ok" and p.findings == []


def test_parse_structured_output_wins_over_text():
    """Level 1 is authoritative: `text` is not consulted when it is eligible."""
    payload = {"summary": "from-structured", "findings": []}
    other = {"summary": "from-text", "findings": []}
    p = GrokAdapter().parse(
        env(structuredOutput=payload, text=json.dumps(other)), b"")
    assert p.summary == "from-structured"


def test_parse_text_fallback_with_structured_output_error():
    """The recoverable shape (oracle issue #3009): null structuredOutput +
    structuredOutputError + EndTurn, with the real review intact in `text`."""
    e = env(structuredOutput=None, structuredOutputError="schema mismatch",
            text=json.dumps(GOOD))
    p = GrokAdapter().parse(e, b"")
    assert p.parse_ok and not p.degraded and p.summary == "ok"


def test_hollow_structured_output_falls_through_to_text():
    p = GrokAdapter().parse(env(structuredOutput={}, text=json.dumps(GOOD)), b"")
    assert p.parse_ok and p.summary == "ok"


def test_hollow_structured_output_with_prose_wrapped_text():
    """A whole-envelope fixture with a hollow `structuredOutput` and the real
    payload buried in prose + a fenced block inside `text`."""
    real = {
        "summary": "Two real issues in the new gate.",
        "findings": [
            {"file": "src/gate.py", "line": 12, "severity": "high",
             "category": "correctness", "title": "exit 1 on corruption",
             "detail": "A parse failure must map to exit 2, never exit 1."},
            {"file": "src/trust.py", "severity": "low",
             "title": "stale comment", "detail": "Mentions a removed flag."},
        ],
    }
    text = (
        "Here is my review of the diff.\n\n"
        "```json\n" + json.dumps(real, indent=2) + "\n```\n\n"
        "Let me know if you want me to dig deeper into the gate."
    )
    e = env(structuredOutput={}, structuredOutputError="empty object", text=text)
    p = GrokAdapter().parse(e, b"")
    assert p.parse_ok and not p.degraded
    assert p.summary == "Two real issues in the new gate."
    assert [f["file"] for f in p.findings] == ["src/gate.py", "src/trust.py"]


def test_text_level_uses_raw_decoder_not_bare_loads():
    """Prose on both sides of the object must not defeat level 2."""
    text = "Sure! " + json.dumps(GOOD) + "\n\nHope that helps."
    p = GrokAdapter().parse(env(text=text), b"")
    assert p.parse_ok and p.summary == "ok"


def test_text_level_skips_finding_shaped_objects():
    finding = {"file": "a", "severity": "low", "title": "t", "detail": "d"}
    text = "prose " + json.dumps(finding) + " more " + json.dumps(GOOD)
    p = GrokAdapter().parse(env(text=text), b"")
    assert p.parse_ok and p.summary == "ok"


def test_parse_raw_scan_skips_finding_shaped_object():
    raw = (b'noise {"file":"a","severity":"low","title":"t","detail":"d"} '
           + json.dumps(GOOD).encode() + b" trailing")
    p = GrokAdapter().parse(raw, b"")
    assert p.parse_ok and p.summary == "ok"
    assert p.stop_reason is None and not p.degraded


def test_parse_raw_scan_handles_duplicated_object_and_fences():
    """The legacy plain-output shapes the oracle's raw scan exists for."""
    blob = json.dumps({"summary": "first", "findings": []})
    raw = ("```json\n" + blob + "\n```\n\n" + blob).encode("utf-8")
    p = GrokAdapter().parse(raw, b"")
    assert p.parse_ok and p.summary == "first"


def test_parse_raw_scan_recovers_when_outer_object_is_truncated():
    """A truncated envelope is not JSON; the scan must reach the inner review."""
    raw = (b'{"structuredOutput": {"summary": "trunc"' + b"\n"
           + json.dumps(GOOD).encode())
    p = GrokAdapter().parse(raw, b"")
    assert p.parse_ok and p.summary == "ok"


def test_envelope_root_echoing_findings_does_not_divert_from_structured_output():
    """Level 1 is chosen by `structuredOutput`, not by the root's own keys."""
    root = json.loads(env(structuredOutput=GOOD).decode())
    root["findings"] = [{"file": "x", "severity": "high", "title": "t",
                         "detail": "d"}]
    root["summary"] = "envelope-level echo"
    p = GrokAdapter().parse(json.dumps(root).encode(), b"")
    assert p.summary == "ok" and p.findings == []


def test_eligibility_findings_only_object_is_accepted():
    """`findings` alone satisfies eligibility at every level (no `summary`)."""
    obj = {"findings": []}
    assert not GrokAdapter().parse(env(structuredOutput=obj), b"").parse_ok
    # ...eligible enough to be *selected* (it stops the fallback), but the
    # schema check then rejects it because `summary` is not a string.
    p = GrokAdapter().parse(env(structuredOutput=obj, text=json.dumps(GOOD)), b"")
    assert not p.parse_ok


def test_no_payload_at_all_is_not_parse_ok():
    for raw in (b"", b"not json at all", b"{}", b"[]", b'{"stopReason":"EndTurn"}'):
        p = GrokAdapter().parse(raw, b"")
        assert not p.parse_ok, raw
        assert p.findings == [] and p.summary == ""


def test_invalid_utf8_does_not_raise():
    p = GrokAdapter().parse(b"\xff\xfe garbage {", b"\xff\xfe")
    assert not p.parse_ok and not p.degraded


def test_findings_are_copied_not_aliased():
    payload = {"summary": "ok", "findings": [
        {"file": "a", "severity": "low", "title": "t", "detail": "d"}]}
    p = GrokAdapter().parse(env(structuredOutput=payload), b"")
    p.findings.append("intruder")
    assert len(payload["findings"]) == 1


def test_parse_returns_parse_result():
    assert isinstance(GrokAdapter().parse(env(), b""), ParseResult)


# --------------------------------------------------------------------------
# parse_ok — schema validation of findings
# --------------------------------------------------------------------------


def _ok(payload) -> bool:
    return GrokAdapter().parse(env(structuredOutput=payload), b"").parse_ok


VALID_FINDING = {"file": "a.py", "severity": "low", "title": "t", "detail": "d"}


def test_valid_findings_parse_ok():
    assert _ok({"summary": "s", "findings": [VALID_FINDING]})
    assert _ok({"summary": "s", "findings": [dict(VALID_FINDING, line=12)]})
    assert _ok({"summary": "s", "findings": [dict(VALID_FINDING,
                                                  category="security")]})
    assert _ok({"summary": "", "findings": []})


@pytest.mark.parametrize("severity", ["high", "medium", "low"])
def test_every_allowed_severity_parses(severity):
    assert _ok({"summary": "s", "findings": [dict(VALID_FINDING,
                                                  severity=severity)]})


@pytest.mark.parametrize("bad_item", [
    pytest.param(1, id="int-item"),
    pytest.param("a finding", id="str-item"),
    pytest.param(None, id="null-item"),
    pytest.param(["file", "a"], id="list-item"),
], )
def test_non_dict_finding_item_fails(bad_item):
    assert not _ok({"summary": "ok", "findings": [bad_item]})


@pytest.mark.parametrize("severity", ["urgent", "HIGH", "critical", "", None, 1])
def test_bad_severity_fails(severity):
    assert not _ok({"summary": "ok",
                    "findings": [dict(VALID_FINDING, severity=severity)]})


@pytest.mark.parametrize("key", ["file", "title", "detail"])
@pytest.mark.parametrize("value", [None, 1, ["a"], {"a": 1}, True])
def test_non_string_required_field_fails(key, value):
    assert not _ok({"summary": "ok",
                    "findings": [dict(VALID_FINDING, **{key: value})]})


@pytest.mark.parametrize("key", ["file", "title", "detail", "severity"])
def test_missing_required_field_fails(key):
    f = dict(VALID_FINDING)
    del f[key]
    assert not _ok({"summary": "ok", "findings": [f]})


@pytest.mark.parametrize("line", [
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param(1.5, id="float"),
    pytest.param("12", id="str"),
    pytest.param(None, id="null"),
])
def test_bad_line_type_fails(line):
    assert not _ok({"summary": "ok",
                    "findings": [dict(VALID_FINDING, line=line)]})


def test_one_bad_item_among_good_ones_fails_the_whole_payload():
    assert not _ok({"summary": "ok",
                    "findings": [VALID_FINDING, 1, VALID_FINDING]})


@pytest.mark.parametrize("summary", [None, 1, ["a"], {"a": 1}, True])
def test_non_string_summary_fails(summary):
    assert not _ok({"summary": summary, "findings": []})


@pytest.mark.parametrize("findings", [None, "none", {"a": 1}, 0])
def test_non_list_findings_fails(findings):
    assert not _ok({"summary": "ok", "findings": findings})


def test_malformed_finding_items_fail_parse():
    assert not _ok({"summary": "ok", "findings": [1]})
    assert not _ok({"summary": "ok", "findings": [
        {"file": "a", "severity": "urgent", "title": "t", "detail": "d"}]})
    assert not _ok({"summary": "ok", "findings": [
        {"file": "a", "severity": "low", "title": "t", "detail": "d",
         "line": True}]})


@pytest.mark.parametrize("payload", [
    pytest.param({"summary": "ok", "findings": [1]}, id="non-dict-item"),
    pytest.param({"summary": "ok", "findings": [
        {"file": "a", "severity": "urgent", "title": "t", "detail": "d"}]},
        id="bad-severity"),
    pytest.param({"summary": "ok", "findings": [
        VALID_FINDING, {"file": "a", "severity": "low", "title": "t"}]},
        id="one-good-one-truncated"),
    pytest.param({"summary": 1, "findings": [VALID_FINDING]},
                 id="good-findings-bad-summary"),
])
def test_rejected_payload_leaks_no_findings(payload):
    """The `ParseResult` invariant: `findings` is empty unless `parse_ok`.

    Each payload HAS a `findings` list — some of it well-formed — and still
    fails validation. A caller that checked `degraded` but forgot `parse_ok`
    must see nothing, not a half-shaped list it will key by `file`/`severity`.
    Guards the `if parse_ok` in `GrokAdapter.parse`: populating `findings`
    whenever `payload["findings"]` is a list passes every other test here.
    """
    p = GrokAdapter().parse(env(structuredOutput=payload), b"")
    assert not p.parse_ok
    assert p.findings == []
    assert p.summary == ""


# --------------------------------------------------------------------------
# degraded detection
# --------------------------------------------------------------------------


CLEAN = env(structuredOutput=GOOD)


def deg(stdout=None, stderr=b""):
    return GrokAdapter().parse(CLEAN if stdout is None else stdout, stderr)


def test_clean_run_is_not_degraded():
    p = deg()
    assert not p.degraded and p.degraded_reason == "" and p.parse_ok


@pytest.mark.parametrize("signal", [
    "tool_error", "execution_failure", "dropped the response channel",
    "harness-side bug", "harness side bug",
])
@pytest.mark.parametrize("case", ["lower", "upper", "title"])
def test_each_stderr_signal_flags_in_any_case(signal, case):
    text = {"lower": signal.lower(), "upper": signal.upper(),
            "title": signal.title()}[case]
    p = deg(stderr=f"worker log: {text} happened\n".encode())
    assert p.degraded and p.degraded_reason


def test_stderr_signals_case_insensitive():
    assert deg(stderr=b"Tool_Error: boom").degraded
    assert deg(stderr=b"Max Turns Reached").degraded


def test_stderr_signals_do_not_fire_from_stdout():
    """These are stderr tells; a review whose text discusses them is clean."""
    payload = {"summary": "the harness-side bug caused a tool_error",
               "findings": []}
    p = deg(stdout=env(structuredOutput=payload))
    assert not p.degraded and p.parse_ok


@pytest.mark.parametrize("case", ["lower", "upper", "title"])
def test_max_turns_reached_in_stderr_flags_in_any_case(case):
    text = {"lower": "max turns reached", "upper": "MAX TURNS REACHED",
            "title": "Max Turns Reached"}[case]
    p = deg(stderr=text.encode())
    assert p.degraded and "turn" in p.degraded_reason.lower()


def test_max_turns_in_stdout_is_not_degraded_but_stderr_is():
    e = env(structuredOutput={"summary": "discusses max turns reached",
                              "findings": []})
    assert not GrokAdapter().parse(e, b"").degraded
    assert GrokAdapter().parse(e, b"max turns reached").degraded


def test_leaked_control_token_in_stdout_flags():
    # `ensure_ascii=False` is not cosmetic: the oracle matches raw UTF-8 bytes,
    # and in its 4020-run corpus every one of the 18 affected envelopes carries
    # the token unescaped. An `▁`-escaped fixture would test nothing.
    p = GrokAdapter().parse(TOKEN_ENVELOPE, b"")
    assert p.degraded and "token" in p.degraded_reason
    assert LEAKED.encode("utf-8") in TOKEN_ENVELOPE


def test_escaped_control_token_is_not_a_byte_match():
    """Pins the byte-level semantics (oracle: `LC_ALL=C grep -F`). Grok emits
    the token unescaped, so this shape is not observed in practice."""
    raw = json.dumps({"structuredOutput": GOOD, "stopReason": "EndTurn",
                      "text": f"<{LEAKED}>"}).encode("utf-8")
    assert rb"\u2581" in raw and LEAKED.encode("utf-8") not in raw
    assert not GrokAdapter().parse(raw, b"").degraded


def test_leaked_control_token_matched_on_bytes_of_invalid_utf8():
    raw = b"\xff\xfe " + LEAKED.encode("utf-8") + b" tail"
    assert GrokAdapter().parse(raw, b"").degraded


def test_plain_ascii_tool_call_is_not_the_leaked_token():
    """`tool call` / `tool_call` without U+2581 is ordinary prose."""
    for text in ("tool call", "tool_call", "toolcall", "tool-call"):
        e = env(structuredOutput={"summary": text, "findings": []})
        assert not GrokAdapter().parse(e, b"").degraded, text


def test_leaked_token_in_stderr_alone_is_not_a_signal():
    """The oracle greps stdout only for the control token."""
    assert not deg(stderr=LEAKED.encode("utf-8")).degraded


def test_degraded_cancelled_stop_reason():
    p = GrokAdapter().parse(env(structuredOutput=GOOD, stopReason="Cancelled"),
                            b"")
    assert p.degraded and "stopReason" in p.degraded_reason
    assert p.stop_reason == "Cancelled"
    assert p.parse_ok  # parse_ok and degraded are independent axes


@pytest.mark.parametrize("value", ["Cancelled", "MaxTokens", "Refusal",
                                   "endturn", "EndTurn "])
def test_any_stop_reason_other_than_endturn_is_degraded(value):
    p = GrokAdapter().parse(env(structuredOutput=GOOD, stopReason=value), b"")
    assert p.degraded, value


def test_absent_stop_reason_is_no_signal():
    raw = json.dumps({"structuredOutput": GOOD}).encode()
    p = GrokAdapter().parse(raw, b"")
    assert not p.degraded and p.stop_reason is None


@pytest.mark.parametrize("value", [5, None, True, [], {}, ""])
def test_non_string_or_empty_stop_reason_is_no_signal(value):
    """Oracle `grok_stop_reason` prints "" unless the value is a str, and the
    caller treats an empty value as absent."""
    raw = json.dumps({"structuredOutput": GOOD, "stopReason": value}).encode()
    p = GrokAdapter().parse(raw, b"")
    assert not p.degraded, value
    assert p.stop_reason is None


def test_stop_reason_inside_findings_text_is_not_grepped():
    payload = {"summary": '"stopReason": "Cancelled" appears in the diff',
               "findings": []}
    p = GrokAdapter().parse(env(structuredOutput=payload), b"")
    assert not p.degraded


def test_non_envelope_output_has_no_stop_reason_signal():
    raw = json.dumps(GOOD).encode()
    p = GrokAdapter().parse(raw, b"")
    assert p.parse_ok and not p.degraded and p.stop_reason is None


# --- trailing bytes must not hide the stopReason --------------------------
#
# The envelope root is parsed with `raw_decode`, not `json.loads`. `loads`
# raises "Extra data" on any trailing byte, while the payload extractor's
# level-3 scan is built to survive exactly that — so with `loads` a good
# envelope plus one trailing line yielded `parse_ok=True, degraded=False,
# stop_reason=None` on a run the model CANCELLED. Each test below fails if the
# root parse goes back to `json.loads`.


def test_trailing_prose_does_not_hide_a_cancelled_stop_reason():
    raw = (json.dumps({"structuredOutput": GOOD, "stopReason": "Cancelled"})
           + "\nVERDICT: clean\n").encode()
    p = GrokAdapter().parse(raw, b"")
    assert p.stop_reason == "Cancelled"
    assert p.degraded and "stopReason" in p.degraded_reason
    # the payload is still recovered: this adds a signal, it removes nothing
    assert p.parse_ok and p.summary == "ok"


def test_trailing_prose_after_endturn_is_still_clean():
    """The fix must not invent a signal: `EndTurn` + trailing bytes is fine."""
    raw = (json.dumps({"structuredOutput": GOOD, "stopReason": "EndTurn"})
           + "\nVERDICT: clean\n").encode()
    p = GrokAdapter().parse(raw, b"")
    assert p.stop_reason == "EndTurn"
    assert not p.degraded and p.degraded_reason == ""
    assert p.parse_ok and p.summary == "ok"


def test_duplicated_final_object_does_not_hide_the_stop_reason():
    """grok sometimes emits its final object twice (the shape the raw scan
    exists for). The root read must survive it too."""
    one = json.dumps({"structuredOutput": GOOD, "stopReason": "Cancelled"})
    p = GrokAdapter().parse((one + "\n" + one).encode(), b"")
    assert p.parse_ok and p.summary == "ok"
    assert p.stop_reason == "Cancelled" and p.degraded


def test_leading_whitespace_envelope_still_yields_a_stop_reason():
    """`raw_decode` does not skip leading whitespace the way `loads` does; a
    pretty-printed / newline-prefixed envelope must not lose its stopReason."""
    raw = ("\n  " + json.dumps(
        {"structuredOutput": GOOD, "stopReason": "Cancelled"}, indent=2)).encode()
    p = GrokAdapter().parse(raw, b"")
    assert p.stop_reason == "Cancelled" and p.degraded and p.parse_ok


def test_trailing_bytes_before_a_root_object_are_still_no_signal():
    """Only a root that OPENS the output counts. Prose first means the object
    is not the envelope root, and a stopReason inside it is not a root field."""
    raw = (b"here is my answer: "
           + json.dumps({"structuredOutput": GOOD,
                         "stopReason": "Cancelled"}).encode())
    p = GrokAdapter().parse(raw, b"")
    assert p.stop_reason is None and not p.degraded


# --- the two explicit NON-signals -----------------------------------------


def test_auth_noise_is_not_degraded():
    err = (b"worker quit with fatal: Transport channel closed, "
           b"when Auth(AuthorizationRequired)")
    assert not GrokAdapter().parse(CLEAN, err).degraded


def test_structured_output_error_with_endturn_is_not_degraded():
    e = env(structuredOutput=None, structuredOutputError="no structured output",
            text=json.dumps(GOOD), stopReason="EndTurn")
    p = GrokAdapter().parse(e, b"")
    assert not p.degraded and p.parse_ok


def test_signal_precedence_stderr_first():
    """Ordering follows the oracle: stderr signals, token, stopReason, turns."""
    e = env(structuredOutput=GOOD, stopReason="Cancelled")
    assert "stderr" in GrokAdapter().parse(e, b"tool_error").degraded_reason


# --------------------------------------------------------------------------
# oracle parity for detect_degraded
# --------------------------------------------------------------------------

# (stdout, stderr, expected_degraded). Each row is fed to BOTH the extracted
# oracle shell function and this module's parser.
PARITY_CASES = [
    ("clean", CLEAN, b"", False),
    ("tool_error", CLEAN, b"tool_error: boom", True),
    ("Tool_Error-mixed-case", CLEAN, b"Tool_Error: boom", True),
    ("execution_failure", CLEAN, b"execution_failure", True),
    ("EXECUTION_FAILURE", CLEAN, b"EXECUTION_FAILURE", True),
    ("dropped-channel", CLEAN, b"dropped the response channel", True),
    ("Dropped-Channel", CLEAN, b"Dropped The Response Channel", True),
    ("harness-side", CLEAN, b"harness-side bug", True),
    ("harness space", CLEAN, b"harness side bug", True),
    ("HARNESS-SIDE", CLEAN, b"HARNESS-SIDE BUG", True),
    ("leaked-token", TOKEN_ENVELOPE, b"", True),
    ("token-in-stderr-only", CLEAN, LEAKED.encode("utf-8"), False),
    ("stopReason-Cancelled", env(structuredOutput=GOOD, stopReason="Cancelled"),
     b"", True),
    ("stopReason-absent", json.dumps({"structuredOutput": GOOD}).encode(),
     b"", False),
    ("stopReason-non-string",
     json.dumps({"structuredOutput": GOOD, "stopReason": 5}).encode(), b"",
     False),
    ("stopReason-empty",
     json.dumps({"structuredOutput": GOOD, "stopReason": ""}).encode(), b"",
     False),
    ("stopReason-in-summary",
     env(structuredOutput={"summary": '"stopReason": "Cancelled"',
                           "findings": []}), b"", False),
    ("max-turns-stderr", CLEAN, b"grok: max turns reached", True),
    ("max-turns-stdout-only",
     env(structuredOutput={"summary": "max turns reached", "findings": []}),
     b"", False),
    ("auth-noise", CLEAN,
     b"fatal: Transport channel closed, when Auth(AuthorizationRequired)",
     False),
    ("structuredOutputError-endturn",
     env(structuredOutput=None, structuredOutputError="x",
         text=json.dumps(GOOD), stopReason="EndTurn"), b"", False),
    ("non-json-stdout", b"plain prose answer, no envelope", b"", False),
    ("empty-both", b"", b"", False),
]

# Three known, deliberate divergences. All are strict supersets in the
# fail-safe direction — skodun flags everything the oracle flags, plus shapes
# the oracle goes silent on — so each is pinned by its own test below and kept
# OUT of the row-by-row sweep above rather than being dropped from it.

# (1) The oracle's turn-limit grep is `grep -Fq` (case-SENSITIVE, line 393),
# while skodun matches case-insensitively.
# Pinned by `test_oracle_is_case_sensitive_for_max_turns_and_skodun_is_not`.
MAX_TURNS_MIXED_CASE = b"Max Turns Reached"

# (2) The oracle's `grok_stop_reason` reads the root with `json.load`, which
# raises "Extra data" on ANY trailing byte, so a Cancelled envelope followed by
# one more line yields NO signal there. skodun reads the root with `raw_decode`
# and still sees the stopReason.
# Pinned by `test_oracle_misses_stop_reason_after_trailing_data`.
TRAILING_DATA_CANCELLED = (
    json.dumps({"structuredOutput": GOOD, "stopReason": "Cancelled"})
    + "\nVERDICT: clean\n").encode("utf-8")

# (3) skodun decodes the envelope with `errors="replace"` while the oracle
# opens it with a strict `encoding="utf-8"` `json.load` whose `except
# Exception` swallows the resulting UnicodeDecodeError. One invalid byte
# anywhere in the envelope therefore hides a Cancelled stopReason from the
# oracle and not from skodun — and invalid multibyte sequences occur in exactly
# the truncated runs this signal exists to catch.
# Pinned by `test_oracle_misses_stop_reason_in_invalid_utf8_envelope`.
INVALID_UTF8_CANCELLED = json.dumps(
    {"structuredOutput": GOOD, "stopReason": "Cancelled", "text": "MARK"},
).encode("utf-8").replace(b"MARK", b"\xff")


def _oracle_driver(tmp_path: Path) -> Path:
    """Extract `detect_degraded` + `grok_stop_reason` verbatim and wrap them."""
    src = (oracle_dir() / "scripts" / "grok-prepush-review.sh").read_text(
        encoding="utf-8")
    lines = src.splitlines()
    body: list[str] = []
    for fn in ("detect_degraded", "grok_stop_reason"):
        try:
            start = lines.index(f"{fn}() {{")
        except ValueError:  # pragma: no cover - oracle drift
            pytest.fail(f"oracle no longer defines {fn}() at column 0")
        end = start
        while lines[end] != "}":
            end += 1
        body.extend(lines[start:end + 1])
    assert "detect_degraded" in "\n".join(body)
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "\n".join(body)
        + '\nif detect_degraded "$1" "$2" >/dev/null 2>&1; then\n'
        '  echo DEGRADED\nelse\n  echo CLEAN\nfi\n',
        encoding="utf-8")
    return driver


def _run_oracle(tmp_path: Path, stdout: bytes, stderr: bytes) -> bool:
    of = tmp_path / "out.txt"
    ef = tmp_path / "err.txt"
    of.write_bytes(stdout)
    ef.write_bytes(stderr)
    r = subprocess.run(
        ["sh", str(_oracle_driver(tmp_path)), str(ef), str(of)],
        capture_output=True, text=True, check=True)
    assert r.stdout.strip() in ("DEGRADED", "CLEAN"), r
    return r.stdout.strip() == "DEGRADED"


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
@pytest.mark.parametrize(
    "name,stdout,stderr,expected",
    PARITY_CASES, ids=[c[0] for c in PARITY_CASES])
def test_detect_degraded_parity_with_oracle(tmp_path, name, stdout, stderr,
                                            expected):
    ours = GrokAdapter().parse(stdout, stderr).degraded
    theirs = _run_oracle(tmp_path, stdout, stderr)
    assert ours == expected, f"{name}: skodun said {ours}"
    assert theirs == expected, f"{name}: oracle said {theirs}"


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
def test_oracle_is_case_sensitive_for_max_turns_and_skodun_is_not(tmp_path):
    """Pins the one deliberate divergence from `detect_degraded`.

    Oracle line 393 uses `grep -Fq` (no `-i`); skodun lowercases first. Both
    agree on the corpus-observed lowercase spelling; skodun additionally flags
    mixed case, which is the fail-safe direction (a false positive costs one
    re-review; a false negative is a silent false all-clear).
    """
    assert _run_oracle(tmp_path, CLEAN, b"max turns reached") is True
    assert GrokAdapter().parse(CLEAN, b"max turns reached").degraded is True
    assert _run_oracle(tmp_path, CLEAN, MAX_TURNS_MIXED_CASE) is False
    assert GrokAdapter().parse(CLEAN, MAX_TURNS_MIXED_CASE).degraded is True


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
def test_oracle_misses_stop_reason_after_trailing_data(tmp_path):
    """Pins divergence (2): trailing bytes hide the stopReason from the oracle.

    `grok_stop_reason` uses `json.load`, so one extra line after the envelope
    makes it raise "Extra data", get swallowed by `except Exception`, and print
    "" — no signal, on a run the model CANCELLED, whose payload parses fine.
    skodun's `raw_decode` root read still sees it. Fail-safe direction: a false
    positive costs one re-review, a false negative is a silent false all-clear.

    If this ever fails because the oracle learned to parse resiliently, the fix
    is to delete the divergence note — never to loosen skodun.
    """
    assert _run_oracle(tmp_path, TRAILING_DATA_CANCELLED, b"") is False
    ours = GrokAdapter().parse(TRAILING_DATA_CANCELLED, b"")
    assert ours.degraded is True and ours.stop_reason == "Cancelled"
    # ...and the payload the oracle would have believed is genuinely valid, so
    # the divergence is an added signal rather than a parse difference.
    assert ours.parse_ok is True
    # The same envelope WITHOUT trailing bytes: both agree. The divergence is
    # confined to the trailing-data shape.
    plain = json.dumps({"structuredOutput": GOOD,
                        "stopReason": "Cancelled"}).encode("utf-8")
    assert _run_oracle(tmp_path, plain, b"") is True
    assert GrokAdapter().parse(plain, b"").degraded is True


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
def test_oracle_misses_stop_reason_in_invalid_utf8_envelope(tmp_path):
    """Pins divergence (3): a bad byte hides the stopReason from the oracle.

    The oracle opens the file with a strict `encoding="utf-8"`, so one invalid
    byte raises `UnicodeDecodeError`, `except Exception` swallows it, and the
    Cancelled run reads clean. skodun decodes with `errors="replace"` and still
    reads the root field.
    """
    assert b"\xff" in INVALID_UTF8_CANCELLED
    assert _run_oracle(tmp_path, INVALID_UTF8_CANCELLED, b"") is False
    ours = GrokAdapter().parse(INVALID_UTF8_CANCELLED, b"")
    assert ours.degraded is True and ours.stop_reason == "Cancelled"
    assert ours.parse_ok is True


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR not set")
def test_oracle_extraction_is_not_vacuous(tmp_path):
    """Guard the parity harness itself: a driver that always says CLEAN (or
    that failed to pick up the real function) must not pass as parity."""
    driver = _oracle_driver(tmp_path)
    text = driver.read_text(encoding="utf-8")
    assert "grep -Eiq" in text and "grok_stop_reason" in text
    assert _run_oracle(tmp_path, CLEAN, b"tool_error") is True
    assert _run_oracle(tmp_path, CLEAN, b"") is False


# --------------------------------------------------------------------------
# the shared conformance suite
# --------------------------------------------------------------------------


class TestGrokConformance(AdapterConformance):
    """grok is the first adapter through the registration gate.

    Everything asserted here is provider-neutral and lives in
    `tests/adapter_conformance.py`; this class supplies only the four things
    the mixin cannot know. The cases above stay as they are: they pin grok's
    own wire details (argv, the three-level extractor, each degraded signal in
    isolation, oracle parity), which the shared suite deliberately does not
    look at.
    """

    provider_id = "xai"
    fixture_dir = Path(__file__).parent / "fixtures" / "adapters" / "xai"

    def adapter(self) -> GrokAdapter:
        return GrokAdapter()

    def effort_reject_case(self) -> tuple[Reviewer, str] | None:
        """grok maps every canonical effort, but refuses `grok-build` models.

        The reject case is offered rather than `None` because a loud refusal is
        the stronger of the two proofs: it exercises the raise, where a total
        mapping only exercises a table. grok's mapping totality is pinned
        separately by `test_effort_appended_when_set` above.
        """
        r = Reviewer(name="f", provider="xai", model="grok-build",
                     role="finder", effort="high")
        return r, "does not support effort"
