"""The extraction contract: what a service is allowed to be.

The CLI suite is the proof that the extraction PRESERVED behaviour -- it passes
unmodified, which is a stronger statement than anything that could be written
here. This module pins the properties that make the extraction reusable rather
than merely equivalent, and every one of them is a rule a future service would
break by accident:

  * **The caller owns the Store, and it is the first parameter.** Connection
    lifetime is a transport question -- one per CLI invocation, one per MCP tool
    call, because sqlite connections are bound to the thread that created them --
    and a service that opened its own would decide it for both, wrongly for one.
  * **Nothing in a service reaches stdout.** Not a verdict, not a diagnostic, not
    a progress line. `skodun mcp` serves JSON-RPC on stdout from a thread that may
    be mid-response, so a stray line there desynchronises the client's parser for
    the rest of the session -- and a process-global `redirect_stdout` would be the
    same bug with a nicer name.
  * **A service returns a decision, never makes one about presentation.**
    `(status, text)`, with the exit code the CLI would exit with; the transport
    decides whether that text belongs on stdout, on stderr, or in a tool result.
"""

from __future__ import annotations

import inspect
import re
import sys
import threading
from pathlib import Path

import pytest

import skodun
from skodun import services, reuse
from skodun.store import Store
from tests.test_cli import _annotation, _artifact, _finding, _loud_round, _round
from tests.test_gitio import _git, _mkrepo

#: Every store-backed service, by name. The list itself is part of the contract:
#: the first nine are what the MCP surface mirrors; `svc_deferrals` is
#: deliberately CLI-only (see its docstring -- it is a human's backlog review,
#: not a step in the loop an agent drives).
STORE_BACKED = ["svc_gate", "svc_review", "svc_log", "svc_surface",
                "svc_triage_list", "svc_triage_dismiss", "svc_adopt_refuter",
                "svc_triage_reopen", "svc_triage_defer", "svc_deferrals",
                "svc_review_status", "svc_review_cancel", "svc_review_detailed",
                "svc_review_readiness"]

#: A deferral needs a filed reference and a reason that clears the same audit
#: floor a dismissal's does -- both, or the service refuses.
TRACKING_REF = "GH-412"
DEFER_REASON = "in-bounds for this surface; the hot path is the batcher upstream"

GOOD_REASON = "the guard at line 12 already rejects a None handler before this"

#: The repository every record in this file is stamped with, and the scope every
#: `svc_surface` call passes. A LITERAL on purpose: the services take an already
#: resolved git common dir, and turning a checkout path into one is the
#: TRANSPORTS' job (`cli._cmd_surface`, `mcpserver._handle_surface`), tested
#: with real repositories where it happens.
REPO = "/repos/services"


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "autouse" / "skodun.db"))
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "absent" / "config.toml"))
    # The provider binaries too, and not as belt-and-braces: `adapters.grok`
    # prefers `~/.grok/bin/grok` over PATH, so on any machine that has grok
    # installed a PATH-only fake would silently lose and a test would run the
    # real CLI. Nothing here should reach a provider at all; pinning them at a
    # path that does not exist is what makes that a failure rather than a bill.
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "no-bin" / "grok"))
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "no-bin" / "codex"))


def _db(tmp_path: Path, *records, repo: str | None = REPO) -> Path:
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        for rec in records:
            # Stamped here rather than at each call site: `delivery`'s query is
            # scoped by `repo` and an unstamped row is invisible to every
            # `svc_surface` below -- the whole file would pass vacuously.
            st.save_review(dict(rec, repo=repo) if repo is not None else rec)
    return db


# ==========================================================================
# the shape of the seam
# ==========================================================================

def test_every_store_backed_service_takes_the_store_first():
    """The caller owns the connection, EXPLICITLY -- so it is the first
    parameter, on every one of them, with no keyword-only escape hatch."""
    for name in STORE_BACKED:
        fn = getattr(services, name)
        params = list(inspect.signature(fn).parameters.values())
        assert params[0].name == "store", (name, params[0].name)
        assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name


def test_status_line_exposes_fingerprint_version_and_lineage_reason():
    line = services.format_status_line({
        "id": "review-1", "status": "clean", "parse_ok": True,
        "findings_total": 1,
        "findings": [{
            "finding_fingerprint_v2": "sha256:" + "a" * 64,
            "finding_lineage_v2": {"match_reason": "moved"},
        }],
    })
    assert "fingerprint_version=finding_fingerprint_v2" in line
    assert "lineage_counts=moved:1" in line


def test_opt_in_reuse_returns_existing_review_without_running_pipeline(
        tmp_path, monkeypatch):
    identity = reuse.ReuseIdentity(
        repo_id="/repo/.git", worktree_root="/repo", branch="feature",
        head="h" * 40, base_sha="b" * 40, diff_hash="d" * 40,
        context_hash="c" * 64, checklist_hash="k" * 64,
        tree_fingerprint="t" * 64, security_policy_hash="p" * 64)
    candidate = {
        "id": "r1", "reviewed_at": "2026-08-09T00:00:00Z",
        "branch": "feature", "head": identity.head, "base_ref": "main",
        "base_sha": identity.base_sha, "diff_hash": identity.diff_hash,
        "context_hash": identity.context_hash,
        "checklist_hash": identity.checklist_hash,
        "tree_fingerprint": identity.tree_fingerprint,
        "security_policy_hash": identity.security_policy_hash,
        "repo_id": identity.repo_id, "worktree_root": identity.worktree_root,
        "mode": "now", "source": "skodun", "status": "clean",
        "parse_ok": True, "degraded": False, "diff_truncated": False,
        "findings": [], "findings_total": 0, "summary": "clean",
        "severity": {"high": 0, "medium": 0, "low": 0},
        "requested_reviewer": None, "client_family": None,
    }
    with Store.open(tmp_path / "reuse.db") as store:
        store.save_review(candidate)
        monkeypatch.setattr("skodun.cli._repo_root", lambda repo: repo)
        monkeypatch.setattr("skodun.config.load_config", lambda root: object())
        monkeypatch.setattr(
            reuse, "probe",
            lambda *args, **kwargs: reuse.ReuseProbe(
                candidate, identity, "exact identity match"))
        monkeypatch.setattr(
            services, "_svc_review_once",
            lambda *args, **kwargs: pytest.fail("provider pipeline was called"))
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, reuse_trusted=True)
        events = store.reuse_events()
    assert status == 0
    assert "review_id=r1" in text
    assert metadata["reuse"]["review_id"] == "r1"
    assert events[0]["outcome"] == "hit"


@pytest.mark.parametrize(("fresh", "expected_resume"), [
    (False, True),
    (True, False),
])
def test_fresh_controls_incomplete_checkpoint_resume_at_shared_service(
        tmp_path, monkeypatch, fresh, expected_resume):
    """CLI and MCP both reach this seam, so `fresh` must have one meaning."""
    monkeypatch.setattr(
        services, "_try_reuse",
        lambda *args, **kwargs: (None, None, {}))
    seen = []

    def fake_review_once(*args, **kwargs):
        seen.append(kwargs.get("resume_checkpoints"))
        return 0, "clean"

    monkeypatch.setattr(services, "_svc_review_once", fake_review_once)
    with Store.open(tmp_path / f"fresh-{fresh}.db") as store:
        status, text, _metadata = services.svc_review_detailed(
            store, tmp_path, fresh=fresh)

    assert (status, text) == (0, "clean")
    assert seen == [expected_resume]


@pytest.mark.parametrize("intent", ["reviewer", "client_family"])
def test_explicit_review_intent_bypasses_incomplete_checkpoint_resume(
        tmp_path, monkeypatch, intent):
    monkeypatch.setattr(
        services, "_try_reuse",
        lambda *args, **kwargs: (None, None, {}))
    seen = []
    monkeypatch.setattr(
        services, "_svc_review_once",
        lambda *args, **kwargs: seen.append(kwargs["resume_checkpoints"])
        or (0, "clean"))
    kwargs = {intent: "named"}
    with Store.open(tmp_path / f"intent-{intent}.db") as store:
        services.svc_review_detailed(store, tmp_path, **kwargs)
    assert seen == [False]


def test_inferred_client_family_keeps_checkpoint_resume_enabled(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        services, "_try_reuse",
        lambda *args, **kwargs: (None, None, {}))
    seen = []
    monkeypatch.setattr(
        services, "_svc_review_once",
        lambda *args, **kwargs: seen.append(kwargs["resume_checkpoints"])
        or (0, "clean"))
    with Store.open(tmp_path / "inferred-family.db") as store:
        services.svc_review_detailed(
            store, tmp_path, client_family="xai", reuse_client_family=None)
    assert seen == [True]
def test_stack_manifest_is_loaded_once_projected_and_bypasses_trusted_reuse(
        tmp_path, monkeypatch):
    from skodun import stack

    request = stack.StackRequest(
        supplied=True, manifest=None,
        problem=stack.StackProblem("malformed_json", "JSONDecodeError"))
    loads = []
    seen = []
    monkeypatch.setattr(
        stack, "load_request",
        lambda path: loads.append(Path(path)) or request)
    monkeypatch.setattr(
        reuse, "probe",
        lambda *args, **kwargs: pytest.fail("stack attribution reused an old review"))

    def fake_once(store, repo, **kwargs):
        seen.append(kwargs["stack_request"])
        kwargs["result_metadata"]["stack"] = {
            "schema_version": None,
            "status": "ignored",
            "reason_code": "malformed_json",
            "repository_id": None,
            "manifest_digest": None,
            "current_slice_id": None,
            "direct_parent": None,
            "dependency_count": 0,
            "downstream_owner_count": 0,
        }
        return 0, "SKODUN VERDICT: trustworthy=true findings=0"

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    manifest_path = tmp_path / "stack.json"
    with Store.open(tmp_path / "stack.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, stack_manifest=manifest_path,
            reuse_trusted=True)
        events = store.reuse_events()

    assert status == 0
    assert loads == [manifest_path]
    assert seen == [request]
    assert text.splitlines() == [
        "SKODUN REUSE: bypass reason=stack_attribution_requested",
        "SKODUN STACK: status=ignored reason=malformed_json",
        "SKODUN VERDICT: trustworthy=true findings=0",
    ]
    assert metadata["stack"]["reason_code"] == "malformed_json"
    assert metadata["reuse"] == {
        "hit": False, "reason": "stack_attribution_requested"}
    assert events[0]["outcome"] == "bypass"
    assert events[0]["reason"] == "stack_attribution_requested"


def test_valid_stack_projection_comes_from_the_persisted_review_result(
        tmp_path, monkeypatch):
    from skodun import stack

    request = object()
    monkeypatch.setattr(stack, "load_request", lambda path: request)

    def fake_once(store, repo, **kwargs):
        assert kwargs["stack_request"] is request
        kwargs["result_metadata"]["stack"] = {
            "status": "valid", "reason_code": "ok",
            "current_slice_id": "pr-14", "dependency_count": 2,
            "manifest_digest": "sha256:" + "a" * 64,
        }
        return 1, "SKODUN VERDICT: trustworthy=true findings=1"

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "valid-stack.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, stack_manifest=tmp_path / "stack.json")

    assert status == 1
    assert text.splitlines() == [
        "SKODUN STACK: status=valid slice=pr-14 dependencies=2 "
        "digest=sha256:" + "a" * 64,
        "SKODUN VERDICT: trustworthy=true findings=1",
    ]
    assert metadata["stack"]["status"] == "valid"


def test_recovery_reuses_one_parsed_stack_request_but_passes_it_to_each_run(
        tmp_path, monkeypatch):
    from skodun import stack
    from skodun.trust import banner

    request = stack.StackRequest(
        supplied=True, manifest=None,
        problem=stack.StackProblem("malformed_json", "JSONDecodeError"))
    loads = []
    monkeypatch.setattr(
        stack, "load_request",
        lambda path: loads.append(Path(path)) or request)
    identity_fields = {
        "repo_id": "repo", "worktree_root": "worktree", "branch": "feat",
        "head": "h", "base_sha": "s", "diff_hash": "d"}
    attempts = [
        _artifact([], review_id="stack-first", degraded=True,
                  trustworthy=False, status="degraded",
                  attempts=[{"provider": "xai"}], **identity_fields),
        _artifact([], review_id="stack-second",
                  attempts=[{"provider": "openai"}], **identity_fields),
    ]
    seen = []
    monkeypatch.setattr(
        services, "_recovery_identity",
        lambda repo: ("repo", "worktree", "feat", "h", "s", "d"))

    def fake_once(store, repo, **kwargs):
        seen.append(kwargs["stack_request"])
        kwargs["result_metadata"]["stack"] = {
            "status": "ignored", "reason_code": "malformed_json"}
        rec = attempts.pop(0)
        store.save_review(rec)
        return (4 if rec["trustworthy"] is not True else 0), banner(rec)

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    manifest_path = tmp_path / "stack.json"
    with Store.open(tmp_path / "stack-recovery.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, stack_manifest=manifest_path, recover=True,
            max_attempts=3, max_wall_seconds=30, reuse_trusted=True)

    assert status == 0
    assert loads == [manifest_path]
    assert seen == [request, request]
    assert text.splitlines()[0].startswith("SKODUN REUSE: bypass")
    assert text.splitlines()[1] == (
        "SKODUN STACK: status=ignored reason=malformed_json")
    assert text.splitlines()[2].startswith("SKODUN RECOVERY:")
    assert text.count("SKODUN STACK:") == 1
    assert metadata["stack"] == {
        "status": "ignored", "reason_code": "malformed_json"}


def test_recovery_does_not_render_prior_stack_after_unpersisted_attempt(
        tmp_path, monkeypatch):
    from skodun import stack
    from skodun.trust import banner, banner_failure

    request = stack.StackRequest(
        supplied=True, manifest=None,
        problem=stack.StackProblem("malformed_json", "JSONDecodeError"))
    identity_fields = {
        "repo_id": "repo", "worktree_root": "worktree", "branch": "feat",
        "head": "h", "base_sha": "s", "diff_hash": "d"}
    first = _artifact([], review_id="stack-first", degraded=True,
                      trustworthy=False, status="degraded",
                      attempts=[{"provider": "xai"}], **identity_fields)
    monkeypatch.setattr(stack, "load_request", lambda path: request)
    monkeypatch.setattr(
        services, "_recovery_identity",
        lambda repo: ("repo", "worktree", "feat", "h", "s", "d"))
    calls = 0

    def fake_once(store, repo, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["result_metadata"]["stack"] = {
                "status": "ignored", "reason_code": "malformed_json"}
            store.save_review(first)
            return 4, banner(first)
        return 2, banner_failure("preflight refusal")

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "stack-recovery-refusal.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, stack_manifest=tmp_path / "stack.json",
            recover=True, max_attempts=2, max_wall_seconds=30)

    assert status == 4
    assert "SKODUN STACK:" not in text
    assert "SKODUN RECOVERY:" in text
    assert "stack" not in metadata


def test_opt_in_reuse_honors_cancellation_before_and_during_a_probe(
        tmp_path, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(
        reuse, "probe",
        lambda *args, **kwargs: pytest.fail("cancelled reuse was probed"))
    with Store.open(tmp_path / "before.db") as store:
        status, text, _metadata = services.svc_review_detailed(
            store, tmp_path, reuse_trusted=True, cancel=cancel)
    assert status == 4 and "review cancelled" in text

    identity = reuse.ReuseIdentity(
        repo_id="/repo/.git", worktree_root="/repo", branch="feature",
        head="h" * 40, base_sha="b" * 40, diff_hash="d" * 40,
        context_hash="c" * 64, checklist_hash="k" * 64,
        tree_fingerprint="t" * 64, security_policy_hash="p" * 64)
    candidate = {"id": "r1"}
    cancel.clear()

    def probe_and_cancel(*args, **kwargs):
        cancel.set()
        return reuse.ReuseProbe(candidate, identity, "exact identity match")

    monkeypatch.setattr(reuse, "probe", probe_and_cancel)
    monkeypatch.setattr("skodun.cli._repo_root", lambda repo: repo)
    monkeypatch.setattr("skodun.config.load_config", lambda root: object())
    with Store.open(tmp_path / "during.db") as store:
        status, text, _metadata = services.svc_review_detailed(
            store, tmp_path, reuse_trusted=True, cancel=cancel)
    assert status == 4 and "review cancelled" in text


def test_no_service_opens_a_store_of_its_own():
    """A service that opened one would decide the connection's lifetime for both
    transports -- and get it wrong for the MCP server, whose review tool answers
    from a thread the read loop's connection cannot be used on."""
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    assert "Store.open(" not in src
    assert "_store_path" not in src


def test_no_module_redirects_stdout_anywhere():
    """`redirect_stdout` is forbidden across the whole package, not just here.

    It is process-global: an MCP review running on a worker thread would redirect
    the READ LOOP's stdout too, so a response written while it was in scope would
    vanish -- and the client would wait forever for a call that had already been
    answered into a StringIO. The banner is returned instead, which is why the
    temptation exists at all.
    """
    # A CALL or an IMPORT, not the word: several modules explain in prose why this
    # is forbidden, and a test that banned the explanation would delete the
    # explanation.
    banned = (r"redirect_stdout\s*\(", r"import\s+redirect_stdout",
              r"^\s*sys\.stdout\s*=")
    for path in sorted((Path(skodun.__file__).parent).rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for pattern in banned:
            hit = re.search(pattern, src, re.MULTILINE)
            assert hit is None, f"{path.name}: {hit.group(0)!r}"


def test_review_readiness_is_read_only_and_returns_structured_metadata(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    for args in (("init", "-b", "main"),
                 ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Readiness Test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"],
                   check=True, capture_output=True, text=True)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "finder"\nprovider = "xai"\nmodel = "grok"\n',
        encoding="utf-8")
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    with Store.open(tmp_path / "readiness.db") as store:
        status, text, metadata = services.svc_review_readiness(store, repo)
    assert status == 0
    assert "potentially_available" in text
    assert metadata["readiness"]["reason_code"] == "health_unknown"


def test_the_services_module_is_importable_without_sqlite_or_git():
    """Every heavy import is INSIDE a function. `skodun mcp` imports this module
    to build its registry and must not pay for the review pipeline's module graph
    before it has served a line."""
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    toplevel = [line for line in src.splitlines()
                if re.match(r"^(import|from)\s", line)]
    assert toplevel == ["from pathlib import Path"], toplevel


def test_bounded_recovery_records_each_attempt_and_avoids_terminal_provider(
        tmp_path, monkeypatch):
    """Recovery retries fresh records and keeps the request-level audit link."""
    from skodun.trust import banner

    identity_fields = dict(repo_id="repo", worktree_root="worktree",
                           branch="feat", head="h" * 20,
                           base_sha="s" * 40, diff_hash="d" * 40)
    first = _artifact([], review_id="first", degraded=True,
                      trustworthy=False, status="degraded",
                      attempts=[{"provider": "xai"}], **identity_fields)
    second = _artifact([], review_id="second", attempts=[{"provider": "openai"}],
                       **identity_fields)
    attempts = [first, second]
    calls = []
    identity = ("repo", "worktree", "feat", "h" * 20, "s" * 40, "d" * 40)
    monkeypatch.setattr(services, "_recovery_identity", lambda repo: identity)

    def fake_once(store, repo, **kwargs):
        calls.append(kwargs)
        rec = attempts.pop(0)
        store.save_review(rec)
        return (4 if rec["trustworthy"] is not True else 0, banner(rec))

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "recovery.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, max_attempts=3,
            max_wall_seconds=30)
        rows = [store.get_review("first"), store.get_review("second")]

    assert status == 0
    assert metadata["recovery"]["attempts"] == 2
    assert metadata["recovery"]["recovered"] is True
    assert metadata["recovery"]["review_ids"] == ["first", "second"]
    assert "trustworthy review reached" in text
    assert calls[0]["avoid_providers"] == set()
    assert calls[1]["avoid_providers"] == {"xai"}
    assert all(call["resume_checkpoints"] is False for call in calls)
    assert rows[0]["orchestration_id"] == rows[1]["orchestration_id"]
    assert rows[0]["attempt_ordinal"] == 0
    assert rows[1]["attempt_ordinal"] == 1


def test_bounded_recovery_stops_when_identity_moves(tmp_path, monkeypatch):
    from skodun.trust import banner

    rec = _artifact([], review_id="only", degraded=True,
                    trustworthy=False, status="degraded",
                    repo_id="repo", worktree_root="worktree",
                    branch="feat", head="h" * 20, base_sha="s" * 40,
                    diff_hash="d" * 40, attempts=[{"provider": "xai"}])
    identities = iter([
        ("repo", "worktree", "feat", "h" * 20, "s" * 40, "d" * 40),
        ("repo", "worktree", "feat", "h" * 20, "s" * 40, "changed" * 8),
    ])
    calls = []
    monkeypatch.setattr(services, "_recovery_identity",
                        lambda repo: next(identities))

    def fake_once(store, repo, **kwargs):
        calls.append(kwargs)
        store.save_review(rec)
        return 4, banner(rec)

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "moved.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, max_attempts=3)
        saved = store.get_review("only")

    assert status == 4
    assert len(calls) == 1
    assert metadata["recovery"]["attempts"] == 1
    assert "moved" in metadata["recovery"]["terminal_reason"]
    assert "moved" in saved["terminal_reason"]


def test_bounded_recovery_rejects_bool_limits_and_preserves_explicit_pin(
        tmp_path, monkeypatch):
    from skodun.trust import banner

    status, text, metadata = services.svc_review_detailed(
        object(), tmp_path, recover=True, max_attempts=True)
    assert status == 2 and "max_attempts" in text
    assert metadata["recovery"]["terminal_reason"]

    first = _artifact([], review_id="p1", degraded=True,
                      trustworthy=False, status="degraded",
                      repo_id="repo", worktree_root="worktree",
                      branch="feat", head="h", base_sha="s", diff_hash="d",
                      attempts=[{"provider": "xai"}])
    second = _artifact([], review_id="p2", repo_id="repo",
                       worktree_root="worktree", branch="feat", head="h",
                       base_sha="s", diff_hash="d",
                       attempts=[{"provider": "openai"}])
    attempts = [first, second]
    calls = []
    monkeypatch.setattr(services, "_recovery_identity",
                        lambda repo: ("repo", "worktree", "feat", "h", "s", "d"))

    def fake_once(store, repo, **kwargs):
        calls.append(kwargs)
        rec = attempts.pop(0)
        store.save_review(rec)
        return (4 if rec["trustworthy"] is not True else 0, banner(rec))

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "pinned.db") as store:
        status, _text, _metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, reviewer="deliberate")
    assert status == 0
    assert all(call["avoid_providers"] == set() for call in calls)


def test_recovery_limits_are_validated_before_reuse_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(
        services, "_try_reuse",
        lambda *args, **kwargs: pytest.fail("reuse probe ran before validation"))
    with Store.open(tmp_path / "recovery.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, max_attempts=0,
            reuse_trusted=True)
    assert status == 2
    assert "max_attempts" in text
    assert metadata["recovery"]["terminal_reason"]


def test_bounded_recovery_rejects_float_overflow(tmp_path):
    status, text, _metadata = services.svc_review_detailed(
        object(), tmp_path, recover=True, max_wall_seconds=10 ** 1000)
    assert status == 2
    assert "max_wall_seconds" in text


def test_bounded_recovery_deadline_cancels_the_shipped_attempt(
        tmp_path, monkeypatch):
    from skodun.trust import banner_failure

    monkeypatch.setattr(services, "_recovery_identity",
                        lambda repo: ("repo", "worktree", "feat", "h", "s", "d"))

    def fake_once(store, repo, **kwargs):
        assert kwargs["cancel"].wait(1) is True
        return 4, banner_failure("review cancelled")

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "deadline.db") as store:
        status, text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, max_wall_seconds=0.001)
    assert status == 4
    assert metadata["recovery"]["terminal_reason"] == (
        "recovery wall budget exhausted")
    assert "recovery wall budget exhausted" in text


def test_bounded_recovery_does_not_accept_a_result_after_identity_moves(
        tmp_path, monkeypatch):
    from skodun.trust import banner

    rec = _artifact([], review_id="moved", repo_id="repo",
                    worktree_root="worktree", branch="feat", head="h",
                    base_sha="s", diff_hash="d",
                    attempts=[{"provider": "xai"}])
    identities = iter([
        ("repo", "worktree", "feat", "h", "s", "d"),
        ("repo", "worktree", "feat", "h", "s", "changed"),
    ])
    monkeypatch.setattr(services, "_recovery_identity",
                        lambda repo: next(identities))

    def fake_once(store, repo, **kwargs):
        store.save_review(rec)
        return 0, banner(rec)

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "moved-trust.db") as store:
        status, _text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True)
    assert status == 4
    assert metadata["recovery"]["recovered"] is False
    assert "moved" in metadata["recovery"]["terminal_reason"]


def test_bounded_recovery_keeps_status_four_after_later_preflight_refusal(
        tmp_path, monkeypatch):
    from skodun.trust import banner, banner_failure

    rec = _artifact([], review_id="prior", degraded=True,
                    trustworthy=False, status="degraded", repo_id="repo",
                    worktree_root="worktree", branch="feat", head="h",
                    base_sha="s", diff_hash="d",
                    attempts=[{"provider": "xai"}])
    calls = []
    monkeypatch.setattr(services, "_recovery_identity",
                        lambda repo: ("repo", "worktree", "feat", "h", "s", "d"))

    def fake_once(store, repo, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            store.save_review(rec)
            return 4, banner(rec)
        return 2, banner_failure("no alternative reviewer")

    monkeypatch.setattr(services, "_svc_review_once", fake_once)
    with Store.open(tmp_path / "later-refusal.db") as store:
        status, _text, metadata = services.svc_review_detailed(
            store, tmp_path, recover=True, max_attempts=2)
    assert status == 4
    assert metadata["recovery"]["review_ids"] == ["prior"]
    assert "preflight" in metadata["recovery"]["terminal_reason"]


# ==========================================================================
# nothing prints
# ==========================================================================

def test_no_service_writes_to_stdout_on_any_path(tmp_path, capsys, monkeypatch):
    """Driven, not inspected: every service, on a path that has something to say.

    A `redirect_stdout` guard would make this test pass while breaking the thing
    it protects, which is why the test above forbids that separately.
    """
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    db = _db(tmp_path, _artifact([_finding(0, _annotation())]), _loud_round())
    capsys.readouterr()

    with Store.open(db) as store:
        results = [
            services.svc_gate(store, repo),
            services.svc_log(store, None, 20),
            services.svc_log(store, None, 0),            # the refusal path
            services.svc_triage_list(store, "rev1"),
            services.svc_triage_list(store, "nope"),      # the refusal path
            services.svc_triage_dismiss(store, "rev1", 0, "fp"),   # refused
            services.svc_adopt_refuter(store, "rev1", 0),
            services.svc_triage_reopen(store, "rev1", 0, GOOD_REASON),
            services.svc_triage_defer(store, "rev1", 0, TRACKING_REF,
                                      DEFER_REASON),
            services.svc_triage_defer(store, "rev1", 0, "", DEFER_REASON),
            services.svc_deferrals(store, 20),
            services.svc_deferrals(store, 0),             # the refusal path
            services.svc_surface(store, "feat", REPO)[:2],
            services.svc_review(store, tmp_path / "not-a-repo"),
        ]
    cap = capsys.readouterr()
    assert cap.out == "", f"a service wrote to stdout: {cap.out!r}"
    # ...and every one of them really did produce something to say, or the
    # assertion above would be about ten no-ops.
    for status, text in results:
        assert isinstance(status, int) and isinstance(text, str)
        assert text, results


def test_a_service_never_writes_to_stdout_even_when_stdout_is_the_only_stream_left(
        tmp_path, monkeypatch):
    """The paranoid form: a stdout that RAISES on any use. If a service touched
    it, this would be an exception rather than a returned refusal."""

    class _DeadStdout:
        def write(self, *_a, **_k):
            raise AssertionError("a service wrote to stdout")

        def flush(self, *_a, **_k):
            raise AssertionError("a service flushed stdout")

    db = _db(tmp_path, _artifact([_finding(0)]))
    monkeypatch.setattr(sys, "stdout", _DeadStdout())
    with Store.open(db) as store:
        assert services.svc_triage_list(store, "rev1")[0] == 0
        assert services.svc_log(store, None, 5)[0] == 0
        assert services.svc_triage_dismiss(store, "rev1", 0, GOOD_REASON)[0] == 0


# ==========================================================================
# svc_surface: three shapes, and the quiet ack
# ==========================================================================

def test_svc_surface_returns_a_payload_and_the_content_bearing_ids(tmp_path):
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat", REPO)
    assert status == 0
    assert "NPE 0" in text and text.endswith("\n")
    assert pending == ["sk_1"], (
        "the transport must be handed exactly the rounds whose delivery depends "
        "on a write it has not performed yet")


def test_svc_surface_returns_nothing_to_report_as_an_empty_payload(tmp_path):
    """`(0, "", [])` and NOT a message: the CLI's stdout is a payload a hook feeds
    to an agent verbatim, so "nothing to report" has to be distinguishable from
    "here is a report that says nothing". Each transport words it itself, from the
    one definition in `surface_no_rounds_note`."""
    db = _db(tmp_path)
    with Store.open(db) as store:
        assert services.svc_surface(store, "feat", REPO) == (0, "", [])
    assert "feat" in services.surface_no_rounds_note("feat")


def test_svc_surface_acknowledges_quiet_rounds_itself_and_never_returns_them(
        tmp_path):
    """A trustworthy round with nothing to say renders nothing, so no write can
    lose it: it is acknowledged HERE, immediately, under the `quiet` channel.
    Leaving it unacknowledged would re-scan it at every session start forever."""
    db = _db(tmp_path, _round())              # clean, zero findings: quiet
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat", REPO)
        rows = [(r["review_id"], r["channel"]) for r in
                store._c.execute("SELECT review_id, channel FROM deliveries")]
    assert (status, text, pending) == (0, "", [])
    assert rows == [("sk_1", "quiet")]


def test_svc_surface_reports_a_broken_ledger_as_a_diagnostic_not_a_payload(
        tmp_path, monkeypatch):
    """Status 2 with text is a DIAGNOSTIC: the CLI puts it on stderr, where a hook
    consuming stdout cannot mistake it for a report."""
    from skodun import delivery

    def boom(*_a, **_kw):
        raise RuntimeError("the ledger is unreadable")

    monkeypatch.setattr(delivery, "surface", boom)
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, pending = services.svc_surface(store, "feat", REPO)
    assert status == 2 and pending == []
    assert "could not read the delivery ledger" in text


def test_svc_surface_refuses_an_unknown_format_before_touching_the_ledger(
        tmp_path):
    """Misuse must not acknowledge anything -- `delivery.surface` validates the
    format FIRST, and the service lets that refusal through as a diagnostic."""
    db = _db(tmp_path, _loud_round())
    with Store.open(db) as store:
        status, text, _ = services.svc_surface(store, "feat", REPO, "yaml")
        rows = list(store._c.execute("SELECT review_id FROM deliveries"))
    assert status == 2 and "yaml" in text
    assert rows == [], "a rejected format acknowledged a round"


def test_resolve_surface_branch_prefers_the_argument_and_never_raises(tmp_path):
    assert services.resolve_surface_branch("feat") == ("feat", "")
    branch, why_not = services.resolve_surface_branch(None, tmp_path / "nowhere")
    assert branch == ""
    assert "pass --branch" in why_not and "Traceback" not in why_not


# ==========================================================================
# svc_log / svc_review shapes
# ==========================================================================

def test_svc_log_refuses_a_non_positive_limit_without_reading_the_store(tmp_path):
    """`-n` becomes SQLite's LIMIT, where NEGATIVE means unlimited: `-n -1` would
    dump the whole store while reading like a request for fewer rows."""
    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as store:
        for bad in (0, -1):
            status, text = services.svc_log(store, None, bad)
            assert status == 2 and "positive" in text
            assert "2026-" not in text, "a rejected limit rendered rows"


def test_svc_log_renders_an_empty_store_as_an_empty_string(tmp_path):
    db = _db(tmp_path)
    with Store.open(db) as store:
        assert services.svc_log(store, None, 20) == (0, "")


def test_svc_log_hands_the_repo_to_the_store_and_only_with_a_branch(tmp_path):
    """The threading itself, pinned: a `svc_log` that accepted `repo` and never
    passed it on would leave `log --repo` a flag that does nothing, and every
    other test in this file would stay green."""
    db = tmp_path / "log.db"
    with Store.open(db) as st:
        # The listing renders the SUMMARY, not the id, so that is what each
        # row is recognised by here.
        st.save_review(dict(_round(id="in_a", branch="main", summary="mine"),
                            repo=REPO))
        st.save_review(dict(_round(id="in_b", branch="main", summary="theirs"),
                            repo="/repos/other"))
    with Store.open(db) as store:
        scoped = services.svc_log(store, "main", 20, REPO)[1]
        assert "mine" in scoped and "theirs" not in scoped
        # ...and the same repo without a branch narrows nothing: that is
        # `list_reviews`'s contract and what `--repo`'s help text promises.
        everything = services.svc_log(store, None, 20, REPO)[1]
        assert "mine" in everything and "theirs" in everything


def test_svc_review_is_keyword_only_for_its_two_new_parameters():
    """`progress_sink` and `cancel` are keyword-only on `run_review` so that every
    shipped positional call site keeps working; the service mirrors that."""
    params = inspect.signature(services.svc_review).parameters
    for name in ("progress_sink", "cancel"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is None, name


def test_svc_review_reports_an_unloadable_pipeline_with_a_banner(monkeypatch,
                                                                tmp_path):
    """The one import failure that must still carry a verdict line: without it a
    broken install reports on stderr and leaves stdout silent where a refusal
    belongs."""
    from skodun import pipeline

    monkeypatch.delattr(pipeline, "run_review")
    db = _db(tmp_path)
    with Store.open(db) as store:
        status, text = services.svc_review(store, tmp_path)
    assert status == 2
    assert text.startswith("SKODUN VERDICT: trustworthy=false reason=")
    assert "no review ran" in text


def test_svc_review_re_raises_keyboard_interrupt_past_every_guard(monkeypatch,
                                                                 tmp_path):
    """Ctrl-C is 130 (the shell's 128 + SIGINT), which none of this service's
    codes can say -- so it must not be swallowed by any of the guards that would
    otherwise report 2 or 4."""
    from skodun import config

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_review(store, repo)


def test_svc_gate_re_raises_keyboard_interrupt_and_records_nothing(monkeypatch,
                                                                   tmp_path):
    """`svc_review` re-raises Ctrl-C at four guards; `svc_gate`'s single
    `except BaseException` used to swallow it.

    Two costs, and the second is the one that lasts. A run the user CANCELLED
    was reported as `FAIL(2) could not run the gate: KeyboardInterrupt()`,
    which is a verdict about the change rather than about the interruption --
    and 130 is what the shell expects, which no code in this contract can say.
    Worse, `_record_setup_failure` wrote a `gate_events` row for it, so the
    audit trail claims a gate decision was taken on a run that never took one.
    """
    from skodun import config

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_gate(store, repo)
    with Store.open(db) as store:
        rows = list(store._c.execute("SELECT * FROM gate_events"))
    assert rows == [], "a cancelled run was recorded as a gate decision"


def test_an_ordinary_gate_setup_failure_is_still_a_recorded_fail_2(monkeypatch,
                                                                   tmp_path):
    """The other half: letting Ctrl-C through must not have been done by
    letting everything through. An ordinary exception is STILL exit 2, still
    reported in words, and still written to the audit trail -- this is a
    fail-closed gate, and 2 is the conservative value in its contract."""
    from skodun import config

    def boom(*_a, **_k):
        raise RuntimeError("unparseable config")

    monkeypatch.setattr(config, "load_config", boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path)
    with Store.open(db) as store:
        status, text = services.svc_gate(store, repo)
    assert status == 2
    assert "SKODUN GATE: FAIL(2) could not run the gate" in text
    assert "unparseable config" in text
    with Store.open(db) as store:
        rows = list(store._c.execute("SELECT * FROM gate_events"))
    assert len(rows) == 1


@pytest.mark.parametrize("call", [
    pytest.param(lambda st, repo: services.svc_gate(st, repo), id="svc_gate"),
    pytest.param(lambda st, repo: services.svc_log(st, None, 10), id="svc_log"),
    pytest.param(
        lambda st, repo: services.svc_surface(st, "feat", REPO, "text", False),
        id="svc_surface"),
    pytest.param(lambda st, repo: services.resolve_surface_branch(None, repo),
                 id="resolve_surface_branch"),
    pytest.param(lambda st, repo: services.svc_adopt_refuter(st, "r1", 0),
                 id="svc_adopt_refuter"),
    pytest.param(
        lambda st, repo: services.svc_triage_reopen(st, "r1", 0, GOOD_REASON),
        id="svc_triage_reopen"),
    pytest.param(
        lambda st, repo: services.svc_triage_dismiss(st, "r1", 0, GOOD_REASON),
        id="svc_triage_dismiss"),
    pytest.param(
        lambda st, repo: services.svc_triage_defer(st, "r1", 0, TRACKING_REF,
                                                   DEFER_REASON),
        id="svc_triage_defer"),
    pytest.param(lambda st, repo: services.svc_deferrals(st, 20),
                 id="svc_deferrals"),
])
def test_no_service_guard_turns_a_ctrl_c_into_a_synthetic_failure(
        monkeypatch, tmp_path, call):
    """Every `except BaseException` in this module, swept in one place.

    Each one exists to stop an ordinary failure (an unreadable store, a git that
    will not run, a ledger that stopped answering) from escaping as a traceback,
    and each was catching Ctrl-C as well -- reporting a synthetic 2 for a run the
    user chose to abandon, and taking the CLI's 130 mapping away from it. A
    future service copying the pattern gets caught here rather than in a user's
    terminal.
    """
    from skodun import config, delivery, gitio, triage

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    for module, name in ((config, "load_config"), (gitio, "current_branch"),
                         (delivery, "surface"), (triage, "adopt_refuter"),
                         (triage, "reopen"), (triage, "dismiss"),
                         (triage, "defer")):
        monkeypatch.setattr(module, name, boom)
    repo = _mkrepo(tmp_path)
    db = _db(tmp_path, _round(id="r1", findings=[_finding(0)], findings_total=1,
                              artifact=_artifact(_finding(0))))
    with Store.open(db) as store:
        monkeypatch.setattr(store, "list_reviews", boom)
        monkeypatch.setattr(store, "open_deferrals", boom)
        with pytest.raises(KeyboardInterrupt):
            call(store, repo)


@pytest.mark.parametrize("service, verb", [
    pytest.param(lambda st: services.svc_triage_dismiss(st, "r1", 0,
                                                        GOOD_REASON),
                 "dismissal", id="svc_triage_dismiss"),
    pytest.param(lambda st: services.svc_triage_reopen(st, "r1", 0,
                                                       GOOD_REASON),
                 "reopen", id="svc_triage_reopen"),
    pytest.param(lambda st: services.svc_triage_defer(st, "r1", 0, TRACKING_REF,
                                                      DEFER_REASON),
                 "deferral", id="svc_triage_defer"),
])
def test_a_store_that_stopped_accepting_writes_is_a_refusal_not_a_traceback(
        monkeypatch, tmp_path, service, verb):
    """The `(status, text)` contract is what BOTH surfaces are built on: the CLI
    turns it into an exit code and a line, and the MCP transport into a tool
    result. A `sqlite3.OperationalError` out of the write escaped both.

    `svc_triage_dismiss` was the one triage service without this guard -- it
    caught only the validation errors -- while its two siblings had it.
    """
    import sqlite3

    from skodun import triage

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(triage, "dismiss", boom)
    monkeypatch.setattr(triage, "reopen", boom)
    monkeypatch.setattr(triage, "defer", boom)
    db = _db(tmp_path, _round(id="r1", findings=[_finding(0)], findings_total=1,
                              artifact=_artifact(_finding(0))))
    with Store.open(db) as store:
        status, text = service(store)
    assert status == 2
    assert f"could not record the {verb}" in text
    assert "database is locked" in text


# ==========================================================================
# the refusal strings live in one place
# ==========================================================================

def test_the_usage_strings_are_module_constants_not_literals():
    """Two callers each -- the CLI's pre-store argparse check and the service's own
    absence check -- so a literal at either site would be a second definition that
    drifts the first time one is reworded."""
    cli_src = (Path(skodun.__file__).parent / "cli.py").read_text(encoding="utf-8")
    for name in ("TRIAGE_REOPEN_USAGE", "TRIAGE_ADOPT_USAGE",
                 "TRIAGE_DEFER_USAGE"):
        constant = getattr(services, name)
        assert constant.startswith("skodun triage: usage:")
        assert name in cli_src, f"the CLI does not use services.{name}"
        assert constant not in cli_src, (
            f"{name}'s text is spelled a second time in cli.py")
    # The plain-dismissal usage string has only ONE caller (the service), because
    # argparse cannot produce that shape on the CLI -- so it is a constant for
    # symmetry, and the test says so rather than pretending otherwise.
    assert services.TRIAGE_DISMISS_USAGE.startswith("skodun triage: usage:")


# ==========================================================================
# svc_deferrals: the listing that keeps a deferral from rotting
# ==========================================================================

def _deferred(tmp_path: Path, *, ref=TRACKING_REF) -> Path:
    from skodun import triage

    db = _db(tmp_path, _artifact([_finding(0), _finding(1)]))
    with Store.open(db) as st:
        triage.defer(st, st.get_review("rev1"), 0, ref, DEFER_REASON,
                     now="2026-07-27T11:00:00Z")
    return db


def test_svc_deferrals_renders_one_line_per_open_deferral(tmp_path):
    db = _deferred(tmp_path)
    with Store.open(db) as st:
        status, text = services.svc_deferrals(st, 20)
    assert status == 0
    lines = text.splitlines()
    assert len(lines) == 1, lines
    # Everything a human needs to chase it: the filing, where the finding is,
    # what it was, when it was deferred, and which review it came from.
    for needle in (TRACKING_REF, "feat", "a0.py", "NPE 0", "2026-07-27T11:00:00Z",
                   "rev1"):
        assert needle in lines[0], (needle, lines[0])


def test_svc_deferrals_is_empty_text_when_nothing_is_deferred(tmp_path):
    """An empty listing is an ANSWER: `(0, "")`, and each transport says so its
    own way -- the CLI with a note on stderr, so `| wc -l` stays honest."""
    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as st:
        assert services.svc_deferrals(st, 20) == (0, "")


@pytest.mark.parametrize("limit", [0, -1, "lots", None])
def test_svc_deferrals_refuses_a_non_positive_limit(tmp_path, limit):
    """`-n` becomes SQLite's LIMIT, where a NEGATIVE value means "no limit" --
    exactly `svc_log`'s reason, and the same refusal."""
    db = _deferred(tmp_path)
    with Store.open(db) as st:
        status, text = services.svc_deferrals(st, limit)
    assert status == 2 and "positive" in text


def test_svc_deferrals_bounds_every_untrusted_field_it_prints(tmp_path):
    """The title is finder-authored model text and reaches a terminal on a
    one-line-per-item listing, exactly as in `triage --list`. Same rule, same
    helper: no forged row, no rewritten line, no 10,000-character field."""
    hostile = dict(_finding(0), title="a\x1b[2K\nGH-9 forged " + "z" * 4000)
    db = _db(tmp_path, _artifact([hostile]))
    with Store.open(db) as st:
        from skodun import triage
        triage.defer(st, st.get_review("rev1"), 0, TRACKING_REF, DEFER_REASON,
                     now="2026-07-27T11:00:00Z")
        status, text = services.svc_deferrals(st, 20)
    assert status == 0
    assert len(text.splitlines()) == 1, text
    assert "\x1b" not in text
    assert len(text) < 600, len(text)


# ==========================================================================
# svc_triage_defer: the exit contract `--reopen` already uses
# ==========================================================================

def test_svc_triage_defer_records_and_names_the_filing(tmp_path):
    """The success line names the reference, because "deferred" without it is
    the very ambiguity this verb exists to remove."""
    from skodun.textnorm import finding_key

    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as st:
        status, text = services.svc_triage_defer(st, "rev1", 0, "  GH-412 ",
                                                 DEFER_REASON)
        assert status == 0, text
        assert "GH-412" in text and "rev1" in text
        assert set(st.triage_for("feat", "s" * 40)) == {
            finding_key("a0.py", "NPE 0")}


@pytest.mark.parametrize("ref, reason, status", [
    (TRACKING_REF, "fp", 1),                        # placeholder reason
    ("", DEFER_REASON, 1),                          # no filing
    ("I will file it later", DEFER_REASON, 1),      # prose, not a filing
    (None, DEFER_REASON, 2),                        # absent: misuse
    (TRACKING_REF, None, 2),                        # absent: misuse
])
def test_svc_triage_defer_splits_refused_from_never_had_an_opinion(
        tmp_path, ref, reason, status):
    """1 is "the finding is right there and the deferral was declined"; 2 is
    "this never got as far as having an opinion". Collapsing them would make
    "your reference is prose" indistinguishable from "you typed no reference"."""
    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as st:
        got, text = services.svc_triage_defer(st, "rev1", 0, ref, reason)
        assert got == status, text
        assert st.triage_for("feat", "s" * 40) == {}, "a refusal recorded something"


@pytest.mark.parametrize("review_id, index", [("nope", 0), ("rev1", 99)])
def test_svc_triage_defer_reports_a_thing_that_does_not_exist_as_a_2(
        tmp_path, review_id, index):
    db = _db(tmp_path, _artifact([_finding(0)]))
    with Store.open(db) as st:
        status, text = services.svc_triage_defer(st, review_id, index,
                                                 TRACKING_REF, DEFER_REASON)
    assert status == 2 and text


def test_the_cancellation_reason_is_one_constant():
    assert services.REVIEW_CANCELLED_REASON == "review cancelled"
    src = (Path(skodun.__file__).parent / "services.py").read_text(encoding="utf-8")
    assert src.count('"review cancelled"') == 1
