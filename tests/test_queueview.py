"""Shipped request/queue inspection with hermetic stores and no providers."""
import json
import threading

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
    skip = {'n': 2, 'provider': 'openai', 'skipped': 'prompt_too_large', 'input_bytes': 9_000_000}
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
