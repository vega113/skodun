# S6.2 lineage hardening

## Contract

Harden the additive fingerprint read model without changing legacy finding keys,
triage, gate, trust, exact-diff reuse, or review publication semantics. Malformed
lineage metadata is ignored per finding, never allowed to roll back an otherwise
valid review. Candidate matching remains repository-qualified, chronological,
bounded, and conservative.

## Scope

Validate nested lineage and scope values at the store boundary, preserve fallback
location fields, avoid linking to later reviews, and cap candidate enrichment
work with explicit artifact telemetry. Keep terminal publication atomic and
document any remaining concurrency limitation rather than weakening trust.

## Verification

Add regression tests for malformed provider scope/lineage values, chronological
candidate ordering, fallback line locations, bounded history, and resource
lifecycle. Run focused fingerprint/store/dispatch tests, the prescribed store
warning sweep, and the full suite.
