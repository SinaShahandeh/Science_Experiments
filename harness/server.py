"""The harness's HTTP server: the dashboard page and the simple API it uses.

  GET  /                         dashboard (dashboard/index.html)
  GET  /dashboard/<file>         its scripts and styles
  GET  /api/config               model, scenarios, the active run, the public demo's limits
  GET  /api/stream?since=N       server-sent events: everything the agent does, live (replays from N)
  GET  /api/runs                 past runs        GET /api/runs/<id>/trace   one run's recorded events
  POST /api/run {prompt, effort, attach_protocol, fresh_rig}   start a run (stops a finished one first)
  POST /api/stop                 stop the agent now (an instrument call in progress finishes; the instrument is not touched)
  POST /api/message {text}       send the agent a message; continues a finished run
  POST /api/operator {id, answer, note}   the person's answer (done | problem) to the agent's call_operator
  GET  /file?p=<path>            images and CSVs under sim_sessions/, sessions/ and harness_runs/
  /rig/...                       the rig's endpoints beside its API, passed through so the page needs one address.
                                 The twin's: GET /rig/sim/{geometry,state,clock,truth,stream}, GET /rig/operator/state,
                                 POST /rig/sim/control, /rig/operator/answer, /rig/operator/stop.
                                 mhapi-service's (instrument mode): GET /rig/operator/state, POST /rig/operator/answer,
                                 /rig/operator/stop; and GET /rig/status/stream, server-sent events from one poll of the
                                 service however many pages watch (StatusStream)
  GET  /feeds/{a1,pi}.mjpg       instrument mode: the A1's own camera and the Pi camera, as MJPEG (harness/feeds.py)
  GET  /feeds/{a1,pi}.jpg        the newest frame

With --public (harness/limits.py) only one run goes at a time, and the POSTs that steer it (stop, message, operator
answers, the twin's controls) need the X-Run-Token header the starting browser got back from POST /api/run.
"""
import json
import queue
import select
import socket
import threading
import time
import urllib.request
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import MODEL
from .agent import AgentRun
from .env import ROOT
from .feeds import PI_FRAME, nadir_px
from .limits import Limits
from .prompts import SCENARIOS

DASH = ROOT / "dashboard"
FILE_ROOTS = [ROOT / "sim_sessions", ROOT / "sessions", ROOT / "harness_runs"]
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".png": "image/png", ".jpg": "image/jpeg", ".csv": "text/csv; charset=utf-8", ".json": "application/json",
          ".svg": "image/svg+xml", ".md": "text/markdown; charset=utf-8", ".py": "text/plain; charset=utf-8"}
EFFORTS = ("low", "medium", "high")
RIG_GET = ("/sim/geometry", "/sim/state", "/sim/clock", "/sim/truth", "/operator/state")
RIG_POST = ("/sim/control", "/operator/answer", "/operator/stop")
CONSOLE_GET = ("/operator/state",)                    # mhapi-service's operator console (instrument mode)
CONSOLE_POST = ("/operator/answer", "/operator/stop")  # Clear stays on the console page itself
# what visitors may change on a public twin: the dashboard's own controls
PUBLIC_CONTROL = {"speed", "auto_operator", "room_light", "fault", "label", "mm", "island", "dx", "dy", "seconds"}
PUBLIC_FAULTS = {"bubble", "camera_bump", "island_shift", "lose_home", "printer_offline"}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Fanout:
    """Server-sent events from one background thread to every dashboard listening. The thread runs while one is."""

    name = "fanout"

    def __init__(self):
        self.lock = threading.Lock()
        self.listeners = []
        self.thread = None

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.listeners.append(q)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name=self.name)
                self.thread.start()
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def _broadcast(self, data):
        with self.lock:
            listeners = list(self.listeners)
        for q in listeners:
            try:
                q.put_nowait(data)
            except queue.Full:  # a slow viewer misses frames
                pass

    def _idle(self):
        """True, and the thread is to end, once nobody listens."""
        with self.lock:
            if not self.listeners:
                self.thread = None
                return True
        return False

    def _run(self):
        raise NotImplementedError


class RigStream(Fanout):
    """One connection to the twin's /sim/stream, shared by every dashboard watching (10 state frames a second)."""

    name = "rig-stream"

    def __init__(self, url):
        super().__init__()
        self.url = url

    def _run(self):
        while not self._idle():
            try:
                with _opener.open(self.url + "/sim/stream", timeout=30) as up:
                    buf = b""
                    while True:
                        chunk = up.read1(65536)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n\n" in buf:
                            ev, buf = buf.split(b"\n\n", 1)
                            self._broadcast(ev + b"\n\n")
                        if self._idle():
                            return
            except OSError:
                pass
            self._broadcast(b": rig unreachable, reconnecting\n\n")
            time.sleep(1.0)


class StatusStream(Fanout):
    """Instrument mode: what the header chips, the operator banner and the bed trace show, for every dashboard.

    While a page watches, polls mhapi-service: /operator/state every second and /operator/status (get_status's
    body) every 2 s. Neither is an API call: they don't reset the heater watchdog (H4), and neither the service's
    calls.jsonl nor the agent's run records them. Each poll goes out as one `status` event, with the cameras'
    health. The A1's camera stays connected meanwhile, so the page can load each new frame from /feeds/a1.jpg."""

    name = "status-poll"

    def __init__(self, rig, feeds):
        super().__init__()
        self.rig, self.feeds = rig, feeds

    def _run(self):
        a1 = self.feeds.get("a1")
        with (a1.watch() if a1 else nullcontext()):
            status = status_error = None
            t_status = 0.0
            while not self._idle():
                t = time.time()
                try:
                    operator, operator_error = self._operator()
                    if t - t_status >= 1.9:
                        t_status = t
                        fresh, status_error = self._status()
                        status = fresh or status   # the last answer stays, marked by the error
                    ev = {"t": round(t, 3), "status": status, "status_error": status_error,
                          "operator": operator, "operator_error": operator_error,
                          "feeds": {name: f.health() for name, f in self.feeds.items()}}
                    self._broadcast(f"event: status\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                except Exception as e:  # noqa: BLE001 - an odd answer must not end the poll
                    self._broadcast(f": poll failed: {type(e).__name__}\n\n".encode())
                time.sleep(max(0.1, t + 1.0 - time.time()))

    def _operator(self):
        return self._get("/operator/state", timeout=3)

    def _status(self):
        """get_status's body, read passively: an agent call would reset H4, so a watched dashboard would keep a
        stalled agent's heater on."""
        return self._get("/operator/status", timeout=5)

    def _get(self, path, timeout):
        status, raw, _ = self.rig.sim("GET", path, timeout=timeout)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return None, f"HTTP {status} from {self.rig.url}{path}"
        if status != 200:
            return None, body.get("message") or body.get("error") or body.get("detail") or f"HTTP {status}"
        return body, None


class Harness:
    def __init__(self, rig, bus, runs_root, client, limits=None, feeds=None):
        self.rig, self.bus, self.runs_root, self.client = rig, bus, Path(runs_root), client
        self.limits = limits or Limits(self.runs_root / "_limits.json")
        self.rig_stream = RigStream(rig.url)
        self.feeds = feeds or {}   # instrument mode: the cameras (harness/feeds.py)
        self.status_stream = None if rig.is_twin else StatusStream(rig, self.feeds)
        self.lock = threading.Lock()
        self.run = None

    def passes(self, method, sub):
        """Whether the page may reach the rig's endpoint `sub` through /rig/: the twin's dashboard endpoints, or
        the service's operator console."""
        if self.rig.is_twin:
            return sub in (RIG_GET if method == "GET" else RIG_POST)
        return sub in (CONSOLE_GET if method == "GET" else CONSOLE_POST)

    def active(self):
        """The run that is working now (not one that finished and awaits messages)."""
        r = self.run
        return r if r and r.thread.is_alive() and r.status in ("starting", "running") else None

    def may_steer(self, token):
        """Public mode: while a run works, only its starter (or the admin) may change the twin."""
        if not self.limits.public or self.limits.is_admin(token):
            return True
        r = self.active()
        return r is None or (bool(token) and token == r.token)

    def owns_run(self, token):
        """Public mode: only the run's starter (or the admin) may stop it or message the agent."""
        if not self.limits.public or self.limits.is_admin(token):
            return True
        return bool(self.run and token and token == self.run.token)

    def start_run(self, prompt, effort="medium", attach_protocol=True, fresh_rig=False, ip="", token=""):
        """(http_status, body). A finished run that awaits messages is stopped first; a working one too, except
        in public mode, where a visitor has to wait for it."""
        lim = self.limits
        admin = lim.is_admin(token)
        if effort not in EFFORTS:
            return 400, {"error": f"effort must be one of {', '.join(EFFORTS)}"}
        if not prompt:
            return 400, {"error": "empty prompt"}
        if lim.public and len(prompt) > lim.prompt_chars:
            return 400, {"error": f"The prompt is too long for the public demo (at most {lim.prompt_chars} characters)."}
        with self.lock:
            cur = self.active()
            if lim.public and not admin and cur and cur.token != token:
                return 409, {"error": "Another visitor's run is in progress. Watch it live, or replay a recorded run "
                                      "from the menu; you can start yours when it ends."}
            if not admin:
                why = lim.check_start(ip)
                if why:
                    return 429, {"error": why}
            if self.run and self.run.thread.is_alive():
                self.run.stop()
                self.run.thread.join(timeout=60)
            if fresh_rig and self.rig.is_twin:
                r = self.rig.reset()
                if not r.get("ok"):
                    return 503, {"error": f"The twin could not be reset: {r.get('message') or r.get('error')}"}
            self.run = AgentRun(self, prompt, effort, attach_protocol).start()
            if not admin:
                lim.note_start(ip)
            return 200, {**self.run.summary(), "token": self.run.token}

    def runs(self):
        out = []
        for d in sorted(self.runs_root.glob("*/run.json"), reverse=True):
            try:
                out.append(json.loads(d.read_text()))
            except ValueError:
                pass
        return out


def make_handler(h):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else (json.dumps(body, default=str) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return {}
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                return b if isinstance(b, dict) else {}
            except ValueError:
                return {}

        def _ip(self):
            # behind the reverse proxy (deploy/), the visitor's address is the first X-Forwarded-For entry
            fwd = self.headers.get("X-Forwarded-For", "")
            return fwd.split(",")[0].strip() if fwd else self.client_address[0]

        def _token(self):
            return self.headers.get("X-Run-Token", "")

        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path in ("/", "/index.html"):
                return self._static(DASH / "index.html")
            if path.startswith("/dashboard/"):
                p = (DASH / path[len("/dashboard/"):]).resolve()
                if DASH.resolve() in p.parents:
                    return self._static(p)
                return self._send(404, {"error": "not found"})
            if path == "/api/config":
                run = h.run.summary() if h.run else None
                cfg = {"rig_url": h.rig.url, "rig_base": "/rig", "twin": h.rig.is_twin, "model": MODEL,
                       "efforts": list(EFFORTS), "scenarios": SCENARIOS, "run": run,
                       "seq": h.bus.seq, "public": h.limits.info()}
                if not h.rig.is_twin:
                    cfg["instrument"] = {"nadir_px": nadir_px(), "pi_frame_px": list(PI_FRAME)}
                return self._send(200, cfg)
            if path == "/api/stream":   # a browser reconnecting by itself sends the last id it saw
                since = self.headers.get("Last-Event-ID") or (qs.get("since") or ["0"])[0]
                return self._stream(int(since) if str(since).isdigit() else 0)
            if path == "/api/runs":
                return self._send(200, h.runs())
            if path.startswith("/api/runs/") and path.endswith("/trace"):
                rid = path[len("/api/runs/"):-len("/trace")]
                f = (h.runs_root / rid / "trace.jsonl")
                if f.is_file() and h.runs_root.resolve() in f.resolve().parents:
                    events = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
                    return self._send(200, events)
                return self._send(404, {"error": "no such run"})
            if path == "/file":
                p = (ROOT / (qs.get("p") or [""])[0]).resolve()
                if any(r.resolve() in p.parents for r in FILE_ROOTS) and p.is_file():
                    return self._static(p)
                return self._send(404, {"error": "not found"})
            if path == "/rig/sim/stream" and h.rig.is_twin:
                return self._relay(h.rig_stream)
            if path == "/rig/status/stream" and h.status_stream:
                return self._relay(h.status_stream)
            if path.startswith("/feeds/"):
                name, _, kind = path[len("/feeds/"):].partition(".")
                feed = h.feeds.get(name)
                if feed and kind == "mjpg":
                    return self._mjpeg(feed)
                if feed and kind == "jpg":
                    return self._jpeg(feed)
                return self._send(404, {"error": "not found"})
            if path.startswith("/rig/") and h.passes("GET", path[4:]):
                status, raw, ctype = h.rig.sim("GET", path[4:])
                return self._send(status, raw, ctype)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            b = self._body()
            lim = h.limits
            if path == "/api/run":
                status, out = h.start_run((b.get("prompt") or "").strip(), b.get("effort", "medium"),
                                          bool(b.get("attach_protocol", True)), bool(b.get("fresh_rig", False)),
                                          ip=self._ip(), token=self._token())
                return self._send(status, out)
            if path == "/api/stop":
                if h.run and not h.owns_run(self._token()):
                    return self._send(403, {"error": "Only the browser that started this run can stop it."})
                if h.run:
                    h.run.stop()
                return self._send(200, {"ok": True})
            if path == "/api/message":
                text = (b.get("text") or "").strip()
                if h.run and not h.owns_run(self._token()):
                    return self._send(403, {"error": "Only the browser that started this run can message the agent."})
                if lim.public and len(text) > lim.message_chars:
                    return self._send(400, {"error": f"Messages are limited to {lim.message_chars} characters here."})
                if lim.public and h.run and h.run.status == "finished" and lim.day_left() <= 0:
                    return self._send(429, {"error": "The public demo's budget for today is used up."})
                if not (h.run and text and h.run.send(text)):
                    return self._send(409, {"error": "no active run, or an empty message"})
                return self._send(200, {"ok": True})
            if path == "/api/operator":   # the person's answer to the agent's call_operator
                if h.run and not h.owns_run(self._token()):
                    return self._send(403, {"error": "Only the browser that started this run can answer the agent."})
                if not (h.run and h.run.answer_operator_call(str(b.get("id") or ""), b.get("answer"), b.get("note"))):
                    return self._send(409, {"error": "That request is no longer open."})
                return self._send(200, {"ok": True})
            if path.startswith("/rig/") and h.passes("POST", path[4:]):
                if not h.may_steer(self._token()):
                    return self._send(403, {"error": "Another visitor's run is in progress; only they can change the twin."})
                if lim.public and path == "/rig/sim/control":
                    b = {k: v for k, v in b.items() if k in PUBLIC_CONTROL}
                    if "fault" in b and b["fault"] not in PUBLIC_FAULTS:
                        return self._send(400, {"error": "fault not available here"})
                    if "speed" in b:
                        b["speed"] = min(20.0, max(1.0, float(b["speed"] or 1)))
                status, raw, ctype = h.rig.sim("POST", path[4:], b)
                return self._send(status, raw, ctype)
            return self._send(404, {"error": "not found"})

        def _static(self, p):
            if not p.is_file():
                return self._send(404, {"error": "not found"})
            return self._send(200, p.read_bytes(), CTYPES.get(p.suffix.lower(), "application/octet-stream"))

        def _sse_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True  # the stream has no length: it ends when the connection closes

        def _stream(self, since):
            self._sse_headers()
            q = h.bus.subscribe()
            try:
                sent = since   # events emitted while replaying arrive on the queue too
                for ev in h.bus.since(since):
                    self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                    sent = ev["seq"]
                self.wfile.flush()
                last = time.time()
                while True:
                    try:
                        ev = q.get(timeout=10)
                        if ev["seq"] <= sent:
                            continue
                        self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                        sent = ev["seq"]
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    if q.empty() or time.time() - last > 0.05:   # batch bursts, never hold the last event back
                        self.wfile.flush()
                        last = time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                h.bus.unsubscribe(q)

        def _relay(self, stream):
            self._sse_headers()
            q = stream.subscribe()
            try:
                while True:
                    try:
                        data = q.get(timeout=10)
                    except queue.Empty:
                        data = b": keep-alive\n\n"
                    self.wfile.write(data)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                stream.unsubscribe(q)

        def _jpeg(self, feed):
            with feed.watch():   # connects first if nobody watches (the A1 takes about 2 s for its first frame)
                got = feed.latest() or feed.wait_newer(0, 12)
            if got is None:
                return self._send(503, {"error": "no_picture", "message": feed.health()["error"] or "no frame yet"})
            return self._send(200, got[2], "image/jpeg")

        def _mjpeg(self, feed):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            with feed.watch():
                seq = 0
                try:
                    self.wfile.write(b"--frame\r\n")
                    while True:
                        got = feed.wait_newer(seq, 5)
                        if got is None:   # no picture: nothing to write, so ask the socket whether the viewer left
                            if self._client_gone():
                                return
                            continue
                        seq, _, jpg = got
                        # each part ends with the next boundary, so browsers show it without waiting for the next
                        self.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                                         + jpg + b"\r\n--frame\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        def _client_gone(self):
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
            except OSError:
                return True

    return Handler


def serve(h, host="127.0.0.1", port=8750):
    httpd = ThreadingHTTPServer((host, port), make_handler(h))
    httpd.daemon_threads = True
    return httpd
