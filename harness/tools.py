"""The agent's tools: the instrument's own (from the service's `describe`) plus wait, note, call_operator and python.

Instrument tools are passed through unchanged; the service holds their agent-facing text. The harness only
adds `strict: true` so the model's arguments always match the schema (forced tool choice isn't available on
claude-opus-5-5), attaches each returned well image to the tool result, and records the data.
"""
import base64
import json
from pathlib import Path

from mhapi import client

HARNESS_TOOLS = [
    {
        "name": "wait",
        "description": (
            "Lets time pass without spending turns. Returns after `seconds` of instrument time (at most 1800), "
            "or earlier when the bed reading reaches `until_bed_at_or_below` or `until_bed_at_or_above` (checked "
            "every few seconds), or when the scientist sends a message. Returns how long it waited, why it "
            "stopped, and the latest bed reading. Use it while the bed heats or cools, during holds, or between "
            "sweeps when the wells change slowly."),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Longest wait, in seconds (1-1800)."},
                "until_bed_at_or_below": {"type": "number", "description": "Stop early once the bed reads at or below this, °C."},
                "until_bed_at_or_above": {"type": "number", "description": "Stop early once the bed reads at or above this, °C."},
                "reason": {"type": "string", "description": "What you are waiting for, shown to the scientist."},
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "note",
        "description": (
            "Writes a line in the run's lab notebook, stamped with the instrument time: decisions, the time of a "
            "temperature jump (t = 0), anomalies, observations. The scientist sees notes live."),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "call_operator",
        "description": (
            "Calls the person at the instrument for help with something only a person can do or fix, other than "
            "loading and cleaning wells (use load_well and clean_all_wells for those). For example: the instrument "
            "is halted or reports camera_moved, get_status stays not ready after prepare, a well image shows a "
            "bubble, spill, debris or no liquid, or the printer seems stuck. Your message is shown on the dashboard "
            "and read aloud at the bench: say briefly what you saw and exactly what you need them to do or check. "
            "Returns when they answer: `done` (they did it; confirm with get_status or a new reading before relying "
            "on it), `problem` (they couldn't; their note says why), `message` (they wrote to you instead; their "
            "message follows the tool results), or `timeout` after `timeout_minutes`. They fix things; the "
            "experiment's decisions stay yours."),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What happened and what you need the person to do, in one or two sentences."},
                "label": {"type": "string", "description": "The well concerned, if any (e.g. P3-A1)."},
                "timeout_minutes": {"type": "number", "description": "Longest wait for an answer, 1-30 minutes (default 15)."},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "python",
        "description": (
            "Runs Python 3 analysis code on this run's recorded data, in a sandbox with no network or instrument "
            "access; each call is a fresh process with a 2 min limit. The working directory holds:\n"
            "- data/readings.csv: one row per get_raw_opacity: t, label, gray, wall_gray, pixels_used, "
            "pixels_total, saturated_px, reflection_px, bed_c, target_c, bed_age_s, image\n"
            "- data/temperatures.csv: every bed reading seen: t, bed_c, target_c, age_s, source\n"
            "- data/events.csv: t, event, label, value, detail. Events: set_temperature (value = target °C), "
            "load_well (t = t_loaded; detail = t_requested), clean_all_wells, prepare, note (detail = text), "
            "operator_call (value = the answer; detail = your message and their note), error.\n"
            "Times are Unix seconds on the instrument clock. numpy, scipy, pandas and matplotlib (Agg) are "
            "available. Print what you need. Save plots as PNG under figures/; they are shown to you and the "
            "scientist. Files you write in the working directory persist between calls."),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python program to run."},
                "purpose": {"type": "string", "description": "One line on what this analysis does."},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
]

HARNESS_TOOL_NAMES = {t["name"] for t in HARNESS_TOOLS}
IMAGE_TOOLS = ("get_image", "get_raw_opacity")


def tool_definitions(rig_tools):
    """Instrument tools with strict schemas, then the harness tools; the order is fixed for the prompt cache."""
    tools = [{**t, "strict": True} for t in rig_tools]
    return tools + [dict(t) for t in HARNESS_TOOLS]


def image_block(path):
    data = Path(path).read_bytes()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.standard_b64encode(data).decode()}}


def rig_result(rig, name, args, record):
    """Call an instrument tool. Returns (is_error, content blocks, body)."""
    try:
        status, body = rig.call(name, args)
    except client.ClientTimeout as e:
        body = {"error": "client_timeout", "message": f"{e}. The operation goes on in the service; check "
                                                       "get_status().last_operation for its result."}
        return True, [{"type": "text", "text": json.dumps(body)}], body
    except client.ServiceUnavailable as e:
        body = {"error": "service_unavailable", "message": str(e)}
        return True, [{"type": "text", "text": json.dumps(body)}], body
    body.pop("image_base64", None)
    is_error = "error" in body
    _record(record, name, args, body)
    content = [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]
    if not is_error and name in IMAGE_TOOLS and body.get("image") and Path(body["image"]).is_file():
        content.append(image_block(body["image"]))
    return is_error, content, body


def _record(record, name, args, body):
    t = body.get("t")
    if "error" in body:
        record.event(t, "error", args.get("label", ""), body.get("error"), f"{name}: {body.get('message', '')}")
        return
    if name == "get_raw_opacity":
        record.reading(body)
    elif name == "get_temperature":
        record.temperature(t, body.get("bed_c"), body.get("target_c"), body.get("age_s"), "get_temperature")
    elif name in ("get_status", "prepare"):
        record.temperature(t, body.get("bed_c"), body.get("target_c"), body.get("bed_age_s"), name)
        if name == "prepare":
            record.event(t, "prepare", "", "", json.dumps({"homed": body.get("homed"), "ready": body.get("ready")}))
    elif name == "set_temperature":
        record.event(t, "set_temperature", "", body.get("target_c"), "")
    elif name == "load_well":
        record.event(body.get("t_loaded"), "load_well", body.get("label"), "", body.get("t_requested"))
    elif name == "clean_all_wells":
        record.event(body.get("t_done"), "clean_all_wells", "", "", json.dumps(body.get("position_check")))
    elif name == "get_image":
        record.temperature(t, body.get("bed_c"), body.get("target_c"), body.get("bed_age_s"), "get_image")


def summarize(name, args, body):
    """One line for the dashboard's step list."""
    if "error" in body:
        return f"{body['error']}: {body.get('message', '')}"
    if name == "get_raw_opacity":
        return f"gray {body['gray']:.1f} · wall {body.get('wall_gray', 0):.0f} · bed {body['bed_c']:.1f} °C"
    if name == "get_image":
        return f"image · bed {body['bed_c']:.1f} °C"
    if name == "get_temperature":
        return f"bed {body['bed_c']:.1f} °C → {body['target_c']:g} °C ({body['age_s']} s old)"
    if name == "set_temperature":
        return f"target {body['target_c']:g} °C"
    if name == "load_well":
        return f"loaded {body['label']} (operator took {body['t_loaded'] - body['t_requested']:.0f} s)"
    if name == "clean_all_wells":
        return f"cleaned · position check {body.get('position_check')}"
    if name in ("get_status", "prepare"):
        return (f"ready {body.get('ready')} · homed {body.get('homed')} · bed {body.get('bed_c', 0):.1f} °C · "
                f"loaded {len(body.get('loaded_wells', []))}")
    if name == "list_wells":
        return f"{len(body.get('labels', []))} wells"
    return json.dumps(body)[:160]
