import argparse
import os
from pathlib import Path

from . import __version__

_DEFAULT_DB = Path(".local/share/skodun/skodun.db")


def _store_path() -> Path:
    """`SKODUN_DB` if set, else the XDG-ish default under the home directory."""
    raw = os.environ.get("SKODUN_DB")
    return Path(raw) if raw else Path.home() / _DEFAULT_DB


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skodun")
    p.add_argument("--version", action="version", version=f"skodun {__version__}")
    sub = p.add_subparsers(dest="command")

    gate = sub.add_parser(
        "gate", help="fail closed unless a trustworthy review covers this change")
    gate.add_argument("--repo", type=Path, default=Path("."),
                      help="repository to gate (default: the current directory)")
    return p


def _cmd_gate(args) -> int:
    # Every failure inside this seam is exit 2, for the same reason every
    # failure inside `run_gate` is: an exception escaping here would leave the
    # interpreter's own exit code of 1, and 1 is the one value that means
    # "findings remain open". Setup failures -- an unparseable config, an
    # unopenable store -- happen strictly before any review is consulted, so
    # reporting them as findings would be a lie in the dangerous direction.
    try:
        from .config import load_config
        from .gate import run_gate
        from .store import Store

        repo = Path(args.repo)
        cfg = load_config(repo)
        store = Store.open(_store_path())
        result = run_gate(store, repo, cfg)
    except BaseException as e:
        print(f"SKODUN GATE: FAIL(2) could not run the gate: {e!r}")
        return 2
    print(result.message)
    return result.code


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse --version exits 0
        return int(e.code or 0)
    if args.command == "gate":
        return _cmd_gate(args)
    return 0


def entry() -> None:
    raise SystemExit(main())
