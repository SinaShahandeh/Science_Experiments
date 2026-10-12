# Model Hardware API Specification

## Overview
This document explains the structure of a command-line tool that an AI agent can call with various commands and receive an output from the device. The idea here is that the AI agent does not need to know how the image or data was obtained. This API spec basically creates that transition: an interface between the AI agent and the device. 

## Device Details

Our device is a Bamboo AI Bamboo A1 3D printer with a group of wells printed on a base plate. There's a central island of 4x4 and a peripheral island of 2x2. We have modified this 3D printer, so we will use it as a hot plate. Instead of the nozzle, we have mounted a camera on top. 

An image is shown here: [open_grid_2mm.png](open_grid_2mm.png)

## API Commands

1- set_temperature(temperature: float) -> None

2- get_temperature() -> float

3- get_images(label: str) -> list[Image]

4- get_raw_opacity(label: str) -> float

5- load_well(label: str) -> None

6- clean_all_wells() -> None

# label-position mapping

We have 5 islands, central, and P1 to P4. For each of the points, we need to assign a string to be able to identify it. 

Calibrated 2026-10-11 by moving the printer slowly and centering each well under the Raspberry Pi camera. Raw data and images are in [calibration/](calibration/).

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

- **Straight down, not image center.** The Pi camera is tilted about 10° on its mount. The point directly below the lens shows up at pixel **(743, 350)** in the 1280 × 720 frame, not at the image center (640, 360). Every well was centered on that pixel. Centering on the image center instead would leave the camera about 21 mm off vertical.
- **Z is fixed at 50 mm** for all captures (nozzle tip 50 mm above the bed). The lens itself is higher, about 135 mm above the bed, estimated from how the image scale changes with Z. One 12 mm well pitch is about 58 px at the straight-down point.
- **Camera offset from the nozzle.** The camera's straight-down point is about **37.5 mm in front of the nozzle (−Y) and about 0.6 mm to its left (−X)**. This is only an estimate: it comes from the STL layout, and the islands have moved by up to ~3 mm. Use the table, not this offset, to aim the camera. To estimate where a well is on the bed, add (−0.6, −37.5) mm to its table entry.
- **The grid has moved.** Compared with the STL layout, the islands have shifted relative to the center island by: P1 (+2.9, +2.2), P2 (−1.3, +3.3), P3 (+2.4, −1.8), P4 (−0.3, +0.7) mm. Each island's rotation is under 0.3°. The center island sits about 0.6 mm right of and 2.5 mm in front of the planned X/Y 103–153 mm area, assuming the camera offset above.

## Positions

Within each island the wells are an exact 12 mm grid. The values below come from fitting that grid to the measured centering of every well. Fit residuals are at most 0.31 mm.

**Accuracy:** a blind check sent the camera to 6 labels across all five islands in random order, using only this table. The camera ended up straight above the well center to within **0.25 mm** every time.

**Center island (C)**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `C-A1` | 111.23 | 180.98 | 50 |
| `C-A2` | 123.23 | 180.95 | 50 |
| `C-A3` | 135.23 | 180.92 | 50 |
| `C-A4` | 147.23 | 180.89 | 50 |
| `C-B1` | 111.20 | 168.98 | 50 |
| `C-B2` | 123.20 | 168.95 | 50 |
| `C-B3` | 135.20 | 168.92 | 50 |
| `C-B4` | 147.20 | 168.89 | 50 |
| `C-C1` | 111.17 | 156.98 | 50 |
| `C-C2` | 123.17 | 156.95 | 50 |
| `C-C3` | 135.17 | 156.92 | 50 |
| `C-C4` | 147.17 | 156.89 | 50 |
| `C-D1` | 111.14 | 144.98 | 50 |
| `C-D2` | 123.14 | 144.95 | 50 |
| `C-D3` | 135.14 | 144.92 | 50 |
| `C-D4` | 147.14 | 144.89 | 50 |

**P1 — back-left**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P1-A1` | 74.12 | 223.11 | 50 |
| `P1-A2` | 86.12 | 223.11 | 50 |
| `P1-B1` | 74.12 | 211.11 | 50 |
| `P1-B2` | 86.12 | 211.11 | 50 |

**P2 — back-right**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P2-A1` | 173.85 | 224.18 | 50 |
| `P2-A2` | 185.85 | 224.24 | 50 |
| `P2-B1` | 173.91 | 212.18 | 50 |
| `P2-B2` | 185.91 | 212.24 | 50 |

**P3 — front-right**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P3-A1` | 177.62 | 115.17 | 50 |
| `P3-A2` | 189.62 | 115.17 | 50 |
| `P3-B1` | 177.62 | 103.17 | 50 |
| `P3-B2` | 189.62 | 103.17 | 50 |

**P4 — front-left**

| Label | X (mm) | Y (mm) | Z (mm) |
|---|---|---|---|
| `P4-A1` | 70.88 | 117.67 | 50 |
| `P4-A2` | 82.88 | 117.67 | 50 |
| `P4-B1` | 70.88 | 105.67 | 50 |
| `P4-B2` | 82.88 | 105.67 | 50 |

## How to move the camera to a label

1. **Home once per power cycle**, using Bambu's own safe sequence (`bambu/home_safe.py`): `G28 X`, raise Z 5 mm, slowly park at X128 Y254 (bare steel behind the grid), then `G28 Z P0 T140`. Never run a plain `G28`: it probes Z near the bed center, where the center island is. XY homing runs at firmware speed and can't be slowed down.
2. **Move slowly** so the wells don't slosh and the camera and light stay put. Use `M204 S500` (acceleration), XY at `F600` (10 mm/s) and Z at `F300` (5 mm/s). If Z goes up, lift first and then move XY. If Z goes down, move XY first and then lower. All moves are absolute (`G90`).
3. **Wait until the move is done before capturing.** The printer doesn't report its position, and the `gcode_line` acknowledgement only means the command was received. Queue `M400` and then turn the part fan on (`M106 P1 S255`). The move is finished when `cooling_fan_speed` changes in the MQTT report. Then turn the fan off again. `bambu/motion.py` does all of this and saves the last commanded position to `calibration/position.json`.
4. Grab a frame from `http://raspberrypi.local:8080/stream.mjpg` (`bambu/pi_frame.py`). Skip a few frames so a buffered one from before the move isn't used.

Example: `cd bambu && python3 motion.py 135.20 168.92 50` puts the camera over `C-B3`.

## Recalibration

Recalibrate if the grid islands, the camera, or its mount move. The steps are scripted in `bambu/` and take about 20 minutes:

1. `python3 jacobian.py 3`: measures how far the image moves (in pixels) per mm of X and Y, at Z50.
2. `python3 nadir.py`: finds the straight-down pixel by stepping Z from 50 to 70 mm and locating the point the image expands from.
3. `python3 calibrate_wells.py [labels…]`: centers each well under that pixel and records its position in `calibration/well_positions.json`.
4. `python3 fit_wells.py`: fits each island's 12 mm grid and prints the tables above.
5. `python3 verify_wells.py <labels…>`: blind check.
