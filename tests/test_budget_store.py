"""Real Store budget writes, execution fences and additive migration checks."""
import json

import pytest

from skodun.store import Store

START = '2026-09-05T12:00:00Z'
NOW = '2026-09-05T12:00:10Z'


def begin(store):
    _, row = store.begin_request(request_id='sk_req_one', scope='/work', request_key=None,
        identity={'worktree_root': '/work'}, intent={}, owner_token='private-owner', pid=123,
        source='cli', now=START, expires_at='2026-09-06T12:00:00Z')
    return row['id'], row['executions'][0]['seq']


def snapshot(rid, seq, **changes):
    return {'scope': 'request_execution', 'request_id': rid, 'execution_seq': seq,
        'phase': 'review', 'limits': {'max_queue_seconds': 30, 'max_review_seconds': 60,
            'max_provider_wait_seconds': 20, 'max_wall_seconds': 120},
        'deadlines': {'queue': None, 'review': '2026-09-05T12:01:00Z',
                      'total': '2026-09-05T12:02:00Z', 'provider_wait': None},
        'timing': {'queue_wait_ms': 1000, 'provider_wait_ms': 2000,
                   'review_wall_ms': 9000, 'review_active_ms': 8000, 'total_ms': 10000},
        'review_paused_for_queue': False, 'reason_code': None, 'updated_at': NOW, **changes}


def layer(store, rid, seq, *, owner='private-owner', aid='admission', **changes):
    return store.record_request_capacity(rid, seq, owner, admission_id=aid,
        resource_class='review-fg', scope='/repo', effective_capacity=1,
        configured_capacity=4, legacy_dual_hold=True, updated_at=NOW, **changes)


def test_budget_snapshot_and_exact_admission_layer_are_public_without_owner_token(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        assert store.request_budget(rid) is None
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        store.capacity_enqueue(admission_id='admission', resource_class='review-fg', scope='/repo')
        store.link_request(rid, 'capacity', 'admission')
        assert layer(store, rid, seq)
        result = store.request_budget(rid)
        assert result['timing']['review_active_ms'] == 8000
        assert result['capacity_layers'][0]['admission_id'] == 'admission'
        assert result['capacity_layers'][0]['execution_seq'] == seq
        assert result['capacity_layers'][0]['effective_capacity'] == 1
        assert result['capacity_layers'][0]['configured_capacity'] == 4
        assert result['capacity_layers'][0]['legacy_dual_hold'] is True
        assert 'owner_token' not in json.dumps(result)
        assert 'private-owner' not in json.dumps(result)


def test_stale_owner_or_execution_never_updates_current_budget(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        assert not store.save_request_budget(rid, seq, 'wrong-owner', snapshot(rid, seq))
        assert not store.save_request_budget(rid, seq + 1, 'private-owner', snapshot(rid, seq + 1))
        assert store.request_budget(rid) is None


@pytest.mark.parametrize('mutation', [
    lambda s: s['limits'].update(max_review_seconds=float('inf')),
    lambda s: s['timing'].update(review_active_ms=-1),
    lambda s: s['timing'].update(total_ms=True),
    lambda s: s.update(updated_at='2026-9-5T12:00:00Z'),
    lambda s: s['deadlines'].update(review='tomorrow'),
    lambda s: s.update(owner_token='must not persist'),
])
def test_budget_invalid_data_is_refused_without_a_row(tmp_path, mutation):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        bad = snapshot(rid, seq)
        mutation(bad)
        with pytest.raises(ValueError):
            store.save_request_budget(rid, seq, 'private-owner', bad)
        assert store.request_budget(rid) is None


def resume(store, rid, old_seq):
    from tests.test_checkpoints import _created
    _created(store, orchestration_id='resume-orchestration')
    store.finish_request(rid, owner_token='private-owner', state='failed', reason_code='interrupted',
                         result=None, now=NOW)
    decision, row = store.begin_request(request_id='unused-new-id', scope='/work', request_key=None,
        identity={'worktree_root': '/work'}, intent={}, owner_token='new-owner', pid=456,
        source='cli', now='2026-09-05T12:00:20Z', expires_at='2026-09-06T12:00:00Z',
        continuation_id=rid, continuation_orchestration_id='resume-orchestration')
    assert decision == 'continued'
    return row['executions'][0]['seq']


def test_current_getter_does_not_fall_back_to_old_execution_and_history_keeps_old_caps(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, old_seq = begin(store)
        store.save_request_budget(rid, old_seq, 'private-owner', snapshot(rid, old_seq))
        store.capacity_enqueue(admission_id='admission', resource_class='review-fg', scope='/repo')
        store.link_request(rid, 'capacity', 'admission')
        assert layer(store, rid, old_seq)
        new_seq = resume(store, rid, old_seq)
        assert new_seq != old_seq
        assert store.request_budget(rid) is None
        assert not store.save_request_budget(rid, old_seq, 'private-owner', snapshot(rid, old_seq))
        assert not layer(store, rid, new_seq, owner='new-owner')
        assert store.save_request_budget(rid, new_seq, 'new-owner', snapshot(
            rid, new_seq, phase='queued', updated_at='2026-09-05T12:00:20Z',
            review_paused_for_queue=True, deadlines={'review': None}))
        current = store.request_budget(rid)
        assert current['execution_seq'] == new_seq
        assert current['capacity_layers'] == []
        assert current['deadlines']['review'] is None
        assert current['review_paused_for_queue'] is True
        history = store.request_budgets(rid)
        assert [row['execution_seq'] for row in history['budgets']] == [new_seq, old_seq]
        assert history['budgets'][1]['capacity_layers'][0]['configured_capacity'] == 4
        assert history['truncated'] is False
        assert store.request_budgets(rid, limit=1)['truncated'] is True


def test_capacity_requires_live_link_and_matching_resource_scope(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        store.capacity_enqueue(admission_id='admission', resource_class='review-fg', scope='/repo')
        assert not layer(store, rid, seq)
        store.link_request(rid, 'capacity', 'admission')
        assert not store.record_request_capacity(rid, seq, 'private-owner', admission_id='admission',
            resource_class='provider:openai', scope='/repo', effective_capacity=1, updated_at=NOW)
        store.capacity_finish('admission', status='released')
        assert not layer(store, rid, seq)
        assert store.request_budget(rid) is None


def test_layers_can_arrive_before_snapshot_and_updates_refuse_old_timestamps(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        store.capacity_enqueue(admission_id='admission', resource_class='review-fg', scope='/repo')
        store.link_request(rid, 'capacity', 'admission')
        assert layer(store, rid, seq)
        assert store.request_budget(rid) is None
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        assert not store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq, updated_at=START))
        assert not store.record_request_capacity(rid, seq, 'private-owner', admission_id='admission',
            resource_class='review-fg', scope='/repo', effective_capacity=2, updated_at=START)
        assert store.request_budget(rid)['capacity_layers'][0]['effective_capacity'] == 1


@pytest.mark.parametrize('kwargs', [{'effective_capacity': True}, {'effective_capacity': -1},
    {'configured_capacity': float('nan')}, {'legacy_dual_hold': 1}, {'updated_at': 'bad'}])
def test_capacity_data_validation_is_strict(tmp_path, kwargs):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        values = dict(admission_id='a', resource_class='review-fg', scope='/repo',
                      effective_capacity=1, updated_at=NOW, **{})
        values.update(kwargs)
        with pytest.raises(ValueError):
            store.record_request_capacity(rid, seq, 'private-owner', **values)


def test_budget_write_preserves_an_outer_transaction(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        store._c.execute('BEGIN IMMEDIATE')
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        assert store._c.in_transaction
        store._c.execute('ROLLBACK')
        assert store.request_budget(rid) is None


@pytest.mark.parametrize('corruption', ['json', 'duplicate', 'wrong_execution', 'missing_scope', 'layer_cap'])
def test_malformed_persisted_data_is_an_explicit_error(tmp_path, corruption):
    from skodun.budget_store import BudgetDataError
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        if corruption == 'json':
            store._c.execute("UPDATE request_budget_snapshots SET snapshot_json='invalid'")
        elif corruption == 'duplicate':
            encoded = json.dumps(snapshot(rid, seq)).replace('"phase": "review"', '"phase": "queued", "phase": "review"')
            store._c.execute('UPDATE request_budget_snapshots SET snapshot_json=?', (encoded,))
        elif corruption == 'missing_scope':
            bad = snapshot(rid, seq)
            bad.pop('scope')
            store._c.execute('UPDATE request_budget_snapshots SET snapshot_json=?', (json.dumps(bad),))
        elif corruption == 'wrong_execution':
            bad = snapshot(rid, seq + 1)
            store._c.execute('UPDATE request_budget_snapshots SET snapshot_json=?', (json.dumps(bad),))
        else:
            store.capacity_enqueue(admission_id='admission', resource_class='review-fg', scope='/repo')
            store.link_request(rid, 'capacity', 'admission')
            assert layer(store, rid, seq)
            store._c.execute('UPDATE request_capacity_layers SET effective_capacity=-1')
        with pytest.raises(BudgetDataError, match='malformed persisted'):
            store.request_budget(rid)


def test_v19_upgrade_is_explicit_additive_and_preserves_requests(tmp_path):
    from unittest.mock import patch
    from skodun import store as store_mod
    from skodun.budget_store import MIGRATION
    from tests.test_store import V19_OBJECTS, _objects
    db = tmp_path / 's.db'
    with patch.object(store_mod, 'SCHEMA_VERSION', 18), patch.object(
            store_mod, '_MIGRATIONS', tuple((target, delta) for target, delta in store_mod._MIGRATIONS if target <= 18)):
        with Store.open(db) as store:
            rid, seq = begin(store)
            store.finish_request(rid, owner_token='private-owner', state='finished',
                                 reason_code='fixture_complete', result=None, now=NOW)
    before_objects = _objects(db)
    before_bytes = db.read_bytes()
    with pytest.raises(store_mod.SchemaLifecycleError):
        Store.open(db)
    assert db.read_bytes() == before_bytes
    receipt = Store.migrate_existing(db, build_commit='a' * 40)
    assert receipt['schema_from'] == 18
    assert receipt['schema_to'] == 19
    assert _objects(db) - before_objects == V19_OBJECTS
    with Store.open(db) as store:
        assert store.get_request(rid)['executions'][0]['seq'] == seq
        assert store.request_budget(rid) is None
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        for _ in range(2):
            for sql in MIGRATION:
                store._c.execute(sql)
        assert store.request_budget(rid)['phase'] == 'review'


def test_layer_and_snapshot_bounds_are_explicit(tmp_path, monkeypatch):
    from skodun import budget_store
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        for index in range(2):
            aid = f'admission-{index}'
            store.capacity_enqueue(admission_id=aid, resource_class='review-fg', scope='/repo')
            store.link_request(rid, 'capacity', aid)
            assert layer(store, rid, seq, aid=aid)
        assert store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))
        monkeypatch.setattr(budget_store, 'MAX_LAYERS', 1)
        result = store.request_budget(rid)
        assert len(result['capacity_layers']) == 1
        assert result['capacity_layers_truncated'] is True
        with pytest.raises(ValueError):
            store.request_budgets(rid, limit=True)
        with pytest.raises(ValueError):
            store.request_budgets(rid, limit=101)
        monkeypatch.setattr(budget_store, 'MAX_SNAPSHOT_BYTES', 10)
        with pytest.raises(ValueError, match='exceeds'):
            store.save_request_budget(rid, seq, 'private-owner', snapshot(rid, seq))


def test_huge_or_boolean_numbers_are_rejected_before_sql_binding(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        rid, seq = begin(store)
        with pytest.raises(ValueError):
            store.save_request_budget(rid, True, 'private-owner', snapshot(rid, seq))
        bad = snapshot(rid, seq)
        bad['timing']['total_ms'] = 10**1000
        with pytest.raises(ValueError):
            store.save_request_budget(rid, seq, 'private-owner', bad)
        with pytest.raises(ValueError):
            store.record_request_capacity(rid, seq, 'private-owner', admission_id='a',
                resource_class='review-fg', scope='/repo', effective_capacity=2**100, updated_at=NOW)


@pytest.mark.parametrize('backup_refusal', [False, True])
def test_explicit_migration_closes_every_connection(tmp_path, monkeypatch, backup_refusal):
    from unittest.mock import patch
    from skodun import store as store_mod
    import sqlite3
    db = tmp_path / 'authority.db'
    with patch.object(store_mod, 'SCHEMA_VERSION', 18), patch.object(
            store_mod, '_MIGRATIONS', tuple((target, delta) for target, delta in store_mod._MIGRATIONS if target <= 18)):
        with Store.open(db):
            pass
    opened = []
    connect = sqlite3.connect
    def track(path, *args, **kwargs):
        if backup_refusal and 'backup-before' in str(path):
            raise sqlite3.OperationalError('synthetic target open refusal')
        connection = connect(path, *args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', track)
    if backup_refusal:
        with pytest.raises(sqlite3.OperationalError):
            Store.migrate_existing(db, build_commit='b' * 40)
    else:
        receipt = Store.migrate_existing(db, build_commit='b' * 40)
        assert receipt['result'] == 'success'
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
