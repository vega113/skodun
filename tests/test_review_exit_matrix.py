"""`skodun review`'s exit code, pinned as a table, on both surfaces.

Issue #79 reported the one failure this file exists to make impossible: a
review that ends `trustworthy=false` reporting process exit **0**, so that an
agent keying on the exit code treats it as success and only a later `skodun
gate` reveals there is no coverage. The three shapes it named were a degraded
grok run, an agy run that came back `status: ERROR`, and a clean finder whose
skeptic pass could not run and demoted the record to `failed`.

The behaviour is correct today -- `services.svc_review` returns 4 whenever the
persisted record is not `trustworthy is True`, and it has since the services
layer was introduced -- and the codes are documented in `README.md` beside the
gate's own 0/1/2. What was missing is a test that says so: the suite pinned
the exhausted-chain 4 (`test_fallback`) and the clean 0 / finding 1 pair
(`test_pipeline`), and NOTHING pinned the two demotion shapes #79 actually
hit. A single edit to that one `is not True` would have shipped green.

So the matrix below is the contract, and every row is driven end to end
through `cli.main(["review", ...])` -- a real repo, a real child process, a
real record. The two properties that matter are asserted as properties rather
than as a list of expected numbers:

* **Exit 0 implies coverage.** Every row that exits 0 must leave a record that
  is `trustworthy` with no open findings -- that is what a caller reads a 0 as.
* **Untrustworthy implies non-zero.** Every row whose record is not
  trustworthy must exit non-zero, whatever the reason it is untrustworthy.

`_UNTRUSTWORTHY_EXIT` is `4` rather than "anything non-zero" because 1 already
means something incompatible (a trustworthy review WITH findings, which the
gate can be satisfied by triaging) and a caller that saw 1 for an untrustworthy
run would look for findings to triage that do not exist.

The MCP half is here too: `isError` is `status != 0`, so a surface that
answered 0 would tell an agent the review succeeded in exactly the same way.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from skodun import mcpserver
from skodun.cli import main
from skodun.store import Store
from tests.test_pipeline import (CFG, CLEAN, DIRTY, _emit, _fake_grok, _repo,
                                 _isolate)      # noqa: F401 - autouse fixture

#: The one exit code for "a review ran and certifies nothing".
_UNTRUSTWORTHY_EXIT = 4

#: A grok envelope whose terminal reason is not a completion. This is #79's
#: first two shapes -- `stop_reason=ERROR` from agy, and grok's own truncation
#: -- as they reach the trust axes: `degraded=true`, so `trustworthy=false`.
DEGRADED = json.dumps({"structuredOutput": {"summary": "s", "findings": []},
                       "stopReason": "ERROR"})

#: A finder chain with a `role = "refuter"` entry on a provider whose binary is
#: not there. A clean finder schedules the SKEPTIC pass, `_pass_reviewer`
#: prefers the refuter-role entry for it, the pass produces nothing, and
#: `passes.merge_failed_extra_pass` demotes the whole record -- #79's third
#: shape, reported as "finder clean + skeptic fail ... status=failed,
#: findings=0, good summary".
CFG_WITH_DEAD_SKEPTIC = CFG + """
[[reviewers]]
name     = "refuter"
provider = "openai"
model    = "gpt-test-0309"
role     = "refuter"
"""


def _record(tmp_path: Path) -> dict:
    """The record the run just persisted, read back from the pinned store."""
    with Store.open(Path(os.environ["SKODUN_DB"])) as st:
        rows = st.list_reviews(None, 10)
    assert rows, "the run persisted no record at all"
    return rows[0]


def _at(rec: dict, *keys):
    """Walk nested mappings, returning None the moment one is not there."""
    node = rec
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _first_attempt(rec: dict) -> dict:
    """`attempts[0]`, or an empty dict when there are none."""
    attempts = rec.get("attempts")
    if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict):
        return attempts[0]
    return {}


def _clean(tmp_path, monkeypatch):
    _fake_grok(tmp_path, _emit(CLEAN))
    return _repo(tmp_path)


def _with_findings(tmp_path, monkeypatch):
    _fake_grok(tmp_path, _emit(DIRTY))
    return _repo(tmp_path)


def _degraded(tmp_path, monkeypatch):
    """#79 rows 1 and 2: the reviewer answered, and the answer is not usable."""
    _fake_grok(tmp_path, _emit(DEGRADED))
    return _repo(tmp_path, "\n[defaults]\ndegraded_retries = 0\n")


def _skeptic_demotion(tmp_path, monkeypatch):
    """#79 row 3: the finder was clean and an extra pass could not run."""
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "no-such-codex"))
    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG_WITH_DEAD_SKEPTIC, encoding="utf-8")
    return repo


def _no_reviewer_could_run(tmp_path, monkeypatch):
    """The shape that was already pinned, kept in the table so the matrix is
    the whole matrix rather than the part nobody had covered yet."""
    from tests.test_fallback import _fake_cli
    _fake_cli(tmp_path, "grok", "exit 127")
    return _repo(tmp_path)


#: `(name, build, expected exit, shape)`. `build` leaves a repo ready to
#: review; `shape` is what the persisted record must look like for the row to
#: be pinning the failure it claims to.
#:
#: The shapes are not decoration. Every untrustworthy row expects the SAME
#: code, so without them a row could quietly start failing for a different
#: reason -- a preflight refusal, a chain that never ran -- and go on passing
#: while the demotion path it was written for stopped being exercised. That is
#: the failure mode #79 itself had: the codes were right, and nothing checked
#: that the interesting paths still reached them.
#
# Every predicate reads through `.get()` and never indexes a list directly, so
# a record whose SHAPE changed returns False and gets the row-specific message
# below rather than raising a `KeyError` out of a lambda -- which is exactly
# the moment the message is worth having.
_MATRIX = (
    ("clean", _clean, 0,
     lambda r: r.get("status") == "clean" and r.get("findings_total") == 0),
    ("findings open", _with_findings, 1,
     lambda r: r.get("trustworthy") is True and r.get("findings_total", 0) > 0),
    # parse_ok TRUE beside degraded TRUE: the reviewer answered and the answer
    # validated. Only the degradation axis makes this untrustworthy, which is
    # what made exit 0 plausible enough to be reported.
    ("degraded answer", _degraded, _UNTRUSTWORTHY_EXIT,
     lambda r: r.get("degraded") is True and r.get("parse_ok") is True),
    # The finder was CLEAN and the pass is what demoted the record.
    ("extra-pass demotion", _skeptic_demotion, _UNTRUSTWORTHY_EXIT,
     lambda r: (r.get("status") == "failed" and r.get("findings_total") == 0
                and _at(r, "extra_passes", "skeptic", "failed") is True)),
    # A provider process can start and still return an unusable invocation
    # result. That is a runtime failure (exit 4), distinct from the new static
    # missing-binary preflight refusal (exit 2).
    ("no reviewer could run", _no_reviewer_could_run, _UNTRUSTWORTHY_EXIT,
     lambda r: (r.get("status") == "failed"
                and _first_attempt(r).get("rc") == 127
                and "all providers unavailable" in r.get("failure_reason", ""))),
)

_ROWS = [(name, build, code) for name, build, code, _ in _MATRIX]


@pytest.mark.parametrize("name,build,expected,shape",
                         _MATRIX, ids=[row[0] for row in _MATRIX])
def test_the_cli_exit_code_matches_the_documented_matrix(
        name, build, expected, shape, tmp_path, monkeypatch, capsys):
    repo = build(tmp_path, monkeypatch)

    code = main(["review", "--repo", str(repo)])

    banner = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == expected, f"{name}: {banner}"
    assert banner.startswith("SKODUN VERDICT: ")
    rec = _record(tmp_path)
    assert shape(rec), (
        f"{name}: this row no longer exercises the failure it pins -- "
        f"status={rec.get('status')!r} trustworthy={rec.get('trustworthy')!r} "
        f"reason={rec.get('failure_reason')!r}")


@pytest.mark.parametrize("name,build,expected",
                         _ROWS, ids=[row[0] for row in _ROWS])
def test_an_untrustworthy_record_never_exits_zero(
        name, build, expected, tmp_path, monkeypatch, capsys):
    """The property behind the table, checked against the RECORD rather than
    against the expectation -- so a future change that made a scenario
    trustworthy (or stopped it being so) has to move the row deliberately."""
    repo = build(tmp_path, monkeypatch)

    code = main(["review", "--repo", str(repo)])
    capsys.readouterr()

    rec = _record(tmp_path)
    if rec.get("trustworthy") is not True:
        assert code != 0, f"{name}: an untrustworthy record exited 0"
        assert code == _UNTRUSTWORTHY_EXIT, (
            f"{name}: untrustworthy runs are {_UNTRUSTWORTHY_EXIT}; 1 means a "
            f"TRUSTWORTHY review with findings to triage")
    if code == 0:
        assert rec.get("trustworthy") is True, f"{name}: 0 without coverage"
        assert not rec.get("findings"), f"{name}: 0 with open findings"


@pytest.mark.parametrize("name,build,expected",
                         _ROWS, ids=[row[0] for row in _ROWS])
def test_the_mcp_review_tool_reports_the_same_status_and_flags_errors(
        name, build, expected, tmp_path, monkeypatch, capsys):
    """CLI/MCP parity on the axis #79 is about.

    An agent does not see a process exit; it sees `isError`, which the
    transport derives from this status. A surface that answered 0 for an
    untrustworthy review would mislead an agent exactly as the reported exit 0
    misled a shell caller -- so the tool is driven through the same registry
    seam the server uses, against the same scenarios.
    """
    repo = build(tmp_path, monkeypatch)
    db = Path(os.environ["SKODUN_DB"])
    spec = {s.name: s for s in mcpserver.default_registry()}["review"]

    res = spec.handler(mcpserver.HandlerCall(
        params={"repo": str(repo)},
        store_factory=lambda: Store.open(db),
        cancel=threading.Event()))

    assert res.status == expected, f"{name}: {res.text}"
    envelope = mcpserver.tool_result(res)
    assert envelope["isError"] is (expected != 0), name
    assert envelope["structuredContent"]["status"] == expected


def test_the_readme_documents_every_code_this_matrix_produces():
    """The other half of #79's ask -- "document the exit matrix next to gate's
    0/1/2" -- kept honest. The table in README.md is what an operator reads
    before wiring `skodun review` into anything, and a code this suite can
    produce and that table does not mention is a code nobody was warned about.
    """
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8")
    row = next(line for line in readme.splitlines()
               if line.startswith("| `skodun review"))
    for code, meaning in ((0, "trustworthy and clean"),
                          (1, "trustworthy, findings open"),
                          (2, "preflight refusal"),
                          (3, "gave up waiting"),
                          (4, "no trustworthy review")):
        assert f"`{code}`" in row, code
        assert meaning in row, meaning
