"""Explicit continuation policy and bounded generation receipts.

A child generation copies only usable exact checkpoints. The parent is immutable,
fresh opinions remain independent, and reporting never certifies coverage.
"""


class ContinuationRefused(ValueError):
    """Stable store refusal without promoting arbitrary exception prose to a code."""
    def __init__(self, reason_code):
        super().__init__(reason_code)
        self.reason_code = reason_code


def validation_error(enabled, *, fresh=False, reuse_trusted=False, request_key=None):
    if type(enabled) is not bool:
        return 'continue_compatible must be a boolean'
    if enabled and (fresh or reuse_trusted or request_key is not None):
        return 'compatible continuation cannot combine fresh, trusted reuse, or a request key'
    return None


def receipt(rec):
    base = rec.get('continuation')
    if not isinstance(base, dict):
        return None
    passes = []
    sources = [('batch', row.get('index'), row) for row in rec.get('batches', [])]
    if isinstance(rec.get('integration'), dict):
        sources.append(('integration', 0, rec['integration']))
    for kind in ('security', 'skeptic'):
        value = (rec.get('extra_passes') or {}).get(kind)
        if isinstance(value, dict):
            sources.append((kind, 0, value))
    counts = {'reused': 0, 'executed': 0, 'failed': 0}
    for kind, index, row in sources:
        action = row.get('continuation_action') or (row.get('provenance') or {}).get('continuation_action')
        if action in counts:
            counts[action] += 1
            if len(passes) < 128:
                item = {'kind': kind, 'index': index, 'action': action}
                if kind in ('security', 'skeptic') and row.get('continuation_reason'):
                    item['reason'] = row['continuation_reason']
                passes.append(item)
    fields = {key: base.get(key) for key in ('policy', 'status', 'source_orchestration_id',
              'orchestration_id', 'first_mismatch')}
    skipped = [{'kind': kind, 'index': 0, 'reason': decision['reason']}
               for kind, decision in (rec.get('followup_decisions') or {}).items()
               if kind in ('security', 'skeptic') and decision.get('scheduled') is False]
    return {**fields, 'passes': passes, 'counts': counts,
            'passes_truncated': sum(counts.values()) > len(passes),
            **({'skipped_passes': skipped} if skipped else {})}


def refuse(reason_code, message, *, first_mismatch=None):
    from .pipeline import PreflightRefused
    error = PreflightRefused(message)
    error.reason_code = reason_code
    error.continuation = {'policy': 'compatible', 'status': 'refused',
                          'first_mismatch': first_mismatch}
    raise error



def valid_receipt(value):
    """Validate the bounded extension before replaying it to automation."""
    if value is None:
        return True
    if not isinstance(value, dict) or value.get('policy') != 'compatible':
        return False
    if value.get('status') not in ('continued', 'refused'):
        return False
    for field in ('source_orchestration_id', 'orchestration_id', 'first_mismatch'):
        if value.get(field) is not None and (not isinstance(value[field], str) or len(value[field]) > 512):
            return False
    base_fields = {'policy','status','source_orchestration_id','orchestration_id','first_mismatch'}
    if value['status'] == 'refused':
        return set(value) <= base_fields
    if set(value) - base_fields - {'passes','counts','passes_truncated','skipped_passes'}:
        return False
    counts = value.get('counts')
    if (not isinstance(counts, dict) or set(counts) != {'reused','executed','failed'} or
            any(type(n) is not int or n < 0 for n in counts.values())):
        return False
    passes = value.get('passes')
    if not isinstance(passes, list) or len(passes) > 128 or type(value.get('passes_truncated')) is not bool:
        return False
    if (not isinstance(value.get('source_orchestration_id'), str) or not value['source_orchestration_id']
            or not isinstance(value.get('orchestration_id'), str) or not value['orchestration_id']
            or value['source_orchestration_id'] == value['orchestration_id']
            or value.get('first_mismatch') is not None):
        return False
    seen = set()
    observed = dict.fromkeys(counts, 0)
    for item in passes:
        if not isinstance(item, dict) or set(item) not in ({'kind','index','action'}, {'kind','index','action','reason'}):
            return False
        if 'reason' in item and (item.get('kind') not in ('security','skeptic') or item['reason'] not in (
                'followup_upstream_changed','followup_schedule_changed','followup_prompt_changed','followup_candidate_unusable')):
            return False
        kind, index, action = item['kind'], item['index'], item['action']
        if (kind not in ('batch','integration','security','skeptic') or type(index) is not int
                or not isinstance(action, str) or action not in observed):
            return False
        if (kind == 'batch' and index < 1) or (kind != 'batch' and index != 0):
            return False
        if (kind, index) in seen:
            return False
        seen.add((kind, index))
        observed[action] += 1
    skipped = value.get('skipped_passes', [])
    if not isinstance(skipped, list) or len(skipped) > 2:
        return False
    for item in skipped:
        if (not isinstance(item, dict) or set(item) != {'kind', 'index', 'reason'}
                or item['kind'] not in ('security', 'skeptic') or type(item['index']) is not int
                or item['index'] != 0 or item['reason'] not in (
                    'followup_upstream_changed', 'followup_schedule_changed',
                    'followup_prompt_changed', 'followup_candidate_unusable')
                or (item['kind'], 0) in seen):
            return False
        seen.add((item['kind'], 0))
    if not value['passes_truncated']:
        return counts == observed
    return (len(passes) == 128 and sum(counts.values()) > len(passes)
            and all(counts[action] >= number for action, number in observed.items()))
