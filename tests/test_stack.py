"""Stack-manifest identity, validation, and attribution contracts (S6.1)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from skodun import stack


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
