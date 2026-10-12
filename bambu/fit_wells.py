"""Fit an exact 12 mm lattice (rigid: rotation + translation) to each island's measured positions.

Writes calibration/well_positions_fit.json and prints a markdown table for the API spec.
"""
import json
import numpy as np
from calibrate_wells import NOMINAL

CAL = "../calibration"
meas = json.load(open(f"{CAL}/well_positions.json"))
islands = sorted({k.split("-")[0] for k in meas}, key=lambda s: (s != "C", s))
fit, report = {}, {}
for isl in islands:
    labels = [k for k in NOMINAL if k.startswith(isl + "-")]
    nom = np.array([NOMINAL[k] for k in labels])
    xy = np.array([meas[k]["xy"] for k in labels])
    a, b = nom - nom.mean(0), xy - xy.mean(0)
    U, _, Vt = np.linalg.svd(a.T @ b)
    R = (U @ Vt).T
    if np.linalg.det(R) < 0:
        raise ValueError("reflection in fit")
    pred = a @ R.T + xy.mean(0)
    res = np.linalg.norm(pred - xy, axis=1)
    rot = np.degrees(np.arctan2(R[1, 0], R[0, 0]))
    report[isl] = {"rotation_deg": round(float(rot), 2), "rms_mm": round(float(np.sqrt((res ** 2).mean())), 2),
                   "max_mm": round(float(res.max()), 2), "offset_from_nominal_mm": (xy.mean(0) - nom.mean(0)).round(2).tolist()}
    for k, p in zip(labels, pred):
        fit[k] = {"x": round(float(p[0]), 2), "y": round(float(p[1]), 2), "z": meas[k]["z"],
                  "measured_xy": meas[k]["xy"]}
json.dump({"wells": fit, "islands": report}, open(f"{CAL}/well_positions_fit.json", "w"), indent=1)
print(json.dumps(report, indent=1))
for isl in islands:
    print(f"\n| Label | X (mm) | Y (mm) | Z (mm) |\n|---|---|---|---|")
    for k in (k for k in NOMINAL if k.startswith(isl + "-")):
        print(f"| `{k}` | {fit[k]['x']:.2f} | {fit[k]['y']:.2f} | {fit[k]['z']:.0f} |")
