# Preview review scope and call sizes

`skodun review-plan --repo PATH --json` prepares the same full diff and prompt
inputs as review execution without starting providers, creating requests,
claiming checkpoints, entering capacity queues, or writing configuration/store
state. MCP `review_plan` returns the same read model. Missing or old stores stay
unchanged and yield an explicit history-unavailable result.

```sh
skodun review-plan --repo . --reviewer finder --batch-target-bytes 20000 --json
skodun review-plan --repo . --reviewer finder --target-source measured \
  --target-latency-seconds 30 --json
```

A preview is a snapshot, not a review, request reservation, or gate certificate.
Exit `0` means the plan was produced; `2` means invalid input, unavailable scope,
a stale tree, or required inputs known to be unreviewable. Check the individual
paths and truncation flags: future integration/refuter inputs are still unknown.
The `scope_capture` fields expose an untracked capture limit explicitly; such a
preview cannot claim all diff bytes are preserved or return application arguments.
Execution captures its own current inputs. Re-run the preview after editing the
tree, rebasing, changing configuration, or changing stack annotations.

## Scope and byte accounting

The result includes the resolver source, requested stack/push base, resolved
base ref and SHA, head, worktree identity, full file/diff counts, content hash,
planning identity, and boundary digest. A stack manifest annotates the execution
resolver's base; it never silently substitutes a narrower base. Invalid stack
annotations retain the full authoritative diff and their validation result.

Primary and batch inputs report exact rendered bytes and hashes. The primary
aggregate is the sum across those calls; the maximum is the largest single call.
For example, an 18-batch aggregate can exceed six megabytes while each batch is
under 500,000 bytes. Byte envelopes and transport limits are not model context
or latency guarantees. Each configured fallback is checked against the actual
input using its adapter capability, independently of the head's sizing envelope.
The preview does not probe quota, binaries, or provider health. Size-capped
security/skeptic inputs retain the execution contract: explicit partial advisory
coverage, rather than a primary-coverage failure.

Integration includes every planned batch. Its structural preview uses empty
result slots to expose known size problems, but final summaries/findings do not
exist yet: exact integration bytes remain unknown. Refuter inputs also depend on
future findings and actual contributor families. The clean-result skeptic and
dirty-result refuter are conditional alternatives. Retry/fallback counts are
upper bounds, never expected launches. Display limits retain full aggregate
counts and explicit truncation flags.

## Measured targets are explicit application suggestions

Measured mode requires a declared per-call latency objective. It qualifies a
cohort only with at least 20 unique launched attempts from at least five requests
inside the named 30-day window, complete matched input/outcome data, and zero
observed failures or censored timeouts. These are conservative policy thresholds,
not statistical confidence guarantees. Candidate skips are not launches; copied
checkpoint attempts are deduplicated independently of enclosing request IDs.
Requests linked by copied attempts count as one request group, so copies cannot
inflate the minimum request count. Unsupported policy versions are labeled
separately; known unrelated running reviews do not invalidate a matching cohort.
Unattributable outcomes in matching policy sources remain incomplete.
Conflicting identities, missing sizes or
planning provenance, and bounded-query truncation prevent qualification.

Cohorts preserve provider/model/effort, foreground versus prepush mode,
primary versus batch kind, planning/capability version, context policy, maximum
turns, a bounded hash of the denied-tools policy, and observed context/input ranges. The report includes scanned counts, sample IDs/digest,
request count, failure/censoring counts, nearest-rank historical quantiles and
provenance limitations. Provider executable versions are often unavailable;
preview does not launch a version probe to fill that gap.

The largest qualifying observed diff target meeting the historical p90 objective
is considered below the hard provider prompt/diff envelope. In explicitly
requested measured mode, a qualifying target may replace a smaller soft
configured batch target; the configured value remains the fallback when evidence
is insufficient. The shipped planner checks
the entire candidate, including integration's structural floor and every required
input. Its largest primary/batch prompt and context size must fit the observed
ranges, and observed timeout caps must not exceed the applicable execution cap.
Smaller remainder batches outside those ranges retain unknown timings.
Insufficient, stale, mismatched, missing, censored or failed evidence retains the
configured target with a reason. A positive explicit `--batch-target-bytes` always
wins; zero retains the shipped
meaning of using the configured/default planner.
Unpinned automatic foreground routing does not select a measured target.

For foreground review, `selection.application` contains fixed existing
`--batch-target-bytes N` arguments. Apply those to `skodun review` with the same
explicit reviewer intent. Nothing writes configuration, probes a live model,
resamples history during execution, or adapts the target during a request.
The fixed target is part of the durable request configuration and checkpoint/
trusted-reuse compatibility. Even a target change that leaves boundaries
unchanged invalidates incompatible work with a planning mismatch reason. Old
artifacts with unknown planning identity cannot satisfy the new reuse check.
Gate and trust semantics remain unchanged.

Historical per-call quantiles describe matching observations. A sum of per-call
p90 values is not a request p90. The request runtime range and expected conditional
launch count therefore remain unknown; queue contention is also unknown. The
hermetic 18-versus-9-batch fixture demonstrates smaller maximum inputs versus more
calls/integration participation; it is not measured provider performance or a
speedup promise.

## Prepush and historical breadth

Supply all four pushed-ref fields to reproduce dispatch scope:

```sh
skodun review-plan --repo . --mode prepush \
  --local-ref refs/heads/topic --local-oid LOCAL_FULL_OID \
  --remote-ref refs/heads/topic --remote-oid REMOTE_FULL_OID --json
```

Existing branches use the exact remote OID as the base. New branches use the
shipped ref resolver (supply an all-zero remote OID). The pushed local OID, not
uncommitted edits or the checked-out branch, supplies the head. Prepush uses the
configured finder and dispatch defaults; it does not accept a foreground reviewer
override. A selected prepush target returns `prepush_configuration_target` and
instructions to manually set `defaults.batch_target_bytes` before dispatch.
It never returns a foreground review command as a substitute for pushed-ref scope.

`--review-id ID` can explain a historical prepush record from the same repository
while its Git objects remain available. Stored aggregate/max/breadth observations
are labeled separately from a reconstruction under current configuration.
Historical working-tree edits and missing Git objects cannot be reconstructed.
Task intent and unavailable historical configuration remain unknown; broad scope
alone is not evidence of an engine defect.
