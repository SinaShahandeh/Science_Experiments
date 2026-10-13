"""Egg-white clouding kinetics run (trial): pre-hold, beep, operator loads, ramp, loop camera over wells.

usage: python3 kinetics_run.py OUTDIR [--pre 50] [--hold 75] [--wells P3-A1,P3-A2,P3-B2,P3-B1]

Control is by trigger files in OUTDIR (created by the agent when the operator says so):
  GO   -> wells loaded, hands clear: ramp to --hold and start imaging (t = 0)
  STOP -> stop imaging, heater off, lift Z to 100.
Outputs in OUTDIR: frames/*.jpg, frames.csv, temps.csv (every MQTT report with a bed temp),
events.csv, run.log (stdout).
"""
import argparse, csv, json, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

from bambu_link import Printer
from camstream import CamStream
from motion import load_pos, save_pos, ACCEL, SLOW_Z
import wellmap

WELLS = wellmap.wells()  # calibration/well_positions_fit.json (camera straight above each well, Z50)
FAST_XY = 1800  # 30 mm/s = 3x motion.SLOW_XY
Z_IMG = 50.0
PARK_X = 60.0   # toolhead far left while the operator loads P3
MAX_RUN_S = 60 * 60


def iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def beep(msg):
    for _ in range(3):
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])
    subprocess.Popen(["say", msg])


class Run:
    def __init__(self, out, wells, pre, hold, quiet=False, leave_heat=False):
        self.quiet, self.leave_heat = quiet, leave_heat
        self.out = Path(out)
        (self.out / "frames").mkdir(parents=True, exist_ok=True)
        self.wells, self.pre, self.hold = wells, pre, hold
        self.t_go = None
        self.lock = threading.Lock()
        self.ef = open(self.out / "events.csv", "w", newline="")
        self.ev = csv.writer(self.ef, lineterminator="\n")
        self.ev.writerow(["t_unix", "t_iso", "t_since_go_s", "event", "detail"])
        self.tf = open(self.out / "temps.csv", "w", newline="")
        self.tw = csv.writer(self.tf, lineterminator="\n")
        self.tw.writerow(["t_unix", "t_iso", "t_since_go_s", "bed_temper", "bed_target_temper"])
        self.ff = open(self.out / "frames.csv", "w", newline="")
        self.fw = csv.writer(self.ff, lineterminator="\n")
        self.fw.writerow(["n", "loop", "label", "file", "t_unix", "t_iso", "t_since_go_s", "x", "y", "z",
                          "bed_temper", "bed_target_temper", "temp_age_s", "move_to_still_s", "saw_motion"])
        self.last_temp = (None, None, None)  # (t, temp, target)
        self.p = Printer()
        orig = self.p._on_message

        def on_msg(c, u, msg):
            orig(c, u, msg)
            d = self.p.reports[-1][1].get("print", {})
            if "bed_temper" in d or "bed_target_temper" in d:
                t = self.p.reports[-1][0]
                s = self.p.state
                with self.lock:
                    self.last_temp = (t, s.get("bed_temper"), s.get("bed_target_temper"))
                    self.tw.writerow([f"{t:.3f}", iso(t), self.rel(t), s.get("bed_temper"), s.get("bed_target_temper")])
                    self.tf.flush()
            self.p.reports.clear()
        self.p.c.on_message = on_msg
        self._poll = True
        threading.Thread(target=self._pushall_loop, daemon=True).start()
        self.cam = CamStream()
        self.n = 0

    def rel(self, t):
        return f"{t - self.t_go:.3f}" if self.t_go else ""

    def event(self, name, detail=""):
        t = time.time()
        self.ev.writerow([f"{t:.3f}", iso(t), self.rel(t), name, detail])
        self.ef.flush()
        print(f"[{iso(t)}] {name} {detail}", flush=True)

    def _pushall_loop(self):
        while self._poll:
            self.p.pushall()
            time.sleep(5)

    def trig(self, name):
        return (self.out / name).exists()

    def move(self, x, y, z=None, f=FAST_XY, timeout=12):
        """Absolute move; returns (t_still, move_to_still_s, saw_motion) using the camera to detect stop."""
        pos = load_pos()
        lines = ["G90", f"M204 S{ACCEL}"]
        if z is not None and z > pos["z"]:
            lines.append(f"G1 Z{z:.2f} F{SLOW_Z}")
        lines.append(f"G1 X{x:.2f} Y{y:.2f} F{f}")
        if z is not None and z <= pos["z"]:
            lines.append(f"G1 Z{z:.2f} F{SLOW_Z}")
        same = abs(pos["x"] - x) < 0.01 and abs(pos["y"] - y) < 0.01 and (z is None or abs(pos["z"] - z) < 0.01)
        t_cmd = time.time()
        self.p.gcode(lines)
        dz = abs(pos["z"] - z) if z is not None else 0.0
        xy_same = abs(pos["x"] - x) < 0.01 and abs(pos["y"] - y) < 0.01
        if same:
            t_still, saw = t_cmd, False
        elif xy_same:  # Z-only: too slow for the frame-difference detector, wait it out
            time.sleep(dz / (SLOW_Z / 60) + 1.5)
            t_still, saw = time.time(), True
        else:
            t_still, saw = self.cam.wait_settled(t_cmd, move_thr=2.0, still_thr=0.3, still_n=3, timeout=timeout)
            if not saw:
                self.event("warn", f"no motion seen moving to X{x} Y{y}")
            if dz:
                time.sleep(dz / (SLOW_Z / 60) + 0.5)
                t_still = time.time()
        pos.update(x=x, y=y, **({"z": z} if z is not None else {}))
        save_pos(pos)
        return t_still, t_still - t_cmd, saw

    def capture(self, loop, label, move_s, saw):
        _, jpg, t, _ = self.cam.next()  # first frame that arrives after the image went still
        self.n += 1
        name = f"{self.n:05d}_{label}_t{t - self.t_go:08.2f}.jpg"
        (self.out / "frames" / name).write_bytes(jpg)
        pos = load_pos()
        with self.lock:
            tt, temp, tgt = self.last_temp
        self.fw.writerow([self.n, loop, label, name, f"{t:.3f}", iso(t), f"{t - self.t_go:.3f}",
                          pos["x"], pos["y"], pos["z"], temp, tgt,
                          f"{t - tt:.2f}" if tt else "", f"{move_s:.2f}", int(saw)])
        self.ff.flush()
        return t, temp

    def go(self):
        self.event("pre_heat", f"M140 S{self.pre}")
        self.p.gcode([f"M140 S{self.pre}"])
        self.event("park", f"X{PARK_X}")
        self.move(PARK_X, WELLS[self.wells[0]][1], Z_IMG, timeout=30)
        while (self.p.state.get("bed_temper") or 0) < self.pre - 0.2:
            time.sleep(0.5)
        self.event("pre_temp_reached", f"bed {self.p.state.get('bed_temper')}")
        if not self.quiet:
            beep(f"Bed at {self.pre} degrees. Load the wells now.")
        self.event("beep")
        # loading: 1 fps oblique frames from the park position, for timing of the operator's fills
        (self.out / "loading").mkdir(exist_ok=True)
        last = 0
        while not self.trig("GO"):
            if self.trig("STOP"):
                return
            _, jpg, t, _ = self.cam.next()
            if t - last >= 1.0:
                (self.out / "loading" / f"load_{iso(t).replace(':', '-')}.jpg").write_bytes(jpg)
                last = t
        self.t_go = time.time()
        self.p.gcode([f"M140 S{self.hold}"])
        self.event("GO", f"M140 S{self.hold}; t=0")
        loop = 0
        while not self.trig("STOP"):
            loop += 1
            for label in self.wells:
                if self.trig("STOP"):
                    break
                x, y = WELLS[label]
                t_still, move_s, saw = self.move(x, y)
                t, temp = self.capture(loop, label, move_s, saw)
                print(f"loop {loop:4d} {label} t={t - self.t_go:7.2f}s bed={temp} move={move_s:.2f}s", flush=True)
            if time.time() - self.t_go > MAX_RUN_S:
                self.event("max_runtime")
                break

    def finish(self):
        self.event("stop", "lift Z100" + ("" if self.leave_heat else ", M140 S0"))
        if not self.leave_heat:
            self.p.gcode(["M140 S0"])
        try:
            pos = load_pos()
            self.move(pos["x"], pos["y"], 100.0, timeout=20)
        except Exception as ex:
            self.event("warn", f"lift failed: {ex}")
        time.sleep(1)
        self._poll = False
        self.cam.close()
        self.p.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--pre", type=float, default=50)
    ap.add_argument("--hold", type=float, default=75)
    ap.add_argument("--wells", default="P3-A1,P3-A2,P3-B2,P3-B1")  # any labels from the table
    ap.add_argument("--quiet", action="store_true", help="no beep (dry runs)")
    ap.add_argument("--leave-heat", action="store_true", help="don't switch the bed off at the end")
    a = ap.parse_args()
    assert a.hold <= 75, "protocol limit: bed <= 75 C (PETG grid)"
    r = Run(a.out, a.wells.split(","), a.pre, a.hold, a.quiet, a.leave_heat)
    (Path(a.out) / "params.json").write_text(json.dumps(vars(a) | {"fast_xy_mm_min": FAST_XY, "accel": ACCEL,
                                                                   "z": Z_IMG, "wells_xy": WELLS}, indent=1))
    try:
        r.go()
    except KeyboardInterrupt:
        r.event("interrupted")
    except Exception as ex:
        r.event("error", repr(ex))
        raise
    finally:
        r.finish()
