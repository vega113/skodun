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

#: What a cancelled foreground review reports. Status 4 in both surfaces: the
#: content has no trustworthy review covering it, which is exactly what 4 says,
#: and a cancelled run must never be able to report anything gentler.
REVIEW_CANCELLED_REASON = "review cancelled"


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


def svc_review(store, repo, *, progress_sink=None,
               cancel=None) -> tuple[int, str]:
    """Run one foreground review. `(code, banner)`. Exit codes, and why:

      0  trustworthy and clean            3  gave up waiting for the lock
      1  trustworthy, findings open       4  no trustworthy review exists
      2  preflight refusal (nothing ran)

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
                         cancel=cancel)
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


def svc_log(store, branch, limit) -> tuple[int, str]:
    """Recent reviews, newest first, one line each. `(code, text)`.

    `2` for a non-positive `-n` and for a store that cannot be read; `0`
    otherwise, including for a store with no rows at all (an empty listing is an
    answer, and `text` is then `""`).
    """
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
        rows = store.list_reviews(branch, rows_wanted)
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
        lines.append(
            f"{mark}{rec.get('reviewed_at')} | {rec.get('branch')} | {nfiles} | "
            f"{coerce_count(sev.get('high'))}-{coerce_count(sev.get('medium'))}"
            f"-{coerce_count(sev.get('low'))} | "
            f"{rec.get('status')} | {summary}")
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


def svc_surface(store, branch, fmt="text",
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
    """
    from . import delivery

    try:
        status, text, pending = delivery.surface(store, branch, fmt,
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
    """
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
    lines = []
    for i, f in enumerate(review["findings"]):
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
        # Only `[{i}]` and `({status})` are ours.
        lines.append(f"[{i}] {shown_field(f.get('severity'))} "
                     f"{shown_field(f.get('file'))}:{shown_field(f.get('line'))} "
                     f"{shown_field(f.get('title'))} ({status})")
        # One extra line for an annotated finding, and never more than one:
        # `refuter_line` flattens and bounds every field it prints, so arbitrary
        # model text cannot forge a second `[n]` row. An annotation is shown
        # whatever its verdict says — the listing reports what the refuter
        # answered; only `--adopt-refuter` decides what may be acted on.
        annotation = refuter_annotation(f) if annotated else None
        if annotation is not None:
            lines.append(refuter_line(annotation))
    return 0, "\n".join(lines)


def svc_triage_dismiss(store, review_id, index, reason) -> tuple[int, str]:
    """Dismiss one finding with an audited reason. `(0, text)` or `(2, why-not)`.

    The PLAIN dismissal path keeps its shipped exit contract, in which a rejected
    reason is a 2 rather than the 1 `--adopt-refuter` and `--reopen` use. That is
    deliberately left alone: it is a shipped contract that pre-push hooks and
    humans already read.
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
