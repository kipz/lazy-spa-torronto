#!/usr/bin/env python3
"""
Torronto Web — control & visualisation server for the Lay-Z-Spa.

Zero-dependency (stdlib only) HTTP server that exposes:
  * live spa state + temperature history (JSONL append)
  * full control API (power/heat/filter/wave/jet/lock/temp)
  * auto-heating mode (drives scheduler.py's optimal-start policy)
  * weather + heating-plan preview for the SPA

Serves the single-page app from ./static and a JSON API under /api.

Config is read from web.env (gitignored); see web.env.example.
Defaults: BIND=0.0.0.0, PORT=8080, MODE=manual (auto-heat is opt-in).

Security note: this server controls a real device with no authentication.
It is intended for a trusted LAN only. Do not expose it to the internet.
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
POLL_SECONDS = 15.0
PLUG_POLL_SECONDS = 15.0           # smart-plug (Tuya cloud) poll cadence
HISTORY_SECONDS = 7 * 86400        # keep ~1 week of history in memory
PLUG_HISTORY_SECONDS = 7 * 86400   # keep ~1 week of plug power samples
PLUG_LOG_FILE = "plug_log.jsonl"   # durable plug power samples (gitignored)
PLUG_LOG_MAX = 60000               # ~10 days at the 15 s poll cadence
SHADOW_LOG_SECONDS = 300           # append one shadow snapshot to JSONL per 5 min
LOG_MAX = 300                      # operation log entries kept in memory
OPERATION_LOG_FILE = "operation_log.jsonl"   # durable operation log (gitignored)
LOG_FILE_MAX = 3000                # prune the durable log to this many entries
# A shadow snapshot older than this is treated as stale (poller cadence is
# 15 s, so anything beyond a few minutes means we are no longer getting data).
STATE_STALE_SECONDS = 5 * 60
# A computed plan older than this is flagged stale in cost/status surfaces.
PLAN_STALE_AGE_S = 3 * 3600
# Re-fetch the Open-Meteo forecast at most this often (the plan itself is
# re-evaluated every poll against live water temperature using the cache).
FORECAST_REFRESH_S = 3600
# Throttle repeated spa-poll errors in the operation feed (log ~1 in N).
POLL_ERR_LOG_EVERY = 20
# Minimum spacing between automatic Bestway re-logins (the user opted into
# this; it invalidates the phone app's session).
REL_LOGIN_COOLDOWN_S = 600
# When a login succeeds but Bestway returns no spa binding (the tub is offline
# on their side, e.g. a wifi/network change), each re-login rotates the account
# session for nothing — and can invalidate the phone app session the user needs
# to reconnect the spa. Back off to this long interval instead.
REL_LOGIN_BACKOFF_S = 3600
# Minimum wall-clock span of plug meter history (h) before a 7-day measured
# number (projected cost / saving) is trusted. With less data the window is
# dominated by a restart/outage and would mislead.
MEASURED_MIN_HOURS = 24
# Online cooling-rate (UA) estimate from real heater-off coast-downs. A 1 h
# span is enough to rise above the spa's 1 C temperature resolution, and a
# "held steady" span (no measurable drop) is still used as an upper-bound
# sample so a very well-insulated tub is learned, not ignored.
UA_MIN_SAMPLES = 2                  # samples needed before the model is updated
UA_SAMPLE_MIN_S = 3600               # 1 h between the two observation points
UA_SAMPLE_MAX_S = 4 * 3600         # discard longer gaps (weather changed too much)
UA_SAMPLE_MAX_N = 50               # rolling median window
UA_BOUNDS = (0.005, 0.3)           # kW/degC, sane physical range
UA_AMBIENT_MIN_DIFF = 3.0          # need a clear water/ambient difference
UA_SETPOINT_MARGIN = 1.0           # only fit well above setpoint (thermostat idle)
# Ongoing register telemetry (probe-offset investigation). The full raw
# B*/P*/W* shadow is also persisted to shadow_log.jsonl every 5 min; this just
# surfaces a rolling probe-vs-water summary in the operation feed.
REG_PROBES = ("B1", "B2", "B9")
REG_MON_INTERVAL_S = 3600          # how often to log a register summary
REG_MAX_N = 288                    # rolling offset samples (~1 day of 5-min logs)
REG_MIN_N = 6                      # samples before an offset baseline is trusted
REG_ANOM_DELTA = 0.6               # probe offset shift (degC) that flags an anomaly
NOTIFY_COOLDOWN_S = 3600           # Telegram "target reached" at most once/hr
DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
DP_LOG_LABELS = {
    "power_state": "Power", "heater_state": "Heat", "filter_state": "Filter",
    "wave_state": "Bubbles", "hydrojet_state": "Jet", "locked": "Panel lock",
    "temperature_setting": "Target temperature",
}

import scheduler  # noqa: E402
import spa  # noqa: E402
import notify  # noqa: E402
import plug  # noqa: E402


def heater_status(state):
    """Semantic heater state for the UI.

    heater_state read semantics (see PROTOCOL.md): 0 off, 1 starting,
    2-3 heating, 4-6 holding temperature, 7-8 winter/frost mode. A nonzero
    value alone doesn't mean heat is being applied (e.g. eco coast-down shows
    heater_state 4 while the tub cools), so cross-check against power and
    water-vs-setpoint. Returns one of off|idle|heating|holding|cooling|frost].
    """
    if not state:
        return "off"
    if not spa.is_online(state):
        return "offline"   # shadow says offline / cloud unreachable
    try:
        heat = int(state.get("heater_state") or 0)
    except (TypeError, ValueError):
        heat = 0
    if state.get("power_state") not in (1, "1"):
        return "off"
    if heat in (7, 8):
        return "frost-watch"
    water = state.get("water_temperature")
    sp = state.get("temperature_setting")
    has = isinstance(water, (int, float)) and isinstance(sp, (int, float))
    if heat in (1, 2, 3):
        return "heating"
    if heat == 0:
        if has and water > float(sp) + 0.5:
            return "cooling"
        return "idle"
    # heater_state 4..6: thermostat holding — decide via water vs setpoint.
    if has:
        if water < float(sp) - 0.5:
            return "heating"     # reheat pull toward setpoint
        if water > float(sp) + 0.5:
            return "cooling"     # satisfied, coasting down to setpoint
    return "holding"


def validate_window(spec):
    """Validate/normalise an HH:MM-HH:MM use window."""
    spec = str(spec or "").strip()
    m = WINDOW_RE.match(spec)
    if not m:
        raise ValueError("window must be HH:MM-HH:MM (got %r)" % spec)
    sh, sm, eh, em = (int(x) for x in m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise ValueError("window times must be 00:00-23:59")
    if eh * 60 + em == sh * 60 + sm:
        raise ValueError("window start and end must differ")
    # An end earlier than the start is a valid overnight window.
    return "%02d:%02d-%02d:%02d" % (sh, sm, eh, em)


def _int_setting(name, value, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % name)
    if not (lo <= n <= hi):
        raise ValueError("%s must be between %d and %d" % (name, lo, hi))
    return n


def _float_setting(name, value, lo, hi):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)
    if not (lo <= n <= hi):
        raise ValueError("%s must be between %g and %g" % (name, lo, hi))
    return n


def cost_overview(cfg, plan, monthly_days=30, stale=False):
    """Estimated cost + saving vs 'always-on', using the configured price.

    `monthly_days` projects the plan-horizon figures to a rolling month.
    `stale` marks the figures as derived from an old plan / offline tub, so
    surfaces can show them as estimates rather than live truth.
    """
    price = float(cfg.get("PRICE_PER_KWH") or 0.0)
    if not plan:
        return {"price_per_kwh": price, "stale": True}
    plan_kwh = plan.get("energy_kwh")
    if not isinstance(plan_kwh, (int, float)):
        return {"price_per_kwh": price, "stale": True}
    days = len(plan.get("plan_ts") or []) / 24.0
    factor = monthly_days / max(1.0, days)
    c = {"price_per_kwh": price, "plan_kwh": plan_kwh, "cost_plan": plan_kwh * price,
         "days": round(days, 1), "cost_monthly": plan_kwh * price * factor,
         "stale": bool(stale)}
    alwayson = plan.get("energy_alwayson_kwh")
    if isinstance(alwayson, (int, float)):
        cost_always = alwayson * price
        saving_kwh = max(0.0, alwayson - plan_kwh)
        saving_cost = max(0.0, cost_always - plan_kwh * price)
        c.update({
            "alwayson_kwh": alwayson, "cost_alwayson": cost_always,
            "saving_kwh": saving_kwh, "saving_cost": saving_cost,
            "saving_pct": (saving_cost / cost_always * 100.0) if cost_always > 0 else 0.0,
            "alwayson_monthly": cost_always * factor,
            "saving_monthly": saving_cost * factor,
        })
    return c


def scheduler_payload(app):
    cfg = app.cfg.scheduler_cfg
    return {
        "mode": app.cfg.mode,
        "windows": cfg["WINDOW"],
        "overrides": {day: cfg.get("WINDOW_" + day) or "" for day in DAY_NAMES},
        "target": int(cfg["TARGET_TEMP"]),
        "eco": int(cfg["ECO_TEMP"]),
        "floor": int(cfg["FLOOR_TEMP"]),
        "margin": float(cfg["MARGIN"]),
        "price_per_kwh": float(cfg.get("PRICE_PER_KWH") or 0.0),
        "cost": cost_overview(cfg, app._plan, stale=app._plan_stale()),
        "measured_cost": app._measured_cost_payload(),
        "measured_kwh_24h": round(app.plug_kwh_window(24), 3),
        "measured_kwh_7d": round(app.plug_kwh_window(168), 3),
        "tub_online": app.tub_online(),
        "state_age_s": app.state_age_s(),
        "location": cfg["LOCATION"],
        "lat": cfg["LAT"] if cfg["LAT"] is not None else app._lat,
        "lon": cfg["LON"] if cfg["LON"] is not None else app._lon,
        "model": scheduler.model_info(cfg),
        "phase": app._plan["phase"] if app._plan else None,
        "setting": app._plan["setting"] if app._plan else None,
    }


class WebConfig:
    """Thin wrapper over web.env with mode + window state."""
    DEFAULTS = {
        "BIND": "0.0.0.0",
        "PORT": 8080,
        "MODE": "manual",           # auto | manual (scheduler enabled?)
        "HISTORY_H": 48,
    }

    def __init__(self):
        env = scheduler.load_env(os.path.join(BASE_DIR, "web.env"))
        self.bind = env.get("BIND", self.DEFAULTS["BIND"])
        self.mode = env.get("MODE", self.DEFAULTS["MODE"])
        try:
            self.port = int(env.get("PORT", self.DEFAULTS["PORT"]))
        except (TypeError, ValueError):
            self.port = self.DEFAULTS["PORT"]
        try:
            self.history_h = int(env.get("HISTORY_H", self.DEFAULTS["HISTORY_H"]))
        except (TypeError, ValueError):
            self.history_h = self.DEFAULTS["HISTORY_H"]
        self.mode = "auto" if str(self.mode).lower() == "auto" else "manual"
        # Smart-plug (Tuya cloud) integration.
        self.plug_device_id = env.get("TUYA_DEVICE_ID", "").strip()
        self.plug_session = env.get("TUYA_SESSION", "").strip()
        try:
            self.plug_poll = float(env.get("PLUG_POLL", PLUG_POLL_SECONDS))
        except (TypeError, ValueError):
            self.plug_poll = PLUG_POLL_SECONDS
        # scheduler config (shared with scheduler.env values where present)
        self.scheduler_cfg = scheduler.load_config()


class SpaServer:
    """Stateful bridge between HTTP handlers and the spa/scheduler."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._state = {}            # latest raw shadow
        self._state_ts = 0.0
        self._history = []          # [{ts, water, target, heater, ambient?}]
        self._lock = threading.Lock()
        self._sse_clients = set()
        self._last_shadow_log = 0.0
        self._ua_prev = None        # anchor sample for the cooling-rate estimate
        self._ua_samples = []       # recent coast-down UA estimates (kW/degC)
        self._amb = {}              # ts -> forecast ambient, kept across plans
        self._plan = None           # latest scheduler_step() result
        self._notify_at = 0.0       # last Telegram "target reached" send
        self._prev_water = None     # previous water temp (rising-edge detect)
        self._reg_weather = None    # cached historical temps for register chart
        self._reg_weather_ts = 0.0
        self._reg_weather_pd = None
        self._plan_ts = 0.0
        self._tokens = None
        self._stopping = False
        self._tub_online_prev = None     # last observed tub online state
        self._poll_err = 0               # consecutive poll failures (log throttle)
        self._notified_auth = False      # one-shot Telegram "tokens expired" alert
        self._last_relogin = 0.0        # throttle automatic Bestway re-logins
        self._forecast_weather = None   # cached hourly forecast (plan input)
        self._forecast_ts = 0.0         # when the forecast cache was filled
        self._log = []              # recent operations, newest last
        self._log_lock = threading.Lock()
        # smart plug (Tuya cloud) state
        self._plugs = {}             # device_id -> latest normalised reading
        self._plug_prev_state = {}   # device_id -> previous switch_on (notify)
        self._plug_hist = []         # [{ts, watts, on, fault}] power samples
        self._plug_kwh_today = 0.0   # integrated energy since local midnight
        self._plug_kwh_day = None    # 'YYYY-MM-DD' the accumulator covers
        self._plug_prev_ts = 0.0     # wall-clock of the previous successful read
        self._plug_next = 0.0        # when to poll the plug next
        self._plug_err = 0           # consecutive plug failures (log throttle)
        # ongoing register telemetry (probe-offset investigation)
        self._reg_bias = {k: [] for k in REG_PROBES}   # rolling offset samples
        self._reg_offsets = {}       # last computed probe offsets vs water
        self._reg_mon_ts = 0.0       # last register-summary log time
        # resolve location once (from scheduler.env: LOCATION or LAT/LON)
        try:
            lat, lon, where = scheduler.resolve_location(cfg.scheduler_cfg)
        except SystemExit:
            lat, lon, where = 51.5074, -0.1278, "London, GB (default)"
        self._lat, self._lon, self._where = lat, lon, where
        self.add_log("info", "Server started in %s mode (window %s, target %d°C)"
                     % (cfg.mode, cfg.scheduler_cfg["WINDOW"],
                        int(cfg.scheduler_cfg["TARGET_TEMP"])))
        self._telegram_poller()  # two-way bot commands if telegram.env set
        self._seed_history_from_log()  # don't wipe the history view on restart
        self._seed_plug_history_from_log()  # measured 7-day power survives restarts
        if plug.configured():
            self.add_log("info",
                         "Smart plug (Tuya cloud) enabled — integrating power usage")

    # -- state -----------------------------------------------------------
    def update_state(self, state):
        with self._lock:
            self._state = state or {}
            self._state_ts = time.time()
            now = int(time.time())
            self._history.append({
                "ts": now,
                "water": state.get("water_temperature"),
                "target": state.get("temperature_setting"),
                "heater": state.get("heater_state"),
                "power": state.get("power_state"),
                "ambient": None,
            })
            # ambient is backfilled from the cached forecast
            if self._plan and self._plan.get("plan_ts"):
                amb_map = dict(zip(self._plan["plan_ts"], self._plan["plan_amb"]))
                for h in self._history[-5:]:
                    if h["ambient"] is None:
                        h["ambient"] = amb_map.get(h["ts"])
            self._history = [h for h in self._history
                             if h["ts"] > now - HISTORY_SECONDS]
        self._maybe_log_shadow(state)
        self._maybe_sample_ua(state)
        self._maybe_notify(state)
        self._monitor_registers(state)
        self._track_tub_online(state)
        self._broadcast("state")

    def get_state(self):
        with self._lock:
            return dict(self._state), self._state_ts

    def state_age_s(self):
        """Age of the last shadow snapshot in whole seconds, or None."""
        return int(time.time() - self._state_ts) if self._state_ts else None

    def tub_online(self, state=None, ts=None):
        """A tub is 'live' only when the shadow is fresh *and* the device
        itself reports online. Never call a 3-day-old snapshot 'online'."""
        if state is None:
            state = self._state
        if ts is None:
            ts = self._state_ts
        if not state or not spa.is_online(state):
            return False
        if ts and time.time() - ts > STATE_STALE_SECONDS:
            return False
        return True

    def _plan_stale(self):
        """True when the cached plan is too old to trust for cost figures."""
        if self._plan is None:
            return True
        if not self._plan_ts or time.time() - self._plan_ts > PLAN_STALE_AGE_S:
            return True
        return not self.tub_online()

    def _track_tub_online(self, state):
        """Detect online<->offline transitions: log them and (once only per
        transition) ping Telegram so the user is told the tub fell off the
        cloud or came back. Never raises."""
        online = self.tub_online(state, int(time.time()))
        prev = self._tub_online_prev
        self._tub_online_prev = online
        if prev is None or online == prev:
            return
        if online:
            self.add_log("ok", "Tub back online")
            msg = ("<b>Torronto</b> \U0001F30A\u00b7 the tub is <b>back online</b>.")
        else:
            self.add_log("warn",
                         "Tub OFFLINE (device reports offline / cloud unreachable) "
                         "\u2014 heating plan held, no commands sent")
            msg = ("<b>Torronto</b> \u26a0\u00b7 the tub went <b>offline</b> \u2014 "
                   "the cloud last heard from it a few minutes ago. Commands and "
                   "auto-heating are paused until it reconnects.")
        if notify.configured():
            ok, err = notify.send_text(msg)
            self.add_log("ok" if ok else "warn",
                         "Telegram offline-alert %s (%s)"
                         % ("sent" if ok else "failed: " + err,
                            "online" if online else "offline"))
        self._broadcast("log")

    def get_history(self, hours):
        cutoff = int(time.time()) - int(hours * 3600)
        with self._lock:
            return [dict(h) for h in self._history if h["ts"] >= cutoff]

    # -- smart plug (Tuya cloud power meter) --------------------------------
    def plug_enabled(self):
        return plug.configured()

    def update_plug(self, reading):
        """Record a fresh plug reading; integrate kWh + notify on change.

        ``reading`` is one normalised device dict from ``plug.read_all()``.
        kWh is only integrated for the energy-meter plug ("Tub mains").
        """
        if not reading:
            return
        now = time.time()
        self._roll_plug_day(now)  # outside the lock
        reading = dict(reading)
        reading["ts"] = int(now)
        dev_id = reading.get("device_id")
        self._maybe_notify_plug_change(reading)  # outside the lock
        with self._lock:
            if dev_id:
                self._plugs[dev_id] = reading
            if (plug.is_energy_device(reading.get("device"))
                    and isinstance(reading.get("watts"), (int, float))):
                sample = {
                    "ts": int(now),
                    "watts": reading.get("watts"),
                    "on": bool(reading.get("switch_on")),
                    "fault": reading.get("fault"),
                }
                if self._plug_prev_ts:
                    dt_h = (now - self._plug_prev_ts) / 3600.0
                    # Ignore huge gaps (server was down) to avoid phantom kWh.
                    if 0 < dt_h <= 1.0:
                        self._plug_kwh_today += reading["watts"] / 1000.0 * dt_h
                self._plug_hist.append(sample)
                self._plug_hist = [h for h in self._plug_hist
                                   if h["ts"] > now - PLUG_HISTORY_SECONDS]
                self._append_plug_log(sample)
            self._plug_prev_ts = now
        self._broadcast("plug")

    def _append_plug_log(self, sample):
        """Persist one power sample so 7-day measured kWh survives restarts."""
        path = os.path.join(BASE_DIR, PLUG_LOG_FILE)
        try:
            with open(path, "a") as f:
                f.write(json.dumps(sample, separators=(",", ":")) + "\n")
        except OSError:
            return
        # Bound the file so it can't grow forever (~10 days of 15 s polls).
        try:
            if os.path.getsize(path) < (1 << 22):   # < 4 MB
                return
            with open(path) as f:
                lines = [ln for ln in f if ln.strip()]
            if len(lines) <= PLUG_LOG_MAX:
                return
            with open(path, "w") as f:
                for ln in lines[-PLUG_LOG_MAX:]:
                    f.write(ln)
        except OSError:
            pass

    def _seed_plug_history_from_log(self):
        """Reload measured power samples from plug_log.jsonl so the 24 h / 7 d
        windows and sparkline are continuous across restarts."""
        path = os.path.join(BASE_DIR, PLUG_LOG_FILE)
        if not os.path.exists(path):
            return
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            return
        if not rows:
            return
        rows.sort(key=lambda r: r["ts"])
        cutoff = int(time.time()) - PLUG_HISTORY_SECONDS
        hist = [r for r in rows if r["ts"] >= cutoff]
        if hist:
            with self._lock:
                if not self._plug_hist:
                    self._plug_hist = hist
                # continue the kWh integration from the last persisted sample
                self._plug_prev_ts = float(hist[-1]["ts"])

    def _maybe_notify_plug_change(self, reading):
        """Send a Telegram alert when a plug relay toggles on/off."""
        dev_id = reading.get("device_id")
        name = reading.get("device")
        on = bool(reading.get("switch_on"))
        prev = self._plug_prev_state.get(dev_id)
        self._plug_prev_state[dev_id] = on
        if prev is None or prev == on or not notify.configured():
            return
        label = "ON" if on else "OFF"
        msg = ("<b>Torronto</b> \U0001F50C · <b>%s</b> plug is now <b>%s</b>"
               % (name, label))
        ok, err = notify.send_text(msg)
        self.add_log("ok" if ok else "warn",
                     "Plug %s → %s (%s)"
                     % (name, label, "sent" if ok else "failed: " + err))

    def _roll_plug_day(self, now=None):
        """Start a fresh kWh accumulator when the local day changes."""
        now = now or time.time()
        day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self._plug_kwh_day is None:
            self._plug_kwh_day = day
        elif self._plug_kwh_day != day:
            self._plug_kwh_day = day
            with self._lock:
                self._plug_kwh_today = 0.0

    def get_plugs(self):
        now = time.time()
        self._roll_plug_day(now)
        with self._lock:
            day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            kwh = self._plug_kwh_today if self._plug_kwh_day == day else 0.0
            return {k: dict(v) for k, v in self._plugs.items()}, kwh

    def get_plug_history(self, hours):
        cutoff = int(time.time()) - int(hours * 3600)
        with self._lock:
            return [dict(h) for h in self._plug_hist if h["ts"] >= cutoff]

    def plug_kwh_window(self, hours):
        """Measured kWh over the last `hours`, integrated from live wattage.

        This is the only source of the kWh figures used for cost: it comes from
        the smart plug's real measured watts, never from heater on/off history.
        """
        cutoff = int(time.time()) - int(hours * 3600)
        with self._lock:
            pts = [h for h in self._plug_hist if h["ts"] >= cutoff]
        kwh = 0.0
        for i in range(1, len(pts)):
            dt_h = (pts[i]["ts"] - pts[i - 1]["ts"]) / 3600.0
            # Skip implausibly long gaps (server was down) and dead-zero days.
            if 0 < dt_h <= 2.0:
                kwh += (pts[i - 1].get("watts") or 0.0) / 1000.0 * dt_h
        return kwh

    def poll_plug(self):
        """Fetch all configured plugs once over the Tuya cloud. Never raises."""
        readings, err = None, ""
        try:
            readings = plug.read_all(self.cfg.plug_device_id)
        except Exception as e:  # noqa: BLE001 - plug must never break the loop
            err = str(e)
        if readings:
            self._plug_err = 0
            for r in readings:
                self.update_plug(r)
            return
        # Cloud unreachable: flag every known plug offline; log ~ every 6th.
        with self._lock:
            for k in self._plugs:
                self._plugs[k] = dict(self._plugs[k], online=False)
        self._plug_err += 1
        if self._plug_err % 6 == 1:  # log ~ every 6th failure (~3 min apart)
            self.add_log("warn", "Plug read failed%s"
                         % (": " + err if err else ""))

    def _seed_history_from_log(self):
        """Backfill the in-memory history from the persisted shadow log.

        The history chart used to only keep samples from the current process,
        so a restart wiped the view. Pre-load the last HISTORY_SECONDS of
        water/target/heater/power from shadow_log.jsonl so the chart shows real
        data immediately and survives restarts (ambient is backfilled live).
        """
        path = os.path.join(BASE_DIR, "shadow_log.jsonl")
        if not os.path.exists(path):
            return
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            return
        rows.sort(key=lambda r: r["ts"])
        cutoff = int(time.time()) - HISTORY_SECONDS
        hist = []
        for r in rows:
            if r["ts"] < cutoff:
                continue
            s = r.get("shadow") or {}
            hist.append({
                "ts": r["ts"],
                "water": s.get("water_temperature"),
                "target": s.get("temperature_setting"),
                "heater": s.get("heater_state"),
                "power": s.get("power_state"),
                "ambient": None,
            })
        if hist:
            with self._lock:
                if not self._history:
                    self._history = hist

    def _register_history(self, hours):
        """Raw register history from shadow_log.jsonl + derived heater kW.

        Reads the persisted shadow log (full raw shadow every ~5 min), then
        for each interval computes an inferred heater draw: when the heater's
        elements are on (heater_state 2,3,5,6,7,8), kW = C·dT/dt + UA·(T−amb)
        using the learned thermal model and backfilled Open-Meteo ambient.
        Returns [{ts, water, heater, b3, on, kw}].
        """
        path = os.path.join(BASE_DIR, "shadow_log.jsonl")
        rows = []
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        rows.sort(key=lambda r: r["ts"])
        cutoff = int(time.time()) - int(hours * 3600)
        rows = [r for r in rows if r["ts"] >= cutoff]
        if len(rows) < 2:
            return []
        cfg = self.cfg.scheduler_cfg
        C = float(cfg["C_KWH_PER_C"])
        UA = float(cfg["UA_KW_PER_C"])
        P = float(cfg.get("P_HEAT_KW") or 2.0)
        past_days = max(1, int(hours // 24) + 1)
        if (self._reg_weather_pd != past_days
                or time.time() - self._reg_weather_ts > 600):
            weather = scheduler.fetch_forecast(
                self._lat, self._lon, past_days=past_days, forecast_days=1)
            self._reg_weather = weather
            self._reg_weather_ts = time.time()
            self._reg_weather_pd = past_days
        else:
            weather = self._reg_weather
        heatmap = {0: False, 1: False, 2: True, 3: True, 4: False,
                   5: True, 6: True, 7: True, 8: True}
        cap = P * 1.1                       # heater can't exceed ~its rating
        min_spread = 1800                   # pair ~30 min apart to smooth 1C res
        out = []
        buf = []                            # recent (ts, water)
        for r in rows:
            s = r["shadow"]
            try:
                ht = int(s.get("heater_state") or 0)
            except (TypeError, ValueError):
                ht = 0
            water = s.get("water_temperature")
            on = bool(heatmap.get(ht))
            kw = None
            ref = None
            for b in buf:
                if r["ts"] - b["ts"] >= min_spread:
                    ref = b
            if (ref is not None and isinstance(ref["water"], (int, float))
                    and isinstance(water, (int, float))):
                dt_h = (r["ts"] - ref["ts"]) / 3600.0
                if 0.1 <= dt_h <= 24:
                    dT = water - ref["water"]
                    T_avg = (water + ref["water"]) / 2.0
                    t_amb = None
                    e = weather.get(r["ts"] - r["ts"] % 3600)
                    if e:
                        t_amb = e[0]
                    if on:
                        k = C * dT / dt_h + (
                            UA * (T_avg - t_amb) if isinstance(t_amb, (int, float)) else 0.0)
                        kw = max(0.0, min(k, cap))
                    elif isinstance(t_amb, (int, float)) and dT < 0:
                        kw = -min(cap, max(0.0, C * (-dT) / dt_h + UA * (T_avg - t_amb)))
            buf.append({"ts": r["ts"], "water": water})
            if len(buf) > 60:
                buf.pop(0)
            out.append({
                "ts": r["ts"], "water": water, "heater": ht,
                "b3": s.get("B3"), "on": on,
                "kw": round(kw, 2) if kw is not None else None,
            })
        return out

    # -- thermal model (automatic cooling-rate measurement) -----------------
    def _maybe_log_shadow(self, state):
        now = time.time()
        if now - self._last_shadow_log < SHADOW_LOG_SECONDS:
            return
        self._last_shadow_log = now
        path = os.path.join(BASE_DIR, "shadow_log.jsonl")
        try:
            with open(path, "a") as f:
                f.write(json.dumps({"ts": int(now), "shadow": state},
                                   separators=(",", ":")) + "\n")
        except OSError as e:
            self.add_log("err", "Could not append shadow log: %s" % e)

    def _maybe_sample_ua(self, state):
        """Estimate the heat-loss coefficient (UA) from real coast-downs.

        Pairs shadow readings ~1 h apart while the tub is well above its
        setpoint (so the thermostat cannot be heating), and keeps a rolling
        median. A measured drop fits UA directly; a *held* temperature (no
        measurable drop at the 1 C display resolution) is still strong evidence
        of low heat loss, so it is fitted as an upper-bound sample using half
        a degree (the most cooling we cannot rule out). Using an upper bound
        keeps the plan safe (never schedules less heat than reality could
        demand) while still learning great insulation. Once enough samples
        exist the fitted value is saved to scheduler_model.json and used by the
        next replan.
        """
        now = int(time.time())
        if not spa.is_online(state):
            return  # never fit thermal data from an offline/stale snapshot
        water = state.get("water_temperature")
        try:
            power = int(state.get("power_state") or 0)
        except (TypeError, ValueError):
            power = 0
        cur = {
            "ts": now,
            "water": water if isinstance(water, (int, float)) else None,
            "setpoint": state.get("temperature_setting"),
            "power": power,
            "ambient": self._amb.get(now - now % 3600),
        }
        prev = self._ua_prev
        if prev is None:
            self._ua_prev = cur
            return
        dt_s = now - prev["ts"]
        if dt_s < UA_SAMPLE_MIN_S:
            return
        self._ua_prev = cur
        if dt_s > UA_SAMPLE_MAX_S:
            return
        if prev["water"] is None or cur["water"] is None:
            return
        if prev["power"] != 1 or cur["power"] != 1:
            return
        sp1, sp2 = prev["setpoint"], cur["setpoint"]
        if not isinstance(sp1, (int, float)) or not isinstance(sp2, (int, float)):
            return
        if state.get("heater_state") in (1, 2, 3):   # actively heating now
            return
        t1, t2 = float(prev["water"]), float(cur["water"])
        if min(t1, t2) <= max(sp1, sp2) + UA_SETPOINT_MARGIN:
            return  # thermostat may have called for heat during the span
        dT = t2 - t1
        hold = dT > -0.5   # no clear cooling at 1 C sensor resolution
        if hold:
            # Upper bound: assume the most cooling we cannot rule out (0.5 C).
            dT = -0.5
        t_amb = prev["ambient"]
        if not isinstance(t_amb, (int, float)):
            return
        if isinstance(cur["ambient"], (int, float)):
            t_amb = (t_amb + cur["ambient"]) / 2.0
        t_avg = (t1 + t2) / 2.0
        if abs(t_avg - t_amb) <= UA_AMBIENT_MIN_DIFF:
            return
        dt_h = dt_s / 3600.0
        ua = (self.cfg.scheduler_cfg["C_KWH_PER_C"] * (-dT / dt_h)
              / (t_avg - t_amb))
        if not (UA_BOUNDS[0] <= ua <= UA_BOUNDS[1]):
            return
        self._ua_samples.append(ua)
        if len(self._ua_samples) > UA_SAMPLE_MAX_N:
            del self._ua_samples[:-UA_SAMPLE_MAX_N]
        if len(self._ua_samples) < UA_MIN_SAMPLES:
            return
        new_ua = statistics.median(self._ua_samples)
        old_ua = float(self.cfg.scheduler_cfg["UA_KW_PER_C"])
        if abs(new_ua - old_ua) < 0.001:
            return
        self.cfg.scheduler_cfg["UA_KW_PER_C"] = new_ua
        self._save_model_ua(new_ua)
        self.add_log("info",
                     "Cooling rate %s: UA=%.4f kW/°C from %d coast-down%s"
                     % ("(held ≈ upper bound)" if hold else "measured",
                        new_ua, len(self._ua_samples),
                        "" if len(self._ua_samples) == 1 else "s"))

    def _monitor_registers(self, state):
        """Ongoing probe-vs-water offset telemetry for calibration work.

        Each sample records how far B1/B2/B9 sit from the control probe
        (water_temperature) into a rolling window. Roughly hourly we log a
        concise summary to the operation feed (so it shows in the web UI and
        /api/log without digging through shadow_log.jsonl) and flag any probe
        whose offset has drifted from its established baseline. The full raw
        B*/P*/W* shadow keeps being persisted to shadow_log.jsonl every 5 min.
        """
        now = time.time()
        if not spa.is_online(state):
            return  # skip probe-offset telemetry on offline snapshots
        w = state.get("water_temperature")
        if not isinstance(w, (int, float)):
            return
        for key in REG_PROBES:
            v = state.get(key)
            if not isinstance(v, (int, float)) or abs(v - w) > 8:
                continue
            buf = self._reg_bias[key]
            buf.append(v - w)
            if len(buf) > REG_MAX_N:
                del buf[:len(buf) - REG_MAX_N]
        baselines = {}
        for key in REG_PROBES:
            buf = self._reg_bias[key]
            if len(buf) >= REG_MIN_N:
                baselines[key] = statistics.median(buf)
        self._reg_offsets = baselines
        if now - self._reg_mon_ts < REG_MON_INTERVAL_S or not baselines:
            return
        self._reg_mon_ts = now
        parts = []
        for key in REG_PROBES:
            o = baselines[key]
            recent = self._reg_bias[key][-12:]
            r = statistics.median(recent) if recent else o
            flag = " \u26a0" if abs(r - o) >= REG_ANOM_DELTA else ""
            parts.append("%s%+.1f%s" % (key, o, flag))
        self.add_log("info",
                     "registers: water=%s \u00b7 %s (offset vs control probe, n=%d)"
                     % (w, " ".join(parts), len(self._reg_bias["B1"])))

    def _maybe_notify(self, state):
        """Telegram 'tub reached target' alert, at most once per hour.

        Notifies on the rising edge (water crosses above the scheduler's
        TARGET while in window/preheat). Nothing happens until telegram.env is
        configured; failures are logged, never fatal.
        """
        if not notify.configured():
            self._prev_water = state.get("water_temperature")
            return
        now = time.time()
        if not spa.is_online(state):
            self._prev_water = state.get("water_temperature")
            return  # never claim "target reached" from an offline snapshot
        water = state.get("water_temperature")
        target = self.cfg.scheduler_cfg.get("TARGET_TEMP")
        phase = (self._plan or {}).get("phase") if self._plan else None
        if not isinstance(water, (int, float)) or not isinstance(target, (int, float)):
            self._prev_water = water
            return
        reached = water >= float(target)
        prev = self._prev_water
        self._prev_water = water
        rising_edge = (reached and (prev is None or prev < float(target)))
        if not rising_edge:
            return
        if phase not in ("window", "preheat"):
            return
        if now - self._notify_at < NOTIFY_COOLDOWN_S:
            return
        self._notify_at = now
        msg = ("<b>Torronto</b> · the tub has reached <b>%g°C</b> (target %g°C, %s)."
               % (water, target, phase))
        ok, err = notify.send_text(msg)
        self.add_log("ok" if ok else "warn",
                     "Telegram target-reached %s (%s)"
                     % ("sent" if ok else "failed: " + err, phase))
        self._broadcast("log")

    def _tg_command(self, command):
        """Build a Telegram reply for a bot /command, or None if unknown."""
        c = (command or "").lower()
        if c in ("/status", "/s", "/state"):
            return self._tg_status()
        if c == "/mode":
            state, _ = self.get_state()
            phase = (self._plan or {}).get("phase")
            setting = (self._plan or {}).get("setting")
            return ("<b>Torronto</b> mode: <b>%s</b> · phase: %s · target %s°C"
                    % (self.cfg.mode.upper(), phase or "?", setting or "?"))
        if c == "/plan":
            return self._tg_plan()
        if c in ("/cost", "/price"):
            return self._tg_cost()
        if c in ("/help", "/start"):
            return ("Commands: <code>/status</code> state · "
                    "<code>/mode</code> mode/phase/target · "
                    "<code>/plan</code> schedule · "
                    "<code>/cost</code> energy spend/saving · "
                    "<code>/help</code> this")
        return None

    def _tg_status(self):
        state, _ = self.get_state()
        cfg = self.cfg.scheduler_cfg
        phase = (self._plan or {}).get("phase") or "?"
        setting = (self._plan or {}).get("setting")
        water = state.get("water_temperature")
        dev_target = state.get("temperature_setting")
        heat = heater_status(state)
        power = state.get("power_state") in (1, "1")
        filt = state.get("filter_state")
        lines = [
            "<b>Torronto</b> \U0001F30A status",
            "mode: <b>%s</b> · phase: %s" % (self.cfg.mode.upper(), phase),
        ]
        if not self.tub_online():
            age = self.state_age_s()
            lines.append("⚠ tub <b>offline</b> · last data %s"
                         % ("%.0fh ago" % (age / 3600.0) if age else "unknown"))
        lines += [
            "water: <b>%s°C</b> (device target %s°C)" % (water, dev_target or "?"),
            "heater: %s · power: %s · filter: %s" % (
                heat, "on" if power else "off",
                "running" if filt == 2 else ("on" if filt else "off")),
            "window: %s · target %s° / eco %s°" % (
                cfg["WINDOW"], cfg["TARGET_TEMP"], cfg["ECO_TEMP"]),
        ]
        if cfg.get("LOCATION"):
            lines.append("location: %s" % cfg["LOCATION"])
        return "\n".join(lines)

    def _tg_plan(self):
        plan = self._plan or {}
        ts = plan.get("plan_ts") or []
        phase = plan.get("phase") or []
        setting = plan.get("setting")
        nxt = None
        for i, ph in enumerate(plan.get("plan_phase") or []):
            if ph == "window" and ts and i < len(ts):
                nxt = ts[i]
                break
        nxt_s = ("%02d:%02d" % (dt.datetime.fromtimestamp(nxt).hour,
                                dt.datetime.fromtimestamp(nxt).minute)) if nxt else "?"
        on24 = sum(1 for u in (plan.get("plan_us") or [])[:24] if u)
        en = plan.get("energy_kwh")
        return ("<b>Torronto</b> plan\n"
                "phase: %s · target %s°C\n"
                "next window: %s\n"
                "heater ON next 24h: %dh\n"
                "energy (plan): %s kWh"
                % (phase or "?", setting or "?",
                   nxt_s, on24, ("%.1f" % en) if isinstance(en, (int, float)) else "?"))

    def _tg_cost(self):
        cfg = self.cfg.scheduler_cfg
        c = cost_overview(cfg, self._plan, stale=self._plan_stale())
        price = float(cfg.get("PRICE_PER_KWH") or 0.0)
        lines = ["<b>Torronto</b> cost @ \u00A3%.4f/kWh" % price]
        # Measured consumption is the ground truth when the plug is enabled:
        # it comes from the smart meter's real draw, never from heater on/off
        # history or from the (predictive) plan.
        if self.plug_enabled():
            m7 = round(self.plug_kwh_window(168), 2)
            m24 = round(self.plug_kwh_window(24), 2)
            lines.append("measured (plug) 24h: %.2f kWh \u00B7 \u00A3%.2f"
                         % (m24, price * m24))
            lines.append("measured (plug) 7d : %.2f kWh \u00B7 \u00A3%.2f"
                         % (m7, price * m7))
        if not self._plan:
            lines.append("no heating plan yet \u2014 run /plan or wait for the next replan")
            return "\n".join(lines)
        if c.get("stale"):
            age = "" if not self._plan_ts else " (%.0fh old)" % max(
                0.0, (time.time() - self._plan_ts) / 3600.0)
            state = "tub offline" if not self.tub_online() else "plan stale"
            lines.append("\u26a0 %s%s \u2014 projected plan figures below are estimates"
                         % (state, age))
        lines.append("plan (%.1fd): %.1f kWh \u00B7 \u00A3%.2f"
                     % (c["days"], c["plan_kwh"], c["cost_plan"]))
        if "cost_alwayson" in c:
            lines.append("always-on baseline: %.1f kWh \u00B7 \u00A3%.2f"
                         % (c["alwayson_kwh"], c["cost_alwayson"]))
            lines.append("plan saving: \u00A3%.2f (%.0f%%)"
                         % (c["saving_cost"], c["saving_pct"]))
            lines.append("projected month: plan \u00A3%.2f \u00B7 save \u00A3%.2f"
                         % (c["cost_monthly"], c["saving_monthly"]))
        else:
            lines.append("projected month: \u00A3%.2f" % c["cost_monthly"])
        return "\n".join(lines)

    def _telegram_poller(self):
        if not notify.configured():
            return
        threading.Thread(target=self._tg_poll_loop, daemon=True).start()

    def _tg_poll_loop(self):
        chat = notify.configured_chat_id()
        offset = 0
        while not self._stopping:
            try:
                updates = notify.get_updates(offset)
            except Exception:
                updates = []
            for u in updates:
                offset = int(u.get("update_id", 0)) + 1
                msg = u.get("message") or u.get("channel_post")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if str(msg.get("chat", {}).get("id")) != str(chat):
                    continue
                reply = self._tg_command(text.split()[0])
                if reply:
                    notify.send_text(reply, chat_id=msg["chat"]["id"])
            time.sleep(0.4)

    def _save_model_ua(self, ua):
        path = os.path.join(BASE_DIR, "scheduler_model.json")
        model = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    model = json.load(f)
            except (OSError, ValueError):
                model = {}
        model.update({"UA_KW_PER_C": ua,
                      "fitted_from": len(self._ua_samples),
                      "ua_samples": len(self._ua_samples),
                      "source": "online coast-down",
                      "saved_at": int(time.time())})
        try:
            with open(path, "w") as f:
                json.dump(model, f, indent=2)
            os.chmod(path, 0o600)
        except OSError as e:
            self.add_log("err", "Could not save thermal model: %s" % e)

    # -- operation log -----------------------------------------------------
    def _log_path(self):
        return os.path.join(BASE_DIR, OPERATION_LOG_FILE)

    def add_log(self, level, msg):
        entry = {"ts": int(time.time()), "level": level, "msg": str(msg)}
        # durable: append to the JSONL log so history survives restarts
        path = self._log_path()
        try:
            with open(path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass
        self._prune_log_file()
        with self._log_lock:
            self._log.append(entry)
            if len(self._log) > LOG_MAX:
                del self._log[:-LOG_MAX]
        self._broadcast("log")
        return entry

    def _prune_log_file(self):
        """Keep the durable log bounded so it can't grow forever."""
        try:
            if os.path.getsize(self._log_path()) < 1 << 20:   # < 1 MB
                return
            with open(self._log_path()) as f:
                lines = [ln for ln in f if ln.strip()]
            if len(lines) <= LOG_FILE_MAX:
                return
            with open(self._log_path(), "w") as f:
                for ln in lines[-LOG_FILE_MAX:]:
                    f.write(ln)
        except OSError:
            pass

    def get_log(self, limit=100):
        """Return the most recent `limit` entries from the durable log."""
        limit = int(limit)
        path = self._log_path()
        entries = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                entries = []
        if not entries:  # fall back to the in-memory cache
            with self._log_lock:
                return [dict(e) for e in self._log[-limit:]]
        return [dict(e) for e in entries[-limit:]]

    # -- SSE --------------------------------------------------------------
    def _broadcast(self, kind):
        msg = json.dumps({"kind": kind, "ts": int(time.time())}) + "\n\n"
        dead = []
        for q in self._sse_clients:
            try:
                q.put(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            self._sse_clients.discard(q)

    def add_sse_client(self, q):
        self._sse_clients.add(q)

    def remove_sse_client(self, q):
        self._sse_clients.discard(q)

    # -- plan --------------------------------------------------------------
    def refresh_plan(self, apply=False, force_weather=False):
        """Recompute the heating plan from the *current* state.

        The weather forecast is cached and only re-fetched hourly (or when
        ``force_weather`` is set, e.g. an explicit /replan or settings edit),
        so calling this frequently to track live water temperature is cheap.
        Commands are only sent when the plan changed versus the device state.
        """
        cfg = self.cfg.scheduler_cfg
        horizon_days = max(3, (int(cfg["HORIZON_H"]) + 23) // 24)
        now = time.time()
        if (force_weather or self._forecast_weather is None
                or now - self._forecast_ts > FORECAST_REFRESH_S):
            self._forecast_weather = scheduler.fetch_forecast(
                self._lat, self._lon, forecast_days=horizon_days)
            self._forecast_ts = time.time()
        res = scheduler.scheduler_step(
            cfg, self._lat, self._lon, state=self.get_state()[0],
            tokens=self._get_tokens(), apply=apply, weather=self._forecast_weather)
        self._plan = res
        self._plan_ts = time.time()
        # Keep forecast ambient for hours that have just left the plan window,
        # so the cooling-rate sampler can still look up its older anchor.
        for ts, amb in zip(res.get("plan_ts") or [], res.get("plan_amb") or []):
            self._amb[ts] = amb
        cutoff = time.time() - 72 * 3600
        self._amb = {ts: a for ts, a in self._amb.items() if ts >= cutoff}
        if apply and res.get("changed"):
            self.add_log("info", "Auto mode applied target %d°C (%s phase)"
                         % (int(res.get("setting") or 0), res.get("phase") or "?"))
        self._broadcast("plan")
        return res

    def get_plan(self):
        return self._plan

    # -- commands -----------------------------------------------------------
    def send_command(self, datapoint, value):
        """Clamp/validate and send a command. Raises ValueError on bad input."""
        if datapoint == "temperature_setting":
            value = int(value)
            if not (20 <= value <= 40):
                raise ValueError("temperature_setting must be 20-40")
            if self.cfg.mode == "auto":
                raise ValueError("temp is controlled by auto mode; switch to manual first")
        aliases = {"power": "power_state", "heat": "heater_state",
                   "filter": "filter_state", "wave": "wave_state",
                   "jet": "hydrojet_state", "lock": "locked"}
        datapoint = aliases.get(datapoint, datapoint)
        if datapoint not in ("power_state", "heater_state", "filter_state",
                             "wave_state", "hydrojet_state", "locked",
                             "temperature_setting"):
            raise ValueError("unknown datapoint %r" % datapoint)
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                raise ValueError("value must be an integer")
        if datapoint != "temperature_setting" and value not in (0, 1):
            raise ValueError("value must be 0 or 1")
        shown = ("%d°C" % value if datapoint == "temperature_setting"
                 else ("ON" if value else "OFF"))
        label = DP_LOG_LABELS.get(datapoint, datapoint)
        self.add_log("info", "Sending %s → %s" % (label, shown))
        try:
            resp = spa.send_command(self._get_tokens(), datapoint, value)
            # confirm in shadow
            ok = spa.wait_for(self._get_tokens(), datapoint, value, timeout=20)
        except Exception as e:
            self.add_log("err", "%s → %s failed: %s" % (label, shown, e))
            raise
        self.add_log("ok" if ok else "warn", "%s → %s %s"
                     % (label, shown,
                        "confirmed" if ok else "sent, not yet confirmed"))
        return {"datapoint": datapoint, "value": value, "confirmed": ok,
                "response": resp}

    # -- scheduler config --------------------------------------------------
    def update_scheduler_config(self, payload):
        """Validate + persist scheduler settings, then replan."""
        if not isinstance(payload, dict):
            raise ValueError("config must be an object")
        path = os.path.join(BASE_DIR, "scheduler.env")
        env = scheduler.load_env(path)
        cur = self.cfg.scheduler_cfg

        # Validate the whole prospective config before writing anything.
        target = _int_setting("target", payload.get("target", cur["TARGET_TEMP"]), 20, 40)
        eco = _int_setting("eco", payload.get("eco", cur["ECO_TEMP"]), 5, 40)
        floor = _int_setting("floor", payload.get("floor", cur["FLOOR_TEMP"]), 0, 40)
        margin = _float_setting("margin", payload.get("margin", cur["MARGIN"]), 0, 10)
        price = _float_setting("price", payload.get("price", cur.get("PRICE_PER_KWH", 0)), 0, 10)
        if not (floor <= eco <= target):
            raise ValueError("temperatures must satisfy floor ≤ eco ≤ target")

        if "window" in payload:
            env["WINDOW"] = validate_window(payload["window"])

        # Location: accept a geocoded choice (lat/lon) or resolve free text.
        location_changed = False
        if "location" in payload:
            label = str(payload.get("location") or "").strip()
            if not label:
                raise ValueError("location must not be empty")
            lat, lon = payload.get("lat"), payload.get("lon")
            if lat not in (None, "") and lon not in (None, ""):
                lat = _float_setting("latitude", lat, -90, 90)
                lon = _float_setting("longitude", lon, -180, 180)
                new_location = label
            else:
                try:
                    hits = scheduler.geocode(label, 1)
                except Exception as e:
                    raise ValueError("could not look up location: %s" % e)
                if not hits:
                    raise ValueError("location not found: %r" % label)
                hit = hits[0]
                lat, lon = hit["lat"], hit["lon"]
                new_location = hit["label"] or label
            old_lat, old_lon = cur.get("LAT"), cur.get("LON")
            location_changed = (
                new_location != cur.get("LOCATION")
                or old_lat is None or old_lon is None
                or abs(lat - old_lat) > 1e-6 or abs(lon - old_lon) > 1e-6)
            env["LOCATION"] = new_location
            env["LAT"] = "%.5f" % lat
            env["LON"] = "%.5f" % lon

        overrides = payload.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        for day in DAY_NAMES:
            if day not in overrides:
                continue
            spec = str(overrides.get(day) or "").strip()
            key = "WINDOW_" + day
            if spec:
                env[key] = validate_window(spec)
            else:
                env.pop(key, None)

        env["TARGET_TEMP"] = str(target)
        env["ECO_TEMP"] = str(eco)
        env["FLOOR_TEMP"] = str(floor)
        env["MARGIN"] = ("%g" % margin)
        env["PRICE_PER_KWH"] = ("%g" % price)
        with open(path, "w") as f:
            for key in sorted(env):
                f.write("%s=%s\n" % (key, env[key]))

        self.cfg.scheduler_cfg = scheduler.load_config()
        cfg = self.cfg.scheduler_cfg

        if location_changed:
            # Re-resolve where we are and drop ambient/coast-down caches that
            # were keyed on the old location's weather.
            try:
                self._lat, self._lon, self._where = \
                    scheduler.resolve_location(cfg)
            except SystemExit as e:
                raise ValueError("location not usable: %s" % e)
            self._amb = {}
            self._ua_prev = None

        summary = ("window %s, target %d°C, eco %d°C, floor %d°C, margin %g°C"
                   % (cfg["WINDOW"], int(cfg["TARGET_TEMP"]), int(cfg["ECO_TEMP"]),
                      int(cfg["FLOOR_TEMP"]), float(cfg["MARGIN"])))

        replanned, plan_error = False, None
        try:
            self.refresh_plan(apply=(self.cfg.mode == "auto"), force_weather=True)
            replanned = True
        except Exception as e:
            plan_error = str(e)

        if location_changed:
            summary += "; location %s (%.4f, %.4f)" % (
                cfg["LOCATION"], self._lat, self._lon)
        self.add_log("ok" if not plan_error else "warn",
                     "Heating settings updated — %s%s"
                     % (summary, "" if not plan_error else " (replan failed)"))
        self._broadcast("config")
        out = scheduler_payload(self)
        out["replanned"] = replanned
        out["plan_error"] = plan_error
        return out

    # -- tokens --------------------------------------------------------------
    def _get_tokens(self):
        if self._tokens is None:
            self._tokens = spa.get_tokens()
        return self._tokens

    def _invalidate_tokens(self):
        """Drop the cached token so the next call re-reads tokens.json."""
        self._tokens = None

    def _measured_cost_payload(self):
        """Measured spend + saving vs the always-on baseline (same basis).

        Gated on MEASURED_MIN_HOURS of real plug history: with less data the
        rolling 7-day window (and any "saving") is dominated by a restart or
        outage and would show a misleading £0/100%.
        """
        cfg = self.cfg.scheduler_cfg
        price = float(cfg.get("PRICE_PER_KWH") or 0.0)
        with self._lock:
            hist = list(self._plug_hist)
        if not hist or price <= 0:
            return {"enough": False, "coverage_h": 0.0}
        span_h = (hist[-1]["ts"] - hist[0]["ts"]) / 3600.0
        m7 = self.plug_kwh_window(168)
        enough = span_h >= MEASURED_MIN_HOURS
        out = {"enough": enough, "coverage_h": round(span_h, 1),
               "measured_kwh_7d": round(m7, 3),
               "monthly_cost": round(price * m7 * 30.0 / 7.0, 2)}
        plan = self._plan or {}
        alwayson = plan.get("energy_alwayson_kwh")
        days = len(plan.get("plan_ts") or []) / 24.0
        if enough and isinstance(alwayson, (int, float)) and days > 0:
            ao_monthly = alwayson * price * 30.0 / days
            saving = max(0.0, ao_monthly - out["monthly_cost"])
            out["saving_monthly"] = round(saving, 2)
            out["saving_pct"] = round(saving / ao_monthly * 100.0, 1) if ao_monthly > 0 else 0.0
        return out

    def _auto_relogin(self):
        """Refresh the Bestway session from secrets.env (opt-in behaviour).

        The user chose to let the server re-login automatically even though a
        new session invalidates the phone app's. Throttled by
        REL_LOGIN_COOLDOWN_S so a revoked token doesn't hammer the login API.
        Never raises. Returns True when the cache was refreshed.
        """
        if time.time() - self._last_relogin < REL_LOGIN_COOLDOWN_S:
            return False
        self._last_relogin = time.time()
        try:
            tokens = spa.get_tokens(force=True)   # logs in, rewrites tokens.json
        except spa.SpaError as e:
            msg = str(e)
            if "no spa binding returned" in msg:
                # Login works but Bestway has no spa bound: the tub is offline
                # on their side (e.g. the wifi changed and it has not rejoined
                # the cloud). Retrying every 10 min just keeps rotating the
                # account session — which can invalidate the phone app session
                # mid-fix — for no benefit, so back off to hourly.
                self._last_relogin = (time.time() - REL_LOGIN_COOLDOWN_S
                                      + REL_LOGIN_BACKOFF_S)
                self.add_log("warn", "Auto re-login: account OK but no spa bound "
                                     "(offline/wifi?) — will retry hourly. %s"
                             % msg[:200])
                return False
            self.add_log("err", "Auto re-login failed: %s" % msg)
            return False
        except Exception as e:  # noqa: BLE001 - never break the poller
            self.add_log("err", "Auto re-login failed: %s" % e)
            return False
        self._tokens = tokens
        self.add_log("ok", "Auto re-login succeeded — tokens refreshed")
        if notify.configured():
            notify.send_text("<b>Torronto</b> \U0001F501 api token refreshed "
                             "automatically (re-login).")
        return True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; server logs at /api/health time

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/state", "/api/health", "/api/history", "/api/history/registers",
                    "/api/plan", "/api/scheduler", "/api/weather", "/api/log",
                    "/api/plug", "/api/location/search"):
            self._api(path)
        elif path == "/api/events":
            self._sse()
        elif path in ("/", "/index.html"):
            self._static("index.html", "text/html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._send_json(400, {"error": "invalid JSON"})
        if path == "/api/cmd":
            self._api_cmd(body)
        elif path == "/api/scheduler":
            self._api_scheduler(body)
        else:
            self._send_json(404, {"error": "not found"})

    # -- API handlers -------------------------------------------------------
    def _api(self, path):
        app = self.server.app
        if path == "/api/state":
            state, ts = app.get_state()
            plugs, plug_kwh = app.get_plugs()
            out = {
                "ts": int(ts),
                "mode": app.cfg.mode,
                "tub_online": app.tub_online(),
                "state_age_s": app.state_age_s(),
                "state": state,
                "heater": heater_status(state),
                "plan_ts": int(app._plan_ts) if app._plan else None,
                "plugs": plugs,
                "plug_kwh_today": plug_kwh,
                "plug_kwh_7d": round(app.plug_kwh_window(168), 3),
                "register_offsets": dict(app._reg_offsets),
            }
            if app._plan:
                out["phase"] = app._plan["phase"]
                out["setting"] = app._plan["setting"]
            self._send_json(200, out)
        elif path == "/api/health":
            state, ts = app.get_state()
            self._send_json(200, {
                "online": bool(state) and app.tub_online(state, ts),
                "tub_online": app.tub_online(),
                "connect": state.get("ConnectType"),
                "is_online": state.get("is_online"),
                "last_update": int(ts),
                "age_s": int(time.time() - ts) if ts else None,
                "mode": app.cfg.mode,
                "plan_age_s": int(time.time() - app._plan_ts) if app._plan_ts else None,
            })
        elif path == "/api/history":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            hours = float(qs.get("range", ["24"])[0])
            hours = min(hours, 168)
            self._send_json(200, {
                "series": app.get_history(hours),
                "plug": app.get_plug_history(hours),
            })
        elif path == "/api/plug":
            plugs, plug_kwh = app.get_plugs()
            self._send_json(200, {
                "enabled": plug.configured(),
                "plugs": plugs,
                "kwh_today": round(plug_kwh, 4),
                "kwh_24h": round(app.plug_kwh_window(24), 4),
                "kwh_7d": round(app.plug_kwh_window(168), 4),
            })
        elif path == "/api/plan":
            if app._plan is None:
                try:
                    app.refresh_plan(apply=False)
                except Exception as e:
                    return self._send_json(502, {"error": str(e)})
            self._send_json(200, app._plan)
        elif path == "/api/scheduler":
            self._send_json(200, scheduler_payload(app))
        elif path == "/api/log":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit = int(qs.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self._send_json(200, {"entries": app.get_log(max(1, min(limit, LOG_FILE_MAX)))})
        elif path == "/api/history/registers":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                hours = float(qs.get("hours", ["168"])[0])
            except ValueError:
                hours = 168
            try:
                self._send_json(200, {"series": app._register_history(
                    max(1, min(hours, 168)))})
            except Exception as e:
                self._send_json(502, {"error": str(e)})
        elif path == "/api/location/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            query = (qs.get("q") or [""])[0]
            try:
                self._send_json(200, {"results": scheduler.geocode(query, 8)})
            except Exception as e:
                self._send_json(502, {"error": "location search failed: %s" % e})
        elif path == "/api/weather":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int(qs.get("days", ["3"])[0])
            except ValueError:
                days = 3
            days = max(1, min(days, 7))
            try:
                self._send_json(200, scheduler.fetch_weather_detail(
                    app._lat, app._lon, days))
            except Exception as e:
                self._send_json(502, {"error": str(e)})

    def _api_cmd(self, body):
        app = self.server.app
        dp = body.get("datapoint")
        val = body.get("value")
        if dp is None or val is None:
            return self._send_json(400, {"error": "datapoint and value required"})
        try:
            res = app.send_command(dp, val)
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(502, {"error": str(e)})
        self._send_json(200, res)

    def _api_scheduler(self, body):
        app = self.server.app  # type: ignore[attr-defined]

        # Full heating-settings update from the UI.
        if body.get("config") is not None:
            try:
                out = app.update_scheduler_config(body["config"])
            except ValueError as e:
                return self._send_json(400, {"error": str(e)})
            except Exception as e:
                return self._send_json(502, {"error": str(e)})
            return self._send_json(200, out)

        mode = body.get("mode")
        if mode in ("auto", "manual"):
            app.cfg.mode = mode
            # persist
            path = os.path.join(BASE_DIR, "web.env")
            existing = scheduler.load_env(path)
            existing["MODE"] = mode
            with open(path, "w") as f:
                for k, v in sorted(existing.items()):
                    f.write("%s=%s\n" % (k, v))
            app.add_log("info", "Mode → %s" % mode.upper())
            app._broadcast("mode")
            if mode == "auto":
                try:
                    app.refresh_plan(apply=True, force_weather=True)
                except Exception as e:
                    app.add_log("err", "Auto start failed: %s" % e)
                    return self._send_json(502, {"error": "auto-start failed: %s" % e})
        if body.get("replan"):
            try:
                app.refresh_plan(apply=(app.cfg.mode == "auto"), force_weather=True)
                app.add_log("info", "Heating plan recomputed")
            except Exception as e:
                app.add_log("err", "Replan failed: %s" % e)
                return self._send_json(502, {"error": str(e)})
        self._send_json(200, scheduler_payload(app))

    # -- static + SSE ---------------------------------------------------------
    def _static(self, name, ctype=None):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path) or ".." in name:
            return self._send_json(404, {"error": "not found"})
        ctype = ctype or {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        import queue
        q = queue.Queue()
        self.server.app.add_sse_client(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    data = q.get(timeout=20)
                except queue.Empty:
                    data = ": keepalive\n\n"
                self.wfile.write(data.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.app.remove_sse_client(q)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _poller_loop(app, stop_event):
    """Periodically fetch the shadow, append history, and re-evaluate the plan.

    The shadow is polled every POLL_SECONDS. The heating plan is re-evaluated
    on every poll against the live water temperature (the weather forecast is
    cached hourly server-side), so phase/setting/heater decisions track the
    tub instead of an hour-old snapshot.
    """
    while not stop_event.is_set():
        now = time.time()
        try:
            state = spa.shadow(app._get_tokens())
            app._poll_err = 0
            app._notified_auth = False     # auth recovered -> can alert again later
            app.update_state(state)
        except SystemExit as e:
            # Defensive: a stray sys.exit (e.g. a library path we missed) must
            # never take down monitoring / auto-heating with it.
            print("[%s] poller exit: %s" % (time.strftime("%F %T"), e), flush=True)
            app.add_log("err", "Spa poll exited: %s" % e)
        except Exception as e:
            # Log throttled (~1 in POLL_ERR_LOG_EVERY) so a DNS/cloud outage
            # doesn't flood the operation feed, but keep trying every poll.
            app._poll_err += 1
            if spa.is_auth_error(e):
                # Token expired/revoked: drop the cached token and re-login
                # automatically (throttled), so the service self-heals instead
                # of waiting for a human.
                app._invalidate_tokens()
                if not app._notified_auth and notify.configured():
                    app._notified_auth = True
                    notify.send_text(
                        "<b>Torronto</b> \u26a0 api token expired or revoked "
                        "\u2014 attempting an automatic re-login.")
                app._auto_relogin()
            if app._poll_err % POLL_ERR_LOG_EVERY == 1:
                msg = str(e)
                if spa.is_auth_error(e):
                    note = ("Spa tokens expired/revoked \u2014 attempting "
                            "automatic re-login")
                else:
                    note = "Spa poll failed"
                print("[%s] %s: %s" % (time.strftime("%F %T"), note, e), flush=True)
                app.add_log("err", "%s: %s" % (note, e))
        # plan refresh: re-evaluate on EVERY poll so the plan tracks the live
        # water temperature / phase (the weather feed is cached hourly inside
        # refresh_plan, so this is cheap). Previously this only ran once an
        # hour, so the plan could say "heat" long after the tub was hot.
        try:
            app.refresh_plan(apply=(app.cfg.mode == "auto"))
        except SystemExit as e:
            app.add_log("err", "Plan refresh exited: %s" % e)
        except Exception:
            pass
        # smart-plug (Tuya cloud) power polling
        if app.plug_enabled() and now >= app._plug_next:
            app._plug_next = now + app.cfg.plug_poll
            try:
                app.poll_plug()
            except Exception:  # noqa: BLE001 - never let the plug stop the loop
                pass
        stop_event.wait(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser(description="Torronto Web server")
    ap.add_argument("--bind", help="bind address (default: web.env BIND)")
    ap.add_argument("--port", type=int, help="port (default: web.env PORT)")
    ap.add_argument("--dry-run", action="store_true",
                    help="start with mock state, never touch the spa")
    args = ap.parse_args()

    cfg = WebConfig()
    bind = args.bind or cfg.bind
    port = args.port or cfg.port
    # ensure static dir exists
    if not os.path.isdir(STATIC_DIR):
        os.makedirs(STATIC_DIR, exist_ok=True)

    app = SpaServer(cfg)
    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.app = app  # type: ignore[attr-defined]
    print("Torronto Web listening on http://%s:%d  (mode=%s)"
          % (bind, port, cfg.mode), flush=True)
    print("  API: /api/state /api/history /api/plan /api/scheduler /api/cmd", flush=True)
    print("  SPA: http://%s:%d/" % (bind, port), flush=True)
    if plug.configured():
        print("  Plug: Tuya cloud power meter (poll every %gs)"
              % cfg.plug_poll, flush=True)

    stop = threading.Event()
    t = threading.Thread(target=_poller_loop, args=(app, stop), daemon=True)
    t.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        print("bye", flush=True)


if __name__ == "__main__":
    main()
