"""Find the nadir pixel: the image point directly below the camera.

Moving the camera straight up/down scales the image about the nadir (the vanishing point
of vertical lines), so we step Z, track the well openings, and fit the fixed point.
"""
import json
import numpy as np
from motion import Gantry
from pi_frame import pi_frame
from calibrate_wells import holes

CAL = "../calibration"
X, Y = 148.0, 141.0          # over the centre island
ZS = [50, 54, 58, 62, 66, 70]


def track(prev, cur, max_px):
    """For each point in prev, its nearest neighbour in cur (or NaN)."""
    out = np.full_like(prev, np.nan)
    for i, p in enumerate(prev):
        if np.isnan(p).any() or len(cur) == 0:
            continue
        d = np.linalg.norm(cur - p, axis=1)
        j = np.argmin(d)
        if d[j] < max_px:
            out[i] = cur[j]
    return out


def fixed_point(p1, p2):
    """p2 - p1 = k*p1 - m  with m = k*n  ->  n = m/k (least squares)."""
    ok = ~(np.isnan(p1).any(1) | np.isnan(p2).any(1))
    p1, p2 = p1[ok], p2[ok]
    A = []; b = []
    for (x1, y1), (x2, y2) in zip(p1, p2):
        A += [[x1, -1, 0], [y1, 0, -1]]; b += [x2 - x1, y2 - y1]
    sol, res, *_ = np.linalg.lstsq(np.array(A), np.array(b), rcond=None)
    k, mx, my = sol
    n = np.array([mx / k, my / k])
    resid = (np.array(b) - np.array(A) @ sol).reshape(-1, 2)
    return n, 1 + k, int(ok.sum()), float(np.sqrt((resid ** 2).sum(1).mean()))


g = Gantry()
try:
    g.move(x=X, y=Y, z=50.0)
    tracks = None
    for z in ZS:
        g.move(z=float(z))
        f = f"{CAL}/nadir_z{z}.jpg"
        pi_frame(f)
        h = holes(f)
        tracks = [h] if tracks is None else tracks + [track(tracks[-1], h, 25)]
        print(f"Z{z}: {len(h)} holes, tracked {np.sum(~np.isnan(tracks[-1]).any(1))}")
    g.move(z=50.0)
finally:
    g.close()

res = {}
for i in range(1, len(ZS)):
    n, s, cnt, rms = fixed_point(tracks[0], tracks[i])
    print(f"Z50->Z{ZS[i]}: nadir px ({n[0]:.1f}, {n[1]:.1f}), scale {s:.4f}, n={cnt}, rms {rms:.2f}px")
    res[ZS[i]] = {"nadir_px": n.tolist(), "scale": s, "n": cnt, "rms_px": rms}
json.dump(res, open(f"{CAL}/nadir.json", "w"), indent=1)
