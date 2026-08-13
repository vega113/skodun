# S7.1 trusted repository evidence receipts

## Status and boundary

This slice defines an advisory, read-model evidence contract. Receipts never
change `trustworthy`, `gate.py`, `trust.py`, triage, exact-diff reuse, or
coverage. A missing, failed, stale, malformed, unverifiable, or conflicting
receipt is visible evidence with a stable reason, never positive coverage.

The producer policy is an input from the reviewed base or operator-owned
configuration. The candidate worktree is not allowed to define or weaken the
policy that validates its own receipt. This module receives the protected
policy explicitly; it does not discover policy from candidate files.

## Threat model

Receipt JSON is untrusted input. The parser rejects duplicate or unknown keys,
non-finite numbers, invalid timestamps, unsafe paths, symlinks, hardlinks,
FIFOs, oversized files, identity mismatches, policy/command mismatches, bad
digests, missing redaction declarations, and conflicting nonce replays. It
never executes a receipt command, follows a receipt path, stores prompts,
secrets, unrestricted environments, or provider logs.

The protected policy contains argv arrays, a repository-relative or explicitly
bounded working directory, and an environment-name allowlist. Shell strings,
inline environment values, and candidate policy documents are not accepted.
The policy digest and command digest are canonical SHA-256 values, so a
receipt cannot substitute a merely equivalent-looking command.

## Canonical envelope

`EvidenceReceipt` is a frozen dataclass with these exact fields:

```text
schema_version, evidence_kind, repository_id, worktree_root,
certification_base, current_head, diff_hash, stack_slice_id,
producer_policy_id, producer_policy_digest, command_id, command_digest,
producer_proof,
started_at, completed_at, exit_code, terminal_state, duration_ms,
counters, artifact_digests, tool, runtime, diagnostic_category,
nonce, redaction, receipt_digest
```

Canonical JSON is UTF-8, sorted keys, compact separators, `ensure_ascii=false`,
and `allow_nan=false`. The digest is `sha256:<64 lowercase hex>` over the
canonical envelope with `receipt_digest` omitted. Timestamps are canonical UTC
`YYYY-MM-DDTHH:MM:SSZ`; completion cannot precede start and duration must
match the non-negative second difference. Counters and artifact digests are
bounded maps/lists; diagnostics are one sanitized category, not free-form
output. `redaction` is exactly `{applied, secrets_removed, logs_included}` and
must be `true,true,false`.

`producer_proof` is `sha256:<64 lowercase hex>` over the canonical envelope
with both `producer_proof` and `receipt_digest` omitted, authenticated with the
HMAC-SHA256 key held by the protected producer policy. The key is injected only
through the runner-owned producer channel and is never serialized in the
receipt. Verification rejects a receipt whose proof does not match before it
can be reported as accepted.

Identity binding is the canonical digest of repository id, worktree root,
base, head, diff hash, and optional stack slice. Ingestion receives the
expected identity and protected policy from outside the receipt, reparses the
canonical envelope, derives its status and reason from verification, and only
then persists it. A rejected receipt is indexed under the expected identity so
stale or mismatched attempts remain queryable; the receipt's claimed identity
is retained only in the validated JSON.

## Ingestion and persistence

Add schema v16, never edit the frozen Phase-1 schema. `evidence_receipts` is an
additive table keyed by `(identity_digest, receipt_digest)` with a unique
`(identity_digest, nonce)` constraint. Identical retries are idempotent. A
different digest for an existing identity/nonce is retained in the separate
bounded `evidence_receipt_conflicts` table as a visible `conflict` result and
is not stored as accepted evidence. Accepted, rejected, and conflict outcomes
are bounded read-model rows; none affect trust.

The stored JSON is the validated canonical envelope only. The store exposes a
bounded summary query by identity digest. It does not store source paths,
commands beyond digests, environment values, prompts, or logs.

## Surfaces and compatibility

`services.py` owns the shared `svc_evidence_summary` projection. CLI `evidence`
and MCP `evidence` call it and return byte-identical JSON when `--json` /
`output=json` is requested. The projection is bounded to the newest 32 rows
and exposes identity, kind, digest, nonce, terminal state, and reason code.

Old stores remain readable; v16 is additive and empty until an explicit open
migrates it under the existing ladder. Existing review artifacts and the
legacy gate/trust modules remain byte-identical.

## Acceptance tests

Test canonical digest stability, duplicate keys, unknown fields, NaN, all
identity/policy mismatch reasons, candidate-policy non-authorization, unsafe
receipt files, size limits, timestamp/duration validation, redaction and
secret-shaped diagnostics, idempotent duplicates, nonce conflicts, migration
from v15, bounded store reads, and CLI/MCP projection parity. Include a
snapshot asserting `gate.py` and `trust.py` are byte-identical to `origin/main`.
