# S8.1 Exact-Identity Batch Checkpoints and Safe Resume

Date: 2026-08-12. Status: implementation design for #150, child of epic #143.

## Scope

Persist completed batched sub-reviews outside the `reviews` table and resume
them only when the complete review and deterministic plan identity still
match. A completed orchestration is finalized into the existing single review
artifact. This slice does not add coverage projections (#151), telemetry or
provider executable provenance (#152), or evidence receipts (#147).

`gate.py` and `trust.py` remain byte-identical.

## Locked safety properties

1. Checkpoints are not reviews. Gate, triage, delivery, dedup, exact-diff
   reuse, and normal review listings cannot query checkpoint tables.
2. Resume requires exact equality for repository, worktree, branch, head,
   certification base, full diff, tree, context, checklist, reviewer graph,
   effective config/policy, planner version, batch budget, ordered boundaries,
   integration plan, and every pass prompt identity.
3. A completed pass is reusable only when its exact prompt identity matches.
   Batch prompt identities are frozen during plan preparation; the integration
   prompt is data-dependent on terminal batch outputs, so its hash is computed
   immediately before its claim and the store rejects any differing runtime
   prompt before a provider call.
4. A transactionally claimed pass has one lease/fencing generation. A second
   resumer never invokes the provider for that pass. An expired lease may be
   reclaimed with a greater fence, and a late prior owner cannot write through
   that fence.
5. Completed checkpoint payloads are bounded, strictly validated, and contain
   only normalized review output and existing sanitized attempt provenance.
   Prompts, transcripts, secrets, environment values, and PATH are never
   persisted.
6. Finalization remains the only route into a coverage-bearing review row. It
   occurs after all required passes are terminal and the current identity has
   been revalidated.
7. Cancellation or a global wall expiry keeps completed valid checkpoints,
   releases the current claim, and leaves no trustworthy aggregate.

## Alternatives considered

### Persist one review row per batch

Rejected. Existing gate, triage, reuse, delivery, and finding-key readers all
consume `reviews`. Hiding selected rows at every reader would be fragile and
would make a checkpoint accidentally look like coverage.

### Store only final batch JSON blobs without claims

Rejected. Two processes could both observe a missing batch, invoke the same
provider, and race to write it. Idempotent storage does not prevent duplicate
paid or quota-consuming calls.

### Resume by full diff hash plus batch index

Rejected. Context, checklist, reviewer, policy, configuration, planner, and
boundary changes can alter what the model saw without altering the full diff.
Approximate identity reuse is unsafe.

## Architecture

### Modules

`src/skodun/checkpoints.py` owns canonical identities, strict bounded payload
validation, first-mismatch reporting, claim results, and conversion between a
stored checkpoint payload and `pipeline._Sub`.

`src/skodun/store.py` owns the v13 additive schema and transactional state
transitions.

`src/skodun/pipeline.py` prepares deterministic pass inputs, asks the checkpoint
controller whether to reuse or run each pass, and aggregates exactly as today.
The prepared-plan `sole` flag is defined before batch context packing and keeps
the one-batch whole-diff behavior aligned with the unbatched path.
The foreground and pre-push paths provide their already-captured repository
identity. Unbatched reviews do not enter the subsystem.

`src/skodun/services.py` continues to own `fresh`: `fresh=true` disables
checkpoint lookup and starts a new orchestration. CLI and MCP already route the
same flag through this service, preserving parity without a new public option.

### Additive schema v13

`review_orchestrations` stores one identity-bound orchestration:

- `id`, `state` (`active|complete|cancelled|failed|expired|consumed`),
  `created_at`, `updated_at`, `expires_at`, `final_review_id`;
- `repo_id`, `worktree_root`, `branch`, `head`, `base_ref`, `base_sha`,
  `diff_hash`, `tree_fingerprint`;
- `context_hash`, `checklist_hash`, `reviewer_hash`, `config_hash`,
  `policy_hash`;
- `planner_version`, `batch_budget`, `batch_count`, `boundary_digest`,
  `integration_plan_digest`, `identity_json`;
- bounded `terminal_reason`, `first_mismatch`, and current `claim_owner`.

`review_checkpoints` stores one planned pass per orchestration:

- `(orchestration_id, pass_kind, pass_index)` primary key;
- `state` (`pending|running|complete|failed`), `prompt_hash`, `diff_hash`,
  `boundary_hash`, `payload_json`, `completed_at`;
- `claim_token`, monotonically increasing `fence`, `claim_owner`,
  `claimed_at`, `lease_expires_at`;
- bounded `failure_reason`.

Foreign keys cascade only when an orchestration is explicitly pruned. Neither
table references `findings` or triage events.

The migration is a replay-safe pair of `CREATE TABLE IF NOT EXISTS` statements
plus indexes in `_MIGRATIONS`; frozen Phase-1 `_SCHEMA` is unchanged.

## Exact identity

The orchestration identity is canonical JSON hashed with SHA-256. It includes:

- canonical `repo_id` and resolved worktree root;
- branch, exact head, base ref, base SHA, full diff hash, tree fingerprint;
- requested reviewer intent and the fully resolved ordered reviewer/fallback
  graph for finder and integration roles;
- effective defaults plus dispatch inputs that influence a pre-push pass;
- security/pass policy identity;
- planner id `skodun-batch-v1`, effective batch budget, ordered batch count,
  each exact batch diff hash/files/truncation flag, and a boundary digest;
- ordered checklist identities and context pack identities;
- exact per-pass prompt hashes and an integration plan digest.

Identity material is persisted as hashes and bounded structural facts, not as
prompt text. Comparison follows a fixed field order and reports the first
mismatch as `resume refused: <field> changed`.

## Resume state machine

1. Under the existing repository/foreground serialization boundary, capture
   the full identity and prepare deterministic batch prompts.
2. If `fresh`, insert a new orchestration. Otherwise select the newest active
   or incomplete orchestration with the same repo/worktree/branch, compare the
   complete identity in fixed order, and either resume the exact match or
   create a new orchestration while recording the first mismatch.
3. For each batch in order, transactionally call `claim_checkpoint`.
   - `complete`: validate and reuse its `_Sub` payload.
   - `claimed`: invoke the provider once, then conditionally complete using the
     returned token and fence.
   - `in_flight`: refuse this resumer with a bounded reason; invoke nothing.
4. Build and claim the integration pass only after every batch payload is
   available. The integration prompt identity is exact and uses the recovered
   batch summaries/findings.
5. Revalidate the repository and orchestration identity. Atomically verify all
   required checkpoints are complete, persist/finalize the existing aggregate
   review, and mark the orchestration consumed.
6. On cancellation or exception, release only the caller's active claim and
   retain every completed checkpoint. The ordinary review row remains failed
   or unfinalized according to the existing foreground/pre-push lifecycle.

Foreground `run_review` already persists a running aggregate row before the
first provider call. That row remains untrustworthy and invisible as coverage.
The final store transition will update it and consume checkpoints in one
transaction. Pre-push already has a reserved running row and uses the same
conditional finalization semantics.

## Claim fencing and crash recovery

Claims use `BEGIN IMMEDIATE`. A claim is live until `lease_expires_at`, bounded
from the existing attempt budget plus cleanup grace. Reclaim increments
`fence`, replaces owner/token/timestamps, and returns `claimed`. Completion
updates only when orchestration id, pass key, token, fence, owner, and
`state='running'` all match. A late process therefore cannot overwrite a newer
owner's payload.

A cancelled caller releases its own claim back to `pending`; a process crash
leaves a lease that another exact-identity request may reclaim after expiry.
Completed rows are immutable.

## Payload validation and retention

Checkpoint JSON reuses the normalized `_Sub` vocabulary: strict booleans,
bounded summary/reasons, bounded findings and attempts, sanitized provenance,
and optional accepted adapter/model identity. The validation door refuses
unknown keys, malformed findings, excessive JSON, and prompt/transcript-shaped
fields. Missing or invalid data is never interpreted as a clean pass.

Incomplete orchestration rows expire after a bounded default retention window.
Pruning marks active rows `expired` before deleting checkpoint payloads; it
never edits review rows. Completed checkpoints survive cancellation and wall
timeout until expiry. Retention is exercised through store APIs in this slice;
operator stats are deferred to #151/#152.

## Test strategy

- migration from v12 and fresh v13; frozen `_SCHEMA` pin;
- payload bounds, unknown fields, malformed checkpoint refusal;
- exact identity match and first mismatch for every identity class;
- timeout/cancel after batch 3/4, then resume without reinvoking 1-3;
- final aggregate equality to an uninterrupted run, excluding orchestration
  metadata explicitly permitted by #150;
- racing claims: one provider invocation, contender observes in-flight;
- expired-lease reclaim and stale-owner fenced completion refusal;
- `fresh` starts a wholly new orchestration and invokes every batch;
- cancelled/expired checkpoints are absent from reviews/reuse/triage/gate;
- foreground and pre-push shipped paths;
- store ResourceWarning sweep and byte pins for `gate.py`/`trust.py`.

## Compatibility and sequencing

Legacy databases migrate additively. Legacy review artifacts remain readable.
Unbatched execution is unchanged. Existing final batched artifact shape remains
compatible; new orchestration ids may be attached as non-trust metadata.

#151 may project partial coverage from these tables. #152 may add telemetry and
attach #147 receipt digests after those contracts land. This design deliberately
does not invent a receipt envelope or a second trust axis.
