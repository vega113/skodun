"""Hermetic shipped foreground requests exercise bounded workers and fences."""
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from skodun import requests, runner, services
from skodun.store import Store
from tests.test_batched_review import _body
from tests.test_requests import _ready_repo

CLEAN = b'{"structuredOutput":{"summary":"ok","findings":[]},"stopReason":"EndTurn"}'


def _lane(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    config = repo / '.skodun.toml'
    config.write_text(config.read_text() + '\n[defaults]\nmax_diff_bytes=4000\ndegraded_retries=0\ntimeout_retries=0\n')
    for index in range(4):
        (repo / f'f{index}.txt').write_text(_body(f'f{index}'))
    monkeypatch.setenv('SKODUN_PROVIDER_MAX_IN_FLIGHT', '2')
    return repo


def _label(cmd):
    return Path(cmd[cmd.index('--prompt-file') + 1]).name.split('.')[:2]


@pytest.mark.parametrize('value', [True, False, 0, 3, None, '2'])
def test_parallel_option_refuses_invalid_input_before_request(tmp_path, monkeypatch, value):
    repo = _lane(tmp_path, monkeypatch)
    with Store.open(tmp_path / 'db') as store:
        code, _, _ = services.svc_review_detailed(store, repo, batch_concurrency=value)
        assert code == 2
        assert store.list_requests() == []


def test_parallel_overlap_local_store_and_ordered_aggregation(tmp_path, monkeypatch):
    repo = _lane(tmp_path, monkeypatch)
    from tests.test_fallback import _fake_cli, _codex_stream
    from skodun import pipeline
    binary = _fake_cli(tmp_path, 'codex', 'exit 0')
    monkeypatch.setenv('SKODUN_CODEX_BIN', str(binary))
    config = repo / '.skodun.toml'
    config.write_text(config.read_text().replace('role = "finder"', 'role = "finder"\nfallbacks = ["backup"]', 1)
        + '\n[[reviewers]]\nname="backup"\nprovider="openai"\nmodel="gpt-test"\nrole="finder"\n')
    owner = threading.get_ident()
    degree, active, peak = [1], [0], [0]
    lock = threading.Lock()
    third_done, first_started = threading.Event(), threading.Event()
    started, finished, prompts, observations = [], [], {}, []
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        grok = '--prompt-file' in cmd
        prompt_path = Path(cmd[cmd.index('--prompt-file') + 1]) if grok else Path(stdin_path)
        role, item = prompt_path.name.split('.')[:2]
        label = item if role == 'primary' else role
        if grok and label == 'b2':
            raise runner.SpawnError(FileNotFoundError(2, 'fixture unavailable', cmd[0]), cmd=cmd, cwd=cwd)
        context = requests.current()
        assert context is not None and context.store._c.execute('SELECT 1').fetchone()[0] == 1
        assert context.budget.cancel.store is context.store
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            started.append(label)
            observations.append((degree[0], label, threading.get_ident(), id(context.store)))
            prompts[(degree[0], label)] = prompt_path.read_bytes()
        try:
            if label == 'b1':
                first_started.set()
            if degree[0] == 2 and label == 'b2':
                assert first_started.wait(5), 'first batch never entered the provider'
            if degree[0] == 2 and label == 'b1':
                assert third_done.wait(5), 'third batch never ran while first was active'
            if label == 'integration':
                assert set(finished) >= {'b1', 'b2', 'b3', 'b4'}
            payload = {'summary': 'ok', 'findings': []}
            if label.startswith('b'):
                payload['findings'] = [{'file': f'f{int(label[1:])-1}.txt', 'line': 1,
                    'severity': 'high', 'category': 'bug', 'title': f'finding-{label}',
                    'detail': 'Distinct finding from this prepared batch.'}]
            out.write_bytes(json.dumps({'structuredOutput': payload, 'stopReason': 'EndTurn'}).encode()
                            if grok else _codex_stream(payload).encode())
            return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
        finally:
            with lock:
                active[0] -= 1
                finished.append(label)
            if label == 'b3':
                third_done.set()
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    records = []
    for workers in (1, 2):
        degree[0] = workers
        active[0] = peak[0] = 0
        started.clear(); finished.clear(); third_done.clear(); first_started.clear()
        with Store.open(tmp_path / f'db-{workers}') as store:
            parent_store = id(store)
            code, message, metadata = services.svc_review_detailed(store, repo, batch_concurrency=workers, fresh=True)
            assert code == 1, (message, metadata)
            rec = store.get_review(metadata['result']['ids']['review_id'])
            records.append(rec)
            assert peak[0] == workers
            if workers == 2:
                assert finished.index('b2') < finished.index('b3') < finished.index('b1')
            rows = store._c.execute("SELECT status FROM capacity_admissions").fetchall()
            assert all(row['status'] not in ('queued', 'admitted', 'running') for row in rows)
            batches = [row for row in observations if row[0] == workers and row[1].startswith('b')]
            assert all((row[2] == owner and row[3] == parent_store) if workers == 1
                       else (row[2] != owner and row[3] != parent_store) for row in batches)
    for label in ('b1', 'b2', 'b3', 'b4', 'integration'):
        assert prompts[(1, label)] == prompts[(2, label)]
    for field in ('findings', 'files_changed', 'diff_bytes', 'prompt_bytes', 'parse_ok', 'degraded', 'diff_truncated'):
        assert records[0][field] == records[1][field]
    assert [finding['title'] for finding in records[1]['findings']] == [f'finding-b{i}' for i in range(1, 5)]
    assert [batch['provider'] for batch in records[1]['batches']] == ['xai', 'openai', 'xai', 'xai']
    assert pipeline._contributing_providers(records[0]) == pipeline._contributing_providers(records[1]) == ['openai', 'xai']
    assert records[1]['planning_policy']['execution_policy']['batch_concurrency'] == 2


def test_parallel_policy_is_a_request_and_checkpoint_boundary(tmp_path, monkeypatch):
    repo = _lane(tmp_path, monkeypatch)
    from skodun import checkpoints, planning_policy
    from skodun.config import Defaults
    from tests.test_checkpoints import _identity
    first = replace(_identity(), planning_policy=planning_policy.describe(Defaults()))
    second = replace(first, planning_policy=planning_policy.describe(Defaults(), batch_concurrency=2))
    assert checkpoints.first_mismatch(first, second) == 'batch_concurrency_changed'
    with Store.open(tmp_path / 'db') as store:
        # Refuse before a provider, while exercising real durable key identity.
        monkeypatch.setattr(services, '_svc_review_once', lambda *a, **k: (2, 'fixture refusal'))
        first = services.svc_review_detailed(store, repo, request_key='key', batch_concurrency=1)
        second = services.svc_review_detailed(store, repo, request_key='key', batch_concurrency=2)
        assert first[2]['request']['id'] == second[2]['request']['id']
        assert second[0] == 2


def test_unusable_parallel_output_is_not_reused_and_followups_wait(tmp_path, monkeypatch):
    repo = _lane(tmp_path, monkeypatch)
    (repo / 'auth').mkdir()
    for path in repo.glob('f*.txt'):
        path.rename(repo / 'auth' / path.name)
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '1')
    monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
    failed = {'b2'}
    calls = []
    lock = threading.Lock()
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        role, item = _label(cmd)
        label = item if role == 'primary' else role
        with lock:
            if label in ('integration', 'security', 'skeptic'):
                assert {'b1', 'b2', 'b3', 'b4'} <= set(calls)
            calls.append(label)
        out.write_bytes(b'not a review' if label in failed else CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        status, message, first = services.svc_review_detailed(store, repo, batch_concurrency=2)
        assert status == 4, message
        source = first['result']['ids']['batch_orchestration_id']
        original = store.list_checkpoints(source)
        assert store.get_review(first['result']['ids']['review_id'])['trustworthy'] is False
        failed.clear()
        # Preserve the original calls as barrier evidence; compare new suffix.
        count = len(calls)
        status, message, resumed = services.svc_review_detailed(store, repo, batch_concurrency=2, continue_compatible=True)
        assert status == 0, (message, resumed)
        assert calls[count:] == ['b2', 'integration', 'security', 'skeptic']
        assert store.list_checkpoints(source) == original


def test_lost_batch_fence_stops_and_joins_sibling_before_new_work(tmp_path, monkeypatch):
    from skodun import pipeline
    repo = _lane(tmp_path, monkeypatch)
    second = threading.Event()
    stopped = threading.Event()
    calls = []
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        role, label = _label(cmd)
        calls.append(label if role == 'primary' else role)
        if label == 'b1':
            assert second.wait(5)
            out.write_bytes(CLEAN)
            return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
        assert label == 'b2'
        second.set()
        assert cancel.wait(5), 'sibling did not observe lost-fence stop'
        stopped.set()
        raise runner.ReviewCancelled('peer stopped')
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    complete = Store.complete_checkpoint
    def lose(self, oid, kind, index, **kwargs):
        if kind == 'batch' and index == 1:
            self._c.execute('UPDATE review_checkpoints SET fence=fence+1 WHERE orchestration_id=? AND pass_kind=? AND pass_index=?', (oid, kind, index))
        return complete(self, oid, kind, index, **kwargs)
    monkeypatch.setattr(Store, 'complete_checkpoint', lose)
    with Store.open(tmp_path / 'db') as store:
        status, _, _ = services.svc_review_detailed(store, repo, batch_concurrency=2)
        assert status == 4 and stopped.is_set()
        assert sorted(calls) == ['b1', 'b2']
        assert store._c.execute("SELECT COUNT(*) FROM reviews WHERE trustworthy=1").fetchone()[0] == 0
    assert not any(t.name.startswith('skodun-batch') for t in threading.enumerate())


def test_real_owned_children_cancel_and_workers_close_before_return(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import os
    import time
    from tests.test_pipeline import _fake_grok
    from skodun.request_cancel import mark_event
    repo = _lane(tmp_path, monkeypatch)
    binary = _fake_grok(tmp_path, 'echo $$ > "$D/pid_$$"\nsleep 30')
    cancel = threading.Event()
    def run():
        with Store.open(tmp_path / 'db') as store:
            return services.svc_review_detailed(store, repo, batch_concurrency=2, cancel=cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            deadline = time.monotonic() + 10
            while len(list(binary.parent.glob('pid_*'))) < 2 and time.monotonic() < deadline:
                time.sleep(.02)
            paths = list(binary.parent.glob('pid_*'))
            assert len(paths) == 2
        finally:
            mark_event(cancel, 'requested_cancel')
        status, message, metadata = future.result(timeout=15)
        assert status == 4, (message, metadata)
    for path in paths:
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.read_text()), 0)
    with Store.open(tmp_path / 'db') as store:
        assert store._c.execute("SELECT COUNT(*) FROM capacity_admissions WHERE status IN ('queued','admitted','running')").fetchone()[0] == 0
        assert store._c.execute("SELECT COUNT(*) FROM reviews WHERE trustworthy=1").fetchone()[0] == 0
    assert not any(t.name.startswith('skodun-batch') for t in threading.enumerate())


@pytest.mark.parametrize('fg_capacity', [1, 2])
def test_simultaneous_requests_respect_repo_cap_and_provider_fifo(tmp_path, monkeypatch, fg_capacity):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from tests.test_gitio import _git
    repo = _lane(tmp_path, monkeypatch)
    peer = tmp_path / 'peer'
    _git(repo, 'worktree', 'add', '-b', 'peer', str(peer))
    for path in [repo / '.skodun.toml', repo / 'a.txt', *repo.glob('f*.txt')]:
        (peer / path.name).write_bytes(path.read_bytes())
    monkeypatch.setenv('SKODUN_LEGACY_FG_LOCK', '0')
    monkeypatch.setenv('SKODUN_REVIEW_FG_CAPACITY', str(fg_capacity))
    monkeypatch.setenv('SKODUN_LOCK_WAIT_SECONDS', '20')
    monkeypatch.setenv('SKODUN_LOCK_POLL_SECONDS', '0.05')
    release, both_a = threading.Event(), threading.Event()
    starts, lock = [], threading.Lock()
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        role, item = _label(cmd)
        label = item if role == 'primary' else role
        name = 'A' if Path(cwd).resolve() == repo.resolve() else 'B'
        with lock:
            starts.append((name, label))
            if {('A', 'b1'), ('A', 'b2')} <= set(starts):
                both_a.set()
        if name == 'A' and label in ('b1', 'b2'):
            assert release.wait(10)
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    db = tmp_path / 'db'
    with Store.open(db):
        pass
    def run(root):
        with Store.open(db) as store:
            return services.svc_review_detailed(store, root, batch_concurrency=2,
                max_queue_seconds=20, max_provider_wait_seconds=20)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run, repo)
        try:
            assert both_a.wait(10)
            b = pool.submit(run, peer)
            with Store.open(db) as observer:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    queued = observer._c.execute("SELECT a.resource_class FROM capacity_admissions a JOIN request_links l ON l.target_id=a.id AND l.kind='capacity' JOIN review_requests r ON r.id=l.request_id WHERE r.scope=? AND a.status='queued'", (str(peer.resolve()),)).fetchall()
                    if any(('provider' in row[0]) if fg_capacity == 2 else row[0] == 'review-fg' for row in queued):
                        break
                    time.sleep(.02)
                else:
                    pytest.fail('peer never entered the expected admission queue')
                assert len([pair for pair in starts if pair[0] == 'A']) == 2
        finally:
            release.set()
        assert a.result(timeout=20)[0] == 0
        assert b.result(timeout=20)[0] == 0
    first_b = next(i for i, item in enumerate(starts) if item[0] == 'B')
    if fg_capacity == 2:
        assert first_b < starts.index(('A', 'b3'))
    else:
        assert first_b > starts.index(('A', 'integration'))


def test_same_second_fifo_uses_committed_enqueue_order(tmp_path, monkeypatch):
    from skodun import store as store_module
    monkeypatch.setattr(store_module, '_iso_now', lambda: '2026-09-05T00:00:00Z')
    with Store.open(tmp_path / 'db') as store:
        store.capacity_enqueue(admission_id='z-first', resource_class='provider:xai', scope='xai')
        store.capacity_enqueue(admission_id='a-second', resource_class='provider:xai', scope='xai')
        assert store.capacity_position('z-first') == 1
        assert store.capacity_try_admit('a-second', capacity=1) is None
        assert store.capacity_try_admit('z-first', capacity=1)['status'] == 'admitted'


def test_enqueue_order_survives_new_process_and_terminal_rows(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from skodun import store as store_module
    monkeypatch.setattr(store_module, '_iso_now', lambda: '2026-09-05T00:00:00Z')
    db = tmp_path / 'db'
    with Store.open(db) as store:
        store.capacity_enqueue(admission_id='z-first', resource_class='provider:xai', scope='xai')
        store.capacity_enqueue(admission_id='a-second', resource_class='provider:xai', scope='xai')
    script = "from skodun.store import Store; import sys; s=Store.open(sys.argv[1]); print(s.capacity_position('z-first')); print(s.capacity_try_admit('a-second',capacity=1)); s.close()"
    result = subprocess.run([sys.executable, '-c', script, str(db)], capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}, check=True)
    assert result.stdout.splitlines() == ['1', 'None']
    with Store.open(db) as store:
        store.capacity_try_admit('z-first', capacity=1)
        store.capacity_finish('z-first', status='released')
        store.capacity_enqueue(admission_id='0-third', resource_class='provider:xai', scope='xai')
        assert store.capacity_position('z-first') is None
        assert store.capacity_position('a-second') == 1
        assert store.capacity_try_admit('0-third', capacity=1) is None


def test_worker_claims_use_the_shared_long_provider_wait_allowance(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    repo = _lane(tmp_path, monkeypatch)
    path = repo / '.skodun.toml'
    path.write_text(path.read_text() + 'timeout_sec=1\n')
    claims = []
    claim = Store.claim_checkpoint
    def inspect(self, oid, identity, **kwargs):
        result = claim(self, oid, identity, **kwargs)
        if identity.kind == 'batch' and result['decision'] == 'claimed':
            start = datetime.strptime(kwargs['now'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            end = datetime.strptime(kwargs['lease_expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            with Store.open(self._path) as peer:
                decision = claim(peer, oid, identity, owner='other-owner',
                    now=(start + timedelta(seconds=200)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    lease_expires_at=(start + timedelta(seconds=2000)).strftime('%Y-%m-%dT%H:%M:%SZ'))
            claims.append(((end-start).total_seconds(), decision['decision']))
        return result
    monkeypatch.setattr(Store, 'claim_checkpoint', inspect)
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        status, message, _ = services.svc_review_detailed(store, repo, batch_concurrency=2, max_provider_wait_seconds=1000)
        assert status == 0, message
    assert len(claims) == 4
    assert all(seconds >= 1000 and decision == 'in_flight' for seconds, decision in claims)


def test_blocked_sqlite_cancel_audit_joins_and_closes_workers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from skodun.request_cancel import mark_event
    repo = _lane(tmp_path, monkeypatch)
    entered, audit_entered = threading.Event(), threading.Event()
    cancel = threading.Event()
    calls, lock = [], threading.Lock()
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        with lock:
            calls.append(_label(cmd)[1])
            if len(calls) == 2:
                entered.set()
        assert cancel.wait(10)
        raise runner.ReviewCancelled('cancelled after SQLite audit')
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    record = Store.record_cancellation
    def audit(self, **kwargs):
        audit_entered.set()
        return record(self, **kwargs)
    monkeypatch.setattr(Store, 'record_cancellation', audit)
    db = tmp_path / 'db'
    with Store.open(db):
        pass
    def run():
        with Store.open(db) as store:
            return services.svc_review_detailed(store, repo, batch_concurrency=2, cancel=cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert entered.wait(10)
        with Store.open(db) as blocker:
            blocker._c.execute('BEGIN IMMEDIATE')
            try:
                mark_event(cancel, 'requested_cancel')
                assert audit_entered.wait(5)
                assert not future.done()
            finally:
                blocker._c.execute('ROLLBACK')
        assert future.result(timeout=15)[0] == 4
    assert sorted(calls) == ['b1', 'b2']
    with Store.open(db) as store:
        assert store._c.execute("SELECT COUNT(*) FROM capacity_admissions WHERE status IN ('queued','admitted','running')").fetchone()[0] == 0
    assert not any(t.name.startswith('skodun-batch') for t in threading.enumerate())


def test_final_diff_movement_refuses_parallel_publication(tmp_path, monkeypatch):
    repo = _lane(tmp_path, monkeypatch)
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        if _label(cmd) == ['primary', 'b1']:
            (repo / 'f0.txt').write_text('moved after frozen preparation\n')
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        status, _, _ = services.svc_review_detailed(store, repo, batch_concurrency=2)
        assert status not in (0, 1)
        assert store._c.execute("SELECT COUNT(*) FROM reviews WHERE trustworthy=1").fetchone()[0] == 0


def test_complete_parallel_request_has_no_compatible_work_to_launch(tmp_path, monkeypatch):
    repo = _lane(tmp_path, monkeypatch)
    calls = []
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(_label(cmd))
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        assert services.svc_review_detailed(store, repo, batch_concurrency=2)[0] == 0
        count = len(calls)
        status, _, _ = services.svc_review_detailed(store, repo, batch_concurrency=2, continue_compatible=True)
        assert status not in (0, 1) and len(calls) == count


def test_cli_mcp_and_preview_carry_parallel_intent(tmp_path, monkeypatch, capsys):
    from skodun import cli, mcpserver
    repo = _lane(tmp_path, monkeypatch)
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    assert cli.main(['review', '--repo', str(repo), '--batch-concurrency', '2', '--json']) == 0
    capsys.readouterr()
    spec = next(item for item in mcpserver.default_registry() if item.name == 'review')
    result = spec.handler(mcpserver.HandlerCall(params={'repo': str(repo), 'batch_concurrency': 2},
        store_factory=lambda: Store.open(db), cancel=threading.Event()))
    assert result.status == 0
    with Store.open(db) as store:
        rec = store.get_review(result.metadata['result']['ids']['review_id'])
        assert rec['planning_policy']['execution_policy']['batch_concurrency'] == 2
        _, single = services.svc_review_plan(store, repo, output='json', batch_concurrency=1)
        _, parallel = services.svc_review_plan(store, repo, output='json', batch_concurrency=2)
        left, right = json.loads(single), json.loads(parallel)
        assert left['boundary_digest'] == right['boundary_digest']
        assert left['planning_policy'] != right['planning_policy']
        assert right['call_counts']['parallel_batch_limit'] == 2
        from skodun.operational_targets import evidence
        from skodun import planning_policy
        from skodun.config import Defaults, Reviewer
        from tests.test_plan_preview import history_records
        observed = evidence(history_records(), reviewer=Reviewer(name='finder', provider='xai', model='test', role='finder'),
            mode='now', execution_policy=planning_policy.execution_policy(Defaults(), 2), now='2026-09-05T12:00:00Z')
        assert observed['execution_policy_mismatched_records'] == 20
        assert not observed['cohorts']


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
