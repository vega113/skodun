# Skill decision (epic #23 D)

**Decision (2026-08-02):** do **not** ship a separate thin skill package for
skodun in this epic.

**Rationale:** agents already get the full loop policy from:

1. `examples/AGENTS.md` (paste into client project AGENTS/CLAUDE/etc.)
2. MCP prompts `review-now` and `gate-check` (static text with stopping rule)
3. Live CLI/MCP tool descriptions via `services` (single wording)

A second skill file would duplicate those sources and drift. Client projects
(e.g. TubeScribes cutover) should copy `examples/AGENTS.md` rather than depend
on a skill registry.

**CLI-only ops (recorded for parity):** `doctor`, `retain`, `schedule install`
are intentionally not MCP tools — they are operator/maintenance verbs; agents
shell out when needed. Review-loop verbs stay on MCP through `services`.
