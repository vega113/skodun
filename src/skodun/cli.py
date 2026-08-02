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
    `main`'s scoped carve-out); `providers`, a read-only diagnostic
    listing that is never a gate and prints no verdict line on any of its
    exit codes; `dispatch`, which reserves records and starts workers but
    decides nothing about a push (and whose exit code is 0 on EVERY path
    for that reason -- a hook must not block on review machinery);
    `install-hooks`, which writes a file and reports on it; `surface`,
    whose STDOUT IS A PAYLOAD -- a SessionStart hook feeds it to an agent
    verbatim, and under `--hook-format claude` it is a single JSON object,
    so a banner appended to it would corrupt exactly the output that is
    meant to be consumed; and `mcp`, whose stdout is a PROTOCOL -- one
    non-JSON-RPC line there desynchronises the client's parser for the rest
    of the session. Everything `surface` and `mcp` have to say ABOUT
    themselves goes to stderr for the same reason.
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


class _GivenFormat(argparse.Action):
    """`--hook-format`, plus a flag saying it was actually typed.

    argparse cannot otherwise tell `skodun surface` from `skodun surface
    --hook-format text`: both leave `hook_format == "text"`. `_cmd_surface`
    needs the difference, because naming a hook format is how a MACHINE caller
    identifies itself, and one of that command's outputs (the "nothing
    undelivered" note on stderr) exists for a human and is noise for a hook.

    The default STAYS `text` — that is genuinely the format an absent flag
    selects — so nothing about the parsed value changes. Only the extra
    `hook_format_given` boolean is new, and `surf.set_defaults` supplies its
    False.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.hook_format_given = True


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
    # THE NAME of a `[[reviewers]]` entry, never a provider id: two enabled
    # entries may share a provider, and picking one of them by a rule nobody
    # asked about would also pick its model, its effort, its own prompt budget
    # and its fallback chain. A name is the one identifier that says all of
    # those. Its own `fallbacks` still apply -- this narrows where the chain
    # STARTS, not whether it can recover -- and a name that does not resolve is
    # refused by `run_review`'s preflight (exit 2, nothing ran), never
    # downgraded to the config's default.
    review.add_argument("--reviewer", default=None, dest="reviewer",
                        metavar="NAME",
                        help="name of the configured [[reviewers]] entry to "
                             "head this review's chain, instead of the "
                             "config's own 'finder' role (default: the "
                             "config decides)")

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
    # `surface`'s shape exactly (`type=Path, default=None`), because the two
    # surfaces must be able to aim the same scope: a scope the CLI cannot aim is
    # a scope the user cannot inspect. It narrows `--branch` and nothing else --
    # `Store.list_reviews`'s own contract, said here so the help text and the
    # query cannot drift apart.
    log.add_argument("--repo", type=Path, default=None,
                     help="narrow --branch to one repository (default: the "
                          "current directory); ignored without --branch")
    log.add_argument("-n", type=int, default=20, dest="limit",
                     help="maximum rows to show (default: 20)")

    dfr = sub.add_parser(
        "deferrals",
        help="list every finding still standing as DEFERRED, across all reviews")
    dfr.add_argument("-n", type=int, default=50, dest="limit",
                     help="maximum rows to show, newest first (default: 50)")

    tri = sub.add_parser(
        "triage",
        help="dismiss, defer or reopen a finding with an audited reason, or "
             "list a review's findings")
    tri.add_argument("review_id")
    tri.add_argument("finding_index", nargs="?", type=int, default=None)
    tri.add_argument("reason", nargs="?", default=None)
    # THE FOURTH POSITIONAL, and it exists only for `--defer`, whose argv is
    # `<review-id> <finding-index> <tracking-ref> "<reason>"`. argparse fills
    # optional positionals left to right, so under `--defer` the third slot
    # (`reason`) carries the TRACKING REFERENCE and this one carries the reason
    # -- see `_cmd_triage`, which is the only place that mapping is applied.
    # Naming it after its one use rather than after its position keeps every
    # other mode's `args.reason` meaning exactly what it always meant, and
    # `_cmd_triage` refuses a fourth positional on any other mode rather than
    # discarding it.
    tri.add_argument("defer_reason", nargs="?", default=None,
                     help="with --defer ONLY: the audited reason, which follows "
                          "the tracking reference")
    tri.add_argument("--list", action="store_true", dest="list_only",
                     help="list a review's findings instead of dismissing one")
    # The third verb. It CLEARS the gate exactly as a dismissal does, and the
    # only thing that keeps that honest is the mandatory tracking reference --
    # a deferral nobody filed and an ignored finding are the same artifact, so
    # the reference is validated at the same door the reason's audit floor is.
    tri.add_argument("--defer", action="store_true", dest="defer",
                     help="defer ONE finding to a MANDATORY tracking reference "
                          "(an issue number, a tracker key or a URL) with an "
                          "audited reason: the finding is real, it is not "
                          "blast-radius for this change, and the work is filed")
    # The audited un-dismissal. It takes a reason of its own -- and the same
    # reason floor a dismissal clears -- because it moves the gate from 0 back
    # to 1, and nothing may do that silently. Append-only: the dismissal it
    # overturns stays in the ledger with its own reason.
    tri.add_argument("--reopen", action="store_true", dest="reopen",
                     help="reopen ONE previously dismissed finding, with an "
                          "audited reason for overturning the dismissal")
    # --- the pre-push surfaces -------------------------------------------
    disp = sub.add_parser(
        "dispatch",
        help="reserve and dispatch background reviews for a push (pre-push hook)")
    disp.add_argument("--repo", type=Path, default=Path("."),
                          help="repository being pushed (default: the current "
                               "directory)")
    # Git's standard pre-push argv. Accepted and recorded into failure notes,
    # otherwise unused -- WITHOUT them argparse would reject the installed shim's
    # own invocation, which passes `"$@"` through verbatim. `nargs="?"` twice
    # rather than `nargs="*"` so a third positional is still a usage error.
    disp.add_argument("remote_name", nargs="?", default="",
                      help="the remote's name, as git passes it")
    disp.add_argument("remote_url", nargs="?", default="",
                      help="the remote's URL, as git passes it")

    # HIDDEN: `skodun worker` is the detached process `dispatch` spawns, not a
    # command a human runs. Omitting `help=` entirely is what hides it -- argparse
    # only lists a subcommand's DESCRIPTION when one was given, so there is no
    # `worker` line among the commands. It stays fully usable (and debuggable) by
    # name. `help=argparse.SUPPRESS` would NOT work: argparse renders subaction
    # help verbatim and the literal `==SUPPRESS==` appears in the listing.
    #
    # The choices metavar (`{gate,review,...}`) still names it, and deliberately:
    # argparse builds that from the real command list, and overriding it with a
    # hand-written `metavar` would be a second list to keep in sync -- one that
    # would silently start lying the next time a command is added.
    worker = sub.add_parser("worker")
    for flag, helptext in (
            ("--record-id", "the reservation this worker must finalize"),
            ("--repo", "the repository holding the pushed ref"),
            ("--branch", "the pushed branch's short name"),
            ("--local-oid", "the pushed commit"),
            ("--base-sha", "the base the diff is computed against"),
            ("--base-ref", "the base's ref name, as recorded")):
        worker.add_argument(flag, required=flag != "--base-ref", default="",
                            help=helptext)

    # The delivery surface. `--hook-format`'s choices are spelled LITERALLY here
    # rather than imported from `delivery.FORMATS`: `build_parser` runs for every
    # invocation of every subcommand, and this module's whole import discipline is
    # that no command pays for another command's module graph (nor inherits its
    # import failures). `test_the_hook_format_choices_are_the_delivery_modules_own`
    # pins the two spellings against each other so they cannot drift.
    surf = sub.add_parser(
        "surface",
        help="report background review rounds nobody has been shown yet")
    # BRANCH DISCOVERY ONLY, and `default=None` rather than `Path(".")` on
    # purpose: `services.resolve_surface_branch` already takes a repo (the MCP
    # `surface` tool has passed one since Task 13), and its own default is what
    # an ABSENT argument means on both surfaces -- one definition of "here",
    # not two. The store is untouched by this flag: which store to read is
    # `SKODUN_DB`, an operational choice (see the README's one-store-per-
    # repository note), and a reporting flag may not make it for the user.
    surf.add_argument("--repo", type=Path, default=None,
                      help="repository whose checked-out branch to report on "
                           "(default: the current directory); --branch "
                           "overrides it")
    surf.add_argument("--branch", default=None,
                      help="branch to report on (default: the checked-out one)")
    # `_GivenFormat` rather than a `default=None` sentinel: the default really is
    # `text` (that is the format an absent flag SELECTS, and
    # `test_the_hook_format_choices_are_the_delivery_modules_own` pins it against
    # `delivery.TEXT`), and what `_cmd_surface` additionally needs to know is
    # whether the flag was TYPED -- a caller that names a hook format is a
    # machine, and the "nothing undelivered" note is written for a human.
    surf.add_argument("--hook-format", default="text", dest="hook_format",
                      action=_GivenFormat, choices=("text", "claude"),
                      help="`text` for plain lines, `claude` for the SessionStart "
                           "JSON envelope (default: text)")
    surf.set_defaults(hook_format_given=False)
    surf.add_argument("--include-delivered", action="store_true",
                      dest="include_delivered",
                      help="replay rounds that were already delivered too")

    # The agent surface. NO options, deliberately: every tool carries its own
    # arguments in its `inputSchema`, so a flag here would be a second place
    # configuration could live -- and one this command would have to ignore for
    # every tool that does not use it.
    sub.add_parser(
        "mcp",
        help="serve the review loop to agents over stdio (MCP JSON-RPC)")

    hooks = sub.add_parser(
        "install-hooks",
        help="install the pre-push shim, chaining any hook already there")
    hooks.add_argument("--repo", type=Path, default=Path("."),
                       help="repository to install into (default: the current "
                            "directory)")
    hooks.add_argument("--force", action="store_true",
                       help="back up a foreign pre-push hook and chain it "
                            "(never discards it)")
    retain = sub.add_parser(
        "retain",
        help="prune worker logs per [retention] (never deletes gate artifacts)")
    retain.add_argument("--repo", type=Path, default=Path("."),
                        help="repository whose config is loaded (default: .)")
    retain.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted without deleting")
    doctor = sub.add_parser(
        "doctor",
        help="diagnose install, store, adapters, and MCP readiness (read-only)")
    doctor.add_argument("--repo", type=Path, default=Path("."),
                        help="repository whose config is loaded (default: .)")
    sched = sub.add_parser(
        "schedule",
        help="generate launchd plists from [schedule] (macOS; not inside MCP)")
    sched_sub = sched.add_subparsers(dest="schedule_command", required=True)
    sched_install = sched_sub.add_parser(
        "install",
        help="write launchd plists for configured jobs")
    sched_install.add_argument(
        "--repo", type=Path, default=Path("."),
        help="repository whose .skodun.toml [schedule] is loaded")
    sched_install.add_argument(
        "--dest", type=Path, default=None,
        help="directory for plists (default: ~/Library/LaunchAgents)")
    sched_install.add_argument(
        "--force-platform", action="store_true",
        help="write plists even off macOS (for tests/CI; launchd will not run them)")
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

    It STAYS in this module even though its callers moved into `services.py`
    (which imports it from here, lazily), because `cli._repo_root` is named as
    the definition by `dispatch.py`'s own docstring and by the tests -- and
    `providers`, which is a CLI-only diagnostic and not a service, still needs
    it. One definition, wherever the readers already look for it.
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


def _warn(message: str, code: int) -> int:
    """Say something on STDERR and return `code`. Never raises, never touches
    stdout.

    `_emit`'s sibling for the one command whose stdout is a PAYLOAD rather than a
    verdict: a note about the surface itself, written onto the stream a hook
    consumes, would corrupt the report -- and under `--hook-format claude` it
    would corrupt a JSON document. stderr is also the only stream left when the
    reason for the message is that stdout is dead.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except BaseException:
        pass        # a diagnostic may never become the failure itself
    return code


def _emit_delivery(text: str) -> bool:
    """Write a delivery payload to stdout and REPORT whether it landed.

    The shipped `_emit` SWALLOWS a write failure, because there the exit code is
    the product and the printed line is a courtesy. Here it is the other way
    round: the payload IS the product, and the acknowledgement that follows is
    only allowed to happen if this returned True. So every failure is reported
    rather than absorbed.

    Buffering is never success: the flush is inside the guard, because bytes
    sitting in a buffer have not reached a reader and a flush at interpreter exit
    would fail where nobody can act on it.

    A `UnicodeEncodeError` gets the same lossy retry `_emit` gives it, and
    counts as DELIVERED when the retry succeeds. That is deliberate: the stream is
    alive and only this text's characters do not fit its encoding -- the reserved
    no-review line carries an em dash, and an ASCII-only locale is exactly where
    it will meet a stream that cannot encode it. `backslashreplace` keeps every
    character accounted for, so the reader gets the whole report; treating that as
    a failed delivery would instead repeat the same report at every session start
    forever on such a machine.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
        return True
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            # Round-tripped through the stream's own encoding with a lossy error
            # handler, so the retry is made only of characters that encoding can
            # represent and cannot fail the same way twice.
            lossy = text.encode(encoding, errors="backslashreplace").decode(encoding)
            sys.stdout.write(lossy)
            sys.stdout.flush()
            return True
        except BaseException:
            _blackhole_stdout()
            return False
    except BaseException:
        # A dead stream (broken pipe, closed fd, full disk). Redirect it at
        # devnull before returning: CPython flushes stdout again during
        # finalization and a failure there is reported as exit status 120, which
        # is not in this command's contract either.
        _blackhole_stdout()
        return False


def _cmd_gate(args) -> int:
    """Parse, open the store, ask `services.svc_gate`, print, return its code.

    Every failure inside this seam is exit 2, for the same reason every failure
    inside `run_gate` is: an exception escaping here would leave the
    interpreter's own exit code of 1, and 1 is the one value that means
    "findings remain open".

    The imports sit outside every guard, deliberately: an unimportable
    `gate.py`/`services.py` is a broken installation, and `main`'s general
    handler (which reports 2) is where that belongs -- pinned by
    `test_gate_import_keyboard_interrupt_maps_to_2_via_mains_general_handler`.
    """
    from .services import svc_gate
    from .store import Store

    # The store is opened FIRST so that a setup failure inside the service still
    # has somewhere to be recorded. THIS failure cannot be: there is no store
    # yet, so returning 2 with no `gate_events` row is the only option available
    # -- and it is the safe one, because an unrecordable refusal is still a
    # refusal. (Only a lenient verdict needs its record to be trustworthy.)
    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"SKODUN GATE: FAIL(2) could not open the store: {e!r}", 2)

    with store:
        code, message = svc_gate(store, Path(args.repo))
    return _emit(message, code)


def _cmd_review(args) -> int:
    """Run one foreground review. Exit codes, and why they are these:

      0  trustworthy and clean            3  gave up waiting for the lock
      1  trustworthy, findings open       4  no trustworthy review exists
      2  preflight refusal (nothing ran)

    THIS seam owns three things and nothing else: the store's lifetime, the
    verdict line on stdout, and the exit code. The decision -- and the banner
    text, rendered by `trust.banner` from the record that was persisted -- comes
    from `services.svc_review`, which the MCP `review` tool calls identically.
    `run_review` itself prints nothing at all: its stdout would be an MCP
    transport's JSON-RPC stream.

    The banner invariant holds through every path: `svc_review` returns a
    `banner_failure` line for every outcome that never reached a record, and the
    two failures ABOVE it (an unimportable service, an unopenable store) carry
    one from here.
    """
    # Outside the guard below on purpose: it is what RENDERS the banner, so a
    # failure to import it is the one import failure no banner can report.
    from .trust import banner_failure

    try:
        # Inside the guard: an import error here -- a partial install, a syntax
        # error introduced in `services.py`, a missing stdlib module in a
        # stripped environment -- used to escape to `main`, which reports on
        # stderr and leaves stdout without the verdict line the contract
        # promises. 2, not 4: nothing ran, so this is a refusal, not a review
        # that came back badly.
        from .services import svc_review
        from .store import Store
    except KeyboardInterrupt:
        # Ctrl-C during the import itself: nothing ran, but that is not what
        # this is -- `main()` maps this to 130, not to the 2 the `except
        # BaseException` immediately below would otherwise give it.
        raise
    except BaseException as e:
        return _emit(banner_failure(
            f"could not load the review pipeline: {e!r}; no review ran"), 2)

    try:
        store = Store.open(_store_path())
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # No store means no record, which is exactly what 4 says.
        return _emit(banner_failure(f"could not open the review store: {e!r}"), 4)

    with store:
        # `svc_review` re-raises `KeyboardInterrupt` past every one of its own
        # guards so `main`'s carve-out can report 130; the `with` closes the
        # store on the way past.
        # `args.reviewer` reaches the service UNCHECKED, and that is the point:
        # whether a name resolves is a question about the loaded config, which
        # this seam has not read, and the MCP tool must be refused in the same
        # words. `getattr` because `_cmd_review` is called by name in the suite
        # with hand-built argument objects that predate the flag.
        code, text = svc_review(store, Path(args.repo),
                                reviewer=getattr(args, "reviewer", None))
    return _emit(text, code)


def _cmd_dispatch(args) -> int:
    """Dispatch background reviews for one push. ALWAYS 0.

    A pre-push hook must never block on review machinery, so this seam has no
    failure exit code at all: every failure is a loud stderr line plus (wherever a
    store can be reached) a durable `failed` review record, which is what Task
    12's delivery surfaces. `dispatch`'s only non-zero exits are argparse's usage
    errors, which never reach here -- and the installed shim absorbs even those
    into its own warn-and-exit-0, so a human's typo stays loud while a hook's call
    cannot break a push.

    No verdict banner, deliberately, and it is the second command with that
    property (`providers` is the first): this gates nothing. It reserves records
    and starts workers, and the verdict about a push comes from `skodun gate`
    reading what those workers recorded.
    """
    try:
        from . import dispatch as dispatch_mod
        # Read stdin ONCE and completely, before anything else can consume it.
        # `errors="replace"` because a ref line is oids and ref names -- ASCII by
        # git's own rules -- so undecodable bytes are corruption, and
        # `parse_ref_lines` classifies the result as malformed rather than
        # raising here where nothing could be recorded.
        try:
            raw = sys.stdin.buffer.read()
        except BaseException:
            raw = b""
        return dispatch_mod.run_dispatch(
            raw.decode("utf-8", "replace"), Path(args.repo), _store_path(),
            remote_name=getattr(args, "remote_name", "") or "",
            remote_url=getattr(args, "remote_url", "") or "")
    except BaseException as e:
        # The outermost guard of the exit-0 contract. Even an import failure or a
        # `MemoryError` here must not fail the push.
        try:
            print(f"skodun: the pre-push dispatcher failed ({e!r}); the push is "
                  f"NOT blocked", file=sys.stderr, flush=True)
        except BaseException:
            pass
        return 0


def _cmd_worker(args) -> int:
    """Run one detached background review. Exit codes:

      0  the reservation reached a terminal state (reviewed, cancelled, or
         already retired by a newer push)
      2  the worker could not do its job at all: no store, or no such reservation

    Nothing consumes this code in production -- the dispatcher does not wait --
    but it is the difference between a log entry that says "nothing to do" and one
    that says "misconfigured", and the seam matrix pins it.
    """
    try:
        from . import dispatch as dispatch_mod
    except BaseException as e:
        from .trust import banner_failure
        return _emit(banner_failure(
            f"the review worker could not be loaded: {e!r}"), 2)
    if not args.record_id:
        from .trust import banner_failure
        return _emit(banner_failure("worker: --record-id is required"), 2)
    try:
        outcome = dispatch_mod.run_worker(
            args.record_id, Path(args.repo or "."), args.branch,
            args.local_oid, args.base_sha, args.base_ref, _store_path())
    except BaseException as e:
        # `run_worker` promises never to raise; this is the belt for that braces.
        from .trust import banner_failure
        return _emit(banner_failure(f"the review worker crashed: {e!r}"), 2)
    return _emit(outcome.message, outcome.code)


def _cmd_surface(args) -> int:
    """Deliver the background review rounds nobody has been shown. Exit codes:

      0  the report reached stdout and the ledger recorded it (or there was
         nothing to report)
      2  it did not: no store, no branch, no readable repository to scope the
         rounds to, an unwritable report, or an unrecordable delivery

    There is no code for "there were findings", deliberately: this command
    delivers history and certifies nothing about the working tree, so a hook
    calling it must not be able to read a verdict out of its status. `skodun
    gate` is the only thing that answers that question.

    THE ORDER BELOW IS THE PRODUCT. `services.svc_surface` renders and
    acknowledges only the QUIET rounds (nothing deliverable can be lost by
    marking a trustworthy zero-finding round now); everything with content is
    acknowledged HERE, after `_emit_delivery` has confirmed the write AND the
    flush. Marking first is the mutation that passes every other test in this
    file: a report dropped on the way out would be recorded as delivered and
    never shown again, which is precisely the undelivered-findings failure this
    command exists to fix, reintroduced by the fix.

    A failed emit therefore leaves the rounds undelivered and says so on stderr.
    So does a failed ACK -- but that one has already reached the reader, so the
    cost is a repeat rather than a loss. Delivered-twice is the designed failure
    mode in both directions.

    THE ACK CHANNEL IS THIS TRANSPORT'S OWN (`cli-text`/`cli-claude`), because
    only whoever performed the write knows whether it landed. The MCP `surface`
    tool acknowledges the same ids under `mcp`, after ITS response line is
    flushed, from a fresh Store -- same discipline, different channel.
    """
    try:
        from . import delivery, gitio, services
        from .store import Store
    except BaseException as e:
        return _warn(f"skodun surface: could not load the delivery surface: {e!r}",
                     2)

    fmt = args.hook_format
    # `--branch` beats `--repo` (`resolve_surface_branch` returns it untouched
    # before any git call), and a `--repo` git cannot read is a REFUSAL, never a
    # quiet fall back to the cwd: reporting a different repository's branch
    # because the named one could not be read would deliver -- and permanently
    # mark delivered -- rounds the caller never asked for.
    branch, why_not = services.resolve_surface_branch(
        args.branch, args.repo if args.repo is not None else ".")
    if not branch:
        return _warn(why_not, 2)

    # The repo the ROWS are scoped by, resolved from the SAME argument
    # `resolve_surface_branch` just used. NEVER `Path(".")`: with a hardcoded
    # cwd, `skodun surface --repo /other` would deliver AND permanently
    # acknowledge the CWD repository's rounds -- a fresh instance of the defect
    # this phase closes. A repo git cannot read is a refusal, exactly as it
    # already is for the branch -- including when `--branch` was given, because
    # there is no repository to scope the rows to and guessing one is the whole
    # bug.
    try:
        repo = str(gitio.git_common_dir(
            args.repo if args.repo is not None else Path(".")))
    except BaseException as e:
        return _warn(f"skodun surface: could not resolve the repository to "
                     f"report on: {e!r}", 2)

    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _warn(f"skodun surface: could not open the store: {e!r}", 2)

    with store:
        status, text, pending = services.svc_surface(
            store, branch, repo, fmt, bool(args.include_delivered))
        if status != 0:
            # `text` is a diagnostic, not a payload: onto stderr with it, where a
            # hook consuming stdout cannot mistake it for a report.
            return _warn(text, status)

        if not text:
            # Silence on stdout, on purpose: a hook that injects an empty report
            # at every session start is noise. The human at the terminal still
            # gets an answer, on the stream a hook does not read.
            #
            # ...unless the caller named a `--hook-format`, in which case it IS
            # the hook and stderr is not private either. The plain-text session
            # template under `examples/hooks/` runs `"$@" surface --hook-format
            # text || true` with stderr unredirected, so this note printed a
            # line into every quiet shell start -- the kind of noise that gets a
            # profile snippet deleted, taking the delivery of every future
            # finding with it. A HUMAN typing `skodun surface` and getting
            # silence still needs to be told which silence it was, so the
            # note is suppressed only for the machine caller. Every real
            # FAILURE below is unaffected and still reaches stderr in both
            # cases: it is the reason the hook printed nothing, and an operator
            # who never learns that has a hook that has quietly reported
            # nothing for weeks.
            if args.hook_format_given:
                return 0
            return _warn(services.surface_no_rounds_note(branch), 0)

        if not _emit_delivery(text):
            return _warn(
                f"skodun surface: the report could not be written to stdout; "
                f"{len(pending)} round(s) stay UNDELIVERED and will be reported "
                f"again", 2)

        if not pending:
            # Reachable only through `--include-delivered`, whose replay may
            # render rounds that are all already in the ledger.
            return 0
        try:
            delivery.acknowledge(store, pending, delivery.channel_for_format(fmt))
        except BaseException as e:
            return _warn(
                f"skodun surface: the report above was delivered but could not be "
                f"recorded ({e!r}); {len(pending)} round(s) will be reported "
                f"again", 2)
        return 0


def _cmd_mcp(args) -> int:
    """Serve MCP on stdio. Exit codes:

      0  the session ended -- the client closed stdin, or went away
      2  there was no session: the server could not be loaded or started

    Exactly two, and no verdict banner: this command's STDOUT IS A PROTOCOL
    STREAM, so every diagnostic goes to stderr (`_warn`), and "the client
    disconnected" is not a failure -- a non-zero exit is how MCP client
    harnesses report a crashed server, which is a different thing that must stay
    distinguishable. There is deliberately no "there were findings" code either:
    a gate verdict belongs to the `gate` TOOL's result, not to the lifetime of
    the transport that carried it.

    `_blackhole_stdout` is handed to the server for the dead-reader case:
    CPython flushes stdout again during finalisation, and a failure there is
    reported as exit status 120, which is not in this contract either.
    """
    try:
        from . import mcpserver
    except BaseException as e:
        return _warn(f"skodun mcp: could not load the MCP server: {e!r}", 2)
    try:
        return mcpserver.serve_stdio(on_stdout_lost=_blackhole_stdout)
    except BaseException as e:
        # `serve_stdio` itself is guarded end to end; reaching here means the
        # process had no usable stdio to serve on in the first place.
        return _warn(f"skodun mcp: could not serve on stdio: {e!r}", 2)


def _cmd_install_hooks(args) -> int:
    """Install the pre-push shim. 0 installed, 1 refused, 2 could not even look.

    1 and 2 are different answers a script may want to act on: 1 is "there is a
    hook here that is not mine and you have to decide" (re-runnable with `--force`,
    or after moving the file), while 2 is "this is not a repository I can install
    into at all".
    """
    try:
        from . import dispatch as dispatch_mod
        from .dispatch import HookRefused
    except BaseException as e:
        return _emit(f"skodun install-hooks: could not load the installer: {e!r}",
                     2)
    try:
        path, what = dispatch_mod.install_hooks(Path(args.repo),
                                                force=bool(args.force))
    except HookRefused as e:
        return _emit(f"skodun install-hooks: refused -- {e}", 1)
    except BaseException as e:
        return _emit(f"skodun install-hooks: {e}", 2)
    return _emit(f"skodun install-hooks: {path} {what}", 0)


def _cmd_schedule(args) -> int:
    """Write launchd plists from `[schedule]`. Never starts a scheduler in-process."""
    if getattr(args, "schedule_command", None) != "install":
        return _emit("skodun schedule: unknown subcommand", 2)
    try:
        from .config import load_config
        from .schedule import install_schedule
    except BaseException as e:
        return _emit(f"skodun schedule: could not load: {e!r}", 2)
    try:
        root = _repo_root(Path(args.repo))
    except BaseException as e:
        return _emit(f"skodun schedule: {e}", 2)
    try:
        cfg = load_config(root)
    except BaseException as e:
        return _emit(f"skodun schedule: config error: {e}", 2)
    if not cfg.schedule_jobs:
        return _emit(
            "skodun schedule install: no [[schedule.jobs]] configured; "
            "nothing written",
            0,
        )
    dest = args.dest
    if dest is None:
        dest = Path.home() / "Library" / "LaunchAgents"
    try:
        result = install_schedule(
            cfg.schedule_jobs,
            Path(dest),
            require_darwin=not bool(args.force_platform),
        )
    except BaseException as e:
        return _emit(f"skodun schedule install: {e}", 2)
    lines = [
        f"skodun schedule install: wrote {len(result.written)} plist(s) under {dest}",
        "Load with: launchctl bootstrap gui/$(id -u) <plist>  (or load -w on older macOS)",
        "No scheduler runs inside `skodun mcp`.",
    ]
    for p, label in zip(result.written, result.labels):
        lines.append(f"  {label} -> {p}")
    return _emit("\n".join(lines), 0)


def _cmd_doctor(args) -> int:
    """Read-only install/MCP readiness report. Does not mutate the store.

    Exit 0 all checks ok · 1 problems found · 2 doctor failed to run.
    CLI-only for epic #23: agents can shell out; MCP tool deferred (comment on #23).
    """
    try:
        from .doctor import run_doctor
    except BaseException as e:
        return _emit(f"skodun doctor: could not load doctor: {e!r}", 2)
    try:
        report = run_doctor(
            repo=Path(args.repo),
            store_path=_store_path(),
        )
    except BaseException as e:
        return _emit(f"skodun doctor: {e!r}", 2)
    return _emit(report.render(), report.exit_code)


def _cmd_retain(args) -> int:
    """Prune worker logs per `[retention]`. Never mutates gate artifacts.

    Exit 0 on a successful pass (including dry-run and nothing-to-do). Exit 2
    when the store/config/log dir cannot be used. Partial delete errors are
    reported and still exit 2 so a schedule job notices.
    """
    try:
        from .config import load_config
        from .retention import retain_worker_logs
        from .store import Store
    except BaseException as e:
        return _emit(f"skodun retain: could not load retention: {e!r}", 2)
    try:
        root = _repo_root(Path(args.repo))
    except BaseException as e:
        return _emit(f"skodun retain: {e}", 2)
    try:
        cfg = load_config(root)
    except BaseException as e:
        return _emit(f"skodun retain: config error: {e}", 2)
    try:
        with Store.open(_store_path()) as store:
            log_dir = store.log_dir()
            # Snapshot that gate artifacts still exist after prune: we never
            # open or delete review rows here; this only proves the store path
            # is the real one and remains usable.
            report = retain_worker_logs(
                log_dir,
                max_age_days=cfg.retention.worker_log_max_age_days,
                max_count=cfg.retention.worker_log_max_count,
                dry_run=bool(args.dry_run),
            )
    except BaseException as e:
        return _emit(f"skodun retain: {e}", 2)
    mode = "dry-run" if report.dry_run else "deleted"
    count = report.would_delete if report.dry_run else report.deleted_count
    lines = [
        f"skodun retain: {mode} {count} worker log(s) under {log_dir}",
        f"  policy: max_age_days={cfg.retention.worker_log_max_age_days} "
        f"max_count={cfg.retention.worker_log_max_count}",
    ]
    for p in report.candidates[:20]:
        lines.append(f"  - {p.name}")
    if len(report.candidates) > 20:
        lines.append(f"  ... and {len(report.candidates) - 20} more")
    if report.errors:
        for path, err in report.errors:
            lines.append(f"  error {path.name}: {err}")
        return _emit("\n".join(lines), 2)
    return _emit("\n".join(lines), 0)


def _fmt_binary(binary: str) -> str:
    """A short word for whether `resolve_binary()`'s answer names something
    this machine can run right now: `"executable"`, `"found, NOT executable"`,
    or `"NOT FOUND"`.

    A path-shaped value (the per-adapter `SKODUN_<X>_BIN` overrides, and
    grok's own `~/.grok/bin/grok` default) is checked directly; a bare name
    goes through `PATH`, exactly how the adapter's own `Popen` call would
    resolve it -- `shutil.which` already requires `os.X_OK` along the way, so
    a match there is never merely "a file exists". The path-vs-PATH split
    itself is `runner._is_path_shaped`, imported rather than re-inlined, so
    it stays the ONE definition `chain._binary_is_absent` also uses; this
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
    # subcommand from paying an import it does not need -- the same reasoning
    # `_cmd_review` already applies to its own `pipeline` import.
    #
    # From `runner`, NOT from `pipeline`, and that is the point rather than an
    # incidental tidy-up. `runner` imports nothing from the package; `pipeline`
    # pulls in the whole review graph. Reaching through `pipeline` for one
    # string predicate made `skodun providers` -- the read-only diagnostic an
    # operator runs when a review will not start -- fail on exactly the
    # installations it exists to diagnose.
    from .runner import _is_path_shaped

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

    The likeliest way to run this is `skodun log | head`, so the listing goes
    through `_emit` rather than a bare `print`: an early-exiting reader closes
    the pipe partway through it, and a `BrokenPipeError` escaping would hand the
    shell the interpreter's own exit code of 1 -- a value this command's contract
    does not even have. The exit code returned is always the one the contract
    promises for the outcome the service already decided (0 or 2), never whatever
    printing happened to return.

    An empty store prints NOTHING and exits 0: `svc_log` returns `""`, and a
    blank line is not an empty listing.

    `--repo` narrows `--branch` and is resolved ONLY when one is given, because
    `gitio.git_common_dir` shells out to git and `skodun log` with no branch has
    always been runnable from anywhere. A repository git cannot read is a
    refusal (2) with a message, never a traceback.
    """
    from .services import svc_log

    repo = None
    if args.branch is not None:
        # ONLY with a branch, and wrapped. `git_common_dir` shells out to git
        # and raises outside a repository -- and `skodun log` with no branch
        # running from anywhere is this command's contract, not an accident.
        from . import gitio
        try:
            repo = str(gitio.git_common_dir(
                args.repo if args.repo is not None else Path(".")))
        except BaseException as e:
            return _emit(f"skodun log: could not resolve the repository for "
                         f"--branch: {e!r}", 2)
    try:
        from .store import Store
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun log: could not read the store: {e!r}", 2)
    with store:
        code, text = svc_log(store, args.branch, args.limit, repo)
    return _emit(text, code) if text else code


def _cmd_deferrals(args) -> int:
    """Print every finding still standing as DEFERRED. `2` if unreadable.

    Its own subcommand rather than a flag on `log` or `triage`, for a reason
    about SHAPE: `log` lists reviews one row each and a deferral is a finding
    inside one, while `triage` takes a review id as a required positional --
    and the question this answers ("what has this project deferred, and where
    is it filed") is precisely the one whose subject is not a review anybody
    already has in mind. A deferral filed three branches ago is the one that
    rots, so the listing has no scope at all.

    An empty ledger prints NOTHING on stdout and says so on stderr, exit 0 --
    `surface`'s convention, and for its reason: stdout here is a listing
    something may count or pipe, and a note injected into it would be a row.
    """
    from .services import svc_deferrals

    try:
        from .store import Store
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun deferrals: could not read the store: {e!r}", 2)
    with store:
        code, text = svc_deferrals(store, args.limit)
    if text:
        return _emit(text, code)
    # `_warn`, not `_emit`: an empty listing is an answer, and a note printed
    # onto stdout would be a row in something a caller may be counting.
    return _warn("skodun deferrals: no open deferrals", code) if code == 0 else code


def _cmd_triage(args) -> int:
    """Dismiss, defer or reopen one finding with an audited reason, or list.

    A rejected reason or a missing/invalid review is reported as a clear
    message and a nonzero exit -- never a traceback -- because both are the
    ordinary shape of "a human needs to try again", not an internal failure.
    `--list` in particular is routinely piped to `head` or `grep -q`, so every
    message here goes through `_emit`: a bare `print` meeting a closed pipe
    raises `BrokenPipeError`, and letting that escape would hand the shell the
    interpreter's own exit code of 1 -- indistinguishable from a real error
    about a decision that in fact was never even reached.

    `--adopt-refuter`, `--reopen` and `--defer` share one exit contract, and the
    split is the point:

      0  the decision was recorded
      1  REFUSED -- the finding is right there and the decision was declined
         (for `--adopt-refuter`: a wrong verdict, thin reasoning, a reasoning
         that fails the audit floor, an annotation that cannot say who
         answered; for `--reopen`: a reason that fails the audit floor, or a
         finding that is not dismissed or deferred and so has nothing to
         overturn; for `--defer`: a reason that fails the audit floor, or a
         tracking reference nobody could look up)
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
    from .services import (TRIAGE_ADOPT_USAGE, TRIAGE_DEFER_USAGE,
                           TRIAGE_REOPEN_USAGE, svc_adopt_refuter,
                           svc_triage_defer, svc_triage_dismiss,
                           svc_triage_list, svc_triage_reopen)
    from .store import Store

    # ARGPARSE-SHAPED MISUSE IS DECIDED HERE, before a store is opened, because
    # it is a question about argv rather than about the ledger. `--list`,
    # `--adopt-refuter` and `--reopen` are different commands sharing one parser,
    # so `triage --list <id> <index> "<reason>"` parses cleanly and then throws
    # the index and the reason away. Someone who typed a reason believes a
    # finding was dismissed; they get a listing and a 0. Reject every mixture
    # instead of picking one of the meanings.
    #
    # The MCP surface cannot reach any of these: each mode is its OWN tool there,
    # with its own `inputSchema`, so a mixture is unrepresentable. What the two
    # surfaces DO share -- a missing index, a missing reason -- is refused with
    # the same words, from the `services` constants imported above.
    modes = [name for name, on in (("--list", args.list_only),
                                   ("--adopt-refuter", args.adopt_refuter),
                                   ("--reopen", args.reopen),
                                   ("--defer", args.defer)) if on]
    if len(modes) > 1:
        return _emit(
            f"skodun triage: {modes[0]} and {modes[1]} are two different "
            "commands; pick one", 2)
    # THE FOURTH POSITIONAL BELONGS TO `--defer` ALONE. Only that mode's argv
    # has four, so anywhere else it is an argument argparse would silently throw
    # away -- and its author, having typed a reference before their reason,
    # would have every right to believe a deferral was recorded. Refused before
    # the store opens, with the flag that WOULD have accepted it named.
    if not args.defer and args.defer_reason is not None:
        return _emit(
            "skodun triage: that is one argument too many. Only --defer takes a "
            "tracking reference before the reason: skodun triage --defer "
            "<review-id> <finding-index> <tracking-ref> \"<reason>\"", 2)
    if args.list_only and not (args.finding_index is None and args.reason is None):
        return _emit(
            "skodun triage: --list takes only a review id; drop the finding "
            "index and the reason to list, or drop --list to dismiss", 2)
    if args.reopen and (args.finding_index is None or args.reason is None):
        return _emit(TRIAGE_REOPEN_USAGE, 2)
    if args.defer and (args.finding_index is None or args.reason is None
                       or args.defer_reason is None):
        # All three, and the reference is the one most likely to be left
        # out: it is the argument a plain dismissal does not take.
        # `TRIAGE_DEFER_USAGE` says so, and the MCP tool refuses the same
        # absence with the same words -- the service owns them.
        return _emit(TRIAGE_DEFER_USAGE, 2)
    if args.adopt_refuter:
        # Same class of mixture, and the same refusal to guess. The reason is
        # SYNTHESIZED from the annotation, so a reason typed alongside the flag
        # is silently discarded -- and its author would have every right to
        # believe their words were the ones recorded in the ledger. There is no
        # `reason` argument on the MCP tool at all, so this refusal has no
        # counterpart to stay in step with.
        if args.reason is not None:
            return _emit(
                "skodun triage: --adopt-refuter takes only a review id and a "
                "finding index; the reason comes from the refuter's own "
                "annotation, so drop yours or drop the flag", 2)
        if args.finding_index is None:
            return _emit(TRIAGE_ADOPT_USAGE, 2)

    try:
        store = Store.open(_store_path())
    except BaseException as e:
        return _emit(f"skodun triage: could not open the store: {e!r}", 2)

    with store:
        if args.list_only:
            code, text = svc_triage_list(store, args.review_id)
        elif args.reopen:
            code, text = svc_triage_reopen(store, args.review_id,
                                           args.finding_index, args.reason)
        elif args.defer:
            # THE POSITIONAL REMAPPING, in the one place it happens: under
            # `--defer` the third positional is the TRACKING REFERENCE and the
            # fourth is the reason (see `build_parser`). Spelled out at the call
            # rather than by renaming the attributes, so nothing else in this
            # function has to know that `args.reason` ever means anything else.
            code, text = svc_triage_defer(store, args.review_id,
                                          args.finding_index,
                                          args.reason,        # <tracking-ref>
                                          args.defer_reason)  # "<reason>"
        elif args.adopt_refuter:
            code, text = svc_adopt_refuter(store, args.review_id,
                                           args.finding_index)
        else:
            code, text = svc_triage_dismiss(store, args.review_id,
                                            args.finding_index, args.reason)
    # A review with no findings lists nothing and exits 0; a blank line is not
    # an empty listing.
    return _emit(text, code) if text else code


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
        if args.command == "deferrals":
            return _cmd_deferrals(args)
        if args.command == "surface":
            return _cmd_surface(args)
        if args.command == "dispatch":
            return _cmd_dispatch(args)
        if args.command == "worker":
            return _cmd_worker(args)
        if args.command == "mcp":
            return _cmd_mcp(args)
        if args.command == "install-hooks":
            return _cmd_install_hooks(args)
        if args.command == "retain":
            return _cmd_retain(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "schedule":
            return _cmd_schedule(args)
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
