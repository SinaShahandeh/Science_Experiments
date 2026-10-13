"""`mhapi`: the thin command-line client of mhapi-service.

    mhapi <command> [argument] [--timeout SECONDS] [--inline-image] [--help]

Prints exactly one JSON object and a newline on stdout, for success and for errors; diagnostics go to
stderr. Exit status: 0 success, 1 an error from the service (or client_timeout), 2 bad usage
(invalid_argument), 3 the service could not be reached (service_unavailable).
"""
import json
import math
import sys
import time

from . import client
from .tools import COMMANDS, TOOLS, tool_definition

USAGE = "mhapi <command> [argument] [--timeout SECONDS] [--inline-image] [--help]"
IMAGE_COMMANDS = ("get_image", "get_raw_opacity")


class UsageError(Exception):
    pass


def emit(obj, code):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.flush()
    return code


def error(code, message, **fields):
    return {"error": code, "message": message, **fields, "t": round(time.time(), 3)}


def parse(argv):
    """Returns (command, args, timeout, help). Raises UsageError."""
    command, positional, timeout, want_help, inline = None, [], None, False, False
    it = iter(argv)
    for tok in it:
        if tok in ("--help", "-h"):
            want_help = True
        elif tok == "--inline-image":
            inline = True
        elif tok == "--timeout" or tok.startswith("--timeout="):
            val = tok.split("=", 1)[1] if "=" in tok else next(it, None)
            try:
                timeout = float(val)
            except (TypeError, ValueError):
                raise UsageError("--timeout needs a number of seconds") from None
            if not (math.isfinite(timeout) and timeout > 0):
                raise UsageError("--timeout must be a positive number of seconds")
        elif tok.startswith("--"):  # a single dash is not an option: "-5" is a (negative) temperature
            raise UsageError(f"unknown option {tok}")
        elif command is None:
            command = tok
        else:
            positional.append(tok)
    if command is None:
        if want_help:
            return None, None, None, True
        raise UsageError(f"no command given. Usage: {USAGE}")
    if command not in COMMANDS:
        raise UsageError(f"unknown command {command!r}; commands: {', '.join(COMMANDS)}")
    if want_help:
        return command, None, None, True
    spec = COMMANDS[command]
    arg = spec["arg"]
    if arg is None:
        if positional:
            raise UsageError(f"{command} takes no argument")
        args = {}
    else:
        if len(positional) != 1:
            raise UsageError(f"{command} takes one argument: the {arg}, for example "
                             f"`mhapi {command} {'75' if arg == 'temperature' else 'P3-A1'}`")
        val = positional[0]
        if arg == "temperature":
            try:
                num = float(val)
            except ValueError:
                raise UsageError(f"the temperature must be a number of °C, not {val!r}") from None
            if not math.isfinite(num):
                raise UsageError(f"the temperature must be a finite number of °C, not {val!r}")
            val = int(num) if num.is_integer() else num
        elif not val.strip():
            raise UsageError("the well label is empty")
        args = {arg: val}
    if inline and command in IMAGE_COMMANDS:
        args["inline_image"] = True
    return command, args, timeout if timeout is not None else float(spec["timeout"]), False


def show_help(command):
    if command is None:
        return emit({"usage": USAGE, "commands": {name: spec["description"] for name, spec in COMMANDS.items()},
                     "options": {"--timeout": "seconds to wait for the result (default: 30 min for load_well "
                                              "and clean_all_wells, 5 min for prepare, 60 s otherwise)",
                                 "--inline-image": "also return the PNG as base64 in image_base64",
                                 "--help": "print the command's agent-facing description"}}, 0)
    if command not in TOOLS:  # describe, stop: not agent tools
        return emit({"name": command, "description": COMMANDS[command]["description"]}, 0)
    try:  # the service holds the text; tools.py is the same source if the service is down
        status, body = client.call("describe", {}, timeout=10)
        for tool in body.get("tools", []):
            if tool.get("name") == command:
                return emit(tool, 0)
    except (client.ServiceUnavailable, client.ClientTimeout):
        print("mhapi: service unreachable; printing the local copy of the description", file=sys.stderr)
    return emit(tool_definition(command), 0)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        command, args, timeout, want_help = parse(argv)
    except UsageError as e:
        return emit(error("invalid_argument", str(e), usage=USAGE), 2)
    if want_help:
        return show_help(command)
    try:
        status, body = client.call(command, args, timeout=timeout)
    except client.ClientTimeout:
        return emit(error("client_timeout", f"stopped waiting after {timeout:g} s; the operation goes on in "
                                            "the service. Check get_status().last_operation for its result.",
                          command=command), 1)
    except client.ServiceUnavailable as e:
        return emit(error("service_unavailable", str(e)), 3)
    if "error" not in body:
        return emit(body, 0)
    return emit(body, 3 if body["error"] == "service_unavailable" else 1)  # the service is shutting down


if __name__ == "__main__":
    sys.exit(main())
