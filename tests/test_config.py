from dataclasses import fields
from pathlib import Path

import pytest

from skodun.config import (
    _DEFAULTS_MINIMUMS, SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOT_NAMES,
    SECURITY_PROMPT_SLOTS, Defaults, load_config,
)


def _write(p: Path, s: str) -> Path:
    p.write_text(s, encoding="utf-8"); return p

def test_project_overrides_global_and_merges_reviewers_by_name(tmp_path):
    g = _write(tmp_path / "global.toml", """
[defaults]
timeout_sec = 420
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[defaults]
timeout_sec = 240
[[reviewers]]
name = "finder"
effort = "high"
""")
    cfg = load_config(repo, global_path=g)
    assert cfg.defaults.timeout_sec == 240
    assert cfg.defaults.max_turns == 40          # untouched default survives
    f = cfg.reviewer("finder")
    assert f.model == "grok-4.20-0309-reasoning"  # inherited from global entry
    assert f.effort == "high"                     # overridden by project entry

def test_unknown_effort_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
provider = "xai"
model = "m"
role = "finder"
effort = "turbo"
""")
    with pytest.raises(ValueError, match="effort"):
        load_config(None, global_path=g)


def test_skodun_config_env_var_honored_when_global_path_is_none(tmp_path, monkeypatch):
    g = _write(tmp_path / "env-global.toml", """
[defaults]
timeout_sec = 111
""")
    monkeypatch.setenv("SKODUN_CONFIG", str(g))
    cfg = load_config(None)
    assert cfg.defaults.timeout_sec == 111


def test_missing_global_config_file_degrades_to_defaults(tmp_path):
    missing = tmp_path / "does-not-exist.toml"
    cfg = load_config(None, global_path=missing)
    assert cfg.defaults == Defaults()
    assert cfg.reviewers == ()


def test_unknown_defaults_key_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
bogus_key = 1
""")
    with pytest.raises(ValueError, match=r"unknown \[defaults\] keys.*bogus_key"):
        load_config(None, global_path=g)


def test_unknown_reviewer_key_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
provider = "xai"
model = "m"
role = "finder"
bogus_key = 1
""")
    with pytest.raises(ValueError, match=r"unknown keys.*bogus_key"):
        load_config(None, global_path=g)


def test_unknown_role_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
provider = "xai"
model = "m"
role = "wizard"
""")
    with pytest.raises(ValueError, match="unknown role"):
        load_config(None, global_path=g)


def test_reviewer_missing_provider_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
model = "m"
role = "finder"
""")
    with pytest.raises(ValueError, match="provider and model are required"):
        load_config(None, global_path=g)


def test_reviewer_missing_model_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
provider = "xai"
role = "finder"
""")
    with pytest.raises(ValueError, match="provider and model are required"):
        load_config(None, global_path=g)


def test_reviewer_missing_name_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
provider = "xai"
model = "m"
role = "finder"
""")
    with pytest.raises(ValueError, match="missing its required 'name'"):
        load_config(None, global_path=g)


# --- numeric [defaults] keys: range and type ------------------------------
#
# These land in arithmetic elsewhere (`max_diff_bytes` slices the diff,
# `timeout_sec` bounds a subprocess). Loading is where a bad one must die: this
# module knows the offending key's *name*, and the code downstream only ever
# sees a number that has already lost its provenance.


def test_every_integer_defaults_field_has_a_declared_minimum():
    """A new numeric key must not arrive unguarded — this is the whole point of
    giving the validation an owner."""
    numeric = {f.name for f in fields(Defaults) if f.type in ("int", int)}
    assert numeric, "expected Defaults to have integer fields"
    assert numeric == set(_DEFAULTS_MINIMUMS)


@pytest.mark.parametrize("key", sorted(_DEFAULTS_MINIMUMS))
def test_numeric_default_accepts_its_minimum(tmp_path, key):
    g = _write(tmp_path / "g.toml",
               f"[defaults]\n{key} = {_DEFAULTS_MINIMUMS[key]}\n")
    assert getattr(load_config(None, global_path=g).defaults, key) \
        == _DEFAULTS_MINIMUMS[key]


@pytest.mark.parametrize("key", sorted(_DEFAULTS_MINIMUMS))
def test_numeric_default_rejects_below_minimum(tmp_path, key):
    below = _DEFAULTS_MINIMUMS[key] - 1
    g = _write(tmp_path / "g.toml", f"[defaults]\n{key} = {below}\n")
    with pytest.raises(ValueError, match=rf"\[defaults\] {key}: must be >= "
                                         rf"{_DEFAULTS_MINIMUMS[key]}, got {below}"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("key", sorted(_DEFAULTS_MINIMUMS))
def test_numeric_default_rejects_a_string(tmp_path, key):
    g = _write(tmp_path / "g.toml", f'[defaults]\n{key} = "big"\n')
    with pytest.raises(
            ValueError,
            match=rf"\[defaults\] {key}: expected an integer, got str"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("key", sorted(_DEFAULTS_MINIMUMS))
def test_numeric_default_rejects_a_bool(tmp_path, key):
    """`bool` is an `int` subclass; `true` would otherwise load as 1."""
    g = _write(tmp_path / "g.toml", f"[defaults]\n{key} = true\n")
    with pytest.raises(
            ValueError,
            match=rf"\[defaults\] {key}: expected an integer, got bool"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("key", sorted(_DEFAULTS_MINIMUMS))
def test_numeric_default_rejects_a_float(tmp_path, key):
    g = _write(tmp_path / "g.toml", f"[defaults]\n{key} = 1.5\n")
    with pytest.raises(
            ValueError,
            match=rf"\[defaults\] {key}: expected an integer, got float"):
        load_config(None, global_path=g)


def test_zero_max_diff_bytes_is_rejected_at_load_not_at_prompt_build(tmp_path):
    """The specific hole this closes: a zero budget used to reach
    `promptbuild.build` and raise there, with no mention of the config key that
    caused it. Nothing downstream should ever see it."""
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", "[defaults]\nmax_diff_bytes = 0\n")
    with pytest.raises(ValueError, match=r"\[defaults\] max_diff_bytes"):
        load_config(repo, global_path=tmp_path / "absent.toml")


def test_bad_numeric_in_project_layer_is_rejected_even_if_global_was_fine(tmp_path):
    g = _write(tmp_path / "g.toml", "[defaults]\nmax_diff_bytes = 400000\n")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", "[defaults]\nmax_diff_bytes = -1\n")
    with pytest.raises(ValueError, match=r"\[defaults\] max_diff_bytes: must be >= 1"):
        load_config(repo, global_path=g)


def test_a_good_project_value_overrides_a_bad_global_one(tmp_path):
    """Validation runs on the merged result, not per layer: the project layer
    wins, so a stale global value it replaces must not veto the load."""
    g = _write(tmp_path / "g.toml", "[defaults]\ntimeout_sec = 0\n")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", "[defaults]\ntimeout_sec = 300\n")
    assert load_config(repo, global_path=g).defaults.timeout_sec == 300


# --- repo-layout tables: defaults, round-trip, layering, validation ---------

def test_repo_layout_tables_default_to_generic_behavior():
    d = Defaults()
    assert d.checklist_map == ()              # only `core` gets selected
    assert d.test_path_patterns == ()
    assert d.security_basename_patterns == ()
    assert d.security_path_segments == (
        "auth", "secret", "credential", "token", "webhook", "payment", "billing")
    assert d.security_path_segments == SECURITY_PATH_SEGMENTS


def test_defaults_stay_hashable_with_configured_tables(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
checklist_map = [["a/", "backend"]]
test_path_patterns = ["*.spec.ts"]
""")
    cfg = load_config(None, global_path=g)
    hash(cfg.defaults)                        # frozen + hashable: no list fields
    assert isinstance(cfg.defaults.checklist_map[0], tuple)


def test_repo_layout_tables_round_trip_from_global_layer(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
checklist_map = [
  ["db/changelog/", "migrations"],
  ["backend/", "backend"],
  ["web/", "frontend"],
]
test_path_patterns = ["*.spec.ts", "src/test/"]
security_path_segments = ["auth", "vault"]
security_basename_patterns = ["*RouteService*"]
""")
    d = load_config(None, global_path=g).defaults
    assert d.checklist_map == (
        ("db/changelog/", "migrations"), ("backend/", "backend"), ("web/", "frontend"))
    assert d.test_path_patterns == ("*.spec.ts", "src/test/")
    assert d.security_path_segments == ("auth", "vault")
    assert d.security_basename_patterns == ("*RouteService*",)


def test_repo_layout_tables_round_trip_from_project_layer(tmp_path):
    g = _write(tmp_path / "g.toml", "[defaults]\ntimeout_sec = 420\n")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[defaults]
checklist_map = [["svc/", "backend"]]
test_path_patterns = ["*_test.py"]
security_path_segments = ["crypto"]
security_basename_patterns = ["*Handler*"]
""")
    d = load_config(repo, global_path=g).defaults
    assert d.checklist_map == (("svc/", "backend"),)
    assert d.test_path_patterns == ("*_test.py",)
    assert d.security_path_segments == ("crypto",)
    assert d.security_basename_patterns == ("*Handler*",)


def test_project_layer_overrides_global_repo_layout_tables(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
checklist_map = [["global/", "backend"]]
test_path_patterns = ["global.spec.ts"]
security_path_segments = ["global-seg"]
security_basename_patterns = ["global*"]
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[defaults]
checklist_map = [["project/", "frontend"]]
test_path_patterns = ["project.spec.ts"]
security_path_segments = ["project-seg"]
security_basename_patterns = ["project*"]
""")
    d = load_config(repo, global_path=g).defaults
    assert d.checklist_map == (("project/", "frontend"),)
    assert d.test_path_patterns == ("project.spec.ts",)
    assert d.security_path_segments == ("project-seg",)
    assert d.security_basename_patterns == ("project*",)


def test_empty_repo_layout_tables_are_accepted(tmp_path):
    # An explicitly empty table is well-formed — it means "match nothing".
    g = _write(tmp_path / "g.toml", """
[defaults]
checklist_map = []
security_path_segments = []
""")
    d = load_config(None, global_path=g).defaults
    assert d.checklist_map == () and d.security_path_segments == ()


@pytest.mark.parametrize("key", [
    "test_path_patterns", "security_path_segments", "security_basename_patterns"])
def test_string_table_rejects_scalar(tmp_path, key):
    g = _write(tmp_path / "g.toml", f'[defaults]\n{key} = "auth"\n')
    with pytest.raises(ValueError,
                       match=rf"\[defaults\] {key}: expected an array of strings, got str"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("key", [
    "test_path_patterns", "security_path_segments", "security_basename_patterns"])
def test_string_table_rejects_non_string_entry(tmp_path, key):
    g = _write(tmp_path / "g.toml", f'[defaults]\n{key} = ["ok", 7]\n')
    with pytest.raises(ValueError,
                       match=rf"\[defaults\] {key}: entry 1 must be a string, got int"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("key", [
    "test_path_patterns", "security_path_segments", "security_basename_patterns"])
def test_string_table_rejects_blank_entry(tmp_path, key):
    g = _write(tmp_path / "g.toml", f'[defaults]\n{key} = ["ok", "  "]\n')
    with pytest.raises(ValueError,
                       match=rf"\[defaults\] {key}: entry 1 must not be empty"):
        load_config(None, global_path=g)


def test_checklist_map_rejects_scalar(tmp_path):
    g = _write(tmp_path / "g.toml", '[defaults]\nchecklist_map = "src/"\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] checklist_map: expected an array of 2-element arrays, got str"):
        load_config(None, global_path=g)


def test_checklist_map_rejects_flat_string_list(tmp_path):
    g = _write(tmp_path / "g.toml", '[defaults]\nchecklist_map = ["src/", "backend"]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] checklist_map: entry 0 must be a 2-element array, got str"):
        load_config(None, global_path=g)


def test_checklist_map_rejects_wrong_arity(tmp_path):
    g = _write(tmp_path / "g.toml",
               '[defaults]\nchecklist_map = [["a/", "backend"], ["b/"]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] checklist_map: entry 1 must be a 2-element array, got 1 elements"):
        load_config(None, global_path=g)


def test_checklist_map_rejects_non_string_member(tmp_path):
    g = _write(tmp_path / "g.toml", '[defaults]\nchecklist_map = [["a/", 3]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] checklist_map: entry 0 must contain strings, got int"):
        load_config(None, global_path=g)


def test_checklist_map_rejects_blank_member(tmp_path):
    g = _write(tmp_path / "g.toml", '[defaults]\nchecklist_map = [["a/", ""]]\n')
    with pytest.raises(
            ValueError, match=r"\[defaults\] checklist_map: entry 0 must not be empty"):
        load_config(None, global_path=g)


# --- security_prompt_slots: the security prompt's variable spans ------------
# Same posture as the layout tables: malformed is loud here, at load time, and
# naming the key; a well-formed table is consumed fail-soft by `passes.py`.

def test_security_prompt_slots_default_to_the_generic_set():
    d = Defaults()
    assert d.security_prompt_slots == SECURITY_PROMPT_SLOTS
    assert {name for name, _ in d.security_prompt_slots} == set(
        SECURITY_PROMPT_SLOT_NAMES) == {"surfaces", "extra_checks"}
    # The shipped prompt describes concerns, never one project's systems.
    blob = " ".join(f for _, f in d.security_prompt_slots).lower()
    for noun in ("telegram", "credits", "dao", "routeservice"):
        assert noun not in blob, noun


def test_security_prompt_slots_round_trip_and_stay_hashable(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
security_prompt_slots = [
  ["surfaces", "widgets,\\nsprockets, or gears"],
  ["extra_checks", "- sprocket integrity (over-torque)"],
]
""")
    d = load_config(None, global_path=g).defaults
    assert d.security_prompt_slots == (
        ("surfaces", "widgets,\nsprockets, or gears"),
        ("extra_checks", "- sprocket integrity (over-torque)"))
    hash(d)


def test_security_prompt_slots_accept_a_partial_table(tmp_path):
    # One slot filled is well-formed; `passes.py` keeps the generic default for
    # the other. Only unknown NAMES are an error.
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces", "widgets"]]\n')
    assert load_config(None, global_path=g).defaults.security_prompt_slots == (
        ("surfaces", "widgets"),)


def test_security_prompt_slots_project_layer_wins(tmp_path):
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces", "global"]]\n')
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml",
           '[defaults]\nsecurity_prompt_slots = [["surfaces", "project"]]\n')
    assert load_config(repo, global_path=g).defaults.security_prompt_slots == (
        ("surfaces", "project"),)


def test_security_prompt_slots_reject_an_unknown_slot_name(tmp_path):
    # A slot filled under a misspelled name would vanish silently and ship the
    # generic prompt — exactly the failure this key exists to prevent.
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces", "w"], '
               '["surfacez", "typo"]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] security_prompt_slots: entry 1 names unknown "
                  r"slot 'surfacez'"):
        load_config(None, global_path=g)


def test_security_prompt_slots_reject_a_repeated_slot_name(tmp_path):
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces", "a"], '
               '["surfaces", "b"]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] security_prompt_slots: entry 1 repeats slot 'surfaces'"):
        load_config(None, global_path=g)


def test_security_prompt_slots_reject_a_scalar(tmp_path):
    g = _write(tmp_path / "g.toml", '[defaults]\nsecurity_prompt_slots = "widgets"\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] security_prompt_slots: expected an array of "
                  r"2-element arrays, got str"):
        load_config(None, global_path=g)


def test_security_prompt_slots_reject_wrong_arity(tmp_path):
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces"]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] security_prompt_slots: entry 0 must be a "
                  r"2-element array, got 1 elements"):
        load_config(None, global_path=g)


def test_security_prompt_slots_reject_a_blank_fragment(tmp_path):
    g = _write(tmp_path / "g.toml",
               '[defaults]\nsecurity_prompt_slots = [["surfaces", "  "]]\n')
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] security_prompt_slots: entry 0 must not be empty"):
        load_config(None, global_path=g)
