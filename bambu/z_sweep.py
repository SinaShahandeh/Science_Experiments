"""Track the well under the camera while stepping Z up and back down (does the image of the well stay put?).
usage: python3 z_sweep.py LABEL   -> calibration/zsweep/LABEL_zNN_{up,down}.jpg + printed table"""
import json, os, sys
import numpy as np
from motion import Gantry, load_pos
from pi_frame import pi_frame
from calibrate_wells import holes, C, J

src = open(os.path.join(os.path.dirname(__file__), "verify_vertical.py")).read()
ns = {}
exec(src[src.index("def hole_at"):src.index("def scale_fit")], {"cv2": __import__("cv2"), "np": np}, ns)
hole_at = ns["hole_at"]
CAL = "../calibration"
os.makedirs(f"{CAL}/zsweep", exist_ok=True)
k = sys.argv[1]
w = json.load(open(f"{CAL}/well_positions_fit.json"))["wells"][k]
ZS = [50, 60, 70, 80, 90, 100]
g = Gantry()
rows = []
try:
    if load_pos()["z"] > 50.01:
        g.move(x=w["x"], y=w["y"]); g.move(z=50.0)
    else:
        g.move(x=w["x"], y=w["y"])
    ref = None
    for leg, zs in (("up", ZS), ("down", ZS[::-1][1:])):
        for z in zs:
            g.move(z=float(z))
            f = f"{CAL}/zsweep/{k}_z{z}_{leg}.jpg"
            pi_frame(f)
            h = holes(f)
            if ref is None:
                ref = h
            # image scale vs Z50 from all wells matched about the nadir
            sc = []
            for s_try in np.linspace(0.6, 1.0, 81):
                q = C + s_try * (ref - C)
                d = np.min(np.linalg.norm(h[None] - q[:, None], axis=2), axis=1)
                sc.append((np.sum(d < 4), -np.mean(np.sort(d)[:8]), s_try))
            s = max(sc)[2]
            p, a = hole_at(f, C)
            rows.append((leg, z, s, p))
            print(f"{leg:4s} Z{z}: scale {s:.3f}  well centre px ({p[0]:.1f}, {p[1]:.1f})  "
                  f"offset from Z50 ({p[0]-rows[0][3][0]:+.1f}, {p[1]-rows[0][3][1]:+.1f})", flush=True)
    g.move(z=50.0)
finally:
    g.close()
p0 = rows[0][3]
A = np.array([[1 - r[2]] for r in rows]); Y = np.array([r[3] - p0 for r in rows])
coef, *_ = np.linalg.lstsq(np.vstack([A[:, 0], [r[1] - 50 for r in rows]]).T, Y, rcond=None) if False else (None,)
slope = np.linalg.lstsq(A, -Y, rcond=None)[0][0]   # p(Z) - p50 = -(1 - s) * J d
d = np.linalg.solve(J, slope)
print(f"{k}: drift fits a horizontal offset d = ({d[0]:+.2f}, {d[1]:+.2f}) mm from the Z-travel line")
