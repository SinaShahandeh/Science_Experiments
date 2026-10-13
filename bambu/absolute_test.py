"""Home, then absolute moves + timed Y moves at different speeds.
Completion is detected by queuing a fan toggle after M400 and watching the MQTT report."""
import time
from bambu_link import Printer
from snapshot import snapshot

p = Printer()
p.pushall(); time.sleep(2)
fan0 = p.state.get("cooling_fan_speed")
print("home_flag before:", p.state.get("home_flag"), "fan:", fan0)


def run_and_wait(lines, label, timeout=120):
    """Send moves, then M400 + fan toggle; return seconds until the fan change is reported."""
    target = "15" if p.state.get("cooling_fan_speed", "0") == "0" else "0"
    s = 255 if target == "15" else 0
    t0 = time.time()
    p.gcode(list(lines) + ["M400", f"M106 P1 S{s}"])
    while time.time() - t0 < timeout:
        if p.state.get("cooling_fan_speed") == target:
            dt = time.time() - t0
            print(f"{label:38s} done in {dt:5.1f}s")
            return dt
        time.sleep(0.05)
    print(f"{label}: TIMEOUT (fan={p.state.get('cooling_fan_speed')})")


run_and_wait(["G28"], "G28 home all")
p.pushall(); time.sleep(2)
print("home_flag after: ", p.state.get("home_flag"), bin(p.state.get("home_flag", 0))[-4:])

run_and_wait(["G90", "G1 Z30 F900", "G1 X128 Y128 F6000"], "center X128 Y128 Z30")
snapshot("abs_center.jpg")
run_and_wait(["G1 X30 Y30 F6000"], "corner X30 Y30")
snapshot("abs_x30y30.jpg")
run_and_wait(["G1 X226 Y226 F6000"], "corner X226 Y226")
snapshot("abs_x226y226.jpg")

# Y speed sweep: 196 mm stroke at increasing feedrates
run_and_wait(["G1 X128 Y30 F6000"], "to Y30")
for mm_s in (10, 50, 150):
    for y in (226, 30):
        run_and_wait([f"G1 Y{y} F{mm_s*60}"], f"Y stroke 196mm @ {mm_s} mm/s")

run_and_wait(["G1 X128 Y128 F6000"], "back to center")
p.gcode(["M106 P1 S0"])
time.sleep(1)
p.close()
