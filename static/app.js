/* Torronto Web — SPA logic. Zero dependencies. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  mode: "manual",
  shadow: {},
  phase: null,
  plan: null,
  history: [],
  weather: [],
  range: 24,
  tempLocked: false,
};

/* ---------------- API client ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

/* ---------------- toasts ---------------- */
function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .4s";
    setTimeout(() => el.remove(), 400);
  }, 2600);
}

/* ---------------- confirm dialog ---------------- */
function confirmDialog(text) {
  return new Promise((resolve) => {
    const box = $("#confirm");
    $("#confirmText").textContent = text;
    box.classList.remove("hidden");
    $("#confirmOk").onclick = () => { box.classList.add("hidden"); resolve(true); };
    $("#confirmCancel").onclick = () => { box.classList.add("hidden"); resolve(false); };
  });
}

/* ---------------- gauge ---------------- */
const G_MIN = 10, G_MAX = 45, G_RANGE = 283; // arc length units
function gaugeArcPath() {
  // semicircular arc 20,120 -> 200,120 (radius 90)
  const T = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const g = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
  g.id = "gradArc";
  g.setAttribute("x1", "0"); g.setAttribute("y1", "0");
  g.setAttribute("x2", "1"); g.setAttribute("y2", "0");
  ["#2dd4a0", "#ffb86b", "#ff6b6b"].forEach((c, i) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    s.setAttribute("offset", (i * 50) + "%");
    s.setAttribute("stop-color", c);
    g.appendChild(s);
  });
  T.appendChild(g);
  $("#gauge").prepend(T);
}
function ticks() {
  const g = $("#gaugeTicks");
  g.innerHTML = "";
  for (let t = G_MIN; t <= G_MAX; t += 5) {
    const frac = (t - G_MIN) / (G_MAX - G_MIN);
    const a = Math.PI * (1 - frac);
    const x1 = 110 + 72 * Math.cos(a), y1 = 120 - 72 * Math.sin(a);
    const x2 = 110 + 82 * Math.cos(a), y2 = 120 - 82 * Math.sin(a);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    g.appendChild(line);
  }
}
function setGauge(temp) {
  const frac = Math.max(0, Math.min(1, (temp - G_MIN) / (G_MAX - G_MIN)));
  $("#gaugeArc").style.strokeDashoffset = String(G_RANGE * (1 - frac));
  const el = $("#gaugeTemp");
  el.textContent = (Math.round(temp * 10) / 10);
  el.classList.toggle("hot", temp >= 40);
  el.classList.toggle("warm", temp >= 30 && temp < 40);
}

/* ---------------- chart ---------------- */
function drawChart() {
  const cv = $("#chart");
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 46, r: 14, t: 16, b: 26 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const ser = state.history;
  if (!ser.length) {
    ctx.fillStyle = "#8b97b5";
    ctx.font = "13px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("collecting data…", W / 2, H / 2);
    return;
  }
  const now = ser[ser.length - 1].ts;
  const from = now - state.range * 3600;
  const pts = ser.filter((p) => p.ts >= from && p.water != null);
  if (!pts.length) return;

  const all = pts.map((p) => p.water).concat(pts.filter((p) => p.ambient != null).map((p) => p.ambient));
  let lo = Math.floor(Math.min.apply(null, all) - 2);
  let hi = Math.ceil(Math.max.apply(null, all) + 2);
  lo = Math.max(0, lo); hi = Math.min(45, hi);

  const X = (ts) => pad.l + iw * (1 - (now - ts) / (state.range * 3600));
  const Y = (t) => pad.t + ih * (1 - (t - lo) / (hi - lo));

  // grid
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "#8b97b5";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (let t = lo; t <= hi; t += Math.max(1, Math.round((hi - lo) / 5))) {
    ctx.beginPath(); ctx.moveTo(pad.l, Y(t)); ctx.lineTo(W - pad.r, Y(t)); ctx.stroke();
    ctx.fillText(t + "°", pad.l - 6, Y(t) + 4);
  }
  // time grid
  ctx.textAlign = "center";
  const step = Math.max(3600, Math.round(state.range * 3600 / 6 / 3600) * 3600);
  for (let t = now - (now % step); t >= from; t -= step) {
    ctx.beginPath(); ctx.moveTo(X(t), pad.t); ctx.lineTo(X(t), pad.t + ih); ctx.stroke();
    const d = new Date(t * 1000);
    ctx.fillText(d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0"), X(t), H - 8);
  }

  // target band
  const tgt = state.shadow.temperature_setting;
  if (tgt) {
    ctx.fillStyle = "rgba(124,92,255,0.10)";
    ctx.fillRect(pad.l, Y(tgt + 1), iw, Math.max(0, Y(tgt - 1) - Y(tgt + 1)));
    ctx.strokeStyle = "rgba(124,92,255,0.8)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, Y(tgt)); ctx.lineTo(W - pad.r, Y(tgt)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // heater-on bands
  const hpts = pts.filter((p) => p.heater);
  if (hpts.length) {
    ctx.fillStyle = "rgba(255,107,107,0.12)";
    let runStart = null, runEnd = null;
    const flush = () => {
      if (runStart == null) return;
      ctx.fillRect(X(runStart), pad.t, Math.max(2, X(runEnd) - X(runStart)), ih);
      runStart = runEnd = null;
    };
    hpts.forEach((p) => {
      if (runStart == null) { runStart = runEnd = p.ts; }
      else if (p.ts - runEnd <= 900) { runEnd = p.ts; }
      else flush(), (runStart = runEnd = p.ts);
    });
    flush();
  }

  // ambient line
  const amb = pts.filter((p) => p.ambient != null);
  if (amb.length > 1) {
    ctx.strokeStyle = "#ffb86b";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    amb.forEach((p, i) => (i ? ctx.lineTo(X(p.ts), Y(p.ambient)) : ctx.moveTo(X(p.ts), Y(p.ambient))));
    ctx.stroke();
  }

  // water line + fill
  ctx.strokeStyle = "#4fd1ff";
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(X(p.ts), Y(p.water)) : ctx.moveTo(X(p.ts), Y(p.water))));
  ctx.stroke();
  // gradient fill
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ih);
  grad.addColorStop(0, "rgba(79,209,255,0.22)");
  grad.addColorStop(1, "rgba(79,209,255,0)");
  ctx.lineTo(X(pts[pts.length - 1].ts), pad.t + ih);
  ctx.lineTo(X(pts[0].ts), pad.t + ih);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // hover crosshair
  cv.onmousemove = (e) => {
    const rect = cv.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const ts = now - (1 - (mx - pad.l) / iw) * state.range * 3600;
    const near = pts.reduce((a, b) => (Math.abs(b.ts - ts) < Math.abs(a.ts - ts) ? b : a));
    if (Math.abs(near.ts - ts) > 3600) return;
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(X(near.ts), pad.t); ctx.lineTo(X(near.ts), pad.t + ih); ctx.stroke();
    ctx.setLineDash([]);
    const d = new Date(near.ts * 1000);
    ctx.fillStyle = "rgba(16,24,48,0.9)";
    const tx = X(near.ts) + 10, ty = Y(near.water) - 8;
    const label = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      + "  " + near.water + "°C" + (near.ambient != null ? "  amb " + near.ambient + "°" : "");
    ctx.fillRect(tx - 4, ty - 14, ctx.measureText(label).width + 8, 20);
    ctx.fillStyle = "#e8edf7";
    ctx.textAlign = "left";
    ctx.fillText(label, tx, ty);
  };
}

/* ---------------- controls ---------------- */
function reflectShadow() {
  const s = state.shadow;
  $$(".tile input").forEach((inp) => {
    const dp = inp.dataset.dp;
    const on = !!(s[dp] && s[dp] !== "0");
    inp.checked = on;
    if (dp === "temperature_setting") return;
  });
  if (s.temperature_setting) {
    $("#tempRange").value = s.temperature_setting;
    $("#tempOut").textContent = s.temperature_setting;
    $("#targetBadge").textContent = "🎯 " + s.temperature_setting + "°C";
    $("#factTarget").textContent = s.temperature_setting + "°C";
  }
  if (s.water_temperature != null) setGauge(s.water_temperature);
  $("#factError").textContent = s.error_code || "none";
  $("#heaterBadge").className = "badge " + (s.heater_state ? "heater-on" : "heater-off");
  $("#heaterBadge").textContent = s.heater_state ? "🔥 heater" : "🔥 heater";
}

function syncMode() {
  const auto = state.mode === "auto";
  $("#modeToggle").checked = auto;
  $("#modeLabel").textContent = auto ? "AUTO" : "MANUAL";
  $("#factPhase").textContent = state.phase ? state.phase.toUpperCase() : "—";
  const locked = auto;
  state.tempLocked = locked;
  $("#tempRange").disabled = locked;
  $("#tempRange").style.opacity = locked ? 0.4 : 1;
  $("#autoHint").textContent = auto
    ? "Auto mode: the scheduler chooses temperature_setting for you. Switch to manual to take over."
    : "Manual mode: you are in control. Flip the switch to let the scheduler optimise heating.";
}

function refreshFacts() {
  if (state.plan) {
    $("#factAmb").textContent = (state.plan.ambient_now != null ? state.plan.ambient_now.toFixed(1) : "—") + "°";
    $("#factEnergy").textContent = state.plan.energy_kwh != null ? state.plan.energy_kwh.toFixed(1) + " kWh" : "—";
  }
  if (state.weather.length) {
    const lo = Math.min.apply(null, state.weather.map((w) => w.temp));
    const hi = Math.max.apply(null, state.weather.map((w) => w.temp));
    $("#factWeather").textContent = lo.toFixed(0) + "…" + hi.toFixed(0) + "°";
  }
}

function renderPlanStrip() {
  const strip = $("#planStrip");
  strip.innerHTML = "";
  if (!state.plan || !state.plan.plan_ts) return;
  const n = Math.min(state.plan.plan_ts.length, 24);
  const now = Date.now() / 1000;
  for (let i = 0; i < n; i++) {
    const b = document.createElement("div");
    b.className = "blk " + (state.plan.plan_us[i] ? "on" : "off");
    const ts = state.plan.plan_ts[i];
    if (ts <= now && now < ts + 3600) b.classList.add("now");
    const d = new Date(ts * 1000);
    b.title = (d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0"))
      + (state.plan.plan_us[i] ? " heater ON" : " off")
      + " · " + state.plan.plan_amb[i].toFixed(0) + "°C";
    strip.appendChild(b);
  }
}

function refreshConfig() {
  api("/api/scheduler").then((d) => {
    $("#cfgWindow").textContent = d.windows || "—";
    $("#cfgTemps").textContent = (d.target || "—") + " / " + (d.eco || "—") + " / " + (d.floor || "—");
    $("#cfgMargin").textContent = d.margin + "°";
    if (d.phase) { state.phase = d.phase; $("#factPhase").textContent = d.phase.toUpperCase(); }
  }).catch(() => {});
}

/* ---------------- data loading ---------------- */
async function loadState() {
  try {
    const d = await api("/api/state");
    state.mode = d.mode;
    state.shadow = d.state || {};
    if (d.ts) state.shadow._ts = d.ts;
    if (d.phase) state.phase = d.phase;
    syncMode();
    reflectShadow();
    if (!state.plan && d.phase) refreshFacts();
  } catch (e) {
    $("#conn").className = "pill offline";
    $("#conn").textContent = "offline";
  }
}

async function loadHistory() {
  try {
    const d = await api("/api/history?range=" + state.range);
    state.history = d.series || [];
    drawChart();
  } catch (e) { /* chart stays empty */ }
}

async function loadPlan() {
  try {
    state.plan = await api("/api/plan");
    renderPlanStrip();
    refreshFacts();
  } catch (e) { /* plan unavailable offline */ }
}

async function loadWeather() {
  try {
    const d = await api("/api/weather");
    state.weather = d.series || [];
    refreshFacts();
  } catch (e) { /* ignore */ }
}

async function refreshAll() {
  await loadState();
  await Promise.all([loadHistory(), loadPlan(), loadWeather(), refreshConfig()]);
  const el = $("#conn");
  if (state.shadow && Object.keys(state.shadow).length) {
    const age = (Date.now() / 1000) - (state.shadow._ts || 0);
    el.className = "pill online";
    el.textContent = "online · " + (state.shadow.temperature_setting ? "target " + state.shadow.temperature_setting + "°" : "synced");
  }
  if ($("#factPhase").textContent !== "—") $("#hero").classList.add("shimmer");
}

/* ---------------- actions ---------------- */
async function sendCmd(datapoint, value) {
  try {
    const r = await api("/api/cmd", {
      method: "POST",
      body: JSON.stringify({ datapoint, value }),
    });
    toast(datapoint + " = " + value + (r.confirmed ? " ✓" : " (sent, unconfirmed)"), r.confirmed ? "ok" : "err");
  } catch (e) {
    toast(e.message, "err");
    await loadState(); // revert optimistic UI
  }
}

async function toggleMode(auto) {
  if (auto && state.shadow.temperature_setting) {
    const ok = await confirmDialog("Switch to AUTO? The scheduler will take over heating (target "
      + state.shadow.temperature_setting + "°C until the next replan).");
    if (!ok) { $("#modeToggle").checked = false; return; }
  }
  try {
    await api("/api/scheduler", { method: "POST", body: JSON.stringify({ mode: auto ? "auto" : "manual" }) });
    state.mode = auto ? "auto" : "manual";
    toast(auto ? "Auto-heating ON — scheduler in control" : "Manual mode — you're in control");
    syncMode();
    loadPlan();
  } catch (e) {
    toast(e.message, "err");
    syncMode();
  }
}

function bindEvents() {
  $("#modeToggle").addEventListener("change", (e) => toggleMode(e.target.checked));
  $("#replanBtn").addEventListener("click", async () => {
    try {
      await api("/api/scheduler", { method: "POST", body: JSON.stringify({ replan: true }) });
      toast("Plan recomputed");
      loadPlan();
    } catch (e) { toast(e.message, "err"); }
  });

  $$(".tile input").forEach((inp) => {
    inp.addEventListener("change", () => {
      const dp = inp.dataset.dp;
      if (dp === "temperature_setting") return;
      sendCmd(dp, inp.checked ? 1 : 0);
    });
  });

  let tempTimer = null;
  $("#tempRange").addEventListener("input", (e) => {
    $("#tempOut").textContent = e.target.value;
    clearTimeout(tempTimer);
    tempTimer = setTimeout(() => {
      if (!state.tempLocked) sendCmd("temperature_setting", parseInt(e.target.value, 10));
    }, 350);
  });

  $("#rangePills").addEventListener("click", (e) => {
    const btn = e.target.closest(".pill-btn");
    if (!btn) return;
    $$(".pill-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.range = parseInt(btn.dataset.range, 10);
    loadHistory();
  });

  window.addEventListener("resize", () => drawChart());
}

/* ---------------- SSE ---------------- */
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      if (d.kind === "state" || d.kind === "mode") refreshAll();
      else if (d.kind === "plan") loadPlan();
    } catch (e) { /* ignore */ }
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 4000); // reconnect with backoff
  };
}

/* ---------------- boot ---------------- */
function boot() {
  gaugeArcPath();
  ticks();
  bindEvents();
  refreshAll();
  setInterval(refreshAll, 15000);
  connectSSE();
}
document.addEventListener("DOMContentLoaded", boot);
