# Versioned review results implementation plan

Goal: expose one bounded `review-result/v1` terminal contract from CLI JSON and MCP without changing default banners, exit codes, or gate authority; closes #184.

Architecture: `review_results.py` projects explicit request identity, terminal causes, compact artifact observations, coverage/findings/gate axes, timing, and candidate attempts. Shared service exception handlers set stable codes from typed causes. Each successful pipeline return contributes a bounded observation of that exact artifact; no current/latest lookup. The outer detailed service projects the terminal result, including idempotent replay and no-store failures. CLI `review --json` emits one JSON object; MCP includes the same object under structured `result`. Root #185 contributes structured termination/timing without requiring prose parsing.

- [x] Add failing shipped service/CLI/MCP tests for schema types, preflight/store/invalid input, clean/findings, cancellation and no stale identity.
- [x] Add compact result projection with explicit unknowns, bounded attempts, separate aggregate/provider input bytes, launched/skipped distinction and stable cause codes.
- [x] Instrument chain input bytes and typed invocation/capacity causes. Wire terminal service exceptions and actual artifact/reuse observations. Keep each recovery iteration's metadata isolated.
- [x] Add CLI JSON and MCP startup/invalid-input terminal projection, keeping progress on stderr and JSON-RPC stdout valid.
- [x] Extend matrix for timeout -> oversized fallback and capable later fallback, partial batching, request replay and boundedness. Run focused tests, self-review, push PR; root owns integrated suite and merge.

Self-review: no migration or artifact/prompt duplication; result is reporting, never gate clearance. Unknown timing/coverage remain null, and input size is not inferred from aggregate prompt sums. Stable codes come from exceptions, classification fields and explicit branch decisions; human prose remains informational only. Existing store-failure and invalid-option paths need a projection even before a request can persist. Request replay validates stored result shape before reporting success. Owner expedited exception permits self-review without external-review waiting.


## Validation and self-review

- Initial result matrix failed 4 tests for missing CLI JSON and service/MCP result fields; the same cases passed after implementation.
- CLI, MCP tools/server, and result matrix: 517 passed in 55.71 seconds.
- Shared request/service/chain/result rerun: 141 passed in 17.35 seconds.
- MCP server and result rerun: 114 passed in 9.22 seconds.
- Final result matrix plus store-sweep inventory with ResourceWarning as error: 25 passed in 7.43 seconds. Includes actual partial-batch CLI/MCP paths and original-owner trusted reuse; no provider calls or charges.
- `git diff --check` passed. New Store-using tests are included in the lifecycle sweep inventory. Root owns the integrated full suite and complete separate lifecycle sweep.
- Self-review checked startup-store versus handler exceptions, typed cancellation/budget distinctions, current-attempt recovery metadata, original artifact ownership on reuse, exact input bytes versus aggregates, bounded observations, and malformed replay refusal. A recovery-metadata persistence exception now reports exit 4 rather than retaining an earlier success status.
- Frozen self-review replaces external waiting under the owner's expedited-review exception. No gate/trust edits and no schema migration. Root coordinates merge and merged-main smoke.

## Bounded review follow-up

The cancel-after-first-checkpoint regression failed through service, CLI, and MCP before the fix. Cancellation now compares current-request review links before/after the service attempt, verifies the newly persisted review's request/identity, and reads validated completed checkpoints. A prior recovery review is never selected for a no-record cancellation. Boundary and in-flight cancellation both retain known evidence while marking counts incomplete, total launches unknown, and known launches as lower bounds.

The same follow-up addresses current review findings: nested and semantic replay validation (10 negative cases failed before tightening), typed reuse cancellation, provider admission expiry, MCP busy, and final recovery persistence failure. Extra-pass metadata without attempt rows now marks counts/causes incomplete and names the missing scopes. No extra-pass schema or execution behavior was added. The MCP store-open status remains the intentionally preserved legacy status 2; authoritative #194 eligibility input bytes remain valid observed data, with the documentation clarified.

Final focused verification: 46 result/inventory tests passed under ResourceWarning-as-error in 14.43 seconds; 168 service/MCP tests passed in 5.10 seconds. An earlier broader ResourceWarning run returned 144 passed with one existing unclosed SQLite finalizer warning in the request dispatch fixture; another earlier MCP warning sweep exposed existing unclosed subprocess pipe finalizers. These are not claims that the complete lifecycle sweep passed. Root still owns that integrated sweep. `git diff --check` and self-review passed; no new external review loop was run.
