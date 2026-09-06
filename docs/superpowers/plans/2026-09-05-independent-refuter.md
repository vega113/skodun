# Independent refuter adoption implementation plan

**Goal:** Require provider-family independence from actual finding contributors before refutation or adoption (refs #189).

**Architecture:** Capture a conservative complete contributor set at the finder snapshot: the accepted single answer, or every accepted batch and integration answer. Filter the configured refuter's entire explicit chain before building its prompt. Persist the snapshot with pass provenance; adoption validates it before any triage write. Provider-family comparison is a proxy, with openai/openai-api sharing one family. No override, trust axis, store migration, or bulk adoption is introduced.

**Tech stack:** Python stdlib and hermetic pytest.

- [x] Add shared provenance validation in `src/skodun/refuter_policy.py`: known provider IDs, complete contributor lists, matching actual annotation/pass attribution, and overlap refusal.
- [x] Update `src/skodun/pipeline.py` to replace lossy aggregate provider reduction, filter the whole refuter chain using `replace(head, fallbacks=...)`, and persist contributors even on skipped/failed passes. Unknown provenance skips without invoking a provider.
- [x] Update `src/skodun/triage.py` and `src/skodun/services.py` to refuse unknown/same-provider adoption before dismissal; retain existing reasoning, index, and manual-dismissal validation.
- [x] Write failing shipped-path tests in `tests/test_refuter.py`, `tests/test_triage.py`, and CLI/MCP parity fixtures for configured/fallback overlap, mixed batch/integration contributors, unknown legacy metadata, unchanged gate/ledger on refusal, and successful independent adoption.
- [x] Document intentional compatibility change and limited independence proxy in `README.md`.
- [x] Run focused suites, self-review frozen diff and `git diff --check`, commit and push a PR with Summary + Test plan. Root owns integration full suite and merge; external model review is optional under the owner's expedited instruction.

Self-review: conservative aggregate scope includes integration and every batch, even clean contributors; failed/missing accepted provenance cannot borrow configured identity. Filtering preserves chain order and does not follow a promoted fallback's own chain. No additional providers outside the configured chain or hidden metered fallback. All refusal paths run before Store writes; protected gate/trust and process code remain unchanged. Existing unusual attribution fixtures intentionally need valid independent metadata or a refusal expectation.

Implementation self-review and verification: selection only promotes an existing explicit fallback and discards the promoted entry's own fallback list; the prompt budget uses that selected head. Accepted refuter provenance is checked again before annotating. Legacy contributor metadata and annotation/pass mismatches refuse before `dismiss`. Coverage/gate/trust, store schema, and process ownership code were left unchanged. Focused surfaces passed 824 tests with 4 skips; pipeline/batched/pass suites passed 229 with 28 skips. After the accepted-provider/matching hardening, refuter/triage rerun passed 328 with 4 skips, and the actual-finder-fallback regression passed separately (1 test). Full integrated suite and merge remain coordinator-owned. External provider review was omitted under the owner's expedited instruction; self-review and hermetic shipped-path evidence are complete.

PR feedback follow-up: preserve available provider/model diagnostics when a parsed refuter answer is rejected for missing or overlapping accepted-provider provenance. Retain failed/ran-false status, the refusal note, contributor set, and no annotation/adoption. Add a CLI test-module contract and a shipped-path regression proving that an unavailable promoted fallback cannot expand its own contributor fallback. Self-review confirms `_chain_for` follows only the head's explicit list and the refuter replaces that list with only filtered entries.

Follow-up validation: the two diagnostic assertions failed before the production fix; all seven focused refuter/CLI checks passed afterward. `git diff --check` passed. No additional external review pass was requested.
