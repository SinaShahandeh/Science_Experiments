"""Repeatability test on the (now static, cooked) P3 wells: static, loop, and fixed-viewpoint captures."""
import csv, sys, time
from pathlib import Path
from kinetics_run import Run, WELLS, Z_IMG

out = Path(sys.argv[1])
r = Run(out, list(WELLS), pre=0, hold=0, quiet=True, leave_heat=True)
r.t_go = time.time()
mf = open(out / "meta.csv", "w", newline="")
mw = csv.writer(mf, lineterminator="\n")
mw.writerow(["file", "exp", "rep", "cam_at", "came_from", "cam_x", "cam_y", "t_rel", "bed_temper", "frame_in_stop"])
n = 0
prev = "P3-A1"
CENTER = (sum(v[0] for v in WELLS.values()) / 4, sum(v[1] for v in WELLS.values()) / 4)
POS = dict(WELLS, **{"P3-center": CENTER})


def goto(name):
    global prev
    x, y = POS[name]
    r.move(x, y, Z_IMG, timeout=30)
    came, prev = prev, name
    return came


def grab(exp, rep, at, came, k=1, gap=0.0):
    global n
    for i in range(k):
        _, jpg, t, _ = r.cam.next()
        n += 1
        f = f"{n:05d}_{exp}_{at}.jpg"
        (out / "frames" / f).write_bytes(jpg)
        x, y = POS[at]
        mw.writerow([f, exp, rep, at, came, x, y, f"{t - r.t_go:.3f}", r.last_temp[1], i])
        mf.flush()
        if gap:
            time.sleep(gap)


try:
    print("descend to Z50 over P3-A1", flush=True)
    r.move(*WELLS["P3-A1"], Z_IMG, timeout=30)
    print("E1 static", flush=True)
    grab("E1static", 0, "P3-A1", "P3-A1", k=40, gap=0.5)
    print("E2 loop", flush=True)
    for rep in range(15):
        for w in ["P3-A1", "P3-A2", "P3-B2", "P3-B1"]:
            came = goto(w)
            grab("E2loop", rep, w, came, k=3)
    print("E3 fixed viewpoints", flush=True)
    for rep in range(2):
        for w in ["P3-A1", "P3-A2", "P3-B2", "P3-B1"]:
            came = goto(w)
            grab("E3fixed", rep, w, came, k=10, gap=0.3)
            came = goto("P3-center")
            grab("E3fixed", rep, "P3-center", came, k=5, gap=0.3)
finally:
    r.event("done")
    r._poll = False
    r.cam.close()
    r.p.close()
print("frames:", n)
