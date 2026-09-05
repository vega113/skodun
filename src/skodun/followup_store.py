"""Follow-up bindings share Store transactions, claims and orchestration ownership.

Candidates are immutable source copies, separate from usable child payloads.
Only runtime binding validation can promote them. No Store opener lives here.
"""
import json
from dataclasses import asdict

from . import followups
from .checkpoints import (CheckpointPayload, OrchestrationIdentity, canonical_digest,
                          MAX_CHECKPOINT_JSON_BYTES)

MIGRATION_V20 = (
"""CREATE TABLE review_followup_checkpoints (
 orchestration_id TEXT NOT NULL,
 pass_kind TEXT NOT NULL CHECK(pass_kind IN ('security','skeptic')),
 pass_index INTEGER NOT NULL CHECK(pass_index=0),
 state TEXT NOT NULL CHECK(state IN ('pending','running','complete','failed')),
 prompt_hash TEXT, diff_hash TEXT NOT NULL, boundary_hash TEXT NOT NULL,
 payload_json TEXT, completed_at TEXT, claim_token TEXT,
 fence INTEGER NOT NULL DEFAULT 0, claim_owner TEXT, claimed_at TEXT,
 lease_expires_at TEXT, failure_reason TEXT,
 binding_json TEXT, binding_hash TEXT, candidate_json TEXT, invalidation_reason TEXT,
 PRIMARY KEY(orchestration_id,pass_kind,pass_index),
 FOREIGN KEY(orchestration_id) REFERENCES review_orchestrations(id) ON DELETE CASCADE
)""",
"""CREATE INDEX ix_followup_checkpoints_state
 ON review_followup_checkpoints(orchestration_id,state,pass_kind,pass_index)""",
)


def _candidate(row, source_id):
    body = followups.decode_binding(row['binding_json'])
    if canonical_digest(body) != row['binding_hash']:
        raise ValueError('follow-up source binding corrupt')
    payload = CheckpointPayload(row['payload_json'])
    return {'source_id': source_id, 'binding': body, 'payload': payload.as_dict(),
            'completed_at': row['completed_at']}


def _decode_candidate(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_CHECKPOINT_JSON_BYTES + followups.MAX_BINDING_BYTES + 4096:
        raise ValueError('follow-up candidate exceeds bound')
    raw = json.loads(text)
    if not isinstance(raw, dict) or set(raw) != {'source_id','binding','payload','completed_at'}:
        raise ValueError('invalid follow-up candidate fields')
    if not isinstance(raw['source_id'], str) or not raw['source_id']:
        raise ValueError('invalid follow-up candidate source')
    from .store import _require_ts
    _require_ts('completed_at', raw['completed_at'])
    raw['binding'] = followups.validate_binding(raw['binding'])
    payload = CheckpointPayload.from_mapping(raw['payload'])
    if not followups.usable(payload):
        raise ValueError('follow-up candidate unusable')
    return raw, payload


class FollowupStoreMixin:
    def _validate_followup_binding(self, orchestration_id, body):
        """Rebuild dependencies within caller's transaction, before publication."""
        body = followups.validate_binding(body)
        orchestration = self.get_orchestration(orchestration_id)
        identity = OrchestrationIdentity.from_json(orchestration['identity_json'])
        if identity.digest() != orchestration['identity_digest']:
            raise ValueError('follow-up orchestration identity corrupt')
        content = asdict(identity)
        content.pop('continuation_source', None)
        if body['content_hash'] != canonical_digest(content):
            raise ValueError('follow-up content identity changed')
        rows = self.list_checkpoints(orchestration_id)
        if {(r['pass_kind'], r['pass_index']) for r in rows} != {(p.kind, p.index) for p in identity.pass_identities}:
            raise ValueError('follow-up upstream plan changed')
        planned = next((p for p in identity.pass_identities if p.kind == body['kind']), None)
        row = next((r for r in rows if r['pass_kind'] == body['kind']), None)
        if (planned is None or row is None or row['diff_hash'] != planned.diff_hash
                or row['boundary_hash'] != planned.boundary_hash):
            raise ValueError('follow-up plan identity changed')
        if body['dependencies'] != followups.dependencies(rows, body['kind']):
            raise ValueError('followup_upstream_changed')
        return body

    def bind_followup_checkpoint(self, orchestration_id, pass_identity, *,
                                 binding, now, request_id=None, owner_token=None):
        from .store import _require_ts
        now = _require_ts('now', now)
        if pass_identity.kind not in followups.KINDS or pass_identity.index != 0:
            raise ValueError('invalid follow-up pass identity')
        self._c.execute('BEGIN IMMEDIATE')
        try:
            if request_id is not None:
                request = self._c.execute('SELECT state,owner_token FROM review_requests WHERE id=?',
                                          (request_id,)).fetchone()
                linked = self._c.execute("SELECT 1 FROM request_links WHERE request_id=? AND kind='batch_orchestration' AND target_id=?",
                                         (request_id, orchestration_id)).fetchone()
                if (request is None or request['owner_token'] != owner_token or linked is None
                        or request['state'] not in ('accepted','queued','running')):
                    raise ValueError('follow-up request ownership changed')
            elif self._c.execute("SELECT 1 FROM request_links l JOIN review_requests r ON r.id=l.request_id WHERE l.kind='batch_orchestration' AND l.target_id=? AND r.state IN ('accepted','queued','running')",
                                 (orchestration_id,)).fetchone() is not None:
                raise ValueError('follow-up request ownership missing')
            orchestration = self.get_orchestration(orchestration_id)
            if orchestration is None or orchestration['state'] not in ('active','failed','cancelled','complete'):
                raise ValueError('follow-up orchestration not active')
            body = self._validate_followup_binding(orchestration_id, binding)
            if body['kind'] != pass_identity.kind:
                raise ValueError('follow-up binding kind mismatch')
            prompt_hash = body['prompt_identity']['hash'] if body['prompt_identity'] is not None else None
            if prompt_hash != pass_identity.prompt_hash:
                raise ValueError('follow-up prompt identity mismatch')
            row = self._c.execute('SELECT * FROM review_followup_checkpoints WHERE orchestration_id=? AND pass_kind=?',
                                  (orchestration_id, pass_identity.kind)).fetchone()
            if row is None or row['diff_hash'] != pass_identity.diff_hash or row['boundary_hash'] != pass_identity.boundary_hash:
                raise ValueError('follow-up pass does not match plan')
            digest = canonical_digest(body)
            if row['binding_hash'] is not None:
                if row['binding_hash'] != digest or canonical_digest(followups.decode_binding(row['binding_json'])) != digest:
                    raise ValueError('follow-up current-generation binding changed')
                self._c.execute('COMMIT')
                return digest
            if row['state'] == 'running':
                raise ValueError('follow-up binding cannot reset a live claim')
            candidate = row['candidate_json']
            payload, completed, reason = None, None, None
            if candidate is not None:
                prior, candidate_payload = _decode_candidate(candidate)
                known = (followups.semantic_payload(candidate_payload)['actual']['provider']
                         and all(dep['provenance_known'] for dep in body['dependencies']))
                if prior['binding'] == body and body['decision']['scheduled'] and known:
                    value = candidate_payload.as_dict()
                    value['provenance'] = {**value['provenance'], 'continuation_source': prior['source_id'],
                                           'continuation_action': 'reused'}
                    payload = CheckpointPayload.from_mapping(value).json_text
                    completed = prior['completed_at']
                else:
                    reason = followups.binding_mismatch(prior['binding'], body) if known else 'followup_candidate_unusable'
            self._c.execute('''UPDATE review_followup_checkpoints
               SET binding_json=?,binding_hash=?,prompt_hash=?,candidate_json=NULL,
                   state=?,payload_json=?,completed_at=?,invalidation_reason=?
               WHERE orchestration_id=? AND pass_kind=?''',
               (json.dumps(body, sort_keys=True, separators=(',', ':')), digest, prompt_hash,
                'complete' if payload is not None else 'pending', payload, completed, reason,
                orchestration_id, pass_identity.kind))
            self._c.execute('COMMIT')
            return digest
        except BaseException:
            self._c.execute('ROLLBACK')
            raise

    def _require_followup_claim_binding(self, orchestration_id, row, binding_hash):
        if not isinstance(binding_hash, str) or binding_hash != row['binding_hash']:
            raise ValueError('follow-up claim binding mismatch')
        body = followups.decode_binding(row['binding_json'])
        if canonical_digest(body) != binding_hash or not body['decision']['scheduled']:
            raise ValueError('follow-up claim is not scheduled')
        self._validate_followup_binding(orchestration_id, body)

    def _seed_followup_candidates(self, source_id, child_id, rows):
        for row in rows:
            if row['pass_kind'] not in followups.KINDS:
                continue
            candidate = None
            if row['state'] == 'complete' and row['payload_json'] is not None:
                payload = CheckpointPayload(row['payload_json'])
                if followups.usable(payload):
                    self._validate_followup_binding(source_id, followups.decode_binding(row['binding_json']))
                    candidate = json.dumps(_candidate(row, source_id), sort_keys=True, separators=(',', ':'))
            elif row['candidate_json'] is not None:
                _decode_candidate(row['candidate_json'])
                candidate = row['candidate_json']
            if candidate is not None:
                self._c.execute('UPDATE review_followup_checkpoints SET candidate_json=? WHERE orchestration_id=? AND pass_kind=?',
                                (candidate, child_id, row['pass_kind']))

    def _require_followup_publication(self, orchestration_id, *, rec):
        orchestration = self.get_orchestration(orchestration_id)
        identity = OrchestrationIdentity.from_json(orchestration['identity_json'])
        expected = {p.kind for p in identity.pass_identities if p.kind in followups.KINDS}
        rows = [r for r in self.list_checkpoints(orchestration_id) if r['pass_kind'] in followups.KINDS]
        if {r['pass_kind'] for r in rows} != expected:
            raise ValueError('follow-up publication plan mismatch')
        for row in rows:
            if row['binding_json'] is None or row['candidate_json'] is not None:
                raise ValueError('follow-up decision is not bound')
            body = self._validate_followup_binding(orchestration_id, followups.decode_binding(row['binding_json']))
            if (canonical_digest(body) != row['binding_hash'] or row['prompt_hash'] !=
                    (body['prompt_identity']['hash'] if body['prompt_identity'] is not None else None)):
                raise ValueError('follow-up publication binding corrupt')
            if body['decision']['scheduled']:
                if row['state'] != 'complete' or row['payload_json'] is None:
                    raise ValueError('required follow-up checkpoint incomplete')
                payload = CheckpointPayload(row['payload_json'])
                metadata = (rec.get('extra_passes') or {}).get(row['pass_kind']) or {}
                if metadata.get('followup_output_hash') != canonical_digest(followups.semantic_payload(payload)):
                    raise ValueError('follow-up publication output changed')
                if rec['trustworthy'] and not followups.usable(payload):
                    raise ValueError('required follow-up checkpoint unusable')
