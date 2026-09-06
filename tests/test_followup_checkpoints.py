"""Required follow-ups continue through the shipped service and fenced store."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from skodun import checkpoints, runner, services
from skodun.store import SCHEMA_VERSION, Store
from tests.test_batched_review import _body
from tests.test_requests import _ready_repo

CLEAN = b'{"structuredOutput":{"summary":"ok","findings":[]},"stopReason":"EndTurn"}'

@pytest.fixture
def lane(tmp_path, monkeypatch):
    repo = _ready_repo(tmp_path, monkeypatch)
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '1')
    monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '1')
    config = repo / '.skodun.toml'
    config.write_text(config.read_text() + '\n[defaults]\nmax_diff_bytes=4000\ndegraded_retries=0\ntimeout_retries=0\n')
    (repo / 'auth').mkdir()
    for i in range(4):
        (repo / 'auth' / f'f{i}.txt').write_text(_body(f'f{i}'))
    calls, failures = [], set()
    def provider(cmd, timeout_sec, cwd, out, err, stdin_path=None, cancel=None):
        name = Path(cmd[cmd.index('--prompt-file') + 1]).name
        label = next((role for role in ('integration', 'security', 'skeptic', 'refuter')
                      if name.startswith(role + '.')), name.split('.')[1])
        calls.append(label)
        out.write_bytes(b'not a review' if label in failures else CLEAN)
        return runner.RunResult(rc=0, timed_out=False, duration_sec=.1, first_output_sec=.05)
    monkeypatch.setattr(runner, 'run_with_watchdog', provider)
    with Store.open(tmp_path / 'db') as store:
        yield repo, store, calls, failures

@pytest.mark.parametrize('failed, expected', [('security', ['security', 'skeptic']), ('skeptic', ['skeptic'])])
def test_required_failure_continues_only_missing_followups(lane, failed, expected):
    repo, store, calls, failures = lane
    failures.add(failed)
    code, message, original = services.svc_review_detailed(store, repo)
    assert code == 4
    source_id = original['result']['ids']['batch_orchestration_id']
    assert source_id, (message, original)
    source = store.get_orchestration(source_id)
    rows = store.list_checkpoints(source_id)
    assert {'security', 'skeptic'} <= {row['pass_kind'] for row in rows}
    from skodun.readmodel import project_review
    projection = project_review(store.get_review(source['final_review_id']), orchestration=source, checkpoints=rows)
    assert projection.passes[failed] == 'failed'
    assert projection.next_resumable_pass == source['batch_count'] + (2 if failed == 'security' else 3)
    calls.clear()
    failures.clear()
    code, _, continued = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0
    assert calls == expected
    assert store.get_orchestration(source_id) == source
    assert store.list_checkpoints(source_id) == rows
    actions = {row['kind']: row['action'] for row in continued['continuation']['passes']}
    assert actions['security'] == ('executed' if failed == 'security' else 'reused')
    assert actions['skeptic'] == 'executed'


def test_followup_pass_identity_has_exact_zero_index():
    for kind in ('security', 'skeptic'):
        assert checkpoints.PassIdentity(kind, 0, None, 'diff', 'policy').kind == kind
        with pytest.raises(ValueError):
            checkpoints.PassIdentity(kind, 1, None, 'diff', 'policy')


def test_followup_schema_is_additive_and_declared(lane):
    _, store, _, _ = lane
    assert store._c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    sql = store._c.execute("SELECT sql FROM sqlite_master WHERE name='review_checkpoints'").fetchone()[0]
    assert "('batch','integration')" in sql
    assert store._c.execute("SELECT sql FROM sqlite_master WHERE name='review_followup_checkpoints'").fetchone()


def _source(store, result):
    oid = result['result']['ids']['batch_orchestration_id']
    return oid, store.get_orchestration(oid), store.list_checkpoints(oid)


def test_changed_base_evidence_invalidates_completed_security(lane):
    repo, store, calls, failures = lane
    failures.add('b2')
    code, _, initial = services.svc_review_detailed(store, repo)
    assert code == 4
    oid, source, rows = _source(store, initial)
    security = next(r for r in rows if r['pass_kind'] == 'security')
    assert security['state'] == 'complete'
    calls.clear()
    failures.clear()
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0 and calls == ['b2', 'integration', 'security', 'skeptic']
    assert store.get_orchestration(oid) == source
    assert store.list_checkpoints(oid) == rows


def test_preparation_failure_is_durable_unusable_and_retried(lane, monkeypatch):
    from skodun import passes
    repo, store, calls, _ = lane
    real = passes.security_prompt
    def fail(*args, **kwargs):
        raise ValueError('fixture preparation failure')
    monkeypatch.setattr(passes, 'security_prompt', fail)
    code, _, result = services.svc_review_detailed(store, repo)
    assert code == 4 and 'security' not in calls
    oid, _, rows = _source(store, result)
    sec = next(r for r in rows if r['pass_kind'] == 'security')
    assert sec['state'] == 'complete'
    assert json.loads(sec['payload_json'])['attempts'] == []
    assert json.loads(sec['binding_json'])['decision']['reason'] == 'preparation_failed'
    monkeypatch.setattr(passes, 'security_prompt', real)
    calls.clear()
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0 and calls == ['security', 'skeptic']
    assert store.get_orchestration(oid)['state'] == 'consumed'


def test_cancel_after_security_completion_reuses_durable_result(lane, monkeypatch):
    import threading
    from skodun.request_cancel import mark_event
    repo, store, calls, _ = lane
    cancel = threading.Event()
    complete = store.complete_checkpoint
    def stop(orchestration_id, kind, index, **kwargs):
        applied = complete(orchestration_id, kind, index, **kwargs)
        if kind == 'security':
            mark_event(cancel, 'requested_cancel')
        return applied
    monkeypatch.setattr(store, 'complete_checkpoint', stop)
    code, _, initial = services.svc_review_detailed(store, repo, cancel=cancel)
    assert code == 4 and calls[-1] == 'security'
    monkeypatch.setattr(store, 'complete_checkpoint', complete)
    calls.clear()
    code, _, continued = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0 and calls == ['skeptic']


def test_global_policy_change_refuses_before_new_calls(lane, monkeypatch):
    repo, store, calls, failures = lane
    failures.add('skeptic')
    assert services.svc_review_detailed(store, repo)[0] == 4
    calls.clear()
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '0')
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code != 0 and calls == []
    assert result['continuation']['status'] == 'refused'
    assert result['continuation']['first_mismatch'] == 'policy_hash'


def test_truncated_extra_preserves_existing_success_and_reuse_policy(lane):
    from skodun import followups
    repo, store, calls, failures = lane
    failures.add('skeptic')
    code, _, initial = services.svc_review_detailed(store, repo)
    assert code == 4
    _, _, rows = _source(store, initial)
    sec = next(r for r in rows if r['pass_kind'] == 'security')
    payload = checkpoints.CheckpointPayload(sec['payload_json'])
    assert payload.as_dict()['diff_truncated'] is True
    assert followups.usable(payload) is True
    assert checkpoints.usable_payload(payload) is False
    calls.clear()
    failures.clear()
    code, _, _ = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0 and calls == ['skeptic']


def test_bound_skips_are_not_pending_required_work(lane, monkeypatch):
    from skodun.readmodel import project_review
    repo, store, _, _ = lane
    monkeypatch.setenv('SKODUN_SECURITY_PASS', '0')
    monkeypatch.setenv('SKODUN_SKEPTIC_PASS', '0')
    code, _, result = services.svc_review_detailed(store, repo)
    assert code == 0
    oid, orchestration, rows = _source(store, result)
    rec = store.get_review(orchestration['final_review_id'])
    projection = project_review(rec, orchestration=orchestration, checkpoints=rows)
    assert projection.passes['security'] == projection.passes['skeptic'] == 'not_planned'
    assert projection.next_resumable_pass is None
    assert projection.gate_eligible is True


@pytest.mark.parametrize('mutation', ['payload', 'binding', 'prompt'])
@pytest.mark.parametrize('kind', ['security', 'skeptic'])
def test_publication_rejects_changed_required_evidence(lane, monkeypatch, mutation, kind):
    repo, store, _, _ = lane
    original = store.save_checkpointed_review
    def corrupt(rec, **kwargs):
        oid = rec['batch_orchestration_id']
        row = next(r for r in store.list_checkpoints(oid) if r['pass_kind'] == kind)
        if mutation == 'payload':
            value = json.loads(row['payload_json'])
            value['summary'] = 'changed after downstream consumed security'
            store._c.execute('UPDATE review_followup_checkpoints SET payload_json=? WHERE orchestration_id=? AND pass_kind=?',
                             (json.dumps(value), oid, kind))
        else:
            field = 'binding_hash' if mutation == 'binding' else 'prompt_hash'
            store._c.execute(f'UPDATE review_followup_checkpoints SET {field}=? WHERE orchestration_id=? AND pass_kind=?',
                             ('wrong', oid, kind))
        return original(rec, **kwargs)
    monkeypatch.setattr(store, 'save_checkpointed_review', corrupt)
    code, _, _ = services.svc_review_detailed(store, repo)
    assert code == 4
    assert store._c.execute('SELECT COUNT(*) FROM reviews WHERE trustworthy=1').fetchone()[0] == 0


def test_expiry_clears_followup_payloads(lane):
    repo, store, _, failures = lane
    failures.add('skeptic')
    code, _, initial = services.svc_review_detailed(store, repo)
    assert code == 4
    oid, _, _ = _source(store, initial)
    store._c.execute("UPDATE review_orchestrations SET state='failed',expires_at='2026-01-01T00:00:00Z' WHERE id=?", (oid,))
    store.expire_orchestrations(now='2026-09-05T00:00:00Z')
    rows = store.list_checkpoints(oid)
    assert all(row['payload_json'] is None for row in rows)


def test_followup_claims_require_binding_and_reject_late_fence(lane, monkeypatch):
    from tests.test_checkpoints import _payload
    repo, store, _, _ = lane
    claim = store.claim_checkpoint
    checks = []
    def inspect(oid, identity, **kwargs):
        result = claim(oid, identity, **kwargs)
        if identity.kind == 'security' and result['decision'] == 'claimed':
            other = claim(oid, identity, **{**kwargs, 'owner': 'competing-owner'})
            assert other['decision'] == 'in_flight'
            from skodun.readmodel import project_review
            projection = project_review({}, orchestration=store.get_orchestration(oid), checkpoints=store.list_checkpoints(oid))
            assert projection.passes['security'] == 'running'
            assert projection.next_resumable_pass is None
            with pytest.raises(ValueError, match='binding'):
                claim(oid, identity, **{**kwargs, 'binding_hash': 'wrong'})
            with pytest.raises(ValueError, match='binding'):
                store.complete_checkpoint(oid, 'security', 0, owner=kwargs['owner'],
                    claim_token=result['claim_token'], fence=result['fence'], payload=checkpoints.CheckpointPayload.from_mapping(_payload()),
                    completed_at=kwargs['now'], binding_hash='wrong')
            assert store.complete_checkpoint(oid, 'security', 0, owner=kwargs['owner'],
                claim_token=result['claim_token'], fence=result['fence'] + 1, payload=checkpoints.CheckpointPayload.from_mapping(_payload()),
                completed_at=kwargs['now'], binding_hash=kwargs['binding_hash']) is False
            checks.append(True)
        return result
    monkeypatch.setattr(store, 'claim_checkpoint', inspect)
    assert services.svc_review_detailed(store, repo)[0] == 0
    assert checks == [True]


def test_candidate_is_not_complete_before_runtime_binding(lane, monkeypatch):
    repo, store, calls, failures = lane
    failures.add('skeptic')
    assert services.svc_review_detailed(store, repo)[0] == 4
    failures.clear()
    bind = store.bind_followup_checkpoint
    observed = []
    def inspect(oid, identity, **kwargs):
        if identity.kind == 'security':
            row = next(r for r in store.list_checkpoints(oid) if r['pass_kind'] == 'security')
            assert row['state'] == 'pending' and row['payload_json'] is None
            assert row['candidate_json'] is not None and row['binding_hash'] is None
            observed.append(True)
        return bind(oid, identity, **kwargs)
    monkeypatch.setattr(store, 'bind_followup_checkpoint', inspect)
    calls.clear()
    assert services.svc_review_detailed(store, repo, continue_compatible=True)[0] == 0
    assert observed == [True] and calls == ['skeptic']


def test_candidate_invalidation_reason_survives_execution(lane):
    repo, store, _, failures = lane
    failures.add('b2')
    assert services.svc_review_detailed(store, repo)[0] == 4
    failures.clear()
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0
    sec = next(r for r in result['continuation']['passes'] if r['kind'] == 'security')
    assert sec['reason'] == 'followup_upstream_changed'
    assert result['continuation']['first_mismatch'] is None


def test_followup_identity_includes_actual_upstream_provider_and_not_timing():
    from skodun.followups import semantic_payload
    from tests.test_checkpoints import _payload
    original = _payload()
    original['accepted'] = {'provider': 'xai', 'model': 'model', 'effort': None, 'adapter_name': 'xai'}
    before = semantic_payload(checkpoints.CheckpointPayload.from_mapping(original))
    original['provenance']['continuation_action'] = 'reused'
    original['provenance']['wall_duration_sec'] = 9.0
    assert semantic_payload(checkpoints.CheckpointPayload.from_mapping(original)) == before
    original['accepted']['provider'] = 'openai'
    assert semantic_payload(checkpoints.CheckpointPayload.from_mapping(original)) != before


def test_optional_refuter_unavailability_does_not_become_a_requirement(lane, monkeypatch):
    from skodun import pipeline
    repo, store, calls, _ = lane
    real = runner.run_with_watchdog
    dirty = b'{"structuredOutput":{"summary":"issue","findings":[{"file":"auth/f0.txt","line":1,"severity":"high","category":"bug","title":"Bad edge","detail":"The edge is not handled."}]},"stopReason":"EndTurn"}'
    def finder(*args, **kwargs):
        result = real(*args, **kwargs)
        name = Path(args[0][args[0].index('--prompt-file') + 1]).name
        if '.b1.' in name:
            args[3].write_bytes(dirty)
        return result
    monkeypatch.setattr(runner, 'run_with_watchdog', finder)
    # Route only the optional annotation boundary to its real failed merger.
    monkeypatch.setattr(pipeline.passes, 'refuter_decision', lambda *a, **kw: (True, ''))
    monkeypatch.setattr(pipeline, '_refuter_pass', lambda rec, n, *a, **kw:
                        pipeline._refuter_failed(rec, n, 'fixture unavailable'))
    code, _, result = services.svc_review_detailed(store, repo)
    assert code == 1
    _, orchestration, rows = _source(store, result)
    rec = store.get_review(orchestration['final_review_id'])
    assert rec['trustworthy'] is True
    assert rec['extra_passes']['refuter']['failed'] is True
    assert not any(r['pass_kind'] == 'refuter' for r in rows)


def test_v19_inspection_does_not_migrate_and_explicit_upgrade_is_additive(tmp_path):
    import sqlite3
    from contextlib import closing
    from skodun.store import inspect_schema, SchemaLifecycleError
    db = tmp_path / 'v19.db'
    with Store.open(db):
        pass
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('DROP TABLE review_followup_checkpoints')
        connection.execute('ALTER TABLE capacity_admissions DROP COLUMN owner_start')
        connection.execute('ALTER TABLE capacity_admissions DROP COLUMN capacity_limit')
        connection.execute('PRAGMA user_version=19')
        connection.commit()
        old_objects = set(connection.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    before = db.read_bytes()
    assert inspect_schema(db).version == 19
    with pytest.raises(SchemaLifecycleError):
        Store.open(db)
    assert db.read_bytes() == before
    receipt = Store.migrate_existing(db, build_commit='a' * 40)
    assert receipt['schema_from'] == 19 and receipt['schema_to'] == SCHEMA_VERSION
    with Store.open(db) as store:
        objects = set(tuple(r) for r in store._c.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        assert objects - old_objects == {('table', 'review_followup_checkpoints'),
                                         ('index', 'ix_followup_checkpoints_state')}


def test_racing_continuation_does_not_launch_live_followup_twice(lane, monkeypatch):
    import threading
    repo, store, calls, _ = lane
    entered, release = threading.Event(), threading.Event()
    real = runner.run_with_watchdog
    def paused(*args, **kwargs):
        name = Path(args[0][args[0].index('--prompt-file') + 1]).name
        if name.startswith('security.'):
            entered.set()
            assert release.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(runner, 'run_with_watchdog', paused)
    db = store._c.execute('PRAGMA database_list').fetchone()[2]
    outcomes = []
    def first():
        with Store.open(Path(db)) as owned:
            outcomes.append(services.svc_review_detailed(owned, repo)[0])
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(10)
        code, _, _ = services.svc_review_detailed(store, repo, continue_compatible=True)
        assert code != 0
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert outcomes == [0]
    assert calls.count('security') == 1 and calls.count('skeptic') == 1


def test_queue_counts_reused_base_integration_and_security_once(lane):
    repo, store, calls, failures = lane
    failures.add('skeptic')
    assert services.svc_review_detailed(store, repo)[0] == 4
    failures.clear()
    calls.clear()
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 0 and calls == ['skeptic']
    code, text = services.svc_queue(store, request_id=result['request']['id'], output='json')
    assert code == 0
    costs = json.loads(text)['requests'][0]['costs']
    assert costs['reused_passes'] == 6  # four batches, integration, security
    assert costs['reported_launched_calls'] == 8  # seven original calls + new skeptic


def test_degraded_required_checkpoint_is_next_but_capped_success_is_not(lane):
    from skodun.readmodel import project_review
    repo, store, _, failures = lane
    _, _, result = services.svc_review_detailed(store, repo)
    _, orchestration, rows = _source(store, result)
    rec = store.get_review(orchestration['final_review_id'])
    assert project_review(rec, orchestration=orchestration, checkpoints=rows).next_resumable_pass is None
    security = next(row for row in rows if row['pass_kind'] == 'security')
    payload = json.loads(security['payload_json'])
    assert payload['diff_truncated'] is True and payload['degraded'] is False
    payload.update(degraded=True, degraded_reason='fixture degraded response')
    security['payload_json'] = json.dumps(payload)
    projection = project_review(rec, orchestration=orchestration, checkpoints=rows)
    assert projection.passes['security'] == 'degraded'
    assert projection.next_resumable_pass == 6
    assert projection.gate_eligible is False


def test_request_provider_wait_keeps_base_and_required_claims_live(lane, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from skodun import budget, pipeline
    from skodun.config import load_config
    repo, store, calls, _ = lane
    monkeypatch.delenv('SKODUN_ADMISSION_WAIT_SECONDS', raising=False)
    path = repo / '.skodun.toml'
    path.write_text(path.read_text().replace('max_diff_bytes=4000', 'max_diff_bytes=4000\ntimeout_sec=1'))
    defaults = load_config(repo).defaults
    old_wait_expiry = budget.worst_runtime(defaults, 1, 0) + 30.0 + 1
    claim = store.claim_checkpoint
    db = Path(store._c.execute('PRAGMA database_list').fetchone()[2])
    observed = []
    def inspect(oid, identity, **kwargs):
        result = claim(oid, identity, **kwargs)
        if (identity.kind, identity.index) in (('batch', 1), ('security', 0)):
            now = datetime.strptime(kwargs['now'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            peer_now = (now + timedelta(seconds=old_wait_expiry)).strftime('%Y-%m-%dT%H:%M:%SZ')
            before_calls = list(calls)
            with Store.open(db) as peer:
                assert pipeline._checkpoint_lease_seconds(defaults, 1) == old_wait_expiry - 1
                assert pipeline._checkpoint_lease_seconds(defaults, 1, store=peer) == old_wait_expiry - 1
                other = peer.claim_checkpoint(oid, identity, **{**kwargs, 'owner': 'peer', 'now': peer_now,
                    'lease_expires_at': (now + timedelta(seconds=2000)).strftime('%Y-%m-%dT%H:%M:%SZ')})
            assert other['decision'] == 'in_flight'
            assert calls == before_calls
            observed.append(identity.kind)
        return result
    monkeypatch.setattr(store, 'claim_checkpoint', inspect)
    code, text, _ = services.svc_review_detailed(store, repo, max_provider_wait_seconds=1000)
    assert code == 0, text
    assert observed == ['batch', 'security']
    assert len(calls) == 7


def _valid_binding_fixture():
    return {'version': 'followup-input/v1', 'kind': 'security', 'content_hash': 'a' * 64,
        'aggregate_hash': 'b' * 64, 'decision': {'scheduled': True, 'required': True, 'reason': 'scheduled'},
        'prompt_identity': {'hash': 'c' * 40, 'bytes': 10, 'diff_truncated': False},
        'dependencies': [{'pass_kind': 'batch', 'pass_index': 1, 'diff_hash': 'd' * 40,
            'boundary_hash': 'e' * 64, 'prompt_hash': 'f' * 64, 'output_hash': '1' * 64,
            'binding_hash': None, 'provenance_known': True}]}


@pytest.mark.parametrize('field', ['diff_hash', 'boundary_hash', 'prompt_hash', 'output_hash', 'binding_hash'])
@pytest.mark.parametrize('value', [True, [], {}, 'not-a-digest'])
def test_persisted_followup_dependency_digests_are_strict(field, value):
    from skodun.followups import decode_binding
    body = _valid_binding_fixture()
    assert decode_binding(json.dumps(body)) == body
    body['dependencies'][0][field] = value
    with pytest.raises(ValueError):
        decode_binding(json.dumps(body))


@pytest.mark.parametrize('kind', ['security', 'skeptic'])
def test_migration_blockers_include_live_required_claims(lane, kind):
    from skodun.store import migration_blockers
    repo, store, _, _ = lane
    _, _, result = services.svc_review_detailed(store, repo)
    oid, _, _ = _source(store, result)
    db = Path(store._c.execute('PRAGMA database_list').fetchone()[2])
    store._c.execute("UPDATE review_followup_checkpoints SET state='running',lease_expires_at='2099-01-01T00:00:00Z' WHERE orchestration_id=? AND pass_kind=?", (oid, kind))
    before = db.read_bytes()
    assert 'active_checkpoint_claim' in migration_blockers(db)
    assert db.read_bytes() == before
    store._c.execute("UPDATE review_followup_checkpoints SET lease_expires_at='2020-01-01T00:00:00Z' WHERE orchestration_id=? AND pass_kind=?", (oid, kind))
    assert 'active_checkpoint_claim' not in migration_blockers(db)


def test_padded_foreground_mode_has_required_pass_identities(lane):
    from skodun import pipeline
    from skodun.config import load_config
    repo, store, calls, _ = lane
    rec = pipeline.run_review(repo, load_config(repo), store, mode=' now ')
    assert rec['mode'] == 'now' and rec['trustworthy'] is True
    assert calls[-2:] == ['security', 'skeptic']


@pytest.mark.parametrize('field, value', [('followup_output_hash', 'wrong'), ('parse_ok', False), ('provider', 'changed'), ('ran', False), ('failed', True)])
def test_final_followup_publication_validates_after_annotation(lane, monkeypatch, field, value):
    repo, store, _, _ = lane
    original = store.save_checkpointed_review
    replacement = value
    def annotate(rec, **kwargs):
        def corrupt(_store, value):
            value['extra_passes']['security'][field] = replacement
        return original(rec, lineage_annotator=corrupt)
    monkeypatch.setattr(store, 'save_checkpointed_review', annotate)
    assert services.svc_review_detailed(store, repo)[0] == 4
    assert store._c.execute('SELECT COUNT(*) FROM reviews WHERE trustworthy=1').fetchone()[0] == 0


def test_newly_unscheduled_candidate_retains_invalidation_without_a_call(lane, monkeypatch):
    import threading
    from skodun import passes
    from skodun.request_cancel import mark_event
    repo, store, calls, _ = lane
    cancel = threading.Event()
    complete = store.complete_checkpoint
    def cancel_after_skeptic(oid, kind, index, **kwargs):
        result = complete(oid, kind, index, **kwargs)
        if kind == 'skeptic':
            mark_event(cancel, 'requested_cancel')
        return result
    monkeypatch.setattr(store, 'complete_checkpoint', cancel_after_skeptic)
    assert services.svc_review_detailed(store, repo, cancel=cancel)[0] == 4
    monkeypatch.setattr(store, 'complete_checkpoint', complete)
    original_prompt = passes.security_prompt
    def revised_prompt(*args, **kwargs):
        prompt = original_prompt(*args, **kwargs)
        extra = b'\nupdated security prompt version\n'
        return replace(prompt, text=prompt.text + extra, prompt_bytes=prompt.prompt_bytes + len(extra))
    monkeypatch.setattr(passes, 'security_prompt', revised_prompt)
    original_runner = runner.run_with_watchdog
    def finding(*args, **kwargs):
        result = original_runner(*args, **kwargs)
        name = Path(args[0][args[0].index('--prompt-file') + 1]).name
        if name.startswith('security.'):
            args[3].write_bytes(b'{"structuredOutput":{"summary":"issue","findings":[{"file":"auth/f0.txt","line":1,"severity":"high","category":"bug","title":"Bad edge","detail":"The edge is not handled."}]},"stopReason":"EndTurn"}')
        return result
    monkeypatch.setattr(runner, 'run_with_watchdog', finding)
    calls.clear()
    code, _, result = services.svc_review_detailed(store, repo, continue_compatible=True)
    assert code == 1 and calls == ['security']
    assert result['continuation']['skipped_passes'] == [
        {'kind': 'skeptic', 'index': 0, 'reason': 'followup_upstream_changed'}]
    assert result['continuation']['counts'] == {'reused': 5, 'executed': 1, 'failed': 0}


@pytest.mark.parametrize('mutation', ['duplicate', 'executed_too', 'bad_reason', 'boolean_index', 'unknown_field'])
def test_skipped_invalidation_receipt_remains_bounded_and_closed(mutation):
    from skodun.continuation import valid_receipt
    value = {'policy': 'compatible', 'status': 'continued', 'source_orchestration_id': 'source',
        'orchestration_id': 'child', 'first_mismatch': None, 'passes': [],
        'counts': {'reused': 0, 'executed': 0, 'failed': 0}, 'passes_truncated': False,
        'skipped_passes': [{'kind': 'skeptic', 'index': 0, 'reason': 'followup_upstream_changed'}]}
    assert valid_receipt(value)
    if mutation == 'duplicate':
        value['skipped_passes'] *= 2
    elif mutation == 'executed_too':
        value['passes'] = [{'kind': 'skeptic', 'index': 0, 'action': 'executed'}]
        value['counts']['executed'] = 1
    elif mutation == 'bad_reason':
        value['skipped_passes'][0]['reason'] = 'arbitrary'
    elif mutation == 'boolean_index':
        value['skipped_passes'][0]['index'] = False
    else:
        value['skipped_passes'][0]['executed'] = False
    assert valid_receipt(value) is False
