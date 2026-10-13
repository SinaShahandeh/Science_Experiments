# Experiment agent harness and dashboard

Claude (`claude-opus-5-5`) runs experiments on the rig through the hardware API's tools; the harness owns the
loop, the clock line, the run record, and a dashboard to watch it. Develop against the digital twin
([twin/README.md](../twin/README.md)); switch to the rig by pointing `--rig` (or `MHAPI_URL`) at mhapi-service.

```bash
.venv-harness/bin/python -m twin --speed 10      # terminal 1: the digital twin on :8743
.venv-harness/bin/python -m harness              # terminal 2: harness + dashboard on :8750
open http://127.0.0.1:8750
```

On the rig ([instrument mode](#instrument-mode)), the same page shows the real cameras and the service's state:

```bash
bin/mhapi-service --quiet                                           # terminal 1: the hardware service on :8742
.venv-harness/bin/python -m harness --rig http://127.0.0.1:8742     # terminal 2: harness + dashboard on :8750
```

`.venv-harness` is a venv of the 3.12 framework Python with its site packages (OpenCV, numpy, pandas, matplotlib)
plus `anthropic` 1.x. The API key comes from `ANTHROPIC_API_KEY` in the environment or in `.env` at the repository
root (git-ignored).

## How a run works

1. **Prompt.** You give a run prompt (the dashboard has two examples: the four-temperature Arrhenius
   experiment, and a short two-well test). The system prompt describes the instrument at the level the API
   does (wells, labels, an operator), how time works, the data and the safety rules. It can attach
   [experiment_protocol.md](../experiment_protocol.md). It is fixed for the run, so it stays in the prompt cache.
2. **Tools.** The instrument's tools come from the service's `describe` (the text lives in `mhapi/tools.py`),
   marked `strict` so arguments always match their schema. The harness adds four:
   - `wait(seconds, until_bed_at_or_below?, until_bed_at_or_above?)`: lets time pass without spending turns;
     polls the bed, which also keeps the heater watchdog fed.
   - `note(text)`: a timestamped lab-notebook line.
   - `call_operator(message, label?, timeout_minutes?)`: asks the person at the bench to fix something only a
     person can (a halt, a camera that moved, a bubble or spill), beyond the instrument's own load and clean
     requests. The dashboard shows the message and reads it aloud; the person answers Done or Can't fix with an
     optional note, which comes back as the tool's result (`done`, `problem`, `message` if they wrote to the agent
     instead, or `timeout`, default 15 min, at most 30). Nothing polls the instrument meanwhile, so the heater
     watchdog still runs. On the twin, time runs at 1× during the call, as while the model thinks.
   - `python(code)`: analysis code on the run's recorded data (`data/readings.csv`, `temperatures.csv`,
     `events.csv`), in a separate process with no network access (so it can't reach the instrument) and
     writes limited to the run folder: `sandbox-exec` on macOS, bubblewrap on Linux (which also hides the rest
     of the host). Figures saved under `figures/` go back to the model and appear in the dashboard.
3. **Loop.** One append-only conversation per run. Each assistant turn may call several tools; they run one after
   another in the given order (one camera). Images from `get_image` / `get_raw_opacity` are attached to the
   results. After each batch the harness appends a `[clock]` line: instrument time, run time, and how long the
   last turn spent thinking and in tool calls. When the model ends its turn without a tool call, its text is the
   run report; a message from the dashboard continues the same conversation.
4. **API settings.** Streaming with adaptive thinking and `display: "updates"` (the model's short progress notes
   show up live), `effort` from the dashboard (medium by default), automatic prompt caching, and
   `fallbacks: "default"` so a classifier refusal retries on a fallback model server-side. Either beta is
   dropped automatically if the account doesn't accept it. API errors are retried with backoff.
5. **Time on the twin.** While the model is thinking (or the API is retrying, or `python` runs), the harness tells
   the twin, which then runs at 1×. Everything else (moves, waits, the simulated operator, heating) runs at the
   chosen speed. From the agent's side the run unfolds exactly as on the rig; only the wall-clock time of
   waits shrinks.

There is no autopilot: if the model or the API stalls, nothing is measured until it comes back. The
instrument's own safety layer still applies: the heater watchdog (heater off after 15 min without an
instrument call, whether or not a dashboard is open), the limits, and Stop.
Stop ends the agent at once (a streaming turn is dropped, a wait or a call to the operator ends; an instrument
call in progress finishes) and leaves the instrument as it is.

## The run record

Everything goes to `harness_runs/<run_id>/` (git-ignored):

| File | Contents |
|---|---|
| `trace.jsonl` | every event: turns, progress notes, tool calls and results, notes, figures, token usage, errors |
| `messages.json` | the conversation as sent to the API (images replaced by their size) |
| `run.json` | prompt, model, effort, status, token totals and estimated cost |
| `report.md` | the agent's final report |
| `data/*.csv` | readings, bed temperatures and events, as the `python` tool sees them |
| `analysis/step_NNN.py`, `figures/` | the agent's analysis scripts and plots |

The dashboard's run picker replays any recorded run from its trace.

## The dashboard

- **Printer camera:** the rig in 3D (three.js), moving with the twin's state: X toolhead, Y bed, Z gantry, wells
  filling and clouding. Drag to look around; the HUD shows instrument time, speed, position and the bed. On the
  rig: the A1's own camera.
- **Nozzle camera:** the Pi camera's view, rendered from the lens through an equidistant fisheye matched to the
  calibration (f ≈ 600 px, nadir at (690.5, 362)), with the room light off and the LED bar lit as during an
  experiment. "nadir" marks the straight-down pixel. On the rig: the Pi camera's live video, with the nadir from
  `calibration/nadir.json`.
- **Data from the wells:** the agent's readings per well (one color per island; the legend and end labels name
  them), the bed reading and target below, a crosshair tooltip across both, and "true curves" from the twin.
  "Agent figures" collects the plots the agent saves.
- **Agent steps:** prompt, effort and protocol options, "fresh rig" (start on a reset twin: empty wells, cold bed,
  not homed), Start / Stop / message; then every turn live: progress notes, tool calls with results and
  cell-image thumbnails, notes, figures, warnings, and the report.
- **Operator banner:** load and clean requests, with Done / Problem / Stop rig. On the twin a simulated person
  answers by default (untick "simulated operator" to answer yourself). Clear after a halt is only on the
  operator console page (`http://127.0.0.1:8743/operator`), as on the rig.
- **The agent's calls:** a second banner for `call_operator`, with the agent's message, a note field, Done and
  Can't fix (which needs a note).
- **Safety stop banner:** while the instrument is halted (or the heater watchdog fired), a red banner says what
  happened (the service's `safety_detail`), the rule behind it and since when, what to do, and links the operator
  console, where Clear is.
- **Sound (🔔 in the header):** when a person is needed, the page chimes and speaks: "Load well P3, A1.",
  "Clean all wells.", "The agent needs help." and its message, and on a halt "Safety stop." with what happened. Load,
  clean and help requests repeat every minute until answered; requests the simulated operator answers stay
  silent. Browsers play sound only after a click on the page, so the bell is outlined amber until then. The
  setting is per browser. On the rig, start mhapi-service with `--quiet` so the Mac doesn't announce the same
  requests itself.
- **Twin controls:** speed and fault injection (bubble, room light, camera bump, island shift, lost home,
  printer offline).

The page talks only to the harness: the twin's dashboard endpoints are passed through under `/rig/` (one shared
connection to the twin's state stream, however many pages watch).

## Instrument mode

With `--rig` pointing at mhapi-service, the page shows the real rig. The harness relays everything, so the page
still needs only the harness's address.

- **Cameras** ([feeds.py](feeds.py)). `/feeds/a1.mjpg` re-serves the A1's own camera as MJPEG: TLS on port 6000,
  as in `bambu/snapshot.py`, about one 1536 × 1080 frame every 2 s. The access code comes from Bambu Studio's
  config, the printer's serial number and last address from `mhapi/safety.toml`; when that address doesn't answer,
  the harness finds the printer by its serial number. `/feeds/pi.mjpg` relays the Pi camera (`--pi-camera`, default
  `http://raspberrypi.local:8080/stream.mjpg`, about 20 frames a second). `/feeds/a1.jpg` and `/feeds/pi.jpg` give
  the newest frame. Each camera has one upstream connection while anyone watches and none otherwise. Both are
  read-only: the harness sends the printer no MQTT and no G-code, so it can run alongside the service.
- **Header, operator banner, bed trace.** While a page is open, the harness polls the service once for all pages:
  `GET /operator/state` every second and `GET /operator/status` (get_status's body) every 2 s. Each poll goes to the
  pages as one event on `/rig/status/stream`. The chips show homed, ready and the safety state (hover for the
  reasons), the bed, and the instrument's clock (hover for the session); the printer camera's corner shows the
  operation under way, and the Pi camera's tag the well the camera is over after a read. The bed trace in the chart is get_status's `bed_c`. Done, Problem and Stop
  rig go to the service's console endpoints. Clear after a stop stays on the console page itself
  (`http://127.0.0.1:8742/operator`).
- **The heater watchdog.** The polls don't feed it. Both are reads for people watching, not API calls, so the
  heater watchdog (H4: heater off after 15 min without an API call) fires on a stalled agent whether or not a
  dashboard is open. Only the agent's own calls reset it (the `wait` tool's bed polls among them).
  `InstrumentWatchdog` in `tests/test_harness.py` checks this on the service's simulator.
- **The records.** The polls are in neither record: the agent's run record (`harness_runs/`) holds only the
  agent's own calls, and the service's `calls.jsonl` only API calls.
- **Browser connections.** Browsers open at most six connections to one server. A visible page holds three (the
  agent's events, the status stream and the Pi's video; the A1's frames load one at a time), a hidden one two (it
  lets go of the video). Two dashboard windows on screen in one browser would leave none for anything else.

## Public deployment

`python -m harness --public` is for a server anyone can open: one run at a time, steering only by the browser
that started it, and caps on cost per run, minutes per run, spending per day and runs per visitor
([limits.py](limits.py)). [deploy/](../deploy/README.md) puts the twin, the harness and the dashboard on a small
Google Cloud VM behind Caddy with HTTPS. It serves the twin only: pointed at anything else, `--public` refuses to
start, so the real rig's cameras and operator console never reach a public server.

## Cost

Each turn re-reads the cached conversation ($0.20 per million tokens) and writes the new tail; most of the cost
is output and thinking. A two-well test is roughly $0.5–1.5; the four-temperature Arrhenius run a few dollars.
The header shows the running total.
