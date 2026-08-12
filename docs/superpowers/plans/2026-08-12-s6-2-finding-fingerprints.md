# Plan: S6.2 finding fingerprints and lineage

1. Inspect the shipped finding construction, artifact validation, store
   migrations, triage projections, and CLI/MCP service parity; freeze the
   additive data shape above.
2. Add pure stdlib normalization and fingerprint helpers with corpus tests for
   exact repeats, retitles, moved lines, renames, changed claims, scope/mutation
   changes, unknowns, and collision safety.
3. Add the additive lineage migration and store read/write helpers. Keep writes
   best-effort metadata only: malformed or missing lineage cannot alter trust or
   gate outcomes.
4. Annotate shipped finding projections/artifacts and shared service output,
   preserving `finding_key` and all triage behavior. Add CLI/MCP parity tests.
5. Run focused fingerprint/store/service tests, the prescribed store warning
   sweep, then the full suite. Review the frozen diff and deliver one PR for
   #145 with exact-head checks.
