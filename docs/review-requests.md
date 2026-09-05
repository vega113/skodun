# Review request identity

Foreground CLI and MCP review calls persist an execution request before
readiness and admission. Progress reports `SKODUN REQUEST: id=sk_req_...`;
MCP review metadata includes the same `request.id` even when preflight fails
or the queue expires before a review record exists. The final verdict text
and existing exit codes are preserved.

Inspect that request directly:

```sh
skodun review-status sk_req_EXAMPLE --json
```

The request projection contains requested repository/worktree/head/base/diff
identity, lifecycle state, source, timestamps, final service result, and links
to capacity tickets and reviews. Recovery and batch orchestration links have
distinct kinds. Internal ownership tokens and unhashed configuration/process
overrides are never returned. A `finished` request means execution returned a
result; inspect its status and the separate gate decision for coverage.

For an idempotent caller retry, supply a stable key:

```sh
skodun review --request-key delivery-4093-round-1
```

MCP accepts the equivalent `request_key` string. The key is limited to 128
characters and scoped to the normalized worktree. Repeating an identical
completed request returns its historical result without another provider
call. An active duplicate returns exit 3 and `request_in_flight`. A changed
head/diff/configuration/policy or explicit invocation intent returns exit 2
and `request_identity_mismatch`. Use a new key for a deliberate fresh second
opinion. Unkeyed new reviews receive distinct request IDs. An ordinary
compatible checkpoint continuation reclaims its originating unsuccessful
request and appends an execution entry, preserving its logical request ID.
Checkpoint identity validation still determines which evidence can be reused.
An active origin is observed rather than stolen. Interrupted result-less keyed
requests refuse automatic retry; an explicit ordinary continuation can reuse
their compatible checkpoints and preserve request identity.

Requested identity and configuration are captured before waiting. Execution
uses that frozen configuration; actual repository, tree, configuration, and
process-policy hashes are checked under admission. A changed identity is
refused before provider work. An
active request's owner is never stolen because a client stopped observing
it. Stored expiry is a diagnostic horizon; existing review budgets still
control execution. Interrupted requests with no complete result refuse
idempotent re-execution. Observe the request and choose a new invocation
explicitly; request bookkeeping never certifies an unfinished review.

Pre-push dispatch retains its existing durable exact-ref reservation. That
reservation's review ID is also its `request_id`, preserving dispatch dedup
and supersession semantics without introducing a competing reservation.
Foreground requests use the `sk_req_` namespace and separate ledger.

Request identity stubs and links are retained with the audit history. Large
terminal result payloads have explicit bounded retention:

```sh
skodun retain --request-results-days 30 --dry-run
skodun retain --request-results-days 30
```

Each invocation processes at most 500 old terminal payloads. The request key,
identity, and links remain, so retention cannot silently turn an old retry
into another provider call. Active requests and review/gate/triage artifacts
are retained. A pruned result reports `request_result_expired`; retrying its
old key refuses execution. Existing worker-log retention continues normally.

The request ledger was introduced by additive schema version 17. Deploy the verified build
and use the existing explicit maintenance procedure (`skodun store migrate
--plan`, then `--apply`) for an older authority store. Ordinary inspection
never upgrades the store. Request rows are execution records, excluded from
trusted reuse, gate coverage, and finding triage.
