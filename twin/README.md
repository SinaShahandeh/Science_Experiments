# Digital twin of the rig

A stand-alone simulation of the egg-white rig behind the same HTTP API as `mhapi-service`
([Model_Hardware_API_spec.md](../Model_Hardware_API_spec.md)). Use it to develop and check agent runs before
they touch the printer: the `mhapi` CLI and the harness only need `MHAPI_URL` to switch between the twin
(`http://127.0.0.1:8743`) and the rig (`http://127.0.0.1:8742`).

```bash
.venv-harness/bin/python -m twin --speed 10          # serve on 127.0.0.1:8743
MHAPI_URL=http://127.0.0.1:8743 .venv-harness/bin/python -m mhapi.cli get_status
```

Options: `--speed` (simulated seconds per wall second, default 1), `--manual-operator` (answer load and clean
requests yourself on `/operator` or the dashboard instead of the simulated person), `--seed`, `--sessions`.

## What matches the spec

- **Commands, fields, errors and HTTP codes** as in the API spec and the `mhapi` session's choices:
  `list_wells` returns `{labels, t}`; `get_status` has `safety_reason`, `t_safety`, `not_ready`, `last_operation`;
  400 for `invalid_argument` / `unknown_label` / `temperature_limit`, 409 for `not_homed` / `already_loaded` /
  `bed_hot` / `halted` / `camera_moved` / `operator_*`, 500 for `move_failed` / `homing_failed`, 503 for
  `printer_unavailable`. `describe` serves `mhapi.tools.tool_definitions()`, the same text the real service holds.
- **One-at-a-time queue** for `prepare`, `get_image`, `get_raw_opacity`, `load_well`, `clean_all_wells`; other calls
  answer at once.
- **Limits from `mhapi/safety.toml`**: 100 °C target limit, `bed_hot` above 60 °C (load) and 40 °C (clean),
  15 min operator timeout, heater watchdog after 15 min without any call, over-temperature and stuck-heater
  halts, F1800 / F300 / 500 mm/s² motion.
- **Operator console** at `/operator` (Done / Problem / Stop, Clear while halted) and the endpoints the dashboard
  uses: `GET /operator/state`, `POST /operator/answer {id, answer, note}`, `POST /operator/stop`,
  `POST /operator/clear {token}`. As on mhapi-service, `GET /operator/status` gives get_status's body without
  counting as a call (no heater-watchdog reset, not in `calls.jsonl`).
- **Session folders** under `sim_sessions/sim_<date>_<time>/`: `calls.jsonl`, `temps.csv`, `events.csv`, `wells/`
  (the agent's 272 × 272 PNGs and the plain 68 × 68 crops), `state.json`, `calibration.json`. Full frames are not
  rendered.

## What is simulated, and from what

| Part | Model | Source |
|---|---|---|
| Positions | Well table from `calibration/well_positions_fit.json`; a well's bed position is its table entry + the camera offset (3, −33) mm | calibration, API spec |
| Motion | Trapezoidal moves at 30 mm/s, 500 mm/s²; Z at 5 mm/s; lift first, lower last; ~0.12 s command latency + 0.25 s stillness wait. A neighbouring well takes ~0.85 s, opposite corners ~6 s | API spec timings, 2026-10-11 trial |
| Homing | Safe sequence (lift, `G28 X` sweeping X and Y fast, park X128 Y254, probe Z, lift to Z50), ~1 min. The printer loses its home 44–62 min after its last move. Homing with wells loaded can add a bubble (slosh) | safety spec M4, observations |
| Bed heater | Heated bed (with the thermistor) and the build plate on it, PID on the reading; reports reach the computer ~6 s late. Fitted to the trial's 50 → 75 °C step (RMS 1.0 °C: 75 °C after ~44 s, peak 76.3 °C at ~60 s); every jump overshoots 1–2 °C. Heat losses set so 75 → 40 °C with the heater off takes ~13 min (no cooling data yet). Readings quantized to 1/32 °C, reported every 1–4 s | `tmp_runs/egg_trial1_*/temps.csv` |
| Liquid temperature | First-order lag behind the plate (τ ≈ 49 s, fitted) settling slightly below it: η = 0.968 in the center island, 0.952 in the corners (~1 °C cooler at 75 °C) | fitted lag; the offsets are assumptions (Phase 0 would measure them) |
| Clouding | Two parallel first-order reactions, with optics that saturate (below) | literature + trial |
| Camera image | The 68 × 68 px cell rendered from the rig's measured colors: bed (30, 8, 35), PETG wall tops ~184 gray seen 9% larger, shaded inner walls, liquid from clear purple-black through blue-gray to pale mint, a fixed LED glint per well (the one in P3-B2 crosses the measured square), sensor noise, slight blur | calibration frames, trial frames |
| Opacity reading | The service's algorithm (port of `bambu/well_gray.py`) run on the rendered pixels: drop saturated pixels + 2 px halo, drop bright outliers above median + 3 MAD, take the median | API spec |
| Lighting | LED bar dims ~0.8%/h (`wall_gray` follows); room light on adds ±2% flicker with a 2.5 s period | API spec, flicker measurements |
| Variation | Per well: rate × lognormal(σ 0.10), lag × lognormal(σ 0.08), clear gray 22 ± 1.2, cooked gray 200 ± 3.5%; ~6% of loads trap a small bubble | trial spread across four wells |

### Kinetics

Each well's egg white holds two protein populations whose native fractions decay with first-order rates
k = k_ref · exp(−Ea/R · (1/T − 1/T_ref)), T the liquid temperature, T_ref = 72 °C:

| Fraction | Ea | Rate | Source |
|---|---|---|---|
| Ovotransferrin (fast, ~13% of protein) | 391 kJ/mol | half-life 1.5 min at 62 °C | Shibata-Ishiwatari et al. 2018 (whole egg white, DSC kinetics) |
| Ovalbumin (most of the whitening, ~54%) | 450 kJ/mol (literature 408–480) | half-life 1.9 min at 75 °C, fitted to the trial (literature 2–7 min) | Shibata-Ishiwatari et al. 2018; Weijers et al. 2003 |

Aggregation follows unfolding quickly, so there is no lag at constant temperature (Van der Plancken et al.
2006, Weijers et al. 2003). The lag before clouding in a run comes from the liquid warming up. A 3 mm layer of
~10% protein is optically thick, so brightness saturates: W = (1 − exp(−β·x)) / (1 − exp(−β)) with
x = w_ot·A_ot + (1 − w_ot)·A_ova, β = 11, and ovotransferrin alone giving W = 0.22 (milky).

`python -m twin.calibrate_kinetics` refits the rates, β and the lag to the four P3 wells of the 2026-10-11 trial
(RMS 3.1% of the final value). The trial ran under camera settings v1, which hid clear egg white below black,
so the fit assumes the first 25% of the rise (the ovotransferrin milkiness) was hidden.

What follows for experiments (liquid held isothermal, half-time to the protocol's 75 °C / 10 min end point):
62 °C 48 min, 64 °C 19 min, 66 °C 7 min, 68 °C 2.8 min, 70 °C 1.1 min. For corner wells the bed reads ~2.5 °C
above the liquid, so half-times of 3–30 min need a bed between about 65.5 and 71 °C.

**Ground truth.** `GET /sim/truth` returns the true parameters, each loaded well's hidden variation, and the
apparent Ea an ideal isothermal experiment between 64 and 69 °C would measure (≈ 448 kJ/mol, dominated by
ovalbumin). The agent never sees it; use it to score an agent's Arrhenius analysis. Expect an analysis that uses
the bed temperature and ignores the warm-up lag to land lower.

References: Shibata-Ishiwatari N. et al. (2018) Jpn J Food Eng 19:49–56, doi:10.11301/jsfe.17501 ·
Weijers M. et al. (2003) Protein Sci 12:2693–2703, doi:10.1110/ps.03242803 ·
Van der Plancken I. et al. (2006) J Food Eng 75:316–326, doi:10.1016/j.jfoodeng.2005.04.019 ·
Van der Plancken I. et al. (2007) J Food Eng 78:1410–1426, doi:10.1016/j.jfoodeng.2006.01.013 ·
Donovan J.W. et al. (1975) J Sci Food Agric 26:73–83, doi:10.1002/jsfa.2740260109 ·
Kuang J. et al. (2023) Foods 12:2528, doi:10.3390/foods12132528.

## Simulation-only endpoints

| Endpoint | Use |
|---|---|
| `GET /sim/geometry` | wells, islands, camera and lens parameters (the dashboard builds its 3D scene from it) |
| `GET /sim/state`, `GET /sim/stream` | full state; server-sent `state` events at 10 Hz and `rig` events (captures, operator, safety) |
| `GET/POST /sim/clock` | simulated time and speed. The harness posts `{thinking: true}` while the model works: time then runs at 1×, so an agent's pacing is realistic at any speed and only waits get shorter |
| `POST /sim/control` | `speed`, `auto_operator`, `room_light`, `led_drain_per_h`, and faults: `bubble` (label), `lose_home`, `camera_bump` (mm), `printer_offline` (seconds), `heater_stuck`, `island_shift` (island, dx, dy). `{"reset": true}` gives a fresh rig (empty wells, cold bed, no faults, not homed, new session folder); the harness sends it when a run starts with "fresh rig" ticked |
| `GET /sim/truth` | the true kinetics (above) |

## Not simulated

The MQTT and G-code layer (the `mhapi` session's `mhapi/sim/` covers that, under the real service), full camera
frames, the camera's slow mount creep beyond a small random drift, evaporation, and leaks between wells.
