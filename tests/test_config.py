from pathlib import Path

import pytest

from skodun.config import Defaults, load_config


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
