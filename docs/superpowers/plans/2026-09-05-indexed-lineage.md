# Indexed advisory lineage implementation plan

**Goal:** Retrieve old exact fingerprints before bounded recent fallback, rank bounded prompt hints, and distinguish retrieval from rendering limits (refs #191).

**Architecture:** Reuse the repository/version/fingerprint index for exact lookups and the recency index for fallback. Keep provenance validation at the store read boundary. Add artifact diagnostics; advisory metadata never mutates triage or gate/trust. No migration.

**Tech stack:** Python stdlib SQLite and hermetic pytest.

- [x] Add failing shipped-store/pipeline regressions for an older exact finding behind 201 unrelated recent findings, repository/version/invalid-row isolation, bounded indexed queries, and independent candidate/prompt truncation.
- [x] Extend the store candidate reader with bounded indexed exact queries and scan diagnostics, retaining its compatibility wrapper. Revalidate candidate artifact provenance and fingerprint before use. Cap exact keys, total raw scans, and returned candidates independently from prompt bytes.
- [x] Combine indexed exact candidates with bounded recent fallback before annotation. Keep duplicate exact/fuzzy candidates ambiguous; de-duplicate only the same provenance row. Report exact/fallback counts, truncation, and unknown/unavailable states on artifacts.
- [x] Rank prompt rows by changed paths, relevant stack owners and prior disposition, deduplicate versioned fingerprints, retain JSON quoting and the 1024-byte bound. Persist separate retrieval/rendering diagnostics from foreground and background review paths.
- [x] Run focused fingerprint/pipeline/store tests, self-review the frozen diff and query plans, then commit/push/open a PR. Root coordinates full-suite/lifecycle verification and merge under the user's expedited external-review exception.

Self-review: all issue acceptance criteria mapped above. Existing indexes cover exact and recent lookup; no schema change or protected-module edit. Prompt relevance is bounded by recent fallback and explicitly reports incomplete retrieval. Historical disposition remains advisory and cannot suppress a new finding. Preserve narrow legacy store-double compatibility. No provider calls or live store mutation.

## Review follow-up: incomplete exact uniqueness and known dispositions

Both live PR #196 findings reproduce: one valid exact row followed by invalid
rows can exhaust the per-key scan before another valid occurrence; recency
fallback then repeats the singleton. Equal-relevance prompt duplicates can also
prefer newer unknown disposition over older known open disposition.

- [x] Add failing regressions through the real store/pipeline and prompt ranker.
- [x] Track non-exhaustive exact keys and explicitly mark their lineage ambiguous
  without a predecessor, even when fallback retrieves the same singleton. Also
  treat keys skipped by global scan/key limits as incomplete; do not convert a
  bounded miss or fuzzy match into asserted unique lineage.
- [x] Rank all recognized dispositions, including open, before unknown, with
  stable recency within equal relevance/disposition knowledge.
- [ ] Run focused regressions, self-review, push one follow-up commit, and resolve
  both existing threads with evidence. Root retains merge coordination.

Self-review: this is an advisory annotation correction; no triage/gate/store
schema changes. Explicit incomplete-key input keeps truncation uncertainty from
being lost when exact and fallback provenance rows are deduplicated.
