# Agent instructions (skodun maintainers)

This file is for **agents working on the skodun repository itself**, not for
client projects that *use* skodun (those paste from `examples/AGENTS.md`).

## Goal / epic completion (non-negotiable)

**A goal, epic, or feature issue is NOT complete when tests pass locally.**

**Done means all of:**

1. Implementation + hermetic tests that drive the shipped code path
2. Branch pushed; PR opened against `main`
3. Code review addressed (bot or human) to the point of no blocking feedback
4. CI / required checks green (or documented non-blocking, e.g. rate-limited bot)
5. **PR merged to `main`**
6. **GitHub issue(s) closed** with a comment linking the merge/PR

Until (5) and (6), report status as **implemented, not landed** — never as
“complete” or “epic done.”

Local pytest green, a design doc, or a goal-harness “verification plan”
alone do **not** close product work.

## Preferred land path

```text
implement → tests → commit → push → PR → review fixes → merge to main → close issues
```

Stack or sequence dependent work so `main` stays green. Do not leave epic
code only on a long-lived feature branch without a merge PR.

## Invariants

- Prefer not changing `src/skodun/gate.py` / `src/skodun/trust.py` without
  explicit owner approval (byte-identical pins on product epics).
- Store schema changes: atomic ladder bump in `store.py` + migration tests.
- MCP and CLI must share the same `services` layer for review-loop verbs.

## Where product intent lives

| Kind | Path |
|---|---|
| Epic seeds | `docs/epics/` |
| Designs | `docs/superpowers/specs/` |
| Client integrate guide | `docs/integrate-external-project.md` |
| Client paste templates | `examples/AGENTS.md`, `examples/fragments/` |
