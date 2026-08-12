# Plan: S8.2 coverage read model (#151)

1. Add a pure bounded projector over a review artifact plus optional
   orchestration/checkpoint rows; define deterministic coverage, evidence,
   topology, pass, and gate-eligibility derivation.
2. Add a shared services formatter and CLI/MCP JSON mode while preserving the
   existing human status line contract.
3. Add shipped-path fixtures for partial checkpoints, complete clean/finding
   reviews, finder-only background, optional refuter failure, required-pass
   failure, and no-output failure.
4. Run focused services/checkpoint/CLI/MCP tests, full suite, and store sweep;
   freeze and review the exact head before merge.
