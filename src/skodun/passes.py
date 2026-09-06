"""Extra review passes, and how their results fold back into the primary.

* **security** — a dedicated, security-only pass, scheduled only in `now` mode
  and only when the change touches a risky path.
* **skeptic** — an adversarial clean-check, scheduled only in `now` mode and
  only when the primary review came back trustworthy with zero findings. One
  extra call on the rounds that are about to clear the gate is cheap; a false
  clear is not.
* **refuter** — a *different provider* re-examines the finder's findings,
  because a model asked to check its own work is agreeable about it. Scheduled
  only in `now` mode, only when the finder came back trustworthy WITH findings,
  and only when a reviewer with role `refuter` is configured. Unlike the other
  two it is **annotation only**: see "The refuter is annotation only" below.

A fourth pass lives here and is NOT one of those three:

* **integration** — one cross-file pass over the seams a BATCHED review cuts.
  Batching guarantees every hunk is reviewed somewhere and guarantees no
  reviewer ever sees two batches at once, so it creates exactly one blind spot:
  a caller in file A broken by a change in file B, where A and B landed in
  different batches. This pass is that blind spot's only cover. It is
  scheduled by BATCH COUNT rather than by mode (`should_run_integration`) — a
  background prepush review gets it too — and it is COVERAGE, not annotation:
  its outcome joins the aggregate's trust axes, so a failed or degraded
  integration pass makes the whole batched aggregate untrustworthy. See "The
  integration pass" below.

The first three are opt-out via env kill switches (`SKODUN_SECURITY_PASS=0`,
`SKODUN_SKEPTIC_PASS=0`, `SKODUN_REFUTER_PASS=0`), so a wedged pass can be
turned off without a config edit or a code change. All three read the switch
through `_killed`, which compares against the exact string `"0"` — `bool("0")`
and `bool("false")` are both True in Python, and treating the env value as a
truthy string is how a kill switch silently stops killing. The integration pass
has NO such switch, deliberately: see "The integration pass".

PARITY-CRITICAL: vendored from the oracle's `scripts/grok-extra-passes.py`
(`path_is_risky`, `should_run_security`, `should_run_skeptic`,
`merge_extra_pass`, `write-security-prompt`, `write-skeptic-prompt`), and, for
the integration pass, from `scripts/grok-prepush-review.sh`'s `--run-batched`
region. The two prompt bodies were transferred from the oracle's own source —
the line lists were extracted from its AST, never retyped — and
`tests/test_passes.py` pins them byte-for-byte against the live oracle, along
with the trigger decision and the merge result. Deliberate divergences are
marked `DIVERGENCE` below and each one has its own test.

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

The refuter is annotation only, and that is a decision, not an omission
-----------------------------------------------------------------------
`merge_refuter_pass` is deliberately NOT `merge_extra_pass`. A refuter verdict
attaches to the finding it judges and moves nothing else: `findings_total`,
`severity`, `rule_ids`, `summary` and all three trust axes come out of the
merge exactly as they went in. A review whose only finding is marked `refuted`
still gates 1. Only a human, through `skodun triage --adopt-refuter`, can turn
a verdict into a dismissal — a model's opinion annotates, it never clears.

Two consequences follow, and both are the opposite of what the other passes do:

* **A failed refuter is a note, never a demotion.** Provider B being
  unavailable is an ABSENT ANNOTATION, not a broken review, so
  `merge_refuter_pass(primary, None, ...)` records `status: "failed"` and
  leaves `parse_ok`/`degraded`/`trustworthy` alone. The contrast with the
  security pass is deliberate: a failed security pass still demotes, whichever
  provider ran it. **Role semantics decide demotion, never provider identity.**
* **Indexes and eligibility come from a FINDER SNAPSHOT**, which is the
  caller's job to take (the pipeline snapshots `(finder_trustworthy,
  finder_findings)` immediately after the finder's parse, before any
  security/skeptic merge) and this module's job to respect:
  `finder_findings_total` bounds which findings a verdict may reach. Finder
  findings keep indexes `0..n-1` in the merged list because extra-pass merges
  APPEND — a verdict pointing past that range is a verdict about a finding the
  refuter was never shown, and it is dropped with a note rather than
  misattributed.

The reasoning floor is `triage`'s, measured `triage`'s way. A verdict whose
reasoning is shorter than `MIN_REASON_CHARS` once whitespace-collapsed is kept
for the human but marked `thin_reasoning`, which is what later refuses to adopt
it. The measurement uses `textnorm.collapse_ws` — the same helper
`triage.validate_reason` uses, and NOT `textnorm.norm`, which lowercases and
can therefore *lengthen* a string past a floor it should not clear.

The channel is unauthenticated by construction, so the module authenticates
it on the way in. `_annotated` is the only place a `refuter` annotation is
WRITTEN, but a finding's `refuter` key can also arrive already present, since
the finder's parsed findings are untrusted model output handed to this module
verbatim. `_strip_finder_refuter_keys` drops any such incoming key before
`merge_refuter_pass` or `skipped_refuter_pass` runs — even on the skip path,
which writes no verdicts of its own and so has nothing else to overwrite a
forgery with. Same defensive class as `_finding_lines`'s title-collapsing:
untrusted, model-authored text must not be able to forge structure a reader
(here, a later `triage --adopt-refuter`) would trust.

The integration pass
--------------------
Everything above is about one review of one diff. The integration pass is about
a review that had to be split, and its rules follow from that:

**Two batches or more, or not at all.** `should_run_integration` is the whole
schedule: one batch has no cross-batch relationship to find, and asking a model
to compare one file list with itself would bill a call for a foregone answer.
The floor is the oracle's (`BATCH_COUNT -ge 2`), and `integration_prompt`
REFUSES fewer than two batch summaries rather than rendering a degenerate
prompt — the same choice `merge_extra_pass(primary, None)` makes for the same
reason: a caller error that costs a model call should not be expressible.

**No kill switch.** The three `--now` passes have one because each is an extra
opinion, and an extra opinion can be given up. This pass is the only cover for
the blind spot batching cut, so switching it off would leave a batched
aggregate reading as a full review of a change no reviewer ever saw whole —
which is the false clear the fail-closed rule exists to forbid. A run that does
not want it can stay under one batch.

**Headers, never bodies.** The prompt carries each batch's file list, its
`diff --git`/`@@` header lines (`batching.changed_regions`, capped per batch),
its one-line summary and its findings. Bodies are what did not fit one prompt;
carrying them would rebuild the prompt batching exists to avoid. The extraction
happens INSIDE the builder, from the batch's own bytes, so no call site can
leak a body by handing over the wrong thing.

**Checklist modes, first consumers.** `checklist.select` has had `batch` and
`integration` modes since Phase 1 with nothing to use them. Now: a per-batch
prompt selects `batch`, which never injects a cross-file rule (a rule about
relationships between files, asked of a reviewer holding one slice of the
change, is a false-positive engine), and the integration prompt selects
`integration`, which is `core` + `cross-file` only. `batch_checklist_mode`
carries the one exception, which is the oracle's: the SOLE batch selects `full`,
because with one batch there is no integration pass and that batch IS the whole
diff — anything else would make a one-batch run review less than the same diff
reviewed unbatched.

**Its own record shape.** The pass does not go through `merge_extra_pass`:
there is no "primary" to fold into, only an aggregate assembled from every
batch. `integration_meta` is the one shape its outcome persists as (oracle A8's
`integration{}`), and it carries `attempts` directly. For extra passes, the
pipeline attaches runtime attempts after this module's pure merge. These are
execution observations; how pass outcomes JOIN the aggregate's trust axes is
the aggregation step's, not this module's.

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
   carries it next to the bytes. `integration_prompt` too, for the same reason:
   the oracle's builder prints `"1"`/`"0"` on stdout precisely so its caller can
   set `GR_DIFF_TRUNCATED`, and that fact belongs beside the bytes it describes.
4. **A non-positive `max_diff_bytes` raises** rather than being clamped back to
   the oracle's default — matching `promptbuild.build`, and for the same reason:
   a zero budget silently ships a prompt with no diff in it, which reads to the
   model as "nothing changed". Same for `integration_prompt`'s
   `max_prompt_bytes`.
5. **Integration findings are TAGGED.** The oracle folds them into the aggregate
   raw (`findings.extend(inf)`), so nothing in the stored record distinguishes a
   cross-file finding from a within-batch one. `tag_integration_findings` puts
   them through the same `_tag` rules every other extra pass uses, because which
   lens produced a finding is the first thing a reader of `surface` needs and it
   is unrecoverable once merged.
6. **Untrusted batch content cannot forge prompt structure.** A batch's summary
   and findings are MODEL OUTPUT being pasted into another model's prompt — the
   same class of problem `_finding_lines` collapses titles for. The oracle
   interpolates them raw, so a summary containing a newline could open a batch
   block of its own; here every interpolated field is whitespace-collapsed, so
   one batch is one block and one finding is one line. The 300-character detail
   bound is the oracle's (it measures the collapsed text here, which is the same
   budget spent on content rather than on whitespace).
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .adapters import REFUTER_VERDICTS
from .batching import MAX_REGION_LINES, changed_regions
from .config import SECURITY_PATH_SEGMENTS, SECURITY_PROMPT_SLOTS, Defaults
from .promptbuild import RULES_BEGIN, RULES_END, Prompt, advisory_context
from .textnorm import collapse_ws
from .triage import MIN_REASON_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from skodun.checklist import Selection

#: Env kill switches. Set any of them to `0` to never schedule that pass.
SECURITY_PASS_ENV = "SKODUN_SECURITY_PASS"
SKEPTIC_PASS_ENV = "SKODUN_SKEPTIC_PASS"
REFUTER_PASS_ENV = "SKODUN_REFUTER_PASS"

#: The refuter pass's name, in `extra_passes` and in every note it writes.
REFUTER_PASS = "refuter"

#: The integration pass's name, in `integration{}`, in the reviewer-role table
#: and in the `(integration) ` tag on every finding it produces.
INTEGRATION_PASS = "integration"

#: The reviewer role the integration pass prefers over the finder. One of
#: `config.ROLES`, shipped since Phase 1 and unused until this pass existed.
INTEGRATION_ROLE = "integrator"

#: `checklist.select` modes. `integration` is `core` + `cross-file` only;
#: `batch` never injects a cross-file rule. Both shipped in Phase 1 with no
#: consumer; these are the two names that give them one.
INTEGRATION_CHECKLIST_MODE = "integration"
BATCH_CHECKLIST_MODE = "batch"

#: What the integration pass's `status` may say. `skipped` is deliberately NOT
#: here: a run that did not earn the pass records no `integration{}` at all
#: (readers tolerate absence), so a `skipped` status would be a second, weaker
#: way of saying the same thing.
INTEGRATION_STATUSES = ("ran", "degraded", "failed")

#: The diff budget, sourced from config so there is exactly one of this number
#: (it is also the oracle's own `GROK_MAX_DIFF_BYTES` default).
DEFAULT_MAX_DIFF_BYTES: int = Defaults.max_diff_bytes

_NON_ALNUM = re.compile(r"[^a-z0-9]")
#: `[kebab-rule-id]` citations, as `_rule_ids` reads them out of a finding's
#: title. The extracted list is persisted telemetry on the review record
#: (`rule_ids`) and nothing else consumes it: no module keys, filters or gates
#: on a rule id.
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


def _enabled_refuter(cfg: Any) -> bool:
    """Whether `cfg` names an enabled reviewer with role `refuter`.

    Duck-typed and total: a config this function cannot read at all means "no
    refuter", which is a silent skip. The refuter is an annotation pass, so
    fail-soft here costs an annotation and never a trust decision — the exact
    opposite of the fail-closed reading every trust axis gets.
    """
    for r in getattr(cfg, "reviewers", None) or ():
        if getattr(r, "enabled", False) and getattr(r, "role", "") == "refuter":
            return True
    return False


#: What the record says when a review earned a refuter pass and no reviewer was
#: configured to run one. Recorded rather than raised: an unconfigured pass is
#: not an error, and a review that silently never mentions the refuter looks
#: exactly like one whose refuter agreed with everything.
NO_REFUTER_CONFIGURED = (
    "no enabled reviewer with role 'refuter' is configured, so the "
    "cross-provider refuter pass was skipped")


def refuter_decision(
    mode: str,
    finder_trustworthy: bool,
    finder_findings_total: int,
    cfg: Any,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """`(run, skip_note)` for the refuter pass, from the FINDER SNAPSHOT.

    `finder_trustworthy` and `finder_findings_total` describe the finder's own
    result, taken before any extra-pass merge. That is what makes this decision
    mean "the finder produced findings worth a second opinion" rather than
    "the record currently has findings from somewhere": a security finding must
    not trigger a refuter the finder did not earn, and a security demotion must
    not suppress one it did.

    `skip_note` is non-empty for EXACTLY ONE skip: a review that was otherwise
    eligible and has no refuter configured. Every other skip — killed, not
    `now`, untrustworthy finder, nothing found — records nothing at all, the
    same way an unscheduled security or skeptic pass does; a `refuter` key on
    every clean review would say nothing and mean less.
    """
    if _killed(env, REFUTER_PASS_ENV):
        return False, ""
    if (mode or "").strip() != "now":
        return False, ""
    if not finder_trustworthy:
        return False, ""
    try:
        n = int(finder_findings_total)
    except (TypeError, ValueError):
        # "Unknown" is not "some": a count that will not parse never fires the
        # pass, exactly as in `should_run_skeptic`.
        n = -1
    if n <= 0:
        return False, ""
    if not _enabled_refuter(cfg):
        return False, NO_REFUTER_CONFIGURED
    return True, ""


def should_run_refuter(
    mode: str,
    finder_trustworthy: bool,
    finder_findings_total: int,
    cfg: Any,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether to spend a second provider's call re-examining the findings."""
    return refuter_decision(mode, finder_trustworthy, finder_findings_total,
                            cfg, env)[0]


def should_run_integration(batch_count: Any) -> bool:
    """Whether a batched review earns a cross-file integration pass.

    Two batches or more, and nothing else — the oracle's `BATCH_COUNT -ge 2`.
    One batch has no cross-batch relationship to find (that batch IS the whole
    diff), and zero batches is the terminal "batching produced nothing" failure,
    which needs a record and not another model call.

    Deliberately NOT mode-gated, unlike the three passes above: the blind spot
    this covers is cut by batching, not by `--now`, so a background prepush
    review that had to be split needs it just as much. Deliberately NOT
    kill-switchable either — see "The integration pass" in the module docstring.

    A count that will not parse as an integer is treated as "unknown", which is
    not "two or more", exactly as in `should_run_skeptic`.
    """
    try:
        n = int(batch_count)
    except (TypeError, ValueError):
        n = 0
    return n >= 2


def batch_checklist_mode(batch_count: Any) -> str:
    """Which `checklist.select` mode ONE batch's prompt asks for.

    `batch` — never a cross-file rule — for every batch of a real split. A rule
    about relationships between files, injected into a prompt holding one slice
    of the change, asks the reviewer to check a contract whose other half is in
    a batch it cannot see; that is a false-positive engine, and it is why the
    cross-file rules go to the integration pass instead.

    ORACLE BEHAVIOR at the sole-batch edge, and it is the one place this
    diverges from the plan's "per-batch prompts select mode `batch`": ONE batch
    selects `full`, because then there is no integration pass and that batch is
    the whole diff. `pipeline.run_review` selects `"full"` for an unbatched
    review, so this keeps a one-batch run byte-identical to the same diff
    reviewed unbatched — anything else would make batching quietly review LESS.

    An unparseable or degenerate count reads as "not the sole batch", i.e. never
    cross-file: the fail-closed direction for a false-positive hazard.
    """
    try:
        n = int(batch_count)
    except (TypeError, ValueError):
        n = 0
    return "full" if n == 1 else BATCH_CHECKLIST_MODE


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

# The refuter prompt is skodun's own; the oracle had no such pass, so there is
# nothing here to keep byte-compatible.
#
# PUBLIC OSS HYGIENE — this text is shipped data, sent to a model in every repo
# that runs skodun, and is held to the same rule as any other committed string:
# no upstream-project names, no one repo's layout vocabulary, no machine paths.
# Unlike the security prompt it names no repo-specific concept at all — it talks
# about "the diff" and "the findings" and nothing else — so it needs no slot
# interface and takes no config. There is deliberately no `%(slot)s` span here
# to fill, and `test_the_shipped_refuter_prompt_is_generic_and_slot_free` pins
# that.
#
# The floor is interpolated from `MIN_REASON_CHARS` rather than typed in, so
# the number the model is asked for and the number the merge measures cannot
# drift apart.
_REFUTER_LEAD: tuple[str, ...] = (
    'You are re-examining another reviewer\'s findings on a pull-request diff.',
    'You did not write them and you are not being asked to agree with them.',
    'A finding that is wrong costs a human real time, so saying so is the job.',
    '',
    'For EACH numbered finding below, decide from the diff alone:',
    '- "confirmed" - the diff really does contain the problem described',
    '- "refuted"   - the finding is wrong: the code does not do what it says,',
    '                the concern is already handled, or it does not apply here',
    '- "uncertain" - the diff alone does not settle it',
    '',
    'Justify every verdict with the evidence that decided it: the line or',
    'construct in the diff, never a restatement of the finding. At least',
    '%d characters of real justification per verdict -- a verdict that does'
    % MIN_REASON_CHARS,
    'not justify itself is discarded. Do not add new findings, do not',
    're-review the change, and do not soften a verdict to be agreeable.',
    '',
    'Do NOT modify files or run commands. Judge ONLY the unified diff below.',
    '',
    'Respond with ONLY a single JSON object (no prose, no markdown fences):',
    '{"verdicts":[{"index":0,"verdict":"confirmed|refuted|uncertain",'
    '"reasoning":"the evidence in the diff that decided it"}]}',
    'Use each finding\'s own bracketed index. Return exactly one verdict per',
    'finding, in any order; judge every one, and omit none.',
    '',
)
_REFUTER_PASS_LINE = 'Pass:   refuter / cross-provider re-examination'
_FINDINGS_BEGIN = '----- BEGIN FINDINGS UNDER RE-EXAMINATION -----'
_FINDINGS_END = '----- END FINDINGS UNDER RE-EXAMINATION -----'


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


def refuter_lead() -> tuple[str, ...]:
    """The refuter prompt's lead lines. No slots, no config, one text.

    Present as a function for symmetry with `security_lead` and so that the
    hygiene test has one thing to read — but it takes no arguments, and that
    is the point: nothing a repo configures can change what this prompt says.
    """
    return _REFUTER_LEAD


def _finding_lines(findings: Sequence[Any]) -> list[str]:
    """The findings block: one `[i]` entry per finding, indented detail.

    The bracketed number is the finding's position in the list it was handed —
    the FINDER's own numbering — and it is what `merge_refuter_pass` keys a
    verdict's `index` back onto. Every element gets an entry, including one
    that is not a mapping at all: skipping a malformed finding would shift
    every later index by one and silently re-point every verdict after it.

    Titles are whitespace-collapsed so one finding is one `[i]` line; details
    keep their own line structure, INDENTED, because a multi-line explanation
    is the normal shape and flattening it costs the model context.

    Both of those also bound what a finding can forge. A finding's text comes
    from a model, so it is untrusted content being pasted into another model's
    prompt: a title carrying a newline would otherwise open a second `[i]`
    entry, and a detail line reading `----- END FINDINGS ... -----` at column 0
    would close the block early. Collapsing the title and indenting every
    detail line keeps both at the structural level of a detail — visible to a
    reader, not a forged frame.
    """
    lines = [_FINDINGS_BEGIN]
    for i, raw in enumerate(findings or ()):
        f = raw if isinstance(raw, Mapping) else {}
        where = collapse_ws(f.get("file")) or "(file not stated)"
        line_no = f.get("line")
        if isinstance(line_no, int) and not isinstance(line_no, bool):
            where = "%s:%d" % (where, line_no)
        severity = collapse_ws(f.get("severity")) or "unrated"
        title = collapse_ws(f.get("title")) or "(no title)"
        lines.append("[%d] (%s) %s -- %s" % (i, severity, where, title))
        detail = str(f.get("detail") or "").strip()
        lines.extend("    " + d for d in detail.splitlines() if d.strip())
    if len(lines) == 1:
        # `should_run_refuter` never schedules this, but a caller may still
        # build the prompt; an empty block would read as "nothing to judge"
        # while the instructions demand a verdict per finding.
        lines.append("(no findings were supplied)")
    lines.append(_FINDINGS_END)
    return lines


def refuter_prompt(
    finder_findings: Sequence[Any],
    diff: bytes,
    branch: str,
    base_ref: str,
    base_sha: str,
    head: str,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> Prompt:
    """The cross-provider re-examination prompt: the diff, plus the findings.

    Takes the diff BYTES, like `security_prompt` and `skeptic_prompt`: the
    pipeline already holds `diff.data`, no diff file exists anywhere in this
    path, and introducing one to pass a value that is already in scope would
    be a filesystem round trip for nothing.

    Returns a `Prompt` rather than bare bytes, for the reason DIVERGENCE 3 in
    the module docstring gives for the other two builders: the truncation flag
    belongs next to the bytes it describes, and the caller records it as this
    pass's partial coverage.
    """
    lead = _REFUTER_LEAD + tuple(_finding_lines(finder_findings)) + ("",)
    return _render(lead, _REFUTER_PASS_LINE, branch, base_ref, base_sha, head,
                   diff, max_diff_bytes)


# ---------------------------------------------------------------------------
# The integration prompt
# ---------------------------------------------------------------------------
# Ported from the oracle's `--run-batched` integration-context builder (an
# inline `python3 - <<'PY'` heredoc, so this is a vendor-and-adapt port of real
# code). The STRUCTURE is the oracle's — cross-file-only instruction, headers
# without bodies, one `===== BATCH n =====` block per batch carrying files /
# changed regions / summary / findings, and a whole-prompt byte cap that flags
# rather than silently drops.
#
# PUBLIC OSS HYGIENE — this is shipped prompt data, sent to a model in every
# repo that runs skodun, and is held to the same rule as any other committed
# string: no upstream-project names, no one repo's layout vocabulary, no machine
# paths. The oracle's own text cited a rule id out of its own registry as the
# citation example; the example here is the one `promptbuild._INTRO` already
# ships, so skodun has exactly ONE rule-id example across every prompt it sends
# and no second project-flavoured literal in the tree. Like the refuter prompt
# and unlike the security prompt, this text names no repo-specific concept at
# all — it talks about batches, files and findings — so it needs no slot
# interface and takes no config. There is deliberately no `%(slot)s` span to
# fill, and `test_the_shipped_integration_prompt_is_generic_and_slot_free` pins
# that.
#
# Apart from that ONE example the lead is the oracle's own wording, line for
# line, and the JSON contract line is byte-identical to it (pinned by
# `test_oracle_response_contract_is_byte_identical`) — it is a schema the model
# is asked to match, so a stray comma there costs more than a whole class of
# bugs here, exactly as `promptbuild`'s docstring says of the primary contract.
#
# `%d` is the batch count and is the ONLY `%` in the template; nothing else here
# may contain a bare one (same rule as `_SECURITY_LEAD_TEMPLATE`). The oracle
# interpolates the count with `%s` off an env var that defaults to `"?"`; in
# process the count is `len(batch_summaries)` and always known, so `%d` refuses
# a non-number rather than rendering a prompt that says "reviewed in ? batches".
_INTEGRATION_LEAD_TEMPLATE: tuple[str, ...] = (
    'You are a senior code reviewer doing the FINAL CROSS-FILE INTEGRATION',
    'pass of a large pull request that was reviewed in %d separate batches.',
    'Each batch below lists the files it covered, its changed regions (the',
    '`diff --git` + `@@` hunk headers, which name the changed files and',
    'enclosing functions), its one-line summary, and its within-batch',
    'findings. Full hunk bodies are omitted (the diff was too large for one',
    'prompt -- that is why it was batched). Your job is to surface ONLY',
    'cross-file / integration problems that a single-batch review could not',
    'see -- e.g. a caller in one file broken by a signature or behaviour',
    'change in another, an inconsistent contract across files, a removed',
    'symbol still used elsewhere, or a migration/schema change not matched by',
    'its callers. Do NOT repeat within-batch findings already listed. Be',
    'precise and conservative -- do not invent issues. Do NOT modify files or',
    'run commands.',
    '',
    'Respond with ONLY a single JSON object (no prose, no markdown fences):',
    '{"summary":"one-line cross-file assessment","findings":[{"file":"path",'
    '"line":0,"severity":"high|medium|low",'
    '"category":"bug|security|perf|correctness|other",'
    '"title":"short title","detail":"why it matters"}]}',
    'If there are no cross-file issues, return an empty findings array.',
    'Additionally check against the repo rules below; cite the rule id in the',
    'finding title when one is violated (e.g. "[no-blocking-handler] ...").',
    '',
)

#: One batch's block frame. The number is the batch's POSITION in the list this
#: builder was handed, which is the order the splitter produced and the order
#: the aggregate reports — so `BATCH 2` here and `batches[1]` there are the same
#: batch without a cross-reference.
_INTEGRATION_BATCH = "===== BATCH %d ====="

#: Said once per batch whose region list hit the cap, so a short list never
#: reads as "this batch changed almost nothing".
_REGIONS_OMITTED = "... (more changed regions omitted)"

_INTEGRATION_TRUNCATED = "----- INTEGRATION CONTEXT TRUNCATED at %d bytes -----"

#: How much of one within-batch finding's detail travels into the cross-file
#: prompt. The oracle's `[:300]`: enough to say what the finding was about,
#: bounded because this prompt carries EVERY batch's findings and a handful of
#: essays would crowd out the change signal they are context for.
_MAX_DETAIL_CHARS = 300


@dataclass(frozen=True)
class BatchSummary:
    """One batch's contribution to the integration pass's view of the change.

    `diff` is that batch's own diff BYTES, and `integration_prompt` reads
    nothing out of them but `diff --git` and `@@` header lines. Handing over the
    whole batch — rather than a pre-extracted list of header lines — is
    deliberate: "no hunk body ever reaches this prompt" is the one property the
    pass must have, and it belongs to the builder, not to every call site that
    might forget. A caller that passes everything still cannot leak a body.

    `files` is the batch's changed-file list (`Batch.files`), `summary` and
    `findings` are what that batch's own review produced — both MODEL OUTPUT,
    and treated as such: see DIVERGENCE 6.
    """

    files: Sequence[str] = ()
    diff: bytes = b""
    summary: str = ""
    findings: Sequence[Any] = ()


def integration_lead(batch_count: int) -> tuple[str, ...]:
    """The integration prompt's lead lines, for a run of `batch_count` batches.

    Present as a function for symmetry with `security_lead` / `refuter_lead`,
    and it takes the batch count and nothing else — no config, no slots. What
    this prompt says cannot be changed by the repo under review.
    """
    return tuple(("\n".join(_INTEGRATION_LEAD_TEMPLATE)
                  % int(batch_count)).split("\n"))


def _integration_finding_line(finding: Mapping[str, Any]) -> str:
    """One within-batch finding as ONE line of cross-file context.

    Every field is whitespace-collapsed (DIVERGENCE 6): a title carrying a
    newline would otherwise open a line of its own, and a detail line reading
    `===== BATCH 9 =====` at column 0 would forge a batch block. The placeholders
    and the `(severity)` parens are `_finding_lines`' — one vocabulary for
    rendering a finding into a prompt, and parens rather than brackets so a
    model echoing the severity back cannot mint `[high]` as a rule-id citation
    for `_rule_ids` to harvest.

    A `line` is rendered only when it really is an integer, exactly as in
    `_finding_lines`: a `line` that arrived as a mapping would otherwise print
    its braces into the prompt.
    """
    where = collapse_ws(finding.get("file")) or "(file not stated)"
    line_no = finding.get("line")
    if isinstance(line_no, int) and not isinstance(line_no, bool):
        where = "%s:%d" % (where, line_no)
    severity = collapse_ws(finding.get("severity")) or "unrated"
    title = collapse_ws(finding.get("title")) or "(no title)"
    detail = collapse_ws(finding.get("detail"))[:_MAX_DETAIL_CHARS]
    rendered = "(%s) %s -- %s" % (severity, where, title)
    return rendered + (" -- " + detail if detail else "")


def integration_prompt(
    batch_summaries: Sequence[BatchSummary],
    selection: Selection | None = None,
    max_prompt_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    max_region_lines: int = MAX_REGION_LINES,
    stack_context: bytes | None = None,
    stack_context_truncated: bool = False,
    lineage_context: bytes | None = None,
    lineage_context_truncated: bool = False,
) -> Prompt:
    """The cross-file pass over the seams a batched review cut.

    `batch_summaries` are the batches in SPLIT ORDER, at least two of them.
    Fewer raises: with one batch there is nothing cross-batch to find, and a
    prompt asking a model to compare one file list with itself would bill a call
    for a foregone answer. Callers decide with `should_run_integration`; the
    raise is there so a caller that forgot cannot express the mistake, the same
    choice `merge_extra_pass(primary, None, ...)` makes (DIVERGENCE 1).

    `selection` is a `checklist.select(..., mode="integration")` result — `core`
    + `cross-file` rules — or None for a repo with no checklists. Its body is
    fenced with `promptbuild`'s own REPO RULES markers, imported rather than
    re-spelled, so every prompt skodun sends frames its rules identically.

    `max_prompt_bytes` caps the WHOLE rendered prompt (the oracle caps the same
    context at `MAX_DIFF_BYTES`). Over the cap, the text is cut, a truncation
    marker is appended, and `Prompt.diff_truncated` is set: the axis the trust
    invariant reads, and the axis the oracle's own caller sets from this
    builder's `"1"`/`"0"`. This pass has no diff of its own, so there is nothing
    else for that flag to mean here — a cut context is cross-file relationships
    the model was never shown, which is exactly a coverage hole.
    """
    if max_prompt_bytes < 1:
        raise ValueError(
            f"max_prompt_bytes must be >= 1, got {max_prompt_bytes}")
    summaries = list(batch_summaries or ())
    if len(summaries) < 2:
        raise ValueError(
            "the integration pass needs at least 2 batch summaries (a single "
            "batch has no cross-batch relationship to find and IS the whole "
            f"diff); got {len(summaries)} — ask should_run_integration("
            "batch_count) before building this prompt")

    lines = list(integration_lead(len(summaries)))

    # `.body` via getattr so a caller may hand over any selection-shaped value;
    # the trailing-newline collapse is `promptbuild.build`'s, so a body that is
    # nothing but newlines drops the section rather than emitting it empty.
    rules = str(getattr(selection, "body", "") or "").rstrip("\n")
    if rules:
        lines.extend([RULES_BEGIN.decode("utf-8").rstrip("\n"), rules,
                      RULES_END.decode("utf-8").rstrip("\n"), ""])

    for position, batch in enumerate(summaries, 1):
        lines.append(_INTEGRATION_BATCH % position)
        files = [collapse_ws(f) for f in (batch.files or ())]
        files = [f for f in files if f]
        lines.append("Files: " + (", ".join(files) if files else "(unknown)"))
        regions, capped = changed_regions(batch.diff or b"", max_region_lines)
        if regions:
            lines.append("Changed regions:")
            lines.extend("  " + r for r in regions)
            if capped:
                lines.append("  " + _REGIONS_OMITTED)
        lines.append("Summary: " + collapse_ws(batch.summary))
        found = _as_findings(list(batch.findings or ()))
        if found:
            lines.append("Findings:")
            lines.extend("  - " + _integration_finding_line(f) for f in found)
        else:
            lines.append("Findings: none")
        lines.append("")

    text = ("\n".join(lines) + "\n").encode("utf-8", "replace")
    extra = advisory_context(stack_context, lineage_context)
    body_budget = max_prompt_bytes
    if extra:
        body_budget = max(1, max_prompt_bytes - len(extra))
    truncated = len(text) > body_budget
    if truncated:
        # `errors="ignore"` on the way back, because the cut may land inside a
        # multi-byte character; the marker then goes on the end, so the returned
        # bytes are allowed to exceed the cap by its length. The oracle does
        # both, and a prompt that says it was cut is worth those bytes.
        # Stack/lineage are reserved out of the cap so a huge seam body cannot
        # drop the advisory blocks the batch prompts already carried.
        cut = text[:body_budget].decode("utf-8", "ignore")
        text = (cut + "\n" + (_INTEGRATION_TRUNCATED % max_prompt_bytes)
                + "\n").encode("utf-8", "replace")
    text = text + extra
    return Prompt(
        text=text, diff_truncated=truncated, prompt_bytes=len(text),
        stack_context_bytes=len(stack_context or b""),
        stack_context_truncated=bool(stack_context)
        and stack_context_truncated is True,
        lineage_context_bytes=len(lineage_context or b""),
        lineage_context_truncated=bool(lineage_context)
        and lineage_context_truncated is True)


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
        elif pass_name in ("skeptic", INTEGRATION_PASS):
            # Both prompts offer the same `bug|security|perf|correctness|other`
            # vocabulary, so a finding that named no category gets the same
            # `other` from either lens. NOT `integration`: what kind of problem
            # this is and which lens found it are different facts, and the tag
            # on the title already carries the second one.
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


def tag_integration_findings(findings: Sequence[Any]) -> list[dict]:
    """Copy each cross-file finding and mark which lens produced it.

    The integration pass's findings do not go through `merge_extra_pass` —
    there is no primary review to fold them into, only an aggregate assembled
    from every batch — but they get the SAME `_tag` treatment every other extra
    pass's findings get, for the same reasons: a `(integration) ` title prefix
    so a reader can tell a cross-file finding from a within-batch one, and a
    `[rule-id]` citation left at position 0 with the tag moved into `detail` so
    `rule_ids` extraction stays clean.

    Entries that are not findings are dropped, by `_as_findings` — the one
    definition of that question this module has. Nothing is mutated: `_tag`
    copies before it writes. (DIVERGENCE 5: the oracle merges these raw.)
    """
    return [_tag(f, INTEGRATION_PASS)
            for f in _as_findings(list(findings or ()))]


def checklist_meta(mode: str, selection: Any) -> dict:
    """The persisted shape of ONE checklist selection.

    Recorded per batch (in the aggregate's `batches[]`) and for the integration
    pass, so a stored review says which rules each prompt actually carried —
    without which "the reviewer missed a cross-file problem" and "the reviewer
    was never given the cross-file rules" are indistinguishable after the fact.

    Duck-typed and total, like every other config-shaped reader here: a
    `selection` of None yields the same KEYS with empty values, so a reader
    never has to ask whether a key is missing because selection failed or
    because this outcome spells it differently.

    `Selection.body` is deliberately absent: it is prompt bytes, sometimes 18KB
    of them, and an artifact is a record of what happened rather than a second
    copy of what was sent.
    """
    return {
        "mode": str(mode or ""),
        "sections": list(getattr(selection, "sections", ()) or ()),
        "bytes_total": int(getattr(selection, "bytes_total", 0) or 0),
        "dropped": list(getattr(selection, "dropped", ()) or ()),
        "over_budget": getattr(selection, "over_budget", False) is True,
        "degraded": getattr(selection, "degraded", False) is True,
        "note": str(getattr(selection, "note", "") or ""),
    }


def integration_meta(
    status: str,
    *,
    ran: bool,
    parse_ok: bool = False,
    degraded: bool = False,
    diff_truncated: bool = False,
    findings_total: int = 0,
    stop_reason: Any = None,
    attempts: Sequence[Any] = (),
    provenance: Mapping[str, Any] | None = None,
    checklist: Mapping[str, Any] | None = None,
    note: str = "",
) -> dict:
    """The fixed skeleton of the artifact's `integration{}` object.

    ONE shape for every outcome the pass has (`ran`, `degraded`, `failed`), for
    the reason `_refuter_meta` gives: a reader — and the aggregation step —
    should never have to ask whether a key is missing because the pass did not
    get that far or because this outcome spells it differently. The fourth
    outcome, "not scheduled", records nothing at all: a run with fewer than two
    batches has no `integration{}` key, and readers tolerate its absence.

    `status` is checked against `INTEGRATION_STATUSES` because this is the field
    a human reads first, and a typo in it would be invisible in an artifact.

    `attempts` is carried directly here. For `extra_passes[<name>]`, the
    pipeline attaches runtime attempts after the pure merge; this module
    keeps its merge schema and trust behavior independent of those observations.

    `stop_reason` is carried for a narrower but equally concrete reason: this
    pass is one of the terms `pipeline._aggregate_stop_reason` reads, and it was
    the only one whose value the artifact did not record. `batches[]` has carried
    a `stop_reason` per sub-review since Task 6, so a word at the top of the
    record could be traced to the batch that reported it — but a word this pass
    reported appeared at the top attributable to nothing, which is exactly how a
    `SUCCESS` nobody could find the source of was read as a verdict. `None` on
    every path where nothing answered, like the batch entries.

    `provenance` is `{provider, model, effort}` for the attempt that answered,
    exactly as `pipeline._provenance` builds it. Explicit `None`s when nothing
    answered, never absent keys: a meta object that quietly omits them invites a
    reader to assume the pass ran on the finder's model.

    This function records; it decides nothing. How `parse_ok`/`degraded`/
    `diff_truncated` JOIN the aggregate's own axes belongs to the aggregation
    step, which is the only place that can see every batch as well as this.
    """
    if status not in INTEGRATION_STATUSES:
        raise ValueError(
            "unknown integration status %r; expected one of %s (a pass that "
            "was never scheduled records no integration{} at all)"
            % (status, ", ".join(INTEGRATION_STATUSES)))
    prov = dict(provenance or {})
    meta = {
        "pass": INTEGRATION_PASS,
        "ran": bool(ran),
        "status": status,
        "parse_ok": bool(parse_ok),
        "degraded": bool(degraded),
        "diff_truncated": bool(diff_truncated),
        "findings_total": int(findings_total),
        "stop_reason": stop_reason,
        "attempts": list(attempts or ()),
        "provider": prov.pop("provider", None),
        "model": prov.pop("model", None),
        "effort": prov.pop("effort", None),
        "checklist": dict(checklist) if checklist is not None else None,
        "note": str(note or ""),
    }
    # `_provenance` adds a `note` of its own when no attempt started a process;
    # anything else it carries lands beside the three identity fields rather
    # than being dropped.
    prov_note = str(prov.pop("note", "") or "").strip()
    if prov_note:
        meta["note"] = "; ".join(n for n in (meta["note"], prov_note) if n)
    meta.update(prov)
    return meta


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


# ---------------------------------------------------------------------------
# The refuter merge — annotation only
# ---------------------------------------------------------------------------

#: How many dropped-verdict indexes one note names before it stops listing
#: them. A note is for a human; a hundred integers is not.
_MAX_LISTED_INDEXES = 10


def _listed(indexes: Sequence[int]) -> str:
    shown = ", ".join(str(i) for i in indexes[:_MAX_LISTED_INDEXES])
    if len(indexes) > _MAX_LISTED_INDEXES:
        shown += ", ..."
    return shown


def _refuter_meta(status: str, ran: bool) -> dict:
    """The fixed skeleton of every `extra_passes.refuter` object.

    One shape for all five outcomes (ran, degraded, failed, skipped, and the
    absent case where nothing is recorded at all), so a reader — and Task 9's
    adoption command — never has to ask whether a key is missing because the
    pass did not get that far or because this outcome spells it differently.
    """
    return {
        "pass": REFUTER_PASS,
        "ran": ran,
        "status": status,
        "degraded": False,
        "verdicts_total": 0,
        "annotated": 0,
        "dropped": 0,
        "provider": None,
        "model": None,
        "effort": None,
        "note": "",
    }


def merge_refuter_pass(
    primary: Mapping[str, Any],
    refuter_result: Mapping[str, Any] | None,
    provenance: Mapping[str, Any],
    finder_findings_total: int | None = None,
    *,
    degraded: bool = False,
    partial_coverage: bool = False,
    notes: Sequence[str] = (),
) -> dict:
    """Annotate a copy of `primary` with one refuter pass's verdicts.

    CONTRACT
    --------
    `refuter_result` is the `REFUTER_CONTRACT` payload — `{"verdicts": [...]}`
    — that a pass which RAN and PARSED produced, or `None` for a pass that
    produced nothing usable. `None` is a legitimate argument here, unlike in
    `merge_extra_pass`: a refuter that could not answer is an absent
    annotation, and there is nothing for the caller to get wrong by passing it,
    because neither branch demotes anything. Anything else that is not a
    mapping raises.

    `provenance` is `{provider, model, effort}` for the attempt that answered
    (plus an optional `note`), exactly as `pipeline._provenance` builds it. The
    provider and model are copied onto every annotation as well as into the
    meta object: a verdict is only worth as much as the model behind it, and a
    reader of one finding must not have to go looking for that elsewhere.

    `finder_findings_total` is the FINDER SNAPSHOT's count and bounds which
    findings a verdict may reach. It defaults to `len(primary["findings"])`,
    which is correct only when nothing has been merged into the record yet —
    the pipeline always passes it explicitly, and passing it is how a verdict
    is kept off a finding some later pass appended. A value wider than the
    record is clamped rather than trusted. Omitting it is refused with a
    `ValueError` when `primary["extra_passes"]` is already non-empty: the
    unsafe default would otherwise silently widen to the merged record.

    Returns a NEW record. `findings` is a fresh list of fresh shallow copies
    (an annotation writes INTO a finding, so unlike `_merge` this cannot share
    the dicts with the caller), and `extra_passes` is a fresh dict. Everything
    else — counts, severity, rule ids, summary, and all three trust axes — is
    carried over untouched. This function cannot make a review untrustworthy
    and cannot change what the gate decides.
    """
    if not isinstance(primary, dict):
        raise TypeError("primary must be a dict")
    if refuter_result is not None and not isinstance(refuter_result, Mapping):
        raise TypeError(
            "refuter_result must be the REFUTER_CONTRACT payload as a mapping, "
            "or None for a pass that produced nothing; got %s"
            % type(refuter_result).__name__)

    # POSITIONAL, and deliberately NOT `_as_findings`: that helper DROPS a
    # non-mapping entry, which would renumber every finding after it and point
    # every later verdict at the wrong one. Nothing is dropped here — an entry
    # this function cannot annotate is carried through exactly as it arrived,
    # holding its index open. ("Annotation only" also means the findings list
    # comes out the same length it went in.)
    #
    # A finder's parsed findings are untrusted model output that reaches this
    # function verbatim — the same class of problem `_finding_lines` guards
    # against when it collapses titles and indents detail lines so a finding
    # cannot forge a `[9]` entry or close the findings fence (passes.py:604 in
    # `_finding_lines`'s docstring). Here the forgery is a `refuter` key: a
    # finder could ship one on its own finding to fabricate a verdict and its
    # provenance before this pass ever runs. Stripping any incoming `refuter`
    # key on copy — before this function writes its own — is what keeps
    # `_annotated` the ONLY place a `refuter` annotation is written, and what
    # keeps this merge's own counters (`annotated`, `dropped`, `note`) the
    # complete truth about what happened. `_strip_finder_refuter_keys` does
    # the same thing for `skipped_refuter_pass`, which has no verdicts of its
    # own to write but must not let a forged one ride through unrun.
    raw_findings = primary.get("findings")
    findings = _strip_finder_refuter_keys(
        raw_findings if isinstance(raw_findings, list) else [])
    if finder_findings_total is None:
        # Defaulting to `len(findings)` is only correct when NOTHING has been
        # merged into `primary` yet — the finder snapshot and the record are
        # then the same thing. Once a security/skeptic merge has appended its
        # own findings, that default silently widens to include them, and a
        # finding the finder never produced becomes eligible for a refuter
        # annotation: exactly the misattribution the finder-snapshot rule
        # exists to prevent. The pipeline's one caller always passes this
        # argument explicitly; refusing the unsafe default here makes the
        # omission unreachable instead of merely unused.
        extra_passes = primary.get("extra_passes")
        if isinstance(extra_passes, dict) and extra_passes:
            raise ValueError(
                "finder_findings_total is required once primary['extra_passes']"
                " is non-empty: defaulting to len(findings) here would widen "
                "the finder snapshot to the merged record and let a "
                "security/skeptic finding receive a refuter annotation the "
                "finder never earned")
        limit = len(findings)
    elif isinstance(finder_findings_total, bool) or not isinstance(
            finder_findings_total, int):
        raise TypeError(
            "finder_findings_total must be an int or None, got %r"
            % (finder_findings_total,))
    else:
        limit = max(0, min(finder_findings_total, len(findings)))

    prov = dict(provenance or {})
    prov_note = str(prov.pop("note", "") or "").strip()
    provider = prov.get("provider")
    model = prov.get("model")

    verdicts: list = []
    out_of_range: list[int] = []
    duplicates: list[int] = []
    malformed = 0
    annotated = 0
    seen: set[int] = set()
    own_notes: list[str] = []

    if refuter_result is not None:
        raw = refuter_result.get("verdicts")
        if isinstance(raw, list):
            verdicts = raw
        else:
            own_notes.append("the refuter response carried no verdicts list")

    for v in verdicts:
        if not isinstance(v, Mapping):
            malformed += 1
            continue
        index = v.get("index")
        # `type(...) is int`, not `isinstance`: `bool` subclasses `int`, so
        # `{"index": true}` would otherwise annotate finding number 1 with a
        # verdict about something else entirely.
        if type(index) is not int:
            malformed += 1
            continue
        if not 0 <= index < limit:
            out_of_range.append(index)
            continue
        if not isinstance(findings[index], dict):
            # An artifact whose findings are not all objects is rejected by
            # `triage.load_valid_artifact` long before the gate reads it; there
            # is nothing here to write an annotation into either way.
            malformed += 1
            continue
        if index in seen:
            duplicates.append(index)
            continue
        verdict = v.get("verdict")
        if not isinstance(verdict, str) or verdict not in REFUTER_VERDICTS:
            malformed += 1
            continue
        reasoning = v.get("reasoning")
        if not isinstance(reasoning, str):
            malformed += 1
            continue
        seen.add(index)
        annotation = {"verdict": verdict, "reasoning": reasoning,
                      "provider": provider, "model": model}
        # `collapse_ws`, the helper `triage.validate_reason` measures its floor
        # with -- NOT `norm`, which lowercases and can lengthen a string past a
        # floor it should not clear. The annotation is KEPT either way: the
        # human still gets to read it, and it is adoption that is refused.
        if len(collapse_ws(reasoning)) < MIN_REASON_CHARS:
            annotation["thin_reasoning"] = True
        findings[index][REFUTER_PASS] = annotation
        annotated += 1

    if out_of_range:
        own_notes.append(
            "dropped %d verdict(s) whose index is outside the finder's "
            "0..%d range: %s" % (len(out_of_range), limit - 1,
                                 _listed(out_of_range)))
    if duplicates:
        own_notes.append(
            "dropped %d duplicate verdict(s) (the first verdict for an index "
            "wins): %s" % (len(duplicates), _listed(duplicates)))
    if malformed:
        own_notes.append("dropped %d malformed verdict(s)" % malformed)

    status = ("failed" if refuter_result is None
              else "degraded" if degraded else "ran")
    meta = _refuter_meta(status, refuter_result is not None)
    meta["degraded"] = bool(degraded)
    meta["verdicts_total"] = len(verdicts)
    meta["annotated"] = annotated
    meta["dropped"] = len(out_of_range) + len(duplicates) + malformed
    if refuter_result is None:
        # The same word `merge_failed_extra_pass` uses, and deliberately NOT
        # the demotion that goes with it there.
        meta["failed"] = True
    if partial_coverage:
        meta["partial_coverage"] = True
    meta.update(prov)
    meta["note"] = "; ".join(
        n for n in ([prov_note] + list(notes) + own_notes) if n)

    return _annotated(primary, findings, meta)


def skipped_refuter_pass(primary: Mapping[str, Any], note: str) -> dict:
    """Record that an eligible refuter pass was skipped, and why.

    For the one skip worth recording — a review that earned a refuter and had
    none configured. A demand for a reason, like `merge_failed_extra_pass`'s,
    but nothing else about it is the same: this changes no finding's CONTENT,
    no count and no trust axis.

    It does, however, still run every finding through
    `_strip_finder_refuter_keys`: no refuter pass is scheduled here at all,
    so without this a finder that shipped its own forged `refuter` key on a
    finding would have that key ride straight into the stored record,
    misattributed to a provider that never ran, with `extra_passes.refuter`
    (correctly) reporting `skipped`. That gap is exactly what a bare
    `dict(primary)` would reproduce, since a skip has no verdicts of its own
    to overwrite it with.
    """
    reason = str(note or "").strip()
    if not reason:
        raise ValueError(
            "skipped_refuter_pass() requires a non-empty note: a record that "
            "mentions the refuter without saying why it did not run is worse "
            "than one that never mentions it")
    raw_findings = primary.get("findings")
    findings = (_strip_finder_refuter_keys(raw_findings)
                if isinstance(raw_findings, list) else None)
    meta = _refuter_meta("skipped", False)
    meta["skipped"] = True
    meta["note"] = reason
    return _annotated(primary, findings, meta)


def _strip_finder_refuter_keys(findings: list) -> list:
    """Copy `findings`, dropping any `refuter` key already present on one.

    A finder's parsed findings are untrusted model output that reaches
    `merge_refuter_pass` and `skipped_refuter_pass` verbatim — the same class
    of problem `_finding_lines` guards against when it collapses titles and
    indents detail lines so a finding cannot forge a `[9]` entry or close the
    findings fence (see that function's docstring). Here the forgery is a
    `refuter` key: a finder could ship one on its own finding to fabricate a
    verdict and its provenance before any refuter pass ever runs, or even
    when none is configured at all. Stripping it on every copy — before
    `merge_refuter_pass` writes its own, and even when `skipped_refuter_pass`
    writes none — is what keeps `_annotated` the ONLY place a `refuter`
    annotation is written, and what keeps this module's own accounting
    (`annotated`, `dropped`, `note`, or simply `skipped`) the complete truth
    about what happened.
    """
    return [
        ({k: v for k, v in f.items() if k != REFUTER_PASS}
         if isinstance(f, dict) else f)
        for f in findings]


def _annotated(primary: Mapping[str, Any], findings: list[dict] | None,
               meta: dict) -> dict:
    """Copy `primary` with new `findings` (if any) and the refuter's meta.

    The one place the refuter writes to a record, so the "annotation only"
    property is enforceable by reading a single function: it assigns exactly
    two keys, and neither is a count, a severity, a summary or a trust axis.
    """
    out = dict(primary)
    if findings is not None:
        out["findings"] = findings
    extras = out.get("extra_passes")
    extras = dict(extras) if isinstance(extras, dict) else {}
    extras[REFUTER_PASS] = meta
    out["extra_passes"] = extras
    return out
