"""Tests for the security / skeptic extra passes.

Three layers:

* trigger + merge semantics, asserted offline against the behaviour the brief
  and the oracle agree on;
* an oracle parity layer (`test_*_oracle_*`, skipped without
  `$SKODUN_ORACLE_DIR`) that drives the real `scripts/grok-extra-passes.py` —
  its `any_path_risky` over a varied path corpus, its `merge` command over
  JSON artifacts, and its two prompt writers byte-for-byte;
* the deliberate divergences, each pinned by its own test so a "fix" that
  re-aligns with the oracle fails loudly instead of silently changing policy.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from skodun.config import (SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOT_NAMES,
                           SECURITY_PROMPT_SLOTS, load_config)
from skodun.passes import (_SECURITY_LEAD_TEMPLATE, failed_pass_reason,
                           merge_extra_pass, merge_failed_extra_pass,
                           security_lead, security_prompt, should_run_security,
                           should_run_skeptic, skeptic_prompt)
from tests.conftest import oracle_dir

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "scala-angular-monorepo.toml"

ORACLE_SCRIPT = Path("scripts") / "grok-extra-passes.py"


# ---------------------------------------------------------------------------
# Triggers — generic committed defaults
# ---------------------------------------------------------------------------

def test_security_trigger_uses_generic_defaults():
    # Committed default: concern words, no project's layout.
    assert should_run_security("now", ["app/auth/Login.scala"])
    assert should_run_security("now", ["svc/billing/Invoice.kt"])
    assert not should_run_security("now", ["web/src/button.ts"])
    assert not should_run_security("prepush", ["app/auth/x.scala"])  # now-mode only


def test_default_segments_are_the_configs_generic_set():
    """The default really is `config.SECURITY_PATH_SEGMENTS`, not a copy."""
    assert "credential" in SECURITY_PATH_SEGMENTS
    assert should_run_security("now", ["svc/credential/Store.go"])
    # ... and a repo's own layout vocabulary is NOT in the default: `dao` is a
    # perfectly good segment to configure, and skodun does not ship it.
    assert not should_run_security("now", ["app/dao/UserStore.scala"])


def test_security_trigger_tables_come_from_config():
    assert should_run_security("now", ["svc/vault/Key.scala"],
                               path_segments=("vault",))
    assert should_run_security("now", ["api/services/FooRouteService.scala"],
                               path_segments=(), basename_patterns=("*RouteService*",))
    # Empty tables are well-formed and simply never trigger — not an error.
    assert not should_run_security("now", ["app/auth/Login.scala"],
                                   path_segments=(), basename_patterns=())


def test_security_segments_match_whole_segments_case_folded():
    assert should_run_security("now", ["APP/AUTH/Login.scala"])
    assert should_run_security("now", ["./app/auth/Login.scala"])
    assert should_run_security("now", [r"app\auth\Login.scala"])
    # substrings of a longer segment do not match
    assert not should_run_security("now", ["app/authentication/Login.scala"])
    assert not should_run_security("now", ["app/reauth/Login.scala"])


def test_security_basename_pattern_forms():
    # No "/" in the pattern -> matched against the flat compacted path.
    assert should_run_security("now", ["telegram/Webhook.scala"],
                               path_segments=(),
                               basename_patterns=("*telegramwebhook*",))
    # "/" in the pattern -> matched against the segment-compacted path, so
    # directory context is anchored and `xapi/services/` must NOT match.
    anchored = ("*/api/services/*routeservice*",)
    assert should_run_security("now", ["backend/api/services/FooRouteService.scala"],
                               path_segments=(), basename_patterns=anchored)
    assert not should_run_security("now", ["xapi/services/FooRouteService.scala"],
                                   path_segments=(), basename_patterns=anchored)
    assert not should_run_security("now", ["foo/RouteService.scala"],
                                   path_segments=(), basename_patterns=anchored)


def test_security_ignores_blank_and_missing_paths():
    assert not should_run_security("now", [])
    assert not should_run_security("now", ["", "   "])


def test_security_kill_switch(monkeypatch):
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "0")
    assert not should_run_security("now", ["app/auth/Login.scala"])
    monkeypatch.setenv("SKODUN_SECURITY_PASS", "1")
    assert should_run_security("now", ["app/auth/Login.scala"])


def test_skeptic_only_on_clean_trustworthy_now():
    assert should_run_skeptic("now", True, 0)
    assert not should_run_skeptic("now", True, 1)
    assert not should_run_skeptic("now", False, 0)
    assert not should_run_skeptic("prepush", True, 0)


def test_skeptic_non_numeric_findings_total_never_fires():
    assert not should_run_skeptic("now", True, "nope")  # type: ignore[arg-type]
    assert not should_run_skeptic("now", True, None)  # type: ignore[arg-type]


def test_skeptic_kill_switch(monkeypatch):
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "0")
    assert not should_run_skeptic("now", True, 0)
    monkeypatch.setenv("SKODUN_SKEPTIC_PASS", "1")
    assert should_run_skeptic("now", True, 0)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _primary() -> dict:
    # fresh nested structures per test — merge_extra_pass is ported from an
    # in-place-mutating oracle, and dict() is only a shallow copy: a shared
    # findings list or severity dict would contaminate the next test.
    return dict(id="r", parse_ok=True, degraded=False, diff_truncated=False,
                trustworthy=True, findings_total=0, findings=[], summary="ok",
                severity={"high": 0, "medium": 0, "low": 0}, extra_passes={})


def test_failed_extra_pass_clears_parse_ok():
    out = merge_failed_extra_pass(_primary(), "security",
                                  failed_pass_reason("security"))
    assert out["parse_ok"] is False
    # The demoted record must not read as more trustworthy than it is: pinning
    # `trustworthy` here as well as `parse_ok` is what stops a `trustworthy:
    # True, parse_ok: False` record from ever being emitted.
    assert out["trustworthy"] is False
    assert "security" in out["failure_reason"]


def test_merge_extra_pass_refuses_to_demote_on_a_None_record():
    """The failure path has to be asked for by name (DIVERGENCE 1).

    A pass `should_run_*` declined is simply not merged; a pass that ran and
    produced nothing goes through `merge_failed_extra_pass`. Neither can be
    reached by handing `merge_extra_pass` a `None`, so no caller demotes a good
    review by accident.
    """
    with pytest.raises(TypeError, match="merge_failed_extra_pass"):
        merge_extra_pass(_primary(), None, "security")  # type: ignore[arg-type]


def test_merge_failed_extra_pass_requires_a_reason():
    with pytest.raises(ValueError, match="failure_reason"):
        merge_failed_extra_pass(_primary(), "security", "")
    with pytest.raises(ValueError, match="failure_reason"):
        merge_failed_extra_pass(_primary(), "security", "   ")


def test_merge_failed_extra_pass_records_the_callers_own_reason():
    out = merge_failed_extra_pass(_primary(), "skeptic", "grok exited 137")
    assert out["failure_reason"] == "grok exited 137"
    assert out["parse_ok"] is False and out["trustworthy"] is False


def test_unparseable_extra_pass_clears_parse_ok_and_keeps_its_reason():
    extra = dict(parse_ok=False, degraded=False, findings=[],
                 failure_reason="no JSON object in output")
    out = merge_extra_pass(_primary(), extra, "skeptic")
    assert out["parse_ok"] is False
    assert out["trustworthy"] is False       # not just the axis — the verdict
    assert out["failure_reason"] == "no JSON object in output"
    assert out["degraded"] is False          # the other axis is untouched


def test_extra_pass_that_omits_parse_ok_is_a_failure_not_a_clear():
    """`parse_ok` must be exactly True. Silence is not success."""
    out = merge_extra_pass(_primary(), dict(findings=[]), "security")
    assert out["parse_ok"] is False
    assert out["trustworthy"] is False
    assert "security" in out["failure_reason"]
    assert out["extra_passes"]["security"]["parse_ok"] is False


def test_degraded_extra_pass_sets_degraded_not_parse_ok():
    extra = dict(parse_ok=True, degraded=True, degraded_reason="stopReason=Cancelled",
                 summary="s", findings=[])
    out = merge_extra_pass(_primary(), extra, "security")
    assert out["parse_ok"] is True          # axes stay independent (oracle semantics)
    assert out["degraded"] is True
    # The pass's OWN reason is carried through verbatim. The task brief's sketch
    # expected the pass name to appear here too; the oracle does not add it when
    # the pass supplied a reason, and per Global Constraints the oracle wins —
    # `test_merge_parity_with_oracle` case 3 pins this against the live oracle.
    # Which pass degraded is recorded structurally, not by string-matching:
    assert out["degraded_reason"] == "stopReason=Cancelled"
    assert out["extra_passes"]["security"]["degraded"] is True
    assert out["trustworthy"] is False


def test_degraded_pass_without_a_reason_falls_back_to_naming_the_pass():
    out = merge_extra_pass(
        _primary(), dict(parse_ok=True, degraded=True, findings=[]), "security")
    assert out["degraded"] is True
    assert "security" in out["degraded_reason"]
    assert out["parse_ok"] is True


def test_demotion_reasons_append_rather_than_replace():
    primary = _primary()
    primary["failure_reason"] = "earlier problem"
    primary["degraded_reason"] = "earlier wobble"
    out = merge_extra_pass(
        primary,
        dict(parse_ok=False, degraded=True, findings=[],
             failure_reason="pass blew up", degraded_reason="pass wobbled"),
        "security")
    assert out["failure_reason"] == "earlier problem; pass blew up"
    assert out["degraded_reason"] == "earlier wobble; pass wobbled"


def test_size_capped_pass_records_partial_coverage_without_demotion():
    extra = dict(parse_ok=True, degraded=False, diff_truncated=True,
                 summary="s", findings=[])
    out = merge_extra_pass(_primary(), extra, "security")
    assert out["extra_passes"]["security"]["partial_coverage"] is True
    assert out["parse_ok"] is True and out["degraded"] is False
    assert out["trustworthy"] is True
    assert "partial coverage" in out["summary"]


def test_merge_prefixes_titles_and_recounts():
    extra = dict(parse_ok=True, degraded=False, summary="found",
                 findings=[dict(file="a", line=1, severity="high",
                                category="", title="SQLi", detail="d")])
    out = merge_extra_pass(_primary(), extra, "security")
    f = out["findings"][0]
    assert f["title"] == "(security) SQLi" and f["category"] == "security"
    assert out["findings_total"] == 1 and out["severity"]["high"] == 1
    assert out["trustworthy"] is True


def test_severity_recount_covers_primary_and_extra_findings():
    primary = _primary()
    primary["findings"] = [
        dict(file="p", line=1, severity="high", category="bug", title="A"),
        dict(file="p", line=2, severity="low", category="bug", title="B"),
    ]
    primary["findings_total"] = 2
    primary["severity"] = {"high": 1, "medium": 0, "low": 1}
    extra = dict(parse_ok=True, degraded=False, summary="s", findings=[
        dict(file="q", line=3, severity="medium", category="other", title="C"),
        dict(file="q", line=4, severity="HIGH", category="security", title="D"),
        dict(file="q", line=5, severity="bogus", category="security", title="E"),
    ])
    out = merge_extra_pass(primary, extra, "security")
    assert out["findings_total"] == 5
    assert out["severity"] == {"high": 2, "medium": 1, "low": 1}


def test_other_category_rewritten_for_security_but_not_skeptic():
    def one(cat):
        return dict(parse_ok=True, degraded=False, summary="s",
                    findings=[dict(file="a", line=1, severity="low",
                                   category=cat, title="t", detail="d")])
    assert merge_extra_pass(_primary(), one("other"), "security")[
        "findings"][0]["category"] == "security"
    assert merge_extra_pass(_primary(), one(""), "security")[
        "findings"][0]["category"] == "security"
    # a real category survives untouched
    assert merge_extra_pass(_primary(), one("perf"), "security")[
        "findings"][0]["category"] == "perf"
    # skeptic normalizes an empty category to `other`, never to `security`
    assert merge_extra_pass(_primary(), one(""), "skeptic")[
        "findings"][0]["category"] == "other"
    assert merge_extra_pass(_primary(), one("other"), "skeptic")[
        "findings"][0]["category"] == "other"


@pytest.mark.parametrize("pass_name", ["security", "skeptic"])
def test_rule_id_title_not_polluted(pass_name):
    extra = dict(parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=1, severity="low",
                                category="bug", title="[no-blocking-handler] x",
                                detail="d")])
    out = merge_extra_pass(_primary(), extra, pass_name)
    f = out["findings"][0]
    assert f["title"].startswith("[no-blocking-handler]")
    assert f["detail"].startswith(f"(extra-pass: {pass_name}) ")
    # ... and the rule id is still extractable from the title.
    assert out["rule_ids"] == ["no-blocking-handler"]


@pytest.mark.parametrize("pass_name", ["security", "skeptic"])
def test_plain_title_takes_the_prefix_branch(pass_name):
    extra = dict(parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=1, severity="low",
                                category="bug", title="plain", detail="d")])
    f = merge_extra_pass(_primary(), extra, pass_name)["findings"][0]
    assert f["title"] == f"({pass_name}) plain"
    assert f["detail"] == "d"           # detail untouched in this branch


def test_merge_is_idempotent_on_an_already_tagged_title():
    extra = dict(parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=1, severity="low",
                                category="bug", title="(security) already",
                                detail="d")])
    f = merge_extra_pass(_primary(), extra, "security")["findings"][0]
    assert f["title"] == "(security) already"


def test_merge_records_observability_meta():
    extra = dict(id="x1", parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=1, severity="low", title="t")])
    meta = merge_extra_pass(_primary(), extra, "security")["extra_passes"]["security"]
    assert meta["ran"] is True and meta["pass"] == "security"
    assert meta["findings_total"] == 1 and meta["id"] == "x1"
    failed = merge_failed_extra_pass(_primary(), "skeptic",
                                     failed_pass_reason("skeptic"))
    meta_failed = failed["extra_passes"]["skeptic"]
    # `failed`, never `skipped`: the observability record has to agree with the
    # demotion it sits next to (`parse_ok: False` and a reason blaming the pass).
    assert meta_failed["ran"] is False and meta_failed["failed"] is True
    assert "skipped" not in meta_failed


def test_merge_does_not_mutate_primary():
    primary = _primary()
    before = copy.deepcopy(primary)
    extra = dict(parse_ok=True, degraded=True, degraded_reason="r", summary="s",
                 findings=[dict(file="a", line=1, severity="high", title="t")])
    out = merge_extra_pass(primary, extra, "security")
    assert primary == before, "merge_extra_pass must not mutate its primary argument"
    # ... and the nested containers of the result are not the caller's objects.
    assert out["findings"] is not primary["findings"]
    assert out["severity"] is not primary["severity"]
    assert out["extra_passes"] is not primary["extra_passes"]


def test_two_merges_from_one_primary_do_not_contaminate_each_other():
    primary = _primary()
    a = merge_extra_pass(primary, dict(parse_ok=True, degraded=False, summary="s",
                                       findings=[dict(file="a", line=1,
                                                      severity="high", title="A")]),
                         "security")
    b = merge_extra_pass(primary, dict(parse_ok=True, degraded=False, summary="s",
                                       findings=[dict(file="b", line=2,
                                                      severity="low", title="B")]),
                         "skeptic")
    assert [f["title"] for f in a["findings"]] == ["(security) A"]
    assert [f["title"] for f in b["findings"]] == ["(skeptic) B"]
    assert a["severity"] == {"high": 1, "medium": 0, "low": 0}
    assert b["severity"] == {"high": 0, "medium": 0, "low": 1}


def test_carried_over_finding_dicts_are_shared_but_never_written():
    """DIVERGENCE 2, stated exactly: fresh containers, shared elements.

    The result's `findings` list is new, but the primary's own finding dicts go
    into it by reference — merging copies a finding only when it tags one. The
    same goes for an earlier pass's meta dict across a chained merge. Nothing
    here writes to either, and this pins that promise at its real width so the
    docstring cannot quietly overstate it.
    """
    primary = _primary()
    primary["findings"] = [dict(file="p", line=1, severity="high", title="A")]
    extra = dict(parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=2, severity="low", title="B")])
    first = merge_extra_pass(primary, extra, "security")

    assert first["findings"] is not primary["findings"]     # fresh container
    assert first["findings"][0] is primary["findings"][0]   # shared element
    assert primary["findings"][0] == dict(file="p", line=1, severity="high",
                                          title="A")        # ... never written
    # The extra pass's own findings are copied before tagging, so the caller's
    # `extra` record is untouched too.
    assert first["findings"][1] is not extra["findings"][0]
    assert extra["findings"][0]["title"] == "B"

    second = merge_extra_pass(first, dict(parse_ok=True, degraded=False,
                                          summary="s", findings=[]), "skeptic")
    assert second["extra_passes"] is not first["extra_passes"]
    assert second["extra_passes"]["security"] is first["extra_passes"]["security"]
    assert first["extra_passes"]["security"]["pass"] == "security"


def test_merge_rejects_a_non_dict_primary():
    with pytest.raises(TypeError, match="primary"):
        merge_extra_pass(["not", "a", "dict"],  # type: ignore[arg-type]
                         dict(parse_ok=True, findings=[]), "security")


@pytest.mark.parametrize("bad", [["findings"], "parse_ok", 7, True])
def test_merge_rejects_a_non_mapping_extra(bad):
    """`extra` is the untrusted side — it must fail as clearly as `primary`."""
    with pytest.raises(TypeError, match="extra must be a mapping"):
        merge_extra_pass(_primary(), bad, "security")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Prompts — offline shape, before the byte-exact oracle parity below
# ---------------------------------------------------------------------------

_ARGS = dict(branch="feat/x", base_ref="origin/main", base_sha="deadbee",
             head="cafe123 (working tree)")


def test_skeptic_prompt_carries_the_adversarial_framing():
    text = skeptic_prompt(diff=b"--- a\n+++ b\n", **_ARGS).text.decode("utf-8")
    assert text.startswith(
        "A previous reviewer cleared this pull-request diff (0 findings).\n"
        "Your job is the ADVERSARIAL CLEAN-CHECK: prove them wrong if you can.\n")
    assert "Pass:   skeptic / adversarial clean-check (#3284)" in text
    assert "Branch: feat/x" in text
    assert "----- BEGIN DIFF -----\n--- a\n+++ b\n----- END DIFF -----\n" in text


def test_security_prompt_is_security_only():
    text = security_prompt(diff=b"--- a\n", **_ARGS).text.decode("utf-8")
    assert text.startswith(
        "You are a SECURITY-FOCUSED code reviewer on a pull request that touches\n")
    assert "Pass:   security (#3285)" in text
    assert '"category":"security"' in text
    # a dedicated pass, not a checklist review
    assert "REPO RULES" not in text


# --- the security prompt's variable spans ----------------------------------

def test_prompt_template_slot_names_match_the_config_key():
    """The template and `Defaults.security_prompt_slots` must agree.

    `config.load_config` validates a user's slot names against
    `SECURITY_PROMPT_SLOT_NAMES`, so a template slot missing from that set would
    be unfillable and a config-only name would be silently dropped.
    """
    used = set(re.findall(r"%\((\w+)\)s", "\n".join(_SECURITY_LEAD_TEMPLATE)))
    assert used == set(SECURITY_PROMPT_SLOT_NAMES)
    assert used == {name for name, _ in SECURITY_PROMPT_SLOTS}
    # No bare `%` anywhere else, or `%`-substitution would explode on a prompt.
    assert security_lead()          # renders at all, with the defaults


def test_default_security_prompt_names_no_particular_project():
    """The shipped prompt is data sent to a model in every repo skodun runs in.

    It must read naturally with no project in mind — the concrete vocabulary of
    any one stack belongs in that repo's config (examples/ carries a worked one).
    """
    text = security_prompt(diff=b"", **_ARGS).text.decode("utf-8").lower()
    lead = text.split("branch:")[0]
    for noun in ("telegram", "credits", "dao", "routeservice", "route service"):
        assert noun not in lead, noun
    # ... and it still names the generic surfaces and checks.
    for phrase in ("authentication, authorization", "public http endpoints",
                   "webhook validation", "payment and quota integrity"):
        assert phrase in lead, phrase


def test_security_prompt_slots_come_from_config():
    text = security_prompt(
        diff=b"", prompt_slots=(("surfaces", "widgets and sprockets"),
                                ("extra_checks", "- sprocket integrity")),
        **_ARGS).text.decode("utf-8")
    assert "risky surfaces (widgets and sprockets). This is a dedicated" in text
    assert "\n- sprocket integrity\n" in text
    assert "authentication, authorization" not in text


def test_unfilled_and_unknown_prompt_slots_are_fail_soft():
    """Consuming a table is fail-soft; a malformed one died at load time."""
    only_one = security_lead((("surfaces", "widgets"),))
    assert "risky surfaces (widgets)." in only_one[1]
    # the slot that was not supplied keeps its generic default
    assert any("payment and quota integrity" in line for line in only_one)
    assert security_lead((("nonesuch", "ignored"),)) == security_lead(())
    assert security_lead(()) == security_lead()


def test_example_config_security_prompt_uses_its_own_vocabulary(tmp_path):
    """Runs without the oracle; the byte-exact check is the parity test below."""
    slots = _example_defaults(tmp_path).security_prompt_slots
    text = security_prompt(diff=b"", prompt_slots=slots, **_ARGS).text.decode("utf-8")
    assert ("risky surfaces (auth, public HTTP routes, db/dao, Telegram webhooks,\n"
            "billing, or credits). This is a dedicated security pass") in text
    assert "- billing/credits integrity (privilege escalation, free usage)\n" in text


@pytest.mark.parametrize("fn", [security_prompt, skeptic_prompt])
def test_prompt_truncation_marker_and_flag(fn):
    p = fn(diff=b"x" * 100, max_diff_bytes=10, **_ARGS)
    text = p.text.decode("utf-8")
    assert p.diff_truncated is True
    assert "----- DIFF TRUNCATED at 10 bytes -----\n----- END DIFF -----\n" in text
    assert "x" * 10 + "\n----- DIFF TRUNCATED" in text
    assert "x" * 11 not in text
    assert fn(diff=b"x" * 10, max_diff_bytes=10, **_ARGS).diff_truncated is False


@pytest.mark.parametrize("fn", [security_prompt, skeptic_prompt])
def test_prompt_rejects_a_non_positive_budget(fn):
    with pytest.raises(ValueError):
        fn(diff=b"x", max_diff_bytes=0, **_ARGS)


@pytest.mark.parametrize("fn", [security_prompt, skeptic_prompt])
def test_prompt_replaces_undecodable_diff_bytes(fn):
    text = fn(diff=b"\xff\xfe ok\n", **_ARGS).text.decode("utf-8")
    assert "�� ok" in text


# ---------------------------------------------------------------------------
# Example config (runs without the oracle)
# ---------------------------------------------------------------------------

def _example_defaults(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".skodun.toml").write_text(EXAMPLE.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return load_config(repo, global_path=tmp_path / "absent.toml").defaults


def test_example_config_declares_both_security_tables(tmp_path):
    d = _example_defaults(tmp_path)
    assert "dao" in d.security_path_segments      # repo-specific, not a default
    assert d.security_path_segments != SECURITY_PATH_SEGMENTS
    assert any("routeservice" in p for p in d.security_basename_patterns)


def test_example_config_fills_every_security_prompt_slot(tmp_path):
    slots = _example_defaults(tmp_path).security_prompt_slots
    assert {name for name, _ in slots} == set(SECURITY_PROMPT_SLOT_NAMES)
    assert slots != SECURITY_PROMPT_SLOTS         # repo-specific, not a default


# ---------------------------------------------------------------------------
# Oracle parity
# ---------------------------------------------------------------------------

#: Paths chosen so every branch of the oracle's `path_is_risky` is exercised:
#: segment hits, the compacted-name substrings (including one that only matches
#: because compaction crosses a `/`), the `api/services/*RouteService*` rule,
#: near misses for each of those, and plain non-matches.
ORACLE_PATH_CORPUS: tuple[str, ...] = (
    # --- segment matches
    "app/auth/Login.scala",
    "src/main/scala/billing/Invoice.scala",
    "backend/credits/Ledger.scala",
    "src/main/scala/dao/UserDao.scala",
    "src/main/resources/db/changelog/001-init.xml",
    "webhook/Handler.scala",
    "APP/AUTH/Login.scala",
    "./app/auth/Login.scala",
    r"app\auth\Login.scala",
    # --- segment near misses
    "app/authentication/Login.scala",
    "app/reauth/Login.scala",
    "app/dao_helpers/Foo.scala",
    "ui/src/app/dbutil/Foo.ts",
    # --- compacted-name matches
    "api/TelegramWebhookController.scala",
    "telegram/Webhook.scala",
    "src/main/scala/routes/WebhookRouteService.scala",
    "ui/src/app/telegram-webhook.service.ts",
    # --- compacted-name near misses
    "api/TelegramController.scala",
    "ui/src/app/telegram.service.ts",
    # --- api/services route-service rule
    "api/services/FooRouteService.scala",
    "backend/api/services/BarRouteService.scala",
    "web/src/app/api/services/RouteServiceX.ts",
    "api/services/route-service.ts",
    # --- route-service rule near misses
    "xapi/services/FooRouteService.scala",
    "foo/RouteService.scala",
    "api/services/plain/Helper.scala",
    "api/services/",
    # --- plain non-matches
    "ui/src/app/button.component.ts",
    "README.md",
    "scripts/build.sh",
    "",
    "   ",
)


#: The ONLY paths on which skodun and the oracle disagree, as
#: `(path, oracle_is_risky, skodun_is_risky)` — found by a 20k-path differential
#: over generated path shapes, and recorded here so they can never resurface as
#: a surprise. All four come from the one deliberate rule change: a
#: `security_basename_patterns` entry containing "/" is matched against the
#: SEGMENT-compacted path, so a directory boundary means what it says, where the
#: oracle compacted the whole path flat and tested `"/api/services/" in path`.
#:
#:  * rows 1-2 are FAIL-OPEN (oracle runs a security pass, skodun does not): a
#:    directory literally named like a route service sitting above the real
#:    `api/services/`, which satisfies the oracle's two independent substring
#:    tests by accident. Nothing under `api/services/` is named like a route
#:    service, so there is no route-service surface in the change;
#:  * rows 3-4 are FAIL-SAFE (skodun runs an extra pass the oracle skips):
#:    punctuation inside a directory name that compaction removes, so skodun
#:    sees the directory the oracle's literal substring test missed.
#:
#: This is a clearly-labeled parity corpus, so it carries the concrete literals.
ORACLE_KNOWN_DIVERGENCES: tuple[tuple[str, bool, bool], ...] = (
    ("RouteService/api/services/x.ts", True, False),
    ("routeservice/api/services/x.ts", True, False),
    ("a.p.i/services/FooRouteService.scala", False, True),
    ("api/serv.ices/FooRouteService.scala", False, True),
)


@pytest.mark.parametrize("path,_oracle,skodun", ORACLE_KNOWN_DIVERGENCES)
def test_known_divergences_skodun_side(tmp_path, path, _oracle, skodun):
    """Pinned without the oracle, so CI holds skodun's half of each row."""
    d = _example_defaults(tmp_path)
    assert should_run_security(
        "now", [path], path_segments=d.security_path_segments,
        basename_patterns=d.security_basename_patterns) is skodun


def _oracle_module(root: Path):
    path = root / ORACLE_SCRIPT
    assert path.is_file(), f"oracle script not found: {path}"
    spec = importlib.util.spec_from_file_location("skodun_oracle_extra_passes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_example_config_reproduces_oracle_security_triggers(tmp_path):
    oracle = _oracle_module(oracle_dir())
    d = _example_defaults(tmp_path)
    tables = dict(path_segments=d.security_path_segments,
                  basename_patterns=d.security_basename_patterns)

    # The corpus must actually exercise both answers, or the loop below would
    # pass vacuously against a constant function.
    expected = [oracle.any_path_risky([p]) for p in ORACLE_PATH_CORPUS]
    assert expected.count(True) >= 12 and expected.count(False) >= 12

    for path, want in zip(ORACLE_PATH_CORPUS, expected):
        assert should_run_security("now", [path], **tables) is want, path

    # ... and `any` semantics over the whole corpus agree too.
    assert should_run_security("now", list(ORACLE_PATH_CORPUS), **tables) is \
        oracle.any_path_risky(ORACLE_PATH_CORPUS)
    clean = [p for p, w in zip(ORACLE_PATH_CORPUS, expected) if not w]
    assert should_run_security("now", clean, **tables) is \
        oracle.any_path_risky(clean)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_known_divergences_are_still_exactly_these(tmp_path):
    """Both halves of every recorded row, against the live oracle.

    A fix that closes one of these must delete its row, not leave it lying;
    a *new* divergence has to be found by re-running the differential, but the
    recorded four staying true is the cheap continuous half of that.
    """
    oracle = _oracle_module(oracle_dir())
    d = _example_defaults(tmp_path)
    for path, want_oracle, want_skodun in ORACLE_KNOWN_DIVERGENCES:
        assert oracle.path_is_risky(path) is want_oracle, path
        assert should_run_security(
            "now", [path], path_segments=d.security_path_segments,
            basename_patterns=d.security_basename_patterns) is want_skodun, path
        assert want_oracle is not want_skodun, path


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
def test_generic_defaults_are_not_the_oracle_table():
    """Parity is asserted with the example config — never with the defaults."""
    oracle = _oracle_module(oracle_dir())
    disagreements = [p for p in ORACLE_PATH_CORPUS
                     if should_run_security("now", [p]) is not oracle.any_path_risky([p])]
    assert disagreements, ("the committed default table reproduced the oracle "
                           "exactly; it is supposed to be deliberately generic")


def _run_oracle_prompt(root: Path, tmp_path: Path, command: str, diff: bytes,
                       max_bytes: int | None) -> bytes:
    diff_file = tmp_path / f"{command}.diff"
    diff_file.write_bytes(diff)
    out = tmp_path / f"{command}.prompt"
    env = {"PATH": "/usr/bin:/bin"}
    if max_bytes is not None:
        env["GROK_MAX_DIFF_BYTES"] = str(max_bytes)
    proc = subprocess.run(
        [sys.executable, str(root / ORACLE_SCRIPT), command, str(out),
         _ARGS["branch"], _ARGS["base_ref"], _ARGS["base_sha"], _ARGS["head"],
         str(diff_file)],
        capture_output=True, env=env, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return out.read_bytes()


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
@pytest.mark.parametrize("command,fn", [("write-security-prompt", security_prompt),
                                        ("write-skeptic-prompt", skeptic_prompt)])
@pytest.mark.parametrize("diff,max_bytes", [
    (b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", None),
    (b"", None),
    (b"no trailing newline", None),
    (b"trailing newlines\n\n\n", None),
    (b"\xff\xfe binary-ish hunk\n", None),
    (b"x" * 5000, 100),
    (b"x" * 100, 100),
])
def test_prompt_parity_with_oracle(tmp_path, command, fn, diff, max_bytes):
    want = _run_oracle_prompt(oracle_dir(), tmp_path, command, diff, max_bytes)
    # The security lead's two variable spans are config, and the example config
    # carries the oracle's own fragments: byte-exact parity is reached THROUGH
    # `load_config`, never from a committed default. (The skeptic prompt has no
    # slots — it names nothing project-specific.)
    slots = {} if fn is skeptic_prompt else {
        "prompt_slots": _example_defaults(tmp_path).security_prompt_slots}
    got = fn(diff=diff, max_diff_bytes=max_bytes or 400_000, **slots, **_ARGS)
    assert got.text == want
    assert got.prompt_bytes == len(want)
    assert got.diff_truncated is (len(diff) > (max_bytes or 400_000))


#: (primary, extra, pass_name) triples driven through the oracle's `merge`
#: command. The failed-pass path is deliberately absent — skodun demotes there
#: and the oracle does not; see DIVERGENCE 1 in `passes` and
#: `test_failed_extra_pass_clears_parse_ok`.
ORACLE_MERGE_CASES: tuple[tuple[dict, dict, str], ...] = (
    (dict(parse_ok=True, degraded=False, trustworthy=True, findings=[],
          summary="ok", severity={"high": 0, "medium": 0, "low": 0}),
     dict(id="e1", parse_ok=True, degraded=False, summary="s",
          findings=[dict(file="a", line=1, severity="high", category="",
                         title="SQLi", detail="d")]),
     "security"),
    (dict(parse_ok=True, degraded=False, trustworthy=True, summary="ok",
          findings=[dict(file="p", line=9, severity="low", category="bug",
                         title="[no-blocking-handler] p", detail="pd")]),
     dict(id="e2", parse_ok=True, degraded=False, summary="s",
          findings=[dict(file="a", line=1, severity="low", category="other",
                         title="[stale-rule] x", detail="d"),
                    dict(file="b", line=2, severity="medium", category="perf",
                         title="slow", detail="d2")]),
     "skeptic"),
    (dict(parse_ok=True, degraded=False, trustworthy=True, findings=[],
          summary="ok", failure_reason="earlier"),
     dict(id="e3", parse_ok=False, degraded=False, findings=[],
          failure_reason="no JSON object in output"),
     "security"),
    (dict(parse_ok=True, degraded=False, trustworthy=True, findings=[],
          summary="ok", degraded_reason="earlier"),
     dict(id="e4", parse_ok=True, degraded=True, findings=[],
          degraded_reason="stopReason=Cancelled"),
     "skeptic"),
    (dict(parse_ok=True, degraded=False, trustworthy=True, findings=[],
          summary="ok"),
     dict(id="e5", parse_ok=True, degraded=False, diff_truncated=True,
          findings=[], summary="s"),
     "security"),
    (dict(parse_ok=True, degraded=False, trustworthy=True, findings=[],
          summary="ok", extra_passes={"security": {"ran": True, "pass": "security"}}),
     dict(id="e6", parse_ok=True, degraded=False, summary="s",
          findings=[dict(file="a", line=1, severity="bogus", category="  other  ",
                         title="(skeptic) already tagged", detail="d")]),
     "skeptic"),
)


@pytest.mark.skipif(oracle_dir() is None, reason="SKODUN_ORACLE_DIR unset")
@pytest.mark.parametrize("case", range(len(ORACLE_MERGE_CASES)))
def test_merge_parity_with_oracle(tmp_path, case):
    primary, extra, pass_name = ORACLE_MERGE_CASES[case]
    p_file = tmp_path / f"primary{case}.json"
    e_file = tmp_path / f"extra{case}.json"
    o_file = tmp_path / f"out{case}.json"
    p_file.write_text(json.dumps(primary), encoding="utf-8")
    e_file.write_text(json.dumps(extra), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(oracle_dir() / ORACLE_SCRIPT), "merge",
         str(p_file), str(e_file), pass_name, str(o_file)],
        capture_output=True, env={"PATH": "/usr/bin:/bin"}, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    want = json.loads(o_file.read_text(encoding="utf-8"))
    got = merge_extra_pass(copy.deepcopy(primary), copy.deepcopy(extra), pass_name)
    assert got == want
