"""The review loop, once, with no transport attached.

Every function here is one of the CLI's own subcommands with the terminal taken
out of it: no `print`, no `argparse`, no `sys.exit`, and no `Store` of its own.
Each returns `(status, text)` — the exit code the CLI would exit with, and the
text the CLI would put on stdout — so the two transports that call them are
mechanically identical surfaces over one implementation:

  * `cli.py` parses argv, opens the Store, calls the service, writes `text`, and
    returns `status` as the process's exit code;
  * `mcpserver.py`'s tools open a Store per call, call the SAME service, and put
    `text` in the tool result with `isError = status != 0`.

Three rules make that identity real rather than aspirational, and each one is
pinned by a test:

1. **The caller owns the Store.** Every store-backed service takes it as the
   FIRST parameter. Connection lifetime is a transport question — a one-shot CLI
   invocation owns one for the length of a command, while the MCP server must
   open one per call because sqlite connections are bound to the thread that
   created them and the review tool answers from another thread. A service that
   opened its own would decide that for both of them, wrongly for one.
2. **Nothing here prints.** Not even a diagnostic: `skodun mcp`'s stdout is a
   JSON-RPC stream that another thread may be mid-write on, so a stray line
   there desynchronises the client's parser for the rest of the session. A
   process-global `redirect_stdout` is forbidden for the same reason. Anything a
   service has to say comes back in `text`.
3. **Refusal strings live HERE, once.** `skodun triage --adopt-refuter` on a
   `confirmed` verdict must refuse with the same words through both surfaces, or
   an agent and a human reading each other's transcripts are looking at two
   different products. The validators themselves stay in `triage.py`; this module
   only decides which of their outcomes maps to which code.

`svc_surface` is the one three-valued shape (`status, text, pending_acks`) and its
contract is spelled on the function.

One rule cuts across every guard here: **`KeyboardInterrupt` is re-raised, never
absorbed.** The `except BaseException` blocks exist so that an unreadable store,
a git that will not run or a ledger that stopped answering comes back as a
status and a sentence instead of a traceback — none of them is about a user who
pressed Ctrl-C. Absorbing it costs the CLI its 130 (the shell's 128 + SIGINT,
which no code in any of these contracts can say) and, in `svc_gate`, wrote a
`gate_events` row asserting a decision about a change nothing ever examined.
An ORDINARY exception still produces the conservative code it always did.
`test_no_service_guard_turns_a_ctrl_c_into_a_synthetic_failure` sweeps them.
"""

from pathlib import Path

# --- the refusal strings both surfaces share --------------------------------
#
# Module constants rather than literals at their one call site, because they have
# TWO call sites: the CLI checks the argparse-shaped misuse before it opens a
# store (a mixture of `--list` and `--reopen` is a question about argv, not about
# the ledger), and the service checks the same absence for a caller that has no
# argparse at all. One definition, so the two can never drift into "the CLI says
# X and the tool says Y about the same mistake".

TRIAGE_DISMISS_USAGE = (
    "skodun triage: usage: skodun triage <review-id> <finding-index> "
    "\"<reason>\"  |  skodun triage --list <review-id>")

TRIAGE_REOPEN_USAGE = (
    "skodun triage: usage: skodun triage --reopen <review-id> "
    "<finding-index> \"<reason>\"  (one finding at a time, and the "
    "reason is required)")

TRIAGE_ADOPT_USAGE = (
    "skodun triage: usage: skodun triage --adopt-refuter "
    "<review-id> <finding-index>  (one finding at a time, on "
    "purpose)")

TRIAGE_DEFER_USAGE = (
    "skodun triage: usage: skodun triage --defer <review-id> "
    "<finding-index> <tracking-ref> \"<reason>\"  (the tracking reference "
    "is mandatory: an unfiled deferral and an ignored finding are the same "
    "artifact)")

#: What a cancelled foreground review reports. Status 4 in both surfaces: the
#: content has no trustworthy review covering it, which is exactly what 4 says,
#: and a cancelled run must never be able to report anything gentler.
REVIEW_CANCELLED_REASON = "review cancelled"

#: Durable demotion reason written when cancel-by-id must finish a dead or
#: unsignallable `running` row. Mapped to report state `cancelled` by
#: `report_state` (substring "cancel"), without introducing a new store status
#: that could drift gate/trust.
REVIEW_CANCEL_DURABLE_REASON = "cancelled by review-cancel"

#: Report vocabulary for `review-status` / `review_status` (epic S1). Durable
#: store statuses stay on the existing enum (`running`/`failed`/`clean`/…);
#: this is the READ MODEL only, so a cancelled round still demotes to
#: `failed`+cancel reason for gate/trust while agents observe `cancelled`.
REPORT_STATES = ("queued", "running", "cancelled", "failed", "clean", "findings")


# --- gate -------------------------------------------------------------------


def svc_gate(store, repo) -> tuple[int, str]:
    """Decide whether a trustworthy review covers this change. `(code, message)`.

    Every failure is a 2, for the reason `gate.run_gate` maps everything to 2:
    the alternative is the interpreter's own exit code of 1, and 1 is the one
    value in the contract that means "findings remain open". Setup failures — an
    unparseable config, a directory that is not a worktree — happen strictly
    before any review is consulted, so reporting them as findings would be a lie
    in the dangerous direction.

    The imports are OUTSIDE the guard below, deliberately: an unimportable
    `gate.py` is a broken installation rather than a decision this function can
    make, and the CLI's contract for that case (`main`'s general handler, exit 2)
    depends on the exception reaching it.

    `KeyboardInterrupt` is re-raised, exactly as `svc_review` re-raises it: 130
    is what the shell expects for a Ctrl-C and none of the codes above can say
    it, and reporting a cancelled run as `FAIL(2) could not run the gate` would
    also write a `gate_events` row claiming a decision was taken about a change
    nothing ever looked at. `SystemExit` is deliberately NOT re-raised with it —
    it carries an exit code of its own, and letting an arbitrary one (0
    included) out of a fail-closed gate is the one direction this function may
    never fail in.
    """
    from .cli import _record_setup_failure, _repo_root
    from .config import load_config
    from .gate import run_gate

    repo = Path(repo)
    try:
        # The ROOT, not the argument: the config and the diff identity must be
        # resolved against the same directory or the gate decides about a
        # different change depending on the cwd. See `cli._repo_root`.
        root = _repo_root(repo)
        cfg = load_config(root)
        result = run_gate(store, root, cfg)  # records its own event; never raises
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        note = f"SKODUN GATE: FAIL(2) could not run the gate: {e!r}"
        _record_setup_failure(store, repo, note)
        return 2, note
    return result.code, result.message


# --- review -----------------------------------------------------------------


def svc_review(store, repo, *, progress_sink=None, cancel=None,
               reviewer=None, client_family=None) -> tuple[int, str]:
    """Run one foreground review. `(code, banner)`. Exit codes, and why:

      0  trustworthy and clean            3  gave up waiting for the lock
      1  trustworthy, findings open       4  no trustworthy review exists
      2  preflight refusal (nothing ran)

    `reviewer` is the NAME of a configured `[[reviewers]]` entry to head this
    run's chain — `skodun review --reviewer <name>` and the MCP `review` tool's
    `reviewer` argument are the same request arriving through two doors, and
    this function is the one implementation both reach. `None` means the config
    decides; the empty string does NOT (it is a request for an entry called
    `""`, and it is refused like any other name nobody configured), so the test
    below is `is not None` and never truthiness.

    The refusals themselves live in `run_review`'s preflight rather than here:
    they are decisions about the loaded CONFIG, which this function has not read
    at the point a caller hands it a name, and they arrive as the
    `PreflightRefused` the 2 branch below already renders. That is what makes
    them word-for-word identical across the two surfaces without either surface
    — or this module — owning the words.

    `client_family` is the caller's own model family (`skodun review
    --client-family`, the MCP `review` tool's `client_family`), and it is
    likewise one request arriving through two doors. It matters only when the
    config enables auto-routing and nobody pinned a reviewer, where it buys a
    soft preference for a DIFFERENT family — a second opinion when one is going
    spare. `None` falls through to `SKODUN_CLIENT_FAMILY` and then to nothing,
    and "nothing" is a perfectly good answer: the router then scores on
    availability alone. Passed through unvalidated for the same reason
    `reviewer` is: `routing.normalize_family` owns what counts as a family, and
    that vocabulary is deliberately OPEN -- an unlisted provider's family is its
    own id, so a new adapter's family is a legitimate value and there is no
    closed set to check against. A family this install has no finder from cannot
    misroute (the bonus lands on every candidate at once, leaving the order
    untouched) but it also cannot do anything, so the router names the families
    it actually has rather than leave an operator believing cross-model review
    is on. Never a review it refuses to run.

    The banner is DERIVED here, from the record `run_review` returns, through
    `trust.banner` — the one definition of it. The pipeline itself prints
    nothing: its stdout would be an MCP transport's protocol stream.

    Every path that never reached a record returns a `banner_failure` line
    instead, so the CLI's "the last line of stdout is always a verdict"
    invariant holds through this function on every path, including the ones that
    got nowhere.

    `KeyboardInterrupt` is re-raised past every guard: the CLI maps it to 130
    (the shell's own 128 + SIGINT), which none of the codes above can say.
    `cancel` is a `threading.Event`; a set token aborts the review at the next
    boundary and reports 4, never a gentler code.
    """
    # Outside the guard below on purpose: it is what RENDERS the failure banner,
    # so a failure to import it is the one import failure no banner can report.
    from .trust import banner, banner_failure

    try:
        # Inside the guard: an import error here — a partial install, a syntax
        # error introduced in `pipeline.py`, a missing stdlib module in a
        # stripped environment — must not escape without the verdict line the
        # contract promises. 2, not 4: nothing ran, so this is a refusal, not a
        # review that came back badly.
        from .config import load_config
        from .gitio import GitError
        from .pipeline import (LockTimeout, PersistenceFailed, PreflightRefused,
                               ReviewCancelled, run_review)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, banner_failure(
            f"could not load the review pipeline: {e!r}; no review ran")

    from .cli import _repo_root

    repo = Path(repo)
    try:
        # Before the config, and for the same reason the gate does it: the config
        # has to be read from the same directory the diff identity is computed
        # against. A repo that is not inside a worktree at all raises here, which
        # is a preflight refusal — nothing ran.
        root = _repo_root(repo)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, banner_failure(f"{e}; no review ran")
    try:
        cfg = load_config(root)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # A config that will not load is a refusal before anything ran, not a
        # review that came back badly: 2, the preflight code.
        return 2, banner_failure(f"could not load the config: {e!r}")

    try:
        rec = run_review(root, cfg, store, progress_sink=progress_sink,
                         cancel=cancel, reviewer=reviewer,
                         client_family=client_family)
    except PreflightRefused as e:
        return 2, banner_failure(str(e))
    except LockTimeout as e:
        return 3, banner_failure(str(e))
    except PersistenceFailed:
        return 4, banner_failure("no review was recorded")
    except GitError as e:
        # A directory that is not a git checkout at all, a git that will not run,
        # a repo with no HEAD: every git call the pipeline makes happens before
        # the reviewer is launched, so this is a preflight failure — nothing ran
        # — and preflight refusals are 2, not "the review failed".
        return 2, banner_failure(f"{e}; no review ran")
    except ReviewCancelled:
        # BEFORE the general `BaseException` below, and it has to be: a
        # cancellation is not "the review failed" with a stack trace worth
        # quoting, and `run_review`'s own `finally` has already downgraded
        # whatever record existed and released the lock. 4, because nothing
        # trustworthy can cover content whose review was cut short — including
        # the pre-persistence case, where there is no record at all.
        return 4, banner_failure(REVIEW_CANCELLED_REASON)
    except KeyboardInterrupt:
        # `run_review`'s own `finally` has already downgraded the `running`
        # record and released the foreground lock by the time this reaches here;
        # this guard only has to let it keep going rather than let the
        # `except BaseException` below turn it into a lying "the review failed".
        raise
    except BaseException as e:
        # Anything else: the review did not complete, so it certifies nothing.
        return 4, banner_failure(f"the review failed: {e!r}")

    # The verdict, rendered from the PERSISTED record and nothing recomputed.
    text = banner(rec)
    if rec.get("trustworthy") is not True:
        return 4, text
    try:
        total = int(rec.get("findings_total") or 0)
    except (TypeError, ValueError):
        total = 1     # an uncountable findings list is not a clean review
    return (1 if total > 0 else 0), text


# --- log --------------------------------------------------------------------


def svc_log(store, branch, limit, repo=None) -> tuple[int, str]:
    """Recent reviews, newest first, one line each. `(code, text)`.

    `2` for a non-positive `-n` and for a store that cannot be read; `0`
    otherwise, including for a store with no rows at all (an empty listing is an
    answer, and `text` is then `""`).

    `repo` narrows `branch` and is IGNORED without one -- `list_reviews`'s own
    contract, and what the `--repo` flag's help text says. It is optional here
    and required on `svc_surface` for one reason: a listing that crossed
    repositories shows a reader too much, while a `surface` that did would
    deliver and permanently acknowledge rounds that were never theirs.

    Each line may append R3 round context (`round: review N of M ...`) when
    the review can be placed on its branch; R3 is annotation only.
    """
    from .roundctx import round_context_for_review
    from .trust import coerce_count, one_line

    try:
        rows_wanted = int(limit)
    except (TypeError, ValueError):
        return 2, f"skodun log: -n must be a positive row count, got {limit!r}"
    if rows_wanted < 1:
        # `-n` becomes SQLite's LIMIT, where a NEGATIVE value means "no limit" —
        # so `log -n -1` would dump the whole store while reading like a request
        # for fewer rows than the default. Below 1 there is no row count to ask
        # for, so this is a usage error rather than something to clamp silently.
        return 2, (f"skodun log: -n must be a positive row count, got "
                   f"{rows_wanted}")
    try:
        rows = store.list_reviews(branch, rows_wanted, repo)
    except KeyboardInterrupt:
        raise           # a cancelled listing is 130's, not "the store is broken"
    except BaseException as e:
        return 2, f"skodun log: could not read the store: {e!r}"

    lines = []
    for rec in rows:
        trustworthy = rec.get("trustworthy") is True
        sev = rec.get("severity") if isinstance(rec.get("severity"), dict) else {}
        files = rec.get("files_changed")
        nfiles = len(files) if isinstance(files, list) else 0
        # A summary carrying a stray newline must not be able to fake a second
        # row in what is meant to be a one-line-per-review listing. Same
        # definition the banner uses — see `trust.one_line`.
        summary = one_line(rec.get("summary") or "")
        mark = "!" if not trustworthy else " "
        # Counts read by THE project's single count rule, so `log` and `banner`
        # can never disagree about the same stored row.
        line = (
            f"{mark}{rec.get('reviewed_at')} | {rec.get('branch')} | {nfiles} | "
            f"{coerce_count(sev.get('high'))}-{coerce_count(sev.get('medium'))}"
            f"-{coerce_count(sev.get('low'))} | "
            f"{rec.get('status')} | {summary}")
        ctx = round_context_for_review(store, rec)
        if ctx is not None:
            line = f"{line} | {ctx.line()}"
        lines.append(line)
    return 0, "\n".join(lines)


# --- surface ----------------------------------------------------------------


def surface_no_rounds_note(branch) -> str:
    """What a surface pass with nothing to report says. ONE definition.

    The CLI writes it to STDERR with exit 0 — its stdout is a payload a hook
    feeds to an agent verbatim, and an empty report injected at every session
    start is noise — while the MCP tool returns it as the tool's text, because a
    tool result with no text at all tells the agent nothing.
    """
    return (f"skodun surface: no undelivered background review rounds on branch "
            f"{branch}")


def resolve_surface_branch(branch, repo=".") -> tuple[str, str]:
    """`(branch, "")`, or `("", why-not)`. Never turns a git failure into one.

    A detached HEAD answers `HEAD` here, which matches no round and reports
    nothing — correct: rounds are keyed to a branch, and a detached checkout is
    not on one. Naming the branch explicitly is how a caller asks anyway.

    The one exception it does let through is `KeyboardInterrupt`, for the reason
    every guard in this module lets it through: a user who hit Ctrl-C during the
    `git rev-parse` did not discover that the branch is unknowable, and the CLI
    owes them 130 rather than a refusal quoting their own interrupt back at them.
    """
    if branch:
        return str(branch), ""
    try:
        from . import gitio
        branch = gitio.current_branch(Path(repo))
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return "", (f"skodun surface: could not work out which branch to report "
                    f"on ({e!r}); pass --branch")
    if not branch:
        return "", ("skodun surface: could not work out which branch to report "
                    "on; pass --branch")
    return branch, ""


def svc_surface(store, branch, repo, fmt="text",
                include_delivered=False) -> tuple[int, str, list]:
    """One delivery pass. `(status, text, pending_acks)`, and the three shapes it
    returns are distinguishable on purpose:

      `(0, payload, ids)`   there is a report. The transport WRITES it, and only
                            if that write (and its flush) landed does it
                            acknowledge `ids` with its own channel.
      `(0, "", [])`         nothing to report. The transport says so its own way
                            (`surface_no_rounds_note`), and acknowledges nothing.
      `(2, diagnostic, [])` the pass could not run. `text` is a diagnostic, not a
                            payload — the CLI puts it on stderr.

    QUIET rounds (trustworthy, zero findings, nothing renderable) are
    acknowledged INSIDE `delivery.surface`, immediately and under the `quiet`
    channel: they will never have anything to say, so nothing deliverable can be
    lost by marking them now. `pending_acks` therefore holds only the
    content-bearing rounds — the ones whose delivery depends on a write this
    module cannot perform and must not claim.

    `repo` is POSITIONAL AND REQUIRED, unlike `svc_log`'s: it is the git common
    dir the rows are scoped by, every transport must resolve it for itself, and
    a `surface` that guessed would deliver AND permanently acknowledge another
    repository's rounds. There is no safe default for that.
    """
    from . import delivery

    try:
        status, text, pending = delivery.surface(store, branch, repo, fmt,
                                                 bool(include_delivered))
    except KeyboardInterrupt:
        raise           # 130's, not "the ledger is unreadable"
    except BaseException as e:
        return 2, f"skodun surface: could not read the delivery ledger: {e!r}", []
    return int(status), text, list(pending)


# --- triage -----------------------------------------------------------------


def _load_review(store, review_id) -> tuple[dict | None, tuple[int, str] | None]:
    """`(review, None)` or `(None, (code, message))`. The triage prologue.

    Shared by all four triage services because the ORDER is load-bearing: a
    review id nobody recognises is reported as "no such review" BEFORE any
    argument shape is judged, so `skodun triage typo-id` says the id is wrong
    rather than lecturing about the argument list.
    """
    from .triage import ArtifactError, load_valid_artifact

    review = store.get_review(review_id)
    if review is None:
        return None, (2, f"skodun triage: no such review: {review_id!r}")
    try:
        return load_valid_artifact(review), None
    except ArtifactError as e:
        return None, (2, f"skodun triage: invalid review artifact: {e}")


def svc_triage_list(store, review_id) -> tuple[int, str]:
    """Every finding in a review, with its EFFECTIVE triage state. `(0, text)`.

    The state comes from `store.triage_state` — the same definition the gate
    reads through `triage_for`, which is a filter over exactly this map. A
    second, independent "latest decision" query here could print DISMISSED for a
    finding the gate still counts as open.

    R2/R3 annotations are prepended (round context + churn summary) and each
    finding line may carry a `churn:yes|no|?` marker. Annotation only: the
    gate still reads the raw findings through `triage_for`.
    """
    from .roundctx import (churn_for_review, churn_marker,
                           round_context_for_review)
    from .textnorm import finding_key
    from .triage import (refuter_annotation, refuter_line, refuter_pass_ran,
                         shown_field, status_token)

    review, refusal = _load_review(store, review_id)
    if refusal is not None:
        return refusal

    states = store.triage_state(review["branch"], review["base_sha"])
    # An annotation is shown only on a record where a refuter pass actually ran.
    # On a record where none did, a `refuter` key is something the FINDER wrote
    # about its own finding (see `triage.refuter_pass_ran`), and printing it as
    # `refuter(<provider>/<model>)` would be this program vouching for a second
    # opinion that was never sought.
    annotated = refuter_pass_ran(review)

    # R3 then R2 headers — fail closed inside the helpers; never raise out.
    lines: list[str] = []
    try:
        ctx = round_context_for_review(store, review)
        if ctx is not None:
            lines.append(ctx.line())
    except Exception:  # noqa: BLE001 - listing must still show findings
        pass
    try:
        findings, churn, _prev = churn_for_review(store, review)
        lines.append(churn.line())
    except Exception:  # noqa: BLE001
        findings = list(review["findings"])
        lines.append("churn: unavailable")

    for i, f in enumerate(findings):
        fkey = finding_key(f.get("file", ""), f.get("title", ""))
        # `OPEN`, `DISMISSED <when>`, or `REOPENED <when>, dismissed <when>` —
        # one definition, in `triage.status_token`, which bounds and strips the
        # stored timestamps the same way every other untrusted field is bounded.
        status = status_token(states.get(fkey))
        # EVERY field on this line is finder-authored, untrusted model text
        # reaching a terminal (or an agent's context) the same way a refuter's
        # `reasoning` does — `severity`, `file` and `line` are read straight off
        # the parsed payload, exactly like `title`. `shown_field` strips the same
        # control/ANSI exposure and bounds the same way, so no field can forge an
        # extra row or rewrite this line's own status the instant it is printed.
        # Only `[{i}]` and `({status})` are ours. Churn marker is ours too.
        marker = churn_marker(f)
        marker_s = f" ({marker})" if marker else ""
        lines.append(f"[{i}] {shown_field(f.get('severity'))} "
                     f"{shown_field(f.get('file'))}:{shown_field(f.get('line'))} "
                     f"{shown_field(f.get('title'))} ({status}){marker_s}")
        # One extra line for an annotated finding, and never more than one:
        # `refuter_line` flattens and bounds every field it prints, so arbitrary
        # model text cannot forge a second `[n]` row. An annotation is shown
        # whatever its verdict says — the listing reports what the refuter
        # answered; only `--adopt-refuter` decides what may be acted on.
        # Refuter annotations still read from the ORIGINAL finding dict so a
        # shallow churn copy that dropped keys cannot hide them.
        raw = review["findings"][i] if i < len(review["findings"]) else f
        annotation = refuter_annotation(raw) if annotated else None
        if annotation is not None:
            lines.append(refuter_line(annotation))
    return 0, "\n".join(lines)


def svc_triage_dismiss(store, review_id, index, reason) -> tuple[int, str]:
    """Dismiss one finding with an audited reason. `(0, text)` or `(2, why-not)`.

    The PLAIN dismissal path keeps its shipped exit contract, in which a rejected
    reason is a 2 rather than the 1 `--adopt-refuter` and `--reopen` use. That is
    deliberately left alone: it is a shipped contract that pre-push hooks and
    humans already read.

    What it does NOT keep is being the one triage service whose store I/O could
    escape. It guarded the validation errors and nothing else, so a store that
    stopped accepting writes came out of here as a traceback and broke the
    `(status, text)` contract the CLI and the MCP transport are both built on.
    Its two siblings already had the guard; this is the same one.
    """
    import time

    from .store import _TS_FORMAT
    from .triage import ArtifactError, TriageError, dismiss

    review, refusal = _load_review(store, review_id)
    if refusal is not None:
        return refusal
    if index is None or reason is None:
        return 2, TRIAGE_DISMISS_USAGE

    try:
        dismiss(store, review, index, reason,
                now=time.strftime(_TS_FORMAT, time.gmtime()))
    except (TriageError, ArtifactError) as e:
        return 2, f"skodun triage: rejected: {e}"
    except KeyboardInterrupt:
        raise           # 130's; the guard below is for a store that broke
    except BaseException as e:
        # A store that stopped accepting writes is not a refusal about the
        # reason — nothing was decided and nothing was recorded.
        return 2, f"skodun triage: could not record the dismissal: {e!r}"
    return 0, (f"skodun triage: dismissed finding {index} on review "
               f"{review_id}")


def svc_adopt_refuter(store, review_id, index) -> tuple[int, str]:
    """Dismiss one finding by adopting its refuter annotation. Exit contract:

      0  the decision was recorded
      1  REFUSED — the finding is right there and the decision was declined (a
         wrong verdict, thin reasoning, a reasoning that fails the audit floor,
         an annotation that cannot say who answered)
      2  NOT FOUND — no such review, no such finding, an artifact that does not
         validate, or plain misuse

    A refusal is a fact about the ledger and is worth acting on; a 2 means this
    never got as far as having an opinion. Collapsing them would make "your
    refuter said `confirmed`" indistinguishable from "you typed the wrong id".
    """
    import time

    from .store import _TS_FORMAT
    from .triage import (ArtifactError, FindingNotFound, TriageError,
                         adopt_refuter, refuter_same_provider_as_finder)

    if index is None:
        # Before the load, matching the CLI's own order: a call with no finding
        # index is misuse whatever the review id turns out to be, and the two
        # surfaces must refuse the same mistake with the same words.
        return 2, TRIAGE_ADOPT_USAGE
    review, refusal = _load_review(store, review_id)
    if refusal is not None:
        return refusal

    try:
        adopt_refuter(store, review, index,
                      now=time.strftime(_TS_FORMAT, time.gmtime()))
    except (FindingNotFound, ArtifactError) as e:
        return 2, f"skodun triage: {e}"
    except TriageError as e:
        return 1, f"skodun triage: refused: {e}"
    except KeyboardInterrupt:
        raise           # 130's; the guard below is for a store that broke
    except BaseException as e:
        # A store that stopped accepting writes is not a refusal about the
        # annotation — nothing was decided and nothing was recorded.
        return 2, f"skodun triage: could not record the dismissal: {e!r}"

    lines = []
    # The refuter exists so that a DIFFERENT provider examines the findings; a
    # model asked to check its own work is agreeable about it. A config may still
    # put the refuter on the finder's provider — the operator's call, and better
    # than no re-examination — and the pass records that it happened. This is the
    # one moment where that fact has consequences, so it is said out loud here
    # rather than left in the artifact for nobody to read. A WARNING and not a
    # refusal: adoption is an explicit human act, and the human is the authority
    # this path exists to consult.
    if refuter_same_provider_as_finder(review):
        lines.append("skodun triage: WARNING the refuter answered from the same "
                     "provider as the finder, so this verdict is a model "
                     "re-examining its own work")
    lines.append(f"skodun triage: adopted the refuter's dismissal of finding "
                 f"{index} on review {review_id}")
    return 0, "\n".join(lines)


def svc_triage_reopen(store, review_id, index, reason) -> tuple[int, str]:
    """Reopen ONE previously dismissed finding, with an audited reason.

    `--adopt-refuter`'s exit contract (see `svc_adopt_refuter`): 0 recorded, 1
    refused (an unauditable reason, or a finding that is not dismissed and so has
    nothing to overturn), 2 not found.

    It takes a reason of its own — and the same reason floor a dismissal clears —
    because it moves the gate from 0 back to 1, and nothing may do that silently.
    Append-only: the dismissal it overturns stays in the ledger with its reason.
    """
    import time

    from .store import _TS_FORMAT
    from .triage import ArtifactError, FindingNotFound, TriageError, reopen

    if index is None or reason is None:
        # Both are mandatory: one finding at a time, and never without a stated
        # reason for overturning a dismissal somebody else may have recorded.
        # Before the load, matching the CLI's own order (see `svc_adopt_refuter`).
        return 2, TRIAGE_REOPEN_USAGE
    review, refusal = _load_review(store, review_id)
    if refusal is not None:
        return refusal

    try:
        reopen(store, review, index, reason,
               now=time.strftime(_TS_FORMAT, time.gmtime()))
    except (FindingNotFound, ArtifactError) as e:
        # The finding or the review does not exist: nothing was decided.
        return 2, f"skodun triage: {e}"
    except TriageError as e:
        # The finding exists and the reopen was declined — an unauditable
        # reason, or a finding that is not dismissed.
        return 1, f"skodun triage: refused: {e}"
    except KeyboardInterrupt:
        raise           # 130's; the guard below is for a store that broke
    except BaseException as e:
        # A store that stopped accepting writes is not a refusal about the
        # reason: nothing was decided and nothing was recorded.
        return 2, f"skodun triage: could not record the reopen: {e!r}"
    return 0, (f"skodun triage: reopened finding {index} on review "
               f"{review_id}; it counts as open again")


def svc_triage_defer(store, review_id, index, tracking_ref,
                     reason) -> tuple[int, str]:
    """Defer ONE finding to a FILED tracking reference, with an audited reason.

    `svc_triage_reopen`'s exit contract, exactly (0 recorded, 1 refused, 2 not
    found), because it is the same shape of decision: the finding is right there
    and either the ledger took the decision or it declined it.

    What makes this verb different from `svc_triage_dismiss` is what a 1 can
    mean here: a MISSING OR UNUSABLE TRACKING REFERENCE is refused on exactly
    the terms a placeholder reason is. A deferral clears the gate, so one that
    names nowhere the work is filed is an auto-dismissal with better manners --
    "an unfiled deferral and an ignored finding are the same artifact" is a
    mechanical fact only because this refusal exists.

    An ABSENT reference (`None`) is a 2 and the usage string, not a 1: that is a
    caller who has not made a deferral yet, which is misuse rather than a
    declined decision -- and it is the refusal both surfaces have to share, so
    it comes from `TRIAGE_DEFER_USAGE` and neither of them owns the words.
    """
    import time

    from .store import _TS_FORMAT
    from .triage import ArtifactError, FindingNotFound, TriageError, defer

    if index is None or reason is None or tracking_ref is None:
        # All three are mandatory: one finding at a time, never without a stated
        # reason, and never without the filing that distinguishes a deferral
        # from a dismissal. Before the load, matching the CLI's own order.
        return 2, TRIAGE_DEFER_USAGE
    review, refusal = _load_review(store, review_id)
    if refusal is not None:
        return refusal

    try:
        rec = defer(store, review, index, tracking_ref, reason,
                    now=time.strftime(_TS_FORMAT, time.gmtime()))
    except (FindingNotFound, ArtifactError) as e:
        # The finding or the review does not exist: nothing was decided.
        return 2, f"skodun triage: {e}"
    except TriageError as e:
        # The finding exists and the deferral was declined -- an unauditable
        # reason, or a reference nobody could look up.
        return 1, f"skodun triage: refused: {e}"
    except KeyboardInterrupt:
        raise           # 130's; the guard below is for a store that broke
    except BaseException as e:
        return 2, f"skodun triage: could not record the deferral: {e!r}"
    # The CANONICAL reference (`triage.defer` returns what it stored), never the
    # caller's raw string: what is echoed has to be what a later `skodun
    # deferrals` will print, or the two disagree about the same filing.
    return 0, (f"skodun triage: deferred finding {index} on review {review_id}, "
               f"filed as {rec['tracking_ref']}; it no longer blocks the gate")


def svc_deferrals(store, limit=50) -> tuple[int, str]:
    """Every finding still standing as DEFERRED, across every review. One line
    each, newest first. `(0, text)`, or `(2, why-not)`.

    THE ANTI-ROT SURFACE, and the reason it is its own command rather than a
    flag on an existing one. `log` lists REVIEWS, one row per stored review; a
    deferral is a FINDING inside one, and `log --deferred` would have to render
    a different row shape under the same command. `triage` takes a review id as
    a required positional and answers about that review -- but a deferral filed
    three branches ago is exactly the one that rots, and it is unreachable from
    any surface scoped to a review a human already has in mind. So: a listing
    with no scope at all, which is the only honest shape for the question "what
    has this project deferred, and where is it filed".

    `2` for a non-positive or non-integer `-n` and for a store that cannot be
    read; `0` otherwise, including for a store with no deferrals at all -- an
    empty listing is an answer, and `text` is then `""`.
    """
    from .triage import shown_field

    try:
        rows_wanted = int(limit)
    except (TypeError, ValueError):
        return 2, (f"skodun deferrals: -n must be a positive row count, got "
                   f"{limit!r}")
    if rows_wanted < 1:
        # `-n` becomes SQLite's LIMIT, where a NEGATIVE value means "no limit",
        # so `-n -1` would dump the whole ledger while reading like a request
        # for fewer rows. Exactly `svc_log`'s rule.
        return 2, (f"skodun deferrals: -n must be a positive row count, got "
                   f"{rows_wanted}")
    try:
        rows = store.open_deferrals(rows_wanted)
    except KeyboardInterrupt:
        raise           # a cancelled listing is 130's, not "the store is broken"
    except BaseException as e:
        return 2, f"skodun deferrals: could not read the ledger: {e!r}"

    # EVERY field below except the separators is finder-authored model text or a
    # stored string, reaching a terminal on a one-line-per-item listing -- the
    # same exposure `triage --list` has, so it goes through the same
    # `shown_field`: flattened, control characters stripped, length bounded. A
    # title carrying a raw newline must not be able to forge a second deferral.
    return 0, "\n".join(
        f"{shown_field(r.get('tracking_ref'))} | {shown_field(r.get('branch'))} | "
        f"{shown_field(r.get('file'))}:{shown_field(r.get('line'))} | "
        f"{shown_field(r.get('severity'))} {shown_field(r.get('title'))} | "
        f"deferred {shown_field(r.get('at'))} | review "
        f"{shown_field(r.get('review_id'))}"
        for r in rows)


# --- review status + cancel (epic S1) ---------------------------------------


def report_state(rec: dict) -> str:
    """Map a durable review record to the S1 status vocabulary.

    Durable cancel still lands as `status=failed` with a cancel reason (see
    `pipeline.cancellation_transform` / `store.mark_cancelled`); introducing a
    new store enum would risk gate/trust drift. This function is the one place
    that maps those rows to reportable `cancelled`.
    """
    status = rec.get("status")
    if status == "running":
        # Prepush reservation before the worker attached a pid: admitted but
        # not yet executing. FG rows always carry pid once persisted.
        if rec.get("pid") is None and rec.get("mode") == "prepush":
            return "queued"
        return "running"
    if _looks_cancelled(rec):
        return "cancelled"
    if status in ("failed", "degraded", "superseded"):
        return "failed"
    try:
        total = int(rec.get("findings_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > 0:
        return "findings"
    if status == "clean" or rec.get("parse_ok") is True:
        return "clean"
    return "failed"


def _looks_cancelled(rec: dict) -> bool:
    for key in ("failure_reason", "degraded_reason"):
        val = str(rec.get(key) or "").lower()
        if "cancel" in val:
            return True
    return False


def _status_field(label: str, val) -> str:
    """One ``key=value`` token; quote values that would break space-splitting."""
    import json

    if val is None:
        return f"{label}="
    if isinstance(val, bool):
        return f"{label}={str(val).lower()}"
    if isinstance(val, int) and not isinstance(val, bool):
        return f"{label}={val}"
    text = str(val)
    # Unquoted only when a single shell-like token; otherwise JSON-string form
    # so `branch=feature one` cannot inject a phantom field.
    if text and all(c.isalnum() or c in "._:@/+-" for c in text):
        return f"{label}={text}"
    return f"{label}={json.dumps(text)}"


def format_status_line(rec: dict, *, now: float | None = None) -> str:
    """One machine-readable status line for CLI and MCP.

    Always includes `id=` and `state=` (the S1 vocabulary). Age, provider,
    model, mode, and branch are appended when known — absence is omission, not
    a second guess. Values with whitespace or special characters are emitted
    as JSON strings so space-splitting consumers stay safe.
    """
    import time as _time

    from .pipeline import _epoch

    state = report_state(rec)
    parts = [_status_field("id", rec.get("id")),
             _status_field("state", state)]
    started = _epoch(rec.get("reviewed_at"))
    if started is not None:
        age = int(max(0.0, (now if now is not None else _time.time()) - started))
        parts.append(f"age={age}s")
    for key, label in (
            ("mode", "mode"),
            ("adapter", "provider"),
            ("model", "model"),
            ("branch", "branch"),
            ("pid", "pid"),
    ):
        val = rec.get(key)
        if val is not None and val != "":
            parts.append(_status_field(label, val))
    return " ".join(parts)


def _maybe_recover_stale(store) -> None:
    """Sweep aged `running` rows with the same rules prepush uses.

    Status and cancel are observation points that must not leave forever-
    `running` FG rows when no push is happening; `recover_stale` is the one
    janitor. Failures here are swallowed -- a broken sweep must not block a
    status read.
    """
    try:
        from .config import load_config
        from .pipeline import recover_stale
        # Config is host-global for budget defaults when no repo is in hand;
        # recover_stale prefers each row's own persisted worst_runtime_sec.
        recover_stale(store, load_config(Path(".")))
    except KeyboardInterrupt:
        raise
    except BaseException:
        pass


def svc_review_status(store, review_id=None, repo=None) -> tuple[int, str]:
    """Observe one review. `(0, line)` or `(2, why-not)`.

    `review_id` wins when both are given. Without an id, `repo` selects the
    current review for that repository (newest `running`, else newest terminal);
    without either, the host-wide current review. Not a gate: never computes
    trust over the worktree.

    Runs `recover_stale` first so an aged FG `running` row is reported as the
    terminal state the sweep produces rather than as forever-running.
    """
    _maybe_recover_stale(store)
    try:
        if review_id is not None and str(review_id).strip() != "":
            rid = str(review_id).strip()
            rec = store.get_review(rid)
            if rec is None:
                return 2, f"skodun review-status: no such review: {rid}"
        else:
            scope = None
            if repo is not None and str(repo).strip() != "":
                scope = str(repo).strip()
            rec = store.current_review(scope)
            if rec is None:
                if scope is not None:
                    return 2, (f"skodun review-status: no review for repo "
                               f"{scope}")
                return 2, "skodun review-status: no reviews in the store"
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, f"skodun review-status: could not read the store: {e!r}"
    return 0, format_status_line(rec)


def svc_review_cancel(store, review_id) -> tuple[int, str]:
    """Request cancellation of one in-flight review. `(0, text)` or `(2, why)`.

    Order of operations (each step is best-effort; together they cover FG, MCP
    long-running slot, and background workers):

    1. Refuse missing ids and already-terminal rows (same words on both
       surfaces).
    2. Set the in-process cancel token when this process holds it (MCP /
       same-process FG).
    3. SIGTERM a confirmed background worker, or a live FG pid that still owns
       the row (handler sets the token).
    4. If nothing is alive to finish the demotion, `fail_if_running` with a
       cancel reason so the row is not forever-`running` and the next FG wait
       is not blocked by a ghost.

    The live path's own finally (FG lock release, provider process-group kill)
    still owns cleanup when the process is reachable; this function does not
    re-implement that stack.
    """
    import os
    import signal

    from . import dispatch, pipeline
    from .store import RUNNING

    if review_id is None or str(review_id).strip() == "":
        return 2, "skodun review-cancel: review_id is required"
    rid = str(review_id).strip()
    _maybe_recover_stale(store)
    try:
        rec = store.get_review(rid)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, f"skodun review-cancel: could not read the store: {e!r}"
    if rec is None:
        return 2, f"skodun review-cancel: no such review: {rid}"
    if rec.get("status") != RUNNING:
        state = report_state(rec)
        return 2, (f"skodun review-cancel: review {rid} is already terminal "
                   f"({state})")

    token_set = pipeline.request_cancel(rid)
    signalled = False
    pid = rec.get("pid")
    # When the token is in THIS process, setting it is enough: SIGTERM would
    # hit our own process (tests, MCP server) and is unnecessary. Cross-process
    # cancel is the only case that needs a signal.
    if not token_set:
        if dispatch.pid_is_skodun_worker(pid, rid):
            try:
                os.kill(int(pid), signal.SIGTERM)
                signalled = True
            except (OSError, ProcessLookupError, ValueError, TypeError):
                pass
        elif _pid_is_live_skodun_fg(pid) and int(pid) != os.getpid():
            # Foreground CLI or MCP server in another process. MCP installs its
            # SIGTERM forwarder on the MAIN thread (reviews run on a worker
            # thread where signal handlers cannot be installed); that forwarder
            # sets the cancel token so the review's finally demotes cleanly.
            # Never signal ourselves.
            try:
                os.kill(int(pid), signal.SIGTERM)
                signalled = True
            except (OSError, ProcessLookupError, ValueError, TypeError):
                pass

    demoted = False
    if not token_set and not signalled:
        # Dead or unconfirmable holder: durable terminal now, same fail-closed
        # posture as recover_stale, with a cancel reason so report_state maps
        # to `cancelled` rather than a generic failed sweep.
        try:
            demoted = bool(store.fail_if_running(rid, REVIEW_CANCEL_DURABLE_REASON))
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            return 2, (f"skodun review-cancel: could not demote review {rid}: "
                       f"{e!r}")
    elif signalled:
        # Wait briefly for the holder to demote via its cancel path. If the
        # process died without demoting (no SIGTERM handler — the MCP bug this
        # poll closes), finish the row here so it is not forever-running.
        outcome = _await_cancel_or_demote(store, rid, pid)
        if outcome == "done":
            return 0, f"skodun review-cancel: cancel completed for {rid}"
        if outcome == "demoted":
            return 0, (f"skodun review-cancel: cancelled {rid} "
                       f"(durable terminal; holder exited without demoting)")
        # Still running: cancel was requested; the holder is finishing.
        return 0, f"skodun review-cancel: cancel requested for {rid}"

    if demoted:
        return 0, (f"skodun review-cancel: cancelled {rid} "
                   f"(durable terminal; holder was not reachable)")
    if token_set:
        return 0, f"skodun review-cancel: cancel requested for {rid}"
    # Live but unconfirmable pid: do not SIGTERM a stranger. Operator can
    # re-run after the process dies, or recover_stale will sweep by age.
    return 0, (f"skodun review-cancel: cancel noted for {rid}; "
               f"holder pid {pid!r} could not be confirmed — "
               f"re-check with review-status")


#: How long cross-process cancel waits for the holder to demote before either
#: demoting a dead unclean exit or returning "cancel requested". Short enough
#: for CLI UX; long enough for a token-driven demotion + provider SIGKILL grace.
_CANCEL_AWAIT_SEC = 8.0


def _await_cancel_or_demote(store, review_id: str, pid) -> str:
    """After signalling: wait for terminal, or demote if the holder died dirty.

    Returns ``"done"`` when the row left ``running`` on its own, ``"demoted"``
    when this function applied `fail_if_running`, or ``"pending"`` when the
    holder is still alive and still running (cancel requested; re-check later).
    """
    import time as _time

    from .store import RUNNING

    deadline = _time.monotonic() + _CANCEL_AWAIT_SEC
    while _time.monotonic() < deadline:
        try:
            rec = store.get_review(review_id)
        except KeyboardInterrupt:
            raise
        except BaseException:
            return "pending"
        if rec is None or rec.get("status") != RUNNING:
            return "done"
        if not _pid_alive(pid):
            try:
                if store.fail_if_running(review_id, REVIEW_CANCEL_DURABLE_REASON):
                    return "demoted"
            except KeyboardInterrupt:
                raise
            except BaseException:
                return "pending"
            # Race: another transition already terminalised the row.
            try:
                rec = store.get_review(review_id)
            except BaseException:
                return "pending"
            if rec is None or rec.get("status") != RUNNING:
                return "done"
            return "pending"
        _time.sleep(0.05)
    # Still running and (if we can tell) still alive.
    try:
        rec = store.get_review(review_id)
    except BaseException:
        return "pending"
    if rec is None or rec.get("status") != RUNNING:
        return "done"
    if not _pid_alive(pid):
        try:
            if store.fail_if_running(review_id, REVIEW_CANCEL_DURABLE_REASON):
                return "demoted"
        except BaseException:
            pass
        return "pending"
    return "pending"


def _pid_alive(pid) -> bool:
    """Whether `pid` still exists. False when unknown / dead / invalid."""
    import os as _os

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                 # pragma: no cover - not ours
        return True
    except OSError:
        return False
    return True


def _pid_is_live_skodun_fg(pid) -> bool:
    """Whether `pid` looks like a live skodun foreground/MCP process.

    Stricter than `kill -0` alone (pid reuse), laxer than the worker argv
    guard: FG argv is `skodun review` / `python -m skodun` / an MCP host
    spawning `skodun mcp`, not `skodun worker --record-id ...`. Requires
    `skodun` in the command line and forbids the worker tokens so a recycled
    pid on another skodun worker is not signalled as FG.
    """
    import subprocess

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        # `-ww` for the same reason as `dispatch.pid_is_skodun_worker` -- see
        # the note there. This call reads the argv in the opposite direction
        # (it REFUSES on the worker tokens rather than requiring them), so a
        # line truncated by procps at 80 columns fails the other way: a real
        # background worker whose tokens got cut is mistaken for a live FG
        # process. Both directions need the full argv to answer honestly.
        cp = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)],
                            capture_output=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0:
        return False
    args = cp.stdout.decode("utf-8", "replace")
    if "skodun" not in args:
        return False
    # Never treat a background worker as FG: workers have their own path.
    # Require BOTH the worker argv tokens AND `--record-id` so a generic
    # path that happens to contain the substring "worker" does not strip
    # FG liveness. `any` of the tokens alone would refuse every FG argv
    # (they all contain `skodun`) and leave cancel on the dead-pid path.
    from .dispatch import WORKER_ARGV_TOKENS, WORKER_RECORD_FLAG
    if (all(tok in args for tok in WORKER_ARGV_TOKENS)
            and WORKER_RECORD_FLAG in args):
        return False
    return True


# --- feedback ledger (non-gate; agents + humans) -----------------------------


def svc_feedback_add(
        store, *, kind, body, actor="agent",
        review_id=None, finding_index=None, provider=None,
        repo=None, source=None) -> tuple[int, str]:
    """Record one feedback note. ``(0, text)`` / ``(1, refused)`` / ``(2, err)``.

    Never clears the gate. Preferred path for agents to record finding
    judgment or skodun product bugs for later human inspection.
    """
    from . import feedback as feedback_mod

    try:
        row = feedback_mod.record(
            store,
            kind=kind,
            body=body,
            actor=actor if actor is not None else "agent",
            review_id=review_id,
            finding_index=finding_index,
            provider=provider,
            repo=repo,
            source=source or "service",
        )
    except feedback_mod.FeedbackError as e:
        return 1, f"skodun feedback: refused: {e}"
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, f"skodun feedback: could not record: {e!r}"
    return 0, (
        f"skodun feedback: recorded #{row['seq']} "
        f"kind={row['kind']} actor={row['actor']}"
    )


def svc_feedback_list(
        store, *, kind=None, review_id=None, limit=50) -> tuple[int, str]:
    """List feedback newest first. ``(0, text)`` or ``(2, err)``."""
    from . import feedback as feedback_mod

    try:
        rows = feedback_mod.list_feedback(
            store, kind=kind, review_id=review_id, limit=limit)
    except feedback_mod.FeedbackError as e:
        return 1, f"skodun feedback: refused: {e}"
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, f"skodun feedback: could not list: {e!r}"
    if not rows:
        return 0, "skodun feedback: (none)"
    lines = [feedback_mod.format_row(r) for r in rows]
    return 0, "\n".join(lines)
