# Plan: issue #148 S7.2 mutation proof receipts

## Scope

Add compiler-valid, non-vacuous mutation proofs on top of the merged S7.1
receipt envelope. Keep the implementation stdlib-only and advisory; do not
edit `gate.py` or `trust.py`, add a trust axis, or permit candidate policy to
authorize its own commands.

## Steps

1. Add a bounded `MutationProof` model and strict canonical parser for the
   required target, compiler, controls, sentinel, outcome, cleanup, restore,
   tree-identity, diagnostic, and artifact fields.
2. Extend S7.1 receipt parsing/digesting so mutation proofs are authenticated
   by the existing producer HMAC while non-mutation receipts remain backward
   compatible.
3. Add the production mutation state machine using safe repository-relative
   file selection, exact byte replacement, protected policy commands, the
   existing watchdog runner, bounded output digests, and unconditional restore
   and final-tree verification.
4. Add shipped-path tests for accepted old-fail/new-pass proofs and every
   vacuity/cleanup/compiler failure in the issue, plus evidence-store and
   CLI/MCP projection coverage for mutation receipts.
5. Run focused tests, the full suite, the store ResourceWarning sweep, freeze
   the diff for exact-head review, merge the PR, and close #148 with evidence.

## Verification gates

- `python3 -m pytest tests/test_mutation.py tests/test_evidence.py tests/test_mcptools.py -q --tb=short`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`
- `python3 -m pytest tests/test_store.py --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error`
- `gate.py` and `trust.py` remain byte-identical to `origin/main`.
