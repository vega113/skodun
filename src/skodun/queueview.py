"""Bounded request/queue observations, never coverage or a spending estimate.

Recovery orchestration IDs and batch orchestration IDs are separate namespaces.
Only explicit launch evidence counts a provider call. Repeated attempt IDs are
one observation; old rows without IDs are marked incomplete. Durations use the
union of real intervals, never the sum of concurrent capacity holds.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone

from .telemetry import attempt_launched, confirmed_input_skip, _token_usage
from .store import _is_canonical_ts

MAX_LINKS = 200
MAX_ATTEMPTS = 2000
MAX_PEERS = 200


def _number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _epoch(value):
    if not _is_canonical_ts(value):
        return None
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _interval(start, end):
    start, end = _epoch(start), _epoch(end)
    return (start, end) if start is not None and end is not None and end >= start else None


def _elapsed(start, end):
    span = _interval(start, end)
    return round((span[1] - span[0]) * 1000) if span else None


def union_ms(intervals):
    spans = sorted(span for span in intervals if span is not None)
    if not spans:
        return None
    total = 0.0
    start, end = spans[0]
    for left, right in spans[1:]:
        if left > end:
            total += end - start
            start, end = left, right
        else:
            end = max(end, right)
    return round((total + end - start) * 1000)


def _wait_end(row, now=None):
    if row.get('admitted_at') is not None:
        return row['admitted_at']
    if row.get('started_at') is None and row.get('status') in ('expired', 'rejected'):
        return row.get('ended_at')
    return now if row.get('status') == 'queued' else None


def _metric(intervals, *, window, denominator):
    valid = [span for span in intervals if span is not None]
    return {'value_ms': union_ms(valid), 'sample_count': len(valid),
            'missing_count': len(intervals) - len(valid), 'unit': 'ms',
            'denominator': denominator, 'method': 'interval_union', 'window': window}


def _json_object(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _owner(store, admission):
    rows = store._c.execute(
        "SELECT request_id FROM request_links WHERE kind='capacity' AND target_id=? LIMIT 2",
        (admission['id'],)).fetchall()
    if len(rows) != 1:
        return {'request_id': None, 'owner_status': 'ambiguous' if rows else 'missing'}
    row = store._c.execute('SELECT id,identity_json FROM review_requests WHERE id=?',
                          (rows[0]['request_id'],)).fetchone()
    if row is None:
        return {'request_id': rows[0]['request_id'], 'owner_status': 'missing'}
    identity = _json_object(row['identity_json'])
    return {'request_id': row['id'], 'owner_status': 'known',
            'worktree_root': identity.get('worktree_root'), 'branch': identity.get('branch')}


def _budget(raw, request_id):
    raw = _json_object(raw)
    if raw.get('scope') != 'request_execution' or raw.get('request_id') != request_id:
        return {}
    limits = _json_object(raw.get('limits'))
    deadlines = _json_object(raw.get('deadlines'))
    timing = _json_object(raw.get('timing'))
    layers = []
    raw_layers = raw.get('capacity_layers')
    for item in (raw_layers if isinstance(raw_layers, (list, tuple)) else ())[:MAX_PEERS]:
        if not isinstance(item, dict):
            continue
        layer = {key: item.get(key) for key in ('resource_class', 'scope')}
        if not all(isinstance(value, str) and len(value) <= 4096 for value in layer.values()):
            continue
        admission_id = item.get('admission_id')
        layer['admission_id'] = admission_id if isinstance(admission_id, str) and len(admission_id) <= 4096 else None
        layer['execution_seq'] = item.get('execution_seq') if type(item.get('execution_seq')) is int else None
        for key in ('effective_capacity', 'configured_capacity'):
            value = item.get(key)
            layer[key] = value if type(value) is int and value >= 0 else None
        layer['legacy_dual_hold'] = (item.get('legacy_dual_hold')
                                    if type(item.get('legacy_dual_hold')) is bool else None)
        layers.append(layer)
    return {'scope': 'request_execution', 'request_id': request_id,
        'execution_seq': raw.get('execution_seq') if type(raw.get('execution_seq')) is int else None,
        'phase': raw.get('phase') if isinstance(raw.get('phase'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', raw['phase']) else None,
        'review_paused_for_queue': raw.get('review_paused_for_queue') if type(raw.get('review_paused_for_queue')) is bool else None,
        'reason_code': raw.get('reason_code') if isinstance(raw.get('reason_code'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', raw['reason_code']) else None,
        'capacity_layers_truncated': raw.get('capacity_layers_truncated') is True or len(raw_layers or ()) > MAX_PEERS,
        'limits': {key: _number(limits.get(key)) for key in ('max_queue_seconds',
            'max_review_seconds', 'max_provider_wait_seconds', 'max_wall_seconds')},
        'deadlines': {key: deadlines.get(key) if _epoch(deadlines.get(key)) is not None else None
                      for key in ('queue', 'review', 'total', 'provider_wait')},
        'capacity_layers': layers,
        'timing': {key: _number(timing.get(key)) for key in
                   ('queue_wait_ms', 'provider_wait_ms', 'review_wall_ms', 'review_active_ms', 'total_ms')},
        'updated_at': raw.get('updated_at') if _epoch(raw.get('updated_at')) is not None else None}


def _read_budgets(store, request_id):
    status, reason, current = 'unavailable', 'budget_getter_unavailable', {}
    getter = getattr(store, 'request_budget', None)
    if callable(getter):
        try:
            raw = getter(request_id)
            current = _budget(raw, request_id)
            status = 'known' if current else 'missing' if raw is None else 'invalid'
            reason = None if current else 'budget_snapshot_missing' if raw is None else 'budget_snapshot_invalid'
        except Exception as exc:
            reason = 'budget_read_failed:' + type(exc).__name__
    history_status, history_truncated = 'unavailable', False
    history = []
    history_getter = getattr(store, 'request_budgets', None)
    if callable(history_getter):
        try:
            result = history_getter(request_id, limit=100)
            if not isinstance(result, dict) or not isinstance(result.get('budgets'), list):
                raise ValueError('invalid budget history')
            history = [_budget(item, request_id) for item in result['budgets'][:100]]
            history_truncated = result.get('truncated') is True or len(result['budgets']) > 100
            history_status = 'partial' if history_truncated or any(not item for item in history) else 'known'
        except Exception:
            history_status = 'unavailable'
    layers = {}
    # Each admission keeps its persisted execution owner, never a new cap
    # inferred from the latest request budget. Current and history may overlap.
    for budget in [current, *history]:
        for layer in budget.get('capacity_layers', ()):
            key = (layer['admission_id'], layer['execution_seq'])
            if key in layers:
                continue
            if len(layers) >= MAX_LINKS:
                history_truncated = True
                break
            layers[key] = layer
        history_truncated |= budget.get('capacity_layers_truncated', False)
    if history_truncated and history_status == 'known':
        history_status = 'partial'
    return current, list(layers.values()), status, reason, history_status, history_truncated


def _admission(store, row, now, layers, resources):
    resource, scope = row['resource_class'], row['scope']
    key = (resource, scope)
    if key not in resources and len(resources) >= 100:
        return {**{key: row.get(key) for key in ('id', 'resource_class', 'scope', 'status',
            'queued_at', 'admitted_at', 'started_at', 'ended_at', 'expire_reason')},
            'wait_elapsed_ms': _elapsed(row['queued_at'], _wait_end(row, now)),
            'position': None, 'holders': [], 'holders_truncated': True,
            'effective_limit': None, 'contended': None, 'historical_median_wait': None}
    if key not in resources:
        peers = [dict(item) for item in store._c.execute(
            "SELECT * FROM capacity_admissions WHERE resource_class=? AND scope=? "
            "AND status IN ('queued','admitted','running') ORDER BY queued_at,rowid LIMIT ?",
            (resource, scope, MAX_PEERS + 1))]
        holders = [{**_owner(store, item), 'admission_id': item['id']}
                   for item in peers[:MAX_PEERS] if item['status'] in ('admitted', 'running')]
        waiting = [item['id'] for item in peers[:MAX_PEERS] if item['status'] == 'queued']
        samples = [dict(item) for item in store._c.execute(
            "SELECT queued_at,admitted_at,started_at,ended_at,status FROM capacity_admissions "
            "WHERE resource_class=? AND scope=? AND status IN ('released','expired','rejected') "
            "ORDER BY ended_at DESC,id DESC LIMIT 20", (resource, scope))]
        waits = [value for item in samples for value in
                 [_elapsed(item['queued_at'], _wait_end(item))]
                 if value is not None]
        resources[key] = peers, holders, waiting, samples, waits
    peers, holders, waiting, samples, waits = resources[key]
    position = waiting.index(row['id']) + 1 if row['id'] in waiting else None
    end = _wait_end(row, now)
    # Limits are execution facts supplied by #185, never current shell config.
    matching = [layer for layer in layers if layer['resource_class'] == resource
                and layer['scope'] == scope and layer['admission_id'] == row['id']]
    limit = matching[-1]['effective_capacity'] if matching else None
    return {**{key: row.get(key) for key in ('id', 'resource_class', 'scope', 'status',
             'queued_at', 'admitted_at', 'started_at', 'ended_at', 'expire_reason')},
            'wait_elapsed_ms': _elapsed(row['queued_at'], end), 'position': position,
            'holders': holders, 'holders_truncated': len(peers) > MAX_PEERS,
            'effective_limit': limit,
            'contended': (len(holders) >= limit if limit is not None
                          and (len(peers) <= MAX_PEERS or len(holders) >= limit) else None),
            'historical_median_wait': {
                'value_ms': statistics.median(waits) if waits else None,
                'sample_count': len(waits), 'small_sample': len(waits) < 3,
                'unit': 'ms', 'denominator': 'terminal admissions in this resource/scope',
                'method': 'median (mean of middle pair)',
                'window': {'kind': 'latest_terminal_admissions', 'limit': 20,
                           'from': min((s['queued_at'] for s in samples if _epoch(s['queued_at']) is not None), default=None),
                           'to': max((s['ended_at'] for s in samples if _epoch(s['ended_at']) is not None), default=None)}}}


def _attempt_rows(review, missing_scopes):
    """Visit raw pass attempts once; nested telemetry is a duplicate projection."""
    namespace = review.get('batch_orchestration_id') or review.get('id')
    containers = [('review', review.get('id'), review)]
    containers += [('batch', b.get('index', i), b) for i, b in
                   enumerate(review.get('batches') or ()) if isinstance(b, dict)]
    if isinstance(review.get('integration'), dict):
        containers.append(('integration', 0, review['integration']))
    extras = review.get('extra_passes')
    if isinstance(extras, dict):
        containers += [('extra_pass', name, part) for name, part in extras.items()
                       if isinstance(part, dict)]
    for kind, part, container in containers:
        # An explicit raw list, even empty, is authoritative over telemetry.
        attempts = container.get('attempts')
        if attempts is None:
            attempts = _json_object(container.get('telemetry')).get('attempts')
        if attempts is None and container.get('status') != 'skipped' and not (
                kind == 'review' and (review.get('batched') or review.get('batches'))):
            missing_scopes.append({'namespace': namespace, 'kind': kind, 'id': part})
        if not isinstance(attempts, (list, tuple)):
            continue
        for item in attempts:
            if isinstance(item, dict):
                yield (namespace, kind, part), item


def _calls(reviews):
    unique = {}
    missing_scopes = []
    unidentified = 0
    truncated = False
    examined = 0
    for review in reviews:
        for scope, item in _attempt_rows(review, missing_scopes):
            examined += 1
            if examined > MAX_ATTEMPTS * 3:
                truncated = True
                break
            attempt_id = item.get('attempt_id')
            if isinstance(attempt_id, str) and attempt_id:
                key = ('attempt', attempt_id)
            else:
                # No content/output hashes. Scope and safe scalar evidence can
                # deduplicate copied legacy telemetry, but are not proven IDs.
                signature = {key: item.get(key) for key in (
                    'n', 'attempt_ordinal', 'provider', 'model', 'rc', 'timed_out',
                    'skipped', 'duration_sec', 'input_bytes', 'capacity_timing')}
                key = ('legacy', *scope, hashlib.sha256(json.dumps(
                    signature, sort_keys=True, default=str).encode()).hexdigest())
            if key in unique:
                if unique[key]['scope']['kind'] == 'review' and scope[1] != 'review':
                    unique[key]['scope'] = {'namespace': scope[0], 'kind': scope[1], 'id': scope[2]}
                continue
            if len(unique) >= MAX_ATTEMPTS:
                truncated = True
                break
            launched = attempt_launched(item)
            raw_usage = _json_object(item.get('usage'))
            usage = (_token_usage(raw_usage) if raw_usage
                     else _json_object(item.get('token_usage')))
            tokens = _number(usage.get('total'))
            # Historical adapters emitted zero even when usage was absent.
            tokens = tokens if tokens and tokens > 0 else None
            timing = _json_object(item.get('capacity_timing'))
            unique[key] = {'attempt_id': attempt_id, 'scope': {'namespace': scope[0],
                'kind': scope[1], 'id': scope[2]}, 'provider': item.get('provider'),
                'model': item.get('model'),
                'launched': launched, 'input_ineligible': confirmed_input_skip(item), 'input_bytes': _number(item.get('input_bytes')),
                'total_tokens': tokens, 'usage': usage, 'ordinal': item.get('n', item.get('attempt_ordinal')),
                'interval': _interval(timing.get('started_at'), timing.get('ended_at'))}
            unidentified += int(key[0] == 'legacy')
        if truncated:
            break
    calls = list(unique.values())
    launched = [row for row in calls if row['launched'] is True]
    bytes_known = [row['input_bytes'] for row in launched if row['input_bytes'] is not None]
    tokens = [row['total_tokens'] for row in launched if row['total_tokens'] is not None]
    retry_groups = Counter((call['scope']['namespace'], call['scope']['kind'],
        call['scope']['id'], call['provider'], call['model']) for call in launched)
    usage_summary = {}
    for kind in ('input', 'output', 'cache', 'reasoning'):
        reported = [value for row in launched for value in [_number(row['usage'].get(kind))]
                    if value is not None and value > 0]
        usage_summary[kind] = sum(reported) if reported and len(reported) == len(launched) else None
        usage_summary['reported_' + kind] = sum(reported) if reported else None
        usage_summary[kind + '_reported_calls'] = len(reported)
    return calls, {'launched_calls': len(launched), 'reported_launched_calls': len(launched),
        'candidate_skips': sum(row['launched'] is False for row in calls),
        'eligibility_skips': sum(row['input_ineligible'] for row in calls),
        'launch_unknown': sum(row['launched'] is None for row in calls),
        'attempt_identity_missing': unidentified, 'attempts_truncated': truncated,
        'missing_attempt_scopes': missing_scopes[:MAX_LINKS],
        'retry_calls': sum(max(0, count - 1) for count in retry_groups.values()),
        'retry_method': 'repeated provider/model launches within one pass namespace',
        'max_per_call_prompt_bytes': max(bytes_known, default=None),
        'reported_max_per_call_prompt_bytes': max(bytes_known, default=None),
        'aggregate_launched_prompt_bytes': sum(bytes_known) if bytes_known and len(bytes_known) == len(launched) else None,
        'reported_launched_prompt_bytes': sum(bytes_known) if bytes_known else None,
        'prompt_bytes_reported_calls': len(bytes_known),
        'prompt_bytes_complete': len(bytes_known) == len(launched) and bool(launched),
        'token_usage': {**usage_summary, 'total': sum(tokens) if tokens and len(tokens) == len(launched) else None,
                        'reported_total': sum(tokens) if tokens else None,
                        'reported_calls': len(tokens), 'launched_calls': len(launched),
                        'complete': bool(launched) and len(tokens) == len(launched)}}


def _reused_pass_observations(reviews):
    """Count explicit reuse once per declared generation/pass, never position.

    This is observed reuse, not an estimate for legacy records without reuse
    metadata. Positive observations missing identity retain an unknown total.
    """
    observed = set()
    unidentified = 0
    for review in reviews:
        generation = review.get('batch_orchestration_id')
        containers = [('batch', part.get('index'), part)
                      for part in review.get('batches') or () if isinstance(part, dict)]
        if isinstance(review.get('integration'), dict):
            containers.append(('integration', 0, review['integration']))
        extras = review.get('extra_passes')
        if isinstance(extras, dict):
            containers.extend((kind, 0, extras[kind]) for kind in ('security', 'skeptic')
                              if isinstance(extras.get(kind), dict))
        for kind, index, part in containers:
            action = part.get('continuation_action') or _json_object(part.get('provenance')).get('continuation_action')
            if action != 'reused' and part.get('reused') is not True:
                continue
            if (not isinstance(generation, str) or not generation
                    or type(index) is not int or index < (1 if kind == 'batch' else 0)):
                unidentified += 1
                continue
            observed.add((generation, kind, index))
    return {'reused_passes': len(observed) if not unidentified else None,
            'reported_reused_passes': len(observed), 'reuse_identity_missing': unidentified,
            'reused_passes_scope': 'explicit reused observations per generation/pass'}


def _request(store, row, now, spend_rows, spend_truncated, resources):
    identity = _json_object(row['identity_json'])
    links = [dict(r) for r in store._c.execute(
        'SELECT kind,target_id FROM request_links WHERE request_id=? ORDER BY kind,target_id LIMIT ?',
        (row['id'], MAX_LINKS + 1))]
    truncated = len(links) > MAX_LINKS
    links = links[:MAX_LINKS]
    ids = {kind: {r['target_id'] for r in links if r['kind'] == kind}
           for kind in ('review', 'capacity', 'recovery_orchestration', 'batch_orchestration')}
    reviews, missing, malformed = [], [], []
    for rid in sorted(ids['review']):
        try:
            rec = store.get_review(rid)
        except (TypeError, ValueError):
            rec = None
            malformed.append(rid)
        if isinstance(rec, dict):
            reviews.append(rec)
        else:
            missing.append(rid)
    missing_namespace_links = {}
    for kind, field in (('recovery_orchestration', 'orchestration_id'),
                        ('batch_orchestration', 'batch_orchestration_id')):
        observed_ids = {rec[field] for rec in reviews if isinstance(rec.get(field), str) and rec[field]}
        missing_namespace_links[kind] = sorted(observed_ids - ids[kind])
        ids[kind].update(observed_ids)
    budget, layers, budget_status, budget_reason, history_status, history_truncated = _read_budgets(store, row['id'])
    admissions, missing_capacity = [], []
    for aid in sorted(ids['capacity']):
        item = store.capacity_get(aid)
        if item is None:
            missing_capacity.append(aid)
        else:
            admissions.append(_admission(store, item, now, layers, resources))
    executions = [dict(r) for r in store._c.execute(
        'SELECT seq,started_at,completed_at,status FROM request_executions '
        'WHERE request_id=? ORDER BY seq DESC LIMIT 101', (row['id'],))]
    executions_truncated = len(executions) > 100
    executions = executions[:100]
    owned_reviews = [rec for rec in reviews if rec.get('request_id') == row['id']]
    unowned = [rec['id'] for rec in reviews if not rec.get('request_id')]
    reused = [rec['id'] for rec in reviews if rec.get('request_id')
              and rec.get('request_id') != row['id']]
    calls, costs = _calls(owned_reviews)
    attributable_review_ids = {rec['id'] for rec in owned_reviews}
    spend = [item for item in spend_rows if item['review_id'] in attributable_review_ids]
    reported_usd = sum(item['cost_usd'] for item in spend) if spend else None
    costs['metered_spend'] = {'usd': reported_usd if not spend_truncated else None,
                             'reported_usd': reported_usd,
                             'attributable_events': len(spend),
                             'scan_truncated': spend_truncated,
                             'scope': 'owned linked review metered API events only',
                             'subscription_cost_usd': None}
    costs['reused_reviews'] = reused
    batch_keys = {(rec.get('batch_orchestration_id') or rec['id'], batch.get('index', index))
        for rec in reviews for index, batch in enumerate(rec.get('batches') or ())
        if isinstance(batch, dict)}
    costs.update(_reused_pass_observations(reviews))
    costs['review_bytes'] = [{'review_id': rec['id'],
        'diff_bytes': _number(rec.get('diff_bytes')),
        'prompt_bytes': _number(rec.get('prompt_bytes')),
        'prompt_scope': 'aggregate_batches_and_integration' if rec.get('batched') or rec.get('batches')
                        else 'recorded_review_prompt'} for rec in reviews]
    observation = _json_object(_json_object(_json_object(row['result_json']).get('metadata')).get('observation'))
    observation_incomplete = observation.get('request_id') == row['id'] and observation.get('counts_complete') is False
    counts_complete = not (missing or unowned or truncated or costs['launch_unknown']
        or costs['missing_attempt_scopes'] or observation_incomplete
        or costs['attempts_truncated'] or costs['attempt_identity_missing']
        or row['state'] in ('accepted', 'queued', 'running'))
    costs['counts_complete'] = counts_complete
    costs['result_observation_incomplete'] = observation_incomplete
    if not counts_complete:
        costs['launched_calls'] = None
        costs['aggregate_launched_prompt_bytes'] = None
        costs['max_per_call_prompt_bytes'] = None
        costs['token_usage']['total'] = None
        costs['token_usage']['complete'] = False
        for kind in ('input', 'output', 'cache', 'reasoning'):
            costs['token_usage'][kind] = None
    missing_orchestrations = {'recovery_orchestration': [], 'batch_orchestration': []}
    for target in ids['recovery_orchestration']:
        if store._c.execute('SELECT id FROM reviews WHERE orchestration_id=? LIMIT 1',
                            (target,)).fetchone() is None:
            missing_orchestrations['recovery_orchestration'].append(target)
    for target in ids['batch_orchestration']:
        if store._c.execute('SELECT id FROM review_orchestrations WHERE id=?',
                            (target,)).fetchone() is None:
            missing_orchestrations['batch_orchestration'].append(target)
    window = {'from': row['created_at'], 'to': now if row['state'] in
              ('accepted', 'queued', 'running') else row['updated_at']}
    queue_intervals = [_interval(a['queued_at'], _wait_end(a, now)) for a in admissions]
    execution_intervals = [_interval(e['started_at'], e['completed_at'] or (
        now if row['state'] in ('accepted', 'queued', 'running') else None)) for e in executions]
    provider_intervals = [call['interval'] for call in calls if call['launched'] is True]
    trustworthy_ends = [rec.get('review_completed_at') for rec in reviews
                        if rec.get('trustworthy') is True and _epoch(rec.get('review_completed_at')) is not None]
    timing = {'queue_elapsed': _metric(queue_intervals, window=window, denominator='linked capacity intervals'),
              'provider_elapsed': _metric(provider_intervals, window=window, denominator='unique launched calls'),
              'execution_elapsed': _metric(execution_intervals, window=window, denominator='request executions'),
              'total_elapsed': _metric([_interval(window['from'], window['to'])], window=window, denominator='request'),
              'time_to_trustworthy': _metric([_interval(row['created_at'], min(trustworthy_ends))
                    if trustworthy_ends else None], window=window, denominator='request to first trustworthy review'),
              'external_gate_lock_wait_ms': None}
    partial = (truncated or missing or malformed or missing_capacity or executions_truncated
               or costs['attempt_identity_missing'] or costs['launch_unknown']
               or costs['attempts_truncated'] or costs['missing_attempt_scopes'] or observation_incomplete or not budget or unowned
               or history_truncated
               or any(missing_orchestrations.values()) or any(missing_namespace_links.values())
               or any(a['holders_truncated'] for a in admissions))
    return {'request_id': row['id'], 'state': row['state'],
        'identity': {key: identity.get(key) for key in ('worktree_root', 'repo_id', 'branch', 'head', 'diff_hash')},
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'request_expiry': row['expires_at'], 'deadlines': budget.get('deadlines'),
        'time_limits': budget.get('limits'), 'capacity_layers': layers or None,
        'execution_budget_timing': ({'scope': 'request_execution',
            'execution_seq': budget.get('execution_seq'), 'measurements_ms': budget['timing'],
            'sample_count': int(any(value is not None for value in budget['timing'].values())),
            'unit': 'ms', 'method': 'observed_runtime_clock', 'denominator': 'request execution',
            'window': {'from': next((e['started_at'] for e in executions if e['seq'] == budget.get('execution_seq')), None),
                       'to': budget.get('updated_at')}} if budget else None),
        'budget_status': budget_status, 'budget_reason_code': budget_reason,
        'budget_phase': budget.get('phase'), 'review_paused_for_queue': budget.get('review_paused_for_queue'),
        'budget_execution_reason_code': budget.get('reason_code'),
        'budget_history_status': history_status, 'budget_history_truncated': history_truncated,
        'admissions': admissions, 'costs': costs, 'timing': timing,
        'orchestrations': {kind: sorted(ids[kind]) for kind in
                          ('recovery_orchestration', 'batch_orchestration')},
        'denominators': {'requested_reviews': 1, 'request_executions': len(executions),
            'review_records': len(reviews), 'review_modes': dict(Counter(str(rec.get('mode') or 'unknown') for rec in reviews)),
            'recovery_orchestration_ids': len(ids['recovery_orchestration']),
            'batch_orchestration_ids': len(ids['batch_orchestration']),
            'nested_batches': len(batch_keys),
            'nested_batch_records': sum(len(rec.get('batches') or ()) for rec in reviews)},
        'coverage': {'status': 'partial' if partial else 'complete', 'links_truncated': truncated,
            'executions_truncated': executions_truncated, 'missing_reviews': missing,
            'malformed_reviews': malformed, 'missing_admissions': missing_capacity,
            'review_cost_owner_missing': unowned, 'missing_orchestrations': missing_orchestrations,
            'missing_namespace_links': missing_namespace_links,
            'usage_complete': costs['token_usage']['complete'],
            'notes': ['Unknown usage is not zero; prompt bytes are not billed tokens.',
                      'External local-gate locks are not Skodun capacity queues.']}}


def inspect(store, *, request_id=None, worktree_root=None, repository_id=None,
            scope='host', limit=50, now=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer in 1..100')
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if _epoch(now) is None:
        raise ValueError('now must be a canonical UTC timestamp')
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('request_id must be nonempty text')
        rows = store._c.execute('SELECT * FROM review_requests WHERE id=?', (request_id,)).fetchall()
        if not rows:
            raise ValueError('request not found')
    elif worktree_root is not None:
        rows = store._c.execute('SELECT * FROM review_requests WHERE scope=? '
            'ORDER BY created_at DESC,id DESC LIMIT ?', (worktree_root, limit + 1)).fetchall()
    else:
        # Host/repository scans are explicitly bounded recent rowid windows;
        # avoid sorting the entire request history without a matching index.
        rows = store._c.execute('SELECT * FROM review_requests ORDER BY rowid DESC LIMIT 1001').fetchall()
    scanned = len(rows)
    scan_truncated = scanned > 1000
    rows = rows[:1000]
    if repository_id is not None and request_id is None:
        rows = [row for row in rows if _json_object(row['identity_json']).get('repo_id') == repository_id]
    truncated = len(rows) > limit or scan_truncated
    # One bounded primary-key scan for the whole inspection, not one
    # unindexed ledger scan per linked review. API request_id is not Skodun ID.
    spend_rows = [dict(item) for item in store._c.execute(
        'SELECT seq,review_id,cost_usd,total_tokens FROM api_spend_events ORDER BY seq DESC LIMIT 2001')]
    spend_truncated = len(spend_rows) > 2000
    projected, resources = [], {}
    output_bytes = 0
    for row in rows[:limit]:
        item = _request(store, row, now, spend_rows[:2000], spend_truncated, resources)
        encoded_size = len(json.dumps(item, ensure_ascii=True).encode())
        if encoded_size > 256 * 1024:
            for admission in item['admissions']:
                admission['holder_count_observed'] = len(admission['holders'])
                admission['holders'] = []
                admission['holders_truncated'] = True
            item['coverage']['status'] = 'partial'
            item['coverage']['output_truncated'] = True
            encoded_size = len(json.dumps(item, ensure_ascii=True).encode())
        if output_bytes + encoded_size > 2 * 1024 * 1024 - 8192:
            truncated = True
            break
        projected.append(item)
        output_bytes += encoded_size
    return {'schema_version': 'request_queue_v1', 'observed_at': now,
            'scope': 'explicit_request' if request_id else scope,
            'requests': projected,
            'coverage': {'requests_scanned': scanned, 'request_limit': limit,
                         'resource_scope_limit': 100, 'output_limit_bytes': 2 * 1024 * 1024,
                         'requests_truncated': truncated}}


def render(data, output='text'):
    if output == 'json':
        return json.dumps(data, sort_keys=True, ensure_ascii=True)
    if output != 'text':
        raise ValueError('output must be text or json')
    # One JSON-quoted row per request keeps arbitrary branch/path text inert.
    # Both formats retain every field; no second cost/timing implementation.
    return '\n'.join([f"SKODUN QUEUE: scope={data['scope']} observed_at={data['observed_at']}",
        *('request=' + json.dumps(row, sort_keys=True, ensure_ascii=True) for row in data['requests']),
        'coverage=' + json.dumps(data['coverage'], sort_keys=True)])


def augment_stats(store, data, *, now=None):
    """Explicit audit denominators and bounded call observations for stats."""
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    since = data['since']
    window = {'from': since, 'to': now}
    for name, metric in data['timing'].items():
        metric.update(sample_count=metric['count'], unit='ms', window=window,
            denominator='review records' if name == 'review_ms' else 'capacity admissions',
            quantile_method='nearest_rank', small_sample=metric['count'] < 3)
    records = store._c.execute(
        'SELECT mode,orchestration_id,artifact_json FROM reviews '
        'WHERE COALESCE(review_started_at,reviewed_at)>=? ORDER BY reviewed_at DESC LIMIT 201',
        (since,)).fetchall()
    reviews = [_json_object(row['artifact_json']) for row in records[:200]]
    _attempts, observed = _calls(reviews)
    namespaces = store._c.execute(
        "SELECT COUNT(DISTINCT orchestration_id), COUNT(DISTINCT CASE WHEN json_valid(artifact_json) "
        "THEN json_extract(artifact_json,'$.batch_orchestration_id') END) FROM reviews "
        'WHERE COALESCE(review_started_at,reviewed_at)>=?', (since,)).fetchone()
    request_count = store._c.execute('SELECT COUNT(*) FROM review_requests WHERE created_at>=?',
                                    (since,)).fetchone()[0]
    executions = store._c.execute('SELECT COUNT(*) FROM request_executions WHERE started_at>=?',
                                 (since,)).fetchone()[0]
    data['audit_denominators'] = {'window': window,
        'requested_reviews_created': request_count, 'request_executions_started': executions,
        'review_records': data['reviews']['total'],
        'recovery_orchestration_ids': namespaces[0], 'batch_orchestration_ids': namespaces[1],
        'review_modes': {row['mode'] or 'unknown': row['n'] for row in store._c.execute(
            'SELECT mode,COUNT(*) AS n FROM reviews WHERE COALESCE(review_started_at,reviewed_at)>=? GROUP BY mode',
            (since,))},
        'namespace_note': 'Recovery IDs are not batch IDs or a request denominator.'}
    counts_complete = not (len(records) > 200 or observed['attempt_identity_missing']
                           or observed['launch_unknown'] or observed['attempts_truncated']
                           or observed['missing_attempt_scopes'])
    if not counts_complete:
        observed['launched_calls'] = None
        observed['token_usage']['total'] = None
        observed['token_usage']['complete'] = False
        observed['aggregate_launched_prompt_bytes'] = None
        observed['max_per_call_prompt_bytes'] = None
        for kind in ('input', 'output', 'cache', 'reasoning'):
            observed['token_usage'][kind] = None
    data['call_observations'] = {**observed, 'counts_complete': counts_complete, 'window': window,
        'sample_count': len(reviews), 'denominator': 'most recent review records in window',
        'review_limit': 200, 'reviews_truncated': len(records) > 200,
        'external_gate_lock_wait_ms': None,
        'scope': 'bounded deduplicated attempt observations; not subscription dollars'}
    return data
