"""Foreground cancellation, and the progress sink that comes with it.

`run_review` gained two keyword-only parameters in Task 14 -- `cancel` and
`progress_sink` -- because an MCP client can close its stdin while a review is
running, and a review that ignored that would keep a foreground lock, a model
subscription and a `running` record alive on behalf of a session that is gone.

The cancellation MECHANISM is Task 10's and is reused verbatim:
`ReviewCancelled` is a `BaseException` (so no demote-don't-destroy `except
Exception` can turn a killed run into a merely degraded review), the provider's
process group is taken down by the watchdog tick loop (the only layer holding the
pgid), and `store.mark_failed`/`mark_cancelled` are the atomic demotions. What is
new here, and what this module pins, is the FOREGROUND's own five windows:

    while WAITING for the lock       abort the wait; no lock, no record
    after the lock, before a record  no record at all
    during any provider call         the group dies; the record is demoted
    after the last provider exits    the pre-persist checkpoint demotes it
    during the final commit          the POST-COMMIT check demotes the row

Every test asserts the same three facts, because each is a different way the same
mistake shows up: the record is untrustworthy (or absent), the lock is gone, and
no process the review started is still alive. NEVER A TRUSTWORTHY FINALIZE AFTER
CANCELLATION is the whole property.

The provider is a shell script that reports its own process group and then hangs,
so "the group died" is observed rather than assumed. Nothing here talks to a real
model, a real store, or the developer's `~/.grok`.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from skodun import pipeline, runner, services
from skodun.config import load_config
from skodun.gitio import git_common_dir
from skodun.pipeline import ReviewCancelled, run_review
from skodun.promptbuild import Prompt
from skodun.store import Store
from tests.test_fallback import _fake_cli
from tests.test_gitio import _git, _mkrepo
from tests.test_pipeline import CLEAN, DIRTY, _emit, _fake_grok, _per_call
from tests.test_refuter import CFG_FINDER_XAI, CFG_REFUTER_OPENAI

CFG = CFG_FINDER_XAI


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Nothing here may reach the developer's store, config, or provider CLIs."""
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "bin" / "codex"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.delenv("SKODUN_REFUTER_PASS", raising=False)
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    # A short SIGTERM->SIGKILL grace: these tests kill process groups on purpose
    # and the production 3s would be three seconds of nothing per test.
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


def _repo(tmp_path: Path, cfg: str = CFG, extra: str = "") -> Path:
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(cfg + extra, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo


def _store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "s.db")


def _hang(marker: str = "started") -> str:
    """A provider body that publishes its PGID and then refuses to finish.

    The pgid file is what makes "the provider group died" an observation. `trap ''
    TERM` is deliberate: a body that dies on SIGTERM would pass this test even if
    the final group SIGKILL were removed.
    """
    return (f'python3 -c "import os; open(\'$D/{marker}.pgid\',\'w\')'
            f'.write(str(os.getpgid(0)))"\n'
            "trap '' TERM\n"
            "sleep 120\n")


def _calls(tmp_path: Path) -> list[str]:
    """Which provider binaries were invoked, in order. `_fake_cli`/`_fake_grok`
    share one log, so this is the whole run's call sequence."""
    log = tmp_path / "bin" / "calls.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def _pgid(tmp_path: Path, marker: str = "started") -> int | None:
    p = tmp_path / "bin" / f"{marker}.pgid"
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except ValueError:                      # pragma: no cover - a partial write
        return None


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                 # pragma: no cover - not ours anymore
        return True
    return True


def _wait_for(predicate, timeout=60.0, what="condition", run=None):
    """Poll until `predicate` answers truthily. `run` makes the wait FAIL FAST.

    Without it, a review that died for an unrelated reason (a config typo, a
    sqlite connection used off its thread) shows up as "timed out waiting for the
    provider to start" sixty seconds later, with the real exception nowhere in the
    report.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if run is not None and not run.thread.is_alive():
            raise AssertionError(
                f"the review ended before {what}: error={run.error!r} "
                f"record={run.record!r}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _Run:
    """`run_review` on its own thread, with a token the test can set.

    A thread, not a subprocess, because the token is the thing under test: the MCP
    server sets it from ITS read loop while the review runs on another thread, and
    that is exactly the shape reproduced here. The end-to-end process version
    (stdin EOF -> exit 0) lives in `test_mcptools.py`.

    The Store is opened INSIDE the thread, from a path, because sqlite connections
    are bound to the thread that created them -- which is exactly why
    `HandlerCall` carries a `store_factory` rather than a Store.
    """

    def __init__(self, repo: Path, db: Path, **kw):
        self.repo, self.db, self.kw = repo, Path(db), kw
        self.cancel = threading.Event()
        self.record: dict | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._body, daemon=True)

    def _body(self) -> None:
        try:
            with Store.open(self.db) as store:
                self.record = run_review(self.repo, load_config(self.repo),
                                         store, cancel=self.cancel, **self.kw)
        except BaseException as e:          # noqa: BLE001 - recorded, not handled
            self.error = e

    def __enter__(self) -> "_Run":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.cancel.set()                   # never leave a review running
        self.thread.join(timeout=120)
        assert not self.thread.is_alive(), "the review thread never finished"

    def join(self, timeout=120) -> None:
        self.thread.join(timeout=timeout)
        assert not self.thread.is_alive(), "the review thread never finished"


def _assert_cancelled(run: _Run, tmp_path: Path, repo: Path, *,
                      expect_record: bool) -> dict | None:
    """The three facts every cancellation must leave behind."""
    assert isinstance(run.error, ReviewCancelled), (
        f"expected ReviewCancelled, got {run.error!r} / record={run.record!r}")
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists(), \
        "the cancelled review kept the foreground lock"
    with Store.open(tmp_path / "s.db") as st:
        rows = st.list_reviews(None, 100)
    if not expect_record:
        assert rows == [], (
            "a review cancelled before it persisted anything left a record "
            f"behind: {rows!r}")
        return None
    assert len(rows) == 1, rows
    rec = rows[0]
    assert rec["status"] == "failed", rec
    assert rec["trustworthy"] is False
    assert type(rec["trustworthy"]) is bool
    return rec


# ==========================================================================
# the progress sink
# ==========================================================================

def test_the_progress_sink_takes_the_notes_and_stderr_stays_empty(tmp_path,
                                                                  capsys):
    """A caller that passes a sink gets the progress; stderr gets none of it.

    The MCP transport is why this exists at all: its stdout is a protocol, so a
    progress line has to be capturable rather than printed. What must NEVER be
    tried is a process-global `redirect_stdout` -- another thread may be
    mid-response on that stream -- so the seam is a callable and the default is
    unchanged.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lines: list[str] = []

    rec = run_review(repo, load_config(repo), _store(tmp_path),
                     progress_sink=lines.append)

    assert rec["status"] == "clean"
    assert any("reviewing" in line for line in lines), lines
    cap = capsys.readouterr()
    assert cap.out == "", f"the pipeline wrote to stdout: {cap.out!r}"
    assert "reviewing" not in cap.err, (
        f"progress went to stderr as well as to the sink: {cap.err!r}")


def test_the_default_sink_is_still_stderr(tmp_path, capsys):
    """`progress_sink=None` is the shipped behaviour, byte for byte."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)

    run_review(repo, load_config(repo), _store(tmp_path))

    cap = capsys.readouterr()
    assert cap.out == ""
    assert "skodun: reviewing" in cap.err


def test_a_sink_is_removed_when_the_call_returns_even_if_it_raises(tmp_path,
                                                                   capsys):
    """A sink left installed would capture the NEXT review's progress on a reused
    thread -- and an MCP server reuses nothing BUT threads."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    st = _store(tmp_path)

    def sink(_line):
        raise RuntimeError("this sink is broken")

    # A broken sink must not fail the review, and must not silence it either:
    # `_note` falls through to stderr.
    rec = run_review(repo, load_config(repo), st, progress_sink=sink)
    assert rec["status"] == "clean"
    assert "skodun: reviewing" in capsys.readouterr().err

    assert getattr(pipeline._PROGRESS, "sink", None) is None
    # ...and the next review with no sink narrates to stderr as it always did.
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    run_review(repo, load_config(repo), st)
    assert "skodun: reviewing" in capsys.readouterr().err


# ==========================================================================
# the lock wait
# ==========================================================================

def test_a_token_set_before_the_lock_is_taken_leaves_nothing_behind(tmp_path):
    """Cancelled before anything: no lock, no record, no model call."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    token = threading.Event()
    token.set()

    with pytest.raises(ReviewCancelled):
        run_review(repo, load_config(repo), _store(tmp_path), cancel=token)

    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()
    with Store.open(tmp_path / "s.db") as st:
        assert st.list_reviews(None, 10) == []
    assert not (tmp_path / "bin" / "calls.log").exists(), "a model call was spent"


def test_the_lock_is_never_even_created_for_a_review_already_cancelled(tmp_path):
    """The check at the TOP of the acquisition loop, pinned by its one observable
    consequence: no lock directory is ever created.

    Redundant with the checkpoint immediately after acquisition as far as the
    RECORD is concerned -- both end in `ReviewCancelled` with nothing persisted --
    but not as far as PEERS are concerned: taking the lock and dropping it again
    is a directory other worktrees poll, and a review that is already cancelled
    has no business publishing itself as the holder even for a millisecond.
    """
    from skodun.gitio import git_common_dir

    repo = _repo(tmp_path)
    common = git_common_dir(repo)
    lock = common / "grok-reviews-foreground.lock"
    token = threading.Event()
    token.set()

    with pytest.raises(ReviewCancelled):
        pipeline._acquire_fg_lock(common, repo, wait=5.0, poll=0.05, stale=60.0,
                                  cancel=token)
    assert not lock.exists(), (
        "a review that was already cancelled published itself as the lock holder")


def test_eof_while_waiting_for_the_lock_aborts_the_wait(tmp_path, monkeypatch):
    """The named mutation is "make the foreground lock wait ignore the token".

    The wait is the LONGEST thing a foreground review does before it does
    anything -- the default is the whole stale ceiling, tens of minutes -- so a
    lock loop that only watched the clock would keep polling for half an hour on
    behalf of an agent session that has already gone away. The wait is set to 300s
    here and the assertion is that the call comes back in seconds: a token-blind
    loop cannot pass that.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    # A live holder: our own pid, so nothing can reclaim it as stale.
    lock.mkdir(parents=True)
    (lock / "budget").write_text("3600\n", encoding="utf-8")
    (lock / "owner").write_text(
        f"pid={os.getpid()}\nstarted={int(time.time())}\nworktree={repo}\n",
        encoding="utf-8")

    token = threading.Event()
    box: dict = {}

    def body():
        started = time.monotonic()
        try:
            run_review(repo, load_config(repo), _store(tmp_path), cancel=token,
                       lock_wait=300.0, lock_poll=30.0)
        except BaseException as e:          # noqa: BLE001
            box["error"] = e
        box["elapsed"] = time.monotonic() - started

    t = threading.Thread(target=body, daemon=True)
    t.start()
    # Wait until it is really WAITING (the note is on stderr; the lock is still
    # the holder's), then cancel.
    time.sleep(0.5)
    token.set()
    t.join(timeout=30)

    assert not t.is_alive(), "the lock wait ignored the cancellation token"
    assert isinstance(box.get("error"), ReviewCancelled), box
    assert box["elapsed"] < 30, (
        f"the wait was abandoned only at the next poll ({box['elapsed']:.1f}s); "
        f"it must wait on the TOKEN, not on the clock")
    # The holder's lock is untouched: a waiter that gives up removes nothing.
    assert lock.exists() and (lock / "owner").exists()
    with Store.open(tmp_path / "s.db") as st:
        assert st.list_reviews(None, 10) == []


# ==========================================================================
# before anything is persisted
# ==========================================================================

def test_cancellation_before_the_record_is_persisted_leaves_no_record(tmp_path,
                                                                     monkeypatch):
    """Cancelled under the lock, before any row exists: NO record.

    The shipped `finally` only demotes what was persisted, and that is right:
    an empty `failed` row for a review that never started is a trace of nothing,
    and it would be one more row the surface has to explain away.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    token = threading.Event()
    real = pipeline.gitio.capture_diff

    def capture_then_cancel(*a, **kw):
        out = real(*a, **kw)
        token.set()                 # under the lock, before the first save
        return out

    monkeypatch.setattr(pipeline.gitio, "capture_diff", capture_then_cancel)

    with pytest.raises(ReviewCancelled):
        run_review(repo, load_config(repo), _store(tmp_path), cancel=token)

    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()
    with Store.open(tmp_path / "s.db") as st:
        assert st.list_reviews(None, 10) == []
    assert not (tmp_path / "bin" / "calls.log").exists(), "a model call was spent"


def test_the_empty_diff_path_is_cancellable_too(tmp_path, monkeypatch):
    """The one path that persists a CLEAN record without calling a model.

    It is the most dangerous path to leave uncancellable, because it is the only
    one that mints `trustworthy=true` with no provider involved at all: a token
    set before it must produce no record rather than a clean verdict for a
    session that is gone.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cfg")
    _git(repo, "checkout", "-b", "feat")        # nothing outgoing
    token = threading.Event()
    real = pipeline.gitio.capture_diff

    def capture_then_cancel(*a, **kw):
        out = real(*a, **kw)
        token.set()
        return out

    monkeypatch.setattr(pipeline.gitio, "capture_diff", capture_then_cancel)

    with pytest.raises(ReviewCancelled):
        run_review(repo, load_config(repo), _store(tmp_path), cancel=token)

    with Store.open(tmp_path / "s.db") as st:
        assert st.list_reviews(None, 10) == [], (
            "an empty-diff run recorded a clean verdict after being cancelled")
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


# ==========================================================================
# during a provider call: PRIMARY, EXTRA, REFUTER
# ==========================================================================

def test_cancellation_during_the_primary_pass_kills_the_provider_and_demotes(
        tmp_path):
    """The main event. A `running` record exists, the model is mid-call, and the
    token is set: the group dies, the row is demoted, the lock is released."""
    _fake_grok(tmp_path, _hang())
    repo = _repo(tmp_path)

    with _Run(repo, tmp_path / "s.db") as run:
        pgid = _wait_for(lambda: _pgid(tmp_path), what="the provider to start",
                         run=run)
        run.cancel.set()
        run.join()

    rec = _assert_cancelled(run, tmp_path, repo, expect_record=True)
    assert "did not finish" in (rec["failure_reason"] or ""), rec
    _wait_for(lambda: not _group_alive(pgid), timeout=30,
              what="the provider's process group to die")


def test_cancellation_during_an_extra_pass_never_finalizes_the_primary(tmp_path,
                                                                      monkeypatch):
    """The named mutation is "swallow `ReviewCancelled` in `_extra_pass`".

    `_extra_pass` catches `Exception` in order to DEMOTE rather than destroy a
    review: a pass that failed leaves the primary standing as an untrustworthy
    record the gate can refuse. Widen that clause to `BaseException` and a
    CANCELLATION becomes a mere failed pass -- the run then walks on to the final
    `_persist` and commits a record for a review whose provider was killed. So
    the assertion is not only "untrustworthy": it is that the primary's own clean
    answer never reached a finalized record.
    """
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    # `_fake_cli`, not `_fake_grok`: its shared log NAMES the binary, so the call
    # sequence asserted below is legible.
    _fake_cli(tmp_path, "grok", _per_call(_emit(CLEAN), _hang("skeptic")))
    repo = _repo(tmp_path)

    with _Run(repo, tmp_path / "s.db") as run:
        pgid = _wait_for(lambda: _pgid(tmp_path, "skeptic"),
                         what="the skeptic pass to start", run=run)
        run.cancel.set()
        run.join()

    rec = _assert_cancelled(run, tmp_path, repo, expect_record=True)
    # TWO calls: the primary answered cleanly and the skeptic was under way. That
    # is what makes this the extra-pass window rather than an earlier failure.
    assert _calls(tmp_path) == ["grok", "grok"], _calls(tmp_path)
    # The record on disk is the `running` shell the run persisted before the
    # first model call, demoted -- the foreground saves the finished record ONCE,
    # at the end, and that save never happened.
    assert rec["failure_reason"] == pipeline.UNFINISHED_REASON, rec
    _wait_for(lambda: not _group_alive(pgid), timeout=30,
              what="the skeptic provider's process group to die")


def test_cancellation_during_the_refuter_pass_never_finalizes_the_primary(
        tmp_path, monkeypatch):
    """The refuter is the more dangerous of the two clauses to widen.

    `_refuter_pass` demotes NOTHING on any failure path -- an annotation that
    could not be produced is an absent annotation -- so a swallowed cancellation
    there would leave a CLEAN, TRUSTWORTHY primary and finalize it. That record
    would satisfy the gate for content whose review was killed.
    """
    monkeypatch.setenv("SKODUN_REFUTER_PASS", "1")
    _fake_cli(tmp_path, "grok", _emit(DIRTY))
    _fake_cli(tmp_path, "codex", _hang("refuter"))
    repo = _repo(tmp_path, CFG_FINDER_XAI + CFG_REFUTER_OPENAI)

    with _Run(repo, tmp_path / "s.db") as run:
        pgid = _wait_for(lambda: _pgid(tmp_path, "refuter"),
                         what="the refuter pass to start", run=run)
        run.cancel.set()
        run.join()

    rec = _assert_cancelled(run, tmp_path, repo, expect_record=True)
    # The finder answered on xai and the refuter was under way on openai: this is
    # the refuter window, not an earlier failure.
    assert _calls(tmp_path) == ["grok", "codex"], _calls(tmp_path)
    assert rec["failure_reason"] == pipeline.UNFINISHED_REASON, rec
    _wait_for(lambda: not _group_alive(pgid), timeout=30,
              what="the refuter provider's process group to die")


# ==========================================================================
# after the last provider exits
# ==========================================================================

def test_a_token_set_after_the_last_provider_exits_still_demotes(tmp_path,
                                                                monkeypatch):
    """The window Task 10's worker closes with its pre-finalize check, here on the
    foreground path.

    Nothing is running any more: the record in hand has clean axes and
    `save_review` recomputes `trustworthy` from those axes ALONE, so without a
    checkpoint immediately before the persist this would commit a TRUSTWORTHY
    review of content the caller has already walked away from.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    token = threading.Event()
    real = pipeline._status_for

    def status_then_cancel(rec):
        out = real(rec)
        token.set()      # after the review, before the final persist
        return out

    monkeypatch.setattr(pipeline, "_status_for", status_then_cancel)

    with pytest.raises(ReviewCancelled):
        run_review(repo, load_config(repo), _store(tmp_path), cancel=token)

    with Store.open(tmp_path / "s.db") as st:
        (rec,) = st.list_reviews(None, 10)
    assert rec["status"] == "failed" and rec["trustworthy"] is False
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


def test_a_token_set_during_the_final_commit_demotes_the_committed_row(
        tmp_path, monkeypatch):
    """THE POST-COMMIT LINEARIZATION CHECK, foreground twin of the worker's.

    A token set while SQLite holds the write lock is invisible to the checkpoint
    that injects before the call -- and the row is then already `clean` and
    `trustworthy=1`. `store.mark_cancelled` is what demotes it, and it is
    `cancellation_transform` as one atomic statement, so the findings survive
    while the trust does not.
    """
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _repo(tmp_path)
    st = _store(tmp_path)
    token = threading.Event()
    real = st.save_review
    saves = {"n": 0}

    def save_then_cancel(rec):
        saves["n"] += 1
        real(rec)
        if saves["n"] > 1:      # the FINAL save; the row is committed and clean
            token.set()

    monkeypatch.setattr(st, "save_review", save_then_cancel)

    with pytest.raises(ReviewCancelled):
        run_review(repo, load_config(repo), st, cancel=token)

    with Store.open(tmp_path / "s.db") as check:
        (rec,) = check.list_reviews(None, 10)
    assert rec["status"] == "failed"
    assert rec["trustworthy"] is False
    assert rec["degraded"] is True, "the DEGRADED axis is what removes trust"
    assert "cancelled during finalization" in (rec["degraded_reason"] or "")
    # The findings are PRESERVED: the finder really did produce them, and "NO
    # REVIEW HAPPENED" printed over real evidence is its own failure.
    assert rec["findings_total"] == 1, rec
    assert not (git_common_dir(repo) / "grok-reviews-foreground.lock").exists()


# ==========================================================================
# the service maps it
# ==========================================================================

def test_svc_review_reports_a_cancellation_as_four_and_says_so(tmp_path):
    """4, never a gentler code, and never a traceback.

    2 would say "nothing ran" about a review that may have spent three model
    calls; 0 or 1 would claim a verdict. 4 is "no trustworthy review exists",
    which is exactly true, and it is the value the gate's own contract uses.
    """
    _fake_grok(tmp_path, _hang())
    repo = _repo(tmp_path)
    token = threading.Event()
    box: dict = {}

    def body():
        # The Store is opened on the thread that uses it, exactly as the MCP
        # `review` handler does through `call.store_factory()`.
        with Store.open(tmp_path / "s.db") as st:
            box["result"] = services.svc_review(st, repo, cancel=token)

    t = threading.Thread(target=body, daemon=True)
    t.start()
    try:
        pgid = _wait_for(lambda: _pgid(tmp_path), what="the provider to start")
        token.set()
        t.join(timeout=120)
    finally:
        token.set()
        t.join(timeout=120)
    assert not t.is_alive()

    code, text = box["result"]
    assert code == 4
    assert text == ("SKODUN VERDICT: trustworthy=false reason=review cancelled"), \
        text
    _wait_for(lambda: not _group_alive(pgid), timeout=30,
              what="the provider's process group to die")
    with Store.open(tmp_path / "s.db") as check:
        (rec,) = check.list_reviews(None, 10)
    assert rec["status"] == "failed" and rec["trustworthy"] is False


def test_a_cancelled_review_never_satisfies_the_gate(tmp_path):
    """The property all of the above exists for, asserted through the gate itself.

    Every demotion above is only worth something if the gate refuses the row it
    leaves. `latest_trustworthy_for` is the exact query `gate.run_gate` makes, so
    this is the gate's own requirement rather than a restatement of it.
    """
    _fake_grok(tmp_path, _hang())
    repo = _repo(tmp_path)

    with _Run(repo, tmp_path / "s.db") as run:
        _wait_for(lambda: _pgid(tmp_path), what="the provider to start",
                  run=run)
        run.cancel.set()
        run.join()

    rec = _assert_cancelled(run, tmp_path, repo, expect_record=True)
    with Store.open(tmp_path / "s.db") as st:
        assert st.latest_trustworthy_for(rec["diff_hash"]) is None, (
            "a cancelled review is a dedup candidate / satisfies the gate")


# ==========================================================================
# the shipped call is unchanged
# ==========================================================================

def test_run_review_without_a_token_is_the_shipped_call(tmp_path, monkeypatch):
    """`cancel=None` must not even add a keyword to the calls below it.

    `_run_chain` and `_acquire_fg_lock` are monkeypatched BY NAME all over the
    suite, against the shipped signatures. `pipeline._cancel_kw` is what keeps
    those stand-ins working, and this is the test that says so.
    """
    seen: dict = {}
    real_chain = pipeline._run_chain
    real_lock = pipeline._acquire_fg_lock

    def chain(*a, **kw):
        seen["chain_kw"] = sorted(kw)
        return real_chain(*a, **kw)

    def lock(*a, **kw):
        seen["lock_kw"] = sorted(kw)
        return real_lock(*a, **kw)

    monkeypatch.setattr(pipeline, "_run_chain", chain)
    monkeypatch.setattr(pipeline, "_acquire_fg_lock", lock)
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)

    run_review(repo, load_config(repo), _store(tmp_path))

    assert "cancel" not in seen["chain_kw"], seen
    assert "cancel" not in seen["lock_kw"], seen
    assert pipeline._cancel_kw(None) == {}
    token = threading.Event()
    assert pipeline._cancel_kw(token) == {"cancel": token}


# ==========================================================================
# the two `except Exception` clauses, pinned directly
# ==========================================================================
#
# The integration drills above prove the OUTCOME; these two prove the MECHANISM,
# and they are the tests the named mutations die on. Both clauses exist to DEMOTE
# rather than destroy a review, so both are written as `except Exception` -- and
# `ReviewCancelled` is a `BaseException` precisely so it slips past them. Widen
# either one and a killed review becomes a merely-failed PASS, after which the
# run walks on and finalizes a record for a review whose provider is dead.
#
# A pass-boundary `_checkpoint` would catch that a moment later, which is exactly
# why these two tests are needed: the second line of defence makes the first
# one's absence invisible end to end.


def _prompt(text: bytes = b"prompt") -> Prompt:
    return Prompt(text=text, diff_truncated=False, prompt_bytes=len(text))


def _bare_rec() -> dict:
    return {"id": "sk_test", "parse_ok": True, "degraded": False,
            "degraded_reason": "", "diff_truncated": False, "summary": "ok",
            "findings": [], "findings_total": 0, "extra_passes": {},
            "failure_reason": "", "severity": {"high": 0, "medium": 0, "low": 0}}


def test_extra_pass_lets_a_cancellation_through_instead_of_demoting(tmp_path,
                                                                   monkeypatch):
    """`_extra_pass`'s `except Exception` must not become `except BaseException`."""
    from skodun.config import Config, Defaults, Reviewer

    def cancelled(*_a, **_kw):
        raise ReviewCancelled("the review was cancelled while a reviewer was running")

    monkeypatch.setattr(pipeline, "_run_chain", cancelled)
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    with Store.open(tmp_path / "s.db") as store:
        with pytest.raises(ReviewCancelled):
            pipeline._extra_pass(_bare_rec(), "skeptic", _prompt, reviewer, cfg,
                                 cfg.defaults, tmp_path, store, tmp_path)


def test_refuter_pass_lets_a_cancellation_through_instead_of_annotating(
        tmp_path, monkeypatch):
    """The same clause in `_refuter_pass`, which is the more dangerous of the two:
    this pass demotes NOTHING, so a swallowed cancellation would leave a clean,
    trustworthy primary standing and finalize it."""
    from skodun.config import Config, Defaults, Reviewer

    def cancelled(*_a, **_kw):
        raise ReviewCancelled("the review was cancelled while a reviewer was running")

    monkeypatch.setattr(pipeline, "_run_chain", cancelled)
    reviewer = Reviewer(name="r", provider="xai", model="m", role="refuter")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    with Store.open(tmp_path / "s.db") as store:
        with pytest.raises(ReviewCancelled):
            pipeline._refuter_pass(_bare_rec(), 1, _prompt, reviewer, cfg,
                                   cfg.defaults, tmp_path, store, tmp_path,
                                   "openai")


def test_a_pass_that_fails_for_any_other_reason_still_demotes_rather_than_raises(
        tmp_path, monkeypatch):
    """The other half, or the tests above would pass with `except ReviewCancelled:
    raise` bolted on and the demote-don't-destroy behaviour deleted."""
    from skodun.config import Config, Defaults, Reviewer

    def boom(*_a, **_kw):
        raise RuntimeError("adapter exploded mid-pass")

    monkeypatch.setattr(pipeline, "_run_chain", boom)
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    with Store.open(tmp_path / "s.db") as store:
        merged = pipeline._extra_pass(_bare_rec(), "skeptic", _prompt, reviewer,
                                      cfg, cfg.defaults, tmp_path, store,
                                      tmp_path)
        annotated = pipeline._refuter_pass(_bare_rec(), 1, _prompt, reviewer, cfg,
                                           cfg.defaults, tmp_path, store,
                                           tmp_path, "openai")
    assert merged["extra_passes"]["skeptic"]["failed"] is True
    assert merged["parse_ok"] is False, "an exploded extra pass must demote"
    assert annotated["parse_ok"] is True, "the refuter demotes nothing, ever"
    assert annotated["extra_passes"]["refuter"]["status"] == "failed"
