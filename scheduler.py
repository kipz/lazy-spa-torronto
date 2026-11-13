#!/usr/bin/env python3
"""
Weather-aware heating scheduler for the Lay-Z-Spa Toronto UltraFit.

Optimises when to run the 2 kW heater so the tub is at TARGET_TEMP during
configured use windows while minimising total heating energy, using a free
hourly weather forecast (Open-Meteo, no API key) and a fitted thermal model
of the tub.

Thermal model (Newton's law + heater, hourly discretised):
    C * dT/dt = P_heat * u(t) - UA * (T(t) - T_amb(t))
    C   thermal mass of the water      (~1.45 kWh/degC for 1250 L)
    P   heater power                   (~2 kW)
    UA  heat-loss coefficient          (fitted; ~0.02-0.05 kW/degC covered)
    u   heater on/off (0/1)

Optimal policy for this first-order system with a hard deadline is bang-bang:
heat at full power only as late as needed ("optimal start"). We compute the
minimum viable temperature trajectory backward from the horizon, then run the
heater forward only when coasting would violate a future requirement. Re-run
hourly with a fresh forecast (receding horizon / MPC flavour).

Usage:
  python3 scheduler.py plan                       # dry run: print 48h plan
  python3 scheduler.py plan --simulate            # offline demo (no network)
  python3 scheduler.py run                        # persistent controller loop
  python3 scheduler.py calibrate                  # fit UA/P from shadow log
  python3 scheduler.py selftest                   # physics sanity check

Config is read from scheduler.env (gitignored); see scheduler.env.example.
Weather: Open-Meteo Forecast API (CC BY 4.0, free non-commercial).
"""
import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Open-Meteo endpoints ---------------------------------------------------
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
UA = "Mozilla/5.0 (LayZspa-scheduler/1.0)"

# --- defaults (overridden by scheduler.env / calibration) -------------------
DEFAULTS = {
    "LOCATION": "London, UK",
    "LAT": None,
    "LON": None,
    "WINDOW": "17:00-23:00",        # base daily use window
    "TARGET_TEMP": 40,              # use-window setpoint (device clamp: 40)
    "ECO_TEMP": 28,                 # comfort floor outside windows
    "FLOOR_TEMP": 8,                # hard floor (freeze protection)
    "MARGIN": 0.0,                  # extra degC safety above TARGET
    "C_KWH_PER_C": 1.45,            # 1250 L water
    "P_HEAT_KW": 2.0,               # heater power
    "UA_KW_PER_C": 0.03,            # initial guess; calibration overwrites
    "WIND_SENSITIVITY": 0.0,        # UA scaling per m/s wind (0 = ignore)
    "SOLAR_GAIN_KW": 0.0,           # peak kW absorbed from sun on cover
    "HORIZON_H": 168,               # planning horizon (hours) == 7-day forecast
    "STEP_H": 1.0,                  # control timestep (hours)
    "LOG_FILE": "scheduler.log",
}

HOUR = 3600.0
EPS = 1e-6

# WMO weather code -> (label, emoji)
WMO_TEXT = {
    0: ("Clear", "☀️"), 1: ("Mostly clear", "🌤"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫"), 48: ("Rime fog", "🌫"),
    51: ("Light drizzle", "🌦"), 53: ("Drizzle", "🌦"), 55: ("Drizzle", "🌧"),
    56: ("Freezing drizzle", "🌧"), 57: ("Freezing drizzle", "🌧"),
    61: ("Light rain", "🌧"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"), 73: ("Snow", "🌨"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨"),
    80: ("Light showers", "🌦"), 81: ("Showers", "🌧"), 82: ("Heavy showers", "🌧"),
    85: ("Snow showers", "🌨"), 86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunder + hail", "⛈"), 99: ("Thunder + hail", "⛈"),
}


def weather_condition(code):
    """Return (label, emoji) for a WMO weather code."""
    return WMO_TEXT.get(int(code or 0), ("Unknown", "🌡"))


# --- tiny stdlib HTTP helpers -------------------------------------------------
def http_get_json(url, params=None, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load_env(path):
    values = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
    return values


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_env(os.path.join(BASE_DIR, "scheduler.env")))
    for k in ("TARGET_TEMP", "ECO_TEMP", "FLOOR_TEMP", "MARGIN",
              "C_KWH_PER_C", "P_HEAT_KW", "UA_KW_PER_C", "WIND_SENSITIVITY",
              "SOLAR_GAIN_KW", "HORIZON_H", "STEP_H"):
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            pass
    cfg["HORIZON_H"] = int(cfg["HORIZON_H"])
    # Calibrated (or online-measured) thermal parameters override the
    # scheduler.env/default guesses. This keeps the CLI and the web server
    # using the same fitted model.
    model_path = os.path.join(BASE_DIR, "scheduler_model.json")
    if os.path.exists(model_path):
        try:
            with open(model_path) as f:
                model = json.load(f)
        except (OSError, ValueError):
            model = {}
        for key in ("UA_KW_PER_C", "P_HEAT_KW"):
            val = model.get(key)
            if isinstance(val, (int, float)):
                cfg[key] = float(val)
    if cfg["LAT"] not in (None, "") and cfg["LON"] not in (None, ""):
        try:
            cfg["LAT"] = float(cfg["LAT"])
            cfg["LON"] = float(cfg["LON"])
        except (TypeError, ValueError):
            cfg["LAT"] = cfg["LON"] = None
    else:
        cfg["LAT"] = cfg["LON"] = None
    return cfg


def model_info(cfg):
    """Describe the thermal parameters currently in use and their origin."""
    info = {
        "ua": round(float(cfg["UA_KW_PER_C"]), 4),
        "p": round(float(cfg["P_HEAT_KW"]), 2),
        "samples": 0,
        "source": "default",
        "saved_at": None,
    }
    path = os.path.join(BASE_DIR, "scheduler_model.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                model = json.load(f)
        except (OSError, ValueError):
            model = {}
        if model:
            info["samples"] = int(model.get("ua_samples") or 0)
            info["source"] = model.get("source") or "calibrated"
            info["saved_at"] = model.get("saved_at")
    return info


# --- location ----------------------------------------------------------------
def geocode(query, count=8):
    """Return place suggestions for the UI (Open-Meteo geocoding)."""
    query = str(query or "").strip()
    if len(query) < 2:
        return []
    res = http_get_json(GEOCODE_URL, {
        "name": query, "count": int(count), "language": "en", "format": "json"})
    out, seen = [], set()
    for h in res.get("results") or []:
        try:
            lat, lon = float(h["latitude"]), float(h["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (round(lat, 3), round(lon, 3))
        if key in seen:
            continue
        seen.add(key)
        name = h.get("name") or query
        admin = h.get("admin1") or ""
        country = h.get("country") or h.get("country_code") or ""
        label = ", ".join(p for p in (name, admin, country) if p)
        out.append({
            "label": label, "name": name, "admin1": admin, "country": country,
            "country_code": h.get("country_code"), "lat": lat, "lon": lon,
        })
    return out


def resolve_location(cfg):
    if cfg["LAT"] is not None and cfg["LON"] is not None:
        return cfg["LAT"], cfg["LON"], cfg["LOCATION"]
    for query in (cfg["LOCATION"], cfg["LOCATION"].split(",")[0]):
        res = http_get_json(GEOCODE_URL, {
            "name": query, "count": 1, "language": "en", "format": "json"})
        hits = res.get("results") or []
        if hits:
            h = hits[0]
            return h["latitude"], h["longitude"], "%s, %s" % (h.get("name"), h.get("country_code", "?"))
    sys.exit("Geocoding failed for %r. Set LAT=/LON= in scheduler.env." % cfg["LOCATION"])


# --- weather -----------------------------------------------------------------
def fetch_forecast(lat, lon, past_days=0, forecast_days=3):
    """Return {epoch_hour: (temp_c, wind_ms, sw_wm2)} for hourly slots."""
    res = http_get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,shortwave_radiation",
        "past_days": past_days, "forecast_days": forecast_days,
        "timeformat": "unixtime", "timezone": "auto",
        "wind_speed_unit": "ms",
    })
    hourly = res.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    winds = hourly.get("wind_speed_10m") or []
    sw = hourly.get("shortwave_radiation") or []
    out = {}
    for i, t in enumerate(times):
        t = int(t)
        out[t - (t % 3600)] = (
            temps[i] if i < len(temps) and temps[i] is not None else None,
            winds[i] if i < len(winds) and winds[i] is not None else 0.0,
            sw[i] if i < len(sw) and sw[i] is not None else 0.0,
        )
    return out


def fetch_weather_detail(lat, lon, days=7):
    """Rich weather for the web UI: current + hourly + daily summaries."""
    res = http_get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "dew_point_2m,precipitation,weather_code,wind_speed_10m,"
                    "wind_direction_10m,surface_pressure,cloud_cover,is_day"),
        "hourly": ("temperature_2m,apparent_temperature,dew_point_2m,"
                   "relative_humidity_2m,precipitation_probability,precipitation,"
                   "rain,snowfall,weather_code,wind_speed_10m,wind_gusts_10m,"
                   "wind_direction_10m,cloud_cover,surface_pressure,"
                   "shortwave_radiation,is_day"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "apparent_temperature_max,precipitation_probability_max,"
                  "precipitation_sum,wind_speed_10m_max"),
        "forecast_days": int(days), "timezone": "auto", "timeformat": "unixtime",
        "wind_speed_unit": "ms",
    })
    hourly = []
    h = res.get("hourly") or {}
    n = len(h.get("time", []))
    for i in range(n):
        ts = int(h["time"][i])
        label, emoji = weather_condition(h["weather_code"][i])
        hourly.append({
            "ts": ts - (ts % 3600), "temp": h["temperature_2m"][i],
            "feels": h["apparent_temperature"][i], "dew": h["dew_point_2m"][i],
            "humidity": h["relative_humidity_2m"][i],
            "precipProb": h["precipitation_probability"][i],
            "precip": h["precipitation"][i], "rain": h["rain"][i],
            "snowfall": h["snowfall"][i], "wind": h["wind_speed_10m"][i],
            "gust": h["wind_gusts_10m"][i], "windDir": h["wind_direction_10m"][i],
            "cloud": h["cloud_cover"][i], "pressure": h["surface_pressure"][i],
            "sw": h["shortwave_radiation"][i], "isDay": h["is_day"][i],
            "code": h["weather_code"][i], "label": label, "emoji": emoji,
        })
    daily = []
    dd = res.get("daily") or {}
    for i in range(len(dd.get("time", []))):
        label, emoji = weather_condition(dd["weather_code"][i])
        daily.append({"date": dd["time"][i], "code": dd["weather_code"][i],
                      "label": label, "emoji": emoji, "hi": dd["temperature_2m_max"][i],
                      "lo": dd["temperature_2m_min"][i],
                      "feelsHi": dd["apparent_temperature_max"][i],
                      "precipProb": dd["precipitation_probability_max"][i],
                      "precip": dd["precipitation_sum"][i],
                      "wind": dd["wind_speed_10m_max"][i]})
    c = res.get("current") or {}
    label, emoji = weather_condition(c.get("weather_code"))
    current = {"ts": c.get("time"), "temp": c.get("temperature_2m"),
               "feels": c.get("apparent_temperature"), "dew": c.get("dew_point_2m"),
               "humidity": c.get("relative_humidity_2m"), "wind": c.get("wind_speed_10m"),
               "windDir": c.get("wind_direction_10m"), "pressure": c.get("surface_pressure"),
               "cloud": c.get("cloud_cover"), "precip": c.get("precipitation"),
               "isDay": c.get("is_day"), "code": c.get("weather_code"),
               "label": label, "emoji": emoji}
    return {"current": current, "hourly": hourly, "daily": daily, "days": days}


def forecast_series(cfg, weather, now):
    """Slice an hourly list of (ts, t_amb_c) starting at the current hour."""
    h = int(cfg["STEP_H"])
    start = int(now) - (int(now) % 3600)
    ts = start
    series = []
    for _ in range(cfg["HORIZON_H"]):
        entry = weather.get(ts)
        t_amb = entry[0] if entry else None
        if t_amb is None:
            # fill with last known or a conservative guess (previous hour)
            t_amb = series[-1][1] if series else cfg["ECO_TEMP"]
        series.append((ts, t_amb))
        ts += h * 3600
    return series


# --- use windows --------------------------------------------------------------
def parse_window_spec(spec):
    start_s, _, end_s = spec.strip().partition("-")
    hm = lambda s: (int(s.split(":")[0]), int(s.split(":")[1]))
    return hm(start_s), hm(end_s)


def windows_from_config(cfg):
    """Return {weekday_int: [(start_hm, end_hm), ...]} with base + per-day overrides."""
    days = {}
    base = parse_window_spec(cfg["WINDOW"])
    names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    for i, name in enumerate(names):
        spec = cfg.get("WINDOW_" + name) or cfg["WINDOW"]
        days[i] = [parse_window_spec(spec)]
    return days


def in_window(ts, windows):
    """ts is epoch seconds; decide using local wall clock.

    A window whose end is earlier than its start crosses midnight, e.g.
    ``22:00-01:00`` runs from 22:00 on its configured day until 01:00 the
    next day. The morning tail therefore belongs to the *previous* weekday's
    window. A missing weekday key means "no window that day".
    """
    local = dt.datetime.fromtimestamp(ts)
    hm = (local.hour, local.minute)

    # Windows starting today: same-day, or the evening part of an overnight one.
    for s, e in windows.get(local.weekday(), []):
        if s < e:
            if s <= hm < e:
                return True
        elif hm >= s:                 # crosses midnight, evening portion
            return True

    # Morning tail of an overnight window that started yesterday.
    prev = (local.weekday() - 1) % 7
    for s, e in windows.get(prev, []):
        if s > e and hm < e:
            return True
    return False


# --- thermal model -------------------------------------------------------------
def make_ua(cfg, wind_ms=0.0):
    ua = cfg["UA_KW_PER_C"]
    if cfg["WIND_SENSITIVITY"]:
        ua = ua * (1.0 + cfg["WIND_SENSITIVITY"] * wind_ms)
    return ua


def simulate(cfg, series, T0, windows, u_schedule=None, verbose=False):
    """Forward-integrate the model over `series`.

    u_schedule: optional list of heater decisions (len == len(series)).
    If None, decides: heater ON during use windows (holds target, like the
    real thermostat), and otherwise only when coasting would violate the
    required trajectory (eco floor / preheat need).
    Returns (T_list, u_list, energy_kwh). Temperature is clamped at 40 C.
    """
    C, P = cfg["C_KWH_PER_C"], cfg["P_HEAT_KW"]
    target = cfg["TARGET_TEMP"] + cfg["MARGIN"]
    eco = cfg["ECO_TEMP"]
    step = cfg["STEP_H"]
    clamp = 40.0  # device hard clamp
    T = T0
    temps = [T]
    us = []
    energy = 0.0
    for i, (ts, t_amb) in enumerate(series):
        ua = make_ua(cfg)
        if u_schedule is not None:
            u = u_schedule[i]
        elif in_window(ts, windows):
            u = 1  # hold target during use windows
        else:
            # required temp at the END of this hour (eco floor unless a window
            # starts then)
            req_end = target if in_window(ts + 3600, windows) else eco
            T_coast = T + step / C * (-ua * (T - t_amb))
            u = 1 if T_coast < req_end - EPS else 0
        loss = ua * (T - t_amb)
        if u:
            if T < clamp - EPS:
                T = T + step / C * (P - loss)
                energy += P * step
            else:
                T = clamp  # already at cap: heater only offsets loss
                energy += loss * step
        else:
            T = T - step / C * loss
        T = min(T, clamp)  # device hard clamp applies after every hour
        temps.append(T)
        us.append(u)
    return temps, us, energy


def optimal_start(cfg, series, T0, windows):
    """Backward induction: latest heating start so every future requirement holds.

    Returns list of heater decisions (0/1) of length len(series) implementing
    the minimum-energy bang-bang policy, plus the resulting energy in kWh.
    """
    C, P = cfg["C_KWH_PER_C"], cfg["P_HEAT_KW"]
    target = cfg["TARGET_TEMP"] + cfg["MARGIN"]
    eco = cfg["ECO_TEMP"]
    step = cfg["STEP_H"]
    clamp = 40.0
    n = len(series)

    # req[i] = minimum temperature that must hold at the START of hour i
    req = [0.0] * (n + 1)
    req[n] = eco  # horizon end: at least eco
    for i in range(n - 1, -1, -1):
        ts, t_amb = series[i]
        r_next = req[i + 1]
        # local constraint at the start of this hour
        local = target if in_window(ts, windows) else eco
        if local >= clamp - EPS:
            req[i] = local
            continue
        a = 1.0 - (step / C) * make_ua(cfg)  # coefficient on T
        b_off = (step / C) * make_ua(cfg) * t_amb
        b_on = (step / C) * (P + make_ua(cfg) * t_amb)
        need_off = (r_next - b_off) / a
        need_on = (r_next - b_on) / a
        # the lowest start temp from which hour i can still reach r_next is the
        # heating case (need_on < need_off): heat only when coasting can't cope
        req[i] = max(local, min(need_off, need_on))
        req[i] = min(req[i], clamp)

    # forward sweep: hold during windows; heat when coasting would violate req.
    # Heating is also required when the tub is already at the clamp and a
    # future requirement is at the clamp: the water still loses heat, so
    # skipping that hour would let it drift below target before the window.
    T = T0
    us = []
    for i in range(n):
        ts, t_amb = series[i]
        ua = make_ua(cfg)
        if in_window(ts, windows):
            us.append(1)
            T = T + step / C * (P - ua * (T - t_amb))
        else:
            T_coast = T + step / C * (-ua * (T - t_amb))
            if T_coast < req[i + 1] - EPS:
                us.append(1)
                T = T + step / C * (P - ua * (T - t_amb))
            else:
                us.append(0)
                T = T_coast
        T = min(T, clamp)
    _, _, energy = simulate(cfg, series, T0, windows, u_schedule=us)
    return us, energy, req


# --- calibration ---------------------------------------------------------------
def load_shadow_log():
    path = os.path.join(BASE_DIR, "shadow_log.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def calibrate_model(cfg, rows, weather):
    """Fit UA (heat loss) from heater-off coast-down stretches and P from
    genuine heating-phase stretches (temp rising, heater on).

    Holding stretches (heater on, temp ~constant at setpoint) are excluded
    from the P fit -- they measure loss-compensating power, not the heater's
    full output. Returns (ua, p, n_ua, n_p). Falls back to config defaults
    when fewer than 3 clean samples are available.
    """
    C = cfg["C_KWH_PER_C"]
    ua_samples, p_samples = [], []
    for prev, cur in zip(rows, rows[1:]):
        s1, s2 = prev["shadow"], cur["shadow"]
        dt_h = (cur["ts"] - prev["ts"]) / 3600.0
        if dt_h <= 0.02 or dt_h > 24:
            continue
        T1, T2 = s1.get("water_temperature"), s2.get("water_temperature")
        if not isinstance(T1, (int, float)) or not isinstance(T2, (int, float)):
            continue
        t = int(prev["ts"]) - (int(prev["ts"]) % 3600)
        t_amb = (weather.get(t) or [None])[0]
        if t_amb is None:
            continue
        dT = T2 - T1
        rate = dT / dt_h                       # degC/hour
        heat_on = (s1.get("power_state") in (1, "1")) and (s1.get("heater_state") not in (0, "0", None))
        Tavg = (T1 + T2) / 2.0
        if not heat_on:
            # clean coast-down: falling, not a fast sensor transient
            if rate < -0.05 and abs(Tavg - t_amb) > 3.0:
                ua = C * (-rate) / (Tavg - t_amb)   # kW/degC
                if 0.005 <= ua <= 0.3:
                    ua_samples.append(ua)
        else:
            # genuine heating phase only: temp clearly rising
            if rate > 0.05:
                ua = cfg["UA_KW_PER_C"]
                p = C * rate + ua * (Tavg - t_amb)
                if 0.5 <= p <= 6.0:
                    p_samples.append(p)
    ua = statistics.median(ua_samples) if len(ua_samples) >= 3 else cfg["UA_KW_PER_C"]
    p = statistics.median(p_samples) if len(p_samples) >= 3 else cfg["P_HEAT_KW"]
    return ua, p, len(ua_samples), len(p_samples)


# --- reporting ------------------------------------------------------------------
def fmt_hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%a %H:%M")


def print_plan(cfg, series, T0, windows):
    us, energy, _ = optimal_start(cfg, series, T0, windows)
    temps, _, _ = simulate(cfg, series, T0, windows, u_schedule=us)
    print("Heating plan for the next %d hours (target %g C from window start)"
          % (len(series), cfg["TARGET_TEMP"] + cfg["MARGIN"]))
    print("  current water temp : %g C" % T0)
    print("  %-9s %6s %6s %7s  %s" % ("time", "amb", "water", "heat", "window"))
    for i, (ts, t_amb) in enumerate(series):
        mark = "USE" if in_window(ts, windows) else ""
        print("  %-9s %6g %6.1f %6s  %s" % (fmt_hm(ts), t_amb, temps[i + 1],
                                              "ON" if us[i] else "-", mark))
    first_on = next((series[i][0] for i in range(len(us)) if us[i]), None)
    print("  forecast energy    : %.1f kWh" % energy)
    if first_on is not None:
        print("  heater first turns : %s" % fmt_hm(first_on))
    else:
        print("  heater first turns : (none needed)")
    # safety check
    for i, (ts, _) in enumerate(series):
        if in_window(ts, windows) and temps[i + 1] < cfg["TARGET_TEMP"] - 0.5:
            print("  !! at %s water is only %g C (below target) -- increase ECO_TEMP"
                  % (fmt_hm(ts), temps[i + 1]))
            break
    return energy


def simulate_demo(cfg, windows):
    """Offline demo: synthetic diurnal weather, no network."""
    print("SIMULATION (synthetic weather, no network)")
    now = time.time()
    start = int(now) - (int(now) % 3600)
    series = []
    for k in range(cfg["HORIZON_H"]):
        ts = start + k * 3600
        hour = dt.datetime.fromtimestamp(ts).hour
        t_amb = 6.0 + 8.0 * math.sin(math.pi * (hour - 6) / 12.0)  # 6C night, 14C day
        series.append((ts, round(t_amb, 1)))
    T0 = cfg["ECO_TEMP"]
    print_plan(cfg, series, T0, windows)


def hot_phase_now(series, us, windows):
    """Classify the current hour for temperature_setting purposes.

    Returns "window" (inside use window), "preheat" (heating block that runs
    straight into a window -> set TARGET now), or "eco" (coast/hold at ECO).
    """
    if in_window(series[0][0], windows):
        return "window"
    # find the first window slot within the horizon
    j = next((i for i in range(len(series)) if in_window(series[i][0], windows)), None)
    if j is None:
        return "eco"
    # heating must be continuous from now until the window for this to be
    # "preheat" (the heater runs the whole way into the window)
    if us[0] == 1 and all(us[k] == 1 for k in range(0, j)):
        return "preheat"
    return "eco"


def phase_series(series, us, windows):
    """Classify every plan hour: window / preheat / eco (see hot_phase_now)."""
    n = len(series)
    phases = []
    for i in range(n):
        if in_window(series[i][0], windows):
            phases.append("window")
            continue
        j = next((k for k in range(i, n) if in_window(series[k][0], windows)), None)
        if j is None:
            phases.append("eco")
            continue
        if us[i] == 1 and all(us[k] == 1 for k in range(i, j)):
            phases.append("preheat")
        else:
            phases.append("eco")
    return phases


def scheduler_step(cfg, lat, lon, state=None, tokens=None, apply=True):
    """Run one hourly scheduler step: fetch weather -> replan -> (optionally) apply.

    Returns a dict: {phase, setting, water_temp, ambient_now, plan_ts, plan_us,
    plan_amb, energy_kwh, changed}. This is the shared unit used by both the
    CLI `run` loop and the web server's scheduler thread.
    """
    import spa  # reuses auth/command machinery
    horizon_days = max(3, (int(cfg["HORIZON_H"]) + 23) // 24)
    weather = fetch_forecast(lat, lon, forecast_days=horizon_days)
    t0 = time.time()
    series = forecast_series(cfg, weather, t0)
    if state is None:
        state = spa.shadow(tokens if tokens is not None else spa.get_tokens())
    T0 = state.get("water_temperature")
    if not isinstance(T0, (int, float)):
        T0 = cfg["ECO_TEMP"]
    windows = windows_from_config(cfg)
    us, energy, _ = optimal_start(cfg, series, T0, windows)
    phase = hot_phase_now(series, us, windows)
    setting = int(cfg["TARGET_TEMP"]) if phase in ("window", "preheat") else int(cfg["ECO_TEMP"])
    # Projected water trace + per-hour setpoint/phase for the UI forecast.
    temps, _, _ = simulate(cfg, series, T0, windows, u_schedule=us)
    phases = phase_series(series, us, windows)
    target = float(cfg["TARGET_TEMP"]) + float(cfg["MARGIN"])
    eco = float(cfg["ECO_TEMP"])
    changed = False
    if apply:
        if int(state.get("power_state") or 0) != 1:
            spa.send_command(tokens, "power_state", 1)
            changed = True
        if int(state.get("temperature_setting") or 0) != setting:
            spa.send_command(tokens, "temperature_setting", setting)
            changed = True
        if not state.get("heater_state"):
            spa.send_command(tokens, "heater_state", 1)
            changed = True
        if int(state.get("filter_state") or 0) == 0:
            spa.send_command(tokens, "filter_state", 1)
            changed = True
    return {
        "phase": phase,
        "setting": setting,
        "water_temp": T0,
        "ambient_now": series[0][1],
        "plan_ts": [s[0] for s in series],
        "plan_amb": [s[1] for s in series],
        "plan_us": us,
        "plan_water": [round(t, 2) for t in temps[:-1]],
        "plan_set": [target if p != "eco" else eco for p in phases],
        "plan_phase": phases,
        "energy_kwh": energy,
        "changed": changed,
        "series_start": series[0][0],
    }


def run_controller(cfg, lat, lon, dry_run=False):
    """Persistent loop: every hour, run a scheduler step via scheduler_step.

    Controls temperature_setting (TARGET during use windows / preheat, ECO
    otherwise) and lets the spa's own thermostat run the heater -- switching
    the setpoint is gentler and more accurate than toggling heater_state.
    """
    tokens = None if dry_run else __import__("spa").get_tokens()
    log_path = os.path.join(BASE_DIR, cfg["LOG_FILE"])
    last_setting = None
    while True:
        t0 = time.time()
        try:
            res = scheduler_step(cfg, lat, lon, tokens=tokens, apply=not dry_run)
            action = ""
            if res["changed"]:
                action = ("  [would set temp_setting=%d]" if dry_run
                          else "  [applied temp_setting=%d]") % res["setting"]
            line = "[%s] T=%gC phase=%s temp_setting=%d next=%s%s" % (
                time.strftime("%F %T"), res["water_temp"], res["phase"],
                res["setting"], fmt_hm(res["series_start"]), action)
            if res["setting"] != last_setting or res["changed"]:
                last_setting = res["setting"]
            with open(log_path, "a") as f:
                f.write(line + "\n")
            print(line, flush=True)
        except Exception as e:  # cloud / network issues: hold last state, retry
            print("[%s] scheduler error (retrying in 10 min): %s"
                  % (time.strftime("%F %T"), e), flush=True)
            time.sleep(600)
            continue
        # sleep to the top of the next hour (never shorter than 10 min)
        sleep_s = max(600.0, 3600.0 - (time.time() - t0))
        time.sleep(sleep_s)


def selftest():
    cfg = load_config()
    windows = windows_from_config(cfg)
    series = [(1e9 + i * 3600, 5.0) for i in range(24)]  # flat 5C night
    T0 = 28.0
    us, energy, req = optimal_start(cfg, series, T0, windows)
    temps, _, _ = simulate(cfg, series, T0, windows, u_schedule=us)
    ok = all((temps[i + 1] >= cfg["ECO_TEMP"] - 0.01) for i in range(24))
    # window check: with WINDOW=17:00-23:00 against a fake "now" we can't
    # guarantee wall-clock windows in a synthetic epoch, so verify invariants:
    ok = ok and all(u in (0, 1) for u in us)
    ok = ok and energy >= 0
    print("selftest: policy energy=%.1f kWh, %d ON hours, invariants %s"
          % (energy, sum(us), "OK" if ok else "FAIL"))
    return 0 if ok else 1


def load_fitted_model(cfg):
    """Merge scheduler_model.json (calibrated UA/P) into cfg if present."""
    model_path = os.path.join(BASE_DIR, "scheduler_model.json")
    if os.path.exists(model_path):
        with open(model_path) as f:
            m = json.load(f)
        cfg["UA_KW_PER_C"] = m.get("UA_KW_PER_C", cfg["UA_KW_PER_C"])
        cfg["P_HEAT_KW"] = m.get("P_HEAT_KW", cfg["P_HEAT_KW"])
        print("Loaded model: UA=%.4f kW/C, P=%.2f kW" %
              (cfg["UA_KW_PER_C"], cfg["P_HEAT_KW"]))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Lay-Z-Spa weather-aware heating scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print the heating plan (dry run)")
    p_plan.add_argument("--simulate", action="store_true",
                        help="offline demo with synthetic weather")
    p_run = sub.add_parser("run", help="persistent controller loop (applies commands)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="log the intended actions without touching the spa")
    sub.add_parser("calibrate", help="fit thermal model from shadow log + weather")
    sub.add_parser("selftest", help="physics sanity checks")
    args = ap.parse_args()

    cfg = load_config()
    windows = windows_from_config(cfg)

    if args.cmd == "selftest":
        sys.exit(selftest())

    if args.cmd == "plan":
        if args.simulate:
            simulate_demo(cfg, windows)
            return
        cfg = load_fitted_model(cfg)
        lat, lon, where = resolve_location(cfg)
        print("Location: %s (%.4f, %.4f)" % (where, lat, lon))
        weather = fetch_forecast(lat, lon)
        series = forecast_series(cfg, weather, time.time())
        import spa
        tokens = spa.get_tokens()
        state = spa.shadow(tokens)
        T0 = state.get("water_temperature")
        if not isinstance(T0, (int, float)):
            T0 = cfg["ECO_TEMP"]
        print_plan(cfg, series, T0, windows)
        return

    if args.cmd == "calibrate":
        lat, lon, where = resolve_location(cfg)
        print("Location: %s (%.4f, %.4f)" % (where, lat, lon))
        rows = load_shadow_log()
        if not rows:
            sys.exit("No shadow_log.jsonl data to fit. Run 'spa.py watch --raw' "
                     "overnight with the heater off first.")
        days = max(1, int((time.time() - rows[0]["ts"]) // 86400) + 2)
        print("Fetching %d days of historical weather..." % days)
        weather = fetch_forecast(lat, lon, past_days=days)
        ua, p, nu, np_ = calibrate_model(cfg, rows, weather)
        cfg["UA_KW_PER_C"] = ua
        cfg["P_HEAT_KW"] = p
        with open(os.path.join(BASE_DIR, "scheduler_model.json"), "w") as f:
            json.dump({"UA_KW_PER_C": ua, "P_HEAT_KW": p,
                       "fitted_from": len(rows), "ua_samples": nu,
                       "p_samples": np_, "saved_at": int(time.time())},
                      f, indent=2)
        os.chmod(os.path.join(BASE_DIR, "scheduler_model.json"), 0o600)
        print("UA = %.4f kW/degC (%d coast samples), P = %.2f kW (%d heat samples)"
              % (ua, nu, p, np_))
        print("Saved scheduler_model.json")
        return

    if args.cmd == "run":
        cfg = load_fitted_model(cfg)
        lat, lon, where = resolve_location(cfg)
        print("Location: %s (%.4f, %.4f)" % (where, lat, lon))
        run_controller(cfg, lat, lon, dry_run=getattr(args, "dry_run", False))


if __name__ == "__main__":
    main()
