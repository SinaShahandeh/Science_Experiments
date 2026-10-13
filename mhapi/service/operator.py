"""Operator hand-offs: three chimes, a spoken sentence on the control computer, and the request on the console.
The person answers Done or Problem (with an optional note) on the console page."""
import itertools
import logging
import queue
import subprocess
import threading
import time

log = logging.getLogger("mhapi.operator")
CHIME = "/System/Library/Sounds/Glass.aiff"


class Announcer:
    """Chimes and speech, played one message at a time in the background."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.q = queue.Queue()
        threading.Thread(target=self._run, name="announcer", daemon=True).start()

    def say(self, text, chimes=3):
        log.info("announce: %s", text)
        if self.enabled:
            self.q.put((text, chimes))

    def _run(self):
        while True:
            text, chimes = self.q.get()
            try:
                for _ in range(chimes):
                    subprocess.run(["afplay", CHIME], timeout=10, check=False)
                subprocess.run(["say", text], timeout=60, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("announcement failed: %s", e)


class Prompt:
    _ids = itertools.count(1)

    def __init__(self, kind, text, label):
        self.id = next(self._ids)
        self.kind, self.text, self.label = kind, text, label
        self.t_requested = time.time()
        self.answer = None       # done | problem | timeout | cancelled
        self.note = None
        self.t_answer = None
        self.event = threading.Event()

    def view(self):
        return {"id": self.id, "kind": self.kind, "text": self.text, "label": self.label,
                "t_requested": round(self.t_requested, 3)}


class OperatorDesk:
    def __init__(self, announcer, auto_done_after_s=None):
        self.announcer = announcer
        self.auto_done_after_s = auto_done_after_s  # simulator only: press Done automatically
        self.lock = threading.Lock()
        self.current = None

    def ask(self, kind, text, spoken, label=None, timeout=900.0):
        """Show a request and wait for Done or Problem. Returns the answered Prompt."""
        p = Prompt(kind, text, label)
        with self.lock:
            self.current = p
        self.announcer.say(spoken)
        if self.auto_done_after_s is not None:
            threading.Timer(self.auto_done_after_s, self.answer, (p.id, "done", None)).start()
        if not p.event.wait(timeout):
            self._finish(p, "timeout", None)
        return p

    def answer(self, prompt_id, answer, note=None):
        with self.lock:
            p = self.current
        if p is None or p.id != prompt_id or answer not in ("done", "problem"):
            return False
        return self._finish(p, answer, note)

    def cancel(self, reason="stop"):
        with self.lock:
            p = self.current
        if p is not None:
            self._finish(p, "cancelled", reason)

    def _finish(self, p, answer, note):
        with self.lock:
            if p.answer is not None:
                return False
            p.answer, p.note, p.t_answer = answer, (note or None), time.time()
            if self.current is p:
                self.current = None
        p.event.set()
        return True

    def view(self):
        with self.lock:
            return self.current.view() if self.current else None
