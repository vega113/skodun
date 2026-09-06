from dataclasses import fields
from pathlib import Path

import pytest

from skodun.config import (
    _DEFAULTS_MINIMUMS, _DISPATCH_FLAGS, _DISPATCH_MINIMUMS,
    SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOT_NAMES, SECURITY_PROMPT_SLOTS,
    _ROUTING_FLAGS, ROUTING_MODE_ENV,
    Defaults, Dispatch, Reviewer, Routing, load_config, quota_pool_for,
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


def test_quota_pool_defaults_separate_google_models():
    assert quota_pool_for(Reviewer(name="gemini", provider="google",
                                   model="gemini-3.6-flash")) == "google:gemini"
    assert quota_pool_for(Reviewer(name="claude", provider="google",
                                   model="claude-sonnet-4-6")) == "google:claude-gpt"
    assert quota_pool_for(Reviewer(name="codex", provider="openai",
                                   model="gpt-5.4")) == "openai"


def test_explicit_quota_pool_is_preserved_and_validated(tmp_path):
    path = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "gemini"
provider = "google"
model = "gemini-3.6-flash"
quota_pool = "google:custom"
""")
    cfg = load_config(None, global_path=path)
    assert cfg.reviewers[0].quota_pool == "google:custom"
    assert quota_pool_for(cfg.reviewers[0]) == "google:custom"

    bad = _write(tmp_path / "bad.toml", """
[[reviewers]]
name = "gemini"
provider = "google"
model = "gemini-3.6-flash"
quota_pool = "   "
""")
    with pytest.raises(ValueError, match="quota_pool"):
        load_config(None, global_path=bad)


def test_repo_capacity_can_only_tighten_machine_cap(tmp_path):
    g = _write(tmp_path / "g.toml", """
[capacity]
machine = 2
review_fg = 2
""")
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".skodun.toml", """
[capacity]
machine = 8
review_fg = 1
""")
    cfg = load_config(repo, global_path=g)
    assert cfg.capacity.machine == 2
    assert cfg.capacity.review_fg == 1


def test_repo_capacity_machine_cannot_raise_the_default_ceiling(tmp_path):
    """A repo file with no global [capacity] machine may only tighten default 1."""
    from skodun.capacity import resolved_fg_capacity, resolved_machine_capacity

    g = _write(tmp_path / "g.toml", "")
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".skodun.toml", """
[capacity]
machine = 8
review_fg = 8
""")
    cfg = load_config(repo, global_path=g)
    assert cfg.capacity.machine == 1
    assert resolved_machine_capacity(cfg, env={}) == 1
    assert resolved_fg_capacity(cfg, env={}) == 1


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


# --------------------------------------------------------------------------
# per-reviewer max_diff_bytes — the envelope override
# --------------------------------------------------------------------------
#
# `[defaults] max_diff_bytes` is one global envelope, so it has to be fitted to
# the LEAST capable provider in the config: this repo's own `.skodun.toml` had
# to drop it to 100000 so `agy` could take the prompt, which needlessly shrank
# the review for `codex` and `grok`, whose prompts travel as files. The override
# lets one entry be budgeted differently without moving the global. Unset means
# "use the global" — the shipped behaviour, unchanged.

def test_reviewer_max_diff_bytes_defaults_to_none_meaning_use_the_global(tmp_path):
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
""")
    assert load_config(None, global_path=p).reviewers[0].max_diff_bytes is None


def test_reviewer_max_diff_bytes_is_read_off_the_entry(tmp_path):
    p = _write(tmp_path / "g.toml", """
[defaults]
max_diff_bytes = 100000
[[reviewers]]
name = "finder"
provider = "openai"
model = "m"
max_diff_bytes = 400000
""")
    cfg = load_config(None, global_path=p)
    assert cfg.defaults.max_diff_bytes == 100000
    assert cfg.reviewers[0].max_diff_bytes == 400000


@pytest.mark.parametrize("literal, shown, why", [
    ("0", "must be >= 1", "zero diff bytes is a review that cannot happen"),
    ("-1", "must be >= 1", "a negative envelope is a typo"),
    ('"big"', "expected an integer, got str", "a quoted number is a string"),
    ("true", "expected an integer, got bool",
     "bool subclasses int, so `true` would otherwise load as a 1-byte envelope"),
    ("1.5", "expected an integer, got float", "bytes do not come in halves"),
])
def test_reviewer_max_diff_bytes_is_validated_exactly_like_the_global(
        tmp_path, literal, shown, why):
    """The same rules as `[defaults] max_diff_bytes`, naming the REVIEWER.

    A config may hold several entries, and the value's provenance is lost the
    moment it reaches the planner — so the message has to say which entry.
    """
    p = _write(tmp_path / "g.toml", f"""
[[reviewers]]
name = "sec"
provider = "xai"
model = "m"
max_diff_bytes = {literal}
""")
    with pytest.raises(ValueError,
                       match=rf"reviewer 'sec': max_diff_bytes: {shown}") as e:
        load_config(None, global_path=p)
    assert "max_diff_bytes" in str(e.value), why


def test_reviewer_max_diff_bytes_accepts_its_minimum(tmp_path):
    """One byte is a coherent (if useless) envelope; zero is not."""
    p = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
max_diff_bytes = 1
""")
    assert load_config(None, global_path=p).reviewers[0].max_diff_bytes == 1


def test_reviewer_max_diff_bytes_merges_per_key_across_layers(tmp_path):
    """The project layer may set it where the global did not, and vice versa."""
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "finder"
provider = "xai"
model = "m"
max_diff_bytes = 200000
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[[reviewers]]
name = "finder"
max_diff_bytes = 50000
""")
    assert load_config(repo, global_path=g).reviewers[0].max_diff_bytes == 50000


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


# --- [routing] (epic S5) ----------------------------------------------------
# Which finder entry heads a review nobody pinned. `mode = "off"` is the
# shipped default and is pre-S5 selection exactly, so a config that has never
# heard of routing behaves as it always did; the env override exists because an
# operator has to be able to say "not on this machine" without editing a file
# somebody else's install wrote.


@pytest.fixture(autouse=True)
def _no_routing_env(monkeypatch, tmp_path):
    """No test in this module inherits the developer's OWN skodun config.

    Two leaks, and the second one shipped broken: clearing the env override was
    not enough, because `load_config(None, global_path=None)` resolves the
    global path from `SKODUN_CONFIG` and falls back to
    `~/.config/skodun/config.toml` -- so a developer whose real machine config
    sets `[routing] mode = "auto"` failed the defaults test, and one whose
    config was plain passed it. A test that reads a file outside the repo is
    testing the machine, not the code.

    `SKODUN_CONFIG` is pointed at a path that does not exist rather than at an
    empty file, because "no global config at all" is the state these defaults
    are about.
    """
    monkeypatch.delenv(ROUTING_MODE_ENV, raising=False)
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-such-global.toml"))


def test_routing_defaults_are_off_with_an_implicit_pool():
    cfg = load_config(None, global_path=None)
    assert cfg.routing == Routing()
    assert (cfg.routing.mode, cfg.routing.pool, cfg.routing.cross_model) == (
        "off", (), True)


def test_routing_values_load_and_the_project_layer_wins(tmp_path):
    g = _write(tmp_path / "g.toml", """
[routing]
mode = "auto"
cross_model = false
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
[[reviewers]]
name = "b"
provider = "openai"
model = "m"
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[routing]
pool = ["b", "a"]
""")
    r = load_config(repo, global_path=g).routing
    assert r.mode == "auto"              # global key survives the merge
    assert r.cross_model is False
    assert r.pool == ("b", "a")          # project layer contributes its own key


def test_routing_rejects_an_unknown_key(tmp_path):
    """`weights` was this test's example of a key nobody configured, until
    Phase B made it one that exists. The example has to be a key that is
    genuinely unknown, or the test passes on the wrong error."""
    g = _write(tmp_path / "g.toml", "[routing]\nstickiness = 1\n")
    with pytest.raises(ValueError,
                       match=r"unknown \[routing\] keys: \['stickiness'\]"):
        load_config(None, global_path=g)


def test_routing_weights_default_to_off_and_a_seven_day_window():
    """The default is Phase A exactly: no weights, so no share term at all."""
    r = load_config(None, global_path=None).routing
    assert r.weights == () and r.weights_window_days == 7


def test_routing_weights_load_as_ordered_pairs(tmp_path):
    g = _write(tmp_path / "g.toml", """
[routing]
weights = { xai = 3, openai = 1.5 }
weights_window_days = 2
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
[[reviewers]]
name = "b"
provider = "openai"
model = "m"
""")
    r = load_config(None, global_path=g).routing
    assert r.weights == (("xai", 3.0), ("openai", 1.5))
    assert r.weights_window_days == 2


@pytest.mark.parametrize("literal, expected", [
    ("{ xai = 0 }", r"must be greater than 0"),
    ("{ xai = -1 }", r"must be greater than 0"),
    # TOML has `inf` as a literal and `inf > 0` is TRUE, so this one reaches
    # the router: `target` becomes `inf / inf` (NaN), the scorer's `round()`
    # raises on it, and `auto_route`'s guard swallows that on EVERY routed run
    # -- auto-routing silently off, from a config that loaded cleanly.
    ("{ xai = inf }", r"must be a finite number"),
    ("{ xai = -inf }", r"must be a finite number"),
    ("{ xai = nan }", r"must be a finite number"),
    ('{ xai = "3" }', r"must be a number, got str"),
    ("{ xai = true }", r"must be a number, got bool"),
    ("3", r"expected a table of provider = number, got int"),
])
def test_routing_weights_reject_what_cannot_be_a_share(tmp_path, literal,
                                                       expected):
    """Every one of these is a typo that would otherwise do NOTHING, quietly,
    while the operator believed a provider was rationed. Zero is refused
    rather than clamped for a sharper reason: it reads as "never route here",
    which `pool` and `enabled = false` already say -- accepting it would add a
    third, SILENT way to exclude a provider from a table whose job is
    preference, not exclusion."""
    g = _write(tmp_path / "g.toml", f"""
[routing]
weights = {literal}
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
""")
    with pytest.raises(ValueError, match=rf"\[routing\] weights: .*{expected}"):
        load_config(None, global_path=g)


def test_routing_weights_reject_a_provider_nobody_configured(tmp_path):
    """`pool`'s argument, for the other table: a weight on a provider that
    cannot be routed to is a typo, and a typo that changes nothing is the one
    an operator never finds."""
    g = _write(tmp_path / "g.toml", """
[routing]
weights = { xia = 3 }
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
""")
    with pytest.raises(
            ValueError,
            match=r"\[routing\] weights: no reviewer uses provider 'xia'"):
        load_config(None, global_path=g)


def test_routing_weights_window_must_be_a_positive_integer(tmp_path):
    g = _write(tmp_path / "g.toml",
               "[routing]\nweights_window_days = 0\n")
    with pytest.raises(ValueError,
                       match=r"\[routing\] weights_window_days: must be >= 1"):
        load_config(None, global_path=g)


def test_routing_rejects_an_unknown_mode(tmp_path):
    g = _write(tmp_path / "g.toml", '[routing]\nmode = "weighted"\n')
    with pytest.raises(ValueError,
                       match=r"\[routing\] mode: unknown mode 'weighted'"):
        load_config(None, global_path=g)


def test_routing_rejects_a_non_string_mode(tmp_path):
    g = _write(tmp_path / "g.toml", "[routing]\nmode = true\n")
    with pytest.raises(ValueError, match=r"\[routing\] mode: expected one of"):
        load_config(None, global_path=g)


@pytest.mark.parametrize("literal, shown", [
    ('"finder"', "str"), ("[1]", "int"), ('[["a"]]', "list"),
])
def test_routing_pool_rejects_a_bad_shape(tmp_path, literal, shown):
    """A bare string is iterable; without the check it becomes letter-names."""
    g = _write(tmp_path / "g.toml", f"[routing]\npool = {literal}\n")
    with pytest.raises(ValueError, match=rf"\[routing\] pool: .*{shown}"):
        load_config(None, global_path=g)


def test_routing_pool_rejects_a_repeated_name(tmp_path):
    g = _write(tmp_path / "g.toml", """
[routing]
pool = ["a", "a"]
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] pool: 'a' listed more than once"):
        load_config(None, global_path=g)


def test_routing_pool_rejects_a_name_nobody_configured(tmp_path):
    g = _write(tmp_path / "g.toml", """
[routing]
pool = ["ghost"]
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] pool: reviewer 'ghost' does not exist"):
        load_config(None, global_path=g)


def test_routing_pool_rejects_a_disabled_entry(tmp_path):
    g = _write(tmp_path / "g.toml", """
[routing]
pool = ["a"]
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
enabled = false
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] pool: reviewer 'a' is disabled"):
        load_config(None, global_path=g)


def test_routing_pool_rejects_a_non_finder_entry(tmp_path):
    """A pool naming the refuter would quietly shrink the candidate set."""
    g = _write(tmp_path / "g.toml", """
[routing]
pool = ["r"]
[[reviewers]]
name = "r"
provider = "xai"
model = "m"
role = "refuter"
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] pool: reviewer 'r' has role 'refuter'"):
        load_config(None, global_path=g)


def test_routing_cross_model_rejects_anything_but_a_bool(tmp_path):
    g = _write(tmp_path / "g.toml", '[routing]\ncross_model = "false"\n')
    with pytest.raises(
            ValueError,
            match=r"\[routing\] cross_model: expected true or false, got str"):
        load_config(None, global_path=g)


def test_routing_flags_cover_every_bool_field_of_routing():
    flags = {f.name for f in fields(Routing) if f.type in ("bool", bool)}
    assert flags == set(_ROUTING_FLAGS)


def test_routing_mode_env_overrides_both_config_layers(tmp_path, monkeypatch):
    g = _write(tmp_path / "g.toml", '[routing]\nmode = "auto"\n')
    monkeypatch.setenv(ROUTING_MODE_ENV, "off")
    assert load_config(None, global_path=g).routing.mode == "off"
    monkeypatch.setenv(ROUTING_MODE_ENV, " auto ")
    assert load_config(None, global_path=None).routing.mode == "auto"


def test_an_empty_routing_mode_env_is_no_opinion(tmp_path, monkeypatch):
    """A wrapper script's `SKODUN_ROUTING_MODE=` must not silently mean off."""
    g = _write(tmp_path / "g.toml", '[routing]\nmode = "auto"\n')
    monkeypatch.setenv(ROUTING_MODE_ENV, "")
    assert load_config(None, global_path=g).routing.mode == "auto"


def test_a_bad_routing_mode_env_is_loud(monkeypatch):
    monkeypatch.setenv(ROUTING_MODE_ENV, "Auto")
    with pytest.raises(ValueError,
                       match=rf"{ROUTING_MODE_ENV}: unknown mode 'Auto'"):
        load_config(None, global_path=None)


def test_routing_weights_reject_a_total_that_overflows_to_infinity(tmp_path):
    """Each term finite is not enough: two finite weights can ADD to `inf`,
    and the router divides by that total -- every target would come out `0.0`,
    so the declared ratio would be gone while `skodun providers` went on
    reporting the weights as set. Same silent-disable this table refuses a
    bare `inf` for, one addition later."""
    g = _write(tmp_path / "g.toml", """
[routing]
weights = { xai = 1e308, openai = 1e308 }
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
[[reviewers]]
name = "b"
provider = "openai"
model = "m"
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] weights: the weights add up to "
                             r"infinity"):
        load_config(None, global_path=g)


def test_routing_weights_accept_large_but_summable_numbers(tmp_path):
    """The check is arithmetic validity, not a policy guess about how big a
    weight may be -- only the RATIO between them matters, so a config that
    expresses one with big numbers is still a config."""
    g = _write(tmp_path / "g.toml", """
[routing]
weights = { xai = 1e300, openai = 1e299 }
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
[[reviewers]]
name = "b"
provider = "openai"
model = "m"
""")
    assert load_config(None, global_path=g).routing.weights == (
        ("xai", 1e300), ("openai", 1e299))


def test_routing_weights_reject_an_integer_no_float_can_hold(tmp_path):
    """TOML integers are arbitrary precision, so `10 ** 400` is a perfectly
    good `int` that no float can represent -- and `math.isfinite` RAISES
    `OverflowError` on it rather than answering. An unusable weight has to
    leave the validator the way every other unusable weight does: as a
    `ValueError` naming the table and the key."""
    g = _write(tmp_path / "g.toml", f"""
[routing]
weights = {{ xai = {10 ** 400} }}
[[reviewers]]
name = "a"
provider = "xai"
model = "m"
""")
    with pytest.raises(ValueError,
                       match=r"\[routing\] weights: 'xai' is too large"):
        load_config(None, global_path=g)


# ---------------------------------------------------------------------------
# Shipped Grok finder defaults (model + effort + pin/fallback graph)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MULTI_PROVIDER_EXAMPLE = _REPO_ROOT / "examples" / "multi-provider.toml"


def _load_shipped_toml(tmp_path: Path, src: Path):
    """Load a committed TOML as a project file, with no global layer.

    The operator's `~/.config/skodun/config.toml` must not leak into these
    assertions: they pin what the repository ships, not what this machine
    happens to have merged on top.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".skodun.toml").write_text(src.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return load_config(repo, global_path=tmp_path / "absent.toml")


def _finder_named(cfg, name: str):
    for entry in cfg.reviewers:
        if entry.name == name:
            return entry
    raise AssertionError(f"no reviewer named {name!r} in { [r.name for r in cfg.reviewers] }")


def test_example_grok_finder_defaults_to_grok_46_medium(tmp_path):
    """The published multi-provider example is the operator-facing default."""
    cfg = _load_shipped_toml(tmp_path, _MULTI_PROVIDER_EXAMPLE)
    finder = _finder_named(cfg, "finder")
    assert finder.provider == "xai"
    assert finder.model == "grok-4.6"
    assert finder.effort == "medium"


def test_dogfood_grok_finder_defaults_to_grok_46_medium(tmp_path):
    """This repository's own `.skodun.toml` must match the shipped Grok default."""
    cfg = load_config(_REPO_ROOT, global_path=tmp_path / "absent.toml")
    finder = _finder_named(cfg, "finder")
    assert finder.provider == "xai"
    assert finder.model == "grok-4.6"
    assert finder.effort == "medium"


def test_example_pin_and_fallback_graph_are_unchanged(tmp_path):
    """Model/effort is the only intended default change.

    A pin still resolves by name, the finder's chain is still
    finder → finder-openai, and auto-routing still scores the same pool.
    """
    from skodun.pipeline import _chain_for, _requested_head

    cfg = _load_shipped_toml(tmp_path, _MULTI_PROVIDER_EXAMPLE)
    finder = _finder_named(cfg, "finder")
    openai = _requested_head(cfg, "finder-openai")
    gemini = _requested_head(cfg, "finder-gemini")
    refuter = _finder_named(cfg, "refuter")

    assert finder.fallbacks == ("finder-openai",)
    assert [r.name for r in _chain_for(cfg, finder)] == ["finder", "finder-openai"]
    assert openai.provider == "openai"
    assert openai.model == "gpt-5.6-luna"
    assert openai.effort == "high"
    assert openai.fallbacks == ("finder-gemini",)
    assert gemini.provider == "google"
    assert gemini.model == "gemini-3.7-flash-high"
    assert gemini.effort is None
    assert refuter.model == "gpt-5.6-luna"
    assert refuter.effort == "high"
    assert cfg.routing.mode == "auto"
    assert cfg.routing.pool == ("finder", "finder-openai")
    assert cfg.routing.cross_model is True


def test_dogfood_pin_and_fallback_graph_are_unchanged(tmp_path):
    from skodun.pipeline import _chain_for, _requested_head

    cfg = load_config(_REPO_ROOT, global_path=tmp_path / "absent.toml")
    finder = _finder_named(cfg, "finder")
    gemini = _requested_head(cfg, "finder-gemini")
    openai = _requested_head(cfg, "finder-openai")

    assert finder.fallbacks == ("finder-gemini", "finder-openai")
    assert [r.name for r in _chain_for(cfg, finder)] == [
        "finder", "finder-gemini", "finder-openai"]
    assert gemini.provider == "google"
    assert gemini.model == "gemini-3.7-flash-high"
    assert openai.provider == "openai"
    assert openai.model == "gpt-5.6-luna"
    assert openai.effort == "high"


@pytest.mark.parametrize("value", ["false", "0", '""', "[]"])
@pytest.mark.parametrize("layer", ["global", "repo"])
def test_falsey_capacity_non_tables_are_rejected(tmp_path, value, layer):
    global_path = _write(tmp_path / "global.toml", "")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = global_path if layer == "global" else repo / ".skodun.toml"
    _write(target, f"capacity = {value}\n")
    with pytest.raises(ValueError, match="capacity.*must be a table"):
        load_config(repo, global_path=global_path)


def test_global_fg_capacity_is_clipped_after_machine_env_resolution(tmp_path):
    from skodun.capacity import resolved_fg_capacity
    global_path = _write(tmp_path / 'global.toml', '[capacity]\nreview_fg = 4\n')
    cfg = load_config(None, global_path=global_path)
    assert resolved_fg_capacity(cfg, env={'SKODUN_REVIEW_MACHINE_CAPACITY': '4'}) == 4
    assert resolved_fg_capacity(cfg, env={}) == 1
