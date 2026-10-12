"""Robust gray value of a well region: drop saturated pixels (+ halo), then bright outliers (glints)."""
import cv2
import numpy as np

NADIR = (743, 350)  # calibration/nadir.json, Z50


def well_gray(img, half=12, center=NADIR, sat=250, halo=2, k=3.0):
    """img: BGR frame. Returns dict(value, n, n_total, sat_px, glint_px, all_saturated, keep_mask)."""
    cx, cy = center
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
