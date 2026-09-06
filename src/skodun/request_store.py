"""Durable execution requests, deliberately separate from review coverage.

This mixin uses the owning Store connection. Claims are transactional; an
active idempotent request is observed, never stolen or restarted on a guessed
PID/lease. Links distinguish recovery orchestration from batch orchestration.
"""

import hashlib
import json


MIGRATION = (
    """CREATE TABLE IF NOT EXISTS review_requests (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_key TEXT,
        identity_json TEXT NOT NULL, intent_digest TEXT NOT NULL,
        owner_token TEXT NOT NULL, pid INTEGER NOT NULL, source TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
          ('accepted','queued','running','finished','cancelled','failed','expired')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        reason_code TEXT, result_json TEXT,
        UNIQUE(scope, request_key)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_review_requests_scope_time
        ON review_requests(scope, created_at, id)""",
    """CREATE INDEX IF NOT EXISTS ix_request_result_retention
        ON review_requests(updated_at,id) WHERE result_json IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS request_links (
        request_id TEXT NOT NULL REFERENCES review_requests(id),
        kind TEXT NOT NULL CHECK(kind IN
          ('capacity','review','recovery_orchestration','batch_orchestration')),
        target_id TEXT NOT NULL,
        PRIMARY KEY(request_id, kind, target_id)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_request_links_target
        ON request_links(kind, target_id)""",
    """CREATE TABLE IF NOT EXISTS request_executions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES review_requests(id),
        owner_token TEXT NOT NULL UNIQUE, source TEXT NOT NULL, pid INTEGER NOT NULL,
        started_at TEXT NOT NULL, completed_at TEXT, status INTEGER,
        reason_code TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_request_execution_history
        ON request_executions(request_id,seq)""",
)


def _json(value):
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('request payload exceeds 2 MiB')
    return text


def _text(label, value, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f'{label} must be a nonempty string of at most {limit} characters')
    return value


def _identity_json(scope, identity):
    """Canonical request identity validation shared with recovery inspection."""
    if not isinstance(identity, dict):
        raise ValueError('request identity must be an object')
    encoded = _json(identity)
    if identity.get('worktree_root') != scope:
        raise ValueError('request scope must match its worktree identity')
    if len(encoded.encode('utf-8')) > 65536:
        raise ValueError('request identity exceeds 64 KiB')
    return encoded


class RequestStoreMixin:
    """Validated methods composed into Store, never another database handle."""

    def begin_request(self, *, request_id, scope, request_key, identity, intent,
                      owner_token, pid, source, now, expires_at, continuation_id=None,
                      continuation_orchestration_id=None, actor=None, allow_consumed=False):
        from .store import _require_ts
        for key, value in (('request_id', request_id), ('scope', scope),
                           ('owner_token', owner_token), ('source', source)):
            _text(key, value)
        if request_key is not None:
            _text('request_key', request_key, 128)
        if type(pid) is not int or pid <= 0:
            raise ValueError('request pid must be a positive integer')
        _require_ts('now', now)
        _require_ts('expires_at', expires_at)
        if expires_at <= now:
            raise ValueError('request expiry must be after creation')
        if not isinstance(identity, dict) or not isinstance(intent, dict):
            raise ValueError('request identity and intent must be objects')
        if actor is not None:
            from .control import audit_text
            actor = audit_text(actor, 'actor', 120)
        encoded = _identity_json(scope, identity)
        if type(allow_consumed) is not bool:
            raise ValueError('allow_consumed must be bool')
        digest = hashlib.sha256(_json(intent).encode()).hexdigest()
        self._c.execute('BEGIN IMMEDIATE')
        try:
            prior = None
            if request_key is not None:
                prior = self._c.execute(
                    'SELECT * FROM review_requests WHERE scope=? AND request_key=?',
                    (scope, request_key)).fetchone()
            elif continuation_id is not None:
                _text('continuation_id', continuation_id)
                prior = self._c.execute(
                    'SELECT * FROM review_requests WHERE id=? AND scope=? AND identity_json=?',
                    (continuation_id, scope, encoded)).fetchone()
            if prior is not None:
                decision = ('existing' if (continuation_id is not None and request_key is None)
                            or prior['identity_json'] == encoded
                            and prior['intent_digest'] == digest else 'mismatch')
                result_id = prior['id']
                if (continuation_id is not None and request_key is None
                        and prior['state'] in ('finished', 'failed', 'cancelled', 'expired')):
                    old_result = json.loads(prior['result_json']) if prior['result_json'] else None
                    complete = old_result is not None and old_result.get('status') in (0, 1)
                    target = self._c.execute(
                        'SELECT state,final_review_id FROM review_orchestrations WHERE id=?',
                        (continuation_orchestration_id,)).fetchone()
                    consumed_failed = False
                    if allow_consumed and target is not None and target['state'] == 'consumed':
                        review = self._c.execute('SELECT trustworthy,status FROM reviews WHERE id=?',
                                                 (target['final_review_id'],)).fetchone()
                        consumed_failed = (review is not None and review['trustworthy'] == 0
                                           and review['status'] != 'running')
                    if complete:
                        decision = 'existing'
                    elif target is None or (target['state'] not in ('active', 'cancelled', 'failed', 'complete') and not consumed_failed):
                        decision = 'continuation_unavailable'
                    else:
                        self._c.execute(
                            """UPDATE review_requests SET state='accepted',owner_token=?,pid=?,source=?,
                               updated_at=?,expires_at=?,result_json=NULL,reason_code=NULL WHERE id=?""",
                            (owner_token, pid, source, now, expires_at, result_id))
                        decision = 'continued'
            else:
                self._c.execute(
                    """INSERT INTO review_requests
                    (id,scope,request_key,identity_json,intent_digest,owner_token,
                     pid,source,state,created_at,updated_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,'accepted',?,?,?)""",
                    (request_id, scope, request_key, encoded, digest, owner_token,
                     pid, source, now, now, expires_at))
                decision, result_id = 'created', request_id
            if decision in ('created', 'continued'):
                self._c.execute('UPDATE review_requests SET actor=? WHERE id=?', (actor, result_id))
                self._c.execute(
                    """INSERT INTO request_executions
                       (request_id,owner_token,source,pid,started_at,actor) VALUES(?,?,?,?,?,?)""",
                    (result_id, owner_token, source, pid, now, actor))
            self._c.execute('COMMIT')
        except BaseException:
            self._c.execute('ROLLBACK')
            raise
        return decision, self.get_request(result_id)

    def get_request(self, request_id):
        _text('request_id', request_id)
        row = self._c.execute('SELECT * FROM review_requests WHERE id=?',
                              (request_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['identity'] = json.loads(result.pop('identity_json'))
        encoded = result.pop('result_json')
        result['result'] = json.loads(encoded) if encoded is not None else None
        result['links'] = [dict(r) for r in self._c.execute(
            'SELECT kind,target_id FROM request_links WHERE request_id=? ORDER BY kind,target_id',
            (request_id,)).fetchall()]
        result['executions'] = [dict(r) for r in self._c.execute(
            """SELECT seq,source,pid,started_at,completed_at,status,reason_code,actor
               FROM request_executions WHERE request_id=? ORDER BY seq DESC LIMIT 101""",
            (request_id,)).fetchall()]
        result['cancellation'] = self.cancellation_events(request_id)
        result['executions_truncated'] = len(result['executions']) > 100
        result['executions'] = result['executions'][:100]
        return result

    def request_for_orchestration(self, orchestration_id, identity):
        """Find the originating incomplete logical request; never steal it."""
        _text('orchestration_id', orchestration_id)
        row = self._c.execute(
            """SELECT r.id FROM review_requests r JOIN request_links l ON l.request_id=r.id
               WHERE l.kind='batch_orchestration' AND l.target_id=? AND r.identity_json=?
                 AND (r.state IN ('accepted','queued','running','failed','cancelled','expired')
                      OR (r.state='finished' AND json_extract(r.result_json,'$.status') > 1))
               ORDER BY r.updated_at DESC,r.id DESC LIMIT 1""",
            (orchestration_id, _json(identity))).fetchone()
        return row['id'] if row else None

    def continuation_request_mismatch(self, orchestration_id, identity):
        """Name a known source-request mismatch, without guessing when none is known."""
        _text('orchestration_id', orchestration_id)
        if not isinstance(identity, dict):
            raise ValueError('request identity must be an object')
        row = self._c.execute(
            """SELECT r.identity_json FROM review_requests r
               JOIN request_links l ON l.request_id=r.id
               WHERE l.kind='batch_orchestration' AND l.target_id=?
               ORDER BY r.updated_at DESC,r.id DESC LIMIT 1""", (orchestration_id,)).fetchone()
        if row is None:
            return None
        source = json.loads(row['identity_json'])
        if not isinstance(source, dict):
            return None
        for field in sorted(set(source) | set(identity)):
            if field not in source or field not in identity or source[field] != identity[field]:
                return field
        return None

    def list_requests(self, *, worktree_root=None, repo_id=None, limit=50, active_first=False):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('request limit must be between 1 and 1000')
        where, args = '', ()
        if worktree_root is not None:
            _text('worktree_root', worktree_root)
            where, args = ' WHERE scope=?', (worktree_root,)
        elif repo_id is not None:
            _text('repo_id', repo_id)
            where, args = " WHERE json_extract(identity_json,'$.repo_id')=?", (repo_id,)
        order = ("(state IN ('accepted','queued','running')) DESC," if active_first else '')
        rows = self._c.execute('SELECT id FROM review_requests' + where +
                               ' ORDER BY ' + order + 'created_at DESC,id DESC LIMIT ?',
                               (*args, limit)).fetchall()
        return [self.get_request(r['id']) for r in rows]

    def link_request(self, request_id, kind, target_id):
        _text('request_id', request_id)
        _text('target_id', target_id)
        if kind not in ('capacity', 'review', 'recovery_orchestration', 'batch_orchestration'):
            raise ValueError('unknown request link kind')
        # Works both in an enclosing publication transaction and autocommit.
        self._c.execute('INSERT OR IGNORE INTO request_links VALUES(?,?,?)',
                        (request_id, kind, target_id))

    def advance_request(self, request_id, *, owner_token, state, now):
        from .store import _require_ts
        _text('request_id', request_id)
        _text('owner_token', owner_token)
        _require_ts('now', now)
        if state not in ('queued', 'running'):
            raise ValueError('invalid active request state')
        return self._c.execute(
            """UPDATE review_requests SET state=?,updated_at=?
               WHERE id=? AND owner_token=? AND state IN ('accepted','queued','running')""",
            (state, now, request_id, owner_token)).rowcount == 1

    def finish_request(self, request_id, *, owner_token, state, reason_code,
                       result, now):
        from .store import _require_ts
        _text('request_id', request_id)
        _text('owner_token', owner_token)
        _text('reason_code', reason_code)
        _require_ts('now', now)
        if state not in ('finished', 'cancelled', 'failed', 'expired'):
            raise ValueError('request completion state must be terminal')
        encoded = None if result is None else _json(result)
        self._c.execute('BEGIN IMMEDIATE')
        try:
            cur = self._c.execute(
                """UPDATE review_requests SET state=?,reason_code=?,result_json=?,updated_at=?
                   WHERE id=? AND owner_token=? AND state IN ('accepted','queued','running')""",
                (state, reason_code, encoded, now, request_id, owner_token))
            if cur.rowcount == 1:
                status = result['status'] if result is not None else (130 if state == 'cancelled' else 4)
                self._c.execute(
                    """UPDATE request_executions SET completed_at=?,status=?,reason_code=?
                       WHERE request_id=? AND owner_token=?""",
                    (now, status, reason_code, request_id, owner_token))
            if cur.rowcount == 1:
                self.finish_cancellations(request_id=request_id, owner_token=owner_token,
                    outcome=('cancelled' if state == 'cancelled' else
                             'completed_before_cancel' if result and result.get('status') in (0,1) else state), now=now)
            self._c.execute('COMMIT')
            return cur.rowcount == 1
        except BaseException:
            self._c.execute('ROLLBACK')
            raise

    def prune_request_results(self, *, before, dry_run=False, limit=500):
        """Bound terminal payload retention without deleting keys or evidence.

        A pruned idempotency key still refuses execution: retention must never
        turn a retried old request into a second paid call. Review artifacts,
        identity, links, and active requests are retained.
        """
        from .store import _require_ts
        _require_ts('before', before)
        if type(dry_run) is not bool or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid request retention bounds')
        select = """SELECT id FROM review_requests WHERE updated_at < ?
                    AND result_json IS NOT NULL
                    AND state IN ('finished','cancelled','failed','expired')
                    ORDER BY updated_at,id LIMIT ?"""
        if dry_run:
            return self._c.execute('SELECT count(*) FROM (' + select + ')',
                                    (before, limit)).fetchone()[0]
        return self._c.execute(
            "UPDATE review_requests SET result_json=NULL,reason_code='request_result_expired' "
            'WHERE id IN (' + select + ')', (before, limit)).rowcount
