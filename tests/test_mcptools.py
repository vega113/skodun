"""The MCP tools: a curated mirror of the CLI, and the parity that makes it one.

Task 13 built the transport and left the registry empty. This module is about
what went into it, and almost every test here is a comparison rather than an
assertion about a value: the tools are four lines each over the same `services`
functions the CLI subcommands call, so the interesting question is never "does
the tool work" but "does it answer EXACTLY what the CLI answers".

Three properties are worth more than the rest and each has its own section:

  * **The list is curated.** A snapshot pins the tool names and their order.
    Another tool is a new agent-facing surface on a fail-closed gate, and it
    should cost a failing test to add -- `triage_defer` (issue #5) cost exactly
    that, deliberately.
  * **Refusals are word-for-word identical across surfaces.** A human reading an
    agent's transcript, or the other way round, must not be looking at two
    products. Every refusal below is produced twice -- once through
    `cli.main(argv)` and once through the tool handler -- and compared as
    STRINGS.
  * **A delivery is acknowledged only after the response really left.** The
    transport's rule from Task 13, now with a real `surface` behind it: a flush
    that raises leaves the rounds undelivered, and a completed buffer write is
    never "delivered".

The end-to-end disconnect drills live here too, because the mechanism only
exists once both ends are real: a `skodun mcp` SUBPROCESS, a fake provider, and
a closed stdin. Default disconnect is **drain** (review finishes); cancel-on-
disconnect is opt-in via ``SKODUN_MCP_DISCONNECT=cancel``.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import skodun
from skodun import delivery, mcpserver, services
from skodun.cli import main
from skodun.mcpserver import HandlerCall, HandlerResult, McpServer
from skodun.store import Store
from tests.test_cli import (_annotation, _artifact, _delivery_rows, _finding,
                            _loud_round, _round, _surface_db)
from tests.test_gitio import _git, _mkrepo

_SRC = str(Path(skodun.__file__).resolve().parents[1])

#: The tools `skodun mcp` serves, in `tools/list` order. THE SNAPSHOT.
#: `triage_defer` is APPENDED rather than slotted beside `triage_dismiss`: the
#: order is the order tools were added, a client's tool picker renders it, and
#: reordering the shipped eight would move every one of them for no reason.
EXPECTED_TOOLS = ["gate", "review_readiness", "review", "log", "surface", "triage_list",
                  "triage_dismiss", "adopt_refuter", "triage_reopen",
                  "triage_defer", "review_status", "review_cancel",
                  "feedback_add", "feedback_list"]

TRACKING_REF = "GH-412"
DEFER_REASON = "in-bounds for this surface; the hot path is the batcher upstream"

EXPECTED_PROMPTS = ["review-now", "gate-check"]

GOOD_REASON = "the guard at line 12 already rejects a None handler before this"
#: `test_cli.py`'s own thin-reasoning fixture, so both surfaces are refused
#: for the SAME reason rather than for two different ones.
THIN = "nope."


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    """`SKODUN_DB` inside `tmp_path`, `SKODUN_CONFIG` at a path that does not
    exist: nothing here may reach the developer's own store or provider config."""
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))
    # The provider binaries too, and not as belt-and-braces: `adapters.grok`
    # prefers `~/.grok/bin/grok` over PATH, so on any machine that has grok
    # installed a PATH-only fake would silently lose and a test would run the
    # real CLI. Nothing here should reach a provider at all; pinning them at a
    # path that does not exist is what makes that a failure rather than a bill.
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "no-bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "no-bin" / "codex"))


# --------------------------------------------------------------------------
# helpers: one call through each surface
# --------------------------------------------------------------------------

def _specs() -> dict:
    return {s.name: s for s in mcpserver.default_registry()}


def _tool(name: str, db: Path, **params) -> HandlerResult:
    """Call one tool through the registry seam, exactly as the server does.

    A fresh Store per call, from a factory, because that is the contract the
    transport imposes: sqlite connections are thread-bound and the review tool
    answers from another thread.
    """
    spec = _specs()[name]
    return spec.handler(HandlerCall(params=params,
                                    store_factory=lambda: Store.open(db),
                                    cancel=threading.Event()))


def _cli(argv: list[str], capsys) -> tuple[int, str]:
    """`(code, stdout-as-the-tool-would-return-it)`.

    `_emit` adds the trailing newline a terminal wants and the tool text does
    not carry, so it is stripped here -- that is the ONLY difference the two
    surfaces are allowed to have, and stripping it is what lets everything else
    be compared as an exact string.
    """
    capsys.readouterr()
    code = main(argv)
    return code, capsys.readouterr().out.rstrip("\n")


def _seeded(tmp_path: Path, *findings, review_id="rev1", **extra) -> Path:
    db = tmp_path / "parity.db"
    with Store.open(db) as st:
        st.save_review(_artifact(list(findings) or [_finding()],
                                 review_id=review_id, **extra))
    return db


def _lkey(file="a0.py", title="NPE 0", branch="feat", base_sha="s" * 40) -> str:
    """The triage ledger's key for one finding. `triage_for` is keyed by FINDING
    key and `triage_history` by LEDGER key; using the wrong one reads as "nothing
    was ever recorded"."""
    from skodun.textnorm import finding_key, ledger_key
    return ledger_key(branch, base_sha, finding_key(file, title))


def _both(db: Path, argv: list[str], tool: str, capsys,
          **params) -> tuple[tuple[int, str], tuple[int, str]]:
    """Run one refusal through both surfaces against the SAME store."""
    cli = _cli(argv, capsys)
    res = _tool(tool, db, **params)
    return cli, (res.status, res.text)


# ==========================================================================
# the curated list
# ==========================================================================

def test_the_tool_list_is_exactly_the_review_loop_and_nothing_more():
    """THE SNAPSHOT. Another tool must cost a failing test.

    Every name here is a `skodun` subcommand's service. What is NOT here is the
    point of the test: no bulk dismissal (a dismissal is a human naming ONE
    finding), no `dispatch`/`worker`/`install-hooks`/`import-legacy`/
    `shadow-compare`/`providers` (machinery and diagnostics, not review-loop
    steps), and no tool that writes configuration or takes a store path.
    """
    assert [s.name for s in mcpserver.default_registry()] == EXPECTED_TOOLS


def test_there_is_no_bulk_tool_of_any_kind():
    """Named separately from the snapshot because it is the rule the snapshot
    exists to protect, and a reader deleting the snapshot should still trip."""
    for spec in mcpserver.default_registry():
        assert not re.search(r"all|bulk|many|batch", spec.name), spec.name
        # No tool takes a LIST of anything either: that is the other shape a
        # bulk dismissal arrives in.
        for prop in spec.input_schema["properties"].values():
            assert prop.get("type") != "array", (spec.name, prop)


def test_exactly_one_tool_is_long_running_and_it_is_review():
    """Capacity 1 is a property of the design (Task 13), and `review` is the tool
    that holds the foreground lock and spends minutes of model time."""
    long_running = [s.name for s in mcpserver.default_registry() if s.long_running]
    assert long_running == ["review"]


def test_every_tool_carries_an_explicit_closed_schema_and_a_description():
    """An `inputSchema` is what a client validates against and what an agent
    reads. `additionalProperties: False` is load-bearing: a misspelled
    `review_id` would otherwise be a well-formed call this server answers
    "no such review: None" to, and the agent would go hunting for the review
    instead of for its own typo."""
    for spec in mcpserver.default_registry():
        schema = spec.input_schema
        assert schema["type"] == "object", spec.name
        assert schema["additionalProperties"] is False, spec.name
        assert isinstance(schema["required"], list), spec.name
        assert set(schema["required"]) <= set(schema["properties"]), spec.name
        for name, prop in schema["properties"].items():
            assert "type" in prop, (spec.name, name)
            assert prop.get("description"), (spec.name, name)
        assert len(spec.description) > 40, spec.name


def test_the_required_arguments_are_the_ones_without_a_default():
    """A triage tool with an optional `review_id` would be a tool that dismisses
    "whatever review it can find"."""
    required = {s.name: set(s.input_schema["required"])
                for s in mcpserver.default_registry()}
    assert required["gate"] == set()            # `repo` defaults to the cwd
    assert required["review"] == set()
    assert required["log"] == set()
    assert required["surface"] == set()
    assert required["triage_list"] == {"review_id"}
    assert required["triage_dismiss"] == {"review_id", "index", "reason"}
    assert required["adopt_refuter"] == {"review_id", "index"}
    assert required["triage_reopen"] == {"review_id", "index", "reason"}
    # The tracking reference is REQUIRED, and that is the whole verb: a
    # `triage_defer` an agent could call without one would be `triage_dismiss`
    # with a friendlier name and no audit trail of what was actually filed.
    assert required["triage_defer"] == {"review_id", "index", "tracking_ref",
                                        "reason"}


def test_the_prompts_are_the_two_static_ones():
    prompts = mcpserver.default_prompts()
    assert [p.name for p in prompts] == EXPECTED_PROMPTS
    for p in prompts:
        assert p.description and p.text.strip()
        # STATIC: a prompt that interpolated a repo or a branch would be a second
        # place those are decided, and the tools already take them as arguments.
        assert "{" not in p.text and "%s" not in p.text


def test_the_review_now_prompt_tells_the_agent_not_to_triage_anything():
    """The one rule an agent most needs to know about this product: a dismissal
    moves the gate and it is a human's decision, recorded with their reason."""
    text = {p.name: p.text for p in mcpserver.default_prompts()}["review-now"]
    assert "Do NOT dismiss" in text
    assert "triage_list" in text


def test_the_review_now_prompt_carries_the_stopping_rule(tmp_path):
    """An agent that can review will keep fixing and re-reviewing until the
    reviewer goes quiet, and that does not converge: every round of fixes is
    new code for the next round to find fault with. Measured on skodun's own
    Phase 3 branch, a second round repeated NONE of the first round's eleven
    findings and put four of its six new ones in code the fix commit had just
    written.

    So the prompt has to answer the question it provokes -- when do I stop? --
    at the moment it provokes it. Three things have to be in there: the
    terminating condition (the GATE, not an empty finding list), the basis for
    triage (consequence, not the severity label), and the escalation trigger
    that the measurement above is about."""
    text = {p.name: p.text for p in mcpserver.default_prompts()}["review-now"]
    # The terminating condition is the gate, not "no findings".
    assert "gate" in text.lower()
    # Triage is by consequence; severity labels are not the criterion.
    assert "severity" in text.lower() and "consequence" in text.lower()
    # The verb for "real, but filed" exists now, and the prompt must name it
    # rather than leaving an agent to overload a dismissal.
    assert "triage_defer" in text
    # The escalation trigger, in the agent's own words rather than a doc link.
    assert "escalat" in text.lower()


def test_the_tool_list_reaches_a_client_over_the_wire(tmp_path):
    """The snapshot again, through a real `skodun mcp` process, because
    `default_registry()` being right is not the same as it being SERVED."""
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
        b'"params":{"protocolVersion":"2025-11-25"}}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":3,"method":"prompts/list"}\n')
    p = subprocess.run([sys.executable, "-m", "skodun", "mcp"], input=payload,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=_env(tmp_path), timeout=120)
    assert p.returncode == 0, p.stderr
    lines = [json.loads(x) for x in p.stdout.decode().splitlines()]
    tools = lines[1]["result"]["tools"]
    assert [t["name"] for t in tools] == EXPECTED_TOOLS
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"
    assert [x["name"] for x in lines[2]["result"]["prompts"]] == EXPECTED_PROMPTS


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["SKODUN_DB"] = str(tmp_path / "mcp.db")
    env["SKODUN_CONFIG"] = str(tmp_path / "absent-config.toml")
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


# ==========================================================================
# one definition: the tools and the CLI call the same code
# ==========================================================================

def test_every_tool_handler_goes_through_the_services_module():
    """The named mutation is "a divergent copy of `svc_gate` in `cli.py`".

    Neither transport may contain review-loop logic. This is checked at the
    SOURCE level because that is where a copy would appear: `cli.py` must not
    import `gate`, `triage` or `delivery.surface` any more, and `mcpserver.py`
    must not either -- both reach them only through `services`.
    """
    cli_src = (Path(skodun.__file__).parent / "cli.py").read_text(encoding="utf-8")
    mcp_src = (Path(skodun.__file__).parent
               / "mcpserver.py").read_text(encoding="utf-8")
    # The DECISION functions, matched as BARE calls: `(?<![\w.])` is what lets
    # `svc_adopt_refuter(...)` through while `adopt_refuter(...)` fails, which is
    # exactly the distinction under test -- a transport may ROUTE to a decision,
    # never make one. (`cli._cmd_providers` still imports `triage.shown_field`, a
    # RENDERER for a diagnostic listing that is no part of the review loop, so the
    # rule names the decisions rather than the module.)
    decisions = ("run_gate", "run_review", "load_valid_artifact", "adopt_refuter",
                 "dismiss", "reopen", "defer", "triage_state",
                 "validate_tracking_ref")
    for name in decisions:
        pattern = rf"(?<![\w.]){name}\s*\("
        assert not re.search(pattern, cli_src), f"cli.py calls {name}()"
        assert not re.search(pattern, mcp_src), f"mcpserver.py calls {name}()"
    assert "from .gate import" not in cli_src and "from .gate import" not in mcp_src
    for surface_src in (cli_src, mcp_src):
        assert "services" in surface_src
    # `delivery.surface` -- the renderer -- is reached through `svc_surface`; the
    # CLI still calls `delivery.acknowledge`, which is the TRANSPORT's own job.
    assert "delivery.surface(" not in cli_src
    assert "delivery.surface(" not in mcp_src


def test_the_gate_tool_and_the_gate_command_answer_identically(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """The seam nothing else spans: two transports, one store, one decision.

    A gate is the only thing in this product that a push is allowed to depend on,
    so "the agent's gate" and "the human's gate" being the same gate is not a
    nicety.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    db = tmp_path / "gate.db"
    monkeypatch.setenv("SKODUN_DB", str(db))

    cli_code, cli_text = _cli(["gate", "--repo", str(repo)], capsys)
    res = _tool("gate", db, repo=str(repo))

    assert (res.status, res.text) == (cli_code, cli_text)
    assert cli_code == 2, "an unreviewed change must fail closed on both surfaces"
    assert "SKODUN GATE: FAIL(2)" in cli_text


def test_the_log_tool_and_the_log_command_render_the_same_lines(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli_code, cli_text = _cli(["log"], capsys)
    res = _tool("log", db)
    assert (res.status, res.text) == (cli_code, cli_text)
    assert "feat" in cli_text


def test_the_triage_list_tool_and_the_command_render_the_same_listing(
        tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path, _finding(0, _annotation()), _finding(1))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli_code, cli_text = _cli(["triage", "--list", "rev1"], capsys)
    res = _tool("triage_list", db, review_id="rev1")
    assert (res.status, res.text) == (cli_code, cli_text)
    assert "[0]" in cli_text and "refuter(" in cli_text


def test_an_empty_listing_is_words_on_the_tool_surface_and_silence_on_the_cli(
        tmp_path, monkeypatch, capsys):
    """The ONE place the two surfaces deliberately differ, and why.

    `skodun log` with an empty store prints nothing: a blank line is not an empty
    listing, and a shell script counting lines must get zero. An agent handed an
    empty tool result cannot tell "no reviews" from "the tool broke", so the tool
    says so in words. Both exit 0 -- the DECISION is identical, only the
    presentation of "nothing" differs.
    """
    db = tmp_path / "empty.db"
    Store.open(db).close()
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli_code, cli_text = _cli(["log"], capsys)
    res = _tool("log", db)
    assert cli_code == 0 and cli_text == ""
    assert res.status == 0 and "no reviews" in res.text


# ==========================================================================
# refusal parity, word for word
# ==========================================================================

@pytest.mark.parametrize("reason", ["fp", "false positive", "wontfix", "nope"])
def test_a_placeholder_reason_is_refused_with_the_same_words_on_both_surfaces(
        tmp_path, monkeypatch, capsys, reason):
    """The audit floor is the product: a dismissal whose reason says nothing is a
    silent dismissal with a receipt. An agent must not be able to get past it by
    asking a different door."""
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "rev1", "0", reason], "triage_dismiss",
                      capsys, review_id="rev1", index=0, reason=reason)
    assert cli == tool, (cli, tool)
    assert cli[0] == 2 and "rejected" in cli[1]
    with Store.open(db) as st:
        assert st.triage_for("feat", "s" * 40) == {}, "a refusal recorded something"


@pytest.mark.parametrize("verdict", ["confirmed", "uncertain"])
def test_adopting_a_non_refuted_verdict_is_refused_identically(tmp_path,
                                                               monkeypatch,
                                                               capsys, verdict):
    db = _seeded(tmp_path, _finding(0, _annotation(verdict=verdict)))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--adopt-refuter", "rev1", "0"],
                      "adopt_refuter", capsys, review_id="rev1", index=0)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1, "a refusal about the ledger is a 1, not a 2"
    assert verdict in cli[1]


def test_adopting_a_thin_reasoning_is_refused_identically(tmp_path, monkeypatch,
                                                          capsys):
    db = _seeded(tmp_path, _finding(0, _annotation(reasoning=THIN, thin_reasoning=True)))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--adopt-refuter", "rev1", "0"],
                      "adopt_refuter", capsys, review_id="rev1", index=0)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1


def test_an_annotation_no_refuter_pass_stands_behind_is_refused_identically(
        tmp_path, monkeypatch, capsys):
    """The forged-annotation path: a FINDER can write a `refuter` key into its own
    finding. Both surfaces refuse to act on it, with the same words."""
    db = _seeded(tmp_path, _finding(0, _annotation()), extra_passes={})
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--adopt-refuter", "rev1", "0"],
                      "adopt_refuter", capsys, review_id="rev1", index=0)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1 and "no refuter pass ran" in cli[1]


@pytest.mark.parametrize("index", [1, 99])
def test_an_out_of_range_index_is_refused_identically(tmp_path, monkeypatch,
                                                      capsys, index):
    db = _seeded(tmp_path, _finding(0, _annotation()))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--adopt-refuter", "rev1", str(index)],
                      "adopt_refuter", capsys, review_id="rev1", index=index)
    assert cli == tool, (cli, tool)
    assert cli[0] == 2, "no such finding: the command never had an opinion"


def test_an_unknown_review_is_refused_identically(tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--list", "nope"], "triage_list", capsys,
                      review_id="nope")
    assert cli == tool, (cli, tool)
    assert cli[0] == 2 and "no such review" in cli[1]


def test_an_unauditable_reopen_reason_is_refused_identically(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    """A reopen moves the gate from 0 back to 1, so it clears the same floor."""
    from skodun.triage import dismiss

    db = _seeded(tmp_path, _finding(0))
    with Store.open(db) as st:
        dismiss(st, st.get_review("rev1"), 0, GOOD_REASON,
                now="2026-07-27T10:00:00Z")
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--reopen", "rev1", "0", "fp"],
                      "triage_reopen", capsys, review_id="rev1", index=0,
                      reason="fp")
    assert cli == tool, (cli, tool)
    assert cli[0] == 1


def test_reopening_a_finding_that_is_not_dismissed_is_refused_identically(
        tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--reopen", "rev1", "0", GOOD_REASON],
                      "triage_reopen", capsys, review_id="rev1", index=0,
                      reason=GOOD_REASON)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1


def test_an_unfiled_deferral_is_refused_with_the_same_words_on_both_surfaces(
        tmp_path, monkeypatch, capsys):
    """THE refusal issue #5 exists for, and the one an agent is most likely to
    hit: a deferral clears the gate, so one that names nowhere the work is filed
    is an auto-dismissal with better manners. An agent must not be able to get
    past it by asking a different door."""
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--defer", "rev1", "0", "", DEFER_REASON],
                      "triage_defer", capsys, review_id="rev1", index=0,
                      tracking_ref="", reason=DEFER_REASON)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1, "a refusal about the ledger is a 1, not a 2"
    with Store.open(db) as st:
        assert st.triage_for("feat", "s" * 40) == {}, "a refusal recorded something"


@pytest.mark.parametrize("ref", ["I will file it later", "GH 412", "#"])
def test_a_reference_nobody_can_look_up_is_refused_identically(
        tmp_path, monkeypatch, capsys, ref):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--defer", "rev1", "0", ref, DEFER_REASON],
                      "triage_defer", capsys, review_id="rev1", index=0,
                      tracking_ref=ref, reason=DEFER_REASON)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1


@pytest.mark.parametrize("reason", ["fp", "false positive", "wontfix"])
def test_a_placeholder_defer_reason_is_refused_identically(tmp_path, monkeypatch,
                                                           capsys, reason):
    """A filed reference buys no way past the reason floor: "filed as GH-412,
    wontfix" is a dismissal wearing a ticket number, through either door."""
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--defer", "rev1", "0", TRACKING_REF, reason],
                      "triage_defer", capsys, review_id="rev1", index=0,
                      tracking_ref=TRACKING_REF, reason=reason)
    assert cli == tool, (cli, tool)
    assert cli[0] == 1


def test_an_out_of_range_defer_index_is_refused_identically(tmp_path, monkeypatch,
                                                            capsys):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["triage", "--defer", "rev1", "99", TRACKING_REF,
                           DEFER_REASON],
                      "triage_defer", capsys, review_id="rev1", index=99,
                      tracking_ref=TRACKING_REF, reason=DEFER_REASON)
    assert cli == tool, (cli, tool)
    assert cli[0] == 2, "no such finding: the command never had an opinion"


def test_a_missing_tracking_ref_is_refused_with_the_services_usage_string(
        tmp_path, monkeypatch, capsys):
    """The two surfaces reach this by different roads -- argparse's missing
    positional for the CLI, the handler's absent argument for the tool -- and
    land on the SAME string, because it lives in `services`.

    ABSENT is a 2 and not the 1 an unusable reference gets: a caller who has not
    supplied a reference has not made a deferral yet, which is misuse rather
    than a declined decision.
    """
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli_code, cli_text = _cli(["triage", "--defer", "rev1", "0"], capsys)
    res = _tool("triage_defer", db, review_id="rev1", index=0,
                reason=DEFER_REASON)
    assert cli_code == 2 and cli_text == services.TRIAGE_DEFER_USAGE
    assert res.status == 2 and res.text == services.TRIAGE_DEFER_USAGE, res.text


def test_a_missing_index_is_refused_with_the_services_usage_string(tmp_path,
                                                                  monkeypatch,
                                                                  capsys):
    """The two surfaces reach this refusal by different roads -- argparse for the
    CLI, the handler's own check for the tool -- and land on the SAME string,
    because it lives in `services` and neither of them owns it."""
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli_code, cli_text = _cli(["triage", "--reopen", "rev1"], capsys)
    res = _tool("triage_reopen", db, review_id="rev1", index=0)
    assert cli_code == 2 and cli_text == services.TRIAGE_REOPEN_USAGE
    assert res.status == 2 and res.text == services.TRIAGE_REOPEN_USAGE, res.text
    # An ABSENT reason is the service's refusal, not the handler's: that is what
    # keeps the two surfaces on the same string. A reason that is present but of
    # the wrong type is the handler's, and argparse cannot produce that shape.
    dismiss = _tool("triage_dismiss", db, review_id="rev1", index=0)
    assert dismiss.text == services.TRIAGE_DISMISS_USAGE, dismiss.text


def test_a_tool_argument_of_the_wrong_type_is_a_message_never_a_traceback(
        tmp_path):
    """`inputSchema` is advisory -- this server does not validate against it -- so
    every handler checks its own arguments. A `TypeError` here would be a client
    waiting forever for a response that became a stderr traceback."""
    db = _seeded(tmp_path, _finding(0))
    for name, params in [
            ("triage_list", {"review_id": 7}),
            ("triage_list", {}),
            ("adopt_refuter", {"review_id": "rev1", "index": "zero"}),
            ("adopt_refuter", {"review_id": "rev1", "index": True}),
            ("triage_dismiss", {"review_id": "rev1", "index": None,
                                "reason": GOOD_REASON}),
            ("log", {"limit": "lots"}),
            ("surface", {"hook_format": "yaml"}),
            # `bool(x)` is not validation: it says True for `"false"`, for
            # `"no"`, for `0.1` and for any non-empty container, so a client
            # sending the STRING "false" -- the single most likely way to get
            # this wrong over JSON-RPC -- would silently get the opposite of
            # what it asked for, and replay rounds the ledger already delivered.
            ("surface", {"include_delivered": "false"}),
            ("surface", {"include_delivered": "true"}),
            ("surface", {"include_delivered": 1}),
            ("surface", {"include_delivered": []}),
            # Found by probing, not by inspection: a LIST reason reached
            # `store.record_triage_event` and came back as
            # `sqlite3.ProgrammingError: Error binding parameter 11`, which the
            # transport would hand the agent as its tool text.
            ("triage_dismiss", {"review_id": "rev1", "index": 0,
                                "reason": ["a", "b"]}),
            ("triage_reopen", {"review_id": "rev1", "index": 0, "reason": 7}),
            ("triage_defer", {"review_id": "rev1", "index": 0,
                              "tracking_ref": ["GH-1"], "reason": DEFER_REASON}),
            ("triage_defer", {"review_id": "rev1", "index": 0,
                              "tracking_ref": 412, "reason": DEFER_REASON}),
            ("triage_defer", {"review_id": "rev1", "index": "zero",
                              "tracking_ref": TRACKING_REF,
                              "reason": DEFER_REASON}),
    ]:
        res = _tool(name, db, **params)
        assert res.status == 2, (name, params, res)
        assert res.text and "Traceback" not in res.text, (name, res.text)
        assert "Error binding" not in res.text, (name, res.text)
    # ...and nothing any of them touched was recorded.
    with Store.open(db) as st:
        assert st.triage_for("feat", "s" * 40) == {}


def test_a_non_positive_log_limit_is_refused_identically(tmp_path, monkeypatch,
                                                        capsys):
    db = _seeded(tmp_path, _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(db))
    cli, tool = _both(db, ["log", "-n", "0"], "log", capsys, limit=0)
    assert cli == tool, (cli, tool)
    assert cli[0] == 2 and "positive" in cli[1]


# ==========================================================================
# choosing the reviewer for one review (issue #16)
# ==========================================================================
#
# `skodun review --reviewer <name>` and the `review` tool's `reviewer` argument
# are the same request, so a name that does not resolve must be refused with the
# same words through both doors. There is exactly one implementation of that
# refusal -- `run_review`'s preflight, reached through `services.svc_review` --
# and these tests are what says so.

#: Three entries covering the three ways a request can fail to resolve: a name
#: nobody configured, a name that is configured but `enabled = false`, and a
#: name whose provider has no registered adapter.
REVIEWER_CFG = """
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"

[[reviewers]]
name = "retired"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
enabled = false

[[reviewers]]
name = "offline"
provider = "no-such-provider"
model = "m"
role = "finder"
"""


def _reviewer_repo(tmp_path: Path, monkeypatch) -> Path:
    """A repo with an outgoing change and the three-entry table above.

    `SKODUN_ALLOW_MAIN`, because a plain `_mkrepo` is a primary checkout and its
    own preflight refusal would fire first -- identically on both surfaces, but
    about something else entirely.
    """
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(REVIEWER_CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    return repo


def test_the_review_tool_takes_a_reviewer_by_name_in_its_schema():
    """An agent can only pass what the schema publishes, and `additionalProperties:
    False` means an undeclared `reviewer` would be a client error rather than a
    selection. Optional: absent means the config's own finder heads the chain."""
    spec = _specs()["review"]
    props = spec.input_schema["properties"]
    assert set(props) == {"repo", "reviewer", "client_family", "recover",
                          "max_attempts", "max_wall_seconds",
                          "reuse_trusted", "fresh", "batch_target_bytes"}
    assert props["reviewer"]["type"] == "string"
    assert props["reviewer"]["description"]
    assert spec.input_schema["required"] == []


def test_the_review_tool_publishes_client_family_and_says_it_is_soft():
    """An agent can only pass what the schema publishes, and it can only use the
    argument well if the description says what it does NOT do: prefer another
    family, never refuse to review for the want of one.

    The two words checked are the SEMANTICS an agent has to be told (another
    family; a preference rather than a filter), not a sentence -- the wording
    itself is documentation and must stay free to be rewritten.
    """
    spec = _specs()["review"]
    prop = spec.input_schema["properties"]["client_family"]
    assert prop["type"] == "string"
    assert "different" in prop["description"].lower()
    assert "preference" in prop["description"].lower()


@pytest.mark.parametrize("name,needle", [
    ("no-such-entry", "is not configured"),
    ("retired", "is disabled"),
    ("offline", "no-such-provider"),
    # An EMPTY name is a request for a reviewer called "", not the absence of a
    # request -- `--reviewer ""` and `{"reviewer": ""}` must therefore be the
    # same refusal, and neither may quietly become "use the config default".
    ("", "is not configured"),
])
def test_a_reviewer_that_does_not_resolve_is_refused_identically(
        tmp_path, monkeypatch, capsys, name, needle):
    repo = _reviewer_repo(tmp_path, monkeypatch)
    db = tmp_path / "select.db"
    monkeypatch.setenv("SKODUN_DB", str(db))

    cli, tool = _both(db, ["review", "--repo", str(repo), "--reviewer", name],
                      "review", capsys, repo=str(repo), reviewer=name)

    assert cli == tool, (cli, tool)
    assert cli[0] == 2, cli
    assert needle in cli[1] and "no review ran" in cli[1], cli[1]
    # A refusal, so the banner invariant holds and nothing was recorded.
    assert cli[1].startswith("SKODUN VERDICT: trustworthy=false reason=")
    with Store.open(db) as st:
        assert st.list_reviews(None, 10) == []


def test_an_absent_reviewer_is_not_a_request_on_either_surface(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """The asymmetry `_repo_arg` already has, for the same reason: an argument
    nobody sent is the caller declining to choose, and it must not be refused.

    Both surfaces get as far as the CONFIG's own finder and report the pinned-
    away provider binary as a static preflight refusal (2), never silently
    spending review capacity on a provider that cannot run.
    """
    repo = _reviewer_repo(tmp_path, monkeypatch)
    db = tmp_path / "absent.db"
    monkeypatch.setenv("SKODUN_DB", str(db))

    cli, tool = _both(db, ["review", "--repo", str(repo)], "review", capsys,
                      repo=str(repo))

    assert cli[0] == tool[0] == 2, (cli, tool)
    assert "is not configured" not in cli[1] and "is not configured" not in tool[1]


def test_a_reviewer_of_the_wrong_type_is_refused_by_the_transport(tmp_path,
                                                                  monkeypatch):
    """argparse cannot produce these shapes, so they have no CLI wording to stay
    in step with -- exactly the split `_int_arg` and `_reason_arg` already make.
    Refused BEFORE the repo is touched, so nothing runs on a malformed call."""
    # An ABSENT repo means the cwd, so the cwd is moved somewhere that is not a
    # repository at all: a missing type check must not be able to launch a real
    # review of whatever directory the suite happens to be running in.
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "t.db"
    for bad in (["x"], 7, True, {"name": "x"}, 1.5):
        res = _tool("review", db, reviewer=bad)
        assert res.status == 2, (bad, res)
        assert "reviewer must be" in res.text, (bad, res.text)
        assert "Traceback" not in res.text, (bad, res.text)
    assert not db.exists(), "a malformed call opened a store"


def test_a_client_family_of_the_wrong_type_is_refused_by_the_transport(
        tmp_path, monkeypatch):
    """Same split as `reviewer`: a shape argparse cannot produce is the
    transport's to refuse, and refused before anything is opened or run."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "t.db"
    for bad in (["xai"], 7, True, {"name": "xai"}):
        res = _tool("review", db, client_family=bad)
        assert res.status == 2, (bad, res)
        assert "client_family must be" in res.text, (bad, res.text)
    assert not db.exists(), "a malformed call opened a store"


def _review_family(monkeypatch, db, *, client_name=None, **params):
    """What `client_family` the review tool hands the service for these inputs."""
    seen: dict = {}

    def fake(store, repo, **kw):
        seen.update(kw)
        return 0, "SKODUN VERDICT: trustworthy=true findings=0"

    monkeypatch.setattr(services, "svc_review", fake)
    spec = _specs()["review"]
    spec.handler(HandlerCall(params=params,
                             store_factory=lambda: Store.open(db),
                             cancel=threading.Event(),
                             client_name=client_name))
    return seen["client_family"]


def test_the_client_family_argument_reaches_the_service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _review_family(monkeypatch, tmp_path / "s.db",
                          repo=str(tmp_path), client_family="XAI ") == "xai"


def test_the_handshake_client_name_is_the_last_resort_default(tmp_path,
                                                              monkeypatch):
    """Priority is by SPECIFICITY: the argument describes this call, the env
    describes this machine, and the client name is a guess about a handshake.

    Resolved in the handler rather than threaded down raw, so the guess cannot
    outrank the operator's env — which is what a naive pass-through would do.
    """
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "s.db"
    monkeypatch.delenv("SKODUN_CLIENT_FAMILY", raising=False)
    assert _review_family(monkeypatch, db, repo=str(tmp_path),
                          client_name="Grok CLI") == "xai"
    monkeypatch.setenv("SKODUN_CLIENT_FAMILY", "google")
    assert _review_family(monkeypatch, db, repo=str(tmp_path),
                          client_name="Grok CLI") == "google"
    assert _review_family(monkeypatch, db, repo=str(tmp_path),
                          client_name="Grok CLI",
                          client_family="junie") == "junie"


def test_reuse_does_not_treat_handshake_family_as_explicit_intent(
        tmp_path, monkeypatch):
    seen: dict = {}

    def fake_detailed(store, repo, **kw):
        seen.update(kw)
        return 0, "SKODUN VERDICT: trustworthy=true findings=0", {}

    monkeypatch.setattr(services, "svc_review_detailed", fake_detailed)
    monkeypatch.chdir(tmp_path)
    _specs()["review"].handler(HandlerCall(
        params={"repo": str(tmp_path), "reuse_trusted": True},
        store_factory=lambda: Store.open(tmp_path / "s.db"),
        cancel=threading.Event(), client_name="Grok CLI"))
    assert seen["client_family"] == "xai"
    assert seen["reuse_client_family"] is None


def test_mcp_reuse_hit_preserves_structured_metadata_without_recovery(
        tmp_path, monkeypatch):
    from skodun import provenance

    def fake_detailed(store, repo, **kw):
        assert kw["reuse_trusted"] is True
        return 0, "SKODUN REUSE: review_id=r1", {
            "reuse": {"hit": True, "review_id": "r1"}}

    monkeypatch.setattr(services, "svc_review_detailed", fake_detailed)
    result = _specs()["review"].handler(HandlerCall(
        params={"repo": str(tmp_path), "reuse_trusted": True},
        store_factory=lambda: Store.open(tmp_path / "s.db"),
        cancel=threading.Event()))
    assert result.metadata["reuse"] == {"hit": True, "review_id": "r1"}
    assert result.metadata["skodun_version"] == skodun.__version__
    assert result.metadata["skodun_commit"] == (
        (provenance.cached_provenance() or {}).get("skodun_commit"))


def test_a_client_name_nothing_recognises_leaves_the_family_undeclared(
        tmp_path, monkeypatch):
    """Availability-only scoring is a perfectly good answer, and the default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKODUN_CLIENT_FAMILY", raising=False)
    assert _review_family(monkeypatch, tmp_path / "s.db", repo=str(tmp_path),
                          client_name="some-editor-nobody-mapped") is None


def test_the_handshake_stashes_the_client_name_for_later_calls(tmp_path):
    """`initialize` is where the only client-side hint this server ever gets
    arrives; a server that dropped it would have nothing to default from."""
    srv = McpServer(store_factory=lambda: Store.open(tmp_path / "s.db"))
    srv._m_initialize({"protocolVersion": "2025-11-25",
                       "clientInfo": {"name": "Grok CLI", "version": "1"}}, 1)
    assert srv._client_name == "Grok CLI"
    # A handshake with no `clientInfo` at all is legal, and leaves it unset.
    srv2 = McpServer(store_factory=lambda: Store.open(tmp_path / "s.db"))
    srv2._m_initialize({"protocolVersion": "2025-11-25"}, 1)
    assert srv2._client_name is None


# ==========================================================================
# the decisions that DO record something
# ==========================================================================

def test_a_dismissal_through_the_tool_records_and_moves_the_gate_exactly_as_the_cli(
        tmp_path, monkeypatch, capsys):
    """Parity where it costs something: the same ledger row, from either door.

    Run separately against two identical stores rather than twice against one,
    because the second dismissal of the same finding is a different question.
    """
    cli_db = _seeded(tmp_path / "a", _finding(0))
    tool_db = _seeded(tmp_path / "b", _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(cli_db))

    cli_code, cli_text = _cli(["triage", "rev1", "0", GOOD_REASON], capsys)
    res = _tool("triage_dismiss", tool_db, review_id="rev1", index=0,
                reason=GOOD_REASON)

    assert (res.status, res.text) == (cli_code, cli_text)
    assert cli_code == 0
    for db in (cli_db, tool_db):
        with Store.open(db) as st:
            state = st.triage_for("feat", "s" * 40)
            assert len(state) == 1, db
            history = st.triage_history(_lkey())
            assert [h["event"] for h in history] == ["dismiss"]
            assert history[-1]["reason"] == GOOD_REASON, "stored verbatim"


def test_adopting_through_the_tool_stores_the_refuters_own_words(tmp_path):
    """The reason is SYNTHESIZED from the annotation, so the tool has no `reason`
    argument at all -- an agent cannot author a dismissal reason here and
    attribute it to a model."""
    db = _seeded(tmp_path, _finding(0, _annotation()))
    assert "reason" not in _specs()["adopt_refuter"].input_schema["properties"]
    res = _tool("adopt_refuter", db, review_id="rev1", index=0)
    assert res.status == 0, res.text
    with Store.open(db) as st:
        assert len(st.triage_for("feat", "s" * 40)) == 1
        reason = st.triage_history(_lkey())[-1]["reason"]
    assert "refuter" in reason and "model-x" in reason, reason


def test_a_deferral_through_the_tool_records_and_moves_the_gate_as_the_cli_does(
        tmp_path, monkeypatch, capsys):
    """Parity where it costs something: the same ledger row and the same filed
    reference, from either door.

    Two identical stores rather than two calls against one, because a second
    deferral of the same finding is a different question.
    """
    cli_db = _seeded(tmp_path / "a", _finding(0))
    tool_db = _seeded(tmp_path / "b", _finding(0))
    monkeypatch.setenv("SKODUN_DB", str(cli_db))

    cli_code, cli_text = _cli(
        ["triage", "--defer", "rev1", "0", TRACKING_REF, DEFER_REASON], capsys)
    res = _tool("triage_defer", tool_db, review_id="rev1", index=0,
                tracking_ref=TRACKING_REF, reason=DEFER_REASON)

    assert (res.status, res.text) == (cli_code, cli_text)
    assert cli_code == 0
    for db in (cli_db, tool_db):
        with Store.open(db) as st:
            # The gate's view: cleared, exactly as a dismissal clears it.
            assert len(st.triage_for("feat", "s" * 40)) == 1, db
            history = st.triage_history(_lkey())
            assert [h["event"] for h in history] == ["defer"], db
            assert history[-1]["tracking_ref"] == TRACKING_REF, db
            assert history[-1]["reason"] == DEFER_REASON, "stored verbatim"
            # ... and it is outstanding debt, not a rejected finding.
            assert [r["tracking_ref"] for r in st.open_deferrals()] == [TRACKING_REF]


def test_the_defer_tool_publishes_the_reference_as_a_required_string(tmp_path):
    """An agent can only pass what the schema publishes, and the description is
    what tells it the reference must be real rather than plausible."""
    schema = _specs()["triage_defer"].input_schema
    assert schema["properties"]["tracking_ref"]["type"] == "string"
    assert "tracking_ref" in schema["required"]
    described = (schema["properties"]["tracking_ref"]["description"]
                 + _specs()["triage_defer"].description).lower()
    assert "issue" in described or "url" in described


def test_a_reopen_through_the_tool_is_append_only(tmp_path):
    from skodun.triage import dismiss

    db = _seeded(tmp_path, _finding(0))
    with Store.open(db) as st:
        dismiss(st, st.get_review("rev1"), 0, GOOD_REASON,
                now="2026-07-27T10:00:00Z")
    res = _tool("triage_reopen", db, review_id="rev1", index=0,
                reason="the guard was deleted in the refactor and this crashes")
    assert res.status == 0, res.text
    with Store.open(db) as st:
        assert st.triage_for("feat", "s" * 40) == {}, "the finding is open again"
        assert [h["event"] for h in st.triage_history(_lkey())] == \
            ["dismiss", "reopen"]


# ==========================================================================
# surface: the acknowledgement is the transport's, after its own write
# ==========================================================================

def _round_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real repository to hand the `surface` tool as its `repo`, and the git
    common dir the rows must be stamped with.

    The two are DIFFERENT values, which is the whole point of this task's MCP
    half: `_repo_arg` returns a CHECKOUT PATH (what `resolve_surface_branch`
    wants) and the column stores `gitio.git_common_dir` of it. `_handle_surface`
    performs that conversion; if it stopped, every assertion below would be
    about an empty report.
    """
    from skodun import gitio

    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = _mkrepo(tmp_path)
    return repo, str(gitio.git_common_dir(repo))


def _round_db(tmp_path: Path, *records, repo: str | None = None) -> Path:
    """A store holding undelivered background rounds. `_loud_round` and
    `_surface_db` are `test_cli.py`'s own fixtures, imported rather than
    re-spelled: a round shaped differently from the one the CLI's surface tests
    use would make the parity assertions below prove nothing."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _surface_db(tmp_path, *(records or (_loud_round(),)), repo=repo)


def _rpc(method: str, id_=None, **params) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params:
        msg["params"] = params
    return json.dumps(msg).encode("utf-8") + b"\n"


_HANDSHAKE = (_rpc("initialize", 100, protocolVersion="2025-11-25")
              + _rpc("notifications/initialized"))


class _Recorder:
    """A binary stdout that can be told to fail from the Nth write or flush on.

    FROM THE Nth, not always, and that is the whole point of the counters: a
    stream that fails on its FIRST flush loses the handshake, the read loop stops,
    and the tool under test never runs at all -- a test written that way passes
    whatever the acknowledgement order is. `from_=2` lets the handshake through
    and kills the TOOL RESULT's write, which is the only interesting moment.
    """

    def __init__(self, *, fail_write_from=None, fail_flush_from=None):
        self.chunks: list[bytes] = []
        self.writes = 0
        self.flushes = 0
        self.fail_write_from = fail_write_from
        self.fail_flush_from = fail_flush_from

    def write(self, data) -> int:
        self.writes += 1
        if self.fail_write_from is not None and self.writes >= self.fail_write_from:
            raise BrokenPipeError(32, "Broken pipe")
        self.chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flushes += 1
        if self.fail_flush_from is not None and self.flushes >= self.fail_flush_from:
            raise BrokenPipeError(32, "Broken pipe")

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


def _serve(db: Path, payload: bytes, out: _Recorder) -> int:
    server = McpServer(
        registry=mcpserver.default_registry(),
        prompts=mcpserver.default_prompts(),
        stdin=io.BytesIO(payload), stdout=out, stderr=io.StringIO(),
        store_factory=lambda: Store.open(db))
    return server.serve()


#: `test_cli.py`'s own ledger reader, so both surfaces' delivery assertions are
#: made against one definition of "what the ledger says".
_delivered = _delivery_rows


def test_the_surface_tool_delivers_a_round_and_acknowledges_it_as_mcp(tmp_path):
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path, repo=scope)
    out = _Recorder()
    code = _serve(db, _HANDSHAKE + _rpc(
        "tools/call", 1, name="surface",
        arguments={"branch": "feat", "repo": str(repo)}), out)
    assert code == 0
    body = json.loads(out.data.decode().splitlines()[1])
    assert body["result"]["isError"] is False
    assert "NPE 0" in body["result"]["content"][0]["text"]
    assert _delivered(db) == [("sk_1", "mcp")], (
        "the round was not acknowledged under this transport's own channel")


def test_a_flush_that_raises_leaves_the_round_undelivered(tmp_path):
    """THE ORDER IS THE PRODUCT. A report acknowledged before the flush would be
    recorded as delivered and never shown again -- the undelivered-findings
    failure the ledger exists to remove, reintroduced by the fix. A crash between
    the flush and the ack re-delivers instead, which is the designed direction.

    The flush is what this test attacks rather than the write, because a buffered
    write that "succeeded" is exactly the mistake: bytes in a buffer have not
    reached a reader.
    """
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path, repo=scope)
    # The handshake's flush succeeds; the TOOL RESULT's flush raises. Anything
    # simpler loses the handshake and never reaches the tool.
    out = _Recorder(fail_flush_from=2)
    code = _serve(db, _HANDSHAKE + _rpc(
        "tools/call", 1, name="surface",
        arguments={"branch": "feat", "repo": str(repo)}), out)
    assert code == 0
    assert out.flushes >= 2, "the tool result was never even attempted"
    assert _delivered(db) == [], (
        "the round was acknowledged from a buffer that never reached the client")


def test_a_write_that_raises_leaves_the_round_undelivered(tmp_path):
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path, repo=scope)
    out = _Recorder(fail_write_from=2)      # the handshake lands; the report does not
    _serve(db, _HANDSHAKE + _rpc(
        "tools/call", 1, name="surface",
        arguments={"branch": "feat", "repo": str(repo)}), out)
    assert out.writes >= 2, "the tool result was never even attempted"
    assert _delivered(db) == []


def test_a_quiet_round_is_acknowledged_by_the_service_not_by_the_transport(
        tmp_path):
    """A trustworthy round with zero findings renders NOTHING, so there is nothing
    a write could lose: `delivery.surface` acknowledges it immediately under the
    `quiet` channel, and it never appears in `pending_acks`. That is why an
    empty report is still progress rather than a round re-scanned forever."""
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path / "quiet", _round(),     # clean, zero findings
                   repo=scope)
    res = _tool("surface", db, branch="feat", repo=str(repo))
    assert res.pending_acks == []
    assert _delivered(db) == [("sk_1", "quiet")]
    assert "no undelivered" in res.text


def test_the_surface_tool_reports_nothing_to_report_in_words(tmp_path):
    repo, _scope = _round_repo(tmp_path / "scope")
    db = tmp_path / "none.db"
    Store.open(db).close()
    res = _tool("surface", db, branch="feat", repo=str(repo))
    assert res.status == 0
    assert res.text == services.surface_no_rounds_note("feat")
    assert res.pending_acks == []


def test_the_surface_tool_scopes_its_rows_to_the_repo_it_was_given(tmp_path):
    """`_repo_arg` hands the handler a CHECKOUT PATH and the column holds
    `git_common_dir`; a handler that skipped the conversion, or converted the
    wrong argument, would deliver another repository's rounds -- and
    permanently acknowledge them."""
    a, scope_a = _round_repo(tmp_path / "a")
    b, scope_b = _round_repo(tmp_path / "b")
    db = tmp_path / "two.db"
    with Store.open(db) as store:
        store.save_review(dict(_loud_round(id="in_a", branch="feat"),
                               repo=scope_a))
        store.save_review(dict(_round(id="in_b", branch="feat",
                                      summary="the b repository"),
                               repo=scope_b))

    res = _tool("surface", db, branch="feat", repo=str(b))
    assert res.pending_acks == []
    assert "NPE 0" not in res.text, "repository A's round was rendered"
    # B's own round is quiet, so B's pass acknowledges exactly that one.
    assert _delivered(db) == [("in_b", "quiet")]

    res = _tool("surface", db, branch="feat", repo=str(a))
    assert res.pending_acks == ["in_a"]
    assert "NPE 0" in res.text


def test_the_surface_tool_refuses_a_repo_git_cannot_read(tmp_path):
    """The conversion can FAIL, and a failure is a refusal rather than a fall
    back to the server's cwd: reporting and permanently acknowledging some other
    repository's rounds because the named one could not be read is the damage
    this scope removes."""
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path / "d", repo=scope)
    # A directory that exists and is not a repository, so the BRANCH resolves
    # (it is given) and the repository is the only thing that cannot.
    plain = tmp_path / "plain"
    plain.mkdir()
    res = _tool("surface", db, branch="feat", repo=str(plain))
    assert res.status == 2
    assert "could not resolve the repository" in res.text
    assert res.pending_acks == []
    assert _delivered(db) == [], "a refused pass acknowledged a round"


def test_the_scoped_log_renders_identically_on_both_surfaces(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    """The parity that acceptance criterion 7 is actually about.

    `test_the_log_tool_and_the_log_command_render_the_same_lines` compares the
    two surfaces with NO branch, so it never touches the repository scope --
    every scoped path could diverge while it stayed green. This runs the
    scoped form on both: same store, same branch, same repository, byte-equal
    output and equal status.
    """
    a, scope_a = _round_repo(tmp_path / "a")
    b, scope_b = _round_repo(tmp_path / "b")
    db = tmp_path / "parity.db"
    with Store.open(db) as store:
        store.save_review(dict(_round(id="in_a", branch="main",
                                      summary="the a repository"), repo=scope_a))
        store.save_review(dict(_round(id="in_b", branch="main",
                                      summary="the b repository"), repo=scope_b))
    monkeypatch.setenv("SKODUN_DB", str(db))

    cli_code, cli_text = _cli(["log", "--branch", "main", "--repo", str(a)],
                              capsys)
    res = _tool("log", db, branch="main", repo=str(a))

    assert (res.status, res.text) == (cli_code, cli_text), (
        "the scoped log diverged between the CLI and the MCP tool")
    assert "the a repository" in cli_text
    assert "the b repository" not in cli_text, (
        "the scope leaked on BOTH surfaces, so parity alone would not catch it")


def test_the_log_tool_scopes_a_branch_and_stays_lazy_without_one(tmp_path):
    """The `log` tool's half of the parity decision. `repo` narrows `branch`
    and is resolved ONLY with one -- `git_common_dir` shells out to git, and an
    unscoped `log` from a server spawned outside a repository is this tool's
    contract, not an accident."""
    a, scope_a = _round_repo(tmp_path / "a")
    b, scope_b = _round_repo(tmp_path / "b")
    db = tmp_path / "log.db"
    with Store.open(db) as store:
        store.save_review(dict(_round(id="in_a", branch="main",
                                      summary="the a repository"), repo=scope_a))
        store.save_review(dict(_round(id="in_b", branch="main",
                                      summary="the b repository"), repo=scope_b))

    scoped = _tool("log", db, branch="main", repo=str(a))
    assert scoped.status == 0
    assert "the a repository" in scoped.text
    assert "the b repository" not in scoped.text

    everything = _tool("log", db, repo=str(tmp_path / "not-a-repository"))
    assert everything.status == 0, (
        "an unscoped `log` resolved a repository it never needed")
    assert "the a repository" in everything.text
    assert "the b repository" in everything.text

    refused = _tool("log", db, branch="main",
                    repo=str(tmp_path / "not-a-repository"))
    assert refused.status == 2
    assert "could not resolve the repository" in refused.text


def test_all_four_delivery_channels_are_reachable_and_persisted(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """`cli-text`, `cli-claude`, `mcp`, `quiet` -- every value in
    `delivery.CHANNELS`, each written by the surface that owns it."""
    repo, scope = _round_repo(tmp_path / "scope")
    seen = {}
    for fmt, channel in (("text", "cli-text"), ("claude", "cli-claude")):
        db = _round_db(tmp_path / f"cli-{fmt}", repo=scope)
        monkeypatch.setenv("SKODUN_DB", str(db))
        assert main(["surface", "--repo", str(repo), "--branch", "feat",
                     "--hook-format", fmt]) == 0
        capsys.readouterr()
        seen[channel] = _delivered(db)

    db = _round_db(tmp_path / "mcp", repo=scope)
    out = _Recorder()
    _serve(db, _HANDSHAKE + _rpc(
        "tools/call", 1, name="surface",
        arguments={"branch": "feat", "repo": str(repo)}), out)
    seen["mcp"] = _delivered(db)

    quiet_db = _round_db(tmp_path / "quiet2", _round(), repo=scope)
    _tool("surface", quiet_db, branch="feat", repo=str(repo))
    seen["quiet"] = _delivered(quiet_db)

    assert {c: rows[0][1] for c, rows in seen.items()} == {
        "cli-text": "cli-text", "cli-claude": "cli-claude", "mcp": "mcp",
        "quiet": "quiet"}
    assert set(seen) == delivery.CHANNELS


def test_the_surface_tool_and_the_surface_command_render_the_same_report(
        tmp_path, monkeypatch, capsys):
    repo, scope = _round_repo(tmp_path / "scope")
    cli_db = _round_db(tmp_path / "a", repo=scope)
    tool_db = _round_db(tmp_path / "b", repo=scope)
    monkeypatch.setenv("SKODUN_DB", str(cli_db))
    capsys.readouterr()
    assert main(["surface", "--repo", str(repo), "--branch", "feat"]) == 0
    cli_text = capsys.readouterr().out
    res = _tool("surface", tool_db, branch="feat", repo=str(repo))
    assert res.text == cli_text
    assert res.pending_acks == ["sk_1"]


def test_include_delivered_takes_real_booleans_and_defaults_to_false(tmp_path):
    """The other half of the refusal above: rejecting `"false"` must not have
    been done by rejecting everything. A JSON `true` replays a round the ledger
    already holds; `false` and an absent argument do not."""
    repo, scope = _round_repo(tmp_path / "scope")
    db = _round_db(tmp_path / "d", repo=scope)
    first = _tool("surface", db, branch="feat", repo=str(repo),
                  include_delivered=False)
    assert first.status == 0 and first.pending_acks == ["sk_1"]
    with Store.open(db) as st:
        delivery.acknowledge(st, ["sk_1"], "mcp")

    # Delivered now, so the default and an explicit False both report nothing.
    for params in ({}, {"include_delivered": False}):
        again = _tool("surface", db, branch="feat", repo=str(repo), **params)
        assert again.status == 0, (params, again)
        assert again.pending_acks == [], (params, again)
        assert "no undelivered" in again.text, (params, again)

    replay = _tool("surface", db, branch="feat", repo=str(repo),
                   include_delivered=True)
    assert replay.status == 0
    assert "NPE 0" in replay.text


# ==========================================================================
# review: one at a time, and cancellable end to end
# ==========================================================================

def test_a_second_review_call_is_refused_while_one_is_in_flight(tmp_path):
    """Capacity 1, with the REAL review tool behind it.

    Two foreground reviews would race for the foreground lock, and the loser
    would sit in a lock wait for the whole stale ceiling. Refused, not queued: a
    queued review would review a working tree that has moved by the time it runs.
    """
    started, release = threading.Event(), threading.Event()
    shipped = mcpserver.default_registry()          # ONE tuple: `is` must match
    real = {s.name: s for s in shipped}["review"].handler

    def slow(call):
        """The shipped review handler, held open so the second call has something
        to be refused by. The handler itself is real -- what is faked is only the
        moment it finishes."""
        started.set()
        release.wait(30)
        return real(call)

    registry = tuple(
        mcpserver.HandlerSpec(name=s.name, long_running=s.long_running,
                              input_schema=s.input_schema,
                              handler=slow if s.name == "review" else s.handler,
                              description=s.description)
        for s in shipped)

    db = tmp_path / "busy.db"
    Store.open(db).close()
    reader, writer = os.pipe()
    out = _Recorder()
    server = McpServer(registry=registry, stdin=os.fdopen(reader, "rb"),
                       stdout=out, stderr=io.StringIO(),
                       store_factory=lambda: Store.open(db))
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("code", server.serve()),
                         daemon=True)
    t.start()
    try:
        with os.fdopen(writer, "wb", buffering=0) as w:
            w.write(_HANDSHAKE)
            w.write(_rpc("tools/call", 1, name="review",
                         arguments={"repo": str(tmp_path)}))
            assert started.wait(30), "the review never started"
            w.write(_rpc("tools/call", 2, name="review",
                         arguments={"repo": str(tmp_path)}))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ids = [json.loads(x).get("id")
                       for x in out.data.decode().splitlines()]
                if 2 in ids:
                    break
                time.sleep(0.02)
            busy = [json.loads(x) for x in out.data.decode().splitlines()
                    if json.loads(x).get("id") == 2]
            assert busy, "the second review was queued instead of refused"
            assert busy[0]["result"]["isError"] is True
            assert busy[0]["result"]["content"][0]["text"] == mcpserver.BUSY_TEXT
            assert busy[0]["result"]["structuredContent"]["status"] == 2
            release.set()
    finally:
        release.set()
        t.join(timeout=60)
    assert not t.is_alive()
    assert box.get("code") == 0


# --- the end-to-end cancellation drill ------------------------------------

_HANG_BODY = """\
python3 -c "import os; open('$D/started.pgid','w').write(str(os.getpgid(0)))"
trap '' TERM
sleep 300
"""

CFG = """
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
"""


def _hang_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with an outgoing change and a fake grok that hangs mid-review."""
    from tests.test_pipeline import _fake_grok

    _fake_grok(tmp_path, _HANG_BODY)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo, tmp_path / "bin"


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                 # pragma: no cover
        return True
    return True


def test_closing_stdin_cancels_the_review_in_flight_end_to_end(tmp_path):
    """Cancel-on-disconnect drill (opt-in ``SKODUN_MCP_DISCONNECT=cancel``).

    Every link in the chain is exercised: stdin EOF -> the server sets the token
    -> `svc_review` -> `run_review` -> chain -> the watchdog tick loop (provider
    process group). Then: demote record, release lock, join, exit 0.

    Default disconnect is **drain** (separate test). This pins the legacy cancel
    path operators can re-enable when they want session end to abort work.
    """
    from skodun.gitio import git_common_dir

    repo, bindir = _hang_repo(tmp_path)
    env = _env(tmp_path)
    env["SKODUN_MCP_DISCONNECT"] = "cancel"
    env["SKODUN_GROK_BIN"] = str(bindir / "grok")
    env["SKODUN_ALLOW_MAIN"] = "1"
    env["SKODUN_SECURITY_PASS"] = "0"
    env["SKODUN_SKEPTIC_PASS"] = "0"
    env["SKODUN_LOCK_WAIT_SECONDS"] = "5"
    env["SKODUN_LOCK_POLL_SECONDS"] = "0.05"

    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "mcp"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True)
    pgid_file = bindir / "started.pgid"
    try:
        proc.stdin.write(_HANDSHAKE)
        proc.stdin.write(_rpc("tools/call", 1, name="review",
                              arguments={"repo": str(repo)}))
        proc.stdin.flush()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not pgid_file.exists():
            assert proc.poll() is None, "the server exited before reviewing"
            time.sleep(0.05)
        assert pgid_file.exists(), "the provider never started"
        pgid = int(pgid_file.read_text(encoding="utf-8").strip())

        proc.stdin.close()                  # EOF, with a review in flight
        # `wait` + `read`, never `communicate()`: `communicate` flushes stdin,
        # which we have deliberately closed. Both streams are tiny (one handshake
        # reply, one tool result, a few progress lines), so no pipe can fill.
        rc = proc.wait(timeout=120)
        out, err = proc.stdout.read(), proc.stderr.read()
    finally:
        if proc.poll() is None:             # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()

    assert rc == 0, err.decode()

    assert b"Traceback" not in err, err.decode()
    # Whatever came back on stdout is still nothing but JSON-RPC.
    for line in out.decode().splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _group_alive(pgid):
        time.sleep(0.05)
    assert not _group_alive(pgid), (
        "the model CLI outlived the cancelled review, still spending quota")

    with Store.open(Path(env["SKODUN_DB"])) as st:
        rows = st.list_reviews(None, 10)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "failed", rows[0]
    assert rows[0]["trustworthy"] is False
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists(), \
        "the cancelled review kept the foreground lock"


def test_closing_stdin_drains_the_review_so_it_finishes_end_to_end(tmp_path):
    """Default disconnect policy: session EOF does not cancel; review completes.

    A slow-but-finite provider starts, stdin closes mid-flight, and the process
    must wait for a real terminal store row (clean) rather than aborting to
    failed/cancelled. That is the restart-safe path.
    """
    from tests.test_pipeline import CLEAN, _emit, _fake_grok
    from skodun.gitio import git_common_dir

    # Start marker + short sleep + clean emit: long enough to close stdin while
    # running, short enough for a 120s wait. `$D` expands in the fake grok shim.
    _fake_grok(
        tmp_path,
        'python3 -c "import os; open(\'$D/started.pgid\',\'w\')'
        '.write(str(os.getpgid(0)))"\n'
        "sleep 1\n"
        + _emit(CLEAN),
    )

    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    bindir = tmp_path / "bin"

    env = _env(tmp_path)
    # Explicit drain (also the default) so a dirty env cannot fluke cancel.
    env["SKODUN_MCP_DISCONNECT"] = "drain"
    env["SKODUN_GROK_BIN"] = str(bindir / "grok")
    env["SKODUN_ALLOW_MAIN"] = "1"
    env["SKODUN_SECURITY_PASS"] = "0"
    env["SKODUN_SKEPTIC_PASS"] = "0"
    env["SKODUN_LOCK_WAIT_SECONDS"] = "5"
    env["SKODUN_LOCK_POLL_SECONDS"] = "0.05"

    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "mcp"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True)
    pgid_file = bindir / "started.pgid"
    try:
        proc.stdin.write(_HANDSHAKE)
        proc.stdin.write(_rpc("tools/call", 1, name="review",
                              arguments={"repo": str(repo)}))
        proc.stdin.flush()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not pgid_file.exists():
            assert proc.poll() is None, "the server exited before reviewing"
            time.sleep(0.05)
        assert pgid_file.exists(), "the provider never started"
        proc.stdin.close()                  # EOF while review still running
        rc = proc.wait(timeout=120)
        out, err = proc.stdout.read(), proc.stderr.read()
    finally:
        if proc.poll() is None:             # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()

    assert rc == 0, err.decode()
    assert b"Traceback" not in err, err.decode()
    assert b"disconnect policy=drain" in err, err.decode()
    for line in out.decode().splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"

    with Store.open(Path(env["SKODUN_DB"])) as st:
        rows = st.list_reviews(None, 10)
    assert len(rows) == 1, rows
    # Drain finishes the work: not cancelled/failed-from-cancel.
    assert rows[0]["status"] not in ("running",), rows[0]
    assert rows[0].get("trustworthy") is True, rows[0]
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_the_server_joins_the_review_thread_before_exiting(tmp_path):
    """Skipping the join is a named mutation, and this is the test it dies on.

    Abandoning the thread makes the process exit sooner and leaves the review
    mid-write: a `running` row, a held lock, and an orphaned provider group. The
    ORDERING is what is asserted -- the review's own record must be terminal by
    the time the process is gone, which is only true if the exit waited for it.

    Uses cancel-on-disconnect so a hanging provider still terminates promptly.
    """
    from skodun.gitio import git_common_dir

    repo, bindir = _hang_repo(tmp_path)
    env = _env(tmp_path)
    env["SKODUN_MCP_DISCONNECT"] = "cancel"
    env["SKODUN_GROK_BIN"] = str(bindir / "grok")
    env["SKODUN_ALLOW_MAIN"] = "1"
    env["SKODUN_LOCK_WAIT_SECONDS"] = "5"

    proc = subprocess.Popen(
        [sys.executable, "-m", "skodun", "mcp"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        start_new_session=True)
    try:
        proc.stdin.write(_HANDSHAKE)
        proc.stdin.write(_rpc("tools/call", 1, name="review",
                              arguments={"repo": str(repo)}))
        proc.stdin.flush()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (bindir / "started.pgid").exists():
            assert proc.poll() is None
            time.sleep(0.05)
        proc.stdin.close()
        assert proc.wait(timeout=120) == 0, proc.stderr.read().decode()
    finally:
        if proc.poll() is None:             # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()

    # THE INSTANT the process is gone, everything the review owned is already
    # settled. No polling here, deliberately: a wait would hide the race.
    with Store.open(Path(env["SKODUN_DB"])) as st:
        rows = st.list_reviews(None, 10)
    assert rows and rows[0]["status"] == "failed", rows
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_the_review_handler_returns_a_result_for_a_cancellation_never_raises(
        tmp_path, monkeypatch):
    """The review thread catches `ReviewCancelled` the way the worker does.

    It does so by delegating: `svc_review` maps it to `(4, banner_failure("review
    cancelled"))`, so the handler returns an ordinary tool result. That matters
    because the alternative is a RAISING handler -- which the transport does turn
    into a status-2 tool error, but as a net, not as an interface: the agent would
    read "the tool failed: ReviewCancelled(...)" instead of a verdict line, and the
    status would say "nothing ran" about a review that may have spent three model
    calls.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    # The primary-checkout refusal fires BEFORE the first cancellation checkpoint
    # and would report 2 instead, proving nothing about cancellation.
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")

    spec = _specs()["review"]
    cancelled = threading.Event()
    cancelled.set()                     # cancelled before it even starts
    res = spec.handler(HandlerCall(
        params={"repo": str(repo)},
        store_factory=lambda: Store.open(tmp_path / "c.db"),
        cancel=cancelled))

    assert isinstance(res, HandlerResult)
    assert res.status == 4, res.text
    assert res.text == ("SKODUN VERDICT: trustworthy=false "
                        "reason=review cancelled"), res.text
    with Store.open(tmp_path / "c.db") as st:
        assert st.list_reviews(None, 10) == [], (
            "a review cancelled before it started left a record behind")


def test_a_review_of_a_directory_that_is_not_a_repository_is_a_tool_error(
        tmp_path):
    """The ordinary misuse an agent will actually commit: a wrong `repo`. It is a
    readable refusal with the verdict banner the CLI would print, not a
    traceback and not a protocol error."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    db = tmp_path / "r.db"
    res = _tool("review", db, repo=str(plain))
    assert res.status == 2, res.text
    assert res.text.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "no review ran" in res.text


def test_the_review_tool_returns_the_verdict_banner_the_cli_prints(tmp_path,
                                                                  monkeypatch,
                                                                  capsys):
    """The banner is `trust.banner(record)` on both surfaces, from the one
    definition -- the pipeline no longer prints it anywhere."""
    from tests.test_pipeline import CLEAN, _emit, _fake_grok

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")

    db = tmp_path / "review.db"
    res = _tool("review", db, repo=str(repo))
    assert res.status == 0, res.text
    assert res.text.startswith("SKODUN VERDICT: trustworthy=true findings=0")
    assert res.metadata["skodun_version"] == skodun.__version__
    assert (mcpserver.tool_result(res)["structuredContent"]
            ["skodun_version"]) == skodun.__version__

    # ...and the CLI, on its own store, prints exactly that shape as its last
    # stdout line.
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "cli-review.db"))
    capsys.readouterr()
    assert main(["review", "--repo", str(repo)]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("SKODUN VERDICT: trustworthy=true findings=0")


def test_a_malformed_repo_is_refused_rather_than_defaulted_to_the_cwd(
        tmp_path, monkeypatch):
    """The asymmetry is the point, and it is a fail-closed argument.

    An ABSENT `repo` means "here", which for a client-spawned server is the
    project it was spawned in. A repo of the wrong TYPE is a client that ignored
    its own schema, and defaulting it would answer a gate question ABOUT A
    DIFFERENT DIRECTORY than the one that was asked about -- a PASS for content
    nobody asked about is the one outcome this product exists to make impossible.
    """
    db = _seeded(tmp_path, _finding(0))
    # `log` joined this list when it grew a `repo` of its own: the refusal is
    # `_repo_arg`'s and must fire on every tool that reads one, including the
    # one whose repo is only ever used to narrow a branch.
    for name in ("gate", "review", "surface", "log"):
        for bad in (["x"], 7, "", "   "):
            res = _tool(name, db, repo=bad)
            assert res.status == 2, (name, bad, res)
            assert "repo must be a path" in res.text, (name, bad, res.text)
    # Absent repo means cwd. Pin cwd to a non-git directory so the gate's
    # fail-closed "nothing covers this" answer is 2 regardless of whether the
    # developer's checkout happens to be clean (which would otherwise be 0
    # "no outgoing change" and make this assertion environment-dependent).
    bare = tmp_path / "not-a-git-worktree"
    bare.mkdir()
    monkeypatch.chdir(bare)
    absent = _tool("gate", db)
    assert absent.status == 2, absent
    assert "repo must be a path" not in absent.text
