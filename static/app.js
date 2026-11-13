/* Torronto Web — SPA logic. Zero dependencies. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];
const DP_LABELS = {
  power_state: "Power",
  heater_state: "Heat",
  filter_state: "Filter",
  wave_state: "Bubbles",
  hydrojet_state: "Jet",
  locked: "Panel lock",
  temperature_setting: "Target temperature",
};

const state = {
  mode: "manual",
  shadow: {},
  phase: null,
  plan: null,
  history: [],
  weather: [],
  config: null,
  log: [],
  range: 24,
  fcDays: 3,
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fmtTime(ts) {
  if (!ts) return "--:--:--";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function describeValue(dp, value) {
  if (dp === "temperature_setting") return value + "°C";
  return Number(value) ? "ON" : "OFF";
}

/* A read-back uses richer values than the write (e.g. filter_state=2,
 * heater_state=4..8), so compare truthiness for switches. */
function isReflected(dp, shadow, value) {
  const got = shadow ? shadow[dp] : undefined;
  if (got === undefined || got === null || got === "") return false;
  if (dp === "temperature_setting") return Number(got) === Number(value);
  return Boolean(Number(got)) === Boolean(Number(value));
}

async function waitForReflection(dp, value, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const d = await api("/api/state");
      state.mode = d.mode;
      state.shadow = d.state || {};
      if (d.ts) state.shadow._ts = d.ts;
      if (d.phase) state.phase = d.phase;
      syncMode();
      reflectShadow();
      if (isReflected(dp, state.shadow, value)) return true;
    } catch (e) { /* keep waiting through transient errors */ }
    await sleep(1000);
  }
  return false;
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

/* ---------------- command progress modal ---------------- */
let opCloseTimer = null;

function openOpModal(title, detail) {
  clearTimeout(opCloseTimer);
  $("#opModal").classList.remove("hidden");
  $("#opSpinner").classList.remove("hidden");
  $("#opIcon").classList.add("hidden");
  $("#opActions").classList.add("hidden");
  $("#opCard").className = "modal-card glass op-card";
  $("#opTitle").textContent = title;
  $("#opDetail").textContent = detail || "";
}

function finishOpModal(kind, title, detail, autoCloseMs = 0) {
  clearTimeout(opCloseTimer);
  const icon = { ok: "✓", warn: "!", err: "✕" }[kind] || "•";
  $("#opSpinner").classList.add("hidden");
  $("#opIcon").className = "op-icon " + kind;
  $("#opIcon").textContent = icon;
  $("#opCard").className = "modal-card glass op-card " + kind;
  $("#opTitle").textContent = title;
  $("#opDetail").textContent = detail || "";
  $("#opActions").classList.remove("hidden");
  if (autoCloseMs) {
    opCloseTimer = setTimeout(() => $("#opModal").classList.add("hidden"), autoCloseMs);
  }
}

function closeOpModal() {
  clearTimeout(opCloseTimer);
  $("#opModal").classList.add("hidden");
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

/* ---------------- forecast & heating plan ---------------- */
function _dayKey(ts) {
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate();
}

function renderDayCards() {
  const host = $("#dayCards");
  host.innerHTML = "";
  const ser = state.weather || [];
  if (ser.length < 2) {
    host.innerHTML = '<p class="hint">Forecast unavailable.</p>';
    return;
  }
  const byDay = new Map();
  for (const w of ser) {
    const k = _dayKey(w.ts);
    if (!byDay.has(k)) byDay.set(k, { ts: w.ts, temps: [] });
    if (w.temp != null) byDay.get(k).temps.push(w.temp);
  }
  // two passes: compute a global lo/hi so every day's bar shares one scale
  const dayStats = [];
  let gLo = Infinity, gHi = -Infinity;
  for (const { ts, temps } of byDay.values()) {
    if (!temps.length) continue;
    const lo = Math.min.apply(null, temps);
    const hi = Math.max.apply(null, temps);
    dayStats.push({ ts, lo, hi });
    gLo = Math.min(gLo, lo); gHi = Math.max(gHi, hi);
  }
  const range = Math.max(1, gHi - gLo);
  const DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const todayKey = _dayKey(Date.now() / 1000);
  let first = true;
  for (const { ts, lo, hi } of dayStats) {
    const d = new Date(ts * 1000);
    const name = _dayKey(ts) === todayKey ? "Today" : DAYS_SHORT[d.getDay()];
    const left = Math.max(0, Math.min(100, (lo - gLo) / range * 100));
    const width = Math.max(8, (hi - lo) / range * 100);
    const card = document.createElement("div");
    card.className = "day-card" + (first ? " today" : "");
    first = false;
    card.innerHTML =
      '<span class="day-name">' + name + "</span>" +
      '<span class="day-date">' + d.getDate() + "/" + (d.getMonth() + 1) + "</span>" +
      '<span class="day-bar"><i style="left:' + left + "%;width:" + width + '%"></i></span>' +
      '<span class="day-temp"><b>' + hi.toFixed(0) + '°</b><span class="day-lo">' + lo.toFixed(0) + "°</span></span>";
    host.appendChild(card);
  }
}

function drawForecastChart() {
  const cv = $("#fcChart");
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 46, r: 14, t: 16, b: 26 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const weather = state.weather || [];
  const plan = state.plan;
  if (!weather.length || !plan || !plan.plan_ts) {
    ctx.fillStyle = "#8b97b5";
    ctx.font = "13px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("planning…", W / 2, H / 2);
    return;
  }
  const t0 = weather[0].ts;
  const t1 = weather[weather.length - 1].ts;
  const span = Math.max(1, t1 - t0);
  const all = weather.filter((w) => w.temp != null).map((w) => w.temp)
    .concat(plan.plan_water || []).concat(plan.plan_amb || []);
  let lo = Math.floor(Math.min.apply(null, all) - 2);
  let hi = Math.ceil(Math.max.apply(null, all) + 2);
  lo = Math.max(-20, lo); hi = Math.min(45, hi);

  const X = (ts) => pad.l + iw * (ts - t0) / span;
  const Y = (t) => pad.t + ih * (1 - (t - lo) / (hi - lo));
  const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // grid
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "#8b97b5";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (let t = lo; t <= hi; t += Math.max(1, Math.round((hi - lo) / 5))) {
    ctx.beginPath(); ctx.moveTo(pad.l, Y(t)); ctx.lineTo(W - pad.r, Y(t)); ctx.stroke();
    ctx.fillText(t + "°", pad.l - 6, Y(t) + 4);
  }
  // time grid + day separators
  ctx.textAlign = "center";
  for (let ts = t0 - (t0 % 86400) - 86400; ts <= t1 + 86400; ts += 86400) {
    if (ts <= t0 || ts > t1) continue;
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.beginPath(); ctx.moveTo(X(ts), pad.t); ctx.lineTo(X(ts), pad.t + ih); ctx.stroke();
    const d = new Date(ts * 1000);
    ctx.fillStyle = "#aab6d4";
    ctx.fillText(DAY_NAMES[d.getDay()] + " " + d.getDate(), X(ts), H - 8);
  }

  const plan_ts = plan.plan_ts;
  const plan_phase = plan.plan_phase || [];
  const plan_us = plan.plan_us || [];
  const plan_set = plan.plan_set || [];
  const plan_water = plan.plan_water || [];

  // window / preheat shading
  plan_phase.forEach((p, i) => {
    if (p === "eco") return;
    const x0 = X(plan_ts[i]), x1 = X(plan_ts[i] + 3600);
    ctx.fillStyle = p === "window" ? "rgba(124,92,255,0.16)" : "rgba(255,184,107,0.10)";
    ctx.fillRect(x0, pad.t, Math.max(1, x1 - x0), ih);
  });

  // target/eco reference lines
  if (plan_set.length) {
    const tgt = plan_set[0];
    ctx.strokeStyle = "rgba(124,92,255,0.8)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, Y(tgt)); ctx.lineTo(W - pad.r, Y(tgt)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // heater-on bands
  if (plan_us.length) {
    ctx.fillStyle = "rgba(255,107,107,0.14)";
    for (let i = 0; i < plan_us.length; i++) {
      if (!plan_us[i]) continue;
      const x0 = X(plan_ts[i]), x1 = X(plan_ts[i] + 3600);
      ctx.fillRect(x0, pad.t, Math.max(1, x1 - x0), ih);
    }
  }

  // ambient forecast line (full span)
  const ambPts = weather.filter((w) => w.temp != null);
  if (ambPts.length > 1) {
    ctx.strokeStyle = "#ffb86b";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ambPts.forEach((p, i) => (i ? ctx.lineTo(X(p.ts), Y(p.temp)) : ctx.moveTo(X(p.ts), Y(p.temp))));
    ctx.stroke();
  }

  // projected water line (plan horizon)
  if (plan_water.length && plan_water.length >= plan_ts.length) {
    ctx.strokeStyle = "#4fd1ff";
    ctx.lineWidth = 2.5;
    ctx.lineJoin = "round";
    ctx.beginPath();
    plan_water.forEach((v, i) => (i ? ctx.lineTo(X(plan_ts[i]), Y(v)) : ctx.moveTo(X(plan_ts[i]), Y(v))));
    ctx.stroke();
  }
}

function renderForecast() {
  renderDayCards();
  drawForecastChart();
}

function setFcDays(days) {
  state.fcDays = days;
  $$("#fcPills .pill-btn").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.fcDays) === days);
  });
  loadWeather().then(renderForecast);
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
  renderHeat(s);
}

// Semantic heater state. Prefer the server-computed `heater`; fall back to
// the protocol codes if the payload predates it.
function heaterLabel(shadow, status) {
  if (status) return status;
  let heat = Number(shadow.heater_state) || 0;
  const power = shadow.power_state === 1 || shadow.power_state === "1";
  const water = shadow.water_temperature, sp = shadow.temperature_setting;
  if (!power || heat === 0) return "off";
  if (heat >= 7) return "frost-watch";
  if (heat <= 3) return "heating";
  if (typeof water === "number" && typeof sp === "number") {
    if (water < sp - 0.5) return "heating";
    if (water > sp + 0.5) return "cooling";
  }
  return "holding";
}

function renderHeat(shadow) {
  const status = heaterLabel(shadow, state.heater);
  const badge = $("#heaterBadge");
  const label = {
    heating: ["🔥 heating", "heater-on"],
    cooling: ["❄ cooling", "heater-cool"],
    holding: ["🔥 holding", "heater-hold"],
    "frost-watch": ["🧊 frost watch", "heater-frost"],
    idle: ["heat on", "heater-hold"],
    off: ["heat off", "heater-off"],
  }[status] || ["heat", "heater-off"];
  badge.className = "badge " + label[1];
  badge.textContent = label[0];
  $("#factHeat").textContent = status.replace("-", " ");
  $("#factHeat").className = "fact-v status-" + status;
}

function syncMode() {
  const auto = state.mode === "auto";
  $("#modeToggle").checked = auto;
  $("#modeLabel").textContent = auto ? "AUTO" : "MANUAL";
  $("#factPhase").textContent = state.phase ? state.phase.toUpperCase() : "—";

  // Scheduler-owned controls are locked in AUTO. Bubbles/jet/lock stay live.
  state.tempLocked = auto;
  $("#tempRange").disabled = auto;
  $("#tempCtl").classList.toggle("auto-locked", auto);
  $("#autoLockBanner").classList.toggle("hidden", !auto);
  $$('.tile input[data-owned="scheduler"]').forEach((inp) => {
    inp.disabled = auto;
    const tile = inp.closest(".tile");
    tile.classList.toggle("auto-locked", auto);
    tile.title = auto ? "Locked: the scheduler controls this in AUTO mode" : "";
  });

  $("#autoHint").textContent = auto
    ? "Auto mode: the scheduler chooses power, heat, filter and target temperature. Switch to manual to take over."
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
  const axis = $("#planAxis");
  strip.innerHTML = "";
  axis.innerHTML = "";
  if (!state.plan || !state.plan.plan_ts) return;
  const n = Math.min(state.plan.plan_ts.length, 24);
  const now = Date.now() / 1000;
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const first = state.plan.plan_ts[0];
  for (let i = 0; i < n; i++) {
    const ts = state.plan.plan_ts[i];
    const d = new Date(ts * 1000);
    const hh = d.getHours();
    // heater block
    const b = document.createElement("div");
    b.className = "blk " + (state.plan.plan_us[i] ? "on" : "off");
    if (ts <= now && now < ts + 3600) b.classList.add("now");
    b.title = (String(hh).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"))
      + (state.plan.plan_us[i] ? " heater ON" : " heater off")
      + (state.plan.plan_amb && state.plan.plan_amb[i] != null
         ? " · " + state.plan.plan_amb[i].toFixed(0) + "°C" : "");
    strip.appendChild(b);
    // time axis label (every 3 h, plus day rollover at 00:00)
    const a = document.createElement("div");
    a.className = "axis-cell";
    if (i === 0 || hh % 3 === 0) {
      const t = String(hh).padStart(2, "0") + ":00";
      a.textContent = (hh === 0 && i !== 0) ? (DAYS[d.getDay()] + " " + t) : t;
    }
    axis.appendChild(a);
  }
  // clear "now" marker spanning the strip at the current time
  const marker = document.createElement("i");
  marker.className = "now-marker";
  if (first && now >= first && now < first + n * 3600) {
    const frac = (now - first) / (n * 3600);
    const pitch = (strip.clientWidth + 3) / n;      // block + 3px gap per hour
    const idx = Math.min(n - 1, Math.floor(frac * n));
    const within = (frac * n) - idx;                // position inside the hour
    const x = idx * pitch + within * (pitch - 3);
    marker.innerHTML = "<b>now</b>";
    marker.style.left = Math.round(x) + "px";
  } else {
    marker.style.display = "none";
  }
  strip.appendChild(marker);
}

function renderConfig() {
  const d = state.config || {};
  $("#cfgWindow").textContent = d.windows || "—";
  $("#cfgTemps").textContent = (d.target != null ? d.target : "—") + " / "
    + (d.eco != null ? d.eco : "—") + " / " + (d.floor != null ? d.floor : "—");
  $("#cfgMargin").textContent = (d.margin != null ? d.margin : "—") + "°";
  const ov = d.overrides || {};
  const active = DAYS.filter((day) => ov[day]).map((day) => day + " " + ov[day]);
  $("#cfgOverrides").textContent = active.length ? active.join(", ") : "none";
  const loc = (d.location || "").trim();
  const lnk = $("#cfgLocation");
  lnk.textContent = loc || "—";
  if (d.lat != null && d.lon != null && loc) {
    lnk.href = "https://maps.google.com/?q=" + Number(d.lat) + "," + Number(d.lon);
    lnk.title = "Open " + loc + " in Google Maps";
  } else {
    lnk.href = "#";
    lnk.title = loc || "no location set";
  }
  const m = d.model || {};
  $("#cfgModel").textContent = `UA ${m.ua} · P ${m.p} kW`
    + (m.samples ? ` · ${m.samples} samples` : "");
  const src = (m.source || "default");
  $("#cfgModelSrc").textContent = src;
  $("#cfgModelSrc").className = "model-badge " + (src === "default" ? "def" : "fitted");
  if (d.phase) {
    state.phase = d.phase;
    $("#factPhase").textContent = d.phase.toUpperCase();
  }
}

function refreshConfig() {
  return api("/api/scheduler").then((d) => {
    state.config = d;
    renderConfig();
  }).catch(() => {});
}

/* ---------------- heating settings modal ---------------- */
function windowError(spec, label) {
  const m = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/.exec(spec);
  if (!m) return label + " must use HH:MM-HH:MM";
  const sh = Number(m[1]), sm = Number(m[2]), eh = Number(m[3]), em = Number(m[4]);
  if (sh > 23 || eh > 23 || sm > 59 || em > 59) return label + " must be between 00:00 and 23:59";
  if (eh * 60 + em === sh * 60 + sm) return label + " start and end must differ";
  // end < start is valid: it crosses midnight, e.g. 22:00-01:00.
  return null;
}

function openSettings() {
  const c = state.config || {};
  const ov = c.overrides || {};
  $("#setWindow").value = c.windows || "17:00-23:00";
  $("#setTarget").value = c.target != null ? c.target : 40;
  $("#setEco").value = c.eco != null ? c.eco : 28;
  $("#setFloor").value = c.floor != null ? c.floor : 8;
  $("#setMargin").value = c.margin != null ? c.margin : 0;
  // Location field: reflect current location; keep pinned coords in the payload.
  $("#setLocation").value = c.location || "";
  state._locPicked = c.lat != null && c.lon != null ? { lat: c.lat, lon: c.lon } : null;
  hideLocSuggest();
  DAYS.forEach((day) => { $("#setWin" + day).value = ov[day] || ""; });
  $("#settingsError").textContent = "";
  $("#settingsModal").classList.remove("hidden");
  setTimeout(() => $("#setWindow").focus(), 30);
}

function closeSettings() {
  $("#settingsModal").classList.add("hidden");
  $("#settingsError").textContent = "";
}

function validateSettings(payload) {
  const err = windowError(payload.window, "Default window");
  if (err) return err;
  const raw = (v) => String(v == null ? "" : v).trim();
  const target = Number(raw(payload.target));
  const eco = Number(raw(payload.eco));
  const floor = Number(raw(payload.floor));
  const margin = Number(raw(payload.margin));
  if (!raw(payload.target) || !Number.isInteger(target) || target < 20 || target > 40) return "Target must be a whole number from 20 to 40";
  if (!raw(payload.eco) || !Number.isInteger(eco) || eco < 5 || eco > 40) return "Eco must be a whole number from 5 to 40";
  if (!raw(payload.floor) || !Number.isInteger(floor) || floor < 0 || floor > 40) return "Floor must be a whole number from 0 to 40";
  if (!raw(payload.margin) || !Number.isFinite(margin) || margin < 0 || margin > 10) return "Margin must be between 0 and 10";
  if (!(floor <= eco && eco <= target)) return "Temperatures must satisfy floor ≤ eco ≤ target";
  for (const day of DAYS) {
    const spec = payload.overrides[day];
    if (!spec) continue;
    const e = windowError(spec, day + " override");
    if (e) return e;
  }
  return null;
}

async function saveSettings(e) {
  e.preventDefault();
  const payload = {
    window: $("#setWindow").value.trim(),
    target: $("#setTarget").value,
    eco: $("#setEco").value,
    floor: $("#setFloor").value,
    margin: $("#setMargin").value,
    overrides: {},
  };
  DAYS.forEach((day) => { payload.overrides[day] = $("#setWin" + day).value.trim(); });
  // Location: include only if the user changed/typed something.
  const locText = $("#setLocation").value.trim();
  if (locText) {
    payload.location = state._locPicked ? state._locPicked.label || locText : locText;
    if (state._locPicked) {
      payload.lat = state._locPicked.lat;
      payload.lon = state._locPicked.lon;
    }
  }
  hideLocSuggest();

  const problem = validateSettings(payload);
  if (problem) {
    $("#settingsError").textContent = problem;
    return;
  }

  const btn = $("#settingsSave");
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  $("#settingsError").textContent = "";
  try {
    const d = await api("/api/scheduler", {
      method: "POST",
      body: JSON.stringify({ config: payload }),
    });
    state.config = d;
    renderConfig();
    closeSettings();
    if (d.plan_error) {
      toast("Settings saved — replan failed: " + d.plan_error, "err");
    } else {
      toast("Heating settings saved", "ok");
    }
    loadPlan();
    loadLog();
  } catch (err) {
    $("#settingsError").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

/* ---------------- location type-ahead ---------------- */
let _locTimer = null;

function hideLocSuggest() {
  $("#locSuggest").classList.add("hidden");
  $("#locSuggest").innerHTML = "";
}

async function searchLocations(query) {
  const q = String(query || "").trim();
  if (q.length < 2) { hideLocSuggest(); return; }
  try {
    const d = await api("/api/location/search?q=" + encodeURIComponent(q));
    const results = d.results || [];
    const box = $("#locSuggest");
    box.innerHTML = "";
    if (!results.length) {
      box.classList.add("hidden");
      return;
    }
    results.forEach((r) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.textContent = r.label;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        state._locPicked = { label: r.label, lat: r.lat, lon: r.lon };
        $("#setLocation").value = r.label;
        hideLocSuggest();
      });
      box.appendChild(li);
    });
    box.classList.remove("hidden");
  } catch (err) {
    hideLocSuggest();
  }
}

function onLocationInput() {
  state._locPicked = null;                 // typed text -> free-form geocode
  clearTimeout(_locTimer);
  const q = $("#setLocation").value;
  if (q.trim().length < 2) { hideLocSuggest(); return; }
  _locTimer = setTimeout(() => searchLocations(q), 220);
}

function bindSettings() {
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#settingsCancel").addEventListener("click", closeSettings);
  $("#settingsForm").addEventListener("submit", saveSettings);
  const loc = $("#setLocation");
  if (loc) {
    loc.addEventListener("input", onLocationInput);
    loc.addEventListener("blur", () => setTimeout(hideLocSuggest, 150));
    loc.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hideLocSuggest();
      if (e.key === "Enter" && !$("#locSuggest").classList.contains("hidden")) {
        const first = $("#locSuggest").querySelector("li");
        if (first) first.click();
      }
    });
  }
  $("#locClear").addEventListener("click", () => {
    $("#setLocation").value = "";
    state._locPicked = null;
    hideLocSuggest();
  });
}

/* ---------------- operation log ---------------- */
function renderLog() {
  const host = $("#opLog");
  host.innerHTML = "";
  if (!state.log.length) {
    const p = document.createElement("p");
    p.className = "log-empty";
    p.textContent = "No operations yet.";
    host.appendChild(p);
    $("#logCount").textContent = "";
    return;
  }
  const entries = state.log.slice().reverse();
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "log-item " + (entry.level || "info");
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = fmtTime(entry.ts);
    const dot = document.createElement("i");
    dot.className = "log-dot";
    const msg = document.createElement("span");
    msg.className = "log-msg";
    msg.textContent = entry.msg;
    row.append(time, dot, msg);
    host.appendChild(row);
  }
  $("#logCount").textContent = state.log.length + " events";
}

async function loadLog() {
  try {
    const d = await api("/api/log?limit=120");
    state.log = d.entries || [];
    renderLog();
  } catch (e) { /* keep the last known log */ }
}

/* ---------------- data loading ---------------- */
async function loadState() {
  try {
    const d = await api("/api/state");
    state.mode = d.mode;
    state.shadow = d.state || {};
    state.heater = d.heater;
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
    renderForecast();
  } catch (e) { /* plan unavailable offline */ }
}

async function loadWeather() {
  try {
    const d = await api("/api/weather?days=" + state.fcDays);
    state.weather = d.series || [];
    refreshFacts();
    renderForecast();
  } catch (e) { /* ignore */ }
}

async function refreshAll() {
  await loadState();
  await Promise.all([loadHistory(), loadPlan(), loadWeather(), refreshConfig(), loadLog()]);
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
  const label = DP_LABELS[datapoint] || datapoint;
  const shown = describeValue(datapoint, value);
  openOpModal("Sending " + label + "…", "Waiting for the spa to accept the command.");

  try {
    const r = await api("/api/cmd", {
      method: "POST",
      body: JSON.stringify({ datapoint, value }),
    });
    $("#opDetail").textContent = r.confirmed
      ? "Accepted — waiting for the status feed to reflect it."
      : "Sent — waiting for the spa to report the change.";

    const reflected = await waitForReflection(datapoint, value,
      r.confirmed ? 12000 : 15000);

    if (reflected) {
      finishOpModal("ok", label + " → " + shown,
        "Confirmed by the spa status feed.", 1200);
      toast(label + " → " + shown, "ok");
    } else if (r.confirmed) {
      finishOpModal("ok", label + " accepted",
        "The spa accepted the command; the status feed will catch up shortly.");
      toast(label + " accepted (status not yet updated)", "ok");
    } else {
      finishOpModal("warn", label + " not reflected yet",
        "The command was sent, but the spa has not reported it. Check the connection or try again.");
      toast(label + " sent, not yet reflected", "err");
    }
    await loadState();
    loadLog();
  } catch (e) {
    finishOpModal("err", label + " failed", e.message);
    toast(e.message, "err");
    await loadState(); // revert optimistic UI
    loadLog();
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
    refreshConfig();
    loadLog();
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
      loadLog();
    } catch (e) { toast(e.message, "err"); }
  });

  $("#settingsBtn").addEventListener("click", openSettings);
  $("#settingsCancel").addEventListener("click", closeSettings);
  $("#settingsForm").addEventListener("submit", saveSettings);
  bindSettings();
  $("#opClose").addEventListener("click", closeOpModal);
  $$("#fcPills .pill-btn").forEach((b) => {
    b.addEventListener("click", () => setFcDays(Number(b.dataset.fcDays)));
  });
  $("#settingsModal").addEventListener("click", (e) => {
    if (e.target.id === "settingsModal") closeSettings();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#settingsModal").classList.contains("hidden")) {
      closeSettings();
    } else if (!$("#opModal").classList.contains("hidden")
               && !$("#opActions").classList.contains("hidden")) {
      closeOpModal();
    }
  });

  $$(".tile input").forEach((inp) => {
    inp.addEventListener("change", () => {
      const dp = inp.dataset.dp;
      if (dp === "temperature_setting") return;
      if (inp.disabled) { reflectShadow(); return; }
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

  window.addEventListener("resize", () => { drawChart(); renderForecast(); });
}

/* ---------------- SSE ---------------- */
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      if (d.kind === "state") loadState();
      else if (d.kind === "mode") refreshAll();
      else if (d.kind === "plan") { loadPlan(); refreshConfig(); }
      else if (d.kind === "config") { refreshConfig(); loadPlan(); }
      else if (d.kind === "log") loadLog();
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
