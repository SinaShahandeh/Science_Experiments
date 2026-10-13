# Hardware Safety Specification

## Purpose

This document specifies the safety layer of the hardware service (`mhapi-service`) described in [Model_Hardware_API_spec.md](Model_Hardware_API_spec.md). It is written so that it can be implemented and tested on its own.

- **Safety sits below the agent.** The agent and its harness are treated as untrusted planners. Every rule here is enforced by the service, or by a separate watchdog process, whatever the agent asks for or fails to ask for. This follows the Model Hardware Standard's approach: limits are enforced at the device, not in the prompt.
- **In scope:** heat, motion, people's hands near the machine, the camera mount and the well grid staying in place, and losing the processes or links that control the printer.
- **Out of scope:** the printer firmware's own protections (thermal runaway, motor stall detection), electrical safety, and handling of the samples (always egg white).

## The rig

A Bambu Lab A1 is used as a hot plate and camera gantry. The bed heats, X moves the toolhead, Y moves the bed, and Z moves the gantry. An open-bottom PETG well grid (five islands, 10 mm tall walls) sits on the bed, so the bed is the floor of every well. A Raspberry Pi camera and a battery-powered LED bar are glued to the toolhead. All captures are at Z50: the nozzle tip is 50 mm above the bed and the lens about 124 mm. A person loads egg white and cleans the wells when the service asks, working under the stationary toolhead. The control computer, a Mac, talks to the printer over MQTT on the LAN and reads the camera's MJPEG stream.

## Facts this spec relies on

Measured or observed on this rig between 2026-10-10 and 2026-10-12:

| Fact | Evidence |
|---|---|
| The bed holds its target with no further commands. | It held 50 °C for over 1.5 h on 2026-10-12, including after the printer had lost its home. No firmware heater timeout has been seen. |
| The printer loses its home 44–62 min after its last move. | Three cases on 2026-10-11. Probably the motors switching off when idle. |
| The printer never reports its position. A G-code acknowledgement only means "received". | The report has no position field; `M114` returns nothing (2026-10-10). |
| G-code is published without delivery confirmation, and the acknowledgement is not checked. | `Printer.gcode` in `bambu/bambu_link.py` (MQTT QoS 0). |
| A plain `G28` probes Z near the bed center, which is on the center island. Z homing presses the nozzle onto the bed. | Bambu wiki. `bambu/home_safe.py` homes Z at X128 Y254 instead. |
| After a 25 °C jump the bed overshoots by about 2 °C for about a minute. | 50 → 75 °C: reached 75 °C in 42 s, peaked at 76.75 °C after about 60 s (2026-10-11). |
| The bed has never been run above 80 °C on this rig. | The highest seen was 80 °C, while printing the grid. |
| Status reports arrive a median 2.5 s apart (at most 5.2 s) when one is requested every 5 s, and can trail the printer's state by about 5 s. | `temps.csv` from the 2026-10-11 trial; a fan-state test on 2026-10-12. |
| The camera mount has moved: 5.5 px of creep after a re-mount, then a 50 px swing within a minute while cycling Z50 ↔ Z100. | 2026-10-12. Re-glued since; tracked with `bambu/cam_tilt.py`. |
| The printer's IP address changes after power cycles. | .50, then .52, then .54. `bambu_link` rediscovers it by serial number. |
| The printer runs firmware 01.08.01.00 in LAN-only Developer Mode. Its `gcode_state` has read `FAILED` since the grid print, which is not an error. | Status reports. |

## Safety states

The service is always in one of three safety states. `get_status()` reports it as `safety`, with `safety_reason` and `t_safety`.

| State | Entered when | Effect | Left when |
|---|---|---|---|
| `ok` | normal operation | none | — |
| `heater_timeout` | the heater watchdog fired (H4) | heater off; every command still works | the agent sets a temperature again |
| `halted` | operator stop, over-temperature, lost printer contact while heating, foreign control, or an unrecoverable fault | heater off. No motion, heating or operator prompts. Queued and running operations return `halted`. Only `get_status`, `get_temperature`, `list_wells` and `describe` answer. | a person presses **Clear** on the operator console after checking the rig |

Clearing a halt does not restore the heater target. The position counts as unknown, so the next motion call has to re-home through `prepare()`.

Every change of state is logged to the session's `events.csv` and announced on the control computer (a chime and a spoken sentence) and on the operator console.

## Requirements

### Heat

- **H1. Target limit.** Reject targets above 100 °C with `temperature_limit`, and negative or non-numeric targets with `invalid_argument`. `0` turns the heater off.
- **H2. Over-temperature cutoff.** Switch the heater off and enter `halted` if any of these happens:
  - a reading exceeds 105 °C;
  - once the bed has reached its target, the reading stays more than 5 °C above the target for over 60 s (normal overshoot is about 2 °C for about a minute; a bed cooling down to a lower target does not count until it has reached it);
  - with the heater off, the reading rises by more than 3 °C within 60 s, which means the heater is stuck on.
- **H3. "Heater off" must be confirmed.** After sending `M140 S0`, wait for a report with `bed_target_temper` = 0. Resend every 5 s until it arrives, and alert a person if it hasn't arrived within 60 s.
- **H4. Heater watchdog.** If the target is above 0 and no API call of any kind has arrived for 15 min, switch the heater off and enter `heater_timeout`. The bed holds its target indefinitely, so an agent or harness that dies mid-run would otherwise leave it hot. Any call, even `get_status`, resets the clock, so a harness can keep a long hold going with cheap calls. Reads for people watching are not API calls and don't reset it: the console's `/operator/state`, and `/operator/status`, which gives a dashboard `get_status`'s body. Otherwise an open dashboard would keep a stalled agent's bed hot.
- **H5. A watchdog that doesn't depend on the service.** H4 stops working if the service itself dies. Run a separate small process, `mhapi-heatguard`, started by launchd with KeepAlive. The service rewrites a heartbeat file every 5 s with `{t, pid, target_c}`. If the heartbeat is older than 120 s and the last target was above 0, the guard connects to the printer itself, switches the heater off as in H3, and alerts. It may reuse `bambu/bambu_link.py`, but must not import the service.
- **H6. Lost contact while heating.** If no status report has arrived for 120 s while the target is above 0, enter `halted`, keep resending `M140 S0` until it is confirmed, and alert a person: the bed may still be hot and the service can't see it.
- **H7. Shutdown and start-up.** On a normal shutdown, switch the heater off (H3) before exiting. On start-up, if the bed target is already above 0 (for example after a crash), leave it and start the H4 clock at start-up, so the agent has 15 min to resume.
- **H8. Keep the Mac awake while heating.** Hold a macOS power assertion, as `caffeinate -i` does, whenever the heater target is above 0 or wells are loaded. A sleeping Mac stops both the service and H5 while the bed stays hot. Closing a laptop's lid can still put it to sleep, so keep the lid open.

### Motion

- **M1. G-code allowlist.** All G-code goes through one function that checks it against a fixed list and rejects anything else: `G90`, `G91`, `G1` (within M2's limits), `G28 X`, `G28 Z P0 T140`, `M204 S500`, and `M140 S<0–100>`. A plain `G28` is never sent. No nozzle or fan commands (`M104`, `M109`, `M106`): the nozzle stays cold. The agent never supplies G-code or coordinates.
- **M2. Workspace and speed.**
  - XY targets are only the positions in `calibration/well_positions_fit.json` and the homing park point X128 Y254, and they must lie within 0–256 mm.
  - Z is 50 for every capture. Other Z moves happen only inside the homing sequence.
  - XY moves are at most F1800 (30 mm/s) and Z moves at most F300 (5 mm/s), including the lifts inside homing. `M204 S500` is sent before every move.
  - The printer's speed mode must be Standard (`spd_lvl` 2, `spd_mag` 100), because the other modes scale every feed rate. Otherwise refuse to move and report `printer_ok: false`.
- **M3. Z before XY, from a known Z.** When Z goes up, lift before moving XY; when it goes down, move XY first. Z counts as known only after the service itself has confirmed a Z move or a homing. In three cases the first motion is `G1 Z50` on its own, confirmed before any XY move: after the service connects or reconnects, after any homing it didn't run, and after `move_failed`. That is safe at any XY, because the walls are 10 mm tall and nothing on the toolhead hangs below the nozzle (C1).
- **M4. Homing from an unknown Z.**
  - The safe sequence is:
    1. `G28 X`, which homes X and Y.
    2. A relative Z lift.
    3. A move to X128 Y254, bare steel behind the grid.
    4. `G28 Z P0 T140`.
    5. A lift to Z50, confirmed before any XY move.
  - `G28 X` sweeps the bed in Y and the toolhead in X, so before starting, the nozzle must be well above the walls.
  - Estimate the camera height from the well pitch in the image: 59 px at Z50, about 90 px at Z5. If it is below Z30, or can't be measured (for example after an interrupted Z homing), lift Z by 30 mm first. Relative upward moves work before homing.
  - Today `home_safe.py` lifts only 5 mm, at F1200, and ends with a timed lift to Z100. All three need changing.
- **M5. Every move confirmed.** A move counts as done only when the printer has acknowledged the command and the camera has seen the scene change and settle. The method is in the API spec under "Move completion". Otherwise the call fails with `move_failed`, the position counts as unknown, and no further XY move happens until `prepare()` re-homes.
- **M6. One controller.**
  - During a session the service must be the only thing commanding the printer: no printer screen, Bambu Studio or Bambu Handy. The calibration scripts in `bambu/` talk to the printer directly, so never run them while the service is running.
  - Enter `halted` with reason `foreign_control` if a report shows any of these:
    - a print job starting (`gcode_state` PREPARE, RUNNING or PAUSE);
    - home bits changing when the service didn't home;
    - a bed target the service didn't set. Every report carries the target, and the printer applies `M140` only
      after the moves queued before it, so until the printer confirms a new target (at most 60 s), reports of the
      target it replaced are stale, not foreign (2026-10-13: 0 °C reported 0.46 s after `M140 S50` halted a run).

### People

- **P1. Stop is always reachable.** A person can stop the rig without going through the agent:
  - the **Stop** button (or Esc) on the operator console page;
  - `mhapi stop` from any terminal;
  - quitting the service (Ctrl-C), which switches the heater off (H7).

  Stop switches the heater off and enters `halted`. A move already under way finishes (no move lasts more than about 7 s), and nothing new starts. `M112` is not used: its effect on the A1 over MQTT is untested and it may need a power cycle. A halt can only be cleared on the console, so an agent with shell access can stop the rig but cannot restart it.
- **P2. Nothing moves while a person works.** While a `load_well` or `clean_all_wells` prompt is open, nothing moves and queued calls wait. The toolhead stays where it is during loading, so the person works under a stationary toolhead.
- **P3. Hot bed.** `load_well` refuses above 60 °C and `clean_all_wells` above 40 °C, returning `bed_hot` with the current reading. The trial loaded at 50 °C.
- **P4. Clear prompts.** Each prompt says what to do, where, and what not to do, and shows the bed temperature. For example: "Load egg white into well P3-A1. Bed 50 °C." and "Clean all wells in place. Do not lift the plate or the grid. Bed 35 °C." The person can always answer **Problem** instead of **Done**.
- **P5. Alerts.** Safety events chime, speak a sentence on the control computer, and appear on the console.

### Camera mount and grid

- **C1. The nozzle is the lowest point.** Z homing presses the nozzle onto the bed, so anything hanging lower would hit the bed first. After any change to the toolhead (camera, LED bar, mount, glue), a person checks this before the next homing, and the check is recorded with the calibration. It was last confirmed on 2026-10-11, before the camera and LED bar were re-mounted twice on 2026-10-12.
- **C2. A loose camera stops motion.** When the API returns `camera_moved` (the camera shifted more than 1 mm on its mount, jumped between captures, or can't be measured), stop moving until a person has checked the mount. A mount that has let go may be about to fall.
- **C3. The grid stays put.** The islands sit on the removable plate, so cleaning happens in place (P4). The position checks in `prepare()` and after `clean_all_wells()` (see the API spec) catch islands that moved.
- **C4. Heat and the PETG grid.** The limit is 100 °C, but the grid is PETG and the bed has never been above 80 °C on this rig. Compare the position checks before and after the first runs above 80 °C: islands that softened or moved show up there.

### Links, power and firmware

- **N1.** Rediscover the printer by serial number whenever the MQTT connection can't be re-established, not only at start-up.
- **N2.** While the MQTT link is down, nothing moves. H6 covers the heater.
- **N3.** After a power cut the printer comes back unhomed, and the normal `not_homed` rules apply.
- **N4.** Stay on firmware 01.08.01.00 in LAN-only Developer Mode, with the printer's internet access blocked so it can't update itself. At start-up, check the version the printer reports and refuse to run on any other.

## Parameters

Keep all of these in one file, for example `mhapi/safety.toml`, that the agent and harness can't write. Proposed defaults:

| Parameter | Default | Why |
|---|---|---|
| Maximum target | 100 °C | Decided 2026-10-12 |
| Hard over-temperature | 105 °C | Above the maximum target plus normal overshoot |
| Overshoot margin and time | 5 °C for 60 s | Normal overshoot is about 2 °C for about 1 min |
| Heater stuck on | rise of 3 °C within 60 s with the heater off | |
| Lost reports while heating | 120 s | Reports are normally at most 5.2 s apart |
| Heater watchdog | 15 min without any API call | |
| Independent heat guard | heartbeat older than 120 s | |
| Maximum bed for loading / cleaning | 60 °C / 40 °C | The trial loaded at 50 °C |
| Operator answer timeout | 15 min | |
| XY feed, Z feed, acceleration | F1800, F300, 500 mm/s² | Approved 2026-10-12 |
| Minimum Z before XY homing | 30 mm | The walls are 10 mm tall |
| Camera mount | warn at 0.4 mm, stop at 1 mm or a 0.4 mm jump between captures | Same as the API spec |

## What the agent sees

- Error codes: `temperature_limit`, `invalid_argument`, `bed_hot`, `move_failed`, `camera_moved` and `halted`, plus `not_homed` from the API spec.
- `get_status()` fields: `safety`, `safety_reason` and `t_safety`.
- Nothing else. Stop, Clear and the parameters belong to people.

## What exists today

- `bambu/home_safe.py`: the safe homing sequence. Its 5 mm lift at F1200 and its timed lift to Z100 at the end need changing (M2, M4).
- `bambu/motion.py`: the speed and acceleration constants, and a `home_flag` check. It doesn't confirm moves (M5).
- `bambu/cam_tilt.py`: measures how far the camera has shifted. It discards the match quality that C2 needs.
- Everything else in this document is new: the limit checks, the cutoffs, both watchdogs, the console's Stop and Clear, the G-code allowlist, the acknowledgement checks, the unknown-Z rules, foreign-control detection, and the speed-mode, firmware and sleep checks.

## Test plan

Run each case against a simulated printer first where marked (sim), then on the rig with empty wells.

| # | Fault | How to cause it | Expected result |
|---|---|---|---|
| 1 | Target too high | `mhapi set_temperature 120` | `temperature_limit`; target unchanged |
| 2 | Bad target | `mhapi set_temperature -5`, `mhapi set_temperature abc` | `invalid_argument` |
| 3 | Agent goes quiet | heat to 50 °C, then make no calls | heater off after 15 min; `heater_timeout`; announced |
| 4 | Service dies | heat to 50 °C, then `kill -9` the service | the heat guard switches the heater off within about 2 min |
| 5 | Over-temperature (sim) | inject a 106 °C reading | `halted`; heater off confirmed |
| 6 | Heater stuck on (sim) | target 0 with rising readings | `halted` |
| 7 | Printer link lost | block the printer's traffic with a firewall rule while heating | after 120 s, `halted` and announced; heater-off retries until the link returns |
| 8 | Lost command (sim) | swallow one move command | `move_failed`; further moves refused until `prepare()` |
| 9 | Wrong speed mode | set Sport mode on the printer screen | moves refused; `printer_ok: false` |
| 10 | Foreign control | jog from Bambu Studio during a session | `halted` with `foreign_control` |
| 11 | Operator stop | press Stop during a move, and during a load prompt | the move finishes, then `halted`; the prompt is cancelled; queued calls return `halted`; `mhapi` can't clear it |
| 12 | Hands on a hot bed | `clean_all_wells` at 60 °C; `load_well` at 70 °C | `bed_hot` |
| 13 | Low nozzle before homing (sim) | camera image as at Z10, printer unhomed | lifts before `G28 X`; no XY move below Z30 |
| 14 | Loose camera (sim) | shift the frames by 6 px | `camera_moved`; motion stops |
| 15 | Mac sleep | leave the Mac idle past its sleep timer while heating | it stays awake |

## Open questions

- Are 60 °C for loading and 40 °C for cleaning the right limits?
- Is 15 min right for the heater watchdog, or should the harness renew an explicit lease instead?
- Do you want a physical stop button, such as a USB button mapped to Stop?
- Should alerts also reach a phone?
