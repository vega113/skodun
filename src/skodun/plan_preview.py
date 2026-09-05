"""Read-only review preparation over the shipped planner and prompt builders.

Exact inputs are reported as bytes/hashes, never printed. Result-dependent
integration/refuter inputs remain unknown. A selected measured target is an
explicit application suggestion, never a mutation of execution policy.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from . import budget, checklist, config, dispatch, gitio, operational_targets, passes, pipeline, planning_policy, routing, stack
from .adapters import PromptTooLarge

MAX_DISPLAY_CALLS = 200
MAX_DISPLAY_FILES = 500


def _selection(root, diff, defaults):
    return checklist.select(diff.files, 'full', pipeline._under(root, defaults.checklist_dir),
        pipeline._under(root, defaults.rules_json), defaults.checklist_map, defaults.test_path_patterns)


def _paths(cfg, reviewer, defaults, prompt=None, *, future=False):
    rows = []
    for entry in pipeline._chain_for(cfg, reviewer):
        adapter = pipeline._adapter_for(entry)
        limit = adapter.prompt_limit()
        row = {'reviewer': entry.name, 'provider': entry.provider, 'model': entry.model,
            'effort': entry.effort, 'configured_envelope_bytes': budget.prompt_budget(defaults, entry),
            'transport': getattr(adapter, 'prompt_transport', None),
            'capability_version': getattr(adapter, 'prompt_capability_version', None),
            'transport_limit_bytes': limit, 'status': 'pending_result_input' if future else 'no_declared_transport_limit',
            'model_context_guaranteed': False, 'latency_guaranteed': False}
        if prompt is not None:
            validate = getattr(adapter, 'validate_prompt', None)
            try:
                if callable(validate):
                    validate(prompt.text, entry)
                    if not future:
                        row['status'] = 'admissible'
                elif type(limit) is int:
                    if prompt.prompt_bytes > limit:
                        row['status'] = 'ineligible'
                        row['reason_code'] = 'prompt_too_large'
                    elif not future:
                        row['status'] = 'admissible'
            except PromptTooLarge as exc:
                row.update(status='ineligible', reason_code='prompt_too_large',
                           actual_bytes=exc.size, transport_limit_bytes=exc.limit)
            except (TypeError, ValueError) as exc:
                row.update(status='invalid_configuration', reason_code=type(exc).__name__)
        rows.append(row)
    return rows


def _call(kind, index, reviewer, cfg, defaults, prompt, *, condition='required', future=False, **extra):
    return {'kind': kind, 'index': index, 'condition': condition,
        'prompt_bytes': None if future or prompt is None else prompt.prompt_bytes,
        'prompt_hash': None if future or prompt is None else gitio.diff_identity(prompt.text),
        'prompt_truncated': None if future or prompt is None else prompt.diff_truncated,
        'partial_coverage': kind in ('security', 'skeptic') and prompt is not None and prompt.diff_truncated,
        'input_status': 'pending_results' if future else 'exact',
        'paths': _paths(cfg, reviewer, defaults, prompt, future=future), **extra}


def _prepare(diff, *, root, cfg, defaults, finder, branch, base, head, mode, advisory):
    batches = pipeline.batch_plan(diff.data, defaults, finder)
    head_label = f'{head} (working tree)' if mode == 'now' else head
    calls = []
    prepared = None
    if not diff.data.rstrip(b'\n'):
        return [], None, None
    if batches is None:
        selection = _selection(root, diff, defaults)
        pack, prompt = pipeline._prepare_single_prompt(diff, d=defaults, root=root, finder=finder,
            selection=selection, branch=branch, base_ref=base.ref, base_sha=base.sha,
            head_label=head_label, context_source='wt' if mode == 'now' else 'oid',
            context_oid=None if mode == 'now' else head, **advisory)
        calls.append(_call('primary', 0, finder, cfg, defaults, prompt,
                           diff_bytes=len(diff.data), diff_hash=gitio.diff_identity(diff.data),
                           context_bytes=pack.bytes_total if pack is not None else 0))
    else:
        prepared = pipeline._prepare_batch_plan(diff, batches=batches, cfg=cfg, d=defaults,
            root=root, finder=finder, branch=branch, base_ref=base.ref, base_sha=base.sha,
            head_label=head_label, context_source='wt' if mode == 'now' else 'oid',
            context_oid=None if mode == 'now' else head, **advisory)
        for item in prepared.batches:
            calls.append(_call('batch', item.identity.index, finder, cfg, defaults, item.prompt,
                diff_bytes=len(item.batch.data), diff_hash=item.identity.diff_hash,
                context_bytes=item.pack.bytes_total if item.pack is not None else 0,
                boundary_hash=item.identity.boundary_hash, files=list(item.batch.files)[:MAX_DISPLAY_FILES],
                files_truncated=len(item.batch.files) > MAX_DISPLAY_FILES,
                splitter_floor_exceeded=item.batch.truncated))
        if prepared.integration_selection is not None:
            integrator = pipeline._pass_reviewer(cfg, 'integration', finder)
            # Empty result slots only establish the known structural floor.
            # This prompt is never returned as input or executed.
            floor = passes.integration_prompt([passes.BatchSummary(files=list(item.batch.files),
                diff=item.batch.data, summary='', findings=[]) for item in prepared.batches],
                prepared.integration_selection, budget.prompt_budget(defaults, integrator),
                stack_context=advisory['stack_context'], lineage_context=advisory['lineage_context'])
            calls.append(_call('integration', 0, integrator, cfg, defaults, floor, future=True,
                structural_preview_bytes=floor.prompt_bytes, structural_floor_truncated=floor.diff_truncated,
                required_batch_count=len(batches)))
    if passes.should_run_security(mode, diff.files, defaults.security_path_segments, defaults.security_basename_patterns):
        reviewer = pipeline._pass_reviewer(cfg, 'security', finder)
        prompt = passes.security_prompt(branch, base.ref, base.sha, head_label, diff.data,
            budget.prompt_budget(defaults, reviewer), defaults.security_prompt_slots)
        calls.append(_call('security', 0, reviewer, cfg, defaults, prompt))
    if passes.should_run_skeptic(mode, True, 0):
        reviewer = pipeline._pass_reviewer(cfg, 'skeptic', finder)
        prompt = passes.skeptic_prompt(branch, base.ref, base.sha, head_label, diff.data,
            budget.prompt_budget(defaults, reviewer))
        calls.append(_call('skeptic', 0, reviewer, cfg, defaults, prompt,
                          condition='trusted review remains clean after security', conditional_group='finder_outcome'))
    if passes.refuter_decision(mode, True, 1, cfg)[0]:
        reviewer = pipeline._pass_reviewer(cfg, 'refuter', finder)
        calls.append(_call('refuter', 0, reviewer, cfg, defaults, None, future=True,
            condition='trusted finder has findings and an independent contributor family is available',
            conditional_group='finder_outcome', actual_contributors_required=True))
    return calls, batches, prepared


def _timeout(call, defaults, cfg, mode):
    timeout = defaults.timeout_sec
    if mode == 'prepush' and call['prompt_bytes'] is not None and call['prompt_bytes'] > cfg.dispatch.large_prompt_bytes:
        timeout = max(timeout, cfg.defaults.timeout_sec)
    return timeout


def preview(store, repo, *, reviewer=None, client_family=None, mode=None, batch_target_bytes=None,
            target_source='configured', target_latency_seconds=None, stack_manifest=None,
            local_ref=None, local_oid=None, remote_ref=None, remote_oid=None, review_id=None, now=None):
    from .services import _validate_batch_target
    if mode is not None and mode not in ('now', 'prepush'):
        raise ValueError('mode must be now or prepush')
    target, refusal = _validate_batch_target(batch_target_bytes)
    if refusal:
        raise ValueError(refusal)
    if target_source not in ('configured', 'measured'):
        raise ValueError('target-source must be configured or measured')
    if target_latency_seconds is not None and (not operational_targets._duration(target_latency_seconds)
            or not 0 < target_latency_seconds <= 86400):
        raise ValueError('target-latency-seconds must be positive, finite and at most 86400')
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    root = gitio._worktree_root(repo).resolve()
    cfg = config.load_config(root)
    if target is not None:
        cfg = replace(cfg, defaults=replace(cfg.defaults, batch_target_bytes=target))
    historical = None
    requested_base = None
    if review_id is not None:
        if store is None:
            raise ValueError('historical inspection needs a readable store')
        historical = store.get_review(review_id)
        if not isinstance(historical, dict):
            raise ValueError('historical review not found')
        if (historical.get('repo_id') or historical.get('repo')) != str(gitio.git_common_dir(root)):
            raise ValueError('historical review belongs to a different repository')
        if any(value is not None for value in (local_ref, local_oid, remote_ref, remote_oid)):
            raise ValueError('historical review and explicit pushed refs cannot be combined')
        if mode is not None and mode != historical.get('mode'):
            raise ValueError('mode differs from historical review')
        mode = historical.get('mode')
        if not all(isinstance(historical.get(key), str) and re.fullmatch(r'[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?', historical[key]) for key in ('base_sha', 'head')):
            raise ValueError('historical base/head must be full object IDs')
        base = gitio.Base(ref=historical['base_ref'], sha=historical['base_sha'])
        head, branch = historical['head'], historical['branch']
        diff = gitio.capture_ref_diff(root, base.sha, head)
        source = 'historical_review_record'
    else:
        mode = mode or 'now'
        if mode == 'prepush':
            fields = (local_ref, local_oid, remote_ref, remote_oid)
            if not all(isinstance(value, str) and value and not any(c.isspace() for c in value) for value in fields):
                raise ValueError('prepush preview requires all four explicit ref fields')
            if not all(re.fullmatch(r'[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?', value) for value in (local_oid, remote_oid)):
                raise ValueError('prepush object IDs must be full hexadecimal OIDs')
            refs = dispatch.parse_ref_lines(' '.join(fields))
            if len(refs) != 1 or not refs[0].actionable:
                raise ValueError('prepush ref is not an actionable branch update')
            ref = refs[0]
            base = dispatch.resolve_dispatch_base(root, ref)
            head, branch = ref.local_oid, ref.branch
            diff = gitio.capture_ref_diff(root, base.sha, head)
            source = 'remote_oid' if ref.remote_oid.strip('0') else 'new_ref_resolver'
            requested_base = {'remote_ref': ref.remote_ref, 'remote_oid': ref.remote_oid}
        elif mode == 'now':
            if any(value is not None for value in (local_ref, local_oid, remote_ref, remote_oid)):
                raise ValueError('explicit pushed refs require prepush mode')
            base = gitio.resolve_base(root)
            head, branch = gitio.head_sha(root), gitio.current_branch(root)
            diff = gitio.capture_diff(root, base.sha, cfg.defaults.untracked_max)
            source = 'foreground_resolver'
        else:
            raise ValueError('mode must be now or prepush')
    if historical is not None and mode != 'prepush':
        raise ValueError('historical working-tree edits cannot be reconstructed; inspect current foreground scope instead')
    if mode == 'prepush':
        if reviewer is not None:
            raise ValueError('prepush execution uses the configured finder; reviewer override is unsupported')
        finder = pipeline._reviewer_for(cfg, 'finder')
        if finder is None:
            raise ValueError('no enabled finder is configured')
        route = {'requested_reviewer': None, 'routed_reviewer': finder.name, 'route_reason': 'config-prepush'}
        defaults = dispatch.effective_defaults(cfg.defaults, cfg.dispatch)
    else:
        finder, route = pipeline.resolve_review_head(cfg, store, requested=reviewer,
            client_family=routing.resolve_client_family(client_family))
        defaults = cfg.defaults
    tree = gitio.tree_fingerprint(root, paths=diff.files) if mode == 'now' else head
    validation = None
    stack_context, stack_truncated = b'', False
    if stack_manifest is not None:
        if mode != 'now':
            raise ValueError('stack annotations are supported only by foreground execution')
        request = stack.load_request(stack_manifest)
        requested_base = {'stack_certification_base': getattr(request.manifest, 'certification_base', None)}
        validation = stack.validate(request, repo=root, certification_base=base.sha, current_head=head,
            full_diff=diff, full_tree_fingerprint=tree, untracked_max=defaults.untracked_max)
        stack_context, stack_truncated = stack.render_prompt_context(validation)
    repository = (validation.manifest.repository_id if validation is not None and validation.status == 'valid'
                  and validation.manifest is not None else gitio.canonical_repository_identity(root) or 'unknown')
    lineage, lineage_truncated = pipeline._lineage_prompt_context(store, repository, before=now,
        changed_paths=diff.files, owner_ids=pipeline._lineage_owner_ids(validation))
    advisory = {'stack_context': stack_context, 'stack_context_truncated': stack_truncated,
        'lineage_context': lineage, 'lineage_context_truncated': lineage_truncated,
        'evidence_context': pipeline._evidence_prompt_context(store, root, base.sha, head, gitio.diff_identity(diff.data))}
    data = operational_targets.read_evidence(store, reviewer=finder, mode=mode, execution_policy=planning_policy.execution_policy(defaults), context_pack=defaults.context_pack, now=now)
    selected = None
    reason = 'configured_target'
    if diff.truncated_untracked:
        reason = 'incomplete_scope_capture'
    elif target_source == 'measured':
        reason = ('explicit_override' if target is not None else 'latency_objective_required'
                  if target_latency_seconds is None else 'route_not_stable'
                  if mode == 'now' and reviewer is None and cfg.routing.mode == 'auto' else 'insufficient_matching_evidence')
        if reason == 'insufficient_matching_evidence':
            ceiling = planning_policy.diff_budget(defaults, finder)
            for cohort in operational_targets.candidates(data, latency_seconds=target_latency_seconds,
                    hard_diff_ceiling=ceiling, diff_bytes=len(diff.data)):
                proposed = replace(defaults, batch_target_bytes=cohort['target_bytes'])
                trial, _, _ = _prepare(diff, root=root, cfg=cfg, defaults=proposed, finder=finder,
                    branch=branch, base=base, head=head, mode=mode, advisory=advisory)
                if any((call.get('prompt_truncated') and not call.get('partial_coverage')) or call.get('structural_floor_truncated')
                       or all(path['status'] in ('ineligible', 'invalid_configuration') for path in call['paths']) for call in trial
                       if call['condition'] == 'required'):
                    reason = 'required_coverage_floor_unfit'
                    continue
                measured_calls = [call for call in trial if call['kind'] in ('primary', 'batch')]
                if not measured_calls or not (
                        cohort['input_min_bytes'] <= max(call['prompt_bytes'] for call in measured_calls) <= cohort['input_max_bytes']
                        and cohort['context_min_bytes'] <= max(call['context_bytes'] for call in measured_calls) <= cohort['context_max_bytes']):
                    reason = 'candidate_inputs_outside_observed_range'
                    continue
                if any(max(cohort['timeout_seconds']) > _timeout(call, proposed, cfg, mode) for call in measured_calls):
                    reason = 'historical_timeout_incompatible'
                    continue
                selected, defaults, reason = cohort, proposed, 'qualified_measured_target'
                break
    calls, batches, prepared = _prepare(diff, root=root, cfg=cfg, defaults=defaults, finder=finder,
        branch=branch, base=base, head=head, mode=mode, advisory=advisory)
    # Mirror the execution's input-aware unbatched reroute at the same snapshot.
    if mode == 'now' and batches is None and calls and reviewer is None and cfg.routing.mode == 'auto':
        candidate, candidate_route = pipeline.resolve_review_head(cfg, store, client_family=routing.resolve_client_family(client_family),
            prompt_size=calls[0]['prompt_bytes'] + 256)
        if candidate.name != finder.name and pipeline.batch_plan(diff.data, defaults, candidate) is None:
            finder, route = candidate, candidate_route
            data = operational_targets.read_evidence(store, reviewer=finder, mode=mode,
                execution_policy=planning_policy.execution_policy(defaults), context_pack=defaults.context_pack, now=now)
            calls, batches, prepared = _prepare(diff, root=root, cfg=cfg, defaults=defaults, finder=finder,
                branch=branch, base=base, head=head, mode=mode, advisory=advisory)
    for call in calls:
        call['configured_timeout_seconds'] = _timeout(call, defaults, cfg, mode)
        call['timeout_may_escalate_after_results'] = mode == 'prepush' and call['input_status'] == 'pending_results'
        call['attempt_budget_per_entry'] = (1 + defaults.timeout_retries + defaults.degraded_retries)
    policy = planning_policy.describe(defaults, finder)
    primary = [call for call in calls if call['kind'] in ('primary', 'batch')]
    for call in calls:
        matching = [cohort for cohort in data['cohorts'] if cohort['qualified']
            and cohort['pass_kind'] == call['kind'] and call['prompt_bytes'] is not None
            and cohort['input_min_bytes'] <= call['prompt_bytes'] <= cohort['input_max_bytes']
            and cohort['context_min_bytes'] <= call.get('context_bytes', -1) <= cohort['context_max_bytes']
            and max(cohort['timeout_seconds']) <= call['configured_timeout_seconds']]
        call['historical_duration_sec'] = matching[0]['historical_duration_sec'] if len(matching) == 1 else None
        call['historical_duration_status'] = 'observed_matching_cohort' if len(matching) == 1 else 'unknown'
    known = [call['prompt_bytes'] for call in calls if call['condition'] == 'required' and call['prompt_bytes'] is not None]
    required = [call for call in calls if call['condition'] == 'required']
    conditional = [call for call in calls if call['condition'] != 'required']
    launch_bound = lambda call: sum(path['status'] not in ('ineligible', 'invalid_configuration') for path in call['paths']) * (1 + defaults.timeout_retries + defaults.degraded_retries)
    identity_calls = [{key: value for key, value in call.items() if not key.startswith('historical_')} for call in calls]
    plan_digest = gitio.diff_identity(json.dumps({'policy': policy, 'calls': identity_calls, 'base': base.sha,
        'head': head, 'diff': gitio.diff_identity(diff.data)}, sort_keys=True).encode())
    changed = False
    if mode == 'now':
        changed = (gitio.head_sha(root) != head or gitio.resolve_base(root).sha != base.sha
            or gitio.diff_identity(gitio.capture_diff(root, base.sha, defaults.untracked_max).data) != gitio.diff_identity(diff.data)
            or gitio.tree_fingerprint(root, paths=diff.files) != tree)
    historical_metrics = None
    if historical is not None:
        observed_batches = historical.get('batches')
        batch_values = [item.get('prompt_bytes') for item in observed_batches or ()
                        if isinstance(item, dict) and type(item.get('prompt_bytes')) is int and item['prompt_bytes'] >= 0]
        historical_metrics = {'scope': 'stored_review', 'aggregate_prompt_bytes': historical.get('prompt_bytes') if type(historical.get('prompt_bytes')) is int and historical['prompt_bytes'] >= 0 else None,
            'maximum_batch_prompt_bytes': max(batch_values, default=None),
            'declared_batch_count': historical.get('batch_count') if type(historical.get('batch_count')) is int and historical['batch_count'] >= 0 else None,
            'observed_batch_records': len(observed_batches) if isinstance(observed_batches, list) else None,
            'changed_file_count': len(historical['files_changed']) if isinstance(historical.get('files_changed'), list) else None,
            'historical_configuration_available': False, 'task_intent': 'unknown'}
    return {'schema_version': 'review-plan/v1', 'snapshot_only': True,
        'status': 'stale' if changed else 'unreviewable' if diff.truncated_untracked or any(
            (call.get('prompt_truncated') and not call.get('partial_coverage')) or call.get('structural_floor_truncated')
            or all(path['status'] in ('ineligible', 'invalid_configuration') for path in call['paths'])
            for call in required) else 'planned',
        'mode': mode, 'base': {'requested': requested_base, 'source': source, 'ref': base.ref,
            'sha': base.sha, 'head': head, 'warning': base.warning}, 'branch': branch,
        'worktree_root': str(root), 'configuration_source': 'current_read_only_config',
        'historical_review_id': review_id, 'historical_observation': historical_metrics, 'historical_scope_note': 'Task intent remains unknown; breadth alone is not an engine defect.' if historical else None,
        'stack': validation.to_dict() if validation else None,
        'changed_file_count': len(diff.files), 'changed_files': list(diff.files)[:MAX_DISPLAY_FILES],
        'changed_files_truncated': len(diff.files) > MAX_DISPLAY_FILES,
        'scope_capture': {'complete': not diff.truncated_untracked,
            'truncated_untracked': diff.truncated_untracked,
            'reason_code': 'untracked_capture_limit' if diff.truncated_untracked else None,
            'untracked_max': defaults.untracked_max},
        'diff_bytes': len(diff.data), 'diff_hash': gitio.diff_identity(diff.data), 'tree_fingerprint': tree,
        'planning_policy': policy, 'plan_digest': plan_digest,
        'batch_count': 0 if batches is None else len(batches),
        'boundary_digest': prepared.boundary_digest if prepared else None,
        'all_diff_bytes_preserved': not diff.truncated_untracked and (b''.join(item.data for item in batches) == diff.data if batches else True),
        'calls': calls[:MAX_DISPLAY_CALLS], 'calls_truncated': len(calls) > MAX_DISPLAY_CALLS,
        'primary_aggregate_prompt_bytes': sum(call['prompt_bytes'] for call in primary),
        'max_primary_prompt_bytes': max((call['prompt_bytes'] for call in primary), default=None),
        'known_required_prompt_bytes': sum(known),
        'aggregate_required_prompt_bytes': sum(known) if len(known) == len(required) else None,
        'call_counts': {'required_logical_passes': len(required), 'maximum_conditional_passes': int(bool(conditional)),
            'provider_launch_upper_bound': sum(map(launch_bound, required)) + max(map(launch_bound, conditional), default=0),
            'method': 'configured retries/fallback upper bound; not expected calls', 'expected_launches': None},
        'route': route, 'selection': {'reason': reason, 'target_source': 'measured' if selected else 'explicit' if target is not None else 'configured',
            'target_bytes': defaults.batch_target_bytes, 'cohort_digest': selected['sample_digest'] if selected else None,
            'application': ['--batch-target-bytes', str(defaults.batch_target_bytes)] if mode == 'now' and not diff.truncated_untracked and (target is not None or defaults.batch_target_bytes > 0) else None,
            'prepush_configuration_target': defaults.batch_target_bytes if mode == 'prepush' else None,
            'application_note': 'Scope capture is incomplete; no target application is qualified. Resolve the capture limit and preview again.' if diff.truncated_untracked else 'Manually set defaults.batch_target_bytes in configuration before dispatch; no foreground command reproduces this pushed-ref scope.' if mode == 'prepush' else 'Apply the returned fixed arguments to review; execution captures fresh inputs and does not resample history.'},
        'measurements': data, 'request_runtime_range_seconds': None,
        'runtime_note': 'Per-call historical ranges are observations; conditional calls and queue waits prevent a request ETA.',
        'provider_processes_launched': 0}


def render(plan, output='text'):
    if output == 'json':
        return json.dumps(plan, sort_keys=True, ensure_ascii=True)
    if output != 'text':
        raise ValueError('output must be text or json')
    return json.dumps(plan, sort_keys=True, ensure_ascii=True, indent=2)
