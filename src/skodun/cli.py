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

    shadow = sub.add_parser(
        "shadow-compare",
        help="compare skodun's verdicts against the legacy .grok-reviews archive")
    shadow.add_argument("--dir", type=Path, default=None, dest="dir",
                        help=f"archive directory (default: ./{_LEGACY_DIR})")

    log = sub.add_parser("log", help="show recent reviews, newest first")
    log.add_argument("--branch", default=None,
                     help="restrict to one branch (default: every branch)")
    log.add_argument("-n", type=int, default=20, dest="limit",
                     help="maximum rows to show (default: 20)")

    tri = sub.add_parser(
        "triage",
        help="dismiss a finding with an audited reason, or list a review's findings")
    tri.add_argument("review_id")
    tri.add_argument("finding_index", nargs="?", type=int, default=None)
    tri.add_argument("reason", nargs="?", default=None)
    tri.add_argument("--list", action="store_true", dest="list_only",
                     help="list a review's findings instead of dismissing one")
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


def _fmt_side(row: dict | None) -> str:
    """Render one side of a shadow comparison as `t/H-M-L`, or `-` if absent.

    `effective_trustworthy` -- not the raw `trustworthy` field -- decides the
    `t`/`f`, so a legacy row that predates the field (absent, not `false`)
    displays the same verdict that `shadow.compare` used to decide `match`.
    """
    from .shadow import effective_trustworthy

    if row is None:
        return "-"
    sev = row.get("severity") if isinstance(row.get("severity"), dict) else {}

    def _n(v: object) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    mark = "t" if effective_trustworthy(row) else "f"
    return f"{mark}/{_n(sev.get('high'))}-{_n(sev.get('medium'))}-{_n(sev.get('low'))}"


def _cmd_shadow_compare(args) -> int:
    """Print the shadow-mode comparison table and summary. Always exits 0.

    Shadow mode is purely observational: it exists to show a human whether
    skodun agrees with the legacy tool, and a workflow that happens to run it
    must never be failed by what it finds -- or by it failing to run at all.
    Every failure path below is reported on stdout and still returns 0.
    """
    try:
        from .legacy_import import INDEX_NAME
        from .shadow import compare
        from .store import Store
    except BaseException as e:
        print(f"skodun shadow-compare: could not load the shadow module: {e!r}")
        return 0

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
            print(f"skodun shadow-compare: no archive directory at {archive} "
                  f"-- nothing on the legacy side to compare against")
        elif not (archive / INDEX_NAME).is_file():
            print(f"skodun shadow-compare: no {INDEX_NAME} in {archive} "
                  f"-- nothing on the legacy side to compare against")
    except BaseException:
        pass   # a notice is a courtesy; it may never become the failure itself

    try:
        store = Store.open(_store_path())
        comparisons = compare(store, archive, None)
    except BaseException as e:
        print(f"skodun shadow-compare: FAILED on {archive}: {e!r}")
        return 0

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
        print(f"{c.diff_hash[:12]} | {_fmt_side(c.skodun)} | {_fmt_side(c.legacy)} "
              f"| {label}")

    print(f"shadow: {len(comparisons)} compared, {matched} matched, "
          f"{skodun_only} skodun-only, {legacy_only} legacy-only")
    return 0


def _cmd_log(args) -> int:
    """Print recent reviews, newest first. `2` if the store cannot be read."""
    # `-n` becomes SQLite's LIMIT, where a NEGATIVE value means "no limit" --
    # so `log -n -1` would dump the whole store while reading like a request
    # for fewer rows than the default. Below 1 there is no row count to ask
    # for, so this is a usage error rather than something to clamp silently.
    if args.limit < 1:
        print(f"skodun log: -n must be a positive row count, got {args.limit}")
        return 2
    try:
        from .store import Store
        store = Store.open(_store_path())
        rows = store.list_reviews(args.branch, args.limit)
    except BaseException as e:
        print(f"skodun log: could not read the store: {e!r}")
        return 2

    def _n(v: object) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    for rec in rows:
        trustworthy = rec.get("trustworthy") is True
        sev = rec.get("severity") if isinstance(rec.get("severity"), dict) else {}
        files = rec.get("files_changed")
        nfiles = len(files) if isinstance(files, list) else 0
        # A summary carrying a stray newline must not be able to fake a second
        # row in what is meant to be a one-line-per-review listing.
        summary = str(rec.get("summary") or "").replace("\r", " ").replace("\n", " ")
        mark = "!" if not trustworthy else " "
        print(f"{mark}{rec.get('reviewed_at')} | {rec.get('branch')} | {nfiles} | "
              f"{_n(sev.get('high'))}-{_n(sev.get('medium'))}-{_n(sev.get('low'))} | "
              f"{rec.get('status')} | {summary}")
    return 0


def _cmd_triage(args) -> int:
    """Dismiss one finding with an audited reason, or list a review's findings.

    A rejected reason or a missing/invalid review is reported as a clear
    message and a nonzero exit -- never a traceback -- because both are the
    ordinary shape of "a human needs to try again", not an internal failure.
    """
    from .store import Store

    # `--list` and a dismissal are two different commands sharing one parser,
    # so `triage --list <id> <index> "<reason>"` parses cleanly and then throws
    # the index and the reason away. Someone who typed a reason believes a
    # finding was dismissed; they get a listing and a 0. Reject the mixture
    # instead of picking one of the two meanings.
    if args.list_only and not (args.finding_index is None and args.reason is None):
        print("skodun triage: --list takes only a review id; drop the finding "
              "index and the reason to list, or drop --list to dismiss")
        return 2

    try:
        store = Store.open(_store_path())
    except BaseException as e:
        print(f"skodun triage: could not open the store: {e!r}")
        return 2

    review = store.get_review(args.review_id)
    if review is None:
        print(f"skodun triage: no such review: {args.review_id!r}")
        return 2

    from .textnorm import finding_key
    from .triage import ArtifactError, TriageError, dismiss, load_valid_artifact

    try:
        review = load_valid_artifact(review)
    except ArtifactError as e:
        print(f"skodun triage: invalid review artifact: {e}")
        return 2

    if args.list_only:
        triaged = store.triage_for(review["branch"], review["base_sha"])
        for i, f in enumerate(review["findings"]):
            fkey = finding_key(f.get("file", ""), f.get("title", ""))
            status = "DISMISSED" if fkey in triaged else "OPEN"
            print(f"[{i}] {f.get('severity')} {f.get('file')}:{f.get('line')} "
                  f"{f.get('title')} ({status})")
        return 0

    if args.finding_index is None or args.reason is None:
        print("skodun triage: usage: skodun triage <review-id> <finding-index> "
              "\"<reason>\"  |  skodun triage --list <review-id>")
        return 2

    try:
        dismiss(store, review, args.finding_index, args.reason,
                now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except (TriageError, ArtifactError) as e:
        print(f"skodun triage: rejected: {e}")
        return 2

    print(f"skodun triage: dismissed finding {args.finding_index} on review "
          f"{args.review_id}")
    return 0


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
