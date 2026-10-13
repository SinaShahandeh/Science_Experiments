"""The simulated rig behind the API: state, physics, the one-at-a-time command queue, the operator,
the safety layer and the session folder.

Behaviour follows Model_Hardware_API_spec.md and Hardware_Safety_spec.md: the same commands, fields,
error codes, timings and limits (read from mhapi/safety.toml), so an agent can't tell the twin from
the rig through the API. The twin also exposes things the rig can't: the true plate and liquid
temperatures, each well's true opacity, and fault injection (see server.py, /sim/...).
"""
import csv
import json
import math
import queue
import secrets
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from mhapi.tools import LABELS, tool_definitions
from . import geometry as geo
from .kinetics import THERMAL, EggWhite
from .optics import CellLook, agent_png, liquid_chroma, render_cell, save_png, wall_gray, well_gray
from .physics import Bed, Gantry, SimClock

SAFETY = tomllib.loads((geo.ROOT / "mhapi" / "safety.toml").read_text())
QUEUED = ("prepare", "get_image", "get_raw_opacity", "load_well", "clean_all_wells")
HTTP_STATUS = {
    "invalid_argument": 400, "unknown_label": 400, "temperature_limit": 400,
    "not_homed": 409, "already_loaded": 409, "bed_hot": 409, "halted": 409, "camera_moved": 409,
    "operator_declined": 409, "operator_timeout": 409,
    "move_failed": 500, "homing_failed": 500,
    "printer_unavailable": 503, "camera_unavailable": 503,
}
T_AMB = 23.0
XY_V, Z_V, ACC = SAFETY["motion"]["xy_feed"] / 60.0, SAFETY["motion"]["z_feed"] / 60.0, float(SAFETY["motion"]["accel"])
CAPTURE_Z = float(SAFETY["motion"]["capture_z"])
POSITION_CHECK = ("P1-A1", "P2-A1", "C-B3", "C-C2", "P4-B1", "P3-B2")


class RigError(Exception):
    def __init__(self, code, message, **fields):
        super().__init__(message)
        self.code, self.message, self.fields = code, message, fields

    def body(self, t):
        return {"error": self.code, "message": self.message, **self.fields, "t": round(t, 3)}


def iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


class Session:
    """The session folder: calls.jsonl, temps.csv, events.csv, wells/, state.json, calibration.json."""

    def __init__(self, root, t):
        self.name = "sim_" + datetime.fromtimestamp(t).strftime("%Y-%m-%d_%H%M%S")
        self.dir = Path(root) / self.name
        (self.dir / "wells").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.n_img = 0
        self._temps = open(self.dir / "temps.csv", "a", newline="")
        self._events = open(self.dir / "events.csv", "a", newline="")
        self._calls = open(self.dir / "calls.jsonl", "a")
        if self._temps.tell() == 0:
            csv.writer(self._temps).writerow(["t", "t_iso", "bed_c", "target_c"])
        if self._events.tell() == 0:
            csv.writer(self._events).writerow(["t", "t_iso", "event", "detail"])
        (self.dir / "calibration.json").write_text(json.dumps({
            "calibration_id": calibration_id(), "source": "digital twin (twin/geometry.py)",
            "wells": geo.well_table(), "nadir_px": geo.NADIR_PX, "camera_offset_mm": geo.CAMERA_OFFSET}, indent=1))

    def temp(self, t, bed, target):
        with self._lock:
            if not self._temps.closed:
                csv.writer(self._temps).writerow([f"{t:.3f}", iso(t), f"{bed:.5f}", f"{target:g}"])
                self._temps.flush()

    def event(self, t, name, detail=""):
        with self._lock:
            if not self._events.closed:
                csv.writer(self._events).writerow([f"{t:.3f}", iso(t), name, detail])
                self._events.flush()

    def call(self, rec):
        with self._lock:
            if not self._calls.closed:
                self._calls.write(json.dumps(rec, default=str) + "\n")
                self._calls.flush()

    def next_image(self, label):
        with self._lock:
            self.n_img += 1
            return self.dir / "wells" / f"{self.n_img:05d}_{label}.png", self.dir / "wells" / f"{self.n_img:05d}_{label}_crop.png"

    def save_state(self, state):
        (self.dir / "state.json").write_text(json.dumps(state, indent=1))

    def close(self):
        with self._lock:
            for f in (self._temps, self._events, self._calls):
                f.close()


def calibration_id():
    p = geo.CAL / "well_positions_fit.json"
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M") + "-twin"


@dataclass
class Well:
    label: str
    look: CellLook
    rng: np.random.Generator
    chem: EggWhite = None
    t_loaded: float = None
    fill: float = 0.0            # 0..1, for the 3D view's fill animation
    bubble: tuple = None
    g_clear: float = 22.0
    g_full: float = 200.0
    k_mult: float = 1.0
    eta: float = THERMAL.eta_center
    tau: float = THERMAL.tau_s
    island_shift_mm: tuple = (0.0, 0.0)

    @property
    def loaded(self):
        return self.chem is not None

    def load(self, t):
        r = self.rng
        self.k_mult = float(np.exp(r.normal(0, 0.10)))          # egg-to-egg variation (trial: 0.92-1.15)
        corner = geo.is_corner_island(self.label)
        self.eta = (THERMAL.eta_corner if corner else THERMAL.eta_center) + r.normal(0, 0.002)
        self.tau = THERMAL.tau_s * float(np.exp(r.normal(0, 0.08)))   # fill volume (trial: 0.90-1.11)
        self.g_clear = 22.0 + r.normal(0, 1.2)
        self.g_full = 200.0 * (1 + r.normal(0, 0.035))
        self.chem = EggWhite(k_mult=self.k_mult, eta=self.eta, tau_s=self.tau, temp_c=THERMAL.t_load_c)
        self.t_loaded = t
        self.bubble = None
        if r.random() < 0.06:  # a small bubble from loading, as in the protocol's warning
            ang, rad = r.uniform(0, 2 * math.pi), r.uniform(0.5, 3.8)
            self.bubble = (rad * math.cos(ang), rad * math.sin(ang), r.uniform(0.25, 0.6))

    def clean(self):
        self.chem, self.t_loaded, self.bubble = None, None, None

    def w(self):
        return self.chem.opacity() if self.chem else 0.0

    def gray_level(self):
        return self.g_clear + (self.g_full - self.g_clear) * self.w() if self.chem else 19.0


class Operator:
    """The person who fills and cleans wells. In the twin a simulated person can answer instead."""

    def __init__(self):
        self.cv = threading.Condition()
        self.pending = None
        self.answer = None
        self.n = 0
        self.history = []

    def request(self, kind, text, t, **fields):
        with self.cv:
            self.n += 1
            self.pending = {"id": self.n, "kind": kind, "text": text, "t_requested": round(t, 3), **fields}
            self.answer = None
            self.cv.notify_all()
            return dict(self.pending)

    def respond(self, rid, answer, note="", who="operator"):
        with self.cv:
            if not self.pending or self.pending["id"] != rid or self.answer is not None:
                return False
            self.answer = {"answer": answer, "note": note or "", "who": who}
            self.cv.notify_all()
            return True

    def cancel(self, reason):
        with self.cv:
            if self.pending and self.answer is None:
                self.answer = {"answer": "cancelled", "note": reason, "who": "service"}
                self.cv.notify_all()

    def close(self, t):
        with self.cv:
            if self.pending:
                self.history.append({**self.pending, **(self.answer or {}), "t_answered": round(t, 3)})
            self.pending, self.answer = None, None


@dataclass
class Op:
    command: str
    args: dict
    t_start: float = None
    t_end: float = None
    state: str = "queued"
    result: dict = None
    status: int = 200
    done: threading.Event = field(default_factory=threading.Event)


class Rig:
    def __init__(self, sessions_root, speed=1.0, auto_operator=True, seed=None, realistic_thinking=True):
        self.clock = SimClock(speed)
        self.user_speed = float(speed)
        self.thinking = False
        self.realistic_thinking = realistic_thinking
        self.rng = np.random.default_rng(seed)
        self.lock = threading.RLock()
        self.sessions_root = Path(sessions_root)
        t = self.clock.now()
        self.t_start = t
        self.gantry = Gantry(self.clock, start=(128.0, 128.0, 62.0))
        self.homed = False                 # a printer that was just powered on
        self.z_known = False
        self.homing = False
        self.bed = Bed(T_AMB)
        self.wells = {lab: Well(lab, CellLook(lab), np.random.default_rng(self.rng.integers(1 << 31))) for lab in LABELS}
        self.operator = Operator()
        self.auto_operator = auto_operator
        self.session = Session(self.sessions_root, t)
        self.safety, self.safety_reason, self.t_safety = "ok", None, None
        self.clear_token = secrets.token_urlsafe(16)
        self.halt_event = threading.Event()
        self.last_call_t = t
        self.last_operation = None
        self.position_check = None
        self.position_warning = False
        self.home_loss_after = self._draw_home_loss()
        self.tilt_mm = np.array([0.0, 0.04])
        self.tilt_prev = self.tilt_mm.copy()
        self.camera_bump_mm = np.array([0.0, 0.0])
        self.camera_fault = False
        self.room_light = False
        self.led_drain_per_h = 0.008
        self.printer_offline_until = 0.0
        self.last_capture = None
        self.reading, self.report_t = self.bed.ts, t
        self.next_report = t + 1.0
        self.overshoot_since = None
        self.reached_target = False
        self.target_dir = "at"
        self.stuck_window = []
        self.heater_off_t = -1e18
        self.t_phys = t
        self.queue = queue.Queue()
        self.listeners = []           # callables(event dict) for the dashboard
        self._stop = threading.Event()
        self.session.event(t, "service_start", "digital twin")
        for fn in (self._physics_loop, self._executor_loop, self._auto_operator_loop):
            threading.Thread(target=fn, daemon=True).start()

    # ---------------------------------------------------------------- clock control (harness hooks)
    def _apply_speed(self):
        eff = 1.0 if (self.thinking and self.realistic_thinking) else self.user_speed
        if abs(eff - self.clock.speed) > 1e-9:
            self.clock.set_speed(eff)

    def set_speed(self, speed):
        self.user_speed = float(min(60.0, max(0.1, speed)))
        self._apply_speed()

    def set_thinking(self, thinking):
        self.thinking = bool(thinking)
        self._apply_speed()

    # ---------------------------------------------------------------- physics
    def _draw_home_loss(self):
        return float(self.rng.uniform(44, 62) * 60)

    def _physics_loop(self):
        while not self._stop.is_set():
            time.sleep(0.05)
            t = self.clock.now()
            try:
                with self.lock:
                    while self.t_phys < t:
                        dt = min(0.1, t - self.t_phys)
                        self._step(self.t_phys + dt, dt)
                        self.t_phys += dt
            except Exception as e:  # never let the simulation stop; skip ahead and report
                self.t_phys = t
                print(f"twin physics step failed: {type(e).__name__}: {e}", flush=True)

    def _step(self, t, dt):
        bed = self.bed
        bed.step(dt)
        for w in self.wells.values():
            if w.chem is not None:
                w.chem.step(bed.tp, T_AMB, dt)
                w.fill = min(1.0, w.fill + dt / 1.5)
            elif w.fill > 0:
                w.fill = max(0.0, w.fill - dt / 1.0)
        offline = t < self.printer_offline_until
        if t >= self.next_report and not offline:
            self.reading = round(bed.reported() * 32) / 32.0
            self.report_t = t
            self.next_report = t + float(self.rng.choice([1.0, 2.0, 2.0, 2.0, 3.0, 4.0])) + float(self.rng.normal(0, 0.03))
            self.session.temp(t, self.reading, bed.target)
            self._check_heat(t)
        # home lost 44-62 min after the last move (motors switching off)
        if self.homed and not self.gantry.moving(t) and t - self.gantry.last_move_end > self.home_loss_after:
            self.homed = False
            self.z_known = False
            self.session.event(t, "home_lost", f"idle {t - self.gantry.last_move_end:.0f} s")
            self._emit({"kind": "home_lost", "t": t})
        # H4 heater watchdog
        wd = SAFETY["heat"]["heater_watchdog_s"]
        if bed.target > 0 and t - self.last_call_t > wd and self.safety == "ok":
            bed.set_target(0)
            self._set_safety("heater_timeout", f"no API call for {wd / 60:.0f} min", t)
        # H6 lost contact while heating
        if offline and bed.target > 0 and t - self.report_t > SAFETY["heat"]["lost_reports_s"]:
            self._halt("lost_printer_contact", t)
        # slow drift of the camera on its glued mount (stays well under the warning level)
        self.tilt_mm = self.tilt_mm + self.rng.normal(0, 0.0004, 2) * math.sqrt(dt)
        self.tilt_mm = np.clip(self.tilt_mm, -0.12, 0.16)

    def _check_heat(self, t):
        h = SAFETY["heat"]
        r, target = self.reading, self.bed.target
        if r > h["hard_max_c"]:
            return self._halt("over_temperature", t, f"bed read {r:.1f} C")
        if target > 0:
            # a bed heating up has reached its target once it gets within 0.5 C from below; a bed cooling down to a
            # lower target only once it gets there from above (safety spec H2)
            if (self.target_dir != "down" and r >= target - 0.5) or (self.target_dir == "down" and r <= target + 0.5):
                self.reached_target = True
            if self.reached_target and r > target + h["overshoot_margin_c"]:
                self.overshoot_since = self.overshoot_since or t
                if t - self.overshoot_since > h["overshoot_time_s"]:
                    return self._halt("over_temperature", t, f"bed {r:.1f} C, target {target:g} C")
            else:
                self.overshoot_since = None
        elif t - self.heater_off_t > SAFETY["heat"].get("stuck_grace_s", 30.0):
            # Grace period: reports trail the bed by ~6 s and the heated bed keeps heating the plate for a few
            # seconds, so right after switching off mid-ramp the reading still climbs several degrees.
            self.stuck_window = [(tt, rr) for tt, rr in self.stuck_window if t - tt <= h["stuck_window_s"]] + [(t, r)]
            if r - min(rr for _, rr in self.stuck_window) > h["stuck_rise_c"]:
                return self._halt("heater_stuck_on", t, f"rose {r - min(rr for _, rr in self.stuck_window):.1f} C with the heater off")

    def _set_safety(self, state, reason, t):
        self.safety, self.safety_reason, self.t_safety = state, reason, t
        self.session.event(t, f"safety_{state}", reason or "")
        self._emit({"kind": "safety", "t": t, "safety": state, "reason": reason})

    def _halt(self, reason, t, detail=""):
        with self.lock:
            if self.safety == "halted":
                return
            self.bed.set_target(0)
            self.bed.stuck_on = False
            self._set_safety("halted", reason + (f": {detail}" if detail else ""), t)
            self.halt_event.set()
        self.operator.cancel("halted")

    def _halted_error(self):
        """As mhapi-service words it (safety.describe_halt): what happened, since when, what a person has to do."""
        code, _, detail = (self.safety_reason or "").partition(": ")
        when = time.strftime("%H:%M:%S", time.localtime(self.t_safety)) if self.t_safety else "an unknown time"
        return RigError("halted", f"the safety stop has been active since {when} ({self.safety_reason}). The heater "
                                  "is off and nothing moves; only reads work until a person checks the instrument "
                                  "and presses Clear on the operator console. After that, prepare re-homes.",
                        safety_reason=code or None, safety_detail=detail or None,
                        t_safety=None if self.t_safety is None else round(self.t_safety, 3))

    def stop(self, who="operator"):
        t = self.clock.now()
        self._halt("operator_stop", t, who)
        return {"safety": "halted", "safety_reason": self.safety_reason, "t": round(t, 3)}

    def clear(self, token):
        if token != self.clear_token:
            return False
        t = self.clock.now()
        with self.lock:
            if self.safety == "ok":
                return True
            self.halt_event.clear()
            self.camera_fault = False
            self.homed = False      # the position counts as unknown after a halt
            self.z_known = False
            self._set_safety("ok", "cleared on the console", t)
        return True

    # ---------------------------------------------------------------- queue
    def submit(self, command, args):
        """Run a command; queued commands wait their turn. Returns (http_status, body)."""
        t = self.clock.now()
        self.last_call_t = t
        if command in QUEUED:
            op = Op(command, dict(args))
            self.queue.put(op)
            op.done.wait()
            status, body = op.status, op.result
        else:
            status, body = self._run(command, args)
        self.session.call({"t_start": round(t, 3), "t_end": round(self.clock.now(), 3), "command": command,
                           "args": args, "response": body})
        return status, body

    def _executor_loop(self):
        while not self._stop.is_set():
            op = self.queue.get()
            op.t_start, op.state = self.clock.now(), "running"
            self.last_operation = op
            self._emit({"kind": "op", "t": op.t_start, "command": op.command, "args": op.args, "state": "running"})
            if self.safety == "halted":
                op.status, op.result = 409, self._halted_error().body(op.t_start)
            else:
                op.status, op.result = self._run(op.command, op.args)
            op.t_end = self.clock.now()
            op.state = "failed" if "error" in op.result else "done"
            op.done.set()
            self._emit({"kind": "op", "t": op.t_end, "command": op.command, "args": op.args, "state": op.state,
                        "result": op.result})

    def _run(self, command, args):
        try:
            fn = getattr(self, "cmd_" + command, None)
            if fn is None:
                raise RigError("invalid_argument", f"unknown command {command!r}")
            return 200, fn(**self._args(command, args))
        except RigError as e:
            return HTTP_STATUS.get(e.code, 400), e.body(self.clock.now())

    def _args(self, command, args):
        args = dict(args or {})
        inline = bool(args.pop("inline_image", False))
        if command in ("get_image", "get_raw_opacity"):
            label = args.get("label")
            if not isinstance(label, str) or not label.strip():
                raise RigError("invalid_argument", f"{command} needs a well label, for example P3-A1")
            return {"label": label.strip(), "inline": inline}
        if command == "load_well":
            label = args.get("label")
            if not isinstance(label, str) or not label.strip():
                raise RigError("invalid_argument", "load_well needs a well label, for example P3-A1")
            return {"label": label.strip()}
        if command == "set_temperature":
            if "temperature" not in args:
                raise RigError("invalid_argument", "set_temperature needs a temperature in °C")
            return {"temperature": args["temperature"]}
        return {}

    # ---------------------------------------------------------------- shared pieces
    def _bed_fields(self, t):
        return {"bed_c": self.reading, "target_c": self.bed.target, "bed_age_s": round(t - self.report_t, 1)}

    def _check_label(self, label):
        if label not in self.wells:
            raise RigError("unknown_label", f"{label!r} is not a well label; call list_wells for the 32 labels.")

    def _check_camera(self):
        if self.camera_fault:
            raise RigError("camera_moved", "The camera has shifted on its mount; motion is stopped until a "
                                           "person checks the mount and recalibrates.")

    def _check_printer(self, t):
        if t < self.printer_offline_until:
            raise RigError("printer_unavailable", "The printer isn't answering. Try again shortly.")

    def _shift_mm(self):
        return self.tilt_mm + self.camera_bump_mm

    def _move(self, x, y, z=CAPTURE_Z):
        """Absolute move with the service's rules: lift first, lower last; then wait until the image is still."""
        g = self.gantry
        cx, cy, cz = g.position()
        if z > cz + 1e-6:
            g.run((cx, cy, z), Z_V, ACC, kind="z")
            cz = z
        if math.hypot(x - cx, y - cy) > 1e-6:
            g.run((x, y, cz), XY_V, ACC)
        if z < cz - 1e-6:
            g.run((x, y, z), Z_V, ACC, kind="z")
        self.clock.sleep(0.25)  # 4 still frames at ~18 fps after the vibration dies down

    def _ensure_homed(self, t):
        """Before a move: re-home if the home was lost and no well is loaded; else fail with not_homed."""
        if self.homed and self.z_known:
            return False
        if any(w.loaded for w in self.wells.values()):
            raise RigError("not_homed", "The printer lost its home position while wells are loaded; nothing "
                                        "moved. Call prepare to re-home (liquid may slosh), or stop.",
                           loaded_wells=[w.label for w in self.wells.values() if w.loaded])
        self._home()
        return True

    def _home(self):
        """The safe homing sequence (Hardware_Safety_spec.md M4), about a minute."""
        g, t = self.gantry, self.clock.now()
        self.homing = True
        self.session.event(t, "homing_start")
        self._emit({"kind": "homing", "t": t, "state": "start"})
        try:
            x, y, z = g.position()
            if not self.z_known or z < SAFETY["motion"]["min_z_before_homing"]:
                z = min(250.0, z + SAFETY["motion"]["homing_lift_mm"])
                g.run((x, y, z), Z_V, ACC, kind="z")
            self.clock.sleep(2.0)                                   # firmware prepares sensorless homing
            g.run((0.0, y, z), 120.0, 3000.0, kind="home")          # G28 X: X, then Y, at firmware speed
            g.run((4.0, y, z), 20.0, 1000.0, kind="home")
            g.run((0.0, y, z), 8.0, 1000.0, kind="home")
            g.run((0.0, 0.0, z), 120.0, 3000.0, kind="home")
            g.run((0.0, 4.0, z), 20.0, 1000.0, kind="home")
            g.run((0.0, 0.0, z), 8.0, 1000.0, kind="home")
            self._slosh()
            self.clock.sleep(3.0)                                   # image moved, then still for 3 s
            z += SAFETY["motion"]["post_xy_home_lift_mm"]
            g.run((0.0, 0.0, z), Z_V, ACC, kind="z")
            px, py = SAFETY["motion"]["park_xy"]
            g.run((px, py, z), XY_V, ACC)
            self.clock.sleep(1.5)
            g.run((px, py, 0.0), 10.0, 500.0, kind="home")          # G28 Z P0 T140 on bare steel
            g.run((px, py, 2.0), 5.0, 500.0, kind="home")
            g.run((px, py, 0.0), 1.5, 200.0, kind="home")
            self.clock.sleep(3.0)
            g.run((px, py, CAPTURE_Z), Z_V, ACC, kind="z")         # lift to Z50, confirmed before any XY move
            self.clock.sleep(0.8 + 0.25)
        finally:
            self.homing = False
        self.homed, self.z_known = True, True
        self.home_loss_after = self._draw_home_loss()
        t = self.clock.now()
        self.session.event(t, "homing_done")
        self._emit({"kind": "homing", "t": t, "state": "done"})

    def _slosh(self):
        for w in self.wells.values():
            if w.loaded and w.bubble is None and w.rng.random() < 0.2:
                ang, rad = w.rng.uniform(0, 2 * math.pi), w.rng.uniform(0.5, 3.5)
                w.bubble = (rad * math.cos(ang), rad * math.sin(ang), w.rng.uniform(0.3, 0.8))
                self.session.event(self.clock.now(), "slosh_bubble", w.label)

    def _position_check(self):
        """Visit one well per corner island and two in the center; measure how far each opening is off."""
        worst = 0.0
        for lab in POSITION_CHECK:
            x, y = geo.well_table()[lab]
            self._move(x, y)
            self.clock.sleep(0.3)
            err = np.hypot(*(np.array(self.wells[lab].island_shift_mm) + self.rng.normal(0, 0.05, 2) + self._shift_mm() * 0.2))
            worst = max(worst, float(err))
        t = self.clock.now()
        self.position_check = {"t": round(t, 3), "max_mm": round(worst, 2)}
        self.position_warning = worst > SAFETY["motion"]["position_check_max_mm"] or float(np.hypot(*self._shift_mm())) > SAFETY["camera"]["warn_mm"]
        self.session.event(t, "position_check", json.dumps(self.position_check))

    def _new_session(self, t):
        new = Session(self.sessions_root, t)
        with self.lock:  # the physics thread writes temperatures to the current session
            old, self.session = self.session, new
        old.close()
        new.event(t, "session_start")

    def _capture(self, label, rehomed, inline, measure):
        t0 = self.clock.now()
        w = self.wells[label]
        if self.camera_fault:
            raise RigError("camera_moved", "The camera has shifted on its mount; motion is stopped.")
        shift = self._shift_mm()
        jump = float(np.hypot(*(shift - self.tilt_prev)))
        self.tilt_prev = shift.copy()
        if np.hypot(*shift) > SAFETY["camera"]["stop_mm"] or jump > SAFETY["camera"]["jump_mm"]:
            self.camera_fault = True
            self._set_safety("halted", "camera_moved", t0)
            self.halt_event.set()
            raise RigError("camera_moved", f"The camera shifted {np.hypot(*shift):.2f} mm on its mount "
                                           f"(jump {jump:.2f} mm since the last capture); motion stopped.")
        self.clock.sleep(1.0 / 18.0 + 0.08)  # the next fresh frame, plus processing
        t = self.clock.now()
        with self.lock:
            hours = (t - self.t_start) / 3600.0
            light = max(0.6, 1.0 - self.led_drain_per_h * hours)
            if self.room_light:
                light *= 1.06 + 0.02 * math.sin(2 * math.pi * t / 2.5)
            err_px = (self.rng.normal(0, 0.06, 2) + np.array(w.island_shift_mm)) / geo.MM_PER_PX
            rgb = render_cell(w.look, loaded=w.loaded, w=w.w(), gray_level=w.gray_level(), light=light,
                              offset_px=(float(err_px[0]), -float(err_px[1])), bubble=w.bubble, rng=self.rng)
            fields = self._bed_fields(t)
        png, crop = self.session.next_image(label)
        save_png(png, agent_png(rgb))
        save_png(crop, rgb)
        self.last_capture = {"label": label, "t": round(t, 3), "image": str(png)}
        body = {"label": label, "t": round(t, 3)}
        if measure:
            g = well_gray(rgb)
            body.update(gray=g["gray"], pixels_total=g["pixels_total"], pixels_used=g["pixels_used"],
                        saturated_px=g["saturated_px"], reflection_px=g["reflection_px"],
                        all_saturated=g["all_saturated"], wall_gray=wall_gray(rgb, (float(err_px[0]), -float(err_px[1]))))
            body["image"] = str(png)
        else:
            body.update(image=str(png), crop_mm=[14, 14], size_px=[272, 272], mm_per_px=0.051)
        body.update(fields)
        body.update(camera_tilt_mm=[round(float(v), 2) for v in shift],
                    position_warning=bool(self.position_warning or np.hypot(*shift) > SAFETY["camera"]["warn_mm"]),
                    rehomed=rehomed)
        if inline:
            import base64
            body["image_base64"] = base64.b64encode(png.read_bytes()).decode()
        self._emit({"kind": "capture", "t": t, "label": label, "image": str(png), "measure": measure,
                    "gray": body.get("gray")})
        return body

    def _read_well(self, label, inline, measure):
        self._check_label(label)
        t = self.clock.now()
        self._check_printer(t)
        self._check_camera()
        rehomed = self._ensure_homed(t)
        x, y = geo.well_table()[label]
        if self.gantry.position()[:2] != (x, y) or abs(self.gantry.position()[2] - CAPTURE_Z) > 1e-6:
            self._move(x, y)
        return self._capture(label, rehomed, inline, measure)

    def _status(self):
        t = self.clock.now()
        shift = self._shift_mm()
        printer_ok = t >= self.printer_offline_until and t - self.report_t <= SAFETY["printer"]["report_max_age_s"]
        camera_ok = True
        pos_warn = bool(self.position_warning or np.hypot(*shift) > SAFETY["camera"]["warn_mm"])
        ready = self.homed and self.z_known and printer_ok and camera_ok and not pos_warn and self.safety == "ok"
        lo = self.last_operation
        body = {
            "t": round(t, 3), "ready": ready, "homed": bool(self.homed and self.z_known),
            "printer_ok": printer_ok, "camera_ok": camera_ok,
            "camera_tilt_mm": [round(float(v), 2) for v in shift], "position_warning": pos_warn,
            "position_check": self.position_check, "safety": self.safety, "safety_reason": self.safety_reason,
            "t_safety": self.t_safety, "idle_s": round(t - self.gantry.last_move_end) if not self.gantry.moving(t) else 0,
            "loaded_wells": [{"label": w.label, "t_loaded": round(w.t_loaded, 3)} for w in self.wells.values() if w.loaded],
            **self._bed_fields(t), "session": self.session.name, "calibration_id": calibration_id(),
            "last_operation": None if lo is None else {
                "command": lo.command, "args": lo.args, "state": lo.state,
                "t_start": None if lo.t_start is None else round(lo.t_start, 3),
                "t_end": None if lo.t_end is None else round(lo.t_end, 3),
                "result": lo.result if lo.state in ("done", "failed") else None},
        }
        if not ready:
            reasons = []
            if not body["homed"]:
                reasons.append("not_homed")
            if not printer_ok:
                reasons.append("printer_unavailable")
            if pos_warn:
                reasons.append("position_warning")
            if self.safety != "ok":
                reasons.append(f"safety_{self.safety}")
            body["not_ready"] = reasons
        return body

    def _wait_operator(self, req):
        t_req = req["t_requested"]
        self._emit({"kind": "operator_request", "t": t_req, "request": req})
        timeout = SAFETY["people"]["operator_timeout_s"]
        while True:
            with self.operator.cv:
                ans = self.operator.answer
            if ans is not None:
                break
            if self.clock.now() - t_req > timeout:
                self.operator.cancel("timeout")
                ans = {"answer": "timeout", "note": ""}
                break
            time.sleep(0.05)
        t = self.clock.now()
        self.session.event(t, f"operator_{ans['answer']}", f"{req['kind']} {req.get('label', '')} {ans.get('note', '')}".strip())
        self._emit({"kind": "operator_answer", "t": t, "request": req, "answer": ans})
        self.operator.close(t)
        if ans["answer"] == "cancelled" or self.safety == "halted":
            raise RigError("halted", "The safety stop was pressed; the request was cancelled.")
        if ans["answer"] == "timeout":
            raise RigError("operator_timeout", f"No answer from the operator within {timeout / 60:.0f} min.")
        if ans["answer"] == "problem":
            raise RigError("operator_declined", "The operator pressed Problem.", note=ans.get("note", ""))
        return t

    # ---------------------------------------------------------------- the commands
    def cmd_get_status(self):
        return self._status()

    def cmd_list_wells(self):
        return {"labels": list(LABELS), "t": round(self.clock.now(), 3)}

    def cmd_describe(self):
        return {"tools": tool_definitions(), "t": round(self.clock.now(), 3)}

    def cmd_stop(self):
        return self.stop("mhapi stop")

    def cmd_get_temperature(self):
        t = self.clock.now()
        return {"bed_c": self.reading, "target_c": self.bed.target, "t": round(t, 3), "age_s": round(t - self.report_t, 1)}

    def cmd_set_temperature(self, temperature):
        t = self.clock.now()
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature):
            raise RigError("invalid_argument", f"the temperature must be a number of °C, not {temperature!r}")
        if temperature < 0:
            raise RigError("invalid_argument", "the temperature can't be negative; 0 turns the heater off")
        if temperature > SAFETY["heat"]["max_target_c"]:
            raise RigError("temperature_limit", f"targets above {SAFETY['heat']['max_target_c']:g} °C are refused",
                           target_c=self.bed.target)
        if self.safety == "halted":
            raise self._halted_error()
        self._check_printer(t)
        with self.lock:
            self.bed.set_target(float(temperature))
            self.target_dir = "up" if temperature > self.reading + 0.5 else "down" if temperature < self.reading - 0.5 else "at"
            self.reached_target = temperature > 0 and self.target_dir == "at"
            self.overshoot_since = None
            if temperature <= 0:
                self.heater_off_t, self.stuck_window = t, []
            if self.safety == "heater_timeout":
                self._set_safety("ok", "heating resumed", t)
        self.session.event(t, "set_temperature", f"{temperature:g}")
        self._emit({"kind": "set_temperature", "t": t, "target": float(temperature)})
        return {"target_c": float(temperature), "t": round(t, 3)}

    def cmd_prepare(self):
        t = self.clock.now()
        self._check_printer(t)
        self._check_camera()
        loaded = any(w.loaded for w in self.wells.values())
        if not (self.homed and self.z_known):
            self._home()
        else:
            x, y, z = self.gantry.position()
            if abs(z - CAPTURE_Z) > 1e-6:
                self._move(x, y, CAPTURE_Z)
        if not loaded:
            self._new_session(self.clock.now())
            self._position_check()
        return self._status()

    def cmd_get_image(self, label, inline=False):
        return self._read_well(label, inline, measure=False)

    def cmd_get_raw_opacity(self, label, inline=False):
        return self._read_well(label, inline, measure=True)

    def cmd_load_well(self, label):
        self._check_label(label)
        t = self.clock.now()
        if not (self.homed and self.z_known):
            raise RigError("not_homed", "The instrument isn't homed. Call prepare before loading any well.")
        w = self.wells[label]
        if w.loaded:
            raise RigError("already_loaded", f"{label} is already loaded.")
        if self.reading > SAFETY["people"]["max_bed_load_c"]:
            raise RigError("bed_hot", f"The bed reads {self.reading:.1f} °C; loading needs it at or below "
                                      f"{SAFETY['people']['max_bed_load_c']:g} °C.", bed_c=self.reading)
        req = self.operator.request("load_well", f"Load egg white into well {label}. Bed {self.reading:.0f} °C.", t,
                                    label=label, bed_c=self.reading)
        t_loaded = self._wait_operator(req)
        with self.lock:
            w.load(t_loaded)
        self.session.save_state(self._state_file())
        return {"label": label, "t_requested": req["t_requested"], "t_loaded": round(t_loaded, 3),
                "bed_c": self.reading, "target_c": self.bed.target}

    def cmd_clean_all_wells(self):
        t = self.clock.now()
        if self.reading > SAFETY["people"]["max_bed_clean_c"]:
            raise RigError("bed_hot", f"The bed reads {self.reading:.1f} °C; cleaning needs it at or below "
                                      f"{SAFETY['people']['max_bed_clean_c']:g} °C. Set the temperature to 0 and wait.",
                           bed_c=self.reading)
        req = self.operator.request("clean_all_wells", "Clean all wells in place. Do not lift the plate or the "
                                                       f"grid. Bed {self.reading:.0f} °C.", t, bed_c=self.reading)
        t_done = self._wait_operator(req)
        with self.lock:
            for w in self.wells.values():
                w.clean()
        self.session.save_state(self._state_file())
        if self.homed and self.z_known:
            self._position_check()
        return {"t_requested": req["t_requested"], "t_done": round(t_done, 3),
                "position_warning": bool(self.position_warning), "position_check": self.position_check}

    def _state_file(self):
        return {"loaded_wells": [{"label": w.label, "t_loaded": w.t_loaded} for w in self.wells.values() if w.loaded]}

    # ---------------------------------------------------------------- the simulated person
    def _auto_operator_loop(self):
        seen = None
        due = None
        while not self._stop.is_set():
            time.sleep(0.05)
            with self.operator.cv:
                req = self.operator.pending if self.operator.answer is None else None
            if req is None:
                seen, due = None, None
                continue
            if not self.auto_operator:
                due = None
                continue
            if seen != req["id"] or due is None:
                seen = req["id"]
                if req["kind"] == "load_well":
                    recent = [h for h in self.operator.history[-1:] if h.get("kind") == "load_well"
                              and req["t_requested"] - h.get("t_answered", 0) < 120]
                    delay = float(self.rng.lognormal(math.log(12 if recent else 28), 0.3))
                else:
                    delay = float(self.rng.lognormal(math.log(150), 0.2))
                due = req["t_requested"] + delay
            if self.clock.now() >= due:
                self.operator.respond(req["id"], "done", "", who="simulated operator")

    # ---------------------------------------------------------------- dashboard support
    def _emit(self, ev):
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass

    def snapshot(self, wells=True):
        t = self.clock.now()
        seg = self.gantry.segment()
        with self.lock:
            snap = {
                "t": round(t, 3), "speed": self.clock.speed, "user_speed": self.user_speed, "thinking": self.thinking,
                "pos": [round(v, 3) for v in self.gantry.position(t)], "seg": seg.to_json() if seg else None,
                "homed": bool(self.homed and self.z_known), "homing": self.homing,
                "bed": {"plate_c": round(self.bed.tp, 2), "reading": self.reading, "target": self.bed.target,
                        "u": round(self.bed.u, 3), "report_t": round(self.report_t, 3)},
                "op": dict(self.operator.pending) if self.operator.pending and self.operator.answer is None else None,
                "safety": {"state": self.safety, "reason": self.safety_reason, "t": self.t_safety},
                "auto_operator": self.auto_operator, "room_light": self.room_light,
                "led": round(max(0.6, 1.0 - self.led_drain_per_h * (t - self.t_start) / 3600.0), 4),
                "tilt_mm": [round(float(v), 3) for v in self._shift_mm()], "session": self.session.name,
                "last_capture": self.last_capture, "running": self.last_operation.command
                if self.last_operation and self.last_operation.state == "running" else None,
            }
            if wells:
                snap["wells"] = {lab: self._well_view(w) for lab, w in self.wells.items() if w.loaded or w.fill > 0}
        return snap

    def _well_view(self, w):
        wv = w.w()
        rgb = liquid_chroma(wv) * w.gray_level() if w.chem else np.array([30.0, 8.0, 35.0])
        return {"fill": round(w.fill, 3), "w": round(wv, 4), "rgb": [int(min(255, max(0, v))) for v in rgb],
                "temp_c": round(w.chem.temp_c, 2) if w.chem else None, "gray": round(w.gray_level(), 1),
                "t_loaded": w.t_loaded, "bubble": w.bubble is not None}

    def busy(self):
        return not self.queue.empty() or bool(self.last_operation and self.last_operation.state == "running")

    def reset(self):
        """Sim only: a fresh rig for the next run on a shared twin. Empty wells, a cold bed, no faults, the
        printer not homed (as after switching it on), and a new session folder. The clock keeps running."""
        self.operator.cancel("reset")
        end = time.time() + 15
        while self.busy() and time.time() < end:
            time.sleep(0.05)
        if self.busy():
            return {"ok": False, "error": "busy", "message": "a command is still running"}
        t = self.clock.now()
        with self.lock:
            self.bed = Bed(T_AMB)
            self.reading, self.report_t, self.next_report = self.bed.ts, t, t + 1.0
            for w in self.wells.values():
                w.clean()
                w.fill, w.island_shift_mm = 0.0, (0.0, 0.0)
            self.homed = self.z_known = self.homing = False
            self.safety, self.safety_reason, self.t_safety = "ok", None, None
            self.halt_event.clear()
            self.camera_bump_mm, self.camera_fault = np.array([0.0, 0.0]), False
            self.tilt_mm = np.array([0.0, 0.04])
            self.tilt_prev = self.tilt_mm.copy()
            self.room_light, self.printer_offline_until = False, 0.0
            self.last_capture, self.last_operation = None, None
            self.position_check, self.position_warning = None, False
            self.overshoot_since, self.reached_target, self.target_dir = None, False, "at"
            self.stuck_window, self.heater_off_t = [], -1e18
            self.home_loss_after = self._draw_home_loss()
            self.last_call_t, self.t_start = t, t
            self.operator.history = []
            self.thinking = False
            self._apply_speed()
            self._new_session(t)
        self.session.event(t, "reset", "fresh rig (simulation)")
        self._emit({"kind": "reset", "t": t})
        return {"ok": True, "t": round(t, 3), "session": self.session.name}

    def control(self, body):
        """Sim-only knobs: speed, simulated operator, faults, and `reset` (a fresh rig)."""
        if body.get("reset"):
            return self.reset()
        t = self.clock.now()
        with self.lock:
            if "speed" in body:
                self.set_speed(float(body["speed"]))
            if "realistic_thinking" in body:
                self.realistic_thinking = bool(body["realistic_thinking"])
                self._apply_speed()
            if "auto_operator" in body:
                self.auto_operator = bool(body["auto_operator"])
            if "room_light" in body:
                self.room_light = bool(body["room_light"])
            if "led_drain_per_h" in body:
                self.led_drain_per_h = float(body["led_drain_per_h"])
            fault = body.get("fault")
            if fault == "bubble":
                lab = body.get("label")
                if lab in self.wells:
                    w = self.wells[lab]
                    w.bubble = (float(self.rng.uniform(-1.5, 1.5)), float(self.rng.uniform(-1.5, 1.5)), 0.9)
            elif fault == "lose_home":
                self.homed, self.z_known = False, False
                self.session.event(t, "home_lost", "fault injection")
            elif fault == "camera_bump":
                self.camera_bump_mm = self.camera_bump_mm + np.array([float(body.get("mm", 0.6)), 0.0])
            elif fault == "printer_offline":
                self.printer_offline_until = t + float(body.get("seconds", 60))
            elif fault == "heater_stuck":
                self.bed.stuck_on = True
            elif fault == "island_shift":
                isl = body.get("island", "P3")
                dx, dy = float(body.get("dx", 0.6)), float(body.get("dy", 0.0))
                for w in self.wells.values():
                    if geo.island_of(w.label) == isl:
                        w.island_shift_mm = (w.island_shift_mm[0] + dx, w.island_shift_mm[1] + dy)
            if fault:
                self.session.event(t, "fault_injected", json.dumps(body))
                self._emit({"kind": "fault", "t": t, "fault": body})
        return self.snapshot(wells=False)
