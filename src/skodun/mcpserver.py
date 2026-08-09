"""The MCP stdio seam: hand-rolled newline-delimited JSON-RPC 2.0, stdlib only.

An MCP client speaks to this server over a pipe, and the pipe has exactly one
rule: **stdout carries nothing but newline-delimited JSON-RPC**. One stray byte
-- a `print`, a warning, a banner, half a line written by another thread -- and
the client's parser desynchronises and every later response is misattributed.
Diagnostics therefore go to stderr, every response is one `write` plus one
`flush` under one lock, and the protocol suite asserts that stdout parses
line-by-line with zero residue.

Everything else here follows from three more facts about the transport:

  * **The stream is hostile input.** It is not a client library, it is bytes. A
    line may not be UTF-8, may not be JSON, may be a 40 MiB JSON document, may
    be a JSON-RPC batch array (which MCP removed), may name a method this
    server never implemented. None of those may end the session or raise: each
    one gets its answer and the loop reads the next line.
  * **A message with no id has nowhere to send an answer.** So an id-less
    message is a NOTIFICATION whatever it names, and one naming a request-only
    method (`initialize`, `tools/call`, ...) is ignored WITHOUT BEING EXECUTED.
    Executing it would dismiss a finding, or acknowledge a delivery, with
    nobody told and no response line to order the acknowledgement behind.
  * **One review at a time, and never on the read loop's thread.** Reviews take
    minutes. Run one inline and the server stops answering `ping` -- clients
    take that for a hung server -- and stops noticing EOF, which is how a
    review learns its client is gone. So the single tool registered
    `long_running=True` runs on one background thread, capacity 1: a second
    call while it is busy is answered "review already in flight" rather than
    queued behind it (epic S3 MCP policy: refuse-if-busy). A queue would review
    a working tree that has usually moved by the time it starts; CLI reviews
    use FIFO ``review-fg`` capacity instead.

The tools do not implement anything. They arrive through the registry seam
(`HandlerSpec`/`HandlerCall`/`HandlerResult`) and every one of them is four lines
over a `services` function -- the SAME function the corresponding `skodun`
subcommand calls. That is the whole design of this surface: an agent and a human
are looking at one product, so a refusal an agent reads is the refusal a human
reads, word for word, because neither surface owns the words. `tools/list` is a
curated mirror of the CLI's review loop and nothing more; a snapshot test pins
the exact list, so growing it is a reviewed decision.

The transport knows only that a handler takes a call and returns a status, some
text, and a list of review ids whose delivery it must acknowledge AFTER the
response has been written and flushed -- never before, because a round marked
delivered from a buffer that never reached a reader is the undelivered-findings
bug the delivery ledger exists to remove.
"""

import json
import math
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import __version__

if TYPE_CHECKING:            # the annotation only; importing `store` for real
    from .store import Store  # would make every `skodun mcp` pay for sqlite3
                              # before it has served a single line

#: The protocol revision this server declares when it does not recognise the
#: client's. Negotiation is a RULE, not a constant: probes against the two
#: installed clients found claude-code 2.1.118 asking for `2025-11-25` and
#: codex-cli 0.144.5 asking for `2025-06-18` on the same day, so echoing a
#: revision we support is the only answer that keeps both happy (both captures
#: are committed under `tests/fixtures/mcp/`).
MCP_PROTOCOL_VERSION = "2025-11-25"

#: Every revision whose `initialize`/`ping`/`tools`/`prompts` subset is the one
#: implemented here -- which is all of them: that subset is the oldest and most
#: stable part of MCP, and the only part skodun serves. `structuredContent` in a
#: tool result post-dates the oldest two, and is additive: a client that does
#: not know the field ignores it and reads `content` as it always did.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26",
                               "2024-11-05")

#: `serverInfo.name`. The client shows it; the version travels beside it.
SERVER_NAME = "skodun"

#: How many consecutive "could not tell" drift probes to tolerate before giving
#: up on the diagnostic for this session. Each probe is two git subprocesses,
#: and a `status --porcelain` wedged on a network filesystem is indistinguishable
#: from a transient `index.lock` held by a concurrent commit -- so an unbounded
#: retry would spend the whole timeout on every tool call, forever, for a note
#: nobody is waiting on. Three is enough to ride out contention and small enough
#: that a genuinely stuck git costs a bounded amount.
_DRIFT_UNREADABLE_TRIES = 3

#: Said when SIGTERM finds nothing to cancel. Pre-encoded because it is written
#: from a SIGNAL HANDLER with a raw `os.write`: that is one syscall taking no
#: Python-level lock, where `print` to a buffered stream could deadlock against
#: the very frame the signal interrupted. An idle server spends all its time
#: blocked in `readline`, so a note deferred to the next message is a note the
#: idle case -- the only case this fires in -- would never actually show.
_IDLE_SIGTERM_NOTE = (
    b"skodun mcp: note: SIGTERM arrived with no review in flight, so nothing "
    b"was cancelled and this server is still serving. On `skodun mcp` SIGTERM "
    b'means "cancel the running review" -- it is how cross-process `skodun '
    b'review-cancel` reaches one -- and never "exit". To stop this server: '
    b"close its stdin (restart the MCP entry in your host), or send SIGINT.\n")

#: A line longer than this is drained and refused. 8 MiB is far above any real
#: `tools/call` (a review argument is a path and a few flags) and far below the
#: size at which a single line becomes a memory problem for a machine that is
#: about to run a code review.
MAX_LINE_BYTES = 8 * 1024 * 1024

#: How much of an oversized line is read at a time while discarding it.
_DRAIN_CHUNK_BYTES = 1 << 20

# JSON-RPC 2.0 error codes, plus the one MCP adds.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_INITIALIZED = -32002

#: Verbatim: clients match on it, and the pinned wording is what the protocol
#: suite asserts.
NOT_INITIALIZED_MESSAGE = "server not initialized"

#: The tool-level text a second concurrent review call gets. A TOOL error, not
#: a protocol error: the agent asked a reasonable question and the answer is
#: "not now", which it can read, retry, and reason about.
BUSY_TEXT = "review already in flight"

#: Returned by ``_install_sigterm_forwarder`` when install is impossible.
#: Distinct from ``None`` (a valid previous disposition from ``signal.signal``)
#: so restore does not leave a review-scoped handler installed forever.
_SIGTERM_INSTALL_FAILED = object()

#: How the server treats an in-flight `review` when the client disconnects
#: (stdin EOF / session end) or the read loop otherwise stops.
#:
#: * ``drain`` (default) — do **not** set the cancel token; join the worker so
#:   the review can finalize to the store. Operator MCP restarts and host
#:   reloads must not throw away minutes of model work or leave a cancelled
#:   row that never covers the tree.
#: * ``cancel`` — set the cancel token then join (legacy). Explicit
#:   ``review_cancel`` / cross-process SIGTERM still cancel regardless.
#:
#: Override with ``SKODUN_MCP_DISCONNECT=cancel`` or ``=drain``.
DISCONNECT_POLICY_ENV = "SKODUN_MCP_DISCONNECT"
DISCONNECT_DRAIN = "drain"
DISCONNECT_CANCEL = "cancel"
DEFAULT_DISCONNECT_POLICY = DISCONNECT_DRAIN

#: Max seconds to wait for an in-flight review under **drain** before falling
#: back to cancel (so a hung provider cannot pin MCP open forever). Override
#: with ``SKODUN_MCP_DRAIN_TIMEOUT_SECONDS``. ``0`` means no extra ceiling
#: (join until cancel policy would apply only via SIGTERM/review_cancel).
DRAIN_TIMEOUT_ENV = "SKODUN_MCP_DRAIN_TIMEOUT_SECONDS"
DEFAULT_DRAIN_TIMEOUT_SEC = 2 * 60 * 60  # 2 hours — above normal review budgets

#: After drain timeout (or cancel policy) sets the cancel token, wait this long
#: for the worker to finish cleanup before exiting the process anyway. Review
#: threads are daemons; a stuck join after cancel would otherwise pin MCP open
#: forever. Provider process groups should die via the cancel path; residual
#: orphans match the SIGKILL failure mode and are swept by stale recovery.
POST_CANCEL_JOIN_ENV = "SKODUN_MCP_POST_CANCEL_JOIN_SECONDS"
DEFAULT_POST_CANCEL_JOIN_SEC = 120.0

#: The status a busy refusal and a failed handler report. 2 is the gate
#: contract's "no trustworthy review covers this content" -- the conservative
#: reading of every outcome where nothing ran to completion.
BUSY_STATUS = 2
HANDLER_FAILURE_STATUS = 2

#: The delivery channel rounds surfaced through this transport are acknowledged
#: under (`delivery.CHANNELS`).
MCP_CHANNEL = "mcp"

#: The methods a client may send before the handshake. `initialize` for obvious
#: reasons; `ping` because a client is allowed to check that the process it just
#: spawned is alive before it commits to a handshake, and answering `{}` tells
#: it nothing it could not already see.
PRE_INIT_METHODS = frozenset({"initialize", "ping"})


@dataclass(frozen=True)
class HandlerSpec:
    """One tool: its name, whether it is the long-running one, its JSON Schema,
    and the callable that runs it.

    `description` is a fifth, DEFAULTED field beyond the four the plan pins:
    `tools/list` carries a description per tool, and an agent that has to guess
    what a tool does from its name is a worse agent. Defaulted so the pinned
    four remain sufficient to construct one.
    """

    name: str
    long_running: bool
    input_schema: dict
    handler: Callable[["HandlerCall"], "HandlerResult"]
    description: str = ""


@dataclass(frozen=True)
class HandlerCall:
    """What a handler is given: the call's `arguments`, a way to open a Store,
    and the cancellation token for this call.

    `store_factory` rather than a Store, because sqlite connections are bound
    to the thread that created them: the long-running handler runs on another
    thread and would get `ProgrammingError` from any connection this loop
    opened. Per-call connections also mean a handler's Store lifetime is exactly
    its call, which is what makes the post-response acknowledgement open a fresh
    one.
    """

    params: dict
    store_factory: Callable[[], "Store"]
    cancel: threading.Event
    #: The `clientInfo.name` this session handshook with, RAW and unmapped, or
    #: None. A hint and nothing more: it is the lowest-priority source of the
    #: caller's model family (`routing.resolve_client_family`), below both the
    #: tool argument and the operator's env, and a name nothing recognises
    #: simply leaves the family undeclared. Defaulted so that every handler test
    #: that builds a call by hand keeps working.
    client_name: str | None = None


@dataclass(frozen=True)
class HandlerResult:
    """What a handler returns: the CLI's exit code, the CLI's text, and the
    review ids whose delivery this transport must acknowledge once the response
    is out.

    `pending_acks` defaults to empty: a tool with nothing to deliver should not
    have to say so.
    """

    status: int
    text: str
    pending_acks: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PromptSpec:
    """A static MCP prompt -- the `/mcp__skodun__<name>` slash command surface."""

    name: str
    description: str
    text: str


class _RpcError(Exception):
    """A protocol-level refusal raised by a method handler."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Deferred:
    """Sentinel: this method answered (or will answer) for itself."""


_DEFERRED = _Deferred()


# ---------------------------------------------------------------------------
# The tools: exactly the CLI's own review loop, and nothing else
# ---------------------------------------------------------------------------
#
# Every handler below is four lines and they are all the same four lines: read
# the arguments, open a Store, call the `services` function the CLI subcommand
# calls, return its `(status, text)` as a `HandlerResult`. That sameness IS the
# feature -- an agent and a human are looking at one product, so a refusal an
# agent reads must be the refusal a human reads, word for word, and the only way
# to guarantee that is for neither surface to own the words.
#
# WHAT IS DELIBERATELY ABSENT is as much of the decision as what is here:
#
#   * no bulk anything. There is no `dismiss_all`, no `adopt_all`, no
#     `triage_many`. A dismissal is a human naming ONE finding and saying why;
#     a tool that dismissed a list would be exactly the auto-dismissal the
#     per-finding path exists to keep out of the product, with an agent holding
#     the pen.
#   * no `dispatch`, no `worker`, no `install-hooks`, no `import-legacy`, no
#     `shadow-compare`, no `providers`. Those are machinery and diagnostics a
#     human runs, not steps in a review loop, and every one of them is a tool
#     surface nobody would have reviewed.
#   * no tool that writes configuration, and no tool that takes a store path.
#     The store is `SKODUN_DB` or the default, resolved in ONE place
#     (`cli._store_path`), and a tool argument for it would be a second answer.
#
# `tools/list`'s order is the order below, and a snapshot test pins the exact
# list: adding a tool here is a reviewed decision, not a convenience.

#: The `repo` property, spelled once: four tools take it and they must agree.
_REPO_PROPERTY = {
    "repo": {"type": "string",
             "description": "path inside the repository to act on; defaults to "
                            "the server's working directory"},
}

_REVIEW_ID_PROPERTY = {
    "review_id": {"type": "string",
                  "description": "the review id, as `log` and the verdict "
                                 "banner print it"},
}

_INDEX_PROPERTY = {
    "index": {"type": "integer", "minimum": 0,
              "description": "the finding index, as `triage_list` prints it in "
                             "`[n]`"},
}

_REASON_PROPERTY = {
    "reason": {"type": "string",
               "description": "the audited reason, in the reviewer's own words; "
                              "it is stored verbatim and must say something "
                              "specific about this finding"},
}

#: The deferral's filed reference. MANDATORY, and the description says why in
#: the schema itself: this is the argument an agent is most likely to want to
#: leave out, and `inputSchema` is the only documentation it reads.
_TRACKING_REF_PROPERTY = {
    "tracking_ref": {
        "type": "string",
        "description": "where the deferred work is FILED: an issue number "
                       "(#412), a tracker key (SKO-7), a repo-qualified issue "
                       "(owner/repo#5), or a URL. One token, not prose, and "
                       "not optional -- an unfiled deferral and an ignored "
                       "finding are the same artifact. File the issue FIRST, "
                       "then record its reference here"},
}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    """One JSON Schema shape for every tool: an object, closed, explicit.

    `additionalProperties: False` is not decoration. A client that misspells
    `review_id` as `reviewID` would otherwise send a well-formed call that this
    server answers "no such review: None" to, and the agent would go looking for
    the review instead of for its own typo.
    """
    return {"type": "object", "properties": dict(properties),
            "required": list(required), "additionalProperties": False}


def _repo_arg(params: dict, tool: str) -> tuple[Path | None, str]:
    """`(repo, "")` or `(None, refusal)`. ABSENT means the server's own cwd.

    `HandlerCall` carries no repo and `skodun mcp` takes no flags, deliberately
    (Task 13): every tool carries its own arguments in its `inputSchema`, so a
    transport-level flag would be a second place the same thing is configured.
    The default is `.`, which for a client-spawned server is the project it was
    spawned in.

    A repo of the WRONG TYPE is refused rather than defaulted, and that asymmetry
    is the point: defaulting a `{"repo": ["x"]}` to the cwd would answer a GATE
    QUESTION ABOUT A DIFFERENT DIRECTORY than the one the client asked about, and
    a wrong PASS is the one failure this product exists to make impossible. An
    absent repo is a client saying "here"; a malformed one is a client saying
    something this server must not guess at.
    """
    if "repo" not in params or params["repo"] is None:
        return Path("."), ""
    repo = params["repo"]
    if not isinstance(repo, str) or not repo.strip():
        return None, (f"skodun {tool}: repo must be a path inside a repository; "
                      f"got {repo!r}")
    return Path(repo), ""


def _string_arg(params: dict, name: str, tool: str) -> tuple[str, str]:
    """`(value, "")` or `("", refusal)`. The tool-level counterpart of argparse.

    The CLI cannot reach these refusals -- argparse rejects a missing positional
    before `_cmd_triage` runs -- so they have no wording to stay in step with, and
    they are worded for the reader they DO have: an agent that will re-call the
    tool. `inputSchema` already says the argument is required; a schema is
    advisory, and this is the enforcement.
    """
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        return "", (f"skodun {tool}: {name} is required and must be a non-empty "
                    f"string; got {value!r}")
    return value, ""


def _int_arg(params: dict, name: str, tool: str) -> tuple[int | None, str]:
    """`(value, "")` or `(None, refusal)`. Rejects `bool`, which is an `int`."""
    if name not in params or params[name] is None:
        return None, ""
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None, (f"skodun {tool}: {name} must be an integer; got "
                      f"{value!r}")
    return value, ""


def _float_arg(params: dict, name: str, tool: str) -> tuple[float | None, str]:
    """Optional finite JSON number, rejecting bool and string coercion."""
    if name not in params or params[name] is None:
        return None, ""
    value = params[name]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))):
        return None, (f"skodun {tool}: {name} must be a finite number; got "
                      f"{value!r}")
    return float(value), ""


def _bool_arg(params: dict, name: str, tool: str,
              default: bool = False) -> tuple[bool, str]:
    """`(value, "")` or `(default, refusal)`. ABSENT is not the same as bad.

    An absent argument (or an explicit JSON `null`) is the caller declining to
    set it, and takes `default` — the same reading `_repo_arg` gives an absent
    repo. Anything else must be a real `bool`.

    `bool(params.get(name, False))` is what this replaces, and it is not
    validation: it says True for the STRING `"false"`, for `"no"`, for `0.1` and
    for any non-empty container. Over JSON-RPC a stringified boolean is the most
    likely way a client gets this wrong, and coercion turns that mistake into the
    OPPOSITE of what was asked — which for `include_delivered` means replaying,
    and re-acknowledging, rounds the ledger has already delivered. A refusal
    tells the agent to re-call; a silent reinterpretation tells it nothing.
    """
    if name not in params or params[name] is None:
        return default, ""
    value = params[name]
    if not isinstance(value, bool):
        return default, (f"skodun {tool}: {name} must be true or false; got "
                         f"{value!r}")
    return value, ""


def _opt_string_arg(params: dict, name: str,
                    tool: str) -> tuple[str | None, str]:
    """`(value-or-None, "")` or `(None, refusal)`. ABSENT is not the same as bad.

    `_reason_arg`'s shape for an argument the SERVICE owns the semantics of. An
    absent argument (or an explicit JSON `null`) is the caller declining to
    choose and passes through as `None`; anything present that is not a string
    is refused here, because argparse cannot produce that shape and so there is
    no CLI wording to stay in step with.

    The EMPTY string is deliberately NOT refused here, which is where this
    differs from `_string_arg`: `skodun review --reviewer ""` is a request for a
    reviewer named `""`, the service refuses it as a name nobody configured, and
    the two surfaces have to say that in the same words. Refusing it at the
    transport would give the agent a different sentence than the human gets.
    """
    if name not in params or params[name] is None:
        return None, ""
    value = params[name]
    if not isinstance(value, str):
        return None, (f"skodun {tool}: {name} must be a string; got {value!r}")
    return value, ""


def _reason_arg(params: dict, tool: str) -> tuple[str | None, str]:
    """`(reason-or-None, "")` or `(None, refusal)`. ABSENT is not the same as bad.

    An absent reason is passed through as `None`, because the service owns that
    refusal and its wording is the one the CLI prints -- the whole point of the
    parity. A reason that is PRESENT but not a string is refused here, and this
    check is not theoretical: `reason=["a", "b"]` reached
    `store.record_triage_event` and came back out as
    `sqlite3.ProgrammingError: Error binding parameter 11`, which the transport
    would then hand the agent as its tool text. Nothing was written -- the
    statement failed at bind time -- but "the tool failed: ProgrammingError" is
    not something an agent can act on, and argparse cannot produce this shape at
    all, so there is nothing to stay in step with.
    """
    value = params.get("reason")
    if value is None:
        return None, ""
    if not isinstance(value, str):
        return None, (f"skodun {tool}: reason must be a string, in the "
                      f"reviewer's own words; got {value!r}")
    return value, ""


def _handle_gate(call: "HandlerCall") -> "HandlerResult":
    from . import services
    repo, refusal = _repo_arg(call.params, "gate")
    if refusal:
        # 2, which is also the gate's own "no trustworthy review covers this":
        # a question this server could not understand has not been answered YES.
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_gate(store, repo)
    return HandlerResult(status=status, text=text)


def disconnect_policy() -> str:
    """``drain`` (default) or ``cancel`` for in-flight review on session end."""
    raw = (os.environ.get(DISCONNECT_POLICY_ENV) or DEFAULT_DISCONNECT_POLICY)
    value = str(raw).strip().lower()
    if value == DISCONNECT_CANCEL:
        return DISCONNECT_CANCEL
    return DISCONNECT_DRAIN


def _env_nonneg_float(name: str, default: float) -> float:
    """Parse a non-negative finite float env, else ``default``.

    Exact ``0`` is kept (callers may treat it as "no wait" / "no ceiling").
    Junk, negative, and non-finite values fall back to ``default``.
    """
    import math

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(str(raw).strip())
    except ValueError:
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return value


def drain_timeout_sec() -> float:
    """Seconds to wait under drain before cancelling a stuck review.

    From ``SKODUN_MCP_DRAIN_TIMEOUT_SECONDS`` (default 2h). Junk / negative /
    non-finite → default. Exact ``0`` disables the drain ceiling.
    """
    return _env_nonneg_float(DRAIN_TIMEOUT_ENV, DEFAULT_DRAIN_TIMEOUT_SEC)


def post_cancel_join_sec() -> float:
    """Seconds to join after cancel before exiting anyway (default 120)."""
    return _env_nonneg_float(
        POST_CANCEL_JOIN_ENV, DEFAULT_POST_CANCEL_JOIN_SEC)


def _handle_review(call: "HandlerCall") -> "HandlerResult":
    """The long-running one. `call.cancel` is the whole point of it being so.

    The token is set by **explicit cancel** (MCP ``review_cancel``, CLI
    ``review-cancel``, or the main-thread SIGTERM forwarder used for
    cross-process cancel) — not by a normal session end. On stdin EOF the
    server **drains** by default: it joins this thread without setting the
    token so the review can finalize into the store (restart-safe). Set
    ``SKODUN_MCP_DISCONNECT=cancel`` to restore legacy cancel-on-disconnect.
    The token still travels into `run_review`, pass boundaries, the chain, and
    the watchdog tick loop. `svc_review` turns `ReviewCancelled` into
    `(4, "... reason=review cancelled")`, so this thread returns an ordinary tool
    result rather than raising, and the server joins it before exiting 0.

    NOTHING is printed. `run_review` writes no stdout at all any more, and its
    progress goes to stderr -- which for an MCP server is the client's log, where
    a human debugging a slow review will actually look for it.

    `reviewer` is the one argument this handler passes through without judging:
    whether a name resolves is a question about the loaded config, and the
    service (through `run_review`'s preflight) owns that refusal so an agent and
    a human get the same sentence for the same mistake. Only its TYPE is checked
    here, and before the store is opened, so a malformed call cannot start a
    review of anything.

    `client_family` is resolved HERE rather than passed through, because this is
    the only surface with a third source for it: the handshake's
    `clientInfo.name`. The documented priority is by specificity -- the tool
    argument describes this call, `SKODUN_CLIENT_FAMILY` describes this machine,
    the client name is a guess about a handshake -- and threading the raw name
    down instead would let the guess outrank the operator's env.
    """
    from . import routing, services
    repo, refusal = _repo_arg(call.params, "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    reviewer, refusal = _opt_string_arg(call.params, "reviewer", "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    family, refusal = _opt_string_arg(call.params, "client_family", "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    family = routing.resolve_client_family(family, client_name=call.client_name)
    recover, refusal = _bool_arg(call.params, "recover", "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    max_attempts, refusal = _int_arg(call.params, "max_attempts", "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    max_wall_seconds, refusal = _float_arg(
        call.params, "max_wall_seconds", "review")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        if recover:
            status, text, metadata = services.svc_review_detailed(
                store, repo, cancel=call.cancel, reviewer=reviewer,
                client_family=family, recover=True,
                max_attempts=max_attempts,
                max_wall_seconds=max_wall_seconds)
        else:
            status, text = services.svc_review(
                store, repo, cancel=call.cancel, reviewer=reviewer,
                client_family=family)
            metadata = {}
    return HandlerResult(status=status, text=text, metadata=metadata)


def _handle_log(call: "HandlerCall") -> "HandlerResult":
    from . import gitio, services
    branch = call.params.get("branch")
    limit = 20                          # the CLI's own `-n` default
    if call.params.get("limit") is not None:
        # `_int_arg`, not a second copy of `svc_log`'s message: a NON-POSITIVE
        # limit is the service's refusal (and the string the CLI prints for it),
        # while a limit of the wrong TYPE is this transport's, exactly as argparse
        # owns `-n lots` for the CLI. `"5"` is refused rather than coerced -- the
        # schema says integer, and coercing would make the tool laxer than the
        # contract it publishes.
        limit, refusal = _int_arg(call.params, "limit", "log")
        if refusal:
            return HandlerResult(status=2, text=refusal)
    repo_path, refusal = _repo_arg(call.params, "log")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    branch = branch if isinstance(branch, str) and branch else None
    # `_repo_arg` hands back a CHECKOUT PATH; the column stores
    # `gitio.git_common_dir` of it, so this transport converts -- and only when
    # there is a branch to narrow, because `git_common_dir` shells out to git
    # and an unscoped `log` from a server spawned outside a repository is this
    # tool's contract. A conversion that fails is a refusal, never a fall back.
    scope = None
    if branch is not None:
        try:
            scope = str(gitio.git_common_dir(repo_path))
        except Exception as e:
            return HandlerResult(
                status=2,
                text=f"skodun log: could not resolve the repository for "
                     f"branch: {e!r}")
    with call.store_factory() as store:
        status, text = services.svc_log(store, branch, limit, scope)
    # An empty listing is an answer, and an empty tool result is not readable as
    # one: an agent cannot tell "no reviews" from "the tool broke".
    return HandlerResult(status=status,
                         text=text or "skodun log: no reviews recorded yet")


def _handle_surface(call: "HandlerCall") -> "HandlerResult":
    """The one tool with an ACKNOWLEDGEMENT, and the order is the product.

    `pending_acks` comes back to the transport, which records the delivery only
    after this response line has been WRITTEN AND FLUSHED, from a fresh Store
    (this one closes with the call). Acknowledging here instead would mark rounds
    delivered from a buffer that may never reach the client -- the undelivered-
    findings failure the delivery ledger exists to remove, reintroduced by the
    fix. A crash between the flush and the ack re-delivers, which is the designed
    direction.
    """
    from . import delivery, gitio, services
    params = call.params
    repo, refusal = _repo_arg(params, "surface")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    fmt = params.get("hook_format", delivery.TEXT)
    if fmt not in delivery.FORMATS:
        return HandlerResult(
            status=2,
            text=f"skodun surface: unknown hook_format {fmt!r}; expected one of "
                 f"{list(delivery.FORMATS)}")
    include_delivered, refusal = _bool_arg(params, "include_delivered",
                                           "surface")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    branch, why_not = services.resolve_surface_branch(params.get("branch"), repo)
    if not branch:
        return HandlerResult(status=2, text=why_not)
    # `_repo_arg` returns a CHECKOUT PATH -- what `resolve_surface_branch`
    # wants, and not what the column stores. The conversion happens here, per
    # transport, and a conversion that fails is a refusal: reporting (and
    # permanently acknowledging) some other repository's rounds because the
    # named one could not be read is the damage this scope exists to remove.
    try:
        scope = str(gitio.git_common_dir(repo))
    except Exception as e:
        return HandlerResult(
            status=2,
            text=f"skodun surface: could not resolve the repository to report "
                 f"on: {e!r}")
    with call.store_factory() as store:
        status, text, pending = services.svc_surface(
            store, branch, scope, fmt, include_delivered)
    if status != 0 or not text:
        # A diagnostic, or nothing to report. Either way there is nothing
        # delivered, so nothing to acknowledge.
        return HandlerResult(
            status=status,
            text=text if status != 0 else services.surface_no_rounds_note(branch))
    return HandlerResult(status=status, text=text, pending_acks=pending)


def _handle_triage_list(call: "HandlerCall") -> "HandlerResult":
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "triage_list")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_triage_list(store, review_id)
    return HandlerResult(
        status=status,
        text=text or f"skodun triage: review {review_id} has no findings")


def _handle_triage_dismiss(call: "HandlerCall") -> "HandlerResult":
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "triage_dismiss")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    index, refusal = _int_arg(call.params, "index", "triage_dismiss")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    reason, refusal = _reason_arg(call.params, "triage_dismiss")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_triage_dismiss(store, review_id, index,
                                                   reason)
    return HandlerResult(status=status, text=text)


def _handle_adopt_refuter(call: "HandlerCall") -> "HandlerResult":
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "adopt_refuter")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    index, refusal = _int_arg(call.params, "index", "adopt_refuter")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_adopt_refuter(store, review_id, index)
    return HandlerResult(status=status, text=text)


def _handle_triage_reopen(call: "HandlerCall") -> "HandlerResult":
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "triage_reopen")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    index, refusal = _int_arg(call.params, "index", "triage_reopen")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    reason, refusal = _reason_arg(call.params, "triage_reopen")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_triage_reopen(store, review_id, index,
                                                  reason)
    return HandlerResult(status=status, text=text)


def _handle_triage_defer(call: "HandlerCall") -> "HandlerResult":
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "triage_defer")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    index, refusal = _int_arg(call.params, "index", "triage_defer")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    # `_opt_string_arg`, not `_string_arg`, and the asymmetry is the parity
    # rule: an ABSENT reference is the SERVICE's refusal (`TRIAGE_DEFER_USAGE`,
    # the string argparse's missing positional produces for the CLI), while an
    # EMPTY one is a real deferral attempt that `triage.validate_tracking_ref`
    # declines with the words a human sees. Refusing `""` here instead would
    # hand the agent a different sentence for the same mistake. Only the TYPE is
    # this transport's business, because argparse cannot produce a non-string.
    tracking_ref, refusal = _opt_string_arg(call.params, "tracking_ref",
                                            "triage_defer")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    reason, refusal = _reason_arg(call.params, "triage_defer")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_triage_defer(store, review_id, index,
                                                 tracking_ref, reason)
    return HandlerResult(status=status, text=text)


def _handle_feedback_add(call: "HandlerCall") -> "HandlerResult":
    """Non-gate feedback: judgment / product bugs for later inspection."""
    from . import services
    kind, refusal = _string_arg(call.params, "kind", "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    body, refusal = _string_arg(call.params, "body", "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    actor, refusal = _opt_string_arg(call.params, "actor", "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    if not actor:
        actor = "agent"
    review_id, refusal = _opt_string_arg(call.params, "review_id",
                                         "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    index = None
    if "index" in call.params and call.params["index"] is not None:
        index, refusal = _int_arg(call.params, "index", "feedback_add")
        if refusal:
            return HandlerResult(status=2, text=refusal)
    provider, refusal = _opt_string_arg(call.params, "provider",
                                        "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    repo_path, refusal = _repo_arg(call.params, "feedback_add")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    repo = str(repo_path) if repo_path is not None else None
    with call.store_factory() as store:
        status, text = services.svc_feedback_add(
            store, kind=kind, body=body, actor=actor,
            review_id=review_id, finding_index=index,
            provider=provider, repo=repo, source="mcp")
    return HandlerResult(status=status, text=text)


def _handle_feedback_list(call: "HandlerCall") -> "HandlerResult":
    from . import services
    kind, refusal = _opt_string_arg(call.params, "kind", "feedback_list")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    review_id, refusal = _opt_string_arg(call.params, "review_id",
                                         "feedback_list")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    limit = 50
    if "limit" in call.params and call.params["limit"] is not None:
        limit, refusal = _int_arg(call.params, "limit", "feedback_list")
        if refusal:
            return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_feedback_list(
            store, kind=kind, review_id=review_id, limit=limit)
    return HandlerResult(status=status, text=text)


def _handle_review_status(call: "HandlerCall") -> "HandlerResult":
    """Read-only lifecycle observation. Same service the CLI calls."""
    from . import gitio, services
    review_id, refusal = _opt_string_arg(call.params, "review_id",
                                         "review_status")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    repo_path, refusal = _repo_arg(call.params, "review_status")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    scope = None
    # Only resolve repo when the caller is asking for "current for repo"
    # (no review_id). An id alone must not require a worktree.
    if not review_id:
        try:
            scope = str(gitio.git_common_dir(repo_path))
        except BaseException as e:
            return HandlerResult(
                status=2,
                text=f"skodun review-status: could not resolve repo: {e!r}")
    with call.store_factory() as store:
        status, text = services.svc_review_status(
            store, review_id=review_id, repo=scope)
    return HandlerResult(status=status, text=text)


def _handle_review_cancel(call: "HandlerCall") -> "HandlerResult":
    """Cancel-by-id. Same service the CLI calls."""
    from . import services
    review_id, refusal = _string_arg(call.params, "review_id", "review_cancel")
    if refusal:
        return HandlerResult(status=2, text=refusal)
    with call.store_factory() as store:
        status, text = services.svc_review_cancel(store, review_id)
    return HandlerResult(status=status, text=text)


def default_registry() -> tuple[HandlerSpec, ...]:
    """The tools `skodun mcp` serves: the CLI's review loop, mirrored exactly.

    The list and its ORDER are pinned by a snapshot test. Growing it is a
    reviewed decision about an agent-facing surface on a fail-closed gate, never
    a convenience -- see the note above this function for what is deliberately
    not here and why.
    """
    return (
        HandlerSpec(
            name="gate", long_running=False,
            input_schema=_schema(_REPO_PROPERTY),
            handler=_handle_gate,
            description="Fail closed unless a trustworthy review covers this "
                        "change. Exit status 0 = clean, 1 = findings remain "
                        "open, 2 = no trustworthy review covers this content. "
                        "The same decision `skodun gate` makes, from the same "
                        "store."),
        HandlerSpec(
            name="review", long_running=True,
            input_schema=_schema({
                **_REPO_PROPERTY,
                # BY NAME, never by provider id: two enabled entries may share a
                # provider, and choosing between them by an unstated rule would
                # also choose a model, an effort, a prompt budget and a fallback
                # chain the caller never asked about. A name that does not
                # resolve is refused before anything runs, and the refusal lists
                # the configured names -- which is how an agent discovers them,
                # since `providers` is deliberately not a tool.
                "reviewer": {
                    "type": "string",
                    "description": "name of the configured reviewer entry to "
                                   "head this review's chain, instead of the "
                                   "config's own `finder`; its own fallbacks "
                                   "still apply. Defaults to the config's "
                                   "choice. A name that is unknown, disabled, "
                                   "or on a provider with no adapter is "
                                   "refused before anything runs, and the "
                                   "refusal names the configured entries"},
                # The CALLER's family, not a reviewer's. Optional, and worth one
                # tie-break: it lets an agent ask for a second opinion from a
                # different model family without pinning a provider (which would
                # also pin a model, an effort and a chain).
                "client_family": {
                    "type": "string",
                    "description": "the calling client's own model family "
                                   "(xai, openai, google, junie). With "
                                   "auto-routing enabled and no `reviewer`, a "
                                   "finder from a DIFFERENT family is "
                                   "preferred when one is free. A soft "
                                   "preference only -- it never leaves the "
                                   "review without a reviewer. Defaults to "
                                   "$SKODUN_CLIENT_FAMILY, else to a guess "
                                   "from the client name in the handshake"},
                "recover": {
                    "type": "boolean",
                    "description": "opt into bounded fresh recovery attempts "
                                   "when the first review is not trustworthy "
                                   "(default false)"},
                "max_attempts": {
                    "type": "integer", "minimum": 1, "maximum": 8,
                    "description": "maximum recovery attempts including the "
                                   "first (default 3)"},
                "max_wall_seconds": {
                    "type": "number", "exclusiveMinimum": 0,
                    "description": "maximum recovery wall-clock budget in "
                                   "seconds (default 900; maximum 86400)"},
            }),
            handler=_handle_review,
            description="Review the outgoing change NOW, in the foreground, and "
                        "record the verdict. LONG-RUNNING: it takes minutes and "
                        "spends model calls. MCP policy (epic S3): only ONE may "
                        "be in flight per server -- a second call while one is "
                        "running is refused, not queued (a queue would review a "
                        "moved tree). CLI uses FIFO review-fg capacity + the "
                        "legacy FG lock instead. Closing the session DRAINS by "
                        "default (review finishes into the store; set "
                        "SKODUN_MCP_DISCONNECT=cancel to abort). Explicit "
                        "review_cancel / review-cancel still cancel. OMIT "
                        "`reviewer` unless you want a specific entry: with "
                        "`[routing] mode = \"auto\"` that lets skodun pick a "
                        "finder with a free provider slot instead of piling "
                        "onto a busy one. A pin is absolute in every mode -- "
                        "use it for a deliberate second opinion. Status 0 "
                        "= clean, 1 = findings open, 2 = nothing ran "
                        "(including busy refusal), 3 = gave up waiting for "
                        "capacity/lock, 4 = no trustworthy review exists."),
        HandlerSpec(
            name="log", long_running=False,
            input_schema=_schema({
                **_REPO_PROPERTY,
                "branch": {"type": "string",
                           "description": "restrict to one branch; defaults to "
                                          "every branch"},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "maximum rows, newest first "
                                         "(default 20)"},
            }),
            handler=_handle_log,
            description="Recent reviews, newest first, one line each: "
                        "`<when> | <branch> | <files> | <high>-<medium>-<low> | "
                        "<status> | <summary>`. A leading `!` marks a review "
                        "that is NOT trustworthy."),
        HandlerSpec(
            name="surface", long_running=False,
            input_schema=_schema({
                **_REPO_PROPERTY,
                "branch": {"type": "string",
                           "description": "branch to report on; defaults to the "
                                          "checked-out one"},
                "hook_format": {"type": "string", "enum": ["text", "claude"],
                                "description": "`text` for plain lines, "
                                               "`claude` for the SessionStart "
                                               "JSON envelope (default text)"},
                "include_delivered": {
                    "type": "boolean",
                    "description": "replay rounds that were already delivered "
                                   "too (default false)"},
            }),
            handler=_handle_surface,
            description="Report background review rounds nobody has been shown "
                        "yet, and record that you have now seen them. Silence is "
                        "never a verdict: a round that produced nothing usable "
                        "says so in words. This certifies NOTHING about the "
                        "current change -- only `gate` answers that."),
        HandlerSpec(
            name="triage_list", long_running=False,
            input_schema=_schema(_REVIEW_ID_PROPERTY, ("review_id",)),
            handler=_handle_triage_list,
            description="Every finding in one review with its EFFECTIVE triage "
                        "state (OPEN / DISMISSED / REOPENED), plus the refuter's "
                        "annotation where a refuter pass produced one. The "
                        "`[n]` index is what the triage tools take."),
        HandlerSpec(
            name="triage_dismiss", long_running=False,
            input_schema=_schema(
                {**_REVIEW_ID_PROPERTY, **_INDEX_PROPERTY, **_REASON_PROPERTY},
                ("review_id", "index", "reason")),
            handler=_handle_triage_dismiss,
            description="Dismiss ONE finding with an audited reason, which is "
                        "stored verbatim and read by whoever audits the ledger "
                        "later. A reason that says nothing specific about this "
                        "finding is REFUSED. There is no bulk form, on purpose. "
                        "This moves the gate, so it is a decision, not "
                        "bookkeeping."),
        HandlerSpec(
            name="adopt_refuter", long_running=False,
            input_schema=_schema(
                {**_REVIEW_ID_PROPERTY, **_INDEX_PROPERTY},
                ("review_id", "index")),
            handler=_handle_adopt_refuter,
            description="Dismiss ONE finding by adopting its refuter "
                        "annotation as the audited reason -- the refuter's own "
                        "words, not yours. Status 1 = REFUSED (the verdict was "
                        "not `refuted`, the reasoning is too thin to audit, or "
                        "no refuter pass stands behind the annotation); 2 = no "
                        "such review or finding."),
        HandlerSpec(
            name="triage_reopen", long_running=False,
            input_schema=_schema(
                {**_REVIEW_ID_PROPERTY, **_INDEX_PROPERTY, **_REASON_PROPERTY},
                ("review_id", "index", "reason")),
            handler=_handle_triage_reopen,
            description="Reopen ONE previously dismissed or deferred finding, "
                        "with an audited reason for overturning that decision. "
                        "It moves the gate from 0 back to 1, so the reason "
                        "clears the same audit floor a dismissal does. "
                        "Append-only: the decision it overturns stays in the "
                        "ledger."),
        HandlerSpec(
            name="triage_defer", long_running=False,
            input_schema=_schema(
                {**_REVIEW_ID_PROPERTY, **_INDEX_PROPERTY,
                 **_TRACKING_REF_PROPERTY, **_REASON_PROPERTY},
                ("review_id", "index", "tracking_ref", "reason")),
            handler=_handle_triage_defer,
            description="Defer ONE finding to a FILED tracking reference: the "
                        "finding is REAL, it is not blast-radius for this "
                        "change, and the work is filed as `tracking_ref`. This "
                        "is not `triage_dismiss` -- use it when the finding "
                        "stands but the fix belongs in other work, and file the "
                        "issue BEFORE calling. It clears the gate, so the "
                        "reference is mandatory and a deferral with none is "
                        "REFUSED. Status 1 = REFUSED (no usable reference, or a "
                        "reason that fails the audit floor); 2 = no such review "
                        "or finding. Like every triage tool, it carries out a "
                        "decision a human already made."),
        # Epic S1: observe + cancel. Appended (not reordered) so the existing
        # tool list snapshot only grows at the end — same discipline as
        # triage_defer.
        HandlerSpec(
            name="review_status", long_running=False,
            input_schema=_schema({
                **_REPO_PROPERTY,
                "review_id": {
                    "type": "string",
                    "description": "id of the review to inspect; when omitted, "
                                   "reports the current review for `repo` "
                                   "(newest running, else newest terminal)"},
            }),
            handler=_handle_review_status,
            description="Observe a review's lifecycle state without gating. "
                        "Reports one of queued|running|cancelled|failed|clean|"
                        "findings plus age, provider, and model when known. "
                        "Same words as `skodun review-status`. Not a second "
                        "gate — use `gate` for coverage of the current change."),
        HandlerSpec(
            name="review_cancel", long_running=False,
            input_schema=_schema(_REVIEW_ID_PROPERTY, ("review_id",)),
            handler=_handle_review_cancel,
            description="Cancel an in-flight review by id: sets the cancel "
                        "token when this process holds it, signals a confirmed "
                        "worker/FG process, and leaves a durable untrustworthy "
                        "terminal when the holder is gone. Same words as "
                        "`skodun review-cancel`. Refuses missing ids and "
                        "already-terminal rows."),
        # Non-gate feedback: agent/human judgment + product bugs. Appended
        # (not reordered) so the tool-list snapshot only grows at the end.
        HandlerSpec(
            name="feedback_add", long_running=False,
            input_schema=_schema({
                "kind": {
                    "type": "string",
                    "enum": ["finding_judgment", "review_quality",
                             "product_bug", "product_note"],
                    "description": "finding_judgment (needs review_id+index), "
                                   "review_quality (needs review_id), "
                                   "product_bug / product_note for skodun "
                                   "product feedback"},
                "body": {
                    "type": "string",
                    "description": "substantive note (≥20 chars); stored "
                                   "verbatim for later human inspection"},
                "actor": {
                    "type": "string",
                    "enum": ["agent", "human", "unknown"],
                    "description": "who wrote this (default agent)"},
                **_REVIEW_ID_PROPERTY,
                **_INDEX_PROPERTY,
                "provider": {
                    "type": "string",
                    "description": "optional provider id for filtering"},
                **_REPO_PROPERTY,
            }, ("kind", "body")),
            handler=_handle_feedback_add,
            description="Record non-gate feedback: agent judgment on a finding, "
                        "review quality, or a skodun product bug/note for "
                        "maintainers to inspect later. Does NOT clear the gate "
                        "— use triage_* only after a human decision. "
                        "Same store as `skodun feedback add`."),
        HandlerSpec(
            name="feedback_list", long_running=False,
            input_schema=_schema({
                "kind": {
                    "type": "string",
                    "enum": ["finding_judgment", "review_quality",
                             "product_bug", "product_note"],
                    "description": "restrict to one kind"},
                "review_id": {
                    "type": "string",
                    "description": "restrict to one review id"},
                "limit": {
                    "type": "integer", "minimum": 1,
                    "description": "maximum rows, newest first (default 50)"},
            }),
            handler=_handle_feedback_list,
            description="List non-gate feedback notes, newest first. Use to "
                        "inspect agent judgment and product_bug notes before "
                        "filing issues. Same words as `skodun feedback list`."),
    )


#: `review-now` and `gate-check`: STATIC text, on purpose. A prompt that
#: interpolated a repo path or a branch would be a second place those are
#: decided, and the tools already take them as arguments. Each one names the
#: tools in the order they should be used and states the one rule an agent most
#: needs to know about this product -- that a dismissal is a human's decision.
_REVIEW_NOW_TEXT = """\
Run a full skodun review of the outgoing change in this repository and report \
what it found.

1. Call the `review` tool. It takes minutes and spends model calls; do not call \
it twice, and do not call it again if it reports that a review is already in \
flight.
2. Read the verdict line it returns. `trustworthy=false` means the review does \
not cover this change and nothing may be concluded from it.
3. If it reports findings, call `triage_list` with the review id from the \
verdict line and summarise each finding for me: file, line, severity, title, \
and whether a refuter annotation disagrees with it.
4. Then STOP and wait for me. Do NOT dismiss, defer or adopt anything. Each of \
those moves the gate and each is my decision, recorded with my reason in an \
audit ledger; `triage_dismiss`, `triage_defer` and `adopt_refuter` carry out a \
decision I have already made, they are not ways to tidy up a report.

When I ask you what to do about the findings, judge each one by its \
CONSEQUENCE, never by its severity label -- labels are wrong in both \
directions. Fixing is for a finding that makes the change not work as \
described, falsifies a safety property the change or its docs promise, states \
something wrong to a user, corrupts data, or would need a migration to undo \
after merge. Everything else -- performance within bounds, style, naming, \
documentation drift, message precision where the outcome is already right -- \
is a candidate for `triage_defer`, which records that a finding is real and \
FILED under a tracking reference rather than rejected. The reference is \
mandatory; a deferral without one is refused.

Stop when the `gate` tool answers 0, which means clean OR every finding \
triaged. It does NOT mean the reviewer found nothing: for a real change that \
may never happen, because each round of fixes is new code the next round will \
review. Tell me to decide rather than starting another round when a round \
raises a must-fix finding in code the previous round's fix wrote, when fixing \
a finding would touch more code than the change under review, or when you \
think a finding is wrong -- escalate, do not iterate.

Install/ops (doctor, retain, schedule) are CLI-only: if review cannot start, \
ask the operator to run `skodun doctor` in a shell. Do not invent a second \
review system. Round/churn annotations on triage list and log are presentation \
only; the gate still keys on the full outgoing diff.
"""

_GATE_CHECK_TEXT = """\
Check whether a trustworthy review covers the current change in this \
repository, and tell me what to do about the answer.

1. Call the `gate` tool.
2. Report its status and its verdict line verbatim, then explain it:
   * 0 -- a trustworthy review covers exactly this content and no findings are \
open. Safe to push.
   * 1 -- findings remain open. List them with `triage_list` so I can decide; do \
not dismiss any of them yourself.
   * 2 -- NO trustworthy review covers this content. This is the fail-closed \
answer and it is the normal one after any edit: the gate keys on the content, \
not on the commit. Running the `review` tool is what fixes it.
3. If there are undelivered background rounds, `surface` reports them -- but \
nothing it reports certifies the current change. Only `gate` answers that.
"""


def default_prompts() -> tuple[PromptSpec, ...]:
    """The prompts `skodun mcp` serves, as `/mcp__skodun__<name>`."""
    return (
        PromptSpec(name="review-now",
                   description="Review the outgoing change now and report the "
                               "findings, without triaging any of them",
                   text=_REVIEW_NOW_TEXT),
        PromptSpec(name="gate-check",
                   description="Ask the gate whether a trustworthy review "
                               "covers this change, and explain the answer",
                   text=_GATE_CHECK_TEXT),
    )


def default_store_factory():
    """Open a Store at the one location the CLI resolves.

    Imported lazily and from `cli` on purpose: `_store_path` is the single
    definition of "where the store lives" (`SKODUN_DB`, else the XDG-ish
    default), and a second spelling of it here would be a second answer that
    starts disagreeing the first time one of them changes.
    """
    from .cli import _store_path
    from .store import Store
    return Store.open(_store_path())


def _handler_failure_text(tool_name: str, exc: BaseException) -> str:
    """Tool-visible failure text for a raised handler.

    Schema-behind (store written by a newer skodun than this MCP process) is
    the failure that most often causes agents to abandon MCP for the CLI —
    which is the wrong fix. Surface the remediation in the tool text itself.
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ValueError) and "newer than this skodun" in str(exc):
        return (
            f"skodun {tool_name}: MCP is schema-behind the store ({detail}). "
            "Restart this MCP server so it loads the same upgraded skodun as "
            f"`skodun --version` / doctor; do not fall back to the CLI for "
            f"{tool_name} while MCP stays on the old build."
        )
    return f"skodun {tool_name}: the tool failed: {exc!r}"


def _validate_result(res) -> str | None:
    """Why `res` is not a usable `HandlerResult`, or None.

    A handler is Task 14's code, and this transport is a fail-closed component's
    outermost layer: a handler that returns a dict, or a status that is a string,
    must become a tool-level error the agent can read rather than a
    `TypeError` traceback on stderr and a client waiting forever for a response
    that will never come.
    """
    if not isinstance(res, HandlerResult):
        return f"expected a HandlerResult, got {type(res).__name__}"
    if isinstance(res.status, bool) or not isinstance(res.status, int):
        return "status is not an int"
    if not isinstance(res.text, str):
        return "text is not a str"
    if not isinstance(res.pending_acks, (list, tuple)):
        return "pending_acks is not a list"
    if any(not isinstance(i, str) or not i for i in res.pending_acks):
        return "pending_acks holds something that is not a review id"
    if not isinstance(res.metadata, dict):
        return "metadata is not a dict"
    return None


def tool_result(res: HandlerResult) -> dict:
    """The MCP tool-result envelope for a `HandlerResult`.

    The CLI's own text in `content[0].text`, the CLI's own exit code in
    `structuredContent.status`, and `isError` = "that status is not 0". A
    refusal is the TOOL answering -- `isError` inside a successful JSON-RPC
    response -- never a JSON-RPC error, because an agent that got a protocol
    error would have no refusal text to read and nothing to do about it.
    """
    structured = {"status": int(res.status), **res.metadata}
    return {"content": [{"type": "text", "text": res.text}],
            "isError": res.status != 0,
            "structuredContent": structured}


class McpServer:
    """The read loop, the dispatch table, and the write lock."""

    def __init__(self, registry=(), *, prompts=(), store_factory=None,
                 stdin=None, stdout=None, stderr=None, acknowledge=None,
                 version: str = __version__, on_stdout_lost=None):
        self._specs: tuple[HandlerSpec, ...] = tuple(registry)
        self._registry: dict[str, HandlerSpec] = {}
        long_running: list[str] = []
        for spec in self._specs:
            if spec.name in self._registry:
                raise ValueError(
                    f"duplicate tool name {spec.name!r} in the MCP registry")
            self._registry[spec.name] = spec
            if spec.long_running:
                long_running.append(spec.name)
        if len(long_running) > 1:
            # Capacity 1 is a property of the design, not of the registry's
            # contents: a second long-running tool needs a second slot and a
            # queueing policy nobody has decided on.
            raise ValueError(
                f"at most one long-running tool may be registered; got "
                f"{long_running}")
        self._prompt_specs: tuple[PromptSpec, ...] = tuple(prompts)
        self._prompts: dict[str, PromptSpec] = {}
        for prompt in self._prompt_specs:
            if prompt.name in self._prompts:
                raise ValueError(f"duplicate prompt name {prompt.name!r}")
            self._prompts[prompt.name] = prompt

        self._store_factory = store_factory or default_store_factory
        if stdin is None:
            stdin = getattr(sys.stdin, "buffer", None)
        if stdout is None:
            stdout = getattr(sys.stdout, "buffer", None)
        if stdin is None or stdout is None:
            raise ValueError("the MCP server needs a binary stdin and stdout")
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._acknowledge = acknowledge
        self._version = version
        self._on_stdout_lost = on_stdout_lost

        #: Whether drift is still worth probing for. Cleared once the note has
        #: been said (it is a standing condition, and repeating it on every
        #: tool call is noise an operator learns to skip) and also when the
        #: probe reports there is no answer to be had -- see
        #: `_warn_if_code_moved`, which is where both cost and correctness of
        #: this live.
        self._watch_code_moved = True
        #: Consecutive "could not tell" probes still allowed. See
        #: `_DRIFT_UNREADABLE_TRIES`.
        self._drift_unreadable_left = _DRIFT_UNREADABLE_TRIES
        # Warmed on a daemon thread, never read synchronously here or in
        # `initialize`. Provenance costs two git calls, and while that is ~27ms
        # on a normal checkout the timeout exists because git can wedge -- a
        # client that times out its handshake has lost the session, and no
        # diagnostic field is worth that. By the time `initialize` arrives the
        # thread has all but certainly finished; if it has not, `serverInfo`
        # simply goes without the commit.
        try:
            from .provenance import warm_async

            warm_async()
        except Exception:       # pragma: no cover - never worth a failed start
            pass                # (KeyboardInterrupt deliberately not caught)

        #: Whether the `initialize` REQUEST has been answered. This -- not the
        #: notification below -- is what the -32002 gate reads: a client that
        #: pipelines `tools/list` behind `initialize` without waiting for the
        #: notification is asking a question this server can answer, and nothing
        #: it can ask before the handshake mutates anything.
        self._initialized = False
        #: Whether `notifications/initialized` arrived. Recorded, not enforced:
        #: it is the fact a stricter gate would be built on, and both observed
        #: clients send it immediately after the handshake.
        self._initialized_notified = False
        #: `clientInfo.name` from the handshake, kept only so an un-pinned
        #: review can guess the caller's model family for the cross-model
        #: preference (epic S5). Never used for anything a client could get
        #: wrong by lying: the worst a bad guess buys is a +20 tie-break.
        self._client_name: str | None = None
        self._stdout_lost = False
        #: Set by the SIGTERM forwarder when the signal found nothing to
        #: cancel; cleared by `_say_sigterm_did_nothing` once it has been
        #: explained. Written from a signal handler, so nothing may read it
        #: under a lock.
        self._sigterm_found_nothing = False
        #: stderr's raw fd when it has one, resolved when the SIGTERM
        #: forwarder is installed. See `_IDLE_SIGTERM_NOTE`.
        self._stderr_fd: int | None = None
        self._write_lock = threading.Lock()
        self._slot_lock = threading.Lock()
        #: Occupancy of the single review slot: True while a long-running
        #: HANDLER is executing. Deliberately not "a thread exists" -- see
        #: `_long_running_body`.
        self._review_active = False
        #: Every worker thread started, alive or not: `_shutdown` joins them.
        #: Liveness and occupancy are different questions.
        self._workers: list[threading.Thread] = []
        self._worker_cancel: threading.Event | None = None

        self._methods = {
            "initialize": self._m_initialize,
            "ping": self._m_ping,
            "tools/list": self._m_tools_list,
            "tools/call": self._m_tools_call,
            "prompts/list": self._m_prompts_list,
            "prompts/get": self._m_prompts_get,
        }

    # -- the loop ---------------------------------------------------------

    def serve(self) -> int:
        """Read until EOF (or until stdout dies) and return the exit code.

        Always 0. A stdio server has two endings and neither is a failure: the
        client closed stdin, or the client went away. A non-zero exit is how
        every MCP client harness reports "your server crashed", which is a
        different thing and must stay distinguishable.

        SIGTERM is installed HERE, on the main thread, deliberately: the
        long-running review runs on a worker thread where `signal.signal` cannot
        install a handler (`ValueError: signal only works in main thread of the
        main interpreter`). Cross-process `review-cancel` SIGTERMs this pid; if
        the default disposition stayed in force the process would die without
        setting the cancel token, orphaning the provider process group and a
        `running` row. The handler only sets the cancel token(s) — the review
        thread's own finally demotes the row, kills the provider group, and
        releases the FG lock.
        """
        previous_sigterm = self._install_sigterm_forwarder()
        try:
            try:
                self._read_loop()
            except BaseException as e:      # never a traceback out of a server
                self._note(f"the read loop stopped unexpectedly: {e!r}")
            return self._shutdown()
        finally:
            self._restore_sigterm_forwarder(previous_sigterm)

    def _install_sigterm_forwarder(self):
        """Main-thread SIGTERM → set the long-running cancel token(s).

        Returns the previous handler, or `_SIGTERM_INSTALL_FAILED` when
        install is impossible. Restored when `serve` returns so a process
        that reuses the interpreter does not keep a stale forwarder.

        The handler must not take ``_slot_lock``: it runs on the main thread
        between bytecodes, and that lock is held on the main thread in
        ``_start_long_running`` / ``_shutdown`` — a non-reentrant lock would
        deadlock cross-process cancel. A bare attribute read of
        ``_worker_cancel`` is enough (only ever rebound to an Event or None).
        ``pipeline`` is imported before install so the handler never blocks
        on the import lock.
        """
        import signal

        from . import pipeline

        # Resolved HERE, not in the handler: `fileno()` on a wrapped stream can
        # take that stream's lock, which is the one thing the handler must not
        # do. `None` means "no raw fd", and the deferred path covers it.
        try:
            fd = self._stderr.fileno()
            self._stderr_fd = fd if isinstance(fd, int) and fd >= 0 else None
        except BaseException:
            self._stderr_fd = None

        def handler(signum, frame):         # pragma: no cover - driven by signal
            cancelled = 0
            cancel = self._worker_cancel
            if cancel is not None:
                cancel.set()
                cancelled += 1
            try:
                cancelled += pipeline.request_cancel_all()
            except BaseException:
                pass
            if cancelled == 0:
                # Nothing to cancel, so from outside this signal did NOTHING.
                # Say so NOW, with a raw write to the stderr fd resolved
                # below: an idle server is by definition blocked in `readline`,
                # so anything deferred to the next message would not be seen in
                # the only situation this fires in.
                #
                # `os.write` and not `_note`: this runs on the main thread
                # between bytecodes, and a buffered stream's lock may be held
                # by the very frame the signal interrupted -- the same
                # non-reentrant deadlock the `_slot_lock` note above avoids.
                fd = self._stderr_fd
                if fd is not None:
                    try:
                        os.write(fd, _IDLE_SIGTERM_NOTE)
                        return
                    except BaseException:
                        pass        # fall through to the deferred path
                # No usable fd (a wrapped or in-memory stderr): leave it to the
                # read loop, which writes it the ordinary way.
                self._sigterm_found_nothing = True

        try:
            return signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError, RuntimeError):
            self._note("could not install the SIGTERM forwarder; cross-process "
                       "review-cancel of a review held by this server may kill "
                       "the process without demoting the row")
            return _SIGTERM_INSTALL_FAILED

    def _restore_sigterm_forwarder(self, previous) -> None:
        if previous is _SIGTERM_INSTALL_FAILED:
            return
        import signal
        try:
            # signal.signal returns None when the prior disposition was not a
            # Python handler; restore the platform default in that case.
            signal.signal(signal.SIGTERM,
                          signal.SIG_DFL if previous is None else previous)
        except (ValueError, OSError, RuntimeError):
            pass

    def _read_loop(self) -> None:
        # `_stdout_lost` is read without the write lock on purpose: it is only
        # ever set (never cleared), so a stale False costs at most one more line
        # of work, and taking the write lock on every iteration would serialise
        # the read loop behind the review thread's response.
        while not self._stdout_lost:
            self._say_sigterm_did_nothing()
            try:
                chunk = self._stdin.readline(MAX_LINE_BYTES + 1)
            except BaseException as e:
                self._note(f"stdin is no longer readable ({e!r}); ending the "
                           f"session")
                return
            if chunk == b"":
                return                      # EOF: the client is done
            if len(chunk) == MAX_LINE_BYTES + 1 and not chunk.endswith(b"\n"):
                # An oversized line: DRAIN the rest of it before answering, so
                # its tail is not read back as a sequence of fragments -- each
                # of which would draw its own -32700, and one of which could
                # parse as a message the client never sent.
                self._drain_oversized_line()
                self._note(f"discarded a line longer than the "
                           f"{MAX_LINE_BYTES}-byte cap")
                self._respond_error(
                    None, PARSE_ERROR,
                    f"parse error: the line exceeds the {MAX_LINE_BYTES}-byte "
                    f"limit")
                continue
            self._handle_line(chunk)

    def _say_sigterm_did_nothing(self) -> None:
        """Explain a SIGTERM that cancelled nothing, once per signal.

        SIGTERM here means "cancel the running review" -- that is how
        cross-process `review-cancel` reaches a review on a worker thread, and
        why the default disposition is replaced at all (see `serve`). With no
        review in flight it therefore does nothing, and USED TO SAY NOTHING:
        an operator saw a process that would not die and no reason why, whose
        natural next step is the `kill -9` the README warns against. Measured
        on 22 live servers, every one ignored SIGTERM (#113).

        THE FALLBACK PATH. The handler normally writes `_IDLE_SIGTERM_NOTE`
        straight to the stderr fd, because an idle server is blocked in
        `readline` and a note deferred to the next message would not appear in
        the only case this fires in. This covers a stderr with no raw fd -- an
        in-memory or wrapped stream -- where the deferral is the price of
        saying it at all.

        An operator at a shell never sees either version: stderr here is the
        host's log. The README is what answers them, and it says the same
        thing.
        """
        if not self._sigterm_found_nothing:
            return
        self._sigterm_found_nothing = False   # per signal, not per iteration
        self._note(
            "note: SIGTERM arrived with no review in flight, so nothing was "
            "cancelled and this server is still serving. On `skodun mcp` "
            "SIGTERM means \"cancel the running review\" -- it is how "
            "cross-process `skodun review-cancel` reaches one -- and never "
            "\"exit\". To stop this server: close its stdin (restart the MCP "
            "entry in your host), or send SIGINT.")

    def _drain_oversized_line(self) -> None:
        while True:
            try:
                more = self._stdin.readline(_DRAIN_CHUNK_BYTES)
            except BaseException:
                return
            if more == b"" or more.endswith(b"\n"):
                return                      # EOF, or the line finally ended

    def _handle_line(self, chunk: bytes) -> None:
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as e:
            self._note(f"a line was not valid UTF-8 ({e})")
            self._respond_error(None, PARSE_ERROR,
                                "parse error: the line is not valid UTF-8")
            return
        stripped = text.strip()
        if not stripped:
            # Framing, not a message. A writer that flushed a bare newline has
            # not asked anything, and -32700 per blank line would fill the
            # client's log with errors about nothing.
            return
        try:
            msg = json.loads(stripped)
        except ValueError as e:
            self._note(f"a line was not valid JSON ({e})")
            self._respond_error(None, PARSE_ERROR, f"parse error: {e}")
            return

        if isinstance(msg, list):
            # MCP removed JSON-RPC batching. An array is not a batch to unpack:
            # its members' responses would be lines no client is waiting for.
            self._respond_error(
                None, INVALID_REQUEST,
                "invalid request: JSON-RPC batches are not supported; send one "
                "message per line")
            return
        if not isinstance(msg, dict):
            self._respond_error(
                None, INVALID_REQUEST,
                "invalid request: a JSON-RPC message must be a JSON object")
            return

        method = msg.get("method")
        if "id" not in msg or msg["id"] is None:
            # No id -> a notification, whatever it names. An explicit
            # `id: null` lands here too: JSON-RPC reserves null for responses
            # whose id could not be determined, MCP forbids it on requests, and
            # answering with id null is exactly what a client cannot match to a
            # call.
            self._handle_notification(msg, method)
            return
        id_ = msg["id"]
        if isinstance(id_, bool) or not isinstance(id_, (str, int)):
            # `bool` first: it is an `int` subclass, and `id: true` is not an id.
            # An id we cannot echo is answered with null, per JSON-RPC.
            self._respond_error(None, INVALID_REQUEST,
                                "invalid request: id must be a string or an "
                                "integer")
            return
        if msg.get("jsonrpc") != "2.0":
            self._respond_error(id_, INVALID_REQUEST,
                                'invalid request: jsonrpc must be "2.0"')
            return
        if not isinstance(method, str) or not method:
            self._respond_error(id_, INVALID_REQUEST,
                                "invalid request: method must be a non-empty "
                                "string")
            return
        if method not in self._methods:
            # Named back on purpose: the client's log is where a version
            # mismatch or a typo in a config file gets diagnosed.
            self._respond_error(id_, METHOD_NOT_FOUND,
                                f"unknown method: {method}")
            return
        if not self._initialized and method not in PRE_INIT_METHODS:
            self._respond_error(id_, NOT_INITIALIZED, NOT_INITIALIZED_MESSAGE)
            return
        params = msg.get("params")
        if params is None:
            params = {}                     # a method that needs none
        if not isinstance(params, dict):
            self._respond_error(id_, INVALID_PARAMS,
                                "invalid params: params must be an object")
            return

        try:
            result = self._methods[method](params, id_)
        except _RpcError as e:
            self._respond_error(id_, e.code, e.message)
            return
        except BaseException as e:
            # A bug in this server, not in the request. The client still gets an
            # answer: a request with no response is a client that waits forever.
            self._note(f"{method} failed inside the server: {e!r}")
            self._respond_error(id_, INTERNAL_ERROR, f"internal error: {e!r}")
            return
        if result is _DEFERRED:
            return
        self._respond_result(id_, result)

    def _handle_notification(self, msg: dict, method) -> None:
        if method == "notifications/initialized" and msg.get("jsonrpc") == "2.0":
            self._initialized_notified = True
            return
        if isinstance(method, str) and method in self._methods:
            # A request-only method with no id: IGNORED WITHOUT EXECUTION.
            # There is no response line to carry its result, so running it
            # would mutate triage or delivery state with nobody told -- and, for
            # the long-running tool, would occupy the single review slot for a
            # call nobody is waiting on.
            self._note(f"ignored an id-less {method}: a request-only method "
                       f"sent as a notification has nowhere to send its answer")
            return
        # An unknown notification is ignored in silence, per the spec: a
        # notification never gets a response, not even an error one.

    # -- the methods ------------------------------------------------------

    def _m_initialize(self, params: dict, id_) -> dict:
        requested = params.get("protocolVersion")
        if requested is not None and not isinstance(requested, str):
            raise _RpcError(INVALID_PARAMS,
                            "invalid params: protocolVersion must be a string")
        negotiated = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                      else MCP_PROTOCOL_VERSION)
        # Stashed, not validated: `clientInfo` is optional in the spec and this
        # server needs nothing from it. A well-formed name becomes a default
        # model family for un-pinned reviews (epic S5); anything else leaves the
        # family undeclared, which is the availability-only scoring every client
        # gets by default.
        info = params.get("clientInfo")
        name = info.get("name") if isinstance(info, dict) else None
        self._client_name = name if isinstance(name, str) else None
        # Idempotent on purpose: a second handshake is answered rather than
        # refused. Nothing here is stateful enough for a re-handshake to
        # corrupt, and refusing one would invent a failure mode.
        self._initialized = True
        # schemaVersion is additive: agents/ops can detect CLI/MCP install skew
        # before a tool call hits the store. Clients that only read name/version
        # ignore the extra field. Imported lazily so cold `skodun mcp` still
        # avoids paying for sqlite until a tool needs the store.
        from .provenance import cached_provenance
        from .store import SCHEMA_VERSION
        # `commit` beside the version because on an editable install every
        # commit is still 0.4.0 -- so the version alone cannot tell an operator
        # whether THIS SERVER is running the code they just merged. It is what
        # `skodun doctor`'s package line asks them to compare against.
        #
        # BEST EFFORT: read from the cache the constructor started warming, and
        # never computed here. This is the handshake, and a client that times it
        # out has lost the session -- so on the rare cold read the field is
        # absent rather than paid for.
        info = {
            "name": SERVER_NAME,
            "version": self._version,
            "schemaVersion": SCHEMA_VERSION,
        }
        warm = cached_provenance()
        if warm is not None:
            info["commit"] = warm.get("skodun_commit")
        return {"protocolVersion": negotiated,
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": info}

    def _m_ping(self, params: dict, id_) -> dict:
        return {}

    def _m_tools_list(self, params: dict, id_) -> dict:
        # Registration order, not sorted: the registry is a curated list and its
        # order is a decision Task 14 makes. No `nextCursor`, so no conformant
        # client ever sends a `cursor`: the whole list is one page, and a
        # curated review-loop mirror is never going to need two.
        return {"tools": [{"name": s.name, "description": s.description,
                           "inputSchema": s.input_schema} for s in self._specs]}

    def _warn_if_code_moved(self) -> None:
        """Say once, on stderr, when the checkout has moved under this server.

        HERE and not in `doctor`, and the difference is the whole point. Every
        `doctor` run is a fresh process: it fills its provenance cache from
        disk and would then re-read the same disk, so the two sides always
        agree and the warning could never fire. Drift only exists inside a
        process that has been alive across a `git pull` -- which is what this
        server is, for hours or days at a time.

        Said once, but WATCHED until then. Nobody pulls between a client
        connecting and its first tool call -- they pull hours later, mid
        session, which is the case #110 is about. Latching "already handled"
        on the first probe regardless of what it found made the note reachable
        only for drift that predated the connection, so the real scenario was
        silently impossible. The flag is set when a note is EMITTED.

        Watching costs two git subprocesses per tool call, so it has two ways
        to stop. `DRIFT_UNCOMPARABLE` ends it outright -- a wheel install will
        never be a checkout. `DRIFT_UNREADABLE` is a probe that failed THIS
        time, so it says nothing and is asked again, but only
        `_DRIFT_UNREADABLE_TRIES` times: a `status --porcelain` that wedges on
        a network filesystem looks exactly like a transient `index.lock`, and
        retrying it forever would charge its whole timeout to every tool call
        for the rest of the session. A clean read resets the budget, because
        that is git working again rather than luck.

        Reported, never acted on. A fail-closed gate must not swap its own code
        underneath a running review, and this server cannot restart itself --
        the host owns the pipe -- so it says what a restart would get and
        leaves the decision where it belongs.
        """
        if not self._watch_code_moved:
            return
        try:
            from .provenance import (DRIFT_MOVED, DRIFT_SAME,
                                     DRIFT_UNCOMPARABLE, DRIFT_UNREADABLE,
                                     code_provenance, short,
                                     stale_against_disk)

            state, on_disk = stale_against_disk()
            if state == DRIFT_UNCOMPARABLE:
                self._watch_code_moved = False
            elif state == DRIFT_UNREADABLE:
                self._drift_unreadable_left -= 1
                self._watch_code_moved = self._drift_unreadable_left > 0
            elif state == DRIFT_SAME:
                self._drift_unreadable_left = _DRIFT_UNREADABLE_TRIES
            elif state == DRIFT_MOVED:
                self._watch_code_moved = False
                running = code_provenance().get("skodun_commit")
                self._note(
                    f"note: this server is running "
                    f"{short(running)}; the checkout has since "
                    f"moved to {short(on_disk)}. Reviews recorded now are "
                    f"stamped with the code above. Restart this MCP server to "
                    f"pick up the new one.")
        except Exception:       # pragma: no cover - a note is never worth a raise
            pass                # KeyboardInterrupt deliberately NOT caught:
                                # `_git` re-raises it so an operator's Ctrl-C
                                # is not absorbed, and swallowing it one frame
                                # up here would undo exactly that.

    def _m_tools_call(self, params: dict, id_):
        self._warn_if_code_moved()
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS,
                            "invalid params: name must name a tool")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}                  # a tool called without arguments
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS,
                            "invalid params: arguments must be an object")
        spec = self._registry.get(name)
        if spec is None:
            # A protocol error, not an `isError` result: no tool ran, so there
            # is no tool output to report.
            raise _RpcError(INVALID_PARAMS, f"invalid params: unknown tool: "
                                            f"{name}")
        if spec.long_running:
            if not self._start_long_running(spec, arguments, id_):
                self._respond_tool(id_, HandlerResult(
                    status=BUSY_STATUS, text=BUSY_TEXT, pending_acks=[]))
            return _DEFERRED
        self._respond_tool(id_, self._run_handler(spec, arguments,
                                                 threading.Event()))
        return _DEFERRED

    def _m_prompts_list(self, params: dict, id_) -> dict:
        return {"prompts": [{"name": p.name, "description": p.description}
                            for p in self._prompt_specs]}

    def _m_prompts_get(self, params: dict, id_) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS,
                            "invalid params: name must name a prompt")
        prompt = self._prompts.get(name)
        if prompt is None:
            raise _RpcError(INVALID_PARAMS,
                            f"invalid params: unknown prompt: {name}")
        # `arguments` is accepted and ignored: these prompts are static, and a
        # client that sends an empty argument object must not get a refusal.
        return {"description": prompt.description,
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": prompt.text}}]}

    # -- handlers ---------------------------------------------------------

    def _run_handler(self, spec: HandlerSpec, arguments: dict,
                     cancel: threading.Event) -> HandlerResult:
        call = HandlerCall(params=arguments, store_factory=self._store_factory,
                           cancel=cancel, client_name=self._client_name)
        try:
            res = spec.handler(call)
        except BaseException as e:
            # A handler that raised is a TOOL failure, reported fail-closed:
            # status 2, `isError` true, the exception named for the log. Not a
            # crashed server, and not a protocol error either.
            self._note(f"the {spec.name} tool raised: {e!r}")
            return HandlerResult(
                status=HANDLER_FAILURE_STATUS,
                text=_handler_failure_text(spec.name, e),
                pending_acks=[])
        problem = _validate_result(res)
        if problem is not None:
            self._note(f"the {spec.name} tool returned something unusable "
                       f"({problem})")
            return HandlerResult(
                status=HANDLER_FAILURE_STATUS,
                text=f"skodun {spec.name}: the tool returned something "
                     f"unusable ({problem})",
                pending_acks=[])
        return res

    def _start_long_running(self, spec: HandlerSpec, arguments: dict,
                            id_) -> bool:
        """Start the one long-running tool, or report that the slot is taken."""
        with self._slot_lock:
            if self._review_active:
                self._note(f"refused a second {spec.name} call: {BUSY_TEXT}")
                return False
            self._review_active = True
            cancel = threading.Event()
            worker = threading.Thread(
                target=self._long_running_body,
                args=(spec, arguments, id_, cancel),
                name=f"skodun-mcp-{spec.name}", daemon=True)
            # Finished threads are dropped here, so a session that runs reviews
            # all day does not accumulate a list of dead ones.
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(worker)
            self._worker_cancel = cancel
            worker.start()
            return True

    def _long_running_body(self, spec: HandlerSpec, arguments: dict, id_,
                           cancel: threading.Event) -> None:
        try:
            res = self._run_handler(spec, arguments, cancel)
        finally:
            # THE SLOT IS FREED WHEN THE HANDLER IS DONE, BEFORE THE RESPONSE IS
            # WRITTEN. A client that has just been told a review finished may
            # legitimately ask for the next one microseconds later, and refusing
            # it because this thread has not returned from its own `write` yet
            # would be a lie: nothing is in flight. Capacity 1 is about handlers
            # executing, not about threads existing -- which is also why
            # `_shutdown` joins from `_workers` rather than from this flag.
            with self._slot_lock:
                self._review_active = False
                # And the token goes with the slot. It is what the SIGTERM
                # forwarder reads to decide whether the signal had anything to
                # cancel, so a completed Event left in place would make every
                # later idle SIGTERM look like a real cancellation -- silently
                # restoring the #113 silence for every server except one that
                # has never run a review.
                #
                # `is`, not truthiness: the next review may already have stored
                # ITS token by the time this thread gets the lock, and clearing
                # that one would disarm a cancel for a review actually running.
                if self._worker_cancel is cancel:
                    self._worker_cancel = None
        try:
            self._respond_tool(id_, res)
        except BaseException as e:
            # `threading.excepthook` would print a traceback -- on stderr, so
            # not a protocol violation, but a lie about what happened: the
            # client would be left waiting for a response instead.
            self._note(f"the {spec.name} thread failed: {e!r}")
            self._respond_error(id_, INTERNAL_ERROR, f"internal error: {e!r}")

    # -- writing ----------------------------------------------------------

    def _respond_tool(self, id_, res: HandlerResult) -> bool:
        """Write a tool result, then -- and only then -- acknowledge deliveries.

        THE ORDER IS THE PRODUCT. Acknowledging first is the mutation that
        passes every other test: a report lost on the way out would be recorded
        as delivered and never shown again, which is the failure the delivery
        ledger exists to prevent, reintroduced by the fix. A crash between the
        flush and the acknowledgement re-delivers instead, which is the designed
        direction.
        """
        written = self._respond_result(id_, tool_result(res))
        if written and res.pending_acks:
            self._ack(list(res.pending_acks))
        return written

    def _respond_result(self, id_, result) -> bool:
        payload = self._encode({"jsonrpc": "2.0", "id": id_, "result": result})
        if payload is None:
            return self._respond_error(
                id_, INTERNAL_ERROR,
                "internal error: the result could not be serialised")
        return self._write(payload)

    def _respond_error(self, id_, code: int, message: str) -> bool:
        payload = self._encode({"jsonrpc": "2.0", "id": id_,
                                "error": {"code": code, "message": message}})
        if payload is None:                 # unreachable: code and message are
            payload = self._encode({        # an int and a str by construction
                "jsonrpc": "2.0", "id": None,
                "error": {"code": INTERNAL_ERROR, "message": "internal error"}})
        return self._write(payload) if payload is not None else False

    def _encode(self, obj) -> bytes | None:
        try:
            # `ensure_ascii=True` deliberately: it cannot fail on a lone
            # surrogate (which lossily-decoded provider output can carry), and
            # the result is pure ASCII, so the encode below cannot fail either.
            return json.dumps(obj, ensure_ascii=True,
                              separators=(",", ":")).encode("ascii") + b"\n"
        except BaseException as e:
            self._note(f"a response could not be serialised: {e!r}")
            return None

    def _write(self, payload: bytes) -> bool:
        """One write plus one flush, under one lock.

        The lock is the whole reason a long-running tool may answer from its own
        thread: two threads writing a line each without it can interleave into
        one line made of halves. The flush is inside it for the same reason a
        delivery is not acknowledged from a buffer -- bytes sitting in a buffer
        have not reached the client.
        """
        with self._write_lock:
            if self._stdout_lost:
                return False
            try:
                self._stdout.write(payload)
                self._stdout.flush()
                return True
            except BaseException as e:
                # The client is gone (broken pipe, closed fd) or the disk is
                # full. Nothing left to say and nobody to say it to: stop.
                self._stdout_lost = True
                self._note(f"stdout is no longer writable ({e!r}); ending the "
                           f"session")
                if self._on_stdout_lost is not None:
                    try:
                        self._on_stdout_lost()
                    except BaseException:
                        pass
                return False

    def _ack(self, ids: list[str]) -> None:
        try:
            if self._acknowledge is not None:
                self._acknowledge(ids)
            else:
                self._default_acknowledge(ids)
        except BaseException as e:
            # The report already reached the client, so a failed ledger write
            # costs a repeat, not a loss -- and must not become the failure.
            self._note(f"{len(ids)} delivered round(s) could not be "
                       f"acknowledged ({e!r}); they will be reported again")

    def _default_acknowledge(self, ids: list[str]) -> None:
        """Acknowledge with a FRESH Store: the handler's own closed with its
        call, and this runs after the response is already out."""
        from . import delivery
        with self._store_factory() as store:
            delivery.acknowledge(store, ids, MCP_CHANNEL)

    # -- shutdown ---------------------------------------------------------

    def _shutdown(self) -> int:
        """End the session: join the long-running worker, then return 0.

        Default disconnect policy is **drain** (see ``disconnect_policy`` /
        ``SKODUN_MCP_DISCONNECT``): do **not** set the cancel token on session
        end so an in-flight review can finish and write its store record. That
        is how an MCP restart or host reload avoids losing ongoing work.
        ``SKODUN_MCP_DISCONNECT=cancel`` restores the old cancel-then-join
        behaviour. Explicit ``review_cancel`` / SIGTERM still cancel while the
        process is alive.

        Under drain, the wait is bounded by ``drain_timeout_sec()`` (default
        2h, env ``SKODUN_MCP_DRAIN_TIMEOUT_SECONDS``). If the worker is still
        alive after that ceiling, cancel is set and we join again so a hung
        provider cannot pin the MCP process open forever. Under cancel policy,
        the cancellation token bounds the wait via the provider watchdog.

        Abandoning a worker mid-write without cancel would leave a `running`
        row and a provider process group with no parent — so we always join
        (after optional drain timeout → cancel), never detach.

        The token is the LAST review's. Only one review executes at a time, so
        any earlier thread still in `_workers` has already finished its handler
        and is only finishing a write -- there is nothing left in it to cancel.
        """
        import time

        with self._slot_lock:
            workers, cancel = list(self._workers), self._worker_cancel
        alive = [w for w in workers if w.is_alive()]
        policy = disconnect_policy()

        def _join_all(*, timeout: float | None, label: str) -> bool:
            """Join alive workers. ``timeout=None`` means unbounded.

            Returns True if any worker is still alive after the wait.
            """
            stuck = False
            for worker in workers:
                if not worker.is_alive():
                    continue
                self._note(f"waiting for {worker.name} to finish before exiting")
                if timeout is None:
                    worker.join()
                else:
                    worker.join(timeout=max(timeout, 0.0))
                if worker.is_alive():
                    stuck = True
                    if timeout is not None:
                        self._note(
                            f"{worker.name} still alive after {label}; "
                            f"continuing shutdown")
            return stuck

        def _demote_orphaned_running_rows() -> None:
            """Best-effort durable terminal for reviews this process still owns.

            Used only when we must exit with a daemon worker still stuck after
            cancel, so we do not leave forever-``running`` rows. Provider
            process groups may still need OS cleanup; stale recovery is the
            backstop for anything left behind.
            """
            import os
            try:
                from . import pipeline
                pipeline.request_cancel_all()
            except BaseException:
                pass
            try:
                with self._store_factory() as store:
                    mine = os.getpid()
                    reason = (
                        "cancelled: MCP process exiting with review worker "
                        "still stuck after cancel wait")
                    for rec in store.running_records():
                        pid = rec.get("pid")
                        rid = rec.get("id")
                        if rid is None:
                            continue
                        # Foreground MCP/CLI rows store os.getpid() of this
                        # process (pipeline). Require an exact match — never
                        # demote pid-less rows (other agents / mid-attach) or
                        # foreign pids (background workers, other MCP).
                        try:
                            if pid is None or int(pid) != mine:
                                continue
                        except (TypeError, ValueError):
                            continue
                        try:
                            if store.fail_if_running(str(rid), reason):
                                self._note(
                                    f"demoted stuck running review {rid} "
                                    f"before MCP exit")
                        except BaseException as e:
                            self._note(
                                f"could not demote stuck review {rid}: {e!r}")
            except BaseException as e:
                self._note(f"could not open store to demote stuck reviews: {e!r}")

        if not alive:
            return 0

        if policy == DISCONNECT_CANCEL:
            if cancel is not None:
                self._note(
                    "disconnect policy=cancel; cancelling in-flight review "
                    "before exit")
                cancel.set()
            if _join_all(timeout=post_cancel_join_sec(), label="cancel wait"):
                _demote_orphaned_running_rows()
            return 0

        # Drain: prefer finishing the review; optional ceiling then cancel.
        ceiling = drain_timeout_sec()
        if ceiling <= 0:
            self._note(
                "disconnect policy=drain; waiting for in-flight review with "
                "no drain ceiling (SKODUN_MCP_DRAIN_TIMEOUT_SECONDS=0)")
            _join_all(timeout=None, label="unlimited drain")
            return 0

        self._note(
            f"disconnect policy=drain; waiting up to {ceiling:g}s for "
            f"in-flight review (then cancel if still running; set "
            f"SKODUN_MCP_DISCONNECT=cancel to abort immediately)")
        deadline = time.monotonic() + ceiling
        for worker in workers:
            if not worker.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._note(f"waiting for {worker.name} to finish before exiting")
            worker.join(timeout=remaining)
        still = [w for w in workers if w.is_alive()]
        if still and cancel is not None:
            self._note(
                "drain timed out; cancelling in-flight review so the "
                "MCP process can exit")
            cancel.set()
            if _join_all(
                    timeout=post_cancel_join_sec(), label="post-cancel wait"):
                _demote_orphaned_running_rows()
        return 0

    # -- diagnostics ------------------------------------------------------

    def _note(self, message: str) -> None:
        """Say something on STDERR. Never stdout, never raises.

        Stdout is the protocol channel: one diagnostic written there
        desynchronises the client's parser for the rest of the session.
        """
        try:
            print(f"skodun mcp: {message}", file=self._stderr, flush=True)
        except BaseException:
            pass                # a diagnostic may never become the failure


def serve_stdio(registry=None, *, prompts=None, store_factory=None,
                on_stdout_lost=None) -> int:
    """Serve MCP on this process's stdin/stdout. Returns the exit code (0).

    The binary buffers, not the text layers: the protocol is UTF-8 bytes with a
    hard line cap, and a text wrapper would add its own encoding and newline
    translation between this module and the pipe.
    """
    server = McpServer(
        registry=default_registry() if registry is None else registry,
        prompts=default_prompts() if prompts is None else prompts,
        store_factory=store_factory,
        stdin=getattr(sys.stdin, "buffer", None),
        stdout=getattr(sys.stdout, "buffer", None),
        stderr=sys.stderr,
        on_stdout_lost=on_stdout_lost)
    return server.serve()
