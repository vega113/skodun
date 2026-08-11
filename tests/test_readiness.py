"""Read-only provider-topology readiness checks for the shipped review path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from skodun import readiness
from skodun.config import Config, Defaults, Reviewer
from skodun.store import Store


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Readiness Test")
    (repo / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
    return repo


def _cfg(*reviewers: Reviewer) -> Config:
    return Config(defaults=Defaults(), reviewers=reviewers)


def test_unknown_provider_health_is_eligible_and_reports_budget(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    cfg = _cfg(Reviewer(name="finder", provider="xai", model="grok"))
    with Store.open(tmp_path / "store.db") as store:
        report = readiness.check(store, repo, cfg)

    assert report.ready is True
    assert report.state == "potentially_available"
    assert report.reason_code == "health_unknown"
    assert report.estimated_worst_runtime_sec > 0
    assert report.estimated_attempts > 0


def test_all_finder_chain_entries_in_active_quota_blackout_fail_fast(tmp_path,
                                                                      monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    cfg = _cfg(
        Reviewer(name="finder", provider="xai", model="grok",
                 fallbacks=("backup",)),
        Reviewer(name="backup", provider="xai", model="grok"),
    )
    with Store.open(tmp_path / "store.db") as store:
        store.mark_provider_unavailable("xai", "quota is exhausted", "quota",
                                        "2999-01-01T00:00:00Z")
        report = readiness.check(store, repo, cfg)

    assert report.ready is False
    assert report.reason_code == "finder_chain_unavailable"
    assert report.estimated_attempts == 0


def test_missing_binary_is_known_impossible_without_capacity(tmp_path,
                                                              monkeypatch):
    repo = _repo(tmp_path)
    missing = str(tmp_path / "missing-grok")
    monkeypatch.setenv("SKODUN_GROK_BIN", missing)
    cfg = _cfg(Reviewer(name="finder", provider="xai", model="grok"))
    with Store.open(tmp_path / "store.db") as store:
        report = readiness.check(store, repo, cfg)

    assert report.ready is False
    assert report.reason_code == "binary_unavailable"
    assert "missing" in report.reason


def test_openai_api_without_key_is_known_impossible(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SKODUN_OPENAI_API_KEY", raising=False)
    cfg = _cfg(Reviewer(name="finder", provider="openai-api", model="gpt"))
    with Store.open(tmp_path / "store.db") as store:
        report = readiness.check(store, repo, cfg)

    assert report.ready is False
    assert report.reason_code == "auth_unavailable"


def test_scheduled_security_pass_must_have_an_eligible_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    monkeypatch.setenv("SKODUN_CODEX_BIN", str(tmp_path / "missing-codex"))
    cfg = Config(
        defaults=Defaults(security_path_segments=("app.py",)),
        reviewers=(
            Reviewer(name="finder", provider="xai", model="grok"),
            Reviewer(name="security", provider="openai", model="codex",
                     role="security"),
        ),
    )
    with Store.open(tmp_path / "store.db") as store:
        report = readiness.check(store, repo, cfg)

    assert report.ready is False
    assert report.reason_code == "required_pass_unavailable"
    assert report.passes[0]["pass"] == "security"


def test_json_and_human_renderings_use_the_same_report(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("SKODUN_GROK_BIN", "/bin/sh")
    cfg = _cfg(Reviewer(name="finder", provider="xai", model="grok"))
    with Store.open(tmp_path / "store.db") as store:
        report = readiness.check(store, repo, cfg)

    payload = report.to_dict()
    assert readiness.render(report, output="json")
    human = readiness.render(report, output="text")
    assert payload["state"] in human
    assert payload["reason_code"] in human
