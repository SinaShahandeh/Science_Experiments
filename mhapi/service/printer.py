"""Printer state from MQTT reports, command acknowledgements, the periodic status request, and the only
path that sends G-code (through the allowlist).

The link is a mhapi.bambu.PrinterLink, or the simulator's SimPrinter: both have start(), publish(payload),
`connected`, close(), and call on_report(t, payload) for every report.
"""
import logging
import random
import threading
import time

from . import gcode
from .errors import ApiError

log = logging.getLogger("mhapi.printer")
HOME_BITS = 0b111
STANDARD_SPEED = (2, 100)  # spd_lvl, spd_mag
PRINT_JOB_STATES = ("PREPARE", "RUNNING", "PAUSE")


class Printer:
    def __init__(self, link_factory, params, allowed_xy):
        self.params = params
        self.allowed_xy = allowed_xy
        self.state = {}
        self.t_report = None     # last status report of any kind
        self.t_bed = None        # last report with bed_temper
        self.t_target = None     # last report with bed_target_temper
        self.firmware = None
        self.listeners = []      # callables(t, changed_fields, state): the safety layer, the temperature log
        self.on_reconnect = []   # callables(): after the link comes back
        self._lock = threading.Lock()
        self._acks = {}
        self._ack_cv = threading.Condition()
        self._seq = random.randint(2_000_000, 8_000_000)
        self._stop = threading.Event()
        self._fast_until = 0.0
        self.link = link_factory(self._on_report)

    # -- lifecycle -------------------------------------------------------------------------------------
    def start(self, timeout=10.0):
        self.link.start(timeout)
        self.request_version()
        self.request_report()
        threading.Thread(target=self._poll, name="pushall", daemon=True).start()

    def close(self):
        self._stop.set()
        self.link.close()

    def _poll(self):
        last, was_up = 0.0, self.connected
        while not self._stop.wait(0.25):
            up = self.connected
            if up and not was_up:  # reconnected: Z counts as unknown again (M3); re-check the firmware (N4)
                log.warning("printer link back up")
                self.firmware = None
                self.request_version()
                for fn in self.on_reconnect:
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        log.exception("reconnect handler failed")
            was_up = up
            period = 1.0 if time.time() < self._fast_until else self.params.printer.pushall_s
            if time.time() - last >= period:
                last = time.time()
                self.request_report()

    def fast_reports(self, seconds):
        """Request a report every second for a while (homing waits on the home bits)."""
        self._fast_until = max(self._fast_until, time.time() + seconds)

    # -- requests --------------------------------------------------------------------------------------
    def _next_seq(self):
        self._seq += 1
        return str(self._seq)

    def request_report(self):
        return self.link.publish({"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}})

    def request_version(self):
        return self.link.publish({"info": {"sequence_id": self._next_seq(), "command": "get_version"}})

    def send(self, lines, homing=False):
        """Send G-code through the allowlist. Returns (sequence id, send time)."""
        lines = list(lines)
        try:
            gcode.check(lines, allowed_xy=self.allowed_xy, motion=self.params.motion,
                        max_target_c=self.params.heat.max_target_c, homing=homing)
        except gcode.GcodeRejected:
            log.error("G-code rejected by the allowlist: %s", lines)
            raise
        seq = self._next_seq()
        t = time.time()
        payload = {"print": {"sequence_id": seq, "command": "gcode_line", "param": "\n".join(lines) + "\n"}}
        if not self.link.publish(payload):
            raise ApiError("printer_unavailable", "the printer connection is down; the command was not sent")
        log.debug("sent %s: %s", seq, lines)
        return seq, t

    def wait_ack(self, seq, timeout=3.0):
        """The acknowledgement (a gcode_line report with the same sequence id): (t, result) or None."""
        with self._ack_cv:
            self._ack_cv.wait_for(lambda: seq in self._acks, timeout)
            return self._acks.pop(seq, None)

    # -- reports ---------------------------------------------------------------------------------------
    def _on_report(self, t, payload):
        info = payload.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            for m in info.get("module", []):
                if m.get("name") == "ota":
                    self.firmware = m.get("sw_ver")
        p = payload.get("print")
        if not isinstance(p, dict):
            return
        cmd = p.get("command")
        if cmd == "gcode_line":
            seq = str(p.get("sequence_id"))
            with self._ack_cv:
                self._acks[seq] = (t, p.get("result"))
                if len(self._acks) > 200:
                    self._acks.pop(next(iter(self._acks)))
                self._ack_cv.notify_all()
            return
        if cmd != "push_status":
            return
        with self._lock:
            self.state.update(p)
            self.t_report = t
            if "bed_temper" in p:
                self.t_bed = t
            if "bed_target_temper" in p:
                self.t_target = t
            snapshot = dict(self.state)
        for fn in self.listeners:
            try:
                fn(t, p, snapshot)
            except Exception:  # noqa: BLE001
                log.exception("report listener failed")

    # -- derived state ---------------------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return dict(self.state), self.t_report, self.t_bed, self.t_target

    @property
    def connected(self):
        return self.link.connected

    @property
    def epoch(self):
        """Which connection this is: changes whenever the link to the printer is re-established."""
        return getattr(self.link, "epoch", 0)

    @property
    def bed_c(self):
        v = self.state.get("bed_temper")
        return None if v is None else float(v)

    @property
    def target_c(self):
        v = self.state.get("bed_target_temper")
        return None if v is None else float(v)

    @property
    def home_flag(self):
        return int(self.state.get("home_flag", 0) or 0)

    @property
    def homed(self):
        return (self.home_flag & HOME_BITS) == HOME_BITS

    @property
    def speed_ok(self):
        try:
            return (int(self.state.get("spd_lvl")), int(self.state.get("spd_mag"))) == STANDARD_SPEED
        except (TypeError, ValueError):
            return False

    @property
    def report_age(self):
        return None if self.t_report is None else time.time() - self.t_report

    def problems(self):
        """Why printer_ok is false (empty when it is true)."""
        out = []
        if not self.connected:
            out.append("MQTT connection down")
        age = self.report_age
        if age is None or age > self.params.printer.report_max_age_s:
            out.append("no status report in the last %g s" % self.params.printer.report_max_age_s)
        if self.state.get("print_error"):
            out.append(f"print_error {self.state.get('print_error')}")
        if self.state.get("hms"):
            out.append(f"HMS alerts {self.state.get('hms')}")
        if self.state and not self.speed_ok:
            out.append(f"speed mode is not Standard (spd_lvl {self.state.get('spd_lvl')}, "
                       f"spd_mag {self.state.get('spd_mag')})")
        if self.firmware and self.firmware != self.params.printer.firmware:
            out.append(f"firmware {self.firmware}, expected {self.params.printer.firmware}")
        return out
