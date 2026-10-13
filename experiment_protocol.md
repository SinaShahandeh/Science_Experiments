# 1 — Activation energy of heat-induced aggregation of egg-white proteins

*Camera-based turbidimetry on the Bambu A1 bed and the printed PETG well grid. Draft protocol.*

## Question

How does the rate at which egg-white proteins aggregate (turn opaque) depend on temperature, and what is the apparent activation energy (Eₐ) of that process?

## What the camera measures

Heat unfolds the proteins (denaturation). Unfolded proteins stick together into disordered clumps (aggregation) and eventually form a gel (coagulation). The clumps scatter light, so against the dark bed a well gets brighter as aggregation proceeds. The camera therefore sees aggregation, not unfolding: the result is the apparent activation energy of aggregation-driven clouding. Strictly, measuring scattered light is nephelometry; turbidimetry is the common umbrella term.

## Approach

Isothermal runs at 4–6 temperatures: one temperature per run, many replicate wells per run. For each well, measure the time to reach fixed fractions of full opacity, then fit ln(1/t) against 1/T (Arrhenius). Validate by predicting a temperature program that was not used for fitting.

## Materials

- A1 bed as the hot plate. Printed PETG open-bottom grid: center 4×4 island and four corner 2×2 islands, 10 mm openings, 10 mm tall walls, spanning X/Y 63–193 mm. The bed surface is the well floor.
- Raspberry Pi camera on the toolhead.
- Eggs (record brand, pack date and storage), cooking oil, water, food coloring, kitchen probe thermometer, 1–5 mL medicine syringe, a white reference (white print or paper), paper towels, isopropyl alcohol.

## Limits

- Bed at most 75 °C. PETG softens from about 70 °C, and the A1 bed tops out near 80 °C in practice.
- The camera needs to move on top of each well and take a photo at that point. During the heating, we need to be able to take a photo as fast as possible across the points by moving the camera to appropriate wells. At higher temperatures, we might limit the number of wells that we fill up with egg white. 
- Camera exposure, white balance and focus locked. Auto-exposure would dim the frame as wells turn white and cancel part of the signal.
- Home Z with the head at X128 Y254, outside the grid, as Bambu's own start G-code does.

## Phase 0 — Qualify the instrument (water only, once)

1. **Leak test.** Dyed water in alternate wells, 75 °C for 30 min. Color in a neighbouring well means the open-bottom seal leaks.
2. **Camera.** Raise the camera until the center island (or all five islands) fits in the frame, then lock its settings. Mark each well's region of interest: the central part of the opening that is visible past the 10 mm walls.
3. **Well temperature vs bed temperature.** Water with an oil layer in 4 center wells and 1 well per corner island. Bed at 45, 55, 60, 65, 70 and 75 °C; after 5 min at each, read every well with the probe. This gives T_well as a function of bed temperature and position.
4. **Step response.** Hold 50 °C, jump to 70 °C, and read one center well every 15 s for 5 min. This heat-up curve is used in the analysis.
5. **Evaporation.** Compare the fill level of oil-covered and uncovered water wells after 30 min at 70 °C.

## Phase 1 — Find the temperature window (one run)

1. Load egg white with the bed at 50 °C and watch for 15 min. It must stay clear; if it clouds, use a lower pre-hold temperature.
2. Ramp 50 → 75 °C at about 1 °C/min. Note where clouding starts and how fast it proceeds.
3. Choose 4–6 temperatures where the half-time to full opacity is between about 5 and 30 min. Faster runs mostly measure the heat-up; slower ones run into evaporation and drift.

## Phase 2 — Isothermal temperature-jump runs (one temperature per run)

1. Separate 3 eggs with no yolk. Stir each white gently without foaming, then let it rest 10 min so bubbles rise; bubbles look white.
2. Hold the bed at the pre-hold temperature validated in Phase 1 (45–50 °C).
3. Fill wells with 0.3 mL each (about 3 mm deep):
   - 12 center wells: egg white, 4 per egg, positions randomized
   - 2 center wells: water, for temperature spot-checks
   - 1 center well: empty (black reference)
   - 1 center well: white reference
   - Corner islands: optional extra replicates, to check center vs corner temperature
4. Add a thin oil layer to every filled well.
5. Wait 3–5 min to equilibrate, then start taking a frame every 5 s.
6. **t = 0:** jump the bed to the target temperature. Record until clouding plateaus, or for 40 min.
7. **End point:** 75 °C for 10 min to fully cloud every well. This is each well's 100% reference.
8. Cool and clean thoroughly. Leftover cooked protein can seed aggregation in the next run.

Run the temperatures in shuffled order, so drift in the room or the eggs does not look like a temperature effect.

## Phase 3 — Analysis

1. Per frame and well: opacity W = (I − I_black) / (I_white − I_black), averaged over the region of interest.
2. Per well: conversion α(t) = (W − W₀) / (W_end − W₀).
3. Per well: times t₁₀, t₂₅ and t₅₀ to reach α = 0.10, 0.25 and 0.50.
4. Per temperature: mean and standard error across wells, at the calibrated well temperature in kelvin.
5. **Arrhenius fit.** Weighted fit of ln(1/t_α) against 1/T for each α, giving Eₐ with a 95% interval that includes the temperature-calibration uncertainty.
6. **Diagnostics:**
   - Eₐ differs between α levels: more than one step.
   - The Arrhenius plot bends: reversible unfolding before aggregation (Lumry–Eyring).
   - A lag before clouding at low temperatures: nucleation.
   - Center and corner wells differ: a bed temperature gradient.
7. **Model fit.** First-order kinetics with an Arrhenius rate constant, integrated over the measured well-temperature history including the jump, compared against a nucleation–growth model.

## Phase 4 — Validation with a held-out temperature program

1. Pick a program not used for fitting, for example 64 °C for 10 min then 70 °C, or a 0.5 °C/min ramp from 50 °C.
2. Before running it, predict each well's α(t) from the fitted model and the calibrated well temperature.
3. Run it and compare. For example, pass if t₅₀ is predicted within ±15%.

## Roles

- **Operator (human):** separating whites, filling wells, oil layers, thermometer readings, cleaning.
- **Agent:** choosing the temperatures and run lengths, deciding when a run is done, the analysis, and the validation prediction.
- **Environment:** the limits above, camera settings, and the held-out validation program, which the agent does not see in advance.

## Record per run

Egg brand and pack date, time the whites were separated, fill volume, overlay, room temperature, bed temperature log, all frames, and timestamped operator events.
