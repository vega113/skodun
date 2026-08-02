"""Descriptor-confined reads for artifacts written inside a Junie capsule.

Ported from the oracle's junie_confined_io (vendor-and-adapt). Opens a
single-link regular file without following replaceable path links: each path
component is opened with O_NOFOLLOW, and the final inode must be a regular
file with nlink == 1. That is what stops a TOCTOU replace of the artifact
with a symlink or hardlink alias to an outside secret after the path has
been "validated" as a string.
"""

from __future__ import annotations

import contextlib
import os
import stat


@contextlib.contextmanager
def open_confined_text(path, root, label, *, errors="strict"):
    """Open a single-link regular file without following replaceable path links."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("descriptor-confined reads require O_NOFOLLOW")
    absolute_root = os.path.abspath(root)
    absolute_path = os.path.abspath(path)
    if os.path.commonpath((absolute_path, absolute_root)) != absolute_root:
        raise ValueError(f"{label} escapes its allowed root")
    relative = os.path.relpath(absolute_path, absolute_root)
    components = relative.split(os.sep)
    if relative == "." or any(component in ("", ".", "..") for component in components):
        raise ValueError(f"{label} must name a file below its allowed root")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_fd = os.open(absolute_root, directory_flags)
    file_fd = None
    handle = None
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            file_fd = os.open(components[-1], file_flags, dir_fd=directory_fd)
        except OSError as error:
            raise ValueError(
                f"{label} must be a single-link regular file"
            ) from error
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} must be a single-link regular file")
        handle = os.fdopen(
            file_fd,
            "r",
            encoding="utf-8",
            errors=errors,
        )
        file_fd = None
        yield handle
    finally:
        if handle is not None:
            handle.close()
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
