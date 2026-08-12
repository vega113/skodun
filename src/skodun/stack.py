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
_HUNK_RANGE = re.compile(
    rb"^@@ -[0-9]+(?:,[0-9]+)? \+(?P<start>[0-9]+)"
    rb"(?:,(?P<count>[0-9]+))? @@")


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
        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
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
    dependency_ids = [item.slice_id for item in manifest.dependencies]
    if manifest.current_slice.slice_id in dependency_ids:
        raise _ManifestError(
            "stack_cycle", "current slice also appears as a dependency")
    if len(set(dependency_ids)) != len(dependency_ids):
        raise _ManifestError("duplicate_slice", "slice ids must be unique")
    commits = [item.commit for item in manifest.dependencies]
    commits.append(manifest.current_slice.commit)
    if len(set(commits)) != len(commits):
        raise _ManifestError("duplicate_commit", "slice commits must be unique")
    expected_parent = (
        manifest.dependencies[-1].slice_id if manifest.dependencies else None)
    if manifest.direct_parent != expected_parent:
        raise _ManifestError(
            "direct_parent_mismatch", "direct_parent is not the last dependency")
    if manifest.current_slice.commit != manifest.current_head:
        raise _ManifestError("stale_head", "current slice commit differs from current_head")
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
    except (OSError, TypeError, ValueError) as exc:
        return _request_problem("unsafe_file", type(exc).__name__)


@dataclass(frozen=True)
class SliceEvidence:
    slice: StackSlice
    files: frozenset[str]
    statuses: tuple[tuple[str, str], ...]
    uncertain_files: frozenset[str]
    changed_lines: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]


@dataclass(frozen=True)
class StackValidation:
    status: str
    reason_code: str
    manifest: StackManifest | None
    dependencies: tuple[SliceEvidence, ...] = ()
    current_slice: SliceEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        """Bounded artifact/service projection; never the raw manifest."""
        manifest = self.manifest
        return {
            "schema_version": None if manifest is None else manifest.schema_version,
            "status": self.status,
            "reason_code": self.reason_code,
            "repository_id": None if manifest is None else manifest.repository_id,
            "manifest_digest": (
                None if manifest is None else manifest.manifest_digest),
            "current_slice_id": (
                None if manifest is None else manifest.current_slice.slice_id),
            "direct_parent": None if manifest is None else manifest.direct_parent,
            "dependency_count": (
                0 if manifest is None else len(manifest.dependencies)),
            "downstream_owner_count": (
                0 if manifest is None else len(manifest.downstream_owners)),
        }


def render_projection(value: dict[str, Any]) -> str:
    """Render one bounded service line from a persisted stack projection."""
    status = value.get("status")
    reason = value.get("reason_code")
    if not isinstance(status, str) or not status:
        status = "ignored"
    if not isinstance(reason, str) or not reason:
        reason = "missing_projection"
    parts = [f"SKODUN STACK: status={status}"]
    if status != "valid":
        parts.append(f"reason={reason}")
    optional = () if status != "valid" else (
        ("slice", value.get("current_slice_id")),
        ("dependencies", value.get("dependency_count")),
        ("digest", value.get("manifest_digest")),
    )
    for label, item in optional:
        if isinstance(item, str) and item:
            parts.append(f"{label}={item}")
        elif label == "dependencies" and type(item) is int and item >= 0:
            parts.append(f"{label}={item}")
    return " ".join(parts)


def _ignored(request: StackRequest, reason_code: str) -> StackValidation:
    return StackValidation(
        status="ignored",
        reason_code=reason_code,
        manifest=request.manifest,
    )


def _scope_path_matches(scope: OwnershipScope, path: str) -> bool:
    if scope.kind == "file":
        return path == scope.path
    return path == scope.path or path.startswith(scope.path + "/")


def _scopes_overlap(left: OwnershipScope, right: OwnershipScope) -> bool:
    if (left.symbol is not None and right.symbol is not None
            and left.symbol != right.symbol):
        return False
    if left.kind == "file" and right.kind == "file":
        paths_overlap = left.path == right.path
    elif left.kind == "prefix" and right.kind == "prefix":
        paths_overlap = (
            left.path == right.path
            or left.path.startswith(right.path + "/")
            or right.path.startswith(left.path + "/")
        )
    else:
        file_scope = left if left.kind == "file" else right
        prefix_scope = right if right.kind == "prefix" else left
        paths_overlap = _scope_path_matches(prefix_scope, file_scope.path)
    if not paths_overlap:
        return False
    if (left.line_start is None or right.line_start is None
            or left.path != right.path):
        return True
    return not (left.line_end < right.line_start
                or right.line_end < left.line_start)


def _changed_line_map(
    evidence: SliceEvidence,
) -> dict[str, tuple[tuple[int, int], ...]]:
    return dict(evidence.changed_lines)


def _scope_is_reachable(scope: OwnershipScope, evidence: SliceEvidence) -> bool:
    matched_paths = [
        path for path in evidence.files if _scope_path_matches(scope, path)
    ]
    if not matched_paths:
        return False
    if scope.line_start is None:
        return True
    if any(path in evidence.uncertain_files for path in matched_paths):
        return True
    changed = _changed_line_map(evidence)
    return any(
        not (end < scope.line_start or start > scope.line_end)
        for path in matched_paths
        for start, end in changed.get(path, ())
    )


def _slice_evidence(item: StackSlice, diff: object) -> SliceEvidence:
    from . import batching

    files = frozenset(str(path) for path in getattr(diff, "files", ()) if path)
    statuses = {
        str(path): str(code)
        for path, code in dict(getattr(diff, "statuses", {})).items()
    }
    uncertain = {
        path for path, code in statuses.items() if code[:1] in {"R", "C", "D"}
    }
    lines = getattr(diff, "data", b"").splitlines(keepends=True)
    changed_lines: dict[str, list[tuple[int, int]]] = {}
    for section in batching.sections(lines):
        path = batching.file_of(section)
        added_lines: list[int] = []
        new_line: int | None = None
        for line in section:
            match = _HUNK_RANGE.match(line)
            if match is not None:
                start = int(match.group("start"))
                new_line = start
                continue
            if new_line is None or line.startswith(b"\\"):
                continue
            prefix = line[:1]
            if prefix == b"+":
                added_lines.append(new_line)
                new_line += 1
            elif prefix == b"-":
                continue
            elif prefix == b" ":
                new_line += 1
        ranges: list[tuple[int, int]] = []
        for line_number in sorted(set(added_lines)):
            if ranges and line_number == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], line_number)
            else:
                ranges.append((line_number, line_number))
        if path and not ranges:
            uncertain.add(path)
        if path and ranges:
            changed_lines.setdefault(path, []).extend(ranges)
    return SliceEvidence(
        slice=item,
        files=files,
        statuses=tuple(sorted(statuses.items())),
        uncertain_files=frozenset(uncertain),
        changed_lines=tuple(
            (path, tuple(ranges))
            for path, ranges in sorted(changed_lines.items())
        ),
    )


def validate(
    request: StackRequest,
    *,
    repo: Path,
    certification_base: str,
    current_head: str,
    full_diff: object,
    full_tree_fingerprint: str,
    untracked_max: int,
) -> StackValidation:
    """Validate attribution against one already captured full review identity."""
    from . import gitio

    if request.problem is not None:
        return _ignored(request, request.problem.reason_code)
    manifest = request.manifest
    if manifest is None:
        return _ignored(request, "invalid_field")
    try:
        repository_id = gitio.canonical_repository_identity(Path(repo))
    except gitio.GitError:
        return _ignored(request, "git_error")
    except (OSError, UnicodeError):
        repository_id = None
    if repository_id is None:
        return _ignored(request, "repository_unresolved")
    if manifest.repository_id != repository_id:
        return _ignored(request, "repository_mismatch")
    tracking_refs = [
        *(item.tracking_ref for item in manifest.dependencies),
        manifest.current_slice.tracking_ref,
        *(item.tracking_ref for item in manifest.downstream_owners),
    ]
    if any(ref.rpartition("#")[0] != manifest.repository_id
           for ref in tracking_refs):
        return _ignored(request, "tracking_repository_mismatch")
    if manifest.certification_base != certification_base:
        return _ignored(request, "stale_base")
    if manifest.current_head != current_head:
        return _ignored(request, "stale_head")
    if getattr(full_diff, "truncated_untracked", False) is True:
        return _ignored(request, "git_error")

    chain = [manifest.certification_base]
    chain.extend(item.commit for item in manifest.dependencies)
    chain.append(manifest.current_head)
    try:
        for oid in chain:
            object_type = gitio.exact_object_type(Path(repo), oid)
            if object_type is None:
                return _ignored(request, "missing_commit")
            if object_type != "commit":
                return _ignored(request, "not_commit")
        for edge, (older, newer) in enumerate(zip(chain, chain[1:])):
            dirty_only_identity = (
                not manifest.dependencies and edge == 0 and older == newer)
            if (not dirty_only_identity
                    and (older == newer
                         or not gitio.is_ancestor(Path(repo), older, newer))):
                return _ignored(request, "dependency_reordered")
    except gitio.GitError:
        return _ignored(request, "git_error")

    try:
        dependencies: list[SliceEvidence] = []
        previous = manifest.certification_base
        for item in manifest.dependencies:
            diff = gitio.capture_ref_diff(Path(repo), previous, item.commit)
            dependencies.append(_slice_evidence(item, diff))
            previous = item.commit
        current_diff = gitio.capture_diff(Path(repo), previous, untracked_max)
        current = _slice_evidence(manifest.current_slice, current_diff)
        recaptured = gitio.capture_diff(
            Path(repo), certification_base, untracked_max)
        same_full_identity = (
            gitio.diff_identity(recaptured.data)
            == gitio.diff_identity(getattr(full_diff, "data", b""))
            and list(recaptured.files) == list(getattr(full_diff, "files", ()))
            and dict(recaptured.statuses)
            == dict(getattr(full_diff, "statuses", {}))
            and recaptured.truncated_untracked
            is getattr(full_diff, "truncated_untracked", False)
            and gitio.tree_fingerprint(Path(repo), paths=recaptured.files)
            == full_tree_fingerprint
        )
    except Exception:
        return _ignored(request, "git_error")
    if not same_full_identity:
        return _ignored(request, "git_error")

    all_evidence = [*dependencies, current]
    for evidence in all_evidence:
        if any(not _scope_is_reachable(scope, evidence)
               for scope in evidence.slice.ownership):
            return _ignored(request, "ownership_unreachable")
    for index, left in enumerate(all_evidence):
        for right in all_evidence[index + 1:]:
            for left_scope in left.slice.ownership:
                for right_scope in right.slice.ownership:
                    if (left_scope.exclusive and right_scope.exclusive
                            and _scopes_overlap(left_scope, right_scope)):
                        return _ignored(request, "exclusive_scope_overlap")

    return StackValidation(
        status="valid",
        reason_code="ok",
        manifest=manifest,
        dependencies=tuple(dependencies),
        current_slice=current,
    )


def _finding_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return _path(value, "finding.file")
    except _ManifestError:
        return None


def _fixture_or_test(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(part in {"test", "tests", "fixtures", "__tests__"}
            for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith((
            "_test.py", "_test.go", ".test.js", ".test.ts",
            ".spec.js", ".spec.ts",
        ))
    )


def _scope_matches_finding(
    scope: OwnershipScope,
    finding: dict[str, Any],
    path: str,
    evidence: SliceEvidence | None = None,
) -> bool:
    if not _scope_path_matches(scope, path):
        return False
    if scope.line_start is not None:
        line = finding.get("line")
        if type(line) is not int or not scope.line_start <= line <= scope.line_end:
            return False
        if evidence is not None and not any(
                start <= line <= end
                for start, end in _changed_line_map(evidence).get(path, ())):
            if path not in evidence.uncertain_files:
                return False
    if scope.symbol is not None and finding.get("symbol") != scope.symbol:
        return False
    return True


def _attribution(
    scope: str,
    reason_code: str,
    *,
    owner_slice_id: str | None = None,
    owner_ref: str | None = None,
    known_finding_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    attribution = {
        "scope": scope,
        "reason_code": reason_code,
        "owner_slice_id": owner_slice_id,
        "owner_ref": owner_ref,
    }
    if known_finding_refs:
        attribution["known_finding_refs"] = list(known_finding_refs)
    return attribution


def classify_findings(
    findings: list,
    result: StackValidation,
) -> list[Any]:
    """Return additive, conservative scope annotations for raw findings."""
    out: list[Any] = []
    for raw in findings if isinstance(findings, list) else []:
        if not isinstance(raw, dict):
            out.append(raw)
            continue
        finding = dict(raw)
        path = _finding_path(finding.get("file"))
        if result.status != "valid":
            attribution = _attribution("unknown", result.reason_code)
        elif path is None:
            attribution = _attribution("unknown", "invalid_finding_path")
        elif _fixture_or_test(path):
            attribution = _attribution("fixture_or_test", "test_or_fixture_path")
        else:
            stack_matches: list[tuple[str, SliceEvidence, OwnershipScope]] = []
            if result.current_slice is not None:
                for scope in result.current_slice.slice.ownership:
                    if (_scope_matches_finding(
                            scope, finding, path, result.current_slice)
                            and path in result.current_slice.files):
                        stack_matches.append((
                            "current_slice", result.current_slice, scope))
            for evidence in result.dependencies:
                for scope in evidence.slice.ownership:
                    if (_scope_matches_finding(scope, finding, path, evidence)
                            and path in evidence.files):
                        stack_matches.append((
                            "inherited_dependency", evidence, scope))
            downstream_matches = []
            if result.manifest is not None:
                for owner in result.manifest.downstream_owners:
                    if any(_scope_matches_finding(scope, finding, path)
                           for scope in owner.ownership):
                        downstream_matches.append(owner)

            # Several scopes from one slice are one ownership match. Counting
            # scopes would mislabel a file+prefix declaration from one owner as
            # cross-slice integration.
            stack_owners: dict[
                tuple[str, str, str], tuple[str, SliceEvidence, bool]
            ] = {}
            for kind, evidence, scope in stack_matches:
                key = (kind, evidence.slice.slice_id,
                       evidence.slice.tracking_ref)
                previous = stack_owners.get(key)
                stack_owners[key] = (
                    kind, evidence,
                    scope.exclusive or bool(previous and previous[2]),
                )
            owner_matches = list(stack_owners.values())

            if any(path in evidence.uncertain_files
                   for _kind, evidence, _exclusive in owner_matches):
                attribution = _attribution("unknown", "uncertain_git_mapping")
            elif len(owner_matches) == 1 and not downstream_matches:
                kind, evidence, _exclusive = owner_matches[0]
                attribution = _attribution(
                    kind,
                    ("exact_current_scope" if kind == "current_slice"
                     else "exact_dependency_scope"),
                    owner_slice_id=evidence.slice.slice_id,
                    owner_ref=evidence.slice.tracking_ref,
                )
            elif (len(owner_matches) > 1 and not downstream_matches
                  and all(not exclusive
                          for _kind, _evidence, exclusive in owner_matches)):
                attribution = _attribution("integration", "cross_slice_scope")
            elif not owner_matches and len(downstream_matches) == 1:
                attribution = _attribution(
                    "downstream_owned", "exact_downstream_scope",
                    owner_ref=downstream_matches[0].tracking_ref,
                    known_finding_refs=downstream_matches[0].known_finding_refs,
                )
            elif owner_matches or downstream_matches:
                attribution = _attribution("unknown", "ambiguous_owner")
            else:
                attribution = _attribution("unknown", "no_owner_evidence")
        finding["scope_attribution"] = attribution
        out.append(finding)
    return out
