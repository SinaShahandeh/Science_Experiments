"""Detect well openings (dark ~square holes inside white PETG walls) in a Pi camera frame."""
import sys
import cv2
import numpy as np


def detect(path, min_area=150, max_area=40000):
    img = cv2.imread(path)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # white walls: bright and low saturation (rejects the rainbow reflections on the bed)
    white = ((g > 150) & (hsv[..., 1] < 110)).astype(np.uint8) * 255
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, hier = cv2.findContours(white, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = []
    if hier is None:
        return img, holes
    for i, c in enumerate(cnts):
        if hier[0][i][3] < 0:  # only inner contours (holes in a white blob)
            continue
        a = cv2.contourArea(c)
        if not (min_area < a < max_area):
            continue
        (cx, cy), (w, h), ang = cv2.minAreaRect(c)
        if min(w, h) / max(w, h) < 0.7 or a / (w * h) < 0.75:
            continue
        holes.append(dict(cx=cx, cy=cy, w=w, h=h, area=a, ang=ang))
    return img, holes


def annotate(img, holes, out):
    H, W = img.shape[:2]
    vis = img.copy()
    cv2.drawMarker(vis, (W // 2, H // 2), (0, 0, 255), cv2.MARKER_CROSS, 40, 2)
    for i, h in enumerate(holes):
        cv2.circle(vis, (int(h["cx"]), int(h["cy"])), 4, (0, 255, 0), -1)
        cv2.putText(vis, str(i), (int(h["cx"]) + 5, int(h["cy"]) - 5), 0, 0.5, (0, 255, 255), 1)
    cv2.imwrite(out, vis)


if __name__ == "__main__":
    img, holes = detect(sys.argv[1])
    for i, h in enumerate(holes):
        print(i, {k: round(v, 1) for k, v in h.items()})
    if len(sys.argv) > 2:
        annotate(img, holes, sys.argv[2])
