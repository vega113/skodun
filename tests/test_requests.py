"""Durable request identity through shipped review and admission paths."""

from skodun import services
from skodun.store import Store
from tests.test_gitio import _mkrepo, _git


def test_preflight_refusal_has_durable_request_before_any_review(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    monkeypatch.setenv('SKODUN_CONFIG', str(tmp_path / 'absent.toml'))
    monkeypatch.setenv('SKODUN_GROK_BIN', str(tmp_path / 'missing'))
    with Store.open(tmp_path / 's.db') as store:
        code, text, metadata = services.svc_review_detailed(store, repo)
        request = metadata['request']
        saved = store.get_request(request['id'])
        assert code == 2
        assert saved['state'] == 'finished'
        assert saved['identity']['worktree_root'] == str(repo.resolve())
        assert saved['identity']['head'] == _git(repo, 'rev-parse', 'HEAD')
        assert saved['links'] == []
        assert saved['result']['status'] == code


def test_request_is_persisted_before_the_attempt_and_context_is_restored(tmp_path, monkeypatch):
    from skodun import requests
    repo = _mkrepo(tmp_path)
    seen = []

    def attempt(store, root, **kwargs):
        ctx = requests.current()
        saved = store.get_request(ctx.id)
        seen.append(saved['state'])
        return 2, 'refused'

    monkeypatch.setattr(services, '_svc_review_once', attempt)
    with Store.open(tmp_path / 's.db') as store:
        code, text, meta = services.svc_review_detailed(store, repo)
        assert seen == ['accepted']
        assert code == 2
        assert requests.current() is None
        assert store.get_request(meta['request']['id'])['result']['text'] == text


def test_idempotent_result_replays_without_second_attempt(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    calls = []
    monkeypatch.setattr(services, '_svc_review_once',
                        lambda *a, **k: (calls.append(1) or 2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        first = services.svc_review_detailed(store, repo, request_key='call-1')
        second = services.svc_review_detailed(store, repo, request_key='call-1')
        assert len(calls) == 1
        assert first[2]['request']['id'] == second[2]['request']['id']
        assert second[2]['request']['replayed'] is True


def test_changed_worktree_rejects_idempotency_key_without_call(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    calls = []
    monkeypatch.setattr(services, '_svc_review_once',
                        lambda *a, **k: (calls.append(1) or 2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        services.svc_review_detailed(store, repo, request_key='same')
        (repo / 'new.txt').write_text('changed')
        code, text, metadata = services.svc_review_detailed(store, repo, request_key='same')
        assert code == 2
        assert metadata['request']['reason_code'] == 'request_identity_mismatch'
        assert len(calls) == 1


def test_live_duplicate_observes_existing_request_without_execution(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    db = tmp_path / 's.db'
    observed = []

    def attempt(store, root, **kwargs):
        with Store.open(db) as peer:
            code, _, meta = services.svc_review_detailed(peer, root, request_key='active')
            observed.append((code, meta['request']['reason_code']))
        return 2, 'refused'

    monkeypatch.setattr(services, '_svc_review_once', attempt)
    with Store.open(db) as store:
        services.svc_review_detailed(store, repo, request_key='active')
    assert observed == [(3, 'request_in_flight')]


def test_capacity_and_artifact_links_are_request_local(tmp_path, monkeypatch):
    from skodun import capacity, requests
    from tests.test_cli import _round
    repo = _mkrepo(tmp_path)

    def attempt(store, root, **kwargs):
        ticket = capacity.enqueue(store, scope=str(repo))
        rec = _round(batch_orchestration_id='batch-1', orchestration_id='recovery-1')
        store.save_review(rec)
        capacity.finish(store, ticket, status=capacity.STATUS_RELEASED)
        return 2, 'refused'

    monkeypatch.setattr(services, '_svc_review_once', attempt)
    with Store.open(tmp_path / 's.db') as store:
        _, _, meta = services.svc_review_detailed(store, repo)
        row = store.get_request(meta['request']['id'])
        kinds = {link['kind'] for link in row['links']}
        assert kinds == {'capacity', 'review', 'batch_orchestration', 'recovery_orchestration'}
        rec = store.get_review('sk_1')
        assert rec['request_id'] == row['id']


def test_keyboard_interrupt_finishes_request_without_swallowing(tmp_path, monkeypatch):
    import pytest
    repo = _mkrepo(tmp_path)

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(services, '_svc_review_once', interrupt)
    with Store.open(tmp_path / 's.db') as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_review_detailed(store, repo)
        row = store.list_requests(worktree_root=str(repo.resolve()))[0]
        assert row['state'] == 'cancelled'
        assert row['reason_code'] == 'interrupted'


def test_request_key_is_scoped_to_worktree(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    other = tmp_path / 'other'
    _git(repo, 'worktree', 'add', '-b', 'other', str(other))
    monkeypatch.setattr(services, '_svc_review_once', lambda *a, **k: (2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        first = services.svc_review_detailed(store, repo, request_key='same')
        second = services.svc_review_detailed(store, other, request_key='same')
        assert first[2]['request']['id'] != second[2]['request']['id']


def _ready_repo(tmp_path, monkeypatch):
    from tests.test_pipeline import _repo, _fake_grok, _emit, CLEAN
    repo = _repo(tmp_path)
    binary = _fake_grok(tmp_path, _emit(CLEAN))
    for key, value in {'SKODUN_CONFIG': str(tmp_path / 'absent.toml'),
                       'SKODUN_GROK_BIN': str(binary),
                       'SKODUN_SECURITY_PASS': '0', 'SKODUN_SKEPTIC_PASS': '0',
                       'SKODUN_ALLOW_MAIN': '1',
                       'SKODUN_LOCK_WAIT_SECONDS': '1'}.items():
        monkeypatch.setenv(key, value)
    return repo


def test_real_review_links_provider_capacity_and_final_artifact(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    with Store.open(tmp_path / 's.db') as store:
        status, _, meta = services.svc_review_detailed(store, repo)
        assert status == 0
        request = store.get_request(meta['request']['id'])
        links = request['links']
        assert len([l for l in links if l['kind'] == 'capacity']) == 2
        review_id = next(l['target_id'] for l in links if l['kind'] == 'review')
        rec = store.get_review(review_id)
        assert rec['request_id'] == request['id']
        assert rec['trustworthy'] is True
        assert rec['head'] == request['identity']['head']


def test_tree_changed_during_admission_refuses_before_provider(tmp_path, monkeypatch):
    from skodun import capacity
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    acquire = capacity.acquire_for_fg

    def change(*args, **kwargs):
        ticket = acquire(*args, **kwargs)
        (repo / 'a.txt').write_text('changed after enqueue\n')
        return ticket

    monkeypatch.setattr(capacity, 'acquire_for_fg', change)
    with Store.open(tmp_path / 's.db') as store:
        status, text, meta = services.svc_review_detailed(store, repo)
        assert status == 2
        assert 'identity changed while queued' in text
        assert _calls(tmp_path) == 0
        assert not [l for l in store.get_request(meta['request']['id'])['links']
                    if l['kind'] == 'review']
        assert not store.capacity_active_views('review-fg', str(repo / '.git'))


def test_terminal_interruption_does_not_report_live_duplicate(tmp_path, monkeypatch):
    import pytest
    repo = _mkrepo(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(services, '_svc_review_once', interrupt)
    with Store.open(tmp_path / 's.db') as store:
        with pytest.raises(KeyboardInterrupt):
            services.svc_review_detailed(store, repo, request_key='cancelled')
        status, _, meta = services.svc_review_detailed(store, repo, request_key='cancelled')
        assert status == 4
        assert meta['request']['reason_code'] == 'request_incomplete'


def test_request_status_is_readable_without_exposing_ownership(tmp_path, monkeypatch):
    import json
    repo = _mkrepo(tmp_path)
    monkeypatch.setattr(services, '_svc_review_once', lambda *a, **k: (2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        _, _, meta = services.svc_review_detailed(store, repo)
        status, text = services.svc_review_status(store, meta['request']['id'], output='json')
        request = json.loads(text)['request']
        assert status == 0 and request['id'] == meta['request']['id']
        assert request['state'] == 'finished'
        assert 'owner_token' not in text and 'intent_digest' not in text


def test_mcp_normal_review_retains_request_metadata(tmp_path, monkeypatch):
    import threading
    from tests.test_mcptools import _specs
    from skodun.mcpserver import HandlerCall
    repo = _mkrepo(tmp_path)
    monkeypatch.setenv('SKODUN_CONFIG', str(tmp_path / 'absent.toml'))
    monkeypatch.setenv('SKODUN_GROK_BIN', str(tmp_path / 'missing'))
    db = tmp_path / 's.db'
    result = _specs()['review'].handler(HandlerCall(
        params={'repo': str(repo), 'request_key': 'mcp-call'},
        store_factory=lambda: Store.open(db), cancel=threading.Event()))
    request = result.metadata['request']
    with Store.open(db) as store:
        assert store.get_request(request['id'])['source'] == 'mcp'


def test_request_schema_upgrade_is_explicit_and_preserves_reviews(tmp_path):
    import sqlite3
    from contextlib import closing
    import pytest
    from skodun.store import inspect_schema, SchemaLifecycleError, SCHEMA_VERSION
    from tests.test_cli import _round
    db = tmp_path / 'authority.db'
    with Store.open(db) as store:
        store.save_review(_round())
    with closing(sqlite3.connect(db)) as c:
        c.execute('DROP TABLE request_links')
        c.execute('DROP TABLE review_requests')
        c.execute('PRAGMA user_version=16')
        c.commit()
    before = db.read_bytes()
    assert inspect_schema(db).state == 'older'
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    assert db.read_bytes() == before
    receipt = Store.migrate_existing(db, build_commit='a' * 40)
    assert receipt['schema_from'] == 16 and receipt['schema_to'] == SCHEMA_VERSION
    with Store.open(db) as store:
        assert store.get_review('sk_1')['trustworthy'] is True
        assert store.list_requests() == []


def test_separate_thread_request_claim_cannot_launch_duplicate(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    repo = _mkrepo(tmp_path)
    db = tmp_path / 's.db'
    with Store.open(db):
        pass
    started, release = threading.Event(), threading.Event()
    calls = []

    def attempt(*a, **k):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return 2, 'refused'

    def request():
        with Store.open(db) as store:
            return services.svc_review_detailed(store, repo, request_key='racing')

    monkeypatch.setattr(services, '_svc_review_once', attempt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(request)
        try:
            assert started.wait(5)
            second = request()
            assert second[0] == 3
        finally:
            release.set()
        assert first.result()[2]['request']['id'] == second[2]['request']['id']
    assert calls == [1]


def test_result_retention_keeps_idempotency_and_review_links(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)
    calls = []
    monkeypatch.setattr(services, '_svc_review_once',
                        lambda *a, **k: (calls.append(1) or 2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        _, _, meta = services.svc_review_detailed(store, repo, request_key='old')
        rid = meta['request']['id']
        store.link_request(rid, 'review', 'historical')
        store._c.execute("UPDATE review_requests SET updated_at='2020-01-01T00:00:00Z'")
        assert store.prune_request_results(before='2021-01-01T00:00:00Z', dry_run=True) == 1
        assert store.get_request(rid)['result'] is not None
        assert store.prune_request_results(before='2021-01-01T00:00:00Z') == 1
        row = store.get_request(rid)
        assert row['result'] is None and row['reason_code'] == 'request_result_expired'
        assert row['links'] == [{'kind': 'review', 'target_id': 'historical'}]
        status, _, _ = services.svc_review_detailed(store, repo, request_key='old')
        assert status == 4 and len(calls) == 1


def test_inflight_plain_service_retry_exposes_existing_id(tmp_path, monkeypatch, capsys):
    repo = _mkrepo(tmp_path)
    db = tmp_path / 's.db'
    observed = []

    def attempt(store, root, **kwargs):
        from skodun.requests import current
        rid = current().id
        capsys.readouterr()
        with Store.open(db) as peer:
            status, text = services.svc_review(peer, root, request_key='active')
        captured = capsys.readouterr()
        observed.append((status, rid in text + captured.out + captured.err))
        return 2, 'refused'

    monkeypatch.setattr(services, '_svc_review_once', attempt)
    with Store.open(db) as store:
        services.svc_review_detailed(store, repo, request_key='active')
    assert observed == [(3, True)]


def test_remote_changed_during_admission_refuses_before_provider(tmp_path, monkeypatch):
    from skodun import capacity
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    _git(repo, 'remote', 'add', 'origin', 'https://github.com/example/a.git')
    acquire = capacity.acquire_for_fg

    def change(*args, **kwargs):
        ticket = acquire(*args, **kwargs)
        _git(repo, 'remote', 'set-url', 'origin', 'https://github.com/example/b.git')
        return ticket

    monkeypatch.setattr(capacity, 'acquire_for_fg', change)
    with Store.open(tmp_path / 's.db') as store:
        status, text, _ = services.svc_review_detailed(store, repo)
        assert status == 2 and 'canonical_repository' in text
        assert _calls(tmp_path) == 0


def test_compatible_checkpoint_continuation_preserves_request(tmp_path, monkeypatch):
    from skodun import pipeline
    from tests.test_batched_review import _clean_checkpoint_sub
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    for index in range(4):
        (repo / f'large{index}.txt').write_text('line\n' * 4000)
    cancel = threading.Event()
    calls = []

    def first(*args, **kwargs):
        label = args[8] if len(args) > 8 else kwargs['label']
        calls.append(label)
        cancel.set()
        return _clean_checkpoint_sub(label)

    monkeypatch.setattr(pipeline, '_run_sub', first)
    with Store.open(tmp_path / 's.db') as store:
        status, _, first_meta = services.svc_review_detailed(
            store, repo, cancel=cancel, batch_target_bytes=10000)
        assert status == 4 and len(calls) == 1
        monkeypatch.setattr(pipeline, '_run_sub', lambda *a, **k:
                            _clean_checkpoint_sub(a[8] if len(a) > 8 else k['label']))
        status, _, second_meta = services.svc_review_detailed(
            store, repo, batch_target_bytes=10000)
        assert status == 0
        assert first_meta['request']['id'] == second_meta['request']['id']
        assert second_meta['request']['continued'] is True
        executions = store.get_request(second_meta['request']['id'])['executions']
        assert [e['status'] for e in executions] == [0, 4]


def test_execution_uses_the_captured_configuration(tmp_path, monkeypatch):
    from skodun import config
    from dataclasses import replace
    repo = _ready_repo(tmp_path, monkeypatch)
    load = config.load_config
    loaded = []

    def changing(root):
        cfg = load(root)
        loaded.append(cfg)
        if len(loaded) > 1:
            return replace(cfg, reviewers=tuple(replace(r, model='different-model')
                                                for r in cfg.reviewers))
        return cfg

    monkeypatch.setattr(config, 'load_config', changing)
    with Store.open(tmp_path / 's.db') as store:
        status, _, meta = services.svc_review_detailed(store, repo)
        assert status == 0
        rid = next(l['target_id'] for l in store.get_request(meta['request']['id'])['links']
                   if l['kind'] == 'review')
        assert store.get_review(rid)['model'] == loaded[0].reviewers[0].model
        assert len(loaded) == 1


def test_policy_changed_while_queued_is_refused(tmp_path, monkeypatch):
    from skodun import capacity
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    acquire = capacity.acquire_for_fg

    def change(*a, **k):
        ticket = acquire(*a, **k)
        monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
        return ticket

    monkeypatch.setattr(capacity, 'acquire_for_fg', change)
    with Store.open(tmp_path / 's.db') as store:
        status, text, _ = services.svc_review_detailed(store, repo)
        assert status == 2 and 'policy_hash' in text
        assert _calls(tmp_path) == 0


def test_keyed_unreadable_repository_still_has_durable_failure(tmp_path):
    with Store.open(tmp_path / 's.db') as store:
        status, _, metadata = services.svc_review_detailed(
            store, tmp_path / 'absent', request_key='invalid-repo')
        assert status == 2
        row = store.get_request(metadata['request']['id'])
        assert row['identity']['capture_error'] == 'GitError'
        assert row['result']['status'] == 2


def test_dispatch_reservation_is_its_request_without_competing_lease(tmp_path):
    from tests.test_store import _reserve
    with Store.open(tmp_path / 's.db') as store:
        reservation = _reserve(store)
        rec = store.get_review(reservation.record_id)
        assert rec['request_id'] == reservation.record_id
        assert rec['status'] == 'running' and rec['trustworthy'] is False
        assert store.list_requests() == []


def test_continuation_claim_rechecks_target_before_reactivating(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from skodun.requests import now
    import os
    repo = _mkrepo(tmp_path)
    monkeypatch.setattr(services, '_svc_review_once', lambda *a, **k: (2, 'refused'))
    with Store.open(tmp_path / 's.db') as store:
        _, _, meta = services.svc_review_detailed(store, repo)
        prior = store.get_request(meta['request']['id'])
        decision, row = store.begin_request(
            request_id='unused', scope=prior['scope'], request_key=None,
            identity=prior['identity'], intent={}, owner_token='new-owner',
            pid=os.getpid(), source='test', now=now(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            continuation_id=prior['id'], continuation_orchestration_id='no-longer-resumable')
        assert decision == 'continuation_unavailable'
        assert row['owner_token'] == prior['owner_token']
        assert len(row['executions']) == 1
