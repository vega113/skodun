import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skodun")
    p.add_argument("--version", action="version", version=f"skodun {__version__}")
    p.add_subparsers(dest="command")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        build_parser().parse_args(argv)
    except SystemExit as e:  # argparse --version exits 0
        return int(e.code or 0)
    return 0


def entry() -> None:
    raise SystemExit(main())
