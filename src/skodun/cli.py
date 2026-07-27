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
    `banner_failure` line too. The two invocations that legitimately exit 0
    without gating anything, `--version` and `--help`, deliberately do not.
"""

import argparse
import os
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
    return p


def _flatten(message: str) -> str:
    """One line, with every line of the message kept.

    The recorded note used to be the LAST line only, which silently dropped
    the `identity note:` lines the gate prefixes to its verdict -- so an
    auditor reading `gate_events` could not see that the decision was made
    against an under-scoped identity. `gate_events.note` is a single-line
    column by convention, so the lines are joined rather than truncated.
    """
    return " | ".join(message.splitlines())


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
        branch = None
        try:
            from . import gitio
            branch = gitio.current_branch(repo)
        except BaseException:
            pass   # a label for the auditor, never a precondition for the row
        store.log_gate_event(dict(
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            repo=str(repo), branch=branch, diff_hash=None,
            outcome="error", code=2, note=_flatten(note)))
    except BaseException:
        pass


def _emit(message: str, code: int) -> int:
    """Print the verdict and return `code` -- whatever printing does.

    The computation of the verdict and the delivery of it are separate
    failures. A `BrokenPipeError` (`skodun gate | head`, `| grep -q`), any
    other `OSError` (full disk, closed fd), or a `UnicodeEncodeError` from an
    ASCII-only locale meeting a non-ASCII message must not turn a 2 into the
    interpreter's exit code of 1.
    """
    try:
        print(message)
        sys.stdout.flush()
    except BaseException:
        # Redirect the doomed stream at devnull before returning: CPython
        # flushes stdout again during finalization, and a failure there is
        # reported as exit status 120, which is not in the contract either.
        # (This is the recipe from the stdlib docs for BrokenPipeError.)
        try:
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(fd, sys.stdout.fileno())
            finally:
                os.close(fd)
        except BaseException:
            pass
    return code


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

    try:
        cfg = load_config(repo)
        result = run_gate(store, repo, cfg)   # records its own event; never raises
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
    except BaseException as e:
        return _emit(banner_failure(
            f"could not load the review pipeline: {e!r}; no review ran"), 2)

    repo = Path(args.repo)
    try:
        store = Store.open(_store_path())
    except BaseException as e:
        # No store means no record, which is exactly what 4 says.
        return _emit(banner_failure(f"could not open the review store: {e!r}"), 4)
    try:
        cfg = load_config(repo)
    except BaseException as e:
        # A config that will not load is a refusal before anything ran, not a
        # review that came back badly: 2, the preflight code.
        return _emit(banner_failure(f"could not load the config: {e!r}"), 2)

    try:
        rec = run_review(repo, cfg, store)
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
        store = Store.open(_store_path())
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
            return _cmd_review(args)
        if args.command == "import-legacy":
            return _cmd_import_legacy(args)
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
