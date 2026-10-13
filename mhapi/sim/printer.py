"""A simulated Bambu A1 with the PrinterLink interface: start(), publish(payload), `connected`, close(), and
on_report(t, payload) for reports shaped like the real printer's (push_status deltas, full reports on
pushall, gcode_line acknowledgements, get_version).

It moves with trapezoidal profiles at the commanded feed and acceleration, homes (G28 X: X and Y; G28 Z),
heats the bed, and records commands that would be unsafe on the real rig in `violations`.
"""
import logging
import math
import re
import threading
import time
from queue import Empty, Queue

log = logging.getLogger("mhapi.sim.printer")
BASE_FLAG = 0x327F4598 & ~0b111   # the real printer's home_flag carries many other bits
FIRMWARE = "01.08.01.00"
AMBIENT_C = 24.0
X_HOME, Y_HOME = 0.0, 0.0


class Segment:
    def __init__(self, p0, p1, feed_mm_s, accel):
        self.p0, self.p1 = p0, p1
        self.d = math.dist(p0, p1)
        v, a = max(feed_mm_s, 1e-3), max(accel, 1e-3)
        if self.d < v * v / a:  # triangular profile
            self.ta = math.sqrt(self.d / a)
            self.v, self.tc = a * self.ta, 0.0
        else:
            self.ta, self.v = v / a, v
            self.tc = (self.d - v * v / a) / v
        self.a = a
        self.T = 2 * self.ta + self.tc
        self.t0 = time.time()

    def at(self, t):
        u = min(max(t - self.t0, 0.0), self.T)
        a, ta, tc, v = self.a, self.ta, self.tc, self.v
        if u < ta:
            s = 0.5 * a * u * u
        elif u < ta + tc:
            s = 0.5 * a * ta * ta + v * (u - ta)
        else:
            w = self.T - u
            s = self.d - 0.5 * a * w * w
        f = 1.0 if self.d == 0 else s / self.d
        return tuple(p + (q - p) * f for p, q in zip(self.p0, self.p1))


class SimPrinter:
    def __init__(self, on_report, park_xy=(128.0, 254.0), homed=False, pos=(128.0, 254.0, 50.0), bed=AMBIENT_C,
                 target=0.0, homing_feed=60.0, z_probe_feed=15.0, report_period_s=1.5):
        self.on_report = on_report
        self.park_xy = tuple(park_xy)
        self.lock = threading.RLock()
        self.home_bits = 0b111 if homed else 0
        self.pos = tuple(float(v) for v in pos)
        self.seg = None
        self.absolute = True
        self.accel = 10000.0          # firmware default until M204
        self.bed, self.target = float(bed), float(target)
        self.gcode_state, self.spd, self.print_error, self.hms = "IDLE", (2, 100), 0, []
        self.homing_feed, self.z_probe_feed = homing_feed, z_probe_feed
        self.report_period_s = report_period_s
        self.q = Queue()
        self.faults = {}
        self.violations = []
        self.commands = []            # every G-code line received, in order
        self._connected = False
        self.epoch = 0                # incremented on every (re)connection, like PrinterLink
        self._stop = threading.Event()
        self._changed = set()
        self._seq = 0
        self.t_last_motion = time.time()
        self.lose_home_after_s = None

    # -- PrinterLink interface -------------------------------------------------------------------------
    def start(self, timeout=10.0):
        self._connected = True
        self.epoch += 1
        threading.Thread(target=self._executor, name="sim-gcode", daemon=True).start()
        threading.Thread(target=self._physics, name="sim-physics", daemon=True).start()
        return self

    @property
    def connected(self):
        return self._connected and not self.faults.get("link_down")

    def close(self):
        self._stop.set()
        self._connected = False

    def publish(self, payload):
        if not self.connected:
            return False
        if "pushing" in payload:
            self._later(0.15, lambda: self._report(full=True))
        elif "info" in payload:
            seq = payload["info"].get("sequence_id")
            self._later(0.2, lambda: self._emit({"info": {"command": "get_version", "sequence_id": seq, "module": [
                {"name": "ota", "sw_ver": self.faults.get("firmware", FIRMWARE), "hw_ver": "OTA"},
                {"name": "mc", "sw_ver": "00.01.30.58", "hw_ver": "MC02"}]}}))
        elif "print" in payload and payload["print"].get("command") == "gcode_line":
            p = payload["print"]
            if self.faults.pop("swallow_next_gcode", False):
                log.info("sim: swallowed %r", p["param"])
                return True
            lines = [l.strip() for l in p["param"].splitlines() if l.strip()]
            no_motion = self.faults.pop("ack_no_motion", False)
            self._later(0.12, lambda: self._emit({"print": {"command": "gcode_line", "sequence_id": p["sequence_id"],
                                                            "param": p["param"], "reason": "success",
                                                            "result": "success"}}))
            if not no_motion:
                for line in lines:
                    self.q.put(line)
        return True

    # -- reports ---------------------------------------------------------------------------------------
    def _later(self, dt, fn):
        threading.Timer(dt, fn).start()

    def _emit(self, payload):
        if self.connected and not self._stop.is_set():
            self.on_report(time.time(), payload)

    def reported_bed(self):
        if self.faults.get("bed_override") is not None:
            return float(self.faults["bed_override"])
        return round(self.bed * 32) / 32

    def _report(self, full=False, fields=()):
        with self.lock:
            self._seq += 1
            p = {"command": "push_status", "msg": 0 if full else 1, "sequence_id": str(self._seq),
                 "bed_temper": self.reported_bed()}
            allf = {"bed_target_temper": self.target, "home_flag": BASE_FLAG | self.home_bits,
                    "spd_lvl": self.spd[0], "spd_mag": self.spd[1], "gcode_state": self.gcode_state,
                    "print_error": self.print_error, "hms": list(self.hms), "nozzle_temper": 30.0,
                    "nozzle_target_temper": 0, "print_type": "idle"}
            keys = allf.keys() if full else (set(fields) | self._changed)
            for k in keys:
                p[k] = allf[k]
            self._changed.clear()
        self._emit({"print": p})

    def mark_changed(self, *fields):
        with self.lock:
            self._changed.update(fields)

    # -- physics -----------------------------------------------------------------------------------------
    def _physics(self):
        last, last_report = time.time(), 0.0
        while not self._stop.wait(0.1):
            now = time.time()
            dt, last = now - last, now
            with self.lock:
                if self.faults.get("heater_stuck"):
                    rate = 0.2
                elif self.target > 0:
                    rate = max(-0.25, min(0.6, (self.target - self.bed) / 12.0))
                else:
                    rate = -(self.bed - AMBIENT_C) / 300.0
                self.bed += rate * dt
                idle = self.seg is None and self.q.empty() and now - self.t_last_motion
                if (self.lose_home_after_s and self.home_bits and idle and idle > self.lose_home_after_s):
                    self.home_bits = 0
                    self._changed.add("home_flag")
            if now - last_report >= self.report_period_s:
                last_report = now
                self._report()

    def position_at(self, t=None):
        with self.lock:
            seg = self.seg
            pos = self.pos
        return seg.at(t or time.time()) if seg is not None else pos

    # -- G-code --------------------------------------------------------------------------------------------
    def _violation(self, msg):
        log.error("SIM VIOLATION: %s", msg)
        self.violations.append({"t": time.time(), "message": msg})

    def _executor(self):
        while not self._stop.is_set():
            try:
                line = self.q.get(timeout=0.2)
            except Empty:
                continue
            self.commands.append(line)
            try:
                self._execute(line)
            except Exception as e:  # noqa: BLE001
                self._violation(f"sim could not execute {line!r}: {e}")

    def _move(self, target, feed_mm_s):
        seg = Segment(self.pos, target, feed_mm_s, self.accel)
        with self.lock:
            self.seg = seg
        time.sleep(seg.T)
        with self.lock:
            self.pos, self.seg = tuple(target), None
            self.t_last_motion = time.time()

    def _execute(self, line):
        x, y, z = self.pos
        if line == "G90":
            self.absolute = True
        elif line == "G91":
            self.absolute = False
        elif m := re.fullmatch(r"M204 S(\d+(?:\.\d+)?)", line):
            self.accel = float(m[1])
        elif m := re.fullmatch(r"M140 S(\d+(?:\.\d+)?)", line):
            with self.lock:
                self.target = float(m[1])
            self.mark_changed("bed_target_temper")
        elif line == "G28 X":
            if z < 20:
                self._violation(f"G28 X with the nozzle at Z{z:.1f} (sweeps over the grid walls)")
            self._move((X_HOME, Y_HOME, z), self.homing_feed)
            with self.lock:
                self.home_bits |= 0b011
            self.mark_changed("home_flag")
        elif line == "G28 Z P0 T140":
            if (self.home_bits & 0b011) != 0b011:
                self._violation("G28 Z before X and Y are homed")
            if math.hypot(x - self.park_xy[0], y - self.park_xy[1]) > 1.0:
                self._violation(f"G28 Z at X{x:.1f} Y{y:.1f}, not on bare steel at the park point")
            self._move((x, y, 0.0), self.z_probe_feed)
            with self.lock:
                self.home_bits |= 0b100
            self.mark_changed("home_flag")
        elif line.startswith("G28"):
            self._violation(f"{line!r}: plain G28 probes Z over the center island")
        elif line.startswith("G1 "):
            w = {a: float(v) for a, v in re.findall(r"([XYZF])(-?\d+(?:\.\d+)?)", line)}
            if self.absolute:
                if ("X" in w or "Y" in w) and (self.home_bits & 0b011) != 0b011:
                    self._violation(f"absolute XY move {line!r} while X/Y are not homed")
                if "Z" in w and not self.home_bits & 0b100:
                    self._violation(f"absolute Z move {line!r} while Z is not homed")
                tgt = (w.get("X", x), w.get("Y", y), w.get("Z", z))
            else:
                tgt = (x + w.get("X", 0.0), y + w.get("Y", 0.0), z + w.get("Z", 0.0))
            if ("X" in w or "Y" in w) and min(tgt[2], z) < 15:
                self._violation(f"XY move {line!r} with the nozzle at Z{min(tgt[2], z):.1f}")
            speed = self.spd[1] / 100.0
            self._move(tgt, w.get("F", 1800) / 60.0 * speed)
        else:
            self._violation(f"unexpected command {line!r}")

    # -- faults --------------------------------------------------------------------------------------------
    def set_fault(self, name, value):
        with self.lock:
            if name == "speed_mode":
                self.spd = (2, 100) if value == "standard" else (3, 124)
                self._changed.update(("spd_lvl", "spd_mag"))
            elif name == "foreign_target":
                self.target = float(value)
                self._changed.add("bed_target_temper")
            elif name == "start_print":
                self.gcode_state = "RUNNING"
                self._changed.add("gcode_state")
            elif name == "lose_home":
                self.home_bits = 0
                self._changed.add("home_flag")
            elif name == "foreign_home":
                self.home_bits = 0b111
                self._changed.add("home_flag")
            elif name == "set_z":           # e.g. an interrupted Z homing left the nozzle low
                self.pos = (self.pos[0], self.pos[1], float(value))
            elif name == "set_bed":
                self.bed = float(value)
            elif name == "lose_home_after_s":
                self.lose_home_after_s = None if value is None else float(value)
            elif name == "link_down" and not value and self.faults.pop("link_down", None):
                self.epoch += 1               # the link comes back: a new connection
            elif name in ("swallow_next_gcode", "ack_no_motion", "link_down", "heater_stuck", "bed_override",
                          "firmware"):
                if value is None or value is False:
                    self.faults.pop(name, None)
                else:
                    self.faults[name] = value
            else:
                raise KeyError(f"unknown printer fault {name!r}")

    def view(self):
        x, y, z = self.position_at()
        return {"pos": [round(x, 2), round(y, 2), round(z, 2)], "moving": self.seg is not None,
                "home_bits": self.home_bits, "bed": round(self.bed, 2), "target": self.target,
                "faults": {k: v for k, v in self.faults.items()}, "violations": self.violations,
                "commands": len(self.commands), "connected": self.connected}
