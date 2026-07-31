"""Delivery: the undelivered query, the presentations, the acknowledgement ledger.

The whole module exists to make ONE failure impossible: a background round that
nobody was ever shown, reported as silence. Research decision 15 -- "no review
happened" is stated EXPLICITLY, never read as an absence of findings -- is what
the wording tests below pin, and the ack-ordering tests pin the other half: a
round is only recorded as delivered once it actually reached a reader.
"""

import json
import sqlite3

import pytest

from skodun import delivery
from skodun.store import Store


# --- fixtures ---------------------------------------------------------------

#: The two repositories every scoping assertion in this file is written
#: against. A literal is enough HERE because nothing in this module shells out
#: to git -- `repo` is an opaque string to the delivery SQL, and it is the
#: TRANSPORTS (`cli._cmd_surface`, `mcpserver._handle_surface`) that must turn a
#: checkout path into `gitio.git_common_dir`. Their tests use real repositories.
REPO_A = "/repos/a"
REPO_B = "/repos/b"


def _rec(**kw) -> dict:
    """A finalized background round, in the shape the worker persists."""
    rec = dict(
        id="sk_1", reviewed_at="2026-07-30T10:00:00Z", branch="b", head="h" * 40,
        base_ref="origin/main", base_sha="s" * 40, diff_hash="d" * 40,
        context_hash="", mode="prepush", source="skodun", model="m",
        adapter="grok", status="clean", parse_ok=True, degraded=False,
        degraded_reason="", diff_truncated=False, stop_reason="EndTurn",
        summary="ok", findings=[], findings_total=0,
        severity={"high": 0, "medium": 0, "low": 0}, failure_reason="",
        usable_output=True, superseded_by=None, repo=REPO_A)
    rec.update(kw)
    return rec


def _finding(**kw) -> dict:
    f = dict(file="a.py", line=3, severity="high", category="bug",
             title="NPE on the empty path", detail="why")
    f.update(kw)
    return f


def _save(st, **kw) -> dict:
    rec = _rec(**kw)
    st.save_review(rec)
    return rec


def _store(tmp_path) -> Store:
    return Store.open(tmp_path / "s.db")


def _deliveries(path) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM deliveries ORDER BY review_id")]
    finally:
        conn.close()


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows]


def _evidence():
    from skodun.dispatch import DedupEvidence
    return DedupEvidence(enabled=False, valid=False, candidate_context_hash=None)


def _reserve(st, **kw):
    args = dict(branch="b", head="h" * 40, base_ref="origin/main",
                base_sha="s" * 40, diff_hash="d" * 40, worst_runtime_sec=99,
                evidence=_evidence(), repo=REPO_A)
    args.update(kw)
    return st.reserve_prepush(**args)


# --- the exact reserved wording ---------------------------------------------


def test_the_reserved_line_is_byte_exact():
    """Research decision 15's line, verbatim. It is a CONSTANT, not a format
    string, because the whole point is that a reader recognises it."""
    assert delivery.NO_REVIEW_LINE == (
        "NO REVIEW HAPPENED — this round reports nothing because it said "
        "nothing, not because it found nothing")


# --- eligibility ------------------------------------------------------------


def test_undelivered_returns_terminal_skodun_prepush_rounds_oldest_first(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_b", reviewed_at="2026-07-30T11:00:00Z")
        _save(st, id="sk_a", reviewed_at="2026-07-30T10:00:00Z")
        assert _ids(delivery.undelivered(st, "b", REPO_A)) == ["sk_a", "sk_b"]


@pytest.mark.parametrize("status", ["clean", "degraded", "failed", "superseded"])
def test_every_terminal_status_is_eligible(tmp_path, status):
    with _store(tmp_path) as st:
        _save(st, status=status, parse_ok=status == "clean",
              degraded=status == "degraded", usable_output=status == "clean")
        assert _ids(delivery.undelivered(st, "b", REPO_A)) == ["sk_1"]


def test_a_running_round_is_invisible_and_becomes_visible_when_it_finalizes(tmp_path):
    """THE transition test. A round is delivered only after its story is final:
    a `running` row is an in-flight review, and acknowledging one would burn the
    delivery of a review that has not happened yet."""
    with _store(tmp_path) as st:
        res = _reserve(st)
        assert delivery.undelivered(st, "b", REPO_A) == []

        reserved = st.get_review(res.record_id)
        final = dict(reserved, status="clean", parse_ok=True, usable_output=True,
                     summary="ok", findings=[], findings_total=0,
                     severity={"high": 0, "medium": 0, "low": 0})
        assert st.finalize_review(res.record_id, final) is True

        assert _ids(delivery.undelivered(st, "b", REPO_A)) == [res.record_id]


def test_a_legacy_imported_prepush_round_never_surfaces(tmp_path):
    """The imported-prepush regression. A legacy archive holds thousands of
    `mode=prepush` rows; surfacing them would flood the first post-upgrade
    session with rounds from months ago -- and the ledger cannot un-flood it."""
    with _store(tmp_path) as st:
        _save(st, id="sk_mine")
        legacy = _rec(id="legacy_1", source="legacy", findings_total=2,
                      findings=[_finding(), _finding()])
        legacy.pop("usable_output")      # it predates the concept entirely
        st.save_review(legacy)
        assert _ids(delivery.undelivered(st, "b", REPO_A)) == ["sk_mine"]


def test_a_foreground_round_never_surfaces(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_now", mode="now", findings_total=1,
              findings=[_finding()])
        assert delivery.undelivered(st, "b", REPO_A) == []


def test_another_branchs_rounds_never_surface(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_other", branch="other")
        assert delivery.undelivered(st, "b", REPO_A) == []


def test_surface_never_delivers_another_repositorys_rounds(tmp_path):
    """One store, two repositories, the same branch. Surfacing A must not
    render B's round -- and must not ACKNOWLEDGE it, which is what left the
    other repository's session with nothing to show."""
    with _store(tmp_path) as st:
        _save(st, id="sk_b", repo=REPO_B)

        assert delivery.undelivered(st, "b", REPO_A) == []

        status, text, pending = delivery.surface(st, "b", REPO_A)
        assert "sk_b" not in text
        assert pending == []
        assert _deliveries(tmp_path / "s.db") == [], (
            "another repository's round was acknowledged")

        # The REPLAY sibling is built from the same select and must be scoped
        # with it: `--include-delivered` reaching across repositories would
        # render B's history into A's session.
        assert delivery.surface(st, "b", REPO_A, include_delivered=True).text == ""
        assert _ids(delivery.undelivered(st, "b", REPO_B)) == ["sk_b"]


def test_a_pre_v5_row_is_reachable_from_no_repository_at_all(tmp_path):
    """The fail-closed NULL rule, at the delivery seam. `repo = ?` never matches
    NULL, so a row written before v5 is invisible to every scoped surface --
    deliberately: guessing which repository an unstamped round belongs to is how
    one repository's session gets another's findings."""
    with _store(tmp_path) as st:
        rec = _rec(id="sk_old", findings=[_finding()], findings_total=1)
        rec.pop("repo")
        st.save_review(rec)
        assert st._c.execute(
            "SELECT repo FROM reviews WHERE id=?", ("sk_old",)
        ).fetchone()["repo"] is None
        assert delivery.undelivered(st, "b", REPO_A) == []
        assert delivery.undelivered(st, "b", REPO_B) == []
        assert delivery.surface(st, "b", REPO_A, include_delivered=True).text == ""
        assert _deliveries(tmp_path / "s.db") == []


def test_an_acknowledged_round_stops_being_undelivered(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_1", findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        assert _ids(delivery.undelivered(st, "b", REPO_A)) == ["sk_1"]
        delivery.acknowledge(st, ["sk_1"], "cli-text")
        assert delivery.undelivered(st, "b", REPO_A) == []


# --- the presentations ------------------------------------------------------


def _surface(st, branch="b", repo=REPO_A, **kw):
    return delivery.surface(st, branch, repo, **kw)


def test_no_usable_output_prints_the_reserved_line_plus_its_failure_reason(tmp_path):
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=False,
              failure_reason="the background review failed: Timeout()")
        out = _surface(st)
        assert delivery.NO_REVIEW_LINE in out.text
        assert "failure_reason: the background review failed: Timeout()" in out.text
        assert out.pending_acks == ["sk_1"]


def test_the_reserved_line_is_absent_from_a_degraded_round(tmp_path):
    """A degraded round DID say something. Printing the no-review line over it
    would contradict its own artifact -- and hide real evidence."""
    with _store(tmp_path) as st:
        _save(st, status="degraded", parse_ok=True, degraded=True,
              degraded_reason="the refuter pass timed out", usable_output=True,
              findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        out = _surface(st)
        assert delivery.NO_REVIEW_LINE not in out.text
        assert "NO REVIEW HAPPENED" not in out.text


def test_a_degraded_round_renders_its_findings_under_the_warning(tmp_path):
    with _store(tmp_path) as st:
        _save(st, status="degraded", parse_ok=True, degraded=True,
              degraded_reason="the refuter pass timed out", usable_output=True,
              summary="one real problem", findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        text = _surface(st).text
        assert delivery.INCOMPLETE_WARNING in text
        assert "cannot certify" in delivery.INCOMPLETE_WARNING
        assert "degraded_reason: the refuter pass timed out" in text
        assert "NPE on the empty path" in text
        assert "a.py:3" in text


def test_a_failed_aggregate_whose_batches_answered_is_partial_evidence(tmp_path):
    """The zero-finding regression, and the exact shape Task 8 produces: three
    batches answered "nothing wrong", the cross-file pass failed. Zero findings,
    untrustworthy -- and emphatically NOT a round that said nothing."""
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=True,
              findings=[], findings_total=0,
              failure_reason="one or more batches were not reviewed "
                             "(integration: no review produced)")
        text = _surface(st).text
        assert delivery.NO_REVIEW_LINE not in text
        assert delivery.INCOMPLETE_WARNING in text
        assert "failure_reason: one or more batches were not reviewed" in text


def test_a_superseded_round_names_the_superseding_record(tmp_path):
    with _store(tmp_path) as st:
        _save(st, status="superseded", parse_ok=False, usable_output=False,
              superseded_by="sk_newer")
        text = _surface(st).text
        assert "sk_newer" in text
        assert delivery.NO_REVIEW_LINE not in text
        # ONE line for the round itself, plus the header and the footer.
        rounds = [ln for ln in text.splitlines() if ln.startswith("  - ")]
        assert len(rounds) == 1
        assert len([ln for ln in text.splitlines() if ln.startswith("      ")]) == 0


def test_a_superseded_round_reads_the_persisted_field_never_the_branch(tmp_path):
    """`superseded_by` is written atomically by the reservation transaction. A
    round with no such field is reported as superseded WITHOUT inventing an id."""
    with _store(tmp_path) as st:
        _save(st, status="superseded", parse_ok=False, usable_output=False,
              superseded_by=None)
        text = _surface(st).text
        assert "superseded" in text
        assert "None" not in text


def test_a_trustworthy_round_with_findings_renders_them_normally(tmp_path):
    with _store(tmp_path) as st:
        _save(st, status="clean", summary="two problems",
              findings=[_finding(), _finding(severity="low", title="nit")],
              findings_total=2, severity={"high": 1, "medium": 0, "low": 1})
        text = _surface(st).text
        assert "2 finding(s)" in text
        assert "1 high / 0 medium / 1 low" in text
        assert delivery.NO_REVIEW_LINE not in text
        assert delivery.INCOMPLETE_WARNING not in text
        assert "skodun triage --list sk_1" in text


def test_a_quiet_round_says_nothing_and_is_acknowledged_immediately(tmp_path):
    """Trustworthy, zero findings: there is nothing a reader must act on, and
    re-scanning it at every session start forever is pure waste. Nothing
    deliverable can be lost by marking it now."""
    with _store(tmp_path) as st:
        _save(st, status="clean", findings=[], findings_total=0)
        out = _surface(st)
        assert out.text == ""
        assert out.pending_acks == []
        rows = _deliveries(tmp_path / "s.db")
        assert [(r["review_id"], r["channel"]) for r in rows] == [("sk_1", "quiet")]


def test_a_content_bearing_round_is_NOT_acknowledged_by_the_service(tmp_path):
    """The service renders; the transport acknowledges after its own write
    succeeds. Marking here would lose a report dropped on the way out."""
    with _store(tmp_path) as st:
        _save(st, findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        out = _surface(st)
        assert out.pending_acks == ["sk_1"]
        assert _deliveries(tmp_path / "s.db") == []


def test_nothing_to_report_is_empty_text_not_an_empty_envelope(tmp_path):
    with _store(tmp_path) as st:
        assert _surface(st).text == ""
        assert _surface(st, fmt="claude").text == ""


# --- the usable_output signal is never the finding count --------------------


def test_the_signal_is_the_field_not_the_finding_count(tmp_path):
    """THREE rounds, every one of them with zero findings, and the count cannot
    tell them apart:

      * `sk_clean`  -- trustworthy and clean: nothing to report.
      * `sk_mute`   -- nothing answered anywhere: the reserved line.
      * `sk_partial`-- a batched aggregate whose batches all answered "nothing
                       wrong" under a failed integration pass. `parse_ok` is
                       False (the aggregate rule is ALL passes), the count is 0,
                       and it is emphatically not a round that said nothing.

    Only the persisted field separates the last two, which is exactly why the
    field exists: the derivation `parse_ok or findings_total > 0` gets `sk_partial`
    WRONG, and reading the count alone gets both of them wrong."""
    with _store(tmp_path) as st:
        _save(st, id="sk_clean", status="clean", findings_total=0,
              usable_output=True)
        _save(st, id="sk_mute", status="failed", parse_ok=False,
              usable_output=False, findings_total=0,
              failure_reason="the worker was killed")
        _save(st, id="sk_partial", status="failed", parse_ok=False,
              usable_output=True, findings_total=0,
              failure_reason="one or more batches were not reviewed")
        text = _surface(st).text
        assert text.count(delivery.NO_REVIEW_LINE) == 1
        assert "sk_mute" in text and "sk_clean" not in text
        assert "sk_partial" in text
        assert text.count(delivery.INCOMPLETE_WARNING) == 1


def test_a_self_contradicting_record_shows_its_evidence(tmp_path):
    """`usable_output=False` beside real findings is unreachable through any
    writer -- findings can only come from a pass that answered -- so it means the
    artifact contradicts itself. The reserved line would then be a banner
    contradicting the record it summarises, and it would DROP the findings with
    it, so the evidence wins.

    This can only ever move a round from the reserved line TOWARD showing what it
    has. A zero-finding round can never be talked out of the reserved line, which
    is the false-clear direction."""
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=False,
              findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0},
              failure_reason="the integration pass produced no review")
        text = _surface(st).text
        assert delivery.NO_REVIEW_LINE not in text
        assert delivery.INCOMPLETE_WARNING in text
        assert "NPE on the empty path" in text


@pytest.mark.parametrize("parse_ok, total, usable", [
    (True, 0, True),        # a pre-Phase-3 round that parsed: it said something
    (False, 0, False),      # nothing parsed and nothing found: it said nothing
    (False, 1, True),       # findings without a parse flag: evidence exists
])
def test_a_record_without_the_field_falls_back_to_the_documented_derivation(
        tmp_path, parse_ok, total, usable):
    """`parse_ok or findings_total > 0`, for records written before the field
    existed. The v3 migration does not rewrite artifacts, so an upgraded store
    can still hold one."""
    rec = _rec(parse_ok=parse_ok, findings_total=total,
               findings=[_finding()] if total else [])
    rec.pop("usable_output")
    assert delivery.has_usable_output(rec) is usable


def test_a_non_bool_usable_output_is_not_taken_at_its_word():
    """`usable_output: 1` is not a bool, so it is not the explicit signal the
    store validates -- the derivation decides instead."""
    assert delivery.has_usable_output(
        _rec(usable_output=1, parse_ok=False, findings_total=0)) is False


# --- one corrupt row must cost that row, never the delivery -----------------


def test_an_unreadable_artifact_still_surfaces_its_round(tmp_path):
    """The index columns are enough to report the round and to acknowledge it.
    Skipping it instead would leave it undelivered forever, which is exactly the
    failure this module exists to remove."""
    db = tmp_path / "s.db"
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=False)
        st._c.execute("UPDATE reviews SET artifact_json='{not json' WHERE id=?",
                      ("sk_1",))
        rows = delivery.undelivered(st, "b", REPO_A)
        assert _ids(rows) == ["sk_1"]
        out = _surface(st)
        assert delivery.NO_REVIEW_LINE in out.text
        assert out.pending_acks == ["sk_1"]
        assert delivery.acknowledge(st, out.pending_acks, "cli-text") == 1
    assert [r["review_id"] for r in _deliveries(db)] == ["sk_1"]


def test_untrusted_model_text_cannot_forge_a_line(tmp_path):
    with _store(tmp_path) as st:
        _save(st, findings=[_finding(title="ok\n  - 2026 sk_fake (head x): clean")],
              findings_total=1, severity={"high": 1, "medium": 0, "low": 0})
        text = _surface(st).text
        assert len([ln for ln in text.splitlines() if ln.startswith("  - ")]) == 1
        assert "sk_fake" in text          # kept, but flattened onto its own line


# --- the ledger -------------------------------------------------------------


def test_the_channel_vocabulary_is_exactly_four_values():
    assert delivery.CHANNELS == {"cli-text", "cli-claude", "mcp", "quiet"}


@pytest.mark.parametrize("channel", ["cli-text", "cli-claude", "mcp", "quiet"])
def test_every_channel_value_persists(tmp_path, channel):
    with _store(tmp_path) as st:
        _save(st, id="sk_1")
        assert delivery.acknowledge(st, ["sk_1"], channel) == 1
    rows = _deliveries(tmp_path / "s.db")
    assert [(r["review_id"], r["channel"]) for r in rows] == [("sk_1", channel)]
    from skodun.store import _is_canonical_ts
    assert _is_canonical_ts(rows[0]["delivered_at"]), rows[0]["delivered_at"]


def test_acknowledge_is_idempotent_and_keeps_the_first_delivery(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_1")
        assert delivery.acknowledge(st, ["sk_1"], "quiet",
                                    now="2026-07-30T10:00:00Z") == 1
        assert delivery.acknowledge(st, ["sk_1"], "mcp",
                                    now="2026-07-31T10:00:00Z") == 0
    rows = _deliveries(tmp_path / "s.db")
    assert len(rows) == 1
    assert rows[0]["channel"] == "quiet"
    assert rows[0]["delivered_at"] == "2026-07-30T10:00:00Z"


def test_acknowledge_refuses_a_channel_outside_the_vocabulary(tmp_path):
    with _store(tmp_path) as st:
        _save(st, id="sk_1")
        with pytest.raises(ValueError, match="channel"):
            delivery.acknowledge(st, ["sk_1"], "email")
    assert _deliveries(tmp_path / "s.db") == []


def test_acknowledge_refuses_an_unusable_id(tmp_path):
    with _store(tmp_path) as st:
        with pytest.raises(ValueError):
            delivery.acknowledge(st, ["sk_1", ""], "mcp")
    assert _deliveries(tmp_path / "s.db") == []


def test_acknowledging_nothing_is_a_no_op(tmp_path):
    with _store(tmp_path) as st:
        assert delivery.acknowledge(st, [], "mcp") == 0
    assert _deliveries(tmp_path / "s.db") == []


def test_the_format_to_channel_mapping(tmp_path):
    assert delivery.channel_for_format("text") == "cli-text"
    assert delivery.channel_for_format("claude") == "cli-claude"
    with pytest.raises(ValueError):
        delivery.channel_for_format("json")


# --- replay -----------------------------------------------------------------


def test_include_delivered_replays_an_already_delivered_round(tmp_path):
    with _store(tmp_path) as st:
        _save(st, findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        delivery.acknowledge(st, ["sk_1"], "cli-text", now="2026-07-30T10:00:00Z")
        assert _surface(st).text == ""
        replay = _surface(st, include_delivered=True)
        assert "NPE on the empty path" in replay.text
        assert "cli-text" in replay.text and "2026-07-30T10:00:00Z" in replay.text


def test_replaying_a_quiet_round_still_prints_nothing(tmp_path):
    with _store(tmp_path) as st:
        _save(st)
        delivery.acknowledge(st, ["sk_1"], "quiet")
        assert _surface(st, include_delivered=True).text == ""


# --- the claude envelope ----------------------------------------------------


def test_the_claude_envelope_carries_both_keys_and_the_body(tmp_path):
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=False,
              failure_reason="the worker was killed")
        out = _surface(st, fmt="claude")
        payload = json.loads(out.text)
        assert set(payload) == {"systemMessage", "hookSpecificOutput"}
        hook = payload["hookSpecificOutput"]
        assert hook["hookEventName"] == "SessionStart"
        assert delivery.NO_REVIEW_LINE in hook["additionalContext"]
        assert isinstance(payload["systemMessage"], str)
        assert payload["systemMessage"]


def test_the_claude_envelope_is_one_ascii_line(tmp_path):
    """A hook's stdout is decoded by another process whose locale is not ours,
    and the reserved line carries an em dash. `ensure_ascii` keeps the envelope
    readable everywhere; one line keeps it parseable line-by-line."""
    with _store(tmp_path) as st:
        _save(st, status="failed", parse_ok=False, usable_output=False)
        text = _surface(st, fmt="claude").text
        assert text.endswith("\n") and text.count("\n") == 1
        text.encode("ascii")           # raises if any non-ASCII survived


def test_both_formats_render_the_same_body(tmp_path):
    with _store(tmp_path) as st:
        _save(st, findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        plain = _surface(st).text
        envelope = json.loads(_surface(st, fmt="claude").text)
        assert envelope["hookSpecificOutput"]["additionalContext"] == plain


def test_an_unknown_format_is_refused(tmp_path):
    with _store(tmp_path) as st:
        _save(st, findings=[_finding()], findings_total=1)
        with pytest.raises(ValueError):
            _surface(st, fmt="yaml")


# --- the service contract Task 14 consumes ----------------------------------


def test_surface_returns_the_status_text_pending_acks_triple(tmp_path):
    with _store(tmp_path) as st:
        _save(st, findings=[_finding()], findings_total=1)
        status, text, pending = delivery.surface(st, "b", REPO_A)
        assert status == 0
        assert text
        assert pending == ["sk_1"]


def test_a_failure_to_acknowledge_quiet_rounds_never_costs_the_report(tmp_path):
    """A quiet ack is an optimisation. A content-bearing report is the product."""
    with _store(tmp_path) as st:
        _save(st, id="sk_quiet")
        _save(st, id="sk_loud", reviewed_at="2026-07-30T11:00:00Z",
              findings=[_finding()], findings_total=1)
        st._c.execute("CREATE TRIGGER no_ack BEFORE INSERT ON deliveries BEGIN"
                      " SELECT RAISE(ABORT, 'nope'); END")
        out = _surface(st)
        assert out.pending_acks == ["sk_loud"]
        assert "NPE on the empty path" in out.text


# ==========================================================================
# The hook templates
# ==========================================================================
#
# They are EXAMPLES, installed by instruction. skodun never writes one into a
# repository: `install-hooks` installs the pre-push shim and nothing else,
# because what appears at the start of somebody's session is their decision. The
# tests below pin the three properties a template has to have -- it runs, it
# passes the payload through untouched, and it cannot break a session.

import os
import stat
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parents[1] / "examples" / "hooks"
_TEMPLATES = {"claude": _HOOKS_DIR / "sessionstart-claude.sh",
              "text": _HOOKS_DIR / "sessionstart-plain.sh"}


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_the_template_is_a_runnable_script(fmt):
    path = _TEMPLATES[fmt]
    assert path.is_file(), path
    assert path.read_text(encoding="utf-8").startswith("#!"), path
    assert path.stat().st_mode & stat.S_IXUSR, f"{path} is not executable"
    subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True)


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_the_template_calls_surface_with_its_own_format(fmt):
    body = _TEMPLATES[fmt].read_text(encoding="utf-8")
    assert f"surface --hook-format {fmt}" in body
    other = "text" if fmt == "claude" else "claude"
    assert f"--hook-format {other}" not in body


def _fake_skodun(tmp_path, body: str) -> Path:
    """A `skodun` on PATH that does whatever `body` says."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "skodun"
    fake.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)
    return bin_dir


def _repo(tmp_path) -> Path:
    """A one-commit repo on `main`, with a hermetic git config.

    The commit is not decoration: `git rev-parse --abbrev-ref HEAD` fails on a
    repository with no commits at all, so a bare `git init` would exercise the
    could-not-work-out-the-branch path instead of the delivery path.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["GIT_CONFIG_SYSTEM"] = str(tmp_path / "gitsystem")
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    (tmp_path / "gitsystem").write_text("", encoding="utf-8")
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=env)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c0"], check=True,
                   capture_output=True, env=env)
    return repo


def _run_template(fmt, cwd, bin_dir=None, **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("SKODUN_BIN", None)
    if bin_dir is not None:
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    env.update(extra)
    return subprocess.run([str(_TEMPLATES[fmt])], capture_output=True, text=True,
                          cwd=str(cwd), env=env, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_the_template_passes_the_payload_through_untouched(tmp_path, fmt):
    bin_dir = _fake_skodun(
        tmp_path, 'printf "%s\\n" "$*"; printf "PAYLOAD-LINE-2\\n"')
    p = _run_template(fmt, _repo(tmp_path), bin_dir)
    assert p.returncode == 0, p.stderr
    assert p.stdout == f"surface --hook-format {fmt}\nPAYLOAD-LINE-2\n"


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_a_failing_skodun_never_fails_the_session(tmp_path, fmt):
    """A hook that breaks a session start is a hook that gets deleted -- taking
    the delivery of every future finding with it."""
    bin_dir = _fake_skodun(tmp_path, 'echo "boom" >&2; exit 2')
    p = _run_template(fmt, _repo(tmp_path), bin_dir)
    assert p.returncode == 0, p.stderr
    assert "boom" in p.stderr


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_no_skodun_at_all_is_still_exit_zero(tmp_path, fmt):
    """A machine where skodun is not installed: the hook must be inert, not
    broken. The PATH below carries exactly `bash` and `git` -- enough for the
    template to run and to recognise the checkout -- and NEITHER `skodun` nor
    `python3`, which is the case under test."""
    import shutil

    bin_dir = tmp_path / "sparse-bin"
    bin_dir.mkdir()
    for tool in ("bash", "git"):
        found = shutil.which(tool)
        assert found, f"the test host has no {tool}"
        (bin_dir / tool).symlink_to(found)
    env = dict(os.environ)
    env.pop("SKODUN_BIN", None)
    repo = _repo(tmp_path)
    p = subprocess.run([str(_TEMPLATES[fmt])], capture_output=True, text=True,
                       cwd=str(repo), stdin=subprocess.DEVNULL,
                       env={**env, "PATH": str(bin_dir)})
    assert p.returncode == 0, p.stderr
    assert p.stdout == ""


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_outside_a_git_checkout_the_template_says_nothing(tmp_path, fmt):
    bin_dir = _fake_skodun(tmp_path, 'echo "SHOULD NOT RUN"')
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    p = _run_template(fmt, outside, bin_dir, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert p.returncode == 0, p.stderr
    assert p.stdout == ""


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_skodun_bin_overrides_the_path(tmp_path, fmt):
    """The override exists for a virtualenv install that is not on the PATH a
    session-start hook happens to inherit."""
    on_path = _fake_skodun(tmp_path, 'echo "WRONG ONE"')
    chosen = tmp_path / "chosen.sh"
    chosen.write_text('#!/usr/bin/env bash\necho "RIGHT ONE $*"\n', encoding="utf-8")
    chosen.chmod(0o755)
    p = _run_template(fmt, _repo(tmp_path), on_path, SKODUN_BIN=str(chosen))
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith("RIGHT ONE surface --hook-format")


@pytest.mark.parametrize("fmt", sorted(_TEMPLATES))
def test_the_template_writes_nothing_into_the_repository(tmp_path, fmt):
    bin_dir = _fake_skodun(tmp_path, 'echo "a report"')
    repo = _repo(tmp_path)
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    p = _run_template(fmt, repo, bin_dir)
    assert p.returncode == 0, p.stderr
    after = sorted(q.relative_to(repo).as_posix() for q in repo.rglob("*"))
    assert after == before


def test_skodun_itself_never_installs_a_delivery_hook():
    """`install-hooks` installs the pre-push shim and NOTHING else. If any module
    ever learns these filenames, delivery has stopped being the user's choice."""
    src = Path(__file__).resolve().parents[1] / "src" / "skodun"
    offenders = [p.name for p in src.rglob("*.py")
                 if "sessionstart" in p.read_text(encoding="utf-8")]
    assert offenders == [], offenders


def test_the_template_end_to_end_against_the_real_command(tmp_path):
    """One real pass: a store with one failed round, the actual CLI behind the
    template, and the reserved line arriving inside a valid SessionStart
    envelope."""
    from skodun import gitio

    repo = _repo(tmp_path)
    db = tmp_path / "s.db"
    with Store.open(db) as st:
        # The REAL common dir, because a real `skodun surface` is what resolves
        # it here: a literal `REPO_A` would make this pass render nothing and
        # the assertions below would be about an empty envelope.
        rec = _rec(branch="main", status="failed", parse_ok=False,
                   usable_output=False, repo=str(gitio.git_common_dir(repo)),
                   failure_reason="the background review failed: Timeout()")
        st.save_review(rec)
    src = str(Path(__file__).resolve().parents[1] / "src")
    bin_dir = _fake_skodun(
        tmp_path, f'exec {sys.executable} -m skodun "$@"')
    env = {"SKODUN_DB": str(db),
           "PYTHONPATH": src,
           "PYTHONUNBUFFERED": "1"}
    p = _run_template("claude", repo, bin_dir, **env)
    assert p.returncode == 0, p.stderr
    payload = json.loads(p.stdout)
    assert delivery.NO_REVIEW_LINE in (
        payload["hookSpecificOutput"]["additionalContext"])
    with Store.open(db) as st:
        rows = [(r["review_id"], r["channel"]) for r in st._c.execute(
            "SELECT review_id, channel FROM deliveries")]
    assert rows == [("sk_1", "cli-claude")]


def test_acknowledge_counts_only_the_rounds_it_actually_recorded(tmp_path):
    """The return value is what a caller reports, so it has to be the number of
    NEW ledger rows -- `executemany`'s aggregate rowcount, not the id count."""
    with _store(tmp_path) as st:
        _save(st, id="sk_1")
        _save(st, id="sk_2", reviewed_at="2026-07-30T11:00:00Z")
        assert delivery.acknowledge(st, ["sk_1"], "mcp") == 1
        assert delivery.acknowledge(st, ["sk_1", "sk_2"], "mcp") == 1
    assert len(_deliveries(tmp_path / "s.db")) == 2


def test_a_superseded_round_that_somehow_carries_findings_shows_them(tmp_path):
    """Unreachable through any writer -- only `running` rows are superseded, and a
    running row has no findings. Guarded in the direction that shows MORE, because
    the cost of being wrong is a real finding rendered as one line of bookkeeping
    and then never seen again."""
    with _store(tmp_path) as st:
        _save(st, status="superseded", parse_ok=True, usable_output=True,
              superseded_by="sk_newer", findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0},
              degraded=True, degraded_reason="retired mid-flight")
        text = _surface(st).text
        assert "sk_newer" in text
        assert delivery.INCOMPLETE_WARNING in text
        assert "NPE on the empty path" in text
        assert delivery.NO_REVIEW_LINE not in text


def test_an_unreadable_count_is_never_quiet(tmp_path):
    """`findings_total: "3"` beside three real findings. Reading the count with
    the project's display rule would render `0` and acknowledge a round with
    three findings as quiet, unseen. "Cannot tell" must never resolve to
    "nothing to see"."""
    with _store(tmp_path) as st:
        rec = _rec(status="clean", findings=[_finding(), _finding(), _finding()],
                   findings_total=0, severity={"high": 3, "medium": 0, "low": 0})
        st.save_review(rec)
        # The stored ARTIFACT is what a reader sees, and only the COLUMN is
        # normalised to an int -- so corrupt the artifact the way a hand-edited or
        # foreign record would be.
        st._c.execute("UPDATE reviews SET artifact_json="
                      "json_set(artifact_json, '$.findings_total', '3')"
                      " WHERE id=?", ("sk_1",))
        out = _surface(st)
        assert out.pending_acks == ["sk_1"]
        assert "an unreadable number of finding(s)" in out.text
        assert out.text.count("NPE on the empty path") == 3
    assert _deliveries(tmp_path / "s.db") == []


def test_an_absent_count_with_no_findings_is_still_quiet(tmp_path):
    """The other direction: absent means zero, exactly as every other reader in
    the project treats it. A clean round stays silent."""
    with _store(tmp_path) as st:
        rec = _rec(status="clean")
        rec.pop("findings_total")
        st.save_review(rec)
        st._c.execute("UPDATE reviews SET artifact_json="
                      "json_remove(artifact_json, '$.findings_total')"
                      " WHERE id=?", ("sk_1",))
        out = _surface(st)
        assert out.text == ""
        assert out.pending_acks == []
    assert [(r["review_id"], r["channel"])
            for r in _deliveries(tmp_path / "s.db")] == [("sk_1", "quiet")]


# ==========================================================================
# Oracle parity, and the two DELIBERATE divergences
# ==========================================================================
#
# The oracle's own SessionStart hook is the reference for the spirit of this
# surface, and the tests below EXECUTE it rather than paraphrase it: its delivery
# logic is a Python block embedded in the hook, so it is extracted and run over a
# synthesized archive. Where it and this module disagree, the disagreement is
# owner-ratified and pinned here in both directions -- an unpinned divergence is
# indistinguishable from a bug next time somebody reads the oracle.

from tests.conftest import oracle_dir

ORACLE_HOOK = ((oracle_dir() / ".claude" / "hooks" / "surface-grok-findings.sh")
               if oracle_dir() else None)
_NO_ORACLE = ORACLE_HOOK is None or not ORACLE_HOOK.exists()
requires_oracle = pytest.mark.skipif(
    _NO_ORACLE, reason="oracle checkout not present (set SKODUN_ORACLE_DIR)")


def _oracle_delivery_block() -> str:
    """The hook's embedded Python, extracted from its `cat > "$_py" <<'PY'`
    heredoc. Running the hook itself would need a git checkout, a `.grok-reviews`
    directory beside it and `jq`; the block is the part that decides what a reader
    is told."""
    body = ORACLE_HOOK.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(body) if ln.strip().endswith("<<'PY'"))
    end = next(i for i, ln in enumerate(body[start + 1:], start + 1)
               if ln.strip() == "PY")
    return "\n".join(body[start + 1:end]) + "\n"


def _run_oracle(tmp_path, rows, branch="feat") -> tuple[str, list[str]]:
    """The oracle's report for `rows`, plus its `surfaced.txt` afterwards."""
    data = tmp_path / ".grok-reviews"
    data.mkdir(parents=True, exist_ok=True)
    (data / "index.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    script = tmp_path / "oracle_delivery.py"
    script.write_text(_oracle_delivery_block(), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        env={**os.environ, "GROK_DATA_DIR": str(data), "GROK_BRANCH": branch})
    assert proc.returncode == 0, proc.stderr
    seen = (data / "surfaced.txt")
    marked = ([ln.strip() for ln in seen.read_text(encoding="utf-8").splitlines()
               if ln.strip()] if seen.exists() else [])
    return proc.stdout, marked


def _legacy_row(**kw) -> dict:
    row = dict(id="g_1", branch="feat", mode="prepush",
               reviewed_at="2026-07-30T10:00:00Z", head="abc123def456",
               trustworthy=True, parse_ok=True, degraded=False,
               diff_truncated=False, findings_total=0,
               severity={"high": 0, "medium": 0, "low": 0}, summary="")
    row.update(kw)
    return row


@requires_oracle
def test_known_divergence_the_oracle_hides_a_degraded_rounds_findings(tmp_path):
    """THE divergence this task exists to correct, executed on both sides.

    The oracle branches on TRUSTWORTHINESS and `continue`s, so a degraded round
    that produced two real findings prints its no-review line -- claiming the
    round "reports 0 findings because it said nothing" -- and the two findings and
    the summary are never printed at all. That banner contradicts the artifact it
    summarises, and the evidence it drops is the whole reason a reader was
    interrupted.

    skodun branches on the explicit `usable_output` signal instead: the same round
    renders under an incomplete-cannot-certify warning WITH its findings, and the
    reserved line stays reserved for rounds that really did say nothing. This is
    the brief's owner-ratified inversion, not an accident of implementation.
    """
    row = _legacy_row(trustworthy=False, degraded=True, findings_total=2,
                      degraded_reason="the refuter pass timed out",
                      summary="two real problems",
                      severity={"high": 1, "medium": 1, "low": 0})
    report, marked = _run_oracle(tmp_path, [row])

    assert "NO REVIEW HAPPENED" in report
    assert "two real problems" not in report        # the evidence, dropped
    assert "2 finding(s)" not in report
    assert marked == []                             # content is acked by the caller

    with _store(tmp_path) as st:
        _save(st, id="sk_1", branch="feat", status="degraded", parse_ok=True,
              degraded=True, degraded_reason="the refuter pass timed out",
              usable_output=True, summary="two real problems",
              findings=[_finding(), _finding(severity="medium", title="second")],
              findings_total=2, severity={"high": 1, "medium": 1, "low": 0})
        out = delivery.surface(st, "feat", REPO_A)
    assert delivery.NO_REVIEW_LINE not in out.text
    assert delivery.INCOMPLETE_WARNING in out.text
    assert "two real problems" in out.text
    assert "second" in out.text
    assert out.pending_acks == ["sk_1"]


@requires_oracle
def test_parity_the_ack_split_is_the_oracles_own(tmp_path):
    """Quiet rounds acknowledged immediately, content-bearing rounds left for
    whoever performs the write: the oracle already works this way, and the brief's
    "only after the emit succeeds" is that rule made explicit rather than a new
    one. Both sides, one fixture."""
    quiet = _legacy_row(id="g_quiet")
    loud = _legacy_row(id="g_loud", reviewed_at="2026-07-30T11:00:00Z",
                       findings_total=1, summary="one problem",
                       severity={"high": 1, "medium": 0, "low": 0})
    report, marked = _run_oracle(tmp_path, [quiet, loud])

    assert marked == ["g_quiet"]                   # marked before any emit
    assert report.splitlines()[0].split() == ["g_loud"]   # awaiting the caller
    assert "g_quiet" not in report

    with _store(tmp_path) as st:
        _save(st, id="sk_quiet", branch="feat")
        _save(st, id="sk_loud", branch="feat", reviewed_at="2026-07-30T11:00:00Z",
              summary="one problem", findings=[_finding()], findings_total=1,
              severity={"high": 1, "medium": 0, "low": 0})
        out = delivery.surface(st, "feat", REPO_A)
        rows = [(r["review_id"], r["channel"]) for r in st._c.execute(
            "SELECT review_id, channel FROM deliveries")]
    assert rows == [("sk_quiet", "quiet")]
    assert out.pending_acks == ["sk_loud"]
    assert "sk_quiet" not in out.text


@requires_oracle
def test_parity_an_unreadable_count_is_reported_as_findings_present(tmp_path):
    """The oracle's hard-won rule, kept: `_count` returns -1 for a corrupt count
    and the round is reported with "an unreadable number of" rather than skipped
    as clean. `_has_evidence`/`_count_phrase` are skodun's spelling of it."""
    row = _legacy_row(id="g_bad", findings_total="three")
    report, marked = _run_oracle(tmp_path, [row])
    assert "an unreadable number of" in report
    assert marked == []

    rec = _rec(findings_total="three", findings=[])
    assert delivery._has_evidence(rec) is True
    assert "an unreadable number of finding(s)" == delivery._count_phrase(rec)


def test_a_round_with_more_findings_than_the_cap_says_how_many_it_hid(tmp_path):
    """The cap keeps one loud round from burying every other round beneath
    itself. Hiding is only acceptable while it is VISIBLE and reversible: the
    count of what was withheld is on the line, and the triage pointer under it
    reaches all of them."""
    total = delivery.MAX_FINDINGS_SHOWN + 3
    with _store(tmp_path) as st:
        _save(st, findings=[_finding(title=f"finding {i}") for i in range(total)],
              findings_total=total,
              severity={"high": total, "medium": 0, "low": 0})
        text = _surface(st).text
    listed = [ln for ln in text.splitlines() if ln.strip().startswith("[")]
    assert len(listed) == delivery.MAX_FINDINGS_SHOWN
    assert "... and 3 more finding(s) not shown" in text
    assert "see: skodun triage --list sk_1" in text
    assert f"{total} finding(s)" in text
    assert "finding 0" in text and f"finding {total - 1}" not in text
