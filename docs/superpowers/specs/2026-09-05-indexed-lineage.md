# Indexed advisory lineage

Issue #191 replaces recency-only lineage matching with exact versioned lookup
before the existing bounded recent/fuzzy fallback. Neither stage changes the
legacy finding key, trust, coverage, or triage. An old dismissal is a historical
hint, never an instruction to dismiss a new finding.

The existing `ix_finding_lineage_lookup` index serves up to 200 distinct current
fingerprints, with at most 1024 raw rows scanned across exact queries. Each key
retains at most two valid occurrences, enough to preserve ambiguity. Recent
fallback retains up to 200 findings and scans at most 804 raw rows. Consequently
at most 600 distinct provenance rows reach annotation. The legacy
`fingerprint_candidate_limit` continues to describe the 200-row recency budget;
`fingerprint_diagnostics` names both stage limits, scanned/retained counts,
truncation, and retrieval state. Counts describe bounded work, not repository
history totals. Duplicate provenance rows are collapsed; independent exact or
fuzzy occurrences remain ambiguous. Incomplete retrieval can miss lineage;
`partial`/truncation signals prevent treating those misses as exhaustive.

Candidate reads validate each source artifact's repository, review ID,
timestamp, finding index, version, and recomputed digest. Invalid projection or
artifact rows consume raw-scan budget but do not supply a match. Unknown
repository identity never joins local repositories. Exact queries order by
creation time and rowid; recency queries add review ID. Both orders use existing
indexes without a temporary sort, including timestamp ties. No migration or
inspection write is needed.

Before a model call, the current claims do not yet exist. Prompt hints therefore
rank the bounded recent candidates by exact changed path, validated current or
dependency stack owner, then known historical disposition, preserving recency
within ties. Fingerprint duplicates are removed. Historical dispositions come
from a separate primary-key-ordered scan of at most 1024 recent triage events,
qualified by the original review ID and finding key; a truncated disposition
scan produces `unknown`, not an assumed open state. Raw triage reasons are never
copied into prompts.

Rendering retains the independent 1024-byte budget and quoted complete path
lines. `lineage_context_diagnostics` exposes candidate/scanned/relevant/selected
counts, candidate and scan truncation, disposition scan counts/truncation, and
prompt-byte truncation. Selected count is before byte packing. The legacy
`lineage_context_truncated` is the OR of candidate and prompt-byte truncation.
The shared service status/log projections expose both diagnostic dictionaries
to CLI and MCP. Unknown or unavailable enrichment remains advisory; error
metadata contains only the exception class.
