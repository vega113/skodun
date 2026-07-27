from __future__ import annotations
import os, tomllib
from dataclasses import dataclass, fields
from pathlib import Path

EFFORTS = {"none", "low", "medium", "high", "max"}
ROLES = {"finder", "refuter", "security", "triager", "integrator"}

# Generic default for the security pass. Deliberately stack-agnostic: these
# segments name concerns, not one project's directory layout. A concrete repo
# layout belongs in that repo's own config (see examples/).
SECURITY_PATH_SEGMENTS: tuple[str, ...] = (
    "auth", "secret", "credential", "token", "webhook", "payment", "billing",
)

# Generic default fragments for the security-pass PROMPT. The pass ships one
# prompt body for every repo that runs skodun, so the parts of it that name what
# "risky" means here are slots, not literals: these defaults name concerns that
# exist in any stack. A repo whose risky surfaces have their own vocabulary
# fills the slots in its own config (see examples/). Slot values may contain
# newlines — the prompt is line-oriented and a slot may span a wrapped line.
SECURITY_PROMPT_SLOTS: tuple[tuple[str, str], ...] = (
    ("surfaces",
     "authentication, authorization, public HTTP endpoints, data access,\n"
     "webhooks, or payments"),
    ("extra_checks",
     "- webhook validation (shared secrets, signatures, unsigned public ingress)\n"
     "- payment and quota integrity (privilege escalation, free usage)"),
)
#: The slot names `security_prompt_slots` accepts. The prompt template in
#: `passes.py` must use exactly these (pinned by `tests/test_passes.py`).
SECURITY_PROMPT_SLOT_NAMES: frozenset[str] = frozenset(
    name for name, _ in SECURITY_PROMPT_SLOTS)

@dataclass(frozen=True)
class Defaults:
    severity_gate: str = "high"
    confidence_threshold: int = 7
    max_diff_bytes: int = 400_000
    timeout_sec: int = 420
    timeout_retries: int = 1
    degraded_retries: int = 1
    max_turns: int = 40
    deny_tools: str = "bash,read,write,edit,web_search,web_fetch"
    context_pack: bool = True
    checklist_dir: str = "docs/review/checklists"
    rules_json: str = "docs/review/code-rules.json"
    untracked_max: int = 100

    # --- Repo-layout tables (configuration, never code literals) ------------
    # skodun ships generic defaults so committed code carries no project's
    # layout. Users describe their own tree in `.skodun.toml`; a worked example
    # lives in examples/.
    #
    # Ordered path-prefix -> checklist-section mapping; first match wins.
    # Default empty: only the `core` section is selected.
    checklist_map: tuple[tuple[str, str], ...] = ()
    # Test-path patterns; a match selects the `tests` section. Default empty.
    test_path_patterns: tuple[str, ...] = ()
    # Path segments that trigger the security pass. Default: the generic set.
    security_path_segments: tuple[str, ...] = SECURITY_PATH_SEGMENTS
    # Basename/glob patterns that trigger the security pass. Default empty.
    security_basename_patterns: tuple[str, ...] = ()
    # (slot-name, fragment) pairs filling the security prompt's variable parts.
    # Default: the generic set. Partial tables are fine — an unfilled slot keeps
    # its generic default.
    security_prompt_slots: tuple[tuple[str, str], ...] = SECURITY_PROMPT_SLOTS

# Matching semantics for the four path tables above (how a prefix/pattern is
# compared against a path), and the prompt template `security_prompt_slots`
# fills, are deliberately NOT defined here — they belong to the consuming
# modules (`checklist.py`, `passes.py`), which own the parity tests that pin
# them. This module defines schema, defaults, and validation.

# Validation posture, on purpose — do not "fix" this to fail soft:
#   * Loading a MALFORMED value is LOUD (ValueError naming the key) — a
#     mistyped table, and equally an out-of-range or non-integer count. It is a
#     typo in the user's own config file and must not be silently swallowed,
#     nor left to surface as a TypeError from a slice several modules away.
#   * CONSUMING a well-formed table is FAIL-SOFT. A table that loads fine but
#     matches nothing — or points at a directory that does not exist — must
#     never crash a review; the consumer degrades to generic behavior with a
#     diagnostic note. The two rules govern different moments and do not
#     conflict.

def _str_tuple(key: str, value: object) -> tuple[str, ...]:
    """Normalize a TOML array of strings into a tuple, or raise naming `key`."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"[defaults] {key}: expected an array of strings, got "
            f"{type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"[defaults] {key}: entry {i} must be a string, got "
                f"{type(item).__name__}")
        if not item.strip():
            raise ValueError(f"[defaults] {key}: entry {i} must not be empty")
        out.append(item)
    return tuple(out)

def _pair_tuple(key: str, value: object) -> tuple[tuple[str, str], ...]:
    """Normalize a TOML array of 2-element string arrays, or raise naming `key`."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"[defaults] {key}: expected an array of 2-element arrays, got "
            f"{type(value).__name__}")
    out: list[tuple[str, str]] = []
    for i, pair in enumerate(value):
        if isinstance(pair, str) or not isinstance(pair, (list, tuple)):
            raise ValueError(
                f"[defaults] {key}: entry {i} must be a 2-element array, got "
                f"{type(pair).__name__}")
        if len(pair) != 2:
            raise ValueError(
                f"[defaults] {key}: entry {i} must be a 2-element array, got "
                f"{len(pair)} elements")
        for item in pair:
            if not isinstance(item, str):
                raise ValueError(
                    f"[defaults] {key}: entry {i} must contain strings, got "
                    f"{type(item).__name__}")
            if not item.strip():
                raise ValueError(f"[defaults] {key}: entry {i} must not be empty")
        out.append((pair[0], pair[1]))
    return tuple(out)

def _slot_pairs(key: str, value: object) -> tuple[tuple[str, str], ...]:
    """Normalize `(slot-name, fragment)` pairs, or raise naming `key`.

    A pair table like `checklist_map`, plus two checks that only make sense for
    named slots: the name has to be one the prompt actually has, and it has to
    appear once. Both are typos in the user's own config — a slot filled under a
    misspelled name would otherwise vanish silently and ship the generic prompt.
    """
    pairs = _pair_tuple(key, value)
    seen: set[str] = set()
    for i, (name, _fragment) in enumerate(pairs):
        if name not in SECURITY_PROMPT_SLOT_NAMES:
            raise ValueError(
                f"[defaults] {key}: entry {i} names unknown slot {name!r}; "
                f"known slots are {sorted(SECURITY_PROMPT_SLOT_NAMES)}")
        if name in seen:
            raise ValueError(
                f"[defaults] {key}: entry {i} repeats slot {name!r}")
        seen.add(name)
    return pairs

def _bounded_int(key: str, value: object, minimum: int) -> int:
    """Validate a numeric `[defaults]` value, or raise naming `key`.

    `bool` is a subclass of `int`, so `max_diff_bytes = true` would otherwise
    load as 1. Nobody means that; it is a typo, and it must say so.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"[defaults] {key}: expected an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(
            f"[defaults] {key}: must be >= {minimum}, got {value}")
    return value

# key -> minimum accepted value. Every integer field of `Defaults` appears here
# (`test_config.py` pins that, so a new numeric key cannot arrive unguarded).
# These are the user's own numbers from their own `.skodun.toml`, so a broken
# one is loud and early rather than a traceback — or, worse, a quietly useless
# review — thousands of lines into a run. Lower bounds only: an upper bound
# would be a policy guess this module has no basis for.
#   >= 1  the value is a capacity, and zero of it means the review cannot
#         happen at all: no diff bytes, no seconds, no turns, no confidence
#         level a finding could ever clear.
#   >= 0  zero is a coherent opt-out: do not retry, do not scan untracked
#         files. Negative still is not.
_DEFAULTS_MINIMUMS = {
    "confidence_threshold": 1,
    "max_diff_bytes": 1,
    "timeout_sec": 1,
    "max_turns": 1,
    "timeout_retries": 0,
    "degraded_retries": 0,
    "untracked_max": 0,
}

# key -> normalizer. Values arrive from TOML as lists; Defaults is frozen and
# instances must stay hashable, so everything becomes (nested) tuples — the way
# Reviewer.dimensions is already handled.
_DEFAULTS_NORMALIZERS = {
    "checklist_map": _pair_tuple,
    "test_path_patterns": _str_tuple,
    "security_path_segments": _str_tuple,
    "security_basename_patterns": _str_tuple,
    "security_prompt_slots": _slot_pairs,
}

@dataclass(frozen=True)
class Reviewer:
    name: str
    provider: str = ""
    model: str = ""
    role: str = "finder"
    effort: str | None = None
    dimensions: tuple[str, ...] = ()
    persona: str | None = None
    max_cost_usd: float | None = None
    enabled: bool = True

@dataclass(frozen=True)
class Config:
    defaults: Defaults
    reviewers: tuple[Reviewer, ...]
    def reviewer(self, name: str) -> Reviewer:
        for r in self.reviewers:
            if r.name == name:
                return r
        raise KeyError(name)

def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)

def _validate(r: Reviewer) -> Reviewer:
    if r.effort is not None and r.effort not in EFFORTS:
        raise ValueError(f"reviewer {r.name!r}: unknown effort {r.effort!r}")
    if r.role not in ROLES:
        raise ValueError(f"reviewer {r.name!r}: unknown role {r.role!r}")
    if not r.provider or not r.model:
        raise ValueError(f"reviewer {r.name!r}: provider and model are required")
    return r

def load_config(repo_root: Path | None, global_path: Path | None = None) -> Config:
    if global_path is None:
        global_path = Path(os.environ.get(
            "SKODUN_CONFIG", Path.home() / ".config" / "skodun" / "config.toml"))
    layers = [_read(global_path)]
    if repo_root is not None:
        layers.append(_read(Path(repo_root) / ".skodun.toml"))

    dvals: dict = {}
    rmap: dict[str, dict] = {}
    order: list[str] = []
    for layer in layers:
        dvals.update(layer.get("defaults", {}))
        for entry in layer.get("reviewers", []):
            if "name" not in entry:
                raise ValueError("reviewer entry is missing its required 'name' key")
            name = entry["name"]
            if name not in rmap:
                rmap[name] = {}; order.append(name)
            rmap[name].update(entry)   # later layer wins per-key, merged by name

    known = {f.name for f in fields(Defaults)}
    unknown = set(dvals) - known
    if unknown:
        raise ValueError(f"unknown [defaults] keys: {sorted(unknown)}")
    for key, normalize in _DEFAULTS_NORMALIZERS.items():
        if key in dvals:
            dvals[key] = normalize(key, dvals[key])
    for key, minimum in _DEFAULTS_MINIMUMS.items():
        if key in dvals:
            dvals[key] = _bounded_int(key, dvals[key], minimum)
    rknown = {f.name for f in fields(Reviewer)}
    reviewers = []
    for name in order:
        e = dict(rmap[name])
        bad = set(e) - rknown
        if bad:
            raise ValueError(f"reviewer {name!r}: unknown keys {sorted(bad)}")
        if "dimensions" in e:
            e["dimensions"] = tuple(e["dimensions"])
        reviewers.append(_validate(Reviewer(**e)))
    return Config(defaults=Defaults(**dvals), reviewers=tuple(reviewers))
