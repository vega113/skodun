"""Bounded review-result/v1 read model. Reporting never evaluates the gate.

Only the artifact returned by this invocation supplies observed coverage.
Candidate log rows are not process counts. Missing fields remain unknown;
aggregate prompt sums never stand in for an individual provider's input.
"""

import math

SCHEMA_VERSION = 'review-result/v1'
MAX_ATTEMPTS = 128
IDENTITY_FIELDS = ('repo_id', 'canonical_repository', 'worktree_root', 'branch',
                   'head', 'base_ref', 'base_sha', 'diff_hash', 'tree_fingerprint')


def number(value):
    if type(value) is int:
        return value if value >= 0 else None
    return value if type(value) is float and math.isfinite(value) and value >= 0 else None


def string(value, limit=512):
    return value if isinstance(value, str) and len(value) <= limit else None


def attempt_reason(row):
    if row.get('reason_code'):
        return string(row['reason_code'])
    if row.get('timed_out') is True:
        return 'provider_timeout'
    category = (row.get('classification') or {}).get('category')
    if category:
        return {'prompt_size': 'transport_ineligible', 'quota': 'provider_quota',
                'auth': 'provider_auth', 'binary': 'provider_binary',
                'model': 'provider_model', 'invocation': 'provider_invocation',
                'transport': 'provider_transport'}.get(category, 'provider_unavailable')
    if row.get('skipped') is not None:
        return 'candidate_skipped'
    if (row.get('classification') or {}).get('kind') == 'degraded':
        return 'provider_degraded'
    return 'provider_completed'


def observation(rec):
    """Compact exact-artifact facts, safe to retain in a request result."""
    rows, causes = [], set()
    candidate_count = launched_count = 0
    sources = [('review', rec.get('id'), rec)]
    if rec.get('batched'):
        sources = [('batch', b.get('index'), b) for b in rec.get('batches', [])]
        if isinstance(rec.get('integration'), dict):
            sources.append(('integration', None, rec['integration']))
    sources += [('extra_pass', name, value) for name, value in
                (rec.get('extra_passes') or {}).items() if isinstance(value, dict)]
    for scope, scope_id, source in sources:
        for row in source.get('attempts') or []:
            candidate_count += 1
            launched = (row.get('skipped') is None and
                        (row.get('rc') is not None or row.get('timed_out') is not None))
            launched_count += int(launched)
            code = attempt_reason(row)
            if code != 'provider_completed':
                causes.add(code)
            eligibility = row.get('input_eligibility') or {}
            if len(rows) < MAX_ATTEMPTS:
                rows.append({
                    'scope': {'kind': scope, 'id': string(scope_id) if isinstance(scope_id, str) else number(scope_id)}, 'ordinal': number(row.get('n')),
                    'attempt_id': string(row.get('attempt_id')),
                    'provider': string(row.get('provider')), 'model': string(row.get('model')),
                    'effort': string(row.get('effort')), 'launched': launched,
                    'reason_code': code, 'input_bytes': number(row.get('input_bytes', eligibility.get('input_bytes'))),
                    'input_scope': 'provider_input', 'limit_bytes': number(eligibility.get('limit_bytes')),
                    'transport': string(eligibility.get('transport')),
                    'duration_sec': number(row.get('duration_sec')),
                    'first_output_sec': number(row.get('first_output_sec')),
                    'queue_wait_ms': number((row.get('capacity_timing') or {}).get('queue_wait_ms')),
                })
    complete = rec.get('trustworthy') is True
    partial = bool(rec.get('batched') and not complete and
                   any(b.get('parse_ok') for b in rec.get('batches', [])))
    code = 'review_clean' if complete and rec.get('findings_total') == 0 else 'review_findings'
    if not complete:
        code = ('no_compatible_route' if 'transport_ineligible' in causes else
                'provider_timeout' if 'provider_timeout' in causes else
                'provider_quota' if 'provider_quota' in causes else
                'review_partial' if partial else 'review_untrustworthy')
    return {
        'review_id': string(rec.get('id')),
        'request_id': string(rec.get('request_id')),
        'batch_orchestration_id': string(rec.get('batch_orchestration_id')),
        'batch_count': number(rec.get('batch_count')),
        'identity': {k: string(rec.get(k), 4096) for k in IDENTITY_FIELDS},
        'trustworthy': rec.get('trustworthy') if type(rec.get('trustworthy')) is bool else None,
        'parse_ok': rec.get('parse_ok') if type(rec.get('parse_ok')) is bool else None,
        'partial': partial, 'findings_total': number(rec.get('findings_total')),
        'aggregate_prompt_bytes': number(rec.get('prompt_bytes')),
        'reason_code': code, 'causes': sorted(causes),
        'attempts': rows, 'candidate_count': candidate_count, 'launched_count': launched_count,
        'attempts_truncated': candidate_count > len(rows),
    }


def project(status, metadata=None, *, reason_code=None):
    """One explicit terminal envelope; never infer a cause from human prose."""
    metadata = metadata or {}
    request = metadata.get('request') or {}
    observed = metadata.get('observation') or {}
    termination = metadata.get('termination') or {}
    recovery = metadata.get('recovery') or {}
    code = (reason_code or termination.get('reason_code') or request.get('reason_code')
            or ('trusted_reuse' if (metadata.get('reuse') or {}).get('hit') else None)
            or observed.get('reason_code') or
            {0: 'review_completed', 1: 'review_findings', 2: 'invalid_input',
             3: 'admission_expired', 4: 'review_failed', 130: 'requested_cancel'}.get(status, 'review_failed'))
    state = ('cancelled' if code in ('requested_cancel', 'interrupted') else
             'expired' if code in ('budget_expired', 'admission_expired', 'recovery_attempts_exhausted') else
             'refused' if status in (2, 3) else 'completed' if status in (0, 1) else 'failed')
    if observed.get('partial'):
        state = 'partial' if state == 'failed' else state
    retryable = (True if code in ('admission_expired', 'provider_quota', 'provider_timeout')
                 else False if code in ('invalid_input', 'invocation_invalid', 'no_compatible_route',
                                      'request_in_flight', 'request_identity_mismatch') else None)
    return {
        'schema_version': SCHEMA_VERSION,
        'ids': {'request_id': string(request.get('id')),
                'review_id': string(observed.get('review_id')),
                'observed_request_id': string(observed.get('request_id')),
                'recovery_orchestration_id': string(recovery.get('orchestration_id')),
                'batch_orchestration_id': string(observed.get('batch_orchestration_id'))},
        'identity': {'requested': {k: string((request.get('identity') or {}).get(k), 4096) for k in IDENTITY_FIELDS},
                     'observed': {k: string((observed.get('identity') or {}).get(k), 4096) for k in IDENTITY_FIELDS}},
        'execution': {'state': termination.get('state', state), 'reason_code': code,
                      'exit_code': status,
                      'retryable': termination.get('retryable', retryable),
                      'continuable': termination.get('continuable'),
                      'request_persisted': request.get('persisted'),
                      'replayed': request.get('replayed') is True,
                      'reused': (metadata.get('reuse') or {}).get('hit') is True},
        'coverage': {'trustworthy': observed.get('trustworthy'), 'parse_ok': observed.get('parse_ok'),
                     'partial': observed.get('partial')},
        'findings': {'total': observed.get('findings_total'), 'open': None, 'triage_evaluated': False},
        'gate': {'evaluated': False, 'exit_code': None},
        'timing': metadata.get('timing') or {'scope': 'request_execution', 'duration_sec': None, 'queue_wait_ms': None},
        'bytes': {'scope': 'review_aggregate', 'prompt_bytes': observed.get('aggregate_prompt_bytes')},
        'counts': {'scope': 'observed_review',
                   'review_id': string(observed.get('review_id')), 'candidates': observed.get('candidate_count'),
                   'provider_launches': observed.get('launched_count')},
        'orchestration': {
            'recovery': {'scope': 'recovery_orchestration',
                         'id': string(recovery.get('orchestration_id')),
                         'attempt_count': number(recovery.get('attempts')),
                         'review_ids': [string(rid) for rid in (recovery.get('review_ids') or [])[:8]]},
            'batch': {'scope': 'batch_orchestration',
                      'id': string(observed.get('batch_orchestration_id')),
                      'batch_count': number(observed.get('batch_count'))}},
        'attempts': observed.get('attempts', []),
        'attempts_truncated': observed.get('attempts_truncated', False),
        'causes': observed.get('causes', []),
    }


def attach(status, text, metadata=None):
    metadata = dict(metadata or {})
    metadata['result'] = project(status, metadata)
    return status, text, metadata


def valid_replay(value):
    """Reject malformed durable results before their status can signal success."""
    if not isinstance(value, dict):
        return False
    if type(value.get('status')) is not int or value['status'] not in (0, 1, 2, 3, 4, 130):
        return False
    if not isinstance(value.get('text'), str) or not isinstance(value.get('metadata'), dict):
        return False
    for field in ('termination', 'recovery', 'reuse', 'timing'):
        if field in value['metadata'] and not isinstance(value['metadata'][field], dict):
            return False
    facts = value['metadata'].get('observation')
    if facts is not None:
        if not isinstance(facts, dict):
            return False
        rows = facts.get('attempts')
        if not isinstance(rows, list) or len(rows) > MAX_ATTEMPTS or not all(isinstance(r, dict) for r in rows):
            return False
        if type(facts.get('trustworthy')) not in (bool, type(None)):
            return False
        if not isinstance(facts.get('identity'), dict):
            return False
        for field in ('findings_total', 'aggregate_prompt_bytes', 'candidate_count', 'launched_count'):
            if facts.get(field) is not None and number(facts[field]) is None:
                return False
    return True
