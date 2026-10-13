"""Frame processing: the opacity reading on synthetic squares, and the real calibration frames when present."""
import unittest
from pathlib import Path

import cv2
import numpy as np

from mhapi.service import vision
from mhapi.service.calibration import Calibration

CAL = Path(__file__).resolve().parents[1] / "calibration"


def frame(gray=100):
    return np.full((720, 1280, 3), gray, np.uint8)


class OpacityTest(unittest.TestCase):
    C = (690.5, 362.0)

    def test_uniform(self):
        o = vision.opacity(frame(120), self.C)
        self.assertEqual((o["gray"], o["pixels_total"], o["pixels_used"], o["all_saturated"]), (120.0, 1156, 1156, False))

    def test_square_is_34_px_centred(self):
        img = frame(50)
        x0, y0 = vision.origin(self.C[0], 17), vision.origin(self.C[1], 17)
        self.assertEqual((x0, y0), (674, 346))  # centre of pixels 674..707 is 690.5
        img[y0:y0 + 34, x0:x0 + 34] = 200       # exactly the measured square
        self.assertEqual(vision.opacity(img, self.C)["gray"], 200.0)

    def test_saturated_and_halo_dropped(self):
        img = frame(100)
        img[355:358, 685:688] = 255             # a 3x3 saturated spot inside the square
        img[353:360, 683:690][(img[353:360, 683:690] == 100)] = 180  # bright edge of the reflection
        o = vision.opacity(img, self.C)
        self.assertEqual(o["saturated_px"], 9)
        self.assertEqual(o["gray"], 100.0)
        self.assertLess(o["pixels_used"], 1156 - 9)

    def test_glare_removed_one_sided(self):
        img = frame(60)
        rng = np.random.default_rng(0)
        img[:] = np.clip(60 + rng.normal(0, 2, img.shape[:2]), 0, 255).astype(np.uint8)[..., None]
        img[360:364, 675:705] = 150             # a glint stripe (not saturated)
        img[340:344, 675:705] = 20              # a dark stripe: kept (reflections are only brighter)
        o = vision.opacity(img, self.C)
        self.assertGreater(o["reflection_px"], 100)
        self.assertAlmostEqual(o["gray"], 60.0, delta=1.5)

    def test_all_saturated(self):
        o = vision.opacity(frame(255), self.C)
        self.assertEqual((o["gray"], o["all_saturated"], o["pixels_used"]), (255.0, True, 0))

    def test_cell_image(self):
        crop = vision.cell_crop(frame(80), self.C)
        self.assertEqual(crop.shape, (68, 68, 3))
        big = vision.cell_image(crop)
        self.assertEqual(big.shape, (272, 272, 3))
        self.assertEqual(tuple(big[66, 136]), (0, 255, 0))      # outline just outside the square
        self.assertEqual(tuple(big[68, 136]), (80, 80, 80))     # the square's own pixels are untouched
        self.assertEqual(tuple(big[203, 203]), (80, 80, 80))

    def test_wall_gray_band(self):
        img = frame(30)
        px = 4.88
        cx, cy = self.C
        ys, xs = np.mgrid[0:720, 0:1280]
        d = np.maximum(np.abs(xs - cx), np.abs(ys - cy)) / px
        img[(d >= 5.0) & (d <= 7.0)] = 175     # a 2 mm wall ring around a 10 mm opening
        self.assertEqual(vision.wall_gray(img, self.C, px), 175.0)


@unittest.skipUnless((CAL / "verify_P3-B2.jpg").exists(), "calibration frames not present (*.jpg are git-ignored)")
class RealFramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = Calibration(CAL)

    def test_centred_wells(self):
        for label in ("P3-B2", "C-A1", "P2-A2", "P4-B1", "C-C3"):
            img = cv2.imread(str(CAL / f"verify_{label}.jpg"))
            dx, dy, match = vision.tilt(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), self.cal)
            self.assertGreater(match, 0.69, label)
            self.assertLess(np.hypot(dx, dy), 0.5, label)
            c = self.cal.nadir + np.array([dx, dy])
            i, d = vision.nearest(vision.holes(img), c)
            self.assertLess(d, 1.5, f"{label}: opening {d:.2f} px from the straight-down pixel")
            o = vision.opacity(img, c)
            self.assertLess(o["gray"], 30, label)                     # empty well: bare black bed
            self.assertTrue(150 < vision.wall_gray(img, c, self.cal.px_per_mm) < 210, label)
            z = vision.estimate_z(vision.holes(img), self.cal)
            self.assertAlmostEqual(z, 50.0, delta=4.0, msg=label)

    def test_height_from_pitch(self):
        for z in (50, 58, 66):
            img = cv2.imread(str(CAL / f"nadir_z{z}.jpg"))
            if img is None:
                self.skipTest("nadir frames not present")
            self.assertAlmostEqual(vision.estimate_z(vision.holes(img), self.cal), z, delta=4.0)

    def test_moved_mount_is_unmatched(self):
        old = sorted((CAL / "archive_20261011_before_camera_move").glob("*.jpg"))[:5]
        if not old:
            self.skipTest("archive frames not present")
        for f in old:
            g = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
            dx, dy, match = vision.tilt(g, self.cal)
            self.assertTrue(match < 0.3 or np.hypot(dx, dy) > 4.9, f.name)  # camera_moved either way


if __name__ == "__main__":
    unittest.main()
