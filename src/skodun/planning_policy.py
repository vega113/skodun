"""Versioned sizing identity for planning/checkpoint/reuse, outside gate policy."""
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


def describe(defaults, reviewer=None):
    payload = {'version': VERSION, 'target_bytes': defaults.batch_target_bytes,
               'effective_diff_budget': effective_diff_budget(defaults, reviewer),
               'prompt_envelope': budget.prompt_budget(defaults, reviewer),
               'context_pack': defaults.context_pack, 'capability': capability(reviewer)}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**payload, 'digest': digest}


def mismatch(stored, expected):
    if not isinstance(stored, dict) or stored.get('version') != VERSION:
        return 'planning_identity_missing'
    if stored.get('target_bytes') != expected['target_bytes']:
        return 'operational_target_changed'
    return None if stored == expected else 'planning_policy_changed'
