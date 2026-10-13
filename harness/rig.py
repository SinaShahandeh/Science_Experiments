"""The harness's view of the instrument: the hardware API (mhapi-service or the twin) and the rig clock.

All agent-visible hardware calls go through mhapi.client.call, so the harness works the same against the
real service (MHAPI_URL=http://127.0.0.1:8742) and the digital twin (http://127.0.0.1:8743). The twin
also exposes /sim/clock: the harness uses it to read simulated time and to tell the twin when the model
is thinking (the twin then runs at 1x, so the agent's pacing stays realistic at any sim speed).
"""
import json
import time
import urllib.error
import urllib.request

from mhapi import client

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Rig:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.is_twin = self._probe()
        self._clock_cache = None  # (rig_t, wall_t, speed)

    def _probe(self):
        try:
            with _opener.open(f"{self.url}/sim/clock", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def call(self, command, args=None, timeout=None):
        """(http_status, body) from the service. Raises client.ServiceUnavailable / client.ClientTimeout."""
        if timeout is None:
            timeout = {"load_well": 1800, "clean_all_wells": 1800, "prepare": 300}.get(command, 60)
        return client.call(command, args or {}, timeout=timeout, url=self.url)

    def sim(self, method, path, body=None, timeout=30):
        """(http_status, raw bytes, content type) from the endpoints beside the API: the twin's /sim/*, and
        /operator/* (the operator console and the passive status read) on both the twin and mhapi-service."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except OSError as e:
            return 502, json.dumps({"error": "rig_unreachable", "message": str(e)}).encode(), "application/json"

    def reset(self):
        """A fresh twin for a new run (empty wells, cold bed, not homed). Returns the twin's answer."""
        if not self.is_twin:
            return {"ok": False, "error": "not_a_twin"}
        status, raw, _ = self.sim("POST", "/sim/control", {"reset": True}, timeout=30)
        try:
            return json.loads(raw)
        except ValueError:
            return {"ok": False, "error": f"HTTP {status}"}

    def describe(self):
        status, body = self.call("describe")
        if status != 200 or "tools" not in body:
            raise RuntimeError(f"describe failed: {body}")
        return body["tools"]

    # ---- time
    def clock(self, max_age=0.25):
        """(rig time, speed). Simulated time on the twin, the wall clock on the rig."""
        if not self.is_twin:
            return time.time(), 1.0
        c, wall = self._clock_cache, time.time()
        if c is None or wall - c[1] > max_age:
            try:
                with _opener.open(f"{self.url}/sim/clock", timeout=2) as r:
                    d = json.loads(r.read())
                c = self._clock_cache = (d["t"], time.time(), d["speed"])
            except Exception:
                if c is None:
                    return time.time(), 1.0
        return c[0] + (time.time() - c[1]) * c[2], c[2]

    def now(self):
        return self.clock()[0]

    def thinking(self, on):
        if not self.is_twin:
            return
        try:
            req = urllib.request.Request(f"{self.url}/sim/clock", data=json.dumps({"thinking": bool(on)}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with _opener.open(req, timeout=2) as r:
                d = json.loads(r.read())
            self._clock_cache = (d["t"], time.time(), d["speed"])
        except Exception:
            pass

    def sleep(self, seconds, stop=None):
        """Sleep `seconds` of rig time (shorter in wall time when the twin runs fast)."""
        t_end = self.now() + seconds
        while True:
            t, speed = self.clock()
            left = t_end - t
            if left <= 0:
                return True
            wall = min(left / max(speed, 0.1), 0.5)
            if stop is not None and stop.wait(wall):
                return False
            if stop is None:
                time.sleep(wall)
