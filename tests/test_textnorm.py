"""Parity tests for the finding/ledger key definitions.

These keys must match the legacy `grok_review_triage.py` (the porting oracle)
byte for byte, or dismissals recorded before migration stop applying and
previously-triaged findings resurface. `test_parity_with_legacy_module` and
`test_ledger_key_parity_with_legacy_module` load the *actual* legacy module
from `$SKODUN_ORACLE_DIR` and assert equality directly -- they are not a
reimplementation double-checking itself.

The oracle's public `finding_key` takes a finding **dict** (`finding["file"]`,
`finding["title"]`), while skodun's takes `(file, title)` directly (see
docstring in `skodun/textnorm.py`). The parity test below adapts to that by
wrapping the args in a dict before calling the legacy function; the assertion
itself -- that the two key values are equal -- is unchanged.
"""

import importlib.util
import sys

import pytest

from skodun.textnorm import collapse_ws, finding_key, ledger_key, norm

from tests.conftest import oracle_dir

LEGACY = (oracle_dir() / "scripts" / "grok_review_triage.py") if oracle_dir() else None


def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_triage", LEGACY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_triage"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_key_shape():
    k = finding_key("src/Foo.scala", "  Missing NULL check ")
    assert len(k) == 16 and int(k, 16) >= 0
    assert finding_key("src/foo.scala", "missing null check") == k  # normalized


def test_norm_basic():
    assert norm("  Hello   World  ") == "hello world"
    assert norm("\tTabs\nand\r\nnewlines\t") == "tabs and newlines"
    assert norm(None) == ""
    assert norm("") == ""
    assert norm("MiXeD CaSe") == "mixed case"


def test_collapse_ws_basic():
    assert collapse_ws("  Hello   World  ") == "Hello World"
    assert collapse_ws("\tTabs\nand\r\nnewlines\t") == "Tabs and newlines"
    assert collapse_ws("  leading and trailing  ") == "leading and trailing"
    assert collapse_ws("") == ""
    assert collapse_ws(None) == ""


def test_collapse_is_the_pre_lowercase_half_of_norm():
    # `collapse_ws` exists only because `triage.validate_reason` must measure
    # its length floor before case folding. It must stay the exact
    # pre-lowercase half of the single `norm` definition here -- if the two
    # ever drift, the placeholder lookup and the length check stop describing
    # the same string.
    for s in ["", "   ", "a b", "  FALSE   POSITIVE  ", "İ" * 3, "Straße",
              "a\t\nb  c", None, 42]:
        assert norm(s) == collapse_ws(s).lower(), repr(s)


def test_finding_key_excludes_line():
    # finding_key's signature has no line parameter at all -- callers cannot
    # even pass one, so drift in the reported line can never change the key.
    assert finding_key("a.py", "bug") == finding_key("a.py", "bug")


def test_ledger_key_shape():
    fkey = finding_key("a.py", "bug")
    lk = ledger_key("feature/x", "abc123", fkey)
    assert lk == "feature/x\x00abc123\x00" + fkey


def test_parity_with_legacy_module():
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    cases = [
        ("src/A.scala", "NPE in handler"),
        ("ui/x.ts", "  race   condition  IN effect "),
        ("db/чейндж.xml", "unicode Title ✓"),
        # adversarial: leading/trailing whitespace
        ("  leading/trailing.py  ", "  leading and trailing whitespace  "),
        # adversarial: tabs and newlines inside the title
        ("tabs\tand\nnewlines.py", "title\twith\ntabs and\nnewlines"),
        # adversarial: mixed case
        ("MiXeD/Case.PY", "MiXeD CaSe Title"),
        # adversarial: empty strings
        ("", ""),
        # adversarial: a None field
        (None, "title with none file"),
        ("file/with/none/title.py", None),
    ]
    for f, t in cases:
        assert finding_key(f, t) == legacy.finding_key({"file": f, "title": t})


def test_ledger_key_parity_with_legacy_module():
    if LEGACY is None or not LEGACY.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()

    fkey = finding_key("src/A.scala", "NPE in handler")
    cases = [
        ("main", "abc123"),
        ("  Feature/X  ", "  DEF456  "),
        (None, "abc"),
        ("feature/tabs\t", "\nbase-sha\n"),
    ]
    for branch, base_sha in cases:
        assert ledger_key(branch, base_sha, fkey) == legacy.ledger_key(branch, base_sha, fkey)
