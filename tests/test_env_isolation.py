"""The suite's own environment, pinned.

Every `SKODUN_*` variable is a switch on the code under test — a capacity
ceiling, a pass toggle, a routing mode, a gate bypass — and an operator who
runs skodun on the machine they develop it on has several of them exported.
Without a scrub, `python3 -m pytest` runs against *that* shell rather than
against a known one, and the suite stops being a statement about the shipped
defaults.

That is not hypothetical. With `SKODUN_PROVIDER_MAX_IN_FLIGHT=2` exported —
an ordinary operator setting — four tests failed on `main`: the three routing
tests that take one provider slot and assert the provider is now busy (with
two slots configured it is not), plus the ResourceWarning sweep, whose
subprocess inherits the same environment and asserts a clean exit.

The direction that costs more is the other one: an ambient `SKODUN_GATE_SKIP`,
`SKODUN_IGNORE_PROVIDER_STATE` or `SKODUN_SKEPTIC_PASS` can turn a test GREEN
while the path it claims to pin is not the path that ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import INHERITED_ENV


def _ambient() -> set[str]:
    return {n for n in os.environ if n.startswith("SKODUN_")}


def test_no_ambient_skodun_variable_reaches_a_test():
    """The scrub itself. Fails on the developer's own shell without it."""
    assert _ambient() - set(INHERITED_ENV) == set()


def test_the_allowlist_is_exactly_the_one_variable_the_suite_reads_outside_in():
    """Widening the allowlist has to be a deliberate edit to this line.

    `SKODUN_ORACLE_DIR` is the one variable that is an INPUT to the suite
    rather than a switch on the code: it names the porting-oracle checkout
    that `tests/conftest.oracle_dir` looks for, and the tests that use it are
    skipped when it is absent. Everything else configures skodun, and a test
    that wants one sets it itself.
    """
    assert set(INHERITED_ENV) == {"SKODUN_ORACLE_DIR"}


def test_a_test_can_still_set_the_variables_it_needs(monkeypatch):
    """The scrub runs BEFORE the test body and before a module's own autouse
    fixture, so it clears the shell's value and never a test's own."""
    monkeypatch.setenv("SKODUN_PROVIDER_MAX_IN_FLIGHT", "7")
    from skodun import capacity

    assert capacity.provider_max_in_flight_from_env() == 7


def test_the_shipped_default_is_what_an_unset_variable_gives():
    """The assertion the routing tests actually depend on, stated once here so
    a regression in the scrub is diagnosed here rather than as three confusing
    `auto:free` != `auto:wait` failures."""
    from skodun import capacity

    assert capacity.provider_max_in_flight_from_env() == 1
    assert capacity.DEFAULT_PROVIDER_MAX_IN_FLIGHT == 1


def test_a_child_process_inherits_the_scrubbed_environment():
    """`monkeypatch.delenv` mutates `os.environ`, so the scrub reaches the
    subprocesses the suite spawns — which is what the store's
    ResourceWarning sweep needs, since it re-runs whole modules through
    `subprocess.run` and asserts they exit 0."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import json,os;print(json.dumps(sorted(n for n in os.environ "
         "if n.startswith('SKODUN_'))))"],
        capture_output=True, text=True, check=True).stdout
    assert set(json.loads(out)) <= set(INHERITED_ENV)


#: A variable no production code reads, injected into a child pytest run so
#: the three assertions above have something to be wrong about.
_PROBE = "SKODUN_ENV_ISOLATION_PROBE"

#: The tests re-run inside that child. Not this module wholesale: the spawning
#: test is in it, and a child that spawned its own child would recurse.
_UNDER_PROBE = (
    "test_no_ambient_skodun_variable_reaches_a_test",
    "test_the_shipped_default_is_what_an_unset_variable_gives",
    "test_a_child_process_inherits_the_scrubbed_environment",
)


def test_the_scrub_is_what_makes_the_assertions_above_pass():
    """The three tests above are VACUOUS on a clean environment.

    That is the whole difficulty of testing a scrub: on CI, where nothing is
    exported, `_ambient()` is empty and `provider_max_in_flight_from_env()` is
    1 whether or not the fixture exists — so deleting `_no_ambient_skodun_env`
    would leave this file green in exactly the place it is supposed to fail.
    The bug it guards against was only ever visible on a machine that had the
    variable set, which is the one place nobody runs the suite to check a
    fixture.

    So this test supplies the missing environment itself: a child `pytest`
    with `SKODUN_ENV_ISOLATION_PROBE` exported, running those three by name.
    They pass in that child only because the root fixture removed it. Remove
    the fixture and this test fails on any machine, CI included.
    """
    env = dict(os.environ) | {_PROBE: "1"}
    here = Path(__file__).resolve()
    root = here.parent.parent

    # The control, and it is not ceremony: if the probe never reached the
    # child, the run below would pass for the wrong reason and this test would
    # be as vacuous as the ones it exists to back up.
    reached = subprocess.run(
        [sys.executable, "-c", f"import os;print({_PROBE!r} in os.environ)"],
        env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert reached == "True", "the probe never reached a child process"

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *(f"{here}::{name}" for name in _UNDER_PROBE)],
        cwd=root, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
