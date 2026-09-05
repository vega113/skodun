"""Shipped worktree scope and execution-fenced cancellation/control contracts."""

import json
import os
import threading

import pytest

from skodun import services, requests, capacity
from skodun.cli import main
from skodun.control import scope_identity
from skodun.mcpserver import HandlerCall, default_registry
from skodun.request_cancel import RequestCancel, mark_event
from skodun.store import Store
from tests.test_gitio import _mkrepo, _git
from tests.test_cli import _artifact, _finding

NOW = '2026-09-05T10:00:00Z'


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv('SKODUN_CONFIG',str(tmp_path/'absent'))
    monkeypatch.setenv('SKODUN_DB',str(tmp_path/'s.db'))
    monkeypatch.setenv('SKODUN_GROK_BIN',str(tmp_path/'missing'))


def begin(store, repo, rid, owner='owner'):
    identity = requests.snapshot(repo)
    return store.begin_request(request_id=rid, scope=identity['worktree_root'],
        request_key=None, identity=identity,intent={},owner_token=owner,pid=os.getpid(),
        source='test',actor='claimed client',now=NOW,expires_at='2026-09-06T10:00:00Z')[1]


def tool(db, name, **params):
    spec = next(s for s in default_registry() if s.name == name)
    return spec.handler(HandlerCall(params=params,store_factory=lambda:Store.open(db),
                       cancel=threading.Event(),client_name='claimed MCP client'))


def test_four_worktree_status_is_local_and_broader_views_are_explicit(tmp_path, monkeypatch, capsys):
    repo = _mkrepo(tmp_path)
    trees = [repo]
    for n in range(3):
        tree=tmp_path/f'lane{n}'
        _git(repo,'worktree','add','-b',f'lane{n}',str(tree))
        trees.append(tree)
    db=tmp_path/'s.db'
    with Store.open(db) as store:
        for n,tree in enumerate(trees):
            begin(store,tree,f'sk_req_{n}',f'owner-{n}')
    for n,tree in enumerate(trees):
        monkeypatch.chdir(tree)
        assert main(['review-status','--json']) == 0
        assert json.loads(capsys.readouterr().out)['request']['id'] == f'sk_req_{n}'
        response=tool(db,'review_status',repo=str(tree),output='json')
        assert response.status == 0
        assert json.loads(response.text)['request']['id'] == f'sk_req_{n}'
    for scope in ('repository','host'):
        response=tool(db,'review_status',repo=str(repo),scope=scope,output='json')
        assert len(json.loads(response.text)['entries']) == 4
    with Store.open(db) as store:
        begin(store,repo,'sk_req_ambiguous','ambiguous-owner')
        code,text=services.svc_review_status(store,repo=repo,output='json')
        assert code == 2 and json.loads(text)['reason_code'] == 'ambiguous_worktree_activity'


def test_empty_or_unresolvable_worktree_never_borrows_another_lane(tmp_path):
    repo=_mkrepo(tmp_path); other=tmp_path/'other'
    _git(repo,'worktree','add','-b','other',str(other))
    with Store.open(tmp_path/'s.db') as store:
        begin(store,repo,'sk_req_other')
        assert services.svc_review_status(store,repo=other)[0] == 2
        assert services.svc_review_status(store,repo=tmp_path/'missing')[0] == 2
        assert services.svc_review_status(store,review_id='sk_req_other',repo=other)[0] == 0


def test_guards_refuse_request_cancel_before_audit_or_signal(tmp_path, monkeypatch):
    repo=_mkrepo(tmp_path)
    monkeypatch.setattr(os,'kill',lambda *a:pytest.fail('must not signal'))
    with Store.open(tmp_path/'s.db') as store:
        begin(store,repo,'sk_req_1')
        for guards in ({'expected_request_id':'other'}, {'expected_worktree':str(tmp_path/'other')},
                       {'expected_head':'stale'}, {'expected_diff_hash':'stale'}):
            assert services.svc_review_cancel(store,'sk_req_1',**guards)[0] == 2
            assert store.cancellation_events('sk_req_1') == []


@pytest.mark.parametrize('name,args',[
    ('svc_triage_dismiss',(0,'A concrete audited reason for this finding')),
    ('svc_adopt_refuter',(0,)),
    ('svc_triage_reopen',(0,'A concrete audited reason for reopening it')),
    ('svc_triage_defer',(0,'https://example.org/issue/1','A concrete audited reason for deferral'))])
def test_all_triage_services_check_identity_before_mutation(tmp_path,name,args):
    with Store.open(tmp_path/'s.db') as store:
        art=_artifact([_finding(0)])
        store.save_review(art)
        code,text=getattr(services,name)(store,art['id'],*args,expected_head='stale')
        assert code == 2 and 'expected_identity_mismatch' in text
        assert store.triage_for(art['branch'],art['base_sha']) == {}


def test_cancel_is_execution_fenced_and_does_not_cancel_another_request(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        one=begin(store,repo,'sk_req_1'); two=begin(store,repo,'sk_req_2','second-owner')
        token=RequestCancel(store,requests.RequestContext(one['id'],store,one['identity'],'owner'))
        other=RequestCancel(store,requests.RequestContext(two['id'],store,two['identity'],'second-owner'))
        code,text=services.svc_review_cancel(store,one['id'],actor='claimed client',output='json')
        assert code == 0 and json.loads(text)['reason_code'] == 'requested_cancel'
        assert token.is_set() and not other.is_set()
        assert token.reason_code == 'requested_cancel'
        stale=RequestCancel(store,requests.RequestContext(one['id'],store,one['identity'],'old-owner'))
        assert not stale.is_set()
        store.finish_request(one['id'],owner_token='owner',state='cancelled',
            reason_code=token.reason_code,result=None,now=NOW)
        event=store.cancellation_events(one['id'])[0]
        assert event['actor'] == 'claimed client' and event['outcome'] == 'cancelled'
        assert 'execution_token' not in event


@pytest.mark.parametrize('cause',['signal','disconnect','disconnect_deadline','recovery_deadline','unknown_cancel_token'])
def test_lifecycle_event_records_known_cause_without_inventing_actor(tmp_path,cause):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        row=begin(store,repo,'sk_req_1')
        upstream=threading.Event()
        token=RequestCancel(store,requests.RequestContext(row['id'],store,row['identity'],'owner'),upstream)
        mark_event(upstream,cause)
        assert token.is_set()
        assert token.reason_code == cause
        event=store.cancellation_events(row['id'])[0]
        assert event['actor'] == 'unknown' and event['source'] == 'lifecycle'
        assert event['cause'] == cause


def test_cancel_finalization_race_preserves_completed_result(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        row=begin(store,repo,'sk_req_1')
        assert services.svc_review_cancel(store,row['id'])[0] == 0
        store.finish_request(row['id'],owner_token='owner',state='finished',reason_code='completed',
            result={'status':0,'text':'complete','metadata':{}},now=NOW)
        assert store.get_request(row['id'])['result']['status'] == 0
        assert store.cancellation_events(row['id'])[0]['outcome'] == 'completed_before_cancel'
        assert services.svc_review_cancel(store,row['id'])[0] == 2


def test_claimed_mcp_actor_and_cli_guard_parity(tmp_path,monkeypatch,capsys):
    repo=_mkrepo(tmp_path); db=tmp_path/'s.db'
    with Store.open(db) as store: begin(store,repo,'sk_req_1')
    assert main(['review-cancel','sk_req_1','--expected-head','stale']) == 2
    text=capsys.readouterr().out.strip()
    result=tool(db,'review_cancel',review_id='sk_req_1',expected_head='stale')
    assert result.status == 2 and result.text == text
    result=tool(db,'review_cancel',review_id='sk_req_1',output='json')
    assert json.loads(result.text)['cancellation'][0]['actor'] == 'claimed MCP client'


@pytest.mark.parametrize('stage',['queued','running'])
def test_shipped_review_observes_request_cancellation_at_lifecycle_stage(tmp_path,monkeypatch,stage):
    from skodun import pipeline
    from tests.test_requests import _ready_repo
    repo=_ready_repo(tmp_path,monkeypatch); db=tmp_path/'s.db'; seen=[]
    with Store.open(db) as store:
        other=begin(store,repo,'sk_req_unrelated','unrelated')

    def cancel_current(store):
        ctx=requests.current()
        if ctx is None: return
        with Store.open(db) as peer:
            row=peer.get_request(ctx.id)
            seen.append(row['state'])
            assert services.svc_review_cancel(peer,ctx.id,actor='test operator')[0] == 0

    if stage == 'queued':
        real=capacity.enqueue
        def enqueue(store,*args,**kwargs):
            result=real(store,*args,**kwargs)
            if kwargs.get('resource_class',capacity.RESOURCE_REVIEW_FG) == capacity.RESOURCE_REVIEW_FG:
                cancel_current(store)
            return result
        monkeypatch.setattr(capacity,'enqueue',enqueue)
    else:
        real=pipeline._run_chain
        def run(*args,**kwargs):
            cancel_current(args[5])
            return real(*args,**kwargs)
        monkeypatch.setattr(pipeline,'_run_chain',run)
    with Store.open(db) as store:
        code,text,meta=services.svc_review_detailed(store,repo)
        row=store.get_request(meta['request']['id'])
        assert code not in (0,1)
        assert seen == [stage]
        assert row['state'] == 'cancelled'
        assert row['reason_code'] == 'requested_cancel'
        assert row['cancellation'][0]['outcome'] == 'cancelled'
        assert store.get_request(other['id'])['state'] == 'accepted'
        assert store.cancellation_events(other['id']) == []
        assert not any(r.get('trustworthy') for r in store.control_reviews())


def test_audit_persistence_failure_prevents_any_cancel_signal(tmp_path,monkeypatch):
    from skodun import pipeline
    with Store.open(tmp_path/'s.db') as store:
        art=_artifact([],status='running',pid=os.getpid(),findings_total=0)
        store.save_review(art)
        def fail(**kwargs): raise OSError('injected audit failure')
        monkeypatch.setattr(store,'record_cancellation',fail)
        monkeypatch.setattr(pipeline,'request_cancel',lambda *args:pytest.fail('must not signal'))
        assert services.svc_review_cancel(store,art['id'])[0] == 2
        assert store.get_review(art['id'])['status'] == 'running'


def test_legacy_unfinished_records_do_not_invent_actor_or_cleanup(tmp_path):
    from skodun import pipeline
    with Store.open(tmp_path/'s.db') as store:
        for n,reason in enumerate((pipeline.UNFINISHED_REASON,pipeline.UNFINISHED_CANCEL_REASON)):
            art=_artifact([],id=f'legacy{n}',status='failed',findings_total=0,
                failure_reason=reason,parse_ok=False,trustworthy=False)
            store.save_review(art)
            before=store.get_review(art['id'])
            code,text=services.svc_review_status(store,art['id'],output='json')
            assert code == 0
            lifecycle=json.loads(text)['lifecycle']
            assert lifecycle['reason_code'] == 'unknown'
            assert lifecycle['attribution'] == 'unattributed'
            assert store.get_review(art['id']) == before


def test_actor_credential_claim_is_refused_without_recording_value(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        row=begin(store,repo,'sk_req_1')
        secret='Bearer example-sensitive-credential'
        code,text=services.svc_review_cancel(store,row['id'],actor=secret)
        assert code == 2 and secret not in text
        assert store.cancellation_events(row['id']) == []


def test_status_limit_cannot_hide_ambiguous_active_requests(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        begin(store,repo,'sk_req_1','one'); begin(store,repo,'sk_req_2','two')
        code,text=services.svc_review_status(store,repo=repo,limit=1,output='json')
        assert code == 2 and json.loads(text)['reason_code'] == 'ambiguous_worktree_activity'


def test_dead_request_owner_is_read_only_process_loss_observation(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        row=begin(store,repo,'sk_req_1')
        store._c.execute('UPDATE review_requests SET pid=? WHERE id=?',(2**30,row['id']))
        code,text=services.svc_review_status(store,row['id'],output='json')
        assert code == 0
        assert json.loads(text)['request']['lifecycle'] == {
            'reason_code':'process_loss','attribution':'observed_pid_absent'}
        assert store.get_request(row['id'])['state'] == 'accepted'
        assert store.cancellation_events(row['id']) == []


def test_request_readback_failure_does_not_erase_committed_result(tmp_path,monkeypatch):
    repo=_mkrepo(tmp_path)
    monkeypatch.setattr(services,'_svc_review_once',lambda *a,**kw:(2,'refused'))
    with Store.open(tmp_path/'s.db') as store:
        real=store.finish_request
        def lost_reply(*args,**kwargs):
            result=real(*args,**kwargs)
            if result: raise OSError('injected readback loss after commit')
            return result
        monkeypatch.setattr(store,'finish_request',lost_reply)
        code,text,meta=services.svc_review_detailed(store,repo)
        assert code == 4
        row=store.get_request(meta['request']['id'])
        assert row['state'] == 'finished' and row['result']['status'] == 2
        assert row['cancellation'] == []


@pytest.mark.parametrize('reason_code',['queue_budget_exhausted','review_budget_exhausted','total_budget_exhausted'])
def test_explicit_budget_termination_is_preserved_at_request_finalization(tmp_path,monkeypatch,reason_code):
    repo=_mkrepo(tmp_path)
    # Exercise the request wrapper around the detailed service implementation,
    # the exact seam used by the budget/results slices.
    def expired(*args,**kwargs):
        return 3,'expired',{'termination':{'reason_code':reason_code,'state':'expired'}}
    with Store.open(tmp_path/'s.db') as store:
        code,text,meta=requests.tracked_review(expired)(store,repo)
        row=store.get_request(meta['request']['id'])
        assert row['state'] == 'expired' and row['reason_code'] == reason_code
        assert meta['termination']['state'] == 'expired'


def test_stale_execution_event_cannot_audit_cancellation_of_new_owner(tmp_path):
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        row=begin(store,repo,'sk_req_1')
        upstream=threading.Event()
        token=RequestCancel(store,requests.RequestContext(row['id'],store,row['identity'],'owner'),upstream)
        store._c.execute('UPDATE review_requests SET owner_token=? WHERE id=?',('replacement',row['id']))
        mark_event(upstream,'signal')
        assert token.is_set() and token.reason_code == 'request_ownership_lost'
        assert store.cancellation_events(row['id']) == []


def test_legacy_live_foreground_pid_is_not_proof_of_target_ownership(tmp_path,monkeypatch):
    from skodun import pipeline, dispatch
    with Store.open(tmp_path/'s.db') as store:
        rec=_artifact([],status='running',pid=os.getpid(),findings_total=0)
        store.save_review(rec)
        before=store.get_review(rec['id'])
        monkeypatch.setattr(pipeline,'request_cancel',lambda rid:False)
        monkeypatch.setattr(dispatch,'pid_is_skodun_worker',lambda *a:False)
        monkeypatch.setattr(services,'_pid_is_live_skodun_fg',lambda pid:True)
        monkeypatch.setattr(services,'_pid_alive',lambda pid:True)
        monkeypatch.setattr(os,'kill',lambda *a:pytest.fail('must not signal unrelated live process'))
        code,text=services.svc_review_cancel(store,rec['id'])
        assert code == 2 and 'legacy_owner_unproven' in text
        assert store.get_review(rec['id']) == before
        assert store.cancellation_events(rec['id'])[0]['outcome'] == 'refused_unproven_owner'


def test_old_review_id_cannot_cancel_another_request_on_the_same_mcp_pid(tmp_path,monkeypatch):
    from skodun import dispatch, pipeline
    repo=_mkrepo(tmp_path)
    with Store.open(tmp_path/'s.db') as store:
        current=begin(store,repo,'sk_req_current')
        stale=_artifact([],id='stale-review',status='running',pid=os.getpid(),findings_total=0)
        store.save_review(stale)
        unrelated=threading.Event()
        pipeline.register_cancel('actual-active-review',unrelated)
        try:
            monkeypatch.setattr(dispatch,'pid_is_skodun_worker',lambda *args:False)
            monkeypatch.setattr(services,'_pid_alive',lambda pid:True)
            monkeypatch.setattr(os,'kill',lambda *args:pytest.fail('must not signal MCP PID'))
            code,text=services.svc_review_cancel(store,stale['id'])
            assert code == 2 and 'legacy_owner_unproven' in text
            assert not unrelated.is_set()
            assert store.get_request(current['id'])['state'] == 'accepted'
            assert store.cancellation_events(current['id']) == []
            assert store.get_review(stale['id'])['status'] == 'running'
        finally:
            pipeline.unregister_cancel('actual-active-review')
