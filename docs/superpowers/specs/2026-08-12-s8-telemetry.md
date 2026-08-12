# S8.3 deterministic slicing and batch telemetry

## Scope

This slice adds a validated, per-review `batch_target_bytes` hint and exposes
the deterministic planner identity plus bounded batch/attempt telemetry in the
existing review artifact. It does not add a store migration, alter gate/trust,
or define the S7.1 receipt envelope while #147 is still open.

## Design

`batch_target_bytes = 0` keeps the shipped planner unchanged. A positive value
is clamped to the provider's effective batch budget and is used only to split a
complete diff more finely. The planner still uses `batching.split`, preserving
file/hunk boundaries and its irreducible-hunk truncation flag. The effective
defaults are already part of the exact S8.1 orchestration identity, so changing
the target invalidates checkpoint reuse.

The CLI and MCP review surfaces pass the same optional override through
`services.py` to `pipeline.run_review`; the config value remains the durable
default. Human and JSON review output expose the existing coverage projection
alongside a compact telemetry summary. Per-batch metadata records planner
version, budget, boundary digest, diff/context/checklist/prompt bytes, and
attempt telemetry. Attempt rows retain unknown token metrics as `None`; no
prompt, transcript, secret, full environment, or PATH is persisted.

Execution provenance is an allowlisted, sanitized projection of adapter
identity: provider, reviewer/model/effort, adapter name, and resolved
executable path when available. Version/build probes remain `None` in the
review artifact because a probe would be an extra provider invocation; the
explicit doctor/providers diagnostic surface owns those probes. The telemetry
layer never shells out to a provider or reads arbitrary environment values.

S7.1 receipt digests are represented only by an optional opaque field boundary
in the telemetry projection. No receipt keys or competing envelope are added
until #147 lands.

## Safety and compatibility

- No schema changes: artifacts remain JSON payloads in the existing review row.
- Existing trust axes and `gate.py`/`trust.py` are untouched.
- Legacy artifacts without telemetry continue to render with unknown/absent
  values.
- A smaller target can increase batch count, never reduce reviewed bytes or
  widen an adapter ceiling.
- Timeout and retry data use the already authoritative attempt fields; absent
  adapter token usage stays unknown rather than becoming zero.
