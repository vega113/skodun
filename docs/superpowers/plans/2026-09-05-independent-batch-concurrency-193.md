# Independent Batch Concurrency Design and Implementation Plan

> **For agentic workers:** After the acceptance gates below, use `superpowers:executing-plans` to implement task by task. Hermetic implementation is now authorized on main e7ed2e6. Merge and live activation remain gated on #192 real-pilot acceptance. Do not spawn agents, change host configuration, or run live providers.

**Goal:** Offer explicitly requested overlap of at most two independent, frozen batch subreviews while retaining sequential defaults, current admission authorities, deterministic full-diff aggregation, and fail-closed publication.

**Architecture:** One coordinator owns request admission, the prepared plan, ordered aggregation and publication. At most two worker threads each open their own Store and install their own request/cancellation/budget handles over shared in-memory execution state. Existing provider FIFO tickets and fenced checkpoint claims remain the only admission and completed-evidence authorities.

**Tech stack:** Python 3.12+ standard library (`concurrent.futures`, `contextvars`, `threading`, `queue`), existing SQLite Store, pytest and hermetic fake-provider processes.

## Status and acceptance gates

Inspected main: `25662a16ddd9ddff1e6c348dc9e36ba456f0f8b6` (PR #209 merged). This worktree is `/Users/vega/.codex/worktrees/ep181-parallel-batches/skodun`, branch `codex/ep181-parallel-batches`. Implementation is authorized after #187 landed at e7ed2e613104491ba1294b228f0d6e5c6537f749; #190 is closed. The initial snapshot below is historical. No production configuration change is authorized.

| Dependency | Live observation at design time | Required before merge/live activation |
|---|---|---|
| [#187](https://github.com/vega113/skodun/issues/187) / [PR #210](https://github.com/vega113/skodun/pull/210) | Open; inspected proposed head `a2499e488babc92d1c559ad6344d87861f93f3c1` read-only | Landed required-follow-up checkpoint bindings, lease correction, review/CI and merged-main acceptance |
| [#189](https://github.com/vega113/skodun/issues/189) | Closed | Preserve independent refuter provenance; no voting or new reviewer selection |
| [#190](https://github.com/vega113/skodun/issues/190) | PR #209 merged; issue still open at inspection | Coordinator's merged-main acceptance/closure; reuse the landed exact preparation and sizing identity |
| [#192](https://github.com/vega113/skodun/issues/192) | Open | Accepted two-review pilot evidence, effective repo/machine/provider limits, legacy interoperability and rollback criteria |
| [#193](https://github.com/vega113/skodun/issues/193) | Open | Coordinator selects the bounded choices below, then authorizes implementation |

Re-read live issue states and main after these gates. A merged implementation is not a substitute for #192's measured acceptance. The coordinator owns pilot workload selection, live-provider authorization/window and conclusions. No production default increase follows from this design or from a synthetic overlap test.

Design baseline check: `python3 -m pytest tests/test_budgets.py tests/test_checkpoints.py -q --tb=short` → **71 passed** in 0.39s. This is a narrow baseline check, not full-suite or #193 acceptance.

## Findings in the shipped seams

- `pipeline._prepare_batch_plan` already materializes batch prompts, packed context, checklist selections and exact pass/boundary hashes before the sequential `_orchestrate` loop. Worker threads must consume these objects; they must not recapture Git state, repack files, resolve another head or change the target.
- `_orchestrate` currently mixes execution with ordered list accumulation. Its aggregation is explicitly batch order, and its integration builder consumes every batch's diff, summary and findings. Ordinary unusable subresults are still aggregated; this design preserves that behavior and its trust consequences.
- `_checkpointed_sub` claims/completes/releases through one Store and uses `_checkpoint_lease_seconds`. PR #210 changes the lease helper to obtain the current, store-owned request provider-wait allowance. Losing `requests.current()` or passing the wrong Store in a worker would silently restore the legacy allowance, risking premature expiry.
- `requests.RequestContext` is frozen but contains the executing Store and budget object. `requests._CURRENT` is a ContextVar; `budgets.current(store)` deliberately requires identity equality with that thread's bound Store. A blind `copy_context()` would copy the coordinator's Store/controller references and is unsafe.
- `RequestCancel.is_set()` and `.wait()` read/write through their captured Store. `ReviewBudget.is_set()` calls its captured cancellation monitor, currently while holding its RLock. Sharing the current controller unchanged would therefore allow worker watchdogs to call the coordinator's SQLite connection.
- `ReviewBudget._update()` invokes persistence in the mutating thread. `requests.tracked_review` currently binds that callback to the active request context. Merely giving the chain a separate Store does not repair a callback or cancellation monitor that still closes over the original connection.
- `ReviewBudget` counts overlapping provider waiters and union wait time, but has only one `provider_deadline_mono`; later starts overwrite earlier deadlines. Parallel passes need independently identified active deadlines and balanced end operations.
- `budget_store.save_request_budget` fences owner and execution sequence, but timestamp ordering has second precision. Concurrent callbacks must not publish older snapshots after newer or terminal snapshots in the same second.
- Provider slots are acquired inside `chain.run_chain`, including fallback and deterministic input-eligibility handling. A chain entry may retain its slot across bounded retries. The scheduler must not create tickets or claims for an entire backlog, and it must not move tickets ahead of peer requests.
- `pipeline._PROGRESS` is `threading.local`, not a ContextVar. Worker output must be marshalled through the coordinator rather than relying on copied context or writing JSON-RPC/stdout from multiple threads.

## Recommended choices for the coordinator

1. **Initial surface: foreground durable requests only.** Add a per-request CLI/MCP `batch_concurrency` value in `{1, 2}`, default `1`; reject booleans and other values before admission. Keep prepush sequential in this first slice. Background parallelism needs its own record-cancellation audit and requestless budget design and can be a separate follow-up. No implicit config/profile edits occur.
2. **Execution: two threads with local handles over one shared budget state.** This reuses provider watchdogs and Store fencing while avoiding a second process-orchestration protocol. A process pool is an alternative only with a separate design for cancellation, request ownership and result transport; it is not a fallback for failing thread tests.
3. **Fairness: bound queued-or-running chains, not only spawned processes.** At most two worker tasks per request may exist at once, including provider waits. Submit the next batch only after a slot actually finishes. Existing FIFO controls which admitted request's provider ticket runs next; no extra fairness database or global scheduler is introduced.
4. **Identity: strict explicit concurrency compatibility.** Store a versioned batch-execution policy and compare it for checkpoints, compatible continuation, exact reuse and calibration. Changing `1 ↔ 2` explicitly invalidates incompatible work. Prefer strict mismatch over guessing that old unlabelled measurements represent parallel execution. Any compatibility optimization for known legacy sequential identities is a separate acceptance choice, not required to ship the opt-in mode.
5. **Failure behavior: retain sequential semantics for ordinary bad output.** An unusable `_Sub` remains an unusable diagnostic; remaining planned batches may run and existing aggregation/integration policy applies. Cancellation, identity/lease loss or an infrastructure exception stops new submissions, cancels active workers through owned watchdogs, joins them, and forbids aggregate publication. Do not relabel internal failure as a user cancellation audit.

The alternative activation surface is a persisted configuration default of `1` with a per-call override. The recommended per-request-only surface is narrower, avoids changing configured behavior of prepush, and makes rollout intent explicit in request identity. Select the surface before implementation; both must retain default sequential execution.

## Coordinator/worker protocol

### Before submission

The existing coordinator performs foreground admission/legacy dual hold once for the request. Workers do not acquire additional `review-fg` tickets and do not bypass repo or machine request limits. Freeze configuration/reviewer intent, effective byte target, concurrency policy, base/head/tree fingerprint, complete diff, prepared batch prompts, checkpoint generation and follow-up plan before creating workers.

The sequential path remains explicit and uses the same ordered result-folding helper as the parallel path. A zero- or one-batch review does not create an executor. Concurrency `2` without a durable request context, usable file-backed Store path, or fenced checkpoint generation is refused before provider work; do not silently drop the request budget or use an independent database.

Define one new `src/skodun/parallel_batches.py` coordinator responsible only for bounded execution and handoff. It receives the already prepared batch entries and a worker callable; it does not select providers, construct prompts, aggregate findings, or publish reviews.

Proposed protocol types (design contracts, not existing APIs):

```text
BatchExecutionPolicy(version="independent-batches/v1", workers=1|2)
WorkerSpec(index, prepared_entry, effective_defaults, checkpoint_run,
           request_id, execution_seq, owner_token, canonical_store_path)
WorkerOutcome(index, owned_subresult, checkpoint_identity)
execute_bounded(specs, workers, run_one, coordinator_poll) -> outcomes_by_index
```

`execute_bounded` submits at most `workers` specs and replenishes in ascending batch index only after a completion. The coordinator uses bounded future waits (for example 50ms slices), drains progress events and polls its own cancellation/budget handle. Futures may finish in any order; results are stored by the frozen one-based batch index.

### Worker ownership and context

Each worker opens and closes `Store.open(canonical_store_path)` inside its own thread. The database must already be current and initialized by the coordinator; ordinary opens cannot migrate an old schema. Never use `check_same_thread=False`, pass a live connection across threads, or open a different database to obtain more capacity.

Each task creates its own `contextvars.copy_context()` and installs a replacement `RequestContext` containing the same request ID, owner token, execution sequence, immutable configuration and identity, but its own Store and budget handle. Never enter the same `Context` simultaneously in two threads. Copying context is not sufficient until those references are replaced.

The worker installs/restores a task-local progress sink that puts advisory progress events into a bounded thread-safe queue without blocking. If full, coalesce/drop advisory updates with a counter; terminal outcomes and failures travel through futures and are never dropped. The coordinator alone calls the external progress sink. Existing chain ContextVars for input bytes and execution provenance are reset on entry and restored on exit; a reused executor thread cannot inherit another batch's skipped/accepted provider metadata. Scratch tags retain generation + batch + entry + attempt uniqueness.

The worker runs the shipped `_checkpointed_sub` with its local Store, effective defaults and exact prepared prompt. The current store-owned request context must reach both `budgets.current(worker_store)` and PR #210's lease helper. Return only owned result data and index, never a Store, context token, callback, capacity ticket or live process handle. Close the Store in `finally`, reset ContextVars/thread-local progress, then complete the future.

### Shared budget and cancellation state

Refactor the budget internals before enabling the executor:

- A shared in-memory state owns monotonic start times, immutable limits, queue/review/total elapsed state, terminal reason, a stop Event, waiter records, and synchronization locks. It contains **no Store, RequestCancel or callback bound to a Store**.
- Each thread has a budget handle over that same state with its own Store-bound cancellation monitor and fenced snapshot callback. The coordinator retains its own handle. Do not mutate the shared controller's `.cancel` or `.on_update` in place to install worker references.
- Worker `is_set`, `wait`, `provider_started`, allowance bookkeeping and snapshot publication can only touch its local Store. Durable request ownership/cancel reads remain fenced to the same owner/execution. Signal/disconnect handlers set memory state only; audit runs in an executing thread through that thread's Store. Coordinate the in-process audit latch so two workers do not duplicate the same observed signal audit.
- Never perform SQLite I/O or callbacks under the shared budget-state lock. Mutate/copy state briefly under that lock, release it, then perform thread-local I/O. A separate shared publication lock serializes callbacks: once acquired, take a **fresh** snapshot and write it through the calling thread's fenced Store. This avoids same-second stale snapshot overwrite without inventing a second execution authority.
- No code may acquire the publication lock while retaining the state lock. Snapshot callbacks hold the publication lock, briefly read state, release the state lock, then write SQLite. A callback refusal due to lost owner/execution broadcasts stop and fails closed; it is not advisory success.
- Request review allowance starts once, at the first real provider launch across workers. Overlap counts as elapsed request time, not a sum of worker durations. Total allowance remains end-to-end; provider waits spend the corresponding pass allowance but do not consume another pass's allowance. Foreground readmission remains coordinator-owned and occurs only with zero active batch workers.

Internal scheduler failure gets a separate stop latch and primary exception owned by the coordinator. It must not fabricate a user cancellation event. Existing watchdog cancellation still performs proven process-group cleanup; after workers join, the coordinator reports the original lifecycle/infrastructure failure through the shipped failure mapping.

### Multiple provider waits

Give each `ProviderAllowance.waiting()` interval an opaque in-memory token associated with its logical pass. `start_provider_wait(token, remaining)` inserts exactly one active interval; `end_provider_wait(token)` removes that exact interval. An unknown/duplicate end is an invariant failure, not a clamp that can hide another worker's active wait.

Maintain an active token-to-deadline map. The legacy scalar provider deadline is the earliest active deadline, or null when none remain. Add a bounded active-wait projection (maximum two independent batch workers in this slice) with logical pass IDs, deadlines and count; update `budget_store`'s strict snapshot/read-model validation additively. Do not persist tokens, owner secrets or callbacks.

Keep `provider_wait_ms` as the union of intervals with at least one active provider waiter. Per-pass elapsed waits remain in existing attempt/capacity telemetry. Ending worker B's wait must not clear worker A's earlier deadline. Fallback entries spend the same pass's cumulative remaining wait budget; each new batch gets its own allowance. Admission expiry remains pass-local until existing pipeline/request failure policy handles its result. Cancellation/total/review expiry remains shared.

## Fairness, leases, results and barriers

- At most two queued-or-running batch chains from one request enter provider admission. Do not pre-enqueue the remaining plan or every fallback. A worker checks cancellation before claim, before provider admission and before launch.
- Provider/quota-pool admission remains the existing FIFO, including deterministic transport skips and per-entry retry behavior. An admitted peer ticket cannot be overtaken by a newly replenished batch from the large request. Fairness is at the existing chain-entry/retry boundary; this plan does not preempt a live owned model process to rotate a slot.
- Repo/machine caps count admitted requests as they do today; provider caps count provider holders. A request blocked outside foreground admission is not falsely reported as a queued provider peer. #192 must provide a profile in which simultaneous admitted requests can actually be measured.
- Each active batch claims only its own frozen pass with a distinct claim token/fence. Lease duration includes effective (possibly escalated) attempt/retry bounds and the local handle's full pass admission allowance, using the landed #187 helper. Verify the bound also covers serialized snapshot/SQLite busy waits; tighten bounded worker I/O or add explicitly fenced renewal if the measured worst case exceeds it, never permit an expired live claim to duplicate a launch. Never multiply shared review/total allowances by the worker count or silently shorten safety leases because nominal parallel runtime looks smaller.
- Failed claims/in-flight peer claims launch zero providers for that pass. A lost claim/fence or request owner stops new work; stale workers cannot complete or publish. Existing usable completed checkpoints may remain for compatible continuation. Unusable/empty/failed-output payloads remain diagnostics and cannot be promoted as reusable evidence.
- After all worker futures finish, fold outcomes in exact batch order through one helper shared with the sequential branch. Preserve findings order, files, context/checklist unions, bytes, attempts and actual contributors; wall timestamps/durations are observations and may differ.
- Only then run the existing integration builder over all batches in order. Preserve its current handling of unusable upstream results and complete-diff truncation; do not omit failed regions, introduce hierarchical integration, or claim partial coverage as complete.
- Required security/skeptic scheduling and bindings from #187 happen after the batch/integration barrier. Security and skeptic remain conditional under existing policies; refuter stays optional, independent and uncheckpointed as currently designed. No worker schedules these passes speculatively.
- On cancellation/infrastructure failure, stop submission, signal active owned watchdogs, drain/join workers and settle their claims/tickets before releasing coordinator foreground ownership or scratch lifetime. The documented cancellation bound must include the existing SQLite busy timeout and watchdog cleanup grace, with a blocked-writer fixture proving termination; do not describe thread cancellation as instantaneous. Do not return a background thread that still references a closed Store, and do not broaden process-group signaling to make cleanup appear successful.
- Revalidate final Git/config/plan identity and request/generation fences through the shipped publication path after the barrier and required follow-ups. Moving the tree or changing policy invalidates publication even if every completed batch once looked usable.

## Bounded implementation slices after authorization

### Task 1: Freeze the option and identities

Files: `src/skodun/services.py`, `src/skodun/cli.py`, `src/skodun/mcpserver.py`, `src/skodun/requests.py`, `src/skodun/planning_policy.py`, `src/skodun/checkpoints.py`, `src/skodun/plan_preview.py`; tests in `tests/test_parallel_batches.py`, `tests/test_requests.py`, `tests/test_mcptools.py`, `tests/test_checkpoints.py`, `tests/test_plan_preview.py`.

- [ ] Add failing service/CLI/MCP tests for omitted/1/2, boolean/0/3 refusal, request replay with changed concurrency, and refusal of unsupported background/requestless use.
- [ ] Implement the selected per-request option before request acceptance; carry the immutable policy in RequestContext and the review artifact. Include it in request and orchestration compatibility without touching `security_policy_identity`, gate or trust.
- [ ] Add explicit concurrency mismatch reasons and matching calibration provenance. Sequential versus parallel history cannot be silently pooled. Preview reports worker limit and barriers; it does not promise overlap, select another provider or change byte boundaries.
- [ ] Verify exact prompts/boundary digests remain equal for degree 1 and 2 on the same frozen input, while execution-policy compatibility differs.

### Task 2: Repair budget/cancel thread ownership first

Files: `src/skodun/budgets.py`, `src/skodun/request_cancel.py`, `src/skodun/requests.py`, `src/skodun/budget_store.py`; tests `tests/test_budgets.py`, `tests/test_budget_store.py`, `tests/test_budget_execution.py`, `tests/test_cancellation.py`.

- [ ] Use fake-clock tests for overlapping waits A=[0,5], B=[2,4]: union wait=5 seconds; B completing at 4 leaves A's deadline active. Reverse completion order and verify exact active tokens/count and earliest deadline.
- [ ] Prove fallback runtime does not spend its admission allowance; a second waiting worker cannot overwrite, extend or clear the first worker's allowance/deadline.
- [ ] Add thread-affinity guards that raise if any worker calls the coordinator Store, RequestCancel, or callback. Drive actual services plus worker watchdog/capacity polling; testing only a fake Event is insufficient.
- [ ] Implement local handles/shared pure state and serialized fresh-snapshot callbacks. Force reverse callback readiness within one UTC second, and verify persistence never regresses timing/phase or overwrites terminal state with a live state.
- [ ] Force SQLite read/write failure and owner/execution replacement while workers poll; assert shared stop, correct existing failure reason, and no late unfenced snapshot accepted.

### Task 3: Add the bounded executor and shared ordered fold

Files: create `src/skodun/parallel_batches.py`; modify `src/skodun/pipeline.py`; tests create `tests/test_parallel_batches.py` and extend `tests/test_batched_review.py`.

- [ ] Extract ordered batch aggregation into one helper used by both paths, with existing sequential fixtures unchanged.
- [ ] Implement the two-active-task submission window, per-task local Store/context/progress setup and teardown, owned outcome transfer, and coordinator polling/join protocol.
- [ ] Use two blocked fake CLIs with explicit filesystem/barrier acknowledgements: prove two overlap in degree 2, no third starts, and degree 1 never overlaps. Avoid sleep-only timing assertions.
- [ ] Complete batches 2,3,1 in controlled order and compare sequential versus parallel semantic outputs, contributor provenance, full diff coverage and integration input bytes. Exclude real timing/attempt IDs from equality; do not hard-code a replacement aggregator in tests.

### Task 4: Exercise racing requests and the failure perimeter

Files: `tests/test_parallel_batches.py`, `tests/test_capacity.py`, `tests/test_continuation.py`, `tests/test_followup_checkpoints.py` (landed #187), `tests/test_store.py` inventory; production changes only at demonstrated seams in Tasks 1–3.

- [ ] Start two real service requests in separate worktrees against one temporary Store path, with explicit fixture repo/machine caps and provider capacity. Admit B's provider ticket while A's first two batches are blocked; release one A slot and prove B enters before A's replenished third batch. Check durable FIFO order and no more than two A tickets, not just eventual completion.
- [ ] Repeat with provider cap=1, machine request cap=1 and repo request cap=1 separately; assert the correct layer serializes the workload and the report names that layer. Never use separate databases to make the test pass.
- [ ] Race two compatible continuers of the same generation; an in-flight claim produces zero duplicate launches. Advance ownership/claim fences while a provider is blocked and prove the stale completion and aggregate are refused.
- [ ] Test long provider wait with tiny model timeout under both workers, using #187's lease calculation: a peer cannot steal either active pass at the old shorter expiry.
- [ ] Cancel while one worker waits for capacity and another runs; cancel after one durable completion; cancel after all batches but before integration; cancel during a required follow-up. Assert no new provider launch, owned cleanup, released tickets, closed thread-local connections, only already-usable evidence resumable, and no aggregate certificate.
- [ ] Return empty stdout, malformed output, degraded output, silent timeout, after-output timeout and lost output files from different batches. Preserve existing fallback distinctions and deterministic failed aggregate; none is a usable checkpoint. Let a sibling complete first to prove success cannot hide failure.
- [ ] Change the working tree after preparation, during a worker, and after all batches before final publication; each case fails exact final identity checks without discarding the original captured byte accounting.
- [ ] Run mixed-provider fallback outcomes and prove exact actual contributor sets survive result reordering into integration and independent refuter eligibility.

### Task 5: Verification, benchmark and gated rollout

Files: `docs/review-plan.md`, `docs/compatible-continuation.md` (landed #187 path must be rechecked), new `docs/parallel-batches.md`; existing #192 pilot artifact location after acceptance. Register every new Store-touching test in `tests/test_store.py`.

- [ ] First run `python3 -m pytest tests/test_parallel_batches.py tests/test_budgets.py tests/test_budget_store.py tests/test_budget_execution.py tests/test_capacity.py tests/test_batched_review.py tests/test_continuation.py tests/test_followup_checkpoints.py tests/test_plan_preview.py -q --tb=short`.
- [ ] Run the full hermetic suite and the required separate lifecycle sweep according to AGENTS.md. Report exact counts and interruptions; a stalled sweep is not green. Add schema migration tests only if implementation demonstrates a required additive store change; the proposed separate connections and optional snapshot fields do not themselves require a new table.
- [ ] Run a reproducible offline degree-1/degree-2 comparison on identical frozen multi-worktree inputs using deterministic fake-provider delays and barriers. Record request count, exact-diff count, actual launches/retries/skips, trustworthy completion, elapsed critical path, per-layer queue union time, per-pass waits, failures, process/connection peaks and integration/follow-up overhead. No fabricated token/dollar totals or N-fold promise.
- [ ] After #192 acceptance, the coordinator separately decides whether to authorize a bounded live comparison using the same existing providers/profile and constraints. Stop on duplicate work, stale publication, unsafe cleanup, cap/fairness violation, or failure-rate regression; restore degree 1 for future requests and explicitly cancel/finish any degree-2 request instead of changing its frozen policy mid-run.
- [ ] Complete exact-head review/CI, merge, merged-main focused smoke and linked issue closure. Keep default 1 unless a later explicit product decision backed by evidence changes it.

## Self-review

- **Coverage:** every #193 acceptance item maps to Tasks 1–5: bounded overlap/barriers; simultaneous caps/FIFO; fencing/identity/cancel/lost-output; deterministic contributor-aware aggregation; benchmark and sequential rollback.
- **Thread safety:** separate SQLite connections alone are insufficient. Local cancellation and budget handles, ContextVar replacement, thread-local progress, callback serialization and lock ordering are explicit prerequisites.
- **Accounting:** request wall time is not summed worker runtime; provider waiting uses interval union plus per-pass allowances and active deadlines. No second waiter can overwrite the first deadline or emit a newer-looking stale snapshot.
- **Fairness limits:** bounded submission prevents a large request from pre-reserving its backlog. Existing finite retries may hold a provider slot; this plan does not promise preemption or admission for peers blocked by an intentional foreground cap.
- **Scope:** no provider selection, model voting, runtime target resampling, integration redesign, gate/trust edit, production profile mutation, or live measurement occurs in this design. Default sequential behavior and existing unusable-result semantics remain explicit.
- **Integration dependency:** PR #210 is an inspected proposal, not a landed authority at this snapshot. Rebase/re-inspect after #187 acceptance before applying the lease/follow-up interfaces described here.
- **Choices returned:** approve per-request foreground-only 1/2 with local budget handles and strict policy compatibility (recommended), or separately expand the activation/background/process model. None of these choices waives #187/#192 acceptance gates.
