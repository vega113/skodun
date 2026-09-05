# Compatible batch continuation

Compatible continuation uses the request budget supervisor from #185. A new
execution may choose new runtime limits without changing coverage identity; the
existing structured termination/timing metadata remains authoritative.

Use an explicit continuation after a batched review returned unusable work or
was interrupted:

```sh
skodun review --continue --json
skodun review --continue --recover --max-attempts 3 --json
```

MCP `review` accepts `continue_compatible: true`. The shared service exposes the
same `continue_compatible=True` option. Combining it with `--fresh`, trusted
reuse, or a request key is refused as conflicting intent. Request keys retain
their existing idempotent replay contract. Plain `--recover` remains a fresh
second-opinion loop; `--fresh` still runs a new independent review. Ordinary
review retains its existing interrupted-resume policy; the explicit compatible
policy filters unusable returned checkpoints and can select consumed failures. Explicit
continuation requires existing incomplete batch work, and never silently starts
an unrelated unbatched review.

A usable batch/integration checkpoint must pass the existing payload validator, contain parsed
output, cover its complete diff, and have neither degradation nor a failure.
A returned failed result marked `complete` is a completed attempt, not reusable
review evidence. Compatible continuation retries that pass. Integration is
reused only when every batch is reusable and the integration checkpoint itself
is usable; otherwise integration runs against the new batch results.

The source orchestration stays unchanged. A new child generation copies usable
checkpoints in one transaction and leaves missing/failed work pending. Its
optional `continuation_source` identity field names the parent. The field is
omitted for original generations, preserving existing canonical identity bytes.
All existing repository, worktree, head, base, diff, context, checklist, reviewer,
configuration, policy, planner, boundary and pass identities must still match.
The generation namespace never weakens those content comparisons.

The source's advisory lineage cutoff is retained when rebuilding its context,
so the source's own newly recorded findings do not change its original prompts.
The rebuilt prompt/context hashes must still match exactly; changed checklist,
policy, content or plan boundaries are refused with a stable first-mismatch
field. No adaptive replan, truncation or approximate reuse is introduced.

Request ownership authorizes the fork and preserves the logical request ID.
Child creation, seeding and request linkage are atomic. Racing callers observe
the same active request or generation, while the existing fenced pass claims
prevent duplicate provider execution. Completed child generations remove their
ancestors from resume selection without rewriting historical source rows.
Atomic final publication requires all base passes and scheduled required follow-ups
to complete against their exact bindings. Bound decisions that decline a conditional
pass require no provider output.

The result's `continuation` extension records source/child IDs, a stable mismatch
when refused, and bounded reused/executed/failed pass receipts. Reused checkpoint
attempt IDs stay unchanged, so observed historical calls can be deduplicated.
Incomplete checkpoint evidence still does not clear the gate.

The #194 transport guard rechecks the actual complete prompt and the current
adapter capability before capacity admission. An unchanged oversized fallback
receives zero admission requests and zero process launches across continuation;
it is not cached as provider-wide failure. A verified capability change is
reconsidered. A changed/smaller diff that requires a different plan must take the
explicit fresh/replan path rather than borrowing mismatched checkpoints.

Batched foreground reviews also checkpoint required security and skeptic passes.
If security fails, continuation reuses usable batches/integration, retries security,
and recomputes whether skeptic is required. If skeptic fails after usable security,
only skeptic runs again. Refuter remains optional annotation, is not checkpointed,
and its failure does not become a required-pass failure. Unbatched and prepush
execution retain their existing pass policies.

Follow-up bindings include exact upstream aggregate/results, actual contributor
and adapter provenance, prompt bytes/cap/truncation, context/checklist, and loaded
reviewer/configuration/policy identities. Usable follow-up evidence requires parsed,
nondegraded output without failure. The existing extra-pass size-cap policy remains:
a capped security/skeptic prompt annotates partial coverage without demoting a
complete base review. Reuse requires that exact capped prompt identity; base
batch/integration evidence still requires complete diff coverage.

Schema 20 adds follow-up checkpoint rows under the same orchestration authority.
Inspection never migrates the store. A continuation child initially holds prior
follow-up results only as candidates; a rebuilt matching binding promotes them.
Changed upstream evidence invalidates dependent candidates with a bounded reason
in their pass receipt. Global configuration/policy changes still refuse the whole
continuation with the existing first-mismatch field. Sources from the earlier
batch-only planner require a fresh review; existing published reviews stay readable.

Claims, completion and atomic publication validate the binding and final output
identity. Lost or pending output cannot become gate coverage. Fences prevent
concurrent duplicate calls; a crash after a provider answers but before durable
completion can require a new measured call because CLI providers supply no
idempotent receipt for recovering that lost output.

Malformed stored pass identities normalize to the stable identity-mismatch
refusal. A known source-request mismatch is rejected before another request is
created, with the actual differing field; missing ownership alone does not invent
an identity mismatch. Explicit receipts are marked by
`request.continuation_policy=compatible`. This differs from the older
`request.continued` flag, which also describes ordinary same-request resume.
Duplicate receipt locations must agree and match the observed generation; early
failures before a generation and ordinary resume do not require an explicit pass
receipt. Local integration preparation failures count as failed local passes,
without fabricating a provider launch or completing a pending checkpoint.
