"""Content-identity keys for the review triage ledger.

`norm`, `finding_key`, and `ledger_key` must match the legacy triage
implementation (`grok_review_triage.py`, the porting oracle) byte for byte:
dismissals already recorded by the legacy tool must keep resolving to the
same key after migration, or every previously-triaged finding resurfaces as
new. `tests/test_textnorm.py` pins this by running the real oracle module
against the same inputs.

`norm` has exactly one definition, here. Every later module imports it and
never re-derives the transformation.
"""

from __future__ import annotations

import hashlib
import re


def collapse_ws(s) -> str:
    r"""Whitespace-collapsed but NOT lowercased form of `s`.

    This is the pre-lowercase half of `norm`: `norm(s) == collapse_ws(s).lower()`
    holds for every input. It exists as its own public function because
    `triage.validate_reason` measures a dismissal reason's length floor on
    this collapsed-but-not-lowercased form, while matching placeholder
    reasons on the fully normalized (lowercased) form -- the two checks are
    NOT interchangeable, since `str.lower()` can *lengthen* a string (U+0130
    lowercases to two codepoints), so measuring the length on the lowercased
    form would let a 10-character reason clear a 20-character floor.
    """
    return re.sub(r"\s+", " ", str(s or "")).strip()


def norm(s) -> str:
    r"""Lowercase, whitespace-collapsed form used for both keys and validation.

    PARITY-CRITICAL: copied verbatim from the oracle's `_norm`
    (grok_review_triage.py:70-72)::

        def _norm(text):
            return re.sub(r"\s+", " ", str(text or "")).strip().lower()
    """
    return collapse_ws(s).lower()


def finding_key(file: str, title: str) -> str:
    r"""Stable content key for a finding: file + title, deliberately NOT line.

    Line numbers drift by a hunk or two between review rounds while the
    finding stays the same claim about the same code; keying on the line
    would make a dismissal evaporate on the next round.

    PARITY-CRITICAL: the oracle's `finding_key` (grok_review_triage.py:75-90)
    takes a finding dict and reads `finding["file"]` / `finding["title"]`;
    this port takes the two values directly, since by the time skodun builds
    a key the caller already holds them as separate fields. The construction
    itself is byte-for-byte identical to the oracle's::

        raw = _norm(finding["file"]) + "\0" + _norm(finding["title"])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    See tests/test_textnorm.py::test_parity_with_legacy_module, which calls
    the real oracle module with `{"file": file, "title": title}` and asserts
    equality.
    """
    raw = norm(file) + "\0" + norm(title)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def ledger_key(branch: str, base_sha: str, fkey: str) -> str:
    r"""Dismissal scope key: one branch AND one merge-base AND one finding.

    Keying on file+title alone would make a dismissal permanent and global;
    including the branch and merge-base bounds the amnesty to one review
    loop, so a rebase onto a new base re-opens every dismissal for a fresh
    judgement.

    PARITY-CRITICAL: byte-for-byte match of the oracle's `ledger_key`
    (grok_review_triage.py:114-135)::

        def ledger_key(branch, base_sha, key):
            return "%s\0%s\0%s" % (_norm(branch), _norm(base_sha), key)
    """
    return "\0".join((norm(branch), norm(base_sha), fkey))
