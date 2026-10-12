"""Home like Bambu's A1 start G-code: XY first, then Z on bare steel at X128 Y254 (behind the grid).

Completion is confirmed by the camera (image moved, then still) and, when the printer had lost its
home, by the MQTT home_flag bits. No fan signalling.
"""
from motion import Gantry, save_pos, SLOW_XY, SLOW_Z, ACCEL

g = Gantry()
try:
    print("XY home...")
    print(" done in %.1fs" % g.run(["G28 X"], still_s=3.0, home_bits=0b011, min_s=5))
    print("lift 5, park X128 Y254...")
    print(" done in %.1fs" % g.run(["G91", "G1 Z5 F1200", "G90", f"M204 S{ACCEL}",
                                    f"G1 X128 F{SLOW_XY}", f"G1 Y254 F{SLOW_XY}"], still_s=1.5))
    print("Z home...")
    print(" done in %.1fs" % g.run(["G28 Z P0 T140"], still_s=3.0, home_bits=0b100, min_s=5))
    save_pos({"x": 128.0, "y": 254.0, "z": None})
    print("lift to Z100...")
    g.p.gcode(["G90", f"G1 Z100 F{SLOW_Z}"])
    import time
    time.sleep(100 / (SLOW_Z / 60) + 1.5)  # Z-only move after homing: absolute Z unknown before, so timed
    save_pos({"x": 128.0, "y": 254.0, "z": 100.0})
    print("homed:", g.homed(), "home_flag:", bin(g.p.state.get("home_flag", 0))[-3:])
finally:
    g.close()
