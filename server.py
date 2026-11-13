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
HISTORY_SECONDS = 7 * 86400        # keep ~1 week of history in memory
SHADOW_LOG_SECONDS = 300           # append one shadow snapshot to JSONL per 5 min
LOG_MAX = 300                      # operation log entries kept in memory
OPERATION_LOG_FILE = "operation_log.jsonl"   # durable operation log (gitignored)
LOG_FILE_MAX = 3000                # prune the durable log to this many entries
# Online cooling-rate (UA) estimate from real heater-off coast-downs. Spans
# are long enough to rise above the spa's 1 C temperature resolution.
UA_MIN_SAMPLES = 3                 # samples needed before the model is updated
UA_SAMPLE_MIN_S = 7200             # 2 h between the two observation points
UA_SAMPLE_MAX_S = 4 * 3600         # discard longer gaps (weather changed too much)
UA_SAMPLE_MAX_N = 50               # rolling median window
UA_BOUNDS = (0.005, 0.3)           # kW/degC, sane physical range
UA_AMBIENT_MIN_DIFF = 3.0          # need a clear water/ambient difference
UA_SETPOINT_MARGIN = 1.0           # only fit well above setpoint (thermostat idle)
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


def cost_overview(cfg, plan):
    """Estimated cost + saving vs 'always-on', using the configured kpWh price."""
    price = float(cfg.get("PRICE_PER_KWH") or 0.0)
    if not plan:
        return None
    plan_kwh = plan.get("energy_kwh")
    if not isinstance(plan_kwh, (int, float)):
        return None
    c = {"price_per_kwh": price, "plan_kwh": plan_kwh, "cost_plan": plan_kwh * price}
    alwayson = plan.get("energy_alwayson_kwh")
    if isinstance(alwayson, (int, float)):
        cost_always = alwayson * price
        saving_kwh = max(0.0, alwayson - plan_kwh)
        saving_cost = max(0.0, cost_always - plan_kwh * price)
        c.update({
            "alwayson_kwh": alwayson, "cost_alwayson": cost_always,
            "saving_kwh": saving_kwh, "saving_cost": saving_cost,
            "saving_pct": (saving_cost / cost_always * 100.0) if cost_always > 0 else 0.0,
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
        "cost": cost_overview(cfg, app._plan),
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
        self._plan_ts = 0.0
        self._tokens = None
        self._stopping = False
        self._log = []              # recent operations, newest last
        self._log_lock = threading.Lock()
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
        self._broadcast("state")

    def get_state(self):
        with self._lock:
            return dict(self._state), self._state_ts

    def get_history(self, hours):
        cutoff = int(time.time()) - int(hours * 3600)
        with self._lock:
            return [dict(h) for h in self._history if h["ts"] >= cutoff]

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

        Pairs shadow readings roughly two hours apart while the tub is well
        above its setpoint (so the thermostat cannot have been heating), and
        keeps a rolling median. Once enough samples exist the fitted value is
        saved to scheduler_model.json and used by the next replan.
        """
        now = int(time.time())
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
        if dT > -0.5:
            return  # no clear cooling at 1 C sensor resolution
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
                     "Cooling rate measured: UA=%.4f kW/°C from %d coast-downs"
                     % (new_ua, len(self._ua_samples)))

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
        if c in ("/help", "/start"):
            return ("Commands: <code>/status</code> full state · "
                    "<code>/mode</code> mode/phase/target · "
                    "<code>/plan</code> schedule · <code>/help</code> this")
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
    def refresh_plan(self, apply=False):
        """Run one scheduler step (weather + plan; apply only if auto mode)."""
        res = scheduler.scheduler_step(
            self.cfg.scheduler_cfg, self._lat, self._lon, state=self.get_state()[0],
            tokens=self._get_tokens(), apply=apply)
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
            self.refresh_plan(apply=(self.cfg.mode == "auto"))
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; server logs at /api/health time

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/state", "/api/health", "/api/history", "/api/plan",
                    "/api/scheduler", "/api/weather", "/api/log",
                    "/api/location/search"):
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
            out = {
                "ts": int(ts),
                "mode": app.cfg.mode,
                "state": state,
                "heater": heater_status(state),
                "plan_ts": int(app._plan_ts) if app._plan else None,
            }
            if app._plan:
                out["phase"] = app._plan["phase"]
                out["setting"] = app._plan["setting"]
            self._send_json(200, out)
        elif path == "/api/health":
            state, ts = app.get_state()
            self._send_json(200, {
                "online": bool(state),
                "last_update": int(ts),
                "age_s": int(time.time() - ts) if ts else None,
                "mode": app.cfg.mode,
            })
        elif path == "/api/history":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            hours = float(qs.get("range", ["24"])[0])
            self._send_json(200, {"series": app.get_history(min(hours, 168))})
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
                    app.refresh_plan(apply=True)
                except Exception as e:
                    app.add_log("err", "Auto start failed: %s" % e)
                    return self._send_json(502, {"error": "auto-start failed: %s" % e})
        if body.get("replan"):
            try:
                app.refresh_plan(apply=(app.cfg.mode == "auto"))
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
    """Periodically fetch the shadow and append history.

    The shadow is polled every POLL_SECONDS; the heating plan (which costs a
    weather API call) is refreshed at most once per hour, unless a replan is
    explicitly requested.
    """
    next_plan = 0.0  # always refresh on first iteration
    while not stop_event.is_set():
        now = time.time()
        try:
            state = spa.shadow(app._get_tokens())
            app.update_state(state)
        except Exception as e:
            print("[%s] poller error: %s" % (time.strftime("%F %T"), e), flush=True)
            app.add_log("err", "Spa poll failed: %s" % e)
        # plan refresh aligned to hour boundaries (weather API budget + so the
        # preheat/window transitions are picked up on time)
        if now >= next_plan:
            try:
                app.refresh_plan(apply=(app.cfg.mode == "auto"))
            except Exception:
                pass
            next_plan = now - (now % 3600) + 3600  # top of next hour
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
