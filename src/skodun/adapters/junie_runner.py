"""Outer process: stage a junie capsule, spawn confined junie, normalize output.

The chain's runner spawns this module (`python -I -m skodun.adapters.junie_runner`)
as a normal child. On success it prints a single REVIEW_CONTRACT- or
REFUTER_CONTRACT-shaped JSON object on stdout; on failure it prints a
sanitized reason on stderr and exits non-zero. That keeps chain/runner free
of junie-specific knowledge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import junie_sanitized as js
from .junie_confined_io import open_confined_text

CAPSULE_MARKER_PREFIX = "skodun-junie-review-capsule-v1:"
_SCHEMA_HINT = (
    'CRITICAL INSTRUCTION: Do not use tools or modify any files. Return JSON '
    'matching this schema only. When findings exist, include every finding with '
    "file, line when known, severity, category, title, and detail. When no "
    'findings exist, return {"summary":"...","findings":[]}. Schema: '
)


def stage_capsule(
    prompt_bytes: bytes,
    *,
    tmp_root: Path | None = None,
    schema_hint: str | None = None,
) -> Path:
    """Create an isolated capsule root and stage the prompt + brave-off config.

    Returns the capsule *root* (parent of the inner `capsule/` directory).
    """
    base = Path(tmp_root) if tmp_root is not None else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="skodun-junie-review.", dir=str(base)))
    try:
        if " " in str(root) or "\t" in str(root):
            raise ValueError("junie capsule paths must not contain whitespace")
        inner = root / "capsule"
        project = inner / "project"
        cache = inner / "cache"
        extensions = inner / "extensions"
        junie_home = inner / "junie-home"
        tmpdir = inner / "tmp"
        logs = inner / "logs"
        for d in (project, cache, extensions, junie_home, tmpdir, logs):
            d.mkdir(parents=True)
        marker = root / ".skodun-junie-review-capsule"
        marker.write_text(
            f"{CAPSULE_MARKER_PREFIX}{root.name}\n", encoding="utf-8"
        )
        prompt_path = inner / "prompt.txt"
        body = prompt_bytes
        if schema_hint:
            suffix = ("\n\n" + schema_hint).encode("utf-8")
            body = body + suffix
        prompt_path.write_bytes(body)
        (inner / "config.json").write_text('{"brave":false}\n', encoding="utf-8")
        return root
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _normalize_model_token(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _model_evidenced(models: list[str], configured_model: str) -> bool:
    want = _normalize_model_token(configured_model)
    if not want:
        return False
    for model in models:
        if want in _normalize_model_token(model) or _normalize_model_token(model) in want:
            return True
    return False


def _walk_project_for_trust(project: Path, review_path: Path) -> None:
    """Reject symlinks, hardlinks, and unexpected files under the project."""
    if project.is_symlink() or not project.is_dir():
        raise ValueError("project path must be a non-symlink directory")
    project_real = project.resolve()
    review_real = review_path.resolve() if review_path.exists() else None
    for root, dirs, files in os.walk(project, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(project)
        rel_s = str(rel_root)
        if rel_s != "." and not (
            rel_s == ".junie" or rel_s.startswith(".junie" + os.sep)
        ):
            raise ValueError(f"unexpected project directory: {rel_s}")
        for dirname in dirs:
            path = root_path / dirname
            if path.is_symlink():
                raise ValueError("project directory symlink is not allowed")
        for filename in files:
            path = root_path / filename
            if path.is_symlink():
                raise ValueError("project file symlink is not allowed")
            metadata = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("project entry is not a regular file")
            if metadata.st_nlink != 1:
                raise ValueError("project file hardlink is not allowed")
            real_path = path.resolve()
            if not str(real_path).startswith(str(project_real) + os.sep):
                raise ValueError("project file escapes the capsule")
            if review_real is not None and real_path == review_real:
                continue
            if rel_s == ".junie" or rel_s.startswith(".junie" + os.sep):
                continue
            if rel_s == "." and filename == "review.json":
                continue
            raise ValueError(f"unexpected project file: {rel_s}/{filename}")


def _review_dict_or_none(value: object) -> dict | None:
    """True review payload shape, else None."""
    if (
        isinstance(value, dict)
        and "summary" in value
        and "findings" in value
        and isinstance(value.get("findings"), list)
    ):
        return value
    return None


def _load_review_from_result(result: str) -> dict:
    """Parse junie's free-text ``result`` field into a review dict.

    Junie is not perfectly disciplined about the result string. Observed live
    shapes (same harness, same prompt):

    * bare JSON: ``{"summary":"...","findings":[]}``
    * fenced JSON: `` ```json ... ``` ``
    * partial markdown with embedded JSON: ``### Summary\\n- {...}``
      (without the full ### Changes / ### Verification trio)
    * full markdown with ``- No findings.`` under Summary

    Rejecting the partial-markdown form caused intermittent ``envelope
    refused`` / degraded retries even when llmUsage showed a successful
    model call. Scan for the first decodable JSON object with the review
    shape before giving up.
    """
    if len(result) > 32768:
        raise ValueError("junie result exceeds the normalization limit")
    direct_text = result.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(\{.*\})\s*```", direct_text, re.DOTALL
    )
    if fenced:
        direct_text = fenced.group(1)
    try:
        direct_review = json.loads(direct_text)
    except json.JSONDecodeError:
        direct_review = None
    got = _review_dict_or_none(direct_review)
    if got is not None:
        return got

    # Markdown form with ### Summary / ### Changes / ### Verification
    if all(
        heading in result
        for heading in ("### Summary", "### Changes", "### Verification")
    ):
        summary_payload = result.split("### Summary", 1)[1].split(
            "### Changes", 1
        )[0]
        embedded = re.fullmatch(
            r"\s*-\s*(\{.*\})\s*", summary_payload, re.DOTALL
        )
        if embedded:
            try:
                candidate = json.loads(embedded.group(1))
            except json.JSONDecodeError:
                candidate = None
            got = _review_dict_or_none(candidate)
            if got is not None:
                return got
        # Clean review: "- No findings." under Summary
        if re.search(r"-\s*No findings\.", summary_payload):
            return {
                "summary": "No findings.",
                "findings": [],
            }

    # Partial markdown / prose with an embedded review object somewhere in
    # the string (live: "### Summary\n- {\"summary\":\"...\",\"findings\":[]}").
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = result.find("{", idx)
        if start < 0:
            break
        try:
            candidate, end = decoder.raw_decode(result, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        got = _review_dict_or_none(candidate)
        if got is not None:
            return got
        idx = start + 1

    raise ValueError("junie result is not a review payload")


def normalize_envelope(
    envelope: dict,
    *,
    project: Path,
    capsule: Path,
    configured_model: str,
) -> dict:
    """Return a REVIEW_CONTRACT-shaped dict, or raise ValueError."""
    if not isinstance(envelope, dict):
        raise ValueError("envelope is not an object")
    if project.is_symlink() or capsule.is_symlink():
        raise ValueError("project or capsule path is a symlink")
    project_real = project.resolve()
    capsule_real = capsule.resolve()
    if not project.is_dir() or project_real != (capsule_real / "project"):
        raise ValueError("project directory escapes the capsule")

    review_path = project / "review.json"
    review_exists = review_path.is_file() and not review_path.is_symlink()
    _walk_project_for_trust(project, review_path)

    changes = envelope.get("changes", [] if not review_exists else None)
    if not isinstance(changes, list):
        raise ValueError("changes must be an array")
    if review_exists:
        if len(changes) != 1:
            raise ValueError("changes must contain only review.json")
        changed = changes[0]
        changed_path = (
            changed.get("afterRelativePath") if isinstance(changed, dict) else ""
        )
        if (project / str(changed_path)).resolve() != review_path.resolve():
            raise ValueError("changes contains a file other than review.json")
    elif changes:
        raise ValueError("changes must be empty when review.json is absent")

    usage_present = "llmUsage" in envelope
    usage = envelope.get("llmUsage", [])
    if not isinstance(usage, list):
        raise ValueError("llmUsage must be an array when present")
    if usage_present and not usage:
        raise ValueError("llmUsage must not be empty when present")
    if usage and not all(
        isinstance(item, dict)
        and isinstance(item.get("model"), str)
        and item["model"].strip()
        for item in usage
    ):
        raise ValueError("llmUsage entries must contain a non-empty model")
    models = [item["model"] for item in usage]
    lower_models = [m.lower() for m in models]
    if usage and not _model_evidenced(models, configured_model):
        raise ValueError("configured model usage is not evidenced")
    if any("gemini" in m or "grok" in m for m in lower_models):
        raise ValueError("upstream provider appears in junie usage")

    if review_exists:
        # open_confined_text compares abspath(path) against abspath(root). On
        # macOS, Path.resolve() rewrites /var -> /private/var while an
        # unresolved Path under tempfile.gettempdir() stays /var/folders/...;
        # mixing the two makes commonpath "/" and every legitimate review.json
        # looks like an escape. Both arguments must share one form — realpath.
        review_for_read = review_path.resolve()
        with open_confined_text(
            str(review_for_read), str(capsule_real), "junie review",
            errors="strict",
        ) as fh:
            review = json.load(fh)
        if not (
            isinstance(review, dict)
            and "summary" in review
            and "findings" in review
        ):
            raise ValueError("review.json is not a review payload")
        return {
            "summary": review["summary"],
            "findings": review["findings"],
        }

    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("junie result is missing")
    review = _load_review_from_result(result)
    return {"summary": review["summary"], "findings": review["findings"]}


def _default_spawner(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    stdin_path: str,
    stdout_path: str,
    stderr_path: str,
) -> int:
    with open(stdin_path, "rb") as stdin_f, open(
        stdout_path, "wb"
    ) as out_f, open(stderr_path, "wb") as err_f:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin_f,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
        )
        try:
            return int(proc.wait())
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            raise


def run_confined_junie(
    *,
    prompt_file: Path,
    binary: str,
    model: str,
    effort: str | None,
    timeout_ms: int,
    contract_schema: str,
    capsule_root: Path | None = None,
    spawner: Callable[..., int] | None = None,
    platform: str | None = None,
) -> tuple[int, bytes, bytes]:
    """Stage (unless given), spawn sandboxed junie, normalize → (rc, out, err)."""
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return (
            2,
            b"",
            b"junie confinement requires macOS; refusing unconfined run\n",
        )

    spawn = spawner or _default_spawner
    root: Path | None = None
    owns_root = capsule_root is None
    try:
        try:
            sandbox_exec = js.resolve_sandbox_exec()
        except RuntimeError as e:
            return 2, b"", f"{e}\n".encode("utf-8")

        prompt_bytes = Path(prompt_file).read_bytes()
        schema_hint = _SCHEMA_HINT + contract_schema
        if capsule_root is None:
            root = stage_capsule(prompt_bytes, schema_hint=schema_hint)
        else:
            root = Path(capsule_root)
            # Ensure prompt is staged when caller pre-created the capsule.
            inner_prompt = root / "capsule" / "prompt.txt"
            if not inner_prompt.is_file():
                root = stage_capsule(
                    prompt_bytes,
                    tmp_root=root.parent,
                    schema_hint=schema_hint,
                )
                owns_root = True

        inner = root / "capsule"
        project = inner / "project"
        cache = inner / "cache"
        extensions = inner / "extensions"
        config = inner / "config.json"
        prompt_in_capsule = inner / "prompt.txt"
        envelope_path = inner / "output.json"
        child_stdout = inner / "stdout.txt"
        child_stderr = inner / "stderr.txt"
        junie_home = inner / "junie-home"
        tmpdir = inner / "tmp"
        logs = inner / "logs"

        home = js.account_home()
        junie_data = str(Path(home) / ".local" / "share" / "junie")
        try:
            junie_data = js.require_managed_junie_data(
                junie_data, home, require_existing=False
            )
            abs_binary = binary
            if not os.path.isabs(abs_binary):
                # Resolve via PATH only for the absolute-path requirement of
                # resolve_junie_binary; tests inject absolute fake bins.
                which = shutil.which(binary)
                if which is None:
                    return (
                        127,
                        b"",
                        f"junie binary not found: {binary}\n".encode("utf-8"),
                    )
                abs_binary = which
            resolved_binary = js.resolve_junie_binary(abs_binary, junie_data)
        except ValueError as e:
            return 2, b"", f"{e}\n".encode("utf-8")

        try:
            profile = js.build_sandbox_profile(
                capsule=str(inner),
                binary=resolved_binary,
                junie_data=junie_data,
                home=home,
            )
        except ValueError as e:
            return 2, b"", f"{e}\n".encode("utf-8")

        profile_path = str(inner / "junie-filesystem.sb")
        try:
            js.write_profile(profile_path, profile)
        except OSError as e:
            return 2, b"", f"could not write sandbox profile: {e}\n".encode()

        env = js.build_sanitized_env(
            home=home,
            junie_home=str(junie_home),
            tmpdir=str(tmpdir),
            junie_data=junie_data,
            log_dir=str(logs),
        )
        junie_argv = js.build_junie_argv(
            binary=resolved_binary,
            output=str(envelope_path),
            project=str(project),
            model=model,
            timeout_ms=timeout_ms,
            cache=str(cache),
            config=str(config),
            extensions=str(extensions),
            effort=effort,
        )
        sandbox_argv = [sandbox_exec, "-f", profile_path, *junie_argv]

        try:
            rc = spawn(
                sandbox_argv,
                env=env,
                cwd=str(project),
                stdin_path=str(prompt_in_capsule),
                stdout_path=str(child_stdout),
                stderr_path=str(child_stderr),
            )
        except FileNotFoundError:
            return 127, b"", b"sandbox-exec or junie binary not found\n"
        except Exception as e:  # noqa: BLE001 - surface as unavailable
            return 2, b"", f"junie spawn failed: {e!r}\n".encode("utf-8")

        # Collect stderr for classify (confined). Path and root must share one
        # realpath form — see normalize_envelope's review.json open.
        inner_real = inner.resolve()
        err_bytes = b""
        try:
            with open_confined_text(
                str(child_stderr.resolve()), str(inner_real),
                "junie stderr", errors="replace",
            ) as fh:
                err_bytes = fh.read().encode("utf-8", "replace")
        except (ValueError, OSError):
            err_bytes = b"junie stderr capture escaped the isolated capsule\n"
            return 2, b"", err_bytes

        if rc != 0:
            return rc, b"", err_bytes or f"junie exited non-zero ({rc})\n".encode()

        if not envelope_path.is_file():
            return 2, b"", err_bytes + b"junie did not produce a JSON output envelope\n"

        try:
            with open_confined_text(
                str(envelope_path.resolve()), str(inner_real),
                "junie envelope", errors="replace",
            ) as fh:
                envelope = json.load(fh)
            payload = normalize_envelope(
                envelope,
                project=project,
                capsule=inner,
                configured_model=model,
            )
        except (ValueError, json.JSONDecodeError, OSError) as e:
            return 2, b"", err_bytes + f"junie envelope refused: {e}\n".encode()

        out = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        return 0, out, err_bytes
    finally:
        if owns_root and root is not None:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skodun.adapters.junie_runner")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-ms", type=int, required=True)
    parser.add_argument("--schema", required=True, help="contract JSON schema string")
    parser.add_argument("--effort", default="")
    args = parser.parse_args(argv)

    effort = args.effort if args.effort and args.effort != "none" else None
    rc, out, err = run_confined_junie(
        prompt_file=Path(args.prompt),
        binary=args.binary,
        model=args.model,
        effort=effort,
        timeout_ms=args.timeout_ms,
        contract_schema=args.schema,
    )
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
