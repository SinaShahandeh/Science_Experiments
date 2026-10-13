"""Robust gray value of a well: 7 x 7 mm square centred on the nadir pixel; drop saturated pixels (+ halo),
then bright outliers (glints), median of the rest. Pass center = NADIR + cam_tilt shift if the mount moved."""
import cv2
import numpy as np

import json
from pathlib import Path
import wellmap

NADIR = tuple(wellmap.nadir())  # pixel straight below the camera (reference pose), Z50
_J = json.loads((Path(__file__).resolve().parent.parent / "calibration" / "jacobian_z50.json").read_text())["J"]
PX_PER_MM = (abs(_J[0][0]) + abs(_J[1][1])) / 2
BOX_MM = 7.0  # centred square inside the 10 mm opening: 1.5 mm margin each side for position error/creep
HALF = int(round(BOX_MM / 2 * PX_PER_MM))


def well_gray(img, half=HALF, center=NADIR, sat=250, halo=2, k=3.0):
    """img: BGR frame. Returns dict(value, n, n_total, sat_px, glint_px, all_saturated, keep_mask)."""
    cx, cy = int(round(center[0])), int(round(center[1]))
    roi = img[cy - half:cy + half, cx - half:cx + half]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(float)
    satm = roi.max(axis=2) >= sat  # saturated in any channel
    n = gray.size
    if satm.all():
        return dict(value=255.0, n=0, n_total=n, sat_px=n, glint_px=0, all_saturated=True, keep_mask=~satm)
    bad = cv2.dilate(satm.astype(np.uint8), np.ones((2 * halo + 1,) * 2, np.uint8)) > 0 if halo else satm
    if bad.all():
        bad = satm
    v = gray[~bad]
    med = np.median(v)
    mad = 1.4826 * np.median(np.abs(v - med))
    glint = gray > med + k * max(mad, 1.0)  # one-sided: reflections are only ever brighter
    keep = ~bad & ~glint
    return dict(value=float(np.median(gray[keep])), n=int(keep.sum()), n_total=n, sat_px=int(satm.sum()),
                glint_px=int((glint & ~bad).sum()), all_saturated=False, keep_mask=keep)
