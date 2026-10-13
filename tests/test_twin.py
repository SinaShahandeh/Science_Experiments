"""The digital twin (twin/): heater fit, kinetics, the camera image and reading, and the API's behaviour.

    .venv-harness/bin/python -m unittest tests.test_twin
"""
import math
import random
import tempfile
import time
import unittest

import numpy as np

from twin import kinetics
from twin.optics import CellLook, agent_png, render_cell, well_gray
from twin.physics import Bed
from twin.rig import Rig


def step_response(t0, t1, dt=0.1, seconds=300, pattern=None):
    bed = Bed(23.0)
    if t0 > 23:
        bed.set_target(t0)
        for _ in range(int(1800 / 0.1)):
            bed.step(0.1)
    bed.set_target(t1)
    t, reached, peak, t_peak = 0.0, None, 0.0, 0.0
    while t < seconds:
        d = pattern() if pattern else dt
        bed.step(d)
        t += d
        if reached is None and bed.ts >= t1 - 0.3:
            reached = t
        if bed.ts > peak:
            peak, t_peak = bed.ts, t
    return reached, peak, t_peak


class Heater(unittest.TestCase):
    def test_matches_the_trial_step(self):
        # 2026-10-11: 50 -> 75 C reached 75 C after ~42 s and peaked at 76.75 C after ~60 s
        reached, peak, t_peak = step_response(50, 75)
        self.assertTrue(38 <= reached <= 48, reached)
        self.assertAlmostEqual(peak, 76.75, delta=0.5)
        self.assertTrue(50 <= t_peak <= 70, t_peak)

    def test_independent_of_the_simulation_step(self):
        rnd = random.Random(1)
        _, a, _ = step_response(23, 50, dt=0.1)
        _, b, _ = step_response(23, 50, pattern=lambda: rnd.choice([0.1, 0.0005, 0.37]))
        self.assertAlmostEqual(a, b, delta=0.05)


class Kinetics(unittest.TestCase):
    def test_literature_anchor(self):
        p = kinetics.PARAMS
        self.assertAlmostEqual(math.log(2) / p.k_ot(62.0), 90.0, delta=1.0)  # OT half-life 1.5 min at 62 C

    def test_half_times_fall_with_temperature(self):
        t = [kinetics.t_half_isothermal(c) for c in (62, 64, 66, 68, 70)]
        self.assertTrue(all(a > b for a, b in zip(t, t[1:])), t)
        self.assertTrue(120 <= kinetics.t_half_isothermal(68) <= 420)

    def test_apparent_activation_energy(self):
        temps = np.array([64, 65, 66, 67, 68, 69], float)
        t50 = np.array([kinetics.t_half_isothermal(c) for c in temps])
        slope = np.polyfit(1 / (temps + 273.15), np.log(1 / t50), 1)[0]
        ea = -slope * kinetics.R / 1000
        self.assertTrue(400 <= ea <= 500, ea)


class Camera(unittest.TestCase):
    def test_reading_recovers_the_liquid_brightness(self):
        rng = np.random.default_rng(3)
        for label in ("P3-B2", "C-A1", "P1-A2", "C-D4"):
            look = CellLook(label)
            for w, g in ((0.0, 22.0), (0.4, 90.0), (1.0, 198.0)):
                rgb = render_cell(look, loaded=True, w=w, gray_level=g, rng=rng)
                r = well_gray(rgb)
                self.assertAlmostEqual(r["gray"], g, delta=3.0, msg=(label, w))
                self.assertLessEqual(r["pixels_used"], r["pixels_total"])

    def test_p3_b2_glint_crosses_the_square(self):
        rgb = render_cell(CellLook("P3-B2"), loaded=True, w=0.0, gray_level=22.0, rng=np.random.default_rng(0))
        self.assertGreater(well_gray(rgb)["saturated_px"], 0)

    def test_agent_image(self):
        big = agent_png(render_cell(CellLook("C-A1"), loaded=False, w=0, gray_level=19, rng=np.random.default_rng(0)))
        self.assertEqual(big.shape, (272, 272, 3))
        self.assertEqual(tuple(big[68, 100]), (0, 255, 0))      # the measured square's outline


class Api(unittest.TestCase):
    """The rig's commands, run in-process at 50x."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.rig = Rig(cls.tmp.name, speed=50, auto_operator=True, seed=5)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, cmd, **args):
        return self.rig.submit(cmd, args)

    def wait_bed(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if cond(self.rig.reading):
                return
            time.sleep(0.05)
        self.fail("bed never reached the condition")

    def test_flow(self):
        status, body = self.call("list_wells")
        self.assertEqual(len(body["labels"]), 32)
        status, body = self.call("set_temperature", temperature=120)
        self.assertEqual((status, body["error"]), (400, "temperature_limit"))
        status, body = self.call("get_raw_opacity", label="Z-9")
        self.assertEqual((status, body["error"]), (400, "unknown_label"))
        status, body = self.call("load_well", label="P3-A1")
        self.assertEqual((status, body["error"]), (409, "not_homed"))
        status, body = self.call("prepare")
        self.assertTrue(body["ready"], body)
        status, body = self.call("load_well", label="P3-A1")
        self.assertEqual(status, 200, body)
        self.assertGreater(body["t_loaded"], body["t_requested"])
        status, body = self.call("load_well", label="P3-A1")
        self.assertEqual(body["error"], "already_loaded")
        status, body = self.call("get_raw_opacity", label="P3-A1")
        for k in ("gray", "wall_gray", "pixels_used", "image", "bed_c", "camera_tilt_mm", "rehomed"):
            self.assertIn(k, body)
        self.call("set_temperature", temperature=75)
        self.wait_bed(lambda r: r > 61)
        status, body = self.call("load_well", label="P3-A2")
        self.assertEqual(body["error"], "bed_hot")
        status, body = self.call("clean_all_wells")
        self.assertEqual(body["error"], "bed_hot")
        st = self.call("get_status")[1]
        self.assertEqual([w["label"] for w in st["loaded_wells"]], ["P3-A1"])
        # stepping down to a lower target: the bed cools for minutes, more than 5 C above it; that must not halt
        self.wait_bed(lambda r: r > 74)
        self.call("set_temperature", temperature=50)
        self.wait_bed(lambda r: r < 51, timeout=40)
        st = self.call("get_status")[1]
        self.assertEqual(st["safety"], "ok", st.get("safety_reason"))
        self.call("set_temperature", temperature=0)


if __name__ == "__main__":
    unittest.main()
