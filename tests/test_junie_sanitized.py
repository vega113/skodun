"""Tests for junie Seatbelt profile, binary resolve, and sanitized env."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from skodun.adapters import junie_sanitized as js


def test_resolve_sandbox_exec_refuses_non_darwin():
    with mock.patch.object(js.sys, "platform", "linux"):
        with pytest.raises(RuntimeError, match="requires macOS"):
            js.resolve_sandbox_exec()


def test_resolve_sandbox_exec_refuses_missing_binary(tmp_path: Path):
    with mock.patch.object(js.sys, "platform", "darwin"):
        with mock.patch.object(js, "SANDBOX_EXEC", str(tmp_path / "nope")):
            with pytest.raises(RuntimeError, match="unavailable"):
                js.resolve_sandbox_exec()


def test_build_sandbox_profile_denies_link_clone_and_scopes_write(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    junie_data = home / ".local" / "share" / "junie"
    junie_data.mkdir(parents=True)
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    binary = junie_data / "junie"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    profile = js.build_sandbox_profile(
        capsule=str(capsule),
        binary=str(binary),
        junie_data=str(junie_data),
        home=str(home),
    )
    assert "(deny file-link file-clone)" in profile
    assert "(deny process-fork)" in profile
    assert "file-write*" in profile
    # Paths are JSON-encoded, not raw-interpolated.
    assert js._sbpl_string(str(capsule.resolve())) in profile
    # Only the capsule (and /dev/null) are writable — not the home tree.
    assert f"(subpath {js._sbpl_string(str(home.resolve()))})" not in profile.split(
        "file-write*"
    )[1]


def test_sbpl_string_rejects_empty_and_nul():
    with pytest.raises(ValueError):
        js._sbpl_string("")
    with pytest.raises(ValueError):
        js._sbpl_string("a\x00b")


def test_resolve_junie_binary_rejects_relative(tmp_path: Path):
    with pytest.raises(ValueError, match="absolute path"):
        js.resolve_junie_binary("junie", str(tmp_path))


def test_resolve_junie_binary_returns_non_shim(tmp_path: Path):
    binary = tmp_path / "junie"
    binary.write_bytes(b"#!/bin/sh\necho real\n")
    binary.chmod(0o755)
    assert js.resolve_junie_binary(str(binary), str(tmp_path)) == str(binary.resolve())


def test_build_sanitized_env_strips_foreign_keys_keeps_junie_key():
    parent = {
        "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-ant",
        "GOOGLE_API_KEY": "g",
        "GEMINI_API_KEY": "ge",
        "XAI_API_KEY": "x",
        "GROK_API_KEY": "gr",
        "OPENROUTER_API_KEY": "or",
        "LITELLM_API_KEY": "ll",
        "GH_TOKEN": "gh",
        "GITHUB_TOKEN": "ght",
        "HEROKU_API_KEY": "hk",
        "EMAILIT_API_KEY": "em",
        "JUNIE_API_KEY": "junie-secret",
        "PATH": "/evil/bin",
        "HOME": "/evil/home",
    }
    env = js.build_sanitized_env(
        home="/real/home",
        junie_home="/cap/home",
        tmpdir="/cap/tmp",
        junie_data="/real/home/.local/share/junie",
        log_dir="/cap/logs",
        parent_env=parent,
    )
    for key in js.STRIPPED_ENV_KEYS:
        assert key not in env, f"{key} must not leak into the child env"
    assert env["JUNIE_API_KEY"] == "junie-secret"
    assert env["PATH"] == js.SYSTEM_PATH
    assert env["HOME"] == "/real/home"
    assert "OPENAI_API_KEY" not in env


def test_build_sanitized_env_omits_junie_key_when_unset():
    env = js.build_sanitized_env(
        home="/h",
        junie_home="/c/h",
        tmpdir="/c/t",
        junie_data="/h/.local/share/junie",
        log_dir="/c/l",
        parent_env={},
    )
    assert "JUNIE_API_KEY" not in env


def test_build_junie_argv_pins_discovery_disable_and_stdin_mode():
    argv = js.build_junie_argv(
        binary="/bin/junie",
        output="/cap/out.json",
        project="/cap/project",
        model="gpt-5.6-luna",
        timeout_ms=120000,
        cache="/cap/cache",
        config="/cap/config.json",
        extensions="/cap/ext",
        effort="high",
    )
    assert "--input-format" in argv and "text" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--json-output-file" in argv
    assert "--project" in argv and "/cap/project" in argv
    assert "--skip-update-check" in argv
    assert argv[argv.index("--config-default-locations") + 1] == "false"
    assert argv[argv.index("--mcp-default-locations") + 1] == "false"
    assert argv[argv.index("--skill-default-locations") + 1] == "false"
    assert argv[argv.index("--command-default-location") + 1] == "false"
    assert argv[argv.index("--agent-default-location") + 1] == "false"
    assert argv[argv.index("--model-default-locations") + 1] == "false"
    # Prompt must never appear on argv.
    joined = " ".join(argv)
    assert "CRITICAL" not in joined
    assert "findings" not in joined
    assert "--effort" in argv and "high" in argv
