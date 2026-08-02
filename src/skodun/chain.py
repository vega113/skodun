"""The chain executor: run one reviewer CHAIN to a verdict, entry by entry,
retry by retry.

Extracted from `pipeline.py` (Phase 3 Task 2) ahead of the background
dispatcher's growth, so that growth lands in a focused module instead of
swelling the pipeline further. This is a behavior-preserving move: every
function below is the same code that used to live in `pipeline.py`, just
relocated. `pipeline._run_chain` remains a one-line compatibility alias for
`run_chain` -- existing monkeypatch tests target the alias by name -- and
later tasks call `chain.run_chain` directly.

A few helpers `run_chain` depends on are genuinely SHARED with other pipeline
code (`_chain_for` is also walked by `run_review`'s preflight; `_note`,
`_iso_now` and `PROVIDER_UNAVAILABLE_TTL_SEC` are used well beyond one reviewer
chain) and so stay defined in `pipeline.py`; the functions below import them
from there, lazily (inside the function body) so that neither module has to
import the other at module load time.

`_is_path_shaped` is the exception, and it went the other way: it is shared with
`cli._fmt_binary`, which is a read-only diagnostic that must not need the review
pipeline to import at all, so it lives in the leaf (`runner`, imported at module
scope here) rather than in `pipeline`.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import capacity, runner
from .adapters import (PROMPT_TOO_LARGE_CATEGORY, REVIEW_CONTRACT,
                       UNAVAILABLE_RC, ClassifyResult, OutputContract,
                       ParseResult, PromptTooLarge, get_adapter)
from .config import Config, Defaults, Reviewer
from .store import Store, _TS_FORMAT

#: Default provider-slot wait when ``SKODUN_ADMISSION_WAIT_SECONDS`` is unset.
#: Pipeline uses the FG lock wait as the shared budget for review-fg; chain
#: uses the same env with this floor when no override is set so hermetic
#: tests and short local runs stay bounded.
_DEFAULT_PROVIDER_WAIT_SEC = 30.0


@dataclass
class _Outcome:
    """What one reviewer CHAIN (every entry, every retry) produced.

    `accepted` names the attempt whose payload the record now carries —
    `{adapter_name, provider, model, effort}` — and is `None` when no attempt
    produced one. It is what makes the record's indexed `adapter`/`model`
    columns mean "who actually answered" rather than "who was asked first".
    """

    parsed: ParseResult | None
    attempts: list[dict]
    failure_reason: str
    accepted: dict | None = None


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _attempt(n: int, r: Reviewer, *, rc: int | None = None,
             timed_out: bool | None = None, duration_sec: float | None = None,
             first_output_sec: float | None = None,
             classification: dict | None = None,
             skipped: str | None = None) -> dict:
    """One `attempts[]` row, in the ONE shape the artifact schema defines.

    Every row carries the identity of the entry it belongs to (`provider`,
    `model`, `effort`) so a flat list of attempts across a chain is readable
    without cross-referencing the config that produced it — the config may well
    have changed by the time anyone reads the record.

    The four execution fields are explicitly `None` on a row where NO PROCESS
    STARTED (a cache skip, a binary that is not there, an invocation that could
    not be built). `None` is not the same as `0`: a zero duration would read as
    "it ran and returned instantly".

    `classification` is the attempt's full `ClassifyResult` for every completed
    attempt — `ok` and `degraded` as well as `unavailable` — and `None` for a
    timed-out one, whose stdout was truncated to nothing by the runner and so
    has nothing to classify. A `None` classification is therefore read together
    with `timed_out`: `True` there means the timeout, and a `skipped` key means
    nothing ever started.
    """
    row: dict = {
        "n": n,
        "provider": r.provider,
        "model": r.model,
        "effort": r.effort,
        "rc": rc,
        "timed_out": timed_out,
        "duration_sec": duration_sec,
        "first_output_sec": first_output_sec,
        "classification": classification,
    }
    if skipped is not None:
        row["skipped"] = skipped
    return row


def _classification(verdict) -> dict:
    """A `ClassifyResult` as it is persisted. Three keys, always all three."""
    return {"kind": verdict.kind, "category": verdict.category,
            "detail": verdict.detail}


def _binary_is_absent(binary: str) -> bool:
    """Whether `binary` names nothing this machine can execute.

    Checked BEFORE spawning, so a missing CLI costs no process and its
    `attempts[]` row is honestly free of execution fields. A path-shaped value
    (see `runner._is_path_shaped`) is tested as a path (the per-adapter
    `SKODUN_<X>_BIN` overrides and grok's `~/.grok/bin/grok` default are both
    paths); a bare name goes through `PATH`. Existence only, not
    executability: a file that is there but not runnable is a permissions
    problem the spawn should report in its own words rather than something to
    route around as "the provider is unavailable".
    """
    if not binary:
        return True
    if runner._is_path_shaped(binary):
        return not Path(binary).exists()
    return shutil.which(binary) is None


def _cached_unavailable(store: Store, provider: str) -> str | None:
    """The cached reason to skip `provider` right now, or None.

    Guarded: the cache is an optimisation that saves a doomed model call, so a
    store that cannot answer costs one attempt rather than the whole review.
    (The store is consulted per ENTRY, deliberately — a `quota` outage one
    entry discovers must be seen by the next entry on the same provider.)
    """
    from .pipeline import _iso_now, _note
    try:
        return store.provider_unavailable_reason(provider, _iso_now())
    except Exception as e:      # pragma: no cover - defensive
        _note(f"could not read the provider-availability cache: {e!r}")
        return None


def _iso_at(epoch: float) -> str:
    """`epoch` as the ONE timestamp shape the store accepts (UTC, seconds, Z).

    The store validates this format at its door and orders `provider_state`
    TTLs by plain string comparison, which is only correct because every field
    is zero-padded to a constant width. `_TS_FORMAT` is `store`'s own
    definition, imported rather than re-spelled here, so the two can never
    quietly drift apart.
    """
    return time.strftime(_TS_FORMAT, time.gmtime(epoch))


def _remember_unavailable(store: Store, provider: str, verdict) -> None:
    """Cache an `unavailable` verdict — but ONLY when it is a `quota` one.

    `quota` is the one category that is a property of the PROVIDER as a whole,
    so it is the only one worth remembering past this attempt. `auth`,
    `binary` and `model` describe THIS reviewer entry's configuration:
    caching them would let one mistyped model id black-hole a whole provider —
    for every other reviewer entry, in every later chain — for the full TTL.

    Best-effort: failing to write the cache costs extra attempts later, and
    must never be the thing that fails a review that otherwise worked.
    """
    from .pipeline import PROVIDER_UNAVAILABLE_TTL_SEC, _note
    if verdict.category != "quota":
        return
    until = _iso_at(time.time() + PROVIDER_UNAVAILABLE_TTL_SEC)
    reason = verdict.detail or "provider reported a quota outage"
    try:
        store.mark_provider_unavailable(provider, reason, "quota", until)
    except Exception as e:      # pragma: no cover - defensive
        _note(f"could not record the {provider} quota outage: {e!r}")
        return
    _note(f"marking provider {provider} unavailable until {until} ({reason})")


def _effective_provider_capacity(store: Store, provider: str) -> int:
    """max_in_flight for ``provider``, or 0 while quota backoff is active.

    Cross-process: active ``provider_state`` (TTL) forces effective capacity
    0 so no new inference admits for that provider until the TTL expires.
    """
    if _cached_unavailable(store, provider) is not None:
        return 0
    return capacity.provider_max_in_flight_from_env()


def _release_provider_slot(store: Store,
                           ticket: capacity.Ticket | None) -> None:
    """Best-effort release of a provider capacity ticket (all terminals)."""
    if ticket is None:
        return
    if ticket.status not in capacity.ACTIVE_STATUSES:
        return
    try:
        capacity.finish(store, ticket, status=capacity.STATUS_RELEASED)
    except Exception:  # pragma: no cover - never fail the review on release
        pass


def _acquire_provider_slot(
        store: Store, provider: str, *,
        wait_sec: float,
        cancel: "threading.Event | None",
        on_progress) -> capacity.Ticket:
    """Acquire ``provider:<id>`` under the remaining shared admission budget.

    Call only after review-fg is held (pipeline admit order). Capacity is
    re-read every poll via :func:`_effective_provider_capacity` so a concurrent
    quota mark (effective 0) stops waiters already in the queue from admitting.
    """
    return capacity.acquire(
        store,
        scope=provider,
        resource_class=capacity.provider_resource_class(provider),
        # capacity_fn re-checks provider_state each poll (pressure reduction).
        capacity_fn=lambda: _effective_provider_capacity(store, provider),
        wait_sec=float(wait_sec),
        cancel=cancel,
        on_progress=on_progress,
    )


def _remaining_admission_sec(deadline_mono: float,
                             clock: Callable[[], float] | None = None) -> float:
    """Seconds left on the shared admit+bind wall-clock deadline (≥ 0)."""
    now = time.monotonic if clock is None else clock
    return max(0.0, float(deadline_mono) - float(now()))


def run_chain(head: Reviewer, cfg: Config, d: Defaults, prompt: bytes,
              cwd: Path, store: Store, scratch: Path, tag: str,
              contract: OutputContract = REVIEW_CONTRACT,
              cancel: "threading.Event | None" = None,
              admission_deadline: float | None = None) -> _Outcome:
    """Run a reviewer chain to a verdict: entry by entry, retry by retry.

    Every retry is a FRESH run of the same prompt — never a resumed session —
    and every entry gets its OWN complete retry budget. Sharing one budget
    across the chain would starve the last entry, which is precisely the entry
    running because everything before it already failed.

    The order of the three decisions inside an entry is the whole safety
    argument, and none of them may be reordered:

    1. **A timed-out attempt is never parsed and never classified.** The runner
       truncated its stdout to nothing on purpose: a process can print a
       complete, clean-looking envelope and then hang.
    2. **Every completed attempt is CLASSIFIED before its output is
       considered.** `classify` is the provider-neutral run-health verdict, and
       `unavailable` means this provider could not serve at all — so the entry
       stops at once (retrying against a dead provider only spends time) and
       the chain advances. `degraded` spends the entry's degraded budget
       exactly as a degraded `parse` does; the two signals are OR-ed, because
       an adapter may see truncation on either axis and a run that is degraded
       by one measure is degraded.
    3. **Only then may the output be believed**, and only `parse_ok` says it is
       worth anything. `classify` returning `ok` means "no positive evidence of
       ill health", NOT "produced usable output": an empty stdout with clean
       stderr is `ok` and worth nothing. Accepting on `kind` alone would be a
       silent false all-clear.

    Advancing the chain is reserved for `unavailable`. An entry that answered
    BADLY — degraded, unparseable, timed out — STOPS the chain and returns its
    failure: that is a harness or config problem, and hopping providers on it
    would spend someone else's quota to hide a bug. An exhausted chain is an
    explicit failure with `parsed=None`, which the record machinery turns into
    an untrustworthy `failed` record. It is never a pass.

    An invocation that could not be BUILT splits along exactly that line, and
    the split is the whole safety argument of the per-provider budgets (#15):

    * `PromptTooLarge` — this CLI cannot physically carry this prompt (`agy`
      has no prompt-file flag and ignores stdin, so its prompt must fit one
      argv word). That is a statement about the PROVIDER's capacity, i.e.
      `unavailable`, so it takes the `unavailable` path: an attempt row, a
      note, and the next entry. The planner already sizes for the head
      (`budget.prompt_budget`), so this is mostly the CHAIN-SPANNING case —
      a prompt sized for a file-fed head reaching an argv-bound fallback.
    * everything else — an effort the CLI cannot express, an unwritable
      sidecar, a prompt that is not decodable text. Config or repo errors, and
      the same bytes would fail identically at every other entry. Fatal.

    Scratch filenames extend `tag` with the chain ORDINAL and the attempt
    number (`<tag>.e<i>.a<n>`), never with the reviewer's name: names come
    from the user's config, which constrains neither `/` nor `..`. They are
    unique PER ATTEMPT, which is load-bearing rather than tidy — an adapter may
    write a sidecar derived from the prompt path (codex writes its
    `--output-schema` beside it and always overwrites), so two attempts sharing
    one prompt path would swap each other's requested response shape between
    `build_cmd` and `exec`.

    `cancel` is the background worker's cancellation token (Task 10). It is
    forwarded to EVERY `run_with_watchdog` call — a supersede landing during
    attempt 3 of a fallback entry must not wait for the chain to exhaust itself
    — and it is checked at each ENTRY boundary as well, because the work between
    two attempts (a provider-availability lookup, a binary probe, a prompt
    write) is not inside the watchdog and can otherwise buy a whole further
    model call after the token was set. `None` is the foreground path and
    changes nothing. `ReviewCancelled` propagates out of here uncaught, by
    design: it is a `BaseException` precisely so the `except Exception` guards
    between here and the worker cannot turn a killed run into a degraded review.

    ``admission_deadline`` is a ``time.monotonic()`` absolute end for the
    shared admit+bind budget (review-fg wait already spent by the pipeline
    before this call, plus every provider wait/hop here). When omitted, a
    fresh budget is started for this chain so standalone callers and tests
    still get one wall-clock bound that is **not** reset per hop.
    """
    from .pipeline import _chain_for, _note
    chain = _chain_for(cfg, head)
    attempts: list[dict] = []
    exhausted: list[str] = []
    # `n` counts attempts across the WHOLE chain, so every row in the flat
    # `attempts[]` list has a unique ordinal; the per-entry count below is what
    # the retry budgets and the timeout message talk about.
    n = 0
    # One wall-clock deadline for all provider waits/hops in this chain.
    if admission_deadline is None:
        admission_deadline = (
            time.monotonic()
            + capacity.admission_wait_from_env(_DEFAULT_PROVIDER_WAIT_SEC))

    for i, entry in enumerate(chain):
        if runner._cancelled(cancel):
            # The ENTRY boundary. Nothing has spawned for this entry yet, so
            # there is no group to take down -- `run_with_watchdog` owns that
            # for an attempt already in flight.
            raise runner.ReviewCancelled(
                f"the review was cancelled before reviewer {entry.name!r} ran")
        adapter = get_adapter(entry.provider)

        cached = _cached_unavailable(store, entry.provider)
        if cached is not None:
            n += 1
            _note(f"skipping {entry.name} ({entry.provider}): {cached}")
            attempts.append(_attempt(
                n, entry, skipped=f"provider marked unavailable: {cached}",
                classification={"kind": "unavailable", "category": "quota",
                                "detail": cached}))
            exhausted.append(f"{entry.name}/{entry.provider}: {cached}")
            continue

        binary = adapter.resolve_binary()
        if _binary_is_absent(binary):
            n += 1
            verdict = adapter.classify(UNAVAILABLE_RC, b"", b"", contract)
            next_step = ("trying the next entry" if i + 1 < len(chain)
                         else "no entries remain")
            _note(f"{entry.name} ({entry.provider}): binary not found at "
                  f"{binary}; {next_step}")
            attempts.append(_attempt(
                n, entry, skipped=f"binary not found: {binary}",
                classification=_classification(verdict)))
            exhausted.append(f"{entry.name}/{entry.provider}: {verdict.detail}")
            continue

        # S4 Phase B: hold provider:<id> for the whole entry (retries included).
        # Repo review-fg is already held by pipeline before run_chain runs.
        # Release on every terminal: hop, quota, cancel, success, fatal return.
        # Remaining shared admit+bind budget — not a fresh full wait per hop.
        wait_sec = _remaining_admission_sec(admission_deadline)
        provider_ticket: capacity.Ticket | None = None
        try:
            try:
                provider_ticket = _acquire_provider_slot(
                    store, entry.provider, wait_sec=wait_sec,
                    cancel=cancel, on_progress=_note)
            except capacity.AdmissionCancelled as e:
                raise runner.ReviewCancelled(str(e)) from e
            except capacity.AdmissionTimeout as e:
                # Slot contention or effective capacity 0 (quota pressure):
                # hop to the next chain entry rather than spinning forever.
                n += 1
                detail = str(e)
                _note(f"{entry.name} ({entry.provider}): {detail}")
                attempts.append(_attempt(
                    n, entry, skipped=f"provider capacity: {detail}"))
                exhausted.append(
                    f"{entry.name}/{entry.provider}: provider capacity wait")
                continue

            timeouts_used = 0
            degraded_used = 0
            entry_n = 0
            while True:
                n += 1
                entry_n += 1
                stem = f"{tag}.e{i}.a{n}"
                prompt_file = scratch / f"{stem}.prompt.txt"
                out_path = scratch / f"{stem}.out"
                err_path = scratch / f"{stem}.err"
                try:
                    prompt_file.write_bytes(prompt)
                    cmd = adapter.build_cmd(prompt_file, entry, d, cwd, contract)
                except PromptTooLarge as e:
                    # THIS PROVIDER cannot carry THIS PROMPT — which is what
                    # `unavailable` means, and what a fallback chain is for. So it
                    # takes the `unavailable` path below rather than the fatal one:
                    # an `agy`-headed chain with a `codex` fallback reviews a large
                    # change instead of dying on it.
                    #
                    # Fail-closed is untouched. Nothing becomes trustworthy that
                    # was not reviewed: this appends an attempt and BREAKS to the
                    # next entry, and a chain that runs out still returns
                    # `parsed=None` with a reason that names the size and the
                    # ceiling (both are in `e`'s message, and on `e.size`/`e.limit`
                    # for a caller that would rather not read prose).
                    #
                    # NOT cached against the provider: `_remember_unavailable` is
                    # not called, and the category is deliberately not `quota` —
                    # the only provider-wide-cacheable one. The next, smaller
                    # prompt will be accepted by the very same CLI.
                    verdict = ClassifyResult(
                        "unavailable", PROMPT_TOO_LARGE_CATEGORY, str(e))
                    next_step = ("trying the next entry" if i + 1 < len(chain)
                                 else "no entries remain")
                    _note(f"{entry.name} ({entry.provider}) cannot take this "
                          f"prompt ({e.size} bytes > {e.limit}); {next_step}")
                    attempts.append(_attempt(
                        n, entry,
                        skipped=f"prompt too large for this provider: {e}",
                        classification=_classification(verdict)))
                    exhausted.append(f"{entry.name}/{entry.provider}: {e}")
                    break
                except Exception as e:
                    # An effort this CLI cannot express, an unwritable schema
                    # sidecar, a prompt that is not decodable text: a LOCAL
                    # failure, not an unavailable provider. It stops the chain
                    # rather than routing around it — silently reviewing
                    # somewhere else at some other default is exactly the
                    # unnoticed downgrade `build_cmd` raises to prevent, and the
                    # same bytes would fail the same way at every other entry.
                    attempts.append(_attempt(
                        n, entry,
                        skipped=f"could not build the invocation: {e!r}"))
                    return _Outcome(None, attempts,
                                    f"reviewer {entry.name!r} could not be "
                                    f"invoked: {e!r}")

                # The prompt travels as a FILE either way; this only decides who
                # opens it. Without it, an adapter whose argv ends in the CLI's
                # stdin marker hangs until the watchdog kills it.
                stdin_path = (prompt_file
                              if getattr(adapter, "stdin_from_prompt_file", False)
                              else None)
                try:
                    result = runner.run_with_watchdog(
                        cmd, d.timeout_sec, cwd, out_path, err_path,
                        stdin_path=stdin_path, cancel=cancel)
                except FileNotFoundError:
                    # The binary existed when we looked and does not now, or the
                    # adapter's argv names something else that is missing. Same
                    # verdict as the pre-spawn check: nothing ran, so the
                    # execution fields stay null, and the chain advances.
                    verdict = adapter.classify(
                        UNAVAILABLE_RC, b"", b"", contract)
                    attempts.append(_attempt(
                        n, entry, skipped=f"binary not found: {cmd[0]}",
                        classification=_classification(verdict)))
                    exhausted.append(
                        f"{entry.name}/{entry.provider}: {verdict.detail}")
                    break

                if result.timed_out:
                    attempts.append(_attempt(
                        n, entry, rc=result.rc, timed_out=True,
                        duration_sec=round(result.duration_sec, 3),
                        first_output_sec=_round(result.first_output_sec),
                        classification=None))
                    if timeouts_used < d.timeout_retries:
                        timeouts_used += 1
                        _note(f"attempt {entry_n} timed out after "
                              f"{d.timeout_sec}s; retrying in a fresh session "
                              f"({timeouts_used}/{d.timeout_retries})")
                        continue
                    # NEVER parsed, and never classified: the runner truncated
                    # stdout precisely so that a hung run's complete-looking
                    # envelope cannot become a review.
                    return _Outcome(None, attempts,
                                    f"timed out after {entry_n} attempts")

                stdout, stderr = _read(out_path), _read(err_path)
                verdict = adapter.classify(result.rc, stdout, stderr, contract)
                attempts.append(_attempt(
                    n, entry, rc=result.rc, timed_out=False,
                    duration_sec=round(result.duration_sec, 3),
                    first_output_sec=_round(result.first_output_sec),
                    classification=_classification(verdict)))

                if verdict.kind == "unavailable":
                    # Quota shrinks effective max_in_flight to 0 via
                    # provider_state; release this slot before hopping.
                    _remember_unavailable(store, entry.provider, verdict)
                    _note(f"{entry.name} ({entry.provider}) is unavailable "
                          f"({verdict.category}): {verdict.detail}")
                    exhausted.append(
                        f"{entry.name}/{entry.provider}: {verdict.detail}")
                    break

                parsed = adapter.parse(stdout, stderr, contract)
                if verdict.kind == "degraded" and not parsed.degraded:
                    # OR-ed, not preferred: a truncation only `classify` can
                    # see is still a truncation, and the record has to say so
                    # on the axis the gate reads.
                    parsed = replace(parsed, degraded=True,
                                     degraded_reason=verdict.detail)
                if parsed.degraded and degraded_used < d.degraded_retries:
                    degraded_used += 1
                    _note(f"attempt {entry_n} came back degraded "
                          f"({parsed.degraded_reason}); retrying in a fresh "
                          f"session ({degraded_used}/{d.degraded_retries})")
                    continue

                # `parse_ok`, not `kind`: `ok` only means nothing looked ill.
                accepted = None
                if parsed.parse_ok:
                    accepted = {"adapter_name": adapter.name,
                                "provider": entry.provider,
                                "model": entry.model,
                                "effort": entry.effort}
                return _Outcome(parsed, attempts, "", accepted)
        finally:
            _release_provider_slot(store, provider_ticket)

    summary = "; ".join(exhausted) or "no chain entry could be attempted"
    return _Outcome(None, attempts, f"all providers unavailable: {summary}")
