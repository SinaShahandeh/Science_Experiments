"""Lock the Pi camera's tone so white paper in C-A1 reads WHITE (232) and the black bed's darkest channel BLACK (8),
leaving headroom above white and below black (v2, 2026-10-11; v1 mapped paper->255, bed->0).

Measures only the pixels inside the C-A1 cell walls (camera parked so C-A1 is centred on the nadir, Z50) and
iterates the service's fixed controls in three phases:
  1. white balance  - colour gains until the paper is neutral (R = G = B)
  2. exposure       - analogue gain until the paper sits mid-high and unclipped
  3. tone stretch   - ISP contrast and brightness until paper -> WHITE and black -> BLACK

Exposure is restricted to whole multiples of 10 ms so mains-powered room light (100 Hz in
Europe) cannot beat against it; light level is set with analogue gain.

Each step rewrites the systemd drop-in on the Pi and restarts mjpeg-stream, so the final
values persist. Adaptive contrast enhancement is disabled via a tuning-file copy, otherwise
the tone curve would follow the scene. Usage: python3 calibrate_exposure.py
"""
import json
import pathlib
import subprocess
import time
import urllib.request

import numpy as np
from PIL import Image

STREAM = 'http://raspberrypi.local:8080/stream.mjpg'
ROI = (712, 329, 757, 375)  # x0, y0, x1, y1: inside the C-A1 walls, 1280x720 frame
DROPIN = '/etc/systemd/system/mjpeg-stream.service.d/fixed-exposure.conf'
SERVER = '/home/alwerds/Documents/Albert/Robot/mjpeg_server.py'
TUNING = '/home/alwerds/Documents/Albert/Robot/ov5647_fixed_tone.json'
EXPOSURES_US = (30000, 20000, 10000)  # flicker-safe: whole 10 ms periods, <= 33 ms at 30 fps
WHITE, BLACK = 232, 8  # paper: mean of R,G,B medians; bare bed: its darkest channel (the bed is bluish)
OUT = pathlib.Path(__file__).resolve().parent.parent / 'calibration' / 'exposure'


def apply(p):
    """Write the drop-in for params p and restart the stream service."""
    exec_start = (
        f'/usr/bin/python3 {SERVER} --width 1280 --height 720 --framerate 30 '
        f'--quality medium --port 8080 --exposure-us {p["exposure"]} --gain {p["gain"]:.3f} '
        f'--awb-gains {p["red"]:.4f},{p["blue"]:.4f} '
        f'--brightness {p["brightness"]:.4f} --contrast {p["contrast"]:.4f}'
    )
    dropin = (
        '# Fixed camera tone, set by bambu/calibrate_exposure.py on the Mac.\n'
        '# White paper in C-A1 -> 255, black bed -> 0. Delete this file, then\n'
        '# `sudo systemctl daemon-reload && sudo systemctl restart mjpeg-stream` for auto.\n'
        '[Service]\n'
        f'Environment=LIBCAMERA_RPI_TUNING_FILE={TUNING}\n'
        'ExecStart=\n'
        f'ExecStart={exec_start}\n'
    )
    subprocess.run(
        ['ssh', 'raspberrypi',
         f'sudo tee {DROPIN} >/dev/null && sudo systemctl daemon-reload '
         '&& sudo systemctl restart mjpeg-stream'],
        input=dropin.encode(), check=True)


def frame(skip=12):
    """Return a fresh RGB frame, skipping the first frames after a restart."""
    for attempt in range(20):
        try:
            with urllib.request.urlopen(STREAM, timeout=5) as r:
                buf, n = b'', 0
                while True:
                    buf += r.read(16384)
                    s = buf.find(b'\xff\xd8')
                    e = buf.find(b'\xff\xd9', s + 2) if s >= 0 else -1
                    if e < 0:
                        continue
                    jpg, buf = buf[s:e + 2], buf[e + 2:]
                    n += 1
                    if n > skip:
                        path = OUT / 'last.jpg'
                        path.write_bytes(jpg)
                        return np.asarray(Image.open(path).convert('RGB')).astype(float)
        except OSError:
            time.sleep(0.5)  # service still starting
    raise RuntimeError('no frame from camera stream')


def measure(a):
    """Split the C-A1 interior into paper and bed by brightness; return statistics."""
    x0, y0, x1, y1 = ROI
    roi = a[y0:y1, x0:x1].reshape(-1, 3)
    lum = roi.mean(1)
    lo, hi = np.percentile(lum, [5, 95])
    thr = (lo + hi) / 2
    # Stay clear of the blurred paper edge: keep pixels well inside each cluster.
    paper = roi[lum > thr + 0.25 * (hi - thr)]
    bed = roi[lum < thr - 0.25 * (thr - lo)]
    return {
        'paper_rgb': np.median(paper, 0).round(1).tolist(),
        'paper_min_p05': float(np.percentile(paper.min(1), 5)),
        'bed_rgb': np.median(bed, 0).round(1).tolist(),
        'bed_max_p95': float(np.percentile(bed.max(1), 95)),
        'paper_px': int(len(paper)), 'bed_px': int(len(bed)),
    }


def scale_light(p, factor):
    """Scale exposure x gain by factor, using the longest flicker-safe exposure that
    keeps gain >= 1 (the sensor minimum)."""
    total = p['gain'] * p['exposure'] * factor
    p['exposure'] = next((e for e in EXPOSURES_US if total / e >= 1.0), EXPOSURES_US[-1])
    p['gain'] = float(np.clip(total / p['exposure'], 1.0, 16.0))


def step(p, label):
    apply(p)
    m = measure(frame())
    print(f'{label:10s} exp={p["exposure"]} gain={p["gain"]:.2f} wb=({p["red"]:.3f},{p["blue"]:.3f}) '
          f'bright={p["brightness"]:+.3f} contrast={p["contrast"]:.3f} | '
          f'paper={m["paper_rgb"]} p05min={m["paper_min_p05"]:.0f} '
          f'bed={m["bed_rgb"]} p95max={m["bed_max_p95"]:.0f}', flush=True)
    return m


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    p = {'exposure': EXPOSURES_US[0], 'gain': 4.0, 'red': 0.87, 'blue': 2.0}
    previous = OUT / 'result.json'
    if previous.exists():  # start from the last white balance and gain
        p.update(json.loads(previous.read_text())['params'])
    p.update(brightness=0.0, contrast=1.0)
    scale_light(p, 1.0)  # snap to a flicker-safe exposure

    # 1. White balance on the paper, at neutral tone and an unclipped level.
    for i in range(8):
        m = step(p, f'awb {i}')
        r, g, b = m['paper_rgb']
        if max(r, g, b) > 235:
            scale_light(p, 0.7)
            continue
        if abs(r / g - 1) < 0.03 and abs(b / g - 1) < 0.03:
            break
        # Output is gamma-encoded; ~2.2 maps the ratio back to linear, damped to 1.6.
        p['red'] = float(np.clip(p['red'] * (g / r) ** 1.6, 0.3, 8))
        p['blue'] = float(np.clip(p['blue'] * (g / b) ** 1.6, 0.3, 8))

    # 2. Exposure: paper to ~200 with neutral tone, leaving headroom for the stretch.
    for i in range(6):
        m = step(p, f'expo {i}')
        level = float(np.mean(m['paper_rgb']))
        if abs(level - 200) < 8:
            break
        scale_light(p, (200 / level) ** 2.0)

    # 3. Stretch. Model the ISP as out = c*(x - 0.5) + 0.5 + br (normalised), invert the
    # measured paper/bed levels to x, and solve c, br for the targets. Medians, not tails:
    # the blurred paper edge always leaves a band of mid-grey pixels.
    def lvl(rgb):
        return float(np.mean(rgb)) / 255

    for i in range(10):
        P, B = lvl(m["paper_rgb"]), min(m["bed_rgb"]) / 255
        c, br = p['contrast'], p['brightness']
        if i and abs(P * 255 - WHITE) <= 3 and abs(B * 255 - BLACK) <= 3:
            break
        xp = (P - 0.5 - br) / c + 0.5
        xb = (B - 0.5 - br) / c + 0.5
        if B * 255 < 1 or P * 255 > 254:  # clipped: inversion invalid, nudge instead
            if B * 255 < 1:
                p['brightness'] += 0.02
            if P * 255 > 254:
                p['contrast'] *= 0.95
        else:
            p['contrast'] = (WHITE - BLACK) / 255 / (xp - xb)
            p['brightness'] = BLACK / 255 - (p['contrast'] * (xb - 0.5) + 0.5)
        m = step(p, f'tone {i}')

    (OUT / 'result.json').write_text(json.dumps({'params': p, 'measure': m}, indent=2))
    (OUT / 'last.jpg').rename(OUT / 'final.jpg')
    print('final', json.dumps(p))


if __name__ == '__main__':
    main()
