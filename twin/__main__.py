"""Run the digital twin: `python -m twin [--port 8743] [--speed 1] [--manual-operator]`."""
import argparse
import signal
import sys

from . import DEFAULT_PORT
from .geometry import ROOT
from .rig import Rig
from .server import serve


def main():
    ap = argparse.ArgumentParser(prog="python -m twin", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="simulated seconds per wall second while the agent isn't thinking (default 1)")
    ap.add_argument("--manual-operator", action="store_true",
                    help="don't simulate the person: answer load/clean requests on /operator or the dashboard")
    ap.add_argument("--sessions", default=str(ROOT / "sim_sessions"), help="where session folders go")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    rig = Rig(a.sessions, speed=a.speed, auto_operator=not a.manual_operator, seed=a.seed)
    httpd = serve(rig, a.host, a.port)
    url = f"http://{a.host}:{a.port}"
    print(f"mhapi digital twin on {url}  (operator console: {url}/operator)", flush=True)
    print(f"  CLI:  MHAPI_URL={url} python -m mhapi.cli get_status", flush=True)

    def bye(*_):
        raise SystemExit(0)  # interrupts serve_forever in the main thread

    signal.signal(signal.SIGTERM, bye)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        rig.bed.set_target(0)  # like the service: heater off on shutdown
        httpd.server_close()


if __name__ == "__main__":
    main()
