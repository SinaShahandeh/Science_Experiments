"""The run record (harness_runs/<run_id>/) and the live event bus the dashboard listens to.

Per run:
  trace.jsonl        every event: turns, progress notes, tool calls and results, notes, usage, errors
  messages.json      the conversation as sent to the API (updated each turn)
  run.json           prompt, model, settings, status, token usage and cost
  data/readings.csv  one row per get_raw_opacity        data/temperatures.csv  every bed reading seen
  data/events.csv    set_temperature, load_well, clean_all_wells, prepare, notes, errors
  figures/, analysis/  the agent's plots and analysis scripts (python tool)
"""
import csv
import json
import queue
import threading
import time
from pathlib import Path

READING_COLS = ["t", "label", "gray", "wall_gray", "pixels_used", "pixels_total", "saturated_px", "reflection_px",
                "bed_c", "target_c", "bed_age_s", "image"]
TEMP_COLS = ["t", "bed_c", "target_c", "age_s", "source"]
EVENT_COLS = ["t", "event", "label", "value", "detail"]


class Bus:
    """In-memory event log with replay, fanned out to server-sent-event listeners."""

    def __init__(self, keep=20000):
        self.lock = threading.Lock()
        self.events = []
        self.seq = 0
        self.keep = keep
        self.listeners = []

    def emit(self, ev):
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "t_wall": round(time.time(), 3), **ev}
            self.events.append(ev)
            if len(self.events) > self.keep:
                self.events = self.events[-self.keep:]
            listeners = list(self.listeners)
        for q in listeners:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def since(self, seq):
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


class RunRecord:
    def __init__(self, root, run_id):
        self.dir = Path(root) / run_id
        for sub in ("data", "figures", "analysis"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._trace = open(self.dir / "trace.jsonl", "a")
        self._files = {}
        for name, cols in (("readings", READING_COLS), ("temperatures", TEMP_COLS), ("events", EVENT_COLS)):
            f = open(self.dir / "data" / f"{name}.csv", "a", newline="")
            if f.tell() == 0:
                csv.writer(f).writerow(cols)
                f.flush()
            self._files[name] = (f, cols)

    def trace(self, ev):
        with self._lock:
            if not self._trace.closed:
                self._trace.write(json.dumps(ev, default=str) + "\n")
                self._trace.flush()

    def _row(self, name, d):
        f, cols = self._files[name]
        with self._lock:
            if not f.closed:
                csv.writer(f).writerow(["" if d.get(c) is None else d.get(c) for c in cols])
                f.flush()

    def reading(self, body):
        self._row("readings", body)
        self.temperature(body.get("t"), body.get("bed_c"), body.get("target_c"), body.get("bed_age_s"), "reading")

    def temperature(self, t, bed_c, target_c, age_s, source):
        if bed_c is not None:
            self._row("temperatures", {"t": t, "bed_c": bed_c, "target_c": target_c, "age_s": age_s, "source": source})

    def event(self, t, event, label="", value="", detail=""):
        self._row("events", {"t": t, "event": event, "label": label, "value": value, "detail": detail})

    def write_json(self, name, obj):
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, default=str))
        tmp.replace(self.dir / name)

    def close(self):
        with self._lock:
            self._trace.close()
            for f, _ in self._files.values():
                f.close()
