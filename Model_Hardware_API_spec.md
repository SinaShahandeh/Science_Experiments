# Model Hardware API Specification

## Overview

This document specifies the command-line interface an AI agent uses to run experiments on the device. The agent works with **wells**, not with a printer, a camera, a grid or an operator.

- **The agent only knows wells.** A well is a label such as `P3-A1`. The agent does not know where the well is, which island it belongs to, how the camera gets there, or who fills it. Every command that reads a well moves the camera there, centers it straight above the well, captures a fresh frame, and returns data about that one well.
- **The agent is always in the loop.** There is no batch or "run" command. To follow a reaction over time, the agent calls `get_image` / `get_raw_opacity` well after well, as often as it decides, and sets the temperature when it decides. Pacing, well order, and when to stop are the agent's choices.
- **The hardware owns everything physical:** homing, motion, speed, waiting for the camera to settle, camera settings, finding the well in the frame, temperature control, operator hand-offs, safety limits, and logging.

## Device

- **Printer:** Bambu Lab A1, used as a hot plate and camera gantry. The bed heats the samples. Build volume 256 × 256 × 256 mm; X moves the toolhead, Y moves the bed, Z moves the gantry.
- **Well grid:** an open-bottom PETG grid on the bed ([open_grid_2mm.png](open_grid_2mm.png)). There is a 4 × 4 center island (C) and four 2 × 2 corner islands (P1–P4), 32 wells in total. Openings are 10 mm on a 12 mm pitch, and the walls are 10 mm tall. The bed surface is the floor of each well.
- **Camera:** a Raspberry Pi camera (OV5647) mounted on the toolhead next to the nozzle, looking down, tilted about 10°. It is served as an MJPEG stream; see [RASPBERRY_PI_CAMERA.md](RASPBERRY_PI_CAMERA.md).
- **Light:** a battery-powered LED bar on the toolhead. It moves with the camera, so every well is lit from the same angle when the camera is centered on it. It does not flicker.
- **Operator:** a person who fills and cleans wells when the hardware asks. A liquid handler could take this role later without changing the API.
- **Temperature limit:** the bed must not exceed **100 °C**.

## API Commands

### Conventions

- Each command is a CLI call that prints one JSON object. The CLI is a thin client of a hardware service that keeps the printer and camera connections open, so a call does not pay connection setup time.
- Every response carries a time `t` (or `t_…`): Unix time in seconds, from the control computer's clock. Temperatures, images and operator events share this clock, so they can be aligned directly.
- Errors are returned as `{"error": "<code>", "message": "..."}`. The codes are `unknown_label`, `temperature_limit`, `not_homed`, `homing_failed`, `move_failed`, `camera_unavailable`, `printer_unavailable`.

### 1. `get_status() -> Status`

- Reports whether the hardware is ready. It does not move anything; it reads the latest MQTT report and one frame from the open stream.
- Call it at the start of a session, or whenever the agent wants to know whether the next well read could have to home first.

```json
{
  "t": 1791747600.120,
  "ready": true,
  "homed": true,
  "printer_ok": true,
  "camera_ok": true,
  "camera_tilt_mm": [0.0, 0.04],
  "position_warning": false,
  "idle_s": 312,
  "loaded_wells": ["P3-A1", "P3-A2"],
  "bed_c": 49.9, "target_c": 50.0, "bed_age_s": 0.8
}
```

- `ready` is `true` when `homed`, `printer_ok` and `camera_ok` are all true and `position_warning` is false. When it is false, call `prepare()`.
- `homed` means `home_flag & 0b111 == 0b111` in the latest printer report.
- `printer_ok`: the MQTT connection is up, the last report is under 5 s old, and the printer reports no error.
- `camera_ok`: the stream is delivering frames.
- `camera_tilt_mm` and `position_warning` are measured on that frame, as in `get_image`. If `position_warning` is true, the positions need recalibration (see Recalibration); the agent cannot fix this.
- `idle_s` is the time since the last move. The printer forgets its home after about 1 h (3600 s) idle.
- `loaded_wells` lists the wells loaded since the last `clean_all_wells`. While it is not empty, the hardware does not home on its own (see `prepare`).

### 2. `prepare() -> Status`

- Makes the hardware ready and returns the same object as `get_status`. Call it at the start of every experiment, before preheating and before `load_well`.
- If the printer is not homed, it runs the safe homing sequence (about 2 min), lifts to Z50, and leaves the camera parked at X128 Y254, behind the grid.
- If the printer is already homed, it only makes sure the camera is at Z50 and does not move XY. When nothing needs doing it returns in under a second.
- Homing moves the bed in Y at firmware speed, which cannot be slowed down, so liquid in the wells can slosh. `prepare()` homes even when wells are loaded: after a `not_homed` error, calling it is how the agent accepts that risk.
- Fails with `homing_failed`, `printer_unavailable` or `camera_unavailable` when it cannot make the hardware ready. If only `position_warning` is set, it returns normally with `ready: false`.

### 3. `set_temperature(temperature: float) -> {target_c, t}`

- Sets the bed target in °C and returns immediately; it does not wait for the bed to reach the target.
- `0` turns the heater off.
- Targets above 100 °C are rejected with `temperature_limit`.

### 4. `get_temperature() -> {bed_c, target_c, t, age_s}`

- Returns the latest bed thermistor reading and its target.
- `age_s` is the age of the reading. The printer reports about every 2 s, so it is up to about 2 s old.
- This is the **bed** temperature, not the sample's (see Known limitations).

### 5. `get_image(label: str) -> WellImage`

- Moves the camera straight above the well, waits until the image is still, captures a fresh frame, and returns one image: the **well's area of interest**, a centred 7 × 7 mm square inside the 10 mm opening, cropped from the frame.
- It is the same region `get_raw_opacity` measures.
- Neighbouring wells and walls are outside the crop.

```json
{
  "label": "P3-A1",
  "t": 1791747635.659,
  "image": "session/2026-10-12_1030/wells/00042_P3-A1.png",
  "size_px": [34, 34],
  "mm_per_px": 0.205,
  "bed_c": 74.9, "target_c": 75.0, "bed_age_s": 1.4,
  "camera_tilt_mm": [0.0, 0.04],
  "position_warning": false,
  "rehomed": false
}
```

- `image` is a PNG of the crop: 34 × 34 px, 7 mm square at the capture height. The 1.5 mm margin to the walls on each side absorbs small position errors and camera creep. The full frame is kept in the session log.
- `t` is when the frame arrived. `bed_c` is the latest temperature at that moment, and `bed_age_s` is how old that reading is.
- `camera_tilt_mm` is how far the camera has rotated on its mount since calibration, measured from the toolhead parts visible in the same frame (`bambu/cam_tilt.py`) and expressed as the shift of the straight-down point on the bed. The crop follows it. Above 1 mm, `position_warning` is `true`: the margin is nearly used up and the camera mount should be checked and the positions recalibrated.
- `rehomed` is `true` when the printer had lost its home and was re-homed first. That adds about 2 min. This happens only while no well is loaded. With wells loaded, the call fails with `not_homed` and nothing moves; the agent decides whether to call `prepare()`.

### 6. `get_raw_opacity(label: str) -> WellReading`

- Same motion and capture as `get_image`, with its own fresh frame.
- Returns one grayscale number for the well, computed from the area of interest. The algorithm is under "Opacity reading" below.

```json
{
  "label": "P3-A1",
  "t": 1791747636.611,
  "gray": 172.0,
  "pixels_total": 1156, "pixels_used": 1031,
  "saturated_px": 0, "reflection_px": 125,
  "all_saturated": false,
  "bed_c": 74.9, "target_c": 75.0, "bed_age_s": 0.4,
  "camera_tilt_mm": [0.0, 0.04],
  "position_warning": false,
  "rehomed": false
}
```

- `gray` ranges from 0 to 255 under the current camera settings. White paper reads about 232, and an empty well (bare black bed) about 19.
- When every pixel is saturated, `gray` is 255 and `all_saturated` is `true`.

### 7. `load_well(label: str) -> {label, t_requested, t_loaded, bed_c, target_c}`

- Fails with `not_homed` if the printer is not homed, so homing always happens before there is liquid in the wells. Call `prepare()` first.
- Has the well filled, then returns. Today the operator does it:
  - The camera does not move. At Z50 there is enough room under the toolhead to fill any well, wherever the camera is.
  - It signals the operator with three chimes and a spoken "Load well P3-A1".
  - It waits until the operator confirms with a key press or button at the control computer.
- `t_loaded` is the confirmation time, which is the best available start time for that well.
- The camera stays where it was, so the next `get_image` / `get_raw_opacity` call only moves if it asks for a different well.
- The well is added to `loaded_wells`.

### 8. `clean_all_wells() -> {t_requested, t_done}`

- Has every well emptied and cleaned (protocol: leftover cooked protein seeds the next run), then returns.
- Operator flow as in `load_well`: the camera does not move; signal, then wait for confirmation.
- The agent should switch the heater off and let the bed cool before calling it.
- Empties `loaded_wells`.

### 9. `list_wells() -> list[str]`

Returns all valid labels, from `C-A1` to `P4-B2`. The scheme is described in the label-position mapping section below.

### What a call costs

| Situation | Time per call |
|---|---|
| `set_temperature`, `get_temperature`, `list_wells` | < 0.1 s |
| `get_status` | < 0.2 s (one frame) |
| `prepare`, already homed | < 1 s, plus a Z move if the camera is not at Z50 |
| `prepare`, not homed | ~2 min |
| `get_image` / `get_raw_opacity`, camera already over the well | ~0.2 s (one fresh frame) |
| … neighbouring well (12 mm) | ~1 s (measured 0.8–1.5 s) |
| … across the grid | distance ÷ 30 mm/s + ~0.5 s (~6 s between opposite corner islands) |
| … printer lost its home, no well loaded | + ~2 min for safe homing |
| … printer lost its home, wells loaded | fails at once with `not_homed` |
| `load_well`, `clean_all_wells` | as long as the operator takes |

Cycling four neighbouring wells therefore gives each well a reading about every 4 s.

### Example: how an agent runs a clouding experiment

This is in sudo code:
```
s = prepare()                             # homes now, while the wells are still empty
if not s.ready: stop and report s         # e.g. position_warning: recalibrate first
set_temperature(50)
poll get_temperature() until bed_c >= 49.8
for label in ["P3-A1", "P3-A2", "P3-B2", "P3-B1"]:
    load_well(label)                      # t_loaded per well
set_temperature(75)                       # the agent records t of this call as the jump time
repeat until the agent decides the curves have plateaued:
    for label in ["P3-A1", "P3-A2", "P3-B2", "P3-B1"]:
        r = get_raw_opacity(label)        # gray, t, bed_c
        when a reading is surprising: get_image(label) and look at the well
set_temperature(0)
poll get_temperature() until the bed is cool
clean_all_wells()
```

The agent can change the well order, sampling rate or temperature at any point, based on what it sees.

## What the hardware does behind each call

The agent never sees any of this.

- **Service and logging.**
  - The hardware runs as a long-lived service.
  - It writes a session folder: every command and response, every MQTT temperature report (about every 2 s), every full frame, and every crop.
  - This keeps a complete temperature log alongside the images even when the agent asks for temperature rarely.
- **Homing.**
  - Before every move the service checks `home_flag & 0b111 == 0b111`. The printer loses its home after a power cycle **and after about 1 h idle**.
  - The agent sets up the home with `prepare()`. If the home is lost later:
    - With no well loaded, the service homes before the move and reports `rehomed: true`.
    - With any well loaded, it returns `not_homed` and does not move. XY homing runs at firmware speed and moves the bed, which would slosh the liquid. The ~2 min it takes could also hide the start of a reaction: in the trial, clouding began 115–140 s after the temperature jump.
  - `load_well` adds to `loaded_wells` and `clean_all_wells` empties it. The list is kept in the session folder, so it survives a service restart.
  - Homing always uses the safe sequence (`bambu/home_safe.py`). It never sends a plain `G28`, which would probe Z on the center island.
  - After homing, the service lifts straight to Z50. `home_safe.py` still lifts to Z100; change that when the service is built, because Z50 ↔ Z100 cycles shifted the camera mount.
- **Motion.**
  - The camera position for each label comes from the table below. All captures are at Z50.
  - Moves are absolute, at 30 mm/s (F1800) with `M204 S500`. This speed was used for all well-to-well and park moves in the 2026-10-11 trial, with liquid in the wells. Z moves run at F300.
  - When Z goes up, Z moves first; when Z goes down, Z moves last.
- **Move completion.** The printer never reports its position, so the service watches the camera instead:
  - For a move, the service waits the move's kinematic duration (XY and Z travel at the set speeds and acceleration) plus 0.8 s. The move counts as finished after 4 consecutive frames whose frame-to-frame difference is below the stillness threshold (`Gantry.move` in `bambu/motion.py`, frames from `bambu/camstream.py`). Z moves use the same check.
  - Homing has no known duration. The service waits until the image has moved and then stayed still, and the MQTT `home_flag` bits are set (`Gantry.run`).
  - The 2026-10-11 trial (`bambu/kinetics_run.py`) used an earlier detector: it waited for motion and then 3 still frames, and used a timed wait for Z-only moves.
  - The previous method (part-fan toggle plus MQTT) took about 12 s per well and is no longer used.
- **Operator hand-offs** (`load_well`, `clean_all_wells`).
  - The camera does not move. At Z50 the toolhead leaves enough room to fill or clean any well under it. Not moving away saves the trip back: the 2026-10-11 trial parked at X60 and spent about 4 s returning for each reading.
  - Chime, speak, and wait for confirmation.
  - In the trial, confirmation went through a chat message, which added several seconds. A key press or button at the control computer is required.
- **Camera.**
  - Exposure, gain, white balance and tone are locked (settings v2, 2026-10-11): 10 ms exposure, gain 1.56, white paper ≈ 232, darkest bed channel ≈ 8. On 2026-10-12, after the camera and LED bar were re-mounted, brightness was raised by 0.016 so the bare bed again reads ≈ (R 33, G 8, B 39); the PETG wall tops read 166–199 and nothing in or around the wells saturates. The white point has not been re-measured with paper since the re-mount.
  - The exposure is a whole number of 10 ms periods, so 100 Hz flicker from mains lighting cannot beat against it.
  - **The room light must be off.** With it on, every well oscillated by ±2% with a period of 2–3 s; with it off, frame-to-frame noise is about 0.2%.
  - The stream reaches the control computer at about 18 fps.
- **Finding the well in the frame.**
  - With the camera centered, the well is at the nadir pixel, the point straight below the lens: (690.5, 362.0) at calibration, plus the camera tilt measured in the same frame.
  - The area of interest is a fixed 7 × 7 mm (34 × 34 px) square centred there, with no searching or snapping. It leaves 1.5 mm to the walls on each side: the absolute aim is good to about ±0.5 mm (0.15 mm repeatability plus ±0.4 mm uncertainty of the nadir pixel), and the rest is margin for creep of the camera mount.
- **Opacity reading.** Measured on the 7 × 7 mm square (`bambu/well_gray.py`):
  1. Convert to gray: Y = 0.299 R + 0.587 G + 0.114 B.
  2. Drop pixels saturated in any channel (≥ 250), plus a 2 px border around them, because the edges of a reflection are bright but not saturated. If every pixel is saturated, return 255.
  3. Drop reflections: pixels above median + 3 × 1.4826 × MAD (with the MAD floored at 1). This step only removes bright pixels, because reflections are only ever brighter.
  4. `gray` = the median of the remaining pixels.

  The centred square already excludes the rim, the wall shadow and unfilled edges, so the earlier step that dropped the dimmest 25 % (needed for a whole-well mask) is not used.

  The alternatives tested on the trial data had more noise:
  - a centred 24 px box: 3.7 gray levels of noise on the plateau
  - a centred 34 px box: 3.1
  - the histogram's main mode cut at its valleys: 6.2
  - the largest component of a Gaussian mixture: 11.6

  A whole-well mask with the dimmest 25 % dropped also gave 3.1, but needs the mask to sit exactly on the opening; the 34 px centred box gives the same noise with a margin for position errors. The well's pixels do not split into clean modes (brightness varies smoothly across the well), so the mode-based methods jumped between groups. Code: `bambu/well_methods.py`.
- **Temperature.**
  - `set_temperature` sends `M140 S<t>`.
  - Readings come from the MQTT report (`bed_temper`, `bed_target_temper`).
  - The 100 °C limit is enforced here.
  - On shutdown or a fatal error the service sets the heater to 0.

## Known limitations (from the 2026-10-11 trial)

- **Bed ≠ sample temperature.**
  - After a 50 → 75 °C jump, the bed reached 75 °C in about 42 s, overshot to 76.7 °C at about 60 s, then held 75.2–75.7 °C.
  - Egg white in P3 started clouding 115–140 s after the jump, which is 75–100 s after the bed reached temperature.
  - The well-temperature calibration in protocol Phase 0 is still needed before kinetics can be extracted.
- **Reflections are fixed per well.** The liquid surface reflects the LED bar. In P3-B2 a reflection streak crossed the measured area in every frame. The opacity algorithm removes it; the image returned by `get_image` still shows it.
- **Repeatability with settings v2 is not yet re-measured for moves.** The camera parked and still gives about 0.2% noise. The trial's ±4 gray levels were mostly room-light flicker.
- **Clear samples sit near black.** Under settings v1, clear egg white read 0, so the onset of clouding was hidden until fewer than half of the pixels were clipped. Settings v2 keeps the empty bed at gray ≈ 18.
- **The camera mount can creep or shift.** On 2026-10-12 it crept 5.5 px (≈ 1.1 mm) in 30 min after a re-mount, and later shifted by tens of pixels during repeated Z50 ↔ Z100 moves. It was re-glued and is now tracked in every frame (`camera_tilt_mm`). Keep Z moves to a minimum.

# label-position mapping

We have 5 islands, central, and P1 to P4. For each of the points, we need to assign a string to be able to identify it.

Calibrated 2026-10-12 (after the camera and LED bar were re-mounted and glued) by centering each well under the Raspberry Pi camera at Z50. Raw data and images are in [calibration/](calibration/); earlier calibrations are in `calibration/archive_*`.

> **Status 2026-10-12 14:30: current.** All 32 wells blind-checked within 0.15 mm (see Positions). The camera mount crept and then shifted on 2026-10-12 before it was re-glued; its tilt is now tracked in every frame (see *What a position means*). Recalibrate if `cam_tilt.py` reports more than about 2 px.

## Label scheme

Labels are `<island>-<row><column>`. The view below is from above, with the printer's front at the bottom of the diagram.

- Rows run A, B, C, D from back to front (high Y to low Y).
- Columns run 1, 2, 3, 4 from left to right (low X to high X).
- Corner islands are numbered clockwise from back-left. Each has wells `A1 A2 / B1 B2`.

```
            BACK (high Y)
  P1                      P2
  A1 A2                   A1 A2
  B1 B2                   B1 B2
            C
            A1 A2 A3 A4
            B1 B2 B3 B4
            C1 C2 C3 C4
            D1 D2 D3 D4
  P4                      P3
  A1 A2                   A1 A2
  B1 B2                   B1 B2
            FRONT (low Y)
```

That gives 32 labels: `C-A1` to `C-D4` (16 wells) and `P1-A1` to `P4-B2` (4 × 4 wells).

Visual reference: [well_map.png](well_map.png) shows every label at its position, with the X/Y from the tables below (redraw it with `python3 calibration/plot_well_map.py`).

![Well labels and camera positions](well_map.png)

## What a position means

Each X/Y/Z below is the **printer (nozzle) coordinate** to command so that the **camera looks straight down into the center of that well**. These are not the coordinates of the wells themselves.

- **Straight down, not image center.** The Pi camera is tilted on its mount, so the point directly below the lens is pixel **(690.5, 362.0) ± 2 px** in the 1280 × 720 frame (`calibration/nadir.json`), not the image center (640, 360). Every well was centered on that pixel. It was found as the fixed point of the image warp when Z steps from 50 to 58–70 mm (homography eigenvector and a robust per-well scale fit agree within ±2 px ≈ ±0.4 mm). It moves whenever the camera mount is touched: earlier values were (743, 350) and (695, 357).
- **Camera tilt is tracked.** The toolhead body (fan-duct lobes, "CAUTION HOT" label) is visible at the top of every frame and does not move relative to the camera unless the mount does. `bambu/cam_tilt.py` compares that strip with `calibration/camera_ref.png` (saved at calibration); the shift in pixels is how far the nadir has moved in the image (≈ 4.9 px per mm at the wells). It reads within ±0.03 px across 100 mm moves. During this calibration it stayed below 0.25 px.
- **Z is fixed at 50 mm** for all captures (nozzle tip 50 mm above the bed). The lens is about 124 mm above the bed, estimated from how the image scale changes with Z. At the straight-down point there are 4.84 px/mm in X and 4.93 px/mm in Y (`calibration/jacobian_z50.json`), so one 12 mm well pitch is about 59 px. Keep Z moves to a minimum: the mount shifted on 2026-10-12 during a run with many Z50 ↔ Z100 cycles.
- **Camera offset from the nozzle.** The camera's straight-down point is now about **33 mm in front of the nozzle (−Y) and about 3 mm to its right (+X)**. This is only an estimate from the STL layout (the islands have moved by up to ~3 mm). Use the table, not this offset, to aim the camera.
- **The grid has moved.** Compared with the STL layout, the islands have shifted relative to the center island by: P1 (+2.9, +2.2), P2 (−1.3, +3.3), P3 (+2.4, −1.8), P4 (−0.3, +0.7) mm. Each island's rotation was under 0.3° on 2026-10-11; on 2026-10-12 the fit gives 1.4° for P2 and ≤ 0.1° for the others. The center island sits about 0.6 mm right of and 2.5 mm in front of the planned X/Y 103–153 mm area, assuming the camera offset above.

## Positions

Within each island the wells are an exact 12 mm grid. The values below come from fitting that grid to the measured centering of every well (each centered to ≤ 0.6 px ≈ 0.12 mm). Fit residuals are at most 0.13 mm.

**Accuracy:** a blind check sent the camera to all 32 labels in random order, using only this table (XY moves at Z50). Every well ended up within **0.15 mm** of the straight-down pixel, which is the detector's half-pixel resolution (`calibration/verify_*.jpg`; contact sheet with the measurement boxes: `calibration/well_alignment_sheet.jpg`). Add the ±0.4 mm uncertainty of the nadir pixel itself for the absolute vertical error.

**Center island (C)**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `C-A1` | 107.14 | 178.97 | 50 |
| `C-A2` | 119.14 | 178.95 | 50 |
| `C-A3` | 131.14 | 178.93 | 50 |
| `C-A4` | 143.14 | 178.91 | 50 |
| `C-B1` | 107.12 | 166.97 | 50 |
| `C-B2` | 119.12 | 166.95 | 50 |
| `C-B3` | 131.12 | 166.93 | 50 |
| `C-B4` | 143.12 | 166.91 | 50 |
| `C-C1` | 107.10 | 154.97 | 50 |
| `C-C2` | 119.10 | 154.95 | 50 |
| `C-C3` | 131.10 | 154.93 | 50 |
| `C-C4` | 143.10 | 154.91 | 50 |
| `C-D1` | 107.08 | 142.97 | 50 |
| `C-D2` | 119.08 | 142.95 | 50 |
| `C-D3` | 131.08 | 142.93 | 50 |
| `C-D4` | 143.08 | 142.91 | 50 |

**P1 — back-left**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P1-A1` | 69.83 | 220.98 | 50 |
| `P1-A2` | 81.83 | 220.98 | 50 |
| `P1-B1` | 69.83 | 208.98 | 50 |
| `P1-B2` | 81.83 | 208.98 | 50 |

**P2 — back-right**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P2-A1` | 169.42 | 222.29 | 50 |
| `P2-A2` | 181.42 | 222.58 | 50 |
| `P2-B1` | 169.72 | 210.29 | 50 |
| `P2-B2` | 181.72 | 210.59 | 50 |

**P3 — front-right**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P3-A1` | 173.78 | 113.32 | 50 |
| `P3-A2` | 185.78 | 113.32 | 50 |
| `P3-B1` | 173.78 | 101.32 | 50 |
| `P3-B2` | 185.78 | 101.32 | 50 |

**P4 — front-left**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P4-A1` | 67.03 | 115.34 | 50 |
| `P4-A2` | 79.03 | 115.34 | 50 |
| `P4-B1` | 67.03 | 103.34 | 50 |
| `P4-B2` | 79.03 | 103.34 | 50 |

## How the hardware moves the camera to a label

These steps are internal to the hardware service; the agent only names the label.

1. **Check the home before every move**, not just once per power cycle. The printer forgets its home after a power cycle and after about 1 h idle. If `home_flag & 0b111 != 0b111` and no well is loaded, run Bambu's safe sequence (`bambu/home_safe.py`): `G28 X`, raise Z 5 mm, slowly park at X128 Y254 (bare steel behind the grid), then `G28 Z P0 T140`, then lift to Z50. If any well is loaded, stop with `not_homed` instead (see Homing above). Never run a plain `G28`: it probes Z near the bed center, where the center island is. XY homing runs at firmware speed and can't be slowed down.
2. **Move gently.** Use `M204 S500` (acceleration), XY at `F1800` (30 mm/s; tested with liquid in the wells) and Z at `F300` (5 mm/s). If Z goes up, lift first and then move XY. If Z goes down, move XY first and then lower. All moves are absolute (`G90`). (`bambu/motion.py` uses the same values.)
3. **Wait until the image is still before capturing.** The printer doesn't report its position, and the `gcode_line` acknowledgement only means the command was received. Read the MJPEG stream continuously. Wait out the move's kinematic duration plus 0.8 s, then wait for 4 consecutive still frames (~1 s for a neighbouring well); Z moves are handled the same way. Save the last commanded position to `calibration/position.json`.
4. **Capture** the first frame that arrives after the image went still, from the open stream `http://raspberrypi.local:8080/stream.mjpg`.

Reference implementation of steps 2–3: `Gantry.move` in `bambu/motion.py`. Step 4: `Run.capture` in `bambu/kinetics_run.py`.

## Recalibration

Recalibrate if the grid islands, the camera, or its mount move. Signs: `position_warning` in responses, or wells visibly off center in full frames.

**Before starting:** all wells empty and clean (the method finds wells as dark holes), nothing in C-A1, and the room light off. The steps are scripted in `bambu/` and take about 20 minutes:

0. `python3 cam_tilt.py ref`: save the toolhead reference strip for tilt tracking; check that `python3 cam_tilt.py` stays near 0 for a few minutes (the mount is still) before going on.
1. `python3 jacobian.py 3`: measures how far the image moves (in pixels) per mm of X and Y, at Z50.
2. `python3 nadir.py`: steps Z from 50 to 70 mm once. In the wide view the per-well fit it prints scatters by several pixels; refine the straight-down pixel from its images with the homography fixed point (method in `calibration/nadir.json` → `nadir_note`) and store it as `nadir_px`.
3. `SEED=<previous well_positions.json> python3 calibrate_wells.py [labels…]`: centers each well under that pixel (to 0.8 px, correcting each frame for camera tilt) and records its position in `calibration/well_positions.json`. If the camera was re-mounted, first move to the old C-A1 entry, measure the offset to the nearest well, check visually that it is C-A1 (island corner at top-left), and shift the seed by it, so no well is mistaken for its neighbour.
4. `python3 fit_wells.py`: fits each island's 12 mm grid and prints the tables above.
5. `python3 verify_wells.py <labels…>`: blind check.
6. Update the tables above and redraw the map (`python3 calibration/plot_well_map.py`).

**Camera tone** (separately, after the positions): put a small piece of white paper in C-A1, park the camera centered over C-A1, and run `python3 calibrate_exposure.py`. It locks white paper ≈ 232 and the bed's darkest channel ≈ 8, using only exposures that are a whole number of 10 ms periods.
