# S6.1 Stack Manifest and Scope Attribution Design

**Status:** implementation contract for issue #144

**Parent:** epic #141

**Date:** 2026-08-12

## 1. Goal

Add an optional, versioned stack manifest and deterministic finding-scope
attribution without changing what a certification review covers. A stack-aware
review still captures, hashes, reviews, stores, and gates the complete configured
base-to-working-tree diff. Stack metadata only explains which slice appears to
own a finding.

This slice establishes the manifest, validation, Git evidence, classifier,
artifact projection, and matching CLI/MCP input. It does not add finding
lineage, inherit triage, reconcile provider-supplied scope, or implement the
compact provider prompt block; those belong to #145 and #146.

## 2. Locked safety properties

1. `gitio.resolve_base`, `gitio.capture_diff`, and `gitio.diff_identity` remain
   the certification identity path. A manifest digest is never mixed into the
   full diff hash.
2. Stack validation runs against the authoritative base/head/diff captured
   under the foreground lock. Validation performed before that capture cannot
   certify attribution.
3. Invalid, stale, missing, unsafe, cyclic, ambiguous, or unreachable stack
   data disables attribution and emits a bounded stable reason. The ordinary
   full-diff review continues.
4. Stack data never changes `parse_ok`, `degraded`, `diff_truncated`,
   `trustworthy`, `finding_key`, triage, gate lookup, dispatcher dedup, R2/R3,
   or refuter behavior.
5. `gate.py` and `trust.py` remain byte-identical.
6. Caller `known_finding_refs` and downstream ownership are claims. They do not
   dismiss, defer, suppress, or clear a finding.
7. This slice does not expose advisory direct-parent review execution. The
   contract reserves `coverage_scope=advisory_slice` and requires
   `gate_eligible=false` plus an independently untrustworthy record if a later
   owner-reviewed issue introduces it. Non-exposure is the safe implementation
   of advisory mode for #144.

## 3. Considered approaches

### A. Dedicated domain module with artifact-only annotations — selected

Create `src/skodun/stack.py` for strict parsing, canonicalization, Git-backed
validation, and classification. Pass a parsed request through the shared
service into the pipeline, then store only a bounded validation summary and
additive finding annotations in `artifact_json`.

This approach keeps stack semantics cohesive, needs no database migration, and
leaves all certification and triage joins untouched. It also gives #145 and
`#147` a precise identity vocabulary without coupling their storage models.

### B. Add stack tables to the SQLite store — rejected for S6.1

A normalized durable graph could answer historical stack queries efficiently,
but #144 does not need cross-review lineage. Persisting graph rows would create
an avoidable migration and concurrency seam before #145 defines the lineage
read model. The full validated summary can be carried in the existing artifact
without changing indexed review identity.

### C. Put the stack in `.skodun.toml` — rejected

Project config describes repeatable repository policy, whereas a stack
manifest is per-review evidence bound to exact commits. Merging it through the
existing operator-plus-candidate config loader would also blur which source
asserted the metadata and make a restack mutate policy-shaped input. The
manifest remains an explicit file argument to one review.

## 4. Canonical identity vocabulary

S6.1 and S7.1 share these names and meanings:

| Field | Meaning |
|---|---|
| `repository_id` | Clone-portable remote identity, separate from the existing local `repo_id` Git-common-dir path. |
| `certification_base` | Exact 40-hex commit captured as the full review base. |
| `current_head` | Exact 40-hex checked-out commit captured for the review. Dirty working-tree bytes remain in the diff/tree identities. |
| `diff_hash` | Existing Git-blob identity over the complete captured diff bytes. Never redefined by stack metadata. |
| `manifest_digest` | SHA-256 over normalized manifest semantics, excluding the digest field itself. |
| `coverage_scope` | `certification_full` for every S6.1 runnable review. `advisory_slice` is reserved but not runnable here. |
| `gate_eligible` | `true` for ordinary full certification records; reserved `false` for any future advisory artifact. It is explanatory in S6.1 and does not replace the existing trust predicate. |

The existing `gitio.repository_identity()` remains unchanged. It identifies one
local repository across linked worktrees and continues to scope store/reuse
queries. `repository_id` is an additional manifest field checked against the
canonicalized `origin` remote.

### 4.1 Remote canonicalization

Accepted remote forms are HTTPS, `ssh://`, and SCP-like SSH URLs. They normalize
to `lowercase-host/path-without-leading-slash-or-.git`. User information and
default transport syntax are removed; query strings, fragments, control
characters, empty path segments, `.`/`..`, local filesystem remotes, and URL
passwords are rejected. Host case is folded; path case is preserved.

Examples:

- `https://github.com/vega113/skodun.git` -> `github.com/vega113/skodun`
- `git@github.com:vega113/skodun.git` -> `github.com/vega113/skodun`

If `origin` is absent or cannot be canonicalized, stack attribution reports
`repository_unresolved`; it does not guess from a directory name.

## 5. Manifest v1

The file is strict UTF-8 JSON, at most 64 KiB, with duplicate keys, unknown
keys, non-finite numbers, invalid UTF-8, and excessive collections rejected.
Every optional semantic value is represented explicitly with `null`, so the
digest has one canonical shape.

```json
{
  "schema_version": 1,
  "repository_id": "github.com/acme/project",
  "certification_base": "0000000000000000000000000000000000000000",
  "current_head": "1111111111111111111111111111111111111111",
  "direct_parent": "pr-12",
  "dependencies": [
    {
      "slice_id": "pr-10",
      "commit": "2222222222222222222222222222222222222222",
      "tracking_ref": "github.com/acme/project#10",
      "ownership": []
    },
    {
      "slice_id": "pr-12",
      "commit": "3333333333333333333333333333333333333333",
      "tracking_ref": "github.com/acme/project#12",
      "ownership": []
    }
  ],
  "current_slice": {
    "slice_id": "pr-14",
    "commit": "1111111111111111111111111111111111111111",
    "tracking_ref": "github.com/acme/project#14",
    "ownership": []
  },
  "downstream_owners": [],
  "producer": {"id": "stack-export", "version": "1.0"},
  "manifest_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`dependencies` are ordered oldest prerequisite to direct parent. With no
dependencies, `direct_parent` is `null` and the current slice is compared
directly with `certification_base`. Otherwise `direct_parent` must equal the
last dependency's `slice_id`. `current_slice.commit` must equal `current_head`.

Limits are 32 dependency slices, 64 downstream owners, 256 ownership scopes
per owner, 2,048 total scopes, 128 known finding references per downstream
owner, 128 characters per identifier/version/symbol, 512 per path, and 1,024
per tracking reference. Control characters are rejected in every string.

### 5.1 Ownership scopes

Every scope has exactly these fields:

```json
{
  "kind": "file",
  "path": "src/example.py",
  "exclusive": true,
  "line_start": null,
  "line_end": null,
  "symbol": null
}
```

- `kind` is `file` or `prefix`.
- Paths are normalized repository-relative POSIX paths. Absolute paths,
  backslashes, empty/`.`/`..` segments, NUL/control characters, and a trailing
  slash are rejected.
- `prefix` scopes cannot use line or symbol anchors.
- A line anchor requires both positive integers with
  `line_start <= line_end`.
- A symbol anchor is an exact string, not a regular expression or fuzzy match.
- `exclusive=true` says no other slice may claim an intersecting exclusive
  scope. Overlap is a validation error rather than an attribution guess.
- `exclusive=false` permits intentional cross-slice overlap; a finding matching
  multiple such slices is classified `integration`.

Downstream owners have `tracking_ref`, `ownership`, and
`known_finding_refs`. A known finding reference is bounded display/audit
metadata only. It has no triage authority.

Tracking references use `repository_id#positive-integer`. Syntax validation is
local; it is never proof that GitHub currently has that issue or PR.

## 6. Parsing, digest, and file safety

The service resolves the supplied manifest path, then opens it with
`O_NOFOLLOW | O_NONBLOCK` where available. It accepts only a single-link regular
file whose descriptor metadata still matches the pre-read metadata and whose
size is within 64 KiB. Symlinks, hardlinks, FIFOs, devices, sockets, directory
paths, and files that move during the read are rejected with stable reasons.

Canonical JSON uses UTF-8, sorted keys, compact separators, `ensure_ascii=false`,
and no NaN/infinity. Validation constructs the complete normalized semantic
shape first, removes `manifest_digest`, hashes the bytes with SHA-256, then
compares the required lowercase `sha256:<64-hex>` claim.

Parse/load failures are represented as a bounded `StackRequest` result and
passed to the review. They do not become service exit 2, because malformed
attribution must not prevent an otherwise valid full-diff review.

## 7. Git-backed validation

Validation occurs after the pipeline captures `base`, `head`, the complete
`Diff`, `diff_hash`, and `tree_fingerprint` under the foreground lock.

1. Compare manifest repository/base/head with the captured identities.
2. Resolve each supplied commit only as a full hexadecimal object ID. Never
   pass manifest strings as revspec syntax.
3. Require every ID to be a commit object.
4. Require a strict ancestry chain:
   `certification_base -> dependency[0] -> ... -> direct parent -> current_head`.
   Equal/repeated commits, repeated slice IDs, the current slice appearing as a
   dependency, and a non-ancestor/reordered edge produce stable errors.
5. For each dependency, call `capture_ref_diff(previous_commit, slice.commit)`.
   For the current slice, capture `previous_commit` to the current working tree,
   so staged, unstaged, and capped untracked content is attributed from the
   same tree the full review captured.
6. A scope is evidence-backed only when its exact file or prefix intersects the
   slice's actual changed paths. A claimed scope with no changed path is invalid.
7. Rename/copy, binary, deletion-only, and mode-only evidence is retained as
   uncertain. It may establish that a path participated in a slice, but it does
   not establish exact line/symbol ownership; a finding needing that distinction
   is `unknown`.
8. Classification uses the captured `Diff` objects, never a second read of
   working-tree contents after provider execution. A later tree move cannot
   rewrite attribution for the frozen review artifact.

The current-slice capture is a second Git read. Immediately afterward the
validator recaptures the full certification diff and compares its
`diff_identity`, files/statuses, and path-scoped `tree_fingerprint` with the
pipeline's original capture. Movement produces `git_error`/unknown attribution
while the already frozen full review continues. It never combines the first
full diff with ownership read from a different tree.

Git uncertainty disables attribution with a stable reason. It never causes a
false owner selection and never marks the review itself untrustworthy.

## 8. Classifier

Each finding is shallow-copied and receives:

```json
{
  "scope_attribution": {
    "scope": "inherited_dependency",
    "reason_code": "exact_dependency_scope",
    "owner_slice_id": "pr-12",
    "owner_ref": "github.com/acme/project#12"
  }
}
```

The closed scope vocabulary is:

- `current_slice`
- `inherited_dependency`
- `downstream_owned`
- `fixture_or_test`
- `integration`
- `unknown`

Classification order and ambiguity rules:

1. Invalid or unavailable stack validation -> `unknown` with the validation
   reason and no owner.
2. A normalized test/fixture path (`tests/`, `test/`, `fixtures/`,
   `__tests__/`, or conventional test filename) -> `fixture_or_test`.
3. Match exact path/prefix, then require every supplied line/symbol anchor to
   match exact finding fields. Missing anchor evidence is not a match.
4. One current-slice match -> `current_slice`.
5. One dependency-slice match -> `inherited_dependency`.
6. One downstream-owner match, with no stack-slice match ->
   `downstream_owned`.
7. Matches across two or more stack slices are `integration` only when all
   intersecting scopes explicitly set `exclusive=false`.
8. Multiple downstream owners, uncertain rename/line mapping, invalid finding
   paths, conflicting exact owners, or no evidence -> `unknown`.

The classifier never drops or merges a raw finding. It does not modify legacy
`finding_key(file,title)` or any ledger key.

## 9. Pipeline and surface integration

### 9.1 Input

- CLI: `skodun review --stack-manifest PATH`
- MCP: optional string `stack_manifest` on the existing `review` tool

Both transports pass the value to `services.py`; only the shared service opens
and parses it. Recovery attempts reuse the same parsed request but revalidate it
against each authoritative capture. A stack-aware request bypasses trusted
review reuse with an audited `stack_attribution_requested` reason, because an
older exact-diff artifact may lack the requested attribution. It does not alter
reuse predicates or candidate identity.

### 9.2 Artifact

The record adds:

```json
{
  "coverage_scope": "certification_full",
  "gate_eligible": true,
  "stack": {
    "schema_version": 1,
    "status": "valid",
    "reason_code": "ok",
    "repository_id": "github.com/acme/project",
    "manifest_digest": "sha256:...",
    "current_slice_id": "pr-14",
    "direct_parent": "pr-12",
    "dependency_count": 2,
    "downstream_owner_count": 0
  }
}
```

Invalid input stores only bounded safe fields: status, reason code, computed or
claimed digest when syntactically safe, and no raw document/error traceback.
No migration is needed because these fields live in `artifact_json`.

### 9.3 Text and MCP structured content

The shared service renders exactly one bounded line before the unchanged
verdict banner:

```text
SKODUN STACK: status=valid slice=pr-14 dependencies=2 digest=sha256:...
```

or:

```text
SKODUN STACK: status=ignored reason=stale_head
```

MCP returns the same summary under `structuredContent.stack`; CLI and MCP use
the same field names and reason codes. Ordinary reviews with no manifest remain
byte-for-byte unchanged and omit stack metadata.

Provider prompt bytes remain unchanged in #144. #146 will consume the bounded
validated projection, charge it to an explicit prompt budget, and reconcile
provider scope without replacing this deterministic attribution.

## 10. Stable reason codes

The initial closed reason vocabulary is:

- file/parser: `unsafe_file`, `too_large`, `invalid_utf8`, `malformed_json`,
  `duplicate_key`, `unknown_field`, `unsupported_schema`, `invalid_field`,
  `limit_exceeded`, `digest_mismatch`
- identity: `repository_unresolved`, `repository_mismatch`, `stale_base`,
  `stale_head`, `tracking_repository_mismatch`
- graph/Git: `missing_commit`, `not_commit`, `duplicate_slice`,
  `duplicate_commit`, `stack_cycle`, `direct_parent_mismatch`,
  `dependency_reordered`, `dependency_unreachable`, `ownership_unreachable`,
  `exclusive_scope_overlap`, `git_error`
- finding: `exact_current_scope`, `exact_dependency_scope`,
  `exact_downstream_scope`, `test_or_fixture_path`, `cross_slice_scope`,
  `ambiguous_owner`, `uncertain_git_mapping`, `invalid_finding_path`,
  `no_owner_evidence`

Display detail is optional, control-flattened, and capped; callers branch on the
reason code, not prose.

## 11. Verification

Hermetic tests must cover:

1. Canonical JSON/digest, duplicate/unknown keys, bounds, unsafe file types,
   path/tracking-reference validation, and remote normalization.
2. A multi-branch Git fixture shaped like #3935 -> #3932 -> #3936 -> #3934,
   proving parent findings are inherited and current-owned findings are current.
3. Stale base/head, missing/non-commit objects, reordered dependencies,
   duplicates/cycles, exclusive overlap, unreachable scopes, tree movement,
   renames, split files, binary/mode/deletion-only changes, and dirty current
   content all yield deterministic valid attribution or explicit `unknown`.
4. With and without a manifest, full certification has identical base SHA,
   diff bytes/hash, trust axes, legacy finding keys, and gate outcome.
5. Stack-aware requests cannot reuse an older artifact and leave an audited
   bypass event; ordinary reuse remains unchanged.
6. CLI and MCP parse the same argument, render the same line, and project the
   same structured fields through `services.py`.
7. Full pytest, `git diff --check`, and the gate/trust byte pins pass.

Because #144 does not touch store schema or process lifecycle, the standalone
store ResourceWarning sweep is not required for this PR. It becomes mandatory
for #145's durable lineage migration.

## 12. Compatibility and follow-on boundaries

- Old artifacts omit `stack`, `coverage_scope`, and `gate_eligible` and remain
  readable.
- Existing consumers may ignore all new fields.
- #145 may persist fingerprints and predecessor links, but must retain the
  legacy finding key and use this exact scope vocabulary.
- #146 may add compact prompt context and broader read-surface rendering, but
  must use this validated projection and audited triage rather than trusting
  caller claims.
- #147 should reuse `repository_id`, `certification_base`, `current_head`,
  `diff_hash`, and canonical JSON/digest terminology. It must not redefine the
  existing local `repo_id` or diff identity.
