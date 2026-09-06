"""Versioned outcomes through shipped CLI, MCP, and shared review services.

The external contract preserves typed causes and exact current request evidence,
keeps provider input separate from aggregates, and never turns incomplete work
or malformed replay data into successful coverage or gate clearance.
"""
import json

import pytest

from skodun import services
from skodun.cli import main
from skodun.store import Store
from tests.test_requests import _ready_repo


def assert_result(result, code):
    assert result['schema_version'] == 'review-result/v1'
    assert result['execution']['exit_code'] == code
    assert isinstance(result['execution']['reason_code'], str)
    assert result['gate'] == {'evaluated': False, 'exit_code': None}
    assert isinstance(result['attempts'], list)
    assert result['execution']['state'] in ('cancelled', 'expired', 'refused', 'completed', 'failed', 'partial')
    assert all(value is None or isinstance(value, str) for value in result['ids'].values())
    assert all(type(value) in (bool, type(None)) for value in result['coverage'].values())
    for side in ('requested', 'observed'):
        assert all(value is None or isinstance(value, str) for value in result['identity'][side].values())
    for attempt in result['attempts']:
        assert type(attempt['launched']) is bool
        assert isinstance(attempt['reason_code'], str)
        assert attempt['input_bytes'] is None or type(attempt['input_bytes']) is int
        assert attempt['input_scope'] == 'provider_input'
    assert result['bytes']['scope'] == 'review_aggregate'


def test_cli_json_clean_review(tmp_path, monkeypatch, capsys):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_DB', str(tmp_path / 'db.sqlite'))
    code = main(['review', '--repo', str(repo), '--json'])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert_result(result, code)
    assert code == 0 and result['execution']['reason_code'] == 'review_clean'
    assert result['ids']['request_id'].startswith('sk_req_')
    assert result['ids']['review_id']
    assert result['coverage']['trustworthy'] is True
    assert result['attempts'][0]['launched'] is True
    assert result['attempts'][0]['input_bytes'] > 0
    assert 'SKODUN REQUEST' in output.err


def test_service_preflight_has_unknown_coverage(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_GROK_BIN', str(tmp_path / 'absent'))
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo)
    result = meta['result']
    assert_result(result, code)
    assert code == 2
    assert result['execution']['reason_code'] == 'preflight_refused'
    assert result['ids']['review_id'] is None
    assert result['coverage']['trustworthy'] is None
    assert result['attempts'] == []


@pytest.mark.parametrize('code, expected', [(2, 'invalid_input'), (4, 'persistence_failed')])
def test_mcp_early_results_use_shared_schema(code, expected):
    from skodun.mcpserver import _review_result
    res = _review_result(code, 'diagnostic', {'termination': {'reason_code': expected}})
    assert_result(res.metadata['result'], code)
    assert res.metadata['result']['execution']['reason_code'] == expected


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
@pytest.mark.parametrize('route', ['size_only', 'timeout_size', 'timeout_size_capable'])
def test_transport_result_matrix_uses_real_chain(tmp_path, monkeypatch, capsys, surface, route):
    from skodun import chain, pipeline, runner
    from skodun.adapters.agy import MAX_PROMPT_ARG_BYTES
    from skodun.config import Config, Defaults, Reviewer
    from tests.test_cli import _round
    from tests.test_mcptools import _specs
    from skodun.mcpserver import HandlerCall
    import threading

    repo = _ready_repo(tmp_path, monkeypatch)
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    monkeypatch.setenv('SKODUN_AGY_BIN', '/bin/sh')
    launches = []
    primary = Reviewer(name='primary', provider='xai', model='primary', role='finder',
                       fallbacks=('small', 'capable') if route.endswith('capable') else ('small',))
    small = Reviewer(name='small', provider='google', model='small', role='finder')
    capable = Reviewer(name='capable', provider='xai', model='capable', role='finder')
    reviewers = (small,) if route == 'size_only' else (primary, small, capable)
    cfg = Config(defaults=Defaults(), reviewers=reviewers)
    prompt = 'é'.encode() * (MAX_PROMPT_ARG_BYTES // 2 + 1)

    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        launches.append(cmd)
        if len(launches) == 1:
            return runner.RunResult(rc=124, timed_out=True, duration_sec=20, first_output_sec=None)
        out.write_bytes(b'{"structuredOutput":{"summary":"s","findings":[]},"stopReason":"EndTurn"}')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)

    def review(root, ignored_cfg, store, **kwargs):
        # Inject only the review orchestration boundary; the shipped chain
        # owns eligibility, slots, retries, classifications, and fake runners.
        outcome = chain.run_chain(reviewers[0], cfg, cfg.defaults, prompt, root, store, tmp_path, 'matrix')
        rec = _round(id='matrix-review', attempts=outcome.attempts,
                     trustworthy=outcome.parsed is not None,
                     parse_ok=outcome.parsed is not None,
                     status='clean' if outcome.parsed else 'failed',
                     failure_reason=outcome.failure_reason, prompt_bytes=len(prompt))
        store.save_review(rec)
        return store.get_review(rec['id'])

    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    monkeypatch.setattr(pipeline, 'run_review', review)
    if surface == 'cli':
        code = main(['review', '--repo', str(repo), '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo': str(repo)}, store_factory=lambda: Store.open(db), cancel=threading.Event()))
        code, result = response.status, response.metadata['result']
    assert_result(result, code)
    expected = 0 if route.endswith('capable') else 4
    assert code == expected
    assert result['execution']['reason_code'] == ('review_clean' if expected == 0 else 'no_compatible_route')
    assert result['counts']['provider_launches'] == len(launches)
    assert len(launches) == {'size_only': 0, 'timeout_size': 1, 'timeout_size_capable': 2}[route]
    skipped = next(a for a in result['attempts'] if not a['launched'])
    assert skipped['reason_code'] == 'transport_ineligible'
    assert skipped['input_bytes'] == len(prompt)
    assert skipped['limit_bytes'] == MAX_PROMPT_ARG_BYTES
    assert skipped['duration_sec'] is None
    if route != 'size_only':
        assert 'provider_timeout' in result['causes']
        assert result['attempts'][0]['launched'] is True
    assert len({a['attempt_id'] for a in result['attempts']}) == len(result['attempts'])


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
def test_startup_store_failure_has_no_stale_review(tmp_path, monkeypatch, capsys, surface):
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    import threading

    def broken(*a, **k):
        raise OSError('unavailable')
    monkeypatch.setattr(Store, 'open', broken)
    if surface == 'cli':
        code = main(['review', '--repo', str(tmp_path), '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo': str(tmp_path)}, store_factory=broken, cancel=threading.Event()))
        code, result = response.status, response.metadata['result']
    assert_result(result, code)
    assert result['execution']['reason_code'] == 'persistence_failed'
    assert result['ids']['review_id'] is None and result['ids']['request_id'] is None
    assert result['coverage']['trustworthy'] is None


def test_invalid_cli_option_is_json(tmp_path, capsys):
    code = main(['review', '--json', '--max-attempts', 'not-an-integer'])
    result = json.loads(capsys.readouterr().out)
    assert_result(result, code)
    assert result['execution']['reason_code'] == 'invalid_input'


def test_json_keyboard_interrupt_keeps_130(tmp_path, monkeypatch, capsys):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_DB', str(tmp_path / 'db'))
    def interrupt(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(services, 'svc_review_detailed', interrupt)
    code = main(['review', '--repo', str(repo), '--json'])
    result = json.loads(capsys.readouterr().out)
    assert code == 130
    assert result['execution']['reason_code'] == 'requested_cancel'


def test_bounded_observation_keeps_aggregate_and_input_bytes_distinct():
    from skodun.review_results import observation, MAX_ATTEMPTS
    rec = {'id': 'batched', 'batched': True, 'prompt_bytes': 987654,
           'batches': [{'index': i, 'parse_ok': True, 'attempts': [{
               'n': 1, 'rc': 0, 'timed_out': False, 'input_bytes': 300,
               'attempt_id': f'a{i}', 'duration_sec': 1}]} for i in range(MAX_ATTEMPTS + 1)],
           'integration': {'attempts': []}, 'trustworthy': False}
    facts = observation(rec)
    assert facts['aggregate_prompt_bytes'] == 987654
    assert facts['attempts'][0]['input_bytes'] == 300
    assert facts['candidate_count'] == MAX_ATTEMPTS + 1
    assert len(facts['attempts']) == MAX_ATTEMPTS and facts['attempts_truncated']
    assert facts['partial'] is True


def test_malformed_request_replay_is_not_success(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(services, '_svc_review_once', lambda *a, **k: (2, 'refused'))
    with Store.open(tmp_path / 'db') as store:
        _, _, meta = services.svc_review_detailed(store, repo, request_key='same')
        store._c.execute('UPDATE review_requests SET result_json=? WHERE id=?',
                         (json.dumps({'status': False, 'text': 'bad', 'metadata': {}}), meta['request']['id']))
        code, _, meta = services.svc_review_detailed(store, repo, request_key='same')
    assert code == 4
    assert meta['result']['execution']['reason_code'] == 'request_result_invalid'
    assert meta['result']['coverage']['trustworthy'] is None


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
def test_findings_result_separates_gate_and_triage(tmp_path, monkeypatch, capsys, surface):
    from skodun import runner
    from tests.test_pipeline import DIRTY
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        out.write_text(DIRTY)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    if surface == 'cli':
        code = main(['review', '--repo', str(repo), '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo': str(repo)}, store_factory=lambda: Store.open(db), cancel=threading.Event()))
        code, result = response.status, response.metadata['result']
    assert_result(result, code)
    assert code == 1 and result['execution']['reason_code'] == 'review_findings'
    assert result['coverage']['trustworthy'] is True
    assert result['findings']['total'] > 0 and result['findings']['open'] is None
    assert result['findings']['triage_evaluated'] is False


def test_idempotent_replay_preserves_result_without_another_launch(tmp_path, monkeypatch):
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    with Store.open(tmp_path / 'db') as store:
        first = services.svc_review_detailed(store, repo, request_key='same')[2]['result']
        second = services.svc_review_detailed(store, repo, request_key='same')[2]['result']
    assert _calls(tmp_path) == 1
    assert first['ids'] == second['ids']
    assert second['execution']['replayed'] is True
    assert second['counts']['scope'] == 'observed_review'


def test_typed_cancellation_has_no_prior_attempt_identity(tmp_path, monkeypatch):
    from skodun.pipeline import ReviewCancelled
    from skodun import pipeline
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    def cancel(*a, **k):
        raise ReviewCancelled('text intentionally unrelated to cancellation')
    monkeypatch.setattr(pipeline, 'run_review', cancel)
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo, cancel=threading.Event())
    assert code == 4 and meta['result']['execution']['reason_code'] == 'requested_cancel'
    assert meta['result']['ids']['review_id'] is None


def test_recovery_budget_and_external_cancel_have_different_codes(tmp_path, monkeypatch):
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    cancelled = threading.Event()
    from skodun.request_cancel import mark_event
    mark_event(cancelled, 'requested_cancel')
    with Store.open(tmp_path / 'db') as store:
        _, _, external = services.svc_review_detailed(store, repo, recover=True, cancel=cancelled)
        clock = iter([0.0, 2.0, 2.0, 2.0, 2.0])
        monkeypatch.setattr('time.monotonic', lambda: next(clock, 2.0))
        _, _, budget = services.svc_review_detailed(store, repo, recover=True, max_wall_seconds=1)
    assert external['result']['execution']['reason_code'] == 'requested_cancel'
    assert budget['result']['execution']['reason_code'] == 'total_budget_exhausted'
    assert external['result']['execution']['exit_code'] == budget['result']['execution']['exit_code'] == 4


def test_trusted_reuse_names_original_request_and_observed_counts(tmp_path, monkeypatch):
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    with Store.open(tmp_path / 'db') as store:
        first_code, _, first_meta = services.svc_review_detailed(store, repo)
        code, _, meta = services.svc_review_detailed(store, repo, reuse_trusted=True)
    result = meta['result']
    assert first_code == code == 0
    assert _calls(tmp_path) == 1
    assert result['execution']['reason_code'] == 'trusted_reuse'
    assert result['execution']['reused'] is True
    assert result['ids']['request_id'] != first_meta['result']['ids']['request_id']
    assert result['ids']['observed_request_id'] == first_meta['result']['ids']['request_id']
    assert result['counts']['scope'] == 'observed_review'
    assert result['counts']['review_id'] == first_meta['result']['ids']['review_id']


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
@pytest.mark.parametrize('successful_pass', ['batch', 'integration'])
def test_partial_batches_keep_provider_input_and_aggregate_scopes(tmp_path, monkeypatch, capsys, surface, successful_pass):
    from skodun import runner
    from tests.test_batched_review import _body
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = repo / '.skodun.toml'
    cfg.write_text(cfg.read_text() + '\n[defaults]\nmax_diff_bytes=4000\ntimeout_retries=0\ndegraded_retries=0\n')
    for i in range(3):
        (repo / f'f{i}.txt').write_text(_body(f'f{i}'))
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    inputs = []
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        from pathlib import Path
        prompt_file = Path(cmd[cmd.index('--prompt-file') + 1])
        inputs.append(len(prompt_file.read_bytes()))
        clean = (len(inputs) == 1 if successful_pass == 'batch' else
                 prompt_file.name.startswith('integration.'))
        out.write_bytes(b'{"structuredOutput":{"summary":"s","findings":[]},"stopReason":"EndTurn"}'
                        if clean else b'no usable review')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    if surface == 'cli':
        code = main(['review', '--repo', str(repo), '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo': str(repo)}, store_factory=lambda: Store.open(db), cancel=threading.Event()))
        code, result = response.status, response.metadata['result']
    assert_result(result, code)
    assert code == 4 and result['coverage']['partial'] is True
    assert result['execution']['state'] == 'partial'
    assert result['orchestration']['batch']['batch_count'] >= 2
    assert result['counts']['provider_launches'] == len(inputs)
    assert [a['input_bytes'] for a in result['attempts']] == inputs
    assert result['bytes']['prompt_bytes'] > max(inputs)
    assert result['attempts'][0]['scope']['kind'] == 'batch'


@pytest.mark.parametrize('surface', ['service', 'cli', 'mcp'])
@pytest.mark.parametrize('cancel_at', ['boundary', 'inflight'])
def test_cancelled_checkpoint_retains_only_current_attempt_evidence(tmp_path, monkeypatch, capsys, surface, cancel_at):
    import threading
    from skodun import pipeline, runner
    from tests.test_batched_review import _body
    from skodun.mcpserver import HandlerCall
    from tests.test_mcptools import _specs
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = repo / '.skodun.toml'
    cfg.write_text(cfg.read_text() + '\n[defaults]\nmax_diff_bytes=4000\n')
    for i in range(3):
        (repo / f'f{i}.txt').write_text(_body(f'f{i}'))
    db = tmp_path / 'db'
    monkeypatch.setenv('SKODUN_DB', str(db))
    cancel = threading.Event()
    run = pipeline.run_review
    def with_cancel(*args, **kwargs):
        kwargs['cancel'] = cancel
        return run(*args, **kwargs)
    monkeypatch.setattr(pipeline, 'run_review', with_cancel)
    complete = Store.complete_checkpoint
    def completed(store, orchestration_id, kind, index, **kwargs):
        applied = complete(store, orchestration_id, kind, index, **kwargs)
        if applied and kind == 'batch' and index == 1 and cancel_at == 'boundary':
            cancel.set()
        return applied
    monkeypatch.setattr(Store, 'complete_checkpoint', completed)
    calls = []
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        calls.append(cmd)
        if len(calls) == 2 and cancel_at == 'inflight':
            cancel.set()
            raise runner.ReviewCancelled('provider stopped mid-pass')
        out.write_bytes(b'{"structuredOutput":{"summary":"s","findings":[]},"stopReason":"EndTurn"}')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    if surface == 'service':
        with Store.open(db) as store:
            code, _, meta = services.svc_review_detailed(store, repo, cancel=cancel)
        result = meta['result']
    elif surface == 'cli':
        code = main(['review', '--repo', str(repo), '--json'])
        result = json.loads(capsys.readouterr().out)
    else:
        response = _specs()['review'].handler(HandlerCall(
            params={'repo': str(repo)}, store_factory=lambda: Store.open(db), cancel=cancel))
        code, result = response.status, response.metadata['result']
    expected_cause = 'requested_cancel' if surface == 'cli' else 'unknown_cancel_token'
    assert code == 4 and result['execution']['reason_code'] == expected_cause
    assert result['ids']['review_id'] is not None
    with Store.open(db) as store:
        rec = store.get_review(result['ids']['review_id'])
        assert rec['request_id'] == result['ids']['request_id']
        assert rec['batch_orchestration_id'] == result['ids']['batch_orchestration_id']
    assert len(calls) == (1 if cancel_at == 'boundary' else 2)
    assert result['coverage']['trustworthy'] is False
    assert result['coverage']['partial'] is True
    assert result['checkpoints']['completed'] == 1
    assert result['counts']['complete'] is False
    assert result['counts']['provider_launches'] is None
    assert result['counts']['known_provider_launches'] == 1
    assert result['attempts'][0]['launched'] is True


def test_reuse_cancellation_is_typed(tmp_path, monkeypatch):
    import threading
    repo = _ready_repo(tmp_path, monkeypatch)
    cancelled = threading.Event()
    from skodun.request_cancel import mark_event
    mark_event(cancelled, 'requested_cancel')
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo, reuse_trusted=True, cancel=cancelled)
    assert code == 4 and meta['result']['execution']['reason_code'] == 'requested_cancel'


def test_provider_admission_expiry_is_terminal_expiry():
    from skodun.review_results import attach, observation
    facts = observation({'id': 'r', 'trustworthy': False, 'attempts': [{
        'n': 1, 'skipped': 'arbitrary prose', 'reason_code': 'admission_expired'}]})
    result = attach(4, 'arbitrary prose', {'observation': facts})[2]['result']
    assert result['execution']['reason_code'] == 'admission_expired'
    assert result['execution']['state'] == 'expired'


@pytest.mark.parametrize('field, value', [
    ('trustworthy', False), ('partial', 'yes'), ('causes', {'bad': 'shape'}),
    ('attempts', [{'launched': 'false'}]), ('attempts_truncated', 1),
])
def test_replay_rejects_nested_contract_or_success_contradictions(field, value):
    from skodun.review_results import valid_replay, observation
    facts = observation({'id': 'r', 'trustworthy': True, 'parse_ok': True,
                         'findings_total': 0, 'attempts': []})
    facts[field] = value
    assert not valid_replay({'status': 0, 'text': 'clean', 'metadata': {'observation': facts}})


@pytest.mark.parametrize('metadata', [
    {'termination': {'retryable': 'yes'}},
    {'termination': {'state': 'cancelled', 'reason_code': 'requested_cancel'}},
    {'timing': {'scope': 'request_execution', 'review_wall_ms': float('nan')}},
    {'timing': {'scope': 'request_execution', 'review_wall_ms': 'fast'}},
    {'recovery': {'review_ids': 7}},
])
def test_replay_rejects_invalid_projected_metadata(metadata):
    from skodun.review_results import valid_replay
    assert not valid_replay({'status': 0, 'text': 'clean', 'metadata': metadata})



def test_pre_record_recovery_cancel_does_not_borrow_prior_review(tmp_path, monkeypatch):
    from skodun import pipeline, runner
    repo = _ready_repo(tmp_path, monkeypatch)
    cfg = repo / '.skodun.toml'
    cfg.write_text(cfg.read_text() + '\n[defaults]\ndegraded_retries=0\n')
    run = pipeline.run_review
    calls = []
    def run_or_cancel(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise runner.ReviewCancelled('cancelled before this attempt has a record')
        return run(*args, **kwargs)
    def unusable(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        out.write_bytes(b'not a review')
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(pipeline, 'run_review', run_or_cancel)
    monkeypatch.setattr(runner, 'run_with_watchdog', unusable)
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo, recover=True, max_attempts=2)
    assert code == 4 and len(calls) == 2
    assert len(meta['result']['orchestration']['recovery']['review_ids']) == 1
    assert meta['result']['ids']['review_id'] is None
    assert meta['result']['ids']['batch_orchestration_id'] is None
    assert meta['result']['coverage']['partial'] is None


def test_final_recovery_persistence_failure_is_not_success(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    save = Store.save_review
    def save_except_terminal(store, rec, **kwargs):
        if rec.get('terminal_reason'):
            raise OSError('cannot publish terminal metadata')
        return save(store, rec, **kwargs)
    monkeypatch.setattr(Store, 'save_review', save_except_terminal)
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo, recover=True)
    assert code == 4
    assert meta['result']['execution']['reason_code'] == 'persistence_failed'
    assert meta['result']['ids']['review_id'] is None
    assert meta['result']['coverage']['trustworthy'] is None



def test_security_attempts_are_complete_and_legacy_omission_is_unknown(tmp_path, monkeypatch):
    from tests.test_pipeline import _calls
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '1')
    (repo / 'auth').mkdir()
    (repo / 'auth' / 'session.py').write_text('def authenticate(): return True\n')
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo)
        legacy = store.get_review(meta['result']['ids']['review_id'])
    result = meta['result']
    assert code == 0 and _calls(tmp_path) == 2
    assert result['counts']['provider_launches'] == 2
    assert result['counts']['complete'] is True
    assert result['missing_attempt_scopes'] == []
    # A historical record has the same outcome but lacks this observation.
    # Preserve the reader's unknown projection; never backfill invented calls.
    legacy['extra_passes']['security'].pop('attempts')
    from skodun.review_results import observation
    old = observation(legacy)
    assert old['launched_count'] is None and old['known_launched_count'] == 1
    assert old['counts_complete'] is False
    assert old['missing_attempt_scopes'] == [{'kind': 'extra_pass', 'id': 'security'}]



def test_replay_rejects_success_termination_on_failed_status():
    from skodun.review_results import valid_replay
    assert not valid_replay({'status': 4, 'text': 'failed', 'metadata': {
        'termination': {'reason_code': 'review_clean', 'state': 'completed'}}})


@pytest.mark.parametrize('change', ['moved', 'unreadable'])
def test_identity_recheck_failure_keeps_the_current_persisted_observation(tmp_path, monkeypatch, change):
    from skodun import pipeline
    repo = _ready_repo(tmp_path, monkeypatch)
    run = pipeline.run_review
    identity = services._recovery_identity
    if change == 'moved':
        def run_then_move(*args, **kwargs):
            rec = run(*args, **kwargs)
            (repo / 'a.txt').write_text('changed after publication\n')
            return rec
        monkeypatch.setattr(pipeline, 'run_review', run_then_move)
    else:
        calls = []
        def unreadable_after_first(root):
            calls.append(1)
            if len(calls) > 1:
                raise OSError('identity unavailable after publication')
            return identity(root)
        monkeypatch.setattr(services, '_recovery_identity', unreadable_after_first)
    with Store.open(tmp_path / 'db') as store:
        code, _, metadata = services.svc_review_detailed(store, repo, recover=True)
        result = metadata['result']
        assert code == 4
        assert result['ids']['review_id'] == metadata['recovery']['review_ids'][0]
        assert store.get_review(result['ids']['review_id']) is not None
    assert result['execution']['reason_code'] == ('identity_changed' if change == 'moved' else 'identity_unavailable')



def test_recovery_preserves_audited_lifecycle_signal(tmp_path, monkeypatch):
    from skodun import pipeline, runner
    from skodun.request_cancel import mark_event
    repo = _ready_repo(tmp_path, monkeypatch)
    def signal(*args, **kwargs):
        mark_event(kwargs['cancel'], 'signal')
        raise runner.ReviewCancelled('interrupted')
    monkeypatch.setattr(pipeline, 'run_review', signal)
    with Store.open(tmp_path / 'db') as store:
        code, _, meta = services.svc_review_detailed(store, repo, recover=True)
        request = store.get_request(meta['request']['id'])
        assert request['state'] == 'cancelled'
        assert request['reason_code'] == 'signal'
    assert code == 4
    assert meta['result']['execution']['reason_code'] == 'signal'
    assert meta['result']['execution']['state'] == 'cancelled'
