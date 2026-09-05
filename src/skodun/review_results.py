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
    missing_scopes = []
    sources = [('review', rec.get('id'), rec)]
    if rec.get('batched'):
        sources = [('batch', b.get('index'), b) for b in rec.get('batches', [])]
        if isinstance(rec.get('integration'), dict):
            sources.append(('integration', None, rec['integration']))
    for name, value in (rec.get('extra_passes') or {}).items():
        if not isinstance(value, dict):
            continue
        sources.append(('extra_pass', name, value))
        if 'attempts' not in value and value.get('status') != 'skipped':
            missing_scopes.append({'kind': 'extra_pass', 'id': string(name)})
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
                'admission_expired' if 'admission_expired' in causes else
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
        'attempts': rows, 'candidate_count': None if missing_scopes else candidate_count,
        'launched_count': None if missing_scopes else launched_count,
        'known_candidate_count': candidate_count, 'known_launched_count': launched_count,
        'counts_complete': not missing_scopes, 'causes_complete': not missing_scopes,
        'missing_attempt_scopes': missing_scopes[:16],
        'attempts_truncated': candidate_count > len(rows),
    }


def linked_reviews(store):
    """Snapshot current-request review links; a failed read is not an empty set."""
    from .requests import current
    ctx = current()
    if ctx is None or ctx.store is not store:
        return None
    try:
        row = store.get_request(ctx.id)
        return {link['target_id'] for link in row['links'] if link['kind'] == 'review'}
    except Exception:
        return None


def cancelled_observation(store, prior_reviews):
    """Read only the new review from this service attempt and its checkpoints.

    The foreground finally has already demoted its persisted row. Its stub may
    lack the in-memory batch aggregate, so validated completed checkpoints are
    separate evidence. Cancellation can lose an in-flight row: observed counts
    are lower bounds, never an assertion that no process ran.
    """
    from .requests import current
    from .checkpoints import CheckpointPayload, sub_fields_from_payload
    ctx = current()
    after = linked_reviews(store)
    if prior_reviews is None or after is None or len(after - prior_reviews) != 1:
        return None
    try:
        rec = store.get_review(next(iter(after - prior_reviews)))
        if rec is None or ctx is None or rec.get('request_id') != ctx.id:
            return None
        if any(rec.get(key) != ctx.identity.get(key) for key in
               ('repo_id', 'worktree_root', 'head', 'base_sha', 'diff_hash')):
            return None
        checkpoint_info = None
        evidence = dict(rec)
        orchestration_id = rec.get('batch_orchestration_id')
        if orchestration_id:
            request = store.get_request(ctx.id)
            if not any(link['kind'] == 'batch_orchestration' and
                       link['target_id'] == orchestration_id for link in request['links']):
                return None
            checkpoints = store.list_checkpoints(orchestration_id)
            completed = []
            for checkpoint in checkpoints:
                if checkpoint['state'] == 'complete':
                    payload = sub_fields_from_payload(CheckpointPayload(checkpoint['payload_json']))
                    completed.append((checkpoint, payload))
            if completed:
                evidence['batches'] = [dict(payload, index=row['pass_index'])
                                       for row, payload in completed if row['pass_kind'] == 'batch']
                evidence['integration'] = next((payload for row, payload in completed
                                                if row['pass_kind'] == 'integration'), {})
            checkpoint_info = {'scope': 'batch_orchestration', 'id': orchestration_id,
                               'completed': len(completed), 'total': len(checkpoints)}
        facts = observation(evidence)
        # Never expose cancelled work as authoritative review coverage, even if
        # the best-effort pipeline demotion could not be persisted.
        facts.update(trustworthy=False, parse_ok=False, counts_complete=False,
                     known_candidate_count=facts['known_candidate_count'],
                     known_launched_count=facts['known_launched_count'],
                     causes_complete=False,
                     candidate_count=None, launched_count=None,
                     aggregate_prompt_bytes=None, findings_total=None,
                     checkpoints=checkpoint_info)
        return facts
    except Exception:
        return None


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
    retryable = (True if code in ('admission_expired', 'provider_quota', 'provider_timeout', 'mcp_busy')
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
                   'provider_launches': observed.get('launched_count'),
                   'complete': observed.get('counts_complete', bool(observed)),
                   'known_candidates': observed.get('known_candidate_count', observed.get('candidate_count')),
                   'known_provider_launches': observed.get('known_launched_count', observed.get('launched_count'))},
        'checkpoints': observed.get('checkpoints'),
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
        'causes_complete': observed.get('causes_complete', bool(observed)),
        'missing_attempt_scopes': observed.get('missing_attempt_scopes', []),
    }


def attach(status, text, metadata=None):
    metadata = dict(metadata or {})
    metadata['result'] = project(status, metadata)
    return status, text, metadata


def valid_replay(value):
    """Validate every projected replay field and reject contradictory success."""
    import json
    import re

    def code(value):
        return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,127}', value) is not None

    def maybe_number(value):
        return value is None or number(value) is not None

    def maybe_count(value):
        return value is None or (type(value) is int and value >= 0)

    def maybe_bool(value):
        return type(value) in (bool, type(None))

    def maybe_string(value):
        return value is None or string(value, 4096) is not None

    def timing_fields(fields, depth=0):
        if not isinstance(fields, dict) or depth > 4 or len(fields) > 64:
            return False
        for key, value in fields.items():
            if not isinstance(key, str):
                return False
            if key.endswith(('_ms', '_sec', '_seconds', '_mono')):
                if not maybe_number(value):
                    return False
            elif isinstance(value, dict):
                if not timing_fields(value, depth + 1):
                    return False
            elif not (maybe_string(value) or maybe_bool(value) or maybe_number(value)):
                return False
        return True

    if not isinstance(value, dict):
        return False
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 2 * 1024 * 1024:
            return False
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    status = value.get('status')
    if type(status) is not int or status not in (0, 1, 2, 3, 4, 130):
        return False
    metadata = value.get('metadata')
    if not isinstance(value.get('text'), str) or not isinstance(metadata, dict):
        return False
    for field in ('termination', 'recovery', 'reuse', 'timing'):
        if field in metadata and not isinstance(metadata[field], dict):
            return False
    termination = metadata.get('termination', {})
    if 'reason_code' in termination and not code(termination['reason_code']):
        return False
    states = {'cancelled', 'expired', 'refused', 'completed', 'failed', 'partial'}
    if 'state' in termination and (not isinstance(termination['state'], str) or termination['state'] not in states):
        return False
    if any(not maybe_bool(termination.get(key)) for key in ('retryable', 'continuable')):
        return False
    if status in (0, 1) and (
            termination.get('state', 'completed') != 'completed' or
            termination.get('reason_code', 'review_completed') not in
            {'review_completed', 'review_clean', 'review_findings', 'trusted_reuse'}):
        return False
    if 'timing' in metadata and not timing_fields(metadata['timing']):
        return False
    recovery = metadata.get('recovery', {})
    ids = recovery.get('review_ids', [])
    if not isinstance(ids, list) or len(ids) > 8 or not all(string(rid) is not None for rid in ids):
        return False
    if not maybe_string(recovery.get('orchestration_id')) or not maybe_count(recovery.get('attempts')):
        return False
    reuse = metadata.get('reuse', {})
    if 'hit' in reuse and type(reuse['hit']) is not bool:
        return False
    facts = metadata.get('observation')
    if facts is None:
        return True  # Old result receipts have no coverage observation.
    if not isinstance(facts, dict) or not isinstance(facts.get('identity'), dict):
        return False
    if not all(maybe_string(v) for v in facts['identity'].values()):
        return False
    for field in ('review_id', 'request_id', 'batch_orchestration_id'):
        if not maybe_string(facts.get(field)):
            return False
    for field in ('trustworthy', 'parse_ok', 'partial', 'attempts_truncated', 'counts_complete', 'causes_complete'):
        if not maybe_bool(facts.get(field)):
            return False
    if not code(facts.get('reason_code')):
        return False
    causes = facts.get('causes')
    if not isinstance(causes, list) or len(causes) > 64 or not all(code(c) for c in causes):
        return False
    for field in ('findings_total', 'aggregate_prompt_bytes', 'candidate_count', 'launched_count',
                  'known_candidate_count', 'known_launched_count', 'batch_count'):
        if not maybe_count(facts.get(field)):
            return False
    if facts.get('counts_complete') is False and any(
            facts.get(key) is not None for key in ('candidate_count', 'launched_count')):
        return False
    checkpoints = facts.get('checkpoints')
    if checkpoints is not None and (
            not isinstance(checkpoints, dict) or checkpoints.get('scope') != 'batch_orchestration'
            or not maybe_string(checkpoints.get('id'))
            or not all(maybe_number(checkpoints.get(k)) for k in ('completed', 'total'))):
        return False
    missing_scopes = facts.get('missing_attempt_scopes', [])
    if not isinstance(missing_scopes, list) or len(missing_scopes) > 16:
        return False
    if any(not isinstance(scope, dict) or scope.get('kind') != 'extra_pass' or
           not maybe_string(scope.get('id')) for scope in missing_scopes):
        return False
    rows = facts.get('attempts')
    if not isinstance(rows, list) or len(rows) > MAX_ATTEMPTS:
        return False
    for row in rows:
        if not isinstance(row, dict) or type(row.get('launched')) is not bool:
            return False
        scope = row.get('scope')
        if (not isinstance(scope, dict) or scope.get('kind') not in
                ('review', 'batch', 'integration', 'extra_pass') or
                not (maybe_string(scope.get('id')) or maybe_number(scope.get('id')))):
            return False
        if not code(row.get('reason_code')) or row.get('input_scope') != 'provider_input':
            return False
        for field in ('attempt_id', 'provider', 'model', 'effort', 'transport'):
            if not maybe_string(row.get(field)):
                return False
        for field in ('ordinal', 'input_bytes', 'limit_bytes'):
            if not maybe_count(row.get(field)):
                return False
        for field in ('duration_sec', 'first_output_sec', 'queue_wait_ms'):
            if not maybe_number(row.get(field)):
                return False
    if facts.get('counts_complete', True):
        if (type(facts.get('candidate_count')) is not int or
                type(facts.get('launched_count')) is not int or
                facts['candidate_count'] < len(rows) or
                facts['launched_count'] < sum(row['launched'] for row in rows) or
                facts['launched_count'] > facts['candidate_count']):
            return False
    if status in (0, 1):
        if facts.get('trustworthy') is not True or facts.get('parse_ok') is not True or facts.get('partial'):
            return False
        if status == 0 and not reuse.get('hit') and facts.get('findings_total') != 0:
            return False
    return True
