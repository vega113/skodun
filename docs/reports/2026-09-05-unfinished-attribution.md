# Unfinished review attribution: historical sample and limits

Source: the September 5 read-only sample summarized in issue #183. This is a
historical 24-record sample, not a new production query, recovery, or retry.

| Recorded evidence | Count | Supported classification | Not established |
|---|---:|---|---|
| Failed with the cancellation-token unfinished-finalization reason | 17 | Finalization did not finish and the token was observed set | Initiator, user intent, reason for token, correctness of cleanup, original signal source |
| Failed with the unfinished-finalization reason without cancellation | 7 | Finalization did not finish; cancellation not named | Process loss, persistence failure, crash, provider cause, or engine defect |

All 24 remain unknown with respect to the initiating actor and root cause.
The 17 token observations are not proof of explicit operator cancellation;
the 7 other rows are not seven proven engine defects. No evidence reproduced
bare cancellation: the existing CLI parser and MCP tool require an ID.
Unscoped status selection was a separate, demonstrable targeting hazard.

New execution-fenced audit events record explicit operator/client cancellation
before control, and lifecycle observations distinguish signal, disconnect,
disconnect drain timeout, recovery deadline, and request-budget expiry when
that evidence exists. A dead PID is only a current process-loss observation.
It never repairs a row or proves why a historical process exited. Structured
termination evidence can name persistence/readback failure without inventing
an initiator. Otherwise attribution remains unknown.

Hermetic fixtures exercise four worktrees, queued/running explicit cancellation,
wrong guards, unknown identity, independent request survival, continuation
fencing, signal/disconnect/deadline causes, absent owner processes, and
cancellation/finalization/readback races. They justify the scope and audit
instrumentation, not a speculative correction to historical finalization.
No production cleanup, provider calls, or retries were performed for this work.
