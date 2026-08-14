# Plan: issue #147 S7.1 trusted evidence receipts

## Scope

Implement the advisory receipt envelope, protected producer-policy validation,
identity binding, additive store read model, and bounded CLI/MCP summary. Do
not execute repository commands or make receipts gate prerequisites; leave
mutation proof execution to #148 and language pilots to #149.

## Steps

1. Add `evidence.py` with frozen identity/policy/receipt models, canonical JSON
   and digest helpers, strict parser, protected-policy verification, and safe
   bounded receipt-file loading.
2. Add migration v16 and Store methods for idempotent accepted/rejected
   ingestion, nonce-conflict visibility, and bounded identity summaries.
3. Add the shared `services.svc_evidence_summary` projection, CLI `evidence`
   command, and MCP `evidence` tool using the same JSON payload.
4. Add hermetic tests for parser threats, identity/policy binding, store
   migration and replay behavior, and CLI/MCP byte parity. Assert gate/trust
   source bytes are unchanged.
5. Run focused tests, full pytest, and the store ResourceWarning sweep; self-
   review the frozen diff, push PR, address exact-head review, merge, and
   close #147 with evidence.

## Verification gates

- `python3 -m pytest tests/test_evidence.py tests/test_store.py tests/test_services.py tests/test_cli.py -q --tb=short`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short`
- `python3 -m pytest tests/test_store.py --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error`
- `git diff --no-index origin/main:src/skodun/gate.py src/skodun/gate.py` and
  the equivalent trust check show no changes.
