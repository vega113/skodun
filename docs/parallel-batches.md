# Opt-in independent batch execution

Foreground durable requests accept `--batch-concurrency 1|2`; MCP `review`
accepts the same `batch_concurrency` integer. The default remains sequential
(`1`). This development slice is experimental: merge and live activation remain
gated on the accepted #192 real-provider pilot. No host profile, provider limit,
legacy lock or background default changes automatically.

```sh
skodun review-plan --repo . --batch-concurrency 2 --json
skodun review --repo . --batch-concurrency 2 --json
```

The first command is read-only. The second explicitly requests up to two
independent batch chains; it does not request multiple opinions on one batch.
The selected reviewer, fallback graph, complete diff, byte targets and prompt
boundaries are frozen before workers start. Zero- and one-batch reviews keep
the existing execution path. Prepush remains sequential.

At most two chains, including chains waiting for provider capacity, are active
for one request. The remaining plan does not enter the provider queue. Actual
overlap depends on existing provider/quota-pool and foreground repo limits;
setting this option does not raise those limits or disable legacy dual hold.
A peer already queued for the same provider cannot be overtaken by replenished
batches within a timestamp second: live admission and queue inspection use the
current SQLite insertion order as that timestamp's tie-break. This is not an
ordering guarantee across arbitrary external database maintenance.

Each worker owns its SQLite connection, request context, cancellation monitor
and budget callback. Shared budget state contains clocks, counters and memory
signals; it contains no Store or Store-bound callback. Snapshot callbacks are
serialized through their own worker Store, use fresh state and retain the
existing request/execution owner fence. Overlapping provider waits keep separate
deadlines and per-pass allowances. Request timing counts wall-clock overlap once;
provider-wait timing is an interval union, not a sum of parallel durations.

Completed results fold in frozen batch order. Integration waits for all batch
results, and required security/skeptic passes retain their existing dependency
bindings and scheduling. Ordinary unusable output keeps the existing failed
aggregate behavior and is never promoted as usable checkpoint evidence. Fatal
errors, cancellation or lost ownership stop new submissions and join active
workers before foreground ownership and scratch resources are released. Only
already-completed usable evidence can participate in compatible continuation.
Cancellation remains cooperative and includes SQLite busy/owned-watchdog cleanup
bounds; no code broadens process-group signaling to make cleanup appear complete.

Concurrency is explicit request, checkpoint, trusted-reuse and measured-history
identity. A `1`/`2` mismatch refuses compatible continuation or reuse; per-call
measurements from sequential execution cannot silently qualify a parallel target.
Sequential policy keeps the existing canonical representation. Preview shows
`call_counts.parallel_batch_limit`, but it does not predict a speedup or a request
completion time.

Rollback is explicit: select `1` for future requests. Finish or cancel an active
`2` request; never change its frozen plan halfway through execution. The owner
must accept the real-pilot evidence and effective limit inventory before live
rollout. Hermetic overlap/fairness tests are not that acceptance.
