"""Execution-fenced request budgets and immutable capacity attribution.

Snapshots and capacity observations are advisory execution facts. Their writes
share the owning Store transaction/connection and cannot be made by an old owner
or execution. Tokens are guard inputs only, never stored in these projections.
"""
from contextlib import contextmanager
import json
import math

MIGRATION = (
    """CREATE TABLE IF NOT EXISTS request_budget_snapshots (
      request_id TEXT NOT NULL REFERENCES review_requests(id),
      execution_seq INTEGER NOT NULL REFERENCES request_executions(seq),
      snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(request_id,execution_seq)
    )""",
    """CREATE TABLE IF NOT EXISTS request_capacity_layers (
      admission_id TEXT PRIMARY KEY REFERENCES capacity_admissions(id),
      request_id TEXT NOT NULL REFERENCES review_requests(id),
      execution_seq INTEGER NOT NULL REFERENCES request_executions(seq),
      resource_class TEXT NOT NULL, scope TEXT NOT NULL,
      effective_capacity INTEGER NOT NULL, configured_capacity INTEGER,
      legacy_dual_hold INTEGER, updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS ix_request_capacity_execution
       ON request_capacity_layers(request_id,execution_seq,admission_id)""",
)

MAX_SNAPSHOT_BYTES = 65536
MAX_LAYERS = 1000
_LIMITS = ('max_queue_seconds', 'max_review_seconds', 'max_provider_wait_seconds', 'max_wall_seconds')
_DEADLINES = ('queue', 'review', 'total', 'provider_wait')
_TIMING = ('queue_wait_ms', 'provider_wait_ms', 'review_wall_ms', 'review_active_ms', 'total_ms')
_FIELDS = frozenset(('scope', 'request_id', 'execution_seq', 'phase', 'limits',
                    'deadlines', 'timing', 'provider_waits', 'review_paused_for_queue', 'reason_code', 'updated_at'))


class BudgetDataError(ValueError):
    """Malformed persisted budget data; never silently reinterpret it."""


def _text(label, value, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f'{label} must be bounded nonempty text')
    return value


def _seq(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError('execution_seq must be a positive integer')
    return value


def _number(label, value):
    try:
        valid = value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f'{label} must be finite and nonnegative or null')
    return value


def _capacity(label, value, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f'{label} must be a nonnegative integer')
    return value


def _timestamp(label, value, *, optional=False):
    from .store import _require_ts
    return None if value is None and optional else _require_ts(label, value)


def _object(label, value, keys):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError(f'{label} must contain only supported fields')
    return value


def _snapshot(request_id, execution_seq, value):
    value = _object('snapshot', value, _FIELDS)
    if value.get('scope', 'request_execution') != 'request_execution':
        raise ValueError('snapshot scope must be request_execution')
    if value.get('request_id', request_id) != request_id:
        raise ValueError('snapshot request_id mismatch')
    echoed = value.get('execution_seq', execution_seq)
    if _seq(echoed) != execution_seq:
        raise ValueError('snapshot execution_seq mismatch')
    limits = _object('limits', value.get('limits'), _LIMITS)
    deadlines = _object('deadlines', value.get('deadlines'), _DEADLINES)
    timing = _object('timing', value.get('timing'), _TIMING)
    paused = value.get('review_paused_for_queue')
    if paused is not None and type(paused) is not bool:
        raise ValueError('review_paused_for_queue must be boolean or null')
    reason = value.get('reason_code')
    if reason is not None:
        _text('reason_code', reason, 128)
    normalized = {'scope': 'request_execution', 'request_id': request_id,
        'execution_seq': execution_seq, 'phase': _text('phase', value.get('phase'), 128),
        'limits': {key: _number(key, limits.get(key)) for key in _LIMITS},
        'deadlines': {key: _timestamp(key, deadlines.get(key), optional=True) for key in _DEADLINES},
        'timing': {key: _number(key, timing.get(key)) for key in _TIMING},
        'review_paused_for_queue': paused, 'reason_code': reason,
        'updated_at': _timestamp('updated_at', value.get('updated_at'))}
    if 'provider_waits' in value:
        waits = _object('provider_waits', value['provider_waits'], ('active_count', 'deadlines'))
        count, items = waits.get('active_count'), waits.get('deadlines')
        if type(count) is not int or not 0 <= count <= 2 or not isinstance(items, list) or len(items) != count:
            raise ValueError('provider_waits must contain at most two active deadlines')
        normalized['provider_waits'] = {'active_count': count,
            'deadlines': [_timestamp('provider wait deadline', item) for item in items]}
    encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        raise ValueError('snapshot exceeds 65536 bytes')
    return normalized, encoded


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate budget JSON field')
        result[key] = value
    return result


@contextmanager
def _write(connection):
    """Use a savepoint inside an existing transaction; never commit its owner."""
    nested = connection.in_transaction
    connection.execute('SAVEPOINT request_budget_write' if nested else 'BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        if nested:
            connection.execute('ROLLBACK TO request_budget_write')
            connection.execute('RELEASE request_budget_write')
        else:
            connection.execute('ROLLBACK')
        raise
    else:
        connection.execute('RELEASE request_budget_write' if nested else 'COMMIT')


class BudgetStoreMixin:
    def _budget_owner(self, request_id, execution_seq, owner_token):
        return self._c.execute(
            """SELECT e.seq,e.started_at FROM review_requests r
                 JOIN request_executions e ON e.request_id=r.id
                WHERE r.id=? AND r.owner_token=? AND e.owner_token=? AND e.seq=?
                  AND e.seq=(SELECT MAX(seq) FROM request_executions WHERE request_id=r.id)""",
            (request_id, owner_token, owner_token, execution_seq)).fetchone()

    def save_request_budget(self, request_id, execution_seq, owner_token, snapshot):
        """Save a current execution snapshot; stale owner/sequence/time returns False."""
        _text('request_id', request_id)
        _text('owner_token', owner_token)
        _seq(execution_seq)
        normalized, encoded = _snapshot(request_id, execution_seq, snapshot)
        with _write(self._c):
            if self._budget_owner(request_id, execution_seq, owner_token) is None:
                return False
            cursor = self._c.execute(
                """INSERT INTO request_budget_snapshots VALUES(?,?,?,?)
                   ON CONFLICT(request_id,execution_seq) DO UPDATE SET
                     snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
                   WHERE request_budget_snapshots.updated_at<=excluded.updated_at""",
                (request_id, execution_seq, encoded, normalized['updated_at']))
            return cursor.rowcount == 1

    def record_request_capacity(self, request_id, execution_seq, owner_token, *,
            admission_id, resource_class, scope, effective_capacity,
            configured_capacity=None, legacy_dual_hold=None, updated_at):
        """Observe a linked live admission, never reassigning an existing layer."""
        for name, value in (('request_id', request_id), ('owner_token', owner_token),
                            ('admission_id', admission_id), ('resource_class', resource_class), ('scope', scope)):
            _text(name, value)
        _seq(execution_seq)
        _capacity('effective_capacity', effective_capacity)
        _capacity('configured_capacity', configured_capacity, optional=True)
        if legacy_dual_hold is not None and type(legacy_dual_hold) is not bool:
            raise ValueError('legacy_dual_hold must be boolean or null')
        _timestamp('updated_at', updated_at)
        with _write(self._c):
            if self._budget_owner(request_id, execution_seq, owner_token) is None:
                return False
            admission = self._c.execute(
                """SELECT resource_class,scope,status FROM capacity_admissions a
                    WHERE id=? AND EXISTS(SELECT 1 FROM request_links l
                      WHERE l.request_id=? AND l.kind='capacity' AND l.target_id=a.id)""",
                (admission_id, request_id)).fetchone()
            if admission is None or (admission['resource_class'], admission['scope']) != (resource_class, scope):
                return False
            existing = self._c.execute(
                'SELECT request_id,execution_seq FROM request_capacity_layers WHERE admission_id=?',
                (admission_id,)).fetchone()
            if existing is None and admission['status'] not in ('queued', 'admitted', 'running'):
                return False
            cursor = self._c.execute(
                """INSERT INTO request_capacity_layers VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(admission_id) DO UPDATE SET
                     effective_capacity=excluded.effective_capacity,
                     configured_capacity=excluded.configured_capacity,
                     legacy_dual_hold=excluded.legacy_dual_hold,updated_at=excluded.updated_at
                   WHERE request_capacity_layers.request_id=excluded.request_id
                     AND request_capacity_layers.execution_seq=excluded.execution_seq
                     AND request_capacity_layers.resource_class=excluded.resource_class
                     AND request_capacity_layers.scope=excluded.scope
                     AND request_capacity_layers.updated_at<=excluded.updated_at""",
                (admission_id, request_id, execution_seq, resource_class, scope,
                 effective_capacity, configured_capacity, legacy_dual_hold, updated_at))
            return cursor.rowcount == 1

    def _read_request_budget(self, row):
        try:
            encoded = row['snapshot_json']
            if not isinstance(encoded, str) or len(encoded.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
                raise ValueError('snapshot size')
            decoded = json.loads(encoded, object_pairs_hook=_unique_json_pairs)
            if not isinstance(decoded, dict) or set(decoded) not in (_FIELDS, _FIELDS - {'provider_waits'}):
                raise ValueError('incomplete snapshot fields')
            for name, keys in (('limits', _LIMITS), ('deadlines', _DEADLINES), ('timing', _TIMING)):
                if not isinstance(decoded.get(name), dict) or set(decoded[name]) != set(keys):
                    raise ValueError('incomplete snapshot dimensions')
            result, _ = _snapshot(row['request_id'], row['execution_seq'], decoded)
            if result['updated_at'] != row['updated_at']:
                raise ValueError('snapshot timestamp mismatch')
            execution = self._c.execute('SELECT request_id FROM request_executions WHERE seq=?',
                                        (row['execution_seq'],)).fetchone()
            if execution is None or execution['request_id'] != row['request_id']:
                raise ValueError('snapshot execution mismatch')
            layers = self._c.execute(
                'SELECT * FROM request_capacity_layers WHERE request_id=? AND execution_seq=? '
                'ORDER BY admission_id LIMIT ?',
                (row['request_id'], row['execution_seq'], MAX_LAYERS + 1)).fetchall()
            public = []
            for item in layers[:MAX_LAYERS]:
                layer = {key: item[key] for key in ('admission_id', 'execution_seq', 'resource_class', 'scope',
                    'effective_capacity', 'configured_capacity', 'legacy_dual_hold', 'updated_at')}
                for key in ('admission_id', 'resource_class', 'scope'):
                    _text(key, layer[key])
                _seq(layer['execution_seq'])
                _capacity('effective_capacity', layer['effective_capacity'])
                _capacity('configured_capacity', layer['configured_capacity'], optional=True)
                legacy = layer['legacy_dual_hold']
                if legacy is not None and (type(legacy) is not int or legacy not in (0, 1)):
                    raise ValueError('invalid legacy_dual_hold')
                layer['legacy_dual_hold'] = bool(legacy) if legacy is not None else None
                _timestamp('updated_at', layer['updated_at'])
                public.append(layer)
            return {**result, 'capacity_layers': public, 'capacity_layers_truncated': len(layers) > MAX_LAYERS}
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise BudgetDataError('malformed persisted request budget') from exc

    def request_budget(self, request_id):
        """Only the current execution's snapshot; never fall back to old work."""
        _text('request_id', request_id)
        execution = self._c.execute(
            'SELECT e.seq,e.owner_token,r.owner_token AS current_owner FROM review_requests r '
            'JOIN request_executions e ON e.request_id=r.id WHERE r.id=? ORDER BY e.seq DESC LIMIT 1',
            (request_id,)).fetchone()
        if execution is None:
            return None
        if execution['owner_token'] != execution['current_owner']:
            raise BudgetDataError('request execution ownership is inconsistent')
        row = self._c.execute('SELECT * FROM request_budget_snapshots WHERE request_id=? AND execution_seq=?',
                              (request_id, execution['seq'])).fetchone()
        return self._read_request_budget(row) if row else None

    def request_budgets(self, request_id, limit=100):
        """Bounded historical snapshots/layers, with explicit truncation."""
        _text('request_id', request_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('budget history limit must be an integer in 1..100')
        rows = self._c.execute('SELECT * FROM request_budget_snapshots WHERE request_id=? '
                              'ORDER BY execution_seq DESC LIMIT ?', (request_id, limit + 1)).fetchall()
        return {'budgets': [self._read_request_budget(row) for row in rows[:limit]],
                'truncated': len(rows) > limit}
