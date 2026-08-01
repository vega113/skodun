"""Tests for the junie outer runner: capsule, normalize, platform refuse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skodun.adapters import junie_runner as jr


def _envelope(
    *,
    result: str | None = None,
    changes: list | None = None,
    usage: list | None = None,
    include_usage: bool = True,
) -> dict:
    env: dict = {}
    if result is not None:
        env["result"] = result
    if changes is not None:
        env["changes"] = changes
    elif result is not None:
        env["changes"] = []
    if include_usage:
        env["llmUsage"] = usage if usage is not None else [
            {"model": "gpt-5.6-luna", "inputTokens": 1, "outputTokens": 1}
        ]
    return env


def test_normalize_from_fenced_json_result(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    payload = {
        "summary": "looks fine",
        "findings": [],
    }
    env = _envelope(result="```json\n" + json.dumps(payload) + "\n```")
    got = jr.normalize_envelope(
        env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
    )
    assert got == payload


def test_normalize_from_review_json(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    review = {
        "summary": "one issue",
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "high",
                "category": "bug",
                "title": "x",
                "detail": "y",
            }
        ],
    }
    (project / "review.json").write_text(json.dumps(review), encoding="utf-8")
    env = _envelope(
        changes=[{"afterRelativePath": "review.json"}],
    )
    got = jr.normalize_envelope(
        env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
    )
    assert got["summary"] == "one issue"
    assert len(got["findings"]) == 1


def test_normalize_rejects_unexpected_project_file(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    (project / "evil.py").write_text("print(1)\n", encoding="utf-8")
    env = _envelope(result=json.dumps({"summary": "ok", "findings": []}))
    with pytest.raises(ValueError, match="unexpected project file"):
        jr.normalize_envelope(
            env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
        )


def test_normalize_rejects_wrong_model_usage(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    env = _envelope(
        result=json.dumps({"summary": "ok", "findings": []}),
        usage=[{"model": "some-other-model", "inputTokens": 1}],
    )
    with pytest.raises(ValueError, match="configured model usage is not evidenced"):
        jr.normalize_envelope(
            env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
        )


def test_normalize_rejects_gemini_in_usage(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    env = _envelope(
        result=json.dumps({"summary": "ok", "findings": []}),
        usage=[
            {"model": "gpt-5.6-luna"},
            {"model": "gemini-2.5-pro"},
        ],
    )
    with pytest.raises(ValueError, match="upstream provider"):
        jr.normalize_envelope(
            env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
        )


def test_normalize_rejects_symlink_in_project(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    # Only a symlink under project — no other unexpected regular files.
    (project / "link.json").symlink_to(outside)
    env = _envelope(result=json.dumps({"summary": "ok", "findings": []}))
    with pytest.raises(ValueError, match="symlink"):
        jr.normalize_envelope(
            env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
        )


def test_run_confined_junie_refuses_non_darwin(tmp_path: Path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("review me", encoding="utf-8")
    called = {"n": 0}

    def spawner(*a, **k):
        called["n"] += 1
        return 0

    rc, out, err = jr.run_confined_junie(
        prompt_file=prompt,
        binary="/bin/junie",
        model="gpt-5.6-luna",
        effort="high",
        timeout_ms=1000,
        contract_schema="{}",
        spawner=spawner,
        platform="linux",
    )
    assert rc == 2
    assert out == b""
    assert b"requires macOS" in err
    assert called["n"] == 0, "spawner must not run off macOS"


def test_stage_capsule_writes_marker_and_brave_off(tmp_path: Path):
    root = jr.stage_capsule(b"hello prompt", tmp_root=tmp_path)
    assert (root / ".skodun-junie-review-capsule").read_text(
        encoding="utf-8"
    ).startswith(jr.CAPSULE_MARKER_PREFIX)
    assert (root / "capsule" / "config.json").read_text(
        encoding="utf-8"
    ).strip() == '{"brave":false}'
    assert b"hello prompt" in (root / "capsule" / "prompt.txt").read_bytes()


def test_normalize_clean_markdown_no_findings(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    md = (
        "### Summary\n- No findings.\n"
        "### Changes\n- No files modified.\n"
        "### Verification\n- No commands or tests were run.\n"
    )
    env = _envelope(result=md)
    got = jr.normalize_envelope(
        env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
    )
    assert got == {"summary": "No findings.", "findings": []}
