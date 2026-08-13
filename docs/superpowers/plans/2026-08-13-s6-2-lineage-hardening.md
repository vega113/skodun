# Plan: S6.2 lineage hardening

1. Audit the landed fingerprint/store/pipeline/dispatch seams against the live
   follow-up findings and current main.
2. Add strict bounded validators and safe fallback projections at storage and
   location boundaries.
3. Make candidate selection chronological and bounded, exposing truncation
   state without changing trust or gate decisions.
4. Cover terminal persistence and malformed-data paths with shipped-path tests.
5. Run focused tests, the store ResourceWarning sweep, and full verification;
   freeze the diff for review and deliver PR #161.
