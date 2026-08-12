"""Validated stack metadata annotates full reviews; it never narrows them.

The module owns the untrusted manifest door and its bounded, canonical model.
Git-backed reachability and finding attribution build on this parser, but none
of these values participate in Skodun's trust axes, legacy finding key, or gate
lookup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_MANIFEST_BYTES = 64 * 1024
MAX_DEPENDENCIES = 32
MAX_DOWNSTREAM_OWNERS = 64
MAX_SCOPES_PER_OWNER = 256
MAX_TOTAL_SCOPES = 2_048
MAX_KNOWN_FINDING_REFS = 128
MAX_ID_CHARS = 128
MAX_PATH_CHARS = 512
MAX_TRACKING_REF_CHARS = 1_024
MAX_PROBLEM_DETAIL_CHARS = 240

_OID = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HOST = re.compile(r"[a-z0-9.-]+(?::[1-9][0-9]{0,4})?")
_ISSUE_NUMBER = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class OwnershipScope:
    kind: str
    path: str
    exclusive: bool
    line_start: int | None
    line_end: int | None
    symbol: str | None


@dataclass(frozen=True)
class StackSlice:
    slice_id: str
    commit: str
    tracking_ref: str
    ownership: tuple[OwnershipScope, ...]


@dataclass(frozen=True)
class DownstreamOwner:
    tracking_ref: str
    ownership: tuple[OwnershipScope, ...]
    known_finding_refs: tuple[str, ...]


@dataclass(frozen=True)
class ManifestProducer:
    id: str
    version: str


@dataclass(frozen=True)
class StackManifest:
    schema_version: int
    repository_id: str
    certification_base: str
    current_head: str
    direct_parent: str | None
    dependencies: tuple[StackSlice, ...]
    current_slice: StackSlice
    downstream_owners: tuple[DownstreamOwner, ...]
    producer: ManifestProducer
    manifest_digest: str


@dataclass(frozen=True)
class StackProblem:
    reason_code: str
    detail: str = ""


@dataclass(frozen=True)
class StackRequest:
    supplied: bool
    manifest: StackManifest | None
    problem: StackProblem | None


class _ManifestError(ValueError):
    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


def _bounded_detail(detail: object) -> str:
    text = str(detail)
    flattened = "".join(
        " " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(flattened.split())[:MAX_PROBLEM_DETAIL_CHARS]


def _request_problem(reason_code: str, detail: object = "") -> StackRequest:
    return StackRequest(
        supplied=True,
        manifest=None,
        problem=StackProblem(reason_code, _bounded_detail(detail)),
    )


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise _ManifestError("unsafe_file", type(exc).__name__) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _ManifestError("unsafe_file", "not a single-link regular file")
        if before.st_size > MAX_MANIFEST_BYTES:
            raise _ManifestError("too_large", "manifest exceeds byte limit")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(16_384, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_MANIFEST_BYTES:
            raise _ManifestError("too_large", "manifest exceeds byte limit")
        after_fd = os.fstat(fd)
        try:
            after_path = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise _ManifestError("unsafe_file", "manifest moved during read") from exc
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns,
        )
        if identity(before) != identity(after_fd) or \
                identity(after_fd) != identity(after_path):
            raise _ManifestError("unsafe_file", "manifest moved during read")
        return data
    finally:
        os.close(fd)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ManifestError("duplicate_key", f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise _ManifestError("malformed_json", f"non-finite number {value}")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _ManifestError("invalid_field", f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _ManifestError(
            "unknown_field", f"{label} has unknown key {unknown[0]!r}")
    if missing:
        raise _ManifestError(
            "invalid_field", f"{label} is missing key {missing[0]!r}")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _text(value: object, label: str, *, maximum: int = MAX_ID_CHARS) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or _has_control(value)):
        raise _ManifestError("invalid_field", f"{label} is not valid text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _repository_id(value: object, label: str = "repository_id") -> str:
    text = _text(value, label, maximum=MAX_PATH_CHARS)
    if "/" not in text:
        raise _ManifestError("invalid_field", f"{label} is not repository-qualified")
    host, path = text.split("/", 1)
    parts = path.split("/")
    if (_HOST.fullmatch(host) is None or host != host.lower()
            or host.startswith(".") or host.endswith(".") or ".." in host
            or any(not part or part in {".", ".."} or "\\" in part
                   for part in parts)):
        raise _ManifestError("invalid_field", f"{label} is not canonical")
    return text


def _oid(value: object, label: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise _ManifestError("invalid_field", f"{label} must be a 40-hex commit")
    return value


def _tracking_ref(value: object, label: str) -> str:
    text = _text(value, label, maximum=MAX_TRACKING_REF_CHARS)
    repository, marker, number = text.rpartition("#")
    if (not marker or _ISSUE_NUMBER.fullmatch(number) is None
            or _repository_id(repository, label) != repository):
        raise _ManifestError("invalid_field", f"{label} is not repository-qualified")
    return text


def _path(value: object, label: str) -> str:
    text = _text(value, label, maximum=MAX_PATH_CHARS)
    parts = text.split("/")
    if (text.startswith("/") or text.endswith("/") or "\\" in text
            or any(not part or part in {".", ".."} for part in parts)):
        raise _ManifestError("invalid_field", f"{label} is not a safe relative path")
    return text


def _line(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise _ManifestError("invalid_field", f"{label} must be a positive integer")
    return value


def _array(value: object, label: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise _ManifestError("invalid_field", f"{label} must be an array")
    if len(value) > maximum:
        raise _ManifestError("limit_exceeded", f"{label} exceeds its item limit")
    return value


def _parse_scope(value: object, label: str) -> OwnershipScope:
    obj = _object(value, label)
    _exact_keys(
        obj,
        {"kind", "path", "exclusive", "line_start", "line_end", "symbol"},
        label,
    )
    kind = obj["kind"]
    if kind not in {"file", "prefix"}:
        raise _ManifestError("invalid_field", f"{label}.kind is unsupported")
    path = _path(obj["path"], f"{label}.path")
    if type(obj["exclusive"]) is not bool:
        raise _ManifestError("invalid_field", f"{label}.exclusive must be boolean")
    line_start = _line(obj["line_start"], f"{label}.line_start")
    line_end = _line(obj["line_end"], f"{label}.line_end")
    if (line_start is None) != (line_end is None) or (
            line_start is not None and line_start > line_end):
        raise _ManifestError("invalid_field", f"{label} has an invalid line range")
    symbol = _optional_text(obj["symbol"], f"{label}.symbol")
    if kind == "prefix" and (line_start is not None or symbol is not None):
        raise _ManifestError("invalid_field", f"{label} prefix cannot have anchors")
    return OwnershipScope(
        kind=kind,
        path=path,
        exclusive=obj["exclusive"],
        line_start=line_start,
        line_end=line_end,
        symbol=symbol,
    )


def _parse_scopes(value: object, label: str) -> tuple[OwnershipScope, ...]:
    items = _array(value, label, MAX_SCOPES_PER_OWNER)
    return tuple(_parse_scope(item, f"{label}[{index}]")
                 for index, item in enumerate(items))


def _parse_slice(value: object, label: str) -> StackSlice:
    obj = _object(value, label)
    _exact_keys(obj, {"slice_id", "commit", "tracking_ref", "ownership"}, label)
    return StackSlice(
        slice_id=_text(obj["slice_id"], f"{label}.slice_id"),
        commit=_oid(obj["commit"], f"{label}.commit"),
        tracking_ref=_tracking_ref(obj["tracking_ref"], f"{label}.tracking_ref"),
        ownership=_parse_scopes(obj["ownership"], f"{label}.ownership"),
    )


def _parse_downstream(value: object, label: str) -> DownstreamOwner:
    obj = _object(value, label)
    _exact_keys(obj, {"tracking_ref", "ownership", "known_finding_refs"}, label)
    refs = _array(
        obj["known_finding_refs"], f"{label}.known_finding_refs",
        MAX_KNOWN_FINDING_REFS,
    )
    return DownstreamOwner(
        tracking_ref=_tracking_ref(obj["tracking_ref"], f"{label}.tracking_ref"),
        ownership=_parse_scopes(obj["ownership"], f"{label}.ownership"),
        known_finding_refs=tuple(
            _text(item, f"{label}.known_finding_refs[{index}]")
            for index, item in enumerate(refs)
        ),
    )


def _scope_dict(scope: OwnershipScope) -> dict[str, Any]:
    return {
        "kind": scope.kind,
        "path": scope.path,
        "exclusive": scope.exclusive,
        "line_start": scope.line_start,
        "line_end": scope.line_end,
        "symbol": scope.symbol,
    }


def _slice_dict(item: StackSlice) -> dict[str, Any]:
    return {
        "slice_id": item.slice_id,
        "commit": item.commit,
        "tracking_ref": item.tracking_ref,
        "ownership": [_scope_dict(scope) for scope in item.ownership],
    }


def _semantic_dict(manifest: StackManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "repository_id": manifest.repository_id,
        "certification_base": manifest.certification_base,
        "current_head": manifest.current_head,
        "direct_parent": manifest.direct_parent,
        "dependencies": [_slice_dict(item) for item in manifest.dependencies],
        "current_slice": _slice_dict(manifest.current_slice),
        "downstream_owners": [
            {
                "tracking_ref": item.tracking_ref,
                "ownership": [_scope_dict(scope) for scope in item.ownership],
                "known_finding_refs": list(item.known_finding_refs),
            }
            for item in manifest.downstream_owners
        ],
        "producer": {
            "id": manifest.producer.id,
            "version": manifest.producer.version,
        },
    }


def _manifest_digest(manifest: StackManifest) -> str:
    encoded = json.dumps(
        _semantic_dict(manifest), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _parse_manifest(document: object) -> StackManifest:
    obj = _object(document, "manifest")
    expected = {
        "schema_version", "repository_id", "certification_base",
        "current_head", "direct_parent", "dependencies", "current_slice",
        "downstream_owners", "producer", "manifest_digest",
    }
    _exact_keys(obj, expected, "manifest")
    if type(obj["schema_version"]) is not int:
        raise _ManifestError("invalid_field", "schema_version must be an integer")
    if obj["schema_version"] != 1:
        raise _ManifestError("unsupported_schema", "only schema_version 1 is supported")
    dependencies_raw = _array(
        obj["dependencies"], "dependencies", MAX_DEPENDENCIES)
    downstream_raw = _array(
        obj["downstream_owners"], "downstream_owners", MAX_DOWNSTREAM_OWNERS)
    producer_obj = _object(obj["producer"], "producer")
    _exact_keys(producer_obj, {"id", "version"}, "producer")
    manifest = StackManifest(
        schema_version=1,
        repository_id=_repository_id(obj["repository_id"]),
        certification_base=_oid(obj["certification_base"], "certification_base"),
        current_head=_oid(obj["current_head"], "current_head"),
        direct_parent=_optional_text(obj["direct_parent"], "direct_parent"),
        dependencies=tuple(
            _parse_slice(item, f"dependencies[{index}]")
            for index, item in enumerate(dependencies_raw)
        ),
        current_slice=_parse_slice(obj["current_slice"], "current_slice"),
        downstream_owners=tuple(
            _parse_downstream(item, f"downstream_owners[{index}]")
            for index, item in enumerate(downstream_raw)
        ),
        producer=ManifestProducer(
            id=_text(producer_obj["id"], "producer.id"),
            version=_text(producer_obj["version"], "producer.version"),
        ),
        manifest_digest=_text(
            obj["manifest_digest"], "manifest_digest", maximum=71),
    )
    total_scopes = (
        sum(len(item.ownership) for item in manifest.dependencies)
        + len(manifest.current_slice.ownership)
        + sum(len(item.ownership) for item in manifest.downstream_owners)
    )
    if total_scopes > MAX_TOTAL_SCOPES:
        raise _ManifestError("limit_exceeded", "ownership scopes exceed total limit")
    expected_parent = (
        manifest.dependencies[-1].slice_id if manifest.dependencies else None)
    if manifest.direct_parent != expected_parent:
        raise _ManifestError(
            "direct_parent_mismatch", "direct_parent is not the last dependency")
    if manifest.current_slice.commit != manifest.current_head:
        raise _ManifestError("stale_head", "current slice commit differs from current_head")
    slice_ids = [item.slice_id for item in manifest.dependencies]
    slice_ids.append(manifest.current_slice.slice_id)
    if len(set(slice_ids)) != len(slice_ids):
        raise _ManifestError("duplicate_slice", "slice ids must be unique")
    commits = [item.commit for item in manifest.dependencies]
    commits.append(manifest.current_slice.commit)
    if len(set(commits)) != len(commits):
        raise _ManifestError("duplicate_commit", "slice commits must be unique")
    if _DIGEST.fullmatch(manifest.manifest_digest) is None:
        raise _ManifestError("invalid_field", "manifest_digest is not canonical")
    if _manifest_digest(manifest) != manifest.manifest_digest:
        raise _ManifestError("digest_mismatch", "manifest digest does not match")
    return manifest


def load_request(path: Path | str) -> StackRequest:
    """Load one untrusted manifest as a total, bounded result."""
    try:
        data = _read_regular_file(Path(path))
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise _ManifestError("invalid_utf8", "manifest is not UTF-8") from exc
        try:
            document = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=_no_constant)
        except _ManifestError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise _ManifestError("malformed_json", type(exc).__name__) from exc
        manifest = _parse_manifest(document)
        return StackRequest(supplied=True, manifest=manifest, problem=None)
    except _ManifestError as exc:
        return _request_problem(exc.reason_code, exc.detail)
