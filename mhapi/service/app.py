"""HTTP interface of mhapi-service: POST /v1/<command> (the API), /operator (the console and a passive status
read), /sim (simulator)."""
import asyncio
import json
import logging
import math
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from ..tools import COMMANDS
from .errors import ApiError
from .rig import QUEUED

log = logging.getLogger("mhapi.app")
CONSOLE_HTML = Path(__file__).with_name("console.html")


def _clean(x):
    """JSON-safe: no NaN or infinity."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def json_response(body, status=200):
    return Response(json.dumps(_clean(body), ensure_ascii=False), status_code=status, media_type="application/json")


def validate(command, body, rig):
    """Check the request body; returns the arguments. Raises invalid_argument / unknown_label."""
    spec = COMMANDS[command]
    allowed = {spec["arg"]} if spec["arg"] else set()
    if command in ("get_image", "get_raw_opacity"):
        allowed.add("inline_image")
    extra = set(body) - allowed
    if extra:
        raise ApiError("invalid_argument", f"{command} does not take {', '.join(sorted(extra))}")
    args = {}
    arg = spec["arg"]
    if arg:
        if arg not in body:
            raise ApiError("invalid_argument", f"{command} needs the argument {arg!r}")
        val = body[arg]
        if arg == "label":
            if not isinstance(val, str) or not val.strip():
                raise ApiError("invalid_argument", "the label must be a well label such as P3-A1")
            if val not in rig.cal.wells:
                raise ApiError("unknown_label", f"{val!r} is not a well; list_wells() gives the 32 labels", label=val)
        elif arg == "temperature":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ApiError("invalid_argument", "the temperature must be a number of °C")
        args[arg] = val
    if "inline_image" in body:
        if not isinstance(body["inline_image"], bool):
            raise ApiError("invalid_argument", "inline_image must be true or false")
        args["inline_image"] = body["inline_image"]
    return args


def create_app(rig, sim_control=None):
    app = FastAPI(title="mhapi-service", docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_hex(16)  # the console's Clear button; never handed to the CLI
    immediate = {
        "get_status": lambda a: rig.get_status(),
        "get_temperature": lambda a: rig.get_temperature(),
        "set_temperature": lambda a: rig.set_temperature(a["temperature"]),
        "list_wells": lambda a: rig.list_wells(),
        "describe": lambda a: rig.describe(),
        "stop": lambda a: rig.stop("mhapi stop"),
    }

    def log_call(t_req, command, args, body, status, note=None):
        entry = {"t_request": round(t_req, 3), "t_response": round(time.time(), 3), "command": command,
                 "args": args, "status": status,
                 "response": {k: v for k, v in (body or {}).items() if k != "image_base64"}}
        if note:
            entry["note"] = note
        try:
            rig.session.log_call(_clean(entry))
        except OSError as e:
            log.warning("call log failed: %s", e)

    @app.post("/v1/{command}")
    async def v1(command: str, request: Request):
        t_req = time.time()
        rig.safety.on_api_call()  # H4: any call resets the heater watchdog
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            err = ApiError("invalid_argument", "the request body must be a JSON object")
            log_call(t_req, command, None, err.body(), 400)
            return json_response(err.body(), 400)
        if command not in COMMANDS:
            err = ApiError("invalid_argument", f"unknown command {command!r}")
            log_call(t_req, command, body, err.body(), 404)
            return json_response(err.body(), 404)
        try:
            args = validate(command, body, rig)
            if command not in QUEUED:
                result = await run_in_threadpool(immediate[command], args)
                log_call(t_req, command, args, result, 200)
                return json_response(result)
            if rig.safety.state == "halted":
                raise rig.safety.halted_error()
        except ApiError as e:
            log_call(t_req, command, body, e.body(), e.http_status)
            return json_response(e.body(), e.http_status)
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", command)
            out = {"error": "internal_error", "message": f"{type(e).__name__}: {e}", "t": round(time.time(), 3)}
            log_call(t_req, command, body, out, 500)
            return json_response(out, 500)
        # queued: one at a time, in arrival order; dropped if the caller leaves before it starts
        op = rig.submit(command, args)
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        op.on_done.append(lambda: loop.call_soon_threadsafe(done.set))
        rig.ops.submit(op)
        while not op.finished.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            if op.state == "queued" and await request.is_disconnected():
                if rig.ops.drop(op):
                    log_call(t_req, command, args, None, 499, note="caller went away before it started; dropped")
                    return Response(status_code=499)
        log_call(t_req, command, args, op.result, op.http_status)
        return json_response(op.result, op.http_status or 500)

    # -- operator console ------------------------------------------------------------------------------
    @app.get("/operator", response_class=HTMLResponse)
    async def console():
        return HTMLResponse(CONSOLE_HTML.read_text().replace("__CLEAR_TOKEN__", token))

    @app.get("/operator/state")
    async def console_state():
        return json_response(await run_in_threadpool(rig.console_state))

    @app.get("/operator/status")
    async def console_status():
        """get_status's body for people watching (the harness dashboard). Not an API call: it doesn't reset the
        heater watchdog (H4) and isn't logged in calls.jsonl, so a watched dashboard can't keep the heater on."""
        try:
            return json_response(await run_in_threadpool(rig.get_status))
        except ApiError as e:
            return json_response(e.body(), e.http_status)

    @app.post("/operator/answer")
    async def console_answer(request: Request):
        try:
            b = await request.json()
            ok = rig.operator.answer(int(b.get("id")), str(b.get("answer")), (b.get("note") or "").strip() or None)
        except (ValueError, TypeError, AttributeError):
            ok = False
        return json_response({"ok": ok}, 200 if ok else 409)

    @app.post("/operator/stop")
    async def console_stop():
        return json_response(await run_in_threadpool(rig.stop, "the operator console"))

    @app.post("/operator/clear")
    async def console_clear(request: Request):
        try:
            b = await request.json()
        except ValueError:
            b = {}
        if not isinstance(b, dict) or not secrets.compare_digest(str(b.get("token", "")), token):
            return json_response({"ok": False, "message": "Clear works only from the operator console page"}, 403)
        await run_in_threadpool(rig.safety.clear, "operator console")
        return json_response({"ok": True})

    # -- simulator faults (sim mode only) ----------------------------------------------------------------
    if sim_control is not None:
        @app.post("/sim/{action}")
        async def sim(action: str, request: Request):
            raw = await request.body()
            b = json.loads(raw) if raw.strip() else {}
            try:
                return json_response({"ok": True, **(sim_control(action, b) or {})})
            except (KeyError, ValueError, TypeError) as e:
                return json_response({"ok": False, "message": str(e)}, 400)

    return app
