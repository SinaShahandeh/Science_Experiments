"""The G-code allowlist (safety spec M1, M2). Every command to the printer passes through check().

Allowed: G90, G91, M204 S500, M140 S<0-100>, G28 X and G28 Z P0 T140 (homing only), and G1 within the
workspace: XY targets only at well positions and the park point, both axes given, at most F1800; absolute Z
only to the capture height; relative Z only upwards inside homing; Z at most F300; never Z and XY in one
move; M204 S500 before every move. A plain G28, nozzle and fan commands are never sent.
"""
import re

_WORD = re.compile(r"([A-Z])(-?\d+(?:\.\d+)?)")
_TOL = 0.005


class GcodeRejected(Exception):
    pass


def check(lines, *, allowed_xy, motion, max_target_c, homing=False):
    absolute = None  # set by G90 / G91 within the same message
    accel_set = False
    for raw in lines:
        line = raw.strip()
        if line == "G90":
            absolute = True
        elif line == "G91":
            absolute = False
        elif line == f"M204 S{motion.accel}":
            accel_set = True
        elif line.startswith("M140 "):
            m = re.fullmatch(r"M140 S(\d+(?:\.\d+)?)", line)
            if not m or not 0 <= float(m[1]) <= max_target_c:
                raise GcodeRejected(f"bed target out of range: {line!r}")
        elif line in ("G28 X", "G28 Z P0 T140"):
            if not homing:
                raise GcodeRejected(f"{line!r} outside the homing sequence")
        elif line.startswith("G1 "):
            _check_move(line, absolute, accel_set, allowed_xy, motion, homing)
        else:
            raise GcodeRejected(f"not on the allowlist: {line!r}")


def _check_move(line, absolute, accel_set, allowed_xy, motion, homing):
    rest = line[3:].replace(" ", "")
    words = _WORD.findall(rest)
    if "".join(a + b for a, b in words) != rest:
        raise GcodeRejected(f"malformed move: {line!r}")
    w = {}
    for axis, val in words:
        if axis not in "XYZF" or axis in w:
            raise GcodeRejected(f"bad move word in {line!r}")
        w[axis] = float(val)
    if absolute is None:
        raise GcodeRejected(f"move without G90/G91 in the same message: {line!r}")
    if not accel_set:
        raise GcodeRejected(f"M204 S{motion.accel} must come before every move: {line!r}")
    if "F" not in w:
        raise GcodeRejected(f"move without a feed rate: {line!r}")
    has_xy, has_z = ("X" in w or "Y" in w), "Z" in w
    if has_xy and has_z:
        raise GcodeRejected(f"Z and XY in one move: {line!r}")
    if has_xy:
        if not absolute:
            raise GcodeRejected(f"relative XY move: {line!r}")
        if "X" not in w or "Y" not in w:
            raise GcodeRejected(f"XY move must give both axes: {line!r}")
        if w["F"] > motion.xy_feed:
            raise GcodeRejected(f"XY feed above F{motion.xy_feed}: {line!r}")
        x, y = w["X"], w["Y"]
        if not (0 <= x <= 256 and 0 <= y <= 256):
            raise GcodeRejected(f"XY target outside the bed: {line!r}")
        if not any(abs(x - ax) <= _TOL and abs(y - ay) <= _TOL for ax, ay in allowed_xy):
            raise GcodeRejected(f"XY target is not a well position or the park point: {line!r}")
    elif has_z:
        if w["F"] > motion.z_feed:
            raise GcodeRejected(f"Z feed above F{motion.z_feed}: {line!r}")
        if absolute:
            if abs(w["Z"] - motion.capture_z) > _TOL:
                raise GcodeRejected(f"absolute Z other than the capture height Z{motion.capture_z:g}: {line!r}")
        elif not (homing and 0 < w["Z"] <= motion.homing_lift_mm):
            raise GcodeRejected(f"relative Z move outside the homing lift: {line!r}")
    else:
        raise GcodeRejected(f"move without a target: {line!r}")
