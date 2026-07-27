"""Two extra review passes, and how their results fold back into the primary.

* **security** — a dedicated, security-only pass, scheduled only in `now` mode
  and only when the change touches a risky path.
* **skeptic** — an adversarial clean-check, scheduled only in `now` mode and
  only when the primary review came back trustworthy with zero findings. One
  extra call on the rounds that are about to clear the gate is cheap; a false
  clear is not.

Both are opt-out via env kill switches (`SKODUN_SECURITY_PASS=0`,
`SKODUN_SKEPTIC_PASS=0`), so a wedged pass can be turned off without a config
edit or a code change.

PARITY-CRITICAL: vendored from the oracle's `scripts/grok-extra-passes.py`
(`path_is_risky`, `should_run_security`, `should_run_skeptic`,
`merge_extra_pass`, `write-security-prompt`, `write-skeptic-prompt`). The two
prompt bodies were transferred from the oracle's own source — the line lists
were extracted from its AST, never retyped — and `tests/test_passes.py` pins
them byte-for-byte against the live oracle, along with the trigger decision and
the merge result. Deliberate divergences are marked `DIVERGENCE` below and each
one has its own test.

Risky surfaces are CONFIGURATION, not code
------------------------------------------
The oracle hardcoded one monorepo's risky surfaces twice over: once in the
tables that decide *when* the pass runs (its own package names, its own webhook
services, its own HTTP route layer), and once in the prompt text that tells the
model *what* risky means in this repo. Neither belongs in code that ships to
every repo, so both arrive from config:

* **when** — `Defaults.security_path_segments` (default: the generic,
  stack-agnostic `config.SECURITY_PATH_SEGMENTS`, words that name a *concern*
  rather than a directory convention) and `Defaults.security_basename_patterns`
  (default: empty);
* **what** — `Defaults.security_prompt_slots`, `(slot-name, fragment)` pairs
  filling the two variable spans of the security prompt (default: the generic
  `config.SECURITY_PROMPT_SLOTS`; an unfilled slot keeps its generic text).

A worked example carrying one concrete stack's tables *and* prompt fragments
lives in `examples/scala-angular-monorepo.toml`. The parity tests load that
file, so the oracle's trigger decisions and its exact prompt bytes stay
asserted end-to-end while committed code and the shipped prompt stay generic.

Matching semantics (this module owns them)
------------------------------------------
A path is first normalized the way the oracle normalizes it: surrounding
whitespace stripped, `\\` folded to `/`, leading `./` removed repeatedly, then
lowercased. An empty result is never risky. Two independent tests then run, and
either one triggers the pass:

`security_path_segments` — the path is split on `/` and a path is risky when
any **whole** segment equals a table entry (entries are stripped and case-folded
too). Whole-segment, not substring: `auth/` matches, `reauthorize/` does not.
This is the oracle's `_RISKY_SEGMENTS` rule exactly.

`security_basename_patterns` — each entry is an `fnmatch.fnmatchcase` glob,
lowercased before use, matched against one of two *compacted* renderings of the
path. Compaction (dropping every character outside `[a-z0-9]`) is what lets one
pattern match `PaymentWebhookHandler.scala`, `payment-webhook.handler.ts`, and
`payment_webhook.py` alike, which is the whole point of the oracle's
name-substring table. Which rendering is used depends on whether the pattern
mentions a directory:

* **no `/` in the pattern** → the *flat* compaction: the whole path with every
  non-alphanumeric character removed, separators included. A pure name-shape
  test, deliberately blind to path boundaries — exactly the oracle's
  `re.sub(r"[^a-z0-9]", "", path)` substring check, so `*paymentwebhook*`
  matches `payment/Webhook.scala` here just as it does there.
* **`/` in the pattern** → the *segment* compaction: each `/`-separated segment
  compacted on its own, rejoined with `/`, and prefixed with `/` so a leading
  directory can be anchored. `*` still crosses `/` (fnmatch semantics), so
  `*/svc/http/*gateway*` reads as "somewhere under a real `svc/http/`
  directory, something whose name compacts to contain `gateway`" — the oracle's
  directory-scoped rule, but with the directory boundary actually respected, so
  `xsvc/http/FooGateway.scala` is correctly excluded where the oracle's flat
  compaction would have let it through had it used one. A 20k-path differential
  found exactly four paths where that stricter reading disagrees with the
  oracle; all four are recorded and pinned by
  `tests/test_passes.py::ORACLE_KNOWN_DIVERGENCES`.

Both tables are consumed FAIL-SOFT: empty, or matching nothing, simply means
"no security pass" and never an error. (A *malformed* table is rejected loudly
at config-load time by `config.load_config`; the two rules govern different
moments.)

Merging, and the two trust axes
-------------------------------
`merge_extra_pass` folds one pass's findings into the primary review record.
Findings are tagged with the lens that produced them, and the tag never lands
in a place that would corrupt something else: a title already opening with a
`[rule-id]` house-rule citation keeps that citation at position 0 and the tag
goes into `detail` instead, so `rule_ids` extraction stays clean.

The demotion rules keep `parse_ok` and `degraded` **independent**, because they
mean different things and the gate reads them separately:

* a *failed* pass (`merge_failed_extra_pass`, or a record whose `parse_ok` is
  not exactly `True`) clears the primary's `parse_ok` and appends to
  `failure_reason`;
* a *degraded* pass sets `degraded` and appends to `degraded_reason`, and does
  not touch `parse_ok`;
* a *size-capped* pass (`diff_truncated`) records `partial_coverage` under
  `extra_passes` and notes it in the summary, but demotes nothing. One extra
  call is the bound on these passes by design; demoting every large risky change
  would make the pass unusable.

Either demotion drives `trustworthy` false — set here, and recomputed from the
axes by `store.save_review` regardless.

DIVERGENCES from the oracle
---------------------------
1. **A pass that produced nothing demotes, and says so by name.** The oracle
   takes `extra=None` to mean "this pass was never scheduled" and records
   `ran: False, skipped: True` without touching the primary. Here the caller
   only merges a pass it decided to run, so "nothing came back" is a failure —
   and a security pass that failed must not leave a false clear behind (Global
   Constraints: nothing may pass the gate unless `trustworthy` is true). Rather
   than hang that on an easily-mistyped `None` argument, the failure path is its
   own function, `merge_failed_extra_pass`, which demands an explicit reason;
   `merge_extra_pass(primary, None, ...)` raises. Pinned by
   `test_failed_extra_pass_clears_parse_ok`; deliberately excluded from the
   merge parity corpus.
2. **No mutation.** The oracle mutates `primary` in place and returns it. This
   returns a new record: the `findings` list, `severity` dict, `rule_ids` list
   and `extra_passes` dict are all freshly built, so merging two passes off one
   primary — a normal thing to want — cannot cross-contaminate them, as a
   shallow `dict()` copy would have. Individual carried-over finding *dicts*,
   and in a chained merge an earlier pass's `extra_passes[<name>]` meta dict,
   are shared with the argument by reference; this module never writes into
   either (it copies a finding before tagging it), and
   `test_carried_over_finding_dicts_are_shared_but_never_written` pins that.
3. **The prompt builders return `promptbuild.Prompt`, not raw bytes.** The
   oracle's CLI writes the truncation flag to a `<prompt>.flags` sidecar because
   its orchestrator needs it to set `partial_coverage`; in-process there is no
   reason to launder that fact through the filesystem, and `Prompt` already
   carries it next to the bytes.
4. **A non-positive `max_diff_bytes` raises** rather than being clamped back to
   the oracle's default — matching `promptbuild.build`, and for the same reason:
   a zero budget silently ships a prompt with no diff in it, which reads to the
   model as "nothing changed".
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .config import SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOTS, Defaults
from .promptbuild import Prompt

#: Env kill switches. Set either to `0` to never schedule that pass.
SECURITY_PASS_ENV = "SKODUN_SECURITY_PASS"
SKEPTIC_PASS_ENV = "SKODUN_SKEPTIC_PASS"

#: The diff budget, sourced from config so there is exactly one of this number
#: (it is also the oracle's own `GROK_MAX_DIFF_BYTES` default).
DEFAULT_MAX_DIFF_BYTES: int = Defaults.max_diff_bytes

_NON_ALNUM = re.compile(r"[^a-z0-9]")
#: `[kebab-rule-id]` citations, as `trust`/triage read them out of a title.
_RULE_ID = re.compile(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]")


# ---------------------------------------------------------------------------
# Risky paths
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """The oracle's path normalization: trim, `\\`→`/`, drop leading `./`."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def path_is_risky(
    path: str,
    path_segments: Sequence[str] = SECURITY_PATH_SEGMENTS,
    basename_patterns: Sequence[str] = (),
) -> bool:
    """True when `path` is a security-pass surface (see module docstring)."""
    p = normalize_path(path)
    if not p:
        return False
    lowered = p.lower()

    wanted = {s.strip().lower() for s in path_segments if s and s.strip()}
    if wanted:
        if any(seg in wanted for seg in lowered.split("/") if seg):
            return True

    if basename_patterns:
        flat = _NON_ALNUM.sub("", lowered)
        segmented = "/" + "/".join(
            _NON_ALNUM.sub("", seg) for seg in lowered.split("/"))
        for pattern in basename_patterns:
            if not pattern:
                continue
            glob = pattern.lower()
            target = segmented if "/" in glob else flat
            if fnmatch.fnmatchcase(target, glob):
                return True
    return False


def any_path_risky(
    paths: Iterable[str],
    path_segments: Sequence[str] = SECURITY_PATH_SEGMENTS,
    basename_patterns: Sequence[str] = (),
) -> bool:
    """True when any of `paths` is risky."""
    return any(path_is_risky(p, path_segments, basename_patterns) for p in paths)


# ---------------------------------------------------------------------------
# Scheduling decisions
# ---------------------------------------------------------------------------

def _killed(env: Mapping[str, str] | None, key: str) -> bool:
    e = os.environ if env is None else env
    return str(e.get(key, "1")).strip() == "0"


def should_run_security(
    mode: str,
    files: Sequence[str],
    path_segments: Sequence[str] = SECURITY_PATH_SEGMENTS,
    basename_patterns: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> bool:
    """Foreground-only, path-triggered, kill-switchable.

    `files` are the repo-relative paths the change touches. Empty or
    non-matching tables mean "no security pass", never an error.
    """
    if _killed(env, SECURITY_PASS_ENV):
        return False
    if (mode or "").strip() != "now":
        return False
    return any_path_risky(files, path_segments, basename_patterns)


def should_run_skeptic(
    mode: str,
    trustworthy: bool,
    findings_total: int,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Foreground clean-check, only when the review would clear the gate.

    Not when the review is dirty (something already has to be fixed) and not
    when it is untrustworthy (it is being redone anyway). A `findings_total`
    that will not parse as an integer is treated as "unknown", which is not
    zero, so the pass does not fire.
    """
    if _killed(env, SKEPTIC_PASS_ENV):
        return False
    if (mode or "").strip() != "now":
        return False
    if not trustworthy:
        return False
    try:
        n = int(findings_total)
    except (TypeError, ValueError):
        n = -1
    return n == 0


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
# --- ORACLE TEXT BEGIN ---
# Extracted from the oracle's `_cmd_write_security_prompt` /
# `_cmd_write_skeptic_prompt` `lines = [...]` literals via its own AST. The
# `Branch:` / `Base:` / `Head:` block and the diff fences are interpolated by
# `_render` below in the oracle's order.
#
# The security lead keeps the oracle's wording EXCEPT at two `%(slot)s` spans,
# where the oracle named its own project's surfaces. Those are filled from
# `Defaults.security_prompt_slots` (generic by default; the oracle's exact
# fragments live in examples/, and loading them reproduces the oracle's prompt
# byte for byte). A slot value may contain newlines and expand to several lines,
# which is how the wrapped surface list keeps the oracle's line breaks. Nothing
# else in either template may contain a bare `%`.

_SECURITY_LEAD_TEMPLATE: tuple[str, ...] = (
    'You are a SECURITY-FOCUSED code reviewer on a pull request that touches',
    'risky surfaces (%(surfaces)s). This is a dedicated security pass — not a general',
    'style or house-rule checklist review.',
    '',
    'Look ONLY for concrete security problems in the unified diff:',
    '- authorization / authn bypass or missing access checks',
    '- data exposure (PII, secrets, cross-tenant leakage)',
    '- injection (SQL, command, template, path traversal)',
    '- secret handling (tokens, keys, signed URLs in logs)',
    '%(extra_checks)s',
    '',
    'Be precise and conservative. Do not invent issues or flag pure style.',
    'Do NOT modify files or run commands. Findings must anchor to the DIFF.',
    '',
    'Respond with ONLY a single JSON object (no prose, no markdown fences):',
    '{"summary":"one-line overall assessment","findings":[{"file":"path","line":0,'
    '"severity":"high|medium|low","category":"security","title":"short title",'
    '"detail":"why it matters"}]}',
    'If there are no real security issues, return an empty findings array.',
    '',
)
_SECURITY_PASS_LINE = 'Pass:   security (#3285)'

_SKEPTIC_LEAD: tuple[str, ...] = (
    'A previous reviewer cleared this pull-request diff (0 findings).',
    'Your job is the ADVERSARIAL CLEAN-CHECK: prove them wrong if you can.',
    '',
    'Review ONLY the unified diff. Hunt for real defects they may have missed:',
    'bugs, security issues, broken error handling, concurrency hazards, data',
    'loss, silent failures, and clear regressions. Be precise — do not invent',
    'style nits. Do NOT modify files or run commands.',
    '',
    'Respond with ONLY a single JSON object (no prose, no markdown fences):',
    '{"summary":"one-line overall assessment","findings":[{"file":"path","line":0,'
    '"severity":"high|medium|low","category":"bug|security|perf|correctness|other",'
    '"title":"short title","detail":"why it matters"}]}',
    'If the clear was correct and there are no real issues, return an empty',
    'findings array.',
    '',
)
_SKEPTIC_PASS_LINE = 'Pass:   skeptic / adversarial clean-check (#3284)'
# --- ORACLE TEXT END ---


def security_lead(
    prompt_slots: Sequence[tuple[str, str]] = SECURITY_PROMPT_SLOTS,
) -> tuple[str, ...]:
    """Render the security prompt's lead lines with `prompt_slots` filled in.

    FAIL-SOFT, like every other consumer of a config table in this module: a
    partial table leaves the unfilled slots on their generic defaults, and a
    slot name the template does not have is ignored (`config.load_config`
    already rejected that loudly, by name, at load time). A slot value spanning
    several lines expands to several prompt lines.
    """
    slots = dict(SECURITY_PROMPT_SLOTS)
    for name, fragment in prompt_slots or ():
        if name in slots:
            slots[name] = fragment
    return tuple(("\n".join(_SECURITY_LEAD_TEMPLATE) % slots).split("\n"))


def _render(
    lead: tuple[str, ...],
    pass_line: str,
    branch: str,
    base_ref: str,
    base_sha: str,
    head: str,
    diff: bytes,
    max_diff_bytes: int,
) -> Prompt:
    """Assemble one extra-pass prompt, in the oracle's line order.

    The diff is capped at `max_diff_bytes` and decoded with `errors="replace"`,
    exactly as the oracle does — an extra-pass prompt is text, and a binary hunk
    must not be the thing that kills the pass. Trailing newlines are stripped
    from the decoded diff (the oracle's `diff_text.rstrip("\\n")`), and the
    truncation marker, when there is one, goes between the diff and
    `----- END DIFF -----`.
    """
    if max_diff_bytes < 1:
        raise ValueError(f"max_diff_bytes must be >= 1, got {max_diff_bytes}")

    truncated = len(diff) > max_diff_bytes
    diff_text = diff[:max_diff_bytes].decode("utf-8", errors="replace")

    lines = [
        *lead,
        "Branch: %s" % branch,
        "Base:   %s (%s)" % (base_ref, base_sha),
        "Head:   %s" % head,
        pass_line,
        "",
        "----- BEGIN DIFF -----",
        diff_text.rstrip("\n"),
        "----- END DIFF -----",
    ]
    if truncated:
        lines.insert(-1, "----- DIFF TRUNCATED at %d bytes -----" % max_diff_bytes)
    text = ("\n".join(lines) + "\n").encode("utf-8")
    return Prompt(text=text, diff_truncated=truncated, prompt_bytes=len(text))


def security_prompt(
    branch: str,
    base_ref: str,
    base_sha: str,
    head: str,
    diff: bytes,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    prompt_slots: Sequence[tuple[str, str]] = SECURITY_PROMPT_SLOTS,
) -> Prompt:
    """The security-only role prompt: no checklist sections, security lens only.

    `prompt_slots` is `Defaults.security_prompt_slots` — what "risky surfaces"
    means in the repo under review. Unset, the prompt names generic concerns.
    """
    return _render(security_lead(prompt_slots), _SECURITY_PASS_LINE, branch,
                   base_ref, base_sha, head, diff, max_diff_bytes)


def skeptic_prompt(
    branch: str,
    base_ref: str,
    base_sha: str,
    head: str,
    diff: bytes,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> Prompt:
    """The adversarial clean-check prompt: "prove them wrong if you can"."""
    return _render(_SKEPTIC_LEAD, _SKEPTIC_PASS_LINE, branch, base_ref,
                   base_sha, head, diff, max_diff_bytes)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _as_findings(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


def _severity_counts(findings: Sequence[Mapping[str, Any]]) -> dict:
    sev = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        s = str(f.get("severity", "")).lower()
        if s in sev:
            sev[s] += 1
    return sev


def _rule_ids(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen = set()
    for f in findings:
        for m in _RULE_ID.finditer(str(f.get("title") or "")):
            rid = m.group(1)
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def _tag(finding: Mapping[str, Any], pass_name: str) -> dict:
    """Copy one extra-pass finding and mark which lens produced it."""
    g = dict(finding)
    cat = str(g.get("category") or "").strip()
    if not cat or cat == "other":
        if pass_name == "security":
            g["category"] = "security"
        elif pass_name == "skeptic":
            g["category"] = g.get("category") or "other"
    # Tag the source pass without [brackets] so rule_ids extraction (which
    # treats [kebab-id] as house-rule citations) is not polluted.
    title = str(g.get("title") or "")
    prefix = "(%s) " % pass_name
    if title and not title.lower().startswith(prefix.lower()):
        if title.startswith("["):
            # Keep [rule-id] at the start; note the pass in detail instead.
            detail = str(g.get("detail") or "")
            marker = "(extra-pass: %s) " % pass_name
            if marker not in detail:
                g["detail"] = marker + detail
        else:
            g["title"] = prefix + title
    return g


def _appended(previous: Any, addition: str) -> str:
    prev = str(previous or "").strip()
    return (prev + "; " if prev else "") + addition


def failed_pass_reason(pass_name: str) -> str:
    """The standard `failure_reason` for a pass that returned nothing usable."""
    return "extra pass %s failed to produce a usable review" % pass_name


def merge_extra_pass(
    primary: Mapping[str, Any],
    extra: Mapping[str, Any],
    pass_name: str,
) -> dict:
    """Fold one extra-pass review into a copy of the primary review record.

    CONTRACT
    --------
    `extra` is the record a pass that RAN produced, and must be a mapping. Two
    neighbouring situations are deliberately NOT expressible here:

    * a pass that ran and returned nothing usable → `merge_failed_extra_pass`,
      which demands an explicit reason and demotes the primary;
    * a pass `should_run_*` declined to schedule → do not merge it at all;
      there is nothing to fold in, and the primary keeps its own verdict.

    Passing `None` (or any non-mapping) raises instead of demoting, so no
    caller can quietly turn a good review into an untrustworthy one by handing
    this function the wrong thing. Within a merged record, `parse_ok` must be
    exactly `True` to count as success: a record that merely omits the key is a
    failed pass, not a clear one.

    Returns a NEW record; `primary` is not mutated and none of the containers
    this function builds are shared with it (DIVERGENCE 2 in the module
    docstring spells out what *is* shared by reference and never written).
    """
    if extra is None:
        raise TypeError(
            "merge_extra_pass() needs the record a pass produced; for a pass "
            "that produced nothing use merge_failed_extra_pass(primary, "
            "pass_name, failure_reason), and for a pass that never ran, merge "
            "nothing at all")
    if not isinstance(extra, Mapping):
        raise TypeError(
            "extra must be a mapping, got %s" % type(extra).__name__)
    return _merge(primary, extra, pass_name, "")


def merge_failed_extra_pass(
    primary: Mapping[str, Any],
    pass_name: str,
    failure_reason: str,
) -> dict:
    """Fold a FAILED extra pass into a copy of the primary review record.

    For a pass that was scheduled, ran, and produced no usable review at all.
    It clears `parse_ok`, clears `trustworthy`, and appends `failure_reason` —
    a demotion, so the reason is required and must not be blank (use
    `failed_pass_reason(pass_name)` when there is nothing more specific to
    say). DIVERGENCE 1 in the module docstring explains why this demotes at all
    where the oracle records a no-op.
    """
    reason = str(failure_reason or "").strip()
    if not reason:
        raise ValueError(
            "merge_failed_extra_pass() requires a non-empty failure_reason: "
            "this demotes the review and the record has to say why")
    return _merge(primary, None, pass_name, reason)


def _merge(
    primary: Mapping[str, Any],
    extra: Mapping[str, Any] | None,
    pass_name: str,
    failure_reason: str,
) -> dict:
    """Shared body of `merge_extra_pass` / `merge_failed_extra_pass`."""
    if not isinstance(primary, dict):
        raise TypeError("primary must be a dict")

    out = dict(primary)
    meta: dict = {"ran": extra is not None, "pass": pass_name}
    # `_as_findings` builds a new list, so extending it cannot reach back into
    # the caller's record.
    findings = _as_findings(out.get("findings"))

    if extra is None:
        # Not `skipped`: the pass ran and came back empty-handed, and the record
        # must not read as a no-op next to the `parse_ok: False` it just caused.
        meta["failed"] = True
        # DIVERGENCE 1: the oracle stops here. A pass we decided to run and got
        # nothing back from is a failure, not a no-op.
        out["parse_ok"] = False
        out["trustworthy"] = False
        out["failure_reason"] = _appended(out.get("failure_reason"),
                                          failure_reason)
    else:
        # `is True`, not truthiness and not `is not False`: a record that omits
        # `parse_ok` has not told us the pass parsed, and an extra pass that
        # cannot say so must not leave the primary clear.
        p_ok = extra.get("parse_ok") is True
        deg = extra.get("degraded") is True
        trunc = extra.get("diff_truncated") is True
        pf = _as_findings(extra.get("findings"))
        findings.extend(_tag(f, pass_name) for f in pf)
        meta.update({
            "parse_ok": p_ok,
            "degraded": deg,
            "diff_truncated": trunc,
            "findings_total": len(pf),
            "id": extra.get("id", ""),
        })
        if not p_ok:
            out["parse_ok"] = False
            out["trustworthy"] = False
            out["failure_reason"] = _appended(
                out.get("failure_reason"),
                str(extra.get("failure_reason") or "")
                or failed_pass_reason(pass_name))
        if deg:
            out["degraded"] = True
            out["trustworthy"] = False
            out["degraded_reason"] = _appended(
                out.get("degraded_reason"),
                str(extra.get("degraded_reason") or "")
                or "extra pass %s was degraded" % pass_name)
        if trunc:
            # A single-call extra pass may only see the first max_diff_bytes of
            # an (already batched) large primary. Record the partial coverage —
            # but do NOT demote for the cap alone: one extra call is the bound
            # these passes are built around, and demoting every large risky
            # change would make the pass unusable. Real parse/degraded failures
            # still demote, above.
            meta["partial_coverage"] = True
            note = ("extra pass %s saw a size-capped diff (partial coverage)"
                    % pass_name)
            summary = str(out.get("summary") or "").strip()
            if note not in summary:
                out["summary"] = _appended(summary, note)

    out["findings"] = findings
    out["findings_total"] = len(findings)
    out["severity"] = _severity_counts(findings)
    out["rule_ids"] = _rule_ids(findings)

    extras = out.get("extra_passes")
    extras = dict(extras) if isinstance(extras, dict) else {}
    extras[pass_name] = meta
    out["extra_passes"] = extras

    # Keep the summary honest about the merge.
    summary = str(out.get("summary") or "").strip()
    note = "merged %s pass (%d finding(s))" % (
        pass_name, meta.get("findings_total", 0) if extra is not None else 0)
    if extra is not None and note not in summary:
        out["summary"] = _appended(summary, note)

    return out
