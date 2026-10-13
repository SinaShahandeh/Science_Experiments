"""Camera tilt on its mount, from toolhead parts visible at the top of every frame.

The toolhead (fan duct, 'CAUTION HOT' label) is rigid with the gantry, so if the camera rotates on its
mount these parts shift in the image by the same amount as the bed scene; ~4.9 px = 1 mm at Z50.
  python3 cam_tilt.py ref    -> save the reference strip (calibration/camera_ref.png), at calibration time
  python3 cam_tilt.py        -> print the current shift (px) against the reference
"""
import sys
from pathlib import Path
import cv2
import numpy as np

REF = Path(__file__).resolve().parent.parent / "calibration" / "camera_ref.png"
STRIP = (0, 110, 410, 950)  # y0, y1, x0, x1: toolhead body only (housing, duct lobes, "CAUTION HOT"); checked stable under XY moves 2026-10-12


def strip(gray):
    y0, y1, x0, x1 = STRIP
    return gray[y0:y1, x0:x1].astype(np.float32)


def tilt_px(gray_frames):
    """Mean (dx, dy) shift of the toolhead strip vs the reference, over the given grayscale frames."""
    R = cv2.imread(str(REF), cv2.IMREAD_GRAYSCALE).astype(np.float32)
    win = cv2.createHanningWindow(R.shape[::-1], cv2.CV_32F)
    return np.mean([cv2.phaseCorrelate(R, strip(g), win)[0] for g in gray_frames], axis=0)


if __name__ == "__main__":
    from camstream import CamStream
    cam = CamStream()
    frames = [cv2.imdecode(np.frombuffer(cam.next()[1], np.uint8), cv2.IMREAD_GRAYSCALE) for _ in range(15)]
    cam.close()
    if sys.argv[1:] == ["ref"]:
        cv2.imwrite(str(REF), np.median(np.stack([strip(f) for f in frames]), axis=0).astype(np.uint8))
        print("saved", REF)
    else:
        d = tilt_px(frames)
        print(f"camera tilt shift vs reference: ({d[0]:+.2f}, {d[1]:+.2f}) px = ({d[0]/4.9:+.2f}, {d[1]/4.98:+.2f}) mm at Z50")


def tilt_of_file(path):
    """Tilt (px) measured on one saved frame."""
    return tilt_px([cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)])
