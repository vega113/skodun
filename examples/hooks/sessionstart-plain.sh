#!/usr/bin/env bash
# Session start, plain text: report background review rounds nobody has read.
#
# The same delivery as `sessionstart-claude.sh`, in plain lines instead of the
# SessionStart JSON envelope -- for a shell profile, a tmux hook, a CI job step,
# or any harness that shows a human whatever a command prints.
#
# WHY IT EXISTS. skodun's pre-push review runs in the BACKGROUND so it cannot
# block `git push`, which means the round lands after the push has returned and
# nobody is watching. Unread findings are the small half of that problem; the
# large half is that a review which FAILED records `findings_total: 0`, so
# anything reporting counts alone turns "no review happened" into "nothing
# wrong". `skodun surface` says which it was, in as many words, and records a
# round as delivered only once it has actually reached a reader.
#
# WHAT TO DO WITH IT. Copy it somewhere of your own and call it from wherever a
# session begins, e.g. in ~/.bashrc or ~/.zshrc:
#
#   [ -x "$HOME/bin/sessionstart-plain.sh" ] && "$HOME/bin/sessionstart-plain.sh"
#
# skodun NEVER installs this file into a repository. `skodun install-hooks`
# installs the pre-push shim and nothing else: what appears at the start of
# someone's shell is their choice, not a tool's.
#
# It never fails a session: every path exits 0, including the ones where skodun
# is not installed at all. A profile snippet that returns non-zero (or worse,
# under `set -e`, stops the profile) is a snippet that gets deleted -- taking the
# delivery of every future finding with it.
set -uo pipefail

# Not a git checkout: no branch, nothing to report.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# How to run skodun, most specific first: an explicit override, the installed
# console script, then the module through python3.
if [ -n "${SKODUN_BIN:-}" ]; then
  set -- "${SKODUN_BIN}"
elif command -v skodun >/dev/null 2>&1; then
  set -- skodun
elif command -v python3 >/dev/null 2>&1; then
  set -- python3 -m skodun
else
  exit 0
fi

# Passed through untouched. `surface` prints NOTHING when there is nothing
# undelivered, so a quiet branch costs a silent command and no screen space; its
# own notes go to stderr, so a wrapper capturing stdout gets only the report.
#
# The exit status is discarded on purpose: a non-zero means the report did not
# land or was not recorded, in which case the round stays undelivered and is
# reported again next time -- repetition is the designed failure mode.
"$@" surface --hook-format text || true
exit 0
