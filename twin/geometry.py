"""Where everything is: wells, islands, the camera and its lens.

Coordinates are the printer's, in mm. "Command" XY is the nozzle position the well table gives for a
label (calibration/well_positions_fit.json): it puts the camera straight above the well at Z50. "Bed" XY
is a point on the plate. The camera's straight-down point sits CAMERA_OFFSET from the nozzle, so a well's
bed position is its command position plus that offset (Model_Hardware_API_spec.md, "What a position
means"). The twin uses the real calibration, so commanding a label's table entry centers that well.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

from mhapi.tools import LABELS

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "calibration"

CAMERA_OFFSET = (3.0, -33.0)  # straight-down point relative to the nozzle (spec estimate)
CAPTURE_Z = 50.0
LENS_ABOVE_BED = 124.6        # at Z50; from the 0.969 scale change for +4 mm in calibration/nadir.json
LENS_ABOVE_NOZZLE = LENS_ABOVE_BED - CAPTURE_Z
FRAME_PX = (1280, 720)
NADIR_PX = (690.5, 362.0)     # calibration/nadir.json, 2026-10-12
F_PX = 600.0                  # equidistant fisheye focal length; fits the island positions in verify_C-A1.jpg
MM_PER_PX = 0.205             # at the bed, Z50 (4.84 / 4.93 px per mm in jacobian_z50.json)
PITCH, WALL, OPENING, WALL_H = 12.0, 2.0, 10.0, 10.0
BED_MM = 256.0
PARK_XY = (128.0, 254.0)      # bare steel behind the grid, where Z is homed
TOP_SCALE = LENS_ABOVE_BED / (LENS_ABOVE_BED - WALL_H)  # wall tops are 10 mm closer: they look ~9% larger


@lru_cache(maxsize=1)
def _fit():
    return json.loads((CAL / "well_positions_fit.json").read_text())


@lru_cache(maxsize=1)
def well_table():
    """{label: (x, y)} nozzle XY that centers the camera over the well at Z50."""
    w = _fit()["wells"]
    return {lab: (float(w[lab]["x"]), float(w[lab]["y"])) for lab in LABELS}


def bed_xy(label):
    x, y = well_table()[label]
    return x + CAMERA_OFFSET[0], y + CAMERA_OFFSET[1]


def nadir_bed_xy(nozzle_x, nozzle_y):
    """The bed point straight below the lens for a nozzle position."""
    return nozzle_x + CAMERA_OFFSET[0], nozzle_y + CAMERA_OFFSET[1]


def island_of(label):
    return label.split("-")[0]


def row_col(label):
    rc = label.split("-")[1]
    return "ABCD".index(rc[0]), int(rc[1]) - 1


@lru_cache(maxsize=1)
def islands():
    """{name: {n, center: (x, y) bed mm, rotation_deg, size, wells: [labels]}}."""
    fit = _fit().get("islands", {})
    out = {}
    for name in ("C", "P1", "P2", "P3", "P4"):
        labs = [lab for lab in LABELS if island_of(lab) == name]
        n = 4 if name == "C" else 2
        pts = [bed_xy(lab) for lab in labs]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        out[name] = {"n": n, "center": (cx, cy), "rotation_deg": float(fit.get(name, {}).get("rotation_deg", 0.0)),
                     "size": n * PITCH + WALL, "wells": labs}
    return out


def is_corner_island(label):
    return island_of(label) != "C"


def geometry_json():
    """Everything the dashboard needs to build the 3D scene and the camera view."""
    return {
        "labels": list(LABELS),
        "wells": {lab: {"command": well_table()[lab], "bed": bed_xy(lab)} for lab in LABELS},
        "islands": {k: {**v, "center": list(v["center"])} for k, v in islands().items()},
        "camera_offset": CAMERA_OFFSET, "capture_z": CAPTURE_Z, "lens_above_bed": LENS_ABOVE_BED,
        "lens_above_nozzle": LENS_ABOVE_NOZZLE, "frame_px": FRAME_PX, "nadir_px": NADIR_PX, "f_px": F_PX,
        "mm_per_px": MM_PER_PX, "pitch": PITCH, "wall": WALL, "opening": OPENING, "wall_h": WALL_H,
        "bed_mm": BED_MM, "park_xy": PARK_XY,
        "tilt_rad": math.atan2(NADIR_PX[0] - FRAME_PX[0] / 2, F_PX),
    }
