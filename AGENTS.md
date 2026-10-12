# Workspace device references

- For the Raspberry Pi and its network camera, read [RASPBERRY_PI_CAMERA.md](RASPBERRY_PI_CAMERA.md). It contains verified discovery details, HTTP endpoints, and a read-only connection check.
- For the Bambu printer, see [bambu/README.md](bambu/README.md).
- For well labels and where to send the printer to image each well, see the "label-position mapping" section of [Model_Hardware_API_spec.md](Model_Hardware_API_spec.md). [well_map.png](well_map.png) shows the same layout as a picture.

Recheck device addresses and availability before use; the documented IP addresses are observations, not guaranteed static assignments.

# Moving the printer (camera gantry)

The Pi camera is mounted on the A1 toolhead. The bed carries the open-bottom PETG well grid: five islands with 10 mm tall walls.

- **Homing.** Never send a plain `G28`. It probes Z near the bed center, where the center island is. Use `bambu/home_safe.py` instead: it homes XY, then homes Z on bare steel at X128 Y254. The printer forgets its home position after a power cycle; `home_flag & 0b111 == 0b111` means homed.
- **Speed.** Move slowly so wells don't slosh and the camera and light don't shift. Use `bambu/motion.py`: XY at F600, Z at F300, acceleration `M204 S500`, absolute moves only. When Z goes up, lift before moving XY; when Z goes down, move XY before lowering.
- **Position.** The printer never reports XYZ. `motion.py` waits for each move to finish (it queues `M400` plus a part-fan toggle and watches the MQTT report), then saves the last commanded position to `calibration/position.json`.
- **Imaging a well.** Move to the table entry for its label. All captures are at Z = 50 mm, and the table's X/Y puts the camera straight above the well center (blind-checked to ≤ 0.25 mm on 2026-10-11). Grab a frame with `bambu/pi_frame.py`.
- **The camera is tilted about 10°.** The point straight below it is pixel (743, 350), not the image center. Any centering or region-of-interest work should use the nadir pixel stored in `calibration/nadir.json`.

# Recalibration

Recalibrate whenever the grid islands, the camera, or its mount move. The islands have already shifted up to ~3 mm from the STL layout. In `bambu/`, run in order: `jacobian.py 3`, `nadir.py`, `calibrate_wells.py`, `fit_wells.py`, `verify_wells.py <labels>`. Then update the tables in the API spec and redraw the map with `python3 calibration/plot_well_map.py`. Raw results live in `calibration/` (`well_positions.json` holds the measured values and `well_positions_fit.json` the per-island 12 mm grid fit). Ignore `well_positions_imagecenter_DISCARD.json`: those positions were centered on the image center, not the nadir.
