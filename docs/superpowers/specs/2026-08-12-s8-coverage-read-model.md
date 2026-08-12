# S8.2 — coverage, pass state, and derived gate eligibility

## Contract

Project review truth into one bounded read model without adding trust axes or
making checkpoints visible to gate, triage, deduplication, or reuse. The
projection must distinguish no evidence, partial evidence, and complete
planned coverage; preserve completed findings when coverage is incomplete; and
describe every planned pass, including finder-only background and annotation-
only refuter topology.

## Derivation

`coverage_state` is `none` when no finder evidence completed, `partial` when a
planned finder/integration pass is incomplete or failed after some evidence,
and `complete` only when every planned checkpoint/pass is terminally complete.
`usable_evidence` is true only when at least one completed finder or integration
pass produced parseable output. `gate_eligible` is true only for complete
coverage with the existing indexed trust axes trustworthy; its reason is an
explanatory projection and is never read by `gate.py`.

Pass states are derived from checkpoint rows and finalized artifact metadata;
missing optional refuter annotations are `skipped`/`unavailable`, while a
refuter failure remains visible but cannot demote finder trust. Required
security/skeptic/integration failures keep `gate_eligible=false`.

## Surface

One pure projector returns frozen, JSON-safe data. `svc_review_status` renders
the same compact human/JSON payload for CLI and MCP, preserving legacy `id=`
and `state=` tokens and adding coverage/pass/gate fields. No schema migration is
needed: checkpoints and artifact JSON are existing sources.
