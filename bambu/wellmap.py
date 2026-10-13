"""Single source of truth for where to send the camera: calibration/well_positions_fit.json + nadir.json.

wells()  -> {label: (x, y)} nozzle XY (mm) that puts the camera straight above the well, at Z_IMG
nadir()  -> pixel straight below the camera (reference camera pose, calibration/camera_ref.png);
            add cam_tilt.tilt_px(...) to get it in a later frame if the mount has moved.
"""
import json
from pathlib import Path
import numpy as np

CAL = Path(__file__).resolve().parent.parent / "calibration"
Z_IMG = 50.0


def wells():
    w = json.loads((CAL / "well_positions_fit.json").read_text())["wells"]
    return {k: (v["x"], v["y"]) for k, v in w.items()}


def nadir():
    n = json.loads((CAL / "nadir.json").read_text())
    if "nadir_px" in n:  # final value (from the per-well up/down check)
        return np.array(n["nadir_px"], float)
    return np.mean([n[k]["nadir_px"] for k in ("58", "62", "66", "70")], axis=0)
