"""Fit the well pitch (px) at Z50 as a function of image position, for estimating the camera height.

The wide-angle lens and the camera's tilt make the 12 mm pitch read 48-66 px depending on where a pair of
openings sits in the frame. This fits a quadratic surface to nearest-neighbour hole spacings in the Z50
calibration frames; mhapi-service divides a measured spacing by it to get the image scale, and so the
nozzle height (scale ~ 1 / (Z + lens offset)). Used before homing (safety spec M4) and to confirm Z50.

Run after calibrate_wells.py / verify_wells.py (it reads their frames):  python3 fit_pitch.py
Writes ../calibration/pitch_z50.json.
"""
import glob
import json
import numpy as np
from wells import detect

CAL = "../calibration"
LENS_OFFSET_MM = 74.0  # lens is ~124 mm above the wall tops at Z50 (nadir.json scales Z50 -> Z54..70)


def holes(path):
    _, h = detect(path)
    if not h:
        return np.zeros((0, 2))
    med = np.median([x["area"] for x in h])
    return np.array([[x["cx"], x["cy"]] for x in h if 0.4 * med < x["area"] < 4.0 * med]).reshape(-1, 2)


def features(m):
    x, y = (m[:, 0] - 640) / 640, (m[:, 1] - 360) / 360
    return np.column_stack([np.ones_like(x), x, y, x * x, y * y, x * y])


frames = sorted(glob.glob(f"{CAL}/frames/*.jpg")) + sorted(glob.glob(f"{CAL}/verify_*.jpg"))
mid, dist = [], []
for f in frames:
    p = holes(f)
    if len(p) < 2:
        continue
    d = np.linalg.norm(p[:, None] - p[None], axis=2)
    np.fill_diagonal(d, np.inf)
    j = d.argmin(axis=1)
    mid += list((p + p[j]) / 2)
    dist += list(d[np.arange(len(p)), j])
mid, dist = np.array(mid), np.array(dist)
A = features(mid)
coef = np.linalg.lstsq(A, dist, rcond=None)[0]
for _ in range(3):  # drop diagonal pairs and missing neighbours
    ok = np.abs(dist / (A @ coef) - 1) < 0.08
    coef = np.linalg.lstsq(A[ok], dist[ok], rcond=None)[0]
sd = float(np.std(dist[ok] / (A[ok] @ coef)))
print(f"{len(frames)} frames, {len(dist)} pairs, {ok.sum()} used, relative sd {sd:.3f}")
json.dump({"coef": coef.tolist(), "lens_offset_mm": LENS_OFFSET_MM, "frames": len(frames), "pairs": int(ok.sum()),
           "relative_sd": round(sd, 4),
           "note": "pitch_px(x, y) at Z50 = coef . [1, u, v, u^2, v^2, u v], u = (x-640)/640, v = (y-360)/360"},
          open(f"{CAL}/pitch_z50.json", "w"), indent=1)
print("wrote", f"{CAL}/pitch_z50.json")
