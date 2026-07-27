"""Path-scoped review-checklist selection, budgeted to a byte envelope.

Given the repo-relative paths a change touches, pick which `<section>.md` files
under the checklist directory to inject into the review prompt, drop the
lowest-value ones when the total exceeds the budget, and never abort a review
because selection went wrong.

PARITY-CRITICAL: vendored from the oracle's `scripts/grok-checklist-select.py`.
Where this module and the oracle disagree, the oracle wins; parity is pinned
end-to-end by `tests/test_checklist.py`, which drives the real oracle script as
a subprocess over the same fixtures. Deliberate divergences are marked
`DIVERGENCE` below and each one is covered by a test.

Repo layout is CONFIGURATION, not code
--------------------------------------
The oracle hardcoded one monorepo's directory tree. Here the two layout tables
arrive as arguments (`Defaults.checklist_map` / `Defaults.test_path_patterns`),
both defaulting to empty — with no config, only `core` (plus the rules-driven
`cross-file`) is ever selected, for any file list. A worked example carrying
the oracle's own tables lives in `examples/scala-angular-monorepo.toml`.

Matching semantics (this module owns them)
------------------------------------------
`checklist_map` — an ordered sequence of `(path_prefix, section)` pairs. A path
matches an entry when `path.startswith(path_prefix)`, and the **first** match in
sequence order wins; a path contributes at most one section this way. The
oracle instead used an if-chain hand-ordered longest-prefix-first, which is the
same outcome exactly when the configured table is ordered longest-prefix-first
(a nested `<backend>/<db>/<changelog>/` entry before the `<backend>/` entry that
contains it). Order your table that way; `test_example_config_orders_longest_prefix_first`
pins it for the shipped example, whose own table is ordered accordingly.

`test_path_patterns` — a match selects the `tests` section, independently of
and in addition to `checklist_map`. Each pattern is read one of two ways:

* ends with `/` → an anchored path-prefix test (`path.startswith(pattern)`),
  for whole test trees (e.g. `spec/`);
* otherwise → an `fnmatch.fnmatchcase` glob over the **whole** path, in which
  `*` crosses `/`. That single form expresses both of the oracle's remaining
  shapes: `*.golden` is "ends with .golden", and `*fixtures*.py` is
  "contains fixtures AND ends with .py".

`crossFile` rule globs read from `code-rules.json` use neither of the above:
they keep the oracle's own minimal matcher (`_glob_match`) — `*` alone matches
everything, a trailing `/**` matches the directory itself and every descendant
at any depth, any other glob goes to `fnmatch.fnmatch`, and a plain string
matches the path itself or anything under it. Notably this is *not*
`pathlib.PurePath.match` semantics (`a/**` there would not match `a` itself).

Modes
-----
`full` — everything eligible. `batch` — per-batch pass, never cross-file (any
unrecognized mode string behaves this way, as in the oracle). `integration` —
`core` + `cross-file` only, with cross-file included unconditionally.

Fail-soft
---------
Any exception yields an empty `Selection` carrying a `note`; the caller drops
path-scoped rules and reviews anyway. An empty or non-matching layout table is
NOT an error — it silently yields `core` only. A missing or malformed
`code-rules.json` is a partial degradation: selection proceeds, `cross-file` is
simply unavailable, and the reason lands in `note`.

`note` alone conflates those two severities, so `Selection.degraded`
disambiguates: total failure leaves `sections` empty and `degraded` False;
partial degradation leaves `sections` non-empty (everything else that could be
selected) and `degraded` True. When `note` is empty, `degraded` is always
False.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Injection budget for all checklist sections combined, in bytes.
BUDGET = 18 * 1024  # 18432

#: Section emission order, most valuable first. From the oracle's `priority`.
PRIORITY: tuple[str, ...] = (
    "core", "cross-file", "migrations", "backend", "tests", "frontend", "tooling",
)

#: Budget eviction order, least valuable first. `core` is never a candidate.
DROP_ORDER: tuple[str, ...] = (
    "tooling", "frontend", "tests", "backend", "migrations", "cross-file",
)


@dataclass(frozen=True)
class Selection:
    """What to inject, what it costs, and what had to go.

    `sections` and `dropped` are tuples, not lists: `select` builds them as
    lists while iterating and mutating (dropping entries under budget), then
    freezes the result here, so `frozen=True` is not just skin deep.

    `dropped` and `over_budget` are the budget's half of that story, and they
    are read by `pipeline.run_review`, which reports both to the operator on
    stderr. Without them, eviction is invisible: the review runs with fewer
    rules than the config asked for and nothing says so.

    `note` and `degraded` together describe why a selection is imperfect,
    disambiguating two severities that would otherwise share one string:

    * total failure — selection raised before choosing anything. `sections`
      is empty, `degraded` is False, and `note` explains what failed.
    * partial degradation — selection succeeded but something it depends on
      (currently: an unreadable `code-rules.json`) was unavailable. `sections`
      still carries everything that WAS selected, `degraded` is True, and
      `note` explains what's missing.

    When `note` is empty, `degraded` is always False.
    """

    sections: tuple[str, ...]
    bytes_total: int
    over_budget: bool
    dropped: tuple[str, ...] = ()
    body: str = ""
    note: str = ""
    degraded: bool = False

    def __post_init__(self) -> None:
        # Accept list arguments (from `select`'s internal working lists, or
        # from callers/tests passing list literals) and coerce them to tuples
        # so every `Selection` in the wild is actually immutable, regardless
        # of what was handed to the constructor.
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "dropped", tuple(self.dropped))


def _section_for(path: str, checklist_map: Sequence[tuple[str, str]]) -> str | None:
    """First `(prefix, section)` entry whose prefix starts `path`, else None."""
    for prefix, section in checklist_map:
        if path.startswith(prefix):
            return section
    return None


def _is_test_path(path: str, patterns: Sequence[str]) -> bool:
    """True when `path` matches any test pattern (see module docstring)."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _glob_match(path: str, glob: str) -> bool:
    """The oracle's minimal `**`/`*` matcher for code-rules globs, verbatim."""
    if glob == "*":
        return True
    if glob.endswith("/**"):
        prefix = glob[:-3]
        return (path == prefix.rstrip("/")
                or path.startswith(prefix if prefix.endswith("/") else prefix + "/"))
    if "*" in glob:
        return fnmatch.fnmatch(path, glob)
    return path == glob or path.startswith(glob.rstrip("/") + "/")


def _cross_file_globs(rules_json: Path) -> tuple[list[str], str]:
    """Globs of every `crossFile` rule, plus a note when they can't be read.

    DIVERGENCE: when the registry is missing or unparseable the oracle falls
    back to a hardcoded glob over its own backend tree. That literal is one
    repo's layout, so it cannot live in committed code; skodun degrades to "no
    cross-file globs" and says so in the note instead of guessing at the
    caller's directory tree. Parity is unaffected whenever the registry is
    readable, which is the only case the oracle's own wrapper reaches in
    practice.
    """
    if not rules_json.is_file():
        return [], (f"{rules_json.name} not found; "
                    f"continuing without cross-file rules")
    try:
        registry = json.loads(rules_json.read_text(encoding="utf-8"))
        globs: list[str] = []
        for rule in registry.get("rules", []):
            if rule.get("crossFile"):
                globs.extend(rule.get("paths") or [])
    except Exception as e:  # malformed JSON, unexpected shape, unreadable file
        return [], (f"{rules_json.name} unreadable ({e}); "
                    f"continuing without cross-file rules")
    return globs, ""


def select(
    files: Sequence[str],
    mode: str,
    checklist_dir: Path,
    rules_json: Path,
    checklist_map: Sequence[tuple[str, str]] = (),
    test_path_patterns: Sequence[str] = (),
) -> Selection:
    """Select, budget, and render the checklist sections for a change.

    `files` are repo-relative paths; `mode` is `full` / `batch` / `integration`;
    `checklist_dir` holds `<section>.md` files; `rules_json` is the code-rules
    registry. `checklist_map` and `test_path_patterns` are the caller's layout
    tables (`cfg.defaults.*`), empty by default. Never raises.
    """
    try:
        checklist_dir = Path(checklist_dir)
        rules_json = Path(rules_json)
        # DIVERGENCE: the oracle is invoked only after its wrapper has already
        # checked the directory exists, and silently emits an empty selection
        # otherwise. Reporting it here turns a misconfigured path into a
        # visible fail-soft note rather than a silently rule-less review.
        #
        # Returned rather than raised into the handler below, and the wording
        # is the point: a repo with no `docs/review/checklists` is the DEFAULT
        # and by far the commonest case -- checklists are opt-in -- so every
        # run of an ordinary repo used to log `checklist selection failed`.
        # "Failed" describes a broken thing; this is an unconfigured one, and a
        # message that cries wolf on every single run is a message nobody reads
        # when something really does break. The classification is unchanged:
        # nothing was selected, so `sections` is empty and `degraded` is False
        # (see the `Selection` docstring). Only the sentence changed.
        if not checklist_dir.is_dir():
            return Selection(
                sections=[], bytes_total=0, over_budget=False, dropped=[],
                body="", degraded=False,
                note=f"no checklist directory at {checklist_dir} -- "
                     f"continuing with generic review rules")

        paths = [f.strip() for f in files if f and f.strip()]
        note = ""

        if mode == "integration":
            # Session-deferred integration pass: core + cross-file only.
            selected = {"core", "cross-file"}
        else:
            selected = {"core"}
            for p in paths:
                if _is_test_path(p, test_path_patterns):
                    selected.add("tests")
                section = _section_for(p, checklist_map)
                if section:
                    selected.add(section)

        # cross-file: single-shot/full consults the registry; the integration
        # pass already set cross-file unconditionally above and never touches
        # the registry, so it must not pick up a note about it; batch never
        # includes cross-file at all.
        if mode == "full":
            globs, note = _cross_file_globs(rules_json)
            wanted = any(_glob_match(p, g) for p in paths for g in globs)
            if wanted:
                selected.add("cross-file")
            else:
                selected.discard("cross-file")

        ordered = [s for s in PRIORITY if s in selected]
        bodies: dict[str, str] = {}
        # Iterate over a copy so removing an entry from `ordered` inside the
        # loop body can't skip the next one. The oracle's own loop rebinds
        # `ordered = [x for x in ordered if x != s]` instead of mutating it in
        # place, so its `for` loop keeps iterating the original list object
        # and still visits every entry; behavior is identical either way.
        for s in list(ordered):
            f = checklist_dir / f"{s}.md"
            if f.is_file():
                bodies[s] = f.read_text(encoding="utf-8")
            else:
                ordered.remove(s)

        def total() -> int:
            return sum(len(bodies[s].encode("utf-8")) for s in ordered)

        dropped: list[str] = []
        while ordered and total() > BUDGET:
            candidates = [s for s in DROP_ORDER if s in ordered]
            if not candidates:  # only `core` left; it is never dropped
                break
            ordered.remove(candidates[0])
            dropped.append(candidates[0])

        parts: list[str] = []
        for s in ordered:
            parts.append(f"\n### Checklist: {s}\n")
            parts.append(bodies[s])
            if not bodies[s].endswith("\n"):
                parts.append("\n")

        return Selection(
            sections=ordered,
            bytes_total=total(),
            over_budget=bool(ordered) and total() > BUDGET,
            dropped=dropped,
            body="".join(parts),
            note=note,
            # Selection succeeded (we're past every raise site above); a
            # non-empty note here can only mean the crossFile registry was
            # unavailable, i.e. partial degradation, not total failure.
            degraded=bool(note),
        )
    except Exception as e:
        return Selection(
            sections=[], bytes_total=0, over_budget=False, dropped=[], body="",
            note=f"checklist selection failed: {e}; "
                 f"continuing without path-scoped rules",
            # Total failure, not degradation: nothing was selected at all.
            degraded=False,
        )
