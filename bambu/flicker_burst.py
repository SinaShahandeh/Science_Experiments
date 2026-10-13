"""Record a burst with the camera parked and report the common-mode brightness oscillation of the P3 wells."""
import csv, sys, time
import cv2
import numpy as np
from camstream import CamStream
from motion import load_pos
from well_px import well_px
from kinetics_run import WELLS


def burst(out_csv, dur=20.0, nadir=None, offset=(0.0, 0.0)):
    """offset: mm added to the table positions (temporary correction until recalibration)."""
    pos = load_pos()
    nd = np.array(nadir if nadir else (0.0, 0.0)) - (well_px((0, 0), (0, 0)) if nadir else 0)
    px = {w: (well_px((pos["x"], pos["y"]), (xy[0] + offset[0], xy[1] + offset[1])) + nd).round().astype(int)
          for w, xy in WELLS.items()}
    cam = CamStream()
    rows = []
    t0 = time.time()
    while time.time() - t0 < dur:
        _, jpg, t, _ = cam.next()
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        vals = []
        for w, (x, y) in px.items():
            roi = img[y - 15:y + 15, x - 15:x + 15]
            g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            vals.append(float(np.median(g[roi.max(axis=2) < 250])))
        rows.append([t - t0] + vals)
    cam.close()
    a = np.array(rows)
    with open(out_csv, "w", newline="") as f:
        csv.writer(f).writerows([["t"] + list(px)] + rows)
    rel = a[:, 1:] / a[:, 1:].mean(0)
    cm = rel.mean(1)
    # 1 s bins: what a slow, sampled measurement sees
    bins = np.floor(a[:, 0]).astype(int)
    b = np.array([cm[bins == k].mean() for k in np.unique(bins)])
    x = cm - cm.mean()
    f = np.fft.rfftfreq(len(x), d=a[-1, 0] / (len(x) - 1))
    P = np.abs(np.fft.rfft(x))
    pk = f[1:][np.argmax(P[1:])]
    print(f"{out_csv}: n={len(a)} fps={len(a)/a[-1,0]:.1f} means={a[:,1:].mean(0).round(1)} "
          f"per-well SD%={np.round(rel.std(0)*100,2)} common-mode SD%={cm.std()*100:.2f} "
          f"(1 s bins {b.std()*100:.2f}) peak {pk:.2f} Hz", flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    burst(a[0], float(a[1]) if len(a) > 1 else 20,
          nadir=(float(a[2]), float(a[3])) if len(a) > 3 else None,
          offset=(float(a[4]), float(a[5])) if len(a) > 5 else (0.0, 0.0))
