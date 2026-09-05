"""Event-compatible request cancellation, fenced to one execution owner.

Only the executing thread touches its Store. External signals/disconnects
mark the upstream Event; the owner audits that observation before propagating
cancellation to the pipeline. Explicit control persists its event first.
"""

import os
import threading
import time

CAUSES = {'requested_cancel','signal','disconnect','disconnect_deadline',
          'recovery_deadline','wall_clock_deadline','unknown_cancel_token',
          'queue_budget_exhausted','review_budget_exhausted','total_budget_exhausted','budget_expired'}


def mark_event(event, cause):
    """Attach observed lifecycle cause; never claim an external actor."""
    if event is not None:
        event.reason_code = cause
        event.set()


class RequestCancel:
    def __init__(self, store, context, upstream=None):
        self.store, self.context, self.upstream = store, context, upstream
        self._local = threading.Event()
        self.reason_code = None
        self._audited = False

    def set(self):
        self._local.set()

    def is_set(self):
        from .requests import now
        event = self.store.request_cancel_event(self.context.id, self.context.owner_token)
        if event:
            self.reason_code = event['cause']
            self._audited = True
            return True
        upstream_set = self.upstream is not None and self.upstream.is_set()
        if not self._local.is_set() and not upstream_set:
            return False
        if not self._audited:
            cause = getattr(self.upstream, 'reason_code', None) or self.reason_code
            self.reason_code = cause if cause in CAUSES else 'unknown_cancel_token'
            request = self.store.get_request(self.context.id)
            if request is None or request['owner_token'] != self.context.owner_token:
                self.reason_code = 'request_ownership_lost'
                self._audited = True
                return True
            self.store.record_cancellation(
                target_id=self.context.id, request=request,
                identity={'request_id':self.context.id, **self.context.identity},
                actor='unknown', source='lifecycle', caller_pid=os.getpid(),
                caller_worktree=self.context.identity.get('worktree_root'),
                reason='Cancellation observed by request owner',
                cause=self.reason_code, now=now())
            self._audited = True
        return True

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._local.wait(.05 if remaining is None else min(.05,remaining))
        return True


RECORD_CANCEL_PROTOCOL = 'record_audit_v1'


class RecordCancel:
    """Current prepush worker's cooperative, record-ID-specific cancellation.

    The validated worker publishes support before invoking its provider. No
    caller signals a PID; legacy rows lacking the marker cannot opt into it.
    """
    def __init__(self, store, record_id, upstream):
        self.store, self.record_id, self.upstream = store, record_id, upstream
        self._local = threading.Event()
        self.reason_code = None

    def set(self):
        self._local.set()

    def is_set(self):
        if self._local.is_set() or self.upstream.is_set():
            return True
        for event in self.store.cancellation_events(self.record_id):
            if event['target_id'] == self.record_id and event['request_id'] is None and event['outcome'] in ('requested','observed'):
                self.reason_code = event['cause']
                self._local.set()
                return True
        return False

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._local.wait(.05 if remaining is None else min(.05,remaining))
        return True
