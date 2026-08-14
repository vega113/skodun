# Plan: #149 S7.3 language capability profiles

## Slice 1: protected profile contract

- Add `src/skodun/profiles.py` with strict frozen profile/fixture/result types.
- Reuse `ProducerPolicy`, `ProducerCommand`, `EvidenceIdentity`, and
  `run_with_watchdog`; never execute caller-supplied commands.
- Add stable unavailable reasons and bounded digest-only run summaries.

## Slice 2: Scala 3 pilot and receipt adapters

- Add a small Scala 3 fixture corpus and a profile definition that advertises
  syntax/compile, harness, symbol/AST, locator, and mutation capabilities
  without shipping a parser or Scala runtime.
- Add offline adapters for local JSON, compiler-valid mutation summaries, CI
  conclusions, and review-thread snapshots, all exact-head bound and redacted.
- Add deterministic compact receipt context with an explicit byte/item cap.

## Slice 3: tests and delivery

- Write hermetic tests for rejection-before-acceptance, missing toolchain,
  version mismatch, unsafe paths, timeouts, output limits, receipt binding,
  and prompt-size reduction.
- Run focused tests, the full suite, and the store ResourceWarning sweep.
- Self-review the frozen diff, run the exact-head hosted checks, address review,
  merge PR #176, and close #149 (and #142 only after its live S7 acceptance
  is verified).
