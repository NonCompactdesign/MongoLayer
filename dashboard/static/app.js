"use strict";
// Live dashboard: knobs -> POST /api/control, snapshots <- /ws once a second, charts with uPlot.

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const LEVELS = ["FAST", "MODERATE", "SAFE"];
const LEVEL_VAR = { FAST: "--fast", MODERATE: "--moderate", SAFE: "--safe" };
const MAX_HISTORY = 900;
const NODE_POS = { mongo1: [200, 40], mongo2: [70, 150], mongo3: [330, 150] };
const CLIENT_POS = [200, 222];
const PYMONGO_LOCAL_THRESHOLD_MS = 15; // "nearest" picks any node within 15 ms of the closest

let history = [];   // one sample per second, oldest first
let rows = [];      // the samples inside the chosen time span
let snap = null;    // the engine's latest snapshot
let control = null; // the control file
let engine = { status: "stopped" };
let spanSeconds = 300;
let lastLocalEdit = 0;
let dragging = false;
const charts = {};

// --- formatting ---------------------------------------------------------------------------------

const fmtMs = (v) => (v == null ? "–" : v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v)} ms`);
const fmtPct = (v, d = 0) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const fmtDur = (s) => (s == null ? "–" : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// --- server calls -------------------------------------------------------------------------------

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

function showError(e) {
  const box = $("engine-error");
  box.textContent = e ? String(e.message || e) : "";
  box.hidden = !e;
}

function setControl(changes) {
  lastLocalEdit = Date.now();
  Object.assign(control, changes);
  renderKnobs(true);
  post("/api/control", changes).then((c) => { control = c; renderKnobs(true); showError(null); }).catch(showError);
}

// --- charts -------------------------------------------------------------------------------------

function axis(extra = {}) {
  return {
    stroke: css("--text-2"),
    grid: { stroke: css("--grid"), width: 1 },
    ticks: { stroke: css("--grid"), width: 1, size: 4 },
    font: "11px system-ui, sans-serif",
    ...extra,
  };
}

const xAxis = () => axis({ space: 70, values: (u, ticks) => ticks.map((t) => fmtTime(t)) });
const xScale = { time: true, range: (u, min, max) => (max == null ? [0, 1] : [max - spanSeconds, max]) };

// vertical line at every switch, in the colour of the setting it switched to
const switchMarkers = {
  hooks: {
    draw: [(u) => {
      if (!snap) return;
      const { ctx } = u;
      const { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.lineWidth = 2 * uPlot.pxRatio;
      for (const ev of snap.events) {
        if (ev.kind !== "switch") continue;
        const x = u.valToPos(ev.t, "x", true);
        if (x < left || x > left + width) continue;
        ctx.strokeStyle = css(LEVEL_VAR[ev.new]);
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
      }
      ctx.restore();
    }],
  },
};

// horizontal reference lines: getLines() -> [{y, color, dash, label}]
function refLines(getLines) {
  return {
    hooks: {
      draw: [(u) => {
        const { ctx } = u;
        const { left, top, width, height } = u.bbox;
        ctx.save();
        ctx.lineWidth = 1.5 * uPlot.pxRatio;
        ctx.font = `${11 * uPlot.pxRatio}px system-ui, sans-serif`;
        for (const l of getLines()) {
          if (l.y == null) continue;
          const y = u.valToPos(l.y, "y", true);
          if (y < top || y > top + height) continue;
          ctx.strokeStyle = l.color;
          ctx.setLineDash(l.dash ? [6 * uPlot.pxRatio, 4 * uPlot.pxRatio] : []);
          ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke();
          if (l.label) {
            ctx.fillStyle = css("--text-2");
            ctx.textAlign = "left";
            ctx.fillText(l.label, left + (l.labelX || 6) * uPlot.pxRatio, y - 4 * uPlot.pxRatio);
          }
        }
        ctx.restore();
      }],
    },
  };
}

// FAST / MODERATE / SAFE zones behind the write-share line
const shareBands = {
  hooks: {
    drawClear: [(u) => {
      const r = (snap && snap.rules) || { low: 0.15, high: 0.4 };
      const { ctx } = u;
      const { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.font = `600 ${11 * uPlot.pxRatio}px system-ui, sans-serif`;
      for (const [lo, hi, name] of [[0, r.low, "FAST"], [r.low, r.high, "MODERATE"], [r.high, 1, "SAFE"]]) {
        const y0 = u.valToPos(lo, "y", true);
        const y1 = u.valToPos(hi, "y", true);
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = css(LEVEL_VAR[name]);
        ctx.fillRect(left, y1, width, y0 - y1);
        ctx.globalAlpha = 1;
        ctx.fillStyle = css("--text-2");
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        if (y0 - y1 > 12 * uPlot.pxRatio) ctx.fillText(name, left + 6 * uPlot.pxRatio, (y0 + y1) / 2);
      }
      ctx.restore();
    }],
  },
};

// the setting timeline: one coloured block per sample
const settingBlocks = {
  hooks: {
    draw: [(u) => {
      const xs = u.data[0];
      const { ctx } = u;
      const { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.beginPath(); ctx.rect(left, top, width, height); ctx.clip();
      for (let i = 0; i < xs.length; i++) {
        const x0 = u.valToPos(xs[i] - 1, "x", true);
        const x1 = u.valToPos(xs[i], "x", true);
        ctx.fillStyle = css(LEVEL_VAR[rows[i].setting] || "--muted");
        ctx.fillRect(x0, top, Math.max(1, x1 - x0 + 0.5), height);
      }
      ctx.restore();
    }],
  },
};

function logRange(extra) {
  return (u, min, max) => {
    const e = extra();
    const lo = Math.min(min ?? 1, ...e.filter((v) => v != null));
    const hi = Math.max(max ?? 10, ...e.filter((v) => v != null));
    return uPlot.rangeLog(Math.max(0.1, lo), Math.max(hi, lo * 10), 10, true);
  };
}

function line(label, colorVar, extra = {}) {
  return { label, stroke: css(colorVar), width: 2, points: { show: false }, spanGaps: false, ...extra };
}

function chartDefs() {
  const ms = (u, v) => fmtMs(v);
  const pctSeries = (u, v) => (v == null ? "–" : `${v.toFixed(1)}%`);
  return {
    setting: {
      el: "c-setting", height: 100,
      opts: {
        legend: { show: false },
        scales: { x: xScale, y: { range: [0, 1] } },
        axes: [xAxis(), { show: false }],
        series: [{}, { paths: () => null, points: { show: false } }],
        plugins: [settingBlocks, switchMarkers],
      },
      data: () => [rows.map((r) => r.t), rows.map(() => 1)],
    },
    share: {
      el: "c-share", height: 190,
      opts: {
        scales: { x: xScale, y: { range: [0, 1] } },
        axes: [xAxis(), axis({ values: (u, v) => v.map((x) => `${Math.round(x * 100)}%`), size: 44 })],
        series: [{}, line("write share", "--text", { value: (u, v) => fmtPct(v, 1) })],
        plugins: [shareBands, switchMarkers, refLines(() => {
          const r = snap && snap.rules;
          if (!r) return [];
          const out = [{ y: r.low, color: css("--text-2") }, { y: r.high, color: css("--text-2") }];
          if (r.hysteresis > 0) {
            out.push({ y: r.low + r.hysteresis, color: css("--muted"), dash: true, label: "leave FAST", labelX: 90 });
            out.push({ y: r.high - r.hysteresis, color: css("--muted"), dash: true, label: "leave SAFE", labelX: 90 });
          }
          return out;
        })],
      },
      data: () => [rows.map((r) => r.t), rows.map((r) => r.write_share)],
    },
    ops: {
      el: "c-ops", height: 190,
      opts: {
        scales: { x: xScale, y: { range: (u, min, max) => [0, Math.max(10, (max || 0) * 1.1)] } },
        axes: [xAxis(), axis({ size: 44 })],
        series: [{},
          line("total", "--text", { value: (u, v) => (v == null ? "–" : `${Math.round(v)}/s`) }),
          line("reads", "--reads", { value: (u, v) => (v == null ? "–" : `${Math.round(v)}/s`) }),
          line("writes", "--writes", { value: (u, v) => (v == null ? "–" : `${Math.round(v)}/s`) }),
          line("target", "--ref", { dash: [6, 4], width: 1.5, value: (u, v) => (v == null ? "–" : `${v}/s`) })],
        plugins: [switchMarkers],
      },
      data: () => [rows.map((r) => r.t), rows.map((r) => r.read_ops + r.write_ops), rows.map((r) => r.read_ops),
        rows.map((r) => r.write_ops), rows.map((r) => r.target_ops)],
    },
    read: {
      el: "c-read", height: 190,
      opts: {
        scales: { x: xScale, y: { distr: 3, log: 10, range: logRange(() => []) } },
        axes: [xAxis(), axis({ size: 50, values: (u, v) => v.map((x) => (x == null ? "" : `${x}`)) })],
        series: [{}, line("p50", "--p50", { value: ms }), line("p95", "--p95", { value: ms }), line("p99", "--p99", { value: ms })],
        plugins: [switchMarkers],
      },
      data: () => [rows.map((r) => r.t), rows.map((r) => r.read_p50), rows.map((r) => r.read_p95), rows.map((r) => r.read_p99)],
    },
    write: {
      el: "c-write", height: 190,
      opts: {
        scales: { x: xScale, y: { distr: 3, log: 10, range: logRange(() => [snap && snap.rules.budget_ms]) } },
        axes: [xAxis(), axis({ size: 50, values: (u, v) => v.map((x) => (x == null ? "" : `${x}`)) })],
        series: [{}, line("p50", "--p50", { value: ms }), line("p95", "--p95", { value: ms }), line("p99", "--p99", { value: ms })],
        plugins: [switchMarkers, refLines(() => {
          const b = snap && snap.rules.budget_ms;
          return b ? [{ y: b, color: css("--critical"), dash: true, label: `SAFE budget ${b} ms` }] : [];
        })],
      },
      data: () => [rows.map((r) => r.t), rows.map((r) => r.write_p50), rows.map((r) => r.write_p95), rows.map((r) => r.write_p99)],
    },
    stale: {
      el: "c-stale", height: 190,
      opts: {
        scales: { x: xScale, y: { range: (u, min, max) => [0, Math.max(10, Math.ceil(((max || 0) * 1.15) / 5) * 5)] } },
        axes: [xAxis(), axis({ size: 44, values: (u, v) => v.map((x) => `${x}%`) })],
        series: [{}, line("stale probes", "--critical", { value: pctSeries, fill: css("--critical-bg") })],
        plugins: [switchMarkers],
      },
      data: () => [rows.map((r) => r.t), rows.map((r) => r.stale_pct)],
    },
  };
}

let defs = null;

function buildCharts() {
  for (const key of Object.keys(charts)) { charts[key].destroy(); delete charts[key]; }
  defs = chartDefs();
  for (const [key, d] of Object.entries(defs)) {
    const el = $(d.el);
    el.innerHTML = "";
    charts[key] = new uPlot({
      width: Math.max(200, el.clientWidth),
      height: d.height,
      cursor: { sync: { key: "t" }, points: { size: 8 } },
      ...d.opts,
    }, d.data(), el);
  }
}

function renderCharts() {
  const tmax = history.length ? history[history.length - 1].t : 0;
  rows = history.filter((p) => p.t >= tmax - spanSeconds);
  for (const [key, d] of Object.entries(defs)) charts[key].setData(d.data());
}

const resizer = new ResizeObserver(() => {
  for (const [key, d] of Object.entries(defs || {})) {
    const w = Math.max(200, $(d.el).clientWidth);
    if (charts[key] && charts[key].width !== w) charts[key].setSize({ width: w, height: d.height });
  }
});

// --- tiles and panels ---------------------------------------------------------------------------

function delta(key, unit = " ms") {
  // change of the 5 s sample against the one 15 s earlier
  if (history.length < 16) return "";
  const now = history[history.length - 1][key];
  const then = history[history.length - 16][key];
  if (now == null || then == null) return "";
  const d = now - then;
  if (Math.abs(d) < 0.5) return `<span>steady vs 15 s ago</span>`;
  return `<span class="${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d).toFixed(d < 10 && d > -10 ? 1 : 0)}${unit}</span> vs 15 s ago`;
}

function renderTiles() {
  if (!snap) {
    for (const id of ["t-ops", "t-ws", "t-rp95", "t-wp95", "t-stale", "t-redir", "t-sw", "t-err"]) $(id).textContent = "–";
    $("t-setting").textContent = "–"; $("t-setting").className = "badge";
    $("decision").textContent = engine.status === "running" ? "waiting for the first decision ..." : "engine not running";
    return;
  }
  const s = snap.summary;
  const t = snap.traffic;
  const st = snap.setting;
  $("t-setting").textContent = st.name;
  $("t-setting").className = `badge ${st.name}`;
  $("t-setting-detail").textContent = `w: ${st.write_concern} · readConcern: ${st.read_concern} · ${st.read_preference}`;
  $("t-ops").textContent = `${Math.round(snap.achieved_ops)}/s`;
  $("t-ops-d").textContent = t.paused ? "traffic paused" : `target ${t.ops_per_sec}/s · last ${snap.window_s} s`;
  $("t-ws").textContent = fmtPct(snap.write_share, 1);
  $("t-ws-d").textContent = `knob ${fmtPct(t.write_share)} · ${s.reads} reads / ${s.writes} writes`;
  $("t-rp95").textContent = fmtMs(s.read_p95);
  $("t-rp95-d").innerHTML = `p50 ${fmtMs(s.read_p50)} · p99 ${fmtMs(s.read_p99)}<br>${delta("read_p95")}`;
  $("t-wp95").textContent = fmtMs(s.write_p95);
  $("t-wp95-d").innerHTML = `p50 ${fmtMs(s.write_p50)} · p99 ${fmtMs(s.write_p99)}<br>${delta("write_p95")}`;
  $("t-stale").textContent = s.probes ? `${s.stale_pct}%` : "–";
  $("t-stale-d").textContent = s.probes ? `${s.stale} of ${s.probes} probes, last ${snap.window_s} s` : "no probes yet";
  $("t-redir").textContent = snap.redirected_reads.toLocaleString();
  $("t-sw").textContent = snap.switches;
  $("t-sw-d").textContent = snap.since_switch == null ? "none yet"
    : `last ${fmtDur(snap.since_switch)} ago${snap.cooldown_left > 0 ? ` · cooldown ${Math.ceil(snap.cooldown_left)} s` : ""}`;
  $("t-err").textContent = s.errors;
  $("t-err-d").textContent = `last ${snap.window_s} s`;
  $("decision").textContent = snap.reason || "(no decision yet)";
}

function renderBySetting() {
  const el = $("by-setting");
  const bs = (snap && snap.by_setting) || {};
  const levels = LEVELS.filter((l) => bs[l]);
  if (!levels.length) { el.innerHTML = `<p class="muted">No operations yet.</p>`; return; }
  let max = 1;
  for (const l of levels) for (const k of ["read", "write"]) if (bs[l][k] && bs[l][k].p95 > max) max = bs[l][k].p95;
  el.innerHTML = levels.map((l) => {
    const bars = ["read", "write"].map((k) => {
      const v = bs[l][k];
      if (!v) return `<div class="bs-bar"><span>${k}</span><div class="bs-track"></div><span class="bs-val muted">no samples</span></div>`;
      const color = `var(${LEVEL_VAR[l]})`;
      return `<div class="bs-bar" title="${l} ${k}s: p50 ${fmtMs(v.p50)}, p95 ${fmtMs(v.p95)} over ${v.n} operations">
        <span>${k}</span>
        <div class="bs-track">
          <div class="bs-fill p95" style="width:${(100 * v.p95) / max}%;background:${color}"></div>
          <div class="bs-fill" style="width:${(100 * v.p50) / max}%;background:${color}"></div>
        </div>
        <span class="bs-val">p50 ${fmtMs(v.p50)} · p95 ${fmtMs(v.p95)}</span></div>`;
    }).join("");
    return `<div class="bs-row"><div class="bs-name"><i class="sw ${l.toLowerCase()}" style="margin:0"></i>${l}</div><div class="bs-bars">${bars}</div></div>`;
  }).join("") + `<small class="muted">Solid = p50, light = p95. Hover a bar for sample counts.</small>`;
}

function renderTopology() {
  const topo = (snap && snap.topology) || null;
  const name = topo ? topo.name : (control && control.topology) || "";
  $("topo-name").textContent = topo ? topo.description : name;
  if (!topo) { $("topo-map").innerHTML = `<p class="muted">Start the engine to see the distances.</p>`; return; }
  const setting = snap.setting.name;
  const minRtt = Math.min(...Object.values(topo.client));
  const readNodes = setting === "FAST"
    ? Object.keys(topo.client).filter((n) => topo.client[n] <= minRtt + PYMONGO_LOCAL_THRESHOLD_MS)
    : [topo.primary];
  const [cx, cy] = CLIENT_POS;
  let svg = `<svg viewBox="0 0 400 248" role="img" aria-label="Replica set topology">`;
  for (const l of topo.links) {
    const [x1, y1] = NODE_POS[l.a]; const [x2, y2] = NODE_POS[l.b];
    svg += `<line class="link" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
  }
  for (const n of Object.keys(NODE_POS)) {
    const [x, y] = NODE_POS[n];
    const isRead = readNodes.includes(n);
    const isWrite = n === topo.primary;
    const cls = isRead && isWrite ? "path-both" : isRead ? "path-read" : isWrite ? "path-write" : "";
    svg += `<line class="clink ${cls}" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"/>`;
  }
  for (const l of topo.links) {
    const [x1, y1] = NODE_POS[l.a]; const [x2, y2] = NODE_POS[l.b];
    const mx = (x1 + x2) / 2 + (l.a === "mongo2" ? 45 : 0);
    const my = (y1 + y2) / 2 - 6;
    svg += `<text class="rtt" x="${mx}" y="${my}" text-anchor="middle" paint-order="stroke" stroke="var(--surface)" stroke-width="4">${l.rtt} ms</text>`;
  }
  for (const n of Object.keys(NODE_POS)) {
    const [x, y] = NODE_POS[n];
    const f = n === "mongo1" ? 0.62 : 0.5;
    const lx = cx + (x - cx) * f; const ly = cy + (y - cy) * f;
    svg += `<text class="rtt" x="${lx}" y="${ly + 4}" text-anchor="middle" paint-order="stroke" stroke="var(--surface)" stroke-width="4">≈${Math.round(topo.client[n])} ms</text>`;
  }
  for (const n of Object.keys(NODE_POS)) {
    const [x, y] = NODE_POS[n];
    svg += `<circle class="node ${n === topo.primary ? "primary" : ""}" cx="${x}" cy="${y}" r="22"/>`;
    svg += `<text x="${x}" y="${y + 4}" text-anchor="middle" font-weight="600">${n.replace("mongo", "m")}</text>`;
    svg += `<text class="rtt" x="${x}" y="${y - 28}" text-anchor="middle">${n}${n === topo.primary ? " (primary)" : ""}</text>`;
  }
  svg += `<rect x="${cx - 26}" y="${cy - 13}" width="52" height="24" rx="5" fill="var(--surface)" stroke="var(--text-2)"/>`;
  svg += `<text x="${cx}" y="${cy + 4}" text-anchor="middle" font-weight="600">client</text></svg>`;
  svg += `<small class="muted"><i class="sw" style="background:var(--reads)"></i>reads (${setting === "FAST" ? "nearest" : "primary"})
    <i class="sw" style="background:var(--writes)"></i>writes (primary) <i class="sw" style="background:var(--text)"></i>both</small>`;
  $("topo-map").innerHTML = svg;
}

function renderBudget() {
  const el = $("budget");
  if (!snap) { el.innerHTML = `<p class="muted">Engine not running.</p>`; return; }
  const r = snap.rules;
  const p95 = r.safe_write_p95;
  const enough = r.safe_write_n >= r.budget_min_samples;
  const ratio = p95 == null ? 0 : Math.min(1, p95 / (r.budget_ms * 3));
  const blocked = r.backoff_left > 0;
  el.innerHTML = `
    <div class="row"><span>Budget for SAFE write p95</span><b>${r.budget_ms} ms</b></div>
    <div class="row"><span>SAFE write p95 now (${r.safe_write_n} writes${enough ? "" : `, needs ${r.budget_min_samples}`})</span><b>${fmtMs(p95)}</b></div>
    <div class="meter" title="Bar runs from 0 to 3× the budget; red once over budget">
      <div class="${p95 != null && p95 > r.budget_ms ? "over" : ""}" style="width:${ratio * 100}%"></div>
    </div>
    <div class="row"><span>SAFE allowed?</span><b>${blocked ? `No, breach of ${fmtMs(r.breach_p95)} held for ${Math.ceil(r.backoff_left)} s more` : "Yes"}</b></div>
    <div class="row"><span>Hysteresis</span><b>${Math.round(r.hysteresis * 100)} points</b></div>
    <div class="row"><span>Switch cooldown left</span><b>${snap.cooldown_left > 0 ? `${Math.ceil(snap.cooldown_left)} s` : "none"}</b></div>`;
}

function renderLog() {
  const events = ((snap && snap.events) || []).slice().reverse();
  $("log").innerHTML = events.length ? events.map((e) => e.kind === "switch"
    ? `<tr><td class="mono time">${fmtTime(e.t)}</td>
         <td class="ev"><i class="sw ${e.old.toLowerCase()}" style="margin-left:0"></i>${e.old} → <i class="sw ${e.new.toLowerCase()}"></i>${e.new}</td>
         <td>${fmtPct(e.write_share, 1)}</td><td class="mono">${esc(e.text)}</td></tr>`
    : `<tr><td class="mono time">${fmtTime(e.t)}</td><td class="ev traffic">traffic</td><td></td><td class="mono muted">${esc(e.text)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No switches yet.</td></tr>`;
}

// --- knobs --------------------------------------------------------------------------------------

function renderKnobs(force) {
  if (!control) return;
  if (!force && (dragging || Date.now() - lastLocalEdit < 1500)) return;
  const set = (id, v) => { const el = $(id); if (document.activeElement !== el || force) el.value = v; };
  set("k-ops", Math.min(1000, control.ops_per_sec));
  set("k-ops-num", control.ops_per_sec);
  set("k-ws", Math.round(control.write_share * 100));
  set("k-hot", control.hot_keys);
  set("k-keys", control.key_space);
  set("k-probe", Math.round(control.probe_fraction * 100));
  $("k-ryw").checked = !!control.read_own_writes;
  updateKnobLabels();
  $("btn-pause").textContent = control.paused ? "Resume traffic" : "Pause traffic";
  $("btn-pause").classList.toggle("primary", !!control.paused);
  for (const b of $("k-topo").children) b.classList.toggle("on", b.dataset.topo === control.topology);
}

function updateKnobLabels() {
  $("v-ops").textContent = `${$("k-ops-num").value}/s`;
  $("v-ws").textContent = `${$("k-ws").value}%`;
  $("v-hot").textContent = +$("k-hot").value === 0 ? "uniform" : `${$("k-hot").value} keys`;
  $("v-probe").textContent = `${$("k-probe").value}%`;
}

function wireKnobs(topologies, presets) {
  const range = (id, onChange) => {
    const el = $(id);
    el.addEventListener("pointerdown", () => { dragging = true; });
    el.addEventListener("pointerup", () => { dragging = false; });
    el.addEventListener("input", () => { if (id === "k-ops") $("k-ops-num").value = el.value; updateKnobLabels(); });
    el.addEventListener("change", () => { dragging = false; onChange(+el.value); });
  };
  range("k-ops", (v) => setControl({ ops_per_sec: v }));
  range("k-ws", (v) => setControl({ write_share: v / 100 }));
  range("k-hot", (v) => setControl({ hot_keys: v }));
  range("k-probe", (v) => setControl({ probe_fraction: v / 100 }));
  $("k-ops-num").addEventListener("change", (e) => setControl({ ops_per_sec: Math.max(0, +e.target.value || 0) }));
  $("k-keys").addEventListener("change", (e) => setControl({ key_space: Math.max(1, +e.target.value || 1000) }));
  $("k-ryw").addEventListener("change", (e) => setControl({ read_own_writes: e.target.checked }));
  $("btn-pause").addEventListener("click", () => setControl({ paused: !control.paused }));

  $("k-topo").innerHTML = topologies.map((t) => `<button data-topo="${t}">${t}</button>`).join("");
  for (const b of $("k-topo").children) b.addEventListener("click", () => setControl({ topology: b.dataset.topo }));

  $("presets").innerHTML = presets.map((p) => `<button data-preset="${p.name}" title="${esc(p.hint)}">${esc(p.label)}</button>`).join("");
  for (const b of $("presets").children) {
    b.addEventListener("click", () => {
      lastLocalEdit = Date.now();
      post(`/api/preset/${b.dataset.preset}`).then((c) => { control = c; renderKnobs(true); showError(null); }).catch(showError);
    });
  }

  for (const b of $("span").children) {
    b.addEventListener("click", () => {
      spanSeconds = +b.dataset.span;
      for (const o of $("span").children) o.classList.toggle("on", o === b);
      renderCharts();
    });
  }

  $("btn-start").addEventListener("click", () => {
    post("/api/engine/start").then((s) => { engine = s; renderEngine(); showError(null); }).catch(showError);
  });
  $("btn-stop").addEventListener("click", () => {
    post("/api/engine/stop").then((s) => { engine = s; renderEngine(); }).catch(showError);
  });
}

function renderEngine() {
  const pill = $("engine-pill");
  pill.textContent = engine.status;
  pill.className = `pill ${engine.status}`;
  $("engine-msg").textContent = engine.message || "";
  const stopped = engine.status === "stopped" || engine.status === "disconnected";
  $("btn-start").disabled = !stopped || engine.status === "disconnected";
  $("btn-stop").disabled = stopped;
  if (engine.error) showError(`Engine: ${engine.error}`);
}

// --- live updates -------------------------------------------------------------------------------

function renderAll() {
  renderEngine();
  renderKnobs(false);
  renderTiles();
  renderCharts();
  renderBySetting();
  renderTopology();
  renderBudget();
  renderLog();
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.type === "init") history = d.history;
    else history.push(...d.history);
    if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
    snap = d.snapshot;
    engine = d.engine;
    control = d.control;
    renderAll();
    if (window.onEvalState) window.onEvalState(d.eval);
  };
  ws.onclose = () => {
    engine = { status: "disconnected", message: "Reconnecting to the dashboard server ..." };
    renderEngine();
    setTimeout(connect, 2000);
  };
}

function showTab(name) {
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("on", b.dataset.tab === name);
  $("tab-live").hidden = name !== "live";
  $("tab-eval").hidden = name !== "eval";
  $("live-controls").hidden = name !== "live";
  try { localStorage.setItem("dashboard-tab", name); } catch (e) { /* storage blocked: fine */ }
  window.dispatchEvent(new Event("resize"));
  if (name === "eval" && window.onEvalShown) window.onEvalShown();
}

function wireTabs() {
  for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => showTab(b.dataset.tab));
  let saved = "live";
  try { saved = localStorage.getItem("dashboard-tab") || "live"; } catch (e) { /* storage blocked */ }
  showTab(saved === "eval" ? "eval" : "live");
}

async function init() {
  const state = await fetch("/api/state").then((r) => r.json());
  control = state.control;
  engine = state.engine;
  wireKnobs(state.topologies, state.presets);
  buildCharts();
  for (const d of Object.values(defs)) resizer.observe($(d.el));
  renderKnobs(true);
  renderAll();
  wireTabs();
  connect();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { buildCharts(); renderAll(); });
}

init().catch(showError);
