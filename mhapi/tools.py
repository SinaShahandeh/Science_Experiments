"""The agent's tools: names, the descriptions the agent reads, and their arguments.

This is the one place the agent-facing text lives (Model_Hardware_API_spec.md, "Tool descriptions").
The service serves it (`POST /v1/describe`) and the CLI prints it (`mhapi describe`,
`mhapi <command> --help`). Standard library only: the CLI imports it.
"""

# All 32 labels, in the order of the spec's tables. The service uses the labels in its calibration.
LABELS = ([f"C-{row}{col}" for row in "ABCD" for col in "1234"]
          + [f"P{i}-{row}{col}" for i in "1234" for row in "AB" for col in "12"])

# arg: the command's single argument, or None. queued: runs in the one-at-a-time queue (moves the
# printer or waits for a person). timeout: the CLI's default --timeout in seconds.
TOOLS = {
    "get_status": dict(arg=None, queued=False, timeout=60, description=(
        "Reports whether the instrument is ready, without moving anything. If `ready` is false, call "
        "`prepare`; if it stays false, a person has to fix something, so stop and report. `loaded_wells` "
        "lists the wells filled with egg white since the last clean, with the time each was filled.")),
    "prepare": dict(arg=None, queued=True, timeout=300, description=(
        "Gets the instrument ready. Call it at the beginning of each experiment run, before heating or "
        "loading wells. If the instrument has lost its home position, it re-homes, which takes about a "
        "minute and moves the bed quickly; that is harmless while the wells are empty. After a `not_homed` "
        "error with wells loaded, calling `prepare` re-homes anyway: liquid may slosh, and readings pause "
        "for about a minute.")),
    "set_temperature": dict(arg="temperature", queued=False, timeout=60, description=(
        "Sets the heated bed's target in °C (0 = off, at most 100) and returns at once. The bed takes tens "
        "of seconds to change and overshoots by about 2 °C. The liquid in the wells lags behind the bed.")),
    "get_temperature": dict(arg=None, queued=False, timeout=60, description=(
        "Returns the latest bed reading, its target, and how old the reading is in seconds (usually under "
        "5). This is the bed under the wells, not the liquid in them.")),
    "get_image": dict(arg="label", queued=True, timeout=60, description=(
        "Moves the camera over the well and returns a fresh top-down image of the cell: the 10 mm opening "
        "and 2 mm of the surrounding walls. The green square is the area `get_raw_opacity` measures. Takes "
        "about 1 s to a neighbouring well and up to about 6 s across the bed.")),
    "get_raw_opacity": dict(arg="label", queued=True, timeout=60, description=(
        "Same move and image as `get_image`. Returns `gray`, the median brightness (0–255) of the measured "
        "square after removing glare. An empty well reads about 19 and white paper about 232; clear egg "
        "white looks dark and gets brighter as it clouds. `wall_gray` tracks the lighting: if it changes "
        "for the same well, the light changed, not the sample. `pixels_used` out of `pixels_total` shows "
        "how much of the square survived the glare filter. Also returns `image`, the picture it measured.")),
    "load_well": dict(arg="label", queued=True, timeout=1800, description=(
        "Has the well filled with egg white and returns when it is done. `t_loaded` is the best available "
        "start time for that well. Fails if the well is already loaded, if the bed is above 60 °C, or if "
        "the instrument isn't homed (call `prepare` before loading any well).")),
    "clean_all_wells": dict(arg=None, queued=True, timeout=1800, description=(
        "Has every well emptied and cleaned, then returns. The bed must be below 40 °C: set the "
        "temperature to 0 and wait first.")),
    "list_wells": dict(arg=None, queued=False, timeout=60, description=(
        "Returns the labels of all 32 wells.")),
}

# Commands for people and the harness, not agent tools.
OTHER_COMMANDS = {
    "describe": dict(arg=None, queued=False, timeout=60, description=(
        "Prints the tool definitions for the harness: {\"tools\": [{\"name\", \"description\", "
        "\"input_schema\"}, ...]} in the Messages API tool format. Not an agent tool.")),
    "stop": dict(arg=None, queued=False, timeout=60, description=(
        "The safety stop, for people: switches the heater off and halts the instrument. A move already "
        "under way finishes; nothing new starts. Only the Clear button on the operator console ends the "
        "halt. Not an agent tool.")),
}

COMMANDS = {**TOOLS, **OTHER_COMMANDS}

ARG_SCHEMAS = {
    "label": {"type": "string", "description": "Well label, for example P3-A1."},
    "temperature": {"type": "number", "description": "Bed target in °C. 0 turns the heater off; at most 100."},
}


def input_schema(name, labels=LABELS):
    arg = COMMANDS[name]["arg"]
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if arg:
        prop = dict(ARG_SCHEMAS[arg])
        if arg == "label":
            prop["enum"] = list(labels)
        schema["properties"][arg] = prop
        schema["required"] = [arg]
    return schema


def tool_definition(name, labels=LABELS):
    return {"name": name, "description": COMMANDS[name]["description"], "input_schema": input_schema(name, labels)}


def tool_definitions(labels=LABELS):
    """The agent's tools in the Messages API format (`mhapi describe`)."""
    return [tool_definition(name, labels) for name in TOOLS]
