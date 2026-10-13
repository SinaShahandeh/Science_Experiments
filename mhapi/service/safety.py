"""The safety layer (Hardware_Safety_spec.md): safety states, the heat rules, foreign-control detection, the
camera-mount lock, the heartbeat for mhapi-heatguard and the macOS power assertion.

States: ok, heater_timeout (H4 switched the heater off; every command still works) and halted (heater off;
no motion, heating or operator prompts; only a person's Clear on the console ends it).
"""
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections import deque

from .errors import ApiError

log = logging.getLogger("mhapi.safety")
TARGET_TOL_C = 1.0      # a reported target within this of a value we set counts as ours
HOMING_GRACE_S = 15.0   # home-bit changes this soon after our own homing are ours
STALE_TARGET_S = 60.0   # until a new target is confirmed (at most this long), reports of the one it replaced are stale


def fmt_temp(v):
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


class Safety:
    def __init__(self, params, printer, announcer, events, heartbeat_path, loaded_count, prompt_limit,
                 sim=False):
        self.p = params
        self.printer = printer
        self.announcer = announcer
        self.events = events                  # callable(event, **detail)
        self.heartbeat_path = heartbeat_path
        self.loaded_count = loaded_count      # callable -> number of loaded wells
        self.prompt_limit = prompt_limit      # callable -> (kind, max bed °C) of an open operator prompt, or None
        self.sim = sim
        self.lock = threading.RLock()
        self.state, self.reason, self.t_state = "ok", None, None
        self.detail = None                    # what caused the halt, in words
        self.camera_lock = None               # C2: motion stopped until a person clears it
        self.expected = None                  # the bed target the service set (or adopted at start-up)
        self.t_expected = None
        self.sent = deque()                   # (t, value) targets sent recently
        self.replaced = None                  # (t, value): the target our last change replaced
        self.confirmed = True
        self.t_sent = None
        self.alerted_unconfirmed = False
        self.t_last_call = time.time()        # H4 clock
        self.reached = False
        self.direction = 0
        self.over_since = None
        self.off_window = deque()             # (t, bed) since the heater was switched off (after the grace)
        self.t_off = time.time()              # when the target last became 0
        self.homing = False
        self.t_homing_end = 0.0
        self.prev_home_bits = None
        self.on_halt = []                     # callables(reason): cancel prompts, fail queued operations
        self.on_clear = []                    # callables(): position unknown after a halt
        self.on_change = []                   # callables(): persist state
        self._caffeinate = None
        self._t_heartbeat = 0.0
        self._lost_alerted = False
        self._stop = threading.Event()
        printer.listeners.append(self.on_report)
        # Seed from the reports that arrived before the safety layer existed (start-up waits for one), so a
        # change in between is judged against them rather than taken as the starting point.
        state = dict(getattr(printer, "state", {}) or {})
        if state.get("bed_target_temper") is not None:
            self._check_reported_target(float(state["bed_target_temper"]), time.time())
        if state.get("home_flag") is not None:
            self.prev_home_bits = int(state["home_flag"] or 0) & 0b111

    # -- lifecycle -------------------------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, name="safety", daemon=True).start()

    def restore(self, saved, calibration_id):
        if saved.get("state") == "halted":
            self.state, self.reason, self.t_state = "halted", saved.get("reason"), saved.get("t")
            self.detail = saved.get("detail")
        lock = saved.get("camera_lock")
        if lock and lock.get("calibration_id") == calibration_id:  # a recalibration clears it
            self.camera_lock = lock

    def as_record(self):
        return {"state": self.state, "reason": self.reason, "detail": self.detail, "t": self.t_state,
                "camera_lock": self.camera_lock}

    def describe_halt(self):
        """The halt in words, for error messages: what happened, when, and what a person has to do."""
        when = time.strftime("%H:%M:%S", time.localtime(self.t_state)) if self.t_state else "an unknown time"
        what = f"{self.reason}: {self.detail}" if self.detail else str(self.reason)
        return (f"the safety stop has been active since {when} ({what}). The heater is off and nothing moves; only "
                "reads work until a person checks the instrument and presses Clear on the operator console. "
                "After that, prepare re-homes.")

    def halted_error(self):
        return ApiError("halted", self.describe_halt(), safety_reason=self.reason, safety_detail=self.detail,
                        t_safety=None if self.t_state is None else round(self.t_state, 3))

    def _changed(self):
        for fn in self.on_change:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("state save failed")

    def _run(self):
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("safety tick failed")

    def close(self):
        self._stop.set()
        self._set_caffeinate(False)

    # -- API calls -------------------------------------------------------------------------------------
    def on_api_call(self):
        self.t_last_call = time.time()

    def check_motion(self):
        """Raise if nothing may move: halted, or the camera mount moved."""
        if self.state == "halted":
            raise self.halted_error()
        if self.camera_lock:
            raise ApiError("camera_moved", "motion is stopped since the camera moved on its mount "
                                           f"({self.camera_lock['detail']}); a person must check the mount, "
                                           "recalibrate and clear it", **self.camera_lock.get("fields", {}))

    def set_target(self, value):
        """set_temperature: validate (H1), send, and track the target until a report confirms it (H3)."""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ApiError("invalid_argument", "the temperature must be a number of °C")
        if value < 0:
            raise ApiError("invalid_argument", "the temperature can't be negative; 0 turns the heater off")
        if value > self.p.heat.max_target_c:
            raise ApiError("temperature_limit", f"targets above {self.p.heat.max_target_c:g} °C are refused",
                           max_c=self.p.heat.max_target_c)
        if self.state == "halted":
            raise self.halted_error()
        limit = self.prompt_limit()
        if limit and value > limit[1]:
            raise ApiError("bed_hot", f"a person is working on the bed ({limit[0]}); the target can't go above "
                                      f"{limit[1]:g} °C until they answer", bed_c=self.printer.bed_c,
                           max_c=limit[1])
        value = float(value)
        self._send_target(value)
        with self.lock:
            resumed = self.state == "heater_timeout"
            if resumed:
                self._set_state("ok", None)
        self.events("set_temperature", target_c=value, resumed_after_timeout=resumed)
        return value

    # -- heater ----------------------------------------------------------------------------------------
    def _send_target(self, value):
        t = time.time()
        with self.lock:
            changed = self.expected is None or abs(self.expected - value) > 1e-9
            if changed and self.expected is not None:
                self.replaced = (t, self.expected)
            self.expected, self.t_expected = value, t
            self.sent.append((t, value))
            self.confirmed = False
            self.alerted_unconfirmed = False
            if changed:
                bed = self.printer.bed_c
                self.reached = False
                self.direction = 0 if bed is None else (1 if bed < value else -1)
                self.over_since = None
                self.off_window.clear()
                if value == 0:
                    self.t_off = t
        self._transmit(value)
        self._set_caffeinate(self._needs_awake())

    def _transmit(self, value):
        self.t_sent = time.time()
        try:
            self.printer.send([f"M140 S{fmt_temp(value)}"])
        except ApiError as e:  # link down: the monitor resends (H3, H6)
            log.warning("could not send M140 S%s: %s", fmt_temp(value), e.message)

    def heater_off(self, reason):
        log.warning("heater off: %s", reason)
        self._send_target(0.0)

    # -- states ----------------------------------------------------------------------------------------
    def _set_state(self, state, reason, detail=None):
        self.state, self.reason, self.detail, self.t_state = state, reason, detail, time.time()
        self._changed()

    def halt(self, reason, detail=""):
        with self.lock:
            first = self.state != "halted"
            if first:
                self._set_state("halted", reason, detail or None)
        self.heater_off(f"halt: {reason}")
        if not first:
            return
        self.events("safety_halted", reason=reason, detail=detail)
        self.announcer.say(f"Safety stop. {detail or reason.replace('_', ' ')}. Heater off.", chimes=1)
        for fn in self.on_halt:
            try:
                fn(reason)
            except Exception:  # noqa: BLE001
                log.exception("halt handler failed")

    def clear(self, who="console"):
        """A person ends a halt and the camera lock (operator console only)."""
        with self.lock:
            was = (self.state, self.reason, self.camera_lock)
            if self.state == "halted":
                self._set_state("ok", None)
            self.camera_lock = None
            self._changed()
        self.events("safety_cleared", by=who, was_state=was[0], was_reason=was[1], camera_lock=was[2])
        self.announcer.say("Safety stop cleared. The heater stays off; the instrument must re-home.", chimes=1)
        for fn in self.on_clear:
            fn()

    def lock_camera(self, detail, calibration_id, **fields):
        with self.lock:
            if self.camera_lock:
                return
            self.camera_lock = {"t": round(time.time(), 3), "detail": detail, "calibration_id": calibration_id,
                                "fields": fields}
            self._changed()
        self.events("camera_moved", detail=detail, **fields)
        self.announcer.say("The camera moved on its mount. Motion is stopped. Check the mount.", chimes=1)

    # -- reports ---------------------------------------------------------------------------------------
    def on_report(self, t, changed, state):
        if "bed_target_temper" in changed:
            self._check_reported_target(float(changed["bed_target_temper"]), t)
        if "home_flag" in changed:
            bits = int(changed["home_flag"] or 0) & 0b111
            prev, self.prev_home_bits = self.prev_home_bits, bits
            ours = self.homing or time.time() - self.t_homing_end < HOMING_GRACE_S
            if prev is not None and (bits & ~prev) and not ours:
                self.halt("foreign_control", "the printer was homed by something other than the service")
        if changed.get("gcode_state") in ("PREPARE", "RUNNING", "PAUSE"):
            self.halt("foreign_control", f"a print job started (gcode_state {changed['gcode_state']})")
        bed = changed.get("bed_temper")
        if bed is not None:
            self._on_bed(float(bed), t)

    def _check_reported_target(self, v, t):
        with self.lock:
            if self.expected is None:  # H7: adopt the target found at start-up, start the H4 clock now
                self.expected, self.t_expected, self.confirmed = v, t, True
                self.t_last_call = time.time()
                if v > 0:
                    self.events("adopted_target", target_c=v)
                return
            if abs(v - self.expected) < TARGET_TOL_C:
                if not self.confirmed:
                    self.confirmed = True
                    if self.alerted_unconfirmed:
                        self.events("target_confirmed_late", target_c=v)
                return
            while self.sent and t - self.sent[0][0] > 60:
                self.sent.popleft()
            if any(abs(v - s) < TARGET_TOL_C for _, s in self.sent):
                return  # an earlier target of ours, still in flight
            r = self.replaced
            if r and not self.confirmed and abs(v - r[1]) < TARGET_TOL_C and t - r[0] < STALE_TARGET_S:
                # The target we just replaced (one adopted at start-up, or ours from long ago): every report
                # carries the target, and the printer applies M140 only after the moves queued before it, so
                # reports keep the old value for a while (2026-10-13: 0 °C reported 0.46 s after M140 S50).
                return
        self.halt("foreign_control", f"the bed target changed to {fmt_temp(v)} °C, which the service didn't set")

    def _on_bed(self, bed, t):
        with self.lock:
            if self.expected == 0:
                # Switched off mid-ramp, the reported reading still climbs for ~10 s (reports trail the
                # printer by ~5 s and the plate keeps taking heat): start judging after a grace period.
                if t < self.t_off + self.p.heat.stuck_grace_s:
                    return
                self.off_window.append((t, bed))
                while self.off_window and t - self.off_window[0][0] > self.p.heat.stuck_window_s:
                    self.off_window.popleft()
            elif self.expected and not self.reached:
                if self.direction == 0:  # a target adopted at start-up: heading up or cooling down to it?
                    self.direction = 1 if bed < self.expected else -1
                if ((self.direction > 0 and bed >= self.expected - 0.5)
                        or (self.direction < 0 and bed <= self.expected + 0.5)):
                    self.reached = True

    # -- monitor ---------------------------------------------------------------------------------------
    def tick(self):
        now = time.time()
        h = self.p.heat
        bed = self.printer.bed_c
        reported = self.printer.target_c
        with self.lock:
            expected = self.expected
        heating = (expected or 0) > 0 or (reported or 0) > 0
        # H2: over-temperature
        if bed is not None and self.state != "halted":
            if bed > h.hard_max_c:
                self.halt("over_temperature", f"the bed read {bed:.1f} °C, above {h.hard_max_c:g} °C")
            elif expected and self.reached and bed > expected + h.overshoot_margin_c:
                self.over_since = self.over_since or now
                if now - self.over_since > h.overshoot_time_s:
                    self.halt("over_temperature", f"the bed stayed above {expected + h.overshoot_margin_c:g} °C "
                                                  f"for over {h.overshoot_time_s:g} s")
            else:
                self.over_since = None
            if expected == 0 and len(self.off_window) >= 2:
                rise = bed - min(b for _, b in self.off_window)
                if rise > h.stuck_rise_c:
                    self.halt("heater_stuck_on", f"the bed rose {rise:.1f} °C in under {h.stuck_window_s:g} s "
                                                 "with the heater off")
        # H6: lost contact while heating
        age = self.printer.report_age
        if heating and (age is None or age > h.lost_reports_s):
            if self.state != "halted":
                self.halt("lost_contact", f"no printer report for {h.lost_reports_s:g} s while heating; "
                                          "the bed may still be hot")
            elif not self._lost_alerted:
                self.announcer.say("No contact with the printer. The bed may still be hot.", chimes=3)
            self._lost_alerted = True
        elif age is not None and age < h.lost_reports_s:
            self._lost_alerted = False
        # H4: heater watchdog
        if self.state == "ok" and (expected or 0) > 0 and now - self.t_last_call > h.heater_watchdog_s:
            self.heater_off("watchdog")
            with self.lock:
                self._set_state("heater_timeout", f"no API call for {h.heater_watchdog_s / 60:g} min")
            self.events("heater_timeout", minutes=h.heater_watchdog_s / 60)
            self.announcer.say("No instructions for a while. Heater switched off.", chimes=1)
        # H3: resend an unconfirmed target, alert a person after a minute
        with self.lock:
            unconfirmed = not self.confirmed and self.expected is not None
            value = self.expected
        if unconfirmed:
            if self.t_sent is None or now - self.t_sent >= h.target_resend_s:
                self._transmit(value)
            if now - (self.t_expected or now) > h.target_alert_s and not self.alerted_unconfirmed:
                self.alerted_unconfirmed = True
                what = "Heater off" if value == 0 else f"Bed target {fmt_temp(value)} degrees"
                self.events("target_unconfirmed", target_c=value, seconds=round(now - self.t_expected))
                self.announcer.say(f"{what} not confirmed by the printer.", chimes=3)
        # H5 heartbeat, H8 power assertion
        if now - self._t_heartbeat >= h.heartbeat_s:
            self._t_heartbeat = now
            self.write_heartbeat()
        self._set_caffeinate(self._needs_awake())

    def heater_target_for_guard(self):
        with self.lock:
            vals = [v for v in (self.expected, self.printer.target_c) if v is not None]
            if self.expected == 0 and self.confirmed:
                return 0.0
        return max(vals) if vals else 0.0

    def write_heartbeat(self, target=None):
        hb = {"t": round(time.time(), 3), "pid": os.getpid(),
              "target_c": self.heater_target_for_guard() if target is None else target,
              "wells_loaded": self.loaded_count(), "sim": self.sim}
        tmp = self.heartbeat_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(hb))
            os.replace(tmp, self.heartbeat_path)
        except OSError as e:
            log.warning("heartbeat write failed: %s", e)

    def _needs_awake(self):
        return self.heater_target_for_guard() > 0 or self.loaded_count() > 0

    def _set_caffeinate(self, on):
        """H8: hold a power assertion (`caffeinate -i`) while heating or while wells are loaded."""
        if self.sim:
            return
        if on and self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
                log.info("power assertion on (caffeinate)")
            except OSError as e:
                log.warning("caffeinate failed: %s", e)
        elif not on and self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None
            log.info("power assertion off")

    @property
    def power_assertion(self):
        return self._caffeinate is not None

    # -- shutdown (H7) ---------------------------------------------------------------------------------
    def shutdown_heater(self, wait_s=15.0):
        """Switch the heater off and wait for the printer to confirm it. Returns True when confirmed."""
        if (self.printer.target_c or 0) == 0 and (self.expected or 0) == 0:
            return True
        self.heater_off("service shutdown")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            time.sleep(0.5)
            with self.lock:
                if self.confirmed and self.expected == 0:
                    return True
            if time.time() - self.t_sent >= self.p.heat.target_resend_s:
                self._transmit(0.0)
        return False
