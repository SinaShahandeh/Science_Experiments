"""Independent vertical check of the well table: at each well's fitted XY, image it at Z50 and Z100.

A well exactly below the camera does not move in the image when Z changes. If it is offset by d (mm),
its image drifts by (1 - s) * J @ d between the two heights (s = image scale at Z100 relative to Z50,
fitted from all wells visible in the pair). So d = J^-1 (p50 - p100) / (1 - s), without using the
nadir estimate. Also reports the error against the nadir pixel at Z50 (what verify_wells.py measures).
usage: python3 verify_vertical.py [labels...]   -> calibration/vertical_check.json, calibration/vertical/
"""
import json, os, sys
import cv2
import numpy as np
from motion import Gantry, load_pos
from pi_frame import pi_frame
from calibrate_wells import holes, C, J

CAL = "../calibration"
ZLO, ZHI = 50.0, 100.0
fit = json.load(open(f"{CAL}/well_positions_fit.json"))["wells"]
labels = sys.argv[1:] or list(fit)
os.makedirs(f"{CAL}/vertical", exist_ok=True)


def hole_at(path, guess, half=60):
    """Centre (px) of the well opening containing/nearest `guess`, found locally: walls = bright and
    unsaturated (Otsu threshold within the window, so it works at any Z); rainbow bed reflections
    are saturated and count as opening. Returns (centre, area) or (None, 0)."""
    img = cv2.imread(path)
    gx, gy = int(round(guess[0])), int(round(guess[1]))
    x0, y0 = gx - half, gy - half
    roi = img[y0:y0 + 2 * half, x0:x0 + 2 * half]
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[..., 1]
    t, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    wall = ((g > np.clip(t, 90, 200)) & (sat < 110)).astype(np.uint8)
    wall = cv2.morphologyEx(wall, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(1 - wall, connectivity=4)
    best, bd = None, 1e9
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if x == 0 or y == 0 or x + w >= 2 * half or y + h >= 2 * half:  # touches window edge: outside the grid
            continue
        if a < 300 or min(w, h) / max(w, h) < 0.7:
            continue
        cnts, _ = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        (cx, cy), _, _ = cv2.minAreaRect(cnts[0])
        d = np.hypot(cx - half, cy - half)
        if d < bd:
            best, bd = (np.array([cx + x0, cy + y0]), int(a)), d
    return best if best else (None, 0)


def scale_fit(a, b):
    """b - n = s (a - n) for matched hole sets: least squares for s and n."""
    A, y = [], []
    for (ax, ay), (bx, by) in zip(a, b):
        A += [[ax, 1, 0], [ay, 0, 1]]; y += [bx, by]
    (s, tx, ty), *_ = np.linalg.lstsq(np.array(A, float), np.array(y, float), rcond=None)
    return s, np.array([tx, ty]) / (1 - s)


out = {}
g = Gantry()
try:
    for k in labels:
        w = fit[k]
        pos = load_pos()
        if pos["z"] > ZLO + 0.01:
            g.move(x=w["x"], y=w["y"])      # XY first, then lower
            g.move(z=ZLO)
        else:
            g.move(x=w["x"], y=w["y"])
        f50 = f"{CAL}/vertical/{k}_z50.jpg"; pi_frame(f50); h50 = holes(f50)
        g.move(z=ZHI)
        f100 = f"{CAL}/vertical/{k}_z100.jpg"; pi_frame(f100); h100 = holes(f100)
        # scale between heights from all clearly detected wells (matched about the nadir)
        pairs = []
        for p in h50:
            q = C + 0.70 * (p - C)
            d = np.linalg.norm(h100 - q, axis=1)
            if len(d) and d.min() < 12:
                pairs.append((p, h100[np.argmin(d)]))
        a = np.array([p for p, _ in pairs]); b = np.array([q for _, q in pairs])
        s, n_local = scale_fit(a, b) if len(pairs) >= 4 else (0.70, np.array([np.nan, np.nan]))
        # the target well itself, found locally at both heights
        p50, a50 = hole_at(f50, C)
        p100, a100 = hole_at(f100, C + s * (p50 - C) if p50 is not None else C)
        if p50 is None or p100 is None:
            print(f"{k}: target well not found (Z50 {p50}, Z100 {p100})", flush=True)
            out[k] = {"error": "not found"}
            continue
        d_par = np.linalg.solve(J, (p50 - p100) / (1 - s))      # well offset from vertical, mm
        d_nad = np.linalg.solve(J, p50 - C)
        out[k] = {"parallax_mm": d_par.round(3).tolist(), "parallax_abs_mm": round(float(np.linalg.norm(d_par)), 3),
                  "nadir_err_mm": d_nad.round(3).tolist(), "p50": p50.round(1).tolist(), "p100": p100.round(1).tolist(),
                  "scale": round(float(s), 4), "local_fixed_point_px": n_local.round(1).tolist(), "n_holes": len(pairs),
                  "area_px": [a50, a100]}
        print(f"{k}: parallax offset ({d_par[0]:+.2f},{d_par[1]:+.2f}) mm |{np.linalg.norm(d_par):.2f}|  "
              f"nadir err ({d_nad[0]:+.2f},{d_nad[1]:+.2f}) mm  hole px Z50 {p50.round(1)} Z100 {p100.round(1)}  "
              f"s={s:.3f} fixed pt {n_local.round(1)} n={len(pairs)}", flush=True)
        json.dump(out, open(f"{CAL}/vertical_check.json", "w"), indent=1)
finally:
    g.close()
v = np.array([o["parallax_abs_mm"] for o in out.values() if "error" not in o])
fp = np.array([o["local_fixed_point_px"] for o in out.values() if "error" not in o])
fp = fp[~np.isnan(fp).any(1)]
print(f"parallax |offset| mm: median {np.median(v):.2f}, max {v.max():.2f};  fixed point px mean {fp.mean(0).round(1)} sd {fp.std(0).round(1)}")
