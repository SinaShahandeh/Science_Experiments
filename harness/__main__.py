"""Run the harness and its dashboard: `python -m harness [--port 8750] [--rig URL] [--public]`.

The rig URL defaults to MHAPI_URL, else the digital twin on http://127.0.0.1:8743. Pointed at mhapi-service
(http://127.0.0.1:8742), the dashboard shows the rig's two cameras (harness/feeds.py). The Anthropic API key is
read from the environment or the repository's .env (ANTHROPIC_API_KEY).

--public is for a deployment anyone can open (deploy/): one run at a time, spending caps and runs per visitor
(harness/limits.py). The caps default to the AISIM_* environment variables; AISIM_ADMIN_TOKEN, when set,
lets its holder bypass them.
"""
import argparse
import os
import signal
import sys

import anthropic

from . import DEFAULT_PORT, MODEL, sandbox
from .env import ROOT, load_dotenv
from .feeds import PI_URL, instrument_feeds
from .limits import Limits
from .record import Bus
from .rig import Rig
from .server import Harness, serve


def env(name, default):
    return type(default)(os.environ.get(name, default))


def main():
    ap = argparse.ArgumentParser(prog="python -m harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rig", default=os.environ.get("MHAPI_URL", "http://127.0.0.1:8743"))
    ap.add_argument("--runs", default=str(ROOT / "harness_runs"))
    ap.add_argument("--pi-camera", default=PI_URL, help="the Pi camera's MJPEG stream, shown in instrument mode")
    ap.add_argument("--public", action="store_true", default=env("AISIM_PUBLIC", 0) == 1,
                    help="limits for a public deployment (also AISIM_PUBLIC=1)")
    ap.add_argument("--run-usd", type=float, default=env("AISIM_RUN_USD", 6.0), help="dollars per run (public)")
    ap.add_argument("--day-usd", type=float, default=env("AISIM_DAY_USD", 40.0), help="dollars per UTC day (public)")
    ap.add_argument("--run-min", type=float, default=env("AISIM_RUN_MIN", 90.0), help="minutes of agent work per run (public)")
    ap.add_argument("--ip-runs", type=int, default=env("AISIM_IP_RUNS", 4), help="runs per visitor per day (public)")
    a = ap.parse_args()
    load_dotenv()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("warning: no ANTHROPIC_API_KEY in the environment or .env; runs will fail", file=sys.stderr)
    rig = Rig(a.rig)
    if a.public and not rig.is_twin:   # the real rig's cameras and operator console stay off public servers
        raise SystemExit(f"--public serves the digital twin only, and {rig.url} is not one (or isn't answering)")
    client = anthropic.Anthropic(max_retries=3, timeout=600.0)
    limits = Limits(os.path.join(a.runs, "_limits.json"), public=a.public, run_usd=a.run_usd, day_usd=a.day_usd,
                    run_min=a.run_min, ip_runs=a.ip_runs, admin_token=os.environ.get("AISIM_ADMIN_TOKEN", ""))
    sandbox.REQUIRE = a.public  # never run the agent's code unsandboxed on a public host
    h = Harness(rig, Bus(), a.runs, client, limits, feeds=None if rig.is_twin else instrument_feeds(a.pi_camera))
    httpd = serve(h, a.host, a.port)
    print(f"harness dashboard on http://{a.host}:{a.port}  (model {MODEL}; rig {rig.url}, "
          f"{'digital twin' if rig.is_twin else 'instrument'})", flush=True)
    if a.public:
        print(f"  public: one run at a time, ${a.run_usd:g}/run, {a.run_min:g} min/run, ${a.day_usd:g}/day, "
              f"{a.ip_runs} runs/visitor/day", flush=True)

    def bye(*_):
        raise SystemExit(0)  # interrupts serve_forever in the main thread

    signal.signal(signal.SIGTERM, bye)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if h.run and h.run.thread.is_alive():  # let the run record that it stopped
            h.run.stop("the harness was shut down")
            h.run.thread.join(timeout=10)
        httpd.server_close()


if __name__ == "__main__":
    main()
