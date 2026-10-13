"""Center the Pi camera on each well in turn and record the nozzle X/Y at that point.

Model: pixel = c - J @ (bed - V), V = nozzle + offset (the bed point at the image center).
J (px per mm of nozzle motion) comes from jacobian.py at the working Z.
"""
import json, sys, time
import numpy as np
import cv2
from motion import Gantry, load_pos
from pi_frame import pi_frame
from wells import detect
from cam_tilt import tilt_of_file

CAL = "../calibration"
Z = 50.0
TOL_PX = 0.8  # ~0.16 mm at Z50
J = np.array(json.load(open(f"{CAL}/jacobian_z{int(Z)}.json"))["J"])
# Target pixel = nadir (point straight below the camera, from nadir.py), not the image centre:
# the camera is tilted ~10 deg, so centring in the frame would put it ~21 mm off vertical.
try:  # nadir.py imports holes() from here before nadir.json exists
    import wellmap
    C = wellmap.nadir()
    open(f"{CAL}/nadir.json").close()
except FileNotFoundError:
    C = np.array([640.0, 360.0])
# bed point under the camera minus nozzle XY: image-centre estimate from P4, moved to the nadir
CAM_OFF_GUESS = tuple(np.array([-22.0, -40.0]) - np.linalg.solve(J, C - np.array([640.0, 360.0])))

# Nominal bed centres (mm) from make_grid.py, shifted so the grid spans 63-193 as printed.
OFF = 48.5
def island(name, ox, oy, n):
    rows = "ABCD"[:n]
    out = {}
    for r, row in enumerate(rows):          # row A = back (highest Y)
        for col in range(n):               # col 1 = left (lowest X)
            out[f"{name}-{row}{col + 1}"] = (ox + OFF + 7 + 12 * col, oy + OFF + 7 + 12 * (n - 1 - r))
    return out

NOMINAL = {}
NOMINAL.update(island("P4", 14.5, 14.5, 2))   # front-left
NOMINAL.update(island("C", 54.5, 54.5, 4))    # centre
NOMINAL.update(island("P3", 118.5, 14.5, 2))  # front-right
NOMINAL.update(island("P2", 118.5, 118.5, 2)) # back-right
NOMINAL.update(island("P1", 14.5, 118.5, 2))  # back-left

ORDER = (["P4-B1", "P4-B2", "P4-A2", "P4-A1"] +
         ["C-D1", "C-D2", "C-D3", "C-D4", "C-C4", "C-C3", "C-C2", "C-C1",
          "C-B1", "C-B2", "C-B3", "C-B4", "C-A4", "C-A3", "C-A2", "C-A1"] +
         ["P3-B1", "P3-B2", "P3-A2", "P3-A1"] +
         ["P2-B1", "P2-B2", "P2-A2", "P2-A1"] +
         ["P1-B2", "P1-B1", "P1-A1", "P1-A2"])


def holes(path):
    _, h = detect(path)
    if not h:
        return np.zeros((0, 2))
    med = np.median([x["area"] for x in h])
    return np.array([[x["cx"], x["cy"]] for x in h if 0.4 * med < x["area"] < 4.0 * med])


def center_on(g, label, nozzle_guess, pitch_px):
    """Iteratively centre on the hole nearest the predicted pixel. Returns final nozzle XY."""
    pos = load_pos()
    g.move(x=nozzle_guess[0], y=nozzle_guess[1])
    target_px = C.copy()  # after the guess move the well should be near the centre
    for it in range(8):
        f = f"{CAL}/frames/{label}_{it}.jpg"
        time.sleep(0.5)
        pi_frame(f)
        h = holes(f)
        if len(h) == 0:
            raise RuntimeError(f"{label}: no holes detected")
        d = np.linalg.norm(h - target_px, axis=1)  # (identity check only; tilt is < 1 px)
        j = int(np.argmin(d))
        if d[j] > 0.45 * pitch_px:
            raise RuntimeError(f"{label}: nearest hole {d[j]:.0f}px from prediction (pitch {pitch_px:.0f}px)")
        Ct = C + tilt_of_file(f)  # nadir in this frame: reference nadir + camera tilt since the reference
        err = h[j] - Ct
        pos = load_pos()
        print(f"  {label} it{it}: nozzle ({pos['x']:.2f},{pos['y']:.2f}) err px ({err[0]:+.1f},{err[1]:+.1f})")
        if np.linalg.norm(err) < TOL_PX:
            img = cv2.imread(f)
            cv2.drawMarker(img, tuple(int(v) for v in C), (0, 0, 255), cv2.MARKER_CROSS, 40, 2)
            cv2.putText(img, "nadir", (int(C[0]) + 8, int(C[1]) + 24), 0, 0.6, (0, 0, 255), 2)
            cv2.circle(img, (int(h[j][0]), int(h[j][1])), 5, (0, 255, 0), -1)
            cv2.putText(img, label, (int(h[j][0]) + 8, int(h[j][1]) - 8), 0, 0.8, (0, 255, 255), 2)
            cv2.imwrite(f"{CAL}/centered/{label}.jpg", img)
            return np.array([pos["x"], pos["y"]]), err, Ct - C
        # the hole should move by -err: J @ d = -err
        d_mm = np.linalg.solve(J, -err)
        g.move(x=round(pos["x"] + d_mm[0], 2), y=round(pos["y"] + d_mm[1], 2))
        target_px = C  # once corrected, the same hole should be close to the centre
    raise RuntimeError(f"{label}: did not converge")


def main():
    import os
    os.makedirs(f"{CAL}/frames", exist_ok=True); os.makedirs(f"{CAL}/centered", exist_ok=True)
    out_path = f"{CAL}/well_positions.json"
    results = json.load(open(out_path)) if os.path.exists(out_path) else {}
    only = sys.argv[1:] or ORDER
    pitch_px = 12 * np.mean(np.abs(np.diag(J)))

    # Initial camera offset (bed point at image centre minus nozzle) from the centre island
    # in the current frame: its 16 holes average to nominal (128, 128).
    g = Gantry()
    try:
        assert g.homed(), "not homed"
        pos = load_pos()
        if abs(pos["z"] - Z) > 0.01:
            g.move(z=Z)
        seed = results or (json.load(open(os.environ["SEED"])) if os.environ.get("SEED") else {})
        if seed:  # SEED=old well_positions.json: starting guesses only, never written back
            offs = np.array([np.array(NOMINAL[k]) - np.array(v["xy"]) for k, v in seed.items()])
            cam_off = offs.mean(axis=0)
        else:
            # bed point at the image centre minus nozzle XY, measured from P4 at Z50
            cam_off = np.array(CAM_OFF_GUESS)
        island_off = {}
        for label in only:
            isl = label.split("-")[0]
            nom = np.array(NOMINAL[label])
            off = island_off.get(isl, cam_off)
            guess = (nom - off).round(2)
            print(f"{label}: nominal bed {tuple(nom)}, nozzle guess {tuple(guess)}")
            xy, err, tilt = center_on(g, label, guess, pitch_px)
            island_off[isl] = nom - xy
            results[label] = {"xy": [round(float(xy[0]), 2), round(float(xy[1]), 2)], "z": Z,
                              "residual_px": [round(float(err[0]), 2), round(float(err[1]), 2)],
                              "tilt_px": [round(float(tilt[0]), 2), round(float(tilt[1]), 2)]}
            json.dump(results, open(out_path, "w"), indent=1)
            print(f"  -> {label} at X{xy[0]:.2f} Y{xy[1]:.2f}")
    finally:
        g.close()


if __name__ == "__main__":
    main()
