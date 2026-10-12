# Experiment Agent Harness Specification

*High-level spec, draft 1, 2026-10-12. The evidence behind each decision is in [reports/Scientific experiment agent harnesses.md](reports/Scientific%20experiment%20agent%20harnesses.md).*

## Purpose

This harness lets an LLM agent run and supervise a physical experiment on our rig while the experiment is running. The rig is a Bambu A1 used as a hot plate, a PETG well grid, and a Pi camera on the toolhead. The agent:

- watches the wells through the camera and reads the sensor feeds (bed temperature, printer state);
- notices when something is wrong or unexpected and decides whether to continue, modify, pause or abort;
- makes the protocol-level decisions the protocol assigns to it. In [experiment_protocol.md](experiment_protocol.md) these are choosing temperatures and run lengths, deciding when a run is done, and writing the analysis and validation prediction;
- asks the human operator for the physical steps it cannot do (filling wells, probe readings, cleaning).

The hardware commands themselves are defined in [Model_Hardware_API_spec.md](Model_Hardware_API_spec.md). This document specifies everything around them: who owns time and motion, what the model sees, how decisions are checked and recorded, and how a person stays in control.

## Why not just use a coding agent

Claude Code, Codex, OpenHands and Pi all share one design: a single tool-calling loop in which the model is the scheduler, the actuator and the judge. They wrap it in context compaction, file-based memory, permission modes, hooks and file checkpoints. That design rests on four assumptions, and a running experiment breaks all of them:

| Coding-harness assumption | What is true on the rig |
|---|---|
| Mistakes can be undone (git, checkpoints) | Heating cooks the egg white. Time spent cannot be recovered. Checkpoints do not cover physical side effects. |
| Files and command output show the whole state | The printer never reports XYZ. The camera can serve a stale frame. The islands drift (up to ~3 mm so far). |
| Nothing happens while the model thinks | Wells keep clouding and evaporating during inference, approval waits and API outages. |
| Checking a result is cheap and repeatable | Every run uses up samples. A missed observation is gone. |

There is also a measured weakness. On image pairs that each contained a real change, vision-language models answered "no difference" for 48–81% of low-level changes (VDiff-Bench, 2026). No model scored above 70% on spotting lab hazards (LabSafety Bench). Automated jailbreaks reach 100% success on LLM-driven robots (RoboPAIR). The model therefore cannot be the only detector of change, and it cannot be the safety layer.

## Design principles

1. **Invert control.** A deterministic **supervisor** owns the hardware, the clock and the record. The LLM sits above it as a slow, untrusted planner and judge. It proposes; the supervisor validates, executes and enforces.
2. **Code detects, the model interprets.** Classical image processing runs on every frame. The model is called on a schedule or when a metric is flagged, and receives a compact summary with evidence attached.
3. **The model is never on the critical path.** Every place where the harness waits for the model has a deadline and a safe default.
4. **The run record is the product.** Every observation, decision, approval and command is an event in an append-only log that a later reader can audit and replay.
5. **One writer for actuators.** Only one agent ever commands the printer. Helper agents may analyse but not act.
6. **Physical text is untrusted.** Text in camera frames, operator notes, files or tool output is data, never instructions.

## Architecture

```
 ┌────────────────────────────────────────────────────────────┐
 │ Operator console (phone/desktop): status, approve, pause,  │
 │ halt, log operator actions & readings                      │
 └──────────────┬──────────────────────────────▲──────────────┘
                │ commands (bypass LLM)        │ escalations
 ┌──────────────▼──────────────────────────────┴──────────────┐
 │ LLM agent loop: wakes on schedule/flag/operator event,     │
 │ reads a "wake packet", calls agent tools, returns a        │
 │ structured decision. Holds no hardware handle.             │
 └──────────────┬─────────────────────────────────────────────┘
                │ proposals (typed plans, verdicts)
 ┌──────────────▼─────────────────────────────────────────────┐
 │ Supervisor (deterministic, always running)                 │
 │  • Run engine: protocol steps, schedule, pause/abort/halt  │
 │  • Validator: checks proposals against policy & state      │
 │  • Watchdogs/suspenders: freshness, temp band, deadlines   │
 │  • Perception pipeline: per-frame CV features & flags      │
 │  • Run record writer                                       │
 └──────────────┬─────────────────────────────────────────────┘
                │ allowlisted verbs only
 ┌──────────────▼─────────────────────────────────────────────┐
 │ Device layer: motion.py, home_safe.py, pi_frame/camstream, │
 │ bambu_link (MQTT), heater                                  │
 └────────────────────────────────────────────────────────────┘
```

The device layer already exists in `bambu/` and implements the safety rules in [AGENTS.md](AGENTS.md) informally. The supervisor turns those rules into enforced policy.

## Features

Priority: **P0** is required before any unattended run, **P1** makes supervision reliable and affordable, **P2** comes later.

### F1. Supervisor and the agent's tool surface (P0)

The agent never sends G-code, opens MQTT or reads the camera stream directly. It sees a small set of typed tools. Each tool carries a **reversibility class** that determines its approval gate.

| Tool | Class | Gate |
|---|---|---|
| `get_status()`: run state, step, temperatures, homed flag, calibration version, staleness of each feed | read | none |
| `get_well_state(labels)`: per-well record (see F4) | read | none |
| `get_well_images(label, t0, t1, n, kind)`: crops from the archive (raw, difference, strip) | read | none |
| `get_sensor_summary(signal, window)`: stats + small plot | read | none |
| `capture_now(labels)`: extra out-of-schedule capture | reversible | standing permission, rate-limited |
| `set_imaging_schedule(labels, period)`: change which wells are imaged and how often | reversible | validator only |
| `set_temperature(target)` / `set_temperature_program(points)` | **irreversible** (acts on samples) | validator + within protocol envelope, otherwise human review |
| `mark_well(label, status, reason)`: exclude/flag a well (bubble, leak, empty) | reversible (annotation) | none, logged |
| `end_run(reason)` / `pause_run(reason)` | compensable | validator; pause is always allowed |
| `request_operator(task, fields, deadline)`: ask a human to do or measure something | — | operator answers |
| `ask(question, options, default, deadline)`: clarification | — | operator answers or default applies |
| `home_safe()`, `recalibrate(step)` | compensable | human review |
| `write_note(text)`: lab notebook entry | read/write notebook | none |

The supervisor enforces **outside the agent's reach**:

- the XY/Z envelope, keep-out zones over the island walls, the homed precondition, and Z-up-before-XY / XY-before-Z-down ordering;
- the speed and acceleration caps. These come from one policy file, not per-script constants. For example, `kinetics_run.py` raises XY to F1800 to reach about 1 s per well, while AGENTS.md says F600; the policy file should be the one place that number lives;
- the bed temperature ceiling (75 °C, set by PETG softening) and a maximum ramp rate;
- rate limits on captures and moves.

Policy, limits and protocol envelopes are files the agent cannot write. Coding-harness instruction files like CLAUDE.md are "context, not enforced configuration". The first AI Scientist tried to edit its own timeouts.

### F2. Time, wake-ups and watchdogs (P0)

The supervisor owns the clock. The agent loop is **event-driven**, not polling, and wakes on:

- **heartbeat**: a protocol-defined period (for example every 60–120 s during a kinetics run, every 15 min during a long hold);
- **flag**: a perception or sensor metric crossed a threshold (F3);
- **step boundary**: a protocol step started, finished or timed out;
- **operator event**: the operator logged an action, a reading or a message;
- **deadline**: a pending question or operator task timed out.

Every wake message states the wall-clock time, the run time (time since t = 0), the time since the last wake and the time since each well was last seen. The model has no reliable sense of elapsed time. This goes in the appended message, not the system prompt, so the cached prefix stays stable.

**Leases and safe defaults.** Every wake has a response deadline. If the model is late, errors out, or the API is down, the supervisor applies the step's declared default and notifies the operator. The default is one of: *continue the current schedule*, *hold temperature and keep imaging*, or *park and pause*. After an outage, the run resumes under agent control only when the operator re-enables it. During the outage the supervisor keeps imaging and logging, so no data is lost.

**Watchdogs and suspenders** (Bluesky-style). These are background conditions that pause the run when a signal leaves its band and resume it, with hysteresis, once the signal returns:

- MQTT report age, camera frame age, and an identical-frame check (stale stream);
- motion-complete timeout. If the `M400` plus fan-toggle confirmation doesn't arrive within the expected travel time, that is a fault, not a retry;
- bed temperature outside the step's band;
- frame brightness at the white and black reference wells out of band (lighting or exposure drift).

Stopping uses graded verbs: **pause** at the next safe point, **pause now** (motors stopped), **stop** (clean up, mark success), **abort** (save data, mark failed), and **halt** (immediate: heater off, Z up, no cleanup).

### F3. Perception pipeline (P0 core, P1 escalation)

Runs in the supervisor on **every** frame, without the LLM. The trial run produced 657 frames in 10 minutes across 4 wells.

Per frame and well, computed on the ROI centred on the calibrated nadir pixel (`calibration/nadir.json`):

- mean intensity and colour in Lab space; opacity W and conversion α from the protocol's formula, using the black and white reference wells;
- change from the well's baseline and from its previous frame (histogram distance, SSIM);
- quality metrics: blur (variance of Laplacian), saturation/clipping fraction, frame staleness;
- rim-centring error: where the well's rim sits relative to the nadir pixel. Drift here means the islands or the camera have moved, so the response is recalibration, not more imaging;
- blob/speck count (bubbles, debris) and a crude fill-level/meniscus estimate.

**Cross-well context**: compare each well with its siblings (same contents, same island). A change across the whole plate points to lighting, exposure or focus. A change in one well points to that well's sample.

**Flags** come from deterministic thresholds on these features, not from the model's self-reported confidence. Examples: α jumps or reverses, a replicate diverges from its siblings, a reference well drifts, blur or clipping rises, the rim is off-centre, the stream is stale.

**Escalation to the VLM**: on a flag or a slow heartbeat, the wake packet includes the flagged wells' baseline, previous and current crops, the numeric deltas, and the protocol's expected behaviour for that phase. The model returns a structured verdict (`normal | artifact | sample_issue | instrument_issue | unsure`, with evidence and a proposed action).

### F4. Context management (P1)

This is the feature coding harnesses lack most. The model's context is rebuilt from structured state rather than accumulated chat history.

**Per-well state record** (outside the chat, in the run store):

```
label, contents (egg/water/black/white/empty), replicate group,
status (active/excluded/flagged), baseline image id,
last N image ids, feature series summary (W, α, t10/t25/t50 so far),
open concerns, last verdict + time
```

**Wake packet.** On each wake the model receives one compact message:

1. time block (F2);
2. run and step status, and what the protocol expects in this phase;
3. a table of the per-well state, text only;
4. sensor summary: stats header plus one small plot (bed temperature vs target with the expected band, α curves of all active wells). Research shows plots beat raw numbers for trend and anomaly questions, while raw values are better for single-point spikes, so raw values are added only for flagged windows;
5. images **only for wells that need them**: baseline / previous / current crops. Each crop is about 224 px, costing about 64 tokens on Claude, versus about 1,560 for a full frame. Sixteen wells × 3 crops costs about 3k tokens, so even a full-plate check is affordable;
6. pending operator tasks and questions.

**Image lifecycle.**

- Images are referenced by ID (Files API or the harness's own store). Old image tool results are cleared, and only the most recent one or two image-bearing results stay in context.
- **Baselines are pinned**: the t = 0 crop of each well and the reference wells.
- The model can always pull history with `get_well_images` (a time-lapse strip or difference image) when it wants to look again. This is the "retrieve on demand" pattern from long-video agents.

**Compaction.** The run's narrative lives in the notebook and the decision log (F8), not in chat. At compaction, or on a new session, the agent rereads `get_status`, the per-well table and the last few decisions. Any image found during compaction is dropped and its ID kept.

**Cache layout.** A stable prefix (system prompt, tool list, protocol, policy summary) is followed by the volatile wake packet. Clearing images breaks the prompt cache, so measure eviction cost against cache hits on recorded runs before tuning.

### F5. Protocol representation (P1)

Protocols become machine-readable step lists that the supervisor executes and the agent reasons over. Each step is:

```
{ id, action, args,
  preconditions,          # e.g. homed, bed within 1 °C of pre-hold, operator confirmed "wells loaded"
  expected_observation,   # e.g. egg wells stay clear (α < 0.05) for 15 min
  tolerance, timeout,
  on_violation,           # pause+escalate | notify | ask | abort
  agent_may_modify,       # which args the agent may change, and within what envelope
  default_on_no_response }
```

- **Decision points** are explicit steps where the protocol hands a choice to the agent, each with an allowed envelope. Examples: choosing Phase 2 temperatures inside 55–75 °C; ending a run once all egg wells reach a plateau, inside a minimum and maximum duration.
- **Operator steps** are steps too, for example "fill wells per layout", "add oil layer", "probe C-B2 and enter °C". They have preconditions and need confirmation, so the run cannot reach t = 0 until the operator confirms. This replaces the `GO`/`STOP` trigger files.
- The **intended plan** is stored beside the **executed trace**, and the difference between them is logged as a divergence record.
- A **validator** checks every plan or modification before execution and returns errors to the agent: labels exist, preconditions are satisfiable, temperature and duration are within envelope, and the imaging cycle time fits the requested schedule. Validate-and-retry loops are what gave the EOS lab agent 94–97% first-attempt success.

### F6. Science-specific features (P1)

These are features a coding harness has no reason to have:

- **Randomisation and layout.** The harness generates well assignments (egg replicates, water, references) and run order from a seed and records them. The agent can request a layout, but the randomisation is reproducible and logged.
- **Pre-registration.** For Phase 4 validation, the agent's predicted α(t) per well is written, hashed and timestamped *before* the held-out program starts, and the agent cannot change it afterwards.
- **Blinding / information hiding.** Some information is withheld from the agent until a defined point, for example the held-out validation program. The harness enforces this; the prompt does not.
- **Controls and references as first-class objects.** Black, white and water wells feed the opacity calibration and the drift suspenders.
- **Deterministic recomputation.** Any number the agent states that drives an action (a t₅₀, a fitted Eₐ, "plateau reached") is recomputed by the analysis code from the raw record before the action runs. If they disagree, the action is blocked.
- **Calibration versioning.** Every run pins hashes of `well_positions_fit.json`, `nadir.json` and the camera settings. A rim-centring drift flag proposes a recalibration step.

### F7. Human in the loop (P0 takeover, P1 ladder)

**Operator takeover goes straight to the supervisor**, independent of the LLM process: pause, halt, end run, set temperature.

**Escalation ladder**, chosen by deterministic triggers:

| Level | When | Effect |
|---|---|---|
| log | routine capture, normal verdict | record only |
| notify | suspender tripped and recovered, one well excluded | push message, run continues |
| question | ambiguous well, unclear operator note | non-blocking `ask`; the default proceeds at the deadline |
| review | action outside envelope, recalibration, re-homing, temperature change not pre-approved | blocks that action; run otherwise continues on schedule |
| halt | motion timeout with unknown position, temperature above ceiling, watchdog with operator unreachable | supervisor halts, pages operator |

Every escalation includes a thumbnail, the proposed action, a one-line rationale, **what will happen if nobody answers by time T**, and one-tap approve / deny / pause.

**Operator as instrument.** Readings the operator enters through `request_operator` (probe temperature, egg brand and pack date, room temperature, observations) become typed events in the run record.

### F8. Run record and provenance (P0)

The run record is an append-only event log on the Bluesky event-model pattern, stored per run under `runs/<run_id>/`:

- **RunStart**: protocol and version, parameters, layout and seed, calibration and camera-setting hashes, model ID, system-prompt and tool-schema hashes, operator, sample metadata.
- **Streams**: `frames` (pointer to the image file plus SHA-256, label, commanded XYZ, time since move completed), `features` (F3 outputs), `temps`, `printer_state`, `operator`, `agent_decisions`.
- **Decision events**: the IDs of the images and readings the model was shown, the wake reason, the verdict, the proposed action, the validator result, the approval (who and when), the executed command, tokens and latency.
- **RunStop**: exit status (succeeded / aborted / failed), reason, and who ended it.

The **lab notebook** is a human-readable view generated from this record, plus the agent's `write_note` entries. It is not a separate source of truth.

### F9. Memory across runs (P2)

- **Campaign state**: the runs done so far, temperatures covered, current Eₐ estimate and next run. The agent reads it at session start.
- **Lessons file**: curated observations such as "bubbles look like early clouding at t < 60 s" or "corner P3 runs ~1 °C hot". The agent proposes entries and the operator approves them, so one bad run doesn't poison later ones.
- **Session-start ritual**: before acting, the agent checks the homed flag, calibration age and hash, a reference-well centring check, camera settings, and the campaign state.

### F10. Evaluation and replay (P2)

- **Replay mode.** The supervisor can feed a recorded run's frames and sensors to the agent as if live, at accelerated time, with no hardware. Every archived run (including `tmp_runs/egg_trial1_*`) becomes a test case for a new prompt, model or context policy.
- **Injected anomalies.** Synthetic perturbations of recorded runs: a bubble pasted into a well, an exposure drift, a stale frame, a temperature overshoot, a misaligned crop, a leaking neighbour. Each has a known correct response.
- **Metrics**: detection rate and delay per anomaly type, false-alarm rate, correct-action rate, tokens and cost per run-hour, and decision latency.
- **Simulator backend**: a fake printer and camera implementing the device-layer interface, used for supervisor tests and validator dry runs.

## How a Phase 2 run looks under the harness

1. **Session start.** The agent reads the campaign state, checks status and calibration, and proposes this run's temperature from the Phase 1 window. The validator checks it against the envelope.
2. **Prep.** The supervisor generates the randomised layout. The agent sends operator tasks: separate whites, fill wells per layout, add oil. The bed holds the pre-hold temperature, and the operator confirms each step on the console.
3. **Baseline.** The supervisor images all wells and pins the baselines. The agent reviews the baseline crops (empty or bubbly wells, white reference visible) and may mark wells as excluded.
4. **t = 0.** The supervisor jumps the bed temperature and starts cyclic imaging at the fastest safe cycle. The agent wakes every ~90 s and on flags.
5. **During the run.** CV computes α(t) per well. A typical wake costs about 1–4k tokens: text table, one plot, crops only for flagged wells. Example flag: "C-B3 α diverged from replicates by 0.3". The agent pulls a strip, sees a bubble, and marks the well `artifact` (excluded from fit, still imaged).
6. **End.** Once every active egg well reaches a plateau, and within the protocol's duration bounds, the agent proposes `end_run`. The supervisor recomputes the plateau test, then runs the 75 °C end point and cooldown, and asks the operator to clean.
7. **After the run.** The analysis code computes t₁₀, t₂₅ and t₅₀. The agent writes the notebook summary and updates the campaign state with the next temperature.

If the API drops at step 5, the supervisor keeps the temperature and imaging going, logs everything, notifies the operator, and waits for re-enable. If the camera stream goes stale, a suspender pauses the timepoint bookkeeping and pages the operator.

## Open questions to settle with recorded runs

- **Image format for change detection.** Separate images, a side-by-side composite or a difference image: no published study compares them. Test on archived runs.
- **Eviction vs. caching.** How many image results to keep live, and whether to clear them at all given the cache cost.
- **Heartbeat period and flag thresholds.** These trade cost and false alarms against detection delay. Tune them in replay with injected anomalies.
- **Model choice per role.** Does a smaller or faster model suffice for routine heartbeats, with escalation to a larger model on flags?
- **Firmware limits.** Does the A1 firmware expose soft limits? If not, the supervisor is the only bound below the model.

## Build order

1. **MVP (P0):** supervisor with policy file, allowlisted tools, run record, watchdogs and graded stop verbs, operator console with takeover, CV features and flags. Agent wakes on heartbeat and flags with a text-plus-crops wake packet.
2. **P1:** typed protocol steps and validator, per-well state and image lifecycle, escalation ladder, science features (randomisation, pre-registration, recomputation).
3. **P2:** replay and injected-anomaly evals, simulator, campaign memory and lessons, model routing.
