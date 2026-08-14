# S7.3 language capability profiles and repository receipts

## Problem

Skodun can now accept protected preflight, full-gate, and mutation receipts,
but it has no bounded description of what a repository's language toolchain can
actually prove. A language name alone must not become a parser claim, and a
CI/review status must not become a trust axis. S7.3 adds an advisory capability
profile layer with a Scala 3 fixture pilot and compact repository-receipt
adapters.

## Design

`skodun.profiles` owns three frozen, stdlib-only contracts:

* `LanguageCapabilityProfile` maps a language/version to protected producer
  command ids for version discovery, compilation, fixture harnesses, optional
  symbol/AST queries, mutation target location, and mutation execution. It
  also owns repository-relative fixture expectations and hard timeout/output
  limits. It describes capabilities, never parser support.
* `run_profile` resolves only commands in the supplied `ProducerPolicy`, checks
  the worktree and fixtures with no-follow/single-link regular-file checks, and
  invokes the existing process-group watchdog with a sanitized environment.
  It returns bounded status, command/output digests, and stable reason codes;
  raw compiler output is never returned. Missing commands, version mismatch,
  unsafe paths, timeouts, output limits, and dirty descendant cleanup are
  unavailable/incomplete outcomes, never accepted capability evidence.
* Repository receipt adapters normalize local preflight/full-gate JSON,
  compiler-valid mutation summaries, CI conclusions, and review-thread
  snapshots into a compact, exact-head-bound `RepositoryReceipt`. They accept
  offline mappings only, reject logs/secrets and stale heads, and are never
  passed to gate/trust. `compact_receipt_context` deterministically bounds the
  resulting prompt context and retains only summary fields and digests.

The Scala pilot is configuration and fixtures only. It uses no Scala or SBT
dependency and no lexical parser. The fixture corpus covers indentation/colon
blocks, `given ... with`, anonymous/nested classes, XML literals, backquoted
identifiers, imports/wildcards/inheritance/root-qualified names, and character,
quoted-expression, and Unicode cases. Tests use a hermetic fake compiler
harness to prove that accepted fixtures compile through the protected command
path and invalid fixtures are rejected before behavioural evidence is accepted.

## Safety and identity

Profile commands are selected by command id, not by caller-provided argv. The
policy digest and command digest remain the authority. Every run is rooted at
the supplied absolute worktree, rejects symlink/hardlink/FIFO fixtures, uses a
bounded temporary scratch directory, and preserves the watchdog's process-group
cleanup guarantees. Profile results do not alter coverage, trust axes, gate
state, or store schema.

Repository receipts require the expected `EvidenceIdentity.current_head`; CI
and review-thread receipts additionally require their exact commit identity.
Their lifecycle fields are display context only. Compact context is JSON
summary data, capped by bytes and item count, with no logs or prompt-sized
artifacts.

## Verification

Hermetic tests cover strict profile validation, protected command selection,
missing/version-mismatched toolchains, unsafe fixtures, timeout/output bounds,
valid and invalid Scala pilot fixtures, all receipt adapters, exact-head
binding, redaction, bounded prompt context, and deterministic CLI/MCP evidence
projection parity. The existing focused, full, and store ResourceWarning
sweeps remain required before landing.
