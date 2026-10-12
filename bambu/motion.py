"""Completion-tracked motion for the A1 used as a camera gantry (no fan signalling).

The printer does not report XYZ, so the last commanded absolute position is saved to
calibration/position.json. Completion is confirmed with the Pi camera on the toolhead:
  - move(): wait the move's kinematic duration + margin, then require 4 consecutive still frames;
  - run():  for commands of unknown duration (homing): wait until the image has moved and then
    stayed still for `still_s` seconds, optionally also until MQTT `home_flag` bits are set.
"""
import json, math, sys, time
from pathlib import Path
from bambu_link import Printer

STATE = Path(__file__).resolve().parent.parent / "calibration" / "position.json"
SLOW_XY = 1800  # mm/min = 30 mm/s (approved 2026-10-12; was 600)
SLOW_Z = 300    # mm/min = 5 mm/s
ACCEL = 500     # mm/s^2 (Bambu default is several thousand)


def load_pos():
    return json.loads(STATE.read_text()) if STATE.exists() else None


def save_pos(pos):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(pos))


class Gantry:
    def __init__(self):
        from camstream import CamStream
        self.cam = CamStream()
        self.p = Printer()
        self.p.pushall()
        time.sleep(2)

    def run(self, lines, still_s=2.0, home_bits=0, min_s=1.0, timeout=240):
        """Send G-code of unknown duration; return when it has visibly finished."""
        t_cmd = time.time()
        self.p.gcode(list(lines))
        moved, still_since, last_push = False, None, 0.0
        while time.time() - t_cmd < timeout:
            _, _, t, d = self.cam.next()
            if time.time() - last_push > 1.0:
                self.p.pushall()
                last_push = time.time()
            if d is None:
                continue
            if d > 2.0:
                moved, still_since = True, None
            elif d < 0.3:
                still_since = still_since or t
            else:
                still_since = None
            flags_ok = (self.p.state.get("home_flag", 0) & home_bits) == home_bits
            if (moved and flags_ok and still_since and t - still_since >= still_s
                    and time.time() - t_cmd >= min_s):
                return time.time() - t_cmd
        raise TimeoutError(f"no motion+stillness within {timeout}s for {lines}")

    def homed(self):
        self.p.pushall()
        time.sleep(2)
        return (self.p.state.get("home_flag", 0) & 0b111) == 0b111

    def _wait_still(self, t_cmd, duration, still_thr=0.3, still_n=4, timeout=30):
        """Camera completion: sleep past the expected end, then wait for still_n still frames."""
        time.sleep(max(0.0, t_cmd + duration + 0.8 - time.time()))
        still, t0 = 0, time.time()
        while time.time() - t0 < timeout:
            _, _, _, d = self.cam.next()
            still = still + 1 if (d is not None and d < still_thr) else 0
            if still >= still_n:
                return time.time() - t_cmd
        raise TimeoutError("camera never went still after move")

    def move(self, x=None, y=None, z=None, f_xy=SLOW_XY, f_z=SLOW_Z):
        pos = load_pos()
        assert pos is not None, "no known position: home first"
        lines = ["G90", f"M204 S{ACCEL}"]
        if z is not None and z > pos["z"]:  # going up: lift first
            lines.append(f"G1 Z{z:.2f} F{f_z}")
        xy = " ".join(f"{a.upper()}{v:.2f}" for a, v in (("x", x), ("y", y)) if v is not None)
        if xy:
            lines.append(f"G1 {xy} F{f_xy}")
        if z is not None and z <= pos["z"]:  # going down: lower last
            lines.append(f"G1 Z{z:.2f} F{f_z}")
        dxy = math.hypot((x if x is not None else pos["x"]) - pos["x"],
                         (y if y is not None else pos["y"]) - pos["y"])
        dz = abs(z - pos["z"]) if z is not None else 0.0
        v = f_xy / 60
        dur = (dxy / v + v / ACCEL if dxy else 0.0) + dz / (f_z / 60)
        t_cmd = time.time()
        self.p.gcode(lines)
        dt = self._wait_still(t_cmd, dur)
        pos.update({k: v for k, v in (("x", x), ("y", y), ("z", z)) if v is not None})
        save_pos(pos)
        return pos, dt

    def close(self):
        self.cam.close()
        self.p.close()


if __name__ == "__main__":
    # usage: python motion.py X Y Z   (absolute, mm; use '-' to keep an axis)
    args = [None if a == "-" else float(a) for a in sys.argv[1:4]]
    g = Gantry()
    try:
        if not g.homed():
            sys.exit("printer is not homed")
        pos, dt = g.move(*args)
        print(f"at {pos} ({dt:.1f}s)")
    finally:
        g.close()
