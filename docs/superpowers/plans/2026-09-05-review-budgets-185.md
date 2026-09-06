# Separate review budgets (#185)

**Goal:** Queueing cannot consume a review's execution allowance; total request
caps still bound the full operation and provider waits behave identically in
every pass shape.

**Contract:** Optional max_queue_seconds caps foreground admission, optional
max_review_seconds starts at the first provider launch and includes later
provider waits but pauses foreground re-admission, and max_provider_wait_seconds caps admission across one pass's
fallback chain (default 30 seconds). Only actual admission waits spend this allowance; provider runtime does not. Provider hops share its remainder; every
new batch/integration/extra pass receives the same policy. Existing
max_wall_seconds remains a total cap, with the recovery default 900 seconds.
Expired provider waits may still try a free fallback immediately; they do not
cancel the entire request. Queue/review/total expiry cancels cooperatively and
never permits a new provider launch. Polling an existing request does not
alter its ticket or lifetime.

- [ ] Write pure fake-clock tests for queue vs execution time, provider shared
  admission limits, total cap, cancellation precedence, validation and frozen
  terminal timing. Implement budgets.py with an Event-compatible controller.
- [ ] Integrate in shared review service/request context, composing #183's
  RequestCancel upstream. Preserve explicit termination reason/state in #184's
  metadata; do not parse prose. Gate/trust remain unchanged.
- [ ] Wire foreground/provider admission and actual launch boundaries;
  suppress queue progress before a failed admission attempt. Keep #188's
  historical median/sample labels and actual effective-capacity observations.
- [ ] After migration18 lands, add migration19 budget snapshots keyed by
  request execution and per-admission capacity observations. Expose
  Store.request_budget(request_id) for #188 without guessing legacy data.
- [ ] Add CLI/MCP flags and active/final limits, deadlines, timings and layers.
  Use actual monotonic enforcement; persisted UTC deadlines are observations.
- [ ] Exercise shipped queue cancellation, delayed admission, mixed adapter
  eligibility, batched/unbatched/extra pass waits and reconnect idempotency.
- [ ] Self-review, run focused schema consumers and runtime tests, push one PR;
  merge and verify main under the owner-approved expedited review policy.

Persistence shape: scope=request_execution, request_id, execution_seq, phase,
limits (queue/review/provider/total seconds), deadlines (queue/review/provider/
total UTC), timing (queue_wait_ms,provider_wait_ms,review_wall_ms,review_active_ms,total_ms),
capacity_layers ({admission_id,resource_class,scope,effective_capacity,
configured_capacity?,legacy_dual_hold?}), updated_at. Old data remains unknown.

Self-review: execution time includes later waits by explicit design; provider
slot expiry alone can fall through, but no exhausted total/review budget can
spawn. Active owners are not stolen on observation timeout or lease expiry.

Implementation validation: 13 shipped execution tests passed, including actual
FIFO reconnection and mixed transport. Schema consumers: 337 passed, 1 heavy
sweep deselected. Broader surfaces: 578 passed, 20 skipped, with one batched
fixture failure subsequently corrected and covered by the 13-test run.
Store-only admission exposed an existing None legacy-lock sidecar update;
_grow_lock_budget now explicitly handles absent legacy locks. Provider wait
ceilings reserve every possible pass against existing grace, without shrinking
interop budgets. Current changes await independent review and final checks.
