"""Fit what the 2026-10-11 trial can tell about the twin's clouding model.

    .venv-harness/bin/python -m twin.calibrate_kinetics [--ea-ova 450] [--clip 0.25] [--w-ot-only 0.22]

The trial: four egg-white wells in P3 (a corner island), bed held at 50 C, then stepped to 75 C. One
temperature can't measure activation energies, so those come from the literature (twin/kinetics.py).
Fitted here, on the shape of each well's gray curve normalized by its final value:
  - the ovalbumin rate at 72 C (kept within half-lives of 2-10 min at 75 C, the literature range),
  - the optical saturation factor beta,
  - how far the liquid lags the bed (tau),
  - per-well rate and lag multipliers (egg-to-egg and fill variation).
Fixed, because the trial can't tell them apart from the rest:
  - --clip: camera settings v1 clipped clear egg white to 0 and hid the start of the rise. The fit
    assumes the hidden part is the ovotransferrin milkiness (--w-ot-only of full brightness) and a bit.
Prints the fitted parameters to paste into twin/kinetics.py.
"""
import argparse
import json
import math
from dataclasses import replace

import numpy as np
from scipy.optimize import least_squares

from .geometry import ROOT
from .kinetics import PARAMS, THERMAL, EggWhite, t_half_isothermal
from .physics import Bed

TRIAL = ROOT / "tmp_runs" / "egg_trial1_20261011_2138"
T_LOAD = -100.0   # the four wells were loaded during the 2 min before GO (events.csv: beep at -121 s)
T_AMB = 23.0


def trial_curves():
    d = json.loads((TRIAL / "run_methods_v2.json").read_text())["box34_madhigh"]
    out = {}
    for lab, (ts, vs) in d.items():
        ts, vs = np.array(ts), np.array(vs)
        out[lab] = (ts, vs / np.mean(vs[ts > ts.max() - 60]))
    return out


def plate_history(t_end=620.0, dt=0.1):
    bed = Bed(T_AMB)
    bed.set_target(50.0)
    t, out = -1800.0, []
    while t < t_end:
        if t >= 0 and bed.target != 75.0:
            bed.set_target(75.0)
        bed.step(dt)
        t += dt
        if t >= T_LOAD - dt:
            out.append((t, bed.tp))
    return np.array(out)


def simulate(params, k_mult, tau, plate, eta, times):
    w = EggWhite(k_mult=k_mult, eta=eta, tau_s=tau, params=params, temp_c=THERMAL.t_load_c)
    res, j, prev = [], 0, None
    for t, tp in plate:
        if t < T_LOAD:
            continue
        if prev is not None:
            w.step(tp, T_AMB, t - prev)
        prev = t
        while j < len(times) and times[j] <= t:
            res.append(w.opacity())
            j += 1
    while len(res) < len(times):
        res.append(w.opacity())
    return np.array(res)


def w_ot_for(beta, w_only):
    return -math.log(1 - w_only * (1 - math.exp(-beta))) / beta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ea-ova", type=float, default=PARAMS.ea_ova / 1e3)
    ap.add_argument("--clip", type=float, default=0.25)
    ap.add_argument("--w-ot-only", type=float, default=0.22)
    a = ap.parse_args()
    curves = trial_curves()
    labels = list(curves)
    plate = plate_history()

    def unpack(x):
        beta = x[1]
        p = replace(PARAMS, ea_ova=a.ea_ova * 1e3, k_ova_ref=math.exp(x[0]), beta=beta, w_ot=w_ot_for(beta, a.w_ot_only))
        tau = x[2]
        km = np.exp(np.r_[x[3:6], -np.sum(x[3:6])])
        tm = np.exp(np.r_[x[6:9], -np.sum(x[6:9])])
        return p, tau, km, tm

    def visible(w, w_end):
        return np.clip((w / w_end - a.clip) / (1 - a.clip), 0, None)

    def resid(x):
        p, tau, km, tm = unpack(x)
        out = []
        for lab, k, tt in zip(labels, km, tm):
            ts, alpha = curves[lab]
            w = simulate(p, k, tau * tt, plate, THERMAL.eta_corner, np.r_[ts, 600.0])
            out.append((visible(w[:-1], w[-1]) - alpha) * 100.0)
        # keep the ovalbumin half-life at 75 C inside the literature's 2-10 min
        t75 = math.log(2) / p.k_ova(75.0) / 60
        pen = [max(0.0, t75 - 10.0) * 50, max(0.0, 2.0 - t75) * 50]
        return np.concatenate(out + [x[3:9] * 15.0, pen])

    x0 = [math.log(PARAMS.k_ova_ref), PARAMS.beta, THERMAL.tau_s, 0, 0, 0, 0, 0, 0]
    lb = [math.log(1e-6), 2.0, 15.0, -0.5, -0.5, -0.5, -0.5, -0.5, -0.5]
    ub = [math.log(1e-1), 60.0, 180.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    r = least_squares(resid, x0, bounds=(lb, ub), max_nfev=400)
    p, tau, km, tm = unpack(r.x)
    n_pts = sum(len(curves[lab][0]) for lab in labels)
    rms = float(np.sqrt(np.mean(r.fun[:n_pts] ** 2)))
    print(f"fit RMS {rms:.1f}% of the final value; liquid lag tau = {tau:.1f} s; "
          f"OVA t1/2 at 75 C = {math.log(2) / p.k_ova(75.0) / 60:.1f} min")
    print("per-well rate multipliers:", {k: round(float(v), 3) for k, v in zip(labels, km)})
    print("per-well lag multipliers: ", {k: round(float(v), 3) for k, v in zip(labels, tm)})
    print(f"k_ova_ref={p.k_ova_ref:.6g}, beta={p.beta:.6g}, w_ot={p.w_ot:.6g}, tau_s={tau:.4g}")
    for lab, k, tt in zip(labels, km, tm):
        ts, alpha = curves[lab]
        w = simulate(p, k, tau * tt, plate, THERMAL.eta_corner, np.r_[ts, 600.0])
        pred = visible(w[:-1], w[-1])
        row = [f"{alpha[np.argmin(abs(ts - t))]:.2f}/{pred[np.argmin(abs(ts - t))]:.2f}" for t in range(60, 620, 60)]
        print(lab, "trial/sim every 60 s:", " ".join(row))
    print("isothermal liquid t50 (min) vs the 75 C/10 min end point:",
          {c: round(t_half_isothermal(c, p) / 60, 1) for c in (62, 64, 65, 66, 67, 68, 69, 70, 72, 75)})


if __name__ == "__main__":
    main()
