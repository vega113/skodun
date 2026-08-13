"""Shipped-path checks for the sdist/wheel identity hook."""

from __future__ import annotations

from pathlib import Path

import pytest

hatchling = pytest.importorskip("hatchling")
from hatch_build import _existing_embedded_commit, _git_commit  # noqa: E402


def test_existing_embedded_commit_reads_sdist_payload(tmp_path):
    payload = tmp_path / "src" / "skodun"
    payload.mkdir(parents=True)
    (payload / "_build.py").write_text(
        "COMMIT = '%s'\nSCHEMA_VERSION = 14\nVERSION = '0.5.0'\n" % ("a" * 40),
        encoding="utf-8")
    assert _existing_embedded_commit(tmp_path) == "a" * 40


def test_git_commit_refuses_unrelated_ancestor(tmp_path):
    nested = tmp_path / "not-this-repo"
    nested.mkdir()
    assert _git_commit(nested) is None


def test_package_root_reports_this_checkout():
    root = Path(__file__).resolve().parents[1]
    commit = _git_commit(root)
    assert commit is not None
    assert commit.split("-")[0]
    assert len(commit.split("-")[0]) == 40
