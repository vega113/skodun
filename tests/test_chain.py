"""The chain executor's module boundary: `chain.run_chain` is the same code
`pipeline._run_chain` used to be, and the alias `pipeline._run_chain` is that
exact function object, not a wrapper or a re-implementation.

Phase 3 Task 2 extracted the executor out of `pipeline.py` into its own
module (behavior-preserving: every function moved verbatim). The actual
behavioral coverage for the chain executor stays where it already lived --
`test_fallback.py`, `test_pipeline.py`, `test_refuter.py`, `test_adapter_agy.py`
and `test_cli.py` all drive it through `pipeline._run_chain` (some of them
monkeypatching that exact name), unmodified by this move. This file only
pins the import-level contract the move must preserve.
"""

from __future__ import annotations

import threading

import pytest

from skodun import chain, pipeline


def test_pipeline_run_chain_is_the_chain_module_s_run_chain():
    """`pipeline._run_chain` is a one-line alias, not a copy: existing tests
    (`test_pipeline.py`, `test_refuter.py`) monkeypatch `pipeline._run_chain`
    by name and rely on `run_review`/`_extra_pass`/`_refuter_pass` picking up
    the patched value through that same name."""
    assert pipeline._run_chain is chain.run_chain


def test_pipeline_outcome_is_the_chain_module_s_outcome():
    """`_apply`'s `outcome: _Outcome` type hint and every `_run_chain` caller
    still see one `_Outcome` type, wherever it is actually defined."""
    assert pipeline._Outcome is chain._Outcome


# --------------------------------------------------------------------------
# the cancellation token's route through the chain (Task 10)
# --------------------------------------------------------------------------


def test_run_chain_forwards_the_cancellation_token_to_every_attempt(tmp_path,
                                                                    monkeypatch):
    """`run_chain` is the only thing between the worker's token and the pgid.

    The token has to reach `run_with_watchdog` on EVERY attempt, not just the
    first: a chain works through fallbacks and retries, and a supersede landing
    during attempt 3 must not have to wait for the chain to exhaust itself.
    """
    from skodun import runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    seen = []

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        seen.append(cancel)
        out.write_bytes(b'{"structuredOutput": {"summary": "s", "findings": []},'
                        b' "stopReason": "EndTurn"}')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.1,
                                first_output_sec=0.05)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    token = threading.Event()
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    store = Store.open(tmp_path / "s.db")
    with store:
        chain.run_chain(reviewer, cfg, cfg.defaults, b"prompt", tmp_path, store,
                        tmp_path, "t", cancel=token)
    assert seen == [token], "the token did not reach the watchdog"


def test_run_chain_without_a_token_is_the_shipped_call(tmp_path, monkeypatch):
    """`cancel=None` by default: the foreground path is byte-identical."""
    from skodun import runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    seen = []

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        seen.append(cancel)
        out.write_bytes(b'{"structuredOutput": {"summary": "s", "findings": []},'
                        b' "stopReason": "EndTurn"}')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.1,
                                first_output_sec=0.05)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    store = Store.open(tmp_path / "s.db")
    with store:
        chain.run_chain(reviewer, cfg, cfg.defaults, b"p", tmp_path, store,
                        tmp_path, "t")
    assert seen == [None]


def test_a_token_set_between_chain_entries_stops_the_chain(tmp_path, monkeypatch):
    """Checked at the ENTRY boundary too, not only inside the watchdog.

    An entry that classifies `unavailable` never reaches the watchdog on the
    NEXT entry until its binary check and its cache lookup have run; a
    cancellation arriving in that window must not buy a whole further fallback
    attempt.
    """
    from skodun import runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.runner import ReviewCancelled
    from skodun.store import Store

    token = threading.Event()
    calls = []

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(cmd)
        token.set()          # a supersede landing as this attempt finishes
        # `unavailable` output, so the chain would otherwise advance.
        err.write_bytes(b"quota exceeded")
        return runner.RunResult(rc=1, timed_out=False, duration_sec=0.1,
                                first_output_sec=None)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("second",))
    second = Reviewer(name="second", provider="xai", model="m2", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, second))
    store = Store.open(tmp_path / "s.db")
    with store:
        with pytest.raises(ReviewCancelled):
            chain.run_chain(head, cfg, cfg.defaults, b"p", tmp_path, store,
                            tmp_path, "t", cancel=token)
    assert len(calls) == 1, "the chain advanced past a cancellation"
