"""Measure image-pixel motion per mm of printer X and Y at the current Z."""
import json, sys
import numpy as np
from motion import Gantry, load_pos
from pi_frame import pi_frame
from wells import detect

D = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
CAL = "../calibration"


def centers(path):
    _, h = detect(path)
    med = np.median([x["area"] for x in h])
    return np.array([[x["cx"], x["cy"]] for x in h if 0.6 * med < x["area"] < 1.6 * med])


def shift(a, b):
    """Median displacement from a to b, matching each point to its nearest neighbour."""
    d = []
    for p in a:
        j = np.argmin(np.linalg.norm(b - p, axis=1))
        d.append(b[j] - p)
    d = np.array(d)
    m = np.median(d, axis=0)
    good = np.linalg.norm(d - m, axis=1) < 5
    return d[good].mean(axis=0), int(good.sum())


g = Gantry()
try:
    p0 = load_pos()
    z = p0["z"]
    pi_frame(f"{CAL}/jac_0.jpg"); c0 = centers(f"{CAL}/jac_0.jpg")
    g.move(x=p0["x"] + D); pi_frame(f"{CAL}/jac_x.jpg"); cx = centers(f"{CAL}/jac_x.jpg")
    g.move(x=p0["x"])
    g.move(y=p0["y"] + D); pi_frame(f"{CAL}/jac_y.jpg"); cy = centers(f"{CAL}/jac_y.jpg")
    g.move(y=p0["y"])
    pi_frame(f"{CAL}/jac_back.jpg"); cb = centers(f"{CAL}/jac_back.jpg")
finally:
    g.close()

dx, nx = shift(c0, cx); dy, ny = shift(c0, cy); db, nb = shift(c0, cb)
J = np.column_stack([dx / D, dy / D])  # px per mm: columns = d(px)/dX, d(px)/dY
print("px/mm per +X:", dx / D, f"(n={nx})")
print("px/mm per +Y:", dy / D, f"(n={ny})")
print("return error px:", db, f"(n={nb})")
json.dump({"z": z, "J": J.tolist()}, open(f"{CAL}/jacobian_z{int(z)}.json", "w"), indent=1)
