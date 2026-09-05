"""Exact semantic input identities for conditional required follow-up passes.

These are orchestration data, never review coverage. Operational generation and
attempt timing annotations cannot change semantic evidence identity.
"""
from dataclasses import asdict
import json
import re

from .checkpoints import CheckpointPayload, canonical_digest

KINDS = ('security', 'skeptic')
PLANNER_VERSION = 'skodun-batch-followups-v1'
MAX_BINDING_BYTES = 262144


def table_for(kind):
    if kind in KINDS:
        return 'review_followup_checkpoints'
    if kind in ('batch', 'integration'):
        return 'review_checkpoints'
    raise ValueError('unknown checkpoint pass kind')


def usable(payload):
    """Extra-pass size caps annotate partial coverage; they do not demote."""
    value = payload.as_dict()
    return (value['parse_ok'] is True and value['degraded'] is False
            and not value['failure_reason'])


def semantic_payload(payload):
    value = payload.as_dict()
    provenance = value['accepted'] or value['provenance']
    return {**{key: value[key] for key in (
        'parse_ok', 'degraded', 'degraded_reason', 'stop_reason',
        'diff_truncated', 'summary', 'findings', 'failure_reason')},
        'actual': {key: provenance.get(key) for key in ('provider', 'model', 'effort', 'adapter_name')},
        'execution': next(({key: attempt['execution_provenance'].get(key) for key in
                           ('adapter', 'resolved', 'version', 'override_source')}
                          for attempt in reversed(value['attempts'])
                          if 'skipped' not in attempt and isinstance(attempt.get('execution_provenance'), dict)), None)}


def dependencies(rows, kind):
    result = []
    for row in rows:
        role = row['pass_kind']
        if role == 'skeptic' or (role == 'security' and kind == 'security'):
            continue
        payload = row.get('payload_json')
        semantic = semantic_payload(CheckpointPayload(payload)) if payload else None
        result.append({key: row.get(key) for key in (
            'pass_kind', 'pass_index', 'diff_hash', 'boundary_hash', 'prompt_hash')}
            | {'output_hash': canonical_digest(semantic),
               'binding_hash': row.get('binding_hash'),
               'provenance_known': (bool(semantic['actual']['provider']) if semantic is not None
                   else role == 'security' and row.get('binding_json') is not None
                   and not decode_binding(row['binding_json'])['decision']['scheduled'])})
    return result


def build_binding(identity, kind, rows, aggregate, *, scheduled, reason, prompt):
    content = asdict(identity)
    content.pop('continuation_source', None)
    snapshot = {key: aggregate.get(key) for key in (
        'parse_ok', 'degraded', 'diff_truncated', 'findings_total', 'findings')}
    body = {'version': 'followup-input/v1', 'kind': kind,
            'content_hash': canonical_digest(content),
            'dependencies': dependencies(rows, kind),
            'aggregate_hash': canonical_digest(snapshot),
            'decision': {'scheduled': scheduled, 'required': scheduled, 'reason': reason},
            'prompt_identity': prompt}
    return validate_binding(body)


def _digest(value, *, lengths=(64,), optional=False):
    if optional and value is None:
        return
    if (not isinstance(value, str) or len(value) not in lengths
            or re.fullmatch(r'[0-9a-f]+', value) is None):
        raise ValueError('invalid canonical follow-up digest')


def validate_binding(body):
    if not isinstance(body, dict) or set(body) != {
            'version', 'kind', 'content_hash', 'dependencies', 'aggregate_hash', 'decision', 'prompt_identity'}:
        raise ValueError('invalid follow-up binding fields')
    if body['version'] != 'followup-input/v1' or body['kind'] not in KINDS:
        raise ValueError('invalid follow-up binding kind/version')
    decision = body['decision']
    if (not isinstance(decision, dict) or set(decision) != {'scheduled', 'required', 'reason'}
            or type(decision['scheduled']) is not bool or decision['required'] is not decision['scheduled']
            or decision['reason'] not in ('scheduled', 'policy_not_scheduled', 'aggregate_not_clean', 'preparation_failed')):
        raise ValueError('invalid follow-up decision')
    if decision['scheduled'] != (decision['reason'] in ('scheduled', 'preparation_failed')):
        raise ValueError('inconsistent follow-up decision')
    for key in ('content_hash', 'aggregate_hash'):
        _digest(body[key])
    deps = body['dependencies']
    if not isinstance(deps, list) or len(deps) > 10000:
        raise ValueError('invalid follow-up dependencies')
    seen = set()
    for dep in deps:
        if not isinstance(dep, dict) or set(dep) != {
                'pass_kind', 'pass_index', 'diff_hash', 'boundary_hash', 'prompt_hash', 'output_hash', 'binding_hash', 'provenance_known'}:
            raise ValueError('invalid follow-up dependency fields')
        kind, index = dep['pass_kind'], dep['pass_index']
        if kind not in ('batch', 'integration', 'security') or type(index) is not int or index < 0:
            raise ValueError('invalid follow-up dependency identity')
        if ((kind == 'batch' and index < 1) or (kind != 'batch' and index != 0)
                or type(dep['provenance_known']) is not bool):
            raise ValueError('invalid follow-up dependency index/provenance')
        if (kind, index) in seen:
            raise ValueError('duplicate follow-up dependency')
        seen.add((kind, index))
        _digest(dep['diff_hash'], lengths=(40, 64))
        _digest(dep['boundary_hash'])
        _digest(dep['prompt_hash'], lengths=(40, 64), optional=True)
        _digest(dep['output_hash'])
        _digest(dep['binding_hash'], optional=True)
    prompt = body['prompt_identity']
    if prompt is not None and (not isinstance(prompt, dict) or set(prompt) != {
            'hash', 'bytes', 'diff_truncated'} or not isinstance(prompt['hash'], str)
            or type(prompt['bytes']) is not int or prompt['bytes'] < 0
            or type(prompt['diff_truncated']) is not bool):
        raise ValueError('invalid follow-up prompt identity')
    if prompt is not None:
        _digest(prompt['hash'], lengths=(40, 64))
    if not decision['scheduled'] and prompt is not None:
        raise ValueError('unscheduled follow-up has prompt identity')
    if decision['scheduled'] and prompt is None and decision['reason'] != 'preparation_failed':
        raise ValueError('scheduled follow-up prompt identity missing')
    from .checkpoints import _scan_json
    _scan_json(body, label='follow-up binding')
    encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > MAX_BINDING_BYTES:
        raise ValueError('follow-up binding exceeds bound')
    return json.loads(encoded)


def decode_binding(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_BINDING_BYTES:
        raise ValueError('invalid follow-up binding serialization')
    return validate_binding(json.loads(text))


def binding_mismatch(old, new):
    for key, reason in (('dependencies', 'followup_upstream_changed'),
                        ('decision', 'followup_schedule_changed'),
                        ('prompt_identity', 'followup_prompt_changed')):
        if old[key] != new[key]:
            return reason
    return 'followup_upstream_changed'
