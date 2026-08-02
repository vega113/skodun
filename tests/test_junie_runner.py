"""Tests for the junie outer runner: capsule, normalize, platform refuse."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
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


def test_normalize_review_json_under_production_tempdir():
    """review.json path must survive /var vs /private/var on macOS.

    Production capsules are made with tempfile.mkdtemp under gettempdir(),
    which on macOS is typically /var/folders/... while Path.resolve() rewrites
    that to /private/var/folders/.... pytest's tmp_path is already under
    /private/var, so it cannot catch the commonpath mismatch. This test stages
    under the real temp root and passes unresolved Path objects the way
    run_confined_junie does.
    """
    tmp = tempfile.gettempdir()
    root = Path(tempfile.mkdtemp(prefix="skodun-junie-prod-shape.", dir=tmp))
    try:
        # Document the split when present: unresolved root is /var/...,
        # resolve() is /private/var/.... That is the defect surface.
        unresolved = str(root)
        resolved = str(root.resolve())
        if unresolved != resolved:
            assert unresolved.startswith("/var/"), unresolved
            assert resolved.startswith("/private/var/"), resolved

        capsule = root / "capsule"
        project = capsule / "project"
        project.mkdir(parents=True)
        review = {
            "summary": "prod-shaped capsule",
            "findings": [
                {
                    "file": "b.py",
                    "line": 2,
                    "severity": "medium",
                    "category": "bug",
                    "title": "prod",
                    "detail": "detail",
                }
            ],
        }
        (project / "review.json").write_text(json.dumps(review), encoding="utf-8")
        # Unresolved paths — what stage_capsule / run_confined_junie hand in.
        assert "/private/" not in str(project) or unresolved == resolved
        env = _envelope(changes=[{"afterRelativePath": "review.json"}])
        got = jr.normalize_envelope(
            env,
            project=project,          # unresolved
            capsule=capsule,          # unresolved; normalize resolves both sides
            configured_model="gpt-5.6-luna",
        )
        assert got["summary"] == "prod-shaped capsule"
        assert got["findings"][0]["title"] == "prod"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_run_confined_junie_e2e_review_json_via_spawner():
    """Full run_confined_junie path: spawner writes envelope + review.json.

    Drives the shipped entry point on a production-shaped mkdtemp capsule so
    confined reads of review.json and the envelope go through the real
    normalize path, not a pytest tmp_path shortcut.
    """
    # Pre-stage nothing: run_confined_junie stages under gettempdir().
    with tempfile.TemporaryDirectory(prefix="skodun-junie-e2e-prompt.") as td:
        prompt = Path(td) / "prompt.txt"
        prompt.write_text("review the change", encoding="utf-8")
        fake_bin = Path(td) / "junie"
        fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_bin.chmod(0o755)

        review = {
            "summary": "e2e review.json",
            "findings": [
                {
                    "file": "c.py",
                    "line": 3,
                    "severity": "low",
                    "category": "style",
                    "title": "e2e",
                    "detail": "from spawner",
                }
            ],
        }

        def spawner(argv, *, env, cwd, stdin_path, stdout_path, stderr_path):
            # Locate --json-output-file and --project from the sandboxed argv.
            # argv is [sandbox-exec, -f, profile, junie, ...flags...]
            out_file = None
            project_dir = None
            i = 0
            while i < len(argv):
                if argv[i] == "--json-output-file" and i + 1 < len(argv):
                    out_file = Path(argv[i + 1])
                if argv[i] == "--project" and i + 1 < len(argv):
                    project_dir = Path(argv[i + 1])
                i += 1
            assert out_file is not None and project_dir is not None
            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "review.json").write_text(
                json.dumps(review), encoding="utf-8"
            )
            envelope = {
                "changes": [{"afterRelativePath": "review.json"}],
                "llmUsage": [
                    {"model": "gpt-5.6-luna", "inputTokens": 1, "outputTokens": 1}
                ],
            }
            out_file.write_text(json.dumps(envelope), encoding="utf-8")
            Path(stdout_path).write_bytes(b"")
            Path(stderr_path).write_bytes(b"")
            # Prompt was staged into the capsule and opened as stdin.
            assert Path(stdin_path).is_file()
            assert b"review the change" in Path(stdin_path).read_bytes()
            # Capsule must be under the system temp root (production shape).
            capsule_inner = project_dir.parent
            tmp = Path(tempfile.gettempdir())
            assert os.path.commonpath(
                (str(capsule_inner.resolve()), str(tmp.resolve()))
            ) == str(tmp.resolve())
            return 0

        # Force darwin path so the spawner runs; skip real sandbox-exec by
        # injecting resolve_sandbox_exec and binary resolution via mocks.
        import unittest.mock as mock

        with mock.patch.object(
            jr.js, "resolve_sandbox_exec", return_value="/usr/bin/sandbox-exec"
        ), mock.patch.object(
            jr.js, "resolve_junie_binary",
            side_effect=lambda binary, junie_data: binary,
        ), mock.patch.object(
            jr.js, "require_managed_junie_data",
            side_effect=lambda path, home, require_existing=False: path,
        ), mock.patch.object(
            jr.js, "build_sandbox_profile",
            return_value="(version 1)\n(allow default)\n",
        ), mock.patch.object(
            jr.js, "account_home",
            return_value=str(Path.home()),
        ), mock.patch.object(
            jr.js, "write_profile",
        ):
            rc, out, err = jr.run_confined_junie(
                prompt_file=prompt,
                binary=str(fake_bin),
                model="gpt-5.6-luna",
                effort="high",
                timeout_ms=5000,
                contract_schema='{"type":"object"}',
                spawner=spawner,
                platform="darwin",
            )
        assert rc == 0, (rc, err)
        payload = json.loads(out.decode("utf-8"))
        assert payload["summary"] == "e2e review.json"
        assert payload["findings"][0]["title"] == "e2e"


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


def test_normalize_partial_markdown_with_embedded_json(tmp_path: Path):
    """Live junie sometimes wraps valid JSON under ### Summary only.

    Reproduced under the confined harness: result was
    ``### Summary\\n- {"summary":"ping","findings":[]}`` without
    ### Changes / ### Verification. That used to raise ``not a review
    payload`` and force a degraded retry even though the model paid for
    a complete answer.
    """
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    payload = {"summary": "ping", "findings": []}
    env = _envelope(result="### Summary\n- " + json.dumps(payload))
    got = jr.normalize_envelope(
        env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
    )
    assert got == payload


def test_normalize_embedded_json_with_nested_findings(tmp_path: Path):
    capsule = tmp_path / "capsule"
    project = capsule / "project"
    project.mkdir(parents=True)
    payload = {
        "summary": "one",
        "findings": [
            {
                "file": "a.py",
                "line": 2,
                "severity": "low",
                "category": "docs",
                "title": "t",
                "detail": "d",
            }
        ],
    }
    # prose before the object — raw_decode scan must still find it
    env = _envelope(result="Here is the review:\n" + json.dumps(payload) + "\n")
    got = jr.normalize_envelope(
        env, project=project, capsule=capsule, configured_model="gpt-5.6-luna"
    )
    assert got == payload
