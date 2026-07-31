"""THE two-repository drill: one store, two repositories, one branch name.

Every other test in this suite looks at one seam. This one looks at the whole
defect, which is what Phase 4 exists to close and what was reproduced live
during the Phase 3 final review:

  * a push of repo A's branch returned repo B's live reservation in
    `Reservation.superseded`, WITH ITS PID, so `signal_superseded` SIGTERMed an
    unrelated running worker (the `ps` guard passed -- it *is* a skodun worker);
  * a single `surface` call rendered AND permanently acknowledged both
    repositories' rounds, after which the other repository's session surfaced
    nothing.

So: two real git repositories, one `SKODUN_DB`, the same branch name in both,
and the real entry points -- `dispatch.run_dispatch`, `dispatch.run_worker`,
`cli.main(["surface", ...])`, `cli.main(["log", ...])`, `gate.run_gate`. Not
`Store` methods called directly; those are pinned in `test_store.py`, and a
scope that the transports cannot aim is a scope the user does not have.

WHAT THE FIXTURE DELIBERATELY DOES, and why each choice is load-bearing:

* **The two repositories are byte-identical.** Same content, same message, same
  identity, same pinned author/committer dates -- so their commit shas, their
  `base_sha` and their outgoing diff BYTES are equal. That is not decoration:
  it makes the gate assertion below a statement about *repositories* rather
  than a statement about content that happened to differ, and it makes every
  scoped query's answer depend on the `repo` column and nothing else. The
  fixture asserts its own premise (`repo_a`'s HEAD == `repo_b`'s HEAD) rather
  than trusting git to have been deterministic.
* **A's push has a positive control.** Repo A carries a running row of its own,
  which the push MUST retire and MUST hand to `signal_superseded`. Without it,
  "B's row survived" would also pass against a supersede that does nothing at
  all.
* **Dedup is off in the config** (`[dispatch] dedup = false`). Suppression is
  content-addressed and deliberately NOT repo-scoped (design spec, "Deliberately
  NOT repo-scoped"), and with byte-identical repositories it would therefore
  suppress A's push against B's finished round -- turning the drill's one real
  push into a no-op. Turning it off keeps the drill about the three scoped
  queries.
* **Every seeded row gets its own `reviewed_at`.** `skodun log` prints no review
  id, so the timestamp is the only thing in a listing that identifies a row --
  and both repositories' rows share a branch, a head and a status.

`tests/test_seams.py` pins `gate.py` and `trust.py` byte-for-byte; nothing here
edits either. What this file adds is the pin on the *decision* those bytes
express: the gate matches by content across repositories, on purpose.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from skodun import dispatch, gitio
from skodun.cli import main as cli_main
from skodun.config import load_config
from skodun.dispatch import (DedupEvidence, reserved_budget, run_dispatch,
                             run_worker)
from skodun.gate import run_gate
from skodun.store import Store
from tests.test_pipeline import CFG, DIRTY, _emit, _fake_grok

BRANCH = "feat"

#: git's "this ref does not exist on the remote yet" oid, as the pre-push
#: protocol spells it on the remote-oid field of a new branch's line.
ZERO = "0" * 40

#: Dedup off (see the module docstring); everything else is the shipped default.
_DRILL_CFG = CFG + "\n[dispatch]\ndedup = false\n"

#: Pinned into BOTH the author and the committer date of every commit this file
#: makes. A commit sha is computed from the tree, the parent, the two identities
#: and the two dates -- pin all of them and two independently built repositories
#: have the same history, which is the fixture's whole point.
_WHEN = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """`test_pipeline.py`'s isolation, plus a hermetic git.

    `SKODUN_DB` is the store BOTH repositories share -- that is the drill's
    subject, not an accident of the environment. The `GIT_CONFIG_*` pins are
    what make the commits below reproducible: an ambient `commit.gpgsign` or a
    `user.name` from the developer's own config would change the commit object
    and the two repositories would stop being identical.
    """
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "shared.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "bin" / "grok"))
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    monkeypatch.setenv("SKODUN_LOCK_WAIT_SECONDS", "5")
    monkeypatch.setenv("SKODUN_LOCK_POLL_SECONDS", "0.05")
    monkeypatch.delenv("SKODUN_LOCK_STALE_SECONDS", raising=False)
    # Never inherited: an ambient bypass in the developer's shell would turn the
    # drill's one push into a no-op that still passed, and an ambient
    # `SKODUN_GATE_SKIP` would make the gate assertion vacuous.
    monkeypatch.delenv("SKODUN_PREPUSH_SKIP", raising=False)
    monkeypatch.delenv("SKODUN_GATE_SKIP", raising=False)
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        empty = tmp_path / name.lower()
        empty.write_text("", encoding="utf-8")
        monkeypatch.setenv(name, str(empty))


# --------------------------------------------------------------------------
# two repositories that are the same repository in every respect but identity
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str, **env_extra: str) -> str:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "-m", message,
         GIT_AUTHOR_DATE=_WHEN, GIT_COMMITTER_DATE=_WHEN)


def _build_repo(root: Path) -> Path:
    """A repository with `main` and one `feat` commit on top, DETERMINISTICALLY.

    Two calls with different paths produce histories with identical shas, so the
    two repositories differ in exactly one thing: where they are. That is the
    variable the whole phase is about.
    """
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.quotepath", "true")
    (root / ".skodun.toml").write_text(_DRILL_CFG, encoding="utf-8")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _commit(root, "c0")
    _git(root, "checkout", "-b", BRANCH)
    (root / "a.txt").write_text("two\nthree\n", encoding="utf-8")
    _git(root, "add", ".")
    _commit(root, "c1")
    return root


def _ts(seconds_ago: int) -> str:
    """A canonical store timestamp `seconds_ago` in the past.

    Seconds, not days: every seeded `running` row must survive the stale sweep
    that `run_dispatch` performs before it dispatches anything, and a row aged
    past the config's worst-case runtime would be reclaimed by that sweep rather
    than left alone by the supersede -- which is a different test (see
    `test_a_stale_running_row_from_another_repository_is_still_swept`).
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - seconds_ago))


def _push_line(repo: Path) -> str:
    return (f"refs/heads/{BRANCH} {_git(repo, 'rev-parse', BRANCH)} "
            f"refs/heads/{BRANCH} {ZERO}\n")


def _identity(repo: Path) -> dict:
    """The push identity of `repo`'s `feat` tip, exactly as the dispatcher sees it."""
    head = _git(repo, "rev-parse", BRANCH)
    base = gitio.resolve_ref_base(repo, head)
    diff = gitio.capture_ref_diff(repo, base.sha, head)
    return {"head": head, "base_ref": base.ref, "base_sha": base.sha,
            "diff_hash": gitio.diff_identity(diff.data),
            "budget": reserved_budget(load_config(repo), diff.data)}


def _reserve(db: Path, repo: Path, ident: dict, *, at: str,
             diff_hash: str | None = None) -> str:
    """One reserved `running` row, through the REAL reservation path.

    `DedupEvidence(valid=False)` because these rows are the drill's furniture,
    not its subject: invalid evidence can never suppress, so seeding a row can
    never silently become "no row was seeded".
    """
    with Store.open(db) as st:
        res = st.reserve_prepush(
            BRANCH, ident["head"], ident["base_ref"], ident["base_sha"],
            diff_hash or ident["diff_hash"], ident["budget"],
            DedupEvidence(enabled=False, valid=False,
                          candidate_context_hash=None),
            repo=str(gitio.git_common_dir(repo)), now=at)
        assert res.record_id is not None, "the fixture's own reservation was suppressed"
        return res.record_id


def _pre_v5_row(db: Path, rid: str, *, at: str, status: str,
                findings: list | None = None) -> str:
    """A row with NO `repo` key at all -- what a v4 store's rows look like.

    v5 backfills nothing, so `repo IS NULL` forever, and `repo = ?` never matches
    NULL. That is the NULL rule, and it is a DECISION (fail-closed: an invisible
    old row beats the wrong repository's worker being killed), which is why it is
    asserted here rather than left to SQL's semantics.
    """
    rec = {
        "id": rid, "reviewed_at": at, "branch": BRANCH, "head": "0" * 40,
        "base_ref": "main", "base_sha": "b" * 40, "diff_hash": "e" * 40,
        "mode": "prepush", "source": "skodun", "status": status,
        "parse_ok": status != "running", "degraded": False,
        "diff_truncated": False, "usable_output": status != "running",
        "findings": findings or [], "findings_total": len(findings or []),
        "summary": f"pre-v5 {status}",
    }
    with Store.open(db) as st:
        st.save_review(rec)
        assert st._c.execute("SELECT repo FROM reviews WHERE id=?",
                             (rid,)).fetchone()["repo"] is None
    return rid


@dataclass(frozen=True)
class World:
    """Two repositories, one store, and every row seeded into it."""

    db: Path
    repo_a: Path
    repo_b: Path
    common_a: str
    common_b: str
    ident: dict
    a_old: str          # A's earlier running row -- the positive control
    b_running: str      # B's running row -- the reservation that got SIGTERMed
    b_round: str        # B's FINALIZED background round -- what `surface` delivers
    null_running: str   # a pre-v5 running row
    null_round: str     # a pre-v5 finished round
    at: dict            # every seeded row's `reviewed_at`, by id


@pytest.fixture
def world(tmp_path) -> World:
    _fake_grok(tmp_path, _emit(DIRTY))
    db = tmp_path / "shared.db"
    repo_a = _build_repo(tmp_path / "A")
    repo_b = _build_repo(tmp_path / "B")

    # THE FIXTURE'S OWN PREMISE, asserted rather than assumed. If git ever stops
    # producing identical commits from identical inputs, the gate assertion below
    # would silently become a test of "different content does not match".
    assert _git(repo_a, "rev-parse", "HEAD") == _git(repo_b, "rev-parse", "HEAD")
    assert _git(repo_a, "rev-parse", BRANCH) == _git(repo_b, "rev-parse", BRANCH)
    common_a = str(gitio.git_common_dir(repo_a))
    common_b = str(gitio.git_common_dir(repo_b))
    assert common_a != common_b, "the two repositories must not share a git dir"

    ident = _identity(repo_a)
    assert ident == _identity(repo_b), (
        "the two repositories must present the SAME push identity: that is what "
        "makes every assertion below a statement about the repo column")

    at = {}
    at["a_old"], at["b_round"] = _ts(9), _ts(8)
    at["b_running"], at["null_running"], at["null_round"] = _ts(7), _ts(6), _ts(5)

    a_old = _reserve(db, repo_a, ident, at=at["a_old"], diff_hash="a" * 40)
    # B's DELIVERABLE round, finalized by the real worker path rather than
    # written by hand: `finalize_review` binds every column from the worker's
    # dict, so a round that was only ever reserved cannot show whether the repo
    # survives the finalize (design spec, correction 1).
    #
    # BEFORE B's running row, and the order is forced: a reservation retires the
    # repository's OWN running rows on the same branch, so reserving this one
    # second would supersede the row assertion 1 is about.
    b_round = _reserve(db, repo_b, ident, at=at["b_round"])
    assert run_worker(b_round, repo_b, BRANCH, ident["head"], ident["base_sha"],
                      ident["base_ref"], db).code == 0
    b_running = _reserve(db, repo_b, ident, at=at["b_running"], diff_hash="c" * 40)
    null_running = _pre_v5_row(db, "sk_prev5_run", at=at["null_running"],
                               status="running")
    null_round = _pre_v5_row(
        db, "sk_prev5_done", at=at["null_round"], status="clean",
        findings=[{"file": "a.txt", "line": 1, "severity": "high",
                   "category": "bug", "title": "pre-v5 finding", "detail": "d"}])

    with Store.open(db) as st:
        for rid in (a_old, b_running, null_running):
            assert st.attach_pid(rid, 999_000 + len(rid)), rid
        # Assertion 6, in the fixture, about B's side of it: a round that has
        # been through `finalize_review` must still carry its repository. Were
        # this NULL, every "A's surface did not render B's round" below would be
        # true because B's round is deliverable to NOBODY.
        assert st.get_review(b_round).get("repo") == common_b, (
            "assertion 6: B's FINALIZED background round lost its repository")
    return World(db=db, repo_a=repo_a, repo_b=repo_b, common_a=common_a,
                 common_b=common_b, ident=ident, a_old=a_old,
                 b_running=b_running, b_round=b_round,
                 null_running=null_running, null_round=null_round, at=at)


def _delivered(db: Path) -> set[str]:
    with Store.open(db) as st:
        return {r["review_id"] for r in
                st._c.execute("SELECT review_id FROM deliveries").fetchall()}


def _statuses(db: Path) -> dict[str, str]:
    with Store.open(db) as st:
        return {r["id"]: r["status"] for r in
                st._c.execute("SELECT id, status FROM reviews").fetchall()}


def _stamps(text: str) -> set[str]:
    """The `reviewed_at` of every row a `skodun log` listing printed.

    `svc_log` prints no review id, and both repositories' rows share a branch, a
    head and a status -- the timestamp is the only column that tells them apart,
    which is why the fixture gives every seeded row its own.
    """
    return {line.split(" | ")[0].lstrip("! ").strip()
            for line in text.splitlines() if line.strip()}


# --------------------------------------------------------------------------
# THE DRILL
# --------------------------------------------------------------------------


def test_two_repositories_sharing_one_store_do_not_collide(world, monkeypatch,
                                                           capsys):
    """One push in A, observed through all three scoped queries. See the module
    docstring for the shape of the world this runs against."""
    handed: list[tuple] = []
    spawned: list[str] = []

    def _spy_signal(retired):
        # The exact value the reservation RETURNED. Spying here rather than on
        # `os.kill` is deliberate: returning the row is the defect, and a
        # `pid_is_skodun_worker` guard that happens to reject the pid today must
        # not be able to hide it.
        handed.append(tuple(retired or ()))
        return 0

    class _Proc:
        pid = os.getpid()

    def _spy_spawn(store, record_id, *a, **kw):
        spawned.append(record_id)
        return _Proc()

    monkeypatch.setattr(dispatch, "signal_superseded", _spy_signal)
    monkeypatch.setattr(dispatch, "spawn_worker", _spy_spawn)

    assert run_dispatch(_push_line(world.repo_a), world.repo_a, world.db) == 0
    assert len(spawned) == 1, "the push must have reserved exactly one round"
    a_new = spawned[0]

    # ---- 1. the supersede is scoped -------------------------------------
    #
    # A's push retires A's own in-flight round and hands it over to be
    # signalled -- the POSITIVE CONTROL, without which everything below would
    # also pass against a supersede that does nothing. It must retire and hand
    # over nothing else.
    assert [tuple(r["id"] for r in call) for call in handed] == [(world.a_old,)], (
        "assertion 1: the reservation returned another repository's running row "
        "for signalling -- that is what SIGTERMed an unrelated worker")
    assert all(r["pid"] for call in handed for r in call), (
        "assertion 1: the control row must carry a pid, or 'nothing was "
        "signalled' would be true for the wrong reason")
    status = _statuses(world.db)
    assert status[world.a_old] == "superseded", "assertion 1 (control)"
    assert status[world.b_running] == "running", (
        "assertion 1: A's push retired the OTHER repository's running round")
    assert status[world.null_running] == "running", (
        "assertion 5: a pre-v5 row (repo IS NULL) must not be retired on the "
        "strength of its branch name alone")

    # The round A just reserved is finalized through the REAL worker path, so
    # everything below is asserted against a round that has been through
    # `finalize_review` -- not against a reservation no worker ever touched.
    assert run_worker(a_new, world.repo_a, BRANCH, world.ident["head"],
                      world.ident["base_sha"], world.ident["base_ref"],
                      world.db).code == 0
    with Store.open(world.db) as st:
        final = st.get_review(a_new)
    assert final["repo"] == world.common_a, (
        "assertion 6: a FINALIZED background round lost its repository. "
        "`finalize_review` binds every column from the worker's dict, so this "
        "is where the whole phase goes inert -- with a NULL here, assertions "
        "2 and 3 pass against rounds nothing can ever deliver")
    assert final["status"] == "clean" and final["findings_total"] == 1, (
        "assertion 6: the round must be deliverable and content-bearing, or "
        "'A's surface did not render B's round' proves nothing")

    # ---- 2. `surface` is scoped, and does not acknowledge across ---------
    assert cli_main(["surface", "--repo", str(world.repo_a)]) == 0
    out_a = capsys.readouterr().out
    assert a_new in out_a, "assertion 2: A's own round was not delivered to A"
    for stranger, why in ((world.b_round, "another repository's"),
                          (world.null_round, "a pre-v5 (repo IS NULL)")):
        assert stranger not in out_a, f"assertion 2: A's surface rendered {why} round"
    # A's own retired round is delivered too -- `superseded` is a terminal
    # status and the reader is told where the story continued.
    assert _delivered(world.db) == {world.a_old, a_new}, (
        "assertion 2: `surface` ACKNOWLEDGES every round it reaches, permanently "
        "and including the quiet ones -- so reaching across repositories spends "
        "another repository's single delivery even when it renders nothing")

    # The control for assertion 2: B's round was deliverable the whole time, so
    # A's silence about it was the repo predicate and not ineligibility.
    assert cli_main(["surface", "--repo", str(world.repo_b)]) == 0
    out_b = capsys.readouterr().out
    assert world.b_round in out_b and a_new not in out_b, (
        "assertion 2 (control): B must still be able to read its own round")
    assert _delivered(world.db) == {world.a_old, a_new, world.b_round}
    assert world.null_round not in out_b, (
        "assertion 5: a pre-v5 round is never delivered by `surface` -- the "
        "phase's one accepted regression, and it is stated in the README")

    # ---- 3. `log --branch` is scoped, `log` alone is not ----------------
    assert cli_main(["log", "--branch", BRANCH, "--repo", str(world.repo_a),
                     "-n", "50"]) == 0
    a_stamps = _stamps(capsys.readouterr().out)
    assert cli_main(["log", "--branch", BRANCH, "--repo", str(world.repo_b),
                     "-n", "50"]) == 0
    b_stamps = _stamps(capsys.readouterr().out)

    assert a_stamps == {world.at["a_old"], final["reviewed_at"]}, (
        "assertion 3: `log --branch` in A listed rows that are not A's -- and it "
        "must list BOTH of A's, or 'B's rows are absent' would also be true of a "
        "listing that returned nothing")
    assert b_stamps == {world.at["b_running"], world.at["b_round"]}, (
        "assertion 3: `log --branch` in B listed rows that are not B's")
    for stamps, where in ((a_stamps, "A"), (b_stamps, "B")):
        assert stamps.isdisjoint({world.at["null_running"], world.at["null_round"]}), (
            f"assertion 5: a pre-v5 row is invisible to `log --branch` in {where}")

    # ...and UNSCOPED `log` still shows everything, pre-v5 rows included. A
    # branch name is the ambiguous key; "show me everything" is not.
    assert cli_main(["log", "-n", "50"]) == 0
    everything = _stamps(capsys.readouterr().out)
    assert set(world.at.values()) <= everything, (
        "assertion 5: unscoped `log` must still reach the pre-v5 rows -- they are "
        "invisible to the scoped queries, not deleted")


# --------------------------------------------------------------------------
# the decisions this phase deliberately did NOT take
# --------------------------------------------------------------------------


def _gate_identity(repo: Path) -> tuple[str, str]:
    """The `(diff_hash, base_sha)` the gate will compute in `repo`."""
    cfg = load_config(repo)
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    return gitio.diff_identity(diff.data), base.sha


def _demote(db: Path, rid: str) -> None:
    """Make one round untrustworthy, in the index AND the artifact.

    Both, because the gate re-asserts one against the other and a row that
    contradicts itself is refused for a reason that has nothing to do with this
    file. Used to clear the way in the gate tests: the two repositories hold the
    SAME content, so B's own finished round covers B's outgoing diff too --
    demoting it is what makes "the gate matched" able to mean only "it crossed
    the repository boundary".
    """
    with Store.open(db) as st:
        st._c.execute(
            "UPDATE reviews SET trustworthy=0, degraded=1, artifact_json="
            "json_set(artifact_json,'$.trustworthy',json('false'),"
            "'$.degraded',json('true')) WHERE id=?", (rid,))
        st._c.commit()


def _trustworthy_round(db: Path, rid: str, dh: str, base_sha: str,
                       repo: str | None) -> str:
    rec = {
        "id": rid, "reviewed_at": _ts(3), "branch": BRANCH, "head": "f" * 40,
        "base_ref": "main", "base_sha": base_sha, "diff_hash": dh,
        "mode": "prepush", "source": "skodun", "status": "clean",
        "parse_ok": True, "degraded": False, "diff_truncated": False,
        "usable_output": True, "findings": [], "findings_total": 0,
        "summary": "clean",
    }
    if repo is not None:
        rec["repo"] = repo
    with Store.open(db) as st:
        st.save_review(rec)
    return rid


def test_the_gate_still_matches_across_repositories_by_content(world, monkeypatch):
    """Assertion 4, and it pins a DECISION rather than an accident.

    The gate is content-addressed: identical diff bytes at the same base are the
    same change, and a review of them is a valid review wherever it happened.
    Phase 3 recorded "tighten the gate lookup to the current branch" as a
    decision deliberately not taken; this is the repository variant of the same
    question, answered the same way -- so `gate.py` stays byte-identical into a
    fourth phase (`tests/test_seams.py` pins the bytes; this pins the meaning).

    The mutation: `AND repo=?` on `Store.latest_trustworthy_for`. It leaves
    `gate.py` untouched, so only this assertion can kill it.
    """
    dh, base_sha = _gate_identity(world.repo_a)
    assert (dh, base_sha) == _gate_identity(world.repo_b), (
        "the fixture's premise: both repositories must present the same content")
    _demote(world.db, world.b_round)
    rid = _trustworthy_round(world.db, "sk_in_a", dh, base_sha, world.common_a)

    # RUN FROM INSIDE B, the way the pre-push hook runs it. Not cosmetic: it is
    # what leaves a repo-scoping mutation its best chance of surviving. A gate
    # scoped from anywhere else would fail for want of ANY match, and this
    # assertion would then be killed by an accident rather than by the decision
    # it is here to pin.
    monkeypatch.chdir(world.repo_b)
    with Store.open(world.db) as st:
        result = run_gate(st, world.repo_b, load_config(world.repo_b))
    assert result.code == 0, (
        f"assertion 4: the gate refused a review of the SAME content because it "
        f"was recorded in another repository -- {result.message}")
    assert rid in result.message, "the gate must name the review it matched"


def test_a_pre_v5_row_is_still_visible_to_the_gate(world, monkeypatch):
    """The other half of assertion 5. `repo IS NULL` hides a row from the three
    scoped queries; it must not hide it from the gate, which never consulted a
    repository and still does not. Otherwise the upgrade itself would demand a
    re-review of every change already reviewed."""
    dh, base_sha = _gate_identity(world.repo_b)
    _demote(world.db, world.b_round)
    rid = _trustworthy_round(world.db, "sk_prev5_clean", dh, base_sha, None)
    monkeypatch.chdir(world.repo_b)
    with Store.open(world.db) as st:
        assert st._c.execute("SELECT repo FROM reviews WHERE id=?",
                             (rid,)).fetchone()["repo"] is None
        result = run_gate(st, world.repo_b, load_config(world.repo_b))
    assert result.code == 0 and rid in result.message, (
        f"assertion 5: the gate stopped matching pre-v5 rows -- {result.message}")


def test_a_stale_running_row_from_another_repository_is_still_swept(
        world, monkeypatch):
    """The stale sweep is UNSCOPED, deliberately, and nothing else asserts it.

    A stale row is stale whichever repository recorded it, and a scoped sweep
    would leave every other repository's abandoned `running` rows to rot forever
    -- including the pre-v5 rows that, by the NULL rule, no scoped query can
    reach at all. "Unscoped" currently rests on the absence of a predicate;
    this makes it rest on an execution.
    """
    aged = _ts(30 * 86400)
    with Store.open(world.db) as st:
        st._c.execute("UPDATE reviews SET reviewed_at=?, "
                      "artifact_json=json_set(artifact_json,'$.reviewed_at',?) "
                      "WHERE id IN (?,?)",
                      (aged, aged, world.b_running, world.null_running))
        st._c.commit()

    monkeypatch.setattr(dispatch, "signal_superseded", lambda retired: 0)
    monkeypatch.setattr(dispatch, "spawn_worker",
                        lambda *a, **k: type("P", (), {"pid": os.getpid()})())
    assert run_dispatch(_push_line(world.repo_a), world.repo_a, world.db) == 0

    status = _statuses(world.db)
    assert status[world.b_running] == "failed", (
        "the sweep skipped a stale row belonging to another repository; a scoped "
        "sweep strands abandoned workers' rows forever")
    assert status[world.null_running] == "failed", (
        "the sweep skipped a stale PRE-V5 row -- the rows no scoped query can "
        "reach are exactly the ones an unscoped sweep exists to reclaim")
