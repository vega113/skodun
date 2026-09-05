"""Request budgets through the shared service, FIFO and provider chain.

Only external providers and time progression are substituted. Requests, budget
persistence, admission tickets, coverage finalization and surface parsers run.
"""
import json

import pytest

from skodun import budgets, capacity, runner, services
from skodun.store import Store
from tests.test_budgets import Clock
from tests.test_requests import _ready_repo


def controlled_clock(monkeypatch):
    clock = Clock()
    original = budgets.ReviewBudget

    def create(limits, **kwargs):
        return original(limits, clock=clock, **kwargs)

    monkeypatch.setattr(budgets, 'ReviewBudget', create)
    return clock


def clean_provider(clock, calls, advance=.5):
    def run(cmd, timeout, cwd, out, err, *, stdin_path=None, cancel=None):
        calls.append(cmd)
        clock.value += advance
        if cancel.is_set():
            raise runner.ReviewCancelled('test provider observed expiry')
        out.write_bytes(b'{"structuredOutput":{"summary":"s","findings":[]},"stopReason":"EndTurn"}')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=advance, first_output_sec=.1)
    return run


def test_real_fg_queue_longer_than_review_budget_keeps_complete_allowance(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_LEGACY_FG_LOCK', '0')
    monkeypatch.setenv('SKODUN_ADMISSION_WAIT_SECONDS', '30')
    clock = controlled_clock(monkeypatch)
    calls, progress = [], []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    acquire = capacity.acquire_for_fg
    with Store.open(tmp_path / 'db') as store:
        holder = capacity.acquire_for_fg(store, scope=str(repo / '.git'), capacity=1,
                                         wait_sec=0, poll_sec=.01)

        def release_after_wait(seconds):
            # A reconnect observes this live request and the same FIFO ticket.
            with Store.open(tmp_path / 'db') as peer:
                duplicate = services.svc_review_detailed(peer, repo, request_key='queued-one',
                    max_queue_seconds=20, max_review_seconds=2, max_provider_wait_seconds=3,
                    progress_sink=lambda text: None)
                assert duplicate[0] == 3
                assert duplicate[2]['request']['reason_code'] == 'request_in_flight'
                row = peer.get_request(duplicate[2]['request']['id'])
                assert len([link for link in row['links'] if link['kind'] == 'capacity']) == 1
                assert peer.request_budget(row['id'])['phase'] == 'queue'
            clock.value += 10
            capacity.finish(store, holder, status=capacity.STATUS_RELEASED)

        def acquire_with_clock(*args, **kwargs):
            return acquire(*args, **kwargs, clock=clock, sleep=release_after_wait)

        monkeypatch.setattr(capacity, 'acquire_for_fg', acquire_with_clock)
        code, text, meta = services.svc_review_detailed(store, repo, request_key='queued-one', max_queue_seconds=20,
            max_review_seconds=2, max_provider_wait_seconds=3, progress_sink=progress.append)
        assert code == 0 and len(calls) == 1, text
        snapshot = store.request_budget(meta['request']['id'])
        assert snapshot['timing']['queue_wait_ms'] == 10000
        assert snapshot['timing']['review_active_ms'] == 500
        assert snapshot['timing']['review_wall_ms'] == 500
        assert snapshot['limits']['max_review_seconds'] == 2
        assert {layer['resource_class'] for layer in snapshot['capacity_layers']} == {'review-fg', 'provider:xai'}
        assert all(layer['execution_seq'] == meta['request']['execution_seq']
                   for layer in snapshot['capacity_layers'])
        assert snapshot['phase'] == 'finished'
        assert len([line for line in progress if 'queue position' in line]) == 1
        assert meta['result']['timing']['queue_wait_ms'] == 10000


@pytest.mark.parametrize('limit,code', [('max_queue_seconds', 'queue_budget_exhausted'),
                                      ('max_wall_seconds', 'total_budget_exhausted')])
def test_expired_queue_never_launches_or_reenqueues(tmp_path, monkeypatch, limit, code):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_LEGACY_FG_LOCK', '0')
    monkeypatch.setenv('SKODUN_ADMISSION_WAIT_SECONDS', '30')
    clock = controlled_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    acquire = capacity.acquire_for_fg
    with Store.open(tmp_path / 'db') as store:
        holder = capacity.acquire_for_fg(store, scope=str(repo / '.git'), capacity=1, wait_sec=0, poll_sec=.01)

        def advance(seconds):
            clock.value += 2

        monkeypatch.setattr(capacity, 'acquire_for_fg',
            lambda *args, **kwargs: acquire(*args, **kwargs, clock=clock, sleep=advance))
        status, _, meta = services.svc_review_detailed(store, repo, request_key='same', **{limit:1})
        request = store.get_request(meta['request']['id'])
        assert status not in (0, 1) and not calls
        assert request['state'] == 'expired'
        assert request['reason_code'] == code
        assert meta['result']['execution']['reason_code'] == code
        assert meta['result']['execution']['state'] == 'expired'
        tickets = [link for link in request['links'] if link['kind'] == 'capacity']
        assert len(tickets) == 1
        replay = services.svc_review_detailed(store, repo, request_key='same', **{limit:1})
        assert replay[2]['request']['replayed'] is True
        assert store.get_request(request['id'])['links'] == request['links']
        assert len(store.capacity_active_views('review-fg', str(repo / '.git'))) == 1
        capacity.finish(store, holder, status=capacity.STATUS_RELEASED)


def test_review_expiry_demotes_actual_artifact_and_releases_both_slots(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    clock = controlled_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls, advance=2))
    with Store.open(tmp_path / 'db') as store:
        status, _, meta = services.svc_review_detailed(store, repo, max_review_seconds=1)
        assert status == 4 and len(calls) == 1
        assert meta['result']['execution']['reason_code'] == 'review_budget_exhausted'
        assert meta['result']['execution']['state'] == 'expired'
        rec = store.get_review(meta['result']['ids']['review_id'])
        assert rec['trustworthy'] is False and rec['status'] == 'failed'
        assert not store.capacity_active_views('review-fg', str(repo / '.git'))
        assert not store.capacity_active_views('provider:xai', 'xai')


def test_free_admission_has_no_queue_progress_and_key_binds_budget(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    clock = controlled_clock(monkeypatch)
    calls, progress = [], []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    with Store.open(tmp_path / 'db') as store:
        first = services.svc_review_detailed(store, repo, request_key='same',
                     max_review_seconds=3, progress_sink=progress.append)
        refused = services.svc_review_detailed(store, repo, request_key='same',
                     max_review_seconds=4, progress_sink=progress.append)
        assert first[0] == 0 and len(calls) == 1
        assert refused[2]['request']['reason_code'] == 'request_identity_mismatch'
        assert not [line for line in progress if 'queue position' in line]
        snapshot = store.request_budget(first[2]['request']['id'])
        fg = next(layer for layer in snapshot['capacity_layers'] if layer['resource_class'] == 'review-fg')
        assert fg['legacy_dual_hold'] is True and fg['effective_capacity'] == 1


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
def test_surface_budget_options_persist_without_recovery(tmp_path, monkeypatch, capsys, surface):
    from skodun.cli import main
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    limits = dict(max_queue_seconds=10, max_review_seconds=20,
                  max_provider_wait_seconds=0, max_wall_seconds=30)
    if surface == 'cli':
        args = ['review', '--repo', str(repo), '--json']
        for key, value in limits.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        assert main(args) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo':str(repo), **limits}, store_factory=lambda:Store.open(db),
            cancel=threading.Event()))
        assert response.status == 0
        result = response.metadata['result']
    with Store.open(db) as store:
        snapshot = store.request_budget(result['ids']['request_id'])
        assert snapshot['limits'] == limits
        assert snapshot['phase'] == 'finished'


@pytest.mark.parametrize('batched', [False, True])
def test_every_pass_receives_same_provider_wait_policy(tmp_path, monkeypatch, batched):
    from skodun import chain
    from tests.test_batched_review import _body
    repo = _ready_repo(tmp_path, monkeypatch)
    if batched:
        config_path = repo / '.skodun.toml'
        config_path.write_text(config_path.read_text() + '[defaults]\nmax_diff_bytes=4000\ncontext_pack=false\n')
        for i in range(4):
            (repo / f'f{i}.txt').write_text(_body(f'f{i}'))
    else:
        monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
    clock = controlled_clock(monkeypatch)
    calls, waits = [], []
    acquire = chain._acquire_provider_slot

    def timed_admission(*args, **kwargs):
        waits.append(kwargs['wait_sec'])
        ticket = acquire(*args, **kwargs)
        clock.value += 2
        return ticket

    monkeypatch.setattr(chain, '_acquire_provider_slot', timed_admission)
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls, advance=5))
    with Store.open(tmp_path / 'db') as store:
        status, text, meta = services.svc_review_detailed(store, repo,
            max_provider_wait_seconds=7, max_review_seconds=100)
        assert status == 0, text
        assert len(calls) > 1 and len(waits) == len(calls)
        assert waits == [7] * len(calls)
        rec = store.get_review(meta['result']['ids']['review_id'])
        if batched:
            assert rec['batched'] is True
            assert len(calls) == rec['batch_count'] + 1
        else:
            assert rec['extra_passes']['skeptic']['ran'] is True
        snapshot = store.request_budget(meta['request']['id'])
        assert snapshot['timing']['provider_wait_ms'] == len(calls) * 2000
        assert snapshot['timing']['review_active_ms'] == (len(calls) * 7 - 2) * 1000


def test_capable_fallback_keeps_wait_allowance_after_primary_runtime_and_size_skip(tmp_path, monkeypatch):
    from skodun import chain, pipeline
    from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
    from skodun.config import Config, Defaults, Reviewer
    from tests.test_cli import _round
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_AGY_BIN', '/bin/sh')
    clock = controlled_clock(monkeypatch)
    primary = Reviewer(name='primary', provider='xai', model='one', role='finder',
                       fallbacks=('small', 'capable'))
    small = Reviewer(name='small', provider='google', model='two', role='finder')
    capable = Reviewer(name='capable', provider='xai', model='three', role='finder')
    cfg = Config(defaults=Defaults(timeout_retries=0), reviewers=(primary, small, capable))
    prompt = b'x' * (MAX_PROMPT_ARG_BYTES + 1)
    calls, waits = [], []
    acquire = chain._acquire_provider_slot

    def timed_admission(*args, **kwargs):
        waits.append((args[1], kwargs['wait_sec']))
        ticket = acquire(*args, **kwargs)
        clock.value += 2
        return ticket

    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(cmd)
        if len(calls) == 1:
            clock.value += 100
            return runner.RunResult(rc=124, timed_out=True, duration_sec=100, first_output_sec=None)
        out.write_bytes(b'{"structuredOutput":{"summary":"s","findings":[]},"stopReason":"EndTurn"}')
        clock.value += 1
        return runner.RunResult(rc=0, timed_out=False, duration_sec=1, first_output_sec=.1)

    def review(root, ignored_cfg, store, **kwargs):
        outcome = chain.run_chain(primary, cfg, cfg.defaults, prompt, root, store, tmp_path,
                                  'budget-mixed', cancel=kwargs['cancel'])
        rec = _round(id='mixed', attempts=outcome.attempts, trustworthy=outcome.parsed is not None)
        store.save_review(rec)
        return rec

    monkeypatch.setattr(chain, '_acquire_provider_slot', timed_admission)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    monkeypatch.setattr(pipeline, 'run_review', review)
    with Store.open(tmp_path / 'db') as store:
        # A busy google slot must not delay the deterministically oversized hop.
        blocked = capacity.acquire(store, scope='google', resource_class='provider:google', capacity=1, wait_sec=0)
        status, text, meta = services.svc_review_detailed(store, repo,
            max_provider_wait_seconds=7, max_review_seconds=200)
        assert status == 0, text
        assert waits == [('xai', 7), ('xai', 5)]
        assert len(calls) == 2
        attempts = meta['result']['attempts']
        assert [a['launched'] for a in attempts] == [True, False, True]
        assert attempts[1]['reason_code'] == 'transport_ineligible'
        snapshot = store.request_budget(meta['request']['id'])
        assert snapshot['timing']['provider_wait_ms'] == 4000
        assert snapshot['timing']['review_active_ms'] == 103000
        assert all(layer['resource_class'] != 'provider:google' for layer in snapshot['capacity_layers'])
        capacity.finish(store, blocked, status=capacity.STATUS_RELEASED)


def test_provider_wait_exhaustion_is_a_distinct_no_launch_result(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    clock = controlled_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    with Store.open(tmp_path / 'db') as store:
        holder = capacity.acquire(store, scope='xai', resource_class='provider:xai', capacity=1, wait_sec=0)
        status, _, meta = services.svc_review_detailed(store, repo, max_provider_wait_seconds=0)
        assert status == 4 and not calls
        assert meta['result']['execution']['reason_code'] == 'provider_wait_exhausted'
        assert meta['result']['execution']['state'] == 'expired'
        assert meta['result']['attempts'][0]['launched'] is False
        assert meta['result']['coverage']['trustworthy'] is False
        capacity.finish(store, holder, status=capacity.STATUS_RELEASED)


def test_extended_provider_wait_is_in_persisted_record_and_lock_ceiling(tmp_path, monkeypatch):
    from skodun import budget, pipeline
    from skodun.config import load_config
    repo = _ready_repo(tmp_path, monkeypatch)
    clock = controlled_clock(monkeypatch)
    calls, ceilings = [], []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    acquire = pipeline._acquire_fg_lock
    def lock(*args, **kwargs):
        ceilings.append(kwargs['budget_sec'])
        return acquire(*args, **kwargs)
    monkeypatch.setattr(pipeline, '_acquire_fg_lock', lock)
    with Store.open(tmp_path / 'db') as store:
        status, text, meta = services.svc_review_detailed(store, repo, max_provider_wait_seconds=1000)
        assert status == 0, text
        rec = store.get_review(meta['result']['ids']['review_id'])
        cfg = load_config(repo)
        assert rec['worst_runtime_sec'] >= budget.worst_runtime(cfg.defaults) + 1000 - budget.GRACE_SEC
        assert ceilings[0] >= budget.lock_stale_ceiling(cfg.defaults) + 3000 - budget.GRACE_SEC


def test_admission_snapshot_failure_releases_new_ticket(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    clock = controlled_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    save = Store.save_request_budget
    def fail_after_admission(store, rid, seq, owner, snapshot):
        if snapshot['phase'] == 'preflight' and store.capacity_holder_count('review-fg', str(repo / '.git')):
            raise OSError('synthetic snapshot persistence failure')
        return save(store, rid, seq, owner, snapshot)
    monkeypatch.setattr(Store, 'save_request_budget', fail_after_admission)
    with Store.open(tmp_path / 'db') as store:
        status, _, meta = services.svc_review_detailed(store, repo)
        assert status == 4 and not calls
        assert not store.capacity_active_views('review-fg', str(repo / '.git'))
        assert not (repo / '.git' / 'grok-reviews-foreground.lock').exists()


def test_snapshot_write_failure_does_not_leave_live_request(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    def refuse(*args, **kwargs):
        raise OSError('budget observations unavailable')
    monkeypatch.setattr(Store, 'save_request_budget', refuse)
    with Store.open(tmp_path / 'db') as store:
        status, _, meta = services.svc_review_detailed(store, repo, request_key='failed-budget')
        row = store.get_request(meta['request']['id'])
        assert status == 4
        assert row['state'] == 'failed'
        assert row['reason_code'] == 'request_failed'
        assert row['executions'][0]['completed_at'] is not None
        assert row['links'] == []
        replay = services.svc_review_detailed(store, repo, request_key='failed-budget')
        assert replay[2]['request']['reason_code'] == 'request_incomplete'


def test_default_fg_deadline_reports_real_admission_bound(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_ADMISSION_WAIT_SECONDS', '7')
    clock = controlled_clock(monkeypatch)
    calls, snapshots = [], []
    monkeypatch.setattr(runner, 'run_with_watchdog', clean_provider(clock, calls))
    save = Store.save_request_budget
    def observe(store, rid, seq, owner, snapshot):
        snapshots.append(snapshot)
        return save(store, rid, seq, owner, snapshot)
    monkeypatch.setattr(Store, 'save_request_budget', observe)
    with Store.open(tmp_path / 'db') as store:
        status, text, _ = services.svc_review_detailed(store, repo)
        assert status == 0, text
    queued = next(snapshot for snapshot in snapshots if snapshot['phase'] == 'queue')
    from datetime import datetime
    parse = lambda value: datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    assert queued['limits']['max_queue_seconds'] is None
    assert (parse(queued['deadlines']['queue']) - parse(queued['updated_at'])).total_seconds() == 7


def test_cancellation_audit_failure_still_stops_owned_provider(tmp_path, monkeypatch):
    import threading
    import time
    from tests.test_pipeline import _fake_grok, _emit, CLEAN, _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    _fake_grok(tmp_path, _emit(CLEAN) + '\nsleep 30')
    event = threading.Event()
    observed = []
    def refuse(*args, **kwargs):
        observed.append(True)
        raise OSError('synthetic cancellation audit failure')
    monkeypatch.setattr(Store, 'record_cancellation', refuse)
    def stop_after_spawn():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _calls(tmp_path):
                event.set()
                return
            time.sleep(.02)
    helper = threading.Thread(target=stop_after_spawn)
    started = time.monotonic()
    helper.start()
    try:
        with Store.open(tmp_path / 'db') as store:
            status, _, meta = services.svc_review_detailed(store, repo, cancel=event)
            assert status == 4 and _calls(tmp_path) == 1
            assert observed == [True]
            assert meta['result']['execution']['reason_code'] == 'cancellation_state_unavailable'
            rec = store.get_review(meta['result']['ids']['review_id'])
            assert rec['trustworthy'] is False and rec['status'] == 'failed'
            assert not store.capacity_active_views('provider:xai', 'xai')
    finally:
        helper.join(timeout=10)
    assert not helper.is_alive()
    assert time.monotonic() - started < 20  # proves cancellation, not the 30s provider exit
