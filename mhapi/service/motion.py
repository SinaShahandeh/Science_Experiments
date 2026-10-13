"""Confirmed motion and the safe homing sequence (API spec "Move completion"; safety spec M2-M5).

A move counts as done only when (1) the printer acknowledged the command within 3 s, (2) the camera saw
the image move and then 4 still frames (Z moves: their expected duration + 0.8 s, then 4 still frames),
and (3) the first still frame differs from the last frame before the move (or, for Z, the image scale
says the camera is at the commanded height). Otherwise the call fails with move_failed and the position
counts as unknown until prepare() re-homes.
"""
import json
import logging
import math
import time

from . import vision
from .errors import ApiError, HomingFailed, MoveFailed

log = logging.getLogger("mhapi.motion")

MOVE_THR = 2.0         # thumbnail mean |diff| above this: the image is moving
STILL_THR = 0.3        # below this: a still frame
STILL_N = 4
SCENE_THR = 1.0        # last frame before vs first still frame: above this the scene changed
ACK_S = 3.0
MOTION_START_S = 2.0   # motion must show within this after the acknowledgement
SETTLE_MARGIN_S = 5.0
Z_MARGIN_S = 0.8
Z_CONFIRM_TOL = 10.0   # image-scale height must be within this of the commanded Z
HOMING_STILL_S = 3.0
HOMING_TIMEOUT_S = 150.0
SAME_XY = 0.005


class NoMotion(MoveFailed):
    """Acknowledged, but the camera saw nothing move."""


class Gantry:
    def __init__(self, printer, camera, cal, params, safety, events, position_file):
        self.printer, self.camera, self.cal, self.p, self.safety = printer, camera, cal, params, safety
        self.events = events
        self.position_file = position_file
        self.pos = None                 # last commanded and confirmed {"x", "y", "z"}
        self.z_known = False            # M3: Z is known only after a Z move or homing we confirmed ...
        self.z_epoch = None             # ... on this connection to the printer
        self.xy_confirmed = False       # XY confirmed by a move of ours since start-up
        self.position_unknown = False   # after move_failed or a halt: no XY move until prepare() re-homes
        self.unknown_reason = None
        self.t_last_move = None
        self._load_hint()

    # -- position bookkeeping -------------------------------------------------------------------------
    def _load_hint(self):
        try:
            d = json.loads(self.position_file.read_text())
            self.pos = {k: float(d[k]) for k in ("x", "y", "z") if d.get(k) is not None}
            self.t_last_move = self.position_file.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            self.pos = None

    def _save(self):
        try:
            self.position_file.write_text(json.dumps(self.pos))
        except OSError as e:
            log.warning("could not save the position: %s", e)
        self.t_last_move = time.time()

    def mark_unknown(self, reason):
        self.position_unknown, self.unknown_reason = True, reason
        self.z_known = False
        self.xy_confirmed = False

    @property
    def at_capture_height(self):
        return (self.z_known and self.z_epoch == self.printer.epoch and self.pos is not None
                and abs(self.pos.get("z", -1) - self.p.motion.capture_z) < 0.01)

    # -- checks ----------------------------------------------------------------------------------------
    def preflight(self, need_homed=True):
        """Everything that must hold before the printer moves (M2, N2, C2)."""
        self.safety.check_motion()
        probs = self.printer.problems()
        if probs:
            raise ApiError("printer_unavailable", "the printer can't move now: " + "; ".join(probs))
        if not self.camera.ok:
            raise ApiError("camera_unavailable", "the camera stream is not delivering frames")
        if need_homed and not self.printer.homed:
            raise ApiError("not_homed", "the printer has lost its home position")

    def _check_halt(self):
        if self.safety.state == "halted":
            self.mark_unknown("halted")
            raise self.safety.halted_error()

    # -- sending ---------------------------------------------------------------------------------------
    def _send(self, lines, homing=False):
        seq, t_cmd = self.printer.send(lines, homing=homing)
        ack = self.printer.wait_ack(seq, ACK_S)
        if ack is None:
            raise MoveFailed(f"the printer did not acknowledge {lines[-1]!r} within {ACK_S:g} s")
        if ack[1] not in (None, "success"):
            raise MoveFailed(f"the printer answered {ack[1]!r} to {lines[-1]!r}")
        self._t_ack = ack[0]
        return t_cmd

    def _frame_after(self, seq):
        try:
            return self.camera.frame_after(seq, timeout=3.0)
        except ApiError as e:
            raise MoveFailed(f"the camera stopped delivering frames during the move ({e.message})") from None

    def _wait_motion_then_still(self, before, t_cmd, expected_s):
        """Wait for the image to move, then for 4 still frames. Returns (first still, last still)."""
        f, moving, still, first_still = before, False, 0, None
        motion_deadline = max(t_cmd, getattr(self, "_t_ack", t_cmd)) + MOTION_START_S
        deadline = t_cmd + ACK_S + expected_s + SETTLE_MARGIN_S
        while True:
            f = self._frame_after(f.seq)
            if f.t < t_cmd or f.diff is None:
                continue
            if not moving:
                # Frame to frame, or against the frame before the command: seen from high up (the park move during
                # homing, at Z ~100 over the dark bed) a 30 mm/s move changes each frame by only ~1.2 for its first
                # 3 s, while the scene has drifted well past MOVE_THR within a fraction of a second (2026-10-13).
                if f.diff > MOVE_THR or self.scene_change(before, f) > MOVE_THR:
                    moving = True
                elif f.t > motion_deadline:
                    raise NoMotion("the camera saw no motion after the command")
                continue
            if f.diff < STILL_THR:
                still += 1
                first_still = first_still or f
                if still >= STILL_N:
                    return first_still, f
            else:
                still, first_still = 0, None
            if f.t > deadline:
                raise MoveFailed(f"the image did not settle within {deadline - t_cmd:.0f} s")

    def _wait_z(self, t_cmd, expected_s):
        """Z moves: wait out the expected duration + 0.8 s, then 4 consecutive still frames."""
        time.sleep(max(0.0, t_cmd + expected_s + Z_MARGIN_S - time.time()))
        f = self.camera.latest_frame() or self._frame_after(self.camera.seq)
        still, deadline = 0, time.time() + 10.0
        while still < STILL_N:
            f = self._frame_after(f.seq)
            still = still + 1 if (f.diff is not None and f.diff < STILL_THR) else 0
            if time.time() > deadline:
                raise MoveFailed("the image did not settle after the Z move")
        return f

    @staticmethod
    def scene_change(a, b):
        return float(abs(a.thumb - b.thumb).mean()) if a.thumb.shape == b.thumb.shape else math.inf

    def _z_of(self, frame):
        return vision.estimate_z(vision.holes(frame.bgr()), self.cal)

    # -- moves -----------------------------------------------------------------------------------------
    def move_z_capture(self):
        """`G1 Z50` on its own, confirmed (M3: first motion after connecting, before any XY move)."""
        z = self.p.motion.capture_z
        vz = self.p.motion.z_feed / 60.0
        before = self.camera.latest_frame() or self._frame_after(self.camera.seq)
        if self.z_known and self.pos:
            z_from = self.pos["z"]
        else:
            z_from = self._z_of(before)
        expected = abs(z - z_from) / vz if z_from is not None else 100.0 / vz
        try:
            t_cmd = self._send(["G90", f"M204 S{self.p.motion.accel}", f"G1 Z{z:g} F{self.p.motion.z_feed}"])
            after = self._wait_z(t_cmd, expected)
            z_after = self._z_of(after)
            changed = self.scene_change(before, after) > SCENE_THR
            if z_after is not None and abs(z_after - z) > Z_CONFIRM_TOL:
                raise MoveFailed(f"after G1 Z{z:g} the image scale says Z ≈ {z_after:.0f} mm")
            if z_after is None and not changed:
                raise MoveFailed(f"could not confirm the move to Z{z:g}: no well openings in view and no change")
        except MoveFailed as e:
            self._failed(e)
            raise
        self.pos = {**(self.pos or {}), "z": z}
        self.z_known, self.z_epoch = True, self.printer.epoch
        self.events("move", z=z, seconds=round(time.time() - t_cmd, 2), z_image=None if z_after is None else round(z_after, 1))
        self._save()
        return after

    def move_xy(self, x, y, may_be_there=False):
        """Absolute XY move at the capture height, confirmed. Returns the last still frame.
        may_be_there: XY is unconfirmed, so seeing no motion may mean the camera is already over (x, y);
        raise NoMotion without marking the position unknown and let the caller prove the printer moves."""
        if not self.at_capture_height:
            raise MoveFailed("internal: XY move requested before Z is confirmed at the capture height")
        v = self.p.motion.xy_feed / 60.0
        dist = math.hypot(x - self.pos["x"], y - self.pos["y"]) if "x" in self.pos else 256 * math.sqrt(2)
        expected = dist / v + v / self.p.motion.accel
        before = self.camera.latest_frame() or self._frame_after(self.camera.seq)
        try:
            t_cmd = self._send(["G90", f"M204 S{self.p.motion.accel}", f"G1 X{x:.2f} Y{y:.2f} F{self.p.motion.xy_feed}"])
            first, last = self._wait_motion_then_still(before, t_cmd, expected)
            if self.scene_change(before, first) <= SCENE_THR:
                raise MoveFailed("the scene after the move is the same as before it")
        except NoMotion as e:
            if may_be_there:
                self.events("no_motion_unconfirmed_xy", x=x, y=y)
                raise
            self._failed(e)
            raise
        except MoveFailed as e:
            self._failed(e)
            raise
        self.pos.update(x=x, y=y)
        self.xy_confirmed = True
        self.events("move", x=x, y=y, seconds=round(last.t - t_cmd, 2))
        self._save()
        return last

    def _failed(self, e):
        self.mark_unknown("move_failed")
        self.events("move_failed", detail=e.message)

    def goto(self, x, y):
        """Bring the camera over (x, y) at the capture height. Returns the last still frame, or None if the
        camera was already there and nothing moved."""
        if not self.at_capture_height:
            self.move_z_capture()
        if self.xy_confirmed:
            if abs(self.pos["x"] - x) < SAME_XY and abs(self.pos["y"] - y) < SAME_XY:
                return None
            return self.move_xy(x, y)
        # Since start-up the XY position is only a hint (calibration/position.json), and the printer never
        # reports it. If nothing moves, the camera may already be there: prove the printer moves with a
        # detour via the nearest other position, then come back.
        try:
            return self.move_xy(x, y, may_be_there=True)
        except NoMotion:
            nx, ny = min((p for p in self.printer.allowed_xy if math.hypot(p[0] - x, p[1] - y) > 1.0),
                         key=lambda p: math.hypot(p[0] - x, p[1] - y))
            self.move_xy(nx, ny)
            self._check_halt()
            return self.move_xy(x, y)

    # -- homing ----------------------------------------------------------------------------------------
    def home(self):
        """The safe homing sequence (safety spec M4). Leaves the camera at the park point, Z50."""
        self.preflight(need_homed=False)
        t0 = time.time()
        self.events("homing_started", home_flag=self.printer.home_flag & 0b111)
        self.safety.homing = True
        self.printer.fast_reports(HOMING_TIMEOUT_S * 2)
        m = self.p.motion
        vz = m.z_feed / 60.0
        try:
            if self.printer.homed and not self.at_capture_height:
                self.move_z_capture()          # M3: a homed printer with an unknown Z: G1 Z50 alone first
                self._check_halt()
            before = self._frame_after(self.camera.seq)
            z_est = self._z_of(before)
            self.events("homing_height_check", z_image=None if z_est is None else round(z_est, 1))
            if z_est is None or z_est < m.min_z_before_homing + 5.0:
                self._relative_lift(m.homing_lift_mm, vz, before)
            self._check_halt()
            self._homing_step(["G28 X"], 0b011)                       # 1. home X and Y
            self._check_halt()
            self._relative_lift(m.post_xy_home_lift_mm, vz)            # 2. lift
            self._check_halt()
            px, py = m.park_xy                                         # 3. bare steel behind the grid
            before = self._frame_after(self.camera.seq)
            t_cmd = self._send(["G90", f"M204 S{m.accel}", f"G1 X{px:.2f} Y{py:.2f} F{m.xy_feed}"])
            first, _ = self._wait_motion_then_still(before, t_cmd, 2 * 256 / (m.xy_feed / 60.0))
            if self.scene_change(before, first) <= SCENE_THR:
                raise MoveFailed("the park move left the scene unchanged")
            self._check_halt()
            self._homing_step(["G28 Z P0 T140"], 0b100)               # 4. home Z on bare steel
            self._check_halt()
            before = self._frame_after(self.camera.seq)                # 5. lift to Z50, confirmed
            t_cmd = self._send(["G90", f"M204 S{m.accel}", f"G1 Z{m.capture_z:g} F{m.z_feed}"])
            after = self._wait_z(t_cmd, m.capture_z / vz)
            z_after = self._z_of(after)
            if z_after is not None and abs(z_after - m.capture_z) > Z_CONFIRM_TOL:
                raise MoveFailed(f"after the lift the image scale says Z ≈ {z_after:.0f} mm")
            if z_after is None and self.scene_change(before, after) <= SCENE_THR:
                raise MoveFailed("could not confirm the lift to the capture height")
            if not self.printer.homed:
                raise HomingFailed("the printer does not report all three axes homed")
        except MoveFailed as e:
            self.mark_unknown("homing_failed")
            self.events("homing_failed", detail=e.message)
            raise HomingFailed(f"homing did not complete: {e.message}") from None
        except ApiError:
            self.mark_unknown("homing_interrupted")
            raise
        finally:
            self.safety.homing = False
            self.safety.t_homing_end = time.time()
        self.pos = {"x": float(m.park_xy[0]), "y": float(m.park_xy[1]), "z": float(m.capture_z)}
        self.z_known = self.xy_confirmed = True
        self.z_epoch = self.printer.epoch
        self.position_unknown, self.unknown_reason = False, None
        self._save()
        self.events("homing_done", seconds=round(time.time() - t0, 1),
                    z_image=None if z_after is None else round(z_after, 1))
        return time.time() - t0

    def _relative_lift(self, mm, vz, before=None):
        before = before or self._frame_after(self.camera.seq)
        m = self.p.motion
        t_cmd = self._send(["G91", f"M204 S{m.accel}", f"G1 Z{mm:g} F{m.z_feed}", "G90"], homing=True)
        after = self._wait_z(t_cmd, mm / vz)
        if self.scene_change(before, after) <= SCENE_THR:
            raise MoveFailed(f"the {mm:g} mm lift left the scene unchanged")
        self.events("lift", mm=mm)

    def _homing_step(self, lines, bits):
        """Commands of unknown duration: wait until the image has moved and then stayed still for 3 s,
        and the home bits are set (bambu/motion.py Gantry.run)."""
        before = self._frame_after(self.camera.seq)
        t_cmd = self._send(lines, homing=True)
        f, moved, still_since = before, False, None
        while time.time() - t_cmd < HOMING_TIMEOUT_S:
            f = self._frame_after(f.seq)
            if f.t < t_cmd or f.diff is None:
                continue
            if f.diff > MOVE_THR:
                moved, still_since = True, None
            elif f.diff < STILL_THR:
                still_since = still_since or f.t
            else:
                still_since = None
            if (moved and still_since and f.t - still_since >= HOMING_STILL_S
                    and (self.printer.home_flag & bits) == bits):
                self.events("homing_step", command=lines[0], seconds=round(f.t - t_cmd, 1))
                return f
        raise MoveFailed(f"{lines[0]!r} did not finish within {HOMING_TIMEOUT_S:g} s "
                         f"(moved: {moved}, home bits {self.printer.home_flag & 0b111:03b})")
