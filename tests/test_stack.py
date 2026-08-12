"""Stack-manifest identity, validation, and attribution contracts (S6.1)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from skodun import gitio, stack


BASE = "1" * 40
HEAD = "2" * 40
REPOSITORY = "github.com/acme/project"


def _scope(path="src/current.py", *, kind="file", exclusive=True,
           line_start=None, line_end=None, symbol=None):
    return {
        "kind": kind,
        "path": path,
        "exclusive": exclusive,
        "line_start": line_start,
        "line_end": line_end,
        "symbol": symbol,
    }


def _manifest():
    return {
        "schema_version": 1,
        "repository_id": REPOSITORY,
        "certification_base": BASE,
        "current_head": HEAD,
        "direct_parent": None,
        "dependencies": [],
        "current_slice": {
            "slice_id": "pr-14",
            "commit": HEAD,
            "tracking_ref": f"{REPOSITORY}#14",
            "ownership": [_scope()],
        },
        "downstream_owners": [],
        "producer": {"id": "stack-export", "version": "1.0"},
        "manifest_digest": "",
    }


def _digest(document):
    semantic = dict(document)
    semantic.pop("manifest_digest", None)
    encoded = json.dumps(
        semantic, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write(path: Path, document=None) -> Path:
    document = _manifest() if document is None else document
    document["manifest_digest"] = _digest(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_request_parses_a_canonical_v1_manifest(tmp_path):
    request = stack.load_request(_write(tmp_path / "stack.json"))

    assert request.supplied is True
    assert request.problem is None
    assert request.manifest is not None
    assert request.manifest.schema_version == 1
    assert request.manifest.repository_id == REPOSITORY
    assert request.manifest.certification_base == BASE
    assert request.manifest.current_head == HEAD
    assert request.manifest.current_slice.slice_id == "pr-14"
    assert request.manifest.current_slice.ownership[0].path == "src/current.py"
    assert request.manifest.manifest_digest.startswith("sha256:")


def test_manifest_digest_is_over_normalized_semantics_not_json_layout(tmp_path):
    document = _manifest()
    document["manifest_digest"] = _digest(document)
    compact = tmp_path / "compact.json"
    pretty = tmp_path / "pretty.json"
    compact.write_text(json.dumps(document, separators=(",", ":")),
                       encoding="utf-8")
    pretty.write_text(json.dumps(document, indent=4), encoding="utf-8")

    first = stack.load_request(compact)
    second = stack.load_request(pretty)

    assert first.problem is None and second.problem is None
    assert first.manifest == second.manifest


def test_duplicate_json_keys_are_refused_with_a_stable_reason(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8")

    request = stack.load_request(path)

    assert request.manifest is None
    assert request.problem.reason_code == "duplicate_key"


@pytest.mark.parametrize(("mutation", "reason"), [
    (lambda d: d.update(extra="nope"), "unknown_field"),
    (lambda d: d.update(schema_version=2), "unsupported_schema"),
    (lambda d: d.update(schema_version=True), "invalid_field"),
    (lambda d: d.update(repository_id="github.com/acme/../project"),
     "invalid_field"),
    (lambda d: d["current_slice"].update(tracking_ref="#14"),
     "invalid_field"),
    (lambda d: d["current_slice"]["ownership"][0].update(
        path="../secret.py"), "invalid_field"),
    (lambda d: d["current_slice"]["ownership"][0].update(
        line_start=9, line_end=None), "invalid_field"),
    (lambda d: d["current_slice"]["ownership"][0].update(
        kind="prefix", line_start=1, line_end=2), "invalid_field"),
])
def test_manifest_shape_errors_have_stable_reasons(tmp_path, mutation, reason):
    document = _manifest()
    mutation(document)

    request = stack.load_request(_write(tmp_path / "bad.json", document))

    assert request.manifest is None
    assert request.problem.reason_code == reason
    assert len(request.problem.detail) <= 240


def test_manifest_digest_mismatch_is_refused(tmp_path):
    document = _manifest()
    document["manifest_digest"] = "sha256:" + "0" * 64
    path = tmp_path / "bad-digest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    request = stack.load_request(path)

    assert request.problem.reason_code == "digest_mismatch"


def test_nonfinite_json_number_is_refused(tmp_path):
    path = tmp_path / "nan.json"
    path.write_text('{"schema_version":NaN}', encoding="utf-8")

    request = stack.load_request(path)

    assert request.problem.reason_code == "malformed_json"


def test_invalid_utf8_is_refused(tmp_path):
    path = tmp_path / "bytes.json"
    path.write_bytes(b"{\xff}")

    request = stack.load_request(path)

    assert request.problem.reason_code == "invalid_utf8"


def test_manifest_file_size_is_bounded_before_json_parsing(tmp_path):
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (stack.MAX_MANIFEST_BYTES + 1))

    request = stack.load_request(path)

    assert request.problem.reason_code == "too_large"


def test_manifest_collection_sizes_are_bounded(tmp_path):
    document = _manifest()
    document["dependencies"] = [
        {
            "slice_id": f"slice-{index}",
            "commit": f"{index + 3:040x}",
            "tracking_ref": f"{REPOSITORY}#{index + 20}",
            "ownership": [],
        }
        for index in range(stack.MAX_DEPENDENCIES + 1)
    ]
    document["direct_parent"] = document["dependencies"][-1]["slice_id"]

    request = stack.load_request(_write(tmp_path / "many.json", document))

    assert request.problem.reason_code == "limit_exceeded"


def test_manifest_unknown_nested_fields_are_refused(tmp_path):
    document = _manifest()
    document["producer"]["secret"] = "must-not-survive"

    request = stack.load_request(_write(tmp_path / "nested.json", document))

    assert request.problem.reason_code == "unknown_field"
    assert "must-not-survive" not in request.problem.detail


def test_manifest_symlink_is_refused(tmp_path):
    target = _write(tmp_path / "target.json")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    request = stack.load_request(link)

    assert request.problem.reason_code == "unsafe_file"


def test_manifest_hardlink_is_refused(tmp_path):
    target = _write(tmp_path / "target.json")
    link = tmp_path / "hard.json"
    os.link(target, link)

    request = stack.load_request(link)

    assert request.problem.reason_code == "unsafe_file"


def test_manifest_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "manifest.fifo"
    os.mkfifo(fifo)

    request = stack.load_request(fifo)

    assert request.problem.reason_code == "unsafe_file"


def test_manifest_directory_is_refused(tmp_path):
    request = stack.load_request(tmp_path)

    assert request.problem.reason_code == "unsafe_file"


def test_manifest_replaced_during_read_is_refused(tmp_path, monkeypatch):
    path = _write(tmp_path / "moving.json")
    replacement = _write(tmp_path / "replacement.json")
    original_read = stack.os.read
    swapped = False

    def read_then_replace(fd, size):
        nonlocal swapped
        data = original_read(fd, size)
        if not swapped:
            swapped = True
            os.replace(replacement, path)
        return data

    monkeypatch.setattr(stack.os, "read", read_then_replace)

    request = stack.load_request(path)

    assert request.problem.reason_code == "unsafe_file"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, relative: str, body: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _stack_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "stack@example.com")
    _git(repo, "config", "user.name", "Stack Test")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/project.git")
    base = _commit(repo, "README.md", "base\n", "base")
    dep1 = _commit(repo, "src/core.py", "core = 1\n", "dependency one")
    dep2 = _commit(repo, "src/shared.py", "shared = 1\n", "dependency two")
    _commit(repo, "src/shared.py", "shared = 2\n", "current shared")
    _commit(repo, "src/current.py", "current = 1\n", "current file")
    head = _commit(repo, "tests/test_current.py", "def test_current(): pass\n",
                   "current test")
    return {
        "repo": repo,
        "base": base,
        "dep1": dep1,
        "dep2": dep2,
        "head": head,
    }


def _stack_document(state):
    return {
        "schema_version": 1,
        "repository_id": REPOSITORY,
        "certification_base": state["base"],
        "current_head": state["head"],
        "direct_parent": "pr-12",
        "dependencies": [
            {
                "slice_id": "pr-10",
                "commit": state["dep1"],
                "tracking_ref": f"{REPOSITORY}#10",
                "ownership": [_scope("src/core.py")],
            },
            {
                "slice_id": "pr-12",
                "commit": state["dep2"],
                "tracking_ref": f"{REPOSITORY}#12",
                "ownership": [_scope("src/shared.py", exclusive=False)],
            },
        ],
        "current_slice": {
            "slice_id": "pr-14",
            "commit": state["head"],
            "tracking_ref": f"{REPOSITORY}#14",
            "ownership": [
                _scope("src/current.py"),
                _scope("tests/test_current.py"),
                _scope("src/shared.py", exclusive=False),
            ],
        },
        "downstream_owners": [
            {
                "tracking_ref": f"{REPOSITORY}#16",
                "ownership": [_scope("src/downstream.py")],
                "known_finding_refs": ["legacy-key-16"],
            }
        ],
        "producer": {"id": "stack-export", "version": "1.0"},
        "manifest_digest": "",
    }


def _validation(tmp_path, state=None, document=None, *, untracked_max=100):
    state = _stack_repo(tmp_path) if state is None else state
    document = _stack_document(state) if document is None else document
    request = stack.load_request(_write(tmp_path / "stack-git.json", document))
    assert request.problem is None
    full_diff = gitio.capture_diff(
        state["repo"], state["base"], untracked_max)
    result = stack.validate(
        request,
        repo=state["repo"],
        certification_base=state["base"],
        current_head=state["head"],
        full_diff=full_diff,
        full_tree_fingerprint=gitio.tree_fingerprint(
            state["repo"], paths=full_diff.files),
        untracked_max=untracked_max,
    )
    return state, request, full_diff, result


def test_tubescribes_shaped_stack_validates_and_classifies_exact_owners(tmp_path):
    _state, _request, _full_diff, result = _validation(tmp_path)

    assert result.status == "valid"
    assert result.reason_code == "ok"
    findings = stack.classify_findings([
        {"file": "src/core.py", "line": 1, "title": "parent"},
        {"file": "src/current.py", "line": 1, "title": "current"},
        {"file": "tests/test_current.py", "line": 1, "title": "fixture"},
        {"file": "src/shared.py", "line": 1, "title": "cross-slice"},
        {"file": "src/downstream.py", "line": 1, "title": "downstream"},
        {"file": "src/unknown.py", "line": 1, "title": "unknown"},
    ], result)

    attributions = [finding["scope_attribution"] for finding in findings]
    assert [item["scope"] for item in attributions] == [
        "inherited_dependency", "current_slice", "fixture_or_test",
        "integration", "downstream_owned", "unknown",
    ]
    assert attributions[0]["owner_slice_id"] == "pr-10"
    assert attributions[0]["owner_ref"] == f"{REPOSITORY}#10"
    assert attributions[1]["owner_slice_id"] == "pr-14"
    assert attributions[4]["owner_ref"] == f"{REPOSITORY}#16"
    assert findings[0]["title"] == "parent"


@pytest.mark.parametrize(("field", "replacement", "reason"), [
    ("repository_id", "github.com/other/project", "repository_mismatch"),
    ("certification_base", "a" * 40, "stale_base"),
    ("current_head", "b" * 40, "stale_head"),
])
def test_validation_refuses_manifest_identity_mismatches(
        tmp_path, field, replacement, reason):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document[field] = replacement
    if field == "current_head":
        document["current_slice"]["commit"] = replacement

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    assert result.status == "ignored"
    assert result.reason_code == reason


def test_validation_distinguishes_missing_and_noncommit_objects(tmp_path):
    state = _stack_repo(tmp_path)
    missing = _stack_document(state)
    missing["dependencies"][0]["commit"] = "f" * 40
    _state, _request, _full_diff, missing_result = _validation(
        tmp_path, state=state, document=missing)

    blob = subprocess.run(
        ["git", "-C", str(state["repo"]), "hash-object", "-w", "--stdin"],
        input=b"not a commit\n", capture_output=True, check=True,
    ).stdout.decode().strip()
    noncommit = _stack_document(state)
    noncommit["dependencies"][0]["commit"] = blob
    _state, _request, _full_diff, noncommit_result = _validation(
        tmp_path, state=state, document=noncommit)

    assert missing_result.reason_code == "missing_commit"
    assert noncommit_result.reason_code == "not_commit"


def test_validation_refuses_reordered_dependencies(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["dependencies"].reverse()
    document["direct_parent"] = document["dependencies"][-1]["slice_id"]

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    assert result.reason_code == "dependency_reordered"


@pytest.mark.parametrize(("mutation", "reason"), [
    (lambda d: d["dependencies"][1].update(slice_id="pr-10"),
     "duplicate_slice"),
    (lambda d: d["dependencies"][1].update(
        commit=d["dependencies"][0]["commit"]), "duplicate_commit"),
    (lambda d: d["dependencies"][0].update(slice_id="pr-14"),
     "stack_cycle"),
    (lambda d: d.update(direct_parent="pr-10"), "direct_parent_mismatch"),
])
def test_manifest_graph_shape_is_refused_before_git(
        tmp_path, mutation, reason):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    mutation(document)

    request = stack.load_request(_write(tmp_path / "bad-graph.json", document))

    assert request.manifest is None
    assert request.problem.reason_code == reason


def test_validation_refuses_claimed_scope_not_changed_by_its_slice(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["dependencies"][0]["ownership"] = [_scope("src/not-changed.py")]

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    assert result.reason_code == "ownership_unreachable"


def test_validation_refuses_overlapping_exclusive_slice_ownership(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["dependencies"][1]["ownership"] = [_scope("src/shared.py")]
    document["current_slice"]["ownership"][-1] = _scope("src/shared.py")

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    assert result.reason_code == "exclusive_scope_overlap"


def test_current_dirty_content_is_validated_from_the_frozen_worktree(tmp_path):
    state = _stack_repo(tmp_path)
    (state["repo"] / "src/current.py").write_text(
        "current = 2\n", encoding="utf-8")

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=_stack_document(state))

    assert result.status == "valid"
    finding = stack.classify_findings(
        [{"file": "src/current.py", "line": 1}], result)[0]
    assert finding["scope_attribution"]["scope"] == "current_slice"


def test_tree_movement_during_stack_capture_disables_attribution(
        tmp_path, monkeypatch):
    state = _stack_repo(tmp_path)
    original = gitio.capture_diff
    moved = False

    def capture_and_move(repo, base_sha, untracked_max):
        nonlocal moved
        captured = original(repo, base_sha, untracked_max)
        if base_sha == state["dep2"] and not moved:
            moved = True
            (state["repo"] / "src/current.py").write_text(
                "moved after slice capture\n", encoding="utf-8")
        return captured

    monkeypatch.setattr(gitio, "capture_diff", capture_and_move)

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=_stack_document(state))

    assert result.status == "ignored"
    assert result.reason_code == "git_error"


def test_truncated_untracked_capture_disables_attribution(tmp_path):
    state = _stack_repo(tmp_path)
    (state["repo"] / "untracked.py").write_text("hidden = True\n", encoding="utf-8")

    _state, _request, full_diff, result = _validation(
        tmp_path, state=state, document=_stack_document(state),
        untracked_max=0)

    assert full_diff.truncated_untracked is True
    assert result.status == "ignored"
    assert result.reason_code == "git_error"


def test_line_and_symbol_anchors_require_exact_finding_evidence(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["current_slice"]["ownership"] = [
        _scope("src/current.py", line_start=1, line_end=1, symbol="target")]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    missing = stack.classify_findings(
        [{"file": "src/current.py", "line": 1}], result)[0]
    exact = stack.classify_findings(
        [{"file": "src/current.py", "line": 1, "symbol": "target"}],
        result,
    )[0]

    assert missing["scope_attribution"]["scope"] == "unknown"
    assert missing["scope_attribution"]["reason_code"] == "no_owner_evidence"
    assert exact["scope_attribution"]["scope"] == "current_slice"


def test_line_scope_outside_the_slice_git_hunk_is_unreachable(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["current_slice"]["ownership"] = [
        _scope("src/current.py", line_start=50, line_end=60)]

    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    assert result.status == "ignored"
    assert result.reason_code == "ownership_unreachable"


def test_prefix_scope_classifies_only_actual_current_slice_paths(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["dependencies"][0]["ownership"] = []
    document["dependencies"][1]["ownership"] = []
    document["current_slice"]["ownership"] = [
        _scope("src", kind="prefix", exclusive=False)]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    current = stack.classify_findings(
        [{"file": "src/current.py", "line": 1}], result)[0]
    inherited = stack.classify_findings(
        [{"file": "src/core.py", "line": 1}], result)[0]

    assert current["scope_attribution"]["scope"] == "current_slice"
    assert inherited["scope_attribution"]["scope"] == "unknown"


def test_multiple_downstream_claims_are_reported_as_ambiguous(tmp_path):
    state = _stack_repo(tmp_path)
    document = _stack_document(state)
    document["downstream_owners"].append({
        "tracking_ref": f"{REPOSITORY}#18",
        "ownership": [_scope("src/downstream.py")],
        "known_finding_refs": [],
    })
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    finding = stack.classify_findings(
        [{"file": "src/downstream.py", "line": 1}], result)[0]

    assert finding["scope_attribution"]["scope"] == "unknown"
    assert finding["scope_attribution"]["reason_code"] == "ambiguous_owner"


def test_renamed_current_file_is_explicitly_unknown(tmp_path):
    state = _stack_repo(tmp_path)
    _git(state["repo"], "mv", "src/core.py", "src/renamed.py")
    _git(state["repo"], "commit", "-m", "rename current file")
    state["head"] = gitio.head_sha(state["repo"])
    document = _stack_document(state)
    document["dependencies"][0]["ownership"] = []
    document["current_slice"]["ownership"] = [_scope("src/renamed.py")]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    finding = stack.classify_findings(
        [{"file": "src/renamed.py", "line": 1}], result)[0]

    assert result.status == "valid"
    assert finding["scope_attribution"]["scope"] == "unknown"
    assert finding["scope_attribution"]["reason_code"] == "uncertain_git_mapping"


def test_deletion_only_current_scope_is_explicitly_unknown(tmp_path):
    state = _stack_repo(tmp_path)
    _git(state["repo"], "rm", "README.md")
    _git(state["repo"], "commit", "-m", "delete current file")
    state["head"] = gitio.head_sha(state["repo"])
    document = _stack_document(state)
    document["current_slice"]["ownership"] = [_scope("README.md")]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    finding = stack.classify_findings(
        [{"file": "README.md", "line": 1}], result)[0]

    assert result.status == "valid"
    assert finding["scope_attribution"]["reason_code"] == "uncertain_git_mapping"


def test_mode_only_current_scope_is_explicitly_unknown(tmp_path):
    state = _stack_repo(tmp_path)
    os.chmod(state["repo"] / "src/core.py", 0o755)
    _git(state["repo"], "add", "src/core.py")
    _git(state["repo"], "commit", "-m", "change executable mode")
    state["head"] = gitio.head_sha(state["repo"])
    document = _stack_document(state)
    document["dependencies"][0]["ownership"] = []
    document["current_slice"]["ownership"] = [_scope("src/core.py")]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    finding = stack.classify_findings(
        [{"file": "src/core.py", "line": 1}], result)[0]

    assert result.status == "valid"
    assert finding["scope_attribution"]["reason_code"] == "uncertain_git_mapping"


def test_binary_current_scope_is_explicitly_unknown(tmp_path):
    state = _stack_repo(tmp_path)
    binary = state["repo"] / "src/data.bin"
    binary.write_bytes(b"\x00\x01\x02\xff")
    _git(state["repo"], "add", "src/data.bin")
    _git(state["repo"], "commit", "-m", "add binary")
    state["head"] = gitio.head_sha(state["repo"])
    document = _stack_document(state)
    document["current_slice"]["ownership"] = [_scope("src/data.bin")]
    _state, _request, _full_diff, result = _validation(
        tmp_path, state=state, document=document)

    finding = stack.classify_findings(
        [{"file": "src/data.bin", "line": 1}], result)[0]

    assert result.status == "valid"
    assert finding["scope_attribution"]["reason_code"] == "uncertain_git_mapping"
