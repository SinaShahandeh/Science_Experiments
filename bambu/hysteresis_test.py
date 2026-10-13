"""Does the camera view of a well depend on the direction of the last move? (camera/toolhead play)"""
import json, sys
import numpy as np, cv2
from motion import Gantry, load_pos
from pi_frame import pi_frame
from calibrate_wells import C
src = open("verify_vertical.py").read(); ns = {}
exec(src[src.index("def hole_at"):src.index("def scale_fit")], {"cv2": cv2, "np": np}, ns)
k = sys.argv[1] if len(sys.argv) > 1 else "C-B3"
w = json.load(open("../calibration/well_positions_fit.json"))["wells"][k]
X, Y = w["x"], w["y"]
g = Gantry()
seq = [("start", None),
       ("X+5 back", [dict(x=X + 5), dict(x=X)]), ("X-5 back", [dict(x=X - 5), dict(x=X)]),
       ("Y+5 back", [dict(y=Y + 5), dict(y=Y)]), ("Y-5 back", [dict(y=Y - 5), dict(y=Y)]),
       ("Z+20 back", [dict(z=70.0), dict(z=50.0)]), ("Y+5 back", [dict(y=Y + 5), dict(y=Y)]),
       ("Z+50 back", [dict(z=100.0), dict(z=50.0)]), ("Y-5 back", [dict(y=Y - 5), dict(y=Y)]),
       ("Z+20 back", [dict(z=70.0), dict(z=50.0)]), ("X+5 back", [dict(x=X + 5), dict(x=X)])]
try:
    assert abs(load_pos()["x"] - X) < 0.01 and abs(load_pos()["y"] - Y) < 0.01 and load_pos()["z"] == 50.0
    for i, (name, moves) in enumerate(seq):
        for m in moves or []:
            g.move(**m)
        f = f"/private/tmp/claude-501/-Users-sina-Documents-Science-Experiments/311ec921-2e75-49c7-a93c-5897211815ab/scratchpad/hy_{i}.jpg"
        pi_frame(f)
        p, _ = ns["hole_at"](f, C)
        print(f"{name:10s} well centre px ({p[0]:.1f}, {p[1]:.1f})  offset from nadir ({p[0]-C[0]:+.1f}, {p[1]-C[1]:+.1f})", flush=True)
finally:
    g.close()
