# Prompt transport eligibility implementation plan

**Goal:** Reject an impossible actual prompt before provider admission or invocation staging, while preserving fatal input/configuration errors and configured fallback policy (refs #194).

**Architecture:** Extract AGY's authoritative effort/UTF-8/NUL/size validation into a side-effect-free byte-input method, shared with `build_cmd`. The chain calls the adapter hook before capacity; unknown-limit adapters retain existing build-time validation. Size skips expose structured input eligibility and never enter provider quota caching. No replan, truncation, identity migration, new provider, or gate/trust change.

**Tech stack:** Python stdlib and hermetic pytest, using shipped `chain.run_chain`.

## Tasks

- [x] Add failing chain fixtures in `tests/test_transport_eligibility.py`: synthetic blocked Google admission must never be requested for above-limit Unicode payloads; at/below limit must request it. Assert zero prompt staging and launches on refusal and numeric eligibility metadata.
- [x] Add mixed-route replay fixtures: silent xAI timeout followed by impossible AGY, with/without a capable later configured fallback; preserve spent-attempt evidence and failure details. Add repeated oversized then smaller prompt checks, fatal effort/UTF-8/NUL precedence, and cancellation.
- [x] Extract `AgyAdapter.validate_prompt(prompt: bytes, r: Reviewer)` and `_validated_prompt` from the existing build guard; read raw bytes so newline translation does not change evidence or ceiling accounting. Keep effort validation first and strict decoding/NUL validation before the ceiling. `build_cmd` calls the shared helper, so no second ceiling exists.
- [x] In `chain.run_chain`, invoke the optional deterministic validation hook once per candidate before `_acquire_provider_slot`, after existing quota/binary/spend refusals. Reuse the existing PromptTooLarge skip policy via a helper and add `input_eligibility` fields: adapter, transport, capability version, reason code, input bytes, and limit. Leave unknown transport limits eligible and preserve existing build-time defensive guard.
- [x] Document behavior in README and adapter protocol. Run `python3 -m pytest tests/test_chain.py tests/test_adapter_agy.py -q --tb=short`, recording the known pre-existing openai-api conformance failure separately. Run focused pipeline/pass coverage for batch/integration chain entry points.
- [x] Self-review frozen diff and `git diff --check`; commit, push, and open PR with Summary/Test plan and Closes #194. Root coordinates integrated full suite, merge, issue closure, and merged-main smoke.

## Self-review

Scope is limited to transport eligibility. Existing configured chain supplies capable fallback and remains fail-closed when exhausted. Unknown model context capacity is never inferred from file transport. The actual complete bytes of each chain call cover both batch and integration inputs. Eligibility is recomputed cheaply per call rather than cached, so unchanged continuation never re-admits an impossible input and smaller inputs remain eligible. Existing quota ordering remains intact; fatal configuration errors cannot be hidden by the new size skip. Capability version identifies the adapter guard, not a newly probed CLI build; no provider process is launched for probing. Parent-requested expedited review exception allows self-review without external Skodun/bot review and delegates full-suite/lifecycle validation and merges to root.

## Validation and implementation review

- Regression red run: 11 failed, 5 passed; failures demonstrated admission-before-size and CRLF normalization.
- Transport regression green run: 16 passed.
- Chain + AGY + transport: 149 passed, 1 failed. The sole failure is the baseline `test_every_registered_adapter_has_conformance_coverage`, which reports missing `openai-api` coverage; the unmodified baseline produced 133 passed, 1 failed with the same diagnostic.
- Chain + transport + batched review + passes: 169 passed, 28 skipped (oracle-dependent tests), 64.44 seconds.
- Real batched-review fixture checks every batch and integration prompt through the shipped pipeline and chain, exact byte metadata, zero Google admissions, preserved primary timeouts, capable fallback, persisted artifact, and complete trustworthy aggregate.
- `git diff --check` passed. Self-review confirmed fatal effort validation still precedes file I/O in direct `build_cmd` callers, UTF-8/NUL still precede size, CRLF bytes are preserved, and size skips remain outside quota caching. No gate/trust/store schema/process lifecycle implementation changed.
- Owner-authorized expedited exception: external Skodun/bot review omitted after self-review to keep this independent foundation moving. Root owns integrated full suite and lifecycle validation, live merge checks, merge, issue close, and merged-main proof. This branch is implemented, not landed.
