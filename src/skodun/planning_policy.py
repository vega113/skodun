"""Versioned sizing identity for planning/checkpoint/reuse, outside gate policy.

``describe`` returns validated scalar sizing/capability facts with a stable digest;
``validate`` rejects malformed persisted projections (None is legacy/unknown).
``mismatch`` classifies missing identity, changed raw target, or other policy
changes. Budget helpers preserve configured ceilings and never consult history.
These helpers do not write state, launch providers or affect gate/trust policy.
"""
import hashlib
import json

from . import budget

VERSION = 'review-planning/v1'


def diff_budget(defaults, reviewer=None):
    envelope = budget.prompt_budget(defaults, reviewer)
    return max(1, envelope // 2 if defaults.context_pack else envelope)


def effective_diff_budget(defaults, reviewer=None):
    ceiling = diff_budget(defaults, reviewer)
    return min(ceiling, defaults.batch_target_bytes) if defaults.batch_target_bytes > 0 else ceiling


def capability(reviewer):
    if reviewer is None:
        return None
    from .adapters import get_adapter
    adapter = get_adapter(reviewer.provider)
    return {'provider': reviewer.provider,
            'version': getattr(adapter, 'prompt_capability_version', None),
            'transport': getattr(adapter, 'prompt_transport', None),
            'limit_bytes': adapter.prompt_limit()}


def execution_policy(defaults, batch_concurrency=1):
    """Bounded execution knobs; tool configuration is represented only by hash."""
    validate_concurrency(batch_concurrency)
    result = {'max_turns': defaults.max_turns,
              'deny_tools_hash': hashlib.sha256(defaults.deny_tools.encode()).hexdigest()}
    if batch_concurrency != 1:
        result['batch_concurrency'] = batch_concurrency
    return result


def describe(defaults, reviewer=None, *, batch_concurrency=1):
    payload = {'version': VERSION, 'target_bytes': defaults.batch_target_bytes,
               'effective_diff_budget': effective_diff_budget(defaults, reviewer),
               'prompt_envelope': budget.prompt_budget(defaults, reviewer),
               'context_pack': defaults.context_pack, 'capability': capability(reviewer),
               'execution_policy': execution_policy(defaults, batch_concurrency)}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**payload, 'digest': digest}


def mismatch(stored, expected):
    try:
        validate(stored)
    except ValueError:
        return 'planning_identity_missing'
    if not isinstance(stored, dict) or stored.get('version') != VERSION:
        return 'planning_identity_missing'
    if stored['execution_policy'].get('batch_concurrency', 1) != expected['execution_policy'].get('batch_concurrency', 1):
        return 'batch_concurrency_changed'
    if stored.get('target_bytes') != expected['target_bytes']:
        return 'operational_target_changed'
    return None if stored == expected else 'planning_policy_changed'


def validate(value):
    """Validate persisted v1 policy shape and digest; retain None as unknown."""
    if value is None:
        return
    keys = {'version', 'target_bytes', 'effective_diff_budget', 'prompt_envelope', 'context_pack', 'capability', 'execution_policy', 'digest'}
    if not isinstance(value, dict) or set(value) != keys or value['version'] != VERSION:
        raise ValueError('invalid planning policy fields/version')
    if any(type(value[key]) is not int or value[key] < minimum for key, minimum in (
            ('target_bytes', 0), ('effective_diff_budget', 1), ('prompt_envelope', 1))) or type(value['context_pack']) is not bool:
        raise ValueError('invalid planning policy sizing types')
    execution = value['execution_policy']
    if (not isinstance(execution, dict) or set(execution) not in ({'max_turns', 'deny_tools_hash'}, {'max_turns', 'deny_tools_hash', 'batch_concurrency'})
            or type(execution['max_turns']) is not int or execution['max_turns'] < 1
            or not isinstance(execution['deny_tools_hash'], str) or len(execution['deny_tools_hash']) != 64
            or any(char not in '0123456789abcdef' for char in execution['deny_tools_hash'])):
        raise ValueError('invalid planning execution policy')
    validate_concurrency(execution.get('batch_concurrency', 1))
    cap = value['capability']
    if cap is not None:
        if (not isinstance(cap, dict) or set(cap) != {'provider', 'version', 'transport', 'limit_bytes'}
                or not isinstance(cap['provider'], str) or not cap['provider']
                or any(cap[key] is not None and (not isinstance(cap[key], str) or not cap[key]) for key in ('version', 'transport'))
                or (cap['limit_bytes'] is not None and (type(cap['limit_bytes']) is not int or cap['limit_bytes'] < 1))):
            raise ValueError('invalid planning policy capability')
    payload = {key: item for key, item in value.items() if key != 'digest'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if value['digest'] != digest:
        raise ValueError('invalid planning policy digest')


def validate_concurrency(value):
    if type(value) is not int or value not in (1, 2):
        raise ValueError('batch_concurrency must be 1 or 2')
    return value
