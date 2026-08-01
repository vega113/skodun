"""Seatbelt profile, binary resolution, and sanitized environment for junie.

Ported from the oracle's junie-sanitized-exec helpers (vendor-and-adapt).
Committed code is fully generic: no machine paths beyond the macOS
sandbox-exec absolute path Apple ships, and no one-project private surfaces.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Operator / provider credentials that must never reach a junie child.
# JUNIE_API_KEY is the one exception and is handled separately in
# build_sanitized_env — it is junie's own auth, not an inherited foreign key.
STRIPPED_ENV_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "OPENROUTER_API_KEY",
    "LITELLM_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "HEROKU_API_KEY",
    "EMAILIT_API_KEY",
)

# Minimal PATH inside the capsule: no developer tooling, no accidental
# helpers. Matches the oracle's system path for the sandboxed child.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def resolve_sandbox_exec() -> str:
    """Return the macOS sandbox-exec path, or raise if confinement is impossible."""
    if sys.platform != "darwin":
        raise RuntimeError("junie filesystem confinement requires macOS")
    if not os.path.isfile(SANDBOX_EXEC) or not os.access(SANDBOX_EXEC, os.X_OK):
        raise RuntimeError(
            f"required macOS sandbox-exec is unavailable: {SANDBOX_EXEC}"
        )
    return SANDBOX_EXEC


def _sbpl_string(value: str) -> str:
    """Encode a path as an SBPL string literal without executable interpolation."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("sandbox path must be a non-empty NUL-free string")
    return json.dumps(value, ensure_ascii=False)


def _subpath_rule(path: str) -> str:
    return f"(subpath {_sbpl_string(os.path.realpath(path))})"


def _literal_rule(path: str) -> str:
    return f"(literal {_sbpl_string(os.path.realpath(path))})"


def _raw_literal_rule(path: str) -> str:
    return f"(literal {_sbpl_string(path)})"


def _optional_user_literal_rule(path: str, root: str) -> str:
    """Allow an optional user file only when it has no link-based escape."""
    absolute_root = os.path.realpath(root)
    absolute_path = os.path.abspath(path)
    if os.path.commonpath((absolute_path, absolute_root)) != absolute_root:
        raise ValueError(f"user literal escapes its allowed root: {absolute_path}")
    relative = os.path.relpath(absolute_path, absolute_root)
    current = absolute_root
    for component in relative.split(os.sep):
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise ValueError(f"user literal path must not contain symlinks: {current}")
    if os.path.lexists(absolute_path):
        file_stat = os.stat(absolute_path, follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"user literal must be a regular file: {absolute_path}")
        if file_stat.st_nlink != 1:
            raise ValueError(f"user literal must not be a hardlink: {absolute_path}")
    return _raw_literal_rule(absolute_path)


def _is_within(path: str, root: str) -> bool:
    resolved_root = os.path.realpath(root)
    return os.path.commonpath((os.path.realpath(path), resolved_root)) == resolved_root


def require_managed_junie_data(
    path: str, home: str, *, require_existing: bool = True
) -> str:
    """Require the fixed managed install path without symlinked components."""
    lexical_home = os.path.abspath(home)
    home = os.path.realpath(home)
    relative_install = os.path.join(".local", "share", "junie")
    lexical_expected = os.path.join(lexical_home, relative_install)
    expected = os.path.join(home, relative_install)
    if os.path.abspath(path) not in (lexical_expected, expected):
        raise ValueError(f"JUNIE_DATA must be the managed installation path: {expected}")
    current = home
    for component in (".local", "share", "junie"):
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise ValueError(f"JUNIE_DATA path must not contain symlinks: {current}")
    if require_existing and not os.path.isdir(expected):
        raise ValueError(f"JUNIE_DATA must be an existing directory: {expected}")
    return expected


def _binary_read_root(binary: str, junie_data: str, capsule: str) -> str | None:
    """Return an external app-bundle resource root, or reject broad pins."""
    binary = os.path.realpath(binary)
    if _is_within(binary, junie_data) or _is_within(binary, capsule):
        return None
    current = os.path.dirname(binary)
    while current != os.path.dirname(current):
        if current.endswith(".app") and os.path.isdir(current):
            return current
        current = os.path.dirname(current)
    raise ValueError(
        "external junie binary must be inside the managed installation or app bundle"
    )


def _require_no_external_hardlinks(roots: tuple[str, ...], label: str) -> None:
    """Require every regular-file hardlink alias to remain inside allowed roots."""
    resolved_roots: list[str] = []
    for root in sorted({os.path.realpath(root) for root in roots}, key=len):
        if not any(_is_within(root, existing) for existing in resolved_roots):
            resolved_roots.append(root)

    observed_links: dict[tuple[int, int], int] = {}
    link_counts: dict[tuple[int, int], int] = {}
    for root in resolved_roots:
        for current_root, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                path = os.path.join(current_root, filename)
                metadata = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                inode = (metadata.st_dev, metadata.st_ino)
                observed_links[inode] = observed_links.get(inode, 0) + 1
                link_counts[inode] = metadata.st_nlink

    if any(
        observed_links[inode] != link_count
        for inode, link_count in link_counts.items()
    ):
        raise ValueError(f"{label} contains an outside hardlink alias")


def resolve_junie_binary(binary: str, junie_data: str) -> str:
    """Bypass the managed updater shim and return the installed runtime binary."""
    resolved = os.path.realpath(binary)
    if not os.path.isabs(binary) or not os.access(resolved, os.X_OK):
        raise ValueError(f"junie CLI is not an executable absolute path: {binary}")
    if not os.path.isfile(resolved):
        raise ValueError(f"junie CLI is not a regular file: {binary}")

    with open(resolved, "rb") as binary_file:
        managed_shim = b"JUNIE_MANAGED_SHIM" in binary_file.read(8192)
    if not managed_shim:
        return resolved

    candidates = (
        os.path.join(
            junie_data,
            "current",
            "Applications",
            "junie.app",
            "Contents",
            "MacOS",
            "junie",
        ),
        os.path.join(junie_data, "current", "junie", "bin", "junie"),
        os.path.join(junie_data, "current", "junie"),
    )
    install_root = os.path.realpath(junie_data)
    for candidate in candidates:
        resolved_candidate = os.path.realpath(candidate)
        if (
            os.path.commonpath((resolved_candidate, install_root)) == install_root
            and os.path.isfile(resolved_candidate)
            and os.access(resolved_candidate, os.X_OK)
        ):
            return resolved_candidate
    raise ValueError("managed junie shim has no executable installed runtime")


def build_sandbox_profile(
    *, capsule: str, binary: str, junie_data: str, home: str
) -> str:
    """Return a filesystem-deny-by-default Seatbelt policy for generic junie."""
    capsule = os.path.realpath(capsule)
    binary = os.path.realpath(binary)
    junie_data = require_managed_junie_data(
        junie_data, home, require_existing=False
    )
    home = os.path.realpath(home)
    keychains = os.path.join(home, "Library", "Keychains")
    capsule_parent = os.path.dirname(capsule)
    binary_read_root = _binary_read_root(binary, junie_data, capsule)
    user_read_roots = (junie_data,)
    if binary_read_root:
        user_read_roots += (binary_read_root,)
    _require_no_external_hardlinks(user_read_roots, "junie read root")

    read_subpaths = (
        "/System/Library",
        "/usr/bin",
        "/usr/lib",
        "/usr/libexec",
        "/usr/share",
        "/bin",
        "/sbin",
        "/private/var/db/timezone",
        "/private/var/select",
        capsule,
        junie_data,
    )
    if binary_read_root:
        read_subpaths += (binary_read_root,)
    read_literals = (
        "/",
        capsule_parent,
        binary,
        "/dev/null",
        "/dev/random",
        "/dev/urandom",
        "/dev/zero",
        "/private/var/run/systemkeychaincheck.done",
        "/private/var/run/systemkeychaincheck.socket",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/services",
        "/etc/protocols",
        "/Library/Keychains/System.keychain",
    )
    read_rules = " ".join(_subpath_rule(path) for path in read_subpaths)
    literal_rules = " ".join(_literal_rule(path) for path in read_literals)
    login_keychain_rules = " ".join(
        _optional_user_literal_rule(os.path.join(keychains, filename), home)
        for filename in ("login.keychain-db", "login.keychain")
    )
    runtime_read_rules = _raw_literal_rule("/var/run/mDNSResponder")
    capsule_rule = _subpath_rule(capsule)
    capsule_parent_rule = _subpath_rule(capsule_parent)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-read* file-write* file-test-existence)\n"
        "(deny file-link file-clone)\n"
        "(deny process-fork)\n"
        "(allow file-read* file-test-existence "
        f"{read_rules} {literal_rules} {login_keychain_rules} "
        f"{runtime_read_rules})\n"
        '(allow file-read-metadata file-test-existence (subpath "/"))\n'
        "(allow file-read-metadata file-test-existence "
        f"{capsule_parent_rule})\n"
        f"(allow file-write* {capsule_rule} {_literal_rule('/dev/null')})\n"
    )


def account_home() -> str:
    """The real login home — Keychain access requires it, not a synthetic HOME."""
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def build_sanitized_env(
    *,
    home: str,
    junie_home: str,
    tmpdir: str,
    junie_data: str,
    log_dir: str,
    path: str = SYSTEM_PATH,
    parent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment: strip foreign secrets, keep junie's own key."""
    parent = parent_env if parent_env is not None else os.environ
    env = {
        "PATH": path,
        "HOME": home,
        "JUNIE_HOME": junie_home,
        "TMPDIR": tmpdir,
        "JUNIE_DATA": junie_data,
        "JUNIE_LOG_DIR": log_dir,
        "JAVA_TOOL_OPTIONS": (
            f"-Djava.io.tmpdir={tmpdir} -Duser.home={junie_home}"
        ),
        "LANG": "C",
        "LC_ALL": "C",
    }
    junie_api_key = parent.get("JUNIE_API_KEY")
    if junie_api_key:
        env["JUNIE_API_KEY"] = junie_api_key
    # Explicitly do not copy any stripped key even if a future edit adds a
    # parent-env merge: the strip is the whole point of this function.
    for key in STRIPPED_ENV_KEYS:
        env.pop(key, None)
    return env


def write_profile(path: str, profile: str) -> None:
    """Write the Seatbelt profile as a new exclusive file inside the capsule."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        payload = profile.encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError("short write while creating junie sandbox profile")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)


def build_junie_argv(
    *,
    binary: str,
    output: str,
    project: str,
    model: str,
    timeout_ms: int,
    cache: str,
    config: str,
    extensions: str,
    effort: str | None = None,
) -> list[str]:
    """The sandboxed junie argv (no prompt — prompt arrives on stdin)."""
    argv = [
        binary,
        "--input-format",
        "text",
        "--output-format",
        "json",
        "--json-output-file",
        output,
        "--project",
        project,
        "--model",
        model,
        "--timeout",
        str(timeout_ms),
        "--skip-update-check",
        "--cache-dir",
        cache,
        "--config-default-locations",
        "false",
        "--config-location",
        config,
        "--mcp-default-locations",
        "false",
        "--skill-default-locations",
        "false",
        "--command-default-location",
        "false",
        "--agent-default-location",
        "false",
        "--model-default-locations",
        "false",
        "--extensions-default-location",
        extensions,
    ]
    if effort is not None:
        argv.extend(["--effort", effort])
    return argv
