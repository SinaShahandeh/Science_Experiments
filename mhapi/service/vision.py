"""Frame processing: camera tilt, the cell crop and image, the opacity reading, wall gray, well openings,
and the camera height. Pure functions of a frame and the calibration (no hardware).

Pixel numbers are those of the API spec: the cell is 68 x 68 camera pixels (14 x 14 mm on the bed's scale),
the measured square 34 x 34 px (7 x 7 mm), both centred on the straight-down pixel plus the camera tilt.
"""
import math

import cv2
import numpy as np

from .calibration import TILT_STRIP

CROP_HALF = 34        # cell: 68 x 68 px
BOX_HALF = 17         # measured square: 34 x 34 px
ENLARGE = 4           # cell image: 272 x 272 px, no smoothing
SATURATED = 250       # any channel >= this is saturated
HALO_PX = 2           # border dropped around saturated pixels
GLARE_K = 3.0         # reflections: above median + 3 * 1.4826 * MAD (scaled MAD floored at 1)
WALL_BAND_MM = (6.0, 6.8)   # wall tops: Chebyshev distance from the cell centre, bed scale
GREEN = (0, 255, 0)
PITCH_MM = 12.0


def decode(jpg, flags=cv2.IMREAD_COLOR):
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), flags)
    if img is None:
        raise ValueError("undecodable JPEG frame")
    return img


def encode_png(img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("PNG encoding failed")
    return buf.tobytes()


def tilt(gray, cal):
    """Shift (dx, dy) in px of the toolhead strip against the reference, and the match quality (0-1)."""
    y0, y1, x0, x1 = TILT_STRIP
    strip = gray[y0:y1, x0:x1].astype(np.float32)
    (dx, dy), response = cv2.phaseCorrelate(cal.tilt_ref, strip, cal.tilt_window)
    return float(dx), float(dy), float(response)


def origin(center, half):
    """First index of the 2*half pixels centred on `center` (pixel-centre coordinates)."""
    return int(math.floor(center - half + 1.0))


def window(img, center, half):
    ox, oy = origin(center[0], half), origin(center[1], half)
    h, w = img.shape[:2]
    if ox < 0 or oy < 0 or ox + 2 * half > w or oy + 2 * half > h:
        raise ValueError(f"window of {2 * half} px around {tuple(np.round(center, 1))} leaves the frame")
    return img[oy:oy + 2 * half, ox:ox + 2 * half], (ox, oy)


def opacity(frame, center):
    """Robust gray of the 7 x 7 mm square: drop saturated pixels and a 2 px halo, then reflections
    (one-sided MAD filter), and return the median of the rest (bambu/well_gray.py)."""
    roi, _ = window(frame, center, BOX_HALF)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(float)
    sat = roi.max(axis=2) >= SATURATED
    n = int(gray.size)
    if sat.all():
        return dict(gray=255.0, pixels_total=n, pixels_used=0, saturated_px=n, reflection_px=0, all_saturated=True)
    k = 2 * HALO_PX + 1
    bad = cv2.dilate(sat.astype(np.uint8), np.ones((k, k), np.uint8)) > 0
    if bad.all():
        bad = sat
    v = gray[~bad]
    med = float(np.median(v))
    mad = 1.4826 * float(np.median(np.abs(v - med)))
    glare = gray > med + GLARE_K * max(mad, 1.0)
    keep = ~bad & ~glare
    return dict(gray=float(np.median(gray[keep])), pixels_total=n, pixels_used=int(keep.sum()),
                saturated_px=int(sat.sum()), reflection_px=int((glare & ~bad).sum()), all_saturated=False)


def wall_gray(frame, center, px_per_mm):
    """Median gray of the wall tops: the square band 6-6.8 mm from the cell centre (in x or y)."""
    crop, (ox, oy) = window(frame, center, CROP_HALF)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    xs = np.arange(ox, ox + 2 * CROP_HALF) - center[0]
    ys = np.arange(oy, oy + 2 * CROP_HALF) - center[1]
    d = np.maximum(np.abs(xs)[None, :], np.abs(ys)[:, None]) / px_per_mm
    band = (d >= WALL_BAND_MM[0]) & (d <= WALL_BAND_MM[1])
    return float(np.median(gray[band]))


def cell_crop(frame, center):
    crop, _ = window(frame, center, CROP_HALF)
    return crop.copy()


def cell_image(crop):
    """The cell enlarged 4x without smoothing, with the measured square outlined in green just outside it."""
    big = cv2.resize(crop, None, fx=ENLARGE, fy=ENLARGE, interpolation=cv2.INTER_NEAREST)
    a = (CROP_HALF - BOX_HALF) * ENLARGE          # first enlarged pixel of the square
    b = (CROP_HALF + BOX_HALF) * ENLARGE - 1      # last one
    cv2.rectangle(big, (a - 2, a - 2), (b + 2, b + 2), GREEN, 2)
    return big


def holes(frame, min_area=150, max_area=40000):
    """Centres (px) of well openings: dark ~square holes inside white PETG walls (bambu/wells.py)."""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    white = ((g > 150) & (hsv[..., 1] < 110)).astype(np.uint8) * 255
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, hier = cv2.findContours(white, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    if hier is None:
        return np.zeros((0, 2))
    for i, c in enumerate(cnts):
        if hier[0][i][3] < 0:  # only inner contours: holes in a white blob
            continue
        a = cv2.contourArea(c)
        if not (min_area < a < max_area):
            continue
        (cx, cy), (w, h), _ = cv2.minAreaRect(c)
        if min(w, h) / max(w, h) < 0.7 or a / (w * h) < 0.75:
            continue
        found.append((cx, cy, a))
    if not found:
        return np.zeros((0, 2))
    med = np.median([f[2] for f in found])
    return np.array([[cx, cy] for cx, cy, a in found if 0.4 * med < a < 4.0 * med], float).reshape(-1, 2)


def nearest(points, p):
    """Index and distance of the point nearest p, or (None, inf)."""
    if len(points) == 0:
        return None, math.inf
    d = np.linalg.norm(points - np.asarray(p, float), axis=1)
    i = int(np.argmin(d))
    return i, float(d[i])


def _pitch_features(m):
    x = (m[:, 0] - 640.0) / 640.0
    y = (m[:, 1] - 360.0) / 360.0
    return np.column_stack([np.ones_like(x), x, y, x * x, y * y, x * y])


def estimate_z(points, cal, z_ref=50.0):
    """Camera height (nozzle Z, mm) from the spacing of the openings, or None if it can't be measured.

    The pitch at Z50 depends on where a pair of openings sits in the frame (lens distortion and the
    camera's tilt); calibration/pitch_z50.json models it. Image scale goes as 1 / (Z + lens offset).
    """
    if len(points) < 3:
        return None
    d = np.linalg.norm(points[:, None] - points[None], axis=2)
    np.fill_diagonal(d, np.inf)
    j = d.argmin(axis=1)
    dist = d[np.arange(len(points)), j]
    mid = (points + points[j]) / 2
    if cal.pitch_model is not None:
        p50 = _pitch_features(mid) @ cal.pitch_model["coef"]
        offset = cal.pitch_model["lens_offset_mm"]
    else:
        p50 = np.full(len(points), PITCH_MM * cal.px_per_mm)
        offset = 74.0
    ok = (p50 > 40) & (p50 < 72)   # only where the model was fitted
    if ok.sum() < 3:
        return None
    s = dist[ok] / p50[ok]
    s = s[np.abs(s - np.median(s)) < 0.12 * np.median(s)]  # drop diagonal or missing neighbours
    if len(s) < 3:
        return None
    return float((z_ref + offset) / float(np.median(s)) - offset)
