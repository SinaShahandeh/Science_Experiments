// Dashboard glue: the harness's event stream (agent steps), the twin's state stream (3D view, operator, chips),
// the well-data charts, and the controls. Everything shown comes from those two streams or the run's trace.
import { RigScene } from "./scene.js";
import { ChartGroup, LineChart } from "./chart.js";

const $ = (id) => document.getElementById(id);
const ISLAND_VAR = { P1: "--s1", P2: "--s2", P3: "--s3", P4: "--s4", C: "--s5" };
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const islandOf = (label) => label.split("-")[0];
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };

// ---------------------------------------------------------------------------------------------- theme
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
applyTheme(store.get("theme"));
$("theme").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const next = dark ? "light" : "dark";
  applyTheme(next); store.set("theme", next); charts.redraw(); renderLegend();
};

// ---------------------------------------------------------------------------------------------- sound alerts
// When a person is needed, this page chimes and says what to do: a load or clean request from the instrument
// ("Load well P3, A1."), the agent's call_operator, a safety halt. Requests repeat every minute until answered.
// On the rig, mhapi-service runs --quiet, so the sound comes from here. Browsers play sound only after a click or
// key press on the page: until then the bell is outlined amber, and the banners say so.
const alerts = (() => {
  let on = store.get("sound") !== "off";
  let ctx = null;
  const open = new Map();   // group -> {key, timer}
  const btn = $("sound");
  const allowed = () => !navigator.userActivation || navigator.userActivation.hasBeenActive;
  function paint() {
    btn.textContent = on ? "🔔" : "🔕";
    btn.classList.toggle("muted", !on);
    btn.classList.toggle("locked", on && !allowed());
    btn.title = !on ? "Sound alerts off (click to turn on)"
      : allowed() ? "Sound alerts on: a chime and a spoken line when a person is needed (click to turn off)"
      : "Sound alerts on, but the browser plays sound only after a click on this page";
  }
  function unlock() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
    } catch { /* no Web Audio */ }
    paint();
  }
  function chime(urgent) {   // two rising notes; four alternating ones for a halt. Returns its length in ms.
    if (!ctx) return 0;
    const notes = urgent ? [660, 520, 660, 520] : [880, 1320];
    const t0 = ctx.currentTime + 0.02;
    notes.forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain(), t = t0 + i * 0.22;
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.22);
    });
    return notes.length * 220 + 150;
  }
  function play(spoken, urgent) {
    console.info(`[alert] ${spoken}`);
    if (!on) return;
    const ms = chime(urgent);
    if (!("speechSynthesis" in window)) return;
    setTimeout(() => {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(spoken); u.rate = 0.95;
      speechSynthesis.speak(u);
    }, ms);
  }
  // One alert per group ("op", "help", "halt"): a new key replaces the group's alert, null ends it.
  function set(group, key, spoken, { repeat_s = 60, urgent = false } = {}) {
    const cur = open.get(group);
    if (cur && cur.key === key) return;
    if (cur) { clearInterval(cur.timer); open.delete(group); }
    if (key == null) return;
    play(spoken, urgent);
    open.set(group, { key, timer: repeat_s ? setInterval(() => play(spoken, urgent), repeat_s * 1000) : null });
  }
  btn.onclick = () => { on = !on; store.set("sound", on ? "on" : "off"); unlock(); if (on) play("Sound on.", false); paint(); };
  for (const ev of ["pointerdown", "keydown"]) addEventListener(ev, unlock, { capture: true });
  paint();
  return { set, hint: () => (on && !allowed() ? "(click anywhere on this page to let it play sound)" : "") };
})();
const sayLabel = (label) => String(label || "").replace("-", ", ");   // "P3-A1" -> "P3, A1"
function spokenRequest(op) {
  if (String(op.kind).startsWith("load") && op.label) return `Load well ${sayLabel(op.label)}.`;
  if (String(op.kind).startsWith("clean")) return "Clean all wells.";
  return op.text;
}
// A safety stop, in words: what happened (the service's detail), the rule behind it, since when, what to do.
// mhapi-service sends safety_reason (a code) and safety_detail; the twin sends "code: detail" as the reason.
const REASON_WORDS = {
  foreign_control: "something other than the service commanded the printer",
  over_temperature: "the bed got too hot", heater_stuck_on: "the bed kept heating with the heater off",
  lost_contact: "the printer stopped reporting while the bed was heating", operator_stop: "a person pressed Stop",
  camera_moved: "the camera moved on its mount", watchdog: "no instrument call for 15 minutes while heating",
};
const sentence = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/[.\s]*$/, ".") : "");
function haltBanner(state, reason, detail, t) {
  const on = !!state && state !== "ok";
  $("halt").hidden = !on;
  if (!on) { alerts.set("halt", null); return; }
  let code = reason || state;
  if (!detail && code.includes(": ")) [code, detail] = [code.slice(0, code.indexOf(": ")), code.slice(code.indexOf(": ") + 2)];
  const when = t ? new Date(t * 1000).toLocaleTimeString([], { hour12: false }) : "";
  const why = `${code}${REASON_WORDS[code] ? ` (${REASON_WORDS[code]})` : ""}${when ? ` · since ${when}` : ""}`;
  const timeout = state === "heater_timeout";
  const todo = timeout
    ? "The heater was switched off. The agent can heat again with set_temperature; nothing else is blocked."
    : "The heater is off and nothing moves. Check the instrument, then press Clear on the operator console; after that, prepare re-homes.";
  $("halt-label").textContent = timeout ? "Heater watchdog" : "Safety stop";
  $("halt-text").textContent = sentence(detail) || sentence(REASON_WORDS[code] || code.replace(/_/g, " "));
  $("halt-sub").textContent = `${why}. ${todo}`;
  $("halt-console").href = `${cfg.rig_url}/operator`;
  $("halt-console").hidden = !!cfg.public || timeout;
  const said = (detail || REASON_WORDS[code] || code.replace(/_/g, " ")).replace(/°C/g, " degrees");
  alerts.set("halt", `${state}:${t ?? ""}`, timeout
    ? `Heater switched off by the watchdog: ${said}.`
    : `Safety stop. ${said}. The heater is off. Check the instrument, then press Clear on the operator console.`,
    { repeat_s: 0, urgent: true });
}

// ---------------------------------------------------------------------------------------------- config
let cfg = await (await fetch("/api/config")).json();
const RIG = cfg.rig_base || cfg.rig_url;   // the harness passes the twin's endpoints through under /rig
const rigHost = cfg.rig_url.replace("http://", "");
$("chip-rig").textContent = cfg.twin ? (cfg.public ? "digital twin · simulation" : `digital twin · ${rigHost}`) : `instrument · ${rigHost}`;
for (const el of document.querySelectorAll(".sim-only")) el.hidden = !cfg.twin;
for (const s of cfg.scenarios) {
  const o = document.createElement("option"); o.value = s.id; o.textContent = s.title; $("scenario").appendChild(o);
}
$("prompt").value = cfg.scenarios[0].prompt;
$("scenario").onchange = () => { $("prompt").value = cfg.scenarios.find((s) => s.id === $("scenario").value).prompt; };

// ---------------------------------------------------------------------------------------------- charts
const charts = new ChartGroup($("tooltip"));
const wellsChart = charts.add(new LineChart($("chart-wells"), { yMin: 0, yMax: 255, yDecimals: 0, endLabels: true }));
const bedChart = charts.add(new LineChart($("chart-bed"), { yAuto: true, yDecimals: 0, yUnit: " °C" }));
charts.fmtX = (x) => `${x.toFixed(2)} min into the run`;

const data = { t0: null, readings: new Map(), truth: new Map(), temps: [], targets: [], live: [] };
function resetData(t0) {
  data.t0 = t0; data.readings.clear(); data.truth.clear(); data.temps = []; data.targets = []; data.live = [];
  charts.setXMax(1); scheduleCharts();
}
const xOf = (t) => (t - data.t0) / 60;
let chartsPending = false;
function scheduleCharts() { if (!chartsPending) { chartsPending = true; setTimeout(updateCharts, 250); } }
function updateCharts() {
  chartsPending = false;
  if (data.t0 == null) return;
  let xMax = 1;
  const series = [];
  const labels = [...data.readings.keys()].sort((a, b) => cfg_labels.indexOf(a) - cfg_labels.indexOf(b));
  const showTruth = $("truth").checked;
  for (const lab of labels) {
    const pts = data.readings.get(lab);
    if (pts.length) xMax = Math.max(xMax, pts[pts.length - 1][0]);
    const color = css(ISLAND_VAR[islandOf(lab)] || "--s1");
    series.push({ label: lab, group: islandOf(lab), color, points: pts, endDot: true, order: cfg_labels.indexOf(lab) });
    if (showTruth && data.truth.has(lab)) series.push({ label: `${lab} true`, group: islandOf(lab), color, points: data.truth.get(lab), dashed: true, alpha: 0.55, width: 1.5, order: 100 + cfg_labels.indexOf(lab), noSnap: true });
  }
  wellsChart.setSeries(series);
  const temps = [...data.temps, ...data.live].sort((a, b) => a[0] - b[0]);
  if (temps.length) xMax = Math.max(xMax, temps[temps.length - 1][0]);
  bedChart.setSeries([
    { label: "target", color: css("--muted"), points: data.targets, step: true, width: 1.5, extendTo: xMax, order: 1, noSnap: true },
    { label: "bed reading", color: css("--ink-2"), points: temps, width: 2, order: 0, noSnap: data.live.length > 0 },
  ]);
  charts.setXMax(xMax * 1.04);
  charts.redraw();
  renderLegend();
}
function renderLegend() {
  const islands = [...new Set([...data.readings.keys()].map(islandOf))].sort();
  const box = $("legend");
  box.replaceChildren();
  for (const isl of islands) {
    const k = document.createElement("span"); k.className = "key";
    const l = document.createElement("span"); l.className = "line"; l.style.borderTopColor = css(ISLAND_VAR[isl]);
    const t = document.createElement("span"); t.textContent = isl === "C" ? "C (center)" : isl;
    k.append(l, t); box.appendChild(k);
  }
  if ($("truth").checked && islands.length) {
    const k = document.createElement("span"); k.className = "key";
    const l = document.createElement("span"); l.className = "line dash"; l.style.borderTopColor = css("--muted");
    const t = document.createElement("span"); t.textContent = "true (twin)";
    k.append(l, t); box.appendChild(k);
  }
}
$("truth").onchange = updateCharts;
const cfg_labels = [];

// ---------------------------------------------------------------------------------------------- tabs, lightbox
for (const b of document.querySelectorAll(".tab")) {
  b.onclick = () => {
    for (const x of document.querySelectorAll(".tab")) x.classList.toggle("active", x === b);
    $("tab-live").hidden = b.dataset.tab !== "live"; $("tab-figs").hidden = b.dataset.tab !== "figs";
    if (b.dataset.tab === "figs") showTruth();
    charts.redraw();
  };
}
async function showTruth() {   // simulation only: what the agent's analysis should find
  if (!cfg.twin) return;
  try {
    const t = await (await fetch(`${RIG}/sim/truth`)).json();
    const box = $("truth-box"); box.replaceChildren(); box.hidden = false;
    const add = (label, value) => { const s = document.createElement("span"); s.textContent = label; const b = document.createElement("b"); b.textContent = value; box.append(s, b, document.createTextNode("   ")); };
    add("Twin truth (not shown to the agent) · ideal apparent Eₐ from isothermal half-times, liquid 64–69 °C: ", `${t.apparent_ea_kj} kJ/mol`);
    add(" · ovalbumin Eₐ ", `${t.ea_ova_kj}`); add(" · ovotransferrin Eₐ ", `${t.ea_ot_kj}`);
    add(" · isothermal t50 (liquid °C: min) ", Object.entries(t.isothermal_t50_s).map(([c, s]) => `${c}: ${(s / 60).toFixed(1)}`).join(", "));
    add(" · corner liquid ≈ 23 + ", `${t.bed_to_liquid.corner.eta} × (plate − 23)`);
  } catch { /* twin unreachable */ }
}
function zoom(src) { $("lightbox-img").src = src; $("lightbox").hidden = false; }
$("lightbox").onclick = () => { $("lightbox").hidden = true; };
const fileUrl = (p) => `/file?p=${encodeURIComponent(p)}`;

// ---------------------------------------------------------------------------------------------- the 3D twin
let scene = null;
if (cfg.twin) {
  const geo = await (await fetch(`${RIG}/sim/geometry`)).json();
  cfg_labels.push(...geo.labels);
  scene = new RigScene($("view3d"), $("viewpi"), geo);
  $("view-reset").onclick = () => scene.resetView();
  $("nadir-mark").onchange = () => scene.setNadirMarker($("nadir-mark").checked);
  const wellsBed = Object.entries(geo.wells).map(([lab, w]) => [lab, w.bed]);
  let lastHud = 0;
  scene.onFrame = () => {
    const now = performance.now();
    const s = scene.state;
    if (!s) return;
    const [x, y, z] = scene.nozzle();
    // which well is straight below the lens
    const nx = x + geo.camera_offset[0], ny = y + geo.camera_offset[1];
    let best = null, bd = 1e9;
    for (const [lab, [bx, by]] of wellsBed) { const d = Math.hypot(bx - nx, by - ny); if (d < bd) { bd = d; best = lab; } }
    if (now - lastHud > 120) {
      lastHud = now;
      const tt = new Date(scene.simNow() * 1000).toLocaleTimeString([], { hour12: false });
      $("hud").textContent = `${tt}  ${s.speed.toFixed(s.speed < 2 ? 1 : 0)}×${s.thinking ? " (agent thinking: 1×)" : ""}\n` +
        `X ${x.toFixed(1)}  Y ${y.toFixed(1)}  Z ${z.toFixed(1)}${s.homing ? "  homing…" : ""}\n` +
        `bed ${s.bed.reading.toFixed(1)} °C → ${s.bed.target} °C  (plate ${s.bed.plate_c.toFixed(1)})\n` +
        `${s.running ? "▶ " + s.running : ""}`;
      $("pi-tag").textContent = `${bd < 1.5 ? "over " + best : "between wells"} · Z ${z.toFixed(0)}`;
    }
    const op = s.op;
    if (op && op.label) {
      const p = scene.project(scene.wellWorld(op.label));
      Object.assign($("pin").style, { left: `${p.x}px`, top: `${p.y - 14}px` });
      $("pin").textContent = op.kind === "load_well" ? `Load ${op.label}` : op.kind;
      $("pin").hidden = !p.visible;
    } else $("pin").hidden = true;
  };
}

// ---------------------------------------------------------------------------------------------- the rig's cameras
// Instrument mode: the A1's own camera and the Pi camera, relayed by the harness (/feeds/). The Pi's MJPEG plays in
// an <img>. The A1 sends a frame about every 2 s, so the page loads each new one when the status stream reports it
// instead of holding one more connection open: browsers allow six per server, and this page already keeps three.
const cams = cfg.twin ? null : rigCameras(cfg.instrument || {});
function rigCameras(inst) {
  $("view3d-sub").textContent = "Bambu A1 camera · live";
  $("viewpi-sub").textContent = "Pi OV5647 · 1280×720 fisheye · live";
  $("view-reset").hidden = true;
  const add = (box, tag, cls) => { const el = document.createElement(tag); el.className = cls; box.prepend(el); return el; };
  const say = (el, h, what, staleS) => {   // what the feed is doing, over the picture, unless it is live
    let m = "";
    if (!h || h.age_s == null) m = h && h.error ? `No picture from ${what}: ${h.error}` : `Connecting to ${what}…`;
    else if (h.age_s > staleS) m = `No new picture from ${what} for ${Math.round(h.age_s)} s${h.error ? `: ${h.error}` : ""}`;
    el.textContent = m; el.hidden = !m;
  };
  const a1Msg = add($("view3d"), "div", "feed-msg"), piMsg = add($("viewpi"), "div", "feed-msg");
  say(a1Msg, null, "the printer camera"); say(piMsg, null, "the Pi camera");
  const a1 = add($("view3d"), "img", "feed"), pi = add($("viewpi"), "img", "feed");
  a1.alt = "The printer's own camera"; pi.alt = "The Pi camera on the toolhead, looking down at the wells";

  // the pixel straight below the lens (calibration/nadir.json), marked in frame pixels like the twin's marker
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const [fw, fh] = inst.pi_frame_px || [1280, 720];
  svg.setAttribute("viewBox", `0 0 ${fw} ${fh}`); svg.setAttribute("class", "nadir"); svg.setAttribute("aria-hidden", "true");
  if (inst.nadir_px) {
    const [nx, ny] = inst.nadir_px;
    svg.innerHTML = `<path d="M${nx - 14} ${ny}H${nx + 14}M${nx} ${ny - 14}V${ny + 14}" stroke="#ff4033" stroke-opacity="0.85" stroke-width="2.4"/>`;
    pi.after(svg);
  } else $("nadir-mark").parentElement.hidden = true;
  $("nadir-mark").checked = true;
  $("nadir-mark").onchange = () => { svg.style.display = $("nadir-mark").checked ? "" : "none"; };

  // a hidden page lets go of the Pi's stream (and decodes nothing); the A1 waits for the page to show again
  let piOn = false, a1Seq = 0, a1Latest = 0;
  const piStart = () => { if (piOn || document.hidden) return; piOn = true; pi.src = `/feeds/pi.mjpg?t=${Date.now()}`; };
  const piStop = () => { piOn = false; pi.removeAttribute("src"); };
  pi.onerror = () => { if (piOn) { piStop(); setTimeout(piStart, 3000); } };
  const a1Load = async () => {   // the last frame stays up if a new one fails to come
    if (!a1Latest || a1Latest === a1Seq || document.hidden) return;
    a1Seq = a1Latest;
    try {
      const r = await fetch(`/feeds/a1.jpg?seq=${a1Seq}`);
      if (!r.ok) return;
      const prev = a1.src, url = URL.createObjectURL(await r.blob());
      a1.onload = () => { if (prev.startsWith("blob:")) URL.revokeObjectURL(prev); };
      a1.src = url;
    } catch { /* the harness is restarting */ }
  };
  document.addEventListener("visibilitychange", () => { if (document.hidden) piStop(); else { piStart(); a1Load(); } });

  return {
    restart() { piStop(); piStart(); a1Seq = 0; a1Load(); },   // the harness came back: its old streams are gone
    offline() {
      for (const el of [a1Msg, piMsg]) { el.textContent = "No connection to the harness; reconnecting…"; el.hidden = false; }
      $("pi-tag").textContent = $("hud").textContent = "";
    },
    update(feeds, status) {
      const h1 = feeds.a1, h2 = feeds.pi;
      if (h1 && h1.age_s != null) { a1Latest = h1.seq; a1Load(); }
      say(a1Msg, h1, "the printer camera", 15);
      say(piMsg, h2, "the Pi camera", 5);
      const lo = status && status.last_operation;   // a well read leaves the camera over that well
      const label = lo && ["get_image", "get_raw_opacity"].includes(lo.command) && lo.args && lo.args.label;
      const where = label ? (lo.state === "done" ? `over ${label}` : lo.state === "running" ? `→ ${label}` : "") : "";
      $("pi-tag").textContent = [where, h2 && h2.fps && h2.age_s != null ? `${h2.fps} fps` : ""].filter(Boolean).join(" · ");
    },
  };
}

// ---------------------------------------------------------------------------------------------- twin state stream
let rigState = null;
let lastLive = 0;
if (cfg.twin) {
  const es = new EventSource(`${RIG}/sim/stream`);
  es.addEventListener("state", (e) => {
    const s = JSON.parse(e.data);
    if (!s.wells && rigState && rigState.wells) s.wells = rigState.wells;  // wells come every 3rd frame
    rigState = s;
    scene && scene.setState(s);
    chips(s);
    operatorBanner(s);
    if (data.t0 != null && liveMode && runActive && s.t - lastLive >= 2) {   // continuous bed log while a run is live
      lastLive = s.t;
      data.live.push([xOf(s.t), s.bed.reading]);
      if (s.wells) for (const [lab, w] of Object.entries(s.wells)) {
        if (!data.truth.has(lab)) data.truth.set(lab, []);
        data.truth.get(lab).push([xOf(s.t), w.gray]);
      }
      scheduleCharts();
    }
  });
  es.addEventListener("rig", (e) => {
    const ev = JSON.parse(e.data);
    if (ev.kind === "capture") { const f = $("flash"); f.style.transition = "none"; f.style.opacity = "0.22"; requestAnimationFrame(() => { f.style.transition = "opacity .35s ease-out"; f.style.opacity = "0"; }); }
  });
}

function chips(s) {
  const st = s.safety.state;
  haltBanner(st, s.safety.reason, s.safety.detail, s.safety.t);
  const dot = (c) => `<span class="dot" style="background:${c}"></span>`;
  $("chip-state").innerHTML = st === "ok"
    ? `${dot(s.homed ? css("--good") : css("--warning"))}${s.homed ? "homed" : "not homed"} · safety ok`
    : `${dot(css("--critical"))}safety ${st}`;
  $("chip-bed").textContent = `bed ${s.bed.reading.toFixed(1)} → ${s.bed.target} °C`;
  const tt = new Date(s.t * 1000).toLocaleTimeString([], { hour12: false });
  $("chip-clock").textContent = `${tt} · ${s.user_speed}×${s.thinking ? " (1× while thinking)" : ""}`;
  $("speed").value = String(s.user_speed);
  $("auto-op").checked = s.auto_operator;
}

function operatorBanner(s) {
  const op = s.op;
  $("operator").hidden = !op;
  alerts.set("op", op && !s.auto_operator ? `${op.id}:${op.t_requested}` : null, op && spokenRequest(op));
  if (!op) return;
  $("op-text").textContent = op.text;
  $("op-auto").textContent = s.auto_operator ? "(the simulated operator will answer)" : alerts.hint();
  $("op-done").onclick = () => post(`${RIG}/operator/answer`, { id: op.id, answer: "done", who: "dashboard" });
  $("op-problem").onclick = () => {
    const note = prompt("What is the problem?");
    if (note !== null) post(`${RIG}/operator/answer`, { id: op.id, answer: "problem", note, who: "dashboard" });
  };
}
$("op-stop").onclick = () => post(`${RIG}/operator/stop`, { who: "dashboard" });

// ---------------------------------------------------------------------------------------------- the rig's status
// Instrument mode: the harness polls mhapi-service once for every page watching (/operator/state every second,
// /operator/status, get_status's body, every 2 s; neither resets the heater watchdog) and sends each poll as one
// `status` event, with the cameras' health.
let lastStatusT = null;
if (!cfg.twin) {
  $("chip-rig").title = `mhapi-service; its operator console (Clear after a stop): ${cfg.rig_url}/operator`;
  const es = new EventSource(`${RIG}/status/stream`);
  es.onopen = () => cams.restart();   // also after the harness restarted
  es.onerror = () => {   // the browser retries by itself; meanwhile, don't pass the last values off as current
    $("chip-state").innerHTML = `<span class="dot" style="background:${css("--critical")}"></span>no connection to the harness`;
    $("chip-state").title = "";
    cams.offline();
  };
  es.addEventListener("status", (e) => {
    const p = JSON.parse(e.data);
    const st = p.status_error ? null : p.status;
    rigChips(st, p);
    rigHud(st, p.operator);
    if (p.operator) operatorBanner({ op: p.operator.prompt, auto_operator: false });
    cams.update(p.feeds || {}, st);
    if (st && st.t !== lastStatusT) {   // continuous bed log while a run is live: get_status's reading, every 2 s
      lastStatusT = st.t;
      if (data.t0 != null && liveMode && runActive && st.bed_c != null) { data.live.push([xOf(st.t), st.bed_c]); scheduleCharts(); }
    }
  });
}

function rigChips(st, p) {
  const op = p.operator;
  const dot = (c) => `<span class="dot" style="background:${c}"></span>`;
  const chip = $("chip-state");
  if (!st) {
    chip.innerHTML = `${dot(css("--critical"))}${op ? "get_status failing" : "service not answering"}`;
    chip.title = p.status_error || "";
  } else if (st.safety !== "ok") {
    haltBanner(st.safety, st.safety_reason, st.safety_detail, st.t_safety);
    chip.innerHTML = `${dot(css("--critical"))}safety ${st.safety}`;
    chip.title = [st.safety_reason, st.safety_detail].filter(Boolean).join(": ");
  } else {
    haltBanner("ok");
    chip.innerHTML = `${dot(st.ready ? css("--good") : css("--warning"))}${st.homed ? "homed" : "not homed"}` +
      `${st.homed && !st.ready ? " · not ready" : ""} · safety ok`;
    chip.title = st.ready ? "Ready" : `Not ready:\n${(st.not_ready || []).join("\n")}`;
  }
  const bed = st ? st.bed_c : op && op.bed_c, target = st ? st.target_c : op && op.target_c;
  $("chip-bed").textContent = bed == null ? "bed –" : `bed ${bed.toFixed(1)} → ${target ?? "–"} °C`;
  $("chip-bed").title = `Bed reading → target${st && st.bed_age_s != null ? ` · reported ${st.bed_age_s} s ago` : ""}`;
  const t = st ? st.t : op && op.t;
  $("chip-clock").textContent = t == null ? "–" : `${new Date(t * 1000).toLocaleTimeString([], { hour12: false })} · real time`;
  if (st) $("chip-clock").title = `Instrument clock · session ${st.session} · calibration ${st.calibration_id}`;
}

function rigHud(st, op) {   // the printer camera's corner: instrument time, the bed, the operation under way
  const t = st ? st.t : op && op.t;
  if (t == null) { $("hud").textContent = ""; return; }
  const bed = st ? st.bed_c : op.bed_c, target = st ? st.target_c : op.target_c;
  const o = op && op.operation;
  $("hud").textContent = `${new Date(t * 1000).toLocaleTimeString([], { hour12: false })}\n` +
    `bed ${bed == null ? "–" : bed.toFixed(1)} °C → ${target ?? "–"} °C` +
    (o ? `\n▶ ${o.command}${o.args && o.args.label ? " " + o.args.label : ""}${op.queued ? ` (+${op.queued} queued)` : ""}` : "");
}

// On a public deployment only the browser that started the live run may steer it; the harness gave it a token.
// An admin link (/?admin=<AISIM_ADMIN_TOKEN>) lifts the limits in this browser.
let myToken = null;
const tokens = new Map();   // run_id → token for this page; also in localStorage for reloads
const tokenOf = (runId) => (runId ? tokens.get(runId) || store.get(`run-token:${runId}`) : null);
{ const a = new URLSearchParams(location.search).get("admin"); if (a) { store.set("admin-token", a); history.replaceState(null, "", location.pathname); } }
const adminToken = store.get("admin-token");
async function post(url, body) {
  const headers = { "Content-Type": "application/json" };
  if (myToken || adminToken) headers["X-Run-Token"] = myToken || adminToken;
  const r = await fetch(url, { method: "POST", headers, body: JSON.stringify(body || {}) });
  let out = {};
  try { out = await r.json(); } catch { /* no body */ }
  if ([400, 403, 409, 429, 503].includes(r.status) && typeof out.error === "string" && out.error.includes(" ")) notice(out.error);
  return out;
}
let noticeTimer = null;
function notice(msg) {
  const el = $("notice"); el.textContent = msg; el.hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { el.hidden = true; }, 12000);
}

// sim controls
$("speed").onchange = () => post(`${RIG}/sim/control`, { speed: +$("speed").value });
$("auto-op").onchange = () => post(`${RIG}/sim/control`, { auto_operator: $("auto-op").checked });
$("fault").onchange = async () => {
  const f = $("fault").value; $("fault").value = "";
  if (!f) return;
  let body = { fault: f };
  if (f === "bubble") { const lab = prompt("Which well gets a bubble?", "P3-A1"); if (!lab) return; body.label = lab.trim(); }
  if (f === "room_light") body = { room_light: !(rigState && rigState.room_light) };
  if (f === "camera_bump") body.mm = 0.6;
  if (f === "island_shift") Object.assign(body, { island: "P3", dx: 0.6, dy: 0 });
  if (f === "printer_offline") body.seconds = 60;
  await post(`${RIG}/sim/control`, body);
};

// ---------------------------------------------------------------------------------------------- agent feed
const feed = $("feed");
let liveMode = true, runActive = false, runWorking = false, currentRun = null;
let turnBox = null, turnNo = 0;
const calls = new Map();
const speech = new Map();
const figures = [];

function stamp(t) {
  if (data.t0 == null || t == null) return "";
  const s = Math.max(0, Math.round(t - data.t0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function row(t, cls) {
  const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  const li = document.createElement("li");
  const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = stamp(t);
  const body = document.createElement("div"); if (cls) body.className = cls;
  li.append(ts, body); feed.appendChild(li);
  if (near) feed.scrollTop = feed.scrollHeight;
  return body;
}
function text(el, s) { el.textContent = s; return el; }
function fmtArgs(input) {
  const v = Object.entries(input || {}).filter(([k]) => k !== "code");
  if (!v.length) return "()";
  return "(" + v.map(([k, x]) => (typeof x === "string" ? (k === "label" ? x : `${k}: “${x.length > 60 ? x.slice(0, 60) + "…" : x}”`) : `${k}: ${x}`)).join(", ") + ")";
}

function md(src) {   // tiny, safe markdown for the final report
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*(.+?)\*/g, "<i>$1</i>");
  const out = []; const lines = src.split("\n"); let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*\|/.test(l) && i + 1 < lines.length && /^\s*\|[\s\-:|]+\|\s*$/.test(lines[i + 1])) {
      const cells = (x) => x.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      let h = `<table><tr>${cells(l).map((c) => `<th>${inline(c)}</th>`).join("")}</tr>`; i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) { h += `<tr>${cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`; i++; }
      out.push(h + "</table>"); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) out.push(`<h${m[1].length < 3 ? 3 : 4}>${inline(m[2])}</h${m[1].length < 3 ? 3 : 4}>`);
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { let h = "<ul>"; while (i < lines.length && (m = lines[i].match(/^\s*[-*]\s+(.*)/))) { h += `<li>${inline(m[1])}</li>`; i++; } out.push(h + "</ul>"); continue; }
    else if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { let h = "<ol>"; while (i < lines.length && (m = lines[i].match(/^\s*\d+[.)]\s+(.*)/))) { h += `<li>${inline(m[1])}</li>`; i++; } out.push(h + "</ol>"); continue; }
    else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
    i++;
  }
  return out.join("");
}

function resetFeed() {
  feed.replaceChildren(); calls.clear(); speech.clear(); turnBox = null; turnNo = 0;
  figures.length = 0; $("figs").replaceChildren(); $("fig-count").textContent = "";
}

function turnContainer(ev) {
  if (ev.turn != null && ev.turn !== turnNo) {
    turnNo = ev.turn;
    turnBox = row(ev.t_rig, "turn");
    turnBox.textContent = `turn ${ev.turn}`;
  }
}
function speechEl(ev, kind) {
  const key = `${ev.turn}:${ev.index}`;
  if (!speech.has(key)) { turnContainer(ev); speech.set(key, row(ev.t_rig, kind)); }
  return speech.get(key);
}

function handle(ev, replay = false) {
  switch (ev.kind) {
    case "run_started":
      if (!replay && !liveMode) return;
      currentRun = ev.run_id; runActive = runWorking = !replay;
      if (!replay) { myToken = tokenOf(ev.run_id); refreshConfig(); }
      resetFeed(); resetData(ev.t_rig);
      text(row(ev.t_rig, "say"), `Run ${ev.run_id} · ${ev.model} · effort ${ev.effort}${ev.twin ? " · digital twin" : ""}`);
      const p = row(ev.t_rig); const d = document.createElement("details"); const sm = document.createElement("summary");
      sm.textContent = "prompt"; const pre = document.createElement("pre"); pre.textContent = ev.prompt; d.append(sm, pre); p.appendChild(d);
      $("chip-agent").textContent = replay ? `replay ${ev.run_id}` : "agent running"; setControls();
      break;
    case "turn_started":
      if (!replay && !runWorking) { runWorking = true; setControls(); }
      if (ev.attempt > 1) text(row(ev.t_rig, "warn"), `retrying turn ${ev.turn} (attempt ${ev.attempt})`);
      break;
    case "progress_delta": speechEl(ev, "progress").textContent += ev.text; break;
    case "text_delta": speechEl(ev, "say").textContent += ev.text; break;
    case "assistant": {
      // the final blocks of a turn (replays have only these; live, they replace the streamed text)
      let idx = 0;
      for (const b of ev.blocks || []) {
        if (ev.stop_reason === "end_turn" && b.type === "text") continue;   // the report is shown formatted
        const key = `${ev.turn}:f${idx++}`;
        const streamed = [...speech.entries()].filter(([k]) => k.startsWith(`${ev.turn}:`) && !k.includes(":f"));
        if (!replay && streamed.length) break;
        turnContainer(ev);
        speech.set(key, text(row(ev.t_rig, b.type === "progress" ? "progress" : "say"), b.text));
      }
      break;
    }
    case "tool_call": {
      if (ev.name === "note") break;   // shown as the note itself
      turnContainer(ev);
      const el = row(ev.t_rig, "call");
      const name = document.createElement("span"); name.className = "name"; name.textContent = ev.name;
      const args = document.createElement("span"); args.textContent = fmtArgs(ev.input);
      const res = document.createElement("span"); res.className = "res"; res.textContent = "  …";
      el.append(name, args, res);
      if (ev.name === "python" && ev.input && ev.input.code) {
        const d = document.createElement("details"); const sm = document.createElement("summary"); sm.textContent = "code";
        const pre = document.createElement("pre"); pre.textContent = ev.input.code; d.append(sm, pre); el.appendChild(d);
      }
      calls.set(ev.id, { el, res });
      break;
    }
    case "tool_result": {
      if (ev.name === "note") break;
      let c = calls.get(ev.id);
      if (!c) { handle({ ...ev, kind: "tool_call" }, replay); c = calls.get(ev.id); }
      c.res.textContent = `  → ${ev.summary}`;
      if (!ev.ok) c.el.classList.add("err");
      if (ev.image) {
        const th = document.createElement("div"); th.className = "thumbs";
        const im = document.createElement("img"); im.src = fileUrl(ev.image); im.alt = ev.input && ev.input.label || ""; im.onclick = () => zoom(im.src);
        th.appendChild(im); c.el.appendChild(th);
      }
      if (ev.name === "python" && ev.body && ev.body.output) {
        const d = document.createElement("details"); const sm = document.createElement("summary"); sm.textContent = "output";
        const pre = document.createElement("pre"); pre.textContent = ev.body.output; d.append(sm, pre); c.el.appendChild(d);
      }
      for (const f of ev.figures || []) addFigure(f, ev.t_rig, c.el);
      ingest(ev);
      break;
    }
    case "note": text(row(ev.t_rig, "note"), `📝 ${ev.text}`); break;
    case "operator_call": if (!replay) helpBanner(ev); break;
    case "operator_answer": if (!replay) helpBanner(null); break;
    case "usage": $("chip-cost").textContent = `$${ev.total.cost.toFixed(2)} · ${ev.total.turns} turns · ${(ev.total.cache_read / 1e6).toFixed(2)}M cached`; break;
    case "warning": case "api_retry": text(row(ev.t_rig, "warn"), `⚠ ${ev.message}`); break;
    case "error": text(row(ev.t_rig, "error"), `✖ ${ev.message}`); break;
    case "user_message": text(row(ev.t_rig, "say"), `👤 ${ev.text}`); break;
    case "finished": {
      for (const [k, el] of [...speech.entries()]) {   // drop the raw streamed copy of the report
        if (k.startsWith(`${turnNo}:`) && el.classList.contains("say")) { el.parentElement.remove(); speech.delete(k); }
      }
      const el = row(ev.t_rig, "report"); el.innerHTML = md(ev.report || "(no report)");
      $("chip-agent").textContent = replay ? $("chip-agent").textContent : "agent finished — message it to continue";
      if (!replay) { runWorking = false; setControls(); }
      break;
    }
    case "run_finished":
      if (!replay) helpBanner(null);
      if (!replay) { runActive = runWorking = false; $("chip-agent").textContent = `agent ${ev.status}`; refreshRuns(); refreshConfig(); }
      setControls();
      break;
  }
}

function addFigure(path, t, el) {
  if (figures.includes(path)) return;
  figures.push(path);
  const src = fileUrl(path) + `&v=${Date.now()}`;
  if (el) { const d = document.createElement("div"); d.className = "fig"; const im = document.createElement("img"); im.src = src; im.onclick = () => zoom(src); d.appendChild(im); el.appendChild(d); }
  const fig = document.createElement("figure"); const im = document.createElement("img"); im.src = src; im.onclick = () => zoom(src);
  const cap = document.createElement("figcaption"); cap.textContent = `${path.split("/").pop()} · ${stamp(t)}`;
  fig.append(im, cap);
  if (figures.length === 1) $("figs").replaceChildren();
  $("figs").prepend(fig);
  $("fig-count").textContent = `(${figures.length})`;
}

function ingest(ev) {   // chart data from tool results
  const b = ev.body || {};
  if (!ev.ok || data.t0 == null) return;
  if (ev.name === "get_raw_opacity" && b.label != null && b.gray != null) {
    if (!cfg_labels.includes(b.label)) { cfg_labels.push(b.label); cfg_labels.sort(); }   // the rig: no geometry, same order
    if (!data.readings.has(b.label)) data.readings.set(b.label, []);
    data.readings.get(b.label).push([xOf(b.t), b.gray]);
  }
  if (b.t != null && b.bed_c != null && !(liveMode && data.live.length)) data.temps.push([xOf(b.t), b.bed_c]);
  if (ev.name === "set_temperature" && b.target_c != null) data.targets.push([xOf(b.t), b.target_c]);
  if (b.t != null && b.target_c != null && !data.targets.length) data.targets.push([xOf(b.t), b.target_c]);
  scheduleCharts();
}

// the agent's call_operator: its request, read aloud, until the person answers here (Done, or Can't fix with a note)
let helpCall = null;
function helpBanner(call) {
  helpCall = call || null;
  $("help").hidden = !call;
  alerts.set("help", call ? call.id : null, call && `The agent needs help. ${call.message.slice(0, 300)}`);
  if (!call) return;
  $("help-text").textContent = call.message;
  $("help-meta").textContent = [call.label, alerts.hint()].filter(Boolean).join(" · ");
}
async function answerHelp(answer) {
  if (!helpCall) return;
  const note = $("help-note").value.trim();
  if (answer === "problem" && !note) { $("help-note").placeholder = "Say why, for the agent"; $("help-note").focus(); return; }
  const r = await post("/api/operator", { id: helpCall.id, answer, note });
  if (r.ok) { $("help-note").value = ""; $("help-note").placeholder = "Note for the agent (optional)"; helpBanner(null); }
}
$("help-done").onclick = () => answerHelp("done");
$("help-problem").onclick = () => answerHelp("problem");

// harness event stream
let lastSeq = 0;
function connectHarness() {
  const es = new EventSource(`/api/stream?since=${lastSeq}`);
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    lastSeq = ev.seq;
    if (!liveMode) return;
    if (ev.run_id && currentRun && ev.run_id !== currentRun && ev.kind !== "run_started") return;
    handle(ev);
  };
}
connectHarness();
if (cfg.run && cfg.run.operator_call) helpBanner(cfg.run.operator_call);
if (cfg.run) {
  runActive = ["running", "starting", "finished"].includes(cfg.run.status);
  runWorking = ["running", "starting"].includes(cfg.run.status);
  myToken = tokenOf(cfg.run.run_id);
}

// ---------------------------------------------------------------------------------------------- run controls
function setControls() {
  const mine = !cfg.public || !!myToken || !!adminToken;
  const watching = runWorking && !mine;   // someone else's run on a public deployment
  $("start").disabled = watching;
  $("stop-agent").disabled = !(liveMode && runActive) || !mine;
  $("msg").disabled = $("send").disabled = !(liveMode && runActive) || !mine;
  for (const id of ["speed", "auto-op", "fault", "fresh", "op-done", "op-problem", "op-stop", "help-done", "help-problem", "help-note"]) $(id).disabled = watching;
  publicNote(watching);
}
function publicNote(watching) {
  const el = $("public-note"), p = cfg.public;
  el.hidden = !p;
  if (!p) return;
  el.textContent = (watching ? "Another visitor's run is in progress; you are watching it live. " : "") +
    `Public demo: one run at a time, up to $${p.run_usd.toFixed(0)} and ${p.run_min.toFixed(0)} min of agent work per run; ` +
    `$${p.day_left.toFixed(2)} of today's $${p.day_usd.toFixed(0)} left. Recorded runs can be replayed from the menu above.`;
}
async function refreshConfig() {
  try { cfg = { ...cfg, ...(await (await fetch("/api/config")).json()) }; setControls(); } catch { /* offline */ }
}
$("start").onclick = async () => {
  if (!liveMode) { $("run-pick").value = "live"; await pickRun(); }
  const r = await post("/api/run", { prompt: $("prompt").value, effort: $("effort").value, attach_protocol: $("protocol").checked,
                                     fresh_rig: !!cfg.twin && $("fresh").checked });
  if (r.token) { tokens.set(r.run_id, r.token); store.set(`run-token:${r.run_id}`, r.token); myToken = r.token; setControls(); }
};
$("stop-agent").onclick = () => post("/api/stop", {});
$("send").onclick = async () => { const t = $("msg").value.trim(); if (!t) return; $("msg").value = ""; await post("/api/message", { text: t }); };
$("msg").onkeydown = (e) => { if (e.key === "Enter") $("send").click(); };

async function refreshRuns() {
  const runs = await (await fetch("/api/runs")).json();
  const sel = $("run-pick"); const keep = sel.value;
  sel.replaceChildren(new Option("Live", "live"));
  for (const r of runs) sel.appendChild(new Option(`${r.run_id} · ${r.status} · $${(r.usage && r.usage.cost || 0).toFixed(2)}`, r.run_id));
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "live";
}
async function pickRun() {
  const id = $("run-pick").value;
  liveMode = id === "live";
  resetFeed(); resetData(null);
  if (liveMode) { lastSeq = 0; currentRun = null; connectHarnessReplay(); setControls(); return; }
  const events = await (await fetch(`/api/runs/${id}/trace`)).json();
  for (const ev of events) handle(ev, true);
  setControls();
}
async function connectHarnessReplay() {   // re-render the live run from the server's in-memory log
  const r = await (await fetch("/api/config")).json();
  if (!r.run) return;
  const events = await (await fetch(`/api/runs/${r.run.run_id}/trace`)).json();
  for (const ev of events) handle(ev, true);
  currentRun = r.run.run_id; runActive = ["running", "starting", "finished"].includes(r.run.status);
  runWorking = ["running", "starting"].includes(r.run.status); myToken = tokenOf(currentRun);
  helpBanner(r.run.operator_call);
  $("chip-agent").textContent = `agent ${r.run.status}`;
  setControls();
}
$("run-pick").onchange = pickRun;
refreshRuns();
setControls();
