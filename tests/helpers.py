"""Test helpers: run mhapi-service on the simulator in a subprocess and talk to it through the real CLI."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
FAST = {"motion.z_feed": 1200}  # quicker Z moves in the simulator; the allowlist follows the parameter


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SimService:
    """mhapi-service --sim in a subprocess. Use as a context manager."""

    def __init__(self, overrides=None, homed=True, auto_operator=None, sessions=None, target=0.0):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._tmp = None if sessions else tempfile.TemporaryDirectory(prefix="mhapi-sim-")
        self.sessions = Path(sessions or self._tmp.name)
        self.overrides = {**FAST, **(overrides or {})}
        self.homed, self.auto_operator, self.target = homed, auto_operator, target
        self.proc = None

    def start(self):
        cmd = [PY, "-m", "mhapi.service.main", "--sim", "--port", str(self.port), "--sessions-dir", str(self.sessions),
               "--log-level", "INFO"]
        if self.homed:
            cmd.append("--sim-homed")
        if self.target:
            cmd += ["--sim-target", str(self.target)]
        if self.auto_operator is not None:
            cmd += ["--sim-auto-operator", str(self.auto_operator)]
        for k, v in self.overrides.items():
            cmd += ["--set", f"{k}={json.dumps(v)}"]
        self.log = open(self.sessions / "test-service.log", "a")
        self.proc = subprocess.Popen(cmd, cwd=REPO, stdout=self.log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 30:
            if self.proc.poll() is not None:
                raise RuntimeError(f"service exited: {(self.sessions / 'test-service.log').read_text()[-2000:]}")
            try:
                if self.http("get_status")[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.3)
        raise RuntimeError("service did not start")

    def stop(self, sig=signal.SIGINT, timeout=30):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        if self._tmp:
            self._tmp.cleanup()

    # -- calling it ------------------------------------------------------------------------------------
    def cli(self, *args, timeout=120):
        """Run the real `mhapi` CLI. Returns (exit status, parsed stdout, raw stdout)."""
        env = {**os.environ, "MHAPI_URL": self.url}
        p = subprocess.run([PY, str(REPO / "bin" / "mhapi"), *map(str, args)], capture_output=True, text=True,
                           env=env, timeout=timeout, cwd=REPO)
        lines = p.stdout.splitlines()
        assert len(lines) == 1, f"stdout must be exactly one JSON line, got {p.stdout!r} (stderr {p.stderr!r})"
        return p.returncode, json.loads(lines[0]), p.stdout

    def cli_bg(self, *args):
        env = {**os.environ, "MHAPI_URL": self.url}
        return subprocess.Popen([PY, str(REPO / "bin" / "mhapi"), *map(str, args)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, cwd=REPO)

    def http(self, command, body=None, timeout=60):
        return post(f"{self.url}/v1/{command}", body, timeout)

    def fault(self, name, value=True):
        return post(f"{self.url}/sim/fault", {"name": name, "value": value})[1]

    def sim_state(self):
        return post(f"{self.url}/sim/state", {})[1]

    def console(self):
        with urllib.request.urlopen(f"{self.url}/operator/state", timeout=5) as r:
            return json.loads(r.read())

    def status(self):
        """get_status's body from GET /operator/status: not an API call (no H4 reset, not in calls.jsonl)."""
        with urllib.request.urlopen(f"{self.url}/operator/status", timeout=5) as r:
            return json.loads(r.read())

    def answer(self, answer="done", note=None, wait=10):
        t0 = time.time()
        while time.time() - t0 < wait:
            p = self.console()["prompt"]
            if p:
                return post(f"{self.url}/operator/answer", {"id": p["id"], "answer": answer, "note": note})[1]
            time.sleep(0.2)
        raise AssertionError("no operator prompt appeared")

    def clear(self):
        with urllib.request.urlopen(f"{self.url}/operator", timeout=5) as r:
            html = r.read().decode()
        token = html.split('const TOKEN = "')[1].split('"')[0]
        return post(f"{self.url}/operator/clear", {"token": token})

    def session_dir(self):
        return self.sessions / self.http("get_status")[1]["session"]

    def events(self):
        import csv
        with open(self.session_dir() / "events.csv") as f:
            return list(csv.DictReader(f))


def post(url, body=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_until(fn, timeout=30, every=0.25, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}")
