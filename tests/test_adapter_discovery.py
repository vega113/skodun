"""Adapter coverage must work before full-suite collection imports providers."""

import subprocess
import sys
from pathlib import Path


def test_conformance_registry_discovers_every_adapter_in_a_fresh_process():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'src'); "
         "from tests.adapter_conformance import "
         "test_every_registered_adapter_has_conformance_coverage as check; check()"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
