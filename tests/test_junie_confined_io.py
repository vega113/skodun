"""Tests for descriptor-confined Junie artifact reads."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

import skodun.adapters.junie_confined_io as mod
from skodun.adapters.junie_confined_io import open_confined_text


def test_reads_single_link_regular_file_inside_root(tmp_path: Path):
    artifact = tmp_path / "project" / "review.json"
    artifact.parent.mkdir()
    artifact.write_text('{"findings":[]}\n', encoding="utf-8")
    with open_confined_text(str(artifact), str(tmp_path), "review") as handle:
        assert handle.read() == '{"findings":[]}\n'


def test_rejects_path_outside_root(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="escapes"):
            with open_confined_text(str(outside), str(tmp_path), "out"):
                pass
    finally:
        outside.unlink(missing_ok=True)


def test_rejects_symlink_final(tmp_path: Path):
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    real.write_text("{}", encoding="utf-8")
    link.symlink_to(real)
    with pytest.raises(ValueError, match="single-link regular file"):
        with open_confined_text(str(link), str(tmp_path), "link"):
            pass


def test_rejects_hardlink(tmp_path: Path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}", encoding="utf-8")
    os.link(a, b)
    with pytest.raises(ValueError, match="single-link regular file"):
        with open_confined_text(str(b), str(tmp_path), "b"):
            pass


def test_rejects_final_path_replaced_after_validation(tmp_path: Path):
    """TOCTOU: path validated as a string, then replaced with a symlink."""
    artifact = tmp_path / "output.json"
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    artifact.write_text('{"safe":true}\n', encoding="utf-8")
    outside.write_text('{"secret":true}\n', encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "output.json" and kwargs.get("dir_fd") is not None:
            artifact.unlink()
            artifact.symlink_to(outside)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    try:
        with mock.patch.object(mod.os, "open", side_effect=replace_before_open):
            with pytest.raises(ValueError, match="single-link regular file"):
                with open_confined_text(str(artifact), str(tmp_path), "output"):
                    pytest.fail("replacement symlink was opened")
        assert replaced
    finally:
        outside.unlink(missing_ok=True)
