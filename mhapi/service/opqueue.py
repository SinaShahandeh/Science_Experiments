"""Calls that move the printer or wait for a person run one at a time, in arrival order (API spec "Calls that
arrive together"). A queued call whose caller has gone away before it starts is dropped; a started one is
finished even if the caller stops waiting, and its result stays in get_status().last_operation."""
import itertools
import logging
import threading
import time
import traceback
from collections import deque

from .errors import ApiError

log = logging.getLogger("mhapi.ops")


class Operation:
    _ids = itertools.count(1)

    def __init__(self, command, args, fn):
        self.id = next(self._ids)
        self.command, self.args, self.fn = command, args, fn
        self.state = "queued"       # queued | running | done | failed | dropped
        self.result, self.http_status = None, None
        self.t_submit, self.t_start, self.t_end = time.time(), None, None
        self.on_done = []
        self.finished = threading.Event()

    def view(self, with_result=True):
        v = {"command": self.command, "args": {k: a for k, a in self.args.items() if k != "inline_image"},
             "state": self.state, "t_start": _r(self.t_start), "t_end": _r(self.t_end)}
        if with_result and self.finished.is_set() and self.result is not None:
            v["result"] = {k: a for k, a in self.result.items() if k != "image_base64"}
        return v


def _r(t):
    return None if t is None else round(t, 3)


class OpQueue:
    def __init__(self, on_change=None):
        self.q = deque()
        self.cv = threading.Condition()
        self.current = None
        self.last = None
        self.on_change = on_change or (lambda: None)
        threading.Thread(target=self._run, name="operations", daemon=True).start()

    def submit(self, op):
        with self.cv:
            self.q.append(op)
            self.cv.notify_all()
        return op

    def drop(self, op):
        """Drop a call that hasn't started (its caller went away). Returns True if it was dropped."""
        with self.cv:
            if op.state != "queued":
                return False
            self.q.remove(op)
            op.state = "dropped"
        self._finish(op, None, None)
        return True

    def fail_queued(self, body_fn, status):
        """Answer every queued call with an error (safety stop, shutdown)."""
        with self.cv:
            ops, self.q = list(self.q), deque()
            for op in ops:
                op.state = "failed"
        for op in ops:
            op.t_start = op.t_end = time.time()
            self._finish(op, body_fn(), status)

    @property
    def queued(self):
        return len(self.q)

    def last_operation(self):
        op = self.current or self.last
        return op.view() if op else None

    def _finish(self, op, result, status):
        op.result, op.http_status = result, status
        op.t_end = op.t_end or time.time()
        op.finished.set()
        for fn in op.on_done:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("completion callback failed")

    def _run(self):
        while True:
            with self.cv:
                self.cv.wait_for(lambda: self.q)
                op = self.q.popleft()
                op.state, op.t_start = "running", time.time()
                self.current = op
            self.on_change()
            try:
                result, status, state = op.fn(), 200, "done"
            except ApiError as e:
                result, status, state = e.body(), e.http_status, "failed"
            except Exception as e:  # noqa: BLE001 - a bug: report it rather than hang the caller
                log.error("operation %s crashed:\n%s", op.command, traceback.format_exc())
                result = {"error": "internal_error", "message": f"{type(e).__name__}: {e}", "t": round(time.time(), 3)}
                status, state = 500, "failed"
            op.state, op.t_end = state, time.time()
            with self.cv:
                self.current, self.last = None, op
            self._finish(op, result, status)
            self.on_change()
