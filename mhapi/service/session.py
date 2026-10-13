"""Session folders: sessions/<YYYY-MM-DD_HHMMSS>/ with calls.jsonl, temps.csv, events.csv, frames/, wells/,
state.json and calibration.json (Model_Hardware_API_spec.md, "Sessions and files")."""
import csv
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("mhapi.session")
_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(_\d+)?$")


def iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.dir = None
        self._capture = 0
        self.on_new = []  # callables(folder) run when a session starts (e.g. copy the calibration)

    @property
    def name(self):
        return self.dir.name if self.dir else None

    @property
    def frames_dir(self):
        return self.dir / "frames"

    @property
    def wells_dir(self):
        return self.dir / "wells"

    def restore_latest(self):
        """Open the most recent session and return its saved state, or start a new session ({})."""
        names = sorted(p.name for p in self.root.iterdir() if p.is_dir() and _NAME.match(p.name))
        if not names:
            self.new()
            return {}
        self._open(self.root / names[-1])
        try:
            return json.loads((self.dir / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def new(self):
        with self.lock:
            name = time.strftime("%Y-%m-%d_%H%M%S")
            d, k = self.root / name, 2
            while d.exists():
                d, k = self.root / f"{name}_{k}", k + 1
            self._open(d)
            for fn in self.on_new:
                fn(self.dir)
            log.info("new session %s", self.dir)
            return self.name

    def _open(self, d):
        with self.lock:
            (d / "frames").mkdir(parents=True, exist_ok=True)
            (d / "wells").mkdir(exist_ok=True)
            self.dir = d
            n = 0
            for sub in ("frames", "wells"):
                for p in (d / sub).iterdir():
                    m = re.match(r"^(\d{5})_", p.name)
                    if m:
                        n = max(n, int(m.group(1)))
            self._capture = n

    def next_capture(self):
        with self.lock:
            self._capture += 1
            return self._capture

    # -- logs ------------------------------------------------------------------------------------------
    def _append_csv(self, name, header, row):
        with self.lock:
            p = self.dir / name
            new = not p.exists()
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            if new:
                w.writerow(header)
            w.writerow(row)
            with open(p, "a", newline="") as f:
                f.write(buf.getvalue())

    def log_call(self, entry):
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self.lock:
            with open(self.dir / "calls.jsonl", "a") as f:
                f.write(line + "\n")

    def log_temp(self, t, bed_c, target_c):
        self._append_csv("temps.csv", ["t_unix", "t_iso", "bed_c", "target_c"],
                         [f"{t:.3f}", iso(t), bed_c, target_c])

    def log_event(self, event, **detail):
        t = time.time()
        self._append_csv("events.csv", ["t_unix", "t_iso", "event", "detail"],
                         [f"{t:.3f}", iso(t), event, json.dumps(detail, ensure_ascii=False, default=str)])
        log.info("event %s %s", event, detail)

    def save_state(self, state):
        with self.lock:
            p = self.dir / "state.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, default=str))
            os.replace(tmp, p)
