# Schema fixture validation after request migration

CI on PR #199 exposed stale schema-16 assertions and exact object/record-shape
expectations. Production migration 17 is intentional and independently tested.

- [x] Read the complete CI failure list: 14 failed, 3950 passed, 161 skipped,
  17 fixture errors; the nested lifecycle failure has the same causes.
- [ ] Replace unrelated current-version constants with actual database-version
  comparisons. Preserve historical version assertions before migration.
- [ ] Add explicit V17 table/index expectations to additive-upgrade tests;
  retain exact schema comparisons and the frozen Phase-1 contract.
- [ ] Update the documented prepush shape to assert its request_id equals its
  reservation ID, retaining all existing status/trust/dedup assertions.
- [ ] Run store (separate heavy sweep excluded), capacity, feedback, API spend,
  stats, request and schema-lifecycle tests; self-review and ship one focused PR.

No production change or external review wait is needed for these expectation
repairs. Later migrations must run these focused consumers before merge as well
as the integrated full suite; this is the early check the first slice missed.
