from __future__ import annotations
import math, os, tomllib
from dataclasses import dataclass, fields
from pathlib import Path

#: The canonical reasoning-effort vocabulary a `[reviewers]` entry may use.
#: These are skodun's OWN names, not any CLI's: each adapter maps them to
#: whatever its binary spells them as (`base.Adapter.effort_map`), and an
#: effort an adapter cannot map is a loud error rather than a dropped flag.
#:
#: `"none"` means **the least reasoning the provider offers**, and what that
#: is depends on the provider — deliberately, because the alternative is a
#: config value that fails on half the CLIs:
#:
#: * where the CLI has no such setting, `"none"` passes NO effort flag at all
#:   and the provider's own default applies (this is what the grok adapter
#:   does);
#: * where the CLI has a real lowest level, `"none"` requests it explicitly
#:   (the codex adapter passes `model_reasoning_effort=none`, which is a value
#:   in the OpenAI API's own enum).
#:
#: Either way it is the floor, and it is the only canonical value an adapter is
#: allowed to leave out of its `effort_map`. Omitting `effort` entirely is a
#: different thing again: unset means "do not express an opinion", so no flag is
#: passed regardless of what the CLI supports.
#:
#: `"max"` is the ceiling and is likewise mapped per provider — an adapter may
#: map it to the highest level EVERY model it serves accepts rather than to a
#: level only some of them offer.
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
    """The `[defaults]` table of `.skodun.toml`."""

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
#         happen at all: no diff bytes, no seconds, no turns.
#   >= 0  zero is a coherent opt-out: do not retry, do not scan untracked
#         files. Negative still is not.
_DEFAULTS_MINIMUMS = {
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

# key -> migration message. `severity_gate` and `confidence_threshold` were
# Phase 1's forward-looking stubs: declared and bounds-checked so a config
# written for a later phase loaded without an "unknown [defaults] keys"
# error, but nothing ever read either one -- `gate.open_findings` blocks on
# ANY untriaged finding regardless of severity, by design. A key that looks
# like it filters findings but does not is a safety trap, so Phase 2 removes
# both rather than ever implement the filter. Loading a config that still
# sets one must read as a decision, not a typo, so this check runs BEFORE the
# generic unknown-key check in `load_config` and produces this dedicated
# message instead of falling through to "unknown [defaults] keys".
_REMOVED_DEFAULTS = {
    "severity_gate": (
        "[defaults] severity_gate was removed in Phase 2: the gate blocks "
        "on any open finding by design — delete the key"),
    "confidence_threshold": (
        "[defaults] confidence_threshold was removed in Phase 2: the gate "
        "blocks on any open finding by design — delete the key"),
}

# Fallback-chain shape limit: head reviewer + at most 3 fallback entries (4
# total), matching the "head + <=3 fallbacks" budget Task 7's execution loop
# is built around.
_MAX_FALLBACK_CHAIN = 3

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
    #: This entry's OWN prompt envelope, overriding `[defaults] max_diff_bytes`.
    #: None means "use the global", which is what every config that does not
    #: set it says.
    #:
    #: The global is one number for every reviewer, so it has to be fitted to
    #: the LEAST capable provider configured — which needlessly shrinks the
    #: review for every other one. A `codex` entry whose prompt travels as a
    #: file can carry the whole change while an `agy` entry beside it, whose
    #: prompt must fit one argv word, cannot. Validated exactly like the
    #: `[defaults]` key it overrides (`_reviewer_max_diff_bytes`): it lands in
    #: the same arithmetic, so a bad value is the same typo.
    #:
    #: This is a CEILING the operator asks for, not a guarantee: the planner
    #: takes the smaller of it and the head adapter's own declared limit
    #: (`budget.prompt_budget`), because an entry cannot be budgeted above what
    #: its CLI can physically accept.
    max_diff_bytes: int | None = None
    #: Ordered quota-fallback chain: other reviewer entries (by name) to try,
    #: in order, when THIS reviewer's own attempt classifies `unavailable`.
    #: Runtime-only uses the HEAD reviewer's list -- a chain member's own
    #: `fallbacks` are never followed while executing a fallback attempt, so
    #: a chain is not transitively expanded at run time (Task 7). Validation
    #: is stricter than execution on purpose: every referenced name must
    #: exist after merging, be `enabled`, not be the reviewer itself, appear
    #: at most once, and the chain (this list) may hold at most
    #: `_MAX_FALLBACK_CHAIN` entries. Cycle validation, unlike execution,
    #: DOES walk each member's own `fallbacks` transitively, so a mutual (or
    #: longer) cycle is rejected at load time rather than discovered mid-run.
    fallbacks: tuple[str, ...] = ()

@dataclass(frozen=True)
class Dispatch:
    """The `[dispatch]` table of `.skodun.toml`: the BACKGROUND worker's budget.

    A table of its own rather than five more `[defaults]` keys, because the two
    describe different runs. `[defaults]` is what a FOREGROUND `skodun review`
    may spend -- a human is waiting, and the whole diff is in one prompt.
    `[dispatch]` is what a detached pre-push worker may spend: the push is
    already over, nothing is blocked on the answer, and a much tighter timeout
    is the right default (the oracle's own background cap is 240s against a 420s
    foreground one).

    * `enabled` -- the CONFIG-level kill switch, parallel to the per-repo
      `git config skodun.prepush false` bypass. False means the dispatcher
      notes it once on stderr and discards every ref: no capture, no
      reservation, no worker, no record.
    * `timeout_sec` / `timeout_retries` -- the worker's effective `Defaults`
      are `replace(defaults, timeout_sec=..., timeout_retries=...)` from these
      two; every other key comes from `[defaults]` untouched.
    * `dedup` -- whether a push whose diff a trustworthy review already covers
      may be suppressed at all. False disables the whole suppression path,
      evidence included.
    * `large_prompt_bytes` -- the per-prompt size above which a background
      attempt's cap escalates to the FOREGROUND `defaults.timeout_sec` (oracle
      A14.7). A whole-diff prompt this large legitimately needs longer than a
      background cap allows, and timing it out would spend the budget and
      record nothing. (The spec's `large_prompt_escalation` name is superseded
      by this one; a config still using it is rejected as an unknown key.)
    """

    enabled: bool = True
    timeout_sec: int = 240
    timeout_retries: int = 0
    dedup: bool = True
    large_prompt_bytes: int = 80_000


# key -> minimum accepted value, exactly as `_DEFAULTS_MINIMUMS` above and for
# the same reason. `test_config.py` pins that every integer field of `Dispatch`
# appears here, so a new numeric key cannot arrive unguarded.
#   >= 1  a capacity: zero seconds is a worker that cannot review, and a zero
#         `large_prompt_bytes` would escalate every prompt (which is a coherent
#         wish, spelled by setting it to 1 -- but a zero would read as "off"
#         while doing the opposite, so it is refused).
#   >= 0  zero is a coherent opt-out: do not retry a timeout.
_DISPATCH_MINIMUMS = {
    "timeout_sec": 1,
    "timeout_retries": 0,
    "large_prompt_bytes": 1,
}

#: The `bool` fields, validated as EXACT bools. `bool("false")` is True and
#: `enabled = "false"` must never enable dispatch -- the same refusal to coerce
#: the store applies to the trust axes.
_DISPATCH_FLAGS = ("dedup", "enabled")


def _strict_bool(key: str, value: object) -> bool:
    """Validate a `[dispatch]` flag, or raise naming `key`.

    No truthiness anywhere near this. TOML has real `true`/`false` literals, so
    anything else in a boolean key is a typo -- and the two typos that matter
    (`"false"`, `0`) would both be read the WRONG way by a truthiness coercion:
    `bool("false")` is True, and `enabled = 0` reading as False is only
    accidentally right while `dedup = 1` reading as True is accidentally wrong.
    """
    if not isinstance(value, bool):
        raise ValueError(
            f"[dispatch] {key}: expected true or false, got "
            f"{type(value).__name__}")
    return value


def _bounded_dispatch_int(key: str, value: object, minimum: int) -> int:
    """`_bounded_int` for `[dispatch]`, naming that table in the message.

    A separate function only because the message names the table: a user whose
    `[dispatch] timeout_sec` is wrong must not be sent to `[defaults]`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"[dispatch] {key}: expected an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(
            f"[dispatch] {key}: must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    # Reviewers are selected by ROLE, never by name: `pipeline._reviewer_for`
    # takes the first enabled reviewer whose `role` matches. A lookup-by-name
    # helper lived here and had no caller outside its own test; it is gone
    # rather than kept as a second, unused selection rule.
    defaults: Defaults
    reviewers: tuple[Reviewer, ...]
    #: The `[dispatch]` table. Defaulted so that every shipped construction of
    #: `Config(defaults=..., reviewers=...)` -- in this module and in the tests
    #: that build one by hand -- keeps working unchanged, and so a config file
    #: with no `[dispatch]` table gets the documented defaults rather than None.
    dispatch: Dispatch = Dispatch()

def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)

def _max_cost(name: str, value: object) -> float | int:
    """Validate one reviewer's `max_cost_usd`, or raise naming the reviewer.

    Phase 1 declared this field and nothing read it; the claude adapter is its
    first consumer, where it becomes `--max-budget-usd <v>`. Validation starts
    here because the failure it prevents is unrecoverable downstream: probed
    live against Claude Code 2.1.118, that flag rejects `0`, `-1`, `abc` and
    `nan` with

        error: --max-budget-usd must be a positive number greater than 0

    thrown as an UNCAUGHT exception — rc 1, a stack trace and a source dump on
    stderr, and completely empty stdout. No result envelope is written at all,
    so no adapter can tell that apart from a provider that produced nothing,
    and the fail-closed answer it would reach ("no trustworthy review") hides a
    one-character typo in the user's own config file.

    The three checks, in this order and each for its own reason:

    * `bool` FIRST, because it subclasses `int`: TOML `max_cost_usd = true`
      would otherwise sail through as a one-dollar cap that nobody wrote.
    * finite, because TOML has real `nan` and `inf` literals. `nan` fails every
      comparison silently, so a bare `value > 0` would let it through; `inf` is
      a cap that caps nothing, and "no cap" is already spelled by omitting the
      key.
    * strictly positive, matching the CLI's own rule. Zero is not a budget of
      zero dollars, it is a run that cannot happen.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"reviewer {name!r}: max_cost_usd must be a positive number, got "
            f"{type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(
            f"reviewer {name!r}: max_cost_usd must be a finite number, got "
            f"{value}")
    if value <= 0:
        raise ValueError(
            f"reviewer {name!r}: max_cost_usd must be greater than 0, got "
            f"{value}")
    return value

def _reviewer_max_diff_bytes(name: str, value: object) -> int:
    """Validate one reviewer's `max_diff_bytes`, or raise naming the reviewer.

    The SAME rule as `_bounded_int` applies to `[defaults] max_diff_bytes` —
    an integer, not a `bool`, at least 1 — because the two values reach exactly
    the same arithmetic: the planner takes one or the other and slices the diff
    with it. A separate function only because the message names the REVIEWER: a
    config may hold several entries, and the number's provenance is lost by the
    time it reaches `promptbuild.build`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"reviewer {name!r}: max_diff_bytes: expected an integer, got "
            f"{type(value).__name__}")
    if value < _DEFAULTS_MINIMUMS["max_diff_bytes"]:
        raise ValueError(
            f"reviewer {name!r}: max_diff_bytes: must be >= "
            f"{_DEFAULTS_MINIMUMS['max_diff_bytes']}, got {value}")
    return value


def _validate(r: Reviewer) -> Reviewer:
    if r.effort is not None and r.effort not in EFFORTS:
        raise ValueError(f"reviewer {r.name!r}: unknown effort {r.effort!r}")
    if r.max_cost_usd is not None:
        _max_cost(r.name, r.max_cost_usd)
    if r.max_diff_bytes is not None:
        _reviewer_max_diff_bytes(r.name, r.max_diff_bytes)
    if r.role not in ROLES:
        raise ValueError(f"reviewer {r.name!r}: unknown role {r.role!r}")
    if not r.provider or not r.model:
        raise ValueError(f"reviewer {r.name!r}: provider and model are required")
    return r

def _fallback_tuple(name: str, value: object) -> tuple[str, ...]:
    """Normalize one reviewer's raw TOML `fallbacks` value into a tuple of
    names, or raise naming the reviewer and the actual shape problem.

    Runs per-reviewer, before `_validate_fallbacks`, so none of the semantic
    checks (existence, enabled, self-reference, duplicates, length, cycles)
    ever see a shape they don't expect. Two hostile shapes motivate this:

    * A bare string (`fallbacks = "backup"`, a plausible typo for
      `["backup"]`) is itself iterable -- without this check it would be
      iterated character-by-character and reported as fabricated missing
      reviewers named single letters, which is worse than a crash because it
      sends the user looking for a reviewer that was never named.
    * A nested array or inline table entry (`[["backup"]]`, `[{name =
      "backup"}]`) is unhashable -- without this check it would reach the
      duplicate-check's `set` in `_validate_fallbacks` and crash with an
      unhandled `TypeError` instead of a clean config error.

    The entry check is a strict `isinstance(item, str)`. `bool` is a
    subclass of `int` in Python (elsewhere in this module `_bounded_int`
    special-cases it), but `bool` is NOT a subclass of `str`, so this check
    already excludes `fallbacks = [true]` correctly with no special-casing
    needed.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"reviewer {name!r}: fallbacks must be an array of strings, got "
            f"{type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"reviewer {name!r}: fallbacks entry {i} must be a string, "
                f"got {type(item).__name__}")
        out.append(item)
    return tuple(out)

def _validate_fallbacks(reviewers: tuple[Reviewer, ...]) -> None:
    """Validate every `fallbacks` chain against the full, merged reviewer set.

    Callers must run `_fallback_tuple` on each reviewer's raw `fallbacks`
    value before constructing `Reviewer` instances, so by the time chains
    reach here every entry is already known to be a plain string -- this
    function only does semantic validation (existence, enabled,
    self-reference, duplicates, length, cycles), never shape validation.

    Every validation failure names both the reviewer whose chain is bad and
    the specific problem, so a config error is locatable without reading this
    module. Two passes on purpose: the first checks each reviewer's own list
    in isolation (existence, self-reference, duplicates, length) so those
    messages are as specific as possible; only once every list is known
    well-formed does the second pass walk chains transitively for cycles --
    a cycle walk that hit a still-unvalidated (e.g. nonexistent) target would
    produce a confusing KeyError instead of a config error.
    """
    by_name = {r.name: r for r in reviewers}
    for r in reviewers:
        seen: set[str] = set()
        for target in r.fallbacks:
            if target == r.name:
                raise ValueError(f"reviewer {r.name!r}: cannot be its own fallback")
            if target in seen:
                raise ValueError(
                    f"reviewer {r.name!r}: fallback {target!r} listed more than once")
            seen.add(target)
            if target not in by_name:
                raise ValueError(
                    f"reviewer {r.name!r}: fallback {target!r} does not exist")
            if not by_name[target].enabled:
                raise ValueError(
                    f"reviewer {r.name!r}: fallback {target!r} is disabled")
        if len(r.fallbacks) > _MAX_FALLBACK_CHAIN:
            raise ValueError(
                f"reviewer {r.name!r}: fallback chain has {len(r.fallbacks)} "
                f"entries, at most {_MAX_FALLBACK_CHAIN} are allowed")

    def _walk(name: str, path: list[str]) -> None:
        for target in by_name[name].fallbacks:
            if target in path:
                raise ValueError(
                    f"reviewer {path[0]!r}: fallback chain has a cycle at {target!r}")
            _walk(target, path + [target])

    for r in reviewers:
        if r.fallbacks:
            _walk(r.name, [r.name])

def load_config(repo_root: Path | None, global_path: Path | None = None) -> Config:
    if global_path is None:
        global_path = Path(os.environ.get(
            "SKODUN_CONFIG", Path.home() / ".config" / "skodun" / "config.toml"))
    layers = [_read(global_path)]
    if repo_root is not None:
        layers.append(_read(Path(repo_root) / ".skodun.toml"))

    dvals: dict = {}
    pvals: dict = {}
    rmap: dict[str, dict] = {}
    order: list[str] = []
    for layer in layers:
        dvals.update(layer.get("defaults", {}))
        # Per-KEY merge, exactly like `[defaults]`: a project file that sets one
        # dispatch key keeps the global file's answer for the others.
        pvals.update(layer.get("dispatch", {}))
        for entry in layer.get("reviewers", []):
            if "name" not in entry:
                raise ValueError("reviewer entry is missing its required 'name' key")
            name = entry["name"]
            if name not in rmap:
                rmap[name] = {}; order.append(name)
            rmap[name].update(entry)   # later layer wins per-key, merged by name

    removed = set(dvals) & set(_REMOVED_DEFAULTS)
    if removed:
        raise ValueError(_REMOVED_DEFAULTS[sorted(removed)[0]])
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
    pknown = {f.name for f in fields(Dispatch)}
    punknown = set(pvals) - pknown
    if punknown:
        raise ValueError(f"unknown [dispatch] keys: {sorted(punknown)}")
    for key in _DISPATCH_FLAGS:
        if key in pvals:
            pvals[key] = _strict_bool(key, pvals[key])
    for key, minimum in _DISPATCH_MINIMUMS.items():
        if key in pvals:
            pvals[key] = _bounded_dispatch_int(key, pvals[key], minimum)
    rknown = {f.name for f in fields(Reviewer)}
    reviewers = []
    for name in order:
        e = dict(rmap[name])
        bad = set(e) - rknown
        if bad:
            raise ValueError(f"reviewer {name!r}: unknown keys {sorted(bad)}")
        if "dimensions" in e:
            e["dimensions"] = tuple(e["dimensions"])
        if "fallbacks" in e:
            e["fallbacks"] = _fallback_tuple(name, e["fallbacks"])
        reviewers.append(_validate(Reviewer(**e)))
    reviewers = tuple(reviewers)
    _validate_fallbacks(reviewers)
    return Config(defaults=Defaults(**dvals), reviewers=reviewers,
                  dispatch=Dispatch(**pvals))
