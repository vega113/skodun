# API conformance discovery implementation plan

**Goal:** Make focused adapter runs discover the existing OpenAI API conformance class; refs #181.

**Architecture:** The gate imports only `tests/test_adapter_*.py`, while `TestOpenAIAPIConformance` lives in `tests/test_openai_api.py`. Move the unchanged conformance subclass and shared gate imports into `tests/test_adapter_openai_api.py`. Keep HTTP runner/spend tests and their registered store-sweep path unchanged. No runtime code, API calls, configuration, or coverage exemption.

- [x] Add a cold-process regression invoking the real registration gate in `tests/test_adapter_discovery.py`; verify its baseline failure reports `openai-api`.
- [x] Move the existing conformance class to the canonical sibling test filename with its existing adapter, model, fixtures, and effort rejection. Remove only the moved class/gate imports from `tests/test_openai_api.py`.
- [x] Run discovery, AGY, API conformance, and API runtime test modules. Check pytest collection confirms one API conformance class, not duplicate inherited runs; `git diff --check` and self-review.
- [x] Push a focused PR to main. Root owns merge coordination; expedited owner exception permits self-review without external review waiting.

Self-review: this repairs module discovery rather than claiming an uncovered adapter is safe. The inherited conformance rules and fixtures stay intact. A fresh Python process proves discovery without relying on pytest's full-suite import order. Moving the subclass preserves existing runtime/spend test node IDs and the store lifecycle sweep mapping. The new conformance/discovery modules never open Store and need no sweep entry. No live model execution is involved.

## Validation

- Cold-process regression first failed at the real registration gate with missing `openai-api`, then passed after the move.
- Discovery + AGY + API conformance + API runtime: 173 passed.
- Original chain + AGY + transport command: 150 passed (previously 149 passed, 1 failed).
- All `tests/test_adapter_*.py`: 533 passed, 28 skipped.
- API conformance collection: exactly 15 unique inherited cases, all in `tests/test_adapter_openai_api.py`; no duplicate old-module class.
- `git diff --check` and self-review passed. The existing conformance class, fixture definitions and effort rejection are unchanged. New regression launches only a local Python test-discovery process, never a provider.
- Root coordinates full-suite validation and merge. No external review wait under the owner's expedited-review exception.
