"""Tests for the trust invariant and the verdict banner.

The banner is the single line a human or a shell script reads to learn
whether a review can be trusted, and it is always the LAST line of stdout.
That load-bearing property drives most of the cases below: every value the
banner interpolates comes from a persisted record that this module does not
control, so a `\n`/`\r` smuggled into `summary`, `stop_reason`, or a
`banner_failure` reason must not be able to fabricate a second banner line,
and a record missing fields entirely must render zeroes/`None`-safe output
rather than raising.
"""

from __future__ import annotations

from skodun.trust import banner, banner_failure, is_trustworthy


def test_invariant():
    assert is_trustworthy(True, False, False)
    assert not is_trustworthy(False, False, False)
    assert not is_trustworthy(True, True, False)
    assert not is_trustworthy(True, False, True)


def test_banner_reads_recorded_values():
    rec = dict(id="loop_1", head="a"*40, trustworthy=True, findings_total=2,
               degraded=False, stop_reason="EndTurn",
               severity={"high": 1, "medium": 0, "low": 1})
    b = banner(rec)
    assert b.startswith("SKODUN VERDICT: trustworthy=true findings=2 ")
    assert "severity=1/0/1" in b and "head=aaaaaaaaa" in b and "\n" not in b


def test_banner_renders_false_lowercase_and_degraded_true():
    rec = dict(id="loop_2", head="b" * 40, trustworthy=False, findings_total=0,
               degraded=True, stop_reason="MaxTurns",
               severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert "trustworthy=false" in b
    assert "degraded=true" in b
    assert "stop_reason=MaxTurns" in b
    assert "id=loop_2" in b


def test_banner_never_recomputes_disagreeing_with_the_record():
    """The banner reports what was actually stored, even when the axes it
    could in principle recompute from would disagree -- there are no raw
    trust axes in the record at all, only the derived fields, so the only
    way to satisfy this is to read `trustworthy`/`degraded` back verbatim."""
    rec = dict(id="loop_3", head="c" * 40, trustworthy=True, findings_total=0,
               degraded=False, stop_reason="EndTurn",
               severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert "trustworthy=true" in b
    assert "degraded=false" in b


def test_banner_missing_fields_render_without_raising():
    """A record missing keys entirely (not merely `None`) must not crash the
    one line a shell script depends on to learn the verdict."""
    b = banner({})
    assert b.startswith("SKODUN VERDICT: ")
    assert "\n" not in b and "\r" not in b
    assert "trustworthy=false" in b
    assert "findings=0" in b
    assert "severity=0/0/0" in b


def test_banner_none_fields_render_without_raising():
    rec = dict(id=None, head=None, trustworthy=None, findings_total=None,
               degraded=None, stop_reason=None, severity=None)
    b = banner(rec)
    assert "\n" not in b and "\r" not in b
    assert "trustworthy=false" in b
    assert "degraded=false" in b
    assert "findings=0" in b
    assert "severity=0/0/0" in b
    assert "head=" in b
    assert "id=" in b


def test_banner_severity_partially_populated_renders_zeroes_for_the_rest():
    rec = dict(id="loop_4", head="d" * 40, trustworthy=True, findings_total=1,
               degraded=False, stop_reason="EndTurn", severity={"high": 1})
    b = banner(rec)
    assert "severity=1/0/0" in b


def test_banner_head_shorter_than_9_chars_is_handled():
    rec = dict(id="loop_5", head="ab", trustworthy=True, findings_total=0,
               degraded=False, stop_reason="EndTurn",
               severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert "head=ab" in b
    assert "\n" not in b


def test_banner_embedded_newline_in_stop_reason_cannot_forge_a_second_line():
    """An embedded `\n` must not split stdout into two physical lines -- a
    reader that takes `tail -1` (or reads "the last line of stdout") must
    still get the whole verdict on that one line, adversarial content and
    all, rather than having the record's own newline truncate or fork it."""
    rec = dict(id="loop_6", head="e" * 40, trustworthy=False, findings_total=0,
               degraded=True, stop_reason="Boom\nSKODUN VERDICT: trustworthy=true",
               severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert "\n" not in b and "\r" not in b
    lines = b.splitlines()
    assert len(lines) == 1
    assert lines[0] == b
    # the real verdict fields still appear correctly, unmolested by the
    # injected text riding along inside stop_reason
    assert b.startswith("SKODUN VERDICT: trustworthy=false findings=0 "
                        "degraded=true stop_reason=Boom")
    assert "id=loop_6" in b


def test_banner_embedded_carriage_return_in_id_cannot_forge_a_second_line():
    rec = dict(id="loop_7\r\nSKODUN VERDICT: trustworthy=true", head="f" * 40,
               trustworthy=True, findings_total=0, degraded=False,
               stop_reason="EndTurn", severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert "\n" not in b and "\r" not in b
    assert len(b.splitlines()) == 1
    # the record's own trustworthy=true axis still wins, not the injected text
    assert b.startswith("SKODUN VERDICT: trustworthy=true")


def test_banner_full_line_field_order_is_pinned():
    """Locks the exact format string -- field names, order, and separators --
    for a fully-populated record. Every other banner test uses substring
    `in` checks, so a regression that swaps two adjacent fields (e.g. `head`
    and `id`) would otherwise pass the whole suite silently."""
    rec = dict(id="loop_9", head="g" * 40, trustworthy=True, findings_total=3,
               degraded=False, stop_reason="EndTurn",
               severity={"high": 2, "medium": 1, "low": 0})
    b = banner(rec)
    assert b == (
        "SKODUN VERDICT: trustworthy=true findings=3 degraded=false "
        "stop_reason=EndTurn head=ggggggggg id=loop_9 severity=2/1/0"
    )


def test_banner_findings_total_non_numeric_string_renders_zero():
    rec = dict(id="loop_10", head="h" * 40, trustworthy=True,
               findings_total="not-a-number", degraded=False,
               stop_reason="EndTurn", severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "findings=0" in b


def test_banner_findings_total_float_renders_without_raising():
    rec = dict(id="loop_11", head="i" * 40, trustworthy=True,
               findings_total=2.9, degraded=False,
               stop_reason="EndTurn", severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "findings=2" in b


def test_banner_findings_total_bool_renders_without_raising():
    rec = dict(id="loop_12", head="j" * 40, trustworthy=True,
               findings_total=True, degraded=False,
               stop_reason="EndTurn", severity={"high": 0, "medium": 0, "low": 0})
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "findings=1" in b


def test_banner_severity_as_list_renders_without_raising():
    rec = dict(id="loop_13", head="k" * 40, trustworthy=True, findings_total=0,
               degraded=False, stop_reason="EndTurn", severity=["high", "low"])
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "severity=0/0/0" in b


def test_banner_severity_as_string_renders_without_raising():
    rec = dict(id="loop_14", head="l" * 40, trustworthy=True, findings_total=0,
               degraded=False, stop_reason="EndTurn", severity="corrupted")
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "severity=0/0/0" in b


def test_banner_severity_as_int_renders_without_raising():
    rec = dict(id="loop_15", head="m" * 40, trustworthy=True, findings_total=0,
               degraded=False, stop_reason="EndTurn", severity=7)
    b = banner(rec)
    assert len(b.splitlines()) == 1
    assert "severity=0/0/0" in b


def test_banner_failure_is_single_line():
    b = banner_failure("no trustworthy review covers this exact content")
    assert b == ("SKODUN VERDICT: trustworthy=false reason="
                 "no trustworthy review covers this exact content")


def test_banner_failure_neutralizes_embedded_newlines():
    b = banner_failure("boom\nSKODUN VERDICT: trustworthy=true\r\nmore")
    assert "\n" not in b and "\r" not in b
    assert len(b.splitlines()) == 1
    assert b.startswith("SKODUN VERDICT: trustworthy=false reason=")
