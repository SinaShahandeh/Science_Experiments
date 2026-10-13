"""`mhapi-heatguard`: switches the bed heater off if mhapi-service dies while heating (safety spec H5).

Runs as its own small process, started by launchd with KeepAlive (mhapi/launchd/). Every 5 s it reads the
heartbeat the service rewrites every 5 s, {t, pid, target_c, wells_loaded}. If the heartbeat is older than
120 s and its target was above 0, it connects to the printer itself, sends M140 S0, waits for a report with
bed_target_temper = 0 (resending every 5 s, alerting a person after 60 s) and announces what it did. While
the last known target is above 0 it also keeps the Mac awake (H8), since a sleeping Mac would stop it too.

It imports only mhapi.bambu (the MQTT link), never the service.
"""
import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .bambu import PrinterLink

REPO = Path(__file__).resolve().parents[1]
log = logging.getLogger("mhapi.heatguard")


def alert(text):
    log.warning("ALERT: %s", text)
    try:
        for _ in range(3):
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], timeout=10, check=False)
        subprocess.run(["say", text], timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def read_heartbeat(path):
    try:
        hb = json.loads(Path(path).read_text())
        return hb if isinstance(hb, dict) and "t" in hb else None
    except (OSError, ValueError):
        return None


def write_heartbeat(path, hb):
    tmp = Path(path).with_suffix(".guard.tmp")
    tmp.write_text(json.dumps(hb))
    os.replace(tmp, path)


def heater_off(serial, ip_hint, resend_s=5.0, alert_s=60.0, give_up_s=900.0):
    """Send M140 S0 until a report confirms bed_target_temper = 0 (H3). Returns True when confirmed."""
    seen = {}

    def on_report(t, payload):
        p = payload.get("print")
        if isinstance(p, dict) and p.get("command") == "push_status" and "bed_target_temper" in p:
            seen["target"], seen["t"] = float(p["bed_target_temper"]), t

    link = PrinterLink(serial, ip_hint, on_report).start()
    seq = random.randint(9_000_000, 9_900_000)
    t0, t_first, last, alerted = time.time(), None, 0.0, False
    try:
        while time.time() - t0 < give_up_s:
            if time.time() - last >= resend_s:
                seq += 2
                if link.publish({"print": {"sequence_id": str(seq), "command": "gcode_line", "param": "M140 S0\n"}}):
                    t_first = t_first or time.time()
                link.publish({"pushing": {"sequence_id": str(seq + 1), "command": "pushall"}})
                last = time.time()
            if t_first and seen.get("target") == 0 and seen.get("t", 0) > t_first:
                return True
            if not alerted and time.time() - t0 > alert_s:
                alerted = True
                alert("Heat guard. The printer has not confirmed that the bed heater is off. Check the printer.")
            time.sleep(0.5)
    finally:
        link.close()
    return False


class Caffeinate:
    def __init__(self):
        self.proc = None

    def set(self, on):
        if on and self.proc is None:
            self.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not on and self.proc is not None:
            self.proc.terminate()
            self.proc = None


def check(hb, now, stale_s):
    """What to do about one heartbeat: None, or the reason to switch the heater off."""
    if hb is None or hb.get("sim"):
        return None
    target = float(hb.get("target_c") or 0)
    age = now - float(hb["t"])
    if target > 0 and age > stale_s:
        return f"no heartbeat from mhapi-service (pid {hb.get('pid')}) for {age:.0f} s with the bed target at {target:g} °C"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mhapi-heatguard", description="Switch the bed heater off if mhapi-service dies.")
    ap.add_argument("--heartbeat", default=str(REPO / "sessions" / "heartbeat.json"))
    ap.add_argument("--params", default=str(REPO / "mhapi" / "safety.toml"))
    ap.add_argument("--dry-run", action="store_true", help="log what it would do; don't connect to the printer")
    ap.add_argument("--once", action="store_true", help="check the heartbeat once and exit")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = tomllib.loads(Path(args.params).read_text())
    stale_s = float(p["heat"]["heatguard_stale_s"])
    serial, ip_hint = p["printer"]["serial"], p["printer"]["ip_hint"]
    awake = Caffeinate()
    log.info("watching %s (acts after %g s without a heartbeat while heating)", args.heartbeat, stale_s)
    try:
        while True:
            hb = read_heartbeat(args.heartbeat)
            if not args.dry_run:
                awake.set(hb is not None and not hb.get("sim") and float(hb.get("target_c") or 0) > 0)
            reason = check(hb, time.time(), stale_s)
            if reason:
                log.warning("%s: switching the heater off", reason)
                if args.dry_run:
                    print(json.dumps({"action": "heater_off", "reason": reason}), flush=True)
                    confirmed = True
                else:
                    alert("The hardware service stopped while the bed was heating. Switching the heater off.")
                    try:
                        confirmed = heater_off(serial, ip_hint)
                    except Exception as e:  # noqa: BLE001 - keep guarding; launchd restarts us anyway
                        log.error("could not reach the printer: %r", e)
                        confirmed = False
                if confirmed:
                    write_heartbeat(args.heartbeat, {**hb, "target_c": 0.0,
                                                     "heatguard": {"t": round(time.time(), 3), "reason": reason}})
                    if not args.dry_run:
                        alert("Heat guard. The bed heater is off.")
                elif not args.dry_run:
                    alert("Heat guard could not switch the bed heater off. Switch the printer off.")
            elif args.once:
                print(json.dumps({"action": None}), flush=True)
            if args.once:
                return 0
            time.sleep(5.0)
    except KeyboardInterrupt:
        return 0
    finally:
        awake.set(False)


if __name__ == "__main__":
    sys.exit(main())
