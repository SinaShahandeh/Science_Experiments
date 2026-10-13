"""Log camera tilt vs calibration/camera_ref.png every 30 s; print a line when it moves > 0.5 px from the last report."""
import csv, time
import cv2, numpy as np
from camstream import CamStream
from cam_tilt import tilt_px

cam = CamStream()
f = open("../calibration/tilt_log.csv", "a", newline=""); w = csv.writer(f)
last = None
while True:
    d = tilt_px([cv2.imdecode(np.frombuffer(cam.next()[1], np.uint8), cv2.IMREAD_GRAYSCALE) for _ in range(5)])
    w.writerow([f"{time.time():.1f}", time.strftime("%H:%M:%S"), f"{d[0]:.3f}", f"{d[1]:.3f}"]); f.flush()
    if last is None or np.hypot(*(d - last)) > 0.5:
        print(f"{time.strftime('%H:%M:%S')} camera tilt now ({d[0]:+.2f}, {d[1]:+.2f}) px vs reference", flush=True)
        last = d
    time.sleep(30)
