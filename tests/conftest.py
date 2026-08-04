"""Suite-wide fixtures. The only thing here is what the suite must NOT read.

Every `SKODUN_*` variable is a switch on the code under test, and skodun is
developed on machines that also RUN it, so the shell that starts pytest can
easily have several of them exported. `_no_ambient_skodun_env` below removes
them all before each test, so the suite is a statement about the shipped
defaults rather than about one developer's environment. See
`tests/test_env_isolation.py` for what this cost when it was missing.
"""

import os
from pathlib import Path

import pytest

#: The only `SKODUN_*` variables a test may inherit from the outside.
#:
#: `SKODUN_ORACLE_DIR` is an INPUT to the suite rather than a switch on the
#: code: it names the porting-oracle checkout `oracle_dir` looks for, and the
#: handful of tests that read it are skipped when it is absent. Everything
#: else configures skodun, and a test that wants one sets it itself -- which
#: is exactly what the per-module `_isolate` fixtures already do for
#: `SKODUN_DB`, `SKODUN_CONFIG` and the `SKODUN_*_BIN` overrides.
INHERITED_ENV = frozenset({"SKODUN_ORACLE_DIR"})


@pytest.fixture(autouse=True)
def _no_ambient_skodun_env(monkeypatch):
    """Delete every non-allowlisted `SKODUN_*` variable, for every test.

    Autouse in the ROOT conftest, so it runs before a module's own autouse
    fixture and before the test body: it can only clear what the shell
    exported, never what a test sets for itself.

    `monkeypatch.delenv` mutates `os.environ`, which is what makes this reach
    the subprocesses the suite spawns -- the store's ResourceWarning sweep
    re-runs whole modules through `subprocess.run` and asserts they exit 0, so
    an ambient variable that fails a test in-process fails the sweep too.
    """
    for name in [n for n in os.environ
                 if n.startswith("SKODUN_") and n not in INHERITED_ENV]:
        monkeypatch.delenv(name, raising=False)


def oracle_dir() -> Path | None:
    """Path to the porting-oracle checkout from $SKODUN_ORACLE_DIR, or None."""
    raw = os.environ.get("SKODUN_ORACLE_DIR")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None
