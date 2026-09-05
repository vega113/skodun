# Controlled foreground concurrency pilot (#192)

Goal: measure one versus two foreground reviews on a frozen four-worktree workload, while preserving every real capacity layer and keeping the safe default unchanged.

Current preparation baseline: main 99a3f94 contains request identity, guarded control, typed results, independent budgets, transport eligibility, lineage, refuter policy, and queue/cost observations. #186 compatible continuation remains a prerequisite before acceptance runs. PR #180 is still open at 366b721; no machine-wide layer is assumed to be installed.

The installed CLI was verified separately: 0.5.0 build 05d7dec, primary authority schema 16. A read-only capacity snapshot found a TubeScribes foreground holder and xai holder owned by PID22804, with a foreground wait from 13:17:07 to 13:41:28 UTC. PID liveness and a stored running ticket are evidence to investigate, not permission to cancel another review. No live store, provider, or environment settings were changed.

## Boundaries

- Hermetic benchmark authority is fixture data and may be isolated. Real provider runs must use the existing shared authority and its actual provider/quota limits; no alternate database may evade a machine or provider cap.
- Do not alter production defaults or disable legacy interop globally. A capacity-two profile is opt-in and only valid when every participant for that repository uses the supported protocol.
- Legacy participants are inventoried from actual active processes, lock ownership and wrappers. Registered worktrees with old files are potential participants, not proof of active holders.
- Freeze each workload before review. Never narrow the review's base/diff, hide findings, reuse failed evidence or auto-dismiss a finding to improve the benchmark.
- No metered adapter is enabled or selected implicitly. The live sample pins explicitly selected existing subscription reviewers and preserves their configured fallbacks only when all are subscription paths.
- No fixed speedup is a success criterion. A constrained or slower result is valid evidence; an unperformed real trial is not acceptance.

## Work and verification sequence

1. Build a bounded benchmark harness around the shipped CLI/shared services. Create one repository with four independent worktrees and deterministic benign diffs, recording base/head/diff identities. Run the same frozen workload under foreground capacity one and two with a hermetic provider executable. Each execution produces request IDs, raw typed results and queue projections.
2. Keep the hermetic baseline's other settings identical: store authority, provider capacity, workload, required pass policy and declared budgets. Parameterize a legacy-on profile and a provider-cap-one profile to prove that a tighter layer suppresses overlap. Every result reports the effective profile rather than inferring it from one environment variable.
3. Verify bounds and safety through shipped paths: at most configured foreground/provider overlap; all four FIFO tickets admitted without starvation; exact final identities; no duplicate finalization; cancellation/identity movement never certify partial work; only owned fake process groups are cleaned up. #186 supplies continuation coverage before accepting the profile.
4. Report raw interval observations, time to trustworthy coverage, request-level completion rate, queue time, launched attempts, skipped candidates and failure reasons. Derive elapsed time from real intervals, never summed overlapping holds. Include sample counts, units and methods; keep missing token/spend and external local-gate lock timing unknown.
5. Revalidate installed build, schema, actual processes/holders, legacy wrappers and PR180. Prepare a concrete install/migration/restart plan before any live change. A live store upgrade must respect the shipped maintenance lock and active-lease refusal. Do not migrate underneath running old workers or replace another task's active review.
6. Once a safe shared-authority window exists, run a small explicitly pinned existing-subscription sample on the frozen workload. Record the actual installed/source build and all caps. Keep provider limits unchanged; if provider or legacy constraints serialize it, publish that observed constraint. No extra paid traffic or broader default concurrency.
7. Publish a report/profile with reproducible commands, sanitized raw evidence, observed limitations and rollback criteria. Rollback restores the previous opt-in execution profile; schema rollback follows its own explicit backup/receipt procedure and never discards new authority records silently.

## Self-review

A fresh installed-build audit matters: merged code is not yet the running authority. The real trial cannot be simulated by bypassing production capacity through another SQLite file. A controlled repository can avoid foreign legacy participants, but only after its participant set is proven; provider capacity remains shared across repositories. Two fake overlapping processes demonstrate scheduler behavior, not a measured real-provider speedup. Keep #192 open until the real bounded sample and honest comparison are recorded.
