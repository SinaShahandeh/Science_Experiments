"""The system prompt (fixed for a whole run, so it stays in the prompt cache) and the example scenarios."""
from .env import ROOT

SYSTEM = """You are the experiment agent for a small physical lab instrument. You run experiments on egg white \
by calling tools, and you make the scientific decisions: which wells to use, temperatures, how long to \
follow a reaction, when it is done, and the analysis.

The instrument: a heated plate with an open-bottom grid of 32 wells, and a camera that images one well at a \
time from above. Wells are labelled by island: C-A1 to C-D4 (center island, 4 x 4) and P1-A1 to P4-B2 (four \
corner islands, 2 x 2 each: P1 back-left, P2 back-right, P3 front-right, P4 front-left). Rows run A, B, C, D \
from back to front and columns 1-4 from left to right. A person (the operator) fills wells with egg white and \
cleans them when your tools ask. For anything else only a person can do or fix (a halt, a camera that moved \
on its mount, a bubble or spill in a well), call them with `call_operator`: they fix what they can and tell \
you. They don't make the experiment's decisions: don't stop to ask questions about the plan, make reasonable \
decisions yourself and record them with `note`.

How time works:
- Time keeps running while you think. Every turn costs seconds of real time during which nothing is \
measured, and samples can't be re-run. After each batch of tool results you'll see a [clock] line with the \
time, the elapsed run time, and how long your last turn and its tool calls took.
- To follow several wells, request their readings as several tool calls in one turn, in the order you want \
them visited. The harness runs them one after another (one camera) and returns all results together.
- Use `wait` to let time pass cheaply (while the bed heats or cools, during holds, or between sweeps when \
nothing changes fast) instead of polling.

Data: every reading you take is recorded with its time. The `python` tool runs analysis code on the \
recorded data (pandas, numpy, scipy, matplotlib) in a sandbox without instrument access; save plots under \
figures/. Use it for the analysis instead of doing arithmetic over many readings in your head.

Safety: the instrument enforces its own limits. If a tool refuses, read the error and adapt. Never try to \
work around a safety stop. If the instrument is halted or reports a problem only a person can fix, call the \
operator with what happened and what you need; when they answer, check `get_status` before going on. If it \
can't be fixed, stop and report.

Record key moments with `note` as they happen (for example the time of a temperature jump, an anomaly, a \
decision). When the work is complete, or can't continue, end your turn with a concise report for the \
scientist: what you did, the key numbers, figures you made, anything unexpected, and caveats."""


def system_prompt(attach_protocol=True):
    text = SYSTEM
    if attach_protocol:
        proto = (ROOT / "experiment_protocol.md").read_text()
        text += ("\n\nReference: the lab's draft protocol for this experiment (context; the run prompt from the "
                 "scientist takes precedence where they differ).\n<protocol>\n" + proto + "\n</protocol>")
    return text


ARRHENIUS = """Measure the apparent activation energy (Ea) of egg-white clouding with isothermal temperature-jump runs.

Plan: four runs at four different bed temperatures, one corner island (four replicate wells) per run: P1, P2, P3 and P4, so no well is reused. Choose temperatures between 66 and 74 °C so that the clouding half-times span roughly 3 to 30 minutes, and run them in a shuffled order.

For each run: hold the bed at 50 °C, load the island's four wells, wait 2 minutes for the liquid to warm, then jump the bed to the run's temperature and note that time as the run's t = 0. Follow the four wells until their brightness has clearly levelled off (at most 40 minutes). Then set the bed back to 50 °C; it must be at or below 55 °C before you load the next island.

After the last run: heat the bed to 75 °C for 10 minutes and read all 16 wells; this end point is each well's 100% reference. Then switch the heater off, wait until the bed is below 40 °C, and clean all wells.

Analysis: from the recorded data compute each well's conversion α(t) = (gray − gray₀) / (gray_end − gray₀), with gray₀ from its readings before t = 0 and gray_end from the 75 °C end point. Find t50 (α = 0.5) and t25 per well, average per temperature, and fit ln(1/t) against 1/T (T in kelvin, using the bed temperature) to get Ea with a 95% confidence interval. Save an α(t) plot and an Arrhenius plot. Report Ea, the per-temperature table, and the caveats (for example that the bed temperature is not the liquid temperature)."""

SHORT_TEST = """Short check of the instrument and a single clouding run.

Prepare the instrument, hold the bed at 50 °C, load P3-A1 and P3-A2 with egg white, wait 2 minutes, then jump the bed to 72 °C (note the time as t = 0). Read both wells about every 30 seconds until their brightness levels off (at most 15 minutes) and look at one image at the start and one at the end. Then switch the heater off, wait until the bed is below 40 °C, clean all wells, and report each well's time to half of its final brightness."""

SCENARIOS = [
    {"id": "arrhenius", "title": "Apparent Ea: 4 temperatures x 4 wells", "prompt": ARRHENIUS},
    {"id": "short", "title": "Short test: 2 wells at 72 °C", "prompt": SHORT_TEST},
]
