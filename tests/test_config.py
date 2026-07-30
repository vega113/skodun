from dataclasses import fields
from pathlib import Path

import pytest

from skodun.config import (
    _DEFAULTS_MINIMUMS, _DISPATCH_FLAGS, _DISPATCH_MINIMUMS,
    SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOT_NAMES, SECURITY_PROMPT_SLOTS,
    Defaults, Dispatch, Reviewer, load_config,
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
    assert [r.name for r in cfg.reviewers] == ["finder"]   # merged, not appended
    f = cfg.reviewers[0]
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


# --- severity_gate / confidence_threshold: removed in Phase 2 --------------
#
# Both keys were declared-but-inert in Phase 1 (see the Phase 1 plan's
# "Known intentional deviations"): they read like a severity/confidence
# filter on the gate but never filtered anything -- `gate.open_findings`
# blocks on ANY untriaged finding, by design. A key that looks like a filter
# but silently is not is a safety trap, so Phase 2 removes both rather than
# ever implement the filter. A config that still sets one must fail with a
# message that reads as a decision, not a typo -- and it must come from the
# dedicated removed-keys check, not the generic "unknown [defaults] keys"
# error, which is why the match below pins the "removed in Phase 2" phrase
# rather than just any ValueError.

def test_severity_gate_removed_key_rejected_from_global_layer(tmp_path):
    g = _write(tmp_path / "g.toml", "[defaults]\nseverity_gate = \"high\"\n")
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] severity_gate was removed in Phase 2: the "
                  r"gate blocks on any open finding by design .* delete the key"):
        load_config(None, global_path=g)


def test_severity_gate_removed_key_rejected_from_project_layer(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", "[defaults]\nseverity_gate = \"high\"\n")
    with pytest.raises(ValueError, match=r"severity_gate was removed in Phase 2"):
        load_config(repo, global_path=tmp_path / "absent.toml")


def test_confidence_threshold_removed_key_rejected_from_global_layer(tmp_path):
    g = _write(tmp_path / "g.toml", "[defaults]\nconfidence_threshold = 7\n")
    with pytest.raises(
            ValueError,
            match=r"\[defaults\] confidence_threshold was removed in Phase 2: "
                  r"the gate blocks on any open finding by design .* delete the key"):
        load_config(None, global_path=g)


def test_confidence_threshold_removed_key_rejected_from_project_layer(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", "[defaults]\nconfidence_threshold = 7\n")
    with pytest.raises(ValueError, match=r"confidence_threshold was removed in Phase 2"):
        load_config(repo, global_path=tmp_path / "absent.toml")


def test_removed_keys_no_longer_appear_on_defaults():
    known = {f.name for f in fields(Defaults)}
    assert "severity_gate" not in known
    assert "confidence_threshold" not in known


# --- Reviewer.fallbacks: the quota-fallback chain --------------------------
#
# This module only builds and validates the chain; Task 7 executes it. A
# member's own `fallbacks` are never followed at runtime -- only the head
# reviewer's list is used when its attempt classifies `unavailable` -- but
# validation still walks every chain transitively so a mutual (or longer)
# cycle is rejected at load time, not discovered mid-run.

def test_fallback_chain_validated(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["backup"]
[[reviewers]]
name = "backup"
provider = "openai"
model = "n"
""")
    cfg = load_config(None, global_path=p)
    assert cfg.reviewers[0].fallbacks == ("backup",)
    assert isinstance(cfg.reviewers[0].fallbacks, tuple)


def test_fallback_defaults_to_empty_tuple():
    assert Reviewer(name="x").fallbacks == ()


def test_fallback_target_can_be_defined_in_a_different_layer(tmp_path):
    """Validation runs on the MERGED reviewer set, not per layer."""
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["backup"]
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[[reviewers]]
name = "backup"
provider = "openai"
model = "n"
""")
    cfg = load_config(repo, global_path=g)
    assert cfg.reviewers[0].fallbacks == ("backup",)


def test_fallback_chain_of_exactly_three_is_legal(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["b1", "b2", "b3"]
[[reviewers]]
name = "b1"
provider = "openai"
model = "n"
[[reviewers]]
name = "b2"
provider = "openai"
model = "n"
[[reviewers]]
name = "b3"
provider = "openai"
model = "n"
""")
    cfg = load_config(None, global_path=p)
    assert cfg.reviewers[0].fallbacks == ("b1", "b2", "b3")


def test_fallback_chain_of_four_is_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["b1", "b2", "b3", "b4"]
[[reviewers]]
name = "b1"
provider = "openai"
model = "n"
[[reviewers]]
name = "b2"
provider = "openai"
model = "n"
[[reviewers]]
name = "b3"
provider = "openai"
model = "n"
[[reviewers]]
name = "b4"
provider = "openai"
model = "n"
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallback chain has 4 entries, at most 3"):
        load_config(None, global_path=p)


def test_fallback_unknown_name_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["ghost"]
""")
    with pytest.raises(
            ValueError, match=r"reviewer 'finder': fallback 'ghost' does not exist"):
        load_config(None, global_path=p)


def test_fallback_self_reference_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["finder"]
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': cannot be its own fallback"):
        load_config(None, global_path=p)


def test_fallback_disabled_target_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["backup"]
[[reviewers]]
name = "backup"
provider = "openai"
model = "n"
enabled = false
""")
    with pytest.raises(
            ValueError, match=r"reviewer 'finder': fallback 'backup' is disabled"):
        load_config(None, global_path=p)


def test_fallback_duplicate_in_chain_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = ["backup", "backup"]
[[reviewers]]
name = "backup"
provider = "openai"
model = "n"
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallback 'backup' listed more than once"):
        load_config(None, global_path=p)


def test_fallback_cycle_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
fallbacks = ["b"]
[[reviewers]]
name = "b"
provider = "openai"
model = "n"
fallbacks = ["a"]
""")
    with pytest.raises(ValueError, match="cycle"):
        load_config(None, global_path=p)


def test_fallback_transitive_cycle_rejected(tmp_path):
    """A mutual (2-node) cycle is the obvious case; validation must also catch
    a longer cycle reached only by walking a chain member's OWN fallbacks
    (never followed at runtime, but still walked for this check)."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
fallbacks = ["b"]
[[reviewers]]
name = "b"
provider = "openai"
model = "n"
fallbacks = ["c"]
[[reviewers]]
name = "c"
provider = "openai"
model = "n"
fallbacks = ["a"]
""")
    with pytest.raises(ValueError, match="reviewer 'a':.*cycle"):
        load_config(None, global_path=p)


def test_fallback_diamond_fan_in_is_not_a_cycle(tmp_path):
    """Two reviewers whose chains reach a common target by different routes
    is legitimate fan-in, not a cycle -- `_walk` tracks the PATH taken to
    reach a node, not the set of every node visited anywhere in the walk, so
    a shared descendant reached twice by different routes must not trip it."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
fallbacks = ["b", "c"]
[[reviewers]]
name = "b"
provider = "openai"
model = "n"
fallbacks = ["d"]
[[reviewers]]
name = "c"
provider = "openai"
model = "n"
fallbacks = ["d"]
[[reviewers]]
name = "d"
provider = "openai"
model = "n"
""")
    cfg = load_config(None, global_path=p)
    by_name = {r.name: r for r in cfg.reviewers}
    assert by_name["a"].fallbacks == ("b", "c")
    assert by_name["b"].fallbacks == ("d",)
    assert by_name["c"].fallbacks == ("d",)


# --- Reviewer.fallbacks: shape validation, before any semantic check -------
#
# Hostile TOML shapes must raise a clean ValueError naming the reviewer and
# the actual shape problem -- never an unhandled TypeError (an unhashable
# nested list/inline-table reaching the duplicate-check's `set`), and never a
# fabricated semantic diagnosis (a bare string silently iterated
# character-by-character into single-letter "reviewer names").

def test_fallback_bare_string_is_rejected_not_iterated_into_characters(tmp_path):
    """`fallbacks = "backup"` is a plausible typo for `["backup"]`. It must
    not silently iterate the string and report a fabricated missing
    reviewer named e.g. 'b'."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = "backup"
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallbacks must be an array of strings, "
                  r"got str"):
        load_config(None, global_path=p)


def test_fallback_nested_list_entry_is_rejected(tmp_path):
    """A nested array entry is unhashable and must not crash the
    duplicate-check's `set` with an unhandled TypeError."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = [["backup"]]
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallbacks entry 0 must be a string, "
                  r"got list"):
        load_config(None, global_path=p)


def test_fallback_inline_table_entry_is_rejected(tmp_path):
    """An inline table entry is unhashable, same failure mode as a nested
    list, and must be rejected the same clean way."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = [{name = "backup"}]
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallbacks entry 0 must be a string, "
                  r"got dict"):
        load_config(None, global_path=p)


def test_fallback_integer_entry_is_rejected(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = [5]
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallbacks entry 0 must be a string, "
                  r"got int"):
        load_config(None, global_path=p)


def test_fallback_boolean_entry_is_rejected(tmp_path):
    """`bool` is a subclass of `int` in Python, so a naive int-or-string
    check could wrongly admit it; the check here is a strict `isinstance
    str`, which already excludes `bool` without special-casing it."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = [true]
""")
    with pytest.raises(
            ValueError,
            match=r"reviewer 'finder': fallbacks entry 0 must be a string, "
                  r"got bool"):
        load_config(None, global_path=p)


def test_fallback_empty_array_still_loads(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
fallbacks = []
""")
    cfg = load_config(None, global_path=p)
    assert cfg.reviewers[0].fallbacks == ()


# --------------------------------------------------------------------------
# max_cost_usd — the budget cap, validated at load time
# --------------------------------------------------------------------------
#
# Phase 1 declared `Reviewer.max_cost_usd` and nothing ever read it. The claude
# adapter is its first consumer (it becomes `--max-budget-usd <v>`), so
# validation starts here too, and the rule is the CLI's own: probed live against
# Claude Code 2.1.118, `--max-budget-usd 0` / `-1` / `abc` / `nan` all die with
#
#     error: --max-budget-usd must be a positive number greater than 0
#
# thrown as an UNCAUGHT exception — rc 1, a Bun stack trace and a source dump on
# stderr, and completely EMPTY stdout. There is no result envelope to classify,
# so an adapter cannot tell that failure apart from a provider that produced
# nothing. TOML will happily hand us every one of those values (`true` even
# arrives as a `bool`, which is a subclass of `int`), so each is a loud
# `ValueError` naming the reviewer, at load time, before any subprocess exists.

def test_max_cost_usd_accepts_a_positive_number(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "sec"
provider = "anthropic"
model = "m"
max_cost_usd = 0.50
""")
    assert load_config(None, global_path=p).reviewers[0].max_cost_usd == 0.50


def test_max_cost_usd_accepts_a_positive_integer(tmp_path):
    """TOML `1` is an int, not a float, and an int budget is not a typo."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "sec"
provider = "anthropic"
model = "m"
max_cost_usd = 2
""")
    assert load_config(None, global_path=p).reviewers[0].max_cost_usd == 2


def test_max_cost_usd_is_optional(tmp_path):
    """Unset means "no cap", which is what every Phase 1 config already says."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "sec"
provider = "anthropic"
model = "m"
""")
    assert load_config(None, global_path=p).reviewers[0].max_cost_usd is None


@pytest.mark.parametrize("literal, why", [
    ("0", "zero is not a budget, and the CLI refuses it outright"),
    ("-1", "a negative cap is a typo the CLI throws on"),
    ("-0.5", "…including a fractional one"),
    ("nan", "TOML has a real nan literal; it is not a finite budget"),
    ("inf", "TOML has a real inf literal; an unbounded cap must say so by "
            "being absent, not by being infinite"),
    ("-inf", "…and the negative infinity likewise"),
    ("true", "bool subclasses int, so `true` would otherwise load as 1"),
    ("false", "…and `false` as 0"),
    ('"0.50"', "a quoted number is a string, not a budget"),
])
def test_max_cost_usd_rejects_a_bad_value_loudly_naming_the_reviewer(
        tmp_path, literal, why):
    """Every rejection names the reviewer, because a config may hold several.

    The alternative to failing here is failing in the child process, where the
    CLI throws before writing a single byte of stdout — an empty-stdout rc 1
    that no adapter can distinguish from a provider that simply said nothing.
    """
    p = _write(tmp_path / "g.toml", f"""
[[reviewers]]
name = "sec"
provider = "anthropic"
model = "m"
max_cost_usd = {literal}
""")
    with pytest.raises(ValueError, match=r"reviewer 'sec': max_cost_usd") as e:
        load_config(None, global_path=p)
    assert "max_cost_usd" in str(e.value), why


# ---------------------------------------------------------------------------
# `[dispatch]`: the background dispatcher's own table
# ---------------------------------------------------------------------------
#
# A separate table rather than more `[defaults]` keys, because the two describe
# different runs: `[defaults]` is what a FOREGROUND review may spend, and
# `[dispatch]` is what a background pre-push worker may spend (a much tighter
# timeout, because the push is already over and nothing is waiting on it).
# Validated exactly like `[defaults]` -- these are the user's own numbers, and a
# typo in them must be loud and early rather than a quietly useless review.


def test_dispatch_defaults_are_the_documented_ones():
    cfg = load_config(None, global_path=None)
    assert cfg.dispatch == Dispatch()
    assert (cfg.dispatch.enabled, cfg.dispatch.timeout_sec,
            cfg.dispatch.timeout_retries, cfg.dispatch.dedup,
            cfg.dispatch.large_prompt_bytes) == (True, 240, 0, True, 80_000)


def test_dispatch_values_load_and_the_project_layer_wins(tmp_path):
    g = _write(tmp_path / "g.toml", """
[dispatch]
timeout_sec = 300
dedup = false
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[dispatch]
timeout_sec = 120
enabled = false
large_prompt_bytes = 1
timeout_retries = 2
""")
    d = load_config(repo, global_path=g).dispatch
    assert d.timeout_sec == 120          # project layer wins per key
    assert d.dedup is False              # ...and the global key survives
    assert d.enabled is False
    assert d.large_prompt_bytes == 1
    assert d.timeout_retries == 2


def test_unknown_dispatch_key_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", "[dispatch]\nlarge_prompt_escalation = 10\n")
    with pytest.raises(ValueError, match=r"unknown \[dispatch\] keys: "
                                         r"\['large_prompt_escalation'\]"):
        load_config(None, global_path=g)


def test_every_integer_dispatch_field_has_a_declared_minimum():
    numeric = {f.name for f in fields(Dispatch) if f.type in ("int", int)}
    assert numeric, "expected Dispatch to have integer fields"
    assert numeric == set(_DISPATCH_MINIMUMS)


def test_every_bool_dispatch_field_is_declared_as_one():
    flags = {f.name for f in fields(Dispatch) if f.type in ("bool", bool)}
    assert flags == set(_DISPATCH_FLAGS)


@pytest.mark.parametrize("key", sorted(_DISPATCH_MINIMUMS))
def test_dispatch_numeric_accepts_its_minimum(tmp_path, key):
    g = _write(tmp_path / "g.toml",
               f"[dispatch]\n{key} = {_DISPATCH_MINIMUMS[key]}\n")
    assert getattr(load_config(None, global_path=g).dispatch, key) \
        == _DISPATCH_MINIMUMS[key]


@pytest.mark.parametrize("key", sorted(_DISPATCH_MINIMUMS))
def test_dispatch_numeric_rejects_below_minimum(tmp_path, key):
    below = _DISPATCH_MINIMUMS[key] - 1
    g = _write(tmp_path / "g.toml", f"[dispatch]\n{key} = {below}\n")
    with pytest.raises(ValueError, match=rf"\[dispatch\] {key}: must be >= "
                                         rf"{_DISPATCH_MINIMUMS[key]}, got {below}"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("literal, shown", [('"420"', "str"), ("1.5", "float"),
                                            ("true", "bool")])
@pytest.mark.parametrize("key", sorted(_DISPATCH_MINIMUMS))
def test_dispatch_numeric_rejects_a_non_integer(tmp_path, key, literal, shown):
    g = _write(tmp_path / "g.toml", f"[dispatch]\n{key} = {literal}\n")
    with pytest.raises(
            ValueError,
            match=rf"\[dispatch\] {key}: expected an integer, got {shown}"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("literal, shown", [('"false"', "str"), ("0", "int"),
                                            ("1", "int"), ("1.0", "float")])
@pytest.mark.parametrize("key", sorted(_DISPATCH_FLAGS))
def test_dispatch_flag_rejects_anything_but_a_bool(tmp_path, key, literal, shown):
    """`bool("false")` is True, and `enabled = "false"` must not enable dispatch.

    This is the same refusal-to-coerce the store applies to the trust axes: a
    kill switch that reads "false" as "yes" is the exact bug class Phase 1 had
    to fix once already.
    """
    g = _write(tmp_path / "g.toml", f"[dispatch]\n{key} = {literal}\n")
    with pytest.raises(
            ValueError,
            match=rf"\[dispatch\] {key}: expected true or false, got {shown}"):
        load_config(None, global_path=g)


def test_a_dispatch_table_does_not_disturb_defaults_or_reviewers(tmp_path):
    g = _write(tmp_path / "g.toml", """
[defaults]
timeout_sec = 411
[dispatch]
timeout_sec = 7
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
""")
    cfg = load_config(None, global_path=g)
    assert cfg.defaults.timeout_sec == 411      # the two tables are separate
    assert cfg.dispatch.timeout_sec == 7
    assert [r.name for r in cfg.reviewers] == ["finder"]
