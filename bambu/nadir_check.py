"""Re-measure the nadir pixel without overwriting calibration/ (same method as nadir.py, Z50 vs Z58/66/70)."""
import json, sys
import numpy as np
from motion import Gantry
from pi_frame import pi_frame
from calibrate_wells import holes

OUT = "../calibration/recheck_20261011"
X, Y = 148.0, 141.0
ZS = [50, 58, 66, 70]


def match(a, b, max_px):
    out = np.full_like(a, np.nan)
    for i, p in enumerate(a):
        d = np.linalg.norm(b - p, axis=1)
        j = np.argmin(d)
        if d[j] < max_px:
            out[i] = b[j]
    return out


def fixed_point(p1, p2):
    ok = ~(np.isnan(p1).any(1) | np.isnan(p2).any(1))
    p1, p2 = p1[ok], p2[ok]
    A, b = [], []
    for (x1, y1), (x2, y2) in zip(p1, p2):
        A += [[x1, -1, 0], [y1, 0, -1]]; b += [x2 - x1, y2 - y1]
    sol, *_ = np.linalg.lstsq(np.array(A), np.array(b), rcond=None)
    k, mx, my = sol
    resid = (np.array(b) - np.array(A) @ sol).reshape(-1, 2)
    return [mx / k, my / k], 1 + k, int(ok.sum()), float(np.sqrt((resid ** 2).sum(1).mean()))


g = Gantry()
try:
    g.move(x=X, y=Y, z=50.0)
    H = {}
    for z in ZS:
        g.move(z=float(z))
        f = f"{OUT}/nadir_z{z}.jpg"
        pi_frame(f)
        H[z] = holes(f)
        print(f"Z{z}: {len(H[z])} holes", flush=True)
    g.move(z=50.0)
finally:
    g.close()
res = {}
tr = H[50]
for z in ZS[1:]:
    tr = match(tr, H[z], 30)
    n, s, cnt, rms = fixed_point(H[50], tr)
    res[z] = dict(nadir_px=n, scale=s, n=cnt, rms_px=rms)
    print(f"Z50->Z{z}: nadir ({n[0]:.1f}, {n[1]:.1f}) scale {s:.3f} n={cnt} rms {rms:.2f}px")
json.dump(res, open(f"{OUT}/nadir.json", "w"), indent=1)
