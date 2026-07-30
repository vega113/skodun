"""The extraction contract: what a service is allowed to be.

The CLI suite is the proof that the extraction PRESERVED behaviour -- it passes
unmodified, which is a stronger statement than anything that could be written
here. This module pins the properties that make the extraction reusable rather
than merely equivalent, and every one of them is a rule a future service would
break by accident:

  * **The caller owns the Store, and it is the first parameter.** Connection
    lifetime is a transport question -- one per CLI invocation, one per MCP tool
    call, because sqlite connections are bound to the thread that created them --
    and a service that opened its own would decide it for both, wrongly for one.
  * **Nothing in a service reaches stdout.** Not a verdict, not a diagnostic, not
    a progress line. `skodun mcp` serves JSON-RPC on stdout from a thread that may
    be mid-response, so a stray line there desynchronises the client's parser for
    the rest of the session -- and a process-global `redirect_stdout` would be the
    same bug with a nicer name.
  * **A service returns a decision, never makes one about presentation.**
    `(status, text)`, with the exit code the CLI would exit with; the transport
    decides whether that text belongs on stdout, on stderr, or in a tool result.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

import skodun
from skodun import services
from skodun.store import Store
from tests.test_cli import _annotation, _artifact, _finding, _loud_round, _round
from tests.test_gitio import _git, _mkrepo

#: Every store-backed service, by name. The list itself is part of the contract:
#: these are the eight the MCP surface mirrors.
STORE_BACKED = ["svc_gate", "svc_review", "svc_log", "svc_surface",
                "svc_triage_list", "svc_triage_dismiss", "svc_adopt_refuter",
                "svc_triage_reopen"]

GOOD_REASON = "the guard at line 12 already rejects a None handler before this"


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))
    # The provider binaries too, and not as belt-and-braces: `adapters.grok`
    # prefers `~/.grok/bin/grok` over PATH, so on any machine that has grok
    # installed a PATH-only fake would silently lose and a test would run the
    # real CLI. Nothing here should reach a provider at all; pinning them at a
    # path that does not exist is what makes that a failure rather than a bill.
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "no-bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "no-bin" / "codex"))


def _db(tmp_path: Path, *records) -> Path:
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        for rec in records:
            st.save_review(rec)
    return db


# ==========================================================================
# the shape of the seam
# ==========================================================================

def test_every_store_backed_service_takes_the_store_first():
    """The caller owns the connection, EXPLICITLY -- so it is the first
    parameter, on every one of them, with no keyword-only escape hatch."""
    for name in STORE_BACKED:
        fn = getattr(services, name)
        params = list(inspect.signature(fn).parameters.values())
        assert params[0].name == "store", (name, params[0].name)
        assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name


def test_no_service_opens_a_store_of_its_own():
    """A service that opened one would decide the connection's lifetime for both
    transports -- and get it wrong for the MCP server, whose review tool answers
    from a thread the read loop's connection cannot be used on."""
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    assert "Store.open(" not in src
    assert "_store_path" not in src


def test_no_module_redirects_stdout_anywhere():
    """`redirect_stdout` is forbidden across the whole package, not just here.

    It is process-global: an MCP review running on a worker thread would redirect
    the READ LOOP's stdout too, so a response written while it was in scope would
    vanish -- and the client would wait forever for a call that had already been
    answered into a StringIO. The banner is returned instead, which is why the
    temptation exists at all.
    """
    # A CALL or an IMPORT, not the word: several modules explain in prose why this
    # is forbidden, and a test that banned the explanation would delete the
    # explanation.
    banned = (r"redirect_stdout\s*\(", r"import\s+redirect_stdout",
              r"^\s*sys\.stdout\s*=")
    for path in sorted((Path(skodun.__file__).parent).rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for pattern in banned:
            hit = re.search(pattern, src, re.MULTILINE)
            assert hit is None, f"{path.name}: {hit.group(0)!r}"


def test_the_services_module_is_importable_without_sqlite_or_git():
    """Every heavy import is INSIDE a function. `skodun mcp` imports this module
    to build its registry and must not pay for the review pipeline's module graph
    before it has served a line."""
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    toplevel = [line for line in src.splitlines()
                if re.match(r"^(import|from)\s", line)]
    assert toplevel == ["from pathlib import Path"], toplevel


# ==========================================================================
# nothing prints
# ==========================================================================

def test_no_service_writes_to_stdout_on_any_path(tmp_path, capsys, monkeypatch):
    """Driven, not inspected: every service, on a path that has something to say.

    A `redirect_stdout` guard would make this test pass while breaking the thing
    it protects, which is why the test above forbids that separately.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    db = _db(tmp_path, _artifact([_finding(0, _annotation())]), _loud_round())
    capsys.readouterr()

    with Store.open(db) as store:
        results = [
            services.svc_gate(store, repo),
            services.svc_log(store, None, 20),
            services.svc_log(store, None, 0),            # the refusal path
            services.svc_triage_list(store, "rev1"),
            services.svc_triage_list(store, "nope"),      # the refusal path
            services.svc_triage_dismiss(store, "rev1", 0, "fp"),   # refused
            services.svc_adopt_refuter(store, "rev1", 0),
            services.svc_triage_reopen(store, "rev1", 0, GOOD_REASON),
            services.svc_surface(store, "feat")[:2],
            services.svc_review(store, tmp_path / "not-a-repo"),
        ]
    cap = capsys.readouterr()
    assert cap.out == "", f"a service wrote to stdout: {cap.out!r}"
    # ...and every one of them really did produce something to say, or the
    # assertion above would be about ten no-ops.
    for status, text in results:
        assert isinstance(status, int) and isinstance(text, str)
        assert text, results


def test_a_service_never_writes_to_stdout_even_when_stdout_is_the_only_stream_left(
        tmp_path, monkeypatch):
    """The paranoid form: a stdout that RAISES on any use. If a service touched
    it, this would be an exception rather than a returned refusal."""

    class _DeadStdout:
        def write(self, *_a, **_k):
            raise AssertionError("a service wrote to stdout")

        def flush(self, *_a, **_k):
            raise AssertionError("a service flushed stdout")

    db = _db(tmp_path, _artifact([_finding(0)]))
    monkeypatch.setattr(sys, "stdout", _DeadStdout())
    with Store.open(db) as store:
        assert services.svc_triage_list(store, "rev1")[0] == 0
        assert services.svc_log(store, None, 5)[0] == 0
        assert services.svc_triage_dismiss(store, "rev1", 0, GOOD_REASON)[0] == 0


# ==========================================================================
# svc_surface: three shapes, and the quiet ack
# ==========================================================================

def test_svc_surface_returns_a_payload_and_the_content_bearing_ids(tmp_path):
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat")
    assert status == 0
    assert "NPE 0" in text and text.endswith("\n")
    assert pending == ["sk_1"], (
        "the transport must be handed exactly the rounds whose delivery depends "
        "on a write it has not performed yet")


def test_svc_surface_returns_nothing_to_report_as_an_empty_payload(tmp_path):
    """`(0, "", [])` and NOT a message: the CLI's stdout is a payload a hook feeds
    to an agent verbatim, so "nothing to report" has to be distinguishable from
    "here is a report that says nothing". Each transport words it itself, from the
    one definition in `surface_no_rounds_note`."""
    db = _db(tmp_path)
    with Store.open(db) as store:
        assert services.svc_surface(store, "feat") == (0, "", [])
    assert "feat" in services.surface_no_rounds_note("feat")


def test_svc_surface_acknowledges_quiet_rounds_itself_and_never_returns_them(
        tmp_path):
    """A trustworthy round with nothing to say renders nothing, so no write can
    lose it: it is acknowledged HERE, immediately, under the `quiet` channel.
    Leaving it unacknowledged would re-scan it at every session start forever."""
    db = _db(tmp_path, _round())              # clean, zero findings: quiet
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat")
        rows = [(r["review_id"], r["channel"]) for r in
                store._c.execute("SELECT review_id, channel FROM deliveries")]
    assert (status, text, pending) == (0, "", [])
    assert rows == [("sk_1", "quiet")]


def test_svc_surface_reports_a_broken_ledger_as_a_diagnostic_not_a_payload(
        tmp_path, monkeypatch):
    """Status 2 with text is a DIAGNOSTIC: the CLI puts it on stderr, where a hook
    consuming stdout cannot mistake it for a report."""
    from skodun import delivery

    def boom(*_a, **_kw):
        raise RuntimeError("the ledger is unreadable")

    monkeypatch.setattr(delivery, "surface", boom)
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat")
    assert status == 2 and pending == []
    assert "could not read the delivery ledger" in text


def test_svc_surface_refuses_an_unknown_format_before_touching_the_ledger(
        tmp_path):
    """Misuse must not acknowledge anything -- `delivery.surface` validates the
    format FIRST, and the service lets that refusal through as a diagnostic."""
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, _ = services.svc_surface(store, "feat", "yaml")
        rows = list(store._c.execute("SELECT review_id FROM deliveries"))
    assert status == 2 and "yaml" in text
    assert rows == [], "a rejected format acknowledged a round"


def test_resolve_surface_branch_prefers_the_argument_and_never_raises(tmp_path):
    assert services.resolve_surface_branch("feat") == ("feat", "")
    branch, why_not = services.resolve_surface_branch(None, tmp_path / "nowhere")
    assert branch == ""
    assert "pass --branch" in why_not and "Traceback" not in why_not


# ==========================================================================
# svc_log / svc_review shapes
# ==========================================================================

def test_svc_log_refuses_a_non_positive_limit_without_reading_the_store(tmp_path):
    """`-n` becomes SQLite's LIMIT, where NEGATIVE means unlimited: `-n -1` would
    dump the whole store while reading like a request for fewer rows."""
    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as store:
        for bad in (0, -1):
            status, text = services.svc_log(store, None, bad)
            assert status == 2 and "positive" in text
            assert "2026-" not in text, "a rejected limit rendered rows"


def test_svc_log_renders_an_empty_store_as_an_empty_string(tmp_path):
    db = _db(tmp_path)
    with Store.open(db) as store:
        assert services.svc_log(store, None, 20) == (0, "")


def test_svc_review_is_keyword_only_for_its_two_new_parameters():
    """`progress_sink` and `cancel` are keyword-only on `run_review` so that every
    shipped positional call site keeps working; the service mirrors that."""
    params = inspect.signature(services.svc_review).parameters
    for name in ("progress_sink", "cancel"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is None, name


def test_svc_review_reports_an_unloadable_pipeline_with_a_banner(monkeypatch,
                                                                tmp_path):
    """The one import failure that must still carry a verdict line: without it a
    broken install reports on stderr and leaves stdout silent where a refusal
    belongs."""
    from skodun import pipeline

    monkeypatch.delattr(pipeline, "run_review")
    db = _db(tmp_path)
    with Store.open(db) as store:
        status, text = services.svc_review(store, tmp_path)
    assert status == 2
    assert text.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "no review ran" in text


def test_svc_review_re_raises_keyboard_interrupt_past_every_guard(monkeypatch,
                                                                 tmp_path):
    """Ctrl-C is 130 (the shell's 128 + SIGINT), which none of this service's
    codes can say -- so it must not be swallowed by any of the guards that would
    otherwise report 2 or 4."""
    from skodun import config

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_review(store, repo)


def test_svc_gate_re_raises_keyboard_interrupt_and_records_nothing(monkeypatch,
                                                                   tmp_path):
    """`svc_review` re-raises Ctrl-C at four guards; `svc_gate`'s single
    `except BaseException` used to swallow it.

    Two costs, and the second is the one that lasts. A run the user CANCELLED
    was reported as `FAIL(2) could not run the gate: KeyboardInterrupt()`,
    which is a verdict about the change rather than about the interruption --
    and 130 is what the shell expects, which no code in this contract can say.
    Worse, `_record_setup_failure` wrote a `gate_events` row for it, so the
    audit trail claims a gate decision was taken on a run that never took one.
    """
    from skodun import config

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_gate(store, repo)
    with Store.open(db) as store:
        rows = list(store._c.execute("SELECT * FROM gate_events"))
    assert rows == [], "a cancelled run was recorded as a gate decision"


def test_an_ordinary_gate_setup_failure_is_still_a_recorded_fail_2(monkeypatch,
                                                                   tmp_path):
    """The other half: letting Ctrl-C through must not have been done by
    letting everything through. An ordinary exception is STILL exit 2, still
    reported in words, and still written to the audit trail -- this is a
    fail-closed gate, and 2 is the conservative value in its contract."""
    from skodun import config

    def boom(*_a, **_k):
        raise RuntimeError("unparseable config")

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        status, text = services.svc_gate(store, repo)
    assert status == 2
    assert "SKODUN GATE: FAIL(2) could not run the gate" in text
    assert "unparseable config" in text
    with Store.open(db) as store:
        rows = list(store._c.execute("SELECT * FROM gate_events"))
    assert len(rows) == 1


@pytest.mark.parametrize("call", [
    pytest.param(lambda st, repo: services.svc_gate(st, repo), id="svc_gate"),
    pytest.param(lambda st, repo: services.svc_log(st, None, 10), id="svc_log"),
    pytest.param(lambda st, repo: services.svc_surface(st, "feat", "text", False),
                 id="svc_surface"),
    pytest.param(lambda st, repo: services.resolve_surface_branch(None, repo),
                 id="resolve_surface_branch"),
    pytest.param(lambda st, repo: services.svc_adopt_refuter(st, "r1", 0),
                 id="svc_adopt_refuter"),
    pytest.param(
        lambda st, repo: services.svc_triage_reopen(st, "r1", 0, GOOD_REASON),
        id="svc_triage_reopen"),
])
def test_no_service_guard_turns_a_ctrl_c_into_a_synthetic_failure(
        monkeypatch, tmp_path, call):
    """Every `except BaseException` in this module, swept in one place.

    Each one exists to stop an ordinary failure (an unreadable store, a git that
    will not run, a ledger that stopped answering) from escaping as a traceback,
    and each was catching Ctrl-C as well -- reporting a synthetic 2 for a run the
    user chose to abandon, and taking the CLI's 130 mapping away from it. A
    future service copying the pattern gets caught here rather than in a user's
    terminal.
    """
    from skodun import config, delivery, gitio, triage

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    for module, name in ((config, "load_config"), (gitio, "current_branch"),
                         (delivery, "surface"), (triage, "adopt_refuter"),
                         (triage, "reopen")):
        monkeypatch.setattr(module, name, boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path, _round(id="r1", findings=[_finding(0)], findings_total=1,
                              artifact=_artifact(_finding(0))))
    with Store.open(db) as store:
        monkeypatch.setattr(store, "list_reviews", boom)
        with pytest.raises(KeyboardInterrupt):
            call(store, repo)


# ==========================================================================
# the refusal strings live in one place
# ==========================================================================

def test_the_usage_strings_are_module_constants_not_literals():
    """Two callers each -- the CLI's pre-store argparse check and the service's own
    absence check -- so a literal at either site would be a second definition that
    drifts the first time one is reworded."""
    cli_src = (Path(skodun.__file__).parent / "cli.py").read_text(encoding="utf-8")
    for name in ("TRIAGE_REOPEN_USAGE", "TRIAGE_ADOPT_USAGE"):
        constant = getattr(services, name)
        assert constant.startswith("skodun triage: usage:")
        assert name in cli_src, f"the CLI does not use services.{name}"
        assert constant not in cli_src, (
            f"{name}'s text is spelled a second time in cli.py")
    # The plain-dismissal usage string has only ONE caller (the service), because
    # argparse cannot produce that shape on the CLI -- so it is a constant for
    # symmetry, and the test says so rather than pretending otherwise.
    assert services.TRIAGE_DISMISS_USAGE.startswith("skodun triage: usage:")


def test_the_cancellation_reason_is_one_constant():
    assert services.REVIEW_CANCELLED_REASON == "review cancelled"
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    assert src.count('"review cancelled"') == 1
