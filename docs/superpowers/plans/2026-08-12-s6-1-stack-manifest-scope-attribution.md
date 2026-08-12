# S6.1 Stack Manifest and Scope Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add strict optional stack-manifest validation and deterministic finding attribution to full certification reviews while preserving all existing trust, gate, triage, and exact-diff identities.

**Architecture:** A new `stack.py` domain module owns safe file loading, canonical repository/manifest identity, Git-backed validation, bounded summaries, and pure finding classification. `services.py` is the only transport-neutral input/rendering door; `pipeline.py` validates against its under-lock capture and stores additive artifact fields. CLI and MCP add one optional argument to the existing review operation, with no new tool or store migration.

**Tech Stack:** Python 3.12+ standard library, Git CLI through existing `gitio`, SQLite artifact JSON without schema changes, pytest.

---

## File map

- Create `src/skodun/stack.py`: manifest dataclasses, safe JSON loading,
  canonical digest, remote identity, Git validation, classifier, summary render.
- Modify `src/skodun/gitio.py`: narrow public exact-OID commit/ancestry and
  canonical-origin helpers used by stack validation.
- Modify `src/skodun/pipeline.py`: accept a parsed stack request, validate it
  against the frozen capture, annotate final findings, persist bounded metadata.
- Modify `src/skodun/services.py`: shared manifest loading, reuse bypass, stack
  text and structured metadata.
- Modify `src/skodun/cli.py`: `review --stack-manifest PATH` forwarding.
- Modify `src/skodun/mcpserver.py`: existing `review.stack_manifest` schema,
  type parsing, and forwarding.
- Modify `README.md`: document the optional input and safety boundary.
- Create `tests/test_stack.py`: strict parsing, identity, Git graph, classifier,
  and adversarial fixtures.
- Modify `tests/test_gitio.py`, `tests/test_pipeline.py`, `tests/test_services.py`,
  `tests/test_cli.py`, `tests/test_mcpserver.py`, and `tests/test_mcptools.py`:
  shipped-path integration and compatibility.

## Task 1: Canonical repository identity and manifest parser

**Files:**

- Create: `src/skodun/stack.py`
- Modify: `src/skodun/gitio.py`
- Create: `tests/test_stack.py`
- Modify: `tests/test_gitio.py`

- [x] **Step 1: Write failing canonical-origin tests.**

  Add tests that create a repository with each accepted `origin` URL and assert:

  ```python
  @pytest.mark.parametrize(("url", "expected"), [
      ("https://github.com/Acme/Project.git", "github.com/Acme/Project"),
      ("git@github.com:Acme/Project.git", "github.com/Acme/Project"),
      ("ssh://git@github.com/Acme/Project.git", "github.com/Acme/Project"),
  ])
  def test_canonical_repository_identity_normalizes_supported_remotes(
          tmp_path, url, expected):
      repo = _repo(tmp_path)
      _git(repo, "remote", "add", "origin", url)
      assert gitio.canonical_repository_identity(repo) == expected
  ```

  Cover missing origin, local paths, URL passwords/query/fragment, control
  characters, and `..` path segments returning `None`, never a guessed name.

- [x] **Step 2: Run the tests and confirm RED.**

  Run:

  ```bash
  python3 -m pytest tests/test_gitio.py -q --tb=short
  ```

  Expected failure: `AttributeError` for the missing
  `canonical_repository_identity` helper.

- [x] **Step 3: Add narrow Git helpers.**

  Add public `canonical_repository_identity(repo: Path) -> str | None`,
  `exact_commit_exists(repo: Path, oid: str) -> bool`, and
  `is_ancestor(repo: Path, older_oid: str, newer_oid: str) -> bool` functions
  without changing existing capture/diff functions.

  Validate full lowercase 40-hex OIDs before invoking Git. Use argument arrays,
  `cat-file -e <oid>^{commit}`, and `merge-base --is-ancestor`; manifest text is
  never accepted as arbitrary revspec syntax.

- [x] **Step 4: Write failing parser/digest/file-safety tests.**

  Define a `_manifest()` fixture that includes every v1 field and a helper that
  recomputes `manifest_digest`. Test successful parsing plus duplicate keys,
  unknown keys, invalid UTF-8, non-finite numbers, wrong types including
  `bool`-as-int, unsupported version, >64 KiB input, bounds, invalid paths,
  malformed tracking refs, bad digest, symlink, hardlink, FIFO, directory, and
  file replacement during read.

  The wished-for API is:

  ```python
  request = stack.load_request(path)
  assert request.supplied is True
  assert request.manifest is not None
  assert request.problem is None
  assert request.manifest.manifest_digest.startswith("sha256:")
  ```

  Invalid input returns a frozen `StackRequest` with a stable `StackProblem`;
  it does not raise raw JSON/OSError text to the caller.

- [x] **Step 5: Run the parser tests and confirm RED.**

  Run:

  ```bash
  python3 -m pytest tests/test_stack.py -q --tb=short
  ```

  Expected failure: import error for the absent `skodun.stack` module.

- [x] **Step 6: Implement minimal strict dataclasses and parser.**

  Use frozen dataclasses with explicit fields:

  ```python
  @dataclass(frozen=True)
  class OwnershipScope:
      kind: str
      path: str
      exclusive: bool
      line_start: int | None
      line_end: int | None
      symbol: str | None

  @dataclass(frozen=True)
  class StackProblem:
      reason_code: str
      detail: str = ""

  @dataclass(frozen=True)
  class StackRequest:
      supplied: bool
      manifest: StackManifest | None
      problem: StackProblem | None
  ```

  Parse JSON with `object_pairs_hook` duplicate detection and
  `parse_constant` rejection. Normalize the complete semantic object, hash
  sorted compact UTF-8 JSON excluding `manifest_digest`, and enforce the exact
  bounds from the spec. Safe-open with no-follow/nonblocking flags, descriptor
  `fstat`, single-link regular-file checks, size cap, and a post-read metadata
  check.

- [x] **Step 7: Run focused tests and commit.**

  Run:

  ```bash
  python3 -m pytest tests/test_stack.py tests/test_gitio.py -q --tb=short
  git diff --check
  ```

  Commit:

  ```bash
  git add src/skodun/stack.py src/skodun/gitio.py tests/test_stack.py tests/test_gitio.py
  git commit -m "Add strict stack manifest identity parsing refs #144"
  ```

## Task 2: Git-backed stack validation and pure attribution

**Files:**

- Modify: `src/skodun/stack.py`
- Modify: `tests/test_stack.py`

- [x] **Step 1: Write the failing TubeScribes-shaped Git fixture.**

  Build a hermetic linear stack with four commits/slices and an HTTPS origin.
  Each slice changes a distinct file; one file is deliberately touched by two
  non-exclusive scopes. Capture the full base-to-working-tree diff with the
  shipped `gitio.capture_diff` and call:

  ```python
  validated = stack.validate(
      request,
      repo=repo,
      certification_base=base,
      current_head=head,
      full_diff=full_diff,
      full_tree_fingerprint=gitio.tree_fingerprint(
          repo, paths=full_diff.files),
      untracked_max=100,
  )
  assert validated.status == "valid"
  ```

  Then assert `classify_findings` yields `inherited_dependency` for the parent
  file, `current_slice` for the current file, `fixture_or_test` for a test path,
  `integration` for intentional non-exclusive cross-slice scope, and
  `downstream_owned` for one exact claimed downstream owner.

- [x] **Step 2: Run the focused test and confirm RED.**

  Run:

  ```bash
  python3 -m pytest tests/test_stack.py -q --tb=short
  ```

  Expected failure: `validate` / `classify_findings` are absent.

- [x] **Step 3: Implement graph and Git evidence validation.**

  Add frozen validated projections containing only normalized slices, actual
  changed paths/statuses, and a bounded summary. Enforce repository/base/head,
  commit type, strict ordered ancestry, direct-parent identity, duplicate/cycle
  checks, actual scope intersection, and exclusive overlap.

  Dependency evidence uses `capture_ref_diff(previous, commit)`. Current-slice
  evidence uses `capture_diff(previous, untracked_max)`. Immediately recapture
  the full certification diff and compare its identity, files/statuses, and
  path-scoped tree fingerprint with the supplied original capture. Any mismatch
  returns invalid attribution with `git_error`; it does not raise into the
  review pipeline.

- [x] **Step 4: Implement conservative pure classification.**

  Add `classify_findings(findings: list, result: StackValidation) ->
  list[dict]`. Shallow-copy each finding, preserve every raw field, and add only
  `scope_attribution`. Use exact normalized paths and exact optional line/symbol
  anchors. Multiple downstream owners, uncertain rename/copy/binary/mode/
  deletion mappings, invalid paths, and conflicts become `unknown` with a
  stable reason. Never omit or merge a finding.

- [x] **Step 5: Add adversarial RED cases, then make them GREEN.**

  Add tests for stale base/head, missing/non-commit OIDs, reordered ancestry,
  duplicate IDs/commits, cycle/current-as-dependency, wrong direct parent,
  unreachable ownership, exclusive overlap, rename, split-file overlap,
  binary/mode/deletion-only evidence, dirty current content, moved tree,
  missing line/symbol anchors, and multiple downstream owners.

  Run after each group:

  ```bash
  python3 -m pytest tests/test_stack.py -q --tb=short
  ```

- [x] **Step 6: Commit the validated classifier.**

  ```bash
  git diff --check
  git add src/skodun/stack.py tests/test_stack.py
  git commit -m "Classify stack findings from exact Git evidence refs #144"
  ```

## Task 3: Attach stack metadata without changing certification

**Files:**

- Modify: `src/skodun/pipeline.py`
- Modify: `tests/test_pipeline.py`

- [x] **Step 1: Write failing full-pipeline tests.**

  Extend the shipped foreground fixture to call:

  ```python
  rec = run_review(repo, cfg, store, stack_request=request)
  assert rec["coverage_scope"] == "certification_full"
  assert rec["gate_eligible"] is True
  assert rec["stack"]["status"] == "valid"
  assert rec["findings"][0]["scope_attribution"]["scope"] == "current_slice"
  ```

  Capture the same repository with and without the request and assert identical
  `base_sha`, `head`, `diff_hash`, `tree_fingerprint`, `parse_ok`, `degraded`,
  `diff_truncated`, `trustworthy`, `finding_key`, and raw finding count.
  Malformed/stale manifests must still invoke the fake reviewer and produce an
  ordinary full record with `stack.status == "ignored"` and unknown
  attribution.

- [x] **Step 2: Run the focused test and confirm RED.**

  ```bash
  python3 -m pytest tests/test_pipeline.py -q --tb=short
  ```

  Expected failure: `run_review` does not accept `stack_request` and records
  carry no stack projection.

- [x] **Step 3: Thread stack validation through the authoritative capture.**

  Add the keyword-only parameter:

  ```python
  stack_request: "stack.StackRequest | None" = None
  ```

  Immediately after full identity capture, call `stack.validate`. Add bounded
  `coverage_scope`, `gate_eligible`, and `stack` fields to `common` only when a
  request was supplied. After finder/extra/refuter findings are final and before
  trust/status persistence, call `stack.classify_findings`. Do not change
  prompt bytes, model calls, trust computation, or empty-diff behavior.

- [x] **Step 4: Pin protected seams.**

  Keep the existing SHA-256 expectations in `tests/test_seams.py` unchanged.
  Add pipeline assertions that no stack field participates in trust-axis
  calculation. Run:

  ```bash
  python3 -m pytest tests/test_pipeline.py tests/test_seams.py tests/test_gate.py tests/test_trust.py -q --tb=short
  ```

- [x] **Step 5: Commit the pipeline integration.**

  ```bash
  git diff --check
  git add src/skodun/pipeline.py tests/test_pipeline.py
  git commit -m "Attach stack attribution to full review artifacts refs #144"
  ```

## Task 4: Preserve CLI/MCP parity through services

**Files:**

- Modify: `src/skodun/services.py`
- Modify: `src/skodun/cli.py`
- Modify: `src/skodun/mcpserver.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_mcpserver.py`
- Modify: `tests/test_mcptools.py`

- [x] **Step 1: Write failing service parity and reuse-bypass tests.**

  Test one valid and one malformed manifest through `svc_review_detailed`.
  Assert the text prefix is produced by `stack.render_summary`, metadata has
  exactly one `stack` key equal to the bounded stored stack summary, and the
  last line remains the existing verdict.
  With `reuse_trusted=True`, assert the provider path runs and one reuse audit
  event records outcome `bypass` with reason
  `stack_attribution_requested` rather than returning an older artifact.

- [x] **Step 2: Run the service tests and confirm RED.**

  ```bash
  python3 -m pytest tests/test_services.py -q --tb=short
  ```

  Expected failure: service signatures do not accept `stack_manifest`.

- [x] **Step 3: Implement the shared service door.**

  Extend `svc_review_detailed`, `svc_review`, and `_svc_review_once` with an
  optional `stack_manifest`/parsed request. Load the file once per user request,
  revalidate it per recovery capture, bypass reuse with an append-only audit
  event using the existing reuse event schema, and render one bounded line from
  the stored result. Existing calls with no manifest must take the exact old
  path and text.

- [x] **Step 4: Write failing CLI and MCP transport tests.**

  CLI parser assertion:

  ```python
  args = cli._parser().parse_args(
      ["review", "--stack-manifest", "stack.json"])
  assert args.stack_manifest == Path("stack.json")
  ```

  MCP handler assertion passes `{"stack_manifest": "stack.json"}` to the
  shared service and returns the same text plus
  `structuredContent.stack`. Wrong MCP types are refused before the store opens.
  Update the closed review-property snapshot in `tests/test_mcptools.py`; do not
  add a new tool.

- [x] **Step 5: Run the transport tests and confirm RED.**

  ```bash
  python3 -m pytest tests/test_cli.py tests/test_mcpserver.py tests/test_mcptools.py -q --tb=short
  ```

- [x] **Step 6: Implement transport forwarding and make GREEN.**

  Add CLI `--stack-manifest` with `type=Path`. In MCP, parse only its string
  type with `_opt_string_arg`; the shared service owns path/file/schema
  semantics. Pass metadata through the existing `HandlerResult` and
  `tool_result` projection.

  Run:

  ```bash
  python3 -m pytest tests/test_services.py tests/test_cli.py tests/test_mcpserver.py tests/test_mcptools.py -q --tb=short
  ```

- [x] **Step 7: Commit the parity surface.**

  ```bash
  git diff --check
  git add src/skodun/services.py src/skodun/cli.py src/skodun/mcpserver.py tests/test_services.py tests/test_cli.py tests/test_mcpserver.py tests/test_mcptools.py
  git commit -m "Expose stack attribution through shared review surfaces refs #144"
  ```

## Task 5: Documentation, compatibility, and local acceptance

**Files:**

- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-12-s6-1-stack-manifest-scope-attribution.md`

- [x] **Step 1: Document the shipped flag and boundaries.**

  Update the CLI table and MCP review description with `stack_manifest`, the
  strict v1 identity, ordinary full-diff continuation on invalid attribution,
  no auto-triage, and no runnable advisory mode. Include a compact manifest
  example that uses real field names from the parser.

- [x] **Step 2: Run focused acceptance tests.**

  ```bash
  python3 -m pytest tests/test_stack.py tests/test_gitio.py tests/test_pipeline.py tests/test_services.py tests/test_cli.py tests/test_mcpserver.py tests/test_mcptools.py tests/test_reuse.py tests/test_gate.py tests/test_trust.py tests/test_seams.py -q --tb=short
  ```

- [x] **Step 3: Run full verification.**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q --tb=short
  git diff --check
  python3 -m pytest tests/test_seams.py -q --tb=short
  ```

  Record exact passed/skipped counts. A stall or interruption is incomplete.
  Verified on the final review head: `3558 passed, 160 skipped, 1 deselected in 437.69s`.
  The deselected test is the nested heavy store ResourceWarning sweep; this
  slice adds no store schema or process-lifecycle code, so the repository
  instructions do not require that sweep here.
  The dedicated store ResourceWarning sweep is not required because this PR
  adds no store schema or process-lifecycle code.

- [x] **Step 4: Self-review every acceptance criterion.**

  Compare the diff with issue #144 and the design. Verify no prompt bytes,
  certification bytes, trust axes, finding keys, triage records, store schema,
  gate/trust files, or advisory execution changed. Freeze scope after this pass.

- [x] **Step 5: Commit documentation and plan state.**

  ```bash
  git add README.md docs/superpowers/plans/2026-08-12-s6-1-stack-manifest-scope-attribution.md
  git commit -m "Document stack-aware full certification refs #144"
  ```

## Task 6: Exact-head review and GitHub delivery

**Files:**

- No planned production changes; review fixes must be issue-narrow and test-first.

- [x] **Step 1: Refresh coordination state.**

  Fetch `origin/main`, inspect all open PRs, list shared seams touched by the
  other lane, and rebase `codex/s6-144-stack-manifest` if main advanced. Resolve
  conflicts semantically and rerun Task 5 verification after any rebase.

- [x] **Step 2: Freeze and review the exact head.**

  Run the repository's current trustworthy Skodun review/gate workflow for the
  exact branch head without editing the tree while review is active. Resolve
  every real finding through a new failing test and focused fix, then rerun full
  verification and exact-head review. Do not weaken gate/trust to obtain a pass.

- [x] **Step 3: Push and open one issue-narrow PR.**

  The PR body must include Lane A, shared seams (`pipeline.py`, `services.py`,
  `cli.py`, `mcpserver.py`), Summary, design/safety decisions, Test plan,
  migration/compatibility note (`no migration`), exact-head Skodun evidence,
  and `Refs #144`.

- [ ] **Step 4: Drive the PR to merge.**

  Monitor required checks and GraphQL `reviewThreads`; verify zero legitimate
  unresolved threads, current/mergeable head, and one late-comment sweep.
  Rebase and rerun verification if another shared-seam PR lands first. Merge
  only after the exact current head is green and reviewed.

  Review follow-up before merge: preserve bounded downstream
  `known_finding_refs` in the persisted attribution, classify Go `_test.go`
  paths as fixtures/tests, and canonicalize only URI-unreserved repository
  path escapes while rejecting encoded separators and traversal. Added
  failing fixtures in `tests/test_stack.py` and `tests/test_gitio.py`; the
  impacted shipped-path suites pass (`763 passed, 9 skipped`).

  Second review follow-up: changed-line evidence now records only added
  new-side lines from unified diff bodies (not whole hunk ranges), and
  distinct symbol-anchored exclusive scopes are treated as non-overlapping.
  Added focused regression tests; stack/Git suites pass (`125 passed,
  9 skipped`) and pipeline/service suites pass (`150 passed`).

  Final parser follow-up: unified-diff file-header detection is limited to
  pre-hunk lines, so added source lines beginning `+++` (and deleted lines
  beginning `---`) remain real body evidence. Regression coverage is included
  in the stack suite (`58 passed`).

  Uncertainty follow-up: line-anchored scopes on rename/copy, deletion-only,
  binary, or mode-only paths remain reachable as path evidence; classification
  now carries the owner match through to `uncertain_git_mapping` instead of
  rejecting the whole manifest. The deletion-only line-anchor regression is
  covered by the stack suite.

- [ ] **Step 5: Verify merged main and close #144.**

  Fetch `origin/main`, verify the merge commit is present, run the focused
  shipped-path smoke tests from a refreshed main checkout, comment on #144 with
  the PR and test evidence, close it, and confirm GitHub reports the issue
  closed with no open PR/review residue.
