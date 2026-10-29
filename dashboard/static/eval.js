"use strict";
// Evaluation tab: the plan form with a live time estimate, start / stop / resume, progress, the
// Locust run in progress (from its stats history), and results as each run finishes.
// Uses helpers from app.js ($, css, post, fmtMs, esc, axis, xAxis).

const CONFIG_VAR = { static_fast: "--fast", static_moderate: "--moderate", static_safe: "--safe", adaptive_signals: "--adaptive" };
const CONFIG_LABEL = { static_fast: "static FAST", static_moderate: "static MODERATE", static_safe: "static SAFE", adaptive_signals: "adaptive layer" };
const STALE_VAR = { ...CONFIG_VAR, adaptive_hot: "--adaptive", adaptive_cold: "--p99" };
const METRICS = {
  read_p50: { label: "read p50", unit: "ms", log: true },
  read_p95: { label: "read p95", unit: "ms", log: true },
  write_p50: { label: "write p50", unit: "ms", log: true },
  write_p95: { label: "write p95", unit: "ms", log: true },
  stale_pct: { label: "stale probes", unit: "%", log: false },
};
const CHECK_NAMES = { C1: "C1 distances (T1 / EQUAL-5)", C2: "C2 read-your-own-writes", C3: "C3 policy floor", C4: "C4 latency budget", C5: "C5 hysteresis" };

let evOpts = null;
let evState = null;
let evProfile = "FULL";
let evMetric = "read_p50";
let evResultsKey = "";
let evEstTimer = null;
const evCharts = {};

const fmtHM = (s) => {
  if (s == null) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
};
const title = (p) => p.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

// --- plan form ----------------------------------------------------------------------------------

function checkboxes(el, items, name) {
  $(el).innerHTML = items.map((it) => `<label><input type="checkbox" name="${name}" value="${esc(it.value)}">
    <span>${esc(it.label)}${it.hint ? ` <small>${esc(it.hint)}</small>` : ""}</span></label>`).join("");
}
const checked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
const setChecked = (name, values) => { for (const i of document.querySelectorAll(`input[name="${name}"]`)) i.checked = values.includes(i.value); };

function evPlan() {
  const fixed = $("ev-fixed-on").checked;
  return {
    setups: checked("ev-setup"),
    configs: checked("ev-config"),
    presets: checked("ev-preset"),
    duration_scale: +$("ev-scale").value,
    fixed_duration_s: fixed ? Math.max(10, +$("ev-fixed").value || 30) : null,
    repeats: Math.max(1, +$("ev-repeats").value || 1),
    run_checks: $("ev-checks").checked,
    staleness_setups: checked("ev-stale-setup"),
    staleness_trials: Math.max(1, +$("ev-trials").value || 200),
  };
}

function fillForm(plan) {
  setChecked("ev-setup", plan.setups);
  setChecked("ev-config", plan.configs);
  setChecked("ev-preset", plan.presets);
  $("ev-scale").value = plan.duration_scale;
  $("ev-fixed-on").checked = !!plan.fixed_duration_s;
  $("ev-fixed").value = plan.fixed_duration_s || 60;
  $("ev-repeats").value = plan.repeats;
  $("ev-checks").checked = plan.run_checks;
  setChecked("ev-stale-setup", plan.staleness_setups);
  $("ev-trials").value = plan.staleness_trials;
  formChanged(false);
}

function formChanged(custom = true) {
  if (custom) {
    evProfile = "CUSTOM";
    for (const b of $("ev-profiles").children) b.classList.remove("on");
  }
  $("ev-scale-v").textContent = `${Math.round(+$("ev-scale").value * 100)}%`;
  $("ev-fixed-wrap").hidden = !$("ev-fixed-on").checked;
  $("ev-scale-wrap").hidden = $("ev-fixed-on").checked;
  clearTimeout(evEstTimer);
  evEstTimer = setTimeout(refreshEstimate, 200);
}

async function refreshEstimate() {
  try {
    const e = await post("/api/eval/estimate", evPlan());
    $("ev-est-total").textContent = fmtHM(e.total);
    $("ev-est-detail").textContent = `${e.runs} load-test runs · finishes around ${e.finish} if started now`;
    $("ev-est-rows").innerHTML = e.rows.map((r) => `<tr><td>${esc(r.phase)}</td><td>${fmtHM(r.seconds)}</td></tr>`).join("")
      + `<tr><td class="muted">run lengths: ${Object.entries(e.lengths).map(([p, s]) => `${title(p)} ${s} s`).join(", ")}</td><td></td></tr>`;
  } catch (err) {
    $("ev-est-total").textContent = "–";
    $("ev-est-detail").textContent = err.message;
    $("ev-est-rows").innerHTML = "";
  }
}

function renderRuns(runs) {
  $("ev-runs").innerHTML = runs.length
    ? runs.map((r) => `<option value="${esc(r.tag)}">${esc(r.tag)} · ${r.done}/${r.planned} runs${r.finished ? " · finished" : ""}</option>`).join("")
    : `<option value="">(none yet)</option>`;
}

async function reloadOptions() {
  evOpts = await fetch("/api/eval/options").then((r) => r.json());
  renderRuns(evOpts.runs);
}

function wireEval() {
  checkboxes("ev-setups", evOpts.setups.map((s) => ({ value: s.name, label: s.name, hint: s.description })), "ev-setup");
  checkboxes("ev-configs", evOpts.configs.map((c) => ({ value: c, label: CONFIG_LABEL[c] || c })), "ev-config");
  checkboxes("ev-presets", evOpts.presets.map((p) => ({
    value: p.name, label: title(p.name), hint: `${p.users} users, ${p.duration}${evOpts.core_presets.includes(p.name) ? "" : " · extra"}`,
  })), "ev-preset");
  checkboxes("ev-stale-setups", evOpts.setups.map((s) => ({ value: s.name, label: s.name })), "ev-stale-setup");
  $("ev-profiles").innerHTML = Object.keys(evOpts.profiles).map((n) => `<button data-profile="${n}">${n}</button>`).join("");
  for (const b of $("ev-profiles").children) {
    b.addEventListener("click", () => {
      fillForm(evOpts.profiles[b.dataset.profile]);
      evProfile = b.dataset.profile;
      for (const o of $("ev-profiles").children) o.classList.toggle("on", o === b);
    });
  }
  for (const el of document.querySelectorAll("#tab-eval aside input")) {
    el.addEventListener(el.type === "range" ? "input" : "change", () => formChanged());
  }
  for (const b of $("ev-metric").children) {
    b.addEventListener("click", () => {
      evMetric = b.dataset.metric;
      for (const o of $("ev-metric").children) o.classList.toggle("on", o === b);
      evResultsKey = "";
      renderEval();
    });
  }
  $("ev-start").addEventListener("click", () => {
    const p = evPlan();
    post("/api/eval/start", { plan: { ...p, profile: evProfile } }).then(afterAction).catch(evError);
  });
  $("ev-stop").addEventListener("click", () => post("/api/eval/stop").then(afterAction).catch(evError));
  $("ev-view").addEventListener("click", () => {
    if ($("ev-runs").value) post("/api/eval/view", { tag: $("ev-runs").value }).then(afterAction).catch(evError);
  });
  $("ev-resume").addEventListener("click", () => {
    if ($("ev-runs").value) post("/api/eval/start", { tag: $("ev-runs").value }).then(afterAction).catch(evError);
  });
  const full = evOpts.profiles.FULL;
  fillForm(full);
  evProfile = "FULL";
  $("ev-profiles").querySelector('[data-profile="FULL"]').classList.add("on");
}

function afterAction(s) {
  evError(null);
  evResultsKey = "";
  window.onEvalState(s);
  reloadOptions().catch(() => {});
}

function evError(e) {
  $("ev-error").textContent = e ? String(e.message || e) : "";
  $("ev-error").hidden = !e;
}

// --- live charts of the run in progress ---------------------------------------------------------

function buildEvalCharts() {
  for (const k of Object.keys(evCharts)) { evCharts[k].destroy(); delete evCharts[k]; }
  const line = (label, v, extra = {}) => ({ label, stroke: css(v), width: 2, points: { show: false }, ...extra });
  const rps = (u, v) => (v == null ? "–" : `${Math.round(v)}/s`);
  const ms = (u, v) => fmtMs(v);
  const mk = (key, el, series, scaleY, axisY) => {
    const node = $(el);
    node.innerHTML = "";
    evCharts[key] = new uPlot({
      width: Math.max(200, node.clientWidth), height: 180, cursor: { points: { size: 8 } },
      scales: { x: { time: true }, y: scaleY }, axes: [xAxis(), axisY], series: [{}, ...series],
    }, [[], ...series.map(() => [])], node);
  };
  mk("rps", "ev-c-rps", [line("total", "--text", { value: rps }), line("reads", "--reads", { value: rps }), line("writes", "--writes", { value: rps })],
    { range: (u, min, max) => [0, Math.max(10, (max || 0) * 1.15)] }, axis({ size: 44 }));
  mk("lat", "ev-c-lat", [
    line("read p50", "--reads", { value: ms }), line("read p95", "--reads", { value: ms, dash: [5, 4] }),
    line("write p50", "--writes", { value: ms }), line("write p95", "--writes", { value: ms, dash: [5, 4] }),
  ], { distr: 3, log: 10, range: (u, min, max) => uPlot.rangeLog(Math.max(0.5, min ?? 1), Math.max(max ?? 10, (min ?? 1) * 10), 10, true) },
  axis({ size: 50 }));
}

function renderCurrent(cur) {
  if (!evCharts.rps) return;
  const pts = cur ? cur.points : [];
  const xs = pts.map((p) => p.t);
  const col = (k) => pts.map((p) => (p[k] == null || p[k] === 0 && k.includes("p") ? null : p[k]));
  evCharts.rps.setData([xs, pts.map((p) => p.rps ?? null), pts.map((p) => p.read_rps ?? null), pts.map((p) => p.write_rps ?? null)]);
  evCharts.lat.setData([xs, col("read_p50"), col("read_p95"), col("write_p50"), col("write_p95")]);
  $("ev-cur-label").textContent = cur
    ? `${cur.setup} · ${CONFIG_LABEL[cur.config] || cur.config} · ${title(cur.preset)} · ${cur.users} users · ${Math.round(cur.elapsed)} / ${cur.length} s`
    : "none right now";
}

for (const el of ["ev-c-rps", "ev-c-lat"]) {
  new ResizeObserver(() => {
    const key = el === "ev-c-rps" ? "rps" : "lat";
    const w = Math.max(200, $(el).clientWidth);
    if (evCharts[key] && evCharts[key].width !== w) evCharts[key].setSize({ width: w, height: 180 });
  }).observe($(el));
}

// --- result charts (SVG) ------------------------------------------------------------------------

function groupedBars(cats, series, value, { log = false, unit = "", colorVar }) {
  const W = 760, H = 220, L = 48, R = 8, T = 18, B = 30;
  const vals = [];
  for (const c of cats) for (const s of series) { const v = value(c, s); if (v != null && !Number.isNaN(v)) vals.push(v); }
  if (!vals.length) return `<p class="muted">No finished runs yet.</p>`;
  let lo = 0, hi = Math.max(...vals, unit === "%" ? 5 : 1) * 1.12;
  let y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  let ticks = [0, hi / 4, hi / 2, (3 * hi) / 4, hi].map((v) => Math.round(v * 10) / 10);
  if (log) {
    const pos = vals.filter((v) => v > 0);
    lo = 10 ** Math.floor(Math.log10(Math.min(...pos, 1)));
    hi = 10 ** Math.ceil(Math.log10(Math.max(...pos, 10)));
    y = (v) => T + (H - T - B) * (1 - (Math.log10(Math.max(v, lo)) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo)));
    ticks = [];
    for (let t = lo; t <= hi; t *= 10) ticks.push(t);
  }
  const gw = (W - L - R) / cats.length;
  const bw = Math.min(26, (gw * 0.78) / series.length);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img">`;
  for (const t of ticks) {
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`;
    svg += `<text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${t}${unit === "%" ? "%" : ""}</text>`;
  }
  cats.forEach((c, i) => {
    const x0 = L + i * gw + (gw - bw * series.length) / 2;
    series.forEach((s, j) => {
      const v = value(c, s);
      const x = x0 + j * bw;
      if (v == null || Number.isNaN(v)) return;
      const top = y(v), base = y(log ? lo : 0);
      const h = Math.max(1, base - top);
      svg += `<rect x="${x + 1}" y="${base - h}" width="${bw - 2}" height="${h}" rx="3" fill="var(${colorVar(s)})"><title>${esc(title(c))} · ${esc(CONFIG_LABEL[s] || s)}: ${v}${unit}</title></rect>`;
      if (bw >= 14) svg += `<text class="val" x="${x + bw / 2}" y="${base - h - 4}" text-anchor="middle">${v < 10 ? Math.round(v * 10) / 10 : Math.round(v)}</text>`;
    });
    svg += `<text x="${L + i * gw + gw / 2}" y="${H - 10}" text-anchor="middle">${esc(title(c))}</text>`;
  });
  return svg + "</svg>";
}

function legend(configs, varMap) {
  return configs.map((c) => `<span><i class="sw" style="background:var(${varMap[c]})"></i>${esc(CONFIG_LABEL[c] || c)}</span>`).join("");
}

function renderResults(s) {
  const rows = s.results || [];
  const key = `${s.tag}|${rows.length}|${evMetric}|${document.documentElement.dataset.theme || ""}`;
  if (key === evResultsKey) return;
  evResultsKey = key;
  const plan = s.plan;
  const m = METRICS[evMetric];
  $("ev-legend").innerHTML = legend(plan.configs, CONFIG_VAR);
  const setups = [...new Set(rows.map((r) => r.topology))];
  const descr = Object.fromEntries((evOpts ? evOpts.setups : []).map((x) => [x.name.replace(/-0$/, "-0ms").replace(/-5$/, "-5ms"), x.description]));
  $("ev-bars").innerHTML = setups.length ? setups.map((setup) => {
    const cell = (p, c) => {
      const r = rows.find((x) => x.topology === setup && x.preset === p && x.config === c);
      return r ? r[evMetric] : null;
    };
    return `<div class="ev-setup"><h3>${esc(setup)} · ${m.label} (${m.unit}${m.log ? ", log" : ""})</h3>
      <p>${esc(descr[setup] || "")}</p>${groupedBars(plan.presets, plan.configs, cell, { log: m.log, unit: m.unit === "%" ? "%" : "", colorVar: (c) => CONFIG_VAR[c] || "--muted" })}</div>`;
  }).join("") : `<p class="muted">Results appear here as each run finishes.</p>`;

  const ad = rows.filter((r) => r.config === "adaptive_signals");
  $("ev-settings").innerHTML = ad.length ? settingsBars(ad) : `<p class="muted">No adaptive-layer runs finished yet.</p>`;

  $("ev-table").innerHTML = rows.length ? rows.map((r) => {
    const f = (v) => (v == null ? "–" : Math.round(v));
    const mix = r.FAST_pct == null ? "–" : `${Math.round(r.FAST_pct)} / ${Math.round(r.MODERATE_pct)} / ${Math.round(r.SAFE_pct)}`;
    return `<tr><td>${esc(r.topology)}</td><td><i class="sw" style="background:var(${CONFIG_VAR[r.config] || "--muted"});margin-left:0"></i>${esc(CONFIG_LABEL[r.config] || r.config)}</td>
      <td>${esc(title(r.preset))}</td><td>${r.requests.toLocaleString()}</td><td>${r.failures}</td>
      <td>${f(r.read_p50)} / ${f(r.read_p95)}</td><td>${f(r.write_p50)} / ${f(r.write_p95)}</td>
      <td>${r.probes ? `${r.stale}/${r.probes} (${r.stale_pct}%)` : "–"}</td><td>${mix}</td><td>${r.switches ?? "–"}</td></tr>`;
  }).join("") : `<tr><td colspan="10" class="muted">No finished runs yet.</td></tr>`;
}

function settingsBars(ad) {
  const W = 760, rowH = 22, L = 230;
  const items = ad.map((r) => ({ label: `${r.topology} · ${title(r.preset)}`, parts: [["FAST", r.FAST_pct], ["MODERATE", r.MODERATE_pct], ["SAFE", r.SAFE_pct]] }));
  let svg = `<svg viewBox="0 0 ${W} ${items.length * rowH + 24}" role="img">`;
  items.forEach((it, i) => {
    const yy = i * rowH + 4;
    svg += `<text x="${L - 8}" y="${yy + 14}" text-anchor="end">${esc(it.label)}</text>`;
    let x = L;
    for (const [name, pct] of it.parts) {
      const w = ((W - L - 10) * (pct || 0)) / 100;
      if (w > 0) svg += `<rect x="${x}" y="${yy + 2}" width="${Math.max(0, w - 2)}" height="${rowH - 6}" rx="3" fill="var(${LEVEL_VAR[name]})"><title>${name} ${Math.round(pct)}%</title></rect>`;
      if (w > 34) svg += `<text x="${x + w / 2}" y="${yy + 14}" text-anchor="middle" style="fill:#fff">${Math.round(pct)}%</text>`;
      x += w;
    }
  });
  svg += `<text x="${L}" y="${items.length * rowH + 20}">% of run time in FAST / MODERATE / SAFE</text>`;
  return svg + "</svg>";
}

function renderChecks(s) {
  const pre = (s.preflight || []).map((p) => `<div class="row"><span class="${p.ok ? "ok-b" : "fail-b"}">${p.ok ? "OK" : "FAIL"}</span><span>${esc(p.check)} <small class="muted">${esc(p.detail)}</small></span></div>`);
  const ran = Object.fromEntries((s.checks || []).map((c) => [c.name, c]));
  const checks = (s.plan && s.plan.run_checks ? Object.keys(CHECK_NAMES) : Object.keys(ran)).map((k) => {
    const c = ran[k];
    const badge = !c ? `<span class="wait-b">–</span>` : `<span class="${c.pass ? "ok-b" : "fail-b"}">${c.pass ? "PASS" : "FAIL"}</span>`;
    return `<div class="row">${badge}<span>${CHECK_NAMES[k] || k}${c && c.seconds ? ` <small class="muted">${Math.round(c.seconds)} s</small>` : ""}</span></div>`;
  });
  $("ev-checks-list").innerHTML = pre.concat(checks).join("") || `<p class="muted">Not started.</p>`;
}

function renderStaleness(s) {
  const st = s.analysis && s.analysis.staleness;
  if (st && st.length) {
    const setups = [...new Set(st.map((r) => r.setup))];
    const configs = [...new Set(st.map((r) => r.config))];
    const v = (setup, c) => { const r = st.find((x) => x.setup === setup && x.config === c); return r ? Math.round(r.stale_pct * 10) / 10 : null; };
    $("ev-stale").innerHTML = `<div class="ev-legend">${configs.map((c) => `<span><i class="sw" style="background:var(${STALE_VAR[c] || "--muted"})"></i>${esc(c)}</span>`).join("")}</div>`
      + groupedBars(setups, configs, v, { unit: "%", colorVar: (c) => STALE_VAR[c] || "--muted" });
  } else {
    const done = s.staleness_done || [];
    const planned = (s.plan && s.plan.staleness_setups) || [];
    $("ev-stale").innerHTML = planned.length
      ? `<p class="muted">${planned.map((p) => `${p}: ${done.includes(p) ? "done" : "pending"}`).join(" · ")}. The chart appears after the analysis.</p>`
      : `<p class="muted">Not in this plan.</p>`;
  }
}

function renderAnalysis(s) {
  const a = s.analysis;
  $("ev-analysis-card").hidden = !a;
  if (!a) return;
  const badge = (ok) => (ok === true ? `<span class="ok-b">YES</span>` : ok === false ? `<span class="fail-b">NO</span>` : `<span class="wait-b">not in run</span>`);
  const list = (items) => `<div class="analysis-list">${items.map((i) => `<div class="row">${badge(i.ok)}<span>${esc(i.item)} <small class="muted">${esc(i.detail)}</small></span></div>`).join("")}</div>`;
  $("ev-analysis").innerHTML = `<div class="sub-h">Integrity of the run</div>${list(a.integrity)}<div class="sub-h">Expectations (fixed before the run)</div>${list(a.expectations)}`;
  const figKey = `${s.tag}|${a.figures.join(",")}`;
  if ($("ev-figs").dataset.key !== figKey) {
    $("ev-figs").dataset.key = figKey;
    $("ev-figs").innerHTML = a.figures.map((f) => `<a href="/api/eval/figure/${encodeURIComponent(s.tag)}/${f}" target="_blank" rel="noopener"><img src="/api/eval/figure/${encodeURIComponent(s.tag)}/${f}?v=${Date.now()}" alt="${esc(f)}"></a>`).join("");
  }
}

// --- state ----------------------------------------------------------------------------------------

function renderEval() {
  const s = evState;
  if (!s || !evOpts) return;
  const running = s.status === "running" || s.status === "stopping";
  $("eval-tab-pill").hidden = !running;
  $("eval-tab-pill").textContent = s.status === "stopping" ? "stopping" : "running";
  $("ev-start").disabled = running;
  $("ev-resume").disabled = running;
  $("ev-view").disabled = running;
  $("ev-stop").disabled = !running;
  for (const el of document.querySelectorAll("#tab-eval aside input, #tab-eval aside button")) el.disabled = running;
  $("ev-status").textContent = s.status;
  $("ev-tag").textContent = s.tag || "no run selected";
  if (s.error) evError(s.error);
  $("ev-log").textContent = (s.lines || []).join("\n");
  if (!s.plan) return;
  const cur = s.current;
  $("ev-phase").textContent = s.phase || (running ? "…" : "–");
  $("ev-phase-d").textContent = cur ? `${cur.setup} · ${CONFIG_LABEL[cur.config] || cur.config} · ${title(cur.preset)}` : "";
  $("ev-done").textContent = `${s.runs_done} / ${s.runs_total}`;
  $("ev-done-d").textContent = "load-test runs";
  $("ev-elapsed").textContent = fmtHM(s.elapsed);
  $("ev-remaining").textContent = running ? fmtHM(s.remaining) : "–";
  $("ev-finish").textContent = running && s.finish ? `finishes around ${s.finish}` : "";
  const partial = cur ? Math.min(1, cur.elapsed / Math.max(1, cur.length)) : 0;
  $("ev-bar").style.width = `${(100 * (s.runs_done + partial)) / Math.max(1, s.runs_total)}%`;
  renderCurrent(cur);
  renderChecks(s);
  renderStaleness(s);
  renderResults(s);
  renderAnalysis(s);
}

let evLastStatus = null;
window.onEvalState = (s) => {
  if (!s) return;
  if (evLastStatus && evLastStatus !== s.status && !["running", "stopping"].includes(s.status)) reloadOptions().catch(() => {});
  evLastStatus = s.status;
  evState = s;
  renderEval();
};

window.onEvalShown = () => {
  if (!evCharts.rps) buildEvalCharts();
  renderEval();
};

(async () => {
  try {
    await reloadOptions();
    wireEval();
    if (!$("tab-eval").hidden) buildEvalCharts();
    renderEval();
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (evCharts.rps) buildEvalCharts(); evResultsKey = ""; renderEval(); });
  } catch (e) {
    evError(e);
  }
})();
