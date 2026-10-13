"""Subscribe to every topic (#), send position-report G-codes, capture everything."""
import json, time
from bambu_link import Printer

p = Printer()
p.c.subscribe("#")
time.sleep(1)
topics = {}
orig = p.c.on_message
def on_msg(c, u, msg):
    topics.setdefault(msg.topic, 0); topics[msg.topic] += 1
    orig(c, u, msg)
p.c.on_message = on_msg

n0 = len(p.reports)
for g in ["M114", "M114 D", "M114 R", "M115", "M503", "M119"]:
    p.gcode([g]); time.sleep(2)
p.pushall(); time.sleep(3)
p.publish({"info": {"sequence_id": "900", "command": "get_version"}}); time.sleep(2)

new = [r for _, r in p.reports[n0:]]
print("topics seen:", topics)
print("top-level keys seen:", sorted({k for r in new for k in r}))
for r in new:
    s = json.dumps(r)
    if "mc_print" in r or "gcode_line" in s or any(t in s for t in ("X:", "Count", "M114")):
        print(s[:400])
p.close()
