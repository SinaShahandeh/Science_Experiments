# Bambu A1 — local control exploration (2026-10-10)

Printer: **Bambu Lab A1** ("inventors lab"), IP `192.168.4.52`, serial `03919D532700850`,
firmware 01.08.01.00, LAN mode, not cloud-bound.

## Channels that work
| Port | Protocol | Use |
|------|----------|-----|
| 8883 | MQTT over TLS (user `bblp`, pw = LAN access code) | status reports + commands (incl. raw G-code) |
| 6000 | TLS JPEG stream | camera snapshots (`snapshot.py`) |
| 990  | FTPS | file upload to SD (not tested yet) |

The access code is read at runtime from Bambu Studio's config
(`~/Library/Application Support/BambuStudio/BambuStudio.conf` → `access_code[serial]`).

## Files
- `bambu_link.py` — `Printer` class: connect, `pushall()`, `gcode(lines)`, live `state` dict.
- `camstream.py` — continuous reader for the Pi MJPEG stream (latest frame + arrival time).
- `well_px.py`, `well_gray.py` — where each well lands in a frame, and its robust gray value.
- `calibrate_exposure.py` — lock the Pi camera's tone (results in `../calibration/exposure/result.json`).
- `kinetics_run.py` — egg-white clouding run: pre-hold, operator loads, ramp, camera loop over wells.
- `open_grid_2mm/` — the well grid on the bed: generator (`make_grid.py`), STLs, project `.3mf`, slicer settings.

The early exploration scripts (`probe_status.py`, `snapshot.py`, `jog_test.py`, `x_test.py`,
`absolute_test.py`, `m114_test.py`, `dump_report.py`) are not kept; their results are recorded below.

## Findings
- Raw G-code via `{"print": {"command": "gcode_line", "param": "..."}}` on
  `device/<serial>/request` is accepted while the printer is IDLE (`result: success`).
- `G28` homes all axes (~39 s). `home_flag` low 3 bits go `000 → 111` (X, Y, Z homed).
- After homing, absolute `G90` / `G1 X.. Y.. Z.. F..` moves land where expected (camera-verified).
- Y feedrate (`F`) is respected: 196 mm stroke took ~24 s @10 mm/s, ~10 s @50, ~6–8 s @150
  (measured times include ~5 s of observation latency, see below).
- Before homing, soft endstops assume the current position is 0, so negative relative jogs are
  clamped to no motion. **Always home first.**
- The printer does **not** report XYZ position over MQTT. We must track commanded position ourselves.
  Confirmed against the community protocol doc (OpenBambuAPI `mqtt.md`: no position field in the
  report schema) and on the device (`m114_test.py`, `dump_report.py`): `M114`/`M114 D`/`M114 R`/
  `M115`/`M503`/`M119` are acked with `result: success` but no output comes back on any topic
  (subscribed to `#`), no `mc_print.push_info` log lines appear, and no report field changes
  while the head moves. The `gcode_line` ack means "received", not "executed".
- Completion detection trick: queue `M400` then a visible state change (here `M106 P1 S255`, the
  part fan), and watch `cooling_fan_speed` in the report. Latency from command to observed
  completion is about 5 s; still to be split into command vs. report latency.
- Camera is mounted on the Z gantry (right side) and looks along X; view shifts with Z.

## Axes / envelope (A1)
X: toolhead, Y: bed, Z: gantry. Build volume 256 × 256 × 256 mm.

## Camera-over-well calibration (2026-10-11)
Label → printer XYZ table and method: see `../Model_Hardware_API_spec.md` (label-position mapping).
- `home_safe.py` — Bambu-style homing: XY, then Z on bare steel at X128 Y254 (never plain `G28` with the grid on the bed).
- `motion.py` — slow absolute moves (F600 XY, F300 Z, M204 S500) with completion wait; tracks position in `../calibration/position.json`.
- `pi_frame.py` — one fresh frame from the Pi MJPEG stream.
- `wells.py`, `jacobian.py`, `nadir.py`, `calibrate_wells.py`, `fit_wells.py`, `verify_wells.py` — recalibration pipeline.
- `verify_vertical.py` — independent check: image each well at its fitted XY at Z50 and Z100.
