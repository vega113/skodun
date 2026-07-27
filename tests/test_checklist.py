"""Tests for path-scoped checklist selection.

Two layers:

* Generic behavior — matching semantics, modes, budget, fail-soft — asserted
  with test-local tables. The committed code ships NO project's layout, so
  every table used here arrives as an argument.
* Oracle parity — `test_example_config_reproduces_oracle_*` load the committed
  `examples/scala-angular-monorepo.toml` through `load_config` and compare
  `select` against the real oracle script (`scripts/grok-checklist-select.py`,
  driven as a subprocess through its documented env interface) over the same
  fixtures. They SKIP, never silently pass, when `$SKODUN_ORACLE_DIR` is unset.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skodun.checklist import BUDGET, DROP_ORDER, PRIORITY, Selection, select
from skodun.config import load_config

from tests.conftest import oracle_dir

# Test-local layout tables — the code under test ships NO project's layout.
MAP = (("app/backend/", "backend"), ("app/web/", "frontend"), ("tools/", "tooling"))
TESTS = ("*.spec.ts", "src/test/")

SECTION_NAMES = ("core", "backend", "frontend", "tests", "migrations",
                 "tooling", "cross-file")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "scala-angular-monorepo.toml"
ORACLE_SCRIPT = "scripts/grok-checklist-select.py"


def _fixtures(tmp_path: Path, *, root: str = ".",
              cross_paths: tuple[str, ...] = ("app/backend/**",)) -> tuple[Path, Path]:
    """Write a checklist dir + a code-rules.json next to it.

    The oracle derives the rules path as `<checklist dir>/../code-rules.json`,
    so this layout lets both implementations read the very same files.
    """
    base = tmp_path if root == "." else tmp_path / root
    base.mkdir(parents=True, exist_ok=True)
    cdir = base / "checklists"
    cdir.mkdir()
    for name in SECTION_NAMES:
        (cdir / f"{name}.md").write_text(f"## {name}\n- rule for {name}\n",
                                         encoding="utf-8")
    rules = base / "code-rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [
        {"id": "x-callers", "crossFile": True, "paths": list(cross_paths),
         "doForm": "d", "flagForm": "f", "rationale": "docs/x.md",
         "layer": "guideline+checklist"}]}), encoding="utf-8")
    return cdir, rules


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------

def test_selection_by_prefix_and_crossfile(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/backend/App.scala", "app/web/thing.ts"], "full", cdir, rules,
                 checklist_map=MAP, test_path_patterns=TESTS)
    assert set(sel.sections) == {"core", "backend", "frontend", "cross-file"}
    assert "rule for backend" in sel.body
    assert sel.note == ""
    assert sel.bytes_total > 0 and sel.dropped == [] and sel.over_budget is False


def test_sections_are_emitted_in_priority_order(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["tools/a.sh", "app/web/b.ts", "app/backend/C.scala"], "full",
                 cdir, rules, checklist_map=MAP)
    assert sel.sections == ["core", "cross-file", "backend", "frontend", "tooling"]


def test_empty_map_selects_core_only_and_does_not_crash(tmp_path):
    # Default (unconfigured) behavior is generic, not an error.
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/backend/App.scala", "app/web/x.spec.ts", "tools/a.sh"],
                 "batch", cdir, rules)
    assert sel.sections == ["core"] and sel.note == ""
    # `full` adds only the rules-driven cross-file section, still no layout sections.
    sel_full = select(["app/backend/App.scala"], "full", cdir, rules)
    assert sel_full.sections == ["core", "cross-file"] and sel_full.note == ""
    # A path matching no crossFile glob leaves core alone.
    sel_plain = select(["docs/notes.md"], "full", cdir, rules)
    assert sel_plain.sections == ["core"]


def test_first_match_wins_in_map_order(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    ordered = (("app/backend/db/", "migrations"), ("app/backend/", "backend"))
    sel = select(["app/backend/db/V1.sql"], "full", cdir, rules,
                 checklist_map=ordered)
    assert "migrations" in sel.sections and "backend" not in sel.sections


def test_test_path_patterns_select_tests_section(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/web/x.spec.ts"], "full", cdir, rules,
                 checklist_map=MAP, test_path_patterns=TESTS)
    assert "tests" in sel.sections
    # ...and a path matching neither form does not.
    plain = select(["app/web/x.ts"], "full", cdir, rules,
                   checklist_map=MAP, test_path_patterns=TESTS)
    assert "tests" not in plain.sections


def test_test_pattern_forms_glob_prefix_and_substring(tmp_path):
    """Trailing `/` = path prefix; anything else = fnmatch over the whole path."""
    cdir, rules = _fixtures(tmp_path)
    pats = ("*.spec.ts", "*test-utils*.ts", "src/test/")

    def has_tests(p: str) -> bool:
        return "tests" in select([p], "batch", cdir, rules,
                                 test_path_patterns=pats).sections

    assert has_tests("ui/src/app/a.spec.ts")        # suffix glob, `*` crosses `/`
    assert has_tests("ui/test-utils/helper.ts")     # substring + suffix
    assert has_tests("src/test/scala/T.scala")      # path prefix
    assert not has_tests("ui/test-utils/helper.tsx")
    assert not has_tests("other/src/test/T.scala")  # prefix is anchored
    assert not has_tests("ui/src/app/a.ts")


def test_crossfile_uses_oracle_glob_semantics_not_pathlib_match(tmp_path):
    """`a/**` matches the directory itself and every descendant, at any depth."""
    cdir, rules = _fixtures(tmp_path, cross_paths=("app/backend/**",))

    def cross(p: str) -> bool:
        return "cross-file" in select([p], "full", cdir, rules).sections

    assert cross("app/backend")                    # the prefix itself
    assert cross("app/backend/deep/nested/A.scala")  # multi-segment tail
    assert not cross("app/backendish/A.scala")     # not a path-segment boundary
    assert not cross("app/web/a.ts")


def test_crossfile_requires_a_matching_path(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/web/only.ts"], "full", cdir, rules, checklist_map=MAP)
    assert "cross-file" not in sel.sections and "frontend" in sel.sections


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def test_batch_mode_never_includes_crossfile(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/backend/App.scala"], "batch", cdir, rules, checklist_map=MAP)
    assert "cross-file" not in sel.sections and "backend" in sel.sections


def test_integration_mode_is_core_plus_crossfile_only(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["app/web/thing.ts", "app/web/x.spec.ts"], "integration", cdir,
                 rules, checklist_map=MAP, test_path_patterns=TESTS)
    # cross-file is unconditional in integration mode, even with no glob match.
    assert sel.sections == ["core", "cross-file"]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def test_budget_drop_order_never_drops_core(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    (cdir / "tooling.md").write_text("x" * 20000, encoding="utf-8")  # blows budget
    sel = select(["tools/a.sh", "app/backend/A.scala"], "full", cdir, rules,
                 checklist_map=MAP)
    assert "tooling" in sel.dropped and "core" in sel.sections
    assert "tooling" not in sel.sections
    assert sel.bytes_total <= BUDGET and sel.over_budget is False


def test_budget_drops_lowest_priority_first(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    for name, size in (("backend", 17000), ("frontend", 5000), ("tooling", 5000)):
        (cdir / f"{name}.md").write_text("x" * size, encoding="utf-8")
    sel = select(["tools/a.sh", "app/web/b.ts", "app/backend/C.scala"], "batch",
                 cdir, rules, checklist_map=MAP)
    # 27000 bytes of sections: tooling then frontend go, and the *largest*
    # section survives because eviction follows priority, not size.
    assert sel.dropped == ["tooling", "frontend"]
    assert sel.sections == ["core", "backend"]
    assert sel.bytes_total <= BUDGET


def test_over_budget_when_core_alone_exceeds_it(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    (cdir / "core.md").write_text("x" * (BUDGET + 1), encoding="utf-8")
    sel = select(["docs/x.md"], "batch", cdir, rules, checklist_map=MAP)
    assert sel.sections == ["core"] and sel.over_budget is True and sel.dropped == []


def test_drop_order_is_priority_order_reversed_minus_core():
    assert DROP_ORDER == tuple(s for s in reversed(PRIORITY) if s != "core")
    assert "core" not in DROP_ORDER and PRIORITY[0] == "core"
    assert BUDGET == 18 * 1024 == 18432


def test_missing_section_file_is_skipped_not_fatal(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    (cdir / "frontend.md").unlink()
    (cdir / "tooling.md").unlink()   # two adjacent-in-priority holes
    sel = select(["app/web/a.ts", "tools/b.sh", "app/backend/C.scala"], "batch",
                 cdir, rules, checklist_map=MAP)
    assert sel.sections == ["core", "backend"] and sel.note == ""


# ---------------------------------------------------------------------------
# Fail-soft
# ---------------------------------------------------------------------------

def test_fail_soft_on_missing_dir(tmp_path):
    sel = select(["a"], "full", tmp_path / "nope", tmp_path / "nope.json")
    assert sel.sections == [] and sel.body == "" and "failed" in sel.note
    assert sel.bytes_total == 0 and sel.dropped == [] and sel.over_budget is False


def test_malformed_rules_json_fails_soft(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    rules.write_text("{ this is not json", encoding="utf-8")
    sel = select(["app/backend/App.scala"], "full", cdir, rules, checklist_map=MAP)
    # Selection still happens; only the rules-driven section is unavailable.
    assert sel.sections == ["core", "backend"]
    assert "cross-file" not in sel.sections
    assert "cross-file" in sel.note and "code-rules.json" in sel.note


def test_missing_rules_json_notes_and_skips_crossfile(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    rules.unlink()
    sel = select(["app/backend/App.scala"], "full", cdir, rules, checklist_map=MAP)
    assert sel.sections == ["core", "backend"] and "cross-file" in sel.note


def test_selection_is_a_dataclass_with_the_documented_fields():
    sel = Selection(["core"], 3, False, [], "x")
    assert (sel.sections, sel.bytes_total, sel.over_budget, sel.dropped,
            sel.body, sel.note) == (["core"], 3, False, [], "x", "")


# ---------------------------------------------------------------------------
# Example config: ordering hazard (runs without the oracle)
# ---------------------------------------------------------------------------

def _example_defaults(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".skodun.toml").write_text(EXAMPLE.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return load_config(repo, global_path=tmp_path / "absent.toml").defaults


def test_example_config_orders_longest_prefix_first(tmp_path):
    """`src/main/resources/db/changelog/...` matches TWO prefixes; the more
    specific one must win, which first-match-wins gives only if the example
    file is ordered longest-prefix-first."""
    d = _example_defaults(tmp_path)
    cdir, rules = _fixtures(tmp_path, root="fx", cross_paths=("src/main/**",))
    sel = select(["src/main/resources/db/changelog/001-init.xml"], "batch",
                 cdir, rules, checklist_map=d.checklist_map,
                 test_path_patterns=d.test_path_patterns)
    assert sel.sections == ["core", "migrations"]
    assert "backend" not in sel.sections
    # ...and the shorter prefix still works on its own.
    sel2 = select(["src/main/scala/App.scala"], "batch", cdir, rules,
                  checklist_map=d.checklist_map)
    assert sel2.sections == ["core", "backend"]


def test_example_config_tables_are_well_formed(tmp_path):
    d = _example_defaults(tmp_path)
    sections = {sec for _, sec in d.checklist_map}
    assert sections <= set(PRIORITY) and sections
    assert d.test_path_patterns
    # Longest-prefix-first: no entry may be an extension of an earlier one,
    # or first-match-wins would shadow the more specific rule.
    prefixes = [p for p, _ in d.checklist_map]
    for i, earlier in enumerate(prefixes):
        for later in prefixes[i + 1:]:
            assert not later.startswith(earlier), (
                f"{later!r} is more specific than {earlier!r} but is listed after it")


# ---------------------------------------------------------------------------
# Oracle parity
# ---------------------------------------------------------------------------

# (paths, mode) — each case exercises a distinct branch of the oracle.
ORACLE_CASES: tuple[tuple[list[str], str], ...] = (
    ([], "full"),
    (["docs/readme.md"], "full"),
    (["src/main/resources/db/changelog/001-init.xml"], "full"),   # two-prefix
    (["src/main/scala/App.scala"], "full"),
    (["ui/src/app/thing.ts"], "full"),
    (["scripts/build.sh", ".github/workflows/ci.yml"], "full"),
    (["ui/src/app/thing.spec.ts"], "full"),
    (["ui/src/app/testing/helper.ts"], "full"),
    (["ui/shared/test-utils/mk.ts"], "full"),
    (["integration-tests/src/test/scala/It.scala"], "full"),
    (["src/test/scala/T.scala"], "full"),
    (["scripts/smoke.test.sh", "tools/x.test.mjs"], "full"),
    # Everything at once.
    (["src/main/scala/App.scala", "src/main/resources/db/changelog/002.xml",
      "ui/src/app/thing.ts", "ui/src/app/thing.spec.ts", "scripts/build.sh",
      ".github/workflows/ci.yml", "src/test/scala/T.scala",
      "docs/notes.md"], "full"),
    (["src/main/scala/App.scala", "ui/src/app/thing.ts"], "batch"),
    (["src/main/scala/App.scala", "ui/src/app/thing.ts"], "integration"),
    (["ui/src/app/thing.ts"], "integration"),
    (["docs/readme.md"], "batch"),
)


def _run_oracle(oracle: Path, cdir: Path, paths: list[str], mode: str,
                tmp: Path) -> tuple[list[str], int, bool, list[str], str]:
    """Drive `scripts/grok-checklist-select.py` through its env interface."""
    script = oracle / ORACLE_SCRIPT
    assert script.is_file(), f"oracle script not found: {script}"
    lst = tmp / "files.txt"
    lst.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    env = {**os.environ, "GR_CHECKLIST_MODE": mode,
           "GR_CHECKLIST_DIR": str(cdir), "GR_FILE_LIST": str(lst)}
    out = subprocess.run([sys.executable, str(script)], env=env, check=True,
                         capture_output=True, text=True).stdout
    head, marker, body = out.partition("----- BEGIN CHECKLIST META END -----\n")
    assert marker, f"oracle emitted no meta marker: {out[:200]!r}"
    lines = head.split("\n")
    return ([s for s in lines[0].split(",") if s], int(lines[1]),
            lines[2] == "1", [s for s in lines[3].split(",") if s], body)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_example_config_reproduces_oracle_selection(tmp_path):
    # Oracle parity end-to-end: tables come from the committed example config,
    # never from a default in committed code.
    d = _example_defaults(tmp_path)
    cdir, rules = _fixtures(tmp_path, root="fx", cross_paths=("src/main/**",))
    seen: set[frozenset[str]] = set()
    for paths, mode in ORACLE_CASES:
        want = _run_oracle(oracle_dir(), cdir, paths, mode, tmp_path)
        sel = select(paths, mode, cdir, rules,
                     checklist_map=d.checklist_map,
                     test_path_patterns=d.test_path_patterns)
        got = (sel.sections, sel.bytes_total, sel.over_budget, sel.dropped, sel.body)
        assert got == want, f"parity drift for {mode} {paths}"
        seen.add(frozenset(sel.sections))
    # Guard against a vacuous pass: the cases must actually exercise every
    # section, including the two-prefix `migrations` win.
    assert set().union(*seen) == set(PRIORITY)
    assert {"core", "migrations", "cross-file"} in seen
    assert {"core"} in seen


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_example_config_reproduces_oracle_budget_drops(tmp_path):
    d = _example_defaults(tmp_path)
    cdir, rules = _fixtures(tmp_path, root="budget", cross_paths=("src/main/**",))
    for name in ("tooling", "frontend", "backend"):
        (cdir / f"{name}.md").write_text("x" * 9000, encoding="utf-8")
    paths = ["scripts/b.sh", "ui/src/app/a.ts", "src/main/scala/A.scala"]
    want = _run_oracle(oracle_dir(), cdir, paths, "full", tmp_path)
    sel = select(paths, "full", cdir, rules, checklist_map=d.checklist_map,
                 test_path_patterns=d.test_path_patterns)
    assert (sel.sections, sel.bytes_total, sel.over_budget, sel.dropped,
            sel.body) == want
    assert sel.dropped, "fixture must actually blow the budget"
    assert "core" in sel.sections
