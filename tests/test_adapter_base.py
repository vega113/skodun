"""Tests for the provider-neutral adapter contract in `adapters.base`.

Three things are pinned here and they are all about *sharing*, not behaviour:

* `ParseResult` is ONE class, moved to `base` and re-exported from `grok`. The
  identity assertion is the point: a copy would let the two drift apart and
  every `isinstance` check upstream would start lying.
* Every registered adapter answers the same run-health question the same way.
  `rc 127` is the shell's command-not-found, so it means `unavailable/binary`
  for every provider, not just the one that happened to be written first.
* Each `OutputContract` validates its OWN shape and rejects the other one.
  Contracts are passed around by value in Phase 2; a validator that accepts a
  foreign payload would let a refuter response be recorded as a review.
"""

from __future__ import annotations

import json

import pytest

from skodun.adapters import ParseResult, REVIEW_CONTRACT, REFUTER_CONTRACT, get_adapter
from skodun.adapters.base import UNAVAILABLE_RC, OutputContract
from skodun.adapters.grok import GrokAdapter
from skodun.config import EFFORTS, Defaults, Reviewer

R = Reviewer(name="f", provider="xai", model="grok-4.5", role="finder")
D = Defaults()


@pytest.fixture(autouse=True)
def pinned_provider_bins(monkeypatch, tmp_path):
    """Never touch the developer's real provider config.

    `resolve_binary()` is exercised below and every adapter's resolver probes a
    dot-directory under `$HOME` before falling back to PATH. Pin the env
    override to a tmp path so the test is hermetic on any machine and the
    developer's live CLI config is never read.
    """
    monkeypatch.setenv("SKODUN_GROK_BIN", str(tmp_path / "grok"))


def test_parse_result_importable_from_base_and_grok():
    from skodun.adapters.base import ParseResult as base_pr
    from skodun.adapters.grok import ParseResult as grok_pr
    assert base_pr is grok_pr          # one class, re-exported — not a copy


def test_grok_adapter_satisfies_protocol():
    a = get_adapter("xai")
    assert a.provider == "xai" and a.name == "grok"
    assert a.stdin_from_prompt_file is False
    assert callable(a.classify) and callable(a.effort_map)
    assert a.resolve_binary()          # non-empty string, env override honored


def test_rc_127_is_unavailable_binary_for_every_registered_adapter():
    from skodun.adapters import _REGISTRY
    for cls in _REGISTRY.values():
        r = cls().classify(UNAVAILABLE_RC, b"", b"command not found")
        assert r.kind == "unavailable" and r.category == "binary"


def test_contracts_validate_their_own_shapes():
    assert REVIEW_CONTRACT.validate({"summary": "s", "findings": []})
    assert not REVIEW_CONTRACT.validate({"verdicts": []})
    assert REFUTER_CONTRACT.validate(
        {"verdicts": [{"index": 0, "verdict": "refuted",
                       "reasoning": "the guard on entry already handles the None case"}]})
    assert not REFUTER_CONTRACT.validate({"summary": "s", "findings": []})


def test_parse_result_is_still_importable_from_the_package():
    assert ParseResult.__module__.endswith("adapters.base")


# --------------------------------------------------------------------------
# classify: the run-health axis
# --------------------------------------------------------------------------

CLEAN = json.dumps(
    {"structuredOutput": {"summary": "ok", "findings": []},
     "stopReason": "EndTurn"}).encode("utf-8")


def test_clean_run_classifies_ok():
    r = GrokAdapter().classify(0, CLEAN, b"")
    assert r.kind == "ok" and r.category == "" and r.detail == ""


def test_auth_noise_alongside_a_healthy_answer_is_still_a_non_signal():
    """Phase 1's documented non-signal, now on the classify axis too.

    `test_auth_noise_is_not_degraded` pins that this stderr does not make the
    run degraded. It must not make it *unavailable* either: the run produced a
    valid review, and falling over to another provider because the harness
    grumbled would discard a good answer.
    """
    err = (b"worker quit with fatal: Transport channel closed, "
           b"when Auth(AuthorizationRequired)")
    assert GrokAdapter().classify(0, CLEAN, err).kind == "ok"


def test_auth_fatal_without_usable_output_is_unavailable_auth():
    err = b"fatal: Auth(AuthorizationRequired)"
    r = GrokAdapter().classify(1, b"", err)
    assert r.kind == "unavailable" and r.category == "auth"


@pytest.mark.parametrize("err", [
    b"error: quota exceeded for this account",
    b"HTTP error: rate limit reached, retry later",
    b"RATE_LIMIT exceeded",
    b"429 Too Many Requests",
])
def test_quota_stderr_without_usable_output_is_unavailable_quota(err):
    r = GrokAdapter().classify(1, b"", err)
    assert r.kind == "unavailable" and r.category == "quota"


def test_unknown_model_stderr_is_unavailable_model():
    r = GrokAdapter().classify(1, b"", b"error: unknown model 'grok-9'")
    assert r.kind == "unavailable" and r.category == "model"


def test_quota_is_the_only_provider_wide_category():
    """A bare number must never mint a provider-wide quota outage.

    `quota` is the one category Task 7 may cache across an entire run, so its
    signal table is the one that must not fire on incidental text. `429` as a
    line number or byte offset is exactly that incidental text.
    """
    r = GrokAdapter().classify(1, b"", b"parse error at offset 429 in stream")
    assert r.category != "quota"


def test_degraded_evidence_classifies_degraded_not_unavailable():
    r = GrokAdapter().classify(0, CLEAN, b"tool_error while streaming")
    assert r.kind == "degraded" and r.category == "" and r.detail


def test_empty_output_with_clean_stderr_is_ok_not_invented_unavailability():
    """A failed attempt with no evidence is not evidence of anything.

    `parse_ok` is False here and the attempt is worthless, but `classify`
    refuses to infer a cause from absence — the same rule that keeps
    `degraded` positive-evidence-only.
    """
    r = GrokAdapter().classify(1, b"", b"")
    assert r.kind == "ok" and r.category == ""


@pytest.mark.parametrize("stdout", [b"", b"\xff\xfe not utf-8", b"{", b"null"])
@pytest.mark.parametrize("rc", [0, 1, -9, 127])
def test_classify_never_raises(rc, stdout):
    assert GrokAdapter().classify(rc, stdout, b"\xff\xfe").kind in (
        "ok", "degraded", "unavailable")


# --------------------------------------------------------------------------
# contract-parametric parse
# --------------------------------------------------------------------------


def test_review_parse_populates_both_payload_and_the_projection():
    p = GrokAdapter().parse(CLEAN, b"")
    assert p.parse_ok
    assert p.payload == {"summary": "ok", "findings": []}
    assert p.summary == "ok" and p.findings == []


def test_refuter_parse_fills_payload_and_leaves_the_review_projection_empty():
    """A refuter response must never be readable as a review.

    `findings`/`summary` are REVIEW_CONTRACT's projection. A Phase 1 caller
    that only knows those two fields sees an empty review here, not a
    mis-shaped one; the refuter merge reads `payload["verdicts"]`.
    """
    verdicts = {"verdicts": [{"index": 0, "verdict": "confirmed",
                              "reasoning": "the call is genuinely unguarded"}]}
    out = json.dumps({"structuredOutput": verdicts,
                      "stopReason": "EndTurn"}).encode("utf-8")
    p = GrokAdapter().parse(out, b"", REFUTER_CONTRACT)
    assert p.parse_ok and p.payload == verdicts
    assert p.findings == [] and p.summary == ""


def test_a_review_payload_does_not_satisfy_the_refuter_contract():
    p = GrokAdapter().parse(CLEAN, b"", REFUTER_CONTRACT)
    assert not p.parse_ok and p.payload is None


def test_build_cmd_asks_for_the_requested_contracts_schema(tmp_path):
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path,
                                  REFUTER_CONTRACT)
    assert cmd[cmd.index("--json-schema") + 1] == REFUTER_CONTRACT.json_schema
    assert "\n" not in REFUTER_CONTRACT.json_schema  # one argv element
    json.loads(REFUTER_CONTRACT.json_schema)


def test_effort_absent_from_the_map_is_loud(tmp_path, monkeypatch):
    """An unmappable effort raises; it is never a silently dropped flag."""
    monkeypatch.setattr(GrokAdapter, "effort_map", lambda self: {"low": "low"})
    r = Reviewer(name="f", provider="xai", model="grok-4.5", role="finder",
                 effort="high")
    with pytest.raises(ValueError, match="high"):
        GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)


def test_effort_map_is_a_copy_not_the_live_table():
    a = GrokAdapter()
    a.effort_map()["low"] = "tampered"
    assert a.effort_map()["low"] == "low"


def test_every_canonical_effort_except_the_opt_out_is_mappable():
    """`config.EFFORTS` is the canonical vocabulary; `none` is the opt-out."""
    assert set(GrokAdapter().effort_map()) == EFFORTS - {"none"}


# --------------------------------------------------------------------------
# "never raises, on any input" — the binding promise, tested where it broke
# --------------------------------------------------------------------------


def _recursion_bomb() -> bytes:
    """JSON nested deep enough to defeat the decoder on THIS interpreter.

    The depth at which `json` gives up is a property of the available C stack
    rather than of `sys.getrecursionlimit()`, so it differs between builds and
    platforms — hardcoding a number would silently stop being deep enough and
    leave the test passing while proving nothing. Probe for the depth instead,
    and fail loudly if no reachable depth defeats the decoder.

    Built programmatically: this is ~65 KB of input, which does not belong in
    the repository as a fixture. The nesting is a single array chain inside one
    object so the raw scan has exactly one `{` to retry from — a stdout full of
    open braces is the same defect at quadratic cost, not a different one.
    """
    depth = 1024
    while depth <= 1 << 20:
        text = '{"a":' + "[" * depth + "]" * depth + "}"
        try:
            json.JSONDecoder().raw_decode(text, 0)
        except RecursionError:
            return text.encode("utf-8")
        depth *= 2
    pytest.fail("no reachable nesting depth defeats this interpreter's "
                "json decoder; the regression this pins cannot be reproduced")


@pytest.mark.parametrize("contract", [REVIEW_CONTRACT, REFUTER_CONTRACT])
def test_deeply_nested_json_does_not_escape_as_an_exception(contract):
    """Model output is untrusted, and the decoder raises more than ValueError.

    `json`'s C scanner raises `RecursionError` — a `RuntimeError` subclass, so
    it sails straight past an `except ValueError` — on deeply nested input.
    Both extraction sites must survive it: an exception escaping `parse` or
    `classify` reaches the gate as an unexpected error rather than as an
    untrustworthy review, and those two have very different consequences.
    """
    bomb = _recursion_bomb()
    a = GrokAdapter()
    p = a.parse(bomb, b"", contract)
    assert not p.parse_ok and p.payload is None
    assert p.findings == [] and p.summary == ""
    assert a.classify(0, bomb, b"", contract).kind in (
        "ok", "degraded", "unavailable")


def _explode(obj: object) -> bool:
    raise RuntimeError("a contract predicate this module cannot vouch for")


HOSTILE_CONTRACT = OutputContract("hostile", "{}", _explode, _explode)


def test_a_contract_predicate_that_raises_cannot_break_the_promise():
    """The other half of totality: `base._ask`.

    `OutputContract` holds callables supplied by whoever built the contract, so
    "never raises, on any input" would otherwise be conditional on a
    third-party predicate behaving. `_ask` answers False instead — the
    fail-closed answer, since a payload nobody can vouch for is not one this
    program may act on.
    """
    a = GrokAdapter()
    p = a.parse(CLEAN, b"", HOSTILE_CONTRACT)
    assert not p.parse_ok and p.payload is None
    assert p.findings == [] and p.summary == ""
    assert a.classify(0, CLEAN, b"", HOSTILE_CONTRACT).kind in (
        "ok", "degraded", "unavailable")


# --------------------------------------------------------------------------
# classify is contract-parametric, and mis-keying it is a safety defect
# --------------------------------------------------------------------------

VERDICTS = {"verdicts": [{"index": 0, "verdict": "confirmed",
                          "reasoning": "the call is genuinely unguarded"}]}
REFUTER_OUT = json.dumps({"structuredOutput": VERDICTS,
                          "stopReason": "EndTurn"}).encode("utf-8")
AUTH_FATAL = b"fatal: Auth(AuthorizationRequired)"


def test_classify_judges_usable_output_by_the_requested_contract():
    """Same bytes, two contracts, two correct-and-opposite answers.

    A valid refuter response must not be judged by review eligibility. Get this
    wrong and a good refuter answer alongside noisy stderr is reported as
    `unavailable` — and if the noise happens to look like quota, the one
    provider-wide-cacheable category, a single misfire removes a healthy
    provider from every later fallback chain in the run.
    """
    a = GrokAdapter()
    assert a.classify(0, REFUTER_OUT, AUTH_FATAL, REFUTER_CONTRACT).kind == "ok"
    r = a.classify(0, REFUTER_OUT, AUTH_FATAL, REVIEW_CONTRACT)
    assert r.kind == "unavailable" and r.category == "auth"


@pytest.mark.parametrize("err, category", [
    (b"fatal: Auth(AuthorizationRequired); rate limit reached", "auth"),
    (b"error: unknown model 'grok-9' (quota unaffected)", "model"),
])
def test_quota_loses_to_any_more_specific_category(err, category):
    """The signal-table order is a safety decision, not alphabetical.

    `quota` is the only provider-wide-cacheable category, so a false `quota`
    takes a working provider out of every later chain in the run while a false
    `auth`/`model` costs one attempt. stderr that matches both must therefore
    report the attempt-local cause.
    """
    r = GrokAdapter().classify(1, b"", err)
    assert r.kind == "unavailable" and r.category == category


# --------------------------------------------------------------------------
# `_valid_verdicts` — Task 8's merge-trust boundary
# --------------------------------------------------------------------------

GOOD_VERDICT = {"index": 0, "verdict": "confirmed",
                "reasoning": "the call is genuinely unguarded"}


@pytest.mark.parametrize("bad, why", [
    ({"verdicts": [{**GOOD_VERDICT, "index": True}]}, "bool index"),
    ({"verdicts": [{**GOOD_VERDICT, "index": False}]}, "bool index"),
    ({"verdicts": [{**GOOD_VERDICT, "index": "0"}]}, "str index"),
    ({"verdicts": [{**GOOD_VERDICT, "index": 0.0}]}, "float index"),
    ({"verdicts": [{k: v for k, v in GOOD_VERDICT.items() if k != "index"}]},
     "missing index"),
    ({"verdicts": [{**GOOD_VERDICT, "reasoning": 123}]}, "non-str reasoning"),
    ({"verdicts": [{**GOOD_VERDICT, "reasoning": None}]}, "null reasoning"),
    ({"verdicts": [{k: v for k, v in GOOD_VERDICT.items() if k != "reasoning"}]},
     "missing reasoning"),
    ({"verdicts": ["confirmed"]}, "non-dict item"),
    ({"verdicts": [None]}, "non-dict item"),
    ({"verdicts": [[GOOD_VERDICT]]}, "non-dict item"),
    ({"verdicts": [GOOD_VERDICT, "confirmed"]}, "one bad item fails all"),
    ({"verdicts": [{**GOOD_VERDICT, "verdict": "maybe"}]}, "off-enum verdict"),
    ({"verdicts": [{**GOOD_VERDICT, "verdict": "Confirmed"}]}, "enum is exact"),
    ({"verdicts": [{**GOOD_VERDICT, "verdict": ""}]}, "empty verdict"),
    ({"verdicts": [{k: v for k, v in GOOD_VERDICT.items() if k != "verdict"}]},
     "missing verdict"),
    ({"verdicts": {"0": GOOD_VERDICT}}, "verdicts is not a list"),
])
def test_valid_verdicts_rejects(bad, why):
    """One malformed item fails the whole payload, so the run is retried.

    This validator is the trust boundary Task 8's merge sits behind: a verdict
    whose `index` cannot be trusted cannot be keyed back onto the finding it
    judges, and `{"index": true}` merged as finding number 1 is a wrong verdict
    attached to a real finding — worse than no verdict at all.
    """
    assert not REFUTER_CONTRACT.validate(bad), why


def test_valid_verdicts_accepts_the_shapes_it_should():
    assert REFUTER_CONTRACT.validate({"verdicts": []})
    assert REFUTER_CONTRACT.validate(
        {"verdicts": [{**GOOD_VERDICT, "verdict": v, "index": i}
                      for i, v in enumerate(("confirmed", "refuted",
                                             "uncertain"))]})
    # Reasoning LENGTH is merge policy (Task 8), not payload shape.
    assert REFUTER_CONTRACT.validate({"verdicts": [{**GOOD_VERDICT,
                                                    "reasoning": ""}]})


def test_refuter_raw_scan_skips_verdict_shaped_objects():
    """A bare verdict object is not an envelope — the refuter mirror image.

    `test_parse_raw_scan_skips_finding_shaped_object` pins this for reviews. It
    matters identically here: a raw scan over a truncated refuter envelope must
    not lock onto the first array element and record `parse_ok` on a fragment
    of the answer.
    """
    raw = (b"prose " + json.dumps(GOOD_VERDICT).encode() + b" more "
           + json.dumps(VERDICTS).encode() + b" trailing")
    p = GrokAdapter().parse(raw, b"", REFUTER_CONTRACT)
    assert p.parse_ok and p.payload == VERDICTS


def test_payload_is_none_when_an_extracted_envelope_fails_validation():
    """`payload` is pinned to None by `parse_ok`, not merely by extraction.

    The distinction is the whole point: here extraction DOES find an eligible
    envelope and hands it over, and only `contract.validate` rejects it. A
    caller that checked the wrong flag must still find nothing to act on — the
    same rule that keeps `findings`/`summary` empty.
    """
    out = b'{"structuredOutput": {"summary": 123, "findings": []}}'
    p = GrokAdapter().parse(out, b"")
    assert not p.parse_ok
    assert p.payload is None
    assert p.findings == [] and p.summary == ""
