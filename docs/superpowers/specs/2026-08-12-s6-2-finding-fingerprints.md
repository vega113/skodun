# S6.2 versioned finding fingerprints and lineage

## Scope

Additive read-model metadata for review findings. The legacy `finding_key`,
triage ledger keys, exact-diff reuse, gate decisions, and raw finding artifact
remain unchanged. Fingerprints are deterministic and fail closed: they may
report a repeated or moved claim, but never inherit a triage decision or remove
an item from the raw artifact.

## Frozen fingerprint contract

`finding_fingerprint_v2` is `sha256:<64 lowercase hex>` over canonical JSON
with sorted keys and compact separators. The payload includes explicit version
and algorithm identifiers plus normalized fields:

- repository-relative path and optional rename ancestry;
- stable symbol or hunk anchor, with `unknown` when unavailable;
- normalized category/rule and semantic claim;
- pass source;
- stack scope identity;
- mutation/evidence identity;
- explicit `unknown` values for absent fields.

Normalization trims and folds Unicode whitespace, lowercases enum-like fields,
and preserves claim meaning without fuzzy similarity or embeddings. Volatile
line numbers are excluded unless no stable anchor exists; then the anchor is
explicitly `unknown`, producing conservative false negatives rather than
collisions.

## Lineage read model

Persist one additive lineage row per finding occurrence keyed by review id and
finding index, storing fingerprint, scope, predecessor review/finding when a
unique exact fingerprint match exists, and a bounded match reason (`new`,
`repeated`, `moved`, `scope_changed`, or `ambiguous`). Ambiguous or missing
predecessors are reported and never selected silently. The read model does not
participate in trust, gate, triage, or reuse.

## Compatibility and safety

The schema change is additive through `_MIGRATIONS` v14 only. Legacy artifacts are
read with absent fingerprint metadata and remain valid. CLI and MCP use the
same service projection, exposing fingerprint version and match reason while
retaining the existing finding key and triage wording.
