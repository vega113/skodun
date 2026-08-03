"""The MCP protocol suite: transcripts, concurrency, and the `skodun mcp` seam.

Correctness here is mechanical, not conventional. The server speaks a protocol
nobody in this repository can eyeball, over a stream whose one inviolable rule is
that STDOUT CARRIES NOTHING BUT NEWLINE-DELIMITED JSON-RPC -- so the suite drives
real bytes through the loop and asserts on the bytes that come back:

  * `tests/fixtures/mcp/*.jsonl` are sessions (see that directory's README for
    the format). Every response line must parse as JSON-RPC, the predicates must
    match IN ORDER, and there must be exactly as many response lines as
    predicates: one stray byte on stdout fails the transcript even when every
    predicate passed. Fixtures that need no tool registry are additionally
    replayed against a real `skodun mcp` subprocess, which is the only way to
    prove the shipped entry point adds no line of its own.
  * Concurrency is pinned with a SLOW FAKE handler registered through the same
    seam Task 14 will register the real services through. No real review, no
    provider, no store is involved in the busy path.
  * The 8 MiB line cap is generated at runtime, both sides of the boundary; an
    8-mebibyte fixture would be committed weight for a loop.
"""

import base64
import dataclasses
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import skodun
from skodun import mcpserver
from skodun.mcpserver import (HandlerCall, HandlerResult, HandlerSpec, McpServer,
                              PromptSpec)

_SRC = str(Path(skodun.__file__).resolve().parents[1])
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mcp"


# --------------------------------------------------------------------------
# The transcript format (normative here; described in the fixtures' README).
# --------------------------------------------------------------------------

class _Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.sends: list[bytes] = []
        self.expects: list[dict] = []
        self.flags: set[str] = set()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.split("\n"), start=1):
            where = f"{path.name}:{lineno}"
            if line == "" or line.startswith("#"):
                continue
            if line.startswith("!"):
                self.flags.add(line[1:].strip())
            elif line == ">":
                self.sends.append(b"\n")
            elif line.startswith("> "):
                self.sends.append(line[2:].encode("utf-8") + b"\n")
            elif line.startswith(">b64 "):
                self.sends.append(base64.b64decode(line[5:].strip()) + b"\n")
            elif line.startswith("< "):
                try:
                    self.expects.append(json.loads(line[2:]))
                except ValueError as e:   # a broken fixture, not a server bug
                    raise AssertionError(f"{where}: unparseable predicate: {e}")
            else:
                raise AssertionError(f"{where}: not a transcript directive: {line!r}")

    @property
    def stdin_bytes(self) -> bytes:
        return b"".join(self.sends)


def _transcripts() -> list[Path]:
    files = sorted(p for p in _FIXTURES.glob("*.jsonl"))
    assert files, f"no transcripts under {_FIXTURES}"
    return files


_TYPES = {"object": dict, "array": list, "string": str, "number": (int, float),
          "boolean": bool, "null": type(None)}


def _check(pred, actual, where: str, problems: list[str]) -> None:
    """Match `pred` against `actual` as a recursive SUBSET, collecting failures.

    Subset rather than equality on purpose: a transcript pins the fields the
    protocol promises, and a future additive field must not fail every fixture
    at once. The `$keys` escape is how a shape that must be EXACT says so.
    """
    if isinstance(pred, dict):
        if not isinstance(actual, dict) and any(
                k not in ("$type", "$contains", "$version") for k in pred):
            problems.append(f"{where}: expected an object, got {actual!r}")
            return
        for key, want in pred.items():
            if key == "$absent":
                continue                    # handled by the parent's walk
            if key == "$type":
                if want not in _TYPES:
                    problems.append(f"{where}: unknown $type {want!r}")
                elif want == "number" and isinstance(actual, bool):
                    problems.append(f"{where}: expected a number, got {actual!r}")
                elif not isinstance(actual, _TYPES[want]):
                    problems.append(f"{where}: expected {want}, got {actual!r}")
            elif key == "$contains":
                if not isinstance(actual, str) or want not in actual:
                    problems.append(f"{where}: expected a string containing "
                                    f"{want!r}, got {actual!r}")
            elif key == "$version":
                if want != "ours":
                    problems.append(f"{where}: unknown $version {want!r}")
                elif actual != mcpserver.MCP_PROTOCOL_VERSION:
                    problems.append(f"{where}: expected our pinned protocol "
                                    f"version {mcpserver.MCP_PROTOCOL_VERSION!r}, "
                                    f"got {actual!r}")
            elif key == "$keys":
                if sorted(actual) != sorted(want):
                    problems.append(f"{where}: expected exactly the keys "
                                    f"{sorted(want)}, got {sorted(actual)}")
            elif key.startswith("$"):
                problems.append(f"{where}: unknown predicate escape {key!r}")
            elif isinstance(want, dict) and want.get("$absent"):
                if key in actual:
                    problems.append(f"{where}.{key}: expected to be absent, "
                                    f"got {actual[key]!r}")
            elif key not in actual:
                problems.append(f"{where}.{key}: missing from {sorted(actual)}")
            else:
                _check(want, actual[key], f"{where}.{key}", problems)
    elif isinstance(pred, list):
        if not isinstance(actual, list):
            problems.append(f"{where}: expected an array, got {actual!r}")
        elif len(pred) != len(actual):
            problems.append(f"{where}: expected {len(pred)} element(s), got "
                            f"{len(actual)}")
        else:
            for i, (want, got) in enumerate(zip(pred, actual)):
                _check(want, got, f"{where}[{i}]", problems)
    elif pred != actual or (isinstance(pred, bool) != isinstance(actual, bool)):
        problems.append(f"{where}: expected {pred!r}, got {actual!r}")


def _responses(raw: bytes, where: str) -> list[dict]:
    """Every line of `raw` as a JSON-RPC response object, or an assertion.

    THE zero-residue check. A line that does not parse, a line that is not an
    object, a message that is neither a result nor an error, or a trailing
    fragment with no newline are all the same failure: something that is not
    JSON-RPC reached stdout.
    """
    if raw == b"":
        return []
    assert raw.endswith(b"\n"), f"{where}: stdout ends mid-line: {raw[-80:]!r}"
    out = []
    for i, line in enumerate(raw.decode("utf-8").split("\n")[:-1]):
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise AssertionError(f"{where}: stdout line {i + 1} is not JSON "
                                 f"({e}): {line[:200]!r}")
        assert isinstance(obj, dict), f"{where}: line {i + 1} is not an object"
        assert obj.get("jsonrpc") == "2.0", f"{where}: line {i + 1}: {obj!r}"
        assert ("result" in obj) != ("error" in obj), \
            f"{where}: line {i + 1} is neither a result nor an error: {obj!r}"
        out.append(obj)
    return out


def _assert_transcript(t: _Transcript, raw_stdout: bytes, where: str) -> None:
    got = _responses(raw_stdout, where)
    problems: list[str] = []
    for i, (pred, actual) in enumerate(zip(t.expects, got)):
        _check(pred, actual, f"{where}: response[{i}]", problems)
    assert not problems, "\n".join(problems)
    assert len(got) == len(t.expects), (
        f"{where}: expected {len(t.expects)} response(s), got {len(got)}: "
        f"{[g.get('id') for g in got]}")


# --------------------------------------------------------------------------
# The fakes behind the registry seam. Task 14 registers real services here.
# --------------------------------------------------------------------------

class _Fakes:
    """Tool and prompt registries made of fakes, plus a log of what ran.

    The log is the side-effect evidence: "ignored WITHOUT execution" is an
    assertion about this list being empty, not about stdout being quiet.
    """

    def __init__(self, *, hold_review: bool = False):
        self.log: list[tuple[str, dict]] = []
        self.hold_review = hold_review
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_seen: list[bool] = []
        self.stores: list[object] = []
        self.review_raises = False

    def registry(self) -> tuple[HandlerSpec, ...]:
        return (
            HandlerSpec(name="echo", long_running=False,
                        input_schema={"type": "object",
                                      "properties": {"text": {"type": "string"}}},
                        handler=self._echo,
                        description="echo the text argument back"),
            HandlerSpec(name="refuse", long_running=False,
                        input_schema={"type": "object", "properties": {}},
                        handler=self._refuse,
                        description="always refuses, like a validator would"),
            HandlerSpec(name="boom", long_running=False,
                        input_schema={"type": "object", "properties": {}},
                        handler=self._boom,
                        description="always raises"),
            HandlerSpec(name="review", long_running=True,
                        input_schema={"type": "object", "properties": {}},
                        handler=self._review,
                        description="the long-running one"),
        )

    def prompts(self) -> tuple[PromptSpec, ...]:
        return (PromptSpec(name="demo-prompt", description="a demo prompt",
                           text="run the demo review now"),)

    def _echo(self, call: HandlerCall) -> HandlerResult:
        self.log.append(("echo", dict(call.params)))
        acks = call.params.get("acks") or []
        return HandlerResult(status=0, text=str(call.params.get("text", "")),
                             pending_acks=list(acks))

    def _refuse(self, call: HandlerCall) -> HandlerResult:
        self.log.append(("refuse", dict(call.params)))
        return HandlerResult(status=2, text="skodun refuse: refused", pending_acks=[])

    def _boom(self, call: HandlerCall) -> HandlerResult:
        self.log.append(("boom", dict(call.params)))
        raise RuntimeError("the boom tool always raises")

    def _review(self, call: HandlerCall) -> HandlerResult:
        self.log.append(("review", dict(call.params)))
        self.started.set()
        if self.hold_review:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.release.is_set() or call.cancel.is_set():
                    break
                time.sleep(0.005)
        self.cancel_seen.append(call.cancel.is_set())
        if self.review_raises:
            raise RuntimeError("the review fake was asked to fail")
        return HandlerResult(status=0, text="reviewed", pending_acks=[])


class _Recorder:
    """A binary stdout that records every write and every flush, in order.

    One write plus one flush per response is a protocol requirement, not a
    style: two writes can interleave with another thread's response and produce
    a line that is half of each.
    """

    def __init__(self, fail=None):
        self.chunks: list[bytes] = []
        self.events: list[str] = []
        self.fail = fail
        self._lock = threading.Lock()

    def write(self, data) -> int:
        with self._lock:
            self.events.append("write")
            if self.fail is not None:
                raise self.fail()
            self.chunks.append(bytes(data))
            return len(data)

    def flush(self) -> None:
        with self._lock:
            self.events.append("flush")

    @property
    def data(self) -> bytes:
        with self._lock:
            return b"".join(self.chunks)

    def response_count(self) -> int:
        return self.data.count(b"\n")


def _no_store():
    raise AssertionError("this test's tools must not open a store")


def _server(*, registry=(), prompts=(), stdin=b"", stdout=None, stderr=None,
            store_factory=_no_store, acknowledge=None, on_stdout_lost=None):
    return McpServer(
        registry=registry, prompts=prompts,
        stdin=io.BytesIO(stdin) if isinstance(stdin, bytes) else stdin,
        stdout=_Recorder() if stdout is None else stdout,
        stderr=io.StringIO() if stderr is None else stderr,
        store_factory=store_factory, acknowledge=acknowledge,
        on_stdout_lost=on_stdout_lost)


def _drive(payload: bytes, *, stdout=None, stderr=None, **kw):
    """Run one session in-process and return `(code, stdout, stderr)`."""
    out = _Recorder() if stdout is None else stdout
    err = io.StringIO() if stderr is None else stderr
    server = _server(stdin=payload, stdout=out, stderr=err, **kw)
    code = server.serve()
    return code, out, err


def _rpc(method: str, id_=None, **params) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params:
        msg["params"] = params
    return json.dumps(msg).encode("utf-8") + b"\n"


#: The handshake every non-transcript test starts from. Its id is deliberately
#: far away from the ids those tests use, so a response collected by id can
#: never be the handshake's by accident.
_HANDSHAKE = _rpc("initialize", 100, protocolVersion="2025-11-25") + \
    _rpc("notifications/initialized")


# --------------------------------------------------------------------------
# The transcripts.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", _transcripts(), ids=lambda p: p.stem)
def test_transcript_in_process(path):
    t = _Transcript(path)
    fakes = _Fakes()
    code, out, err = _drive(
        t.stdin_bytes,
        registry=fakes.registry() if "requires-fakes" in t.flags else (),
        prompts=fakes.prompts() if "requires-fakes" in t.flags else ())
    assert code == 0, f"{t.name}: exit {code}; stderr={err.getvalue()!r}"
    _assert_transcript(t, out.data, t.name)
    if "assert no-handler-calls" in t.flags:
        assert fakes.log == [], (
            f"{t.name}: a request-only method with no id was EXECUTED: "
            f"{fakes.log!r}")


@pytest.mark.parametrize(
    "path", [p for p in _transcripts() if "requires-fakes" not in _Transcript(p).flags],
    ids=lambda p: p.stem)
def test_transcript_against_a_real_skodun_mcp_process(path, tmp_path):
    """The same session through the shipped entry point.

    In-process runs cannot see a startup banner, a warning, or a stray `print`
    somewhere in the import graph -- all of which would land on the real
    process's stdout and corrupt the stream. This row is the one that would
    catch it.
    """
    t = _Transcript(path)
    p = subprocess.run([sys.executable, "-m", "skodun", "mcp"],
                       input=t.stdin_bytes, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=_env(tmp_path), timeout=120)
    assert p.returncode == 0, f"{t.name}: exit {p.returncode}: {p.stderr!r}"
    assert b"Traceback" not in p.stderr, p.stderr
    _assert_transcript(t, p.stdout, f"{t.name} (subprocess)")


def test_the_fixture_ledger_accounts_for_every_transcript():
    """The fixture-provenance rule, enforced rather than promised: every file
    has a README entry, and exactly the two handshake captures are CAPTURED."""
    readme = (_FIXTURES / "README").read_text(encoding="utf-8")
    entries = dict(re.findall(r"^(\S+\.jsonl)\n((?:    .*\n)+)", readme,
                              re.MULTILINE))
    names = {p.name for p in _transcripts()}
    assert set(entries) == names, (
        f"ledger and directory disagree: {set(entries) ^ names}")
    captured = {n for n, body in entries.items() if "CAPTURED" in body}
    synthesized = {n for n, body in entries.items() if "SYNTHESIZED" in body}
    assert captured == {"handshake-claude-code.jsonl", "handshake-codex-cli.jsonl"}
    assert synthesized == names - captured


def test_the_captured_handshakes_are_byte_identical_to_what_the_clients_sent():
    """The captures are evidence, and evidence gets edited by accident. These
    two lines are the ones a real client wrote; if one is ever reflowed or
    "tidied", it stops being a capture."""
    claude = _Transcript(_FIXTURES / "handshake-claude-code.jsonl")
    codex = _Transcript(_FIXTURES / "handshake-codex-cli.jsonl")
    first_claude = json.loads(claude.sends[0])
    first_codex = json.loads(codex.sends[0])
    assert first_claude["params"]["clientInfo"]["name"] == "claude-code"
    assert first_claude["params"]["protocolVersion"] == "2025-11-25"
    assert first_codex["params"]["clientInfo"]["name"] == "codex-mcp-client"
    assert first_codex["params"]["protocolVersion"] == "2025-06-18"
    # Both revisions must be RECOGNIZED, or negotiation would answer a real
    # client with a version it did not ask for.
    for v in ("2025-11-25", "2025-06-18"):
        assert v in mcpserver.SUPPORTED_PROTOCOL_VERSIONS


# --------------------------------------------------------------------------
# The pinned constants and the registry contract Task 14 consumes.
# --------------------------------------------------------------------------

def test_the_protocol_version_constant_is_one_we_support():
    assert mcpserver.MCP_PROTOCOL_VERSION in mcpserver.SUPPORTED_PROTOCOL_VERSIONS
    assert mcpserver.SERVER_NAME == "skodun"


def test_the_line_cap_is_eight_mebibytes():
    assert mcpserver.MAX_LINE_BYTES == 8 * 1024 * 1024


def test_the_registry_types_are_the_pinned_contract():
    """Task 14 registers real services behind these exact names. A renamed
    field is a broken hand-off, and a hand-off is not a thing tests usually
    get to check -- so it is checked here."""
    spec = [f.name for f in dataclasses.fields(HandlerSpec)]
    assert spec[:4] == ["name", "long_running", "input_schema", "handler"]
    assert [f.name for f in dataclasses.fields(HandlerCall)] == \
        ["params", "store_factory", "cancel"]
    assert [f.name for f in dataclasses.fields(HandlerResult)] == \
        ["status", "text", "pending_acks"]
    # `pending_acks` defaults to empty: a tool with nothing to acknowledge
    # should not have to say so.
    assert HandlerResult(status=0, text="x").pending_acks == []


def test_the_serverinfo_names_skodun_and_its_own_version():
    from skodun.store import SCHEMA_VERSION

    code, out, _ = _drive(_rpc("initialize", 1, protocolVersion="2025-11-25"))
    result = _responses(out.data, "initialize")[0]["result"]
    assert result["serverInfo"] == {
        "name": "skodun",
        "version": skodun.__version__,
        "schemaVersion": SCHEMA_VERSION,
    }
    assert result["capabilities"] == {"tools": {}, "prompts": {}}


def test_two_long_running_tools_are_refused_at_construction():
    """Capacity 1 is a property of the design, not of the registry's contents:
    two long-running tools would need two slots and a queueing policy nobody
    has decided on."""
    fakes = _Fakes()
    extra = HandlerSpec(name="review2", long_running=True, input_schema={},
                        handler=fakes._review)
    with pytest.raises(ValueError, match="long-running"):
        _server(registry=(*fakes.registry(), extra))


def test_a_duplicate_tool_name_is_refused_at_construction():
    fakes = _Fakes()
    dupe = HandlerSpec(name="echo", long_running=False, input_schema={},
                       handler=fakes._echo)
    with pytest.raises(ValueError, match="echo"):
        _server(registry=(*fakes.registry(), dupe))


def test_the_default_registry_is_the_curated_review_loop():
    """Task 13 shipped this transport with an EMPTY registry, deliberately: an
    accidental tool would be an agent-facing surface nobody reviewed, on a
    fail-closed gate. Task 14 filled it, so what this pins now is the shape of
    the hand-off rather than its emptiness -- every entry is a well-formed
    `HandlerSpec` with a callable behind it, and exactly one of them is the
    long-running review.

    WHICH tools, in WHICH order, and with WHICH schemas is `test_mcptools.py`'s
    snapshot; this module owns the transport and stops at "the registry the
    transport is handed is usable".
    """
    registry = mcpserver.default_registry()
    prompts = mcpserver.default_prompts()
    assert registry and prompts, "the shipped server serves no tools at all"
    for spec in registry:
        assert isinstance(spec, HandlerSpec)
        assert spec.name and callable(spec.handler)
        assert isinstance(spec.input_schema, dict) and spec.input_schema
        assert spec.description
    assert [s.name for s in registry if s.long_running] == ["review"]
    for prompt in prompts:
        assert isinstance(prompt, PromptSpec)
        assert prompt.name and prompt.description and prompt.text
    # Constructible: duplicate names and a second long-running tool are refused
    # at construction, so this is also the assertion that the shipped registry
    # satisfies those two rules.
    _server(registry=registry, prompts=prompts)


def test_a_handler_that_returns_nonsense_is_a_tool_error_not_a_crash():
    def liar(call):
        return {"status": 0, "text": "a dict is not a HandlerResult"}

    registry = (HandlerSpec(name="liar", long_running=False, input_schema={},
                            handler=liar),)
    code, out, err = _drive(
        _HANDSHAKE + _rpc("tools/call", 2, name="liar", arguments={}),
        registry=registry)
    assert code == 0
    result = _responses(out.data, "liar")[-1]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["status"] == 2
    assert "liar" in result["content"][0]["text"]


def test_schema_behind_tool_failure_tells_agents_to_restart_mcp_not_use_cli():
    """When store schema is newer than this build, agents must restart MCP —
    not shell out to CLI. The tool text is the only signal most agents see."""
    from skodun.store import schema_too_new_message

    def boom(call):
        raise ValueError(schema_too_new_message(99))

    registry = (HandlerSpec(name="gate", long_running=False, input_schema={},
                            handler=boom),)
    code, out, err = _drive(
        _HANDSHAKE + _rpc("tools/call", 2, name="gate", arguments={}),
        registry=registry)
    assert code == 0, err.getvalue()
    result = _responses(out.data, "gate")[-1]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "schema-behind" in text
    assert "Restart this MCP server" in text
    assert "do not fall back to the CLI" in text
    assert "gate" in text


def test_handler_failure_text_formats_schema_behind_and_generic():
    from skodun.mcpserver import _handler_failure_text
    from skodun.store import schema_too_new_message

    schema = _handler_failure_text("review", ValueError(schema_too_new_message(9)))
    assert "schema-behind" in schema
    assert "do not fall back to the CLI" in schema
    generic = _handler_failure_text("log", RuntimeError("disk full"))
    assert "the tool failed" in generic
    assert "disk full" in generic


# --------------------------------------------------------------------------
# The 8 MiB line cap, generated -- never committed.
# --------------------------------------------------------------------------

def _padded_line(total: int, id_: int) -> bytes:
    """A syntactically VALID `ping` request padded to exactly `total` bytes.

    Valid on purpose: a line refused for its length must not be refused for its
    content, and the id it carries is what proves it was never executed.
    """
    skeleton = json.dumps({"jsonrpc": "2.0", "id": id_, "method": "ping",
                           "pad": ""}).encode("utf-8")
    pad = total - len(skeleton)
    assert pad >= 0
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": "ping",
                       "pad": "x" * pad}).encode("utf-8")


def test_an_oversized_line_is_drained_and_answered_once_and_the_loop_continues():
    """8 MiB + 1. The oversized line is drained (not re-parsed as fragments,
    which would answer several -32700s for one line, or worse, execute a
    fragment), answered -32700 with id null, and the next request is served."""
    over = _padded_line(mcpserver.MAX_LINE_BYTES + 1, 99)
    payload = _rpc("ping", 1) + over + b"\n" + _rpc("ping", 2)
    code, out, err = _drive(payload)
    assert code == 0
    got = _responses(out.data, "oversized")
    assert [r.get("id") for r in got] == [1, None, 2], got
    assert got[1]["error"]["code"] == -32700
    assert "parse error" in got[1]["error"]["message"]
    assert 99 not in [r.get("id") for r in got], "the oversized line was executed"
    assert "8388608" in err.getvalue() or "cap" in err.getvalue().lower()


def test_the_tail_of_an_oversized_line_is_never_read_back_as_a_message():
    """Why the drain exists, and the only shape that shows it.

    The cap is enforced by reading `cap + 1` bytes, so whatever followed stays
    in the buffer. If it is not discarded, the loop reads it as the NEXT line --
    and a tail can be crafted to be a perfectly valid request. Then a client
    that sent one oversized line gets an answer to a request it never made, and
    a `tools/call` smuggled in a padded line would EXECUTE. The tail below is a
    complete `ping`; it must never be answered.
    """
    head = _padded_line(mcpserver.MAX_LINE_BYTES + 1, 99)
    smuggled = json.dumps({"jsonrpc": "2.0", "id": 66,
                           "method": "ping"}).encode("utf-8")
    code, out, _ = _drive(_rpc("ping", 1) + head + smuggled + b"\n"
                          + _rpc("ping", 2))
    assert code == 0
    ids = [r.get("id") for r in _responses(out.data, "smuggled")]
    assert 66 not in ids, "the tail of an oversized line was executed"
    assert ids == [1, None, 2], ids


def test_a_line_exactly_at_the_cap_is_still_served():
    """The boundary the other way round. A cap that is off by one silently
    refuses the largest legitimate `review` argument somebody will ever send."""
    at = _padded_line(mcpserver.MAX_LINE_BYTES, 5)
    code, out, _ = _drive(at + b"\n" + _rpc("ping", 6))
    assert code == 0
    assert [r.get("id") for r in _responses(out.data, "at-cap")] == [5, 6]


def test_an_oversized_line_without_a_trailing_newline_ends_the_session_cleanly():
    """The pathological shape: 8 MiB + 1 bytes and then EOF, no newline. The
    drain must notice EOF instead of blocking forever."""
    code, out, _ = _drive(_padded_line(mcpserver.MAX_LINE_BYTES + 1, 7))
    assert code == 0
    got = _responses(out.data, "oversized-eof")
    assert [r.get("id") for r in got] == [None]
    assert got[0]["error"]["code"] == -32700


# --------------------------------------------------------------------------
# Concurrency: one long-running tool, capacity 1, on its own thread.
# --------------------------------------------------------------------------

class _Pipe:
    """A real OS pipe for stdin, so `readline` blocks exactly as in production.

    A `BytesIO` cannot express "the client has not said anything yet", which is
    the whole state the busy path lives in.
    """

    def __init__(self):
        self._r, self._w = os.pipe()
        self.reader = io.open(self._r, "rb")

    def send(self, data: bytes) -> None:
        os.write(self._w, data)

    def close(self) -> None:
        try:
            os.close(self._w)
        except OSError:
            pass

    def cleanup(self) -> None:
        self.close()
        try:
            self.reader.close()
        except OSError:
            pass


def _serve_in_thread(server):
    box: dict = {}

    def run():
        box["code"] = server.serve()

    t = threading.Thread(target=run, name="serve", daemon=True)
    t.start()
    return t, box


def _wait_until(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _by_id(out: _Recorder) -> dict:
    return {r.get("id"): r for r in _responses(out.data, "collected")}


def test_a_second_review_is_refused_while_one_is_in_flight_and_the_loop_stays_live():
    """Capacity 1, and the read loop keeps answering while the slot is busy.

    Both halves are load-bearing and they kill different mistakes. If `review`
    ran INLINE, the loop would be blocked inside the handler: ids 2 and 3 would
    stay unanswered until the review finished, so the busy error could not
    exist at all. If the slot were unbounded, id 2 would start a second review
    behind the first -- two reviews of the same repository, racing for the
    foreground lock.
    """
    fakes = _Fakes(hold_review=True)
    pipe, out = _Pipe(), _Recorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(fakes.started.is_set, what="the review to start")
        pipe.send(_rpc("tools/call", 2, name="review", arguments={}))
        pipe.send(_rpc("tools/call", 3, name="echo", arguments={"text": "alive"}))
        _wait_until(lambda: {2, 3} <= set(_by_id(out)),
                    what="the busy refusal and the inline call")
        got = _by_id(out)
        busy = got[2]["result"]
        assert busy["isError"] is True
        assert busy["content"][0]["text"] == mcpserver.BUSY_TEXT
        assert busy["structuredContent"]["status"] != 0
        assert got[3]["result"]["content"][0]["text"] == "alive"
        assert 1 not in got, "the review answered before it was released"
        # Exactly ONE review ever started.
        assert [name for name, _ in fakes.log].count("review") == 1
        fakes.release.set()
        _wait_until(lambda: 1 in _by_id(out), what="the review's own response")
        assert _by_id(out)[1]["result"]["content"][0]["text"] == "reviewed"
    finally:
        pipe.close()
        t.join(timeout=10)
        pipe.cleanup()
    assert not t.is_alive()
    assert box["code"] == 0


def test_the_review_slot_is_reusable_and_a_raising_review_frees_it():
    """Capacity 1 is not one-shot, and an exception inside the handler must not
    leave the slot occupied for the life of the server -- an agent would be told
    "review already in flight" forever, with nothing in flight.

    The second call here is sent the instant the first result appears, which is
    also the tightest form of the freeing rule: the slot must be free by the time
    the client can see the answer, not merely by the time the thread that wrote
    it has returned. A slot released after the write leaves a window in which the
    honest answer to "review again?" is refused.
    """
    fakes = _Fakes()
    fakes.review_raises = True
    pipe, out = _Pipe(), _Recorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(lambda: 1 in _by_id(out), what="the failed review's answer")
        assert _by_id(out)[1]["result"]["isError"] is True
        fakes.started.clear()
        fakes.review_raises = False
        pipe.send(_rpc("tools/call", 2, name="review", arguments={}))
        _wait_until(lambda: 2 in _by_id(out), what="the second review's answer")
        assert _by_id(out)[2]["result"]["isError"] is False
    finally:
        pipe.close()
        t.join(timeout=10)
        pipe.cleanup()
    assert box["code"] == 0


class _BlockingRecorder(_Recorder):
    """A stdout that HANGS inside the first tool-result write, on command.

    The only way to observe the difference between "the slot is free when the
    handler finishes" and "the slot is free when the thread finishes": those two
    moments are microseconds apart in real time, and a test that races them is a
    test that passes for the wrong reason.
    """

    def __init__(self):
        super().__init__()
        self.blocked = threading.Event()
        self.release = threading.Event()
        self._blocked_once = False

    def write(self, data):
        if b'"isError"' in bytes(data) and not self._blocked_once:
            self._blocked_once = True
            self.blocked.set()
            self.release.wait(10)
        return super().write(data)


def test_the_slot_is_free_as_soon_as_the_handler_is_done_not_when_the_write_is():
    """A review whose result is stuck in the write must not block the next one.

    The window is real: an agent that receives "review finished" can ask for the
    next review in the same millisecond, and "review already in flight" would be
    a lie about a slot occupied by nothing but our own `write` call. Held open
    deliberately here, because otherwise the two orderings are indistinguishable.
    """
    fakes = _Fakes()
    pipe, out = _Pipe(), _BlockingRecorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(out.blocked.is_set, what="the first result's write to block")
        pipe.send(_rpc("tools/call", 2, name="review", arguments={}))
        _wait_until(lambda: [n for n, _ in fakes.log].count("review") == 2,
                    what="the second review to be ACCEPTED, not refused")
    finally:
        out.release.set()
        pipe.close()
        t.join(timeout=15)
        pipe.cleanup()
    assert box["code"] == 0
    got = _by_id(out)
    for id_ in (1, 2):
        assert got[id_]["result"]["isError"] is False, got[id_]


def test_eof_drains_the_review_in_flight_without_cancelling(monkeypatch):
    """Default disconnect policy is drain: session end must not abort a review.

    Operator MCP restarts and host reloads close stdin; cancelling would throw
    away the in-flight work. The server joins the worker without setting the
    cancel token, and the tool response is still written before serve returns.
    """
    monkeypatch.delenv("SKODUN_MCP_DISCONNECT", raising=False)
    fakes = _Fakes(hold_review=True)
    pipe, out = _Pipe(), _Recorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(fakes.started.is_set, what="the review to start")
        pipe.close()                      # EOF with a review still running
        # Drain: release the held review so it can finish without cancel.
        fakes.release.set()
        t.join(timeout=10)
    finally:
        fakes.release.set()
        pipe.cleanup()
    assert not t.is_alive(), "serve() did not return"
    assert box["code"] == 0
    assert fakes.cancel_seen == [False], (
        "default drain must not set the cancel token on EOF")
    assert 1 in _by_id(out), "the in-flight review's response was abandoned"


def test_eof_cancels_when_disconnect_policy_is_cancel(monkeypatch):
    """Legacy cancel-on-disconnect is opt-in via SKODUN_MCP_DISCONNECT=cancel."""
    monkeypatch.setenv("SKODUN_MCP_DISCONNECT", "cancel")
    fakes = _Fakes(hold_review=True)
    pipe, out = _Pipe(), _Recorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(fakes.started.is_set, what="the review to start")
        pipe.close()
        t.join(timeout=10)
    finally:
        pipe.cleanup()
    assert not t.is_alive(), "serve() did not return"
    assert box["code"] == 0
    assert fakes.cancel_seen == [True], (
        "cancel policy must set the cancel token at EOF")
    assert 1 in _by_id(out), "the in-flight review's response was abandoned"


def test_disconnect_policy_defaults_to_drain(monkeypatch):
    from skodun.mcpserver import disconnect_policy

    monkeypatch.delenv("SKODUN_MCP_DISCONNECT", raising=False)
    assert disconnect_policy() == "drain"
    monkeypatch.setenv("SKODUN_MCP_DISCONNECT", "CANCEL")
    assert disconnect_policy() == "cancel"
    monkeypatch.setenv("SKODUN_MCP_DISCONNECT", "nope")
    assert disconnect_policy() == "drain"


def test_drain_timeout_sec_defaults_and_parses(monkeypatch):
    from skodun.mcpserver import DEFAULT_DRAIN_TIMEOUT_SEC, drain_timeout_sec

    monkeypatch.delenv("SKODUN_MCP_DRAIN_TIMEOUT_SECONDS", raising=False)
    assert drain_timeout_sec() == float(DEFAULT_DRAIN_TIMEOUT_SEC)
    monkeypatch.setenv("SKODUN_MCP_DRAIN_TIMEOUT_SECONDS", "0")
    assert drain_timeout_sec() == 0.0
    monkeypatch.setenv("SKODUN_MCP_DRAIN_TIMEOUT_SECONDS", "12.5")
    assert drain_timeout_sec() == 12.5
    monkeypatch.setenv("SKODUN_MCP_DRAIN_TIMEOUT_SECONDS", "nope")
    assert drain_timeout_sec() == float(DEFAULT_DRAIN_TIMEOUT_SEC)


def test_drain_timeout_falls_back_to_cancel_if_review_stuck(monkeypatch):
    """A hung review must not pin MCP forever under default drain."""
    monkeypatch.delenv("SKODUN_MCP_DISCONNECT", raising=False)
    monkeypatch.setenv("SKODUN_MCP_DRAIN_TIMEOUT_SECONDS", "0.05")
    fakes = _Fakes(hold_review=True)
    pipe, out = _Pipe(), _Recorder()
    server = _server(registry=fakes.registry(), stdin=pipe.reader, stdout=out)
    t, box = _serve_in_thread(server)
    try:
        pipe.send(_HANDSHAKE)
        pipe.send(_rpc("tools/call", 1, name="review", arguments={}))
        _wait_until(fakes.started.is_set, what="the review to start")
        pipe.close()                      # EOF; do NOT release hold
        t.join(timeout=10)
    finally:
        fakes.release.set()
        pipe.cleanup()
    assert not t.is_alive(), "serve() did not return after drain timeout"
    assert box["code"] == 0
    assert fakes.cancel_seen == [True], (
        "drain timeout must set cancel so a stuck review can finish cleanup")


def test_an_id_less_call_never_occupies_the_review_slot():
    """The id-less rule, in the one place where executing it would be visible
    even without a response: the single review slot."""
    fakes = _Fakes(hold_review=True)
    code, out, _ = _drive(
        _HANDSHAKE
        + _rpc("tools/call", None, name="review", arguments={})
        + _rpc("tools/call", 5, name="echo", arguments={"text": "free"}),
        registry=fakes.registry())
    assert code == 0
    assert fakes.log == [("echo", {"text": "free"})]
    assert [r.get("id") for r in _responses(out.data, "idless")] == [100, 5]


# --------------------------------------------------------------------------
# Per-call stores: sqlite connections are thread-bound.
# --------------------------------------------------------------------------

def test_every_tool_call_opens_its_own_store(tmp_path):
    """Two calls, two Store objects, both usable. A store cached on the server
    would be a connection created on the read loop's thread and then used by
    the review thread -- which sqlite refuses outright (next test)."""
    from skodun.store import Store

    db = tmp_path / "s.db"
    opened: list[object] = []

    def factory():
        store = Store.open(db)
        opened.append(store)
        return store

    def peek(call: HandlerCall) -> HandlerResult:
        with call.store_factory() as store:
            rows = store.list_reviews(None, limit=1)
        return HandlerResult(status=0, text=f"rows={len(rows)}", pending_acks=[])

    registry = (
        HandlerSpec(name="peek", long_running=False, input_schema={},
                    handler=peek, description="reads the store inline"),
        HandlerSpec(name="peek-slow", long_running=True, input_schema={},
                    handler=peek, description="reads the store off-thread"),
    )
    code, out, err = _drive(
        _HANDSHAKE
        + _rpc("tools/call", 1, name="peek", arguments={})
        + _rpc("tools/call", 2, name="peek-slow", arguments={}),
        registry=registry, store_factory=factory)
    assert code == 0, err.getvalue()
    got = _by_id(out)
    assert got[1]["result"]["content"][0]["text"] == "rows=0"
    assert got[2]["result"]["content"][0]["text"] == "rows=0", err.getvalue()
    assert len(opened) == 2 and opened[0] is not opened[1]


def test_a_store_opened_on_one_thread_cannot_be_used_on_another(tmp_path):
    """Why the factory exists at all, pinned rather than trusted: this is the
    exception a cached Store would produce inside the review thread."""
    from skodun.store import Store

    boom: list[BaseException] = []
    with Store.open(tmp_path / "s.db") as store:
        def use():
            try:
                store.list_reviews(None, limit=1)
            except BaseException as e:      # noqa: BLE001 - the point of the test
                boom.append(e)

        t = threading.Thread(target=use)
        t.start()
        t.join(timeout=10)
    assert boom and isinstance(boom[0], sqlite3.ProgrammingError), boom


# --------------------------------------------------------------------------
# Writes: one write + flush per response, acknowledgement strictly after.
# --------------------------------------------------------------------------

def test_every_response_is_exactly_one_write_then_one_flush():
    fakes = _Fakes()
    out = _Recorder()
    code, out, _ = _drive(
        _HANDSHAKE + _rpc("ping", 2)
        + _rpc("tools/call", 3, name="echo", arguments={"text": "x"}),
        registry=fakes.registry(), stdout=out)
    assert code == 0
    assert out.events == ["write", "flush"] * 3, out.events


def test_pending_acks_are_recorded_only_after_the_response_is_flushed():
    """Task 14's delivery contract, at the transport end: buffering is never
    "delivered". The acknowledgement must come after the write AND the flush,
    because a round marked delivered from a buffer that never reached a reader
    is the undelivered-findings bug this whole surface exists to remove.
    """
    order: list[str] = []

    class _Ordered(_Recorder):
        def write(self, data):
            order.append("write")
            return super().write(data)

        def flush(self):
            order.append("flush")
            return super().flush()

    fakes = _Fakes()
    acked: list[list[str]] = []

    def acknowledge(ids):
        order.append("ack")
        acked.append(list(ids))

    code, out, _ = _drive(
        _HANDSHAKE + _rpc("tools/call", 2, name="echo",
                          arguments={"text": "t", "acks": ["sk_1", "sk_2"]}),
        registry=fakes.registry(), stdout=_Ordered(), acknowledge=acknowledge)
    assert code == 0
    assert acked == [["sk_1", "sk_2"]]
    assert order[-3:] == ["write", "flush", "ack"], order


def test_a_failed_write_leaves_the_rounds_unacknowledged():
    """The other direction: the response never reached the client, so nothing
    was delivered. Re-delivery is the designed failure mode."""
    fakes = _Fakes()
    acked: list[list[str]] = []
    lost: list[int] = []
    code, out, err = _drive(
        _HANDSHAKE + _rpc("tools/call", 2, name="echo",
                          arguments={"text": "t", "acks": ["sk_1"]}),
        registry=fakes.registry(), stdout=_Recorder(fail=BrokenPipeError),
        acknowledge=lambda ids: acked.append(list(ids)),
        on_stdout_lost=lambda: lost.append(1))
    assert code == 0
    assert acked == []
    assert lost == [1]


def test_a_failing_acknowledgement_never_becomes_the_failure():
    """A ledger write that fails after the client already has the answer is a
    repeat, not a crash: the response is out, and the loop keeps serving."""
    fakes = _Fakes()

    def acknowledge(ids):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    code, out, err = _drive(
        _HANDSHAKE
        + _rpc("tools/call", 2, name="echo", arguments={"text": "t",
                                                       "acks": ["sk_1"]})
        + _rpc("ping", 3),
        registry=fakes.registry(), acknowledge=acknowledge)
    assert code == 0
    assert [r.get("id") for r in _responses(out.data, "ack-fail")] == [100, 2, 3]
    assert "readonly" in err.getvalue()


def test_a_dead_stdout_reader_stops_the_loop_cleanly():
    """`skodun mcp` with a client that went away mid-session. Nothing left to
    say and nobody to say it to: stop, exit 0, no traceback -- and do not keep
    executing the requests still queued on stdin, whose answers can no longer
    be delivered."""
    fakes = _Fakes()
    lost: list[int] = []
    code, out, err = _drive(
        _HANDSHAKE + _rpc("tools/call", 2, name="echo", arguments={"text": "x"}),
        registry=fakes.registry(), stdout=_Recorder(fail=BrokenPipeError),
        on_stdout_lost=lambda: lost.append(1))
    assert code == 0
    assert lost == [1], "the CLI was never told to blackhole its stdout"
    assert fakes.log == [], "requests were executed after stdout died"
    assert "Traceback" not in err.getvalue()


def test_diagnostics_go_to_stderr_and_stdout_stays_pure():
    code, out, err = _drive(b"not json\n" + _rpc("ping", 1))
    assert code == 0
    assert [r.get("id") for r in _responses(out.data, "diag")] == [None, 1]
    assert err.getvalue().strip(), "an unparseable line said nothing anywhere"


def test_a_notification_only_client_gets_no_bytes_at_all():
    code, out, err = _drive(_rpc("notifications/initialized")
                            + _rpc("notifications/whatever")
                            + b"\n")
    assert code == 0
    assert out.data == b""


def test_the_handshake_gate_is_the_initialize_REQUEST_not_the_notification():
    """A recorded, deliberate leniency. The spec has the client send
    `notifications/initialized` before its first real request, and both
    observed clients do -- but a client that pipelines `tools/list` behind
    `initialize` without waiting is asking a question this server can answer,
    and nothing here mutates anything, so refusing it would buy strictness at
    the cost of interoperability. The refusal that matters -- no handshake at
    all -- is pinned by `pre-init.jsonl`.
    """
    code, out, _ = _drive(_rpc("initialize", 1, protocolVersion="2025-11-25")
                          + _rpc("tools/list", 2))
    got = _by_id(out)
    assert "result" in got[2], got[2]
    fresh = _server()
    assert fresh._initialized is False and fresh._initialized_notified is False


def test_the_initialized_notification_is_recorded_even_though_it_gates_nothing():
    """The fact a stricter gate would be built on, and the reason this one is
    not: both observed clients send it, immediately, and it is silent."""
    server = _server(stdin=_rpc("initialize", 1, protocolVersion="2025-11-25")
                     + _rpc("notifications/initialized"))
    assert server.serve() == 0
    assert server._initialized is True
    assert server._initialized_notified is True


# --------------------------------------------------------------------------
# The `skodun mcp` seam.
# --------------------------------------------------------------------------

def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["SKODUN_DB"] = str(tmp_path / "mcp.db")
    env["SKODUN_CONFIG"] = str(tmp_path / "absent-config.toml")
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


@pytest.mark.parametrize("form", ["module", "module-cli", "console",
                                  "closed-stdout", "pipefail", "no-terminal",
                                  "dead-reader"])
def test_mcp_seam_matrix(tmp_path, form):
    """Exit code correctness across every invocation form, with stdin closed
    immediately -- the idle-EOF case, which must be a clean 0. A non-zero here
    makes every MCP client harness report a crashed server, and a traceback on
    stderr makes a lost client look like a bug in skodun.
    """
    env = _env(tmp_path / form)
    if form == "pipefail":
        script = (f'set -o pipefail; {shlex.quote(sys.executable)} -m skodun mcp '
                  f'< /dev/null | head -1; echo "SKODUN_EXIT=${{PIPESTATUS[0]}}"')
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=env, timeout=120)
        m = re.search(r"SKODUN_EXIT=(\d+)", p.stdout)
        assert m and int(m.group(1)) == 0, f"{p.stdout!r} {p.stderr!r}"
        assert "Traceback" not in p.stderr, p.stderr
        return
    if form == "dead-reader":
        # A reader that is gone before the first response: the server has one
        # request to answer and nowhere to put the answer.
        script = (f'{shlex.quote(sys.executable)} -m skodun mcp | true; '
                  f'echo "SKODUN_EXIT=${{PIPESTATUS[0]}}"')
        p = subprocess.run(["bash", "-c", script],
                           input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                           capture_output=True, text=True, env=env, timeout=120)
        assert re.search(r"SKODUN_EXIT=0", p.stdout), f"{p.stdout!r} {p.stderr!r}"
        assert "Traceback" not in p.stderr, p.stderr
        return
    if form == "closed-stdout":
        r_fd, w_fd = os.pipe()
        os.close(r_fd)
        try:
            p = subprocess.run([sys.executable, "-m", "skodun", "mcp"],
                               stdout=w_fd, stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.DEVNULL, env=env, timeout=120)
        finally:
            os.close(w_fd)
    elif form == "console":
        p = subprocess.run(
            [sys.executable, "-c", "from skodun.cli import entry; entry()", "mcp"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env,
            timeout=120)
    elif form == "no-terminal":
        p = subprocess.run([sys.executable, "-m", "skodun", "mcp"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True, env=env,
                           start_new_session=True, timeout=120)
    else:
        module = "skodun" if form == "module" else "skodun.cli"
        p = subprocess.run([sys.executable, "-m", module, "mcp"],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=env, timeout=120)
    assert p.returncode == 0, f"stderr={p.stderr!r}"
    assert "Traceback" not in p.stderr, p.stderr


@pytest.mark.parametrize("argv", [["mcp", "--no-such-flag"],
                                  ["mcp", "extra-positional"],
                                  ["mcp", "--repo", "."]])
def test_mcp_misuse_is_a_message_never_a_traceback(tmp_path, monkeypatch, capsys,
                                                   argv):
    """`mcp` takes no arguments: the tools carry their own, and a flag this
    command silently ignored would be a lie about where configuration lives."""
    from skodun.cli import main

    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "s.db"))
    assert main(argv) == 2
    cap = capsys.readouterr()
    assert "Traceback" not in cap.out and "Traceback" not in cap.err
    assert "usage:" in cap.err or "usage:" in cap.out


def test_the_mcp_subcommand_is_listed_for_humans():
    from skodun.cli import build_parser

    described = re.findall(r"^\s{4}(\S+)\s{2,}\S", build_parser().format_help(),
                          re.MULTILINE)
    assert "mcp" in described


def test_a_real_process_answers_a_tool_call_with_one_line_and_no_residue(tmp_path):
    """A tool result written by an actual `skodun mcp`-shaped process, through
    real pipes. The registry is injected by the bootstrap below because the
    shipped one is empty until Task 14 -- everything else (stdin, stdout, the
    loop) is the production path.
    """
    boot = tmp_path / "boot.py"
    boot.write_text(
        "import sys\n"
        "from skodun.mcpserver import HandlerResult, HandlerSpec, serve_stdio\n"
        "def echo(call):\n"
        "    return HandlerResult(status=0, text=call.params.get('text', ''),\n"
        "                         pending_acks=[])\n"
        "spec = HandlerSpec(name='echo', long_running=False,\n"
        "                   input_schema={'type': 'object'}, handler=echo,\n"
        "                   description='echo')\n"
        "raise SystemExit(serve_stdio(registry=(spec,)))\n",
        encoding="utf-8")
    payload = (_rpc("initialize", 1, protocolVersion="2025-11-25")
               + _rpc("notifications/initialized")
               + _rpc("tools/call", 2, name="echo", arguments={"text": "hello"}))
    p = subprocess.run([sys.executable, str(boot)], input=payload,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=_env(tmp_path), timeout=120)
    assert p.returncode == 0, p.stderr
    got = _responses(p.stdout, "real-process")
    assert [r.get("id") for r in got] == [1, 2]
    assert got[1]["result"]["content"][0]["text"] == "hello"
    assert got[1]["result"]["structuredContent"] == {"status": 0}
