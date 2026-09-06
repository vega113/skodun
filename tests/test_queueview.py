"""Shipped request/queue inspection with hermetic stores and no providers."""
import json
import threading

import pytest

from skodun import cli, mcpserver, services
from skodun.store import Store
from tests.test_cli import _round

NOW = '2026-09-05T12:00:30Z'


def request(store, rid='sk_req_a', root='/work/a'):
    store.begin_request(request_id=rid, scope=root, request_key=None,
        identity={'worktree_root': root, 'repo_id': '/common', 'branch': rid},
        intent={}, owner_token=rid, pid=123, source='cli',
        now='2026-09-05T12:00:00Z', expires_at='2026-09-06T12:00:00Z')
    return rid


def admission(store, rid, aid, status, queued, admitted=None, ended=None):
    store.capacity_enqueue(admission_id=aid, resource_class='review-fg', scope='/common')
    store._c.execute('UPDATE capacity_admissions SET status=?,queued_at=?,admitted_at=?,ended_at=? WHERE id=?',
                     (status, queued, admitted, ended, aid))
    store.link_request(rid, 'capacity', aid)


def test_four_owner_queue_has_actual_wait_and_no_invented_eta(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        for name in 'abcd':
            request(store, 'sk_req_' + name, '/work/' + name)
        admission(store, 'sk_req_a', 'running', 'running', '2026-09-05T12:00:00Z', '2026-09-05T12:00:00Z')
        admission(store, 'sk_req_b', 'queued', 'queued', '2026-09-05T12:00:05Z')
        admission(store, 'sk_req_c', 'free', 'released', '2026-09-05T12:00:00Z', '2026-09-05T12:00:00Z', '2026-09-05T12:00:01Z')
        admission(store, 'sk_req_d', 'expired', 'expired', '2026-09-05T12:00:00Z', ended='2026-09-05T12:00:10Z')
        code, text = services.svc_queue(store, scope='host', output='json', now=NOW)
        assert code == 0
        data = json.loads(text)
        rows = {r['request_id']: r for r in data['requests']}
        queued = rows['sk_req_b']['admissions'][0]
        assert queued['wait_elapsed_ms'] == 25000
        assert queued['position'] == 1
        assert queued['holders'][0]['request_id'] == 'sk_req_a'
        assert queued['effective_limit'] is None
        assert rows['sk_req_c']['admissions'][0]['wait_elapsed_ms'] == 0
        assert rows['sk_req_c']['admissions'][0]['position'] is None
        assert rows['sk_req_d']['admissions'][0]['wait_elapsed_ms'] == 10000
        assert 'eta' not in queued
        assert queued['historical_median_wait']['sample_count'] == 2


def test_request_costs_dedup_nested_attempts_and_union_concurrent_intervals(tmp_path):
    from skodun import queueview
    def attempt(key, start, end, prompt, usage=None):
        return {'attempt_id': key, 'n': 1, 'provider': 'openai', 'rc': 0,
                'timed_out': False, 'input_bytes': prompt, 'usage': usage,
                'capacity_timing': {'started_at': start, 'ended_at': end}}
    a = attempt('call-a', '2026-09-05T12:00:01Z', '2026-09-05T12:00:11Z', 3_000_000,
                {'total_tokens': 9})
    b = attempt('call-b', '2026-09-05T12:00:06Z', '2026-09-05T12:00:16Z', 3_900_000)
    skip = {'n': 2, 'provider': 'openai', 'skipped': 'prompt_too_large', 'input_bytes': 9_000_000,
            'input_eligibility': {'reason': 'prompt_too_large'}}
    rec = _round(id='review', request_id='sk_req_a', batch_orchestration_id='same', orchestration_id='same',
                 prompt_bytes=6_900_000, batches=[{'index': 0, 'attempts': [a, skip],
                     'telemetry': {'attempts': [a, skip]}}, {'index': 1, 'attempts': [b]}],
                 attempts=[a], extra_passes={})
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        store.save_review(rec)
        store.link_request(rid, 'review', rec['id'])
        store.link_request(rid, 'review', 'missing')
        store.link_request(rid, 'recovery_orchestration', 'same')
        store.link_request(rid, 'batch_orchestration', 'same')
        data = queueview.inspect(store, request_id=rid, now=NOW)['requests'][0]
        costs = data['costs']
        assert costs['launched_calls'] is None  # one linked review is missing
        assert costs['reported_launched_calls'] == 2
        assert costs['eligibility_skips'] == 1
        assert costs['aggregate_launched_prompt_bytes'] is None
        assert costs['reported_launched_prompt_bytes'] == 6_900_000
        assert costs['max_per_call_prompt_bytes'] is None
        assert costs['reported_max_per_call_prompt_bytes'] == 3_900_000
        assert costs['token_usage']['total'] is None
        assert costs['token_usage']['reported_total'] == 9
        assert costs['metered_spend']['usd'] is None
        assert data['timing']['provider_elapsed']['value_ms'] == 15000
        assert data['denominators']['recovery_orchestration_ids'] == 1
        assert data['denominators']['batch_orchestration_ids'] == 1
        assert data['coverage']['missing_reviews'] == ['missing']
        assert data['coverage']['status'] == 'partial'


def test_cli_mcp_queue_json_agree_and_do_not_write_store(tmp_path, monkeypatch, capsys):
    db = tmp_path / 's.db'
    with Store.open(db) as store:
        rid = request(store)
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='complete',
                             result=None, now=NOW)
    monkeypatch.setenv('SKODUN_DB', str(db))
    before = db.read_bytes()
    assert cli.main(['queue', rid, '--json']) == 0
    cli_data = json.loads(capsys.readouterr().out)
    handler = next(spec.handler for spec in mcpserver.default_registry() if spec.name == 'queue')
    result = handler(mcpserver.HandlerCall(params={'request_id': rid, 'output': 'json'},
        store_factory=mcpserver.default_store_factory, cancel=threading.Event()))
    assert result.status == 0
    mcp_data = json.loads(result.text)
    # Terminal-independent metadata and frozen timestamps make this invariant
    # robust even if the wall clock crosses a second between surfaces.
    cli_data.pop('observed_at'); mcp_data.pop('observed_at')
    assert cli_data == mcp_data
    assert db.read_bytes() == before
    assert 'owner_token' not in result.text


def test_queue_refuses_missing_store_without_creating_it(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'missing.db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    assert cli.main(['queue', '--scope', 'host']) == 2
    assert not db.exists()
    assert 'read-only' in capsys.readouterr().out


def test_historical_wait_excludes_provider_run_time(tmp_path):
    from skodun import capacity
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        for i in range(3):
            admission(store, rid, f'done-{i}', 'released', '2026-09-05T12:00:00Z',
                      '2026-09-05T12:00:01Z', '2026-09-05T12:00:30Z')
        store._c.execute('UPDATE capacity_admissions SET wait_ms=30000,queue_wait_ms=1000')
        median, count = capacity._historical_wait(store, 'review-fg', '/common')
        assert median == 1.0
        assert count == 3


def test_scoped_queue_defaults_do_not_select_another_worktree(tmp_path, monkeypatch):
    from tests.test_gitio import _mkrepo
    repo = _mkrepo(tmp_path)
    with Store.open(tmp_path / 's.db') as store:
        own = request(store, root=str(repo.resolve()))
        request(store, 'sk_req_other', '/elsewhere')
        code, text = services.svc_queue(store, repo, output='json', now=NOW)
        assert code == 0
        assert [row['request_id'] for row in json.loads(text)['requests']] == [own]
        code, text = services.svc_queue(store, repo, request_id='sk_req_other', output='json', now=NOW)
        assert code == 0
        assert json.loads(text)['scope'] == 'explicit_request'
        assert json.loads(text)['requests'][0]['identity']['worktree_root'] == '/elsewhere'


def test_latest_execution_budget_is_allowlisted_and_reports_effective_caps(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        admission(store, rid, 'queued', 'queued', '2026-09-05T12:00:00Z')
        store.request_budget = lambda _rid: {
            'scope': 'request_execution', 'request_id': rid, 'execution_seq': 1,
            'limits': {'max_queue_seconds': 30.0, 'secret': 'never expose'},
            'deadlines': {'queue': NOW, 'secret': 'never expose'},
            'capacity_layers': [{'admission_id': 'queued', 'resource_class': 'review-fg', 'scope': '/common',
                'effective_capacity': 1, 'configured_capacity': 4, 'legacy_dual_hold': True,
                'secret': 'never expose'}],
            'timing': {'queue_wait_ms': 30000, 'secret': 'never expose'},
            'secret': 'never expose'}
        code, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        assert code == 0
        row = json.loads(text)['requests'][0]
        assert row['time_limits']['max_queue_seconds'] == 30
        assert row['capacity_layers'][0]['configured_capacity'] == 4
        assert row['admissions'][0]['effective_limit'] == 1
        assert row['admissions'][0]['contended'] is False
        assert row['deadlines']['queue'] == NOW
        assert 'secret' not in text and 'never expose' not in text


def test_reused_prior_review_does_not_charge_provider_calls_again(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='old', request_id='sk_req_previous', attempts=[
            {'attempt_id': 'old-call', 'rc': 0, 'input_bytes': 50,
             'usage': {'total_tokens': 9}, 'provider': 'openai'}])
        store.save_review(rec)
        store.link_request(rid, 'review', rec['id'])
        code, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['reported_launched_calls'] == 0
        assert costs['launched_calls'] is None  # request is still active
        assert costs['reused_reviews'] == ['old']
        assert costs['token_usage']['total'] is None


def test_fully_observed_prompt_costs_are_not_single_aggregate_call(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='review', request_id=rid, batched=True, prompt_bytes=6_900_000,
            attempts=[], batches=[{'index': i, 'attempts': [
                {'attempt_id': f'call-{i}', 'n': 1, 'rc': 0, 'provider': 'openai',
                 'input_bytes': 690_000, 'usage': {'total_tokens': 10}}]} for i in range(10)])
        store.save_review(rec)
        store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='complete', result=None, now=NOW)
        code, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['launched_calls'] == 10
        assert costs['aggregate_launched_prompt_bytes'] == 6_900_000
        assert costs['max_per_call_prompt_bytes'] == 690_000
        assert costs['review_bytes'][0]['prompt_scope'] == 'aggregate_batches_and_integration'
        assert costs['token_usage']['total'] == 100
        assert costs['metered_spend']['usd'] is None


def test_text_and_json_have_identical_request_fields(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        request(store, root='/work/"\nspoof')
        _, encoded = services.svc_queue(store, scope='host', output='json', now=NOW)
        _, text = services.svc_queue(store, scope='host', output='text', now=NOW)
        row = json.loads(next(line.removeprefix('request=') for line in text.splitlines()
                              if line.startswith('request=')))
        assert row == json.loads(encoded)['requests'][0]
        assert len(text.splitlines()) == 3


def test_stats_denominators_keep_two_orchestration_namespaces_and_sample_metadata(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        store.save_review(_round(id='round', request_id=rid, orchestration_id='same',
            batch_orchestration_id='same', reviewed_at='2026-09-05T12:00:00Z'))
        code, text = services.svc_stats(store, since_days=36500, fmt='json')
        assert code == 0
        stats = json.loads(text)
        denominator = stats['audit_denominators']
        assert denominator['requested_reviews_created'] == 1
        assert denominator['request_executions_started'] == 1
        assert denominator['recovery_orchestration_ids'] == 1
        assert denominator['batch_orchestration_ids'] == 1
        for metric in stats['timing'].values():
            assert metric['unit'] == 'ms'
            assert metric['quantile_method'] == 'nearest_rank'
            assert metric['sample_count'] == metric['count']
            assert set(metric['window']) == {'from', 'to'}


def test_missing_admission_timestamp_on_a_holder_is_not_queue_wait(tmp_path):
    from skodun import capacity
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        admission(store, rid, 'broken-holder', 'released', '2026-09-05T12:00:00Z',
                  ended='2026-09-05T12:00:30Z')
        store._c.execute("UPDATE capacity_admissions SET started_at='2026-09-05T12:00:01Z'")
        assert capacity._historical_wait(store, 'review-fg', '/common') == (None, 0)
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        row = json.loads(text)['requests'][0]['admissions'][0]
        assert row['wait_elapsed_ms'] is None
        assert row['historical_median_wait']['sample_count'] == 0


def test_queue_shares_indexed_peer_reads_and_labels_legacy_missing_timing(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        for name in 'abcd':
            rid = request(store, 'sk_req_' + name)
            admission(store, rid, 'admit-' + name, 'queued', '2026-09-05T12:00:00Z')
        store._c.execute("UPDATE capacity_admissions SET status='released' WHERE id='admit-d'")
        statements = []
        store._c.set_trace_callback(statements.append)
        code, text = services.svc_queue(store, scope='host', output='json', now=NOW)
        store._c.set_trace_callback(None)
        assert code == 0
        peer_queries = [sql for sql in statements if "AND status IN ('queued','admitted','running')" in sql]
        assert len(peer_queries) == 1
        plan = ' '.join(row['detail'] for row in store._c.execute('EXPLAIN QUERY PLAN ' + peer_queries[0]))
        assert 'ix_capacity_scope_status' in plan
        legacy = next(r for r in json.loads(text)['requests'] if r['request_id'] == 'sk_req_d')
        assert legacy['admissions'][0]['wait_elapsed_ms'] is None
        assert legacy['admissions'][0]['historical_median_wait']['value_ms'] is None


def test_resumed_request_deduplicates_checkpoint_calls_and_execution_wall_time(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        old = {'attempt_id': 'original-call', 'provider': 'openai', 'rc': 0, 'n': 1,
            'input_bytes': 10, 'usage': {'input_tokens': 5},
            'capacity_timing': {'started_at': '2026-09-05T12:00:02Z', 'ended_at': '2026-09-05T12:00:10Z'}}
        new = {**old, 'attempt_id': 'new-call', 'input_bytes': 20,
            'capacity_timing': {'started_at': '2026-09-05T12:00:22Z', 'ended_at': NOW}}
        for rec in (_round(id='first', request_id=rid, batch_orchestration_id='batch',
                          batches=[{'index': 1, 'attempts': [old]}]),
                    _round(id='second', request_id=rid, batch_orchestration_id='batch',
                          batches=[{'index': 1, 'attempts': [old], 'reused': True},
                                   {'index': 2, 'attempts': [new]}])):
            store.save_review(rec)
            store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='complete', result=None, now=NOW)
        store._c.execute("UPDATE request_executions SET completed_at='2026-09-05T12:00:10Z'")
        store._c.execute('INSERT INTO request_executions(request_id,owner_token,source,pid,started_at,completed_at,status) VALUES(?,?,?,?,?,?,?)',
            (rid, 'second-owner', 'cli', 123, '2026-09-05T12:00:20Z', NOW, 0))
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        row = json.loads(text)['requests'][0]
        assert row['costs']['launched_calls'] == 2
        assert row['costs']['aggregate_launched_prompt_bytes'] == 30
        assert row['costs']['reused_passes'] == 1
        assert row['denominators']['nested_batches'] == 2
        assert row['denominators']['nested_batch_records'] == 3
        assert row['denominators']['batch_orchestration_ids'] == 1
        assert row['coverage']['missing_namespace_links']['batch_orchestration'] == ['batch']
        assert row['costs']['token_usage']['input'] == 10
        assert row['costs']['token_usage']['total'] is None
        assert row['timing']['provider_elapsed']['value_ms'] == 16000
        assert row['timing']['execution_elapsed']['value_ms'] == 20000
        assert row['timing']['total_elapsed']['value_ms'] == 30000


def test_legacy_calls_and_metered_provider_request_ids_do_not_create_totals(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='legacy', request_id=rid, attempts=[{'rc': 0, 'n': 1, 'provider': 'openai'}])
        store.save_review(rec); store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='complete', result=None, now=NOW)
        store._c.execute('INSERT INTO api_spend_events(at,provider,prompt_tokens,completion_tokens,total_tokens,cost_usd,request_id) VALUES(?,?,?,?,?,?,?)',
            (NOW, 'openai-api', 10, 2, 12, 1.0, rid))
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['reported_launched_calls'] == 1
        assert costs['launched_calls'] is None
        assert costs['metered_spend']['usd'] is None
        assert costs['token_usage']['total'] is None


def test_absent_budget_getter_is_explicitly_unavailable(tmp_path):
    class LegacyStore:
        def __init__(self, store):
            self._c = store._c
            self.get_review = store.get_review
            self.capacity_get = store.capacity_get
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        _, text = services.svc_queue(LegacyStore(store), request_id=rid, output='json', now=NOW)
        row = json.loads(text)['requests'][0]
        assert row['budget_status'] == 'unavailable'
        assert row['budget_reason_code'] == 'budget_getter_unavailable'
        assert row['execution_budget_timing'] is None
        assert row['time_limits'] is None


def test_paused_review_budget_preserves_active_time_and_wall_time_separately(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        store.request_budget = lambda _rid: {
            'scope': 'request_execution', 'request_id': rid, 'execution_seq': 1,
            'phase': 'queue', 'limits': {}, 'deadlines': {'review': None},
            'timing': {'review_active_ms': 10000, 'review_wall_ms': 110000, 'total_ms': 110000},
            'review_paused_for_queue': True, 'reason_code': 'readmission', 'capacity_layers': [],
            'updated_at': '2026-09-05T12:01:50Z'}
        _, text = services.svc_queue(store, request_id=rid, output='json', now='2026-09-05T12:01:50Z')
        row = json.loads(text)['requests'][0]
        assert row['budget_phase'] == 'queue'
        assert row['review_paused_for_queue'] is True
        assert row['deadlines']['review'] is None
        measured = row['execution_budget_timing']['measurements_ms']
        assert measured['review_active_ms'] == 10000
        assert measured['review_wall_ms'] == measured['total_ms'] == 110000


def test_missing_extra_pass_attempts_keep_current_cost_totals_unknown(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='partial-telemetry', request_id=rid, attempts=[{
            'attempt_id': 'known', 'provider': 'openai', 'rc': 0, 'input_bytes': 10}],
            extra_passes={'skeptic': {'status': 'clean', 'ran': True}})
        store.save_review(rec)
        store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='completed', result=None, now=NOW)
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['reported_launched_calls'] == 1
        assert costs['counts_complete'] is False
        assert costs['launched_calls'] is None
        assert costs['aggregate_launched_prompt_bytes'] is None
        assert costs['missing_attempt_scopes'][0]['id'] == 'skeptic'


def test_real_review_attempt_ids_feed_request_costs(tmp_path, monkeypatch):
    from tests.test_requests import _ready_repo
    repo = _ready_repo(tmp_path, monkeypatch)  # executable fixture, no external provider
    with Store.open(tmp_path / 's.db') as store:
        status, _, metadata = services.svc_review_detailed(store, repo)
        assert status == 0
        rid = metadata['request']['id']
        observed = metadata['observation']
        assert observed['attempts'][0]['attempt_id']
        _, text = services.svc_queue(store, request_id=rid, output='json')
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['counts_complete'] is True
        assert costs['launched_calls'] == observed['launched_count'] == 1
        assert costs['aggregate_launched_prompt_bytes'] == observed['attempts'][0]['input_bytes']


def test_budget_reader_failure_remains_explicit_without_sensitive_error_text(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        def broken(_rid):
            raise ValueError('sensitive diagnostic text')
        store.request_budget = broken
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        row = json.loads(text)['requests'][0]
        assert row['budget_status'] == 'unavailable'
        assert row['budget_reason_code'] == 'budget_read_failed:ValueError'
        assert 'sensitive diagnostic text' not in text


def test_cancelled_observation_does_not_turn_empty_stub_into_zero_calls(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='stub', request_id=rid, attempts=[])
        store.save_review(rec); store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='cancelled', reason_code='interrupted',
            result={'status': 4, 'text': 'cancelled', 'metadata': {'observation': {
                'request_id': rid, 'counts_complete': False}}}, now=NOW)
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        row = json.loads(text)['requests'][0]
        assert row['costs']['launched_calls'] is None
        assert row['costs']['reported_launched_calls'] == 0
        assert row['costs']['result_observation_incomplete'] is True
        assert row['coverage']['status'] == 'partial'


@pytest.mark.skipif(not hasattr(Store, 'save_request_budget'), reason='real #185 budget store not yet merged')
def test_real_budget_api_keeps_current_timing_and_historical_admission_caps_separate(tmp_path):
    from tests.test_checkpoints import _created
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        first = store.get_request(rid)
        old_seq = first['executions'][0]['seq']
        def persist(seq, owner, phase, when, paused=False):
            assert store.save_request_budget(rid, seq, owner, {
                'scope': 'request_execution', 'request_id': rid, 'execution_seq': seq,
                'phase': phase, 'limits': {}, 'deadlines': {'review': None},
                'timing': {'review_active_ms': 10000, 'review_wall_ms': 110000, 'total_ms': 110000},
                'review_paused_for_queue': paused, 'reason_code': None, 'updated_at': when})
        def capacity(seq, owner, aid, cap, when):
            store.capacity_enqueue(admission_id=aid, resource_class='review-fg', scope='/common')
            store.link_request(rid, 'capacity', aid)
            assert store.record_request_capacity(rid, seq, owner, admission_id=aid,
                resource_class='review-fg', scope='/common', effective_capacity=cap,
                configured_capacity=4, legacy_dual_hold=cap == 1, updated_at=when)
        persist(old_seq, rid, 'review', NOW)
        capacity(old_seq, rid, 'old-admission', 4, NOW)
        store.capacity_finish('old-admission', status='released')
        store.finish_request(rid, owner_token=rid, state='failed', reason_code='interrupted', result=None, now=NOW)
        _created(store, orchestration_id='resume')
        _, resumed = store.begin_request(request_id='unused', scope='/work/a', request_key=None,
            identity=first['identity'], intent={}, owner_token='next', pid=123, source='cli',
            now='2026-09-05T12:00:20Z', expires_at='2026-09-06T12:00:00Z',
            continuation_id=rid, continuation_orchestration_id='resume')
        new_seq = resumed['executions'][0]['seq']
        persist(new_seq, 'next', 'queue', '2026-09-05T12:02:10Z', paused=True)
        capacity(new_seq, 'next', 'new-admission', 1, '2026-09-05T12:02:10Z')
        _, text = services.svc_queue(store, request_id=rid, output='json', now='2026-09-05T12:02:10Z')
        row = json.loads(text)['requests'][0]
        assert row['budget_status'] == 'known'
        assert row['budget_history_status'] == 'known'
        assert row['budget_phase'] == 'queue' and row['review_paused_for_queue'] is True
        assert row['execution_budget_timing']['execution_seq'] == new_seq
        assert row['execution_budget_timing']['measurements_ms']['review_active_ms'] == 10000
        assert row['execution_budget_timing']['measurements_ms']['review_wall_ms'] == 110000
        capacities = {item['id']: item['effective_limit'] for item in row['admissions']}
        assert capacities == {'old-admission': 4, 'new-admission': 1}


def test_candidate_skips_do_not_all_claim_transport_ineligibility(tmp_path):
    rows = [
        {'attempt_id': 'transport', 'skipped': 'too big', 'input_bytes': 200,
         'input_eligibility': {'reason': 'prompt_too_large', 'input_bytes': 200, 'limit_bytes': 100}},
        {'attempt_id': 'binary', 'skipped': 'binary missing'},
        {'attempt_id': 'quota', 'skipped': 'cached quota', 'classification': {'category': 'quota'}},
        {'attempt_id': 'admission', 'skipped': 'provider capacity wait', 'reason_code': 'provider_wait_exhausted'},
        {'attempt_id': 'launched', 'rc': 0, 'input_bytes': 50, 'provider': 'openai'},
    ]
    with Store.open(tmp_path / 's.db') as store:
        rid = request(store)
        rec = _round(id='mixed-skips', request_id=rid, attempts=rows)
        store.save_review(rec); store.link_request(rid, 'review', rec['id'])
        store.finish_request(rid, owner_token=rid, state='finished', reason_code='completed', result=None, now=NOW)
        _, text = services.svc_queue(store, request_id=rid, output='json', now=NOW)
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['candidate_skips'] == 4
        assert costs['eligibility_skips'] == 1
        assert costs['launched_calls'] == 1
        assert costs['aggregate_launched_prompt_bytes'] == 50


def test_reused_pass_observations_deduplicate_generations_and_keep_unknown_ids():
    from skodun.queueview import _reused_pass_observations
    reused = {'continuation_action': 'reused'}
    rec = {'batch_orchestration_id': 'generation', 'batches': [{'index': 1, **reused}],
           'integration': {'provenance': reused}, 'extra_passes': {'security': reused, 'skeptic': reused}}
    assert _reused_pass_observations([rec, rec])['reused_passes'] == 4
    next_generation = {**rec, 'batch_orchestration_id': 'next'}
    assert _reused_pass_observations([rec, next_generation])['reused_passes'] == 8
    legacy = {'batches': [{'reused': True}]}
    result = _reused_pass_observations([rec, legacy])
    assert result['reused_passes'] is None
    assert result['reported_reused_passes'] == 4
    assert result['reuse_identity_missing'] == 1


def test_zero_based_batch_reuse_is_an_unknown_identity():
    from skodun.queueview import _reused_pass_observations
    value = _reused_pass_observations([{'batch_orchestration_id': 'generation',
        'batches': [{'index': 0, 'continuation_action': 'reused'}]}])
    assert value['reused_passes'] is None
    assert value['reported_reused_passes'] == 0 and value['reuse_identity_missing'] == 1


def test_bounded_queue_inspection_uses_the_same_front_as_admission(tmp_path, monkeypatch):
    from skodun import queueview, store as store_module
    monkeypatch.setattr(store_module, '_iso_now', lambda: '2026-09-05T00:00:00Z')
    monkeypatch.setattr(queueview, 'MAX_PEERS', 1)
    with Store.open(tmp_path / 'db') as store:
        first = store.capacity_enqueue(admission_id='z-first', resource_class='provider:xai', scope='xai')
        store.capacity_enqueue(admission_id='a-second', resource_class='provider:xai', scope='xai')
        row = queueview._admission(store, first, '2026-09-05T00:00:01Z', [], {})
        assert row['position'] == store.capacity_position('z-first') == 1
        assert row['holders_truncated'] is True


@pytest.mark.parametrize("failed_followup", [False, True])
@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_unbatched_followup_costs_retain_real_attempts(tmp_path, monkeypatch, capsys, failed_followup, surface):
    from tests.test_requests import _ready_repo
    from tests.test_pipeline import _fake_grok, _emit, CLEAN
    repo = _ready_repo(tmp_path, monkeypatch)
    config = repo / '.skodun.toml'
    config.write_text(config.read_text() + '\n[defaults]\ntimeout_retries=0\ndegraded_retries=0\n')
    body = ('if [ "$CALL" = "2" ]; then exit 7; fi\n' if failed_followup else '') + _emit(CLEAN)
    monkeypatch.setenv('SKODUN_GROK_BIN', str(_fake_grok(tmp_path, body)))
    monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
    db = tmp_path / 'costs.db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    if surface == 'cli':
        code = cli.main(['review', '--repo', str(repo), '--fresh', '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        spec = next(item for item in mcpserver.default_registry() if item.name == 'review')
        response = spec.handler(mcpserver.HandlerCall(params={'repo': str(repo), 'fresh': True},
            store_factory=lambda: Store.open(db), cancel=threading.Event()))
        code, result = response.status, response.metadata['result']
    assert code == (4 if failed_followup else 0)
    assert (tmp_path / 'bin/calls.log').read_text().splitlines() == ['invoked', 'invoked']
    assert result['counts']['complete'] is True
    assert result['counts']['provider_launches'] == 2
    assert len({row['attempt_id'] for row in result['attempts']}) == 2
    with Store.open(db) as store:
        record = store.get_review(result['ids']['review_id'])
        assert record['trustworthy'] is (not failed_followup)
        extra = record['extra_passes']['skeptic']['attempts']
        assert len(extra) == 1 and extra[0]['input_bytes'] > 0
        code, text = services.svc_queue(store, request_id=result['ids']['request_id'], output='json')
        assert code == 0
        costs = json.loads(text)['requests'][0]['costs']
        assert costs['counts_complete'] is True and costs['launched_calls'] == 2
        assert costs['aggregate_launched_prompt_bytes'] == sum(a['input_bytes'] for a in record['attempts'] + extra)
        assert services.svc_gate(store, repo)[0] == (2 if failed_followup else 0)


def test_unbatched_chain_exception_keeps_attempt_count_unknown(tmp_path, monkeypatch, capsys):
    from tests.test_requests import _ready_repo
    from skodun import pipeline
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
    monkeypatch.setenv('SKODUN_DB', str(tmp_path / 'costs.db'))
    real = pipeline._run_chain
    def fail_extra(*args, **kwargs):
        if (args[7] if len(args) > 7 else kwargs.get('tag')) == 'skeptic':
            raise RuntimeError('chain failed without a returned observation')
        return real(*args, **kwargs)
    monkeypatch.setattr(pipeline, '_run_chain', fail_extra)
    assert cli.main(['review', '--repo', str(repo), '--fresh', '--json']) == 4
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['complete'] is False
    assert result['counts']['provider_launches'] is None
    assert result['counts']['known_provider_launches'] == 1
    assert result['missing_attempt_scopes'][0]['id'] == 'skeptic'
