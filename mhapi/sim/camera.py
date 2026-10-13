"""A simulated camera: renders the bed as the Pi camera would see it from the simulated toolhead position.

The grid comes from the calibration's well table: a well whose table entry is Q appears at
    pixel = nadir + s(Z) * J @ (nozzle - Q),   s(Z) = (50 + 74) / (Z + 74)
which is the service's own model of the camera. The real toolhead strip (calibration/camera_ref.png) is
pasted at the top so tilt tracking works, and loaded wells cloud over with first-order Arrhenius kinetics.
"""
import logging
import math
import threading
import time

import cv2
import numpy as np

from ..service.calibration import TILT_STRIP

log = logging.getLogger("mhapi.sim.camera")
W, H = 1280, 720
R = 5.0                        # mosaic resolution, px per mm
Q0 = np.array([-200.0, -120.0])
Q_SIZE = (660.0, 500.0)        # mm
WALL_BGR = (176, 182, 178)
BED_BGR = (39, 8, 33)
LENS_OFFSET_MM = 74.0


class SimWorld:
    def __init__(self, cal, printer, fps=18.0, seed=1):
        self.cal, self.printer, self.fps = cal, printer, fps
        self.rng = np.random.default_rng(seed)
        self.loaded = {}              # label -> {"t", "alpha", "temp"}
        self.island_offset = {}       # island -> (dx, dy) mm: an island moved (fault)
        self.shift_px = (0.0, 0.0)    # the camera moved on its mount (fault)
        self.frozen = False           # no frames (fault)
        self.k75_per_s = math.log(2) / 150.0   # clouding half-time 2.5 min at 75 °C
        self.ea_j_mol = 250e3
        self.lock = threading.Lock()
        ref = cv2.imread(str(cal.dir / "camera_ref.png"), cv2.IMREAD_GRAYSCALE)
        self.toolhead = cv2.cvtColor(ref, cv2.COLOR_GRAY2BGR)
        self.noise = [self.rng.integers(0, 3, (H, W, 3), dtype=np.uint8) for _ in range(6)]
        self.mosaic = self._mosaic()
        self._n = 0
        threading.Thread(target=self._kinetics, name="sim-kinetics", daemon=True).start()

    # -- scene ---------------------------------------------------------------------------------------------
    def _mm_to_m(self, q):
        return (np.asarray(q, float) - Q0) * R

    def _mosaic(self):
        w, h = int(Q_SIZE[0] * R), int(Q_SIZE[1] * R)
        rng = np.random.default_rng(7)
        m = np.empty((h, w, 3), np.uint8)
        m[:] = (70, 110, 150)  # the room beyond the bed
        for _ in range(400):    # texture, so moves and Z changes are visible anywhere
            x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
            c = tuple(int(v) for v in rng.integers(30, 220, 3))
            cv2.rectangle(m, (x0, y0), (x0 + int(rng.integers(20, 160)), y0 + int(rng.integers(20, 160))), c, -1)
        b0, b1 = self._mm_to_m((0, 0)), self._mm_to_m((256, 256))
        bed = np.empty((int(b1[1] - b0[1]), int(b1[0] - b0[0]), 3), np.uint8)
        bed[:] = BED_BGR
        speck = rng.integers(0, 12, bed.shape[:2], dtype=np.uint8)
        bed[..., 0] = cv2.add(bed[..., 0], speck)
        bed[..., 2] = cv2.add(bed[..., 2], speck)
        m[int(b0[1]):int(b0[1]) + bed.shape[0], int(b0[0]):int(b0[0]) + bed.shape[1]] = bed
        islands = {}
        for label, (x, y, _) in self.cal.wells.items():
            islands.setdefault(label.split("-")[0], []).append((x, y))
        for name, pts in islands.items():
            off = np.array(self.island_offset.get(name, (0.0, 0.0)))
            pts = np.array(pts) + off
            lo, hi = pts.min(0) - 7.0, pts.max(0) + 7.0
            a, b = self._mm_to_m(lo), self._mm_to_m(hi)
            cv2.rectangle(m, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), WALL_BGR, -1)
            for p in pts:
                a, b = self._mm_to_m(p - 5.0), self._mm_to_m(p + 5.0)
                cv2.rectangle(m, (int(round(a[0])), int(round(a[1]))), (int(round(b[0])) - 1, int(round(b[1])) - 1),
                              BED_BGR, -1)
        return m

    def set_island_offset(self, island, dx, dy):
        self.island_offset[island] = (float(dx), float(dy))
        self.mosaic = self._mosaic()

    # -- egg white -----------------------------------------------------------------------------------------
    def load(self, label, t):
        with self.lock:
            self.loaded[label] = {"t": t, "alpha": 0.0, "temp": self.printer.bed}

    def clean(self):
        with self.lock:
            self.loaded.clear()

    def _kinetics(self):
        last = time.time()
        while True:
            time.sleep(0.5)
            now = time.time()
            dt, last = now - last, now
            bed = self.printer.bed
            with self.lock:
                for w in self.loaded.values():
                    w["temp"] += (bed - w["temp"]) * min(1.0, dt / 40.0)
                    tk = w["temp"] + 273.15
                    k = self.k75_per_s * math.exp(-self.ea_j_mol / 8.314 * (1 / tk - 1 / 348.15))
                    w["alpha"] += k * (1 - w["alpha"]) * dt

    # -- frames --------------------------------------------------------------------------------------------
    def render(self, t=None):
        x, y, z = self.printer.position_at(t)
        s = (50.0 + LENS_OFFSET_MM) / (z + LENS_OFFSET_MM)
        J = self.cal.J
        n = np.array([x, y])
        A = -(s / R) * J
        b = self.cal.nadir + s * J @ (n - Q0)
        frame = cv2.warpAffine(self.mosaic, np.hstack([A, b[:, None]]), (W, H), flags=cv2.INTER_LINEAR,
                               borderValue=(60, 60, 60))
        with self.lock:
            wells = {k: dict(v) for k, v in self.loaded.items()}
        half = 5.0 * self.cal.px_per_mm * s
        for label, w in wells.items():
            q = np.array(self.cal.wells[label][:2]) + np.array(self.island_offset.get(label.split("-")[0], (0, 0)))
            c = self.cal.nadir + s * J @ (n - q)
            if not (-50 < c[0] < W + 50 and -50 < c[1] < H + 50):
                continue
            g = int(22 + 190 * w["alpha"])
            cv2.rectangle(frame, (int(c[0] - half), int(c[1] - half)), (int(c[0] + half), int(c[1] + half)),
                          (int(g * 0.93), g, g), -1)
            # the LED bar's reflection on the liquid: a saturated streak, removed by the glare filter
            cv2.line(frame, (int(c[0] - 0.7 * half), int(c[1] + 0.35 * half)),
                     (int(c[0] + 0.6 * half), int(c[1] + 0.25 * half)), (255, 255, 255), 2)
        y0, y1, x0, x1 = TILT_STRIP
        frame[y0:y1, x0:x1] = self.toolhead
        dx, dy = self.shift_px
        if dx or dy:
            frame = cv2.warpAffine(frame, np.float32([[1, 0, dx], [0, 1, dy]]), (W, H), borderValue=(0, 0, 0))
        self._n += 1
        return cv2.add(frame, self.noise[self._n % len(self.noise)])

    def frames(self, stop):
        """Frame source for the service's CameraStream: yields (jpeg, arrival time)."""
        period = 1.0 / self.fps
        nxt = time.time()
        while not stop.is_set():
            nxt += period
            time.sleep(max(0.0, nxt - time.time()))
            if self.frozen:
                continue
            ok, buf = cv2.imencode(".jpg", self.render(), [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                yield buf.tobytes(), time.time()
