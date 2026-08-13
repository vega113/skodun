"""Runtime budgets: how long one review may legitimately take.

Two numbers, one shape. `worst_runtime` is the age at which a `running` record
is swept; `lock_stale_ceiling` is the age at which a HELD foreground lock may be
taken from its owner. The second is deliberately the wider of the two (the extra
passes run inside the lock), and both scale by the same two multipliers: the
configured chain width, and the batch count of a batched review.

Every number here is asserted against the arithmetic spelled out, never against
a magic constant, so a change to the formula fails the test that names the term
it dropped: the retry term, the grace term, the chain width, the batch count.
`tests/test_pipeline.py` and `tests/test_fallback.py` pin the shipped
`pipeline.*_sec` wrappers at `batch_count=0`; the delegation test below is what
keeps those two spellings from becoming two implementations.
"""

from __future__ import annotations

import pytest

from skodun import budget, pipeline, promptbuild
from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
from skodun.config import Defaults, Reviewer

#: Nonzero on BOTH retry axes on purpose: a formula that dropped the
#: `(1 + timeout_retries + degraded_retries)` factor is indistinguishable from
#: the real one on the zero-retry default.
RETRIES = Defaults(timeout_sec=100, timeout_retries=2, degraded_retries=3)


def test_worst_runtime_is_the_shipped_shape_with_nonzero_retries():
    # 2x the timeout per attempt (the watchdog's SIGTERM grace, plus the
    # oracle's doubling for a wedged attempt) x every attempt the two retry
    # axes permit, plus the sweep grace.
    assert budget.attempt_budget(RETRIES) == 2 * 100 * (1 + 2 + 3)
    assert budget.worst_runtime(RETRIES, 1, 0) == 2 * 100 * (1 + 2 + 3) + 60


def test_worst_runtime_carries_the_stale_record_grace():
    """The grace is a term of the budget, not slack someone added twice."""
    assert budget.GRACE_SEC == 60
    assert (budget.worst_runtime(RETRIES, 1, 0)
            - budget.attempt_budget(RETRIES)) == budget.GRACE_SEC
    # ...and it is there for a config with no retries at all, which is the one
    # the shipped pipeline tests pin (2*1 + 60 = 62).
    zero = Defaults(timeout_sec=1, timeout_retries=0, degraded_retries=0)
    assert budget.worst_runtime(zero, 1, 0) == 62


def test_the_batch_count_multiplies_the_whole_budget():
    """`batch_count + 1` calls: every batch, plus the integration pass."""
    one = budget.worst_runtime(RETRIES, 1, 0)
    assert budget.worst_runtime(RETRIES, 1, 1) == 2 * one
    assert budget.worst_runtime(RETRIES, 1, 3) == 4 * one
    assert budget.worst_runtime(RETRIES, 1, 40) == 41 * one


def test_chain_width_and_batch_count_compose():
    one = budget.worst_runtime(RETRIES, 1, 0)
    assert budget.worst_runtime(RETRIES, 4, 0) == 4 * one
    assert budget.worst_runtime(RETRIES, 4, 3) == 4 * 4 * one


def test_the_lock_ceiling_is_the_wider_formula_and_scales_identically():
    ceiling = budget.lock_stale_ceiling(RETRIES, 1, 0)
    assert ceiling == (budget.MAX_PASSES_UNDER_LOCK
                       * budget.attempt_budget(RETRIES) + budget.GRACE_SEC)
    assert budget.MAX_PASSES_UNDER_LOCK == 3
    assert ceiling > budget.worst_runtime(RETRIES, 1, 0)
    assert budget.lock_stale_ceiling(RETRIES, 1, 3) == 4 * ceiling
    assert budget.lock_stale_ceiling(RETRIES, 4, 3) == 16 * ceiling


@pytest.mark.parametrize("width", [1, 2, 4])
def test_the_shipped_pipeline_names_delegate_at_batch_count_zero(width):
    """ONE implementation behind two spellings.

    The shipped names are what `test_pipeline.py` / `test_fallback.py` import
    and what an unbatched review still uses; a second copy of the arithmetic
    behind them could only drift.
    """
    for d in (RETRIES, Defaults()):
        assert pipeline.worst_runtime_sec(d, max_chain_width=width) == \
            budget.worst_runtime(d, width, 0)
        assert pipeline.lock_stale_ceiling_sec(d, max_chain_width=width) == \
            budget.lock_stale_ceiling(d, width, 0)
    # The shipped module constants keep naming the same two numbers.
    assert pipeline.STALE_RECORD_GRACE_SEC == budget.GRACE_SEC
    assert pipeline._MAX_PASSES_UNDER_LOCK == budget.MAX_PASSES_UNDER_LOCK


def test_the_default_arguments_are_the_unbatched_single_entry_budget():
    d = Defaults()
    assert budget.worst_runtime(d) == pipeline.worst_runtime_sec(d)
    assert budget.lock_stale_ceiling(d) == pipeline.lock_stale_ceiling_sec(d)


@pytest.mark.parametrize("width,count", [(0, 0), (-4, 0), (1, -5), (0, -1)])
def test_degenerate_arguments_clamp_UP_never_down(width, count):
    """A computed width/count can legitimately arrive at zero or below.

    Clamping to the single-entry, unbatched budget is the fail-safe direction:
    the only thing riding on these numbers is never reclaiming a live holder's
    lock, so a too-SMALL budget is the one error with a cost.
    """
    assert budget.worst_runtime(RETRIES, width, count) == \
        budget.worst_runtime(RETRIES, 1, 0)
    assert budget.lock_stale_ceiling(RETRIES, width, count) == \
        budget.lock_stale_ceiling(RETRIES, 1, 0)


def test_more_batches_never_shrink_either_budget():
    prev_run = prev_lock = 0
    for n in range(0, 12):
        run = budget.worst_runtime(RETRIES, 2, n)
        lock = budget.lock_stale_ceiling(RETRIES, 2, n)
        assert run > prev_run and lock > prev_lock
        assert isinstance(run, int) and isinstance(lock, int)
        prev_run, prev_lock = run, lock


# ==========================================================================
# the PROMPT budget: how many bytes this reviewer's prompt envelope may hold
# ==========================================================================
#
# `[defaults] max_diff_bytes` alone is one global envelope, so it has to be
# fitted to the LEAST capable provider configured -- and the mismatch used to be
# discovered only at invocation, where `agy.build_cmd` raises. `prompt_budget`
# is the ONE definition of "the envelope for this reviewer": the operator's
# number (per-entry override, else the global) capped by what the head adapter
# can physically accept.

#: The three heads these tests contrast. `google`/agy carries the prompt in one
#: argv word and so declares a ceiling; `openai`/codex feeds it on stdin and
#: `xai`/grok passes `--prompt-file`, so neither has one.
AGY = Reviewer(name="a", provider="google", model="m", role="finder")
CODEX = Reviewer(name="c", provider="openai", model="m", role="finder")
GROK = Reviewer(name="g", provider="xai", model="m", role="finder")


def test_with_no_reviewer_the_budget_is_the_global_envelope():
    """The shipped behaviour, unchanged: no reviewer, no ceiling to apply."""
    d = Defaults(max_diff_bytes=400_000)
    assert budget.prompt_budget(d, None) == 400_000


def test_a_file_fed_provider_keeps_the_whole_global_envelope():
    """This is the point of the change: codex/grok must NOT be shrunk to fit
    agy. Their prompts travel as files and have no argv ceiling at all."""
    d = Defaults(max_diff_bytes=400_000)
    assert budget.prompt_budget(d, CODEX) == 400_000
    assert budget.prompt_budget(d, GROK) == 400_000


def test_the_per_reviewer_override_replaces_the_global():
    """Both directions: an entry may be budgeted smaller OR larger than the
    global without moving it for everyone else."""
    d = Defaults(max_diff_bytes=100_000)
    assert budget.prompt_budget(d, Reviewer(
        name="c", provider="openai", model="m", max_diff_bytes=400_000)) \
        == 400_000
    assert budget.prompt_budget(d, Reviewer(
        name="c", provider="openai", model="m", max_diff_bytes=20_000)) \
        == 20_000


def test_an_argv_bound_provider_is_capped_by_its_own_ceiling():
    """The operator asks for 400_000; the kernel will not carry it.

    The reservation is what the prompt spends OUTSIDE the envelope -- the
    reviewer instructions, the response contract, the identity block and the
    injected repo rules -- so the envelope has to be the ceiling MINUS it, not
    the ceiling.
    """
    d = Defaults(max_diff_bytes=400_000)
    assert budget.prompt_budget(d, AGY) == \
        MAX_PROMPT_ARG_BYTES - budget.PROMPT_OVERHEAD_BYTES
    assert budget.prompt_budget(d, AGY) < 400_000


def test_the_ceiling_never_RAISES_a_smaller_configured_budget():
    """`min`, not "the adapter decides". A deliberately tight envelope stays
    tight -- an operator who lowered it did so for a reason the adapter has no
    view on (cost, latency, a model's own attention)."""
    d = Defaults(max_diff_bytes=4_000)
    assert budget.prompt_budget(d, AGY) == 4_000
    assert budget.prompt_budget(d, Reviewer(
        name="a", provider="google", model="m", max_diff_bytes=7_000)) == 7_000


def test_a_per_reviewer_override_is_still_capped_by_the_adapter():
    """A ceiling is physics, an override is a wish. The wish does not win."""
    d = Defaults(max_diff_bytes=1_000)
    r = Reviewer(name="a", provider="google", model="m",
                 max_diff_bytes=10_000_000)
    assert budget.prompt_budget(d, r) == \
        MAX_PROMPT_ARG_BYTES - budget.PROMPT_OVERHEAD_BYTES


def test_the_budget_is_never_below_one(monkeypatch):
    """`promptbuild.build` refuses a non-positive envelope, and rightly: a zero
    envelope ships a prompt with no diff in it, which reads to the model as
    "nothing changed". A ceiling narrower than the reservation must not be able
    to compute one -- such a provider simply cannot review anything, and part 4
    is what turns that into a fallback rather than a crash."""
    from skodun.adapters import _REGISTRY

    class _TinyAdapter:
        name = "tiny"
        provider = "tiny"
        def prompt_limit(self):
            return 1

    monkeypatch.setitem(_REGISTRY, "tiny", _TinyAdapter)
    d = Defaults(max_diff_bytes=400_000)
    got = budget.prompt_budget(d, Reviewer(name="t", provider="tiny", model="m"))
    assert got == 1
    # ...and it is a value `promptbuild.build` accepts rather than raises on.
    promptbuild.build("b", "r", "s", "h", b"x", got, None, None)


def test_an_unresolvable_provider_falls_back_to_the_configured_number():
    """A provider with no adapter is a CONFIG error, and it already has an
    owner: `pipeline._run_review`'s preflight refuses the run before anything
    is locked or persisted. Raising a second time from an arithmetic helper
    would only move that error somewhere less informative, so this degrades to
    exactly the pre-change behaviour."""
    d = Defaults(max_diff_bytes=400_000)
    assert budget.prompt_budget(
        d, Reviewer(name="x", provider="nope", model="m")) == 400_000


def test_the_reservation_covers_the_prompt_bytes_the_envelope_does_not():
    """The reservation is composed from the real constants, not guessed.

    Anything `promptbuild.build` emits outside the diff+context envelope has to
    fit inside it: the instructions, the response contract, the section markers
    and the whole repo-rules injection budget.
    """
    from skodun import checklist
    fixed = (len(promptbuild._INTRO) + len(promptbuild._CONTEXT_INSTRUCTIONS)
             + len(promptbuild._RESPONSE_CONTRACT)
             + len(promptbuild.RULES_BEGIN) + len(promptbuild.RULES_END)
             + len(promptbuild.DIFF_BEGIN) + len(promptbuild.DIFF_END)
             + len(promptbuild.DIFF_TRUNCATED))
    assert budget.PROMPT_OVERHEAD_BYTES >= fixed + checklist.BUDGET
    # ...and it leaves room for the identity block, whose length depends on the
    # branch name and the two refs.
    assert budget.PROMPT_OVERHEAD_BYTES - (fixed + checklist.BUDGET) >= 256


def test_the_reservation_covers_the_maximum_stack_and_lineage_prompt_blocks():
    from skodun import fingerprint, stack

    assert budget.PROMPT_OVERHEAD_BYTES >= (
        stack.MAX_STACK_PROMPT_BYTES + fingerprint.MAX_LINEAGE_PROMPT_BYTES)


def test_a_full_prompt_with_max_advisory_context_fits_the_declared_ceiling():
    """Stack/lineage blocks sit outside the diff envelope; the reservation
    must still keep argv-bound adapters under their declared ceiling."""
    from skodun.checklist import BUDGET, Selection
    from skodun import fingerprint, stack

    d = Defaults(max_diff_bytes=400_000)
    envelope = budget.prompt_budget(d, AGY)
    selection = Selection(sections=("core",), bytes_total=BUDGET,
                          over_budget=False, body="r" * BUDGET)
    prompt = promptbuild.build(
        "a-fairly-long-feature-branch-name", "origin/main", "0" * 40,
        "1" * 40 + " (working tree)", b"d" * envelope, envelope, selection,
        b"", stack_context=b"S" * stack.MAX_STACK_PROMPT_BYTES,
        lineage_context=b"L" * fingerprint.MAX_LINEAGE_PROMPT_BYTES)
    assert prompt.prompt_bytes <= MAX_PROMPT_ARG_BYTES


def test_a_full_prompt_at_the_budget_fits_the_declared_ceiling():
    """THE property the reservation exists for, asserted end to end.

    A prompt built at exactly this reviewer's budget -- envelope full, repo
    rules at their whole injection budget -- must still fit the ceiling the
    adapter declared. If it does not, the planner is cutting batches the
    provider will refuse and the reservation is too small.
    """
    from skodun.checklist import BUDGET, Selection
    d = Defaults(max_diff_bytes=400_000)
    envelope = budget.prompt_budget(d, AGY)
    selection = Selection(sections=("core",), bytes_total=BUDGET,
                          over_budget=False, body="r" * BUDGET)
    prompt = promptbuild.build(
        "a-fairly-long-feature-branch-name", "origin/main", "0" * 40,
        "1" * 40 + " (working tree)", b"d" * envelope, envelope, selection,
        b"")
    assert prompt.prompt_bytes <= MAX_PROMPT_ARG_BYTES
