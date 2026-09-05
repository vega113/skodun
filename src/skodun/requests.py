"""Execution-local request identity; no request is evidence of review coverage.

The service owns request lifetime before readiness. Context is thread/task
local and links writes on the same Store only. Idempotency observes one exact
request; it never reacquires an active request's ownership or gate authority.
"""

from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from . import ids

_CURRENT = ContextVar('skodun_request', default=None)


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def current():
    return _CURRENT.get()


@dataclass(frozen=True)
class RequestContext:
    id: str
    store: object
    identity: dict
    owner_token: str
    stack_request: object = None
    config: object = None
    execution_seq: int | None = None
    budget: object = None
    continue_compatible: bool = False


def _config_hash(cfg):
    return hashlib.sha256(json.dumps(asdict(cfg), sort_keys=True, default=str).encode()).hexdigest()


def _policy_hash():
    return hashlib.sha256(json.dumps(
        {k: v for k, v in os.environ.items() if k.startswith('SKODUN_')},
        sort_keys=True).encode()).hexdigest()


def snapshot(repo, *, config_sink=None, batch_target_bytes=None):
    from . import config, gitio
    result = {'worktree_root': str(Path(repo).resolve()), 'repo_id': None,
              'canonical_repository': None, 'branch': None, 'head': None,
              'base_ref': None, 'base_sha': None, 'diff_hash': None,
              'tree_fingerprint': None, 'config_hash': None, 'capture_error': None}
    # Hash effective process overrides without persisting their names/values.
    result['policy_hash'] = _policy_hash()
    try:
        root = gitio._worktree_root(Path(repo)).resolve()
        result.update(worktree_root=str(root), repo_id=str(gitio.git_common_dir(root)),
                      canonical_repository=gitio.canonical_repository_identity(root),
                      branch=gitio.current_branch(root), head=gitio.head_sha(root))
        cfg = config.load_config(root)
        if batch_target_bytes:
            cfg = replace(cfg, defaults=replace(cfg.defaults, batch_target_bytes=batch_target_bytes))
        if config_sink is not None:
            config_sink['config'] = cfg
        result['config_hash'] = _config_hash(cfg)
        base = gitio.resolve_base(root)
        diff = gitio.capture_diff(root, base.sha, cfg.defaults.untracked_max)
        result.update(base_ref=base.ref, base_sha=base.sha,
                      diff_hash=gitio.diff_identity(diff.data),
                      tree_fingerprint=gitio.tree_fingerprint(root, paths=diff.files))
    except Exception as exc:
        result['capture_error'] = type(exc).__name__
    return result


def bind_review(store, rec):
    ctx = current()
    if ctx is None or ctx.store is not store:
        # Dispatch already has a durable exact-ref reservation before worker
        # spawn. That ID is its request identity; do not create a competing
        # reservation or alter prepush deduplication.
        if rec.get('mode') == 'prepush':
            rec.setdefault('request_id', rec['id'])
        return
    rec['request_id'] = ctx.id
    if ctx.execution_seq is not None:
        rec['request_execution_seq'] = ctx.execution_seq
    for kind, target in (('review', rec['id']),
                         ('recovery_orchestration', rec.get('orchestration_id')),
                         ('batch_orchestration', rec.get('batch_orchestration_id'))):
        if target:
            store.link_request(ctx.id, kind, target)


def link_capacity(store, admission_id, resource_class):
    ctx = current()
    if ctx is not None and ctx.store is store:
        store.link_request(ctx.id, 'capacity', admission_id)
        if resource_class == 'review-fg':
            if not store.advance_request(ctx.id, owner_token=ctx.owner_token,
                                         state='queued', now=now()):
                raise RuntimeError('request ownership lost before queue admission')


def validate_admitted(store, *, repo_id, worktree_root, branch, head, base_sha,
                       diff_hash, tree_fingerprint, cfg):
    ctx = current()
    if ctx is None or ctx.store is not store:
        return
    from .pipeline import PreflightRefused
    observed = locals()
    expected = ctx.identity
    if expected.get('capture_error'):
        raise PreflightRefused('request identity could not be captured before admission')
    for field in ('repo_id', 'worktree_root', 'branch', 'head', 'base_sha',
                  'diff_hash', 'tree_fingerprint'):
        if expected.get(field) != observed[field]:
            raise PreflightRefused(f'request identity changed while queued: {field}')
    from . import gitio
    actual = {'canonical_repository': gitio.canonical_repository_identity(Path(worktree_root)),
              'config_hash': _config_hash(cfg), 'policy_hash': _policy_hash()}
    for field, value in actual.items():
        if expected.get(field) != value:
            raise PreflightRefused(f'request identity changed while queued: {field}')
    if not store.advance_request(ctx.id, owner_token=ctx.owner_token,
                                 state='running', now=now()):
        raise PreflightRefused('request ownership lost while queued')


def projection(row):
    """Public request read model: never expose the internal ownership token."""
    from .control import request_lifecycle
    return {'lifecycle':request_lifecycle(row), **{key: row.get(key) for key in (
        'id', 'scope', 'identity', 'source', 'pid', 'state', 'created_at',
        'updated_at', 'expires_at', 'reason_code', 'links', 'executions',
        'executions_truncated', 'result', 'actor', 'cancellation')}}


def tracked_review(fn):
    """Wrap the shared detailed service while preserving its public signature."""
    @wraps(fn)
    def run(store, repo, *, request_key=None, request_source='service', request_actor=None,
            budget_limits=None, **kwargs):
        from .trust import banner_failure
        rid = ids.new_review_id('sk_req_')
        config_sink = {}
        identity = snapshot(repo, config_sink=config_sink,
                            batch_target_bytes=kwargs.get('batch_target_bytes'))
        intent = {k: v for k, v in kwargs.items()
                  if k not in ('cancel', 'progress_sink', 'reuse_client_family')}
        if budget_limits is not None:
            intent['budgets'] = budget_limits.to_dict()
        from .services import _REUSE_INTENT_UNSET
        family_intent = kwargs.get('reuse_client_family', _REUSE_INTENT_UNSET)
        intent['reuse_client_family'] = (
            '<unspecified>' if family_intent is _REUSE_INTENT_UNSET else family_intent)
        stack_request = None
        if kwargs.get('stack_manifest') is not None:
            from .stack import load_request
            stack_request = load_request(kwargs['stack_manifest'])
            manifest = getattr(stack_request, 'manifest', None)
            intent['stack_manifest_digest'] = (
                getattr(manifest, 'manifest_digest', None)
                or getattr(stack_request, 'claimed_manifest_digest', None))
            intent['stack_manifest_state'] = ('valid' if manifest is not None else
                getattr(getattr(stack_request, 'problem', None), 'reason_code', 'unknown'))
        # Persist only a hash of intent. Paths/values must not become telemetry.
        intent = json.loads(json.dumps(intent, sort_keys=True, default=str))
        coverage_intent = {k: intent.get(k) for k in (
            'reviewer', 'client_family', 'reuse_client_family', 'batch_target_bytes',
            'stack_manifest_digest', 'stack_manifest_state')}
        identity['coverage_intent_hash'] = hashlib.sha256(json.dumps(
            coverage_intent, sort_keys=True).encode()).hexdigest()
        owner = uuid.uuid4().hex
        metadata = {'id': rid, 'identity': identity, 'replayed': False}
        try:
            continuation_id = None
            candidate = None
            resume_family = (kwargs.get('client_family')
                             if family_intent is _REUSE_INTENT_UNSET else family_intent)
            compatible = kwargs.get('continue_compatible', False)
            if (request_key is None and not identity['capture_error'] and
                    (compatible or (not kwargs.get('recover') and not kwargs.get('fresh')
                     and kwargs.get('reviewer') is None and resume_family is None))):
                candidate = store.find_resume_candidate(
                    identity['repo_id'], identity['worktree_root'], identity['branch'],
                    include_consumed=compatible)
                if candidate is not None:
                    continuation_id = store.request_for_orchestration(candidate['id'], identity)
            decision, row = store.begin_request(
                request_id=rid, scope=identity['worktree_root'],
                request_key=request_key, identity=identity, intent=intent,
                owner_token=owner, pid=os.getpid(), source=request_source, now=now(),
                actor=request_actor, allow_consumed=compatible,
                continuation_id=continuation_id,
                continuation_orchestration_id=(candidate['id'] if candidate is not None else None),
                expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime(
                    '%Y-%m-%dT%H:%M:%SZ'))
        except (ValueError, TypeError) as exc:
            return 2, banner_failure(str(exc)), {'request': {
                **metadata, 'reason_code': 'request_invalid', 'persisted': False}}
        except Exception as exc:
            return 4, banner_failure('could not persist review request'), {'request': {
                **metadata, 'reason_code': 'request_persistence_failed', 'persisted': False,
                'error_type': type(exc).__name__}}
        rid = row['id']
        metadata.update(id=rid, identity=row['identity'], persisted=True,
                        continued=decision == 'continued',
                        execution_seq=row['executions'][0]['seq'] if row['executions'] else None)
        # Even a duplicate/mismatch must tell a plain CLI caller which durable
        # request to inspect; svc_review intentionally discards metadata.
        sink = kwargs.get('progress_sink')
        note = f'SKODUN REQUEST: id={rid}'
        try:
            if sink is not None:
                sink(note)
            else:
                print(note, file=sys.stderr, flush=True)
        except BaseException as exc:
            if decision in ('created', 'continued'):
                store.finish_request(
                    rid, owner_token=owner,
                    state='cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed',
                    reason_code='interrupted' if isinstance(exc, KeyboardInterrupt) else 'progress_failed',
                    result=None, now=now())
            if isinstance(exc, KeyboardInterrupt):
                raise
            return 4, banner_failure('request progress could not be delivered'), {
                'request': {**metadata, 'reason_code': 'progress_failed'}}
        if decision == 'mismatch':
            return 2, banner_failure('request key belongs to different identity or intent'), {
                'request': {**metadata, 'reason_code': 'request_identity_mismatch'}}
        if decision == 'continuation_unavailable':
            return 3, banner_failure('checkpoint continuation changed; observe its request ID'), {
                'request': {**metadata, 'reason_code': 'request_continuation_unavailable'}}
        if decision == 'existing':
            result = row['result']
            if result is not None:
                from .review_results import valid_replay
                if not valid_replay(result):
                    return 4, banner_failure('stored request result is invalid'), {
                        'request': {**metadata, 'reason_code': 'request_result_invalid'}}
                return result['status'], result['text'], {
                    **result['metadata'], 'request': {**metadata, 'replayed': True}}
            active = row['state'] in ('accepted', 'queued', 'running')
            reason = 'request_in_flight' if active else 'request_incomplete'
            return (3 if active else 4), banner_failure(
                'request already in flight; observe its request ID' if active else
                'request ended without a complete result; use a new request key'), {
                    'request': {**metadata, 'reason_code': reason}}
        context = RequestContext(rid, store, identity, owner, stack_request,
                                 config_sink.get('config'), metadata['execution_seq'],
                                 continue_compatible=compatible)
        token = _CURRENT.set(context)
        from .request_cancel import RequestCancel
        cancel = RequestCancel(store, context, kwargs.get('cancel'))
        from .budgets import ReviewBudget
        from dataclasses import replace
        controller = None
        if budget_limits is not None:
            def persist_budget(value):
                active = current()
                if active is None or active.budget is not controller:
                    raise RuntimeError('budget observation has no current execution')
                if not active.store.save_request_budget(
                        active.id, active.execution_seq, active.owner_token, value):
                    raise RuntimeError('budget observation lost its execution owner')
            controller = ReviewBudget(budget_limits, cancel=cancel, on_update=persist_budget)
            context = replace(context, budget=controller)
            _CURRENT.set(context)
            cancel = controller
        kwargs['cancel'] = cancel
        try:
            if controller is not None:
                controller._update()
            status, text, extra = fn(store, repo, **kwargs)
            if controller is not None:
                controller.finish()
                extra = {**extra, 'timing': {'scope': 'request_execution',
                                           **controller.snapshot()['timing']}}
            reused = (extra.get('reuse') or {}).get('review_id')
            if reused:
                store.link_request(rid, 'review', reused)
            for kind, key in (('recovery_orchestration', 'orchestration_id'),):
                target = (extra.get('recovery') or {}).get(key)
                if target:
                    store.link_request(rid, kind, target)
            termination = extra.get('termination') or {}
            observed_cause = cancel.reason_code
            cause = (observed_cause if observed_cause in (
                'queue_budget_exhausted', 'review_budget_exhausted', 'total_budget_exhausted')
                else termination.get('reason_code') or observed_cause)
            expired = termination.get('state') == 'expired' or cause in (
                'queue_budget_exhausted','review_budget_exhausted','total_budget_exhausted')
            cancelled = bool(cancel.reason_code) and status not in (0, 1)
            state = 'expired' if expired else 'cancelled' if cancelled else 'finished'
            if expired or cancelled:
                extra = {**extra, 'termination': {**termination, 'reason_code': cause,
                         'state': state, 'retryable': False, 'continuable': False}}
                metadata['reason_code'] = cause
            extra = {**extra, 'request': metadata}
            if not store.finish_request(
                    rid, owner_token=owner, state=state,
                    reason_code=cause or 'completed',
                    result={'status': status, 'text': text, 'metadata': extra}, now=now()):
                raise RuntimeError('request ownership lost before completion')
            return status, text, extra
        except BaseException as exc:
            if controller is not None:
                try:
                    controller.finish()
                except Exception:
                    pass
            # Advisory snapshot failure must not skip authoritative execution
            # finalization. Use already observed cause here: another failing
            # Store poll cannot be allowed to prevent this best-effort write.
            cause = controller._reason if controller is not None else cancel.reason_code
            try:
                store.finish_request(
                    rid, owner_token=owner,
                    state=('expired' if cause in ('queue_budget_exhausted',
                        'review_budget_exhausted', 'total_budget_exhausted') else
                        'cancelled' if cause or isinstance(exc, KeyboardInterrupt) else 'failed'),
                    reason_code=cause or ('signal' if isinstance(exc, KeyboardInterrupt) else 'request_failed'),
                    result=None, now=now())
            except Exception:
                pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            return 4, banner_failure('review request did not finish'), {
                'termination': {'reason_code': cause or 'request_failed'},
                'request': {**metadata, 'reason_code': cause or 'request_failed',
                            'error_type': type(exc).__name__}}
        finally:
            _CURRENT.reset(token)
    return run
