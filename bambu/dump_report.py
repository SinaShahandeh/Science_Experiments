"""Dump the full raw pushall report, and every report seen while the head moves."""
import json, time
from bambu_link import Printer

p = Printer()
p.pushall(); time.sleep(3)
full = [r for _, r in p.reports if r.get("print", {}).get("command") == "push_status"]
json.dump(full[-1] if full else p.reports, open("report_full.json", "w"), indent=1)
n0 = len(p.reports)
p.gcode(["G90", "G1 X60 Y60 Z40 F3000", "G1 X200 Y200 F3000", "G1 X128 Y128 Z30 F3000"])
time.sleep(15)
moving = [r for _, r in p.reports[n0:]]
json.dump(moving, open("report_moving.json", "w"), indent=1)
print("reports during motion:", len(moving))
changed = set()
for r in moving:
    changed |= set(r.get("print", {}).keys())
print("keys that appeared during motion:", sorted(changed))
p.close()
