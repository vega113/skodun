"""Tests for the agy adapter: invocation, envelope parsing, degradation.

Three axes, deliberately independent, same shape as the grok and codex suites:

* **build_cmd** — the flag list is a contract with an external binary, and
  every flag asserted here was accepted by agy 1.1.8 during this task's probe.
  Asserted on the argv *list*, never a joined string, so a value containing a
  space cannot masquerade as two arguments. This CLI has no prompt-file flag
  and does not read the prompt from stdin, so `build_cmd` reads the prompt FILE
  and places its text in the argv — the single deviation from the "prompt
  travels as a file" rule, and the reason the size guard below exists.
* **parse** — finding the payload in a single JSON envelope whose
  `structured_output` is the authoritative copy, with `response` and a raw scan
  behind it.
* **degraded** — positive evidence only. Each signal (stderr wording, a
  terminal `status` that is not SUCCESS) is exercised on its own so a passing
  test cannot be riding another, and the non-signals (empty stdout, noisy but
  served stderr, finding text) are pinned negative.

The fixture bytes under `fixtures/adapters/google/` are live captures; their
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
from skodun.adapters.agy import (
    MAX_PROMPT_ARG_BYTES,
    AgyAdapter,
    resolve_agy_bin,
    _AUTH_SIGNALS,
    _DEGRADED_STDERR_SIGNALS,
    _INVOCATION_SIGNALS,
    _MODEL_SIGNALS,
    _QUOTA_SIGNALS,
)
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

FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "google"

# A model id this CLI was seen to accept live (rc 0, `status: SUCCESS`) during
# the probe, listed by `agy models`' own base-id form. Nothing in this file
# names a model id that was not tried.
#
# It is the BASE id deliberately. `agy models` also lists effort-suffixed ids
# (`gemini-3.6-flash-low`), and those REFUSE any `--effort` that disagrees with
# the suffix — "conflicts with --effort=medium", rc 1. A base id is the only
# spelling for which this adapter's effort table is meaningful, and the base id
# in turn REQUIRES `--effort`. See `test_effort_conflict_capture_is_model`.
MODEL = "gemini-3.6-flash"

R = Reviewer(name="f", provider="google", model=MODEL, role="finder")
D = Defaults()


@pytest.fixture(autouse=True)
def pinned_agy_bin(monkeypatch, tmp_path):
    """Never let a test resolve the developer's real `agy` on PATH.

    `argv[0]` would otherwise vary by machine. The binary-resolution tests
    below override this explicitly, which is the point: resolution order is
    tested only where it is the subject.
    """
    monkeypatch.setenv("SKODUN_AGY_BIN", str(tmp_path / "pinned" / "agy"))


def fx(name: str):
    return load_fixture(FIXTURES / f"{name}.txt")


def written(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "prompt.txt"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# binary resolution
# --------------------------------------------------------------------------


def test_binary_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SKODUN_AGY_BIN", str(tmp_path / "elsewhere"))
    assert resolve_agy_bin() == str(tmp_path / "elsewhere")


def test_binary_falls_back_to_path_name(monkeypatch):
    monkeypatch.delenv("SKODUN_AGY_BIN", raising=False)
    assert resolve_agy_bin() == "agy"


def test_empty_override_is_not_an_override(monkeypatch):
    """An exported-but-empty variable must not resolve to `""`."""
    monkeypatch.setenv("SKODUN_AGY_BIN", "")
    assert resolve_agy_bin() == "agy"


def test_registry_serves_the_agy_adapter():
    a = get_adapter("google")
    assert isinstance(a, AgyAdapter)
    assert a.name == "agy" and a.provider == "google"


def test_prompt_does_not_travel_on_stdin():
    """The probe proved this CLI ignores stdin in print mode.

    `printf 'The secret word is BANANA.' | agy --print '<question>'` answered
    NONE, and `--print -` sent the literal string `-` rather than reading the
    file. So the runner must NOT open the prompt file as the child's stdin:
    doing so would leave a reader nobody reads while the prompt still had to
    reach the model some other way.
    """
    assert AgyAdapter.stdin_from_prompt_file is False


# --------------------------------------------------------------------------
# build_cmd
# --------------------------------------------------------------------------


def test_build_cmd_shape(tmp_path):
    prompt = written(tmp_path, "review this\n")
    cmd = AgyAdapter().build_cmd(prompt, R, D, tmp_path)
    assert cmd[0].endswith("agy")
    assert cmd[cmd.index("--model") + 1] == MODEL
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--print-timeout") + 1] == f"{D.timeout_sec}s"


def test_build_cmd_carries_the_prompt_text_not_its_path(tmp_path):
    """The one deviation from "the prompt travels as a file", stated loudly.

    agy 1.1.8 has no `--prompt-file` and ignores stdin in print mode (see
    `test_prompt_does_not_travel_on_stdin`), so the ONLY channel the installed
    binary offers is the `--print` argv value. No shell is involved — the argv
    is a list handed to `subprocess` — so nothing is shell-interpolated; what
    changes is who holds the bytes.
    """
    prompt = written(tmp_path, "review this diff\n")
    cmd = AgyAdapter().build_cmd(prompt, R, D, tmp_path)
    assert cmd[cmd.index("--print") + 1] == "review this diff\n"
    assert str(prompt) not in cmd


def test_build_cmd_reads_the_prompt_as_utf8(tmp_path):
    """Explicit encoding, per the global text-I/O rule."""
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes("héllo — ünicode\n".encode("utf-8"))
    cmd = AgyAdapter().build_cmd(prompt, R, D, tmp_path)
    assert cmd[cmd.index("--print") + 1] == "héllo — ünicode\n"


def test_build_cmd_inlines_the_contract_schema_verbatim(tmp_path):
    """`--json-schema` took the contract's own schema, unprojected.

    The probe handed the contract schema to the CLI byte-for-byte and got a
    `structured_output` back, so unlike codex there is no strict-mode
    projection to apply and nothing to translate on the way out. Inline rather
    than a sidecar file, so `build_cmd` writes nothing and there is no stale
    schema for a second attempt in the same directory to inherit.
    """
    cmd = AgyAdapter().build_cmd(written(tmp_path, "x"), R, D, tmp_path)
    assert cmd[cmd.index("--json-schema") + 1] == REVIEW_CONTRACT.json_schema
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prompt.txt"], (
        "build_cmd must not write a sidecar")


def test_build_cmd_asks_for_the_requested_contract(tmp_path):
    cmd = AgyAdapter().build_cmd(written(tmp_path, "x"), R, D, tmp_path,
                                 REFUTER_CONTRACT)
    assert cmd[cmd.index("--json-schema") + 1] == REFUTER_CONTRACT.json_schema


def test_build_cmd_never_skips_permissions(tmp_path):
    """A reviewer must not be able to execute anything.

    `--dangerously-skip-permissions` auto-approves every tool request. Without
    it, and with `--sandbox`, a tool call is auto-denied and the CLI says so on
    stderr — which is the `degraded_stderr` capture. Denied-and-noisy is the
    correct outcome for a reviewer; silently running commands is not.
    """
    cmd = AgyAdapter().build_cmd(written(tmp_path, "x"), R, D, tmp_path)
    assert "--dangerously-skip-permissions" not in cmd
    assert "--add-dir" not in cmd


def test_oversize_prompt_is_refused_loudly(tmp_path):
    """A prompt too large for an argv element fails closed, not at exec().

    A single argv element is capped at 128 KiB on Linux (`MAX_ARG_STRLEN`) and
    the whole argv at `ARG_MAX` elsewhere. Left unguarded, an oversize prompt
    surfaces as an `OSError` from `subprocess` — an unexpected exception in the
    gate path rather than a reviewer that could not be invoked. `build_cmd`'s
    `ValueError` is the shape `pipeline._run_chain` already turns into a
    stopped chain with a stated reason.
    """
    prompt = written(tmp_path, "x" * (MAX_PROMPT_ARG_BYTES + 1))
    with pytest.raises(ValueError, match="prompt is too large"):
        AgyAdapter().build_cmd(prompt, R, D, tmp_path)


def test_a_prompt_at_the_limit_is_accepted(tmp_path):
    """The guard is a ceiling, not an off-by-one that rejects the limit."""
    prompt = written(tmp_path, "x" * MAX_PROMPT_ARG_BYTES)
    cmd = AgyAdapter().build_cmd(prompt, R, D, tmp_path)
    assert len(cmd[cmd.index("--print") + 1]) == MAX_PROMPT_ARG_BYTES


def test_the_guard_measures_bytes_not_characters(tmp_path):
    """`MAX_ARG_STRLEN` counts bytes; a multibyte prompt must not slip past."""
    prompt = tmp_path / "prompt.txt"
    # Every character is 3 bytes in UTF-8, so this is under the limit in
    # characters and over it in bytes.
    prompt.write_bytes("あ".encode("utf-8") * (MAX_PROMPT_ARG_BYTES // 3 + 1))
    with pytest.raises(ValueError, match="prompt is too large"):
        AgyAdapter().build_cmd(prompt, R, D, tmp_path)


def test_non_utf8_prompt_is_refused_loudly(tmp_path):
    """A latin-1 source file's diff is not UTF-8 decodable; refuse, don't abort.

    Reachable on a normal path: `git --no-pager diff --no-ext-diff --no-textconv`
    over a latin-1 source file emits raw bytes (`gitio.py`), and
    `promptbuild.py` concatenates them into the prompt untouched. Unguarded,
    `prompt_file.read_text(encoding="utf-8")` raises a bare
    `UnicodeDecodeError` out of `build_cmd`, which `pipeline._run_chain`
    (only `except Exception` around the *build*, not the decode specifically)
    still turns into "could not build the invocation" — but the guard makes
    that outcome explicit and tested rather than an accident of exception
    inheritance, and gives a message that says WHY in adapter terms instead of
    a raw codec error.

    A realistic prompt file: a diff-shaped hunk carrying one latin-1-encoded
    accented byte sequence that latin-1 -> UTF-8 confusion actually produces
    (`é` as a single 0xE9 byte, not the two-byte UTF-8 encoding).
    """
    prompt = tmp_path / "prompt.txt"
    diff_hunk = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-DEFAULT_NAME = \"cafe\"\n"
        "+DEFAULT_NAME = \"caf\xe9\"\n"  # a real, standalone invalid-UTF-8 byte
    ).encode("latin-1")
    prompt.write_bytes(diff_hunk)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        AgyAdapter().build_cmd(prompt, R, D, tmp_path)


def test_embedded_nul_in_prompt_is_refused_loudly(tmp_path):
    """A NUL in the prompt must not reach `subprocess.Popen` unguarded.

    `subprocess.Popen` raises `ValueError: embedded null byte` for a NUL
    anywhere in argv, and that raise happens in `runner.py` -- outside
    `build_cmd` -- where `pipeline._run_chain` catches only
    `FileNotFoundError` around the run call, so an unguarded NUL propagates
    all the way out of `_run_chain` as an unexpected exception. NUL is valid
    UTF-8 (the single byte 0x00), so the UTF-8 decode above does not catch it
    -- this is a genuinely separate precondition needing its own check.

    Realistic: a NUL survives into a diff when it is past git's first-8000-
    byte binary-file detection heuristic, so a mixed text/binary-looking file
    can carry one through to the prompt.
    """
    prompt = tmp_path / "prompt.txt"
    diff_hunk = (
        "diff --git a/data.bin b/data.bin\n"
        "--- a/data.bin\n"
        "+++ b/data.bin\n"
        "@@ -1,2 +1,2 @@\n"
        "-header\n"
        "+header\x00trailer\n"
    )
    prompt.write_text(diff_hunk, encoding="utf-8")
    with pytest.raises(ValueError, match="embedded NUL byte"):
        AgyAdapter().build_cmd(prompt, R, D, tmp_path)


def test_non_utf8_check_runs_before_the_size_check(tmp_path):
    """Decode-and-NUL guards fire even on a prompt well under the size cap.

    Pins the ORDER a future edit could quietly break: if the size check ran
    first and short-circuited, a small non-UTF-8 or NUL-carrying prompt would
    sail through to `subprocess` unguarded while only an oversize one was
    protected.
    """
    small_bad = tmp_path / "prompt.txt"
    small_bad.write_bytes(b"tiny diff with one bad byte: \xe9\n")
    assert len(small_bad.read_bytes()) < MAX_PROMPT_ARG_BYTES
    with pytest.raises(ValueError, match="not valid UTF-8"):
        AgyAdapter().build_cmd(small_bad, R, D, tmp_path)


@pytest.mark.parametrize("canonical", ["low", "medium", "high"])
def test_effort_maps_through(tmp_path, canonical):
    """The three levels `--effort` accepts, verbatim.

    `agy --effort max` was refused live with `invalid --effort "max" (valid:
    low, medium, high)`, and so were `none`, `xhigh` and `minimal`.
    """
    r = Reviewer(name="f", provider="google", model=MODEL, role="finder",
                 effort=canonical)
    cmd = AgyAdapter().build_cmd(written(tmp_path, "x"), r, D, tmp_path)
    assert cmd[cmd.index("--effort") + 1] == canonical


@pytest.mark.parametrize("effort", [None, "none"])
def test_effort_opt_out_passes_no_flag(tmp_path, effort):
    """`None` is unset and `"none"` is the explicit opt-out; neither invents a
    flag. The CLI then applies whatever the model id itself implies — which for
    a base id is an error it reports loudly, not a silent default."""
    r = Reviewer(name="f", provider="google", model=MODEL, role="finder",
                 effort=effort)
    cmd = AgyAdapter().build_cmd(written(tmp_path, "x"), r, D, tmp_path)
    assert "--effort" not in cmd


def test_max_effort_is_loudly_rejected(tmp_path):
    """There is no fourth level, so `max` must raise rather than downgrade.

    Quietly sending `high` in place of `max` reviews at a weaker setting and
    reports the result as the configured one — the unnoticed downgrade the
    explicit-effort rule exists to prevent.
    """
    r = Reviewer(name="f", provider="google", model=MODEL, role="finder",
                 effort="max")
    with pytest.raises(ValueError, match="no CLI value for effort"):
        AgyAdapter().build_cmd(written(tmp_path, "x"), r, D, tmp_path)


def test_unknown_effort_is_loud(tmp_path):
    r = Reviewer(name="f", provider="google", model=MODEL, role="finder")
    object.__setattr__(r, "effort", "turbo")   # bypass config validation
    with pytest.raises(ValueError, match="no CLI value for effort"):
        AgyAdapter().build_cmd(written(tmp_path, "x"), r, D, tmp_path)


def test_effort_map_is_a_copy():
    a = AgyAdapter()
    a.effort_map()["high"] = "tampered"
    assert a.effort_map()["high"] == "high"


# --------------------------------------------------------------------------
# parse — the envelope
# --------------------------------------------------------------------------


def test_healthy_capture_parses():
    f = fx("healthy")
    res = AgyAdapter().parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is True
    assert res.degraded is False
    assert res.stop_reason == "SUCCESS"
    assert len(res.findings) == 3
    assert res.findings[0]["title"] == (
        "Unhandled KeyError when fetching non existent key")
    assert res.summary.startswith("Reviewed the cache implementation")
    assert res.payload is not None


def test_structured_output_is_preferred_over_the_response_text():
    """Two copies of the answer ride in one envelope; the validated one wins.

    `response` is the model's raw text — the probe saw it carry a ```json fence
    and, once, the payload twice at two different indentations.
    `structured_output` is what the CLI validated against `--json-schema`.
    """
    envelope = json.dumps({
        "status": "SUCCESS",
        "response": json.dumps({"summary": "from response", "findings": []}),
        "structured_output": {"summary": "from structured", "findings": []},
    }).encode("utf-8")
    res = AgyAdapter().parse(envelope, b"", REVIEW_CONTRACT)
    assert res.summary == "from structured"


def test_response_text_is_the_fallback_when_there_is_no_structured_output():
    """No `--json-schema` echo, or a run the CLI could not validate."""
    envelope = json.dumps({
        "status": "SUCCESS",
        "response": "Here you go:\n```json\n"
                    + json.dumps({"summary": "s", "findings": []}) + "\n```",
    }).encode("utf-8")
    res = AgyAdapter().parse(envelope, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.summary == "s"


def test_an_empty_structured_output_does_not_mask_the_response():
    """`{}` is not an eligible candidate, so extraction falls through."""
    envelope = json.dumps({
        "status": "SUCCESS",
        "structured_output": {},
        "response": json.dumps({"summary": "s", "findings": []}),
    }).encode("utf-8")
    assert AgyAdapter().parse(envelope, b"", REVIEW_CONTRACT).summary == "s"


def test_a_bare_payload_with_no_envelope_is_still_read():
    """The last-resort raw scan: output that is not the CLI's envelope at all."""
    raw = json.dumps({"summary": "s", "findings": []}).encode("utf-8")
    res = AgyAdapter().parse(raw, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.stop_reason is None


@pytest.mark.parametrize("contract", [REVIEW_CONTRACT, REFUTER_CONTRACT])
def test_the_echoed_json_schema_can_never_pass_as_a_payload(contract):
    """The envelope echoes the schema it was given, and the schema LOOKS eligible.

    `--json-schema` comes back as a `json_schema` key, and the raw scan behind
    the two envelope levels walks every `{` in stdout — so it can and does
    reach the schema's own `properties` object, which carries the keys
    `summary`/`findings` (or `verdicts`) and is therefore *eligible*. That is
    only ever harmless because the contract's VALIDATOR refuses it: `summary`
    there is `{"type":"string"}`, a dict, and `verdicts` is a dict rather than
    a list.

    Pinned as a test rather than trusted as an argument, because "eligible but
    never valid" is the kind of property a future schema edit could break
    silently — and the consequence would be a `parse_ok=True` review whose
    entire content is the schema we sent.
    """
    f = fx("degraded_timeout")
    a = AgyAdapter()
    # The premise: this capture really does echo the schema and carry no
    # answer, so the raw scan is genuinely reached.
    assert b'"json_schema":' in f.stdout
    assert a.parse(f.stdout, f.stderr, contract).parse_ok is False
    assert a.parse(f.stdout, f.stderr, contract).payload is None


def test_a_truncated_envelope_yields_nothing_rather_than_half_a_review():
    res = AgyAdapter().parse(b'{"status":"SUCCESS","structured_out',
                             b"", REVIEW_CONTRACT)
    assert res.parse_ok is False and res.payload is None


@pytest.mark.parametrize("contract", [REVIEW_CONTRACT, REFUTER_CONTRACT])
def test_deeply_nested_output_does_not_raise(contract):
    """`json` signals "too deeply nested" with RecursionError, not ValueError.

    A `RuntimeError` subclass sails straight past an `except ValueError`, and
    64 KB of `[[[[` is a plausible thing for a confused model to emit. It must
    be worth `parse_ok=False`, never an exception escaping into the gate path.

    The bomb is built by the probe in `test_adapter_base` rather than
    hardcoded: the depth at which `json` gives up is a property of the C stack
    and differs between builds, so a fixed number would quietly stop being deep
    enough.
    """
    bomb = _recursion_bomb()
    a = AgyAdapter()
    for stdout in (bomb,
                   json.dumps({"status": "SUCCESS",
                               "response": bomb.decode("utf-8")}
                              ).encode("utf-8")):
        res = a.parse(stdout, b"", contract)
        assert res.parse_ok is False and res.payload is None
        assert res.findings == [] and res.summary == ""
        assert a.classify(0, stdout, b"", contract).kind in {
            "ok", "degraded", "unavailable"}


def test_refuter_capture_parses_and_is_not_a_review():
    f = fx("refuter_healthy")
    a = AgyAdapter()
    res = a.parse(f.stdout, f.stderr, REFUTER_CONTRACT)
    assert res.parse_ok is True
    assert [v["verdict"] for v in res.payload["verdicts"]] == [
        "confirmed", "refuted", "refuted"]
    assert res.findings == [] and res.summary == ""
    assert a.parse(f.stdout, f.stderr, REVIEW_CONTRACT).parse_ok is False


# --------------------------------------------------------------------------
# degraded — each signal alone
# --------------------------------------------------------------------------


def test_auto_denied_tool_capture_is_degraded():
    """rc 0, `status: SUCCESS`, an EMPTY response — the silent all-clear shape.

    This is a real capture: a prompt that needed a tool, in a headless run that
    cannot prompt for permission, so the call was auto-denied. The CLI still
    exits 0 and still reports SUCCESS; the only evidence anywhere is the stderr
    line `no output produced`. Without that signal the run is a plain failed
    attempt, and a reviewer that yields nothing forever without saying why is
    the shape Phase 1 was built to refuse.
    """
    f = fx("degraded_stderr")
    a = AgyAdapter()
    assert b"no output produced" in f.stderr
    res = a.parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.degraded is True and res.degraded_reason
    assert a.classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "degraded"


def test_a_terminal_status_other_than_success_is_degraded():
    """The captured `--print-timeout` expiry: rc 1, `status: ERROR`.

    It carries no auth, model or quota wording, so it must land on `degraded`
    rather than `unavailable`: the provider was reachable and the answer was
    cut short, which buys a same-reviewer retry instead of advancing the chain.
    """
    f = fx("degraded_timeout")
    a = AgyAdapter()
    assert b"timeout waiting for response" in f.stdout
    res = a.parse(f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.parse_ok is False and res.degraded is True
    assert res.stop_reason == "ERROR"
    assert a.classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "degraded"


@pytest.mark.parametrize("signal", _DEGRADED_STDERR_SIGNALS)
def test_every_degraded_stderr_signal_is_individually_load_bearing(signal):
    """Each entry alone must trip `degraded`, not just `no output produced`.

    The fixture-based tests above exercise exactly one of the four entries in
    `_DEGRADED_STDERR_SIGNALS` (`no output produced`, via `degraded_stderr`)
    plus the terminal-status path, which is a SEPARATE signal entirely. That
    leaves `stream error`, `was interrupted` and `context deadline exceeded`
    unpinned: deleting any of the three currently leaves all existing tests
    green. This closes that gap by exercising every tuple entry on its own.
    """
    stderr = b"agy: " + signal + b"\n"
    a = AgyAdapter()
    res = a.parse(b"", stderr, REVIEW_CONTRACT)
    assert res.degraded is True and res.degraded_reason
    assert a.classify(0, b"", stderr, REVIEW_CONTRACT).kind == "degraded"


def test_empty_stdout_is_not_degraded():
    """Absence of a signal is never taken as proof of anything."""
    a = AgyAdapter()
    res = a.parse(b"", b"", REVIEW_CONTRACT)
    assert res.parse_ok is False and res.degraded is False
    assert a.classify(0, b"", b"", REVIEW_CONTRACT).kind == "ok"


def test_a_successful_status_is_not_a_degradation_signal():
    envelope = json.dumps({
        "status": "SUCCESS",
        "structured_output": {"summary": "s", "findings": []},
    }).encode("utf-8")
    res = AgyAdapter().parse(envelope, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.degraded is False


def test_finding_text_is_never_a_degraded_signal():
    """A review that DISCUSSES an interrupted stream is not one."""
    payload = {"summary": "the retry path is wrong", "findings": [{
        "file": "net.py", "severity": "low",
        "title": "no output produced is swallowed",
        "detail": "a stream error or a request that was interrupted is lost"}]}
    envelope = json.dumps({"status": "SUCCESS",
                           "response": json.dumps(payload),
                           "structured_output": payload}).encode("utf-8")
    a = AgyAdapter()
    res = a.parse(envelope, b"", REVIEW_CONTRACT)
    assert res.parse_ok is True and res.degraded is False
    assert a.classify(0, envelope, b"", REVIEW_CONTRACT).kind == "ok"


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------


def test_missing_binary():
    res = AgyAdapter().classify(UNAVAILABLE_RC, b"", b"", REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "binary"


def test_auth_capture_is_unavailable_auth():
    f = fx("unavailable_auth")
    res = AgyAdapter().classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "auth"


@pytest.mark.parametrize("signal", _AUTH_SIGNALS)
def test_every_auth_signal_is_individually_load_bearing(signal):
    """Each of the eight `_AUTH_SIGNALS` entries alone must classify `auth`.

    The fixture-based test above exercises exactly one entry
    (`authentication required`, via `unavailable_auth`); deleting any of the
    other seven currently leaves every existing test green.
    """
    err = b"agy: " + signal + b"\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "auth"


def test_model_capture_is_unavailable_model_from_the_envelope_alone():
    """stderr is empty in this capture; the only evidence is `error`.

    `error` is a harness-authored field of the CLI's own envelope, which is
    what keeps review CONTENT out of the classification: `response` is never
    read for a verdict.
    """
    f = fx("unavailable_model")
    assert f.stderr == b""
    res = AgyAdapter().classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "model"


def test_effort_conflict_capture_is_model(tmp_path):
    """An effort the model id disagrees with is a MODEL-selection failure.

    Live: `--model gemini-3.6-flash-low --effort medium` is rc 1 and
    `invalid model selection (...): --model gemini-3.6-flash-low conflicts
    with --effort=medium`. Every model/effort rejection this CLI emits opens
    with that same `invalid model selection` phrase, which is why one signal
    covers the unknown id, the missing effort and the conflicting effort alike.
    Attempt-local, so `model`, never the provider-wide `quota`.
    """
    envelope = json.dumps({
        "status": "ERROR", "response": "",
        "error": 'invalid model selection (--model "gemini-3.6-flash-low" '
                 '--effort "medium"): --model gemini-3.6-flash-low conflicts '
                 'with --effort=medium',
    }).encode("utf-8")
    res = AgyAdapter().classify(1, envelope, b"", REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "model"


@pytest.mark.parametrize("signal", _MODEL_SIGNALS)
def test_every_model_signal_is_individually_load_bearing(signal):
    """Each of the six `_MODEL_SIGNALS` entries alone must classify `model`.

    The fixture/synthetic tests above exercise exactly one entry
    (`invalid model selection`); deleting any of the other five currently
    leaves every existing test green.
    """
    err = b"agy: " + signal + b"\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "model"


def test_quota_wording_is_unavailable_quota():
    err = b"rpc error: code = ResourceExhausted desc = quota exceeded\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "quota"


@pytest.mark.parametrize("signal", _QUOTA_SIGNALS)
def test_every_quota_signal_is_individually_load_bearing(signal):
    """Each `_QUOTA_SIGNALS` entry alone must classify `quota`.

    `quota` is the one provider-wide-cacheable category, so a table entry
    that silently stops matching is the most expensive kind of dead signal
    this module has: it would keep re-trying a provider that has told every
    caller it is out of budget.
    """
    err = b"agy: " + signal + b"\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "quota"


def test_auth_beats_quota_when_both_appear():
    """`quota` is the only provider-wide-cacheable category, so anything that
    also looks attempt-local is reported as that instead."""
    err = b"Authentication required while checking your rate limit quota\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "auth"


def test_a_rejected_invocation_is_unavailable_other():
    """The CLI refusing our argv is an attempt-local failure, not silence.

    Live: a malformed `--json-schema` is rc 1 with EMPTY stdout and
    `Error: invalid --json-schema: schema is not valid JSON` on stderr. Left
    unclassified that is an empty run with (as far as the tables are
    concerned) clean stderr — indistinguishable from a provider that simply
    said nothing. `other` because nothing about it is a provider outage, and
    `other` caches nothing.
    """
    err = b"Error: invalid --json-schema: schema is not valid JSON: " \
          b"unexpected end of JSON input\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "other"


@pytest.mark.parametrize("signal", _INVOCATION_SIGNALS)
def test_every_invocation_signal_is_individually_load_bearing(signal):
    """Each `_INVOCATION_SIGNALS` entry alone must classify `other`.

    The other test in this section exercises `invalid --json-schema` only;
    deleting `empty prompt` or the undefined-flag signal currently leaves
    every existing test green.
    """
    err = b"agy: " + signal + b"\n"
    res = AgyAdapter().classify(1, b"", err, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "other"


def test_undefined_flag_signal_matches_the_real_binarys_plural_wording():
    """The installed agy 1.1.8 wrapper's own words, verified live, not assumed.

    `agy --nonexistent-flag-xyz` (a cheap, read-only, no-inference call) gave:

        flags provided but not defined: -nonexistent-flag-xyz
        Usage of agy:
          ...
        rc=2

    PLURAL, every time, even for exactly one bad flag. Go stdlib's `flag`
    package itself spells this singular ("flag provided but not defined:"),
    and that string IS present somewhere in the binary too, but it is
    unreachable through agy's own argument parser -- only the wrapper's
    plural form ever reaches stderr on a real run. A table entry spelled
    singular would never match this real output and a rejected invocation
    would fall through to plain `ok` with no explanation.
    """
    stderr = (
        b"flags provided but not defined: -nonexistent-flag-xyz\n"
        b"Usage of agy:\n"
        b"  --add-dir  Add a directory to the workspace (repeatable)\n"
    )
    res = AgyAdapter().classify(2, b"", stderr, REVIEW_CONTRACT)
    assert res.kind == "unavailable" and res.category == "other"
    # The premise this test protects: the singular Go-stdlib spelling is a
    # DIFFERENT string that must not be relied on -- it is not what a real
    # rejected invocation contains.
    assert b"flag provided but not defined" not in stderr


def test_usable_output_wins_over_auth_noise():
    f = fx("healthy_noisy_stderr")
    a = AgyAdapter()
    assert b"Authentication required" in f.stderr
    assert a.classify(0, f.stdout, f.stderr, REVIEW_CONTRACT).kind == "ok"
    # …and the same stderr with nothing on stdout is genuinely unavailable, so
    # the case above is not vacuous.
    assert a.classify(1, b"", f.stderr, REVIEW_CONTRACT).kind == "unavailable"


def test_usable_output_does_not_win_over_degradation():
    """Availability and degradation are different questions.

    "The provider served" is proved by the payload; "the answer is complete"
    is not. Conflating them re-introduces the Phase 1 silent false all-clear.
    """
    f = fx("degraded_stderr")
    a = AgyAdapter()
    assert a.parse(f.stdout, f.stderr, REVIEW_CONTRACT).parse_ok is True
    res = a.classify(f.rc, f.stdout, f.stderr, REVIEW_CONTRACT)
    assert res.kind == "degraded" and res.category == ""


@pytest.mark.parametrize("text, why", [
    ('{"summary":"the code ignores quota exceeded errors","findings":[',
     "truncated mid-payload"),
    ('{"summary":"Authentication required is swallowed","findings":"nope"}',
     "complete but schema-invalid"),
    ("I could not finish: the diff quotes an invalid model selection error "
     "and a rate limit, and I ran out of context.",
     "prose, no payload at all"),
])
def test_a_failed_run_is_not_unavailable_because_of_what_the_model_said(
        text, why):
    """The half of rule 6 that `classify`'s short-circuit cannot answer for.

    `classify` returns before the diagnostics read whenever the payload
    validates (usable output), so a signal-word test built on a VALID review
    proves nothing about what the classifier reads.

    These three are runs that produced no usable payload — a truncated answer,
    a schema-invalid one, and an apology — each carrying auth, model or quota
    wording in the model's own words and nothing on stderr. The diagnostics
    read IS reached, and it must still find only the harness's words, of which
    there are none. A `quota` verdict here would be cached provider-wide and
    would take agy out of every later fallback chain in the run, on the
    strength of a sentence the model wrote about someone else's code.
    """
    envelope = json.dumps({"status": "SUCCESS",
                           "response": text}).encode("utf-8")
    a = AgyAdapter()
    # The premise: this run really did fail to produce a payload, so nothing
    # short-circuits ahead of the diagnostics read.
    assert a.parse(envelope, b"", REVIEW_CONTRACT).parse_ok is False, why
    for rc in (0, 1):
        res = a.classify(rc, envelope, b"", REVIEW_CONTRACT)
        assert res.kind == "ok", (
            f"{why} (rc {rc}): classified {res.kind}/{res.category} "
            f"({res.detail!r}) on wording that appears only inside the "
            f"model's own message")
        assert res.category == ""


# --------------------------------------------------------------------------
# the registration gate
# --------------------------------------------------------------------------


class TestAgyConformance(AdapterConformance):
    """The provider-neutral half; agy's own wire details stay above.

    Everything asserted here lives in `tests/adapter_conformance.py`; this
    class supplies only the four things the mixin cannot know.
    """

    provider_id = "google"
    fixture_dir = FIXTURES

    def adapter(self) -> AgyAdapter:
        return AgyAdapter()

    def effort_reject_case(self) -> tuple[Reviewer, str]:
        """`max` is the one canonical effort this CLI has no level for.

        `agy --effort max` is refused live with `invalid --effort "max"
        (valid: low, medium, high)`, so there is nothing honest to map it to
        and `build_cmd` must raise. `none` is excused separately by the mixin
        as the opt-out; `low`/`medium`/`high` map through, which rule 5 then
        checks is the WHOLE remainder of `config.EFFORTS`.
        """
        r = Reviewer(name="f", provider="google", model=MODEL, role="finder",
                     effort="max")
        return r, "no CLI value for effort"
