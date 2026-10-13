"""The calibration in force: well table, straight-down pixel, Jacobian, tilt reference, camera settings.

Files in calibration/ (see the API spec's Recalibration section). The service refuses to start without
them. `calibration_id` changes whenever any of them changes.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

# Toolhead strip used for tilt tracking: y0, y1, x0, x1 (bambu/cam_tilt.py STRIP)
TILT_STRIP = (0, 110, 410, 950)
# Files whose content defines calibration_id: positions, straight-down pixel, image scale, tilt reference,
# camera settings.
ID_FILES = ("well_positions_fit.json", "nadir.json", "jacobian_z50.json", "camera_ref.png",
            "exposure/result.json")


class CalibrationError(Exception):
    pass


class Calibration:
    def __init__(self, cal_dir):
        self.dir = Path(cal_dir)
        missing = [f for f in ID_FILES if not (self.dir / f).exists()]
        if missing:
            raise CalibrationError(f"calibration files missing in {self.dir}: {', '.join(missing)}")
        try:
            fit = json.loads((self.dir / "well_positions_fit.json").read_text())["wells"]
            self.wells = {k: (float(v["x"]), float(v["y"]), float(v.get("z", 50.0))) for k, v in fit.items()}
            n = json.loads((self.dir / "nadir.json").read_text())
            self.nadir = np.array(n["nadir_px"], float)
            self.J = np.array(json.loads((self.dir / "jacobian_z50.json").read_text())["J"], float)
            self.camera_settings = json.loads((self.dir / "exposure" / "result.json").read_text()).get("params", {})
        except (KeyError, ValueError, TypeError) as e:
            raise CalibrationError(f"calibration file unreadable: {e!r}") from None
        ref = cv2.imread(str(self.dir / "camera_ref.png"), cv2.IMREAD_GRAYSCALE)
        y0, y1, x0, x1 = TILT_STRIP
        if ref is None or ref.shape != (y1 - y0, x1 - x0):
            raise CalibrationError("calibration/camera_ref.png is not the toolhead strip "
                                   f"({y1 - y0} x {x1 - x0} px); re-run `cam_tilt.py ref`")
        self.tilt_ref = ref.astype(np.float32)
        self.tilt_window = cv2.createHanningWindow(self.tilt_ref.shape[::-1], cv2.CV_32F)
        # px per mm at the wells: |dpx/dX| and |dpy/dY| (≈ 4.84, 4.93), and their mean (≈ 4.88)
        self.px_per_mm_xy = np.abs(np.diag(self.J))
        self.px_per_mm = float(self.px_per_mm_xy.mean())
        if not (3.0 < self.px_per_mm < 8.0):
            raise CalibrationError(f"implausible image scale {self.px_per_mm:.2f} px/mm in jacobian_z50.json")
        self.pitch_model = self._pitch_model()
        self.id = self._id()

    def _pitch_model(self):
        """Hole pitch (px) at Z50 as a function of image position; see bambu/fit_pitch.py."""
        p = self.dir / "pitch_z50.json"
        if p.exists():
            d = json.loads(p.read_text())
            return {"coef": np.array(d["coef"], float), "lens_offset_mm": float(d.get("lens_offset_mm", 74.0))}
        return None  # fall back to a constant pitch of 12 mm * px_per_mm

    def _id(self):
        h = hashlib.sha256()
        newest = 0.0
        for f in ID_FILES:
            p = self.dir / f
            h.update(f.encode() + b"\0" + p.read_bytes())
            newest = max(newest, p.stat().st_mtime)
        return time.strftime("%Y-%m-%dT%H:%M", time.localtime(newest)) + "_" + h.hexdigest()[:6]

    @property
    def labels(self):
        return list(self.wells)

    def mm_from_px(self, d_px):
        """Bed-scale mm for an image displacement (per image axis)."""
        return np.asarray(d_px, float) / self.px_per_mm_xy

    def nozzle_correction_mm(self, err_px):
        """Nozzle move (mm) that would bring a feature err_px from the straight-down pixel onto it."""
        return np.linalg.solve(self.J, -np.asarray(err_px, float))

    def as_record(self):
        return {"calibration_id": self.id, "dir": str(self.dir),
                "wells": {k: {"x": x, "y": y, "z": z} for k, (x, y, z) in self.wells.items()},
                "nadir_px": self.nadir.tolist(), "jacobian_z50": self.J.tolist(),
                "tilt_reference": {"file": "camera_ref.png", "strip_y0_y1_x0_x1": list(TILT_STRIP),
                                   "sha256": hashlib.sha256((self.dir / "camera_ref.png").read_bytes()).hexdigest()},
                "camera_settings": self.camera_settings,
                "pitch_model": None if self.pitch_model is None else
                {"coef": self.pitch_model["coef"].tolist(), "lens_offset_mm": self.pitch_model["lens_offset_mm"]}}

    def write_copy(self, folder):
        folder = Path(folder)
        (folder / "calibration.json").write_text(json.dumps(self.as_record(), indent=1))
        shutil.copyfile(self.dir / "camera_ref.png", folder / "camera_ref.png")
