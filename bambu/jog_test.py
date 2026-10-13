"""Small relative jogs (no homing), mimicking Bambu Studio's jog buttons. Verifies via camera."""
import time
from bambu_link import Printer
from snapshot import snapshot


def jog(p, axis, dist, feed):
    p.gcode(["M211 S", "M211 X1 Y1 Z1", "M1002 push_ref_mode", "G91",
             f"G1 {axis}{dist} F{feed}", "M1002 pop_ref_mode", "M211 R"])


p = Printer()
n0 = len(p.reports)
steps = [("X", -60, 3000, "x_moved.jpg"), ("Y", 40, 3000, "y_moved.jpg"),
         ("Y", -40, 3000, None), ("X", 60, 3000, "returned.jpg")]
for axis, d, f, shot in steps:
    print(f"jog {axis}{d:+} F{f}")
    jog(p, axis, d, f)
    time.sleep(3.5)
    if shot:
        print("  snapshot", snapshot(shot))
time.sleep(1)
for t, r in p.reports[n0:]:
    pr = r.get("print", {})
    interesting = {k: pr[k] for k in ("command", "result", "reason", "print_error", "hms", "gcode_state", "home_flag") if k in pr}
    if interesting.get("command") != "push_status" or "print_error" in interesting and interesting["print_error"]:
        print("report:", interesting)
p.pushall(); time.sleep(2)
print("final:", {k: p.state.get(k) for k in ("gcode_state", "print_error", "hms", "home_flag")})
p.close()
