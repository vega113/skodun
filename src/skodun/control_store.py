"""Execution-fenced cancellation audit; observations never repair coverage."""

import json

MIGRATION = (
    "ALTER TABLE review_requests ADD COLUMN actor TEXT",
    "ALTER TABLE request_executions ADD COLUMN actor TEXT",
    """CREATE TABLE cancellation_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL,
      request_id TEXT, execution_token TEXT, identity_json TEXT NOT NULL,
      actor TEXT NOT NULL, source TEXT NOT NULL, caller_pid INTEGER NOT NULL,
      caller_worktree TEXT, created_at TEXT NOT NULL, reason TEXT NOT NULL,
      cause TEXT NOT NULL, outcome TEXT NOT NULL, completed_at TEXT
    )""",
    "CREATE INDEX ix_cancel_target ON cancellation_audit(target_id,id)",
    "CREATE INDEX ix_cancel_execution ON cancellation_audit(request_id,execution_token,id)",
)


class ControlStoreMixin:
    def cancellation_events(self, target_id):
        rows = self._c.execute(
            """SELECT id,target_id,request_id,identity_json,actor,source,caller_pid,
               caller_worktree,created_at,reason,cause,outcome,completed_at,
               (SELECT seq FROM request_executions e WHERE e.owner_token=c.execution_token) AS execution_seq
               FROM cancellation_audit c WHERE target_id=? OR request_id=?
               ORDER BY id DESC LIMIT 100""", (target_id, target_id)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['identity'] = json.loads(item.pop('identity_json'))
            result.append(item)
        return result

    def request_cancel_event(self, request_id, owner_token):
        row = self._c.execute(
            """SELECT id,cause FROM cancellation_audit WHERE request_id=?
               AND execution_token=? AND outcome IN ('requested','observed')
               ORDER BY id LIMIT 1""", (request_id, owner_token)).fetchone()
        return dict(row) if row else None

    def record_cancellation(self, *, target_id, request, identity, actor, source,
                            caller_pid, caller_worktree, reason, cause, now):
        from .store import _require_ts
        from .control import audit_text
        _require_ts('now', now)
        if not isinstance(target_id, str) or not target_id or len(target_id) > 4096:
            raise ValueError('invalid cancellation target')
        if not isinstance(identity, dict):
            raise ValueError('cancellation identity must be an object')
        if caller_worktree is not None and (not isinstance(caller_worktree, str) or len(caller_worktree) > 4096):
            raise ValueError('invalid caller worktree')
        actor = audit_text(actor, 'actor', 120)
        source = audit_text(source, 'source', 80)
        reason = audit_text(reason, 'reason', 500)
        cause = audit_text(cause, 'cause', 80)
        if type(caller_pid) is not int or caller_pid <= 0:
            raise ValueError('caller_pid must be a positive integer')
        self._c.execute('BEGIN IMMEDIATE')
        try:
            if request is not None:
                row = self._c.execute('SELECT state,owner_token FROM review_requests WHERE id=?',
                                      (request['id'],)).fetchone()
                if row is None or row['owner_token'] != request['owner_token'] or row['state'] not in ('accepted','queued','running'):
                    raise ValueError('target became terminal or changed execution')
            else:
                row = self._c.execute('SELECT status,artifact_json FROM reviews WHERE id=?', (target_id,)).fetchone()
                if row is None or row['status'] != 'running':
                    raise ValueError('target became terminal')
                from .control import review_identity
                if review_identity(json.loads(row['artifact_json'])) != identity:
                    raise ValueError('target identity changed before cancellation')
            cur = self._c.execute(
                """INSERT INTO cancellation_audit(target_id,request_id,execution_token,
                   identity_json,actor,source,caller_pid,caller_worktree,created_at,
                   reason,cause,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,'requested')""",
                (target_id, request['id'] if request else None,
                 request['owner_token'] if request else None,
                 json.dumps(identity, sort_keys=True), actor, source, caller_pid,
                 caller_worktree, now, reason, cause))
            self._c.execute('COMMIT')
            return cur.lastrowid
        except BaseException:
            self._c.execute('ROLLBACK')
            raise

    def observe_worker_cancellation(self, owner, *, cause, now):
        """Audit the current worker's observation, even after its own commit.

        This separate owner-only path does not relax external cancellation's
        running-only guard. Captured reservation identity and the current
        process must still match before any audit write.
        """
        from .request_cancel import RecordOwner
        from .store import _require_ts
        from .control import audit_text, review_identity
        _require_ts('now', now)
        cause = audit_text(cause, 'cause', 80)
        if not isinstance(owner, RecordOwner):
            raise ValueError('worker cancellation requires a captured owner')
        self._c.execute('BEGIN IMMEDIATE')
        try:
            row = self._c.execute('SELECT artifact_json FROM reviews WHERE id=?',
                                  (owner.record_id,)).fetchone()
            record = json.loads(row[0]) if row else {}
            if not owner.matches(record):
                raise ValueError('worker cancellation observation ownership changed')
            cur = self._c.execute(
                """INSERT INTO cancellation_audit(target_id,identity_json,actor,
                   source,caller_pid,caller_worktree,created_at,reason,cause,outcome)
                   VALUES(?,?,'unknown','worker_lifecycle',?,?,?,
                          'Cancellation observed by current worker',?,'observed')""",
                (owner.record_id,json.dumps(review_identity(record),sort_keys=True),
                 owner.pid,owner.worktree_root,now,cause))
            self._c.execute('COMMIT')
            return cur.lastrowid
        except BaseException:
            self._c.execute('ROLLBACK')
            raise

    def finish_cancellations(self, *, outcome, now, request_id=None,
                             owner_token=None, target_id=None):
        from .store import _require_ts
        _require_ts('now', now)
        if request_id is not None:
            where, args = 'request_id=? AND execution_token=?', (request_id, owner_token)
        else:
            where, args = 'target_id=? AND request_id IS NULL', (target_id,)
        self._c.execute(
            "UPDATE cancellation_audit SET outcome=?,completed_at=? WHERE " + where +
            " AND outcome IN ('requested','observed')", (outcome, now, *args))

    def publish_record_cancellation(self, record_id, pid):
        """Publish capability only from the validated current worker path."""
        from .request_cancel import RECORD_CANCEL_PROTOCOL
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid worker pid')
        cur = self._c.execute(
            """UPDATE reviews SET artifact_json=json_set(artifact_json,
                 '$.cancellation_protocol',?) WHERE id=? AND mode='prepush'
                 AND status='running' AND (pid IS NULL OR pid=?)""",
            (RECORD_CANCEL_PROTOCOL, record_id, pid))
        return cur.rowcount == 1

    def control_reviews(self, *, worktree_root=None, repo_id=None, limit=100,
                        exclude_active_request_links=False):
        clauses, args = [], []
        if worktree_root is not None:
            clauses.append('worktree_root=?')
            args.append(worktree_root)
        elif repo_id is not None:
            clauses.append('(repo=? OR repo_id=?)')
            args.extend((repo_id,repo_id))
        if exclude_active_request_links:
            nested = ''
            if worktree_root is not None:
                nested = ' AND r.scope=?'
                args.append(worktree_root)
            elif repo_id is not None:
                nested = " AND json_extract(r.identity_json,'$.repo_id')=?"
                args.append(repo_id)
            clauses.append("""NOT EXISTS (SELECT 1 FROM request_links l
                JOIN review_requests r ON r.id=l.request_id
                WHERE l.kind='review' AND l.target_id=reviews.id
                  AND r.state IN ('accepted','queued','running')""" + nested + ')')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self._c.execute('SELECT artifact_json FROM reviews' + where +
                               " ORDER BY (status='running') DESC,reviewed_at DESC,id DESC LIMIT ?",
                               (*args, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]
