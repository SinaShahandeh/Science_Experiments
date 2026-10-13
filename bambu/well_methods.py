"""Candidate ways to turn a well's pixels into one gray value (for comparison)."""
import cv2
import numpy as np
from sklearn.mixture import GaussianMixture

SAT = 250


def snap(img, c, side=46, search=8):
    """Shift a side x side square around c so it contains the fewest wall pixels (white PETG is saturated)."""
    wall = (img.min(axis=2) >= 245).astype(np.uint8)
    ii = cv2.integral(wall)
    best = None
    cx, cy = int(round(c[0])), int(round(c[1]))
    h = side // 2
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            x0, y0 = cx + dx - h, cy + dy - h
            s = ii[y0 + side, x0 + side] - ii[y0, x0 + side] - ii[y0 + side, x0] + ii[y0, x0]
            key = (s, dx * dx + dy * dy)
            if best is None or key < best[0]:
                best = (key, (cx + dx, cy + dy))
    return best[1]


def pixels(img, c, side):
    cx, cy = int(round(c[0])), int(round(c[1]))
    h = side // 2
    roi = img[cy - h:cy - h + side, cx - h:cx - h + side]
    return cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(float), roi.max(axis=2) >= SAT


def drop_sat(gray, satm, halo=2):
    if satm.all():
        return None
    bad = cv2.dilate(satm.astype(np.uint8), np.ones((2 * halo + 1,) * 2, np.uint8)) > 0
    if bad.all():
        bad = satm
    return gray[~bad]


def median_all(gray, satm):
    return float(np.median(gray))


def mad_high(gray, satm, k=3.0):
    v = drop_sat(gray, satm)
    if v is None:
        return 255.0
    med = np.median(v)
    mad = 1.4826 * np.median(np.abs(v - med))
    return float(np.median(v[v <= med + k * max(mad, 1.0)]))


def mode_valley(gray, satm, sigma=4.0, floor=0.05):
    """Main histogram mode, cut at the valleys (or where it falls below `floor` of the peak) on each side."""
    v = drop_sat(gray, satm)
    if v is None:
        return 255.0
    h = np.bincount(np.clip(v, 0, 255).astype(int), minlength=256).astype(float)
    x = np.arange(-15, 16)
    hs = np.convolve(h, np.exp(-0.5 * (x / sigma) ** 2), mode="same")
    pk = int(np.argmax(hs))
    lo = pk
    while lo > 0 and hs[lo - 1] <= hs[lo] and hs[lo - 1] > floor * hs[pk]:
        lo -= 1
    hi = pk
    while hi < 255 and hs[hi + 1] <= hs[hi] and hs[hi + 1] > floor * hs[pk]:
        hi += 1
    sel = v[(v >= lo) & (v <= hi)]
    return float(np.median(sel)) if sel.size else float(np.median(v))


def gmm_main(gray, satm, kmax=3):
    v = drop_sat(gray, satm)
    if v is None:
        return 255.0
    X = v.reshape(-1, 1)
    if np.ptp(v) < 3:
        return float(np.median(v))
    best = min((GaussianMixture(k, random_state=0).fit(X) for k in range(1, kmax + 1)), key=lambda m: m.bic(X))
    lab = best.predict(X)
    main = np.argmax(best.weights_)
    return float(np.median(v[lab == main]))


METHODS = {  # name: (side px, snap?, fn)
    "box24_median": (24, False, median_all),
    "box24_madhigh": (24, False, mad_high),
    "box34_madhigh": (34, False, mad_high),
    "well46_madhigh": (46, True, mad_high),
    "well46_modevalley": (46, True, mode_valley),
    "well46_gmm": (46, True, gmm_main),
}


def measure(img, c, methods=METHODS):
    out = {}
    cs = None
    for name, (side, sn, fn) in methods.items():
        cc = c
        if sn:
            cs = cs or snap(img, c)
            cc = cs
        g, s = pixels(img, cc, side)
        out[name] = fn(g, s)
    return out, cs


def mad_both(gray, satm, k_hi=3.0, k_lo=2.0):
    """Drop saturated (+halo), bright glints (> med + k_hi*MAD) and dim unfilled/shadow edge (< med - k_lo*MAD)."""
    v = drop_sat(gray, satm)
    if v is None:
        return 255.0
    med = np.median(v)
    mad = max(1.4826 * np.median(np.abs(v - med)), 1.0)
    keep = v[(v <= med + k_hi * mad) & (v >= med - k_lo * mad)]
    return float(np.median(keep))


def trim_low(gray, satm, q=25):
    """Drop saturated (+halo) and bright glints, then the dimmest q% (unfilled/shadow rim)."""
    v = drop_sat(gray, satm)
    if v is None:
        return 255.0
    med = np.median(v)
    mad = max(1.4826 * np.median(np.abs(v - med)), 1.0)
    v = v[v <= med + 3 * mad]
    return float(np.median(v[v >= np.percentile(v, q)]))


METHODS.update({
    "well46_madboth": (46, True, mad_both),
    "well46_trimlow25": (46, True, trim_low),
})
