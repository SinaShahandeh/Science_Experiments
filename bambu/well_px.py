"""Where each well appears in a frame, given the camera (nozzle) position: linear Jacobian model at Z50."""
import json
from pathlib import Path
import numpy as np

CAL = Path(__file__).resolve().parent.parent / "calibration"
J = np.array(json.loads((CAL / "jacobian_z50.json").read_text())["J"])  # px per mm (cols: dX, dY)
NADIR = np.array([743.0, 350.0])


def well_px(cam_xy, well_xy):
    """Pixel of the well centre (its table entry is where the camera must be to put it at the nadir)."""
    return NADIR + J @ (np.asarray(cam_xy, float) - np.asarray(well_xy, float))
