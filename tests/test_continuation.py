"""Explicit continuation reuses only exact usable batch evidence.

Shipped services, pipeline, store claims and finalization run against temporary
repos/stores. Providers are fake runners; consumed source evidence never mutates.
"""

import threading
from pathlib import Path

import pytest

from skodun import runner, services
from skodun.store import Store
from tests.test_batched_review import _body
from tests.test_requests import _ready_repo

CLEAN = b'{"structuredOutput":{"summary":"ok","findings":[]},"stopReason":"EndTurn"}'


@pytest.fixture
def lane(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    config = repo / '.skodun.toml'
    config.write_text(config.read_text() + '\n[defaults]\nmax_diff_bytes=4000\ndegraded_retries=0\ntimeout_retries=0\n')
    for i in range(4):
        (repo / f'f{i}.txt').write_text(_body(f'f{i}'))
    calls = []
    failures = set()
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        name = Path(cmd[cmd.index('--prompt-file') + 1]).name
        label = 'integration' if name.startswith('integration.') else name.split('.')[1]
        calls.append(label)
        out.write_bytes(b'not a review' if label in failures else CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        yield repo, store, calls, failures


@pytest.mark.parametrize('failed', ['b2', 'integration'])
def test_consumed_untrustworthy_continuation_retries_failed_and_dependent_work(lane, failed):
    repo, store, calls, failures = lane
    failures.add(failed)
    status, _, original = services.svc_review_detailed(store, repo)
    assert status == 4
    source_id = original['result']['ids']['batch_orchestration_id']
    source = store.get_orchestration(source_id)
    checkpoints = store.list_checkpoints(source_id)
    assert source['state'] == 'consumed'
    calls.clear()
    failures.clear()
    status, _, continued = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert status == 0
    assert calls == (['b2', 'integration'] if failed == 'b2' else ['integration'])
    assert continued['request']['id'] == original['request']['id']
    child_id = continued['result']['ids']['batch_orchestration_id']
    assert child_id != source_id
    assert store.get_orchestration(source_id) == source
    assert store.list_checkpoints(source_id) == checkpoints
    assert continued['continuation']['source_orchestration_id'] == source_id
    assert store.get_orchestration(child_id)['state'] == 'consumed'


def test_interrupted_after_three_usable_batches_runs_only_missing_and_integration(lane, monkeypatch):
    from skodun.request_cancel import mark_event
    repo, store, calls, failures = lane
    cancel = threading.Event()
    complete = store.complete_checkpoint
    def stop_after_three(orchestration_id, kind, index, **kwargs):
        applied = complete(orchestration_id, kind, index, **kwargs)
        if kind == 'batch' and index == 3:
            mark_event(cancel, 'requested_cancel')
        return applied
    monkeypatch.setattr(store, 'complete_checkpoint', stop_after_three)
    status, _, original = services.svc_review_detailed(store, repo, cancel=cancel)
    assert status == 4 and calls == ['b1', 'b2', 'b3']
    monkeypatch.setattr(store, 'complete_checkpoint', complete)
    calls.clear()
    status, _, continued = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert status == 0 and calls == ['b4', 'integration']
    assert continued['request']['id'] == original['request']['id']
    identity = continued['request']['identity']
    assert store.find_resume_candidate(identity['repo_id'], identity['worktree_root'], identity['branch']) is None
    calls.clear()
    assert services.svc_review_detailed(store, repo)[0] == 0
    assert calls == ['b1', 'b2', 'b3', 'b4', 'integration']


def test_continue_rejects_fresh_intent_without_execution(lane):
    repo, store, calls, failures = lane
    status, _, metadata = services.svc_review_detailed(store, repo, continue_compatible=True, fresh=True)
    assert status == 2 and not calls
    assert metadata['result']['execution']['reason_code'] == 'invalid_input'


@pytest.mark.parametrize('change', ['diff', 'boundary', 'reviewer'])
def test_changed_identity_refuses_reuse_with_stable_mismatch(lane, change):
    repo, store, calls, failures = lane
    failures.add('b2')
    status, _, original = services.svc_review_detailed(store, repo)
    assert status == 4
    calls.clear()
    kwargs = {}
    if change == 'diff':
        (repo / 'f1.txt').write_text(_body('different'))
    elif change == 'boundary':
        kwargs['batch_target_bytes'] = 1500
    else:
        kwargs['reviewer'] = 'finder'
    status, _, metadata = services.svc_review_detailed(store, repo, continue_compatible=True, **kwargs)
    assert status == 2 and calls == []
    assert metadata['continuation']['status'] == 'refused'
    assert metadata['continuation']['first_mismatch']
    assert metadata['result']['coverage']['trustworthy'] is None


def test_fresh_second_opinion_reruns_all_batches(lane):
    repo, store, calls, failures = lane
    failures.add('b2')
    _, _, original = services.svc_review_detailed(store, repo)
    calls.clear()
    failures.clear()
    status, _, fresh = services.svc_review_detailed(store, repo, fresh=True)
    assert status == 0 and calls == ['b1', 'b2', 'b3', 'b4', 'integration']
    assert fresh['request']['id'] != original['request']['id']
    assert fresh['result']['continuation'] is None


def test_two_racing_continuers_launch_no_duplicate(lane, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repo, store, calls, failures = lane
    failures.add('b2')
    _, _, original = services.svc_review_detailed(store, repo)
    failures.clear()
    calls.clear()
    started, release = threading.Event(), threading.Event()
    provider = runner.run_with_watchdog
    def hold_failed_pass(cmd, *args, **kwargs):
        if '.b2.' in Path(cmd[cmd.index('--prompt-file') + 1]).name:
            started.set()
            assert release.wait(10)
        return provider(cmd, *args, **kwargs)
    monkeypatch.setattr(runner, 'run_with_watchdog', hold_failed_pass)
    def first():
        with Store.open(tmp_path / 'db') as peer:
            return services.svc_review_detailed(peer, repo, continue_compatible=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(first)
        try:
            assert started.wait(10)
            status, _, duplicate = services.svc_review_detailed(store, repo, continue_compatible=True)
            assert status == 3
            assert duplicate['request']['id'] == original['request']['id']
        finally:
            release.set()
        assert active.result()[0] == 0
    assert calls == ['b2', 'integration']


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
def test_explicit_continuation_on_both_shipped_surfaces(lane, tmp_path, monkeypatch, capsys, surface):
    import json
    from skodun.cli import main
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    repo, store, calls, failures = lane
    monkeypatch.setenv('SKODUN_DB', str(tmp_path / 'db'))
    failures.add('integration')
    if surface == 'cli':
        assert main(['review', '--repo', str(repo), '--json']) == 4
        first = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(params={'repo': str(repo)},
            store_factory=lambda: Store.open(tmp_path / 'db'), cancel=threading.Event()))
        assert response.status == 4
        first = response.metadata['result']
    calls.clear()
    failures.clear()
    if surface == 'cli':
        assert main(['review', '--repo', str(repo), '--json', '--continue']) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(params={'repo': str(repo), 'continue_compatible': True},
            store_factory=lambda: Store.open(tmp_path / 'db'), cancel=threading.Event()))
        assert response.status == 0
        result = response.metadata['result']
    assert calls == ['integration']
    assert result['ids']['request_id'] == first['ids']['request_id']
    assert result['continuation']['counts'] == {'reused': 4, 'executed': 1, 'failed': 0}


def test_changed_capability_reconsiders_transport_without_provider_poisoning(lane, monkeypatch):
    from skodun import chain
    from skodun.adapters.agy import AgyAdapter
    repo, store, calls, failures = lane
    cfg = repo / '.skodun.toml'
    cfg.write_text(cfg.read_text().replace('role = "finder"', 'role = "finder"\nfallbacks = ["small"]') +
                   '\n[[reviewers]]\nname="small"\nprovider="google"\nmodel="small-model"\nrole="finder"\n')
    monkeypatch.setenv('SKODUN_AGY_BIN', '/bin/sh')
    limit = [1]
    monkeypatch.setattr(AgyAdapter, 'prompt_limit', lambda self: limit[0])
    admitted = []
    acquire = chain._acquire_provider_slot
    def admission(store, provider, **kwargs):
        admitted.append(provider)
        return acquire(store, provider, **kwargs)
    monkeypatch.setattr(chain, '_acquire_provider_slot', admission)
    launched = []
    def timeout_then_fallback(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        google = '--print' in cmd
        label = 'google' if google else Path(cmd[cmd.index('--prompt-file') + 1]).name
        launched.append(label)
        if not google and '.b2.' in label:
            return runner.RunResult(rc=124, timed_out=True, duration_sec=10, first_output_sec=None)
        out.write_bytes(CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', timeout_then_fallback)
    assert services.svc_review_detailed(store, repo)[0] == 4
    assert services.svc_review_detailed(store, repo, continue_compatible=True)[0] == 4
    assert 'google' not in admitted and 'google' not in launched
    limit[0] = 100_000
    status, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert status == 0
    assert admitted.count('google') == 1 and launched.count('google') == 1
    assert result['continuation']['counts']['reused'] == 3


def test_child_identity_is_distinct_while_source_canonical_identity_stays_stable(lane):
    import json
    from skodun.checkpoints import OrchestrationIdentity, first_mismatch
    repo, store, calls, failures = lane
    failures.add('integration')
    _, _, initial = services.svc_review_detailed(store, repo)
    source = store.get_orchestration(initial['result']['ids']['batch_orchestration_id'])
    parsed = OrchestrationIdentity.from_json(source['identity_json'])
    assert 'continuation_source' not in json.loads(parsed.canonical_json())
    assert parsed.digest() == source['identity_digest']
    failures.clear()
    _, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    child = store.get_orchestration(result['result']['ids']['batch_orchestration_id'])
    continued = OrchestrationIdentity.from_json(child['identity_json'])
    assert continued.continuation_source == source['id']
    assert first_mismatch(parsed, continued) is None
    assert parsed.digest() != continued.digest()


def test_corrupted_source_pass_cannot_seed_new_evidence(lane):
    repo, store, calls, failures = lane
    failures.add('b2')
    _, _, initial = services.svc_review_detailed(store, repo)
    source_id = initial['result']['ids']['batch_orchestration_id']
    store._c.execute("UPDATE review_checkpoints SET diff_hash='wrong' WHERE orchestration_id=? AND pass_index=1", (source_id,))
    calls.clear()
    status, _, metadata = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert status == 2 and not calls
    assert metadata['result']['execution']['reason_code'] == 'continuation_source_pass_mismatch'


def test_usable_findings_do_not_change_the_continued_prompt_context(lane, monkeypatch):
    from skodun import pipeline
    from tests.test_gitio import _git
    import json
    repo, store, calls, failures = lane
    _git(repo, 'remote', 'add', 'origin', 'https://github.com/acme/continuation-test.git')
    clock = ['2026-09-05T00:00:00Z']
    monkeypatch.setattr(pipeline, '_iso_now', lambda: clock[0])
    provider = runner.run_with_watchdog
    def finding_in_first_batch(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        result = provider(cmd, timeout_sec, cwd, out, err, stdin_path=stdin_path, cancel=cancel)
        name = Path(cmd[cmd.index('--prompt-file') + 1]).name
        if '.b1.' in name:
            out.write_text(json.dumps({'structuredOutput': {'summary': 'one finding', 'findings': [
                {'file': 'f0.txt', 'line': 1, 'severity': 'medium', 'title': 'Missing guard', 'detail': 'Verify input before applying it'}]},
                'stopReason': 'EndTurn'}))
        return result
    monkeypatch.setattr(runner, 'run_with_watchdog', finding_in_first_batch)
    failures.add('b2')
    code, _, first = services.svc_review_detailed(store, repo)
    assert code == 4 and first['result']['findings']['total'] == 1
    clock[0] = '2026-09-05T00:01:00Z'
    failures.clear()
    calls.clear()
    code, _, continued = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 1
    assert calls == ['b2', 'integration']
    assert continued['request']['id'] == first['request']['id']
    assert continued['result']['findings']['total'] == 1
    calls.clear()
    fresh_code, _, fresh = services.svc_review_detailed(store, repo, fresh=True)
    assert fresh_code == code == 1
    assert fresh['result']['coverage']['trustworthy'] is True
    assert calls == ['b1', 'b2', 'b3', 'b4', 'integration']


def test_explicit_compatible_recovery_keeps_successful_batches_across_attempts(lane, monkeypatch):
    repo, store, calls, failures = lane
    failures.add('b2')
    _, _, first = services.svc_review_detailed(store, repo)
    calls.clear()
    provider = runner.run_with_watchdog
    attempts = []
    def succeeds_on_second_retry(cmd, *args, **kwargs):
        if '.b2.' in Path(cmd[cmd.index('--prompt-file') + 1]).name:
            attempts.append(1)
            if len(attempts) == 2:
                failures.clear()
        return provider(cmd, *args, **kwargs)
    monkeypatch.setattr(runner, 'run_with_watchdog', succeeds_on_second_retry)
    status, _, result = services.svc_review_detailed(
        store, repo, continue_compatible=True, recover=True, max_attempts=2)
    assert status == 0 and calls == ['b2', 'integration', 'b2', 'integration']
    assert result['request']['id'] == first['request']['id']
    assert result['recovery']['attempts'] == 2
    assert result['continuation']['counts'] == {'reused': 3, 'executed': 2, 'failed': 0}
