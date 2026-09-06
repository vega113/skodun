"""Bounded foreground batch execution over one frozen, fenced request.

Only two active chains may enter existing provider FIFO admission. Every worker
owns its Store, request context, cancellation monitor and budget callback. Shared
state contains no SQLite objects. The coordinator owns progress, ordered folding,
barriers and publication; exceptions stop submission and join owned watchdogs.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import Context
from dataclasses import replace
from queue import Empty, Full, Queue
import threading
import time

from . import budgets, requests, runner
from .request_cancel import RequestCancel
from .store import Store


class _FencedCancel(RequestCancel):
    def is_set(self):
        if self.store._budget_owner(self.context.id, self.context.execution_seq, self.context.owner_token) is None:
            self.reason_code = 'request_ownership_lost'
            return True
        return super().is_set()


class _WorkerCancel:
    """Internal peer failure is not an external cancellation audit."""
    def __init__(self, controller, stop):
        self.controller, self.stop = controller, stop

    @property
    def reason_code(self):
        return 'parallel_peer_stopped' if self.stop.is_set() else self.controller.reason_code

    def is_set(self):
        return self.stop.is_set() or self.controller.is_set()

    def wait(self, seconds=None):
        end = None if seconds is None else time.monotonic() + max(0, seconds)
        while not self.is_set():
            remaining = .05 if end is None else min(.05, end - time.monotonic())
            if remaining <= 0:
                return False
            self.stop.wait(remaining)
        return True


def execute(prepared, *, context, store_path, run_one, cancel, progress):
    """Return results by one-based index; no callback may cross a Store thread."""
    if context is None or context.budget is None or context.execution_seq is None or store_path is None:
        raise ValueError('parallel batches require a durable, budgeted foreground request')
    template = replace(context, store=None, budget=None)
    state = context.budget._shared_state
    stop, events = threading.Event(), Queue(maxsize=128)
    dropped, drop_lock = [0], threading.Lock()

    def narrate(message):
        try:
            events.put_nowait(message)
        except Full:
            with drop_lock:
                dropped[0] += 1

    def drain():
        while True:
            try:
                message = events.get_nowait()
            except Empty:
                break
            progress(message)

    def work(item):
        from .pipeline import _PROGRESS
        if stop.is_set():
            raise runner.ReviewCancelled('parallel submission stopped before claim')
        with Store.open(store_path) as worker_store:
            local = replace(template, store=worker_store)
            monitor = _FencedCancel(worker_store, local)
            controller = None
            def publish(snapshot):
                active = requests.current()
                if active is None or active.store is not worker_store or active.budget is not controller:
                    raise RuntimeError('parallel budget callback lost its local request context')
                if not worker_store.save_request_budget(local.id, local.execution_seq, local.owner_token, snapshot):
                    raise RuntimeError('parallel budget callback lost its execution fence')
            controller = budgets.ReviewBudget.from_shared(state, cancel=monitor, on_update=publish)
            local = replace(local, budget=controller)
            token = requests._CURRENT.set(local)
            previous = getattr(_PROGRESS, 'sink', None)
            _PROGRESS.sink = narrate
            try:
                token_cancel = _WorkerCancel(controller, stop)
                if token_cancel.is_set():
                    raise runner.ReviewCancelled('parallel batch cancelled before claim')
                return run_one(item, worker_store, token_cancel)
            finally:
                _PROGRESS.sink = previous
                requests._CURRENT.reset(token)

    results, pending = {}, {}
    items = iter(enumerate(prepared, 1))
    first_error = None
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='skodun-batch')
    def submit():
        while len(pending) < 2:
            if cancel is not None and cancel.is_set():
                raise runner.ReviewCancelled("parallel request cancelled before submission")
            try:
                index, item = next(items)
            except StopIteration:
                return
            # Fresh contexts deliberately exclude coordinator Store/callbacks.
            pending[executor.submit(Context().run, work, item)] = index
    try:
        submit()
        while pending:
            drain()
            if first_error is None:
                try:
                    if cancel is not None and cancel.is_set():
                        raise runner.ReviewCancelled('parallel request cancelled')
                except BaseException as exc:
                    first_error = exc
                    stop.set()
            done, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=pending.__getitem__):
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                        stop.set()
            if first_error is None:
                submit()
        if first_error is not None:
            raise first_error
        return results
    finally:
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
        drain()
        if dropped[0]:
            progress(f'parallel progress: {dropped[0]} advisory updates coalesced')
