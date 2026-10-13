// Canvas time-series charts with a shared x axis (minutes), a synced crosshair and one tooltip.
// Marks: 2px lines with round joins, >=8px end dots with a 2px surface ring, hairline solid grid.
// Text uses ink tokens, never the series color. Colors are read from CSS custom properties.

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

export class ChartGroup {
  constructor(tooltipEl) {
    this.charts = [];
    this.tooltip = tooltipEl;
    this.hoverX = null;
    this.xMax = 1;
    this.fmtX = (x) => `${x.toFixed(1)} min`;
  }
  add(chart) { chart.group = this; this.charts.push(chart); return chart; }
  setXMax(x) { this.xMax = Math.max(1, x); }
  redraw() { for (const c of this.charts) c.requestDraw(); }
  hover(x, source, clientX, clientY) {
    this.hoverX = x;
    for (const c of this.charts) c.requestDraw();
    if (x == null) { this.tooltip.hidden = true; return; }
    const rows = source.tooltipRows(x);
    if (!rows.length) { this.tooltip.hidden = true; return; }
    const tt = this.tooltip;
    tt.replaceChildren();
    const head = document.createElement("div");
    head.className = "tt-head";
    head.textContent = this.fmtX(x);
    tt.appendChild(head);
    for (const r of rows.slice(0, 18)) {
      const row = document.createElement("div");
      row.className = "tt-row";
      const key = document.createElement("span");
      key.className = "key";
      key.style.borderTopColor = r.color;
      if (r.dashed) key.style.borderTopStyle = "dashed";
      const val = document.createElement("b");
      val.textContent = r.value;
      const lab = document.createElement("span");
      lab.textContent = r.label;
      row.append(key, val, lab);
      tt.appendChild(row);
    }
    if (rows.length > 18) {
      const more = document.createElement("div");
      more.className = "tt-head";
      more.textContent = `+${rows.length - 18} more`;
      tt.appendChild(more);
    }
    tt.hidden = false;
    const host = tt.offsetParent.getBoundingClientRect();
    let left = clientX - host.left + 14, top = clientY - host.top + 12;
    if (left + tt.offsetWidth > host.width - 4) left = clientX - host.left - tt.offsetWidth - 14;
    tt.style.left = `${Math.max(4, left)}px`;
    tt.style.top = `${Math.max(4, top)}px`;
  }
}

export class LineChart {
  constructor(el, opts) {
    this.el = el;
    this.opts = Object.assign({ yMin: 0, yMax: 255, yAuto: false, yUnit: "", yDecimals: 0, endLabels: false }, opts);
    this.canvas = document.createElement("canvas");
    el.appendChild(this.canvas);
    this.series = [];
    this.pad = { l: 40, r: 40, t: 8, b: 22 };
    this._raf = 0;
    new ResizeObserver(() => this.requestDraw()).observe(el);
    this.canvas.addEventListener("pointermove", (e) => this._move(e));
    this.canvas.addEventListener("pointerleave", () => this.group && this.group.hover(null, this));
  }
  setSeries(series) { this.series = series; this.requestDraw(); }
  requestDraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
  }
  xToPx(x) { const w = this.w - this.pad.l - this.pad.r; return this.pad.l + (x / this.group.xMax) * w; }
  pxToX(px) { const w = this.w - this.pad.l - this.pad.r; return ((px - this.pad.l) / w) * this.group.xMax; }
  yToPx(y) { const h = this.h - this.pad.t - this.pad.b; return this.pad.t + (1 - (y - this.y0) / (this.y1 - this.y0)) * h; }

  _yDomain() {
    if (!this.opts.yAuto) return [this.opts.yMin, this.opts.yMax];
    let lo = Infinity, hi = -Infinity;
    for (const s of this.series) for (const [, y] of s.points) { if (y < lo) lo = y; if (y > hi) hi = y; }
    if (!isFinite(lo)) return [this.opts.yMin, this.opts.yMax];
    const span = Math.max(4, hi - lo);
    const step = niceStep(span, 4);
    return [Math.floor((lo - span * 0.08) / step) * step, Math.ceil((hi + span * 0.08) / step) * step];
  }

  draw() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = this.el.getBoundingClientRect();
    this.w = r.width; this.h = r.height;
    if (!this.w || !this.h || !this.group) return;
    const cv = this.canvas;
    if (cv.width !== Math.round(this.w * dpr) || cv.height !== Math.round(this.h * dpr)) {
      cv.width = Math.round(this.w * dpr); cv.height = Math.round(this.h * dpr);
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, this.w, this.h);
    [this.y0, this.y1] = this._yDomain();
    const surface = css("--surface"), grid = css("--grid"), axis = css("--axis"), muted = css("--muted"), ink2 = css("--ink-2");
    const pl = this.pad.l, pr = this.w - this.pad.r, pt = this.pad.t, pb = this.h - this.pad.b;
    g.font = "11px system-ui, -apple-system, Segoe UI, sans-serif";
    g.textBaseline = "middle";
    // y grid + ticks
    const ys = niceStep(this.y1 - this.y0, this.h > 200 ? 5 : 3);
    g.lineWidth = 1;
    for (let y = Math.ceil(this.y0 / ys) * ys; y <= this.y1 + 1e-9; y += ys) {
      const py = Math.round(this.yToPx(y)) + 0.5;
      g.strokeStyle = grid; g.beginPath(); g.moveTo(pl, py); g.lineTo(pr, py); g.stroke();
      g.fillStyle = muted; g.textAlign = "right";
      g.fillText(y.toFixed(this.opts.yDecimals), pl - 6, py);
    }
    // x ticks
    const xs = niceStep(this.group.xMax, Math.max(3, Math.floor((pr - pl) / 90)));
    g.textAlign = "center"; g.textBaseline = "top";
    for (let x = 0; x <= this.group.xMax + 1e-9; x += xs) {
      const px = Math.round(this.xToPx(x)) + 0.5;
      g.strokeStyle = grid; g.beginPath(); g.moveTo(px, pt); g.lineTo(px, pb); g.stroke();
      g.fillStyle = muted; g.fillText(`${+x.toFixed(1)}`, px, pb + 5);
    }
    g.textAlign = "left";
    g.fillText("min", pr + 6, pb + 5);
    g.strokeStyle = axis; g.beginPath(); g.moveTo(pl, pb + 0.5); g.lineTo(pr, pb + 0.5); g.stroke();
    // lines: dashed/overlay series first, so data lines sit on top
    const order = [...this.series].sort((a, b) => (b.dashed ? 1 : 0) - (a.dashed ? 1 : 0));
    g.save();
    g.beginPath(); g.rect(pl, pt - 4, pr - pl + 6, pb - pt + 8); g.clip();
    for (const s of order) {
      if (s.points.length < 1) continue;
      g.strokeStyle = s.color;
      g.globalAlpha = s.alpha ?? 1;
      g.lineWidth = s.width ?? 2;
      g.lineJoin = "round"; g.lineCap = "round";
      g.setLineDash(s.dashed ? [5, 4] : []);
      g.beginPath();
      let prev = null;
      for (const [x, y] of s.points) {
        const px = this.xToPx(x), py = this.yToPx(y);
        if (prev === null) g.moveTo(px, py);
        else if (s.step) { g.lineTo(px, this.yToPx(prev)); g.lineTo(px, py); }
        else g.lineTo(px, py);
        prev = y;
      }
      if (s.step && s.extendTo != null && prev != null) g.lineTo(this.xToPx(s.extendTo), this.yToPx(prev));
      g.stroke();
    }
    g.setLineDash([]); g.globalAlpha = 1;
    // end dots with a surface ring
    for (const s of this.series) {
      if (s.dashed || !s.endDot || !s.points.length) continue;
      const [x, y] = s.points[s.points.length - 1];
      const px = this.xToPx(x), py = this.yToPx(y);
      g.fillStyle = surface; g.beginPath(); g.arc(px, py, 6, 0, Math.PI * 2); g.fill();
      g.fillStyle = s.color; g.beginPath(); g.arc(px, py, 4, 0, Math.PI * 2); g.fill();
    }
    g.restore();
    // direct labels per group at the right edge, skipped when they would collide
    if (this.opts.endLabels) {
      const groups = new Map();
      for (const s of this.series) {
        if (s.dashed || !s.points.length || !s.group) continue;
        const last = s.points[s.points.length - 1];
        const cur = groups.get(s.group);
        if (!cur || last[0] > cur.x || (last[0] === cur.x && last[1] > cur.y)) groups.set(s.group, { x: last[0], y: last[1] });
      }
      const placed = [];
      g.font = "600 11px system-ui, -apple-system, Segoe UI, sans-serif";
      g.textBaseline = "middle"; g.textAlign = "left"; g.fillStyle = ink2;
      for (const [name, p] of [...groups.entries()].sort((a, b) => b[1].y - a[1].y)) {
        const py = this.yToPx(p.y);
        if (placed.some((q) => Math.abs(q - py) < 13)) continue;
        placed.push(py);
        g.fillText(name, Math.min(pr + 8, this.xToPx(p.x) + 9), py);
      }
    }
    // crosshair
    if (this.group.hoverX != null) {
      const px = Math.round(this.xToPx(this.group.hoverX)) + 0.5;
      g.strokeStyle = ink2; g.globalAlpha = 0.5; g.lineWidth = 1;
      g.beginPath(); g.moveTo(px, pt); g.lineTo(px, pb); g.stroke(); g.globalAlpha = 1;
    }
  }

  _move(e) {
    if (!this.group) return;
    const r = this.canvas.getBoundingClientRect();
    const x = this.pxToX(e.clientX - r.left);
    if (x < 0 || x > this.group.xMax) return this.group.hover(null, this);
    // snap to the nearest data time across all charts
    let best = x, bd = Infinity;
    for (const c of this.group.charts) for (const s of c.series) {
      if (s.dashed || s.noSnap) continue;
      for (const [px] of s.points) { const d = Math.abs(px - x); if (d < bd) { bd = d; best = px; } }
    }
    if (bd > this.group.xMax * 0.03) best = x;
    this.group.hover(best, this, e.clientX, e.clientY);
  }

  tooltipRows(x) {
    const rows = [];
    for (const c of this.group.charts) {
      for (const s of c.series) {
        if (!s.points.length || s.noTooltip) continue;
        let best = null, bd = Infinity;
        for (const p of s.points) { const d = Math.abs(p[0] - x); if (d < bd) { bd = d; best = p; } }
        if (s.step) {  // value in force at x
          best = null;
          for (const p of s.points) { if (p[0] <= x + 1e-9) best = p; else break; }
          bd = 0;
        }
        if (!best || bd > Math.max(0.6, this.group.xMax * 0.02)) continue;
        rows.push({ color: s.color, dashed: s.dashed, label: s.label, value: `${best[1].toFixed(c.opts.yDecimals)}${c.opts.yUnit}`, order: s.order ?? 0 });
      }
    }
    return rows.sort((a, b) => a.order - b.order);
  }
}
