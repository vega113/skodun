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
    counts = {'reused': 0, 'executed': 0, 'failed': 0}
    for kind, index, row in sources:
        action = row.get('continuation_action') or (row.get('provenance') or {}).get('continuation_action')
        if action in counts:
            counts[action] += 1
            if len(passes) < 128:
                passes.append({'kind': kind, 'index': index, 'action': action})
    fields = {key: base.get(key) for key in ('policy', 'status', 'source_orchestration_id',
              'orchestration_id', 'first_mismatch')}
    return {**fields, 'passes': passes, 'counts': counts,
            'passes_truncated': sum(counts.values()) > len(passes)}


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
    if set(value) - base_fields - {'passes','counts','passes_truncated'}:
        return False
    counts = value.get('counts')
    if (not isinstance(counts, dict) or set(counts) != {'reused','executed','failed'} or
            any(type(n) is not int or n < 0 for n in counts.values())):
        return False
    passes = value.get('passes')
    if not isinstance(passes, list) or len(passes) > 128 or type(value.get('passes_truncated')) is not bool:
        return False
    return all(isinstance(item, dict) and item.get('kind') in ('batch','integration')
               and type(item.get('index')) is int and item['index'] >= 0
               and item.get('action') in ('reused','executed','failed') for item in passes)
