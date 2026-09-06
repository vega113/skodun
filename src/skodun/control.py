"""Worktree-local observation and explicit guarded control, never authorization.

Actor labels are audited claims. Expected identities are caller preconditions;
explicit IDs may intentionally target other worktrees. Unknown lifecycle causes
stay unknown rather than being inferred from an unfinished row or set token.
"""

import json
import os
from pathlib import Path
import re

GUARDS = ('expected_request_id', 'expected_worktree', 'expected_head', 'expected_diff_hash')


def scope_identity(repo='.'):
    from . import gitio
    root = gitio._worktree_root(Path(repo)).resolve()
    return {'worktree_root': str(root), 'repo_id': str(gitio.git_common_dir(root))}


def audit_text(value, field, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)
            or re.search(r'(?i)(?:sk-(?:proj-|ant-)?[a-z0-9_-]{12,}|bearer\s+|(?:api[_-]?key|token|secret)\s*[:=])', value)):
        raise ValueError(f'{field} must be bounded plain text without credentials')
    return value.strip()


def review_identity(rec):
    return {key: rec.get(key) for key in ('request_id','worktree_root','repo_id','head','diff_hash')}


def guard(identity, **expected):
    fields = dict(zip(GUARDS, ('request_id','worktree_root','head','diff_hash')))
    for name, field in fields.items():
        value = expected.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            return f'expected_identity_invalid:{field}'
        if field == 'worktree_root':
            value = str(Path(value).resolve())
        if not identity.get(field):
            return f'target_identity_missing:{field}'
        if identity[field] != value:
            return f'expected_identity_mismatch:{field}'
    return None


def guard_review(store, review_id, expected):
    if set(expected) - set(GUARDS):
        return 'expected_identity_invalid'
    if not any(value is not None for value in expected.values()):
        return None
    if not isinstance(review_id, str) or not review_id.strip():
        return 'review_id_required'
    rec = store.get_review(review_id)
    if rec is None:
        return 'review_not_found'
    return guard(review_identity(rec), **expected)


def lifecycle(rec, events=()):
    if rec.get('request_execution_seq') is not None:
        events = [event for event in events
                  if event.get('execution_seq') == rec['request_execution_seq']]
    effective = [event for event in events if event['outcome'] in
                 ('requested','observed','cancelled','expired')]
    if effective:
        return {'reason_code': effective[0]['cause'], 'attribution': 'audited',
                'cancellation': list(events)}
    reason = str(rec.get('failure_reason') or '')
    if 'did not finish' in reason or 'unfinished' in reason or 'cancel' in reason.lower():
        return {'reason_code': 'unknown', 'attribution': 'unattributed',
                'cancellation': list(events)}
    return {'reason_code': rec.get('terminal_reason') or None,
            'attribution': 'unattributed', 'cancellation': list(events)}


def status_candidates(store, repo, scope, limit):
    if scope not in ('worktree','repository','host') or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('scope must be worktree, repository, or host; limit must be 1..100')
    identity = scope_identity(repo or '.') if scope != 'host' else {}
    worktree = identity.get('worktree_root') if scope == 'worktree' else None
    requests = store.list_requests(worktree_root=worktree,
        repo_id=identity.get('repo_id') if scope == 'repository' else None,
        active_first=True, limit=1000)
    reviews = store.control_reviews(worktree_root=worktree,
        repo_id=identity.get('repo_id') if scope == 'repository' else None, limit=100,
        exclude_active_request_links=True)
    from .requests import projection
    from .services import report_state
    rows = [{'kind':'request','id':row['id'],'state':row['state'],
             'identity':row['identity'],'created_at':row['created_at'],
             'request':projection(row)} for row in requests]
    rows += [{'kind':'review','id':row['id'],'state':report_state(row),
              'identity':review_identity(row),'created_at':row['reviewed_at']}
             for row in reviews]
    rows.sort(key=lambda row:(row['state'] in ('accepted','queued','running'),
                              row['created_at'],row['id']), reverse=True)
    return rows[:limit], identity


def request_lifecycle(row):
    executions = row.get('executions') or []
    current_seq = executions[0]['seq'] if executions else None
    events = [event for event in (row.get('cancellation') or [])
              if event.get('execution_seq') == current_seq]
    effective = [event for event in events if event['outcome'] in
                 ('requested','observed','cancelled','expired')]
    if effective:
        return {'reason_code':effective[0]['cause'], 'attribution':'audited'}
    termination = ((row.get('result') or {}).get('metadata') or {}).get('termination') or {}
    if termination.get('reason_code'):
        return {'reason_code':termination['reason_code'], 'attribution':'execution'}
    pid = row.get('pid')
    if row.get('state') in ('accepted','queued','running') and type(pid) is int and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {'reason_code':'process_loss', 'attribution':'observed_pid_absent'}
        except (PermissionError, OSError):
            pass
    return {'reason_code':row.get('reason_code') or 'unknown', 'attribution':'unattributed'}


def cancellation_completion(rec):
    if rec.get('trustworthy') is True:
        return 'completed_before_cancel'
    if 'cancel' in str(rec.get('failure_reason') or '').lower():
        return 'cancelled'
    return 'failed_after_cancel'


def client_actor(value):
    """Optional client metadata is a hint, never an admission precondition."""
    try:
        return audit_text(value, 'actor', 120)
    except ValueError:
        return 'unknown'
