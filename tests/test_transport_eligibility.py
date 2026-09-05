"""Actual prompt eligibility through the shipped chain, with no live providers."""

import threading

import pytest

from skodun import capacity, chain, runner
from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
from skodun.config import Config, Defaults, Reviewer, quota_pool_for
from skodun.store import Store


CLEAN = (b'{"structuredOutput":{"summary":"s","findings":[]},'
         b'"stopReason":"EndTurn"}')


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_AGY_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_ADMISSION_WAIT_SECONDS", "0")
    admissions, launches = [], []
    acquire = chain._acquire_provider_slot

    def tracked_admission(store, provider, **kwargs):
        admissions.append(provider)
        return acquire(store, provider, **kwargs)

    def answer(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        launches.append(cmd)
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=0.1,
                                first_output_sec=0.05)

    monkeypatch.setattr(chain, "_acquire_provider_slot", tracked_admission)
    monkeypatch.setattr(chain.runner, "run_with_watchdog", answer)
    with Store.open(tmp_path / "store.db") as store:
        def run(prompt, reviewers=None, **kwargs):
            reviewers = reviewers or (Reviewer(
                name="google", provider="google", model="m", role="finder"),)
            cfg = Config(defaults=Defaults(), reviewers=reviewers)
            return chain.run_chain(reviewers[0], cfg, cfg.defaults, prompt,
                                   tmp_path, store, tmp_path, "transport", **kwargs)
        yield run, admissions, launches, store


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("unit", [b"x", "é".encode(), b"\r\n"])
def test_actual_byte_boundary_precedes_admission(transport, tmp_path, delta, unit):
    run, admissions, launches, _ = transport
    size = MAX_PROMPT_ARG_BYTES + delta
    prompt = unit * (size // len(unit)) + b"x" * (size % len(unit))
    outcome = run(prompt)
    if delta <= 0:
        assert admissions == ["google"] and len(launches) == 1
        assert outcome.parsed is not None and outcome.parsed.parse_ok
        # The argv carries the exact input; universal-newline decoding must
        # not silently shrink CRLF evidence or the size used for admission.
        assert launches[0][launches[0].index("--print") + 1].encode() == prompt
    else:
        assert admissions == [] and launches == []
        assert list(tmp_path.glob("*.prompt.txt")) == []
        row, = outcome.attempts
        assert row["classification"]["category"] == "prompt_size"
        assert row["input_eligibility"] == {
            "adapter_name": "agy", "transport": "argv",
            "capability_version": "agy-argv-v1", "reason": "prompt_too_large",
            "input_bytes": size, "limit_bytes": MAX_PROMPT_ARG_BYTES,
        }
        assert "capacity_timing" not in row
        assert "execution_provenance" not in row
        assert row["rc"] is None and row["timed_out"] is None
        assert outcome.parsed is None and outcome.accepted is None


@pytest.mark.parametrize("capable", [False, True])
def test_silent_timeout_then_oversized_busy_fallback(transport, monkeypatch, capable):
    run, admissions, launches, _ = transport
    acquire = chain._acquire_provider_slot

    def busy_google(store, provider, **kwargs):
        if provider == "google":
            admissions.append(provider)
            raise capacity.AdmissionTimeout("synthetically busy Google slot")
        return acquire(store, provider, **kwargs)

    answer = chain.runner.run_with_watchdog

    def silent_first(cmd, *args, **kwargs):
        if not launches:
            launches.append(cmd)
            return runner.RunResult(rc=124, timed_out=True, duration_sec=420,
                                    first_output_sec=None)
        return answer(cmd, *args, **kwargs)

    monkeypatch.setattr(chain, "_acquire_provider_slot", busy_google)
    monkeypatch.setattr(chain.runner, "run_with_watchdog", silent_first)
    head = Reviewer(name="primary", provider="xai", model="m", role="finder",
                    fallbacks=("small", "capable") if capable else ("small",))
    small = Reviewer(name="small", provider="google", model="m", role="finder")
    backup = Reviewer(name="capable", provider="xai", model="m2", role="finder")
    outcome = run(b"x" * (MAX_PROMPT_ARG_BYTES + 1),
                  (head, small, backup) if capable else (head, small))
    assert admissions == (["xai", "xai"] if capable else ["xai"])
    assert len(launches) == (2 if capable else 1)
    primary, declined, *rest = outcome.attempts
    assert primary["timed_out"] is True and primary["duration_sec"] == 420
    assert declined["classification"]["category"] == "prompt_size"
    assert declined["input_eligibility"]["input_bytes"] == MAX_PROMPT_ARG_BYTES + 1
    assert declined["rc"] is None and "capacity_timing" not in declined
    if capable:
        assert outcome.accepted["model"] == "m2"
    else:
        assert outcome.parsed is None and outcome.accepted is None
        assert "primary/xai: timed out with no output" in outcome.failure_reason
        assert str(MAX_PROMPT_ARG_BYTES + 1) in outcome.failure_reason
        assert str(MAX_PROMPT_ARG_BYTES) in outcome.failure_reason


@pytest.mark.parametrize("bad, effort, message", [
    (b"x", "max", "effort"),
    (b"\xff", None, "UTF-8"),
    (b"\x00", None, "NUL"),
])
def test_fatal_errors_precede_size_and_admission(transport, bad, effort, message):
    run, admissions, launches, _ = transport
    head = Reviewer(name="head", provider="google", model="m", role="finder",
                    effort=effort, fallbacks=("backup",))
    backup = Reviewer(name="backup", provider="xai", model="m", role="finder")
    outcome = run(bad + b"x" * MAX_PROMPT_ARG_BYTES, (head, backup))
    assert admissions == [] and launches == []
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0]["classification"] is None
    assert "could not be invoked" in outcome.failure_reason
    assert message in outcome.failure_reason


def test_unchanged_retries_and_smaller_input_do_not_poison_provider(transport):
    run, admissions, launches, store = transport
    oversized = b"x" * (MAX_PROMPT_ARG_BYTES + 1)
    for _ in range(2):
        assert run(oversized).attempts[0]["classification"]["category"] == "prompt_size"
    assert admissions == [] and launches == []
    reviewer = Reviewer(name="google", provider="google", model="m", role="finder")
    assert chain._cached_unavailable(store, "google", quota_pool_for(reviewer)) is None
    assert run(b"smaller").parsed.parse_ok
    assert admissions == ["google"] and len(launches) == 1


def test_cancel_before_eligibility_does_no_work(transport):
    run, admissions, launches, _ = transport
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(runner.ReviewCancelled):
        run(b"x" * (MAX_PROMPT_ARG_BYTES + 1), cancel=cancel)
    assert admissions == [] and launches == []
