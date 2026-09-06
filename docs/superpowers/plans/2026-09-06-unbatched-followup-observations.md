# Preserve unbatched follow-up observations (#215)

The real #192 pilot completed required skeptic passes but their available attempt arrays were discarded by `_extra_pass`, leaving CLI/MCP and queue totals correctly unknown. Batched required follow-ups already attach attempts after the pure merge.

1. Add shipped CLI/shared-queue regressions for successful and returned-failed unbatched skeptic outcomes, asserting real fake-provider invocation counts, distinct attempt IDs, bytes and unchanged gate results. Confirm they fail on current main7f97e84.
2. Attach `outcome.attempts` through `_with_provenance` on both returned-outcome paths. Preserve pure merge semantics and keep pre-outcome exceptions/legacy records unknown; no historical backfill or zero fabrication.
3. Run focused pipeline, pass, result and queue checks; self-review, narrow PR and merged smoke under the owner's expedited policy. Validate attribution with a bounded real follow-up after installing the final tested build. Preserve the first pilot's raw slower/cost-incomplete observations rather than replacing them with a favorable sample.

Self-review: observational JSON only; no gate/trust, routing, model policy, process ownership or schema changes. Reuse actual chain observations rather than infer calls from capacity admissions.
