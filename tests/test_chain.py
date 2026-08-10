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
import time

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


def test_run_chain_does_not_hop_on_non_spawn_oserror(tmp_path, monkeypatch):
    """Watchdog I/O errors are not evidence that a provider was unavailable."""
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="google", model="m2",
                      role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("stdout close failed")

    calls = []
    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    with store, pytest.raises(OSError, match="stdout close failed"):
        chain.run_chain(head, cfg, cfg.defaults, b"p", tmp_path, store,
                        tmp_path, "t")
    assert len(calls) == 1


def test_run_chain_does_not_hop_on_missing_working_directory(tmp_path,
                                                              monkeypatch):
    """A Popen ENOENT naming cwd is local setup failure, not binary absence."""
    import errno
    from skodun.config import Config, Defaults, Reviewer
    from skodun.runner import SpawnError
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="google", model="m2",
                      role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    missing_cwd = tmp_path / "missing-cwd"

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        raise SpawnError(
            OSError(errno.ENOENT, "No such file or directory", str(cwd)),
            cmd=cmd, cwd=cwd)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    with store, pytest.raises(SpawnError, match="No such file"):
        chain.run_chain(head, cfg, cfg.defaults, b"p", missing_cwd, store,
                        tmp_path, "t")


def test_run_chain_does_not_hop_on_host_resource_spawn_error(tmp_path,
                                                              monkeypatch):
    """EMFILE/ENOMEM-style spawn failures remain fatal local errors."""
    import errno
    from skodun.config import Config, Defaults, Reviewer
    from skodun.runner import SpawnError
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="google", model="m2",
                      role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")

    def fake(*args, **kwargs):
        raise SpawnError(OSError(errno.EMFILE, "Too many open files"))

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    with store, pytest.raises(SpawnError, match="Too many open files"):
        chain.run_chain(head, cfg, cfg.defaults, b"p", tmp_path, store,
                        tmp_path, "t")


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


# --------------------------------------------------------------------------
# a prompt over an adapter's ceiling: `unavailable`, and the chain ADVANCES
# --------------------------------------------------------------------------
#
# A fallback chain exists precisely for "this reviewer cannot do it", and a
# prompt that does not fit is that case. It used to be FATAL -- `build_cmd`
# raised, `run_chain` returned, and an `agy`-headed chain with a `codex`
# fallback died on a large change the fallback could have reviewed.
#
# Fail-closed is unchanged and is what these tests pin alongside it: an
# exhausted chain is still a failure, the reason still names the size and the
# ceiling, and the OTHER `build_cmd` failures -- an effort the CLI cannot
# express, an unwritable sidecar -- stay fatal, because those are config errors
# rather than "this provider cannot take this prompt".

GROK_CLEAN = (b'{"structuredOutput": {"summary": "s", "findings": []},'
              b' "stopReason": "EndTurn"}')


def _too_large_for_agy() -> bytes:
    from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
    return b"x" * (MAX_PROMPT_ARG_BYTES + 1)


def _answering(monkeypatch, calls: list):
    """Patch the watchdog with a fake that answers every spawn cleanly."""
    from skodun import runner

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(cmd[0])
        out.write_bytes(GROK_CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.1,
                                first_output_sec=0.05)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)


def _pin_bins(monkeypatch):
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_AGY_BIN", "/bin/sh")


def test_a_prompt_over_the_ceiling_advances_to_the_fallback(tmp_path, monkeypatch):
    """THE defect: the head cannot carry this prompt, so the fallback reviews it.

    `agy` has no prompt-file flag and ignores stdin, so its prompt must fit one
    argv word. That is a statement about this provider, not about the change --
    exactly what `unavailable` means -- and the entry beside it can answer.
    """
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    _pin_bins(monkeypatch)
    calls: list[str] = []
    _answering(monkeypatch, calls)
    head = Reviewer(name="f", provider="google", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="xai", model="m2", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    with store:
        out = chain.run_chain(head, cfg, cfg.defaults, _too_large_for_agy(),
                              tmp_path, store, tmp_path, "t")

    assert out.parsed is not None and out.parsed.parse_ok is True
    assert out.accepted["provider"] == "xai"
    assert len(calls) == 1, "the head must not have spawned anything"
    head_row, backup_row = out.attempts
    assert head_row["provider"] == "google"
    assert head_row["classification"]["kind"] == "unavailable"
    # NOT `quota`: that is the one category cached provider-wide, and caching
    # this would take a healthy provider out of every later chain in the run
    # over a fact about ONE prompt.
    assert head_row["classification"]["category"] != "quota"
    assert head_row["rc"] is None and head_row["timed_out"] is None
    assert "too large" in head_row["skipped"]


def test_the_declined_entry_names_the_size_and_the_ceiling(tmp_path, monkeypatch):
    """A reason that says only "unavailable" sends the operator nowhere.

    Both numbers, on the row and in the exhausted chain's failure reason: the
    fix is either a smaller envelope or a different provider, and neither is
    choosable without knowing by how much.
    """
    from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    _pin_bins(monkeypatch)
    calls: list[str] = []
    _answering(monkeypatch, calls)
    prompt = _too_large_for_agy()
    only = Reviewer(name="f", provider="google", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(only,))
    store = Store.open(tmp_path / "s.db")
    with store:
        out = chain.run_chain(only, cfg, cfg.defaults, prompt, tmp_path, store,
                              tmp_path, "t")

    # FAIL-CLOSED: an exhausted chain is still a failure, never a pass.
    assert out.parsed is None and out.accepted is None
    assert calls == []
    assert "all providers unavailable" in out.failure_reason
    for number in (str(len(prompt)), str(MAX_PROMPT_ARG_BYTES)):
        assert number in out.failure_reason, out.failure_reason
        assert number in out.attempts[0]["skipped"]


def test_an_effort_the_cli_cannot_express_is_still_FATAL(tmp_path, monkeypatch):
    """The distinction that keeps the new path from swallowing config errors.

    An unmappable effort is not "this provider cannot take this prompt" -- it
    is a typo in the user's own config, and routing around it would review at
    some other provider's default effort and say nothing. It must still stop
    the chain, with the fallback untouched.
    """
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    _pin_bins(monkeypatch)
    calls: list[str] = []
    _answering(monkeypatch, calls)
    # `agy` has no `--effort max`: `build_cmd` raises a plain ValueError.
    head = Reviewer(name="f", provider="google", model="m", role="finder",
                    effort="max", fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="xai", model="m2", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    with store:
        out = chain.run_chain(head, cfg, cfg.defaults, b"small", tmp_path,
                              store, tmp_path, "t")

    assert out.parsed is None
    assert calls == [], "nothing may spawn"
    assert len(out.attempts) == 1, "the chain advanced past a config error"
    assert "could not be invoked" in out.failure_reason
    assert out.attempts[0]["classification"] is None


def test_a_non_utf8_prompt_is_still_FATAL(tmp_path, monkeypatch):
    """The other `agy` `build_cmd` refusal, and the same rule.

    A prompt that is not decodable text is a statement about the REPO (a
    latin-1 source file), not about a provider's capacity, and `agy` refuses it
    rather than review a lossy re-decode. Hopping providers on it would review
    the same bytes somewhere else and hide the problem.
    """
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    _pin_bins(monkeypatch)
    calls: list[str] = []
    _answering(monkeypatch, calls)
    head = Reviewer(name="f", provider="google", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="xai", model="m2", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    with store:
        out = chain.run_chain(head, cfg, cfg.defaults, b"\xff\xfe not utf-8",
                              tmp_path, store, tmp_path, "t")

    assert out.parsed is None and calls == []
    assert len(out.attempts) == 1
    assert "could not be invoked" in out.failure_reason


# --------------------------------------------------------------------------
# S4 Phase B — provider:<id> slot around inference
# --------------------------------------------------------------------------


def test_s4_provider_slot_held_during_inference_and_released(tmp_path,
                                                             monkeypatch):
    """Acquire provider:<id> before watchdog; release after the entry ends."""
    from skodun import capacity, runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_ADMISSION_WAIT_SECONDS", "2")
    holders_during = []
    store = Store.open(tmp_path / "s.db")

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        n = store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai")
        holders_during.append(n)
        out.write_bytes(GROK_CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.05,
                                first_output_sec=0.01)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    with store:
        out = chain.run_chain(
            reviewer, cfg, cfg.defaults, b"p", tmp_path, store, tmp_path, "t")
        assert out.accepted is not None
        assert holders_during == [1]
        assert store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai") == 0


def test_s4_quota_releases_slot_and_hops_to_fallback(tmp_path, monkeypatch):
    """Quota on head → mark unavailable, release slot, hop succeeds."""
    from skodun import capacity, runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_AGY_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_ADMISSION_WAIT_SECONDS", "2")
    calls = []

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(list(cmd))
        if len(calls) == 1:
            err.write_bytes(b"quota exceeded")
            return runner.RunResult(rc=1, timed_out=False, duration_sec=0.05,
                                    first_output_sec=None)
        out.write_bytes(GROK_CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.05,
                                first_output_sec=0.01)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="google", model="m2",
                      role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    with store:
        out = chain.run_chain(
            head, cfg, cfg.defaults, b"p", tmp_path, store, tmp_path, "t")
        assert out.accepted is not None
        assert out.accepted["provider"] == "google"
        assert store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai") == 0
        assert store.capacity_holder_count(
            capacity.provider_resource_class("google"), "google") == 0
        assert chain._effective_provider_capacity(store, "xai") == 0
        # Shipped path: _acquire_provider_slot must honor effective 0 via
        # capacity_fn — not a hand-built capacity=0 acquire.
        with pytest.raises(capacity.AdmissionTimeout):
            chain._acquire_provider_slot(
                store, "xai", wait_sec=0.05, cancel=None, on_progress=None)
        assert store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai") == 0


def test_s4_post_quota_run_chain_does_not_hold_provider_slot(tmp_path,
                                                             monkeypatch):
    """After mark_provider_unavailable, a new run_chain must not hold provider:id.

    Drives the real skip + effective-capacity path: cache skip avoids inference,
    and a direct _acquire_provider_slot after the mark still cannot admit.
    """
    from skodun import capacity, runner
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store
    from skodun.store import _TS_FORMAT
    import time as time_mod

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_ADMISSION_WAIT_SECONDS", "1")
    spawned = []

    def fake(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        spawned.append(1)
        out.write_bytes(GROK_CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.01,
                                first_output_sec=0.01)

    monkeypatch.setattr(chain.runner, "run_with_watchdog", fake)
    reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    store = Store.open(tmp_path / "s.db")
    until = time_mod.strftime(
        _TS_FORMAT, time_mod.gmtime(time_mod.time() + 1800))
    with store:
        store.mark_provider_unavailable(
            "xai", "rate limited", "quota", until)
        out = chain.run_chain(
            reviewer, cfg, cfg.defaults, b"p", tmp_path, store, tmp_path, "t")
        assert out.accepted is None
        assert spawned == [], "inference must not start on a quota-backed-off provider"
        assert store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai") == 0
        # Real admit path after mark (bypass skip by calling acquire helper):
        with pytest.raises(capacity.AdmissionTimeout):
            chain._acquire_provider_slot(
                store, "xai", wait_sec=0.05, cancel=None, on_progress=None)
        assert store.capacity_holder_count(
            capacity.provider_resource_class("xai"), "xai") == 0


def test_s4_provider_hops_share_one_admission_deadline(tmp_path, monkeypatch):
    """Provider waits/hops use remaining shared budget — not a full reset each hop."""
    from skodun import capacity
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_AGY_BIN", "/bin/sh")
    waits: list[float] = []
    real = capacity.acquire

    def spy(store, *, scope, resource_class, wait_sec, **kwargs):
        waits.append(float(wait_sec))
        if len(waits) == 1:
            # Consume part of the shared budget, then fail so the chain hops.
            time.sleep(0.08)
            raise capacity.AdmissionTimeout(
                f"gave up after {wait_sec:g}s waiting for {resource_class}")
        # Second hop: still fail; we only need wait_sec remaining.
        raise capacity.AdmissionTimeout(
            f"gave up after {wait_sec:g}s waiting for {resource_class}")

    monkeypatch.setattr(capacity, "acquire", spy)
    head = Reviewer(name="f", provider="xai", model="m", role="finder",
                    fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="google", model="m2",
                      role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(head, backup))
    store = Store.open(tmp_path / "s.db")
    budget = 0.25
    deadline = time.monotonic() + budget
    with store:
        chain.run_chain(
            head, cfg, cfg.defaults, b"p", tmp_path, store, tmp_path, "t",
            admission_deadline=deadline)
    assert len(waits) == 2, waits
    assert waits[0] <= budget + 0.05
    # Second hop must see a smaller remaining budget (shared deadline).
    assert waits[1] < waits[0]
    assert waits[1] <= waits[0] - 0.05


def test_s4_cancel_during_provider_wait_releases_ticket(tmp_path, monkeypatch):
    """Cancel while waiting on a full provider slot does not leak a holder."""
    from skodun import capacity
    from skodun.config import Config, Defaults, Reviewer
    from skodun.runner import ReviewCancelled
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_ADMISSION_WAIT_SECONDS", "2")
    store = Store.open(tmp_path / "s.db")
    rc = capacity.provider_resource_class("xai")
    with store:
        holder = capacity.acquire(
            store, scope="xai", resource_class=rc, capacity=1,
            wait_sec=0.5, poll_sec=0.01)
        assert holder.status == capacity.STATUS_RUNNING

        token = threading.Event()
        token.set()
        reviewer = Reviewer(name="f", provider="xai", model="m", role="finder")
        cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
        with pytest.raises(ReviewCancelled):
            chain.run_chain(
                reviewer, cfg, cfg.defaults, b"p", tmp_path, store,
                tmp_path, "t", cancel=token)
        assert store.capacity_holder_count(rc, "xai") == 1
        capacity.finish(store, holder, status=capacity.STATUS_RELEASED)


def test_a_too_large_prompt_is_never_cached_against_the_provider(tmp_path,
                                                                 monkeypatch):
    """One oversized prompt must not black-hole a provider for the TTL.

    `quota` is the only provider-wide-cacheable category precisely because it
    is the only one that is a property of the provider rather than of this
    attempt. A size refusal is a property of THIS PROMPT, and the same provider
    will happily take the next, smaller one.
    """
    from skodun.config import Config, Defaults, Reviewer
    from skodun.store import Store

    _pin_bins(monkeypatch)
    calls: list[str] = []
    _answering(monkeypatch, calls)
    only = Reviewer(name="f", provider="google", model="m", role="finder")
    cfg = Config(defaults=Defaults(), reviewers=(only,))
    store = Store.open(tmp_path / "s.db")
    with store:
        chain.run_chain(only, cfg, cfg.defaults, _too_large_for_agy(),
                        tmp_path, store, tmp_path, "t")
        # A second, small prompt against the SAME provider must run.
        out = chain.run_chain(only, cfg, cfg.defaults, b"small", tmp_path,
                              store, tmp_path, "t2")

    assert out.parsed is not None and out.parsed.parse_ok is True
    assert len(calls) == 1
