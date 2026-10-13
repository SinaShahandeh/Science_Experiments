"""Limits for a public deployment, where visitors start runs on the owner's API key.

Off for local use. `python -m harness --public` turns them on:
- one agent run at a time; only the browser that started it may stop it, message it, or steer the twin
  (speed, faults, operator answers) while it runs. Anyone may watch and replay.
- each run stops at `run_usd` dollars or `run_min` minutes of agent work (wall clock),
- no new runs once `day_usd` has been spent today (UTC), and at most `ip_runs` runs per visitor per day,
- prompts and messages limited in length.
The day's spending survives restarts in <runs>/_limits.json. Visitors are counted by a salted hash of their
address, not the address itself. An admin token (AISIM_ADMIN_TOKEN) bypasses the run limits.
"""
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path


class Limits:
    def __init__(self, path, public=False, run_usd=6.0, day_usd=40.0, run_min=90.0, ip_runs=4,
                 prompt_chars=6000, message_chars=2000, admin_token=""):
        self.path = Path(path)
        self.public = public
        self.run_usd, self.day_usd, self.run_min = float(run_usd), float(day_usd), float(run_min)
        self.ip_runs, self.prompt_chars, self.message_chars = int(ip_runs), int(prompt_chars), int(message_chars)
        self.admin_token = admin_token or ""
        self.lock = threading.Lock()
        self.state = self._load()

    # ---------------------------------------------------------------- persistence
    @staticmethod
    def _today():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self):
        try:
            s = json.loads(self.path.read_text())
            if isinstance(s, dict) and "day" in s:
                s.setdefault("salt", secrets.token_hex(8))
                return s
        except (OSError, ValueError):
            pass
        return {"day": self._today(), "spent": 0.0, "visitors": {}, "salt": secrets.token_hex(8)}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.path)

    def _roll(self):
        if self.state.get("day") != self._today():
            self.state = {"day": self._today(), "spent": 0.0, "visitors": {}, "salt": secrets.token_hex(8)}

    def _visitor(self, ip):
        return hashlib.sha256((self.state["salt"] + (ip or "?")).encode()).hexdigest()[:16]

    # ---------------------------------------------------------------- checks
    def is_admin(self, token):
        return bool(self.admin_token) and bool(token) and hmac.compare_digest(token, self.admin_token)

    def check_start(self, ip):
        """None if this visitor may start a run now, else the reason (shown in the dashboard)."""
        if not self.public:
            return None
        with self.lock:
            self._roll()
            if self.state["spent"] >= self.day_usd:
                return (f"Today's budget for the public demo (${self.day_usd:.0f}) is used up. It resets at 00:00 UTC; "
                        "recorded runs can still be replayed from the menu.")
            if self.state["visitors"].get(self._visitor(ip), 0) >= self.ip_runs:
                return f"You have started {self.ip_runs} runs today, the most the public demo allows per visitor."
        return None

    def note_start(self, ip):
        if not self.public:
            return
        with self.lock:
            self._roll()
            v = self._visitor(ip)
            self.state["visitors"][v] = self.state["visitors"].get(v, 0) + 1
            self._save()

    def add_cost(self, usd):
        if not self.public or not usd:
            return
        with self.lock:
            self._roll()
            self.state["spent"] = round(self.state["spent"] + float(usd), 4)
            self._save()

    def day_left(self):
        with self.lock:
            self._roll()
            return max(0.0, self.day_usd - self.state["spent"])

    def info(self):
        if not self.public:
            return None
        return {"run_usd": self.run_usd, "run_min": self.run_min, "day_usd": self.day_usd,
                "day_left": round(self.day_left(), 2), "ip_runs": self.ip_runs, "prompt_chars": self.prompt_chars}
