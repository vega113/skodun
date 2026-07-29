"""The CLI seam.

Everything here exists to make sure the number the shell sees is the number
the gate decided. The exit contract (0 clean / 1 findings open / 2 no
trustworthy review) is enforced in `gate.py`; this module's job is to not lose
it on the way out. Three failure modes are specific to this seam and all three
are guarded below:

  * An invocation form that runs nothing and exits 0. A silent 0 is
    indistinguishable from a PASS to the pre-push hook that consumes it, so
    every entry point (`skodun`, `python -m skodun`, `python -m skodun.cli`)
    goes through `main()` and a missing subcommand is a usage error, not a 0.
  * An output failure editing the verdict. `print` can raise -- a broken pipe
    from `skodun gate | head`, a full disk, a closed fd -- and an exception
    escaping the process leaves Python's own exit code of 1, which is the one
    value that means "findings remain open".
  * A refusal that leaves stdout silent. The last line of stdout is always a
    verdict, so the two paths that used to exit without one -- an argparse
    usage error, and an import failure inside `_cmd_review` -- now carry a
    `banner_failure` line too. Several invocations legitimately carry no
    verdict banner, and deliberately do not gain one: `--version` and
    `--help`, which gate nothing and exit 0; `review` cut short by Ctrl-C,
    which exits 130 with stdout entirely empty -- an operator's own
    interruption, not a refusal this contract owes a banner for (see
    `main`'s scoped carve-out); and `providers`, a read-only diagnostic
    listing that is never a gate and prints no verdict line on any of its
    exit codes.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__

_DEFAULT_DB = Path(".local/share/skodun/skodun.db")
_LEGACY_DIR = ".grok-reviews"


def _store_path() -> Path:
    """`SKODUN_DB` if set, else the XDG-ish default under the home directory."""
    raw = os.environ.get("SKODUN_DB")
    return Path(raw) if raw else Path.home() / _DEFAULT_DB


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skodun")
    p.add_argument("--version", action="version", version=f"skodun {__version__}")
    # `required=True`: `skodun` with no subcommand must not exit 0. argparse
    # reports the usage error as SystemExit(2), which `main` turns into an
    # exit 2 carrying a verdict banner -- and 2 is also the contract's "no
    # trustworthy review covers this", i.e. exactly the conservative reading of
    # "nothing ran". `--version` is unaffected: the version action fires as soon
    # as the option is seen, before the required-subparser check, and exits 0,
    # which `main` passes through with argparse's own output and no banner.
    sub = p.add_subparsers(dest="command", required=True)

    gate = sub.add_parser(
        "gate", help="fail closed unless a trustworthy review covers this change")
    gate.add_argument("--repo", type=Path, default=Path("."),
                      help="repository to gate (default: the current directory)")

    review = sub.add_parser(
        "review", help="review the outgoing change now, in the foreground")
    review.add_argument("--repo", type=Path, default=Path("."),
                        help="repository to review (default: the current directory)")

    imp = sub.add_parser(
        "import-legacy",
        help="import a legacy .grok-reviews archive into the skodun store")
    imp.add_argument("--repo", type=Path, default=Path("."),
                     help="repository holding the archive (default: the "
                          "current directory)")
    imp.add_argument("--dir", type=Path, default=None, dest="dir",
                     help=f"archive directory (default: <repo>/{_LEGACY_DIR})")

    shadow = sub.add_parser(
        "shadow-compare",
        help="compare skodun's verdicts against the legacy .grok-reviews archive")
    shadow.add_argument("--dir", type=Path, default=None, dest="dir",
                        help=f"archive directory (default: ./{_LEGACY_DIR})")
    shadow.add_argument("--diff-hash", default=None, dest="diff_hash",
                        help="restrict the comparison to one diff_hash "
                             "(default: every hash on either side)")
    shadow.add_argument("--since", default=None, dest="since",
                        help="only compare rows reviewed at or after this "
                             "canonical UTC timestamp, exactly "
                             "%%Y-%%m-%%dT%%H:%%M:%%SZ (e.g. "
                             "2026-07-28T12:00:00Z); default: the whole "
                             "archive")

    providers = sub.add_parser(
        "providers",
        help="list registered provider adapters and their cached availability")
    providers.add_argument(
        "--repo", type=Path, default=Path("."),
        help="repository whose .skodun.toml to read (default: the current "
             "directory)")

    log = sub.add_parser("log", help="show recent reviews, newest first")
    log.add_argument("--branch", default=None,
                     help="restrict to one branch (default: every branch)")
    log.add_argument("-n", type=int, default=20, dest="limit",
                     help="maximum rows to show (default: 20)")

    tri = sub.add_parser(
        "triage",
        help="dismiss or reopen a finding with an audited reason, or list a "
             "review's findings")
    tri.add_argument("review_id")
    tri.add_argument("finding_index", nargs="?", type=int, default=None)
    tri.add_argument("reason", nargs="?", default=None)
    tri.add_argument("--list", action="store_true", dest="list_only",
                     help="list a review's findings instead of dismissing one")
    # The audited un-dismissal. It takes a reason of its own -- and the same
    # reason floor a dismissal clears -- because it moves the gate from 0 back
    # to 1, and nothing may do that silently. Append-only: the dismissal it
    # overturns stays in the ledger with its own reason.
    tri.add_argument("--reopen", action="store_true", dest="reopen",
                     help="reopen ONE previously dismissed finding, with an "
                          "audited reason for overturning the dismissal")
    # Explicit, per-finding, and deliberately WITHOUT a bulk form: a refuter
    # verdict is an annotation, and the only way one may ever dismiss a
    # finding is a human naming that finding. `--adopt-all` would be exactly
    # the auto-dismissal this path exists to keep out of the product.
    tri.add_argument("--adopt-refuter", action="store_true", dest="adopt_refuter",
                     help="dismiss ONE finding by adopting its refuter "
                          "annotation as the audited reason")
    return p


def _repo_root(repo: Path) -> Path:
    """The worktree root containing `repo`. Raises `GitError` if there is none.

    ONE directory decides both halves of a gate decision, and it has to be the
    same one. `gitio.capture_diff` normalises to the worktree root before any
    git call -- see its docstring for why that is load-bearing -- so the diff
    identity is a property of the worktree, not of the cwd. The config was not:
    `load_config` reads `<its argument>/.skodun.toml`, and `--repo` defaults to
    `.`, so the same working tree produced a DIFFERENT `diff_hash` depending on
    which directory the command was run from.

    Everything downstream of that followed the split. `untracked_max` read from
    the root but not from a subdirectory, so a review taken from the root could
    never satisfy a gate run from a subdirectory -- and pre-push hooks and
    manual runs routinely differ. The `identity note: untracked scan capped at
    N` warning, whose whole job is to make an under-scoped identity loud at the
    enforcement point, silently vanished with the config that set the cap. And
    `skodun review` from a subdirectory refused with "no enabled reviewer with
    role 'finder' is configured", blaming the user's reviewer table for their
    cwd.

    So both seams resolve this first and pass the ROOT everywhere: to
    `load_config`, and on to `run_gate`/`run_review`.
    """
    from . import gitio
    return gitio._worktree_root(repo)


def _record_setup_failure(store, repo: Path, note: str) -> None:
    """Record a FAIL(2) that was decided before `run_gate` could be reached.

    The contract is that every decision is recorded, and a setup failure IS a
    decision: it is reported to the caller as a FAIL(2) and it stops a push.
    `run_gate` records its own; this covers the ones taken above it, with
    `diff_hash=None` because no identity was ever computed.

    Best-effort, and deliberately so: the verdict here is already 2, the most
    conservative value in the contract, so an unwritable record cannot make it
    any safer. (That is the opposite of `run_gate._record`, which escalates a
    0 or a 1 to 2 when it cannot write -- there the record is what makes the
    lenient verdict trustworthy.)
    """
    try:
        from .store import _TS_FORMAT
        from .trust import flatten_lines
        branch = None
        try:
            from . import gitio
            branch = gitio.current_branch(repo)
        except BaseException:
            pass   # a label for the auditor, never a precondition for the row
        store.log_gate_event(dict(
            at=time.strftime(_TS_FORMAT, time.gmtime()),
            repo=str(repo), branch=branch, diff_hash=None,
            # The same `gate_events.note` convention `run_gate._record` uses,
            # from the same definition -- two spellings of it could drift.
            outcome="error", code=2, note=flatten_lines(note)))
    except BaseException:
        pass


def _emit(message: str, code: int) -> int:
    """Print the verdict and return `code` -- whatever printing does.

    The computation of the verdict and the delivery of it are separate
    failures. A `BrokenPipeError` (`skodun gate | head`, `| grep -q`), any
    other `OSError` (full disk, closed fd), or a `UnicodeEncodeError` from an
    ASCII-only locale meeting a non-ASCII message must not turn a 2 into the
    interpreter's exit code of 1.

    A `UnicodeEncodeError` gets one more chance than the others: the STREAM is
    still alive, only THIS line's characters do not fit its encoding -- an
    ASCII locale meeting the `refuter(...)` line's em dash, or a non-ASCII
    finding title. `triage --list` calls this once per line, and the old
    behaviour (poison the stream at devnull, same as a dead pipe) silently
    dropped every line printed after the first one that failed to encode,
    while still returning the caller's `code` -- an operator piping the
    listing through `grep -c` would undercount findings and see exit 0. Retry
    once with `errors="backslashreplace"` instead: every character is still
    accounted for, and the exit code stays exactly what it already was.
    """
    try:
        print(message)
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            # Round-trip through the stream's own encoding with a lossy
            # error handler, so the retry below is guaranteed to be made of
            # characters that encoding can represent -- it cannot fail with
            # the same error again.
            lossy = message.encode(encoding, errors="backslashreplace").decode(encoding)
            print(lossy)
            sys.stdout.flush()
        except BaseException:
            _blackhole_stdout()
    except BaseException:
        # BrokenPipeError, any other OSError (full disk, closed fd): the
        # STREAM itself is dead, not just this line's encoding, so there is
        # nothing a retry can do. Redirect it at devnull before returning:
        # CPython flushes stdout again during finalization, and a failure
        # there is reported as exit status 120, which is not in the contract
        # either. (This is the recipe from the stdlib docs for
        # BrokenPipeError.)
        _blackhole_stdout()
    return code


def _blackhole_stdout() -> None:
    """Redirect fd 1 at devnull so nothing further can raise writing to it."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, sys.stdout.fileno())
        finally:
            os.close(fd)
    except BaseException:
        pass


def _cmd_gate(args) -> int:
    # Every failure inside this seam is exit 2, for the same reason every
    # failure inside `run_gate` is: an exception escaping here would leave the
    # interpreter's own exit code of 1, and 1 is the one value that means
    # "findings remain open". Setup failures -- an unparseable config, an
    # unopenable store -- happen strictly before any review is consulted, so
    # reporting them as findings would be a lie in the dangerous direction.
    from .config import load_config
    from .gate import run_gate
    from .store import Store

    repo = Path(args.repo)

    # The store is opened FIRST so that a setup failure below still has
    # somewhere to be recorded. THIS failure cannot be: there is no store yet,
    # so returning 2 with no `gate_events` row is the only option available --
    # and it is the safe one, because an unrecordable refusal is still a
    # refusal. (Only a lenient verdict needs its record to be trustworthy.)
    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"SKODUN GATE: FAIL(2) could not open the store: {e!r}", 2)

    with store:
        try:
            # The ROOT, not `--repo`: the config and the diff identity must be
            # resolved against the same directory or the gate decides about a
            # different change depending on the cwd. See `_repo_root`.
            root = _repo_root(repo)
            cfg = load_config(root)
            result = run_gate(store, root, cfg)  # records its own event; never raises
        except BaseException as e:
            note = f"SKODUN GATE: FAIL(2) could not run the gate: {e!r}"
            _record_setup_failure(store, repo, note)
            return _emit(note, 2)
        return _emit(result.message, result.code)


def _cmd_review(args) -> int:
    """Run one foreground review. Exit codes, and why they are these:

      0  trustworthy and clean            3  gave up waiting for the lock
      1  trustworthy, findings open       4  no trustworthy review exists
      2  preflight refusal (nothing ran)

    `run_review` prints the verdict banner itself on every path where a record
    was persisted, because the banner has to be rendered from that record and
    not from anything recomputed here. This function's job is the other half of
    the invariant: every path that never reached a record still ends with a
    `banner_failure` line as the last line of stdout.
    """
    # Outside the guard below on purpose: it is what RENDERS the banner, so a
    # failure to import it is the one import failure no banner can report.
    from .trust import banner_failure

    try:
        # Inside the guard: an import error here -- a partial install, a
        # syntax error introduced in `pipeline.py`, a missing stdlib module in
        # a stripped environment -- used to escape to `main`, which reports on
        # stderr and leaves stdout without the verdict line the contract
        # promises. 2, not 4: nothing ran, so this is a refusal, not a review
        # that came back badly.
        from .config import load_config
        from .gitio import GitError
        from .pipeline import (LockTimeout, PersistenceFailed, PreflightRefused,
                               run_review)
        from .store import Store
    except KeyboardInterrupt:
        # Ctrl-C during the import itself: nothing ran, but that is not what
        # this is -- `main()` maps this to 130, not to the 2 the `except
        # BaseException` immediately below would otherwise give it. See the
        # module-level note on why every one of `_cmd_review`'s five
        # `BaseException` guards needs this immediately above it.
        raise
    except BaseException as e:
        return _emit(banner_failure(
            f"could not load the review pipeline: {e!r}; no review ran"), 2)

    repo = Path(args.repo)
    try:
        store = Store.open(_store_path())
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # No store means no record, which is exactly what 4 says.
        return _emit(banner_failure(f"could not open the review store: {e!r}"), 4)

    with store:
        try:
            # Before the config, and for the same reason the gate does it: the
            # config has to be read from the same directory the diff identity is
            # computed against. See `_repo_root`. A `--repo` that is not inside a
            # worktree at all raises here, which is a preflight refusal -- nothing
            # ran -- and lands on the same 2 the `GitError` handler below gives.
            root = _repo_root(repo)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            return _emit(banner_failure(f"{e}; no review ran"), 2)
        try:
            cfg = load_config(root)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            # A config that will not load is a refusal before anything ran, not a
            # review that came back badly: 2, the preflight code.
            return _emit(banner_failure(f"could not load the config: {e!r}"), 2)

        try:
            rec = run_review(root, cfg, store)
        except PreflightRefused as e:
            return _emit(banner_failure(str(e)), 2)
        except LockTimeout as e:
            return _emit(banner_failure(str(e)), 3)
        except PersistenceFailed:
            return _emit(banner_failure("no review was recorded"), 4)
        except GitError as e:
            # A directory that is not a git checkout at all, a git that will not
            # run, a repo with no HEAD: every git call the pipeline makes happens
            # before the reviewer is launched, so this is a preflight failure --
            # nothing ran -- and preflight refusals are 2, not "the review failed".
            return _emit(banner_failure(f"{e}; no review ran"), 2)
        except KeyboardInterrupt:
            # `run_review`'s own `finally` has already downgraded the `running`
            # record to `failed` and released the foreground lock (pipeline.py,
            # "never leave a `running` record or a held lock behind") by the time
            # this exception reaches here -- this guard only has to let it keep
            # going rather than let the `except BaseException` below turn it into
            # a lying "the review failed" 4.
            raise
        except BaseException as e:
            # Anything else: the review did not complete, so it certifies nothing.
            return _emit(banner_failure(f"the review failed: {e!r}"), 4)

        # The banner is already out; only the exit code is left, and it is read
        # back off the persisted record like everything else.
        if rec.get("trustworthy") is not True:
            return 4
        try:
            total = int(rec.get("findings_total") or 0)
        except (TypeError, ValueError):
            total = 1     # an uncountable findings list is not a clean review
        return 1 if total > 0 else 0


def _fmt_binary(binary: str) -> str:
    """A short word for whether `resolve_binary()`'s answer names something
    this machine can run right now: `"executable"`, `"found, NOT executable"`,
    or `"NOT FOUND"`.

    A path-shaped value (the per-adapter `SKODUN_<X>_BIN` overrides, and
    grok's own `~/.grok/bin/grok` default) is checked directly; a bare name
    goes through `PATH`, exactly how the adapter's own `Popen` call would
    resolve it -- `shutil.which` already requires `os.X_OK` along the way, so
    a match there is never merely "a file exists". The path-vs-PATH split
    itself is `pipeline._is_path_shaped`, imported rather than re-inlined, so
    it stays the ONE definition `pipeline._binary_is_absent` also uses; this
    function's job on top of that shared split is to additionally check
    EXECUTABILITY, not just existence: `providers` is read by a human deciding
    whether a review can actually run, and "there but not runnable" is a
    different, more specific fact than "not found" for them to act on.

    A path-shaped value also gets an `is_file()` check, not just `X_OK`:
    `os.access(<dir>, os.X_OK)` is true for any traversable directory, so
    without it a `SKODUN_<X>_BIN` pointed at a directory would report
    "executable" while the adapter's `Popen` on that same path would fail
    immediately -- exactly the wrong answer for a diagnostic whose whole
    point is "can a review actually run".
    """
    # Imported here, not at module scope: this keeps every OTHER `skodun`
    # subcommand from paying `pipeline`'s (heavier) import graph just because
    # `providers` needs one split it shares with it -- the same reasoning
    # `_cmd_review` already applies to its own `pipeline` import.
    from .pipeline import _is_path_shaped

    if not binary:
        return "NOT FOUND"
    if _is_path_shaped(binary):
        p = Path(binary)
        if not p.exists():
            return "NOT FOUND"
        if p.is_file() and os.access(p, os.X_OK):
            return "executable"
        return "found, NOT executable"
    return "executable" if shutil.which(binary) else "NOT FOUND"


def _fmt_provider_state(row: dict | None, shown_field) -> str:
    if row is None:
        return "none"
    return (f"active={row['active']} until={shown_field(row['unavailable_until'])} "
            f"reason={shown_field(row['reason'])} category={shown_field(row['category'])}")


def _shown_binary(binary: str, shown_field, cap: int) -> str:
    """`shown_field(binary)`, marked when the cap actually cut it.

    Visible in every run against a pytest tmp path (and any sufficiently
    deep real one): `binary=/private/.../s (NOT FOUND)` reads as a COMPLETE
    path that merely does not exist -- exactly the wrong conclusion for an
    operator debugging a missing CLI. `shown_field`'s cap is the right
    sanitization; it just needs to say, when it fires, that it fired.
    """
    shown = shown_field(binary)
    return shown + "...(truncated)" if len(binary) > cap else shown


def _cmd_providers(args) -> int:
    """List every registered provider adapter: its id, its adapter name,
    where `resolve_binary()` says its CLI lives and whether that is really
    runnable, and the cached `provider_state` row for it, if any.

    Read-only and diagnostic, not a gate, so a missing binary or an expired
    cache row is exactly the kind of thing an operator runs this to discover
    -- it is reported, not refused. Exit 0 covers all of that.

    Exit 1 is reserved for one thing: the loaded CONFIG names a reviewer
    whose `provider` has no registered adapter at all. That is a typo or a
    provider skodun has not shipped support for, worth failing this listing
    loudly in CI rather than only being discovered later. It is NOT the case
    that `run_review`'s own preflight would always catch it first: its
    adapter-resolution loop (`pipeline._adapter_for`, walked from
    `run_review` around `pipeline.py:1007-1011`, not from `_repo_root`, which
    is a `cli.py` function with nothing to do with adapters) only resolves
    adapters for the selected finder, the role-specific reviewer for each
    configured extra pass, and their fallback chains. A reviewer with
    `enabled = false` and a bad provider that is not reachable through any of
    those roles is never resolved there, so it refuses NO review at all, no
    matter how many times one runs -- for that config, this check is not
    merely "fail earlier than a review would," it is the ONLY place the typo
    is ever caught. A reviewer entry always carries a non-empty `provider` by
    the time `load_config` returns one (`config._validate` requires it), so
    every entry is considered "configured" here -- `enabled = false`
    included, because a config that silently disables the one reviewer with
    the typo instead of fixing it is not a config this check should wave
    through as clean.

    `--repo` falls back to the literal path directly when it names a real
    directory that is merely not inside a git worktree: unlike `gate`/
    `review`, this command certifies nothing about a diff, so it has no need
    of `_repo_root`'s "config and diff identity from the same directory"
    invariant, and refusing to run outside a git checkout would make this
    diagnostic tool less useful exactly where an operator reaches for it
    first -- before `git init`, or against a bare checkout. A `--repo` that
    is not even a directory gets no such fallback: `load_config` would find
    no `.skodun.toml` at a nonexistent path and report a clean 0 for the
    input most likely to be a typo, silently disabling this command's exit-1
    contract, so that case is refused with exit 2 instead, matching `gate`.
    """
    from . import store as store_mod
    from .adapters import _REGISTRY
    from .config import load_config
    from .store import Store
    from .triage import MAX_ANNOTATION_DISPLAY_CHARS, shown_field

    repo = Path(args.repo)
    try:
        root = _repo_root(repo)
    except BaseException as e:
        # `_repo_root` raises for anything that is not inside a git worktree
        # -- including a path that does not exist at all. Falling back to the
        # literal path is only safe for the first case: a real directory that
        # is simply not (in) a git checkout still has somewhere for
        # `load_config` to look for a `.skodun.toml` and honestly report "no
        # config error" if none is found. A path that is not even a
        # directory -- almost always a typo in `--repo` -- has nowhere to
        # look, so falling back the same way would make `load_config` find
        # nothing and report a clean exit 0 for exactly the input most likely
        # to be a mistake, silently disabling this command's exit-1 CI
        # contract. Refuse instead, matching `gate --repo` on the same input.
        if repo.is_dir():
            root = repo
        else:
            return _emit(
                f"skodun providers: could not resolve --repo {str(repo)!r}: "
                f"{e!r}", 2)
    try:
        cfg = load_config(root)
    except BaseException as e:
        return _emit(f"skodun providers: could not load the config: {e!r}", 2)

    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun providers: could not open the store: {e!r}", 2)

    with store:
        now = time.strftime(store_mod._TS_FORMAT, time.gmtime())
        try:
            state_rows = {row["provider"]: row
                          for row in store.provider_state_rows(now)}
        except BaseException as e:
            return _emit(f"skodun providers: could not read provider state: {e!r}", 2)

        if store_mod._provider_state_bypassed(os.environ):
            raw = os.environ.get(store_mod.IGNORE_PROVIDER_STATE_ENV, "")
            _emit(f"skodun providers: NOTE {store_mod.IGNORE_PROVIDER_STATE_ENV}="
                  f"{shown_field(raw)!r} is set -- the provider_state rows below "
                  f"are informational only; a review run right now would ignore "
                  f"every one of them", 0)

        for provider in sorted(_REGISTRY):
            adapter = _REGISTRY[provider]()
            binary = adapter.resolve_binary()
            status = _fmt_binary(binary)
            state = _fmt_provider_state(state_rows.get(provider), shown_field)
            shown_binary = _shown_binary(binary, shown_field,
                                         MAX_ANNOTATION_DISPLAY_CHARS)
            _emit(f"{provider} | adapter={adapter.name} | "
                  f"binary={shown_binary} ({status}) | state={state}", 0)

        for provider, row in sorted(state_rows.items()):
            if provider not in _REGISTRY:
                _emit(f"skodun providers: NOTE cached provider_state for "
                      f"{shown_field(provider)!r} has no registered adapter -- "
                      f"{_fmt_provider_state(row, shown_field)}", 0)

        unregistered = [(r.name, r.provider) for r in cfg.reviewers
                        if r.provider and r.provider not in _REGISTRY]
        if not unregistered:
            return 0
        for name, provider in unregistered:
            # `name`/`provider` are config-authored text of unbounded length,
            # same class of risk as every other untrusted field this command
            # prints -- `repr` alone stops a raw ESC or a forged row but has no
            # length cap, so a 10,000-char `provider` would still bury the rest
            # of the listing under itself. `shown_field` first, same as
            # everywhere else, `!r` after for the quoting.
            _emit(f"skodun providers: FAILED reviewer {shown_field(name)!r} uses "
                  f"provider {shown_field(provider)!r}, which has no registered "
                  f"adapter (known: {sorted(_REGISTRY)})", 0)
        return 1


def _cmd_import_legacy(args) -> int:
    """One-shot migration of a legacy `.grok-reviews` archive.

    Not part of the gate contract, so the exit codes are the ordinary ones:
    `0` the import ran, `2` it could not run -- or could not finish what it
    claimed. It deliberately does NOT emit a verdict banner -- nothing was
    gated and nothing was reviewed, and a banner here would give a pre-push
    hook a line it is entitled to read as a verdict.

    A missing archive is a `0` with `reviews=0`: on a machine that never used
    the legacy tool there is simply nothing to import, and `import_legacy`
    already reports every unusable line through `skipped_lines` rather than by
    failing.

    `store_failures` is the one counter that changes the exit code.
    `import_legacy` never raises, so a store that stopped accepting writes
    halfway -- a full disk, an I/O error -- comes back as an ordinary result
    object with a nonzero count in it. Exiting 0 on that would tell a migration
    script that history it does not have was preserved. Every counter the
    importer produces is printed, because a counter an operator cannot see is
    a counter that does not exist: `findings_reconciled` in particular is how
    many rows were imported on the ARTIFACT's word rather than the index's.
    """
    try:
        from .legacy_import import import_legacy
        from .store import Store
    except BaseException as e:
        return _emit(f"skodun import-legacy: FAILED to load the importer: {e!r}", 2)

    archive = Path(args.dir) if args.dir else Path(args.repo) / _LEGACY_DIR
    try:
        with Store.open(_store_path()) as store:
            stats = import_legacy(store, archive)
    except BaseException as e:
        return _emit(f"skodun import-legacy: FAILED on {archive}: {e!r}", 2)
    failed = stats.store_failures > 0
    return _emit(
        f"skodun import-legacy: {'FAILED' if failed else 'ok'} {archive} -> "
        f"reviews={stats.reviews} "
        f"triage={stats.triage} skipped_lines={stats.skipped_lines} "
        f"demoted_no_artifact={stats.demoted_no_artifact} "
        f"demoted_untrustworthy={stats.demoted_untrustworthy} "
        f"findings_reconciled={stats.findings_reconciled} "
        f"triage_unauditable={stats.triage_unauditable} "
        f"store_failures={stats.store_failures}", 2 if failed else 0)


def _fmt_side(row: dict | None) -> str:
    """Render one side of a shadow comparison as `t/H-M-L`, or `-` if absent.

    `effective_trustworthy` -- not the raw `trustworthy` field -- decides the
    `t`/`f`, so a legacy row that predates the field (absent, not `false`)
    displays the same verdict that `shadow.compare` used to decide `match`.
    """
    from .shadow import effective_trustworthy
    from .trust import coerce_count

    if row is None:
        return "-"
    sev = row.get("severity") if isinstance(row.get("severity"), dict) else {}
    mark = "t" if effective_trustworthy(row) else "f"
    return (f"{mark}/{coerce_count(sev.get('high'))}"
            f"-{coerce_count(sev.get('medium'))}-{coerce_count(sev.get('low'))}")


def _cmd_shadow_compare(args) -> int:
    """Print the shadow-mode comparison table and summary. Exits 0, except a
    malformed `--since` (checked first, below): that is a usage error, 2.

    Shadow mode is purely observational: it exists to show a human whether
    skodun agrees with the legacy tool, and a workflow that happens to run it
    must never be failed by what it finds -- or by it failing to run at all.
    Every failure path below is reported on stdout and still returns 0. That
    "still" has to survive the printing itself, not just the computation: a
    bare `print` can raise (`skodun shadow-compare | head` closes the pipe
    partway through the table), and an escaping `BrokenPipeError` would hand
    the shell the interpreter's own exit code of 1 -- observational output
    silently becoming "findings remain open". Every line below goes through
    `_emit`, the same broken-pipe guard `gate` and `review` use, for exactly
    that reason.

    `--since`, once validated, is the ONE exception to "never fails": it is
    misuse, not a data problem, so it is rejected before anything else runs
    (no archive notice, no comparison) with exit 2 -- the same contract every
    other usage error in this CLI carries, and the one Task 11's
    `providers --repo <nonexistent>` review found missing for a look-alike
    flag.

    Each row is `<hash> | <skodun> | <legacy> | <label>`, where a side reads
    `t/H-M-L`. A row the two sides disagree about carries a second, indented
    `deltas:` line: `match` is coarse by design -- both sides present, both
    agreeing on trust, both agreeing on clean-vs-dirty -- because two LLM runs
    over one diff are not expected to tally the same counts. The counts are
    exactly what a human deciding whether the new reviewer is *worse* needs,
    which is why `compare` carries them, so they are printed for the rows where
    they can mean something and suppressed for the rows where they cannot
    (a MATCH, or a side that is simply absent).
    """
    try:
        from .legacy_import import INDEX_NAME
        from .shadow import compare
        from .store import Store, _require_ts
    except BaseException as e:
        return _emit(
            f"skodun shadow-compare: could not load the shadow module: {e!r}", 0)

    # Validated FIRST and via the store's own helper -- one spelling of "what
    # counts as canonical", reused rather than restated -- so a malformed
    # `--since` is refused before any archive I/O, any store I/O, or any
    # output that a genuine data problem would otherwise have earned.
    since = args.since
    if since is not None:
        try:
            since = _require_ts("--since", since)
        except ValueError as e:
            return _emit(f"skodun shadow-compare: {e}", 2)

    archive = Path(args.dir) if args.dir else Path(_LEGACY_DIR)

    # A missing archive is not an error -- a machine that never ran the legacy
    # tool has nothing to compare against -- but it must not be SILENT. With no
    # archive found, every legacy row is absent, so the table renders every
    # skodun row as SKODUN-ONLY and the summary line below states that with
    # full confidence. `--dir` defaults to a RELATIVE path, so the commonest
    # way to get here is running from the wrong directory, and naming the path
    # that was not found is what makes that diagnosable at a glance. The exit
    # code stays 0: shadow mode is observational and must never fail a
    # workflow, not even on its own misconfiguration.
    try:
        if not archive.is_dir():
            _emit(f"skodun shadow-compare: no archive directory at {archive} "
                  f"-- nothing on the legacy side to compare against", 0)
        elif not (archive / INDEX_NAME).is_file():
            _emit(f"skodun shadow-compare: no {INDEX_NAME} in {archive} "
                  f"-- nothing on the legacy side to compare against", 0)
    except BaseException:
        pass   # a notice is a courtesy; it may never become the failure itself

    # `--diff-hash` reaches `compare`'s own filter rather than filtering the
    # result here: it also decides what "nothing to report" means (a hash on
    # neither side yields an empty list, and the summary below then says
    # `0 compared` instead of inventing an empty row for it). `since` is
    # already validated above, so this call can only raise on a genuine data
    # or I/O problem -- caught below and reported at exit 0, same as ever.
    try:
        with Store.open(_store_path()) as store:
            result = compare(store, archive, args.diff_hash, since=since)
    except BaseException as e:
        return _emit(f"skodun shadow-compare: FAILED on {archive}: {e!r}", 0)

    comparisons = result.comparisons
    matched = skodun_only = legacy_only = 0
    for c in sorted(comparisons, key=lambda c: c.diff_hash):
        if c.legacy is None:
            skodun_only += 1
            label = "SKODUN-ONLY"
        elif c.skodun is None:
            legacy_only += 1
            label = "LEGACY-ONLY"
        elif c.match:
            matched += 1
            label = "MATCH"
        else:
            label = "MISMATCH"
        _emit(f"{c.diff_hash[:12]} | {_fmt_side(c.skodun)} | {_fmt_side(c.legacy)} "
              f"| {label}", 0)
        if label == "MISMATCH":
            _emit(f"{'':12} | deltas (skodun vs legacy): "
                  + ", ".join(f"{k}={v[0]}/{v[1]}"
                              for k, v in _deltas(c).items()), 0)

    # `since=` and the excluded count are printed on EVERY run, windowed or
    # not -- a fixed schema, rather than a field that appears only sometimes,
    # is what lets Task 14's runbook (and any other script) read this line
    # the same way for both the whole-archive and the windowed comparison.
    return _emit(
        f"shadow: {len(comparisons)} compared, {matched} matched, "
        f"{skodun_only} skodun-only, {legacy_only} legacy-only, "
        f"since={since if since is not None else 'none'}, "
        f"{result.excluded_unparseable} unparseable-timestamp rows excluded", 0)


def _deltas(c) -> dict:
    """`Comparison.deltas` as a plain `{name: (skodun, legacy)}` mapping.

    Defensive rather than trusting: `deltas` is data off a dataclass this
    command only reads, and shadow mode may never fail a workflow, so a shape
    it does not recognise renders as no deltas at all instead of raising
    inside the table.
    """
    deltas = getattr(c, "deltas", None)
    if not isinstance(deltas, dict):
        return {}
    return {k: v for k, v in deltas.items()
            if isinstance(v, (tuple, list)) and len(v) == 2}


def _cmd_log(args) -> int:
    """Print recent reviews, newest first. `2` if the store cannot be read.

    The likeliest way to run this is `skodun log | head`, so every line goes
    through `_emit` rather than a bare `print`: an early-exiting reader closes
    the pipe partway through the listing, and a `BrokenPipeError` escaping
    would hand the shell the interpreter's own exit code of 1 -- a value this
    command's contract does not even have. The exit code below is always the
    one the contract promises for the outcome that was already decided (0 or
    2), never whatever printing happened to return.
    """
    # `-n` becomes SQLite's LIMIT, where a NEGATIVE value means "no limit" --
    # so `log -n -1` would dump the whole store while reading like a request
    # for fewer rows than the default. Below 1 there is no row count to ask
    # for, so this is a usage error rather than something to clamp silently.
    if args.limit < 1:
        return _emit(
            f"skodun log: -n must be a positive row count, got {args.limit}", 2)
    try:
        from .store import Store
        from .trust import coerce_count, one_line
        with Store.open(_store_path()) as store:
            rows = store.list_reviews(args.branch, args.limit)
    except BaseException as e:
        return _emit(f"skodun log: could not read the store: {e!r}", 2)

    for rec in rows:
        trustworthy = rec.get("trustworthy") is True
        sev = rec.get("severity") if isinstance(rec.get("severity"), dict) else {}
        files = rec.get("files_changed")
        nfiles = len(files) if isinstance(files, list) else 0
        # A summary carrying a stray newline must not be able to fake a second
        # row in what is meant to be a one-line-per-review listing. Same
        # definition the banner uses -- see `trust.one_line`.
        summary = one_line(rec.get("summary") or "")
        mark = "!" if not trustworthy else " "
        # Counts read by THE project's single count rule, so `log` and `banner`
        # can never disagree about the same stored row.
        _emit(f"{mark}{rec.get('reviewed_at')} | {rec.get('branch')} | {nfiles} | "
              f"{coerce_count(sev.get('high'))}-{coerce_count(sev.get('medium'))}"
              f"-{coerce_count(sev.get('low'))} | "
              f"{rec.get('status')} | {summary}", 0)
    return 0


def _cmd_triage(args) -> int:
    """Dismiss or reopen one finding with an audited reason, or list a review's.

    A rejected reason or a missing/invalid review is reported as a clear
    message and a nonzero exit -- never a traceback -- because both are the
    ordinary shape of "a human needs to try again", not an internal failure.
    `--list` in particular is routinely piped to `head` or `grep -q`, so every
    message here goes through `_emit`: a bare `print` meeting a closed pipe
    raises `BrokenPipeError`, and letting that escape would hand the shell the
    interpreter's own exit code of 1 -- indistinguishable from a real error
    about a decision that in fact was never even reached.

    `--adopt-refuter` and `--reopen` share one exit contract, and the split is
    the point:

      0  the decision was recorded
      1  REFUSED -- the finding is right there and the decision was declined
         (for `--adopt-refuter`: a wrong verdict, thin reasoning, a reasoning
         that fails the audit floor, an annotation that cannot say who
         answered; for `--reopen`: a reason that fails the audit floor, or a
         finding that is not dismissed and so has nothing to overturn)
      2  NOT FOUND -- no such review, no such finding, an artifact that does
         not validate, a store that will not open, or plain misuse

    A refusal is a fact about the ledger and is worth acting on; a 2 means the
    command never got as far as having an opinion. Collapsing them would make
    "your refuter said `confirmed`" indistinguishable from "you typed the wrong
    review id".

    The PLAIN dismissal path keeps its own shipped behaviour, in which a
    rejected reason is a 2. That is deliberately left alone here: it is a
    shipped contract that pre-push hooks and humans already read, and this task
    is not the place to change what `skodun triage <id> <n> "<reason>"` returns.
    """
    from .store import Store, _TS_FORMAT

    # `--list`, `--adopt-refuter` and `--reopen` are different commands sharing
    # one parser, so `triage --list <id> <index> "<reason>"` parses cleanly and
    # then throws the index and the reason away. Someone who typed a reason
    # believes a finding was dismissed; they get a listing and a 0. Reject every
    # mixture instead of picking one of the meanings.
    modes = [name for name, on in (("--list", args.list_only),
                                   ("--adopt-refuter", args.adopt_refuter),
                                   ("--reopen", args.reopen)) if on]
    if len(modes) > 1:
        return _emit(
            f"skodun triage: {modes[0]} and {modes[1]} are two different "
            "commands; pick one", 2)
    if args.list_only and not (args.finding_index is None and args.reason is None):
        return _emit(
            "skodun triage: --list takes only a review id; drop the finding "
            "index and the reason to list, or drop --list to dismiss", 2)
    if args.reopen and (args.finding_index is None or args.reason is None):
        # Both are mandatory: one finding at a time, and never without a stated
        # reason for overturning a dismissal somebody else may have recorded.
        return _emit(
            "skodun triage: usage: skodun triage --reopen <review-id> "
            "<finding-index> \"<reason>\"  (one finding at a time, and the "
            "reason is required)", 2)
    if args.adopt_refuter:
        # Same class of mixture, and the same refusal to guess. The reason is
        # SYNTHESIZED from the annotation, so a reason typed alongside the flag
        # is silently discarded -- and its author would have every right to
        # believe their words were the ones recorded in the ledger.
        if args.reason is not None:
            return _emit(
                "skodun triage: --adopt-refuter takes only a review id and a "
                "finding index; the reason comes from the refuter's own "
                "annotation, so drop yours or drop the flag", 2)
        if args.finding_index is None:
            return _emit(
                "skodun triage: usage: skodun triage --adopt-refuter "
                "<review-id> <finding-index>  (one finding at a time, on "
                "purpose)", 2)

    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun triage: could not open the store: {e!r}", 2)

    with store:
        review = store.get_review(args.review_id)
        if review is None:
            return _emit(f"skodun triage: no such review: {args.review_id!r}", 2)

        from .textnorm import finding_key
        from .triage import (ArtifactError, FindingNotFound, TriageError,
                             adopt_refuter, dismiss, load_valid_artifact,
                             refuter_annotation, refuter_line, refuter_pass_ran,
                             refuter_same_provider_as_finder, reopen, shown_field,
                             status_token)

        try:
            review = load_valid_artifact(review)
        except ArtifactError as e:
            return _emit(f"skodun triage: invalid review artifact: {e}", 2)

        if args.list_only:
            # The EFFECTIVE state of every finding in this review's scope, from
            # the store's one definition of it -- the same answer the gate gets
            # from `triage_for`, which is a filter over exactly this map. A
            # second, independent "latest decision" query here could print
            # DISMISSED for a finding the gate still counts as open.
            states = store.triage_state(review["branch"], review["base_sha"])
            # An annotation is shown only on a record where a refuter pass
            # actually ran. On a record where none did, a `refuter` key is
            # something the FINDER wrote about its own finding (see
            # `triage.refuter_pass_ran`), and printing it as
            # `refuter(<provider>/<model>)` would be this program vouching for a
            # second opinion that was never sought -- which is the same misleading
            # line whether or not `--adopt-refuter` goes on to refuse it.
            annotated = refuter_pass_ran(review)
            for i, f in enumerate(review["findings"]):
                fkey = finding_key(f.get("file", ""), f.get("title", ""))
                # `OPEN`, `DISMISSED <when>`, or `REOPENED <when>, dismissed
                # <when>` -- one definition, in `triage.status_token`, which
                # bounds and strips the stored timestamps the same way every
                # other untrusted field on this line is bounded and stripped.
                status = status_token(states.get(fkey))
                # EVERY field on this line is finder-authored, untrusted model
                # text reaching the terminal the same way a refuter's `reasoning`
                # does -- `severity`, `file` and `line` are read straight off the
                # parsed payload, exactly like `title`. `shown_field` strips the
                # same control/ANSI exposure and bounds the same way, so no field
                # can forge an extra row or rewrite this line's own status the
                # instant it is printed. Only `[{i}]` and `({status})` are ours.
                _emit(f"[{i}] {shown_field(f.get('severity'))} "
                      f"{shown_field(f.get('file'))}:{shown_field(f.get('line'))} "
                      f"{shown_field(f.get('title'))} ({status})", 0)
                # One extra line for an annotated finding, and never more than
                # one: `refuter_line` flattens and bounds every field it prints,
                # so arbitrary model text cannot forge a second `[n]` row. An
                # annotation is shown whatever its verdict says -- the listing
                # reports what the refuter answered; only `--adopt-refuter`
                # decides what may be acted on.
                annotation = refuter_annotation(f) if annotated else None
                if annotation is not None:
                    _emit(refuter_line(annotation), 0)
            return 0

        if args.reopen:
            try:
                reopen(store, review, args.finding_index, args.reason,
                       now=time.strftime(_TS_FORMAT, time.gmtime()))
            except (FindingNotFound, ArtifactError) as e:
                # The finding or the review does not exist: nothing was decided.
                return _emit(f"skodun triage: {e}", 2)
            except TriageError as e:
                # The finding exists and the reopen was declined -- an
                # unauditable reason, or a finding that is not dismissed.
                return _emit(f"skodun triage: refused: {e}", 1)
            except BaseException as e:
                # A store that stopped accepting writes is not a refusal about
                # the reason: nothing was decided and nothing was recorded.
                return _emit(
                    f"skodun triage: could not record the reopen: {e!r}", 2)
            return _emit(
                f"skodun triage: reopened finding {args.finding_index} on review "
                f"{args.review_id}; it counts as open again", 0)

        if args.adopt_refuter:
            try:
                adopt_refuter(store, review, args.finding_index,
                              now=time.strftime(_TS_FORMAT, time.gmtime()))
            except (FindingNotFound, ArtifactError) as e:
                return _emit(f"skodun triage: {e}", 2)
            except TriageError as e:
                return _emit(f"skodun triage: refused: {e}", 1)
            except BaseException as e:
                # A store that stopped accepting writes is not a refusal about the
                # annotation -- nothing was decided and nothing was recorded.
                return _emit(
                    f"skodun triage: could not record the dismissal: {e!r}", 2)

            # The refuter exists so that a DIFFERENT provider examines the
            # findings; a model asked to check its own work is agreeable about it.
            # A config may still put the refuter on the finder's provider -- the
            # operator's call, and better than no re-examination -- and the pass
            # records that it happened. This is the one moment where that fact has
            # consequences, so it is said out loud here rather than left in the
            # artifact for nobody to read. A WARNING and not a refusal: adoption is
            # an explicit human act, and the human is the authority this path
            # exists to consult; turning an operator's own configuration into a
            # hard block would be a second, implicit policy on top of the explicit
            # per-finding one.
            if refuter_same_provider_as_finder(review):
                _emit("skodun triage: WARNING the refuter answered from the same "
                      "provider as the finder, so this verdict is a model "
                      "re-examining its own work", 0)
            return _emit(
                f"skodun triage: adopted the refuter's dismissal of finding "
                f"{args.finding_index} on review {args.review_id}", 0)

        if args.finding_index is None or args.reason is None:
            return _emit(
                "skodun triage: usage: skodun triage <review-id> <finding-index> "
                "\"<reason>\"  |  skodun triage --list <review-id>", 2)

        try:
            dismiss(store, review, args.finding_index, args.reason,
                    now=time.strftime(_TS_FORMAT, time.gmtime()))
        except (TriageError, ArtifactError) as e:
            return _emit(f"skodun triage: rejected: {e}", 2)

        return _emit(
            f"skodun triage: dismissed finding {args.finding_index} on review "
            f"{args.review_id}", 0)


def main(argv: list[str] | None = None) -> int:
    try:
        # Inside the guard: building the parser is not inert. argparse probes
        # `sys.stdout` for colour support while constructing its formatter, so
        # a stdout that is already unusable raises here, before any argument
        # has been looked at.
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            # argparse exits 0 for --version/--help and 2 for a usage error.
            # Anything unexpected reads as 2, never as 1.
            code = e.code
            if code is None:
                return 0
            if not isinstance(code, int) or isinstance(code, bool):
                # A message-carrying or otherwise unexpected code reads as 2.
                # `bool` is caught explicitly because it is an `int` subclass
                # and `SystemExit(False)` would otherwise become a silent 0.
                code = 2
            if code == 0:
                # `--version` / `--help`: argparse already wrote the output the
                # user asked for, nothing was gated, and no verdict is owed. A
                # banner here would corrupt exactly the two invocations whose
                # stdout is meant to be consumed verbatim.
                return 0
            # A usage error never reaches a subcommand, so nothing below would
            # print the verdict line the contract promises as the LAST line of
            # stdout -- argparse's own message goes to stderr, and a consumer
            # reading stdout would see silence where a refusal belongs.
            from .trust import banner_failure
            return _emit(banner_failure(
                "usage error; no review ran"), code)
        if args.command == "gate":
            return _cmd_gate(args)
        if args.command == "review":
            try:
                return _cmd_review(args)
            except KeyboardInterrupt:
                # Scoped to exactly this dispatch, on purpose. `_cmd_review`
                # re-raises `KeyboardInterrupt` past every one of its own
                # `BaseException` guards (see its docstring), and 130 -- 128 +
                # SIGINT, the shell's own convention -- is the honest answer
                # for "the operator hit Ctrl-C", never the 2/3/4 those guards
                # would otherwise report. Every OTHER path through `main` --
                # `gate` included -- still falls through to the general
                # `except BaseException` below and reports 2: `_cmd_gate`'s
                # fail-closed contract maps every exception, Ctrl-C included,
                # to 2, and a Ctrl-C during argument parsing or a subcommand
                # this carve-out does not name is "nothing ran", which 2
                # already says correctly.
                return 130
        if args.command == "providers":
            return _cmd_providers(args)
        if args.command == "import-legacy":
            return _cmd_import_legacy(args)
        if args.command == "shadow-compare":
            return _cmd_shadow_compare(args)
        if args.command == "log":
            return _cmd_log(args)
        if args.command == "triage":
            return _cmd_triage(args)
        # Unreachable while the subparsers are `required=True`, and kept as
        # defence in depth: if that ever comes off, an unrecognised command
        # must still not certify a push by exiting 0.
        parser.print_usage(sys.stderr)
        return 2
    except BaseException as e:
        # Nothing escapes `main`. An exception propagating out of the process
        # leaves Python's exit code of 1 -- "findings remain open" -- about a
        # review that was never consulted.
        try:
            print(f"SKODUN GATE: FAIL(2) internal CLI error: {e!r}", file=sys.stderr)
        except BaseException:
            pass
        return 2


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":   # `python -m skodun.cli`
    raise SystemExit(main())
