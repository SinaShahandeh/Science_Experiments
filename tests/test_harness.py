"""The harness's public-deployment pieces: limits, run ownership, the twin pass-through, the sandbox; and instrument
mode: the camera feeds, the status poll and the operator console pass-through (mhapi-service on its simulator).
No API calls, no hardware.

    .venv-harness/bin/python -m unittest tests.test_harness
"""
import itertools
import json
import re
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import sandbox, server
from harness.agent import AgentRun
from harness.env import ROOT
from harness.feeds import Feed, a1_frames, mjpeg_source
from harness.limits import Limits
from harness.record import Bus
from harness.rig import Rig as RigClient
from mhapi import client as mhapi_client
from twin.rig import Rig
from twin.server import serve as serve_twin

try:
    import fastapi  # noqa: F401 - mhapi-service runs on it
    import mhapi.service  # noqa: F401
    from tests.helpers import SimService
except ImportError:  # the public deployment (deploy/gcp.sh) ships neither the service nor its test helpers
    SimService = None


class FakeRun:
    """Stands in for AgentRun: alive until stopped."""

    def __init__(self, harness, prompt, effort, attach_protocol):
        self.token = secrets.token_urlsafe(8)
        self.status = "running"
        self.run_id = f"run{secrets.token_hex(3)}"
        self._stopped = threading.Event()
        self.thread = types.SimpleNamespace(is_alive=lambda: not self._stopped.is_set(), join=lambda timeout=None: None)

    def start(self):
        return self

    def stop(self, reason=None):
        self._stopped.set()
        self.status = "stopped"

    def summary(self):
        return {"run_id": self.run_id, "status": self.status}


class FakeRig:
    url, is_twin = "http://127.0.0.1:1", True

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1
        return {"ok": True}

    def now(self):
        return time.time()


class LimitsTest(unittest.TestCase):
    def test_budget_and_visitors(self):
        with tempfile.TemporaryDirectory() as d:
            lim = Limits(Path(d) / "l.json", public=True, day_usd=10, ip_runs=2, admin_token="adm")
            self.assertIsNone(lim.check_start("1.2.3.4"))
            lim.note_start("1.2.3.4")
            lim.note_start("1.2.3.4")
            self.assertIn("2 runs", lim.check_start("1.2.3.4"))
            self.assertIsNone(lim.check_start("5.6.7.8"))
            lim.add_cost(10.5)
            self.assertIn("budget", lim.check_start("5.6.7.8"))
            self.assertNotIn("1.2.3.4", (Path(d) / "l.json").read_text())   # addresses are stored hashed
            again = Limits(Path(d) / "l.json", public=True, day_usd=10)       # survives a restart
            self.assertEqual(again.day_left(), 0.0)
            self.assertTrue(lim.is_admin("adm") and not lim.is_admin("x") and not lim.is_admin(""))

    def test_new_day(self):
        with tempfile.TemporaryDirectory() as d:
            lim = Limits(Path(d) / "l.json", public=True, day_usd=10)
            lim.add_cost(20)
            lim.state["day"] = "2000-01-01"
            self.assertEqual(lim.day_left(), 10)

    def test_off_locally(self):
        lim = Limits("/nonexistent/l.json")
        lim.add_cost(1e6)
        self.assertIsNone(lim.check_start("x"))
        self.assertIsNone(lim.info())


class PublicRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = server.AgentRun
        server.AgentRun = FakeRun
        lim = Limits(Path(self.tmp.name) / "l.json", public=True, day_usd=10, ip_runs=3, admin_token="adm")
        self.h = server.Harness(FakeRig(), Bus(), self.tmp.name, client=None, limits=lim)

    def tearDown(self):
        server.AgentRun = self._orig
        self.tmp.cleanup()

    def test_one_run_at_a_time(self):
        st, a = self.h.start_run("go", fresh_rig=True, ip="a")
        self.assertEqual(st, 200)
        self.assertEqual(self.h.rig.resets, 1)
        st, b = self.h.start_run("me too", ip="b")
        self.assertEqual(st, 409)
        self.assertFalse(self.h.owns_run("") or self.h.owns_run("wrong"))
        self.assertTrue(self.h.owns_run(a["token"]))
        self.assertFalse(self.h.may_steer(""))
        self.assertTrue(self.h.may_steer(a["token"]) and self.h.may_steer("adm"))
        self.h.run.status = "finished"                        # finished, awaiting messages: others may start
        self.assertTrue(self.h.may_steer(""))
        st, b = self.h.start_run("me too", ip="b")
        self.assertEqual(st, 200)
        self.assertNotEqual(a["token"], b["token"])

    def test_admin_and_validation(self):
        self.h.start_run("go", ip="a")
        self.assertEqual(self.h.start_run("x", ip="b", token="adm")[0], 200)
        self.assertEqual(self.h.start_run("x", effort="max", ip="b")[0], 400)
        self.assertEqual(self.h.start_run("", ip="b")[0], 400)
        self.assertEqual(self.h.start_run("x" * 7000, ip="b")[0], 400)

    def test_daily_budget(self):
        self.h.limits.add_cost(11)
        st, out = self.h.start_run("go", ip="a")
        self.assertEqual(st, 429)


class RunCaps(unittest.TestCase):
    def test_stops_at_the_run_limit(self):
        with tempfile.TemporaryDirectory() as d:
            lim = Limits(Path(d) / "l.json", public=True, run_usd=1.0, day_usd=10, run_min=60)
            h = types.SimpleNamespace(rig=FakeRig(), bus=Bus(), runs_root=Path(d), limits=lim)
            run = AgentRun(h, "go")
            run._set_status("running")
            run._check_limits()
            self.assertFalse(run.stop_event.is_set())
            run.usage["cost"] = 1.2
            run._check_limits()
            self.assertTrue(run.stop_event.is_set())
            self.assertIn("$1.00", run.stop_reason)
            run.record.close()

    def test_time_limit_counts_only_work(self):
        with tempfile.TemporaryDirectory() as d:
            lim = Limits(Path(d) / "l.json", public=True, run_usd=5, day_usd=10, run_min=1)
            h = types.SimpleNamespace(rig=FakeRig(), bus=Bus(), runs_root=Path(d), limits=lim)
            run = AgentRun(h, "go")
            run._set_status("running")
            run.active_since -= 50
            run._set_status("finished")          # awaiting a message: the clock stops
            run.active_s += 0
            run._check_limits()
            self.assertFalse(run.stop_event.is_set())
            run._set_status("running")
            run.active_since -= 15
            run._check_limits()
            self.assertIn("1 min", run.stop_reason or "")
            run.record.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TwinPassThrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.twin = Rig(cls.tmp.name, speed=50, auto_operator=True, seed=3)
        port = free_port()
        cls.twin_http = serve_twin(cls.twin, "127.0.0.1", port)
        threading.Thread(target=cls.twin_http.serve_forever, daemon=True).start()
        rig = RigClient(f"http://127.0.0.1:{port}")
        lim = Limits(Path(cls.tmp.name) / "l.json", public=True)
        cls.h = server.Harness(rig, Bus(), cls.tmp.name, client=None, limits=lim)
        cls.port = free_port()
        cls.http = server.serve(cls.h, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.twin_http.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.read()

    def post(self, path, body, token=""):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Run-Token": token}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_endpoints(self):
        st, raw = self.get("/rig/sim/geometry")
        self.assertEqual(st, 200)
        self.assertIn("wells", json.loads(raw))
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/rig/v1/get_status")                      # the agent's API is not exposed
        st, out = self.post("/rig/sim/control", {"speed": 500, "fault": "heater_stuck"})
        self.assertEqual(st, 400)                                # not a public fault
        st, out = self.post("/rig/sim/control", {"speed": 500})
        self.assertEqual(out["user_speed"], 20.0)                # clamped for visitors

    def test_shared_stream(self):
        def read_some(n=1500):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/rig/sim/stream", timeout=10) as r:
                return r.read1(n) + r.read1(n)
        outs = []
        ts = [threading.Thread(target=lambda: outs.append(read_some())) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(len(outs), 3)
        self.assertTrue(all(b"event: state" in o for o in outs))

    def test_passive_status(self):
        tw = self.twin
        t_call = tw.last_call_t
        with urllib.request.urlopen(f"{self.h.rig.url}/operator/status", timeout=10) as r:
            body = json.loads(r.read())
        self.assertEqual(set(body), set(tw.cmd_get_status()))      # get_status's body
        self.assertEqual(tw.last_call_t, t_call)                    # without resetting H4

    def test_reset(self):
        tw = self.twin
        self.assertTrue(tw.submit("prepare", {})[1]["ready"])
        self.assertEqual(tw.submit("load_well", {"label": "C-A1"})[0], 200)
        tw.submit("set_temperature", {"temperature": 60})
        tw.stop("test")
        old = tw.session.name
        time.sleep(1.1)                                          # session folders are named by the second
        out = self.h.rig.reset()
        self.assertTrue(out["ok"], out)
        st = tw.cmd_get_status()
        self.assertEqual(st["loaded_wells"], [])
        self.assertEqual((st["homed"], tw.safety, tw.bed.target), (False, "ok", 0))
        self.assertAlmostEqual(tw.bed.ts, 23.0, delta=0.01)
        self.assertNotEqual(tw.session.name, old)


def wait_for(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def jpeg(i):
    return b"\xff\xd8" + b"frame %05d" % i + b"\xff\xd9"


class FakePi(BaseHTTPRequestHandler):
    """An MJPEG server like the Pi camera's: a new frame every 50 ms."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            for i in itertools.count(1):
                f = jpeg(i)
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(f), f))
                time.sleep(0.05)
        except OSError:
            pass


class Feeds(unittest.TestCase):
    def test_a1_frames(self):
        a, b = socket.socketpair()
        with a, b:
            for i in (1, 2):
                a.sendall(struct.pack("<IIII", len(jpeg(i)), 0, 1, 0) + jpeg(i))
            a.sendall(struct.pack("<IIII", 0, 0, 1, 0))
            frames = a1_frames(b)
            self.assertEqual([next(frames), next(frames)], [jpeg(1), jpeg(2)])
            with self.assertRaises(ConnectionError):
                next(frames)

    def test_one_upstream_while_watched(self):
        events = []

        def source(problem):
            events.append("open")
            try:
                for i in itertools.count(1):
                    time.sleep(0.02)
                    yield jpeg(i)
            finally:
                events.append("close")

        feed = Feed("t", source)
        with feed.watch(), feed.watch():                       # two viewers, one upstream
            seq, _, jpg = feed.wait_newer(0, 5)
            self.assertEqual(jpg[:2], b"\xff\xd8")
            self.assertGreater(feed.wait_newer(seq, 5)[0], seq)
            feed.wait_newer(seq + 1, 5)                         # a third frame: enough for a rate
            self.assertIsNotNone(feed.health()["fps"])
        wait_for(lambda: feed.thread is None)
        self.assertEqual(events, ["open", "close"])
        self.assertIsNone(feed.latest())                        # nothing left over from a closed connection
        self.assertEqual((feed.health()["age_s"], feed.health()["watched"]), (None, False))

    def test_problems_show_in_health(self):
        def source(problem):
            while True:
                problem("camera unreachable")
                yield None
                time.sleep(0.02)

        feed = Feed("t", source)
        with feed.watch():
            self.assertIsNone(feed.wait_newer(0, 0.3))
            self.assertEqual(feed.health()["error"], "camera unreachable")


class InstrumentFeeds(unittest.TestCase):
    """The harness re-serves a camera (here a fake Pi): MJPEG and single frames, one upstream while anyone watches."""

    @classmethod
    def setUpClass(cls):
        cls.pi = ThreadingHTTPServer(("127.0.0.1", 0), FakePi)
        cls.pi.daemon_threads = True
        threading.Thread(target=cls.pi.serve_forever, daemon=True).start()
        cls.feed = Feed("pi", mjpeg_source(f"http://127.0.0.1:{cls.pi.server_port}/stream.mjpg"))
        cls.tmp = tempfile.TemporaryDirectory()
        rig = types.SimpleNamespace(url="http://127.0.0.1:9", is_twin=False)
        cls.h = server.Harness(rig, Bus(), cls.tmp.name, client=None, feeds={"pi": cls.feed})
        cls.port = free_port()
        cls.http = server.serve(cls.h, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.pi.shutdown()
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_mjpeg(self):
        with urllib.request.urlopen(self.url("/feeds/pi.mjpg"), timeout=10) as r:
            self.assertEqual(r.headers["Content-Type"], "multipart/x-mixed-replace; boundary=frame")
            buf = b""
            while buf.count(b"--frame\r\n") < 3:
                buf += r.read1(4096)
        for part in buf.split(b"--frame\r\n")[1:3]:
            head, _, rest = part.partition(b"\r\n\r\n")
            self.assertIn(b"Content-Type: image/jpeg", head)
            n = int(re.search(rb"Content-Length: (\d+)", head).group(1))
            self.assertTrue(rest[:n].startswith(b"\xff\xd8frame") and rest[:n].endswith(b"\xff\xd9"))
            self.assertEqual(rest[n:], b"\r\n")                 # the next boundary follows at once
        wait_for(lambda: self.feed.thread is None)              # the viewer left: the upstream closes

    def test_single_frame(self):
        with urllib.request.urlopen(self.url("/feeds/pi.jpg"), timeout=15) as r:
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/jpeg"))
            self.assertTrue(r.read().startswith(b"\xff\xd8frame"))
        for path in ("/feeds/a1.jpg", "/feeds/pi.gif", "/rig/sim/geometry", "/rig/v1/get_status"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.url(path), timeout=5)
            self.assertEqual(e.exception.code, 404, path)

    def test_config(self):
        with urllib.request.urlopen(self.url("/api/config"), timeout=5) as r:
            inst = json.loads(r.read())["instrument"]
        self.assertEqual(inst["pi_frame_px"], [1280, 720])
        nadir = ROOT / "calibration" / "nadir.json"
        if nadir.exists():                                        # not in the public deployment's bundle
            self.assertEqual(inst["nadir_px"], json.loads(nadir.read_text())["nadir_px"])


@unittest.skipIf(SimService is None, "needs mhapi-service (its simulator)")
class InstrumentStatus(unittest.TestCase):
    """Instrument mode against mhapi-service on its simulator: the status poll, and the operator console's
    Done and Stop through the harness."""

    @classmethod
    def setUpClass(cls):
        cls.svc = SimService(homed=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.h = server.Harness(RigClient(cls.svc.url), Bus(), cls.tmp.name, client=None)
        cls.port = free_port()
        cls.http = server.serve(cls.h, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.svc.stop()
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, body):
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def calls(self, session, command):
        lines = (self.svc.sessions / session / "calls.jsonl").read_text().splitlines()
        return sum(json.loads(line)["command"] == command for line in lines)

    def test_1_status_stream(self):
        self.assertFalse(self.h.rig.is_twin)
        session = self.svc.http("get_status")[1]["session"]
        n = self.calls(session, "get_status")
        with urllib.request.urlopen(self.url("/rig/status/stream"), timeout=10) as r:
            buf = b""
            while buf.count(b"\n\n") < 3:
                buf += r.read1(65536)
        evs = [json.loads(ev.split(b"data: ", 1)[1]) for ev in buf.split(b"\n\n")[:3]]
        last = evs[-1]
        self.assertIsNone(last["status_error"])
        self.assertTrue(last["status"]["homed"] and last["status"]["ready"])
        self.assertEqual((last["status"]["safety"], last["operator"]["prompt"], last["feeds"]), ("ok", None, {}))
        self.assertEqual(last["status"]["session"], session)
        self.assertEqual(len({e["operator"]["t"] for e in evs}), 3)   # the console every second
        self.assertLessEqual(len({e["status"]["t"] for e in evs}), 2)  # get_status's body every 2 s
        wait_for(lambda: self.h.status_stream.thread is None)        # nobody watches: the poll stops
        self.assertEqual(self.calls(session, "get_status"), n)        # read passively: not an API call

    def test_2_operator_done_and_stop(self):
        out = {}
        load = threading.Thread(target=lambda: out.update(
            r=mhapi_client.call("load_well", {"label": "C-A1"}, timeout=60, url=self.svc.url)))
        load.start()
        prompt = {}

        def asked():
            with urllib.request.urlopen(self.url("/rig/operator/state"), timeout=5) as r:
                prompt.update(json.loads(r.read())["prompt"] or {})
            return bool(prompt)
        wait_for(asked)
        self.assertIn("C-A1", prompt["text"])
        self.assertEqual(self.post("/rig/operator/answer", {"id": prompt["id"], "answer": "done", "who": "dashboard"}),
                         (200, {"ok": True}))
        load.join(30)
        self.assertEqual(out["r"][0], 200, out)
        st, body = self.post("/rig/operator/stop", {"who": "dashboard"})
        self.assertEqual((st, body["safety"]), (200, "halted"))
        self.assertEqual(self.post("/rig/operator/clear", {"token": "x"})[0], 404)   # Clear stays on the console page
        self.assertEqual(self.post("/rig/sim/control", {"speed": 5})[0], 404)
        self.assertEqual(self.svc.clear()[0], 200)


@unittest.skipIf(SimService is None, "needs mhapi-service (its simulator)")
class InstrumentWatchdog(unittest.TestCase):
    """H4 on mhapi-service's simulator with a 6 s heater watchdog: a dashboard left open doesn't keep a stalled
    agent's heater on, and the agent's own calls still do."""

    WATCHDOG_S = 6

    def setUp(self):
        self.svc = SimService(overrides={"heat.heater_watchdog_s": self.WATCHDOG_S}).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.h = server.Harness(RigClient(self.svc.url), Bus(), self.tmp.name, client=None)
        self.port = free_port()
        self.http = server.serve(self.h, "127.0.0.1", self.port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.events, self.stop = [], threading.Event()
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.watcher.start()

    def tearDown(self):
        self.stop.set()
        self.watcher.join(5)
        self.http.shutdown()
        self.http.server_close()
        self.svc.__exit__()
        self.tmp.cleanup()

    def watch(self):
        """A dashboard page left open: reads /rig/status/stream until the test ends."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/rig/status/stream", timeout=10) as r:
            buf = b""
            while not self.stop.is_set():
                buf += r.read1(65536)
                *done, buf = buf.split(b"\n\n")
                self.events += [json.loads(ev.split(b"data: ", 1)[1]) for ev in done if b"data: " in ev]

    def shown(self):
        """The safety state the page shows now (from the status poll)."""
        ev = self.events[-1] if self.events else None
        return ev and ev["status"] and ev["status"]["safety"]

    def agent(self, command, args=None):
        return mhapi_client.call(command, args or {}, timeout=30, url=self.svc.url)

    def calls(self, session):
        return len((self.svc.sessions / session / "calls.jsonl").read_text().splitlines())

    def test_poll_does_not_feed_the_watchdog(self):
        W = self.WATCHDOG_S
        wait_for(lambda: self.shown() == "ok")
        self.assertEqual(self.agent("set_temperature", {"temperature": 50})[0], 200)
        t_end = time.time() + 1.5 * W                               # the agent keeps calling: H4 stays quiet
        while time.time() < t_end:
            code, st = self.agent("get_status")
            self.assertEqual((code, st["safety"]), (200, "ok"))
            time.sleep(1)
        self.assertEqual(self.svc.sim_state()["printer"]["target"], 50.0)
        n_calls, t_quiet = self.calls(st["session"]), time.time()  # then it stalls; the page keeps watching
        wait_for(lambda: self.shown() == "heater_timeout", timeout=W + 10)
        quiet = [e for e in self.events if e["t"] > t_quiet]
        self.assertGreaterEqual(len({e["status"]["t"] for e in quiet}), 2)   # the poll ran throughout
        self.assertTrue(self.h.status_stream.thread and self.h.status_stream.thread.is_alive())
        self.assertIsNone(self.events[-1]["status_error"])
        self.assertEqual(self.calls(st["session"]), n_calls)                  # and left no API calls
        wait_for(lambda: self.svc.sim_state()["printer"]["target"] == 0.0)    # heater off
        self.assertEqual(self.events[-1]["operator"]["safety"]["state"], "heater_timeout")
        self.assertEqual(self.agent("set_temperature", {"temperature": 50})[0], 200)   # the agent resumes
        wait_for(lambda: self.shown() == "ok")
        wait_for(lambda: self.svc.sim_state()["printer"]["target"] == 50.0)


class Sandbox(unittest.TestCase):
    def test_contained(self):
        outside = ROOT / "harness" / "_sandbox_escape_test.txt"   # the harness's own code folder
        with tempfile.TemporaryDirectory() as d:
            code = (
                "import socket, os, pathlib\n"
                "try:\n    socket.create_connection(('127.0.0.1', 8743), timeout=2); print('NET OPEN')\n"
                "except OSError:\n    print('net blocked')\n"
                f"try:\n    pathlib.Path({str(outside)!r}).write_text('x'); print('WROTE OUTSIDE')\n"
                "except OSError:\n    print('write blocked')\n"
                "pathlib.Path('figures/ok.txt').write_text('fine'); print('wrote inside')\n"
                "print('key' if 'ANTHROPIC_API_KEY' in os.environ else 'no key')\n")
            try:
                ok, text, _ = sandbox.run_python(code, d, 1)
                self.assertFalse(outside.exists())
            finally:
                outside.unlink(missing_ok=True)
            self.assertTrue(ok, text)
            for want in ("net blocked", "write blocked", "wrote inside", "no key"):
                self.assertIn(want, text)
            self.assertNotIn("NOT sandboxed", text)

    @unittest.skipUnless(sys.platform.startswith("linux"), "bubblewrap is Linux only")
    def test_linux_hides_the_host(self):
        with tempfile.TemporaryDirectory() as d:
            code = ("import os\nprint(sorted(os.listdir('/')))\nprint(len(os.listdir('/proc')))\n"
                    "print(os.path.exists('/etc/aisim'), os.path.exists('/home'))\n")
            ok, text, _ = sandbox.run_python(code, d, 1)
            self.assertTrue(ok, text)
            self.assertIn("False False", text)


if __name__ == "__main__":
    unittest.main()
