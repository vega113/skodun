"""Review planning exercises the shipped builders without launching providers."""
from dataclasses import replace
import json

from skodun import services
from skodun.config import Config, Defaults, Reviewer
from skodun.store import Store
from tests.test_requests import _ready_repo


def test_preview_is_read_only_and_matches_single_review_prompt(tmp_path, monkeypatch):
    from skodun import runner
    repo = _ready_repo(tmp_path, monkeypatch)
    launched = []
    monkeypatch.setattr(runner, 'run_with_watchdog', lambda *a, **k: launched.append(1))
    with Store.open(tmp_path / 's.db') as store:
        before = store._c.total_changes
        code, text = services.svc_review_plan(store, repo, output='json')
        assert code == 0
        plan = json.loads(text)
        assert plan['mode'] == 'now'
        assert plan['calls'][0]['kind'] == 'primary'
        assert plan['calls'][0]['prompt_bytes'] > plan['diff_bytes']
        assert plan['provider_processes_launched'] == 0
        assert launched == []
        assert store._c.total_changes == before
        assert store.list_requests() == []


def history_records(*, failure=False, request_count=5):
    from skodun.planning_policy import describe
    policy = describe(Defaults(context_pack=False), Reviewer(name='finder', provider='xai', model='test', role='finder'))
    records = []
    for i in range(20):
        records.append({'id': f'review-{i}', 'request_id': f'request-{i % request_count}',
            'reviewed_at': '2026-09-01T12:00:00Z', 'mode': 'now',
            'planning_policy': policy,
            'batched': True, 'batches': [{'index': 1, 'diff_bytes': 8000,
                'parse_ok': True, 'degraded': False, 'diff_truncated': False,
                'telemetry': {'bytes': {'context': 0}, 'attempts': [{'attempt_id': f'call-{i}', 'timeout_sec': 60}]},
                'attempts': [{'attempt_id': f'call-{i}', 'provider': 'xai', 'model': 'test',
                    'effort': None, 'rc': 0, 'timed_out': failure and i == 0,
                    'classification': {'kind': 'ok'}, 'duration_sec': 5.0, 'input_bytes': 8193 if i % 2 else 16384}]}]})
    return records


def test_measured_target_needs_complete_uncensored_multi_request_cohort():
    from skodun.operational_targets import evidence
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    good = evidence(history_records(), reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert good['cohorts'][0]['qualified'] is True
    assert good['cohorts'][0]['request_count'] == 5
    assert good['cohorts'][0]['target_bytes'] == 8000
    bad = evidence(history_records(failure=True), reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert bad['cohorts'][0]['qualified'] is False
    assert bad['cohorts'][0]['censored_count'] == 1
    single = evidence(history_records(request_count=1), reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert single['cohorts'][0]['qualified'] is False


def test_measured_selection_is_explicit_and_never_discards_failures(tmp_path, monkeypatch):
    from skodun import config
    from tests.test_cli import _round
    repo = _ready_repo(tmp_path, monkeypatch)
    for i in range(30):
        (repo / f'diff-{i}.txt').write_text('content\n' * 100)
    cfg = Config(defaults=Defaults(context_pack=False, max_diff_bytes=100000),
                 reviewers=(Reviewer(name='finder', provider='xai', model='test', role='finder'),))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    with Store.open(tmp_path / 's.db') as store:
        for item in history_records():
            store.save_review(_round(**item))
        common = dict(reviewer='finder', target_source='measured', now='2026-09-05T12:00:00Z', output='json')
        code, text = services.svc_review_plan(store, repo, **common)
        assert code == 0
        assert json.loads(text)['selection']['reason'] == 'latency_objective_required'
        code, text = services.svc_review_plan(store, repo, target_latency_seconds=6, **common)
        assert code == 0
        plan = json.loads(text)
        assert plan['selection']['target_bytes'] == 8000, (plan['selection'], [(x['kind'], x['prompt_bytes']) for x in plan['calls']])
        assert plan['selection']['application'] == ['--batch-target-bytes', '8000']
        assert plan['selection']['target_source'] == 'measured'
        assert plan['batch_count'] > 1
        code, text = services.svc_review_plan(store, repo, target_latency_seconds=6, batch_target_bytes=16000, **common)
        assert json.loads(text)['selection']['target_bytes'] == 16000
        assert json.loads(text)['selection']['reason'] == 'explicit_override'
        failed = history_records(failure=True)[0]
        store.save_review(_round(**failed))
        _, text = services.svc_review_plan(store, repo, target_latency_seconds=6, **common)
        assert json.loads(text)['selection']['reason'] == 'insufficient_matching_evidence'
        assert json.loads(text)['selection']['target_bytes'] == 0


def test_preview_and_execution_share_exact_prepared_prompt_bytes(tmp_path, monkeypatch):
    from skodun import pipeline
    repo = _ready_repo(tmp_path, monkeypatch)
    seen = []
    prepare = pipeline._prepare_single_prompt
    def capture(*args, **kwargs):
        pack, prompt = prepare(*args, **kwargs)
        seen.append(prompt.text)
        return pack, prompt
    monkeypatch.setattr(pipeline, '_prepare_single_prompt', capture)
    with Store.open(tmp_path / 's.db') as store:
        code, text = services.svc_review_plan(store, repo, output='json')
        assert code == 0 and len(seen) == 1
        expected = seen[0]
        status, _, metadata = services.svc_review_detailed(store, repo)
        assert status == 0
        assert seen[1] == expected
        rec = store.get_review(metadata['observation']['review_id'])
        assert rec['planning_policy'] == json.loads(text)['planning_policy']


def test_checkpoint_target_change_is_named_even_when_boundaries_match():
    from skodun import checkpoints
    from tests.test_checkpoints import _identity
    original = _identity()
    different = replace(original, batch_budget=original.batch_budget + 1, config_hash='different')
    assert checkpoints.first_mismatch(original, different) == 'batch_budget'


def test_new_planning_policy_refuses_old_or_changed_target_reuse(tmp_path):
    from skodun import planning_policy, reuse
    from tests.test_reuse import _identity, _record
    first = planning_policy.describe(Defaults(batch_target_bytes=1000))
    identity = _identity(planning_policy=first)
    with Store.open(tmp_path / 's.db') as store:
        store.save_review(_record(identity))
        assert reuse.find_exact_candidate(store, identity) is not None
        changed = replace(identity, planning_policy=planning_policy.describe(Defaults(batch_target_bytes=2000)))
        assert reuse.find_exact_candidate(store, changed) is None
        assert planning_policy.mismatch(first, changed.planning_policy) == 'operational_target_changed'
        store.save_review(_record(identity, planning_policy=None))
        assert reuse.find_exact_candidate(store, identity) is None


def test_prepush_scope_uses_remote_oid_and_ignores_working_tree_edits(tmp_path, monkeypatch):
    from skodun import gitio
    from tests.test_gitio import _git
    repo = _ready_repo(tmp_path, monkeypatch)
    remote = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'pushed change')
    head = _git(repo, 'rev-parse', 'HEAD')
    args = dict(mode='prepush', local_ref='refs/heads/feat', local_oid=head,
                remote_ref='refs/heads/feat', remote_oid=remote, output='json')
    _, first = services.svc_review_plan(None, repo, **args)
    (repo / 'a.txt').write_text('UNPUSHED local edit\n')
    code, second = services.svc_review_plan(None, repo, **args)
    assert code == 0
    left, right = json.loads(first), json.loads(second)
    assert right['base']['source'] == 'remote_oid'
    assert right['base']['sha'] == remote and right['base']['head'] == head
    assert right['diff_hash'] == gitio.diff_identity(gitio.capture_ref_diff(repo, remote, head).data)
    assert right['calls'][0]['prompt_hash'] == left['calls'][0]['prompt_hash']
    assert right['diff_hash'] == left['diff_hash']


def test_stack_base_is_reported_but_never_overrides_execution_scope(tmp_path, monkeypatch):
    from tests.test_pipeline import _stack_request
    from skodun import gitio
    repo = _ready_repo(tmp_path, monkeypatch)
    _stack_request(tmp_path, repo)
    manifest = tmp_path / 'stack.json'
    code, text = services.svc_review_plan(None, repo, stack_manifest=manifest, output='json')
    assert code == 0
    plan = json.loads(text)
    assert plan['base']['requested']['stack_certification_base'] == gitio.resolve_base(repo).sha
    assert plan['stack']['status'] == 'valid'
    doc = json.loads(manifest.read_text())
    doc['certification_base'] = 'f' * 40
    manifest.write_text(json.dumps(doc))  # malformed annotation remains advisory
    code, text = services.svc_review_plan(None, repo, stack_manifest=manifest, output='json')
    assert code == 0
    assert json.loads(text)['stack']['status'] != 'valid'
    assert json.loads(text)['base']['sha'] == plan['base']['sha']
    assert json.loads(text)['diff_hash'] == plan['diff_hash']


def test_changed_worktree_invalidates_the_preview_snapshot(tmp_path, monkeypatch):
    from skodun import pipeline
    repo = _ready_repo(tmp_path, monkeypatch)
    build = pipeline._prepare_single_prompt
    def moving(*args, **kwargs):
        result = build(*args, **kwargs)
        (repo / 'a.txt').write_text('changed during preview\n')
        return result
    monkeypatch.setattr(pipeline, '_prepare_single_prompt', moving)
    code, text = services.svc_review_plan(None, repo, output='json')
    assert code == 2 and json.loads(text)['status'] == 'stale'
    assert json.loads(text)['provider_processes_launched'] == 0


def test_eighteen_batch_preview_separates_aggregate_and_maximum_and_checks_fallback(tmp_path, monkeypatch):
    from skodun import config
    from tests.test_gitio import _git
    repo = _ready_repo(tmp_path, monkeypatch)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'fixture baseline')
    _git(repo, 'branch', '-f', 'main', 'HEAD')
    for i in range(18):
        (repo / f'é-{i}.txt').write_text('é' * 190000 + '\n')
    finder = Reviewer(name='finder', provider='xai', model='test', role='finder', fallbacks=('small',))
    small = Reviewer(name='small', provider='google', model='test', role='finder')
    cfg = Config(defaults=Defaults(context_pack=False, max_diff_bytes=10000000), reviewers=(finder, small))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    code, text = services.svc_review_plan(None, repo, reviewer='finder', batch_target_bytes=400000, output='json')
    assert code == 0
    plan = json.loads(text)
    assert plan['batch_count'] == 18 and plan['all_diff_bytes_preserved']
    primary = [call for call in plan['calls'] if call['kind'] == 'batch']
    assert len(primary) == 18
    assert plan['primary_aggregate_prompt_bytes'] == sum(call['prompt_bytes'] for call in primary)
    assert plan['primary_aggregate_prompt_bytes'] > 6_000_000
    assert plan['max_primary_prompt_bytes'] < 500_000
    assert all(call['paths'][1]['status'] == 'ineligible' for call in primary)
    integration = next(call for call in plan['calls'] if call['kind'] == 'integration')
    assert integration['required_batch_count'] == 18
    assert integration['prompt_bytes'] is None
    assert plan['aggregate_required_prompt_bytes'] is None
    _, coarser = services.svc_review_plan(None, repo, reviewer='finder', batch_target_bytes=800000, output='json')
    coarse = json.loads(coarser)
    assert coarse['batch_count'] == 9
    assert coarse['max_primary_prompt_bytes'] > plan['max_primary_prompt_bytes']
    assert coarse['call_counts']['required_logical_passes'] < plan['call_counts']['required_logical_passes']
    assert coarse['request_runtime_range_seconds'] is None


def test_cli_mcp_preview_are_same_read_model_and_missing_store_is_not_created(tmp_path, monkeypatch, capsys):
    import threading
    from skodun import cli, mcpserver
    repo = _ready_repo(tmp_path, monkeypatch)
    missing = tmp_path / 'missing.db'
    monkeypatch.setenv('SKODUN_DB', str(missing))
    assert cli.main(['review-plan', '--repo', str(repo), '--json']) == 0
    cli_data = json.loads(capsys.readouterr().out)
    handler = next(spec.handler for spec in mcpserver.default_registry() if spec.name == 'review_plan')
    result = handler(mcpserver.HandlerCall(params={'repo': str(repo), 'output': 'json'},
        store_factory=mcpserver.default_store_factory, cancel=threading.Event()))
    assert result.status == 0
    mcp_data = json.loads(result.text)
    assert cli_data['plan_digest'] == mcp_data['plan_digest']
    assert cli_data['calls'] == mcp_data['calls']
    assert cli_data['measurements']['status'] == 'unavailable'
    assert not missing.exists()


def test_cohort_missing_size_outcome_and_duplicate_identity_are_not_cherry_picked():
    from skodun.operational_targets import evidence
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    records = history_records()
    duplicate = evidence(records + records, reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert duplicate['cohorts'][0]['sample_count'] == 20
    assert duplicate['duplicate_rows'] == 20
    records[0]['batches'][0]['attempts'][0]['input_bytes'] = None
    missing = evidence(records, reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert missing['incomplete_rows'] == 1
    assert not any(cohort['qualified'] for cohort in missing['cohorts'])


def test_historical_prepush_breadth_is_labeled_as_stored_aggregate(tmp_path, monkeypatch):
    from skodun import gitio
    from tests.test_gitio import _git
    from tests.test_cli import _round
    repo = _ready_repo(tmp_path, monkeypatch)
    base = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'pushed fixture')
    head = _git(repo, 'rev-parse', 'HEAD')
    with Store.open(tmp_path / 's.db') as store:
        store.save_review(_round(id='historical', mode='prepush', repo_id=str(gitio.git_common_dir(repo)),
            branch='feat', base_ref='refs/heads/feat', base_sha=base, head=head,
            prompt_bytes=6_914_679, batch_count=18, files_changed=[str(i) for i in range(459)],
            batches=[{'prompt_bytes': 406186}]))
        code, text = services.svc_review_plan(store, repo, review_id='historical', output='json')
        assert code == 0
        plan = json.loads(text)
        assert plan['base']['sha'] == base
        assert plan['base']['source'] == 'historical_review_record'
        old = plan['historical_observation']
        assert old['aggregate_prompt_bytes'] == 6_914_679
        assert old['maximum_batch_prompt_bytes'] == 406186
        assert old['declared_batch_count'] == 18 and old['changed_file_count'] == 459
        assert old['historical_configuration_available'] is False
        assert old['task_intent'] == 'unknown'


def test_checkpoint_raw_target_change_is_named_above_identical_ceiling():
    from skodun import checkpoints, planning_policy
    from tests.test_checkpoints import _identity
    first = planning_policy.describe(Defaults(max_diff_bytes=1000, batch_target_bytes=2000))
    second = planning_policy.describe(Defaults(max_diff_bytes=1000, batch_target_bytes=3000))
    assert first['effective_diff_budget'] == second['effective_diff_budget']
    left = replace(_identity(), planning_policy=first)
    right = replace(left, planning_policy=second, config_hash='changed')
    assert checkpoints.first_mismatch(left, right) == 'operational_target_changed'


def test_missing_or_mismatched_context_history_never_qualifies():
    from skodun.operational_targets import evidence
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    records = history_records()
    records[0].pop('planning_policy')
    missing = evidence(records, reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    assert missing['incomplete_rows'] == 1
    assert not any(c['qualified'] for c in missing['cohorts'])
    different = evidence(history_records(), reviewer=reviewer, mode='now', context_pack=True, now='2026-09-05T12:00:00Z')
    assert different['cohorts'] == []


def test_large_unicode_preview_matches_execution_batches_and_boundaries(tmp_path, monkeypatch):
    from skodun import pipeline
    repo = _ready_repo(tmp_path, monkeypatch)
    for i in range(6):
        (repo / f'unicode-{i}.txt').write_text('évidence\n' * 180)
    seen = []
    prepare = pipeline._prepare_batch_plan
    def capture(*args, **kwargs):
        plan = prepare(*args, **kwargs)
        seen.append(plan)
        return plan
    monkeypatch.setattr(pipeline, '_prepare_batch_plan', capture)
    with Store.open(tmp_path / 's.db') as store:
        code, text = services.svc_review_plan(store, repo, batch_target_bytes=4000, output='json')
        assert code == 0 and len(seen) == 1
        preview = json.loads(text)
        assert preview['batch_count'] > 1 and preview['all_diff_bytes_preserved']
        first = seen[0]
        status, _, meta = services.svc_review_detailed(store, repo, batch_target_bytes=4000)
        assert status == 0
        assert len(seen) >= 2
        executed = seen[1]
        assert [item.prompt.text for item in first.batches] == [item.prompt.text for item in executed.batches]
        assert first.boundary_digest == executed.boundary_digest == preview['boundary_digest']
        rec = store.get_review(meta['observation']['review_id'])
        assert rec['planning_policy'] == preview['planning_policy']


def test_new_prepush_branch_uses_shipped_ref_resolver(tmp_path, monkeypatch):
    from skodun import gitio
    from tests.test_gitio import _git
    repo = _ready_repo(tmp_path, monkeypatch)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'new branch work')
    head = _git(repo, 'rev-parse', 'HEAD')
    expected = gitio.resolve_ref_base(repo, head)
    code, text = services.svc_review_plan(None, repo, mode='prepush',
        local_ref='refs/heads/feat', local_oid=head, remote_ref='refs/heads/feat',
        remote_oid='0' * 40, batch_target_bytes=4000, output='json')
    assert code == 0
    plan = json.loads(text)
    assert plan['base']['source'] == 'new_ref_resolver'
    assert plan['base']['sha'] == expected.sha
    assert plan['selection']['application'] is None
    assert 'defaults.batch_target_bytes' in plan['selection']['application_note']


def test_measured_candidate_rejects_new_input_above_observed_prompt_range(tmp_path, monkeypatch):
    from skodun import config
    from tests.test_cli import _round
    repo = _ready_repo(tmp_path, monkeypatch)
    for i in range(8):
        (repo / f'diff-{i}.txt').write_text('content\n' * 300)
    cfg = Config(defaults=Defaults(context_pack=False, max_diff_bytes=100000),
                 reviewers=(Reviewer(name='finder', provider='xai', model='test', role='finder'),))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    with Store.open(tmp_path / 's.db') as store:
        records = history_records()
        for item in records:
            item['batches'][0]['attempts'][0]['input_bytes'] = 8001
            store.save_review(_round(**item))
        code, text = services.svc_review_plan(store, repo, reviewer='finder', target_source='measured',
            target_latency_seconds=6, now='2026-09-05T12:00:00Z', output='json')
        assert code == 0
        plan = json.loads(text)
        assert plan['selection']['reason'] == 'candidate_inputs_outside_observed_range'
        assert plan['selection']['target_source'] == 'configured'


def test_launch_upper_bound_counts_attempts_instead_of_runtime_seconds(tmp_path, monkeypatch):
    from skodun import config
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = Config(defaults=Defaults(timeout_sec=100, timeout_retries=1, degraded_retries=2),
                 reviewers=(Reviewer(name='finder', provider='xai', model='test', role='finder'),))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    code, text = services.svc_review_plan(None, repo, output='json')
    assert code == 0
    plan = json.loads(text)
    assert plan['calls'][0]['attempt_budget_per_entry'] == 4
    assert plan['call_counts']['provider_launch_upper_bound'] == 4


def test_zero_target_uses_shipped_configured_target_semantics(tmp_path, monkeypatch):
    from skodun import config
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = Config(defaults=Defaults(batch_target_bytes=4000),
                 reviewers=(Reviewer(name='finder', provider='xai', model='test', role='finder'),))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    code, text = services.svc_review_plan(None, repo, batch_target_bytes=0, output='json')
    assert code == 0
    assert json.loads(text)['selection']['target_source'] == 'configured'
    assert json.loads(text)['selection']['application'] == ['--batch-target-bytes', '4000']


def test_evidence_scopes_running_and_unsupported_rows_before_qualification():
    from copy import deepcopy
    from skodun import operational_targets as targets, planning_policy
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    kwargs = dict(reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    unrelated = deepcopy(history_records()[0])
    unrelated['status'] = 'running'
    unrelated['planning_policy'] = planning_policy.describe(Defaults(context_pack=False),
        Reviewer(name='other', provider='google', model='other', role='finder'))
    good = targets.evidence(history_records() + [unrelated], **kwargs)
    assert good['cohorts'][0]['qualified']
    matching = deepcopy(history_records()[0])
    matching['status'] = 'running'
    bad = targets.evidence(history_records() + [matching], **kwargs)
    assert not bad['cohorts'][0]['qualified']
    legacy = deepcopy(history_records()[0])
    legacy['planning_policy']['version'] = 'review-planning/v0'
    old = targets.evidence(history_records() + [legacy], **kwargs)
    assert old['cohorts'][0]['qualified']
    assert old['unsupported_policy_records'] == 1


def test_missing_attempt_attribution_is_incomplete_but_known_other_provider_is_not():
    from skodun.operational_targets import evidence
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    kwargs = dict(reviewer=reviewer, mode='now', now='2026-09-05T12:00:00Z')
    for malformed in (None, {}, {'provider': 'xai'}, {'provider': 'xai', 'model': 'test'}):
        records = history_records()
        records[0]['batches'][0]['attempts'].append(malformed)
        result = evidence(records, **kwargs)
        assert result['incomplete_rows'] >= 1
        assert not any(c['qualified'] for c in result['cohorts'])
    records = history_records()
    records[0]['batches'][0]['attempts'].append({'provider': 'google'})
    assert evidence(records, **kwargs)['cohorts'][0]['qualified']


def test_checkpoint_copy_request_ids_do_not_inflate_or_conflict():
    from copy import deepcopy
    from skodun.operational_targets import evidence
    records = history_records()
    copies = deepcopy(records)
    for item in copies:
        item['request_id'] += '-copy'
        item['skodun_commit'] = 'copying-build'
    result = evidence(copies + records, reviewer=Reviewer(name='finder', provider='xai', model='test', role='finder'),
        mode='now', now='2026-09-05T12:00:00Z')
    assert result['conflicting_attempt_ids'] == 0
    assert result['cohorts'][0]['sample_count'] == 20
    assert result['cohorts'][0]['request_count'] == 5
    assert result['cohorts'][0]['qualified']


def test_checkpoint_rejects_malformed_planning_policy():
    import pytest
    from skodun import checkpoints, planning_policy
    from tests.test_checkpoints import _identity
    policy = planning_policy.describe(Defaults())
    for bad in (True, [], {'version': 'review-planning/v1'}, {**policy, 'target_bytes': True},
                {**policy, 'context_pack': 1}, {**policy, 'digest': 'f' * 64}):
        with pytest.raises(ValueError, match='planning'):
            replace(_identity(), planning_policy=bad)
        assert planning_policy.mismatch(bad, policy) == 'planning_identity_missing'


def test_readonly_open_errors_keep_cli_mcp_preview_available(tmp_path, monkeypatch, capsys):
    import sqlite3
    import threading
    from skodun import cli, mcpserver
    repo = _ready_repo(tmp_path, monkeypatch)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('unreadable fixture')
    monkeypatch.setattr(Store, 'open_readonly', unavailable)
    assert cli.main(['review-plan', '--repo', str(repo), '--json']) == 0
    left = json.loads(capsys.readouterr().out)
    handler = next(s.handler for s in mcpserver.default_registry() if s.name == 'review_plan')
    result = handler(mcpserver.HandlerCall(params={'repo': str(repo), 'output': 'json'},
        store_factory=mcpserver.default_store_factory, cancel=threading.Event()))
    assert result.status == 0
    right = json.loads(result.text)
    assert left['measurements']['unavailable_reason'] == right['measurements']['unavailable_reason'] == 'OperationalError'


def test_fitting_fallback_limit_without_validator_is_reported(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from skodun import plan_preview, pipeline
    reviewer = Reviewer(name='finder', provider='xai', model='test', role='finder')
    cfg = Config(defaults=Defaults(), reviewers=(reviewer,))
    monkeypatch.setattr(pipeline, '_adapter_for', lambda _entry: SimpleNamespace(prompt_limit=lambda: 1000))
    paths = plan_preview._paths(cfg, reviewer, cfg.defaults, SimpleNamespace(text=b'a', prompt_bytes=1))
    assert paths[0]['status'] == 'admissible'


def test_measured_timeout_and_advisory_security_truncation(tmp_path, monkeypatch):
    from skodun import config
    from tests.test_cli import _round
    repo = _ready_repo(tmp_path, monkeypatch)
    for i in range(30):
        (repo / f'diff-{i}.txt').write_text('content\n' * 100)
    finder = Reviewer(name='finder', provider='xai', model='test', role='finder')
    cfg = Config(defaults=Defaults(context_pack=False, max_diff_bytes=100000, timeout_sec=1), reviewers=(finder,))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    with Store.open(tmp_path / 's.db') as store:
        for item in history_records():
            store.save_review(_round(**item))
        code, text = services.svc_review_plan(store, repo, reviewer='finder', target_source='measured',
            target_latency_seconds=6, now='2026-09-05T12:00:00Z', output='json')
        assert code == 0
        assert json.loads(text)['selection']['reason'] == 'historical_timeout_incompatible'
    (repo / 'auth').mkdir()
    (repo / 'auth' / 'login.py').write_text('check\n' * 100)
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '1')
    security = Reviewer(name='security', provider='xai', model='test', role='security', max_diff_bytes=100)
    cfg = replace(cfg, defaults=replace(cfg.defaults, timeout_sec=60), reviewers=(finder, security))
    code, text = services.svc_review_plan(None, repo, reviewer='finder', batch_target_bytes=8000, output='json')
    plan = json.loads(text)
    assert code == 0
    call = next(c for c in plan['calls'] if c['kind'] == 'security')
    assert call['prompt_truncated'] and call['partial_coverage']


def test_reuse_reason_preserves_only_an_otherwise_matching_candidate(tmp_path, monkeypatch):
    from skodun import config, gitio, reuse, planning_policy
    from tests.test_reuse import _record
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = config.load_config(repo)
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    identity = reuse._identity_for(repo, cfg, base, diff, branch=gitio.current_branch(repo), reviewer_name=cfg.reviewers[0].name)
    changed = planning_policy.describe(replace(cfg.defaults, batch_target_bytes=1000), cfg.reviewers[0])
    target_only = _record(identity, planning_policy=changed, trustworthy=True)
    unrelated = _record(identity, branch='other', trustworthy=True)
    with Store.open(tmp_path / 's.db') as store:
        monkeypatch.setattr(store, 'reuse_candidates', lambda *args: [target_only, unrelated])
        assert reuse.probe(store, repo, cfg=cfg).reason == 'operational_target_changed'
        unrelated['planning_policy'] = changed
        monkeypatch.setattr(store, 'reuse_candidates', lambda *args: [unrelated])
        assert reuse.probe(store, repo, cfg=cfg).reason == 'no exact trustworthy review matched'


def test_mcp_preview_rejects_boolean_and_invalid_enum_inputs(tmp_path, monkeypatch):
    import threading
    from skodun import mcpserver
    repo = _ready_repo(tmp_path, monkeypatch)
    handler = next(s.handler for s in mcpserver.default_registry() if s.name == 'review_plan')
    with Store.open(tmp_path / 's.db') as store:
        from contextlib import nullcontext
        for invalid in ({'mode': False}, {'mode': ''}, {'batch_target_bytes': True},
                        {'target_latency_seconds': True}, {'target_source': 'unknown'}, {'output': 'unknown'}):
            result = handler(mcpserver.HandlerCall(params={'repo': str(repo), **invalid},
                store_factory=lambda: nullcontext(store), cancel=threading.Event()))
            assert result.status == 2


def test_untracked_capture_limit_reports_incomplete_scope_and_no_application(tmp_path, monkeypatch):
    from skodun import config, gitio
    repo = _ready_repo(tmp_path, monkeypatch)
    for index in range(3):
        (repo / f'untracked-{index}.txt').write_text('uncaptured scope\n')
    cfg = config.load_config(repo)
    cfg = replace(cfg, defaults=replace(cfg.defaults, untracked_max=1))
    monkeypatch.setattr(config, 'load_config', lambda _root: cfg)
    captured = gitio.capture_diff(repo, gitio.resolve_base(repo).sha, cfg.defaults.untracked_max)
    assert captured.truncated_untracked
    code, text = services.svc_review_plan(None, repo, batch_target_bytes=4000,
        target_source='measured', target_latency_seconds=30, output='json')
    plan = json.loads(text)
    assert code == 2 and plan['status'] == 'unreviewable'
    assert plan['scope_capture']['truncated_untracked']
    assert plan['scope_capture']['reason_code'] == 'untracked_capture_limit'
    assert not plan['all_diff_bytes_preserved']
    assert plan['selection']['reason'] == 'incomplete_scope_capture'
    assert plan['selection']['application'] is None
