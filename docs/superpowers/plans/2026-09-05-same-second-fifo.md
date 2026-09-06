# Preserve same-second FIFO admission order

Refs #192 and #193. The current main d5c5659 orders equal-second admission timestamps by random ID, allowing a later request to overtake an earlier one. This must be corrected independently before the foreground pilot, so the pilot does not depend on the parallel-batch rollout it gates.

1. Move the already reviewed regression fixtures from draft #213 into existing capacity/queue test modules; confirm failure on current main.
2. Apply only the reviewed Store/WaiterView/queue-view insertion-order tie-break from #213. Preserve timestamps, schema, IDs, holder limits and legacy pure-view compatibility. No cross-maintenance rowid guarantee.
3. Run focused capacity/queue/inventory checks and self-review. The identical runtime delta is already included in #213's full partition (4346 passed); its separate lifecycle run is active. Use the owner-authorized expedited procedure for this narrow split, with exact-head review-state inspection and merged-main smoke.
4. Merge independently, then rebase #213 after its frozen lifecycle finishes. Keep both #192/#193 open until their remaining acceptance is satisfied.

Self-review: no new functionality beyond the inspected draft, no gate/trust or process cleanup edits, no migration, and no provider calls. Tests use real Store admission plus a fresh interpreter and bounded queue read. This fixes the dependency ordering without relaxing pilot acceptance.
