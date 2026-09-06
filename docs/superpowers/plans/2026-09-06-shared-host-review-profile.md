# Shared host review profile

Observed on current main 3a872c7: the installed wheel matches main, all observed
MCP starts postdate installation, and TubeScribes review-fg admissions still use
capacity 1 with legacy dual hold. Request 4162 waited 3863 seconds and reviewed
for 678 seconds. CLI/Claude provider capacity is 2; Codex MCP has no override.

Implement a small optional launcher that reads one operator-owned shell profile
before execing an explicitly configured installed Skodun executable. It gives
CLI and MCP the same capacity settings without changing product defaults,
provider routing, gate/trust, or schema. Document install, version checks,
current-only legacy cutover, in-flight drain, client refresh and rollback.
Hermetic launcher tests cover argument forwarding, environment propagation,
missing/invalid profile, recursion refusal and process exit status.

Activate an owner-authorized two-slot foreground profile on this host after
checking active participants; retain the existing provider cap of two. Keep
ongoing requests intact. Existing MCP processes cannot be hot-reconfigured;
configure future starts consistently, and report any connections still needing
host refresh rather than killing their pipe and claiming a successful restart.

Self-review: no package upgrade is needed for this operational fix. A wrapper
must not call its own PATH alias or load project-controlled configuration. The
profile is trusted host configuration, contains no secrets, and may restore
capacity 1/legacy lock for rollback. Multi-slot activation is an operator choice,
not a new default or a promise of a specific speedup. Run focused tests and the
repository suite, inspect PR feedback/checks, merge and smoke the merged files.
