"""The agent loop: one append-only conversation per run with claude-opus-5-5.

Design (see the brainstorm in this session and Agent_Harness_spec.md):
- The agent paces the experiment: it calls the instrument's tools turn by turn. Several readings in one
  turn run one after another, in the order given.
- The conversation is only ever appended to: the system prompt and the tool list are fixed for the run
  (prompt cache, and Opus 5.5 ties its thinking blocks to the exact history). The harness's per-turn
  [clock] line goes after the tool results.
- Streaming with adaptive thinking and `display: "updates"`, so the model's short progress notes reach
  the dashboard live; refusals fall back server-side (`fallbacks: "default"`).
- Against the twin, the harness tells it when the model is thinking, so simulated time runs at 1x then
  and the run unfolds as it would on the rig, only with waits shortened.
- `call_operator` puts the agent's request for a person on the dashboard (which chimes and reads it aloud)
  and waits for their answer.
- Stopping ends the agent at once: a streaming turn is dropped, a wait or a call to the operator ends, an
  instrument call in progress finishes. The instrument itself is not touched. A public deployment also stops runs at its limits
  (harness/limits.py).
"""
import json
import queue
import secrets
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import anthropic
import cv2

from . import MODEL
from .env import ROOT
from .prompts import system_prompt
from .record import RunRecord
from .sandbox import run_python
from .tools import IMAGE_TOOLS, image_block, rig_result, summarize, tool_definitions

PRICE = {"input": 4.0, "cache_write": 5.0, "cache_read": 0.20, "output": 20.0}  # $/MTok, claude-opus-5-5
BETA_UPDATES = "thinking-display-updates-2026-08-18"
BETA_FALLBACK = "server-side-fallback-2026-07-01"


class _Stopped(Exception):
    """Raised inside a streaming turn when the run is stopped."""


def fmt_time(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(s):
    s = int(round(s))
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else f"{s // 60} min {s % 60:02d} s"


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return None


class OperatorCall:
    """The agent's request for a person (call_operator), open until they answer on the dashboard."""

    def __init__(self, call_id, message, label):
        self.id, self.message, self.label = call_id, message, label
        self.t_requested = time.time()
        self.answer = self.note = None
        self.event = threading.Event()

    def view(self):
        return {"id": self.id, "message": self.message, "label": self.label, "t_requested": round(self.t_requested, 3)}


class AgentRun:
    def __init__(self, harness, prompt, effort="medium", attach_protocol=True):
        self.h = harness
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.prompt = prompt
        self.effort = effort
        self.attach_protocol = attach_protocol
        self.record = RunRecord(harness.runs_root, self.run_id)
        self.messages = []
        self.status = "starting"
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.token = secrets.token_urlsafe(16)  # lets the browser that started the run steer it (public mode)
        self.active_s, self.active_since = 0.0, None  # wall time the agent spent working (not awaiting messages)
        self.inbox = queue.Queue()
        self.operator_call = None    # the open OperatorCall, if any
        self.calls_made = 0
        self.call_lock = threading.Lock()
        self.turn = 0
        self.py_step = 0
        self.usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost": 0.0, "turns": 0}
        self.features = {"display_updates": True, "fallbacks": True}
        self.t0_rig = harness.rig.now()
        self.t0_wall = time.time()
        self.thread = threading.Thread(target=self._main, daemon=True, name=f"agent-{self.run_id}")

    # ------------------------------------------------------------ public
    def start(self):
        self.thread.start()
        return self

    def stop(self, reason=None):
        if reason and not self.stop_event.is_set():
            self.stop_reason = reason
            self.emit("warning", message=f"Stopping the agent: {reason}.")
        self.stop_event.set()

    def _set_status(self, status):
        now = time.time()
        if self.active_since is not None:
            self.active_s += now - self.active_since
        self.active_since = now if status == "running" else None
        self.status = status

    def _check_limits(self):
        lim = self.h.limits
        if not lim.public or self.stop_event.is_set():
            return
        active = self.active_s + (time.time() - self.active_since if self.active_since else 0.0)
        if self.usage["cost"] >= lim.run_usd:
            self.stop(f"the run reached the public demo's limit of ${lim.run_usd:.2f}")
        elif lim.day_left() <= 0:
            self.stop("the public demo's budget for today is used up")
        elif active >= lim.run_min * 60:
            self.stop(f"the run reached the public demo's limit of {lim.run_min:.0f} min")

    def send(self, text):
        if not self.thread.is_alive():
            return False
        self.inbox.put(text)
        self.emit("user_message", text=text)
        return True

    def answer_operator_call(self, call_id, answer, note=None):
        """The person's answer from the dashboard: done or problem, with an optional note. False if that request
        isn't open (answered already, or another one)."""
        c = self.operator_call
        if c is None or c.id != call_id or answer not in ("done", "problem"):
            return False
        with self.call_lock:
            if c.answer is not None:
                return False
            c.answer, c.note = answer, (str(note or "").strip()[:1000] or None)
        c.event.set()
        return True

    def summary(self):
        return {"run_id": self.run_id, "prompt": self.prompt, "model": MODEL, "effort": self.effort,
                "attach_protocol": self.attach_protocol, "rig": self.h.rig.url, "twin": self.h.rig.is_twin,
                "status": self.status, "stop_reason": self.stop_reason, "t0_rig": self.t0_rig, "t0_wall": self.t0_wall,
                "turns": self.turn,
                "operator_call": self.operator_call.view() if self.operator_call else None,
                "usage": self.usage, "features": self.features, "dir": rel(self.record.dir)}

    # ------------------------------------------------------------ events
    def emit(self, kind, trace=True, **kw):
        ev = self.h.bus.emit({"run_id": self.run_id, "kind": kind, "t_rig": round(self.h.rig.now(), 3), **kw})
        if trace:
            self.record.trace(ev)
        return ev

    def _save(self):
        self.record.write_json("run.json", self.summary())

    def _save_messages(self):
        def clean(obj):
            if hasattr(obj, "model_dump"):
                obj = obj.model_dump(mode="json", exclude_none=True)
            if isinstance(obj, dict):
                if obj.get("type") == "base64" and "data" in obj:
                    return {**obj, "data": f"<{len(obj['data'])} base64 chars>"}
                return {k: clean(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [clean(v) for v in obj]
            return obj
        self.record.write_json("messages.json", {"model": MODEL, "system": self.system, "tools": self.tools,
                                                 "messages": clean(self.messages)})

    # ------------------------------------------------------------ main loop
    def _main(self):
        try:
            self._set_status("running")
            self._save()
            self.emit("run_started", prompt=self.prompt, model=MODEL, effort=self.effort, rig=self.h.rig.url,
                      twin=self.h.rig.is_twin, dir=rel(self.record.dir))
            self._loop()
        except Exception as e:  # anything unexpected ends the run visibly
            self._set_status("failed")
            self.emit("error", message=f"{type(e).__name__}: {e}", detail=traceback.format_exc()[-3000:])
        finally:
            self.h.rig.thinking(False)
            self._save()
            self.emit("run_finished", status=self.status, usage=self.usage)
            self.record.close()

    def _loop(self):
        self.tools = tool_definitions(self.h.rig.describe())
        self.system = [{"type": "text", "text": system_prompt(self.attach_protocol),
                        "cache_control": {"type": "ephemeral"}}]
        first = f"[run {self.run_id}] Started at {fmt_time(self.h.rig.now())} (instrument clock).\n\n{self.prompt}"
        self.messages.append({"role": "user", "content": [{"type": "text", "text": first}]})
        while True:
            self._check_limits()
            if self.stop_event.is_set():
                self._set_status("stopped")
                return
            self.turn += 1
            t_think = self.h.rig.now()
            msg = self._model_turn()
            if msg is None or self.stop_event.is_set():
                self._set_status("stopped" if self.stop_event.is_set() else "failed")
                return
            think_s = self.h.rig.now() - t_think
            if msg.stop_reason == "refusal":
                cat = getattr(getattr(msg, "stop_details", None), "category", None)
                self.emit("error", message=f"The model declined to continue (refusal, category {cat}).")
                self._set_status("refused")
                return
            self.messages.append({"role": "assistant", "content": msg.content})
            self._emit_assistant(msg)
            tool_uses = [b for b in msg.content if b.type == "tool_use"]
            if not tool_uses:
                report = "\n\n".join(b.text for b in msg.content if b.type == "text").strip()
                (self.record.dir / "report.md").write_text(report + "\n")
                self._set_status("finished")
                self._save()
                self._save_messages()
                self.emit("finished", report=report, usage=self.usage)
                text = self._await_message()
                if text is None:
                    return
                self._set_status("running")
                self.messages.append({"role": "user", "content": [{"type": "text", "text": f"[message from the scientist] {text}"}]})
                continue
            t_tools = self.h.rig.now()
            results = [self._run_tool(tu) for tu in tool_uses]
            tools_s = self.h.rig.now() - t_tools
            extra = []
            while not self.inbox.empty():
                extra.append({"type": "text", "text": f"[message from the scientist] {self.inbox.get_nowait()}"})
            extra.append({"type": "text", "text": self._clock_line(think_s, tools_s)})
            self.messages.append({"role": "user", "content": results + extra})
            self._save()
            self._save_messages()

    def _await_message(self):
        while not self.stop_event.is_set():
            try:
                return self.inbox.get(timeout=0.5)
            except queue.Empty:
                pass
        return None

    def _clock_line(self, think_s, tools_s):
        t = self.h.rig.now()
        return (f"[clock] {fmt_time(t)} · run time {fmt_dur(t - self.t0_rig)} · your last turn: "
                f"{think_s:.0f} s thinking, {tools_s:.0f} s of tool calls")

    # ------------------------------------------------------------ the model
    def _params(self, max_tokens):
        p = dict(model=MODEL, max_tokens=max_tokens, system=self.system, tools=self.tools, messages=self.messages,
                 thinking={"type": "adaptive", "display": "updates" if self.features["display_updates"] else "summarized"},
                 output_config={"effort": self.effort}, cache_control={"type": "ephemeral"})
        betas = []
        if self.features["display_updates"]:
            betas.append(BETA_UPDATES)
        if self.features["fallbacks"]:
            betas.append(BETA_FALLBACK)
            p["fallbacks"] = "default"
        if betas:
            p["betas"] = betas
        return p

    def _model_turn(self, max_tokens=32000):
        self.h.rig.thinking(True)  # time on the twin runs at 1x while the model works (or is unreachable)
        try:
            return self._model_turn_attempts(max_tokens)
        finally:
            self.h.rig.thinking(False)

    def _model_turn_attempts(self, max_tokens):
        delay = 2.0
        for attempt in range(8):
            if self.stop_event.is_set():
                return None
            self.emit("turn_started", turn=self.turn, attempt=attempt + 1)
            try:
                with self.h.client.beta.messages.stream(**self._params(max_tokens)) as stream:
                    self._consume(stream)
                    msg = stream.get_final_message()
            except _Stopped:
                return None
            except anthropic.BadRequestError as e:
                text = str(e)
                if self.features["fallbacks"] and "fallback" in text.lower():
                    self.features["fallbacks"] = False
                    self.emit("warning", message="server-side fallbacks not available on this account; continuing without")
                    continue
                if self.features["display_updates"] and ("display" in text or BETA_UPDATES in text):
                    self.features["display_updates"] = False
                    self.emit("warning", message="thinking display 'updates' not available; using 'summarized'")
                    continue
                raise
            except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
                self.emit("api_retry", message=f"{type(e).__name__}: {e}", attempt=attempt + 1, wait_s=delay)
                if self.stop_event.wait(delay):
                    return None
                delay = min(delay * 2, 60.0)
                continue
            except anthropic.APIStatusError as e:
                if e.status_code in (408, 409, 429, 500, 502, 503, 504, 529):
                    self.emit("api_retry", message=f"HTTP {e.status_code}: {e.message}", attempt=attempt + 1, wait_s=delay)
                    if self.stop_event.wait(delay):
                        return None
                    delay = min(delay * 2, 60.0)
                    continue
                raise
            self._account(msg)
            if msg.stop_reason == "max_tokens" and max_tokens < 64000:
                self.emit("warning", message="the reply hit max_tokens; asking again with 64k")
                max_tokens = 64000
                continue
            return msg
        return None

    def _consume(self, stream):
        kinds = {}
        for ev in stream:
            if self.stop_event.is_set():
                raise _Stopped()
            et = getattr(ev, "type", "")
            if et == "content_block_start":
                b = ev.content_block
                kinds[ev.index] = b.type
                if b.type == "fallback":
                    self.emit("warning", message="the request fell back to another model for this turn")
            elif et == "content_block_delta":
                d = ev.delta
                if d.type == "thinking_delta" and getattr(d, "thinking", ""):
                    self.emit("progress_delta", trace=False, turn=self.turn, index=ev.index, text=d.thinking)
                elif d.type == "text_delta" and d.text:
                    self.emit("text_delta", trace=False, turn=self.turn, index=ev.index, text=d.text)

    def _emit_assistant(self, msg):
        blocks = []
        for b in msg.content:
            if b.type == "thinking" and getattr(b, "thinking", ""):
                blocks.append({"type": "progress", "text": b.thinking})
            elif b.type == "text" and b.text:
                blocks.append({"type": "text", "text": b.text})
        self.emit("assistant", turn=self.turn, blocks=blocks, stop_reason=msg.stop_reason, model=msg.model)

    def _account(self, msg):
        u = msg.usage
        inp = u.input_tokens or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        out = u.output_tokens or 0
        cost = (inp * PRICE["input"] + cw * PRICE["cache_write"] + cr * PRICE["cache_read"] + out * PRICE["output"]) / 1e6
        for k, v in (("input", inp), ("cache_write", cw), ("cache_read", cr), ("output", out)):
            self.usage[k] += v
        self.usage["cost"] = round(self.usage["cost"] + cost, 4)
        self.usage["turns"] += 1
        self.h.limits.add_cost(cost)
        self.emit("usage", turn=self.turn, input=inp, cache_write=cw, cache_read=cr, output=out,
                  cost=round(cost, 4), total=dict(self.usage))
        self._check_limits()

    # ------------------------------------------------------------ tools
    def _run_tool(self, tu):
        name, args = tu.name, dict(tu.input or {})
        self.emit("tool_call", id=tu.id, name=name, input=args, turn=self.turn)
        image = None
        figures = []
        try:
            if name == "wait":
                is_error, content, body = self._tool_wait(args)
                summary = f"waited {body['waited_s']:.0f} s ({body['ended']}) · bed {body.get('bed_c')} °C"
            elif name == "note":
                t = self.h.rig.now()
                self.record.event(round(t, 3), "note", "", "", args.get("text", ""))
                body = {"ok": True, "t": round(t, 3)}
                is_error, content, summary = False, [{"type": "text", "text": json.dumps(body)}], args.get("text", "")
                self.emit("note", text=args.get("text", ""))
            elif name == "call_operator":
                is_error, content, body = self._tool_call_operator(args)
                summary = body.get("message") if is_error else \
                    f"{body['answer']}{': ' + body['note'] if body.get('note') else ''} · {body['waited_s']:.0f} s"
            elif name == "python":
                is_error, content, body, figures = self._tool_python(args)
                summary = (args.get("purpose") or "analysis") + (" · failed" if is_error else "")
            else:
                is_error, content, body = rig_result(self.h.rig, name, args, self.record)
                summary = summarize(name, args, body)
                if name in IMAGE_TOOLS and not is_error:
                    image = rel(body.get("image", ""))
        except Exception as e:
            is_error = True
            body = {"error": "harness_error", "message": f"{type(e).__name__}: {e}"}
            content = [{"type": "text", "text": json.dumps(body)}]
            summary = body["message"]
        compact = {k: v for k, v in body.items() if k not in ("image_base64",)} if isinstance(body, dict) else {}
        self.emit("tool_result", id=tu.id, name=name, input=args, ok=not is_error, summary=summary, body=compact,
                  image=image, figures=figures, turn=self.turn)
        res = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
        if is_error:
            res["is_error"] = True
        return res

    def _tool_wait(self, a):
        rig = self.h.rig
        seconds = max(1.0, min(1800.0, float(a.get("seconds", 60))))
        below, above = a.get("until_bed_at_or_below"), a.get("until_bed_at_or_above")
        cond = below is not None or above is not None
        t0 = rig.now()
        last_poll, ended, b = -1e18, "time", {}
        while True:
            now = rig.now()
            left = seconds - (now - t0)
            if left <= 0:
                break
            self._check_limits()
            if self.stop_event.is_set():
                ended = "stopped"
                break
            if not self.inbox.empty():
                ended = "message"
                break
            if now - last_poll >= (5.0 if cond else 60.0):  # also keeps the heater watchdog fed
                status, b = rig.call("get_temperature")
                last_poll = now
                if "bed_c" in b:
                    self.record.temperature(b.get("t"), b.get("bed_c"), b.get("target_c"), b.get("age_s"), "wait")
                    if (below is not None and b["bed_c"] <= below) or (above is not None and b["bed_c"] >= above):
                        ended = "condition"
                        break
            rig.sleep(min(left, 2.0), self.stop_event)
        status, b = rig.call("get_temperature")
        if "bed_c" in b:
            self.record.temperature(b.get("t"), b.get("bed_c"), b.get("target_c"), b.get("age_s"), "wait")
        body = {"waited_s": round(rig.now() - t0, 1), "ended": ended, "bed_c": b.get("bed_c"),
                "target_c": b.get("target_c"), "t": b.get("t")}
        return False, [{"type": "text", "text": json.dumps(body)}], body

    def _tool_call_operator(self, a):
        message = str(a.get("message") or "").strip()
        if not message:
            body = {"error": "invalid_argument", "message": "message is empty: say what you need the person to do"}
            return True, [{"type": "text", "text": json.dumps(body)}], body
        label = str(a.get("label") or "").strip() or None
        try:
            timeout = max(1.0, min(30.0, float(a.get("timeout_minutes") or 15))) * 60
        except (TypeError, ValueError):
            timeout = 900.0
        self.calls_made += 1
        call = OperatorCall(f"{self.run_id}-{self.calls_made}", message, label)
        self.operator_call = call
        self.emit("operator_call", id=call.id, message=message, label=label, timeout_s=timeout)
        self.h.rig.thinking(True)  # a person answers in real time: the twin runs at 1x meanwhile, as for the model
        try:
            while True:
                self._check_limits()
                if call.event.wait(0.5):
                    break
                with self.call_lock:   # whatever ends the wait, settle it under the lock so a late answer is refused
                    if self.stop_event.is_set():
                        call.answer = "stopped"
                    elif not self.inbox.empty():
                        call.answer = "message"
                    elif time.time() - call.t_requested >= timeout:
                        call.answer = "timeout"
                if call.answer is not None:
                    break
        finally:
            self.h.rig.thinking(False)
            self.operator_call = None
        t = self.h.rig.now()
        body = {"answer": call.answer, "note": call.note, "waited_s": round(time.time() - call.t_requested, 1),
                "t": round(t, 3)}
        self.emit("operator_answer", id=call.id, answer=call.answer, note=call.note, waited_s=body["waited_s"])
        self.record.event(round(t, 3), "operator_call", label or "", call.answer,
                          message + (f" | note: {call.note}" if call.note else ""))
        return False, [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], body

    def _tool_python(self, a):
        self.py_step += 1
        self.h.rig.thinking(True)  # analysis runs in real time on the rig too
        try:
            ok, text, figs = run_python(a.get("code", ""), self.record.dir, self.py_step)
        finally:
            self.h.rig.thinking(False)
        content = [{"type": "text", "text": text}]
        shown = []
        for f in figs[:4]:
            img = cv2.imread(str(f))
            if img is not None and max(img.shape[:2]) > 1400:
                s = 1400 / max(img.shape[:2])
                (self.record.dir / ".views").mkdir(exist_ok=True)
                small = self.record.dir / ".views" / f.name
                cv2.imwrite(str(small), cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA))
                content.append(image_block(small))
            else:
                content.append(image_block(f))
            shown.append(rel(f))
            self.emit("figure", path=rel(f), name=f.name)
        body = {"ok": ok, "figures": shown, "output": text[:2000]}
        return (not ok), content, body, shown
