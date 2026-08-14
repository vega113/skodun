# S7.2 compiler-valid mutation proof receipts

## Boundary

Mutation evidence is advisory read-model context. It is never a coverage,
trust, gate, or triage input. The proof is accepted only when the protected
S7.1 producer policy names every compiler, harness, and control command used
by the run.

## Receipt shape

An `evidence_kind: mutation` S7.1 receipt carries an optional
`mutation_proof` object. The object is canonical, bounded, and included in the
outer receipt digest and producer HMAC. It includes a protected baseline
command id in addition to compiler, positive-control, negative-control, and
mutant command ids. Existing non-mutation receipts keep their exact envelope
shape.

The proof binds the mutation id/type, repository-relative target, anchor,
preimage digest, exact match cardinality, changed-content digest, trusted
command ids, fixture identity, compile validity, positive/negative/mutant run
ids and exit assertions, sentinel and child-observation digests, baseline and
mutant outcomes, command/fixture existence and execution, cleanup/restore
status, initial/final tree digests, and bounded artifact/diagnostic summaries.

## State machine

`prepare -> select -> baseline (old fails) -> mutate -> validate -> controls ->
mutant (new passes) -> restore -> verify`.

The implementation owns only the target file and child process groups created
by the existing `runner.run_with_watchdog`. It rejects unsafe paths, missing or
ambiguous targets, no-op replacements, undeclared commands, missing fixtures,
missing controls, invalid compiler output, skipped sentinels, failed restore,
and a changed final tree. Restoration runs in `finally`, including timeout and
cancellation paths; incomplete receipts are never reported as accepted.

## Compatibility and limits

The protocol does not invent language parsers or source mutations. A caller
supplies the exact byte replacement and protected command ids. Compiler and
harness commands must be argv arrays from `ProducerPolicy`; shell strings and
candidate-defined policy remain rejected by S7.1. Output is read only for
bounded digests and marker checks; logs are not stored. Metadata-only restacks
can replay a receipt only when the S7.1 identity and relevant file/tree
digests remain exact.
