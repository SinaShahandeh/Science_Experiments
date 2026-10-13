"""HTTP front of the twin. Same API as mhapi-service, plus /sim/* for the dashboard.

- POST /v1/<command>              the agent-facing API (Model_Hardware_API_spec.md, "CLI and service")
- GET  /operator                  operator console page (Done / Problem / Stop, Clear while halted)
- GET  /operator/state            POST /operator/answer {id, answer, note}   POST /operator/stop
- GET  /operator/status           get_status's body without counting as an API call (no H4 reset, not logged)
- POST /operator/clear {token}    only the console page holds the token
- GET  /sim/geometry  /sim/state  /sim/truth  /sim/clock     GET /sim/stream (server-sent events)
- POST /sim/clock {thinking, speed}   POST /sim/control {speed, auto_operator, room_light, fault, ..., reset}
- GET  /files/<session>/wells/<png>   images, for the dashboard's thumbnails
"""
import json
import math
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from mhapi.tools import COMMANDS
from . import geometry as geo
from .kinetics import PARAMS, R, t_half_isothermal, truth

OPERATOR_PAGE = (Path(__file__).parent / "operator.html").read_text()


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(type(o).__name__)


def dumps(obj):
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def make_handler(rig):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mhapi-twin/0.1"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -------------------------------------------------- helpers
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status, body, ctype="application/json; charset=utf-8", cors=True):
            data = body if isinstance(body, bytes) else (dumps(body) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cors:
                self._cors()
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                b = json.loads(raw)
            except ValueError:
                return None
            return b if isinstance(b, dict) else None

        # -------------------------------------------------- verbs
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, {"service": "mhapi digital twin", "api": "POST /v1/<command>",
                                        "operator": "/operator", "commands": list(COMMANDS), "t": rig.clock.now()})
            if path == "/operator":
                page = OPERATOR_PAGE.replace("__CLEAR_TOKEN__", rig.clear_token)
                return self._send(200, page.encode(), "text/html; charset=utf-8", cors=False)
            if path == "/operator/state":
                s = rig.snapshot(wells=False)
                st = rig.cmd_get_status()
                return self._send(200, {"request": s["op"], "safety": s["safety"], "bed_c": s["bed"]["reading"],
                                        "target_c": s["bed"]["target"], "homed": st["homed"],
                                        "loaded_wells": [w["label"] for w in st["loaded_wells"]],
                                        "running": s["running"], "auto_operator": rig.auto_operator, "t": s["t"]})
            if path == "/operator/status":  # get_status for watchers: no H4 reset, not in the session's calls
                return self._send(200, rig.cmd_get_status())
            if path == "/sim/geometry":
                return self._send(200, geo.geometry_json())
            if path == "/sim/state":
                return self._send(200, rig.snapshot())
            if path == "/sim/clock":
                return self._send(200, {"t": rig.clock.now(), "speed": rig.clock.speed, "user_speed": rig.user_speed,
                                        "thinking": rig.thinking, "realistic_thinking": rig.realistic_thinking})
            if path == "/sim/truth":
                return self._send(200, sim_truth(rig))
            if path == "/sim/stream":
                return self._stream()
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            return self._send(404, {"error": "not_found", "message": path})

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._body()
            if body is None:
                return self._send(400, {"error": "invalid_argument", "message": "the body must be a JSON object",
                                        "t": round(rig.clock.now(), 3)})
            if path.startswith("/v1/"):
                cmd = path[len("/v1/"):]
                if cmd not in COMMANDS:
                    return self._send(400, {"error": "invalid_argument", "message": f"unknown command {cmd!r}",
                                            "t": round(rig.clock.now(), 3)})
                status, out = rig.submit(cmd, body)
                return self._send(status, out)
            if path == "/operator/answer":
                ok = rig.operator.respond(body.get("id"), body.get("answer", "done"), body.get("note", ""),
                                          who=body.get("who", "operator"))
                return self._send(200 if ok else 409, {"ok": ok})
            if path == "/operator/stop":
                return self._send(200, rig.stop(body.get("who", "operator console")))
            if path == "/operator/clear":
                ok = rig.clear(body.get("token", ""))
                return self._send(200 if ok else 403, {"ok": ok, "safety": rig.safety})
            if path == "/sim/clock":
                if "thinking" in body:
                    rig.set_thinking(body["thinking"])
                if "speed" in body:
                    rig.set_speed(float(body["speed"]))
                return self._send(200, {"t": rig.clock.now(), "speed": rig.clock.speed, "user_speed": rig.user_speed,
                                        "thinking": rig.thinking})
            if path == "/sim/control":
                return self._send(200, rig.control(body))
            return self._send(404, {"error": "not_found", "message": path})

        # -------------------------------------------------- streams and files
        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            self.close_connection = True  # the stream has no length: it ends when the connection closes
            q = queue.Queue(maxsize=500)

            def listener(ev):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

            rig.listeners.append(listener)
            try:
                n = 0
                while True:
                    snap = rig.snapshot(wells=(n % 3 == 0))
                    self.wfile.write(f"event: state\ndata: {dumps(snap)}\n\n".encode())
                    while not q.empty():
                        self.wfile.write(f"event: rig\ndata: {dumps(q.get_nowait())}\n\n".encode())
                    self.wfile.flush()
                    n += 1
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if listener in rig.listeners:
                    rig.listeners.remove(listener)

        def _file(self, rel):
            root = rig.sessions_root.resolve()
            p = (root / unquote(rel)).resolve()
            if root not in p.parents or not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".csv", ".json"):
                return self._send(404, {"error": "not_found", "message": rel})
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".csv": "text/csv", ".json": "application/json"}[p.suffix.lower()]
            return self._send(200, p.read_bytes(), ctype)

    return Handler


def sim_truth(rig):
    """What only the twin knows: the true kinetics and each loaded well's hidden parameters.

    `apparent_ea_kj` is the activation energy an ideal experiment would measure from the isothermal
    half-times of the liquid between 64 and 69 C, where they are 3-30 min (dominated by ovalbumin).
    `bed_to_liquid` gives the liquid temperature the bed setting produces at steady state."""
    temps = [64, 65, 66, 67, 68, 69]
    t50 = [t_half_isothermal(c) for c in temps]
    x = np.array([1 / (c + 273.15) for c in temps])
    y = np.log(1 / np.array(t50))
    slope = np.polyfit(x, y, 1)[0]
    from .kinetics import THERMAL
    from .rig import T_AMB
    bed_to_liquid = {"center": {"eta": THERMAL.eta_center}, "corner": {"eta": THERMAL.eta_corner}, "t_amb_c": T_AMB,
                     "formula": "T_liquid = T_amb + eta * (T_plate - T_amb); the plate settles at the target"}
    wells = {}
    with rig.lock:
        for lab, w in rig.wells.items():
            if w.loaded:
                wells[lab] = {"k_mult": round(w.k_mult, 3), "eta": round(w.eta, 4), "tau_s": round(w.tau, 1),
                              "g_clear": round(w.g_clear, 1), "g_full": round(w.g_full, 1),
                              "temp_c": round(w.chem.temp_c, 2), "w": round(w.w(), 4)}
    return {**truth(), "isothermal_t50_s": dict(zip(temps, [round(v) for v in t50])),
            "apparent_ea_kj": round(-slope * R / 1000, 1), "ea_ova_kj": PARAMS.ea_ova / 1000,
            "ea_ot_kj": PARAMS.ea_ot / 1000, "bed_to_liquid": bed_to_liquid, "wells": wells, "t": rig.clock.now()}


def serve(rig, host="127.0.0.1", port=8743):
    httpd = ThreadingHTTPServer((host, port), make_handler(rig))
    httpd.daemon_threads = True
    return httpd
