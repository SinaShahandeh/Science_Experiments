"""The G-code allowlist (safety spec M1, M2)."""
import unittest

from mhapi.service.config import Params
from mhapi.service.gcode import GcodeRejected, check

P = Params.load()
XY = [(173.78, 113.32), (107.14, 178.97), (128.0, 254.0)]


def ok(lines, homing=False):
    check(lines, allowed_xy=XY, motion=P.motion, max_target_c=P.heat.max_target_c, homing=homing)


class AllowlistTest(unittest.TestCase):
    def test_allowed(self):
        ok(["G90", "M204 S500", "G1 X173.78 Y113.32 F1800"])
        ok(["G90", "M204 S500", "G1 X128.00 Y254.00 F1800"])
        ok(["G90", "M204 S500", "G1 Z50 F300"])
        ok(["M140 S0"])
        ok(["M140 S100"])
        ok(["M140 S62.5"])
        ok(["G28 X"], homing=True)
        ok(["G28 Z P0 T140"], homing=True)
        ok(["G91", "M204 S500", "G1 Z30 F300", "G90"], homing=True)

    def test_rejected(self):
        bad = [
            (["G28"], True), (["G28 X"], False), (["G28 Z P0 T140"], False), (["G28 Z"], True),
            (["M104 S200"], False), (["M109 S200"], False), (["M106 P1 S255"], False), (["M112"], False),
            (["M140 S101"], False), (["M140 S-1"], False), (["M140"], False),
            (["G90", "M204 S500", "G1 X100 Y100 F1800"], False),          # not a well position
            (["G90", "M204 S500", "G1 X173.78 Y113.32 F3000"], False),    # too fast
            (["G90", "M204 S500", "G1 X173.78 F1800"], False),            # one axis only
            (["G90", "M204 S500", "G1 X173.78 Y113.32 Z50 F300"], False),  # Z and XY together
            (["G90", "M204 S500", "G1 Z100 F300"], False),                # absolute Z other than 50
            (["G90", "M204 S500", "G1 Z50 F600"], False),                 # Z too fast
            (["G91", "M204 S500", "G1 Z30 F300", "G90"], False),          # relative Z outside homing
            (["G91", "M204 S500", "G1 Z-5 F300", "G90"], True),           # downwards
            (["G91", "M204 S500", "G1 Z31 F300", "G90"], True),           # more than the homing lift
            (["G91", "M204 S500", "G1 X5 Y5 F300", "G90"], True),         # relative XY
            (["G1 X173.78 Y113.32 F1800"], False),                        # no G90 / M204
            (["G90", "G1 X173.78 Y113.32 F1800"], False),                 # no M204 before the move
            (["G90", "M204 S5000", "G1 X173.78 Y113.32 F1800"], False),   # other acceleration
            (["G90", "M204 S500", "G1 X173.78 Y113.32"], False),          # no feed
            (["G90", "M204 S500", "G1 X173.78 Y113.32 E5 F1800"], False),  # extrusion
        ]
        for lines, homing in bad:
            with self.assertRaises(GcodeRejected, msg=str(lines)):
                ok(lines, homing)


if __name__ == "__main__":
    unittest.main()
