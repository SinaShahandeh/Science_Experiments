import json, time
from bambu_link import Printer

p = Printer()
print("connected")
p.pushall()
time.sleep(4)
keys = ["gcode_state", "print_type", "mc_print_stage", "home_flag", "hw_switch_state",
        "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper",
        "spd_lvl", "spd_mag", "lights_report", "sdcard", "wifi_signal", "print_error",
        "hms", "nozzle_diameter", "nozzle_type", "ipcam", "upgrade_state", "mc_percent",
        "subtask_name", "fan_gear", "xcam", "stg_cur", "command"]
for k in keys:
    if k in p.state:
        v = p.state[k]
        if k == "upgrade_state": v = {kk: v.get(kk) for kk in ("ota_new_version_number", "new_version_state", "status")}
        print(f"{k:22s} {json.dumps(v)[:200]}")
print("\nall keys:", sorted(p.state.keys()))
p.close()
