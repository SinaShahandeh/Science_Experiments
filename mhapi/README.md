# mhapi: the Model Hardware API

The `mhapi` CLI and the `mhapi-service` it talks to, as specified in
[Model_Hardware_API_spec.md](../Model_Hardware_API_spec.md) (commands, CLI and service) and
[Hardware_Safety_spec.md](../Hardware_Safety_spec.md) (the safety layer).

```
bin/mhapi-service              # the rig: http://127.0.0.1:8742, operator console at /operator
bin/mhapi get_status           # the agent's commands, one JSON object per call
bin/mhapi-service --sim        # the same service on a simulated printer and camera: http://127.0.0.1:8744
MHAPI_URL=http://127.0.0.1:8744 bin/mhapi prepare
```

The wrappers in `bin/` run with `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`, which
has OpenCV, paho-mqtt, FastAPI and uvicorn. To call `mhapi` from anywhere, put `bin/` on the PATH or link
it (`ln -s "$PWD/bin/mhapi" ~/.local/bin/mhapi`); the wrappers follow symlinks.

## Layout

| Path | What |
|---|---|
| `tools.py` | The agent-facing tool text and arguments: the one copy, served by `describe` and `--help`. |
| `client.py` | Standard-library HTTP client, `call(command, args, timeout) -> (status, body)`, for the CLI and a Python harness. |
| `cli.py` | `mhapi`. Prints exactly one JSON object; exit 0 / 1 (service error, `client_timeout`) / 2 (`invalid_argument`) / 3 (`service_unavailable`). |
| `bambu.py` | MQTT link to the A1 (connect, rediscover by serial number, publish, reports). No command policy. |
| `service/` | `mhapi-service`: `app.py` (HTTP), `rig.py` (the commands), `motion.py` (confirmed moves, safe homing), `safety.py` (safety states, heat rules, watchdogs), `gcode.py` (allowlist), `printer.py`, `camera.py`, `vision.py` (tilt, cell image, opacity, wall gray, openings, height), `opqueue.py` (one-at-a-time queue), `operator.py`, `session.py`, `console.html`. |
| `safety.toml` | Safety parameters, for people only. |
| `heatguard.py` | `mhapi-heatguard` (H5): switches the heater off if the service dies while heating. Imports only `bambu.py`. |
| `launchd/` | LaunchAgent for the heat guard (not installed; see the file for the two commands). |
| `sim/` | Simulated A1 (motion, homing, bed heating, faults) and camera (renders the grid from the calibration). |

The service writes `sessions/<YYYY-MM-DD_HHMMSS>/` (git-ignored) with `calls.jsonl`, `temps.csv`,
`events.csv`, `frames/`, `wells/`, `state.json` and `calibration.json`, plus `service.log` and the heat
guard's `heartbeat.json` in `sessions/`. The simulator uses `sessions_sim/`.

## Tests

```
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m unittest discover -s tests -v
```

About 10 minutes, no hardware: unit tests for the CLI, the allowlist, the safety rules and the frame
processing (on the calibration frames when present), then the real service on the simulator through the
real CLI, covering every command and the safety spec's test plan cases 1-3 and 5-14 (7 with the
simulated link dropping), plus N4 and M3.

## Choices the specs left open

- **`list_wells`** returns `{"labels": [...], "t": ...}`: every response is one JSON object.
- **Halts say what happened.** A halt keeps its detail (e.g. "the bed target changed to 0 °C, which the service
  didn't set"): `get_status` has `safety_detail`, and every `halted` error carries `safety_reason`, `safety_detail`
  and `t_safety`, with a message saying since when, why, that the heater is off, and that a person must press Clear.
- **`get_status`** also has `safety_reason`, `safety_detail` and `t_safety` (safety spec) and, when `ready` is false,
  `not_ready`: the reasons, for the agent's report. `camera_tilt_mm` is null when the toolhead can't be
  matched. `last_operation` is `{command, args, state, t_start, t_end, result}` with state `running`,
  `done`, `failed` or `interrupted` (the service restarted during it).
- **Passive status read:** `GET /operator/status` returns get_status's body for people watching (the
  harness dashboard polls it every 2 s). It is not an API call: it doesn't reset the heater watchdog (H4)
  and isn't logged in `calls.jsonl`, so an open dashboard can't keep a stalled agent's bed hot. The CLI and
  the agent's tools don't use it; the twin serves it too.
- **`get_image`** computes `crop_mm` and `mm_per_px` from the calibration's scale: `[14.1, 13.8]` and 0.0512.
- **Errors** carry HTTP 400 (`invalid_argument`, `unknown_label`, `temperature_limit`), 409 (state
  conflicts, `halted`, `camera_moved`, operator outcomes), 500 (`move_failed`, `homing_failed`) and 503
  (`printer_unavailable`, `camera_unavailable`). The CLI decides by the `error` key alone. A bug in the
  service answers `internal_error`.
- **A person on the bed:** while a `load_well` or `clean_all_wells` prompt is open, `set_temperature`
  above that prompt's limit (60 / 40 °C) is refused with `bed_hot`. The spec checks the bed only when the
  prompt opens; this stops a concurrent call from heating the bed under a person's hands.
- **Clear** works only from the console page: the page carries a per-start token that the CLI never has.
  That keeps an agent with shell access from clearing a halt by accident; it is not a barrier against a
  local process that reads the page.
- **Position check** wells: P1-A1, P2-A2, P3-B2, P4-B1, C-A1, C-D4 (outer corners, so rotations show),
  visited by the shortest route from wherever the camera is (about 19 s).
- **Camera mount:** "the toolhead parts can't be matched" means a phase-correlation quality below 0.3.
  Good frames score 0.69-1.05; the frames from before the 2026-10-11 and 2026-10-12 re-mounts score below
  0.3 or measure shifts of 40-160 px. `camera_moved` stops motion until a person presses Clear (the heater
  is left alone). A recalibration (new `calibration_id`) also clears it.
- **Height before homing (M4):** the spacing of the openings gives the camera height. The pitch at Z50
  varies from 48 to 66 px across the frame (lens distortion and tilt), so `calibration/pitch_z50.json`
  (from `bambu/fit_pitch.py`) models it; estimates are within ±4 mm from Z50 to Z70. The service lifts 30 mm
  first if the estimate is below Z35 or unmeasurable. After `G28 X` it lifts 10 mm (F300), and it confirms
  the final Z50 by the image scale.
- **Z confirmation:** a `G1 Z50` that may not move anything (Z already 50) is confirmed by the image scale
  (Z within ±10 mm) rather than by a scene change. Z counts as known only on the MQTT connection on which
  it was confirmed, so after a reconnect the first motion is `G1 Z50` alone (M3).
- **XY after a restart:** `calibration/position.json` is only a hint. If the first move shows no motion
  (the camera may already be there), the service proves the printer moves with a detour via the nearest
  well, then comes back.
- **H2 details:** "reached the target" depends on the direction: at or above target − 0.5 °C when heating
  up, at or below target + 0.5 °C when cooling down. A target found at start-up gets its direction from
  the first reading, so a bed stepping down from 69 to 50 °C never counts as overshoot. "Heater stuck on"
  is judged only 30 s after the heater goes off (`stuck_grace_s`). Switched off mid-ramp, the reported
  reading still climbs 5–7 °C for about 10 s, because reports lag and the plate keeps taking heat; this
  comes from the digital twin's refit of the 2026-10-11 trial.
- **`--set` overrides** of safety parameters work only with `--sim`; on the rig, edit `safety.toml`.

## Not yet verified on the rig

The simulator runs the same code, but these need the real printer:
- Homing and moves: the safe sequence, the acknowledgement timing, and the motion and stillness thresholds
  on real frames. The real stream's still-frame difference is 0.02 against the 0.3 threshold. Whether
  `G28 X` pauses for 3 s or more mid-homing, which the completion rule would mistake for the end when
  the home bits were already set (re-homing), is untested.
- Safety spec C1 (nothing below the nozzle) must be checked by a person before the first homing.
- The heat guard against the real printer (test plan 4), Mac sleep (15), the link-loss firewall test (7).
