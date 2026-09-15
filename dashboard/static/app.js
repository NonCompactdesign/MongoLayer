const POLL_INTERVAL_MS = 1000;

let config = null;
let lastSettings = null;

const $ = (id) => document.getElementById(id);

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function setConnIndicator(state) {
  const el = $("conn-indicator");
  el.className = "conn-indicator " + (state === "ok" ? "conn-ok" : state === "error" ? "conn-error" : "conn-unknown");
  el.textContent = state === "ok" ? "live" : state === "error" ? "connection lost" : "connecting…";
}

function flashIfChanged(elId, newValue, prevValue) {
  const el = $(elId);
  el.querySelector(".setting-value").textContent = newValue;
  if (prevValue !== null && prevValue !== newValue) {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1200);
  }
}

function renderSettings(settings) {
  const prev = lastSettings;
  flashIfChanged("setting-write-concern", String(settings.write_concern), prev ? String(prev.write_concern) : null);
  flashIfChanged("setting-read-concern", settings.read_concern, prev ? prev.read_concern : null);
  flashIfChanged("setting-read-preference", settings.read_preference, prev ? prev.read_preference : null);

  let mode = "MODERATE (default / balanced)";
  if (settings.write_concern === "majority" && settings.read_concern === "majority") {
    mode = "SAFE — hot/write-heavy pattern detected";
  } else if (settings.write_concern === 1 && settings.read_preference === "nearest") {
    mode = "FAST — cold/read-heavy pattern detected";
  }
  $("settings-mode").textContent = mode;

  lastSettings = settings;
}

function renderStats(stats) {
  $("stat-reads").textContent = stats.read_count;
  $("stat-writes").textContent = stats.write_count;
  $("stat-write-freq").textContent = stats.write_freq.toFixed(2) + "/s";
  $("stat-ratio").textContent = stats.read_write_ratio_is_infinite ? "∞" : stats.read_write_ratio.toFixed(2);

  if (config) {
    const writePct = Math.min(100, (stats.write_freq / config.high_write_threshold) * 100);
    $("write-freq-bar").style.width = writePct + "%";

    const ratioForBar = stats.read_write_ratio_is_infinite ? config.read_heavy_ratio_threshold * 1.5 : stats.read_write_ratio;
    const ratioPct = Math.min(100, (ratioForBar / config.read_heavy_ratio_threshold) * 100);
    $("ratio-bar").style.width = ratioPct + "%";
  }
}

function renderTraffic(traffic) {
  $("traffic-state").textContent = traffic.running ? "running" : "stopped";
  $("traffic-count").textContent = traffic.op_count;
}

function roleClass(role) {
  if (role === "PRIMARY") return "role-primary";
  if (role === "SECONDARY") return "role-secondary";
  return "role-unreachable";
}

function renderNodes(nodes) {
  const grid = $("nodes-grid");
  grid.innerHTML = "";
  for (const node of nodes) {
    const box = document.createElement("div");
    box.className = "node-box";
    if (!node.reachable) {
      box.innerHTML = `
        <div class="node-name">${node.name} <span class="role-badge role-unreachable">unreachable</span></div>
        <div class="node-stat">no response</div>`;
    } else {
      const oc = node.opcounters;
      box.innerHTML = `
        <div class="node-name">${node.name} <span class="role-badge ${roleClass(node.role)}">${node.role}</span></div>
        <div class="node-stat"><span>inserts</span><b>${oc.insert}</b></div>
        <div class="node-stat"><span>queries</span><b>${oc.query}</b></div>
        <div class="node-stat"><span>getmore</span><b>${oc.getmore}</b></div>`;
    }
    grid.appendChild(box);
  }
}

function renderFeed(ops) {
  const list = $("feed-list");
  list.innerHTML = "";
  for (const op of ops) {
    const item = document.createElement("div");
    item.className = "feed-item";
    const opClass = op.op_type === "write" ? "op-write" : "op-read";
    const time = new Date(op.timestamp * 1000).toLocaleTimeString();
    item.innerHTML = `<span><span class="${opClass}">${op.op_type.toUpperCase()}</span> ${op.collection}</span><span>${time}</span>`;
    list.appendChild(item);
  }
}

async function poll() {
  try {
    const status = await fetchJSON("/api/status");
    setConnIndicator("ok");
    renderSettings(status.active_settings);
    renderStats(status.monitor_stats);
    renderTraffic(status.traffic);
    renderNodes(status.nodes);
    renderFeed(status.recent_ops);
  } catch (err) {
    setConnIndicator("error");
    console.error(err);
  }
}

function wireControls() {
  const ratioSlider = $("ratio-slider");
  const rateSlider = $("rate-slider");

  function currentWriteRatio() {
    return Number(ratioSlider.value) / 100;
  }
  function currentRate() {
    return Number(rateSlider.value);
  }
  function updateLabels() {
    const writePct = Number(ratioSlider.value);
    $("ratio-write-pct").textContent = writePct + "%";
    $("ratio-read-pct").textContent = (100 - writePct) + "%";
    $("rate-value").textContent = currentRate().toFixed(1);
  }

  ratioSlider.addEventListener("input", () => {
    updateLabels();
    fetchJSON("/api/traffic/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ write_ratio: currentWriteRatio() }),
    }).catch(console.error);
  });

  rateSlider.addEventListener("input", () => {
    updateLabels();
    fetchJSON("/api/traffic/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ops_per_sec: currentRate() }),
    }).catch(console.error);
  });

  $("btn-start").addEventListener("click", () => {
    fetchJSON("/api/traffic/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ write_ratio: currentWriteRatio(), ops_per_sec: currentRate() }),
    }).catch(console.error);
  });

  $("btn-stop").addEventListener("click", () => {
    fetchJSON("/api/traffic/stop", { method: "POST" }).catch(console.error);
  });

  $("btn-reset").addEventListener("click", async () => {
    const btn = $("btn-reset");
    const originalText = btn.textContent;
    try {
      await fetchJSON("/api/reset", { method: "POST" });
      btn.textContent = "Reset ✓";
      await poll();
    } catch (err) {
      console.error(err);
      btn.textContent = "Reset failed";
    } finally {
      setTimeout(() => { btn.textContent = originalText; }, 1200);
    }
  });

  document.querySelectorAll(".btn-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      const ratio = Number(btn.dataset.ratio);
      const rate = Number(btn.dataset.rate);
      ratioSlider.value = ratio;
      rateSlider.value = rate;
      updateLabels();
      fetchJSON("/api/traffic/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ write_ratio: ratio / 100, ops_per_sec: rate }),
      }).catch(console.error);
    });
  });

  updateLabels();
}

async function init() {
  try {
    config = await fetchJSON("/api/config");
    $("high-write-label").textContent = `HIGH_WRITE_THRESHOLD (${config.high_write_threshold}/s)`;
    $("read-heavy-label").textContent = `READ_HEAVY_RATIO_THRESHOLD (${config.read_heavy_ratio_threshold})`;
  } catch (err) {
    console.error("failed to load config", err);
  }
  wireControls();
  poll();
  setInterval(poll, POLL_INTERVAL_MS);
}

init();
