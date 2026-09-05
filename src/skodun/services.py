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


def svc_review_readiness(store, repo, *, reviewer=None, client_family=None,
                         output="text") -> tuple[int, str, dict]:
    """Read-only readiness for a review, without acquiring review capacity."""
    from .cli import _repo_root
    from .config import load_config
    from . import readiness

    try:
        root = _repo_root(Path(repo))
        cfg = load_config(root)
        report = readiness.check(
            store, root, cfg, requested=reviewer,
            client_family=client_family)
        text = readiness.render(report, output=output)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        report = readiness.ReadinessReport(
            ready=False, state="known_impossible",
            reason_code="readiness_error",
            reason=f"could not run readiness check: {exc!r}",
            finder=None, topology=(), passes=(), diff_bytes=0,
            prompt_budget_bytes=None, batch_count=0, estimated_attempts=0,
            estimated_worst_runtime_sec=0, estimated_lock_budget_sec=0)
        text = readiness.render(report, output=output)
    return (0 if report.ready else 2), text, {"readiness": report.to_dict()}


# --- review -----------------------------------------------------------------


def _svc_review_once(store, repo, *, progress_sink=None, cancel=None,
                     reviewer=None, client_family=None,
                     avoid_providers=None,
                     resume_checkpoints=True, batch_target_bytes=None,
                     stack_request=None, result_metadata=None) -> tuple[int, str]:
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

    def failure(code):
        if result_metadata is not None:
            result_metadata.clear()
            result_metadata['termination'] = {'reason_code': code}


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
        failure("pipeline_unavailable")
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
        failure("repository_invalid")
        return 2, banner_failure(f"{e}; no review ran")
    try:
        from .requests import current
        ctx = current()
        cfg = (ctx.config if ctx is not None and ctx.store is store and ctx.config is not None
               else load_config(root))
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # A config that will not load is a refusal before anything ran, not a
        # review that came back badly: 2, the preflight code.
        failure("configuration_invalid")
        return 2, banner_failure(f"could not load the config: {e!r}")

    from .review_results import linked_reviews, cancelled_observation
    prior_reviews = linked_reviews(store)
    try:
        if batch_target_bytes is not None:
            from dataclasses import replace
            cfg = replace(cfg, defaults=replace(
                cfg.defaults, batch_target_bytes=batch_target_bytes))
        rec = run_review(root, cfg, store, progress_sink=progress_sink,
                         cancel=cancel, reviewer=reviewer,
                         client_family=client_family,
                         avoid_providers=avoid_providers,
                         resume_checkpoints=resume_checkpoints,
                         stack_request=stack_request)
    except PreflightRefused as e:
        failure(getattr(e, "reason_code", "preflight_refused"))
        return 2, banner_failure(str(e))
    except LockTimeout as e:
        failure("admission_expired")
        return 3, banner_failure(str(e))
    except PersistenceFailed:
        failure("persistence_failed")
        return 4, banner_failure("no review was recorded")
    except GitError as e:
        failure("repository_invalid")
        # A directory that is not a git checkout at all, a git that will not run,
        # a repo with no HEAD: every git call the pipeline makes happens before
        # the reviewer is launched, so this is a preflight failure — nothing ran
        # — and preflight refusals are 2, not "the review failed".
        return 2, banner_failure(f"{e}; no review ran")
    except ReviewCancelled:
        failure(getattr(cancel, "reason_code", None) or "requested_cancel")
        if result_metadata is not None:
            facts = cancelled_observation(store, prior_reviews)
            if facts is not None:
                result_metadata['observation'] = facts
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
        failure("execution_failed")
        return 4, banner_failure(f"the review failed: {e!r}")

    # The verdict and optional stack projection are derived from the PERSISTED
    # record. The caller owns presentation so the verdict remains the final
    # line on both CLI and MCP surfaces.
    if result_metadata is not None and isinstance(rec.get("stack"), dict):
        result_metadata["stack"] = dict(rec["stack"])
    if result_metadata is not None:
        from .review_results import observation
        result_metadata["observation"] = observation(rec)
    text = banner(rec)
    if rec.get("trustworthy") is not True:
        return 4, text
    try:
        total = int(rec.get("findings_total") or 0)
    except (TypeError, ValueError):
        total = 1     # an uncountable findings list is not a clean review
    return (1 if total > 0 else 0), text


# --- bounded recovery ------------------------------------------------------

_RECOVERY_DEFAULT_ATTEMPTS = 3
_RECOVERY_MAX_ATTEMPTS = 8
_RECOVERY_DEFAULT_WALL_SECONDS = 900
_REUSE_INTENT_UNSET = object()
_BATCH_TARGET_MAX_BYTES = 10_000_000


def _validate_batch_target(value):
    """Validate the optional per-call planner hint shared by both surfaces."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return value, "batch_target_bytes must be a non-negative integer"
    if value < 0 or value > _BATCH_TARGET_MAX_BYTES:
        return value, ("batch_target_bytes must be between 0 and "
                       f"{_BATCH_TARGET_MAX_BYTES}")
    # Zero is the wire-level spelling for "use configured/default planner".
    if value == 0:
        return None, None
    return value, None


def _validate_recovery_limits(max_attempts, max_wall_seconds):
    import math

    if max_attempts is None:
        max_attempts = _RECOVERY_DEFAULT_ATTEMPTS
    if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= _RECOVERY_MAX_ATTEMPTS):
        return max_attempts, None, (
            f"max_attempts must be an int from 1 to "
            f"{_RECOVERY_MAX_ATTEMPTS}, got {max_attempts!r}")
    if max_wall_seconds is None:
        max_wall_seconds = _RECOVERY_DEFAULT_WALL_SECONDS
    wall_seconds = None
    if (not isinstance(max_wall_seconds, bool)
            and isinstance(max_wall_seconds, (int, float))):
        try:
            wall_seconds = float(max_wall_seconds)
        except (OverflowError, ValueError):
            wall_seconds = None
    if (wall_seconds is None or not math.isfinite(wall_seconds)
            or wall_seconds <= 0 or wall_seconds > 86400):
        return max_attempts, None, (
            "max_wall_seconds must be a positive number no greater than "
            f"86400, got {max_wall_seconds!r}")
    return max_attempts, wall_seconds, None


def _reuse_audit(store, probe, *, outcome: str, reason: str,
                 reviewer=None, client_family=None) -> None:
    import time

    identity = None if probe is None else probe.identity
    try:
        store.append_reuse_event(
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            outcome=outcome, reason=reason,
            repo_id=None if identity is None else identity.repo_id,
            worktree_root=None if identity is None else identity.worktree_root,
            branch=None if identity is None else identity.branch,
            base_sha=None if identity is None else identity.base_sha,
            diff_hash=None if identity is None else identity.diff_hash,
            context_hash=None if identity is None else identity.context_hash,
            checklist_hash=None if identity is None else identity.checklist_hash,
            tree_fingerprint=None if identity is None
            else identity.tree_fingerprint,
            security_policy_hash=None if identity is None
            else identity.security_policy_hash,
            requested_reviewer=reviewer, client_family=client_family,
            matched_review_id=(None if probe is None or probe.candidate is None
                               else probe.candidate.get("id")))
    except KeyboardInterrupt:
        raise
    except BaseException:
        # Reuse is optional optimization telemetry. A failed audit must never
        # turn a safe fresh review into a refusal.
        pass


def _try_reuse(store, repo, *, reuse_trusted: bool, fresh: bool,
               reviewer=None, client_family=None, intent_client_family=None,
               cancel=None, batch_target_bytes=None, bypass_reason=None):
    """Return a reused verdict or a diagnostic to prefix to a fresh review."""
    from . import reuse
    if not reuse_trusted:
        return None, None, {}
    if bypass_reason is not None:
        reason = bypass_reason
        _reuse_audit(store, None, outcome="bypass", reason=reason,
                     reviewer=reviewer, client_family=client_family)
        return None, f"SKODUN REUSE: bypass reason={reason}", {
            "reuse": {"hit": False, "reason": reason}}
    if fresh:
        reason = "explicit fresh requested"
        _reuse_audit(store, None, outcome="bypass", reason=reason,
                     reviewer=reviewer, client_family=client_family)
        return None, f"SKODUN REUSE: bypass reason={reason}", {
            "reuse": {"hit": False, "reason": reason}}
    if reviewer is not None or intent_client_family is not None:
        reason = "explicit reviewer or client-family intent requested"
        _reuse_audit(store, None, outcome="bypass", reason=reason,
                     reviewer=reviewer, client_family=intent_client_family)
        return None, f"SKODUN REUSE: bypass reason={reason}", {
            "reuse": {"hit": False, "reason": reason}}
    if cancel is not None and cancel.is_set():
        from .trust import banner_failure
        reason = REVIEW_CANCELLED_REASON
        return (4, banner_failure(reason)), None, {
                "termination": {"reason_code": getattr(cancel, "reason_code", None) or "requested_cancel"},
            "reuse": {"hit": False, "reason": reason}}
    result = None
    from .review_results import observation

    try:
        from .cli import _repo_root
        from .config import load_config

        root = _repo_root(Path(repo))
        from .requests import current
        ctx = current()
        cfg = (ctx.config if ctx is not None and ctx.store is store and ctx.config is not None
               else load_config(root))
        if batch_target_bytes is not None:
            from dataclasses import replace
            cfg = replace(cfg, defaults=replace(
                cfg.defaults, batch_target_bytes=batch_target_bytes))
        result = reuse.probe(
            store, root, cfg=cfg, client_family=client_family,
            intent_client_family=intent_client_family)
        if (result.candidate is not None and cancel is not None
                and cancel.is_set()):
            reason = REVIEW_CANCELLED_REASON
            _reuse_audit(store, result, outcome="bypass", reason=reason)
            from .trust import banner_failure
            return (4, banner_failure(reason)), None, {
                "termination": {"reason_code": getattr(cancel, "reason_code", None) or "requested_cancel"},
                "reuse": {"hit": False, "reason": reason}}
        if result.candidate is None:
            _reuse_audit(store, result, outcome="miss", reason=result.reason)
            return None, f"SKODUN REUSE: miss reason={result.reason}", {
                "reuse": {"hit": False, "reason": result.reason}}
        status, verdict = reuse.project(
            store, result.candidate, branch=result.identity.branch,
            base_sha=result.identity.base_sha)
        if cancel is not None and cancel.is_set():
            reason = REVIEW_CANCELLED_REASON
            from .trust import banner_failure
            return (4, banner_failure(reason)), None, {
                "termination": {"reason_code": getattr(cancel, "reason_code", None) or "requested_cancel"},
                "reuse": {"hit": False, "reason": reason}}
        _reuse_audit(store, result, outcome="hit", reason=result.reason)
        text = (f"SKODUN REUSE: review_id={result.candidate['id']} "
                f"reason={result.reason}\n{verdict}")
        return (status, text), None, {
            "reuse": {"hit": True, "review_id": result.candidate["id"]},
            "observation": observation(result.candidate)}
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        reason = f"probe failed ({type(exc).__name__}: {exc})"
        _reuse_audit(store, result, outcome="error", reason=reason,
                     reviewer=reviewer, client_family=client_family)
        return None, f"SKODUN REUSE: error reason={reason}", {
            "reuse": {"hit": False, "reason": reason}}


def _recovery_identity(repo):
    """Capture the exact local review identity used between attempts."""
    from .cli import _repo_root
    from .config import load_config
    from . import gitio

    root = _repo_root(Path(repo))
    cfg = load_config(root)
    base = gitio.resolve_base(root)
    diff = gitio.capture_diff(root, base.sha, cfg.defaults.untracked_max)
    return (str(gitio.repository_identity(root)),
            str(gitio.observed_worktree_root(root)),
            gitio.current_branch(root), gitio.head_sha(root), base.sha,
            gitio.diff_identity(diff.data))


def _recovery_review_id(text: str) -> str | None:
    import re

    match = re.search(r"(?:^| )id=([^ ]+)", text)
    return match.group(1) if match else None


def _annotate_recovery_attempt(store, text: str, orchestration_id: str,
                               ordinal: int, *, review_id=None) -> tuple[dict | None, str | None]:
    """Persist v9 orchestration metadata without changing trust axes."""
    review_id = review_id or _recovery_review_id(text)
    if review_id is None:
        return None, None
    rec = store.get_review(review_id)
    if rec is None:
        return None, review_id
    trustworthy = rec.get("trustworthy") is True
    rec["orchestration_id"] = orchestration_id
    rec["attempt_ordinal"] = ordinal
    rec["outcome"] = "trustworthy" if trustworthy else "untrustworthy"
    store.save_review(rec)
    return store.get_review(review_id), review_id


def _recovery_record_identity(rec: dict) -> tuple[str, ...] | None:
    """Read the v9 identity fields needed to accept a recovery attempt."""
    fields = ("repo_id", "worktree_root", "branch", "head", "base_sha",
              "diff_hash")
    values = tuple(rec.get(field) for field in fields)
    if any(not isinstance(value, str) or not value for value in values):
        return None
    return values


def _recovery_attempt_provider(rec: dict) -> str | None:
    """Return the provider id from the terminal chain attempt, not its adapter."""
    attempts = rec.get("attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict) or "skipped" in attempt:
            continue
        provider = attempt.get("provider")
        if isinstance(provider, str) and provider:
            return provider
    return None


def svc_review_detailed(store, repo, *, progress_sink=None, cancel=None,
                        reviewer=None, client_family=None, recover=False,
                        max_attempts=None, max_wall_seconds=None,
                        max_queue_seconds=None, max_review_seconds=None,
                        max_provider_wait_seconds=None,
                        reuse_trusted=False, fresh=False,
                        reuse_client_family=_REUSE_INTENT_UNSET,
                        batch_target_bytes=None, stack_manifest=None,
                        request_key=None, request_source="service", request_actor=None
                        ) -> tuple[int, str, dict]:
    """Durably identify an accepted request before readiness or admission."""
    from .review_results import attach
    from .budgets import Limits
    try:
        limits = Limits.from_args(recover=recover, max_wall_seconds=max_wall_seconds,
            max_queue_seconds=max_queue_seconds, max_review_seconds=max_review_seconds,
            max_provider_wait_seconds=max_provider_wait_seconds)
    except (ValueError, TypeError, OverflowError) as exc:
        from .trust import banner_failure
        return attach(2, banner_failure(str(exc)), {
            "termination": {"reason_code": "invalid_input"}})
    kwargs = dict(progress_sink=progress_sink, cancel=cancel, reviewer=reviewer,
                  client_family=client_family, recover=recover,
                  max_attempts=max_attempts, max_wall_seconds=max_wall_seconds,
                  reuse_trusted=reuse_trusted, fresh=fresh,
                  reuse_client_family=reuse_client_family,
                  batch_target_bytes=batch_target_bytes, stack_manifest=stack_manifest)
    # Syntactically invalid options are not accepted execution requests. Keep
    # their no-store validation contract, including overflow/bool refusals.
    if (_validate_batch_target(batch_target_bytes)[1]
            or (recover and _validate_recovery_limits(max_attempts, max_wall_seconds)[2])):
        return attach(*_svc_review_detailed_impl(store, repo, **kwargs))
    from .requests import tracked_review
    return attach(*tracked_review(_svc_review_detailed_impl)(
        store, repo, request_key=request_key, request_source=request_source,
        request_actor=request_actor, budget_limits=limits, **kwargs))


def _svc_review_detailed_impl(store, repo, *, progress_sink=None, cancel=None,
                        reviewer=None, client_family=None, recover=False,
                        max_attempts=None, max_wall_seconds=None,
                        reuse_trusted=False, fresh=False,
                        reuse_client_family=_REUSE_INTENT_UNSET,
                        batch_target_bytes=None, stack_manifest=None
                        ) -> tuple[int, str, dict]:
    """Shared review surface plus recovery metadata for MCP structured output."""
    import threading
    import time

    batch_target_bytes, target_reason = _validate_batch_target(
        batch_target_bytes)
    if target_reason:
        from .trust import banner_failure
        return 2, banner_failure(target_reason), {
            "termination": {"reason_code": "invalid_input"},
            "telemetry": {"batch_target_bytes": batch_target_bytes,
                           "validation_error": target_reason}}

    if recover:
        max_attempts, wall_seconds, reason = _validate_recovery_limits(
            max_attempts, max_wall_seconds)
        if reason:
            from .trust import banner_failure
            return 2, banner_failure(reason), {
                "termination": {"reason_code": "invalid_input"},
                "recovery": {"terminal_reason": reason}}

    stack_request = None
    if stack_manifest is not None:
        from . import stack
        from .requests import current
        ctx = current()
        stack_request = (ctx.stack_request if ctx is not None and ctx.store is store
                         else stack.load_request(stack_manifest))

    reuse_result, reuse_note, reuse_metadata = _try_reuse(
        store, repo, reuse_trusted=reuse_trusted, fresh=fresh,
        reviewer=reviewer, client_family=client_family,
        intent_client_family=(client_family
                              if reuse_client_family is _REUSE_INTENT_UNSET
                              else reuse_client_family),
        cancel=cancel, batch_target_bytes=batch_target_bytes,
        bypass_reason=(
            "stack_attribution_requested" if stack_request is not None
            else None))
    if reuse_result is not None:
        return (*reuse_result, reuse_metadata)
    if not recover:
        # MCP resolves an omitted family from the handshake, then passes the
        # separate ``reuse_client_family`` intent marker.  A handshake guess
        # must not turn an otherwise resumable call into a fresh run; only an
        # explicitly declared family is a resume boundary.
        intent_family = (client_family
                         if reuse_client_family is _REUSE_INTENT_UNSET
                         else reuse_client_family)
        result_metadata = {}
        status, text = _svc_review_once(
            store, repo, progress_sink=progress_sink, cancel=cancel,
            reviewer=reviewer, client_family=client_family,
            resume_checkpoints=(not fresh and reviewer is None
                                and intent_family is None),
            batch_target_bytes=batch_target_bytes,
            stack_request=stack_request, result_metadata=result_metadata)
        if "stack" in result_metadata:
            from .stack import render_projection
            stack_line = render_projection(result_metadata["stack"])
            text = f"{stack_line}\n{text}"
        if reuse_note:
            text = f"{reuse_note}\n{text}"
        return status, text, {**reuse_metadata, **result_metadata}

    from . import ids
    from .store import RUNNING
    from .trust import banner_failure

    orchestration_id = ids.new_review_id("sk_orch_")
    deadline = time.monotonic() + wall_seconds

    class _RecoveryCancel:
        """One cancellation token that also wakes at the recovery deadline."""

        def __init__(self):
            self._event = threading.Event()
            self.deadline_expired = False
            self._cause = None

        def _refresh(self):
            if cancel is not None and cancel.is_set():
                self._event.set()
            elif time.monotonic() >= deadline:
                self.deadline_expired = True
                self._event.set()

        @property
        def reason_code(self):
            self._refresh()
            if self.deadline_expired:
                return 'budget_expired'
            if self._event.is_set():
                return getattr(cancel, 'reason_code', None) or self._cause or 'unknown_cancel_token'
            return None

        @reason_code.setter
        def reason_code(self, cause):
            self._cause = cause

        def is_set(self):
            self._refresh()
            return self._event.is_set()

        def set(self):
            if cancel is not None:
                if self._cause is not None:
                    from .request_cancel import mark_event
                    mark_event(cancel, self._cause)
                else:
                    cancel.set()
            self._event.set()

        def wait(self, seconds):
            self._refresh()
            if self._event.is_set():
                return True
            remaining = max(0.0, deadline - time.monotonic())
            timeout = remaining if seconds is None else min(seconds, remaining)
            result = self._event.wait(timeout)
            self._refresh()
            return result or self._event.is_set()

    request_cancel = _RecoveryCancel()

    def cancellation_reason():
        cause = getattr(cancel, 'reason_code', None)
        if request_cancel.deadline_expired or cause == 'total_budget_exhausted':
            return "recovery wall budget exhausted"
        if cause in ('queue_budget_exhausted', 'review_budget_exhausted'):
            return cause.replace('_', ' ')
        if cancel is not None and cancel.is_set():
            return REVIEW_CANCELLED_REASON
        return None

    initial_identity = None
    try:
        initial_identity = _recovery_identity(repo)
    except KeyboardInterrupt:
        raise
    except BaseException:
        # The normal attempt owns the canonical preflight wording. Recovery
        # only uses identity capture to decide whether a second attempt is safe.
        pass

    review_ids: list[str] = []
    attempt_count = 0
    terminal_providers: set[str] = set()
    last_status, last_text = 4, banner_failure("no review was recorded")
    last_rec: dict | None = None
    terminal_reason = ""
    terminal_code = None
    result_metadata = {}
    for ordinal in range(max_attempts):
        reason = cancellation_reason()
        if reason is not None:
            last_status, last_text = 4, banner_failure(REVIEW_CANCELLED_REASON)
            if reason == "recovery wall budget exhausted":
                last_text = banner_failure(reason)
            terminal_reason = reason
            terminal_code = request_cancel.reason_code
            break
        if ordinal and initial_identity is not None:
            try:
                if _recovery_identity(repo) != initial_identity:
                    terminal_reason = "repository identity or diff moved between recovery attempts"
                    terminal_code = "identity_changed"
                    break
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                terminal_reason = f"could not recheck identity before recovery: {e!r}"
                terminal_code = "identity_unavailable"
                break
        if time.monotonic() >= deadline:
            terminal_reason = "recovery wall budget exhausted"
            terminal_code = "budget_expired"
            break

        attempt_count += 1
        attempt_metadata = {}
        last_status, last_text = _svc_review_once(
            store, repo, progress_sink=progress_sink, cancel=request_cancel,
            reviewer=reviewer, client_family=client_family,
            avoid_providers=(set(terminal_providers)
                             if reviewer is None else set()),
            # Recovery attempts are deliberately independent second looks.
            # The caller may opt into checkpoint resume for an ordinary review,
            # but the bounded recovery contract has always promised fresh
            # records and provider diversity; reusing a partial batch here
            # would silently turn a retry into the same interrupted run.
            resume_checkpoints=False, batch_target_bytes=batch_target_bytes,
            stack_request=stack_request,
            result_metadata=attempt_metadata)
        result_metadata = attempt_metadata
        try:
            observed_id = (attempt_metadata.get('observation') or {}).get('review_id')
            if observed_id:
                last_rec, review_id = _annotate_recovery_attempt(
                    store, last_text, orchestration_id, ordinal, review_id=observed_id)
            elif 'termination' in attempt_metadata:
                # Typed no-record failures cannot borrow an ID from prose.
                last_rec, review_id = None, None
            else:
                last_rec, review_id = _annotate_recovery_attempt(
                    store, last_text, orchestration_id, ordinal)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            terminal_reason = f"could not persist recovery metadata: {e!r}"
            terminal_code = "persistence_failed"
            result_metadata = {}
            last_status = 4
            break
        # Keep only metadata belonging to the persisted terminal attempt. A
        # preflight refusal or lock timeout may leave no record at all; in that
        # case a prior attempt's stack projection must not be rendered beside
        # the later failure banner.
        if review_id is not None:
            review_ids.append(review_id)
        if last_rec is None:
            terminal_code = (request_cancel.reason_code or
                             (attempt_metadata.get("termination") or {}).get("reason_code"))
            terminal_reason = (
                cancellation_reason() or
                (REVIEW_CANCELLED_REASON if "cancel" in last_text.lower()
                else "preflight or persistence refusal; recovery stopped")
            )
            if review_ids:
                last_status = 4
            break
        if last_rec.get("status") == RUNNING:
            terminal_reason = "review remained running; recovery stopped"
            terminal_code = "review_incomplete"
            break
        reason = cancellation_reason()
        if reason is not None:
            last_status = 4
            terminal_reason = reason
            terminal_code = request_cancel.reason_code
            break
        if initial_identity is None:
            last_status = 4
            terminal_reason = ("could not establish recovery identity; "
                               "recovery stopped")
            terminal_code = "identity_unavailable"
            break
        try:
            if (_recovery_record_identity(last_rec) != initial_identity
                    or _recovery_identity(repo) != initial_identity):
                last_status = 4
                terminal_reason = "repository identity or diff moved after recovery attempt"
                terminal_code = "identity_changed"
                break
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            last_status = 4
            terminal_reason = f"could not recheck identity after recovery: {e!r}"
            terminal_code = "identity_unavailable"
            break
        if last_rec.get("trustworthy") is True:
            terminal_reason = "trustworthy review reached"
            break
        failure = str(last_rec.get("failure_reason")
                      or last_rec.get("degraded_reason")
                      or last_rec.get("stop_reason") or "")
        if last_status in (2, 3) or "cancel" in failure.lower():
            terminal_reason = failure or "terminal refusal; recovery stopped"
            break
        provider = _recovery_attempt_provider(last_rec)
        if provider and reviewer is None:
            terminal_providers.add(provider)
        if ordinal + 1 < max_attempts:
            terminal_reason = "recovery attempt was untrustworthy"
        else:
            terminal_reason = ""

    if not terminal_reason:
        terminal_reason = "recovery attempt budget exhausted"
        terminal_code = "recovery_attempts_exhausted"
    if last_rec is not None:
        try:
            last_rec["terminal_reason"] = terminal_reason
            last_rec["outcome"] = (
                "trustworthy" if last_rec.get("trustworthy") is True
                else "recovery_terminal")
            store.save_review(last_rec)
            last_text = _render_review_banner(last_rec)
        except KeyboardInterrupt:
            raise
        except BaseException:
            terminal_code = "persistence_failed"
            last_status = 4
            last_text = banner_failure("could not persist recovery result")
            result_metadata = {}
    if terminal_code is not None:
        result_metadata['termination'] = {'reason_code': terminal_code}
        if terminal_code == 'persistence_failed':
            result_metadata.pop('observation', None)
        elif terminal_code in ('identity_changed', 'identity_unavailable'):
            observed = result_metadata.get('observation')
            if observed is not None:
                result_metadata['observation'] = {
                    **observed, 'current_identity_match': False if terminal_code == 'identity_changed' else None}
    prefix = (f"SKODUN RECOVERY: orchestration_id={orchestration_id} "
              f"attempts={attempt_count} "
              f"review_ids={','.join(review_ids) or '-'} "
              f"terminal_reason={terminal_reason}")
    metadata = {"recovery": {
        "orchestration_id": orchestration_id,
        "review_ids": review_ids,
        "attempts": attempt_count,
        "terminal_reason": terminal_reason,
        "recovered": bool(last_rec and last_rec.get("trustworthy") is True
                           and len(review_ids) > 1),
    }}
    prefix_lines = [reuse_note] if reuse_note else []
    if "stack" in result_metadata:
        from .stack import render_projection
        prefix_lines.append(render_projection(result_metadata["stack"]))
    prefix_lines.append(prefix)
    prefix = "\n".join(prefix_lines)
    return last_status, f"{prefix}\n{last_text}", {
        **reuse_metadata, **metadata, **result_metadata}


def _render_review_banner(rec: dict) -> str:
    from .trust import banner
    return banner(rec)


def svc_review(store, repo, *, progress_sink=None, cancel=None,
               reviewer=None, client_family=None, recover=False,
               max_attempts=None, max_wall_seconds=None,
               max_queue_seconds=None, max_review_seconds=None,
               max_provider_wait_seconds=None,
               reuse_trusted=False, fresh=False,
               reuse_client_family=_REUSE_INTENT_UNSET,
               batch_target_bytes=None, stack_manifest=None,
               request_key=None, request_source="service") -> tuple[int, str]:
    status, text, _ = svc_review_detailed(
        store, repo, progress_sink=progress_sink, cancel=cancel,
        reviewer=reviewer, client_family=client_family, recover=recover,
        max_attempts=max_attempts, max_wall_seconds=max_wall_seconds,
        max_queue_seconds=max_queue_seconds, max_review_seconds=max_review_seconds,
        max_provider_wait_seconds=max_provider_wait_seconds,
        reuse_trusted=reuse_trusted, fresh=fresh,
        reuse_client_family=reuse_client_family,
        batch_target_bytes=batch_target_bytes, stack_manifest=stack_manifest,
        request_key=request_key, request_source=request_source)
    return status, text


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
        line = f"{line}{_review_annotation_suffix(rec)}"
        lines.append(line)
    return 0, "\n".join(lines)


def svc_stats(store, since_days=7, fmt="text") -> tuple[int, str]:
    """Render CLI-only operational statistics from the store read model."""
    from . import stats

    try:
        lower = stats.since_iso(since_days)
        data = store.telemetry_stats(since_iso=lower)
        from .queueview import augment_stats
        data = augment_stats(store, data)
        return 0, stats.render(data, fmt=fmt)
    except KeyboardInterrupt:
        raise
    except (TypeError, ValueError) as e:
        return 2, f"skodun stats: refused: {e}"
    except BaseException as e:
        return 2, f"skodun stats: could not read the store: {e!r}"


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
        finding_line = (f"[{i}] {shown_field(f.get('severity'))} "
                        f"{shown_field(f.get('file'))}:{shown_field(f.get('line'))} "
                        f"{shown_field(f.get('title'))} ({status}){marker_s}")
        scope = f.get("scope_attribution") if isinstance(f, dict) else None
        if isinstance(scope, dict):
            scope_name = shown_field(scope.get("scope"))
            scope_reason = shown_field(scope.get("reason_code"))
            if scope_name:
                finding_line += f" scope={scope_name}"
            if scope_reason:
                finding_line += f" scope_reason={scope_reason}"
        lineage = f.get("finding_lineage_v2") if isinstance(f, dict) else None
        if isinstance(lineage, dict):
            match = shown_field(lineage.get("match_reason"))
            predecessor = shown_field(lineage.get("predecessor_review_id"))
            if match:
                finding_line += f" lineage={match}"
            if predecessor:
                finding_line += f" predecessor={predecessor}"
        if isinstance(states.get(fkey), dict) and states[fkey].get("event") == "defer":
            ref = shown_field(states[fkey].get("tracking_ref"))
            if ref:
                finding_line += f" deferred_to={ref}"
        lines.append(finding_line)
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


def svc_triage_dismiss(store, review_id, index, reason, **expected) -> tuple[int, str]:
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
    from .control import guard_review
    try:
        refusal = guard_review(store, review_id, expected)
    except Exception:
        refusal = 'target_identity_unavailable'
    if refusal:
        return 2, f"skodun triage: refused reason_code={refusal}"

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


def svc_adopt_refuter(store, review_id, index, **expected) -> tuple[int, str]:
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
    from .control import guard_review
    try:
        refusal = guard_review(store, review_id, expected)
    except Exception:
        refusal = 'target_identity_unavailable'
    if refusal:
        return 2, f"skodun triage: refused reason_code={refusal}"

    import time

    from .store import _TS_FORMAT
    from .triage import (ArtifactError, FindingNotFound, TriageError,
                         adopt_refuter)

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
    lines.append(f"skodun triage: adopted the refuter's dismissal of finding "
                 f"{index} on review {review_id}")
    return 0, "\n".join(lines)


def svc_triage_reopen(store, review_id, index, reason, **expected) -> tuple[int, str]:
    """Reopen ONE previously dismissed finding, with an audited reason.

    `--adopt-refuter`'s exit contract (see `svc_adopt_refuter`): 0 recorded, 1
    refused (an unauditable reason, or a finding that is not dismissed and so has
    nothing to overturn), 2 not found.

    It takes a reason of its own — and the same reason floor a dismissal clears —
    because it moves the gate from 0 back to 1, and nothing may do that silently.
    Append-only: the dismissal it overturns stays in the ledger with its reason.
    """
    from .control import guard_review
    try:
        refusal = guard_review(store, review_id, expected)
    except Exception:
        refusal = 'target_identity_unavailable'
    if refusal:
        return 2, f"skodun triage: refused reason_code={refusal}"

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
                     reason, **expected) -> tuple[int, str]:
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
    from .control import guard_review
    try:
        refusal = guard_review(store, review_id, expected)
    except Exception:
        refusal = 'target_identity_unavailable'
    if refusal:
        return 2, f"skodun triage: refused reason_code={refusal}"

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


def _review_annotation_suffix(rec: dict) -> str:
    """Bounded additive stack/lineage tokens shared by log and status."""
    tokens = []
    stack = rec.get("stack")
    if isinstance(stack, dict):
        for key in ("status", "reason_code", "current_slice_id"):
            value = stack.get(key)
            if value not in (None, ""):
                tokens.append(_status_field(f"stack_{key}", value))
    for key in ("stack_context_bytes", "stack_context_truncated",
                "lineage_context_bytes", "lineage_context_truncated",
                "lineage_context_diagnostics", "fingerprint_diagnostics",
                "fingerprint_status", "fingerprint_candidate_count",
                "fingerprint_candidate_limit", "fingerprint_candidates_truncated"):
        value = rec.get(key)
        if value not in (None, ""):
            tokens.append(_status_field(key, value))
    findings = rec.get("findings")
    if isinstance(findings, list):
        reasons = []
        for item in findings:
            if isinstance(item, dict) and isinstance(item.get("finding_lineage_v2"), dict):
                reason = item["finding_lineage_v2"].get("match_reason")
                if isinstance(reason, str) and reason:
                    reasons.append(reason)
        if reasons:
            counts = {reason: reasons.count(reason) for reason in sorted(set(reasons))}
            tokens.append(_status_field(
                "lineage_counts", ",".join(f"{k}:{counts[k]}" for k in counts)))
    return (" | " + " ".join(tokens)) if tokens else ""


def format_status_line(rec: dict, *, now: float | None = None,
                       projection=None) -> str:
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
    if projection is not None:
        parts.extend((_status_field("coverage", projection.coverage_state),
                      _status_field("usable_evidence", projection.usable_evidence),
                      _status_field("gate_eligible", projection.gate_eligible),
                      _status_field("gate_reason", projection.gate_reason),
                      _status_field("completed_passes", projection.completed_passes),
                      _status_field("planned_passes", projection.planned_passes)))
        if projection.prompt_bytes is not None:
            parts.append(_status_field("prompt_bytes", projection.prompt_bytes))
        if projection.batch_count:
            parts.append(_status_field("batch_count", projection.batch_count))
            parts.append(_status_field("failed_passes", projection.failed_passes))
        if projection.planner_version:
            parts.append(_status_field("planner", projection.planner_version))
        if projection.boundary_digest:
            parts.append(_status_field("boundary_digest", projection.boundary_digest))
    findings = rec.get("findings")
    if isinstance(findings, list):
        lineage = [
            item.get("finding_lineage_v2", {}).get("match_reason")
            for item in findings if isinstance(item, dict)
            and isinstance(item.get("finding_lineage_v2"), dict)
            and isinstance(item.get("finding_lineage_v2", {}).get("match_reason"), str)
            and item.get("finding_lineage_v2", {}).get("match_reason")
        ]
        if any(isinstance(item, dict)
               and item.get("finding_fingerprint_v2") for item in findings):
            parts.append(_status_field("fingerprint_version",
                                       "finding_fingerprint_v2"))
            if lineage:
                counts = {}
                for reason in lineage:
                    counts[reason] = counts.get(reason, 0) + 1
                summary = ",".join(
                    f"{reason}:{counts[reason]}"
                    for reason in ("new", "repeated", "moved",
                                   "scope_changed", "ambiguous")
                    if reason in counts)
                parts.append(_status_field("lineage_counts", summary))
    parts.extend(_status_field(key, rec.get(key)) for key in (
        "stack_context_bytes", "stack_context_truncated",
        "lineage_context_bytes", "lineage_context_truncated",
        "lineage_context_diagnostics", "fingerprint_diagnostics",
        "fingerprint_status", "fingerprint_candidate_count",
        "fingerprint_candidate_limit", "fingerprint_candidates_truncated")
                 if rec.get(key) not in (None, ""))
    stack = rec.get("stack")
    if isinstance(stack, dict):
        for key in ("status", "reason_code", "current_slice_id"):
            if stack.get(key) not in (None, ""):
                parts.append(_status_field(f"stack_{key}", stack[key]))
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


def svc_review_status(store, review_id=None, repo=None, *, output="text",
                      scope="worktree", limit=50) -> tuple[int, str]:
    """Read explicit identity, or unambiguous caller-worktree activity only.

    Broader scopes always list entries. Observation never recovers stale rows.
    """
    import json
    from . import control, requests

    try:
        if review_id is None or review_id == "":
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('invalid status limit')
            rows, identity = control.status_candidates(store, repo, scope,
                100 if scope == 'worktree' else limit)
            if scope != 'worktree':
                payload = {'scope': scope, 'entries': rows, 'limit': limit}
                return 0, json.dumps(payload, sort_keys=True)
            active = [r for r in rows if r['state'] in ('accepted','queued','running')]
            if len(active) > 1:
                return 2, json.dumps({'reason_code':'ambiguous_worktree_activity',
                    'scope':identity, 'entries':active}, sort_keys=True)
            if not rows:
                return 2, json.dumps({'reason_code':'no_worktree_activity',
                    'scope':identity, 'entries':[]}, sort_keys=True)
            review_id = (active or rows)[0]['id']
        rid = str(review_id).strip()
        row = store.get_request(rid)
        if row is not None:
            request = requests.projection(row)
            if output == 'json':
                return 0, json.dumps({'request':request, 'reason_code':row.get('reason_code')}, sort_keys=True)
            return 0, ('SKODUN REQUEST: ' + json.dumps(request, sort_keys=True))
        rec = store.get_review(rid)
        if rec is None:
            return 2, f"skodun review-status: no such review or request: {rid}"
        lifecycle = control.lifecycle(rec, store.cancellation_events(
            rec.get('request_id') or rec['id']))
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        return 2, (f"skodun review-status: scope/read refusal: {type(exc).__name__} "
                   "reason_code=scope_unavailable")
    from .readmodel import project_review
    orchestration = None
    checkpoints = ()
    oid = rec.get("batch_orchestration_id") or rec.get("orchestration_id")
    if oid:
        try:
            orchestration = store.get_orchestration(oid)
            checkpoints = store.list_checkpoints(oid)
        except BaseException:
            orchestration = None
            checkpoints = ()
    projection = project_review(rec, orchestration=orchestration,
                                checkpoints=checkpoints)
    if output == "json":
        import json
        payload = {"id": rec.get("id"), "state": report_state(rec),
                   "coverage": projection.to_dict(), "identity": control.review_identity(rec),
                   "lifecycle": lifecycle}
        for key in ("stack_context_bytes", "stack_context_truncated",
                    "lineage_context_bytes", "lineage_context_truncated",
                    "lineage_context_diagnostics", "fingerprint_diagnostics",
                    "fingerprint_status", "fingerprint_candidate_count",
                    "fingerprint_candidate_limit",
                    "fingerprint_candidates_truncated"):
            if rec.get(key) not in (None, ""):
                payload[key] = rec[key]
        return 0, json.dumps(payload, sort_keys=True)
    return 0, (format_status_line(rec, projection=projection) + " identity=" +
               json.dumps(control.review_identity(rec), sort_keys=True) + " lifecycle=" +
               json.dumps(lifecycle, sort_keys=True))


def svc_evidence_summary(store, identity_digest: str, *, output="text") -> tuple[int, str]:
    """Return the bounded advisory receipt projection for one review identity."""
    try:
        digest = str(identity_digest).strip()
        if not digest:
            return 2, "skodun evidence: identity digest is required"
        import re
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            return 2, "skodun evidence: identity digest is invalid"
        rows = store.list_evidence_receipts(digest, 32)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return 2, f"skodun evidence: could not read the store: {e!r}"
    payload = {"identity_digest": digest, "receipts": rows,
               "receipt_count": len(rows)}
    if output == "json":
        import json
        return 0, json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
    if not rows:
        return 0, f"skodun evidence: identity={digest} receipts=0"
    return 0, "\n".join(
        f"skodun evidence: identity={digest} receipt={row['receipt_digest']} "
        f"kind={row['evidence_kind']} status={row['status']} "
        f"reason={row['reason_code']} terminal={row['terminal_state']} "
        f"nonce={row['nonce']}"
        for row in rows)


def svc_review_cancel(store, review_id, *, expected_request_id=None,
                      expected_worktree=None, expected_head=None, expected_diff_hash=None,
                      actor="operator", source="service", caller_worktree=None,
                      reason="Explicit cancellation requested", output="text") -> tuple[int, str]:
    """Audit an explicitly guarded target before any cancellation side effect."""
    import json
    import os
    from . import control, requests

    if not isinstance(review_id, str) or not review_id.strip():
        return 2, "skodun review-cancel: review_id is required"
    rid = review_id.strip()
    try:
        request = store.get_request(rid)
        rec = None if request else store.get_review(rid)
        if request is None and rec is None:
            return 2, f"skodun review-cancel: no such review: {rid}"
        if request is None and rec.get('request_id'):
            request = store.get_request(rec['request_id'])
        identity = ({**request['identity'], 'request_id':request['id']} if request
                    else control.review_identity(rec))
        refusal = control.guard(identity, expected_request_id=expected_request_id,
            expected_worktree=expected_worktree, expected_head=expected_head,
            expected_diff_hash=expected_diff_hash)
        if refusal:
            return 2, f"skodun review-cancel: refused reason_code={refusal}"
        if (request and request['state'] not in ('accepted','queued','running')
                or rec is not None and rec.get('status') != 'running'):
            return 2, f"skodun review-cancel: target {rid} is already terminal ({request['state'] if request else report_state(rec)})"
        try:
            caller_scope = control.scope_identity(caller_worktree or '.')['worktree_root']
        except Exception:
            caller_scope = None
        audit_id = store.record_cancellation(target_id=rid, request=request,
            identity=identity, actor=actor, source=source, caller_pid=os.getpid(),
            caller_worktree=caller_scope,
            reason=reason, cause='requested_cancel', now=requests.now())
        if request is not None:
            # The execution-fenced owner observes this durable event before its
            # next queue/provider checkpoint. No signal can hit another request.
            if _pid_alive(request['pid']):
                code, text = 0, (f"skodun review-cancel: cancel requested for {rid}; "
                                 "pending owner acknowledgement; reachability unverified")
            else:
                store.finish_cancellations(request_id=request['id'],
                    owner_token=request['owner_token'], outcome='owner_unreachable', now=requests.now())
                code, text = 2, (f"skodun review-cancel: request owner is absent for {rid}; "
                                 "reason_code=request_owner_unreachable; no recovery performed")
        else:
            code, text = _cancel_legacy_review(store, rid)
            current = store.get_review(rid)
            if code != 0:
                store.finish_cancellations(target_id=rid, outcome='refused_unproven_owner', now=requests.now())
            elif current and current.get('status') != 'running':
                store.finish_cancellations(target_id=rid,
                    outcome=control.cancellation_completion(current),
                    now=requests.now())
        payload = {'target_id':rid, 'request_id':request['id'] if request else None,
                   'reason_code':('requested_cancel' if code == 0 else
                                  'request_owner_unreachable' if request else 'legacy_owner_unproven'),
                   'audit_id':audit_id,
                   'identity':identity, 'cancellation':store.cancellation_events(rid),
                   'delivery_state':'pending_owner_acknowledgement' if code == 0 and (request or (rec or {}).get('cancellation_protocol')) else 'not_pending',
                   'owner_reachability':'unverified' if code == 0 and (request or (rec or {}).get('cancellation_protocol')) else 'not_asserted'}
        return code, json.dumps(payload, sort_keys=True) if output == 'json' else (
            text + ' audit_id=' + str(audit_id))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return 2, ('skodun review-cancel: refused before control completion '
                   f'reason_code=cancel_refused error={type(exc).__name__}')


def _cancel_legacy_review(store, review_id) -> tuple[int, str]:
    """Request cancellation of one in-flight review. `(0, text)` or `(2, why)`.

    Order of operations (each step is best-effort; together they cover FG, MCP
    long-running slot, and background workers):

    1. Refuse missing ids and already-terminal rows (same words on both
       surfaces).
    2. Set the in-process cancel token when this process holds it (MCP /
       same-process FG).
    3. Current prepush workers observe record-specific durable audit intent.
       Legacy argv/PID matches alone are never enough to authorize signals.
    4. If nothing is alive to finish the demotion, `fail_if_running` with a
       cancel reason so the row is not forever-`running` and the next FG wait
       is not blocked by a ghost.

    The live path's own finally (FG lock release, provider process-group kill)
    still owns cleanup when the process is reachable; this function does not
    re-implement that stack.
    """
    from . import pipeline
    from .store import RUNNING

    if review_id is None or str(review_id).strip() == "":
        return 2, "skodun review-cancel: review_id is required"
    rid = str(review_id).strip()
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

    from .request_cancel import RECORD_CANCEL_PROTOCOL
    if rec.get('cancellation_protocol') == RECORD_CANCEL_PROTOCOL:
        return 0, (f"skodun review-cancel: cancel requested for {rid}; "
                   "pending worker acknowledgement; reachability unverified")

    token_set = pipeline.request_cancel(rid)
    pid = rec.get("pid")
    # Legacy artifacts have no immutable process-instance witness. Even argv
    # naming this review can belong to a reused PID or another invocation.
    if token_set:
        return 0, f"skodun review-cancel: cancel requested for {rid}"
    if _pid_alive(pid):
        return 2, (f"skodun review-cancel: refused {rid}; "
                   "reason_code=legacy_owner_unproven; use the original caller's cancellation control")
    try:
        demoted = store.fail_if_running(rid, REVIEW_CANCEL_DURABLE_REASON)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return 2, f"skodun review-cancel: could not demote review {rid}: {type(exc).__name__}"
    if demoted:
        return 0, (f"skodun review-cancel: cancelled {rid} "
                   "(durable terminal; holder was not reachable)")
    return 2, f"skodun review-cancel: target {rid} became terminal"


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


def svc_queue(store, repo=None, *, request_id=None, scope='worktree', limit=50,
              output='text', now=None) -> tuple[int, str]:
    """Shared read-only request ownership/cost inspection for CLI and MCP."""
    from . import control, queueview
    try:
        if scope not in ('worktree', 'repository', 'host'):
            raise ValueError('scope must be worktree, repository or host')
        worktree, repository = None, None
        if request_id is None and scope != 'host':
            identity = control.scope_identity(repo or '.')
            if scope == 'worktree':
                worktree = identity['worktree_root']
            else:
                repository = identity['repo_id']
        data = queueview.inspect(store, request_id=request_id, worktree_root=worktree,
            repository_id=repository, scope=scope, limit=limit, now=now)
        return 0, queueview.render(data, output)
    except (ValueError, TypeError) as exc:
        return 2, f'skodun queue: refused: {exc}'
    except Exception as exc:
        return 2, f'skodun queue: inspection unavailable: {type(exc).__name__}'
