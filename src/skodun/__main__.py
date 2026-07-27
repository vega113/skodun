"""`python -m skodun` -- the same entry point as the `skodun` console script.

A fail-closed component must not have an invocation form that runs nothing and
exits 0, so every way of starting skodun goes through `cli.main()` and returns
its code unchanged. `python -m skodun.cli` is covered by the `__main__` guard
in `cli.py` for the same reason.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
