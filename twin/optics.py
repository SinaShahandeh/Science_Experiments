"""What the Pi camera sees of one cell, and the opacity reading the service takes from it.

The twin renders the 68 x 68 camera pixels (14 x 14 mm at Z50, 0.205 mm per pixel) around the
straight-down point, then measures them exactly as the real service does (bambu/well_gray.py):
drop saturated pixels and a 2 px halo, drop bright outliers above median + 3 MAD, take the median.
So glare, bubbles and lighting changes affect the twin's readings the way they affect the rig's.

Appearance, from the rig's frames (2026-10-11 trial, 2026-10-12 calibration):
- bare bed (empty well) about RGB (30, 8, 35), gray ~19, with faint rainbow lines from the plate;
- PETG wall tops 166-199 gray, seen ~9% larger than the floor because they are 10 mm closer;
- clear egg white dark, clouding through blue-gray to an opaque pale mint;
- the liquid surface reflects the LED bar as a thin saturated streak that is fixed per well
  (in P3-B2 it crosses the measured square).
"""
import hashlib
import math

import cv2
import numpy as np

from .geometry import MM_PER_PX, TOP_SCALE, LENS_ABOVE_BED

CROP = 68           # camera px: the 14 x 14 mm cell image
BOX = 34            # camera px: the 7 x 7 mm measured square
ENLARGE = 4         # the agent's PNG is 272 x 272 px
LIQUID_MM = 3.0     # 0.3 mL in a 10 mm opening
BED_RGB = np.array([30.0, 8.0, 35.0])
WALL_RGB = np.array([186.0, 181.0, 193.0])
# clear -> blue-gray -> opaque pale mint (trial frames); luminance is set separately from the model
LIQ_STOPS = [(0.00, (31, 12, 37)), (0.25, (54, 82, 94)), (0.55, (104, 140, 142)), (1.00, (180, 203, 190))]
LUMA = np.array([0.299, 0.587, 0.114])

_c = np.arange(CROP) - (CROP - 1) / 2.0
_U, _V = np.meshgrid(_c, _c)  # x right, y down (image), in px from the crop center


def _rng(label, salt=""):
    return np.random.default_rng(int(hashlib.md5((label + salt).encode()).hexdigest()[:8], 16))


def _smooth_noise(rng, scale_px, amp):
    n = rng.normal(0, 1, (CROP, CROP)).astype(np.float32)
    n = cv2.GaussianBlur(n, (0, 0), scale_px)
    return (n / (n.std() + 1e-6) * amp).astype(np.float32)


def _bezier_dist(fx, fy, p0, p1, p2, n=40):
    t = np.linspace(0, 1, n)[:, None]
    pts = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2
    d = np.full(fx.shape, 1e9, np.float32)
    for x, y in pts:
        d = np.minimum(d, np.hypot(fx - x, fy - y))
    return d


class CellLook:
    """The fixed, per-well parts of a cell's appearance (seeded by the label, like a real rig's quirks)."""

    def __init__(self, label):
        r = _rng(label)
        self.label = label
        a = r.uniform(0, 2 * math.pi)
        self.grad = (math.cos(a), math.sin(a))
        self.grad_amp = r.uniform(0.03, 0.07)
        self.texture = _smooth_noise(r, 3.0, 1.0)
        self.fine = _smooth_noise(r, 1.2, 1.0)
        # LED reflection on the liquid surface: a short curved streak, fixed per well
        self.glint = None
        if label == "P3-B2" or r.random() < 0.55:
            if label == "P3-B2":  # spec: crosses the measured area in every frame
                p0, p1, p2 = np.array([-2.6, 1.9]), np.array([0.3, 0.6]), np.array([2.4, -1.6])
            else:  # meniscus reflections run roughly along a wall, a few mm long
                side = r.integers(0, 4)
                along, off = r.uniform(-2.0, 2.0), r.uniform(2.6, 4.3)
                ln = r.uniform(2.5, 5.5)
                ang = side * math.pi / 2 + r.normal(0, 0.25)
                n = np.array([math.cos(ang), math.sin(ang)])            # toward the wall
                tdir = np.array([-n[1], n[0]])                           # along the wall
                c = n * off + tdir * along
                p0, p2 = c - tdir * ln / 2, c + tdir * ln / 2
                p1 = c + n * r.uniform(-0.6, 0.9)                        # bowed like the meniscus
            self.glint = (p0, p1, p2, r.uniform(0.12, 0.2))
        # rainbow lines of the plate seen through an empty or clear well
        self.holo = []
        for _ in range(r.integers(0, 3)):
            ang = math.pi / 4 + (math.pi / 2) * r.integers(0, 2) + r.normal(0, 0.03)
            off = r.uniform(-4.5, 4.5)
            hue = r.uniform(0, 1)
            self.holo.append((ang, off, hue, r.uniform(0.3, 0.9)))


def _hue_rgb(h):
    return np.array([max(0.0, math.cos(2 * math.pi * (h - k / 3))) for k in range(3)])


def liquid_chroma(w):
    """Unit-luminance color of the liquid at brightness fraction w."""
    for (w0, c0), (w1, c1) in zip(LIQ_STOPS, LIQ_STOPS[1:]):
        if w <= w1:
            f = 0.0 if w1 == w0 else (w - w0) / (w1 - w0)
            c = np.array(c0, float) * (1 - f) + np.array(c1, float) * f
            break
    else:
        c = np.array(LIQ_STOPS[-1][1], float)
    return c / float(LUMA @ c)


def render_cell(look, *, loaded, w, gray_level, light=1.0, offset_px=(0.0, 0.0), bubble=None, rng=None):
    """RGB uint8 crop (68 x 68) of one cell.

    loaded: liquid present. w: brightness fraction 0..1. gray_level: the liquid's luminance (0-255)
    before lighting. light: LED x room-light factor. offset_px: where the cell's center sits relative to
    the straight-down pixel (positioning error and camera tilt). bubble: (x_mm, y_mm, r_mm) or None.
    """
    rng = rng or np.random.default_rng()
    fx = ((_U - offset_px[0]) * MM_PER_PX).astype(np.float32)   # floor plane, mm from the cell center
    fy = ((_V - offset_px[1]) * MM_PER_PX).astype(np.float32)
    tx, ty = fx / TOP_SCALE, fy / TOP_SCALE                        # wall-top plane
    cheb_f = np.maximum(np.abs(fx), np.abs(fy))
    cheb_t = np.maximum(np.abs(tx), np.abs(ty))
    wall_top = cheb_t >= 5.0
    floor = cheb_f < 5.0
    inner = ~wall_top & ~floor

    img = np.zeros((CROP, CROP, 3), np.float32)
    lit = (1.0 + look.grad_amp * (look.grad[0] * fx + look.grad[1] * fy) / 5.0)[..., None]

    # wall tops: matte white PETG with faint print texture
    wt = WALL_RGB * (1.0 + 0.012 * look.fine[..., None])
    img[wall_top] = wt[wall_top]
    # inner wall faces between the top edge and the floor: shaded, darker toward the bottom
    depth = np.clip((cheb_f - 5.0) / (5.0 * TOP_SCALE - 5.0 + 1e-6), 0, 1)[..., None]
    face = WALL_RGB * (0.28 + 0.32 * depth)
    img[inner] = face[inner]

    # floor: bare bed with faint rainbow diffraction lines
    bed = np.broadcast_to(BED_RGB, img.shape).copy()
    for ang, off, hue, strength in look.holo:
        d = np.abs(fx * math.sin(ang) - fy * math.cos(ang) - off)
        line = np.clip(1.0 - d / 0.18, 0, 1)[..., None]
        bed += line * strength * 70.0 * _hue_rgb(hue)
    if loaded:
        chroma = liquid_chroma(w)
        base = chroma * gray_level
        edge = np.clip((cheb_f - 3.4) / 1.6, 0, 1)                   # meniscus and wall shadow
        shade = 1.0 - (0.10 + 0.18 * w) * edge ** 1.5
        tex = 1.0 + w * (0.035 * look.texture + 0.015 * look.fine)    # set egg white is not uniform
        liq = base * (shade * tex)[..., None]
        see_through = (1.0 - min(1.0, w / 0.35)) ** 2                 # the plate shows through clear egg
        liq = liq + see_through * np.clip(bed - BED_RGB, 0, None)
        if bubble is not None:
            bx, by, br = bubble
            dd = np.hypot(fx - bx, fy - by)
            rim = np.exp(-((dd - br) / 0.12) ** 2)[..., None]
            inside = (dd < br)[..., None]
            liq = np.where(inside, liq * 0.9 + 70.0, liq) + rim * 150.0
        img[floor] = liq[floor]
    else:
        img[floor] = bed[floor]

    img *= lit * light
    img = cv2.GaussianBlur(img, (0, 0), 0.55)
    if loaded and look.glint is not None:  # specular: sharp and saturated, added after the lens blur
        p0, p1, p2, width = look.glint
        d = _bezier_dist(fx, fy, p0, p1, p2)
        core = (d < width) & floor
        halo = (np.clip(1.0 - (d - width) / 0.3, 0, 1) ** 1.5)[..., None] * floor[..., None]
        img = img * (1 - halo) + np.array([250.0, 246.0, 238.0]) * halo * min(1.0, light)
        img[core] = 255.0
    img += rng.normal(0, 1.7, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def well_gray(rgb, sat=250, halo=2, k=3.0):
    """The service's opacity reading on the centred 7 x 7 mm square (port of bambu/well_gray.py)."""
    h = BOX // 2
    c = CROP // 2
    roi = rgb[c - h:c + h, c - h:c + h].astype(np.float32)
    gray = roi @ LUMA
    satm = roi.max(axis=2) >= sat
    n = gray.size
    if satm.all():
        return dict(gray=255.0, pixels_total=n, pixels_used=0, saturated_px=n, reflection_px=0, all_saturated=True)
    bad = cv2.dilate(satm.astype(np.uint8), np.ones((2 * halo + 1,) * 2, np.uint8)) > 0 if halo else satm
    if bad.all():
        bad = satm
    v = gray[~bad]
    med = np.median(v)
    mad = 1.4826 * np.median(np.abs(v - med))
    glint = gray > med + k * max(mad, 1.0)
    keep = ~bad & ~glint
    return dict(gray=round(float(np.median(gray[keep])), 1), pixels_total=int(n), pixels_used=int(keep.sum()),
                saturated_px=int(satm.sum()), reflection_px=int((glint & ~bad).sum()), all_saturated=False)


def wall_gray(rgb, offset_px=(0.0, 0.0)):
    """Median gray of the wall tops in the band 6-6.8 mm (bed scale) from the cell's center."""
    fx = (_U - offset_px[0]) * MM_PER_PX
    fy = (_V - offset_px[1]) * MM_PER_PX
    cheb = np.maximum(np.abs(fx), np.abs(fy))
    band = (cheb >= 6.0) & (cheb <= 6.8)
    g = rgb.astype(np.float32) @ LUMA
    return round(float(np.median(g[band])), 1)


def agent_png(rgb):
    """The 272 x 272 image the agent sees: 4x nearest-neighbour, measured square outlined in green."""
    big = np.repeat(np.repeat(rgb, ENLARGE, axis=0), ENLARGE, axis=1).copy()
    a = (CROP - BOX) // 2 * ENLARGE
    b = a + BOX * ENLARGE - 1
    green = np.array([0, 255, 0], np.uint8)
    big[a:a + 2, a:b + 1] = green
    big[b - 1:b + 1, a:b + 1] = green
    big[a:b + 1, a:a + 2] = green
    big[a:b + 1, b - 1:b + 1] = green
    return big


def save_png(path, rgb):
    cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def surface_scale():
    """How much larger the liquid surface looks than the floor (3 mm closer to the lens)."""
    return LENS_ABOVE_BED / (LENS_ABOVE_BED - LIQUID_MM)
