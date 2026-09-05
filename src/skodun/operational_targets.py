"""Advisory measured targets from complete, uncensored observed cohorts.

These thresholds are policy, not confidence guarantees. All matching outcomes
enter their size cohort; missing sizes cannot be silently discarded to select
only successful work. No providers, probes, configuration writes or predictions.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from itertools import islice

from .store import _is_canonical_ts
from .planning_policy import VERSION, capability, validate
from .telemetry import attempt_launched

WINDOW_DAYS = 30
MIN_CALLS = 20
MIN_REQUESTS = 5
RECORD_LIMIT = 1000
ATTEMPT_LIMIT = 5000


def _positive_int(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _duration(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _sources(record):
    if record.get('batched') or record.get('batches'):
        for batch in record.get('batches') or ():
            if isinstance(batch, dict):
                yield 'batch', batch
    else:
        yield 'primary', record


def _attempt_matches(item, reviewer):
    """False means known unrelated; None means attribution is incomplete."""
    if not isinstance(item, dict):
        return None
    for key, expected in (('provider', reviewer.provider), ('model', reviewer.model), ('effort', reviewer.effort)):
        value = item.get(key)
        if isinstance(value, str) and value and value != expected:
            return False
    if (not isinstance(item.get('provider'), str) or not item['provider']
            or not isinstance(item.get('model'), str) or not item['model']
            or 'effort' not in item or (item['effort'] is not None and not isinstance(item['effort'], str))):
        return None
    return (item['provider'], item['model'], item['effort']) == (reviewer.provider, reviewer.model, reviewer.effort)


def evidence(records, *, reviewer, mode, execution_policy, context_pack=False, now=None):
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if not _is_canonical_ts(now):
        raise ValueError('now must be canonical UTC')
    since = (datetime.strptime(now, '%Y-%m-%dT%H:%M:%SZ') - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = list(islice(records, RECORD_LIMIT + 1))
    groups, seen, request_parents = {}, {}, {}
    def request_root(request_id):
        request_parents.setdefault(request_id, request_id)
        while request_parents[request_id] != request_id:
            request_id = request_parents[request_id]
        return request_id
    def join_requests(left, right):
        left, right = request_root(left), request_root(right)
        request_parents[max(left, right)] = min(left, right)
    unsupported = execution_mismatched = 0
    invalid = skipped = duplicates = conflicts = scanned = unfinished = 0
    for record in rows[:RECORD_LIMIT]:
        if not isinstance(record, dict):
            invalid += 1
            continue
        if record.get('mode') != mode:
            continue
        at = record.get('review_started_at') or record.get('reviewed_at')
        if _is_canonical_ts(at) and not since <= at <= now:
            continue
        policy = record.get('planning_policy')
        if isinstance(policy, dict) and isinstance(policy.get('version'), str) and policy['version'] != VERSION:
            unsupported += 1
            continue
        try:
            validate(policy)
            if policy is None or policy['capability'] is None:
                raise ValueError('missing current planning capability')
        except ValueError:
            invalid += 1
            continue
        if policy['capability'] != capability(reviewer) or policy['context_pack'] != context_pack:
            continue
        if policy['execution_policy'] != execution_policy:
            execution_mismatched += 1
            continue
        if not _is_canonical_ts(at):
            invalid += 1
            continue
        if (record.get('batched') or record.get('batches')) and (not isinstance(record.get('batches'), list) or any(not isinstance(batch, dict) for batch in record['batches'])):
            invalid += 1
            continue
        if record.get('batched') and type(record.get('batch_count')) is int and record['batch_count'] > len(record.get('batches') or ()):
            invalid += 1
        if record.get('status') == 'running':
            rows_for_scope = [item for _, source in _sources(record) for item in (source.get('attempts') or ())]
            if not rows_for_scope or any(_attempt_matches(item, reviewer) is not False for item in rows_for_scope):
                unfinished += 1
            continue
        for kind, source in _sources(record):
            attempts = source.get('attempts')
            if not isinstance(attempts, list):
                invalid += 1
                continue
            launched = [item for item in attempts if isinstance(item, dict) and attempt_launched(item) is True]
            for item in attempts:
                scanned += 1
                if scanned > ATTEMPT_LIMIT:
                    break
                attribution = _attempt_matches(item, reviewer)
                if attribution is False:
                    continue
                if attribution is None:
                    invalid += 1
                    continue
                if attempt_launched(item) is False:
                    skipped += 1
                    continue
                aid, request_id = item.get('attempt_id'), record.get('request_id')
                input_bytes, diff_bytes = item.get('input_bytes'), source.get('diff_bytes')
                duration = item.get('duration_sec')
                telemetry = source.get('telemetry') or {}
                byte_dimensions = telemetry.get('bytes') if isinstance(telemetry, dict) else None
                byte_dimensions = byte_dimensions if isinstance(byte_dimensions, dict) else {}
                context_bytes = byte_dimensions.get('context') if kind == 'batch' else source.get('context_bytes')
                timeout = record.get('timeout_seconds') if kind == 'primary' else None
                if kind == 'batch' and isinstance(telemetry, dict):
                    paired = [row for row in telemetry.get('attempts', ()) if isinstance(row, dict) and row.get('attempt_id') == aid]
                    if len(paired) == 1:
                        timeout = paired[0].get('timeout_sec')
                if (attempt_launched(item) is not True or not isinstance(aid, str) or not aid
                        or len(aid) > 256 or not isinstance(request_id, str) or not request_id or len(request_id) > 256
                        or not _positive_int(input_bytes) or not _positive_int(diff_bytes)
                        or not _positive_int(timeout) or not _duration(duration) or type(context_bytes) is not int or context_bytes < 0):
                    invalid += 1
                    continue
                if input_bytes < diff_bytes and source.get('diff_truncated') is False:
                    invalid += 1
                    continue
                classification = item.get('classification')
                classification = classification if isinstance(classification, dict) else {}
                censored = item.get('timed_out') is True
                failure = censored or (type(item.get('rc')) is int and item['rc'] != 0) or classification.get('kind') in ('degraded', 'unavailable')
                final = bool(launched) and item is launched[-1]
                flags_known = all(type(source.get(key)) is bool for key in ('parse_ok', 'degraded', 'diff_truncated'))
                if final and flags_known:
                    failure |= not source['parse_ok'] or source['degraded'] or source['diff_truncated']
                outcome_known = failure or (final and flags_known and item.get('rc') == 0
                    and item.get('timed_out') is False and classification.get('kind') == 'ok')
                sample = {'attempt_id': aid, 'request_id': request_id, 'review_id': record.get('id'),
                          'input_bytes': input_bytes, 'diff_bytes': diff_bytes, 'context_bytes': context_bytes, 'duration_sec': duration,
                          'timeout_seconds': timeout, 'censored': censored, 'failure': bool(failure), 'outcome_known': bool(outcome_known),
                          'kind': kind, 'at': at,
                          'skodun_commit': record.get('skodun_commit') if isinstance(record.get('skodun_commit'), str) else None,
                          'executable_version': (item.get('execution_provenance') or {}).get('version')
                              if isinstance(item.get('execution_provenance'), dict) else None}
                # Copied checkpoint observations are one actual model call.
                signature = {key: value for key, value in sample.items() if key not in ('review_id', 'at', 'request_id', 'skodun_commit')}
                if aid in seen:
                    duplicates += 1
                    conflicts += int(seen[aid][0] != signature)
                    join_requests(seen[aid][1], request_id)
                    continue
                seen[aid] = (signature, request_id)
                request_root(request_id)
                bucket = 1 << (input_bytes - 1).bit_length()
                groups.setdefault((kind, bucket), []).append(sample)
            if scanned > ATTEMPT_LIMIT:
                break
        if scanned > ATTEMPT_LIMIT:
            break
    truncated = len(rows) > RECORD_LIMIT or scanned > ATTEMPT_LIMIT
    cohorts = []
    for (kind, upper), samples in sorted(groups.items()):
        failures = sum(item['failure'] for item in samples)
        censored = sum(item['censored'] for item in samples)
        incomplete = sum(not item['outcome_known'] for item in samples)
        for item in samples:
            item['request_id'] = request_root(item['request_id'])
        request_count = len({item['request_id'] for item in samples})
        reasons = []
        if len(samples) < MIN_CALLS:
            reasons.append('insufficient_calls')
        if request_count < MIN_REQUESTS:
            reasons.append('insufficient_requests')
        if failures:
            reasons.append('observed_failures')
        if censored:
            reasons.append('censored_outcomes')
        if incomplete or invalid or conflicts or truncated or unfinished:
            reasons.append('incomplete_evidence')
        durations = [item['duration_sec'] for item in samples if item['outcome_known'] and not item['failure']]
        samples.sort(key=lambda sample: sample['attempt_id'])
        cohort_digest = hashlib.sha256(json.dumps(samples, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        cohorts.append({'pass_kind': kind, 'input_bucket_max_bytes': upper,
            'target_bytes': max(item['diff_bytes'] for item in samples),
            'context_min_bytes': min(item['context_bytes'] for item in samples),
            'context_max_bytes': max(item['context_bytes'] for item in samples),
            'timeout_seconds': sorted({item['timeout_seconds'] for item in samples}),
            'input_min_bytes': min(item['input_bytes'] for item in samples),
            'input_max_bytes': max(item['input_bytes'] for item in samples),
            'sample_count': len(samples), 'request_count': request_count,
            'failure_count': failures, 'censored_count': censored, 'incomplete_count': incomplete,
            'qualified': not reasons, 'reasons': reasons, 'sample_digest': cohort_digest,
            'provenance': {'skodun_commits': sorted({item['skodun_commit'] for item in samples if item['skodun_commit']}),
                'executable_versions': sorted({item['executable_version'] for item in samples if isinstance(item['executable_version'], str) and item['executable_version']}),
                'unknown_executable_version_count': sum(not isinstance(item['executable_version'], str) or not item['executable_version'] for item in samples)},
            'sample_ids': [item['attempt_id'] for item in samples[:25]],
            'sample_ids_truncated': len(samples) > 25,
            'historical_duration_sec': {'p25': _percentile(durations, .25),
                'p50': _percentile(durations, .50), 'p90': _percentile(durations, .90),
                'sample_count': len(durations), 'unit': 'seconds', 'method': 'nearest_rank',
                'denominator': 'successful observed calls (qualification also checks every failure)',
                'window': {'from': since, 'to': now}}})
    return {'window': {'from': since, 'to': now, 'days': WINDOW_DAYS},
        'minimum_calls': MIN_CALLS, 'minimum_requests': MIN_REQUESTS,
        'provider': reviewer.provider, 'model': reviewer.model, 'effort': reviewer.effort, 'mode': mode,
        'context_pack': context_pack, 'planning_version': VERSION, 'capability': capability(reviewer),
        'records_scanned': min(len(rows), RECORD_LIMIT), 'attempts_scanned': min(scanned, ATTEMPT_LIMIT),
        'truncated': truncated, 'incomplete_rows': invalid, 'duplicate_rows': duplicates,
        'unsupported_policy_records': unsupported,
        'execution_policy': execution_policy, 'execution_policy_mismatched_records': execution_mismatched,
        'conflicting_attempt_ids': conflicts, 'candidate_skips': skipped, 'unfinished_records_excluded': unfinished, 'cohorts': cohorts,
        'note': 'Advisory observations; thresholds are not confidence guarantees and ranges are not forecasts.'}


def read_evidence(store, *, reviewer, mode, execution_policy, context_pack=False, now=None):
    if store is None:
        result = evidence([], reviewer=reviewer, mode=mode, execution_policy=execution_policy, context_pack=context_pack, now=now)
        return {**result, 'status': 'unavailable'}
    try:
        rows = store._c.execute('SELECT artifact_json FROM reviews ORDER BY rowid DESC LIMIT ?',
                                (RECORD_LIMIT + 1,)).fetchall()
        records = []
        for row in rows:
            try:
                records.append(json.loads(row['artifact_json']))
            except (TypeError, ValueError):
                records.append(None)
        return {**evidence(records, reviewer=reviewer, mode=mode, execution_policy=execution_policy, context_pack=context_pack, now=now), 'status': 'available'}
    except Exception as exc:
        return {**evidence([], reviewer=reviewer, mode=mode, execution_policy=execution_policy, context_pack=context_pack, now=now),
                'status': 'unavailable', 'reason_code': type(exc).__name__}


def candidates(data, *, latency_seconds, hard_diff_ceiling, diff_bytes):
    if latency_seconds is None:
        return []
    if not _duration(latency_seconds) or latency_seconds <= 0 or latency_seconds > 86400:
        raise ValueError('target-latency-seconds must be positive, finite and at most 86400')
    return sorted((cohort for cohort in data['cohorts'] if cohort['qualified']
        and cohort['target_bytes'] <= hard_diff_ceiling
        and cohort['pass_kind'] == ('batch' if diff_bytes > cohort['target_bytes'] else 'primary')
        and cohort['historical_duration_sec']['p90'] <= latency_seconds),
        key=lambda cohort: cohort['target_bytes'], reverse=True)
