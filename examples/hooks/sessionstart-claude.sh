#!/usr/bin/env bash
# SessionStart hook: hand undelivered background review rounds to the agent.
#
# WHY THIS EXISTS. skodun's pre-push hook runs its review in the BACKGROUND, on
# purpose: it must not block `git push`. But the push has already returned by the
# time the review lands, so the round becomes a store record nobody ever saw --
# and the dangerous half is not the findings that go unread, it is the rounds
# that FAILED. A timed-out review records `findings_total: 0`, and anything that
# reports that as "0 findings" turns a review that never happened into a clean
# bill of health. `skodun surface` states "NO REVIEW HAPPENED" explicitly for
# those, renders partial evidence under an incomplete-cannot-certify warning, and
# marks a round as delivered only once it has actually reached a reader.
#
# WHAT TO DO WITH IT. Copy it somewhere of your own (or point at it where it is)
# and register it as a SessionStart hook in your Claude Code settings:
#
#   {"hooks": {"SessionStart": [{"hooks": [
#      {"type": "command", "command": "/path/to/sessionstart-claude.sh"}]}]}}
#
# skodun NEVER installs this file into a repository. `skodun install-hooks`
# installs the pre-push shim and nothing else: a delivery hook decides what
# appears in someone's session, and that is a choice for the person whose session
# it is.
#
# ONE STORE PER REPOSITORY. Background rounds are keyed by BRANCH NAME, and the
# store defaults to ONE GLOBAL FILE (~/.local/share/skodun/skodun.db). If you use
# skodun in more than one repository, two of them on a branch called `main` share
# a namespace: each can supersede the other's in-flight rounds and consume the
# other's reports -- this hook would then deliver, and permanently mark delivered,
# a round belonging to a different project. Give each repository its own store:
#
#   export SKODUN_DB="$HOME/.local/share/skodun/myproject.db"   # in your profile
#
# and make sure the same value is in effect for `git push` (the pre-push shim and
# the background worker read it too). The gate is unaffected either way -- it is
# content-addressed -- so this is about delivery, not about verdicts.
#
# It never fails a session. Every path exits 0, including the ones where skodun
# is not installed at all -- a hook that breaks a session start is a hook that
# gets deleted, taking the delivery of every future finding with it.
set -uo pipefail

# Not a git checkout: there is no branch to report on. Exit quietly.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# How to run skodun, most specific first: an explicit override, the installed
# console script, then the module through python3. `python3 -m skodun` is the
# fallback rather than the default because a virtualenv install puts `skodun` on
# PATH while the bare `python3` outside it cannot import the package at all.
if [ -n "${SKODUN_BIN:-}" ]; then
  set -- "${SKODUN_BIN}"
elif command -v skodun >/dev/null 2>&1; then
  set -- skodun
elif command -v python3 >/dev/null 2>&1; then
  set -- python3 -m skodun
else
  exit 0
fi

# stdout is the payload: `--hook-format claude` writes exactly ONE JSON object,
# the SessionStart envelope Claude Code reads, and skodun keeps every diagnostic
# of its own on stderr so this stream stays parseable. It is passed through
# UNTOUCHED -- no filtering, no reformatting, nothing that could turn a report
# into a truncated one.
#
# The exit status is deliberately discarded. A non-zero `surface` means the
# report could not be written or could not be recorded as delivered; either way
# the round stays undelivered and comes back at the next session start, which is
# the whole point of acknowledging after the emit rather than before it.
"$@" surface --hook-format claude || true
exit 0
