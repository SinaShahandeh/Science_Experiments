"""Blind check: go to labels from well_positions_fit.json and measure the nadir error (no correction)."""
import json, sys
import numpy as np
from motion import Gantry
from pi_frame import pi_frame
from calibrate_wells import holes, C, J

CAL = "../calibration"
fit = json.load(open(f"{CAL}/well_positions_fit.json"))["wells"]
labels = sys.argv[1:]
g = Gantry()
out = {}
try:
    for k in labels:
        w = fit[k]
        g.move(x=w["x"], y=w["y"], z=w["z"])
        f = f"{CAL}/verify_{k}.jpg"
        pi_frame(f)
        h = holes(f)
        e = h[np.argmin(np.linalg.norm(h - C, axis=1))] - C
        mm = np.linalg.solve(J, -e)  # nozzle correction that would have centred it
        out[k] = round(float(np.linalg.norm(mm)), 2)
        print(f"{k}: err px ({e[0]:+.1f},{e[1]:+.1f}) = {out[k]:.2f} mm")
finally:
    g.close()
print("max error mm:", max(out.values()))
