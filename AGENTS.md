# Workspace device references

- For the Raspberry Pi and its network camera, read [RASPBERRY_PI_CAMERA.md](RASPBERRY_PI_CAMERA.md). It contains verified discovery details, HTTP endpoints, and a read-only connection check.
- For the Bambu printer, see [bambu/README.md](bambu/README.md).
- For well labels and where to send the printer to image each well, see the "label-position mapping" section of [Model_Hardware_API_spec.md](Model_Hardware_API_spec.md). [well_map.png](well_map.png) shows the same layout as a picture.

Recheck device addresses and availability before use; the documented IP addresses are observations, not guaranteed static assignments. The printer's address changes after power cycles; `bambu_link.Printer()` rediscovers it by serial number.

Run scripts with `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`: it has OpenCV, paho-mqtt and scikit-learn. The `python3` first on PATH may be the system 3.9 without them.

# Moving the printer (camera gantry)

The Pi camera is mounted on the A1 toolhead. The bed carries the open-bottom PETG well grid: five islands with 10 mm tall walls.

- **Homing.** Never send a plain `G28`. It probes Z near the bed center, where the center island is. Use `bambu/home_safe.py` instead: it homes XY, then homes Z on bare steel at X128 Y254. The printer forgets its home position after a power cycle and after about 1 h idle; `home_flag & 0b111 == 0b111` means homed. Check before every move.
- **Speed.** Use `bambu/motion.py`: XY at F1800 (30 mm/s, approved 2026-10-12), Z at F300, acceleration `M204 S500`, absolute moves only. When Z goes up, lift before moving XY; when Z goes down, move XY before lowering. Keep Z moves to a minimum: the camera mount shifted during repeated Z50 ↔ Z100 cycles.
- **Position.** The printer never reports XYZ. `motion.py` waits for each move to finish using the camera: the move's kinematic duration plus a margin, then 4 consecutive still frames. Homing waits for camera stillness and the MQTT `home_flag`. Never use the part fan as a completion signal. The last commanded position is saved to `calibration/position.json`.
- **Imaging a well.** Move to the table entry for its label (`bambu/wellmap.py` loads it from `calibration/well_positions_fit.json`). All captures are at Z = 50 mm, and the table's X/Y puts the camera straight above the well center (all 32 blind-checked to ≤ 0.15 mm on 2026-10-12). Grab a frame with `bambu/pi_frame.py`. Measure a well on the centred 7 × 7 mm square (`bambu/well_gray.py`), which leaves 1.5 mm margin to the walls.
- **The camera is tilted.** The point straight below it is pixel (690.5, 362) as of 2026-10-12, not the image center. Any centering or region-of-interest work should use `wellmap.nadir()` (`calibration/nadir.json`, key `nadir_px`).
- **The camera mount can move.** `bambu/cam_tilt.py` measures camera rotation from the toolhead parts at the top of every frame against `calibration/camera_ref.png` (≈ 4.9 px per mm at the wells). Add the shift to the nadir pixel; recalibrate if it exceeds about 2 px. `bambu/tilt_logger.py` logs it every 30 s.

# Recalibration

Recalibrate whenever the grid islands, the camera, or its mount move. The islands have already shifted up to ~3 mm from the STL layout. In `bambu/`, run in order: `cam_tilt.py ref` (then confirm the mount is still), `jacobian.py 3`, `nadir.py` (refine the result as described in the API spec's Recalibration section), `SEED=<old well_positions.json> calibrate_wells.py`, `fit_wells.py`, `verify_wells.py <labels>`. Then update the tables in the API spec and redraw the map with `python3 calibration/plot_well_map.py`. Raw results live in `calibration/` (`well_positions.json` holds the measured values and `well_positions_fit.json` the per-island 12 mm grid fit). Ignore `well_positions_imagecenter_DISCARD.json`: those positions were centered on the image center, not the nadir.
