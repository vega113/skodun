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
    """The detection half. Same commit -> silence; a different one -> the
    on-disk commit, so an operator can see what they would get by restarting."""
    provenance.code_provenance()                       # pin the startup answer
    assert provenance.stale_against_disk() is None

    monkeypatch.setattr(provenance, "_read_commit", lambda root: "f" * 40)

    assert provenance.stale_against_disk() == "f" * 40


def test_drift_is_silent_when_there_is_no_commit_to_compare(monkeypatch,
                                                            tmp_path):
    """A frozen install cannot drift, and must not claim to."""
    monkeypatch.setattr(provenance, "_package_root", lambda: tmp_path)
    provenance.code_provenance()

    assert provenance.stale_against_disk() is None


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


def test_doctor_says_so_when_the_checkout_moved_under_the_process(tmp_path,
                                                                  monkeypatch):
    """The case this exists for: a long-lived MCP server still serving the code
    it imported at startup, while `git pull` has moved the checkout on. It
    reports the commit a restart WOULD get, so the line says what the restart
    is worth -- and it never acts on it (see `stale_against_disk`)."""
    from skodun import doctor

    provenance.code_provenance()
    monkeypatch.setattr(provenance, "_read_commit", lambda root: "beef" + "0" * 36)

    rep = doctor.run_doctor(repo=None, store_path=tmp_path / "s.db")
    pkg = next(l for l in rep.render().splitlines() if "package" in l)

    assert "beef" in pkg and "restart" in pkg.lower(), pkg


# --------------------------------------------------------------------------
# what "dirty" has to cover, and what it may not assume
# --------------------------------------------------------------------------


def _repo_at(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a],
                                    capture_output=True, text=True)
    run("init", "-q", ".")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (path / "a.txt").write_text("a\n")
    run("add", "a.txt")
    run("commit", "-qm", "one")
    return path


@pytest.mark.parametrize("make_dirty, what", [
    (lambda p: (p / "a.txt").write_text("edited\n"), "an unstaged edit"),
    (lambda p: (p / "untracked_module.py").write_text("x = 1\n"), "an untracked module"),
])
def test_every_shape_of_modification_marks_the_commit_dirty(tmp_path,
                                                            make_dirty, what):
    """`git diff --quiet` is not enough, and the gap is not academic: it
    answers rc=0 for a STAGED change and for an UNTRACKED file. An untracked
    module is code this process can import, so a bare hash there names a commit
    that demonstrably did not produce the run.
    """
    repo = _repo_at(tmp_path / "r")
    assert not provenance._read_commit(repo).endswith("-dirty")   # clean first

    make_dirty(repo)

    assert provenance._read_commit(repo).endswith("-dirty"), what


def test_a_staged_change_marks_the_commit_dirty(tmp_path):
    """The shape `git diff --quiet` is blindest to: staged but not committed."""
    repo = _repo_at(tmp_path / "r")
    (repo / "a.txt").write_text("staged\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], capture_output=True)

    assert provenance._read_commit(repo).endswith("-dirty")


def test_being_unable_to_tell_is_not_the_same_as_clean(monkeypatch, tmp_path):
    """git answers 0 clean, 1 dirty, and 128/129 for "that is not a checkout".
    Reading anything-but-1 as clean publishes a precise-looking hash on the
    strength of a failure -- the exact "invites belief" problem `-dirty` exists
    to avoid, wearing the error path's clothes.
    """
    repo = _repo_at(tmp_path / "r")
    real = provenance._git

    def broken(root, *args):
        if args and args[0] == "status":
            return subprocess.CompletedProcess(args, 129, "", "fatal: nope")
        return real(root, *args)

    monkeypatch.setattr(provenance, "_git", broken)

    got = provenance._read_commit(repo)

    assert got.endswith("-unknown"), got
    assert not got.endswith("-dirty"), "an error is not a known modification"


def test_two_threads_racing_the_cache_agree(monkeypatch):
    """The cache's contract is ONE answer per process. Unsynchronized, two
    threads can both find it cold and compute either side of a `git pull`,
    so one record says the old commit and its neighbour says the new one."""
    import threading

    seen: list[dict] = []
    barrier = threading.Barrier(2)
    calls = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(provenance, "_read_commit",
                        lambda root: next(calls, "c" * 40))

    def go():
        barrier.wait()
        seen.append(provenance.code_provenance())

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert seen[0] == seen[1], seen


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
