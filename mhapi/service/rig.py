"""The instrument behind the API: every command of Model_Hardware_API_spec.md, on top of the printer, the
camera, the calibration, confirmed motion, the safety layer, the operator desk and the session folder."""
import base64
import itertools
import logging
import math
import time
from collections import deque

import numpy as np

from . import vision
from .errors import ApiError, MoveFailed
from .motion import Gantry
from .operator import OperatorDesk
from .opqueue import Operation, OpQueue
from .safety import Safety
from .session import SessionStore
from ..tools import TOOLS, tool_definitions

log = logging.getLogger("mhapi.rig")

# The position check: one well in each corner island and two in the center island (outer corners).
CHECK_WELLS = ("P1-A1", "P2-A2", "P3-B2", "P4-B1", "C-A1", "C-D4")


def r(x, n=2):
    if x is None:
        return None
    x = float(x)
    return round(x, n) if math.isfinite(x) else None


class Rig:
    def __init__(self, params, paths, printer, camera, cal, announcer, sim=None, auto_operator_s=None):
        self.p, self.paths, self.printer, self.camera, self.cal = params, paths, printer, camera, cal
        self.announcer = announcer
        self._early_events = []
        self.sim = sim                       # simulator world (sim mode only)
        self.recent_events = deque(maxlen=40)
        self.session = SessionStore(paths.sessions)
        self.session.on_new.append(cal.write_copy)
        self.operator = OperatorDesk(announcer, auto_operator_s)
        self.loaded = []                     # [{"label", "t_loaded"}] since the last clean
        self.position_check = None           # {"t", "max_mm", ...} of the last position check
        self.last_capture_tilt = None        # camera shift (mm) at the previous capture
        self.interrupted = None              # operation that was running when the service stopped
        self.ops = OpQueue(on_change=self.save_state)
        self.safety = Safety(params, printer, announcer, self.event, paths.heartbeat,
                             loaded_count=lambda: len(self.loaded), prompt_limit=self._prompt_limit, sim=bool(sim))
        self.gantry = Gantry(printer, camera, cal, params, self.safety, self.event, paths.position_file)
        self.safety.on_halt.append(self._on_halt)
        self.safety.on_clear.append(lambda: self.gantry.mark_unknown("halt cleared"))
        self.safety.on_change.append(self.save_state)
        printer.listeners.append(self._log_temp)
        printer.on_reconnect.append(self._on_reconnect)
        self.t_start = time.time()

    def _on_reconnect(self):
        self.gantry.z_known = False   # M3: the first motion after a reconnect is G1 Z50 on its own
        self.event("printer_reconnected")

    # -- start-up and shutdown -------------------------------------------------------------------------
    def restore(self):
        saved = self.session.restore_latest()
        for name, detail in self._early_events:
            self.session.log_event(name, **detail)
        self._early_events = []
        self.loaded = [w for w in saved.get("loaded_wells", []) if w.get("label") in self.cal.wells]
        if saved.get("calibration_id") == self.cal.id:
            self.position_check = saved.get("position_check")
        self.safety.restore(saved.get("safety", {}), self.cal.id)
        op = saved.get("operation")
        if op and op.get("state") == "running":
            self.interrupted = {**op, "state": "interrupted"}
            if op.get("command") in ("prepare", "get_image", "get_raw_opacity", "clean_all_wells"):
                self.gantry.mark_unknown("interrupted by a service restart")
        if saved.get("position_unknown"):
            self.gantry.mark_unknown(saved.get("position_unknown_reason") or "unknown before restart")
        if saved.get("t_last_move"):
            self.gantry.t_last_move = saved["t_last_move"]
        self.event("service_started", loaded_wells=[w["label"] for w in self.loaded], safety=self.safety.state,
                   interrupted=self.interrupted)
        self.save_state()

    def begin_shutdown(self):
        """Answer pending calls before the server stops: cancel the operator prompt, fail queued calls."""
        body = lambda: {"error": "service_unavailable", "message": "the service is shutting down",  # noqa: E731
                        "t": round(time.time(), 3)}
        self.ops.fail_queued(body, 503)
        self.operator.cancel("shutdown")

    def shutdown(self):
        """H7: switch the heater off and wait for the printer to confirm it."""
        confirmed = self.safety.shutdown_heater()
        self.event("service_stopped", heater_off_confirmed=confirmed)
        if confirmed:
            self.safety.write_heartbeat(target=0.0)
        else:  # leave a stale heartbeat with the target: mhapi-heatguard takes over (H5)
            self.safety.write_heartbeat()
            log.error("heater off NOT confirmed at shutdown; mhapi-heatguard will act")
        self.safety.close()
        self.save_state()

    # -- bookkeeping -----------------------------------------------------------------------------------
    def event(self, name, **detail):
        self.recent_events.append({"t": round(time.time(), 3), "event": name, "detail": detail})
        if self.session.dir is None:  # before restore() opens the session (e.g. the start-up target)
            self._early_events.append((name, detail))
            log.info("event %s %s", name, detail)
            return
        try:
            self.session.log_event(name, **detail)
        except OSError as e:
            log.warning("event log failed: %s", e)

    def save_state(self):
        if self.session.dir is None:
            return
        op = self.ops.current
        self.session.save_state({
            "session": self.session.name, "calibration_id": self.cal.id,
            "loaded_wells": self.loaded,
            "operation": op.view(with_result=False) if op else None,
            "position_check": self.position_check,
            "safety": self.safety.as_record(),
            "position_unknown": self.gantry.position_unknown,
            "position_unknown_reason": self.gantry.unknown_reason,
            "t_last_move": self.gantry.t_last_move,
        })

    def _log_temp(self, t, changed, state):
        if "bed_temper" in changed or "bed_target_temper" in changed:
            self.session.log_temp(t, state.get("bed_temper"), state.get("bed_target_temper"))

    def _on_halt(self, reason):
        self.operator.cancel("halted")
        body = lambda: self.safety.halted_error().body()  # noqa: E731
        self.ops.fail_queued(body, 409)

    def _prompt_limit(self):
        p = self.operator.current
        if p is None:
            return None
        return ("loading a well", self.p.people.max_bed_load_c) if p.kind == "load" else \
            ("cleaning the wells", self.p.people.max_bed_clean_c)

    # -- camera checks ---------------------------------------------------------------------------------
    def measure_tilt(self, frame):
        dx, dy, match = vision.tilt(frame.gray(), self.cal)
        mm = self.cal.mm_from_px([dx, dy])
        return {"px": np.array([dx, dy]), "mm": mm, "norm_mm": float(np.hypot(*mm)), "match": match}

    def check_camera(self, frame, capture):
        """C2: fail with camera_moved (and stop motion) if the camera shifted over 1 mm, jumped over 0.4 mm
        since the previous capture, or the toolhead parts can't be matched."""
        c = self.p.camera
        t = self.measure_tilt(frame)
        tilt_mm = [r(v) for v in t["mm"]]
        detail = None
        if t["match"] < c.min_match:
            detail = f"the toolhead parts can't be matched (match quality {t['match']:.2f})"
        elif t["norm_mm"] > c.stop_mm:
            detail = f"the camera shifted {t['norm_mm']:.2f} mm on its mount"
        elif self.last_capture_tilt is not None and float(np.hypot(*(t["mm"] - self.last_capture_tilt))) > c.jump_mm:
            jump = float(np.hypot(*(t["mm"] - self.last_capture_tilt)))
            detail = f"the camera jumped {jump:.2f} mm on its mount since the previous capture"
        if detail:
            self.safety.lock_camera(detail, self.cal.id, camera_tilt_mm=tilt_mm, match=r(t["match"]))
            raise ApiError("camera_moved", detail + "; motion is stopped until a person checks the mount",
                           camera_tilt_mm=tilt_mm)
        if capture:
            self.last_capture_tilt = t["mm"]
        return t

    def _position_warning(self, tilt=None):
        pc = self.position_check
        check_bad = pc is not None and (pc.get("max_mm") is None or pc["max_mm"] > self.p.motion.position_check_max_mm
                                        or bool(pc.get("unmeasured")))
        tilt_bad = tilt is not None and (tilt["norm_mm"] > self.p.camera.warn_mm or tilt["match"] < self.p.camera.min_match)
        return bool(check_bad or tilt_bad or self.safety.camera_lock)

    # -- immediate commands ----------------------------------------------------------------------------
    def get_status(self, finishing=None):
        """`finishing`: the operation whose own result this is (prepare), shown as done."""
        t = time.time()
        frame = self.camera.latest_frame()
        tilt = None
        if frame is not None:
            try:
                tilt = self.measure_tilt(frame)
            except ValueError:
                frame = None
        problems = self.printer.problems()
        homed = self.printer.homed
        camera_ok = frame is not None
        warning = self._position_warning(tilt)
        st = self.safety
        not_ready = []
        if not homed:
            not_ready.append("not homed")
        if self.gantry.position_unknown:
            not_ready.append(f"position unknown ({self.gantry.unknown_reason}); prepare() re-homes")
        not_ready += [f"printer: {p}" for p in problems]
        if not camera_ok:
            not_ready.append("camera stream not delivering frames")
        matched = tilt is not None and tilt["match"] >= self.p.camera.min_match
        if tilt is not None and not matched:
            not_ready.append(f"camera: the toolhead parts can't be matched (match quality {tilt['match']:.2f}); "
                             "the camera may have moved on its mount")
        if warning:
            not_ready.append("position_warning: recalibration needed" if not st.camera_lock
                             else f"camera moved: {st.camera_lock['detail']}")
        if st.state != "ok":
            not_ready.append(f"safety {st.state}: {st.reason}")
        state, t_report, t_bed, _ = self.printer.snapshot()
        out = {
            "t": round(t, 3), "ready": not not_ready, "homed": homed, "printer_ok": not problems,
            "camera_ok": camera_ok, "camera_tilt_mm": [r(v) for v in tilt["mm"]] if matched else None,
            "position_warning": warning,
            "position_check": None if self.position_check is None else
            {k: v for k, v in self.position_check.items() if k in ("t", "max_mm", "unmeasured")},
            "safety": st.state, "safety_reason": st.reason, "safety_detail": st.detail, "t_safety": r(st.t_state, 3),
            "idle_s": None if self.gantry.t_last_move is None else int(t - self.gantry.t_last_move),
            "loaded_wells": [dict(w) for w in self.loaded],
            "bed_c": r(self.printer.bed_c), "target_c": r(self.printer.target_c),
            "bed_age_s": None if t_bed is None else r(t - t_bed, 1),
            "session": self.session.name, "calibration_id": self.cal.id,
            "last_operation": self.ops.last_operation() or self.interrupted,
        }
        if finishing is not None:
            out["last_operation"] = {**finishing.view(with_result=False), "state": "done", "t_end": round(t, 3)}
        if not_ready:
            out["not_ready"] = not_ready
        return out

    def get_temperature(self):
        t = time.time()
        _, _, t_bed, _ = self.printer.snapshot()
        if t_bed is None:
            raise ApiError("printer_unavailable", "no temperature report from the printer yet")
        return {"bed_c": r(self.printer.bed_c), "target_c": r(self.printer.target_c), "t": round(t, 3),
                "age_s": r(t - t_bed, 1)}

    def set_temperature(self, temperature):
        value = self.safety.set_target(temperature)
        return {"target_c": r(value), "t": round(time.time(), 3)}

    def list_wells(self):
        return {"labels": self.cal.labels, "t": round(time.time(), 3)}

    def describe(self):
        return {"tools": tool_definitions(self.cal.labels)}

    def stop(self, who):
        self.safety.halt("operator_stop", f"stop from {who}")
        return {"safety": self.safety.state, "safety_reason": self.safety.reason, "safety_detail": self.safety.detail,
                "message": "Heater off and halted. Only the Clear button on the operator console ends the halt.",
                "t": round(time.time(), 3)}

    # -- queued operations -----------------------------------------------------------------------------
    def submit(self, command, args):
        fn = {"prepare": self.prepare, "get_image": lambda: self.read_well(args["label"], False, args.get("inline_image")),
              "get_raw_opacity": lambda: self.read_well(args["label"], True, args.get("inline_image")),
              "load_well": lambda: self.load_well(args["label"]), "clean_all_wells": self.clean_all_wells}[command]
        return Operation(command, args, fn)

    def _fresh_frame(self):
        f = self.camera.latest_frame()
        if f is None:
            raise ApiError("camera_unavailable", "the camera stream is not delivering frames")
        return f

    def prepare(self):
        self.safety.check_motion()
        self.check_camera(self._fresh_frame(), capture=False)
        self.gantry.preflight(need_homed=False)
        no_wells = not self.loaded
        if no_wells:
            self.session.new()
            self.event("session_started", calibration_id=self.cal.id)
            self.save_state()
        if not self.printer.homed or self.gantry.position_unknown:
            self.gantry.home()
        elif not self.gantry.at_capture_height:
            self.gantry.move_z_capture()
        if no_wells:
            self.run_position_check()
        return self.get_status(finishing=self.ops.current)

    def _ready_to_move(self, allow_home):
        """Home (no wells loaded) or refuse (wells loaded) before a move. Returns True if it homed."""
        if self.gantry.position_unknown:
            if self.gantry.unknown_reason == "move_failed":
                raise MoveFailed("the position is unknown since a move could not be confirmed; prepare() re-homes")
            if not allow_home:
                raise ApiError("not_homed", f"the position is unknown ({self.gantry.unknown_reason}); "
                                            "prepare() re-homes", loaded_wells=[w["label"] for w in self.loaded])
        if not self.printer.homed or self.gantry.position_unknown:
            if self.loaded:
                raise ApiError("not_homed", "the printer lost its home position while wells are loaded; nothing "
                                            "moved. prepare() re-homes (liquid may slosh), or stop.",
                               loaded_wells=[w["label"] for w in self.loaded])
            if not allow_home:
                raise ApiError("not_homed", "the printer is not homed; call prepare()")
            self.gantry.home()
            return True
        return False

    def read_well(self, label, opacity, inline):
        self.safety.check_motion()
        self.check_camera(self._fresh_frame(), capture=False)
        self.gantry.preflight(need_homed=False)
        rehomed = self._ready_to_move(allow_home=not self.loaded and not self.gantry.position_unknown)
        self.gantry.preflight()
        x, y, _ = self.cal.wells[label]
        last = self.gantry.goto(x, y)
        self.gantry._check_halt()    # a move under way finishes; nothing new starts
        frame = self.camera.frame_after(last.seq) if last is not None else self.camera.next()
        tilt = self.check_camera(frame, capture=True)
        img = frame.bgr()
        center = self.cal.nadir + tilt["px"]
        try:
            crop = vision.cell_crop(img, center)
        except ValueError as e:
            raise ApiError("camera_moved", f"the cell is outside the frame ({e})") from None
        n = self.session.next_capture()
        stem = f"{n:05d}_{label}"
        (self.session.frames_dir / f"{stem}.jpg").write_bytes(frame.jpg)
        png = vision.encode_png(vision.cell_image(crop))
        image_path = self.session.wells_dir / f"{stem}.png"
        image_path.write_bytes(png)
        (self.session.wells_dir / f"{stem}_crop.png").write_bytes(vision.encode_png(crop))
        state, _, t_bed, _ = self.printer.snapshot()
        out = {"label": label, "t": round(frame.t, 3)}
        if opacity:
            o = vision.opacity(img, center)
            out.update(gray=r(o["gray"], 1), pixels_total=o["pixels_total"], pixels_used=o["pixels_used"],
                       saturated_px=o["saturated_px"], reflection_px=o["reflection_px"],
                       all_saturated=o["all_saturated"], wall_gray=r(vision.wall_gray(img, center, self.cal.px_per_mm), 1))
        out["image"] = str(image_path.resolve())
        if not opacity:
            side = 2 * vision.CROP_HALF
            out.update(crop_mm=[r(side / s, 1) for s in self.cal.px_per_mm_xy],
                       size_px=[side * vision.ENLARGE] * 2,
                       mm_per_px=r(1.0 / (self.cal.px_per_mm * vision.ENLARGE), 4))
        out.update(bed_c=r(state.get("bed_temper")), target_c=r(state.get("bed_target_temper")),
                   bed_age_s=None if t_bed is None else r(max(0.0, frame.t - t_bed), 1),
                   camera_tilt_mm=[r(v) for v in tilt["mm"]], position_warning=self._position_warning(tilt),
                   rehomed=rehomed)
        if inline:
            out["image_base64"] = base64.b64encode(png).decode()
        self.event("capture", label=label, file=stem, kind="get_raw_opacity" if opacity else "get_image",
                   gray=out.get("gray"), tilt_mm=out["camera_tilt_mm"])
        return out

    def load_well(self, label):
        if self.safety.state == "halted":
            raise self.safety.halted_error()
        if any(w["label"] == label for w in self.loaded):
            raise ApiError("already_loaded", f"{label} is already loaded", label=label)
        if not self.printer.homed or self.gantry.position_unknown:
            raise ApiError("not_homed", "the instrument isn't homed; call prepare() before loading any well")
        bed = self.printer.bed_c
        if bed is None:
            raise ApiError("printer_unavailable", "no bed temperature from the printer")
        limit = self.p.people.max_bed_load_c
        if bed > limit:
            raise ApiError("bed_hot", f"the bed is {bed:.1f} °C; wells are loaded only at {limit:g} °C or below",
                           bed_c=r(bed), max_c=limit)
        t_req = time.time()
        self.event("operator_request", kind="load", label=label, bed_c=r(bed))
        p = self.operator.ask("load", f"Load egg white into well {label}. Bed {bed:.0f} °C.",
                              f"Load egg white into well {label.replace('-', ' ')}. Bed {bed:.0f} degrees.",
                              label=label, timeout=self.p.people.operator_timeout_s)
        self.event("operator_answer", kind="load", label=label, answer=p.answer, note=p.note)
        self._prompt_outcome(p)
        self.loaded.append({"label": label, "t_loaded": round(p.t_answer, 3)})
        self.save_state()
        if self.sim:
            self.sim.load(label, p.t_answer)
        return {"label": label, "t_requested": round(t_req, 3), "t_loaded": round(p.t_answer, 3),
                "bed_c": r(self.printer.bed_c), "target_c": r(self.printer.target_c)}

    def _prompt_outcome(self, p):
        if p.answer == "done":
            return
        if p.answer == "problem":
            raise ApiError("operator_declined", "the operator pressed Problem" + (f": {p.note}" if p.note else ""),
                           note=p.note)
        if p.answer == "timeout":
            raise ApiError("operator_timeout", f"no answer from the operator within "
                                               f"{self.p.people.operator_timeout_s / 60:g} min")
        if p.note == "shutdown":
            raise ApiError("service_unavailable", "the service is shutting down")
        raise self.safety.halted_error()

    def clean_all_wells(self):
        if self.safety.state == "halted":
            raise self.safety.halted_error()
        bed = self.printer.bed_c
        if bed is None:
            raise ApiError("printer_unavailable", "no bed temperature from the printer")
        limit = self.p.people.max_bed_clean_c
        if bed > limit:
            raise ApiError("bed_hot", f"the bed is {bed:.1f} °C; wells are cleaned only at {limit:g} °C or below. "
                                      "Set the temperature to 0 and wait.", bed_c=r(bed), max_c=limit)
        t_req = time.time()
        self.event("operator_request", kind="clean", bed_c=r(bed))
        p = self.operator.ask("clean", f"Clean all wells in place. Do not lift the plate or the grid. Bed {bed:.0f} °C.",
                              f"Clean all wells in place. Do not lift the plate or the grid. Bed {bed:.0f} degrees.",
                              timeout=self.p.people.operator_timeout_s)
        self.event("operator_answer", kind="clean", answer=p.answer, note=p.note)
        self._prompt_outcome(p)
        t_done = round(p.t_answer, 3)
        self.loaded = []
        self.save_state()
        if self.sim:
            self.sim.clean()
        try:  # cleaning can shift the islands
            self.safety.check_motion()
            self.check_camera(self._fresh_frame(), capture=False)
            self.gantry.preflight(need_homed=False)
            if not self.printer.homed or self.gantry.position_unknown:
                self.gantry.home()
            self.run_position_check()
        except ApiError as e:
            e.fields.update(t_requested=round(t_req, 3), t_done=t_done, wells_cleaned=True)
            e.message += " (the wells were cleaned; the position check after cleaning did not complete)"
            raise
        return {"t_requested": round(t_req, 3), "t_done": t_done, "position_warning": self._position_warning(),
                "position_check": {k: v for k, v in self.position_check.items() if k in ("t", "max_mm", "unmeasured")}}

    def run_position_check(self):
        """Visit one well per corner island and two in the center island; measure how far each opening is from
        the straight-down point (about 20 s). A well more than 0.5 mm off sets position_warning."""
        labels = [l for l in CHECK_WELLS if l in self.cal.wells]
        pos = self.gantry.pos or {}
        here = (pos["x"], pos["y"]) if "x" in pos else self.cal.wells[labels[0]][:2]

        def travel(route):
            pts = [here] + [self.cal.wells[l][:2] for l in route]
            return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        order = min(itertools.permutations(labels), key=travel)  # shortest route from here (6! = 720)
        t0 = time.time()
        results, unmeasured = {}, []
        pitch_px = 12.0 * self.cal.px_per_mm
        for label in order:
            self.gantry._check_halt()
            x, y, _ = self.cal.wells[label]
            last = self.gantry.goto(x, y)
            frame = self.camera.frame_after(last.seq) if last is not None else self.camera.next()
            tilt = self.check_camera(frame, capture=True)
            center = self.cal.nadir + tilt["px"]
            pts = vision.holes(frame.bgr())
            i, d = vision.nearest(pts, center)
            n = self.session.next_capture()
            (self.session.frames_dir / f"{n:05d}_check_{label}.jpg").write_bytes(frame.jpg)
            if i is None or d > 0.45 * pitch_px:
                results[label] = None
                unmeasured.append(label)
            else:
                results[label] = r(float(np.hypot(*self.cal.mm_from_px(pts[i] - center))), 3)
        measured = [v for v in results.values() if v is not None]
        self.position_check = {"t": round(time.time(), 3), "max_mm": max(measured) if measured else None,
                               "wells": results, "seconds": r(time.time() - t0, 1)}
        if unmeasured:
            self.position_check["unmeasured"] = unmeasured
        self.event("position_check", **self.position_check)
        self.save_state()
        if self._position_warning():
            self.announcer.say("Position check: a well is off by more than half a millimetre. Recalibrate.", chimes=1)
        return self.position_check

    # -- operator console --------------------------------------------------------------------------------
    def console_state(self):
        st = self.safety
        prompt = self.operator.view()
        op = self.ops.current
        return {
            "t": round(time.time(), 3), "sim": bool(self.sim), "session": self.session.name,
            "calibration_id": self.cal.id, "prompt": prompt,
            "safety": {"state": st.state, "reason": st.reason, "detail": st.detail, "t": r(st.t_state, 3)},
            "camera_lock": st.camera_lock, "clear_needed": st.state == "halted" or bool(st.camera_lock),
            "homed": self.printer.homed, "printer_problems": self.printer.problems(), "camera_ok": self.camera.ok,
            "bed_c": r(self.printer.bed_c), "target_c": r(self.printer.target_c),
            "target_confirmed": st.confirmed, "power_assertion": st.power_assertion,
            "loaded_wells": self.loaded, "position_check": self.position_check,
            "position_unknown": self.gantry.position_unknown and self.gantry.unknown_reason,
            "operation": op.view(with_result=False) if op else None, "queued": self.ops.queued,
            "events": list(self.recent_events)[-15:][::-1],
        }


QUEUED = {name for name, spec in TOOLS.items() if spec["queued"]}
