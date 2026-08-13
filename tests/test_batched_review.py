"""Batched review: an over-budget diff reviewed in pieces, recorded ONCE.

A diff bigger than one prompt's envelope used to fail closed as
`diff_truncated` -- unreviewable, and therefore ungateable. These tests drive
the real orchestrator against a real git repo and a real child process (only
the model CLI is faked, exactly as in `test_pipeline.py`) and pin the three
properties the whole arc exists for:

  * **One artifact at the FULL identity.** N batch sub-reviews plus one
    cross-file integration pass produce exactly ONE record, keyed to the whole
    diff's hash, with no per-batch rows, banners or deliveries.
  * **Aggregation demotes.** A single failed, degraded or truncated sub-review
    (or a failed integration pass) makes the whole aggregate untrustworthy, so
    the gate answers 2 rather than reading a partial pass as an all-clear.
  * **The lock survives the longer run.** A batched holder legitimately runs
    for `batch_count + 1` reviewer budgets, so it publishes that budget in a
    `<lock>/budget` sidecar and a small-diff waiter may not reclaim it.

The isolation rules are `test_pipeline.py`'s and are not optional: `SKODUN_DB`,
`SKODUN_CONFIG` and every `SKODUN_<X>_BIN` are pinned inside `tmp_path`, so no
test can reach the developer's own store, config or CLI.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import pytest

from skodun import (batching, budget, capacity, chain, checklist, contextpack, gitio,
                    passes, pipeline, promptbuild, runner, services, trust)
from skodun.adapters import ParseResult
from skodun.cli import main
from skodun.config import Defaults, load_config
from skodun.gate import run_gate
from skodun.gitio import capture_diff, diff_identity, git_common_dir, resolve_base
from skodun.pipeline import LockTimeout
from skodun.store import Store
from skodun.triage import load_valid_artifact
from tests.conftest import oracle_dir
from tests.test_gitio import _git, _mkrepo
from tests.test_pipeline import (CANCELLED, CFG, CLEAN, DIRTY, _calls, _emit,
                                 _fake_grok, _per_call, _repo, _run, _running,
                                 _store, _write_owner)

ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None
requires_oracle = pytest.mark.skipif(
    ORACLE is None or not ORACLE.exists(),
    reason="oracle checkout not present (set SKODUN_ORACLE_DIR)")

#: A second abnormal `stopReason`, so "first abnormal" and "last abnormal" are
#: distinguishable values rather than the same word twice.
TOKENS = json.dumps({"structuredOutput": {"summary": "s", "findings": []},
                     "stopReason": "MaxOutputTokens"})
#: Output no adapter can parse: the integration pass RAN and answered nothing.
GARBAGE = "printf 'not a review\\n'"

#: A small envelope budget so a handful of ordinary files is "over budget", and
#: no retries so the fake CLI's `$CALL` numbering is the sub-review numbering.
BATCH_CFG = """
[defaults]
max_diff_bytes = 4000
timeout_retries = 0
degraded_retries = 0
"""


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "store" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    # Pinned even though no test installs one: an `integrator` reviewer on a
    # second provider must find NOTHING rather than the developer's own CLI.
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "bin" / "no-such-codex"))
    monkeypatch.setenv("SKODUN_AGY_BIN", str(tmp_path / "bin" / "no-such-agy"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    monkeypatch.setattr(runner, "_TERM_GRACE_SEC", 0.25)


# --------------------------------------------------------------------------
# repos whose outgoing diff is over the envelope
# --------------------------------------------------------------------------


def _body(tag: str, lines: int = 90) -> str:
    return "".join(f"{tag} line {j:04d}\n" for j in range(lines))


def _oversized(tmp_path: Path, extra_cfg: str = "", files: int = 3) -> Path:
    """A repo whose outgoing diff needs several batches (one file each)."""
    repo = _repo(tmp_path, BATCH_CFG + extra_cfg)
    for i in range(files):
        (repo / f"f{i}.txt").write_text(_body(f"f{i}"), encoding="utf-8")
    return repo


def _sole_batch_repo(tmp_path: Path, extra_cfg: str = "") -> Path:
    """A repo whose diff is ONE irreducible hunk -- the floor case.

    Everything but `a.txt` is committed on `main` before branching, so the
    outgoing diff is a single file section with a single oversized hunk: the one
    shape that cannot be split further, and therefore the one that yields
    exactly one batch.
    """
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG + BATCH_CFG + extra_cfg, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cfg")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("q" * 9000 + "\n", encoding="utf-8")
    return repo


def _plan(repo: Path, cfg) -> tuple:
    """The diff and the batch plan production will build from it."""
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    return diff, batching.split(diff.data, pipeline._batch_budget(cfg.defaults))


def _lock_of(repo: Path) -> Path:
    return git_common_dir(repo) / pipeline.LOCK_NAME


def _rules_block(prompt: Path) -> str:
    """Just the INJECTED repo-rules section of a prompt.

    Asserting over the whole prompt would be a false pass or a false failure
    either way: a checklist file is itself a file in the repo, so its text can
    legitimately appear in the reviewed diff as well.
    """
    text = prompt.read_text(encoding="utf-8")
    begin = text.find(promptbuild.RULES_BEGIN.decode())
    end = text.find(promptbuild.RULES_END.decode())
    assert begin >= 0 and end > begin, "the prompt has no repo-rules section"
    return text[begin:end]


# --------------------------------------------------------------------------
# the batch budget, and who enters the orchestrator at all
# --------------------------------------------------------------------------


def test_the_per_batch_diff_budget_halves_the_envelope_for_context_headroom():
    """A full-size batch would leave zero headroom, making batched context a
    no-op -- the oracle halves the batch budget when packing is on."""
    from skodun.config import Defaults
    on = Defaults(max_diff_bytes=4000, context_pack=True)
    off = Defaults(max_diff_bytes=4000, context_pack=False)
    assert pipeline._batch_budget(on) == 2000
    assert pipeline._batch_budget(off) == 4000
    # Never zero: `split` would then flag every unit as an irreducible floor.
    assert pipeline._batch_budget(Defaults(max_diff_bytes=1)) == 1


def _synthetic_diff(files: int, lines_per_file: int) -> bytes:
    """A well-formed multi-file unified diff, big enough to need splitting.

    Synthetic rather than captured from a repo because these tests are about
    the PLANNER's arithmetic at the shipped 400_000-byte envelope, and a git
    fixture that large would dominate their runtime for no extra coverage.
    """
    out = bytearray()
    for i in range(files):
        out += f"diff --git a/f{i}.txt b/f{i}.txt\n".encode()
        out += b"--- /dev/null\n"
        out += f"+++ b/f{i}.txt\n".encode()
        out += f"@@ -0,0 +1,{lines_per_file} @@\n".encode()
        for j in range(lines_per_file):
            out += f"+f{i} line {j:06d} {'x' * 60}\n".encode()
    return bytes(out)


def test_batches_are_sized_for_the_provider_that_will_actually_run_them(tmp_path):
    """THE defect: the planner used to size every batch from the global number.

    `agy` carries its prompt in one argv word and refuses anything over
    `MAX_PROMPT_ARG_BYTES`; `codex`/`grok` pass a file and have no such limit.
    At the shipped 400_000-byte envelope the planner cut batches ~200_000 bytes
    wide, and every one of them was refused by `build_cmd` — the mismatch was
    only ever discovered at invocation.
    """
    from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES, AgyAdapter
    from skodun.config import Reviewer

    d = Defaults(max_diff_bytes=400_000)
    agy = Reviewer(name="f", provider="google", model="m", role="finder")
    diff = _synthetic_diff(files=8, lines_per_file=1200)
    assert len(diff) > d.max_diff_bytes, "the fixture must need splitting"

    # The premise, stated rather than assumed: sized from the global alone,
    # the batches do not fit.
    blind = pipeline.batch_plan(diff, d)
    assert max(len(b.data) for b in blind) > MAX_PROMPT_ARG_BYTES

    plan = pipeline.batch_plan(diff, d, agy)
    envelope = budget.prompt_budget(d, agy)
    assert plan and len(plan) > len(blind)
    for i, b in enumerate(plan):
        prompt = promptbuild.build("br", "origin/main", "0" * 40, "1" * 40,
                                   b.data, envelope, None, None)
        assert not prompt.diff_truncated, f"batch {i} does not fit its own budget"
        pf = tmp_path / f"p{i}.txt"
        pf.write_bytes(prompt.text)
        # The proof: the provider the planner sized for accepts every batch.
        AgyAdapter().build_cmd(pf, agy, d, tmp_path)


def test_a_file_fed_head_is_not_shrunk_to_fit_an_argv_bound_one(tmp_path):
    """The other half: `codex` must keep the whole envelope.

    Fitting one global number to the least capable provider is exactly what
    this change exists to stop, so a provider with no ceiling must plan
    identically to a plan with no reviewer at all.
    """
    from skodun.config import Reviewer

    d = Defaults(max_diff_bytes=400_000)
    diff = _synthetic_diff(files=8, lines_per_file=1200)
    codex = Reviewer(name="f", provider="openai", model="m", role="finder")
    assert [b.data for b in pipeline.batch_plan(diff, d, codex)] == \
        [b.data for b in pipeline.batch_plan(diff, d)]


def test_the_batch_budget_is_the_reviewers_envelope_halved_not_the_globals():
    """`_batch_budget` reads the ONE definition, not `d.max_diff_bytes`."""
    from skodun.config import Reviewer

    d = Defaults(max_diff_bytes=400_000, context_pack=True)
    agy = Reviewer(name="f", provider="google", model="m", role="finder")
    assert pipeline._batch_budget(d, agy) == budget.prompt_budget(d, agy) // 2
    assert pipeline._batch_budget(d, agy) < pipeline._batch_budget(d)


def test_a_diff_that_fits_the_reviewers_envelope_is_still_never_batched():
    """The unbatched path stays the unbatched path: `None` up to the envelope.

    The threshold moves WITH the reviewer's budget rather than being abandoned
    — a diff over agy's tighter envelope now batches where it used to be
    truncated, and one under it still does not batch at all.
    """
    from skodun.config import Reviewer

    d = Defaults(max_diff_bytes=400_000)
    agy = Reviewer(name="f", provider="google", model="m", role="finder")
    envelope = budget.prompt_budget(d, agy)
    assert pipeline.batch_plan(b"x" * envelope, d, agy) is None
    assert pipeline.batch_plan(b"x" * (envelope + 1), d, agy) is not None
    # ...and the same diff is a single prompt for a file-fed provider.
    codex = Reviewer(name="f", provider="openai", model="m", role="finder")
    assert pipeline.batch_plan(b"x" * (envelope + 1), d, codex) is None


def test_a_small_diff_never_enters_the_orchestrator(tmp_path, capsys, monkeypatch):
    """PINNED: the unbatched path is untouched, not merely equivalent."""
    def _boom(*a, **kw):
        raise AssertionError("the orchestrator ran for a diff that fits")

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)          # the shipped 400_000-byte envelope
    monkeypatch.setattr(pipeline, "_orchestrate", _boom)
    rec = _run(repo, _store(tmp_path))

    assert rec["status"] == "clean" and _calls(tmp_path) == 1
    for key in ("batched", "batch_count", "batches", "integration"):
        assert key not in rec
    assert rec["context_hash"] != ""     # the unbatched identity is unchanged


# --------------------------------------------------------------------------
# the happy path: one aggregate at the full identity
# --------------------------------------------------------------------------


def test_an_over_budget_diff_is_reviewed_in_batches_and_recorded_once(tmp_path,
                                                                     capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    diff, plan = _plan(repo, cfg)
    assert len(diff.data) > cfg.defaults.max_diff_bytes
    assert len(plan) >= 2, "the fixture must actually need splitting"
    st = _store(tmp_path)

    rec = _run(repo, st)

    # ONE record, at the FULL diff's identity.
    assert len(st.list_reviews("feat", 50)) == 1
    assert rec["diff_hash"] == diff_identity(diff.data)
    assert rec["diff_bytes"] == len(diff.data)
    assert rec["batched"] is True and rec["batch_count"] == len(plan)
    assert rec["trustworthy"] is True and rec["status"] == "clean"
    assert rec["parse_ok"] is True and rec["degraded"] is False
    assert rec["diff_truncated"] is False
    assert rec["usable_output"] is True
    assert st.get_review(rec["id"]) == rec
    assert load_valid_artifact(rec) is rec

    # N batches + ONE integration pass, and nothing else.
    assert _calls(tmp_path) == len(plan) + 1
    assert [b["index"] for b in rec["batches"]] == list(range(1, len(plan) + 1))
    assert [b["files"] for b in rec["batches"]] == [b.files for b in plan]
    assert [b["diff_bytes"] for b in rec["batches"]] == [len(b.data) for b in plan]
    assert all(b["parse_ok"] is True and b["degraded"] is False
               for b in rec["batches"])
    assert all(b["id"] == f"{rec['id']}.b{b['index']}" for b in rec["batches"])
    assert all(b["attempts"] and b["attempts"][0]["provider"] == "xai"
               for b in rec["batches"])
    assert rec["integration"]["ran"] is True
    assert rec["integration"]["status"] == "ran"
    assert rec["integration"]["parse_ok"] is True
    assert rec["integration"]["attempts"]

    # The record persists the budget its own shape implies -- what
    # `recover_stale` reads instead of recomputing from the current config.
    width = pipeline.max_chain_width(cfg)
    assert rec["worst_runtime_sec"] == budget.worst_runtime(
        cfg.defaults, width, len(plan))
    assert rec["worst_runtime_sec"] > pipeline.worst_runtime_sec(
        cfg.defaults, width)
    # ...on the indexed column as well as in the artifact: they are written from
    # one dict, so they cannot disagree (the Phase 1 rule).
    row = st._c.execute("SELECT worst_runtime_sec FROM reviews WHERE id=?",
                        (rec["id"],)).fetchone()
    assert row["worst_runtime_sec"] == rec["worst_runtime_sec"]

    # Context WAS packed per batch, and the aggregate carries the deterministic
    # identity of those packs for exact foreground reuse.
    assert rec["context_bytes"] > 0 and rec["context_files"]
    assert rec["context_hash"] and len(rec["context_hash"]) == 64


def test_batched_foreground_with_context_disabled_persists_empty_context_identity(
        tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, extra_cfg="context_pack = false\n")
    rec = _run(repo, _store(tmp_path))
    assert rec["context_hash"] == ""
    assert rec["checklist_hash"] and len(rec["checklist_hash"]) == 64


def test_a_trustworthy_aggregate_satisfies_the_gate_it_was_taken_for(tmp_path,
                                                                    capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    st = _store(tmp_path)
    rec = _run(repo, st)
    assert rec["trustworthy"] is True

    result = run_gate(st, repo, load_config(repo))
    assert result.code == 0, result.message
    assert result.diff_hash == rec["diff_hash"]


def test_findings_from_every_batch_and_the_integration_pass_are_merged(tmp_path,
                                                                      capsys):
    _fake_grok(tmp_path, _emit(DIRTY))          # every sub-review finds one
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)

    rec = _run(repo, st)

    n = len(plan)
    assert rec["findings_total"] == n + 1 == len(rec["findings"])
    assert rec["severity"] == {"high": n + 1, "medium": 0, "low": 0}
    assert rec["rule_ids"] == ["no-foo"]
    # Batch order first, the cross-file pass last, and only the latter is
    # tagged: `batches[]` is the provenance for the rest.
    titles = [f["title"] for f in rec["findings"]]
    assert titles[:n] == ["[no-foo] bad thing"] * n
    assert titles[n].startswith("[no-foo]")
    assert "(extra-pass: integration)" in rec["findings"][n]["detail"]
    assert [b["findings_total"] for b in rec["batches"]] == [1] * n
    assert rec["integration"]["findings_total"] == 1
    assert rec["status"] == "open-findings" or rec["status"] == "clean"


# --------------------------------------------------------------------------
# aggregation demotes
# --------------------------------------------------------------------------


def test_one_degraded_batch_degrades_the_whole_aggregate(tmp_path, capsys):
    """`any`, not `all`: a healthy sibling must not cover for a bad batch."""
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(CANCELLED), _emit(CLEAN)))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert len(plan) >= 3
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["degraded"] is True and rec["parse_ok"] is True
    assert rec["trustworthy"] is False and rec["status"] == "degraded"
    assert [b["degraded"] for b in rec["batches"]][:3] == [False, True, False]
    assert "batch 2" in rec["degraded_reason"]
    assert rec["usable_output"] is True
    assert run_gate(st, repo, cfg).code == 2


def test_the_stop_reason_is_the_FIRST_abnormal_one_not_the_last(tmp_path, capsys):
    """A single truncated batch must not hide behind its healthy siblings --
    and the last abnormal sub-review must not hide the first."""
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(CANCELLED), _emit(CLEAN),
                                   _emit(TOKENS)))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert len(plan) == 3, "call 4 must be the integration pass"
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["batches"][1]["stop_reason"] == "Cancelled"
    assert rec["integration"]["ran"] is True
    assert rec["stop_reason"] == "Cancelled"        # not "MaxOutputTokens"
    assert rec["degraded"] is True


def test_a_clean_run_reports_the_normal_stop_reason(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    rec = _run(_oversized(tmp_path), _store(tmp_path))
    assert rec["stop_reason"] == "EndTurn"


# --------------------------------------------------------------------------
# a round where NOTHING ran must not report a success-shaped stop reason
#
# Observed live, and the shape is the point: nine batches whose chain was
# exhausted (`parsed=None`, so `stop_reason` is None on every one of them) and
# a cross-file pass on a SECOND provider that answered rc 0 with the agy
# harness's own normal terminal status, `SUCCESS`, and no usable payload. Two
# defects met:
#
#   * `_aggregate_stop_reason` measured "abnormal" as `!= "EndTurn"` -- grok's
#     word -- so another adapter's NORMAL word was promoted as if it were a
#     truncation signal; and
#   * `integration{}` never recorded a `stop_reason` at all, so the value at the
#     top of the record was attributable to nothing a reader could see. The
#     verdict banner said `stop_reason=SUCCESS` for a round that produced no
#     review whatsoever.
#
# The trust axes were already right (`parse_ok=False` -> `trustworthy=false` ->
# gate 2), and nothing here may change them.
# --------------------------------------------------------------------------

#: rc 0, the harness's normal terminal status, and NOTHING usable in it. The
#: captured auto-denied-tool shape (`adapters/agy.py`), which is also what a
#: run that never got an answer out of the provider looks like from outside.
AGY_SUCCESS_BUT_EMPTY = json.dumps({"status": "SUCCESS", "response": ""})

_AGY_INTEGRATOR_CFG = """
[[reviewers]]
name = "integrator"
provider = "google"
model = "gemini-test-0309"
role = "integrator"
"""


def _fake_agy(body: str) -> Path:
    """A fake agy CLI at the path `SKODUN_AGY_BIN` already points at.

    No `tmp_path`, unlike `_fake_grok`: the autouse `_isolate` fixture already
    pins `SKODUN_AGY_BIN` inside it, and re-deriving the path here would be a
    second answer to where the fake goes.
    """
    path = Path(os.environ["SKODUN_AGY_BIN"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _nothing_ran_round(tmp_path):
    """Every batch answers garbage; the cross-file pass answers `SUCCESS` and
    nothing else. Returns `(repo, cfg, store, rec)`."""
    _fake_grok(tmp_path, GARBAGE)
    _fake_agy(_emit(AGY_SUCCESS_BUT_EMPTY))
    repo = _oversized(tmp_path, _AGY_INTEGRATOR_CFG)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert len(plan) >= 2, "the integration pass must be scheduled"
    st = _store(tmp_path)
    return repo, cfg, st, _run(repo, st)


def test_a_round_where_nothing_ran_reports_no_stop_reason(tmp_path, capsys):
    repo, cfg, st, rec = _nothing_ran_round(tmp_path)

    # The fixture really is the observed shape, or the assertion below is vacuous.
    assert [b["stop_reason"] for b in rec["batches"]] == \
        [None] * rec["batch_count"]
    assert rec["integration"]["ran"] is True
    assert rec["integration"]["parse_ok"] is False
    assert rec["usable_output"] is False, "nothing in this round produced a review"

    assert rec["stop_reason"] is None
    assert "stop_reason=SUCCESS" not in trust.banner(rec)


def test_the_integration_pass_records_the_stop_reason_it_saw(tmp_path, capsys):
    """The aggregate's `stop_reason` has to be attributable. `batches[]` has
    carried one since Task 6; `integration{}` did not, so the one sub-review
    that could put a word at the top of the record was the one sub-review whose
    word was invisible."""
    _repo_, _cfg, _st, rec = _nothing_ran_round(tmp_path)
    assert rec["integration"]["stop_reason"] == "SUCCESS"


def test_the_trust_axes_of_a_nothing_ran_round_are_untouched(tmp_path, capsys):
    """The REPORTING fix may not move a single trust axis: this round is
    exactly as untrustworthy as it was, and the gate still refuses it."""
    repo, cfg, st, rec = _nothing_ran_round(tmp_path)
    assert rec["parse_ok"] is False
    assert rec["trustworthy"] is False
    assert rec["status"] == "failed"
    assert run_gate(st, repo, cfg).code == 2


def test_an_abnormal_stop_reason_still_wins_even_when_nothing_ran(tmp_path,
                                                                 capsys):
    """The suppression is of NORMAL words only. A round in which everything
    failed AND something reported `MaxOutputTokens` must still say so -- that is
    the diagnostic the field exists for, and it is most valuable precisely when
    there is no review to read instead."""
    _fake_grok(tmp_path, _per_call(GARBAGE, _emit(TOKENS), GARBAGE))
    _fake_agy(_emit(AGY_SUCCESS_BUT_EMPTY))
    repo = _oversized(tmp_path, _AGY_INTEGRATOR_CFG)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["usable_output"] is True     # the TOKENS batch did parse
    assert rec["stop_reason"] == "MaxOutputTokens"


def test_aggregate_stop_reason_is_a_pure_function_of_the_sub_reviews():
    """The rule, spelled out at the seam. Four cases, and the third is the one
    that was wrong."""
    def sub(stop_reason, parse_ok=True):
        return pipeline._Sub(parse_ok, False, "", stop_reason, False, "", [],
                             "", [], {}, None)

    agg = pipeline._aggregate_stop_reason
    # 1. the first ABNORMAL value wins, over any later one and over normality.
    assert agg([sub("EndTurn"), sub("Cancelled"), sub("MaxOutputTokens")]) \
        == "Cancelled"
    # 2. every reporting sub-review ended normally: its own word, not a
    #    translation of it into another adapter's vocabulary.
    assert agg([sub("EndTurn"), sub("EndTurn")]) == "EndTurn"
    assert agg([sub("SUCCESS"), sub("SUCCESS")]) == "SUCCESS"
    assert agg([sub("turn.completed")]) == "turn.completed"
    # 3. a NORMAL word from a round in which nothing produced a review is not
    #    reported at all -- the record's own "nothing to say" value instead.
    assert agg([sub(None, parse_ok=False), sub("SUCCESS", parse_ok=False)]) \
        is None
    assert agg([sub("EndTurn", parse_ok=False)]) is None
    # ...but an abnormal one still is (see the test above).
    assert agg([sub("Cancelled", parse_ok=False)]) == "Cancelled"
    # 4. the normal word comes from a sub-review that PRODUCED a review. A
    #    failed sub-review can still report its adapter's normal terminal
    #    status (an exhausted chain whose last attempt exited cleanly with
    #    nothing usable in it), and reporting that word would describe the
    #    process of a run that answered nothing, over the word of the run that
    #    answered. Abnormality is still judged across ALL of them (case 3).
    assert agg([sub("SUCCESS", parse_ok=False), sub("EndTurn")]) == "EndTurn"
    assert agg([sub("EndTurn", parse_ok=False), sub("SUCCESS"),
                sub("SUCCESS", parse_ok=False)]) == "SUCCESS"
    # 5. nothing reported at all.
    assert agg([sub(None), sub("")]) is None
    assert agg([]) is None


def test_every_adapters_own_normal_terminal_value_is_in_the_set():
    """Assembled from the three adapters' OWN constants, so the aggregation
    rule cannot drift away from what an adapter actually reports -- which is
    exactly how agy's `SUCCESS` came to be read as an abnormal stop."""
    from skodun.adapters import NORMAL_STOP_REASONS
    from skodun.adapters import agy as agy_mod
    from skodun.adapters import codex as codex_mod
    from skodun.adapters import grok as grok_mod

    # FLAT, and that is the property with teeth. An adapter may export one
    # normal word or a set of them, and building this set with a literal put
    # grok's set inside as a single element -- so `"EndTurn" in
    # NORMAL_STOP_REASONS` was False and a clean grok batch published its own
    # terminal word as the round's first ABNORMAL one.
    assert all(isinstance(word, str) for word in NORMAL_STOP_REASONS), (
        NORMAL_STOP_REASONS)
    # Every word each adapter calls normal is IN the set, whichever shape that
    # adapter exports. Membership rather than a re-derived union: rebuilding
    # the union here would make this test agree with the production code by
    # construction, including when both are wrong.
    for own in (grok_mod._STOP_REASON_OK, agy_mod._STATUS_OK,
                codex_mod._TURN_COMPLETED):
        words = {own} if isinstance(own, str) else set(own)
        assert words <= NORMAL_STOP_REASONS, (own, NORMAL_STOP_REASONS)
    # ...and nothing else is, so a stray word cannot be waved through as normal.
    assert len(NORMAL_STOP_REASONS) == 4
    assert "EndTurn" in NORMAL_STOP_REASONS
    assert "SUCCESS" in NORMAL_STOP_REASONS


def test_a_failed_integration_pass_demotes_an_aggregate_whose_batches_answered(
        tmp_path, capsys):
    """The zero-finding regression, and it is the whole reason
    `usable_output` is an explicit field rather than a finding count.

    Every batch answered, cleanly, with nothing to report. The cross-file pass
    then produced nothing usable, so the aggregate cannot certify the change --
    but it is NOT a round that said nothing, and a surface that judged by
    `findings_total` would print "NO REVIEW HAPPENED" over three real reviews.
    """
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(CLEAN), _emit(CLEAN),
                                   GARBAGE))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert len(plan) == 3
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["parse_ok"] is False and rec["trustworthy"] is False
    assert rec["status"] == "failed"
    assert rec["findings_total"] == 0 and rec["findings"] == []
    assert rec["usable_output"] is True
    assert all(b["parse_ok"] is True for b in rec["batches"])
    assert rec["integration"]["status"] == "failed"
    assert rec["integration"]["ran"] is True
    assert rec["integration"]["parse_ok"] is False
    assert "integration" in rec["failure_reason"]
    assert run_gate(st, repo, cfg).code == 2


def test_an_unavailable_integrator_with_no_fallback_demotes_the_aggregate(
        tmp_path, capsys):
    """A configured cross-file reviewer whose CLI is not there is a coverage
    hole, not a pass: nothing ever looked at the seams."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, """
[[reviewers]]
name = "integrator"
provider = "openai"
model = "gpt-test-0309"
role = "integrator"
""")
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)

    with pytest.raises(pipeline.PreflightRefused,
                       match="required_pass_unavailable"):
        _run(repo, st)
    assert _calls(tmp_path) == 0      # readiness refuses before any batch


def test_a_failed_batch_demotes_the_aggregate(tmp_path, capsys):
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), GARBAGE, _emit(CLEAN)))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["parse_ok"] is False and rec["status"] == "failed"
    assert rec["batches"][1]["parse_ok"] is False
    assert "2" in rec["failure_reason"]
    assert rec["usable_output"] is True       # batches 1 and 3 answered
    assert run_gate(st, repo, cfg).code == 2


def test_an_aggregate_nothing_answered_has_no_usable_output(tmp_path, capsys):
    _fake_grok(tmp_path, GARBAGE)
    repo = _oversized(tmp_path)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["parse_ok"] is False and rec["trustworthy"] is False
    assert rec["usable_output"] is False
    assert all(b["parse_ok"] is False for b in rec["batches"])
    assert rec["integration"]["parse_ok"] is False


def test_a_truncated_batch_prompt_demotes_the_aggregate(tmp_path, capsys):
    """The irreducible floor: one hunk no split can make fit.

    The batch carries it whole and the PROMPT cap cuts it, so the model did not
    see the change -- `diff_truncated`, and the gate refuses.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, files=2)
    (repo / "huge.txt").write_text("z" * 9000 + "\n", encoding="utf-8")
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert any(b.truncated for b in plan)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["diff_truncated"] is True and rec["trustworthy"] is False
    assert rec["status"] == "failed"
    assert rec["parse_ok"] is True and rec["degraded"] is False
    floors = [b for b in rec["batches"] if b["splitter_truncated"]]
    assert floors and all(b["diff_truncated"] for b in floors)
    # ...and the batches that fit are not tarred with it.
    assert any(b["diff_truncated"] is False for b in rec["batches"])
    assert rec["usable_output"] is True
    assert run_gate(st, repo, cfg).code == 2


def test_zero_batches_is_a_terminal_failure_never_a_clean_verdict(
        tmp_path, capsys, monkeypatch):
    """ORACLE: "diff batching produced no batches" is a recorded failure.

    An empty batch plan would otherwise send an empty prompt and risk minting a
    clean verdict out of a diff nothing looked at.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    monkeypatch.setattr(batching, "split", lambda *a, **kw: [])
    st = _store(tmp_path)

    with pytest.raises(pipeline.PreflightRefused, match="prompt_unfit"):
        _run(repo, st)
    assert _calls(tmp_path) == 0             # no model call is spent


# --------------------------------------------------------------------------
# the sole batch: the whole diff, reviewed exactly as an unbatched one
# --------------------------------------------------------------------------


def test_the_sole_batch_earns_no_integration_pass_and_selects_full_mode(
        tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _sole_batch_repo(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    assert len(plan) == 1 and plan[0].truncated
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["batch_count"] == 1 and _calls(tmp_path) == 1
    assert "integration" not in rec          # ABSENT, not a "skipped" status
    assert rec["batches"][0]["checklist"]["mode"] == "full"
    assert passes.batch_checklist_mode(1) == "full"
    # The floor case: the whole diff is one hunk, so the prompt is capped.
    assert rec["diff_truncated"] is True and rec["status"] == "failed"
    assert run_gate(st, repo, cfg).code == 2


def test_the_sole_batch_prompt_is_byte_identical_to_the_unbatched_prompt(
        tmp_path):
    """The one-batch seam, and why `batch_checklist_mode(1)` is `full`.

    A one-batch orchestration must review exactly what the unbatched builder
    would send -- same rules, same context, same labels -- or batching would
    quietly review LESS than the same diff reviewed whole.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)                  # small diff: driven through the seam
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    branch = gitio.current_branch(repo)
    head_label = f"{gitio.head_sha(repo)} (working tree)"

    # The unbatched builder, called exactly as `run_review` calls it.
    selection = checklist.select(
        diff.files, "full", pipeline._under(root, d.checklist_dir),
        pipeline._under(root, d.rules_json), d.checklist_map,
        d.test_path_patterns)
    pack = contextpack.pack(
        root, diff.files, diff.statuses,
        promptbuild.context_headroom(d.max_diff_bytes, len(diff.data),
                                     packing=True),
        pack_large_added=False)
    expected = promptbuild.build(branch, base.ref, base.sha, head_label,
                                 diff.data, d.max_diff_bytes, selection,
                                 pack.body)

    st = _store(tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        out = pipeline._orchestrate(
            {"id": "sk_seam", "summary": "", "findings": []}, diff,
            batches=[batching.Batch(data=diff.data, files=list(diff.files))],
            cfg=cfg, d=d, root=root, store=st, scratch=Path(tmp),
            finder=pipeline._reviewer_for(cfg, "finder"), branch=branch,
            base_ref=base.ref, base_sha=base.sha, head_label=head_label)

    assert out["batch_count"] == 1
    sent = (tmp_path / "bin" / "prompt_1.txt").read_bytes()
    assert sent == expected.text
    assert out["prompt_bytes"] == expected.prompt_bytes


def test_checkpoint_preparation_is_deterministic_and_invokes_no_provider(
        tmp_path, monkeypatch):
    """Identity is frozen from the exact prompts before resumable work runs."""
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    finder = pipeline._reviewer_for(cfg, "finder")
    batches = pipeline.batch_plan(diff.data, d, finder)
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preparing checkpoint identity invoked a provider")

    monkeypatch.setattr(pipeline, "_run_chain", forbidden)
    one = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)")
    two = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)")

    assert [item.prompt.text for item in one.batches] == \
        [item.prompt.text for item in two.batches]
    assert one.context_hash == two.context_hash
    assert one.checklist_hash == two.checklist_hash
    assert one.boundary_digest == two.boundary_digest
    assert one.integration_plan_digest == two.integration_plan_digest
    assert [item.identity for item in one.batches] == \
        [item.identity for item in two.batches]


def test_prepared_batch_prompts_include_stack_and_lineage_context(tmp_path):
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    finder = pipeline._reviewer_for(cfg, "finder")
    batches = pipeline.batch_plan(diff.data, d, finder)
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)
    stack_context = (
        b"----- BEGIN STACK CONTEXT -----\n"
        b"version=1 status=valid\n"
        b"----- END STACK CONTEXT -----\n")
    lineage_context = (
        b"----- BEGIN PRIOR FINDINGS -----\n"
        b"count=1 truncated=false\n"
        b"----- END PRIOR FINDINGS -----\n")

    with_context = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)",
        stack_context=stack_context, lineage_context=lineage_context)
    without = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)")

    assert stack_context.rstrip(b"\n") in with_context.batches[0].prompt.text
    assert lineage_context.rstrip(b"\n") in with_context.batches[0].prompt.text
    assert with_context.batches[0].identity.prompt_hash != \
        without.batches[0].identity.prompt_hash
    assert with_context.stack_context == stack_context
    assert with_context.lineage_context == lineage_context
    assert with_context.integration_plan_digest != \
        without.integration_plan_digest


def test_prepared_prompts_are_the_prompts_the_orchestrator_sends(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    finder = pipeline._reviewer_for(cfg, "finder")
    batches = pipeline.batch_plan(diff.data, d, finder)
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)
    prepared = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)")

    with tempfile.TemporaryDirectory() as scratch:
        pipeline._orchestrate(
            {"id": "sk_prepared", "mode": "now", "model": finder.model,
             "adapter": "grok", "summary": "", "findings": []}, diff,
            batches=batches, cfg=cfg, d=d, root=root, store=_store(tmp_path),
            scratch=Path(scratch), finder=finder, branch=branch,
            base_ref=base.ref, base_sha=base.sha,
            head_label=f"{head} (working tree)", prepared_plan=prepared)

    for index, item in enumerate(prepared.batches, 1):
        assert (tmp_path / "bin" / f"prompt_{index}.txt").read_bytes() == \
            item.prompt.text


def test_orchestrator_sends_stack_and_lineage_to_the_integration_pass(tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    finder = pipeline._reviewer_for(cfg, "finder")
    batches = pipeline.batch_plan(diff.data, d, finder)
    assert batches is not None and len(batches) >= 2
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)
    stack_context = (
        b"----- BEGIN STACK CONTEXT -----\n"
        b"version=1 status=valid\n"
        b"----- END STACK CONTEXT -----\n")
    lineage_context = (
        b"----- BEGIN PRIOR FINDINGS -----\n"
        b"count=1 truncated=false\n"
        b"----- END PRIOR FINDINGS -----\n")
    prepared = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)",
        stack_context=stack_context, lineage_context=lineage_context)

    with tempfile.TemporaryDirectory() as scratch:
        pipeline._orchestrate(
            {"id": "sk_integration_ctx", "mode": "now", "model": finder.model,
             "adapter": "grok", "summary": "", "findings": []}, diff,
            batches=batches, cfg=cfg, d=d, root=root, store=_store(tmp_path),
            scratch=Path(scratch), finder=finder, branch=branch,
            base_ref=base.ref, base_sha=base.sha,
            head_label=f"{head} (working tree)", prepared_plan=prepared)

    seam = (tmp_path / "bin" / f"prompt_{len(batches) + 1}.txt").read_bytes()
    assert stack_context.rstrip(b"\n") in seam
    assert lineage_context.rstrip(b"\n") in seam
    assert b"FINAL CROSS-FILE INTEGRATION" in seam


def test_prepared_plan_builds_the_complete_checkpoint_identity(tmp_path):
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    finder = pipeline._reviewer_for(cfg, "finder")
    batches = pipeline.batch_plan(diff.data, d, finder)
    branch = gitio.current_branch(repo)
    head = gitio.head_sha(repo)
    prepared = pipeline._prepare_batch_plan(
        diff, batches=batches, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, base_ref=base.ref, base_sha=base.sha,
        head_label=f"{head} (working tree)")
    rec = {"mode": "now", "requested_reviewer": None,
           "client_family": None, "routed_reviewer": finder.name}

    identity = pipeline._orchestration_identity(
        rec, diff, prepared, cfg=cfg, d=d, root=root, finder=finder,
        branch=branch, head=head, base_ref=base.ref, base_sha=base.sha,
        tree_fingerprint=gitio.tree_fingerprint(repo, paths=diff.files))

    assert identity.batch_count == len(batches)
    assert identity.batch_budget == pipeline._batch_budget(d, finder)
    assert identity.context_hash == prepared.context_hash
    assert identity.checklist_hash == prepared.checklist_hash
    assert identity.pass_identities[:-1] == tuple(
        item.identity for item in prepared.batches)
    assert identity.pass_identities[-1].kind == "integration"
    assert identity.pass_identities[-1].prompt_hash is None


def test_checkpoint_claim_lease_includes_configured_admission_wait():
    defaults = Defaults(timeout_sec=1, timeout_retries=0, degraded_retries=0)
    assert pipeline._checkpoint_claim_lease_seconds(
        defaults, 1, env={"SKODUN_ADMISSION_WAIT_SECONDS": "90"}) == (
        budget.worst_runtime(defaults, 1, 0) + 90)
    assert pipeline._checkpoint_claim_lease_seconds(
        defaults, 1, env={"SKODUN_ADMISSION_WAIT_SECONDS": "1.9"}) == (
        budget.worst_runtime(defaults, 1, 0) + 2)
    assert capacity.admission_wait_from_env(
        30.0, {"SKODUN_ADMISSION_WAIT_SECONDS": "nan"}) == 30.0
    assert capacity.admission_wait_from_env(
        30.0, {"SKODUN_ADMISSION_WAIT_SECONDS": "inf"}) == 30.0


def _clean_checkpoint_sub(label: str) -> pipeline._Sub:
    return pipeline._Sub(
        parse_ok=True, degraded=False, degraded_reason="",
        stop_reason="EndTurn", diff_truncated=False,
        summary=f"clean {label}", findings=[], failure_reason="", attempts=[],
        provenance={"provider": "xai", "model": "grok", "effort": None},
        accepted={"adapter_name": "grok", "model": "grok",
                  "provider": "xai", "effort": None})


def _without_run_identity(rec: dict) -> dict:
    """Comparable aggregate fields across two independently minted reviews."""
    ignored = {
        "id", "reviewed_at", "review_started_at", "review_completed_at",
        "batch_orchestration_id", "tree_fingerprint",
    }
    timing_keys = {
        "queued_at", "admitted_at", "started_at", "completed_at",
        "queue_duration_sec", "run_duration_sec", "wall_duration_sec",
    }
    out = {key: value for key, value in rec.items() if key not in ignored}
    out["batches"] = [
        _without_timing({key: value for key, value in batch.items()
                         if key != "id" and key not in timing_keys},
                        timing_keys)
        for batch in out.get("batches", [])]
    if isinstance(out.get("integration"), dict):
        out["integration"] = _without_timing(
            {key: value for key, value in out["integration"].items()
             if key not in timing_keys},
            timing_keys)
    return out


def _without_timing(item: dict, timing_keys: set[str]) -> dict:
    telemetry = item.get("telemetry")
    if not isinstance(telemetry, dict):
        return item
    timing = telemetry.get("timing")
    if not isinstance(timing, dict):
        return item
    cleaned = dict(item)
    cleaned["telemetry"] = dict(telemetry)
    cleaned["telemetry"]["timing"] = {
        key: value for key, value in timing.items() if key not in timing_keys}
    return cleaned


def test_cancel_after_three_batches_resumes_only_the_missing_work(
        tmp_path, monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, files=4)
    cfg = load_config(repo)
    store = _store(tmp_path)
    cancel = threading.Event()
    first_calls = []

    def first(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        first_calls.append(label)
        if label == "batch 3":
            cancel.set()
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", first)
    with pytest.raises(pipeline.ReviewCancelled):
        pipeline.run_review(repo, cfg, store, cancel=cancel)

    orchestration = store._c.execute(
        "SELECT id FROM review_orchestrations").fetchone()
    checkpoints = store.list_checkpoints(orchestration["id"])
    assert first_calls == ["batch 1", "batch 2", "batch 3"]
    assert [row["state"] for row in checkpoints] == [
        "complete", "complete", "complete", "pending", "pending"]
    from skodun.readmodel import project_review
    projection = project_review(
        {"status": "failed", "trustworthy": False, "usable_output": False,
         "batches": [], "extra_passes": {}},
        orchestration=store.get_orchestration(orchestration["id"]),
        checkpoints=checkpoints)
    assert projection.usable_evidence is True
    assert projection.coverage_state == "partial"
    assert projection.gate_eligible is False
    assert projection.completed_passes == 3
    assert projection.passes["integration"] == "queued"
    assert projection.next_resumable_pass == 4

    resumed_calls = []

    def resumed(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        resumed_calls.append(label)
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", resumed)
    resumed_rec = pipeline.run_review(
        repo, cfg, store, cancel=threading.Event())

    assert resumed_calls == ["batch 4", "the integration pass"]
    assert resumed_rec["trustworthy"] is True
    consumed = store.get_orchestration(
        resumed_rec["batch_orchestration_id"])
    assert consumed["state"] == "consumed"
    assert consumed["final_review_id"] == resumed_rec["id"]

    fresh_calls = []

    def fresh(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        fresh_calls.append(label)
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", fresh)
    fresh_rec = pipeline.run_review(
        repo, cfg, store, cancel=threading.Event(),
        resume_checkpoints=False)
    assert fresh_calls == [
        "batch 1", "batch 2", "batch 3", "batch 4",
        "the integration pass"]
    assert _without_run_identity(resumed_rec) == _without_run_identity(fresh_rec)


def test_global_wall_timeout_preserves_three_batches_for_resume(
        tmp_path, monkeypatch):
    """The service deadline uses the same cancellation token as process cleanup."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, files=4)
    cfg = load_config(repo)
    store = _store(tmp_path)
    first_calls = []

    def times_out_on_fourth(*args, **kwargs):
        label = args[8] if len(args) > 8 else kwargs["label"]
        first_calls.append(label)
        if label == "batch 4":
            cancel = kwargs["cancel"]
            assert cancel.wait(5), "recovery deadline did not cancel the pass"
            raise pipeline.ReviewCancelled("review cancelled")
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", times_out_on_fourth)
    status, text, _metadata = services.svc_review_detailed(
        store, repo, recover=True, max_attempts=1, max_wall_seconds=3)
    assert status == 4
    assert "wall budget exhausted" in text
    assert first_calls == ["batch 1", "batch 2", "batch 3", "batch 4"]

    candidate = store.find_resume_candidate(
        gitio.repository_identity(repo),
        gitio.observed_worktree_root(repo), gitio.current_branch(repo))
    assert [row["state"] for row in store.list_checkpoints(candidate["id"])] == [
        "complete", "complete", "complete", "pending", "pending"]

    resumed_calls = []

    def clean(*args, **kwargs):
        label = args[8] if len(args) > 8 else kwargs["label"]
        resumed_calls.append(label)
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", clean)
    resumed = pipeline.run_review(repo, cfg, store, cancel=threading.Event())
    assert resumed["trustworthy"] is True
    assert resumed_calls == ["batch 4", "the integration pass"]


def test_live_racing_resumer_invokes_no_provider(tmp_path, monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, files=4)
    cfg = load_config(repo)
    store = _store(tmp_path)
    cancel = threading.Event()

    def stop_after_three(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        if label == "batch 3":
            cancel.set()
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", stop_after_three)
    with pytest.raises(pipeline.ReviewCancelled):
        pipeline.run_review(repo, cfg, store, cancel=cancel)
    orchestration = store.find_resume_candidate(
        gitio.repository_identity(repo),
        gitio.observed_worktree_root(repo), gitio.current_branch(repo))
    rows = store.list_checkpoints(orchestration["id"])
    fourth = rows[3]
    from skodun.checkpoints import PassIdentity
    store.claim_checkpoint(
        orchestration["id"], PassIdentity(
            kind="batch", index=4, prompt_hash=fourth["prompt_hash"],
            diff_hash=fourth["diff_hash"],
            boundary_hash=fourth["boundary_hash"]),
        owner="other-resumer", now="2026-08-12T10:00:00Z",
        lease_expires_at="2099-08-12T10:00:00Z")

    monkeypatch.setattr(
        pipeline, "_run_sub",
        lambda *_args, **_kwargs: pytest.fail("racing resumer invoked provider"))
    with pytest.raises(pipeline.CheckpointInFlight, match="batch 4"):
        pipeline.run_review(repo, cfg, store, cancel=threading.Event())


def test_resume_mismatch_restarts_fresh_and_explains_the_first_field(
        tmp_path, monkeypatch):
    from dataclasses import replace as dc_replace

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path, files=4)
    cfg = load_config(repo)
    store = _store(tmp_path)
    cancel = threading.Event()

    def stop_after_one(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        if label == "batch 1":
            cancel.set()
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", stop_after_one)
    with pytest.raises(pipeline.ReviewCancelled):
        pipeline.run_review(repo, cfg, store, cancel=cancel)

    # Even a setting outside the currently executing foreground path belongs
    # to the exact CONFIG identity. Resume is deliberately conservative: a
    # later release may make this value plan-affecting, and old checkpoints
    # must not then acquire approximate compatibility by accident.
    changed = dc_replace(
        cfg, dispatch=dc_replace(
            cfg.dispatch,
            large_prompt_bytes=cfg.dispatch.large_prompt_bytes + 1))
    calls = []
    progress = []

    def clean(*_args, **kwargs):
        label = _args[8] if len(_args) > 8 else kwargs["label"]
        calls.append(label)
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, "_run_sub", clean)
    pipeline.run_review(
        repo, changed, store, cancel=threading.Event(),
        progress_sink=progress.append)

    assert calls == [
        "batch 1", "batch 2", "batch 3", "batch 4",
        "the integration pass"]
    assert any(
        "checkpoint resume refused: config_hash changed" in line
        for line in progress)


# --------------------------------------------------------------------------
# what each prompt is allowed to carry
# --------------------------------------------------------------------------


def test_no_batch_prompt_carries_a_cross_file_rule_but_the_seam_pass_does(
        tmp_path, capsys):
    """A rule about relationships between files, handed to a reviewer holding
    one slice of the change, is a false-positive engine -- the cross-file rules
    go to the integration pass instead."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cl = repo / "docs" / "review" / "checklists"
    cl.mkdir(parents=True)
    (cl / "core.md").write_text("- CORE-RULE-MARKER\n", encoding="utf-8")
    (cl / "cross-file.md").write_text("- CROSS-FILE-MARKER\n", encoding="utf-8")
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)

    rec = _run(repo, st)

    # The INJECTED rules only: the checklist files are themselves untracked, so
    # their text is legitimately inside the reviewed diff as well.
    for i in range(1, len(plan) + 1):
        rules = _rules_block(tmp_path / "bin" / f"prompt_{i}.txt")
        assert "CORE-RULE-MARKER" in rules
        assert "CROSS-FILE-MARKER" not in rules
    seam = (tmp_path / "bin" / f"prompt_{len(plan) + 1}.txt").read_text(
        encoding="utf-8")
    assert "CROSS-FILE-MARKER" in _rules_block(
        tmp_path / "bin" / f"prompt_{len(plan) + 1}.txt")
    assert "FINAL CROSS-FILE INTEGRATION" in seam

    assert all(b["checklist"]["mode"] == "batch" for b in rec["batches"])
    assert rec["integration"]["checklist"]["mode"] == "integration"
    assert "core" in rec["checklist_sections"]
    assert "cross-file" in rec["checklist_sections"]     # union across prompts
    assert rec["checklist_bytes"] > 0


def test_a_batch_prompt_is_labelled_with_its_own_position_and_files(tmp_path,
                                                                   capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    _run(repo, _store(tmp_path))

    first = (tmp_path / "bin" / "prompt_1.txt").read_text(encoding="utf-8")
    assert f"Branch: feat (batch 1/{len(plan)})" in first
    assert f"batch 1 of {len(plan)}" in first
    for name in plan[0].files:
        assert name in first
    # Each batch prompt carries ONLY its own slice of the diff.
    assert plan[0].data.decode("utf-8", "replace")[:40] in first
    if len(plan) > 1:
        assert plan[1].data.decode("utf-8", "replace")[:40] not in first


# --------------------------------------------------------------------------
# who answered
# --------------------------------------------------------------------------


def _canned(model: str, provider: str = "xai"):
    """A `_run_chain` stand-in that answers cleanly as a named entry."""
    def run(*a, **kw):
        return chain._Outcome(
            parsed=ParseResult(parse_ok=True, findings=[], summary="ok",
                               stop_reason="EndTurn", degraded=False,
                               degraded_reason=""),
            attempts=[{"n": 1, "provider": provider, "model": model,
                       "effort": "medium"}],
            failure_reason="",
            accepted={"adapter_name": "grok", "provider": provider,
                      "model": model, "effort": "medium"})
    return run


def test_a_per_batch_fallback_hop_is_recorded_and_demotes_NOTHING(tmp_path,
                                                                 monkeypatch):
    """DIVERGENCE from the oracle, deliberately.

    The oracle demotes an aggregate whose sub-reviews did not all report the
    same model ("provider-model-mismatch"), because there a second model is an
    invisible fallback nobody asked for. In skodun a hop is a designed, recorded
    outcome — `run_chain` advances only on `unavailable` — so it demotes nothing;
    the aggregate's indexed columns simply keep the configured finder's identity
    when its sub-reviews do not agree on one answering entry.
    """
    repo = _repo(tmp_path, BATCH_CFG)
    cfg = load_config(repo)
    d = cfg.defaults
    root = gitio._worktree_root(repo)
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    two = [batching.Batch(data=diff.data, files=list(diff.files)),
           batching.Batch(data=diff.data, files=list(diff.files))]
    calls = {"n": 0}
    hopped = _canned("fallback-model-0309")
    home = _canned("grok-4.20-0309-reasoning")

    def run(*a, **kw):
        calls["n"] += 1
        return (hopped if calls["n"] == 2 else home)(*a, **kw)

    monkeypatch.setattr(pipeline, "_run_chain", run)
    st = _store(tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        out = pipeline._orchestrate(
            {"id": "sk_hop", "model": "grok-4.20-0309-reasoning",
             "adapter": "grok", "summary": "", "findings": []}, diff,
            batches=two, cfg=cfg, d=d, root=root, store=st, scratch=Path(tmp),
            finder=pipeline._reviewer_for(cfg, "finder"), branch="feat",
            base_ref=base.ref, base_sha=base.sha, head_label="h (working tree)")

    assert out["parse_ok"] is True and out["degraded"] is False
    assert out["diff_truncated"] is False          # nothing demoted
    assert [b["model"] for b in out["batches"]] == [
        "grok-4.20-0309-reasoning", "fallback-model-0309"]
    # Disagreement -> the configured finder's identity is kept, not one batch's.
    assert out["model"] == "grok-4.20-0309-reasoning"


def test_an_aggregate_names_the_entry_that_actually_answered_when_all_agree(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path, BATCH_CFG)
    cfg = load_config(repo)
    d = cfg.defaults
    base = resolve_base(repo)
    diff = capture_diff(repo, base.sha, d.untracked_max)
    monkeypatch.setattr(pipeline, "_run_chain", _canned("backup-model-0309"))
    st = _store(tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        out = pipeline._orchestrate(
            {"id": "sk_agree", "model": "grok-4.20-0309-reasoning",
             "adapter": "grok", "summary": "", "findings": []}, diff,
            batches=[batching.Batch(data=diff.data, files=list(diff.files))] * 2,
            cfg=cfg, d=d, root=gitio._worktree_root(repo), store=st,
            scratch=Path(tmp), finder=pipeline._reviewer_for(cfg, "finder"),
            branch="feat", base_ref=base.ref, base_sha=base.sha,
            head_label="h (working tree)")
    assert out["model"] == "backup-model-0309" and out["adapter"] == "grok"


# --------------------------------------------------------------------------
# extra passes
# --------------------------------------------------------------------------


def test_a_batched_foreground_run_applies_the_extra_passes_to_the_aggregate(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    (repo / "auth").mkdir()
    (repo / "auth" / "session.py").write_text("token = 1\n", encoding="utf-8")
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)

    rec = _run(repo, st)

    assert rec["extra_passes"]["security"]["ran"] is True
    assert rec["extra_passes"]["security"]["parse_ok"] is True
    # batches + integration + ONE security pass over the whole diff.
    assert _calls(tmp_path) == len(plan) + 2
    assert rec["batch_count"] == len(plan)


def test_a_batched_prepush_run_is_batches_and_the_integration_pass_only(
        tmp_path, capsys, monkeypatch):
    """The `--now`-only predicates are unchanged: a background batched review
    runs the finder over every batch plus the cross-file pass, and nothing
    else."""
    for var in ("SKODUN_SECURITY_PASS", "SKODUN_SKEPTIC_PASS",
                "SKODUN_REFUTER_PASS"):
        monkeypatch.setenv(var, "1")
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _oversized(tmp_path)
    (repo / "auth").mkdir()
    (repo / "auth" / "session.py").write_text("token = 1\n", encoding="utf-8")
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)

    rec = _run(repo, st, mode="prepush")

    assert rec["mode"] == "prepush"
    assert rec["extra_passes"] == {}
    assert _calls(tmp_path) == len(plan) + 1
    assert rec["integration"]["ran"] is True


# --------------------------------------------------------------------------
# the CLI seam
# --------------------------------------------------------------------------


def test_the_cli_exits_and_banners_from_a_batched_aggregate(tmp_path, capsys):
    """The aggregate is an ordinary record on the way out: one banner as the
    last line of stdout, and the shipped exit-code mapping."""
    _fake_grok(tmp_path, _emit(DIRTY))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)

    code = main(["review", "--repo", str(repo)])
    lines = capsys.readouterr().out.strip().splitlines()

    assert code == 1, "findings remain open"
    assert len(lines) == 1, lines          # ONE banner, not one per batch
    assert lines[0].startswith(
        f"SKODUN VERDICT: trustworthy=true findings={len(plan) + 1}")


def test_the_cli_exits_4_on_an_untrustworthy_aggregate(tmp_path, capsys):
    _fake_grok(tmp_path, _per_call(_emit(CLEAN), _emit(CANCELLED), _emit(CLEAN)))
    repo = _oversized(tmp_path)
    code = main(["review", "--repo", str(repo)])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 4
    assert "trustworthy=false" in last and "degraded=true" in last


# --------------------------------------------------------------------------
# stale recovery reads the budget the record persisted
# --------------------------------------------------------------------------


def _running_with_budget(store: Store, rid: str, age_sec: float,
                         worst: int) -> None:
    store.save_review({
        "id": rid,
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - age_sec)),
        "branch": "feat", "base_sha": "b" * 40, "diff_hash": "d" * 40,
        "status": "running", "parse_ok": False, "degraded": False,
        "diff_truncated": False, "findings": [], "findings_total": 0,
        "worst_runtime_sec": worst,
    })


def test_recover_stale_prefers_the_budget_the_record_persisted(tmp_path):
    """A batched run makes many sequential model calls under one record.

    Judged by the single-review ceiling, its `running` row is stale long before
    the run is over -- and sweeping it marks a live review failed. The record
    carries its own budget precisely so the janitor does not have to guess.
    """
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)
    st = _store(tmp_path)
    single = pipeline.worst_runtime_sec(cfg.defaults, pipeline.max_chain_width(cfg))
    age = single + 30
    _running_with_budget(st, "sk_batched", age,
                         budget.worst_runtime(cfg.defaults, 1, 40))
    _running(st, "sk_no_budget", age)        # pre-Phase-3 shape: no column

    assert pipeline.recover_stale(st, cfg) == 1
    assert st.get_review("sk_batched")["status"] == "running"
    assert st.get_review("sk_no_budget")["status"] == "failed"


def test_a_persisted_budget_that_has_also_expired_is_still_swept(tmp_path):
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)
    st = _store(tmp_path)
    _running_with_budget(st, "sk_done_for", 5000, 600)
    assert pipeline.recover_stale(st, cfg) == 1
    assert st.get_review("sk_done_for")["status"] == "failed"


@pytest.mark.parametrize("junk", [0, -1, "600", True, None, 1.5])
def test_an_unusable_persisted_budget_falls_back_to_the_computed_ceiling(
        tmp_path, junk):
    """Only a positive plain int is evidence. Anything else is treated as
    absent -- never as "never sweep this row"."""
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)
    st = _store(tmp_path)
    _running_with_budget(st, "sk_junk", 5000, junk)
    assert pipeline.recover_stale(st, cfg) == 1


def test_the_running_aggregate_carries_its_batched_budget_before_it_finishes(
        tmp_path, capsys, monkeypatch):
    """The budget has to be on the row while the run is still going: that is
    the only moment `recover_stale` can act on it."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    cfg = load_config(repo)
    _diff, plan = _plan(repo, cfg)
    st = _store(tmp_path)
    seen: dict = {}
    real = pipeline._run_chain

    def spy(*a, **kw):
        if "row" not in seen:
            rows = [r for r in st.list_reviews("feat", 10)
                    if r["status"] == "running"]
            seen["row"] = rows[0] if rows else None
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "_run_chain", spy)
    _run(repo, st)

    row = seen["row"]
    assert row is not None
    assert row["batched"] is True and row["batch_count"] == len(plan)
    assert row["worst_runtime_sec"] == budget.worst_runtime(
        cfg.defaults, pipeline.max_chain_width(cfg), len(plan))
    assert pipeline.recover_stale(st, cfg) == 0     # ...and it is not swept


# --------------------------------------------------------------------------
# the lock's budget sidecar
# --------------------------------------------------------------------------


def test_the_sidecar_sits_beside_an_unchanged_legacy_owner_file(tmp_path):
    repo = _repo(tmp_path)
    lock = pipeline._acquire_fg_lock(git_common_dir(repo), repo, wait=1,
                                     poll=0.1, stale=1234, grace=30)
    try:
        assert (lock.path / "budget").read_text(encoding="utf-8") == "1234\n"
        raw = (lock.path / "owner").read_text(encoding="utf-8")
        assert re.fullmatch(r"pid=\d+\nstarted=\d+\nworktree=.+\n", raw)
        # Exactly two entries: no temp file survives the atomic rename.
        assert sorted(p.name for p in lock.path.iterdir()) == ["budget", "owner"]
        assert pipeline._holder_budget(lock.path) == 1234.0
    finally:
        pipeline._release_fg_lock(lock)
    assert not lock.path.exists()


def test_the_budget_is_published_BEFORE_the_owner(tmp_path, monkeypatch):
    """A complete owner from a skodun holder must imply the sidecar exists --
    that is what lets a waiter tell a batched skodun holder from a legacy one
    instead of guessing."""
    repo = _repo(tmp_path)
    seen: dict = {}
    real = pipeline._write_owner

    def spy(lock: Path, pid: int, worktree: Path):
        seen["sidecar_present"] = (Path(lock) / "budget").exists()
        return real(lock, pid, worktree)

    monkeypatch.setattr(pipeline, "_write_owner", spy)
    lock = pipeline._acquire_fg_lock(git_common_dir(repo), repo, wait=1,
                                     poll=0.1, stale=99, grace=30)
    pipeline._release_fg_lock(lock)
    assert seen["sidecar_present"] is True


def test_a_sampling_hammer_never_sees_an_owner_without_its_budget(tmp_path):
    """The concurrent form of the same invariant, including teardown: a lock
    being RELEASED must not read as a complete legacy holder either."""
    repo = _repo(tmp_path)
    common = git_common_dir(repo)
    lock = common / pipeline.LOCK_NAME
    bad: list[list[str]] = []
    stop = threading.Event()

    def sample() -> None:
        # ONE `listdir` per sample, deliberately: two `exists()` calls are two
        # instants, and a lock released between them reads as a violation that
        # never happened. A single directory read is one snapshot of both names.
        while not stop.is_set():
            try:
                names = os.listdir(lock)
            except OSError:
                continue
            if "owner" in names and "budget" not in names:
                bad.append(sorted(names))

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        for _ in range(300):
            held = pipeline._acquire_fg_lock(common, repo, wait=1, poll=0.01,
                                             stale=500, grace=30)
            assert pipeline._release_fg_lock(held) is True
    finally:
        stop.set()
        sampler.join(timeout=5)
    assert bad == [], f"{len(bad)} sample(s) saw an owner with no budget"


def test_an_existing_empty_lock_directory_is_never_REPLACED(tmp_path):
    """`mkdir` is the atomic no-replace primitive, and that is why it is used.

    `os.rename` of a prepared temp dir would silently replace an existing EMPTY
    directory -- clobbering a legacy holder caught between its own `mkdir` and
    its owner write, which is the one moment the lock cannot defend itself.
    """
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = _lock_of(repo)
    lock.mkdir(parents=True)               # a holder mid-initialization
    with pytest.raises(LockTimeout):
        _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert lock.is_dir()
    assert list(lock.iterdir()) == [], "the waiter wrote into someone's lock"
    assert _calls(tmp_path) == 0


def test_a_waiter_arriving_after_the_budget_but_before_the_owner_never_reclaims(
        tmp_path):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = _lock_of(repo)
    lock.mkdir(parents=True)
    pipeline._write_budget(lock, 100000)
    with pytest.raises(LockTimeout):
        _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert lock.is_dir()
    assert (lock / "budget").exists() and not (lock / "owner").exists()


def test_an_aged_bare_lock_directory_is_still_reclaimed(tmp_path):
    """Unchanged shipped behaviour, and the legacy-interop case: a lock with
    neither owner nor sidecar is an orphan once the write grace has passed."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = _lock_of(repo)
    lock.mkdir(parents=True)
    old = time.time() - 600
    os.utime(lock, (old, old))
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"


def test_a_large_batched_holder_is_not_reclaimed_by_a_small_waiter(tmp_path):
    """The reason the sidecar exists. Waiters reclaim on their OWN ceiling, so
    a small-diff waiter would take a live multi-batch holder's lock and put two
    reviews on one inference backend."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path, "\n[defaults]\ntimeout_sec = 1\n"
                           "timeout_retries = 0\ndegraded_retries = 0\n")
    cfg = load_config(repo)
    own = pipeline.lock_stale_ceiling_sec(cfg.defaults,
                                         pipeline.max_chain_width(cfg))
    lock = _lock_of(repo)
    age = own * 3
    _write_owner(lock, os.getpid(), int(time.time()) - age, repo)   # LIVE pid
    pipeline._write_budget(lock, own * 100)

    with pytest.raises(LockTimeout):
        _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert lock.is_dir() and _calls(tmp_path) == 0

    # Without the sidecar -- a legacy holder -- the very same lock IS stale by
    # the waiter's own ceiling. That is the recorded coexistence limitation,
    # and it is what makes the assertion above about the sidecar and nothing
    # else.
    (lock / "budget").unlink()
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"


def test_the_sidecar_grows_when_the_under_lock_plan_is_bigger(tmp_path, capsys,
                                                             monkeypatch):
    """Two-stage ordering: the pre-lock capture only SIZES the ceiling, and a
    long lock wait can change the worktree under us. The authoritative plan is
    the one built under the lock, and the sidecar must catch up -- a waiter
    reading the pre-wait number would reclaim a live holder."""
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path, BATCH_CFG)       # a small diff, for now
    cfg = load_config(repo)
    d = cfg.defaults
    seen: dict = {}
    real_acquire = pipeline._acquire_fg_lock
    real_chain = pipeline._run_chain

    def acquire(common_dir, worktree, *, wait, poll, stale, grace=30.0,
                budget_sec=None):
        seen["pre_lock_stale"] = stale
        held = real_acquire(common_dir, worktree, wait=wait, poll=poll,
                            stale=stale, grace=grace, budget_sec=budget_sec)
        seen["sidecar_at_acquire"] = pipeline._holder_budget(held.path)
        for i in range(3):      # the worktree grows while we hold the lock
            (Path(worktree) / f"g{i}.txt").write_text(_body(f"g{i}"),
                                                      encoding="utf-8")
        return held

    def chain(*a, **kw):
        seen.setdefault("sidecar_at_review",
                        pipeline._holder_budget(_lock_of(repo)))
        return real_chain(*a, **kw)

    monkeypatch.setattr(pipeline, "_acquire_fg_lock", acquire)
    monkeypatch.setattr(pipeline, "_run_chain", chain)
    rec = _run(repo, _store(tmp_path))

    assert rec["batch_count"] >= 2, "the growth must have made it over-budget"
    width = pipeline.max_chain_width(cfg)
    grown = float(budget.lock_stale_ceiling(d, width, rec["batch_count"]))
    assert seen["pre_lock_stale"] == float(
        pipeline.lock_stale_ceiling_sec(d, width))          # estimate: 0 batches
    assert seen["sidecar_at_acquire"] == seen["pre_lock_stale"]
    assert seen["sidecar_at_review"] == grown
    assert seen["sidecar_at_review"] > seen["sidecar_at_acquire"]
    assert not _lock_of(repo).exists()      # released, sidecar and all


def test_an_operator_stale_override_does_not_shrink_the_holders_own_budget(
        tmp_path, capsys, monkeypatch):
    """`SKODUN_LOCK_STALE_SECONDS` says how long a WAITER waits; the sidecar
    says how long this holder may legitimately need. They are different facts,
    so a small override cannot make peers reclaim a holder that is still inside
    its own budget — the recourse for a genuinely wedged lock is removing the
    lock directory, which is what the `LockTimeout` message says. (A DEAD
    holder is still reclaimed at once, override or not.)"""
    monkeypatch.setenv("SKODUN_LOCK_STALE_SECONDS", "5")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    cfg = load_config(repo)
    seen: dict = {}
    real = pipeline._run_chain

    def spy(*a, **kw):
        seen.setdefault("sidecar", pipeline._holder_budget(_lock_of(repo)))
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "_run_chain", spy)
    _run(repo, _store(tmp_path))

    assert seen["sidecar"] == float(pipeline.lock_stale_ceiling_sec(
        cfg.defaults, pipeline.max_chain_width(cfg)))
    assert seen["sidecar"] > 5


def test_an_operator_stale_override_does_not_shrink_the_budget_at_ACQUISITION(
        tmp_path, monkeypatch):
    """The test above samples the sidecar at the first model call, i.e. AFTER
    `_grow_lock_budget` has republished it from the authoritative under-lock
    plan. That leaves a window: acquisition itself publishes a number, and
    everything from `mkdir` until the under-lock capture finishes is spent
    advertising it. If acquisition published the operator's own
    `SKODUN_LOCK_STALE_SECONDS` instead of the ceiling this holder's diff
    implies, a peer polling inside that window reads a budget SMALLER than the
    holder needs and reclaims a live batched review -- the exact overlap the
    sidecar exists to prevent, reintroduced in the one window nothing sampled.

    So the assertion is on the value published AT ACQUISITION, and it is paired
    with the waiter-side consequence: with the holder aged past the override but
    still well inside its own ceiling, `_lock_is_reclaimable` at the waiter's
    small figure must answer False.
    """
    monkeypatch.setenv("SKODUN_LOCK_STALE_SECONDS", "5")
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    cfg = load_config(repo)
    ceiling = float(pipeline.lock_stale_ceiling_sec(
        cfg.defaults, pipeline.max_chain_width(cfg)))
    assert ceiling > 60, "the override has to be meaningfully smaller"
    seen: dict = {}
    real = pipeline._acquire_fg_lock

    def acquire(*a, **kw):
        held = real(*a, **kw)
        seen["at_acquire"] = pipeline._holder_budget(held.path)
        # Age the holder past the operator's 5s override, but leave it well
        # inside the ceiling its own plan legitimately needs. The pid stays
        # ours, so this is a LIVE holder and `_release_fg_lock`'s ABA guard
        # still recognises it on the way out.
        _write_owner(held.path, os.getpid(),
                     int(time.time() - ceiling / 2), repo)
        seen["reclaimable"] = pipeline._lock_is_reclaimable(held.path, 5.0, 30.0)
        return held

    monkeypatch.setattr(pipeline, "_acquire_fg_lock", acquire)
    _run(repo, _store(tmp_path))

    assert seen["at_acquire"] == ceiling, (
        "acquisition published the operator's waiter override as this holder's "
        "own budget")
    assert seen["reclaimable"] is False, (
        "a small-diff waiter could reclaim a live holder in the window between "
        "acquisition and the under-lock republish")
    assert not _lock_of(repo).exists()


def test_the_sidecar_is_never_shrunk_by_a_smaller_under_lock_plan(tmp_path):
    repo = _repo(tmp_path)
    lock = _lock_of(repo)
    lock.mkdir(parents=True)
    pipeline._write_budget(lock, 5000)
    assert pipeline._grow_budget(lock, 4000) is False
    assert pipeline._holder_budget(lock) == 5000.0
    assert pipeline._grow_budget(lock, 6000) is True
    assert pipeline._holder_budget(lock) == 6000.0


@pytest.mark.parametrize("raw", ["", "\n", "junk\n", "-5\n", "0\n", "12 34\n",
                                 "9" * 400, "1e6\n", " 90 \n"])
def test_an_unusable_sidecar_is_treated_as_absent(tmp_path, raw):
    """A corrupt sidecar must not become "never reclaim this lock".

    `9 * 400` is the case that matters most: `str.isdigit()` accepts it and
    `float()` cannot even represent it.
    """
    lock = tmp_path / "lock"
    lock.mkdir()
    (lock / "budget").write_text(raw, encoding="utf-8")
    expected = None
    line = raw.strip()
    if line.isdigit() and 0 < int(line) <= pipeline.LOCK_BUDGET_MAX_SEC:
        expected = float(line)
    assert pipeline._holder_budget(lock) == expected
    # ...and with no sidecar at all.
    (lock / "budget").unlink()
    assert pipeline._holder_budget(lock) is None


def test_a_dead_holder_is_reclaimed_however_large_its_sidecar_says(tmp_path):
    """The sidecar can only ever protect a holder that is genuinely ALIVE.

    Age is checked first, liveness second — so a published budget delays the
    age check and nothing else. A dead pid is still reclaimed at once, which is
    what keeps a nonsense sidecar from wedging anything.
    """
    from tests.test_pipeline import _spawned_pid
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    lock = _lock_of(repo)
    _write_owner(lock, _spawned_pid(), int(time.time()), repo)
    pipeline._write_budget(lock, pipeline.LOCK_BUDGET_MAX_SEC)
    rec = _run(repo, _store(tmp_path), lock_wait=1, lock_poll=0.2)
    assert rec["status"] == "clean"


def test_a_batched_run_releases_its_lock_and_its_sidecar(tmp_path, capsys):
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _oversized(tmp_path)
    _run(repo, _store(tmp_path))
    assert not _lock_of(repo).exists()


# --------------------------------------------------------------------------
# oracle anchors (skipped without $SKODUN_ORACLE_DIR)
# --------------------------------------------------------------------------


@requires_oracle
def test_oracle_batches_only_an_over_budget_diff():
    src = ORACLE.read_text(encoding="utf-8")
    assert src.count('if [ "$REVIEW_DIFF_BYTES" -gt "$MAX_DIFF_BYTES" ]; then') == 2
    assert "sh \"$SELF\" --run-batched" in src


@requires_oracle
def test_oracle_halves_the_batch_budget_for_context_headroom():
    src = ORACLE.read_text(encoding="utf-8")
    assert 'GROK_BATCH_BYTES="${GROK_REVIEW_BATCH_BYTES:-$MAX_DIFF_BYTES}"' in src
    assert "_batch_diff_budget=$((GROK_BATCH_BYTES / 2))" in src
    assert '[ "$_batch_diff_budget" -lt 1 ] && _batch_diff_budget=1' in src


@requires_oracle
def test_oracle_scales_the_recovery_budget_by_batches_plus_the_integration_pass():
    src = ORACLE.read_text(encoding="utf-8")
    assert "_batch_calls=$(( BATCH_COUNT + RUN_INTEGRATION ))" in src
    assert "BATCHED_MAX_RUNTIME=$(( _batch_calls * _batch_worst_runtime ))" in src
    # ...and it is persisted for the reaper, exactly as skodun persists
    # `worst_runtime_sec` on the record.
    assert '"max_runtime_seconds": int(os.environ.get("GR_MAXRUN_V","0") or 0)' in src


@requires_oracle
def test_oracle_aggregation_formulas_are_the_ones_ported():
    src = ORACLE.read_text(encoding="utf-8")
    # parse_ok is ALL, degraded/truncated are ANY, and the integration pass
    # participates in all three.
    assert "all_parsed = False" in src and "any_degraded = True" in src
    assert 'failed_batches.append("integration")' in src
    assert "trustworthy = bool(all_parsed and not any_degraded and not any_truncated)" in src
    # The first ABNORMAL stop reason wins.
    assert 'next((s for s in stop_reasons if s != "EndTurn"),' in src
    # Zero batches is a terminal failure, never a clean verdict.
    assert '"failure_reason": "diff batching produced no batches"' in src


@requires_oracle
def test_oracle_batch_prompt_labels_and_the_sole_batch_checklist_mode():
    src = ORACLE.read_text(encoding="utf-8")
    assert 'if [ "$BATCH_COUNT" -eq 1 ]; then _bcl_mode=full; else _bcl_mode=batch; fi' in src
    assert '"$GR_BRANCH (batch $_i/$BATCH_COUNT)"' in src
    assert '"$GR_HEAD -- batch $_i of $BATCH_COUNT; files: $_bfilelist"' in src
