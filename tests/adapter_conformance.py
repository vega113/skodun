"""The registration gate: what every adapter must prove before it may ship.

This module is a MIXIN, not a test module — its filename deliberately does not
match pytest's `test_*.py` pattern, so nothing here is collected on its own.
`AdapterConformance` has no adapter bound to it and would fail every rule if it
were; it becomes live only when an adapter's own test module subclasses it.

An adapter that cannot recognise its CLI failing is worse than no adapter: a
provider that silently returns nothing looks exactly like a clean review, and a
clean review is what the gate exits 0 on. So the suite below is not a courtesy
check on a new adapter — it is the condition for being in `_REGISTRY` at all,
and `test_every_registered_adapter_has_conformance_coverage` makes that
mechanical rather than a matter of reviewer diligence.

Adding an adapter
-----------------

In `tests/test_adapter_<name>.py`::

    from tests.adapter_conformance import (  # noqa: F401 - collected below
        AdapterConformance,
        test_coverage_gate_fails_without_a_conformance_subclass,
        test_every_registered_adapter_has_conformance_coverage,
        test_load_fixture_rejects_a_malformed_rc,
    )

    class TestAcmeConformance(AdapterConformance):
        provider_id = "acme"                    # the `_REGISTRY` key
        fixture_dir = Path(__file__).parent / "fixtures" / "adapters" / "acme"

        def adapter(self):
            return AcmeAdapter()

        def effort_reject_case(self):
            r = Reviewer(name="f", provider="acme", model="acme-mini",
                         role="finder", effort="max")
            return r, "effort"          # or None: see `effort_reject_case`

The three imported `test_*` functions are NOT decorative and NOT re-declared
per adapter: importing them into a collected module is what makes pytest run
them at all, since this module is deliberately not collected (see above). They
are the registry coverage gate, its own self-proof, and the fixture loader's
contract — defined once, next to the suite they gate, so they cannot drift
per adapter. Dropping them from the import silently removes the gate.

The class name MUST start with `Test`, or pytest never collects it and the
coverage gate — which checks exactly that — fails.

Then supply the fixture files described under "Fixture file format" below. The
required set is fixed, because each rule needs a witness and a rule with no
witness proves nothing:

===========================  ============================================
`*healthy*`                  rules 1c, 2, 6 (>= 1; rule 6 needs one whose
                             findings carry a JSON-clean `title`)
`*degraded*`                 rule 3 (>= 2, and one of them `*_stderr*`)
`*unavailable*`              rule 4 (>= 1, each with a `category=` line;
                             rule 6's failed-run half additionally needs at
                             least one whose STDERR carries the wording, so
                             a set in which every unavailable capture hides
                             its evidence inside the stream is not enough)
`*unavailable_quota*`        rule 4 (>= 1, and see below)
`*healthy_noisy_stderr*`     rule 7 (>= 1)
`*refuter*healthy*`          rule 1b (>= 1)
===========================  ============================================

Why rule 4 requires a `quota` fixture specifically
--------------------------------------------------

`quota` is the only provider-wide-cacheable category, so it is the one whose
misdetection has blast radius beyond a single attempt — and Task 14's live
acceptance run found exactly that defect: every adapter satisfied rule 4 with
an `auth` or `model` fixture, no adapter had a `quota` capture, and real xAI
budget exhaustion (`402 Payment Required ... usage balance exhausted`) matched
none of the shipped quota signals. `classify` returned `ok`, the fallback chain
never advanced, and nothing was cached.

The rule is therefore mechanical: every adapter must ship a
`*unavailable_quota*` fixture, the same way every adapter must ship a
`*refuter*healthy*` one.

There is a real objection to this, and it is recorded rather than dropped,
because it says what the rule can and cannot buy. A quota failure cannot be
produced on demand: it needs a real account with a real balance to run out, at
a moment nobody schedules. So most adapters will satisfy this rule by
synthesizing — and a synthesized quota fixture whose wording its author
*imagined* asserts only that the author's guess matches the signal table that
same author wrote. That circle passes cleanly while the provider's actual
wording goes on matching nothing, which is the Task 14 defect wearing a green
tick.

What closes the circle is WHERE the wording comes from, so that is the part
the rule constrains:

    A synthesized `unavailable_quota` fixture must be built from wording that
    is verifiably the provider's own — a string in the installed binary, or
    the provider's documented error text — never from wording invented to
    match the table. Record which it is, per fixture, in the fixture
    directory's README, and say plainly whether the envelope AROUND that
    wording was captured or assembled.

That is a weaker guarantee than a live capture and is not presented as an
equal one: it proves the table matches a sentence the CLI can really emit,
not that the CLI emits that sentence on budget exhaustion specifically. It is
strictly stronger than no witness at all, which is what the previous version
of this rule left in place. The standing obligation survives the rule rather
than being replaced by it:

    When a live quota failure DOES occur for your provider, capture it,
    sanitize it, and REPLACE the synthesized fixture with the real envelope.

`tests/fixtures/adapters/xai/unavailable_quota.txt` is a real live capture and
is the model for what the other two should eventually become. `openai` and
`google` currently hold binary-sourced syntheses; their READMEs say so, and
the per-adapter "every quota signal is individually load-bearing" tests are
what keep the tables from growing entries that fire on nothing.

Fixture file format
-------------------

A fixture is one captured (or, where the archive cannot supply it, synthesized)
run::

    rc=0
    category=auth              # optional; REQUIRED on every *unavailable* file
    --- stdout ---
    <raw bytes>
    --- stderr ---
    <raw bytes>

`rc=` is an ASCII integer, optionally negated, and is the only required
header. `category=` is the `ClassifyResult.category` the adapter is expected
to return, and the vocabulary is CLOSED — exactly one of::

    quota    the provider is out of budget. The ONLY category that is a
             property of the provider as a whole, and therefore the only one
             a caller may remember beyond a single attempt: mislabel
             something else as `quota` and a healthy provider drops out of
             every later fallback chain in the run.
    auth     credentials missing, expired or refused. Attempt-local.
    binary   the CLI is not installed or not on PATH (rc 127). This is the
             reviewer's configuration being wrong, never a provider outage.
    model    the requested model id is unknown to the CLI. Attempt-local.
    other    unavailable for a reason none of the above names. Deliberately
             last: reach for it only when the stderr genuinely says nothing
             more specific, because it caches nothing and explains nothing.

Anything else fails rule 4 — a fixture may not invent a category, and the
adapter's answer must equal the declared one exactly.

Both section markers are mandatory even when a section is empty: an absent
`--- stderr ---` is far more likely to be a typo than an intention, and
defaulting it to `b""` would quietly weaken whichever rule the fixture exists
to witness. Each section's content is the bytes between its marker line and the
next marker line (or EOF), minus exactly one trailing newline — the separator
every text file ends with. Everything else is byte-faithful, which is the whole
point of capturing rather than hand-writing envelopes.

Fixtures captured from real runs must be sanitized before they are committed:
no tokens, no usernames, no machine paths, no upstream-project names inside the
bytes. Record capture-vs-synthesized provenance per fixture in the fixture
directory's `README`.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from skodun.adapters import (
    _REGISTRY,
    REFUTER_CONTRACT,
    REVIEW_CONTRACT,
    UNAVAILABLE_RC,
    Adapter,
    ClassifyResult,
    OutputContract,
)
from skodun.config import EFFORTS, Defaults, Reviewer

# `tests.test_adapter_codex` already imports this the same way for its own
# decode-site regression test — reused here rather than duplicated. See its
# one use in `_GARBAGE` below for why.
from tests.test_adapter_base import _recursion_bomb

# The complete `ClassifyResult.category` vocabulary. Spelled here rather than
# imported so that a category quietly added to `base` still has to be argued
# for HERE, where the cacheability consequences live: only `quota` is a
# property of the provider as a whole and may be remembered beyond one attempt.
_UNAVAILABLE_CATEGORIES = frozenset({"quota", "auth", "binary", "model", "other"})

_KINDS = frozenset({"ok", "degraded", "unavailable"})

# The canonical effort value that means "do not pass the flag at all". It is a
# member of `config.EFFORTS` but is deliberately NOT expected in any adapter's
# `effort_map`: it is the user's explicit opt-out, handled before any lookup
# happens, not a CLI spelling. See `base.Adapter.effort_map`.
_EFFORT_OPT_OUT = "none"

# Deep-enough JSON nesting to defeat the decoder with a `RecursionError`
# rather than a `ValueError` — the exact defect `base._DECODE_FAILURES`
# exists to guard, and the one entry `_GARBAGE` below needs so that a Task
# 6/10 adapter with its own decode site (as codex's `_events` has) cannot
# pass this registration gate with no `RecursionError` guard at all: removing
# `RecursionError` from `base._DECODE_FAILURES` fails zero conformance-mixin
# tests without it. `_recursion_bomb` itself is imported, not duplicated —
# see the top of this file.

# Inputs no adapter may raise on. `b"{"` is a truncated object (a real
# mid-write capture), `b"\x00\xff" * 512` is invalid UTF-8 (which is what a
# decoder-level crash needs), `b"[]"` is valid JSON of the wrong shape — the
# case a `json.loads(...)["findings"]` would die on — and `_recursion_bomb()`
# (built once, here, at import time: `_GARBAGE` is exercised across every
# contract and rc value the mixin tries, and probing per-case would slow an
# already quadratic scan for no reason) is the deep-nesting bomb above.
#
# The last entry is ELIGIBLE but INVALID: `_review_eligible` accepts it (it has
# both keys), `_valid_payload` rejects it (`summary` is not a string). Without
# such an input, rule 1's `payload is None` clause is satisfied by extraction
# finding nothing at all, and an adapter that returned its payload regardless of
# `parse_ok` would pass the whole suite. It discriminates only for adapters
# whose extractor scans raw stdout; the wire-format-agnostic version of the same
# assertion is `test_no_payload_survives_a_failed_validation` below.
_GARBAGE: tuple[bytes, ...] = (b"", b"{", b"\x00\xff" * 512, b"[]",
                               b'{"summary": 5, "findings": []}',
                               _recursion_bomb())

# Two probes over the REVIEW contract's own eligibility rule, differing only in
# `validate`. Together they pin "payload is gated by `parse_ok`, not merely by
# extraction" for ANY adapter, whatever its wire format: the permissive one
# proves an eligible envelope really is extracted from a given capture, the
# refusing one then proves that a payload the contract rejected is not handed
# on. Neither is a contract any adapter ships — they exist to make the negative
# assertion non-vacuous, which a literal malformed payload cannot do for an
# adapter whose extractor only reads its own event envelopes.
_PROBE_ANY_VALID = OutputContract("review-probe", REVIEW_CONTRACT.json_schema,
                                  REVIEW_CONTRACT.eligible, lambda obj: True)
_PROBE_NONE_VALID = OutputContract("review-probe", REVIEW_CONTRACT.json_schema,
                                   REVIEW_CONTRACT.eligible, lambda obj: False)

# Both contracts, every time. An adapter that only ever gets exercised on the
# review shape breaks at Task 8 runtime, in the refuter pass, on a real review.
_CONTRACTS: tuple[OutputContract, ...] = (REVIEW_CONTRACT, REFUTER_CONTRACT)

_STDOUT_MARKER = b"--- stdout ---"
_STDERR_MARKER = b"--- stderr ---"

# The only spelling of `rc=` a fixture may use: ASCII digits, optionally
# negated. One spelling per value, so two fixtures cannot declare the same exit
# code in two ways. See `load_fixture`.
_RC = re.compile(r"-?[0-9]+")


# --------------------------------------------------------------------------
# the fixture file format
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """One recorded run: what the CLI exited with, said, and complained about.

    `category` is the EXPECTED `ClassifyResult.category`, declared by the
    fixture author rather than read back from the adapter — a conformance
    suite that asked the adapter what it meant would agree with it by
    construction.
    """

    name: str
    path: Path
    rc: int
    category: str | None
    stdout: bytes
    stderr: bytes


def _strip_one_newline(blob: bytes) -> bytes:
    return blob[:-1] if blob.endswith(b"\n") else blob


def load_fixture(path: Path) -> Fixture:
    """Parse one fixture file. Loud on any malformation.

    Every failure here is an `AssertionError` naming the file: a fixture that
    silently loads as empty would let the rule it witnesses pass vacuously,
    which is the one outcome this whole module exists to prevent.
    """
    raw = path.read_bytes()
    head, sep, rest = raw.partition(_STDOUT_MARKER + b"\n")
    assert sep, f"{path}: no {_STDOUT_MARKER.decode()!r} section marker"
    body, sep, err = rest.partition(b"\n" + _STDERR_MARKER + b"\n")
    assert sep, (
        f"{path}: no {_STDERR_MARKER.decode()!r} section marker — both "
        f"sections are mandatory even when one is empty")

    rc: int | None = None
    category: str | None = None
    for lineno, line in enumerate(head.decode("utf-8").splitlines(), start=1):
        key, eq, value = line.partition("=")
        assert eq, f"{path}:{lineno}: header line is not `key=value`: {line!r}"
        if key == "rc":
            assert rc is None, f"{path}:{lineno}: duplicate `rc=`"
            # ASCII, exactly one optional sign, and nothing `int()` would take
            # that a reader would not: `str.isdigit()` is true of `１` and of
            # every other Unicode decimal, and `lstrip("-")` turns `--1` into
            # something it approves of and `int()` then rejects — as a bare
            # `ValueError` that names no file, breaking this loader's one
            # promise. Match first, convert second, and the conversion can no
            # longer fail.
            assert _RC.fullmatch(value.strip()), (
                f"{path}:{lineno}: rc must be an ASCII integer "
                f"(optionally negated), got {value!r}")
            rc = int(value.strip())
        elif key == "category":
            assert category is None, f"{path}:{lineno}: duplicate `category=`"
            category = value.strip()
        else:
            raise AssertionError(
                f"{path}:{lineno}: unknown header key {key!r} "
                f"(known: rc, category)")
    assert rc is not None, f"{path}: missing the required first line `rc=<int>`"
    return Fixture(name=path.stem, path=path, rc=rc, category=category,
                   stdout=body, stderr=_strip_one_newline(err))


def test_load_fixture_rejects_a_malformed_rc(tmp_path):
    """A bad `rc=` line fails as an AssertionError that NAMES the file.

    Imported into each adapter's test module alongside the coverage gate, for
    the same reason: the loader is what stops a fixture from loading as
    something it is not, and `load_fixture`'s own docstring promises that every
    failure names the path. A bare `ValueError: invalid literal for int()`
    keeps that promise for nobody — the author of a 400-line captured envelope
    is left grepping six files for the typo.

    The accepted spellings are ASCII and exact. `１` (FULLWIDTH DIGIT ONE) is
    the sharp case: `int()` takes it happily, so a lenient loader would read a
    header nobody can see is odd and report no problem at all.
    """
    written_count = 0

    def written(header: str) -> Path:
        nonlocal written_count
        written_count += 1
        # A distinct file per case, and the name says which: the assertion
        # below checks the path is IN the message, so two cases sharing a name
        # could pass on each other's failure.
        p = tmp_path / f"case_{written_count:02d}.txt"
        p.write_bytes(header.encode("utf-8")
                      + b"\n--- stdout ---\n\n--- stderr ---\n")
        return p

    for bad in ("rc=--1", "rc=１", "rc=+1", "rc=", "rc=1 2", "rc=0x10",
                "rc=nan"):
        path = written(bad)
        with pytest.raises(AssertionError) as excinfo:
            load_fixture(path)
        assert str(path) in str(excinfo.value), (
            f"{bad!r} failed without naming the fixture file: "
            f"{excinfo.value}")

    # …and the spellings a real fixture uses still load.
    assert load_fixture(written("rc=0")).rc == 0
    assert load_fixture(written("rc=127")).rc == 127
    assert load_fixture(written("rc=-1")).rc == -1
    assert load_fixture(written("rc= 2 ")).rc == 2


# --------------------------------------------------------------------------
# fixture selectors — deliberately explicit, never incidental
# --------------------------------------------------------------------------
#
# Each rule below picks its witnesses by NAME. The selectors are written so
# that the refuter exclusion is a stated rule rather than a side effect of how
# the files happen to sort: `refuter_healthy` is a healthy REFUTER envelope,
# and asking the review contract to parse it is precisely the assertion rule 1b
# makes in the negative direction. Selecting it under rule 2 as well would turn
# that into a contradiction.


def _is_refuter(name: str) -> bool:
    """Any fixture whose name mentions the refuter at all.

    Substring, not the `refuter_*` prefix the plan spells: a future
    `healthy_refuter_noisy` must not leak into the review selector because of
    where in the name the word landed.
    """
    return "refuter" in name


def _is_healthy(name: str) -> bool:
    """A healthy REVIEW fixture: rules 2 and 6. Refuter fixtures excluded."""
    return "healthy" in name and not _is_refuter(name)


def _is_degraded(name: str) -> bool:
    return "degraded" in name


def _is_unavailable(name: str) -> bool:
    return "unavailable" in name


def _is_unavailable_quota(name: str) -> bool:
    """Rule 4's mandatory `quota` witness.

    Substring rather than an exact filename, for the same reason
    `_is_refuter` is: a future `unavailable_quota_402` or
    `unavailable_quota_rate_limited` is the same witness and must count.
    """
    return _is_unavailable(name) and "quota" in name


def _is_noisy_healthy(name: str) -> bool:
    """Rule 7's witness: a healthy run whose stderr carries a false alarm."""
    return _is_healthy(name) and "noisy_stderr" in name


def _is_refuter_healthy(name: str) -> bool:
    return _is_refuter(name) and "healthy" in name


def _is_degraded_stderr(name: str) -> bool:
    """Rule 6's SOURCE of signal words: the adapter's own harness tells."""
    return _is_degraded(name) and "stderr" in name


# Rule 6's failed-run half asks "is `signal` alone, with no payload, a REAL
# unavailability tell for this adapter?" It must ask that at a FIXED rc,
# never at the fixture's own `rc`: an `*unavailable*` fixture may (and
# naturally does — it is the single most obvious first `*unavailable*`
# fixture an adapter author writes) declare `rc=127`, and every registered
# adapter's `classify` returns `unavailable/binary` at rc 127 BEFORE it ever
# looks at stderr (see `test_missing_binary_is_unavailable_binary`). Probing
# with the fixture's own rc would then certify even completely inert stderr
# as "a real tell" whenever the fixture happens to carry rc=127, and the
# splice loop below would run on a signal that proves nothing. `0` matches
# one of the rc values the splice loop itself probes at.
_PREMISE_PROBE_RC = 0


# --------------------------------------------------------------------------
# the mixin
# --------------------------------------------------------------------------


class AdapterConformance:
    """Every rule an adapter must pass to be allowed into `_REGISTRY`.

    Subclasses supply four things and nothing else:

    * `provider_id` — the `_REGISTRY` key this class claims coverage for. The
      coverage gate reads it; it is not decorative.
    * `fixture_dir` — where this adapter's captured runs live.
    * `adapter()` — a fresh adapter instance.
    * `effort_reject_case()` — see its docstring.
    """

    #: The `skodun.adapters._REGISTRY` key this subclass proves coverage for.
    provider_id: str = ""

    #: Directory of fixture files, in the format documented at module level.
    fixture_dir: Path | None = None

    # ---- extension points -------------------------------------------------

    def adapter(self) -> Adapter:
        """A fresh instance of the adapter under test."""
        raise NotImplementedError(
            "conformance subclasses must implement `adapter()`")

    def effort_reject_case(self) -> tuple[Reviewer, str] | None:
        """One `Reviewer` whose effort this adapter must LOUDLY reject, or None.

        Return `(reviewer, expected_message_regex)` when the adapter has a
        configuration it refuses — a model that does not take an effort flag,
        an effort its CLI has no spelling for. `build_cmd` must then raise
        `ValueError` whose message matches the regex (it is handed to
        `pytest.raises(match=...)`, so escape any metacharacters), because the
        alternative — dropping the flag — reviews at the CLI's own default
        effort and reports the result as the configured one.

        Return None to claim TOTAL effort support, which rule 5 then verifies
        against `config.EFFORTS` rather than taking on trust.
        """
        raise NotImplementedError(
            "conformance subclasses must implement `effort_reject_case()`")

    # ---- machinery --------------------------------------------------------

    @pytest.fixture(autouse=True)
    def _pinned_adapter_binary(self, monkeypatch, tmp_path):
        """Never let a conformance run stat the developer's real CLI config.

        Adapters resolve their binary as `SKODUN_<NAME>_BIN` -> a path under
        `~` -> the bare name. Pinning the override keeps `build_cmd` off the
        real home directory and keeps `argv[0]` from varying by machine.
        """
        name = self.adapter().name
        monkeypatch.setenv(f"SKODUN_{name.upper()}_BIN",
                           str(tmp_path / "pinned" / name))

    def fixtures(self) -> list[Fixture]:
        """Every fixture in `fixture_dir`, sorted by name."""
        d = self.fixture_dir
        assert d is not None, (
            f"{type(self).__name__} must set `fixture_dir` to its adapter's "
            f"fixture directory")
        d = Path(d)
        assert d.is_dir(), f"fixture directory does not exist: {d}"
        files = sorted(p for p in d.iterdir()
                       if p.is_file() and p.suffix == ".txt")
        assert files, f"no *.txt fixtures in {d}"
        return [load_fixture(p) for p in files]

    def select(self, pred) -> list[Fixture]:
        return [f for f in self.fixtures() if pred(f.name)]

    def _is_real_unavailability_tell(self, a: Adapter, signal: str) -> bool:
        """True iff `signal` ALONE, at a FIXED rc, makes `a` report unavailable.

        See `_PREMISE_PROBE_RC`'s docstring for why the rc must be fixed
        rather than taken from whichever fixture `signal` was borrowed from.
        """
        return a.classify(_PREMISE_PROBE_RC, b"", signal.encode("utf-8"),
                          REVIEW_CONTRACT).kind == "unavailable"

    def _classified(self, res: ClassifyResult, where: str) -> ClassifyResult:
        """Assert the invariants every `ClassifyResult` owes, then return it.

        The empty-category half is rule 4's converse and is checked on EVERY
        classification this suite makes, not only on the unavailable fixtures:
        a stray category on an `ok` verdict is a caching decision waiting to be
        read by code that only looks at `category`.
        """
        assert res.kind in _KINDS, f"{where}: unknown kind {res.kind!r}"
        if res.kind == "unavailable":
            assert res.category in _UNAVAILABLE_CATEGORIES, (
                f"{where}: unavailable category {res.category!r} is not one of "
                f"{sorted(_UNAVAILABLE_CATEGORIES)}")
        else:
            assert res.category == "", (
                f"{where}: a {res.kind!r} classification must carry an EMPTY "
                f"category, got {res.category!r}")
        return res

    # ---- rule 0: the class is wired to a real, registered adapter ----------

    def test_subclass_is_bound_to_its_registry_provider(self):
        """`provider_id` names this adapter, and `_REGISTRY` agrees.

        Without this a subclass could satisfy the coverage gate for `"acme"`
        while exercising a completely different adapter — coverage by string
        rather than by test.
        """
        a = self.adapter()
        assert self.provider_id, (
            f"{type(self).__name__} must set `provider_id` to the "
            f"`_REGISTRY` key it covers")
        assert a.provider == self.provider_id, (
            f"{type(self).__name__}.provider_id is {self.provider_id!r} but "
            f"the adapter it returns declares provider {a.provider!r}")
        assert self.provider_id in _REGISTRY, (
            f"provider {self.provider_id!r} is not registered")
        assert type(a) is _REGISTRY[self.provider_id], (
            f"_REGISTRY[{self.provider_id!r}] is "
            f"{_REGISTRY[self.provider_id].__name__}, but this suite exercises "
            f"{type(a).__name__}")

    def test_every_fixture_is_claimed_by_at_least_one_rule(self):
        """No fixture sits in the directory being checked by nothing.

        A file nobody selects is worse than a missing one: it reads like
        coverage in a diff and asserts nothing at all.
        """
        selectors = (_is_healthy, _is_degraded, _is_unavailable,
                     _is_refuter_healthy)
        orphans = [f.name for f in self.fixtures()
                   if not any(s(f.name) for s in selectors)]
        assert not orphans, (
            f"fixtures selected by no rule: {orphans} — a fixture name must "
            f"contain 'healthy', 'degraded' or 'unavailable' (and 'refuter' "
            f"for the refuter shape) or it is never exercised")

    # ---- rule 1: totality --------------------------------------------------

    def test_parse_and_classify_are_total_on_garbage(self):
        """Neither entry point may raise, on any bytes, under any contract.

        This is a trust property, not politeness: an exception escaping into
        the gate path is reported as an unexpected error (exit 2 by a
        different route, with a traceback) rather than as an untrustworthy
        review, and the two have very different consequences for a human
        reading CI output.
        """
        a = self.adapter()
        for contract in _CONTRACTS:
            for blob in _GARBAGE:
                where = f"parse({blob[:8]!r}..., {contract.name})"
                res = a.parse(blob, blob, contract)
                assert res.parse_ok is False, (
                    f"{where}: garbage must never be parse_ok")
                assert res.findings == [] and res.summary == "", where
                assert res.payload is None, (
                    f"{where}: no payload may survive a failed parse")
                for rc in (0, 1, UNAVAILABLE_RC):
                    self._classified(
                        a.classify(rc, blob, blob, contract),
                        f"classify({rc}, {blob[:8]!r}..., {contract.name})")

    # ---- rule 1c: nothing the contract rejected reaches the caller ---------

    def test_no_payload_survives_a_failed_validation(self):
        """`payload` is gated by `parse_ok`, not merely by extraction.

        The distinction is invisible until an adapter extracts an envelope the
        contract then REJECTS: `payload=payload` unconditionally and
        `payload=payload if parse_ok else None` behave identically on every
        input where extraction finds nothing, which is every garbage blob a
        format-agnostic suite can write by hand. So the discriminating input is
        built out of the adapter's own healthy capture instead, with two probe
        contracts that share the real eligibility rule and differ only in
        `validate`:

        * the permissive probe proves an eligible envelope IS extracted from
          these bytes — without it this rule would pass on an adapter that
          extracted nothing, which is the vacuity it exists to close;
        * the refusing probe then demands that the very same envelope reaches
          the caller as `payload=None`.

        A caller that checked the wrong flag must find nothing to act on, for
        the same reason `findings`/`summary` stay empty: a rejected payload is
        one this program may not act on, and handing it over anyway makes
        `parse_ok` advisory.
        """
        a = self.adapter()
        healthy = self.select(_is_healthy)
        assert healthy, "no *healthy* fixture"
        for fx in healthy:
            extracted = a.parse(fx.stdout, fx.stderr, _PROBE_ANY_VALID)
            assert extracted.payload is not None, (
                f"{fx.name}: no eligible envelope was extracted even with a "
                f"validator that accepts everything, so this rule would pass "
                f"vacuously — the fixture, not the adapter, is at fault")
            res = a.parse(fx.stdout, fx.stderr, _PROBE_NONE_VALID)
            assert res.parse_ok is False, (
                f"{fx.name}: parse_ok is True although contract.validate "
                f"refused every object")
            assert res.payload is None, (
                f"{fx.name}: an envelope the contract REJECTED was still "
                f"handed to the caller as `payload` — `parse_ok` is then "
                f"advisory, and a caller reading `payload` acts on a review "
                f"nothing validated")
            # No `findings`/`summary` assertion here: both probe contracts are
            # `OutputContract("review-probe", ...)`, never `is REVIEW_CONTRACT`
            # by identity, so every adapter's projection guard already forces
            # those two fields empty regardless of `parse_ok` or of the
            # mutation under test — an assertion here could not fail and would
            # read as evidence when it is not. Rule 1b
            # (`test_refuter_shape_is_parsed_classified_and_not_a_review`)
            # exercises that projection guard non-vacuously, against a real
            # foreign contract.

    # ---- rule 1b: the refuter shape ---------------------------------------

    def test_refuter_shape_is_parsed_classified_and_not_a_review(self):
        """The adapter can request, classify and parse the refuter response.

        Every adapter must prove this now, on a fixture, or Task 8's refuter
        pass breaks at runtime on a real review — the one moment there is no
        second opinion available to notice.

        The negative half matters as much as the positive: the same bytes
        under `REVIEW_CONTRACT` must NOT parse. A refuter response that a
        Phase 1 caller could read as a review is a review with no findings,
        which is a clean bill of health.
        """
        a = self.adapter()
        found = self.select(_is_refuter_healthy)
        assert found, (
            "no *refuter*healthy* fixture: every adapter must witness the "
            "refuter shape it will be asked for in Task 8")
        noisy = self.select(_is_noisy_healthy)
        for fx in found:
            res = a.parse(fx.stdout, fx.stderr, REFUTER_CONTRACT)
            assert res.parse_ok is True, (
                f"{fx.name}: refuter envelope did not parse under "
                f"REFUTER_CONTRACT")
            assert isinstance(res.payload, dict), f"{fx.name}: no payload"
            verdicts = res.payload.get("verdicts")
            assert isinstance(verdicts, list) and verdicts, (
                f"{fx.name}: payload['verdicts'] must be a non-empty list, "
                f"got {verdicts!r}")
            # The `findings`/`summary` projection belongs to REVIEW_CONTRACT
            # alone. A refuter payload reaching it is the SAME false all-clear
            # as the negative half below, arriving by the other door: a Phase 1
            # caller that reads only these two fields would take refuter
            # verdicts for review findings, or an invented summary for a real
            # one. The refuter's content is available on `payload`.
            assert res.findings == [], (
                f"{fx.name}: parse(..., REFUTER_CONTRACT) projected "
                f"{len(res.findings)} item(s) into `findings` — that field is "
                f"REVIEW_CONTRACT's, and a caller reading it sees refuter "
                f"output as a review")
            assert res.summary == "", (
                f"{fx.name}: parse(..., REFUTER_CONTRACT) projected "
                f"{res.summary!r} into `summary` — that field is "
                f"REVIEW_CONTRACT's and must stay empty under any other "
                f"contract")
            self._classified(
                a.classify(fx.rc, fx.stdout, fx.stderr, REFUTER_CONTRACT),
                f"{fx.name} refuter classify")
            assert a.classify(fx.rc, fx.stdout, fx.stderr,
                              REFUTER_CONTRACT).kind == "ok", (
                f"{fx.name}: a healthy refuter run must classify ok")

            # The same envelope with a false-alarm stderr borrowed from rule
            # 7's fixture: usable output wins on the unavailable axis for
            # EVERY contract, not only for reviews.
            for n in noisy:
                res2 = self._classified(
                    a.classify(0, fx.stdout, n.stderr, REFUTER_CONTRACT),
                    f"{fx.name} + {n.name} stderr")
                assert res2.kind == "ok", (
                    f"{fx.name} with {n.name}'s noisy stderr classified "
                    f"{res2.kind}/{res2.category}: a valid refuter payload is "
                    f"proof the provider served")

            review = a.parse(fx.stdout, fx.stderr, REVIEW_CONTRACT)
            assert review.parse_ok is False, (
                f"{fx.name}: a refuter response parsed as a REVIEW — a Phase "
                f"1 caller would read it as a review with no findings")
            assert review.findings == [] and review.summary == ""

    # ---- rule 2: healthy runs ---------------------------------------------

    def test_healthy_fixtures_classify_ok_and_parse(self):
        """A run the provider served well is `ok` and yields a review."""
        a = self.adapter()
        healthy = self.select(_is_healthy)
        assert healthy, "no *healthy* fixture"
        for fx in healthy:
            res = self._classified(
                a.classify(0, fx.stdout, fx.stderr, REVIEW_CONTRACT),
                f"{fx.name} classify")
            assert res.kind == "ok", (
                f"{fx.name}: healthy run classified {res.kind} "
                f"({res.detail!r})")
            parsed = a.parse(fx.stdout, fx.stderr, REVIEW_CONTRACT)
            assert parsed.parse_ok is True, (
                f"{fx.name}: healthy envelope did not parse")
            assert parsed.degraded is False, (
                f"{fx.name}: healthy envelope reported degraded "
                f"({parsed.degraded_reason!r})")

    # ---- rule 3: degradation ----------------------------------------------

    def test_degraded_fixtures_are_recognised(self):
        """Truncation must show up on one of the two axes, and be witnessed twice.

        `parse` and `classify` are independent axes and an adapter may report
        degradation on either — what it may not do is report it on neither. Two
        witnesses are required because one signal passing is not evidence that
        the detection is general; in the oracle's corpus a single unrecognised
        `Cancelled` shape accounted for 116 silently-clean runs.

        What it also may not do is call the degradation `unavailable`.
        Degradation and unavailability are different questions —
        "was this answer cut short?" versus "could the provider serve at all?"
        — with different costs: `degraded` buys one same-reviewer retry, while
        `unavailable` advances the fallback chain and, at category `quota`,
        takes the provider out of every later chain in the run. A valid
        envelope whose harness complained on stderr is a degradation of a
        provider that demonstrably served, so conflating the two throws away a
        working provider on evidence that never said it was down.
        """
        a = self.adapter()
        degraded = self.select(_is_degraded)
        assert len(degraded) >= 2, (
            f"need >= 2 *degraded* fixtures, found {[f.name for f in degraded]}")
        for fx in degraded:
            res = self._classified(
                a.classify(fx.rc, fx.stdout, fx.stderr, REVIEW_CONTRACT),
                f"{fx.name} classify")
            assert res.kind != "unavailable", (
                f"{fx.name}: a degradation classified unavailable/"
                f"{res.category} ({res.detail!r}) — availability and "
                f"degradation are different axes. This run is a truncated "
                f"answer from a provider that served; 'unavailable' advances "
                f"the fallback chain instead of retrying, and at category "
                f"'quota' removes the provider from every later chain")
            parsed = a.parse(fx.stdout, fx.stderr, REVIEW_CONTRACT)
            assert res.kind == "degraded" or parsed.degraded is True, (
                f"{fx.name}: neither classify ({res.kind}) nor parse "
                f"(degraded={parsed.degraded}) noticed the degradation")
            if parsed.degraded:
                assert parsed.degraded_reason, (
                    f"{fx.name}: degraded with an empty reason — the human "
                    f"reading CI has nothing to act on")

    # ---- rule 4: unavailability, and its exact category --------------------

    def test_unavailable_fixtures_carry_the_declared_category(self):
        """The category is a CACHING decision, so it must be exactly right.

        `quota` is the only category that is a property of the provider as a
        whole and therefore the only one that may be remembered beyond one
        attempt. An auth failure mislabelled `quota` takes a perfectly healthy
        provider out of every later fallback chain in the run; a quota failure
        mislabelled `auth` burns the rest of the budget rediscovering it.
        """
        a = self.adapter()
        unavailable = self.select(_is_unavailable)
        assert unavailable, "no *unavailable* fixture"
        assert self.select(_is_unavailable_quota), (
            "no *unavailable_quota* fixture: `quota` is the only "
            "provider-wide-cacheable category, so every adapter must witness "
            "its own provider's budget-exhaustion wording against its own "
            "signal table. Capture one if a live quota failure is available; "
            "otherwise synthesize it from wording that is VERIFIABLY this "
            "CLI's own — the installed binary's strings, or the provider's "
            "documented error — and record which it is in the fixture "
            "directory's README. See the module docstring")
        for fx in unavailable:
            assert fx.category, (
                f"{fx.name}: every *unavailable* fixture must declare the "
                f"expected `category=` line")
            assert fx.category in _UNAVAILABLE_CATEGORIES, (
                f"{fx.name}: declared category {fx.category!r} is not one of "
                f"{sorted(_UNAVAILABLE_CATEGORIES)}")
            for contract in _CONTRACTS:
                res = self._classified(
                    a.classify(fx.rc, fx.stdout, fx.stderr, contract),
                    f"{fx.name} classify ({contract.name})")
                assert res.kind == "unavailable", (
                    f"{fx.name} ({contract.name}): classified {res.kind}, "
                    f"expected unavailable")
                assert res.category == fx.category, (
                    f"{fx.name} ({contract.name}): classified category "
                    f"{res.category!r}, fixture declares {fx.category!r}")

    def test_missing_binary_is_unavailable_binary(self):
        """rc 127 is the shell's command-not-found, for every CLI alike.

        `binary` and not `other`: a missing binary is this reviewer's
        configuration being wrong, which is attempt-local and must never be
        cached as a provider outage.
        """
        a = self.adapter()
        for contract in _CONTRACTS:
            res = self._classified(
                a.classify(UNAVAILABLE_RC, b"", b"", contract),
                f"classify(127, ..., {contract.name})")
            assert res.kind == "unavailable" and res.category == "binary", (
                f"rc {UNAVAILABLE_RC} ({contract.name}) classified "
                f"{res.kind}/{res.category}, expected unavailable/binary")

    # ---- rule 5: the effort contract --------------------------------------

    def test_effort_is_mapped_or_loudly_rejected(self, tmp_path):
        """No silent downgrade: either every effort maps, or it is refused.

        Quietly dropping an unknown `--effort` reviews at the CLI's own
        default and reports the result as if the configured effort had been
        used, which is how a weak review passes for a strong one.
        """
        a = self.adapter()
        mapping = a.effort_map()
        assert isinstance(mapping, dict), "effort_map() must return a dict"
        unknown = sorted(set(mapping) - EFFORTS)
        assert not unknown, (
            f"effort_map() invents canonical efforts {unknown} that "
            f"config.EFFORTS does not define")
        for canonical, cli in mapping.items():
            assert isinstance(cli, str) and cli, (
                f"effort_map()[{canonical!r}] is {cli!r}; a CLI spelling must "
                f"be a non-empty string")

        case = self.effort_reject_case()
        if case is None:
            # Total support claimed — verify it. `"none"` is excluded by
            # design: it is the opt-out, handled before any lookup, not a CLI
            # value (see `base.Adapter.effort_map`).
            missing = sorted((EFFORTS - {_EFFORT_OPT_OUT}) - set(mapping))
            assert not missing, (
                f"effort_reject_case() returned None (total support) but "
                f"effort_map() has no CLI value for {missing}")
            return
        reviewer, message = case
        assert isinstance(reviewer, Reviewer), (
            "effort_reject_case() must return (Reviewer, str) or None")
        # `tmp_path`, not a relative path: `build_cmd` may own sidecar files it
        # writes beside the prompt (codex writes its `--output-schema` there),
        # and a relative prompt path would put them in whatever directory
        # pytest was started from the moment an adapter's guard stopped
        # preceding its writes. A test must not be able to litter the repo.
        with pytest.raises(ValueError, match=message):
            a.build_cmd(tmp_path / "prompt.txt", reviewer, Defaults(), tmp_path)
        # A reject case excuses exactly ONE effort — the one it declares. The
        # rest of the mapping still has to be total, or an adapter could prove
        # it refuses `max` loudly and then silently drop `high` as well: the
        # design spec's "every mapping is pinned by the conformance suite"
        # would hold for one value and nothing else. This is the same
        # obligation the `None` branch above carries, minus the declared
        # exception.
        excused = {_EFFORT_OPT_OUT, reviewer.effort}
        missing = sorted((EFFORTS - excused) - set(mapping))
        assert not missing, (
            f"effort_reject_case() declares only effort {reviewer.effort!r} "
            f"unsupported, but effort_map() has no CLI value for {missing} "
            f"either — an unmapped effort is a silent downgrade unless "
            f"build_cmd refuses it too")

    # ---- rule 6: finding text is content, never signal ---------------------

    def test_finding_text_never_triggers_degraded(self):
        """A review that DISCUSSES a harness failure is not a harness failure.

        Reviews quote the code and the logs they are reviewing. An adapter
        that greps its stderr tells out of stdout would flag every review of
        its own error-handling code as truncated — and, worse, would do it
        non-deterministically, depending on what the diff happened to contain.

        The mutated envelope is built by splicing the adapter's OWN stderr
        signal words (taken from its `*degraded*stderr*` fixture, so no
        adapter-specific table is needed here) into a finding title of its own
        healthy capture, byte-for-byte in place. That keeps the envelope
        genuinely well-formed rather than approximately so.
        """
        a = self.adapter()
        sources = self.select(_is_degraded_stderr)
        assert sources, (
            "no *degraded*stderr* fixture to borrow signal words from")
        healthy = self.select(_is_healthy)
        assert healthy, "no *healthy* fixture"

        spliced_any = False
        for src in sources:
            signal = _json_safe(src.stderr)
            assert signal, f"{src.name}: stderr carries no printable signal"
            for fx in healthy:
                mutated = self._splice_into_finding_title(fx.stdout, signal)
                if mutated is None:
                    continue
                spliced_any = True
                where = f"{fx.name} + {src.name} signal words in a title"
                assert signal.encode("utf-8") in mutated, where
                parsed = a.parse(mutated, b"", REVIEW_CONTRACT)
                assert parsed.parse_ok is True, (
                    f"{where}: splicing text into a title broke the envelope; "
                    f"the fixture, not the adapter, is at fault")
                assert parsed.degraded is False, (
                    f"{where}: parse reported degraded "
                    f"({parsed.degraded_reason!r}) on finding TEXT")
                res = self._classified(
                    a.classify(0, mutated, b"", REVIEW_CONTRACT), where)
                assert res.kind == "ok", (
                    f"{where}: classified {res.kind} ({res.detail!r}) — "
                    f"stdout content is not a harness signal")
        assert spliced_any, (
            "rule 6 needs a healthy fixture that parses and has at least one "
            "finding whose `title` appears verbatim in the captured bytes; "
            "none of "
            f"{[f.name for f in healthy]} qualifies")

    def test_model_text_cannot_make_a_run_unavailable(self):
        """Rule 6's other half, and the one that is not satisfied by luck.

        The test above splices signal words into a review that still VALIDATES,
        so every adapter whose `classify` short-circuits on a usable payload
        passes it without its diagnostic reader ever being reached. That
        short-circuit is correct, but it means the assertion proves nothing
        about what the adapter would read if there were no payload — and "no
        payload" is the common case for exactly the runs a classifier is for.

        So the discriminating input is a run that FAILED: the model's words are
        on the wire, they carry this adapter's own unavailability wording, and
        the payload does not validate, so no short-circuit can hide the answer.
        `unavailable` is then a claim about the provider that could only have
        come from reading the model's message text — and the cost of getting it
        wrong is a healthy provider dropped from the fallback chain, or, at
        category `quota`, from every later chain in the run.

        Every ingredient is the adapter's own: the wording comes from its
        `*unavailable*` fixtures' stderr and is proved to be a real tell before
        it is used, and the carrier is its own healthy capture.
        """
        a = self.adapter()
        healthy = self.select(_is_healthy)
        assert healthy, "no *healthy* fixture"
        sources = self.select(_is_unavailable)
        assert sources, "no *unavailable* fixture to borrow wording from"

        proved = False
        for src in sources:
            signal = _json_safe(src.stderr)
            if not signal:
                # A capture whose evidence lives only in the stream (an empty
                # stderr) offers no portable wording. Another fixture must.
                continue
            # …and these exact bytes really are a tell: on stderr, with nothing
            # on stdout, they take the provider down. Without this the splice
            # below could be harmless text and the rule would pass vacuously.
            # Probed at a FIXED rc (see `_is_real_unavailability_tell`), never
            # at `src.rc`: an *unavailable* fixture may declare rc=127, at
            # which every adapter is unavailable/binary regardless of stderr.
            if not self._is_real_unavailability_tell(a, signal):
                continue
            for fx in healthy:
                mutated = self._splice_into_finding_title(fx.stdout, signal)
                if mutated is None:
                    continue
                broken = _invalidate_payload(mutated)
                where = f"{fx.name} + {src.name} wording, payload invalidated"
                assert signal.encode("utf-8") in broken, where
                # The two halves of "no short-circuit can hide this": the model
                # text survived, and the payload no longer validates.
                parsed = a.parse(broken, b"", REVIEW_CONTRACT)
                if parsed.parse_ok:
                    continue
                for rc in (0, 1):
                    res = self._classified(
                        a.classify(rc, broken, b"", REVIEW_CONTRACT),
                        f"{where} (rc {rc})")
                    assert res.kind != "unavailable", (
                        f"{where} (rc {rc}): classified unavailable/"
                        f"{res.category} ({res.detail!r}) — the ONLY place "
                        f"that wording appears is the model's own message "
                        f"text, so this adapter reads review CONTENT as a "
                        f"provider verdict and any review of auth or rate-"
                        f"limit code takes the provider out of the chain")
                proved = True
        assert proved, (
            "rule 6's failed-run half was never exercised: it needs an "
            "*unavailable* fixture whose STDERR carries the wording (one whose "
            "evidence is only in the stream cannot supply it) and a healthy "
            "fixture with a spliceable finding title")

    def test_rule_6_premise_probe_ignores_an_unavailable_fixtures_own_rc(self):
        """Regression: an `*unavailable*` fixture declaring `rc=127` must not
        make the premise probe above certify inert text as a real tell.

        `binary` (rc 127) is the single most natural first `*unavailable*`
        fixture an adapter author writes, and every registered adapter's
        `classify` returns `unavailable/binary` at rc 127 before it even
        looks at stderr. A premise probe that asked "is this wording a real
        tell?" using such a fixture's OWN `rc` would answer yes for
        completely inert stderr — measured directly here as the reviewer
        measured it: `classify(127, b"", b"...inert...")` is `unavailable`
        regardless of what the inert text says. `_is_real_unavailability_tell`
        must therefore probe at a FIXED rc (`_PREMISE_PROBE_RC`), not at
        whatever rc the fixture under test happens to declare.
        """
        a = self.adapter()
        inert = "the weather is nice today, and this stderr line names no " \
                "provider failure of any kind"
        # Sanity + the defect's precondition: rc 127 alone is unavailable/
        # binary for every adapter, independent of stderr content — this is
        # exactly what a rc=127 *unavailable* fixture's rc would supply to a
        # premise probe that (wrongly) used `src.rc` instead of a fixed rc.
        assert a.classify(UNAVAILABLE_RC, b"", inert.encode("utf-8"),
                          REVIEW_CONTRACT).kind == "unavailable", (
            f"{type(self).__name__}: sanity failed — rc {UNAVAILABLE_RC} did "
            f"not classify unavailable/binary regardless of stderr; see "
            f"test_missing_binary_is_unavailable_binary")
        # The premise probe itself must not reach that same wrong conclusion:
        # inert wording is not a real tell at the FIXED probe rc.
        assert not self._is_real_unavailability_tell(a, inert), (
            f"{type(self).__name__}: inert text was certified as a real "
            f"unavailability tell — the premise probe must use a fixed rc "
            f"({_PREMISE_PROBE_RC}), not a fixture's own rc=127, or an "
            f"*unavailable* fixture with rc=127 reopens the vacuity rule 6 "
            f"exists to close")

    def _splice_into_finding_title(self, stdout: bytes,
                                   signal: str) -> bytes | None:
        """Append `signal` to a finding title, in place, in the raw bytes.

        Returns None when this envelope offers no title that can be edited
        without re-encoding it — the caller then tries the next fixture.

        Byte replacement rather than decode-edit-reencode, and `signal` is
        pre-filtered to characters that need no JSON escaping, for one reason:
        an envelope typically carries the payload TWICE (once as a nested JSON
        string, once as a structured object) at two different escaping depths.
        A plain substring that needs no escaping is identical at every depth,
        so a single `replace` keeps both copies valid and consistent.
        """
        a = self.adapter()
        parsed = a.parse(stdout, b"", REVIEW_CONTRACT)
        if not parsed.parse_ok:
            return None
        for finding in parsed.findings:
            if not isinstance(finding, dict):
                continue
            title = finding.get("title")
            if not isinstance(title, str) or not title:
                continue
            # Only a title that survives JSON encoding unchanged appears in the
            # raw bytes verbatim and can be edited there.
            if json.dumps(title)[1:-1] != title:
                continue
            needle = title.encode("utf-8")
            if needle not in stdout:
                continue
            return stdout.replace(needle, (title + " " + signal).encode("utf-8"))
        return None

    # ---- rule 7: usable output wins over stderr noise ----------------------

    def test_usable_output_wins_over_stderr_noise(self):
        """`unavailable` means the provider could not serve, and it did.

        Provider CLIs write warnings, retries and recovered auth handshakes to
        stderr while the run succeeds. Reading those as an outage discards a
        perfectly good review and — for `quota` — takes the provider out of
        every later chain in the run.

        The fixture proves itself: the SAME stderr with no payload on stdout
        must classify `unavailable`. Without that, a fixture whose stderr said
        nothing alarming would satisfy this rule vacuously.
        """
        a = self.adapter()
        noisy = self.select(_is_noisy_healthy)
        assert noisy, (
            "no *healthy_noisy_stderr* fixture: every adapter must witness "
            "the non-signal rule with its own auth/quota wording")
        for fx in noisy:
            alone = self._classified(
                a.classify(fx.rc, b"", fx.stderr, REVIEW_CONTRACT),
                f"{fx.name} stderr with empty stdout")
            assert alone.kind == "unavailable", (
                f"{fx.name}: its stderr classified {alone.kind} even with NO "
                f"stdout, so this fixture does not actually witness the "
                f"non-signal rule — put real auth/quota wording in it")
            res = self._classified(
                a.classify(0, fx.stdout, fx.stderr, REVIEW_CONTRACT),
                f"{fx.name} classify")
            assert res.kind == "ok", (
                f"{fx.name}: classified {res.kind}/{res.category} "
                f"({res.detail!r}) although stdout carried a valid payload")
            assert a.parse(fx.stdout, fx.stderr,
                           REVIEW_CONTRACT).parse_ok is True, (
                f"{fx.name}: stdout must carry a usable payload for this rule "
                f"to mean anything")

    # ---- rule 8: the prompt ceiling is declared, not discovered ------------

    def test_prompt_limit_is_declared(self):
        """Every adapter answers "how large a prompt can you take?".

        The planner sizes batches BEFORE anything is invoked, so a ceiling that
        only `build_cmd` knows about is a ceiling discovered at the moment it
        is too late to do anything but fail. An adapter whose CLI takes the
        prompt as a file has no such ceiling and says so with `None`; one whose
        CLI takes it in the argv returns the byte limit it enforces.

        `None` is the only permitted non-integer, and a declared limit must be
        a real capacity: zero would mean "no prompt is ever small enough",
        which is a broken adapter rather than a tight one, and `bool`
        subclasses `int` so `True` would otherwise read as a one-byte ceiling.
        """
        a = self.adapter()
        assert hasattr(a, "prompt_limit"), (
            f"adapter {a.name!r} does not implement `prompt_limit()`; every "
            f"registered adapter must declare its prompt ceiling or None")
        limit = a.prompt_limit()
        if limit is None:
            return
        assert not isinstance(limit, bool) and isinstance(limit, int), (
            f"adapter {a.name!r}: prompt_limit() returned {limit!r}; it must "
            f"be an int or None")
        assert limit >= 1, (
            f"adapter {a.name!r}: prompt_limit() is {limit}, which no prompt "
            f"can ever satisfy")
        # Stable: the planner calls this once and invokes later, so a limit
        # that moves between the two sizes a batch against a ceiling that no
        # longer applies.
        assert self.adapter().prompt_limit() == limit, (
            f"adapter {a.name!r}: prompt_limit() is not stable across "
            f"instances")


def _invalidate_payload(stdout: bytes) -> bytes:
    """Break the review payload in-place, leaving the envelope well-formed.

    `summary` is renamed rather than retyped, so the result is still a JSON
    object carrying `findings` — `_review_eligible` accepts it and
    `_valid_payload` refuses it (a missing `summary`), which is precisely the
    "extracted but rejected" state rule 6's failed-run half needs.

    A BARE word is replaced, with no surrounding quotes: a payload typically
    appears at two escaping depths in one capture (`"summary"` at the root,
    `\\"summary\\"` nested inside a JSON string), and only a needle that needs no
    escaping is byte-identical at both. Renaming an incidental occurrence of the
    word inside prose is harmless — it stays a word inside a string.
    """
    return stdout.replace(b"summary", b"summaryX")


def _json_safe(blob: bytes) -> str:
    """`blob` reduced to characters that need no JSON escaping at any depth."""
    text = blob.decode("utf-8", "replace")
    return "".join(ch for ch in text
                   if ch.isprintable() and ch not in '"\\').strip()


# --------------------------------------------------------------------------
# the coverage gate
# --------------------------------------------------------------------------
#
# These two functions are IMPORTED into each adapter's test module, which is
# what makes pytest collect them. They live here so that the gate is defined
# once, next to the suite it gates, and cannot drift per adapter.


def _load_sibling_adapter_test_modules() -> None:
    """Import every `tests/test_adapter_*.py` that is not already loaded.

    A gate that only holds when the whole suite happens to be selected is not
    a gate: `pytest tests/test_adapter_codex.py` would otherwise report the
    grok adapter as uncovered merely because its module was never imported.
    Modules pytest already imported are left alone, under whatever name it
    gave them, so nothing is executed twice in a normal run.
    """
    here = Path(__file__).resolve().parent
    loaded = {name.rsplit(".", 1)[-1] for name in list(sys.modules)}
    for path in sorted(here.glob("test_adapter_*.py")):
        if path.stem not in loaded:
            importlib.import_module(f"tests.{path.stem}")


def _conformance_subclasses() -> list[type]:
    """Every subclass of `AdapterConformance`, transitively."""
    out: list[type] = []
    seen: set[int] = set()
    stack = list(AdapterConformance.__subclasses__())
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        out.append(cls)
        stack.extend(cls.__subclasses__())
    return out


def test_every_registered_adapter_has_conformance_coverage():
    """Registering an adapter without a conformance suite fails CI.

    By construction rather than by convention: the alternative is a checklist
    in a plan document, which the next contributor has no way of being
    reminded of. An unconformed adapter is one whose ability to notice its own
    CLI failing has never been demonstrated, and that adapter reporting "no
    findings" is indistinguishable from a clean review.
    """
    _load_sibling_adapter_test_modules()
    covered: dict[str, list[str]] = {}
    for cls in _conformance_subclasses():
        where = f"{cls.__module__}.{cls.__qualname__}"
        provider = getattr(cls, "provider_id", "")
        assert provider, (
            f"{where} subclasses AdapterConformance without setting "
            f"`provider_id`, so it proves coverage for nothing")
        # pytest collects `Test*` classes only. A subclass named otherwise is
        # never run, and a suite that never runs is not coverage.
        assert cls.__name__.startswith("Test"), (
            f"{where} is a conformance subclass pytest will never collect: "
            f"rename it to Test{cls.__name__}")
        covered.setdefault(provider, []).append(where)

    missing = sorted(set(_REGISTRY) - set(covered))
    assert not missing, (
        f"registered providers with no AdapterConformance subclass: "
        f"{missing} — add one in tests/test_adapter_<name>.py before "
        f"registering the adapter")
    unknown = sorted(set(covered) - set(_REGISTRY))
    assert not unknown, (
        f"conformance subclasses claim providers that are not registered: "
        f"{ {p: covered[p] for p in unknown} }")


def test_coverage_gate_fails_without_a_conformance_subclass(monkeypatch):
    """The gate is proved to bite, on a provider that has no suite.

    A coverage gate nobody has watched fail is an assertion about itself.
    """
    class _UnconformedAdapter:
        name = "unconformed"
        provider = "acme-unconformed"

    monkeypatch.setitem(_REGISTRY, "acme-unconformed", _UnconformedAdapter)
    with pytest.raises(AssertionError, match="acme-unconformed"):
        test_every_registered_adapter_has_conformance_coverage()
