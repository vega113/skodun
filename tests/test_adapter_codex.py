"""Tests for the codex adapter: invocation, event-stream parsing, degradation.

Three axes, deliberately independent, same shape as the grok suite:

* **build_cmd** — the flag list is a contract with an external binary, and
  every flag asserted here was accepted by codex-cli 0.144.5 during this
  task's probe. Asserted on the argv *list*, never a joined string, so a value
  containing a space cannot masquerade as two arguments. `build_cmd` also
  WRITES the `--output-schema` sidecar, so its content and its overwrite
  behaviour are tested here too.
* **parse** — finding the final agent message in a JSONL event stream and
  deciding `parse_ok` from it. A `parse_ok=True` record is, by the trust
  invariant, a record the gate is allowed to believe.
* **degraded** — positive evidence only. Each of the three signals (stderr
  wording, `turn.failed`, a stream with no terminal event) is exercised on its
  own so a passing test cannot be riding another signal, and the two
  non-signals (empty stdout, noisy-but-served stderr) are pinned negative.

The fixture bytes under `fixtures/adapters/openai/` are live captures; their
provenance is recorded in that directory's README.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skodun.adapters import (
    REFUTER_CONTRACT,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    get_adapter,
)
from skodun.adapters.codex import CodexAdapter, resolve_codex_bin, strict_schema
from skodun.config import Defaults, Reviewer
from tests.adapter_conformance import (  # noqa: F401 - see below
    AdapterConformance,
    load_fixture,
    test_coverage_gate_fails_without_a_conformance_subclass,
    test_every_registered_adapter_has_conformance_coverage,
    test_load_fixture_rejects_a_malformed_rc,
)
from tests.test_adapter_base import _recursion_bomb

# The three `test_*` functions above are imported, not re-declared: they are
# the registry coverage gate, its self-proof, and the fixture loader's
# contract, defined once next to the suite they gate. Importing them into a
# collected module is what makes pytest run them — `adapter_conformance.py` is
# a mixin module and is deliberately not collected.

FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "openai"

# A model id this CLI was seen to accept live (rc 0, `turn.completed`) during
# the probe. Nothing in this file names a model id that was not tried.
MODEL = "gpt-5.4-mini"

R = Reviewer(name="f", provider="openai", model=MODEL, role="finder")
D = Defaults()


@pytest.fixture(autouse=True)
def pinned_codex_bin(monkeypatch, tmp_path):
    """Never let a test resolve the developer's real `codex` on PATH.

    `argv[0]` would otherwise vary by machine. The binary-resolution tests
    below override this explicitly, which is the point: resolution order is
    tested only where it is the subject.
    """
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "pinned" / "codex"))


def fx(name: str):
    return load_fixture(FIXTURES / f"{name}.txt")


# --------------------------------------------------------------------------
# binary resolution
# --------------------------------------------------------------------------


def test_binary_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "elsewhere"))
    assert resolve_codex_bin() == str(tmp_path / "elsewhere")


def test_binary_falls_back_to_path_name(monkeypatch):
    monkeypatch.delenv("SKODUN_CODEX_BIN", raising=False)
    assert resolve_codex_bin() == "codex"


def test_empty_override_is_not_an_override(monkeypatch):
    """An exported-but-empty variable must not resolve to `""`."""
    monkeypatch.setenv("SKODUN_CODEX_BIN", "")
    assert resolve_codex_bin() == "codex"


def test_registry_serves_the_codex_adapter():
    a = get_adapter("openai")
    assert isinstance(a, CodexAdapter)
    assert a.name == "codex" and a.provider == "openai"


def test_prompt_travels_on_stdin():
    """The CLI has no input-file flag, so the runner must feed the file in."""
    assert CodexAdapter.stdin_from_prompt_file is True


# --------------------------------------------------------------------------
# the strict-mode schema projection
# --------------------------------------------------------------------------


def test_strict_schema_closes_every_object_and_requires_every_key():
    """OpenAI structured outputs reject anything else — verified live.

    The unprojected contract schema came back as a 400
    (`'additionalProperties' is required to be supplied and to be false`,
    then `'required' ... including every key in properties. Missing 'line'`),
    which is what `degraded_turn_failed.txt` captured.
    """
    s = json.loads(strict_schema(REVIEW_CONTRACT.json_schema))
    assert s["additionalProperties"] is False
    assert sorted(s["required"]) == sorted(s["properties"])
    item = s["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == sorted(item["properties"])


def test_strict_schema_makes_contract_optionals_nullable():
    """A property the contract does not require becomes `["<t>", "null"]`.

    Strict mode cannot express an absent key. Widening the type is how the
    model is still allowed to say "I do not know this one", and `parse` then
    translates that `null` back into the contract's spelling of absence.
    """
    s = json.loads(strict_schema(REVIEW_CONTRACT.json_schema))
    props = s["properties"]["findings"]["items"]["properties"]
    assert props["line"]["type"] == ["integer", "null"]
    assert props["category"]["type"] == ["string", "null"]
    # …and a REQUIRED property is left exactly as the contract spells it.
    assert props["file"]["type"] == "string"
    assert props["severity"]["enum"] == ["high", "medium", "low"]


def test_strict_schema_leaves_a_fully_required_contract_alone():
    """The refuter contract requires every key, so only the closures appear."""
    s = json.loads(strict_schema(REFUTER_CONTRACT.json_schema))
    item = s["properties"]["verdicts"]["items"]
    assert sorted(item["required"]) == ["index", "reasoning", "verdict"]
    assert item["properties"]["index"]["type"] == "integer"
    assert item["properties"]["verdict"]["enum"] == [
        "confirmed", "refuted", "uncertain"]


def test_strict_schema_is_a_single_line():
    """It is written to a file, but a one-line schema keeps diffs readable."""
    assert "\n" not in strict_schema(REVIEW_CONTRACT.json_schema)


# --------------------------------------------------------------------------
# build_cmd
# --------------------------------------------------------------------------


def test_build_cmd_shape(tmp_path):
    prompt = tmp_path / "prompt.txt"
    cmd = CodexAdapter().build_cmd(prompt, R, D, tmp_path)
    assert cmd[1] == "exec"
    assert cmd[-1] == "-", "argv must end in the CLI's stdin marker"
    assert "--json" in cmd
    assert cmd[cmd.index("-m") + 1] == MODEL
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--color") + 1] == "never"
    # Never inherit the CLI's own settings file, and never let the prompt be
    # interpolated into the argv.
    assert str(prompt) not in cmd


def test_build_cmd_disables_web_search(tmp_path):
    """The reviewer reads a diff, not the internet — and `-c web_search=off`
    is not a spelling the CLI takes (`expected string only` / unknown value);
    `disabled` is the one it accepted."""
    cmd = CodexAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    assert "web_search=disabled" in cmd


def test_build_cmd_writes_the_schema_sidecar(tmp_path):
    prompt = tmp_path / "prompt.txt"
    cmd = CodexAdapter().build_cmd(prompt, R, D, tmp_path)
    sidecar = prompt.with_suffix(".schema.json")
    assert cmd[cmd.index("--output-schema") + 1] == str(sidecar)
    assert sidecar.read_text(encoding="utf-8") == strict_schema(
        REVIEW_CONTRACT.json_schema)


def test_schema_sidecar_is_always_overwritten(tmp_path):
    """A stale sidecar from an earlier contract must never survive.

    Two attempts share a prompt directory; the second asking for a different
    response shape and finding the first one's schema would ask the model for
    a review and validate it as a refuter reply, or the reverse.
    """
    prompt = tmp_path / "prompt.txt"
    a = CodexAdapter()
    a.build_cmd(prompt, R, D, tmp_path, REFUTER_CONTRACT)
    sidecar = prompt.with_suffix(".schema.json")
    assert "verdicts" in sidecar.read_text(encoding="utf-8")
    a.build_cmd(prompt, R, D, tmp_path, REVIEW_CONTRACT)
    text = sidecar.read_text(encoding="utf-8")
    assert "verdicts" not in text and "findings" in text


def test_schema_sidecar_is_utf8(tmp_path):
    """Explicit encoding, per the global text-I/O rule."""
    prompt = tmp_path / "prompt.txt"
    CodexAdapter().build_cmd(prompt, R, D, tmp_path)
    raw = prompt.with_suffix(".schema.json").read_bytes()
    assert raw.decode("utf-8") == strict_schema(REVIEW_CONTRACT.json_schema)


@pytest.mark.parametrize("canonical,cli", [
    ("none", "none"), ("low", "low"), ("medium", "medium"),
    ("high", "high"), ("max", "xhigh"),
])
def test_effort_is_passed_as_a_config_override(tmp_path, canonical, cli):
    """Every canonical effort maps, and the CLI value is one the API takes.

    The API enumerates `none, minimal, low, medium, high, xhigh, max`, but the
    models this CLI offers accept only `none, low, medium, high, xhigh` — the
    probe had `minimal` refused with "Unsupported value: 'minimal' is not
    supported with the '...' model". So `none` maps to the API's own `none`
    rather than to `minimal`, and `max` maps DOWN to `xhigh`, which every
    listed model supports.
    """
    r = Reviewer(name="f", provider="openai", model=MODEL, role="finder",
                 effort=canonical)
    cmd = CodexAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)
    assert f"model_reasoning_effort={cli}" in cmd


def test_unset_effort_passes_no_effort_flag(tmp_path):
    """`effort = None` is "unset", and an unset flag is not an invented one."""
    cmd = CodexAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    assert not any(c.startswith("model_reasoning_effort=") for c in cmd)


def test_unknown_effort_is_loud(tmp_path):
    """A dropped `--effort` reviews at the CLI's default and lies about it."""
    a = CodexAdapter()
    r = Reviewer(name="f", provider="openai", model=MODEL, role="finder")
    object.__setattr__(r, "effort", "turbo")   # bypass config validation
    with pytest.raises(ValueError, match="no CLI value for effort"):
        a.build_cmd(Path("p.txt"), r, D, Path("."))


def test_effort_map_is_a_copy():
    a = CodexAdapter()
    a.effort_map()["high"] = "tampered"
    assert a.effort_map()["high"] == "high"


# --------------------------------------------------------------------------
# parse — the event stream
# --------------------------------------------------------------------------


def test_healthy_capture_parses():
    f = fx("healthy")
    res = CodexAdapter().parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is True
    assert res.degraded is False
    assert res.stop_reason == "turn.completed"
    assert len(res.findings) == 3
    assert res.findings[0]["title"] == "Unsafe SQL string concatenation in lookup"
    assert res.summary.startswith("The patch introduces")
    assert res.payload is not None


def test_null_optionals_are_stripped_not_rejected():
    """Strict mode spells "absent" as `null`; the contract spells it "absent".

    `_valid_payload` rejects `{"line": null}` outright (a non-int `line`), so
    without this translation every finding whose line the model could not
    determine would fail the whole payload and the review would be retried
    forever. The fixture is a live capture, so this is what real bytes do.
    """
    f = fx("healthy_noisy_stderr")
    res = CodexAdapter().parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is True
    assert res.findings, "fixture must carry findings for this to mean anything"
    for finding in res.findings:
        assert "line" not in finding
        assert "category" not in finding
    # …and the fixture really does carry the nulls being stripped. The payload
    # is a JSON string INSIDE the event line, so the quotes are escaped once.
    assert rb'\"line\":null' in f.stdout
    assert rb'\"category\":null' in f.stdout


def test_last_agent_message_wins():
    """A codex turn narrates before it answers; the answer is the last one."""
    stream = b"\n".join([
        b'{"type":"thread.started","thread_id":"t"}',
        b'{"type":"turn.started"}',
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": json.dumps({"summary": "draft", "findings": []})}}
        ).encode("utf-8"),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": json.dumps({"summary": "final", "findings": []})}}
        ).encode("utf-8"),
        b'{"type":"turn.completed","usage":{}}',
        b"",
    ])
    res = CodexAdapter().parse(stream, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.summary == "final"


def test_reasoning_and_command_items_are_not_candidates():
    """Only `agent_message` items carry the answer.

    A `command_execution` item's `aggregated_output` routinely contains JSON
    the model merely READ. Treating it as the answer would report a file's
    contents as a review.
    """
    stream = json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "cat x",
        "aggregated_output": json.dumps({"summary": "not mine", "findings": []}),
    }}).encode("utf-8") + b'\n{"type":"turn.completed"}\n'
    res = CodexAdapter().parse(stream, b"", REVIEW_CONTRACT)
    assert res.parse_ok is False and res.payload is None


def test_prose_wrapped_payload_is_recovered():
    """The model sometimes fences its JSON even under an output schema."""
    text = "Here you go:\n```json\n" + json.dumps(
        {"summary": "s", "findings": []}) + "\n```"
    stream = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": text}}).encode("utf-8") + \
        b'\n{"type":"turn.completed"}\n'
    res = CodexAdapter().parse(stream, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.summary == "s"


def test_a_malformed_line_does_not_lose_the_stream():
    """A half-written line is a real mid-write capture, not a parse failure."""
    good = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message",
        "text": json.dumps({"summary": "s", "findings": []})}})
    stream = (b'{"type":"thread.star\n' + good.encode("utf-8")
              + b'\n{"type":"turn.completed"}\n')
    assert CodexAdapter().parse(stream, b"", REVIEW_CONTRACT).parse_ok is True


@pytest.mark.parametrize("contract", [REVIEW_CONTRACT, REFUTER_CONTRACT])
def test_deeply_nested_output_does_not_raise(contract):
    """`json` signals "too deeply nested" with RecursionError, not ValueError.

    A `RuntimeError` subclass sails straight past an `except ValueError`, so
    each of this adapter's TWO decode sites has to catch it separately, and
    both are exercised here:

    * the payload scan, with the bomb inside an `agent_message` — the same
      site grok has;
    * the event scan, with the bomb as an event LINE. That one is codex's
      alone: grok decodes one envelope, this decodes every line of untrusted
      stdout, and a line that defeats the decoder must be a skipped line
      rather than an exception in the gate path.

    The bomb is built by the probe in `test_adapter_base` rather than
    hardcoded: the depth at which `json` gives up is a property of the C stack
    and differs between builds, so a fixed number would quietly stop being
    deep enough.
    """
    bomb = _recursion_bomb()
    a = CodexAdapter()
    for stdout in (
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message",
                             "text": bomb.decode("utf-8")}}).encode("utf-8"),
        bomb + b"\n",
    ):
        res = a.parse(stdout, b"", contract)
        assert res.parse_ok is False and res.payload is None
        assert res.findings == [] and res.summary == ""
        assert a.classify(0, stdout, b"", contract).kind in {
            "ok", "degraded", "unavailable"}


def test_refuter_capture_parses_and_is_not_a_review():
    f = fx("refuter_healthy")
    a = CodexAdapter()
    res = a.parse(f.stdout, f.stderr, REFUTER_CONTRACT)
    assert res.parse_ok is True
    assert [v["verdict"] for v in res.payload["verdicts"]] == [
        "confirmed", "confirmed", "refuted"]
    assert res.findings == [] and res.summary == ""
    assert a.parse(f.stdout, f.stderr, REVIEW_CONTRACT).parse_ok is False


# --------------------------------------------------------------------------
# degraded — each signal alone
# --------------------------------------------------------------------------


def test_missing_terminal_event_is_degraded():
    f = fx("degraded_no_turn_completed")
    res = CodexAdapter().parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is True, "the payload is complete — that is the danger"
    assert res.degraded is True
    assert res.stop_reason is None
    assert "terminal" in res.degraded_reason or "complete" in res.degraded_reason


def test_turn_failed_is_degraded_not_unavailable():
    f = fx("degraded_turn_failed")
    a = CodexAdapter()
    assert a.parse(f.stdout, f.stderr, REVIEW_CONTRACT).degraded is True
    assert a.classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "degraded"


def test_stderr_disconnect_wording_is_degraded():
    f = fx("degraded_stderr")
    a = CodexAdapter()
    res = a.parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is True and res.degraded is True
    assert a.classify(0, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "degraded"


def test_empty_stdout_is_not_degraded():
    """Absence of a signal is never taken as proof of anything."""
    res = CodexAdapter().parse(b"", b"", REVIEW_CONTRACT)
    assert res.parse_ok is False and res.degraded is False
    assert CodexAdapter().classify(0, b"", b"", REVIEW_CONTRACT).kind == "ok"


def test_finding_text_is_never_a_degraded_signal():
    """A review that DISCUSSES a dropped stream is not a dropped stream."""
    payload = {"summary": "s", "findings": [{
        "file": "net.py", "severity": "low",
        "title": "stream disconnected before completion is unhandled",
        "detail": "turn.failed and stream closed before response.completed"}]}
    stream = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps(payload)}}).encode("utf-8") \
        + b'\n{"type":"turn.completed"}\n'
    a = CodexAdapter()
    res = a.parse(stream, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.degraded is False
    assert a.classify(0, stream, b"", REVIEW_CONTRACT).kind == "ok"


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------


def test_missing_binary():
    res = CodexAdapter().classify(UNAVAILABLE_RC, b"", b"", REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "binary"


def test_auth_capture_is_unavailable_auth():
    f = fx("unavailable_auth")
    res = CodexAdapter().classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "auth"


def test_model_capture_is_unavailable_model_from_the_stream_alone():
    """stderr is empty in this capture; the only evidence is in the events."""
    f = fx("unavailable_model")
    assert f.stderr == b""
    res = CodexAdapter().classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "model"


def test_quota_wording_is_unavailable_quota():
    err = b"ERROR codex_api: 429 Too Many Requests: rate limit exceeded\n"
    res = CodexAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "quota"


def test_auth_beats_quota_when_both_appear():
    """`quota` is the only provider-wide-cacheable category, so anything that
    also looks attempt-local is reported as that instead."""
    err = b"401 Unauthorized while checking your rate limit quota\n"
    res = CodexAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "auth"


def test_usable_output_wins_over_auth_noise():
    f = fx("healthy_noisy_stderr")
    a = CodexAdapter()
    assert b"401 Unauthorized" in f.stderr
    assert a.classify(0, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "ok"
    # …and the same stderr with nothing on stdout is genuinely unavailable, so
    # the case above is not vacuous.
    assert a.classify(1, b"", f.stderr, REVIEW_CONTRACT).kind == "unavailable"


def test_usable_output_does_not_win_over_degradation():
    """Availability and degradation are different questions.

    "The provider served" is proved by the payload; "the answer is complete"
    is not. Conflating them re-introduces the Phase 1 silent false all-clear.
    """
    f = fx("degraded_no_turn_completed")
    res = CodexAdapter().classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "degraded" and res.category == ""


def test_classify_never_reads_agent_message_text():
    """Signal words inside the model's own answer cannot make a run
    unavailable — otherwise a review OF an auth bug takes the provider down."""
    payload = {"summary": "401 Unauthorized handling is missing",
               "findings": [{"file": "a.py", "severity": "low",
                             "title": "quota rate limit is not honoured",
                             "detail": "429 Too Many Requests is swallowed"}]}
    stream = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps(payload)}}).encode("utf-8") \
        + b'\n{"type":"turn.completed"}\n'
    res = CodexAdapter().classify(0, stream, b"", REVIEW_CONTRACT)
    assert res.kind == "ok"


# --------------------------------------------------------------------------
# the registration gate
# --------------------------------------------------------------------------


class TestCodexConformance(AdapterConformance):
    """codex is the first adapter through the gate that was not retrofitted.

    Everything asserted here is provider-neutral and lives in
    `tests/adapter_conformance.py`; this class supplies only the four things
    the mixin cannot know. The cases above stay as they are: they pin codex's
    own wire details (argv, the strict-schema projection, the event scan, each
    degraded signal in isolation), which the shared suite deliberately does
    not look at.
    """

    provider_id = "openai"
    fixture_dir = FIXTURES

    def adapter(self) -> CodexAdapter:
        return CodexAdapter()

    def effort_reject_case(self) -> None:
        """codex maps every canonical effort, so there is nothing to refuse.

        Returning None makes rule 5 verify that claim against `config.EFFORTS`
        rather than taking it on trust — which is the whole reason the
        totality claim is spelled as a return value and not a comment.
        """
        return None
