"""Which skodun produced a record, and whether this process is still it.

A verdict is trusted across time: the gate honours a review recorded last week
whenever the diff identity still matches. So a change in how skodun classifies,
batches or scores is invisible in the records it left behind -- #92 turned a
junie envelope failure from `degraded` into `unavailable`, #99 gave
`openai-api` a degradation axis it did not have, and artifacts either side of
those merges describe the same provider behaviour with different verdicts and
nothing that says which rule applied.

The artifact already names WHO ANSWERED (`adapter`, `model`) and HOW THE HEAD
WAS CHOSEN (`route_reason`). This module supplies the last missing half of the
provenance: which skodun asked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import skodun
from skodun import provenance
from tests.test_pipeline import _isolate  # noqa: F401 - autouse, pins env/bins


@pytest.fixture(autouse=True)
def _uncached(monkeypatch):
    """Every test starts with the cache cold.

    The cache is not an optimisation to be tested around -- it is the module's
    contract (see `test_the_answer_is_the_code_this_process_started_with`), so
    each test has to say which side of it it is on.
    """
    monkeypatch.setattr(provenance, "_CACHED", None)


def test_the_version_is_the_package_s_own():
    assert provenance.code_provenance()["skodun_version"] == skodun.__version__


def test_an_editable_checkout_reports_its_commit():
    """This repo IS the install (pipx editable), so the commit is resolvable
    and must be the checkout's real HEAD -- not a guess, not a placeholder."""
    src = Path(skodun.__file__).resolve().parents[2]
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    commit = provenance.code_provenance()["skodun_commit"]

    assert commit is not None
    assert commit.split("-")[0] == head


def test_a_modified_checkout_says_so():
    """`-dirty`, the git-describe convention. During development the tree is
    usually modified, and a bare commit would name code that is not what ran --
    which is worse than saying nothing, because it invites belief."""
    src = Path(skodun.__file__).resolve().parents[2]
    # `status --porcelain` is the question "is this tree exactly HEAD", and it
    # is the one production asks -- `diff --quiet` here would disagree with it
    # on a tree whose only change is staged or untracked.
    clean = not subprocess.run(
        ["git", "-C", str(src), "status", "--porcelain"],
        capture_output=True, text=True).stdout.strip()
    commit = provenance.code_provenance()["skodun_commit"]

    assert commit.endswith("-dirty") is (not clean), (
        f"checkout clean={clean} but commit reads {commit!r}")


def test_a_frozen_install_reports_no_commit(monkeypatch, tmp_path):
    """A wheel is not a checkout. `None` is the honest answer, and the version
    still identifies the code."""
    monkeypatch.setattr(provenance, "_package_root", lambda: tmp_path)

    got = provenance.code_provenance()

    assert got["skodun_commit"] is None
    assert got["skodun_version"] == skodun.__version__


def test_git_being_broken_never_raises(monkeypatch, tmp_path):
    """Provenance is a record of what happened, not a precondition for it. A
    machine without git, or with a git that hangs, must still be able to
    review."""
    def boom(*a, **k):
        raise OSError("no git here")

    monkeypatch.setattr(provenance.subprocess, "run", boom)

    assert provenance.code_provenance()["skodun_commit"] is None


def test_the_answer_is_the_code_this_process_started_with(monkeypatch):
    """Cached ON PURPOSE, and this is the contract rather than a speed trick.

    A long-lived MCP server imported its modules at startup. If somebody runs
    `git pull` underneath it -- which is exactly what an editable install
    invites, and what happened on this machine mid-session -- the process goes
    on running the OLD code. Re-reading the commit per review would stamp
    verdicts with a commit that never produced them.
    """
    first = provenance.code_provenance()
    monkeypatch.setattr(provenance, "_read_commit", lambda root: "0" * 40)

    assert provenance.code_provenance() == first


def test_drift_is_reported_only_when_the_disk_really_moved(monkeypatch):
    """The detection half. Same commit -> `same`; a different one -> `moved`
    WITH the on-disk commit, so an operator sees what a restart would get."""
    provenance.code_provenance()                       # pin the startup answer
    assert provenance.stale_against_disk()[0] == provenance.DRIFT_SAME

    monkeypatch.setattr(provenance, "_read_commit", lambda root: "f" * 40)

    assert provenance.stale_against_disk() == (provenance.DRIFT_MOVED,
                                               "f" * 40)


def test_drift_is_silent_when_there_is_no_commit_to_compare(monkeypatch,
                                                            tmp_path):
    """A frozen install cannot drift, and must not claim to."""
    monkeypatch.setattr(provenance, "_package_root", lambda: tmp_path)
    provenance.code_provenance()

    assert provenance.stale_against_disk() == (provenance.DRIFT_UNCOMPARABLE,
                                               None)


def test_a_failed_disk_read_is_not_a_move(monkeypatch):
    """`-unknown` says we could not establish the tree's state, so comparing
    the whole string would read `abc-unknown` as different from `abc` and
    announce a move that never happened. One transient `index.lock` from a
    concurrent commit is enough to produce it, and the resulting line would be
    a confident claim built on a failed read -- the exact thing the `-unknown`
    suffix exists to prevent."""
    monkeypatch.setattr(provenance, "_CACHED",
                        {"skodun_version": "0.4.0", "skodun_commit": "a" * 40})
    monkeypatch.setattr(provenance, "_read_commit",
                        lambda root: "a" * 40 + "-unknown")

    assert provenance.stale_against_disk() == (provenance.DRIFT_UNREADABLE,
                                               None)


def test_a_different_commit_is_a_move_even_when_the_tree_state_is_unknown():
    """The other half of that rule: `-unknown` costs us the TREE state, not the
    hash. A different hash is a real move and must still be reported, or a
    checkout that moved during a failed `status` read would go unnoticed."""
    import skodun
    from unittest import mock

    with mock.patch.object(provenance, "_CACHED",
                           {"skodun_version": skodun.__version__,
                            "skodun_commit": "a" * 40}), \
         mock.patch.object(provenance, "_read_commit",
                           lambda root: "b" * 40 + "-unknown"):
        state, on_disk = provenance.stale_against_disk()

    assert (state, on_disk) == (provenance.DRIFT_MOVED, "b" * 40 + "-unknown")


def test_an_unreadable_checkout_says_stop_asking_not_all_is_well(monkeypatch):
    """`uncomparable` is not a quieter `same`, and a caller must be able to
    tell them apart: each probe costs two subprocesses, and a wheel install or
    a wedged git will never become answerable later in the session. A poller
    that read this as "no drift" would pay the whole timeout budget forever to
    re-learn the same nothing."""
    monkeypatch.setattr(provenance, "_CACHED",
                        {"skodun_version": "0.4.0", "skodun_commit": "a" * 40})
    monkeypatch.setattr(provenance, "_read_commit", lambda root: None)

    assert provenance.stale_against_disk() == (provenance.DRIFT_UNCOMPARABLE,
                                               None)


# --------------------------------------------------------------------------
# on the record itself
# --------------------------------------------------------------------------


def test_a_foreground_review_records_which_skodun_produced_it(tmp_path,
                                                              capsys):
    """End to end through the shipped pipeline: the fields have to survive the
    record being built, persisted and read back, not merely exist in a dict."""
    from tests.test_pipeline import CLEAN, _emit, _fake_grok, _repo, _run, _store

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)

    rec = _run(repo, _store(tmp_path))

    want = provenance.code_provenance()
    assert rec["skodun_version"] == want["skodun_version"]
    assert rec["skodun_commit"] == want["skodun_commit"]


def test_the_fields_reach_the_stored_artifact_not_just_the_returned_record(
        tmp_path, capsys):
    """The gate and `triage` read the ARTIFACT, so a field that lived only on
    the in-memory record would answer nobody's question later."""
    import json

    from skodun.store import Store
    from tests.test_pipeline import CLEAN, _emit, _fake_grok, _repo, _run

    _fake_grok(tmp_path, _emit(CLEAN))
    repo = _repo(tmp_path)
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        rec = _run(repo, st)
    with Store.open(db) as st:
        art = json.loads(st._c.execute(
            "SELECT artifact_json FROM reviews WHERE id=?",
            (rec["id"],)).fetchone()["artifact_json"])

    assert art["skodun_version"] == provenance.code_provenance()["skodun_version"]
    assert "skodun_commit" in art, "the commit key must be present even as null"


# --------------------------------------------------------------------------
# detection: has the checkout moved under a running process?
# --------------------------------------------------------------------------


def test_doctor_names_the_code_this_process_is_running(tmp_path, monkeypatch):
    """`version=` alone cannot answer "is this the code I just merged?" on an
    editable install, where every commit is still version 0.4.0."""
    from skodun import doctor

    monkeypatch.setattr(provenance, "_read_commit", lambda root: "abc1234" + "0" * 33)
    rep = doctor.run_doctor(repo=None, store_path=tmp_path / "s.db")
    line = next(l for l in rep.render().splitlines() if "package" in l)

    assert "abc1234" in line, line


def test_doctor_is_quiet_when_the_checkout_has_not_moved(tmp_path):
    """No drift, no noise. A diagnostic that always warns is one nobody
    reads."""
    from skodun import doctor

    provenance.code_provenance()
    rep = doctor.run_doctor(repo=None, store_path=tmp_path / "s.db")
    pkg = next(l for l in rep.render().splitlines() if "package" in l)

    assert "stale" not in pkg.lower(), pkg


def test_doctor_points_at_serverinfo_rather_than_claiming_to_detect_drift(
        tmp_path):
    """`doctor` cannot detect drift and must not imply it can.

    Every `doctor` run is a FRESH process: it fills its provenance cache from
    disk and would then re-read the same disk, so the two sides always agree
    and a drift warning there could never fire for an operator. It is also
    CLI-only by an explicit rule ("Do not invent" in AGENTS.md), so it never
    runs inside the long-lived MCP server where drift actually happens.

    What it can honestly give is the CLI's own commit, to compare against the
    `serverInfo.commit` the client shows -- which is what it now says.
    """
    from skodun import doctor

    rep = doctor.run_doctor(repo=None, store_path=tmp_path / "s.db")
    pkg = next(l for l in rep.render().splitlines() if "package" in l)

    assert "serverInfo" in pkg, pkg
    assert "restart" in pkg.lower(), pkg


def test_the_short_form_keeps_the_marker_it_exists_to_show():
    """Truncating the whole string drops `-dirty`/`-unknown` -- so the short
    form would read as a clean exact commit at the one moment that is least
    true. Found by running `doctor` on a modified tree and seeing a bare hash.
    """
    assert provenance.short("a" * 40) == "a" * 12
    assert provenance.short("a" * 40 + "-dirty") == "a" * 12 + "-dirty"
    assert provenance.short("a" * 40 + "-unknown") == "a" * 12 + "-unknown"
    assert provenance.short(None) == "unknown"


def test_doctor_shows_the_dirty_marker_not_a_bare_hash(tmp_path, monkeypatch):
    from skodun import doctor

    monkeypatch.setattr(provenance, "_read_commit",
                        lambda root: "d" * 40 + "-dirty")
    rep = doctor.run_doctor(repo=None, store_path=tmp_path / "s.db")
    pkg = next(l for l in rep.render().splitlines() if "package" in l)

    assert "-dirty" in pkg, pkg


def test_provenance_is_read_before_the_foreground_lock_is_taken(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """Two git subprocesses is unbudgeted work, and it must not happen while
    holding the lock every other review is queued behind.

    On a normal checkout it is ~27ms, but the timeout exists because git can
    wedge -- a network filesystem, a stuck index lock -- and that whole budget
    would otherwise be spent inside the critical section, delaying peers for
    work that has nothing to do with reviewing. Warming the cache first is
    free: the record built deep inside the lock then reads it from memory.
    """
    from skodun import pipeline
    from tests.test_pipeline import CLEAN, _emit, _fake_grok, _repo, _run, _store

    order: list[str] = []
    real_lock = pipeline._acquire_fg_lock
    real_prov = provenance.code_provenance

    def spy_lock(*a, **k):
        order.append("lock")
        return real_lock(*a, **k)

    def spy_prov():
        order.append("provenance")
        return real_prov()

    monkeypatch.setattr(pipeline, "_acquire_fg_lock", spy_lock)
    monkeypatch.setattr(pipeline.provenance, "code_provenance", spy_prov)
    _fake_grok(tmp_path, _emit(CLEAN))

    _run(_repo(tmp_path), _store(tmp_path))

    assert "provenance" in order and "lock" in order
    assert order.index("provenance") < order.index("lock"), order
