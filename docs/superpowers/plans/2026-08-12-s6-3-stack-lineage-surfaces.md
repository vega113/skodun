# Plan: S6.3 stack ownership, deferrals, and lineage surfaces

1. Inspect existing stack validation, fingerprint lineage, services, CLI, MCP,
   triage, and provider prompt assembly; identify shared seams and preserve
   their contracts.
2. Add a pure bounded context builder and reconciliation projection with
   explicit unknown/conflict/truncated states.
3. Thread the projection through the shared services layer and CLI/MCP read
   surfaces without duplicating wording or changing gate/trust decisions.
4. Add hermetic tests for compact prompts, topology, audited deferrals,
   lineage fields, parity, and legacy behavior.
5. Run focused tests, the store ResourceWarning sweep if touched, and the full
   suite; review the frozen diff and deliver one narrow PR for #146.
