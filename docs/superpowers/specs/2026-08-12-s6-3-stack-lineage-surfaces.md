# S6.3 stack ownership, deferrals, and lineage surfaces

## Contract

Validated stack metadata and persisted finding lineage are projected as bounded,
additive review context. The full certification diff remains authoritative;
caller claims never clear findings, and provider attribution never changes gate,
trust, legacy finding keys, or triage decisions.

## Design

Build one compact structured context block from the validated manifest and prior
fingerprint rows, with an explicit byte budget and degradation marker. Reconcile
provider scope with deterministic stack ownership without mutating raw findings.
Expose the same bounded fields and wording through shared services used by CLI
and MCP status/read surfaces. Audited triage deferrals are the only source that
may render a finding as deferred; unverified caller metadata remains a claim.

Unknown, conflict, and truncated states remain visible. Background and extra
passes retain their topology markers. All changes are additive and preserve
exact-diff, readiness, gate, trust, triage, and legacy artifact compatibility.

## Verification

Hermetic shipped-path tests cover prompt budgeting/degradation, reconciliation,
audited versus unaudited deferrals, CLI/MCP parity, legacy artifacts, and
unchanged gate/trust projections.
