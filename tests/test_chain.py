"""The chain executor's module boundary: `chain.run_chain` is the same code
`pipeline._run_chain` used to be, and the alias `pipeline._run_chain` is that
exact function object, not a wrapper or a re-implementation.

Phase 3 Task 2 extracted the executor out of `pipeline.py` into its own
module (behavior-preserving: every function moved verbatim). The actual
behavioral coverage for the chain executor stays where it already lived --
`test_fallback.py`, `test_pipeline.py`, `test_refuter.py`, `test_adapter_agy.py`
and `test_cli.py` all drive it through `pipeline._run_chain` (some of them
monkeypatching that exact name), unmodified by this move. This file only
pins the import-level contract the move must preserve.
"""

from __future__ import annotations

from skodun import chain, pipeline


def test_pipeline_run_chain_is_the_chain_module_s_run_chain():
    """`pipeline._run_chain` is a one-line alias, not a copy: existing tests
    (`test_pipeline.py`, `test_refuter.py`) monkeypatch `pipeline._run_chain`
    by name and rely on `run_review`/`_extra_pass`/`_refuter_pass` picking up
    the patched value through that same name."""
    assert pipeline._run_chain is chain.run_chain


def test_pipeline_outcome_is_the_chain_module_s_outcome():
    """`_apply`'s `outcome: _Outcome` type hint and every `_run_chain` caller
    still see one `_Outcome` type, wherever it is actually defined."""
    assert pipeline._Outcome is chain._Outcome
