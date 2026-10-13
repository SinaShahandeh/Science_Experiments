"""`mhapi-service`: the long-lived hardware service, one per rig (Model_Hardware_API_spec.md, "CLI and service").

    mhapi-service                 # the rig: printer over MQTT, Pi camera stream, http://127.0.0.1:8742
    mhapi-service --sim           # simulated printer and camera, sessions_sim/, http://127.0.0.1:8744

Start-up: load the calibration (refuse without it), find the printer by serial number and connect, check
its firmware, open the camera stream, restore state.json, start the safety layer, request a status report
every 5 s. Ctrl-C switches the heater off (confirmed) before exiting.
"""
import argparse
import json
import logging
import logging.handlers
import sys
import time

import uvicorn

from ..bambu import PrinterLink
from .app import create_app
from .calibration import Calibration, CalibrationError
from .camera import CameraStream, mjpeg_frames
from .config import CAMERA_URL, PORT, REPO, SAFETY_TOML, Params, Paths
from .operator import Announcer
from .printer import Printer
from .rig import Rig

log = logging.getLogger("mhapi")
SIM_PORT = 8744


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="mhapi-service", description=__doc__.split("\n")[0])
    ap.add_argument("--sim", action="store_true", help="simulated printer and camera (no hardware)")
    ap.add_argument("--port", type=int, help=f"default {PORT}, or {SIM_PORT} with --sim")
    ap.add_argument("--sessions-dir", help="default sessions/ (sessions_sim/ with --sim)")
    ap.add_argument("--params", default=str(SAFETY_TOML), help="safety parameters file")
    ap.add_argument("--camera-url", default=CAMERA_URL)
    ap.add_argument("--quiet", action="store_true", help="no chimes or speech (always quiet with --sim)")
    ap.add_argument("--log-level", default="INFO")
    sim = ap.add_argument_group("simulator only")
    sim.add_argument("--set", action="append", default=[], metavar="SECTION.NAME=VALUE",
                     help="override a safety parameter, e.g. heat.heater_watchdog_s=20")
    sim.add_argument("--sim-homed", action="store_true", help="start the simulated printer homed, at Z50")
    sim.add_argument("--sim-target", type=float, default=0.0, metavar="C",
                     help="the simulated bed is already heating to this target at start-up")
    sim.add_argument("--sim-auto-operator", type=float, metavar="SECONDS",
                     help="press Done automatically this long after each operator request")
    sim.add_argument("--sim-sounds", action="store_true", help="chimes and speech in the simulator")
    return ap.parse_args(argv)


def setup_logging(level, folder):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level.upper())
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root.addHandler(h)
    fh = logging.handlers.RotatingFileHandler(folder / "service.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("uvicorn.access", "paho"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Server(uvicorn.Server):
    """Answers pending calls before uvicorn waits for them to finish."""

    rig = None

    def handle_exit(self, sig, frame):
        if self.rig is not None and not self.should_exit:
            log.warning("shutting down (signal %s)", sig)
            self.rig.begin_shutdown()
        super().handle_exit(sig, frame)


def build(args):
    overrides = {}
    if args.set and not args.sim:
        raise SystemExit("mhapi-service: --set is for the simulator; edit the safety parameters file instead")
    for s in args.set:
        key, _, val = s.partition("=")
        overrides[key.strip()] = json.loads(val)
    params = Params.load(args.params, overrides)
    sessions = args.sessions_dir or (REPO / ("sessions_sim" if args.sim else "sessions"))
    paths = Paths(sessions=sessions)
    if args.sim:
        paths.position_file = paths.sessions / "position.json"
    setup_logging(args.log_level, paths.sessions)

    try:  # 3. the calibration (checked first: no point connecting without it)
        cal = Calibration(paths.calibration)
    except CalibrationError as e:
        raise SystemExit(f"mhapi-service: refusing to start: {e}")
    log.info("calibration %s: %d wells, nadir %s", cal.id, len(cal.wells), cal.nadir.tolist())
    allowed_xy = [(x, y) for x, y, _ in cal.wells.values()] + [tuple(params.motion.park_xy)]
    announcer = Announcer(enabled=not args.quiet and (not args.sim or args.sim_sounds))

    world = sim_control = None
    if args.sim:
        from ..sim.camera import SimWorld
        from ..sim.printer import SimPrinter
        cal.pitch_model = None  # the simulated camera is an ideal lens: a uniform 12 mm pitch at Z50
        holder = {}

        def link_factory(cb):
            sp = SimPrinter(cb, park_xy=params.motion.park_xy, homed=args.sim_homed,
                            pos=(cal.wells["C-B2"][0], cal.wells["C-B2"][1], 50.0) if args.sim_homed else
                            (params.motion.park_xy[0], params.motion.park_xy[1], 50.0),
                            bed=max(24.0, args.sim_target), target=args.sim_target)
            holder["printer"] = sp
            return sp
        printer = Printer(link_factory, params, allowed_xy)
        world = SimWorld(cal, holder["printer"])
        source = world.frames
    else:
        printer = Printer(lambda cb: PrinterLink(params.printer.serial, params.printer.ip_hint, cb), params, allowed_xy)
        source = lambda stop: mjpeg_frames(args.camera_url, stop)  # noqa: E731

    # 1. the printer, found by serial number; N4: refuse any other firmware
    try:
        printer.start()
    except (OSError, TimeoutError, KeyError) as e:
        raise SystemExit(f"mhapi-service: can't connect to the printer: {e!r}")
    t0 = time.time()
    while (printer.firmware is None or printer.t_report is None) and time.time() - t0 < 15:
        if time.time() - t0 > 5:
            printer.request_version()
            printer.request_report()
        time.sleep(0.5)
    if printer.firmware is None:
        raise SystemExit("mhapi-service: the printer did not report its firmware version")
    if printer.firmware != params.printer.firmware:
        raise SystemExit(f"mhapi-service: refusing to run on printer firmware {printer.firmware}; "
                         f"only {params.printer.firmware} is allowed (safety spec N4)")
    log.info("printer connected: firmware %s, home bits %s, bed %s/%s °C", printer.firmware,
             format(printer.home_flag & 0b111, "03b"), printer.bed_c, printer.target_c)

    camera = CameraStream(source)  # 2. the camera stream
    t0 = time.time()
    while camera.latest is None and time.time() - t0 < 10:
        time.sleep(0.2)
    if camera.latest is None:
        log.error("no camera frames yet; starting anyway (camera_ok false)")

    rig = Rig(params, paths, printer, camera, cal, announcer, sim=world,  # 4. state.json
              auto_operator_s=args.sim_auto_operator if args.sim else None)
    rig.restore()
    rig.safety.start()  # 5. the safety layer; 6. status reports every 5 s run in the printer

    if args.sim:
        def sim_control(action, body):
            sp = world.printer
            if action == "state":
                return {"printer": sp.view(), "camera": {"shift_px": list(world.shift_px), "frozen": world.frozen},
                        "wells": {k: round(v["alpha"], 3) for k, v in world.loaded.items()}}
            if action != "fault":
                raise KeyError(f"unknown simulator action {action!r}")
            name, value = body["name"], body.get("value")
            if name == "camera_shift":
                world.shift_px = (float(value[0]), float(value[1]))
            elif name == "camera_freeze":
                world.frozen = bool(value)
            elif name == "island_offset":
                world.set_island_offset(value["island"], value.get("dx", 0.0), value.get("dy", 0.0))
            else:
                sp.set_fault(name, value)
            return {"fault": name, "value": value}
    return rig, create_app(rig, sim_control), printer, camera


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    rig, app, printer, camera = build(args)
    port = args.port or (SIM_PORT if args.sim else PORT)
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                   timeout_graceful_shutdown=5, lifespan="off"))
    server.rig = rig
    log.info("mhapi-service listening on http://127.0.0.1:%d (console: /operator)%s", port,
             " [SIMULATOR]" if args.sim else "")
    try:
        server.run()
    finally:
        rig.begin_shutdown()
        rig.shutdown()
        camera.close()
        printer.close()
        log.info("mhapi-service stopped")


if __name__ == "__main__":
    main()
