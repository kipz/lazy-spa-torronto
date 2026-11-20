#!/usr/bin/env python3
"""Torronto plug — read the tub's smart plug power usage via the Tuya cloud.

The tub is fed by a Tuya / Smart Life energy-monitoring plug ("Tub mains").
It sits behind the garden router's NAT, so it can't be reached over the LAN
from here; instead we read it through the Tuya *cloud* with the account-level
device-sharing SDK — the same flow the Home Assistant Tuya integration uses.

One-time setup (done once, then the session is cached and auto-refreshed):

  1. Log in once with the QR-code flow (Smart Life app -> "+ -> Scan" ->
     "Confirm login"). The generated session JSON is saved next to this file
     as ``tuya.session.json`` (gitignored, chmod 600).
  2. Optionally pin the device in web.env::

        TUYA_DEVICE_ID=bf44bba069707d19bf5ysd   # auto-detected when blank
        TUYA_SESSION=/path/to/tuya.session.json # default: ./tuya.session.json

Runtime dependency: ``tuya-device-sharing-sdk`` (pip). It is imported lazily,
so the rest of the project runs fully (and stays dependency-free) without it
installed; ``read()`` just returns ``None``.

Everything is safe to call before/without configuration — the functions
return ``None`` / ``{}`` on missing config or any failure, and never raise,
so the web server can't be broken by plug wiring (or a flaky cloud).
"""
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SESSION = os.path.join(BASE_DIR, "tuya.session.json")
DEFAULT_DEVICE_NAME = "Tub mains"
# Which plug devices to monitor (names). The first/energy one ("Tub mains")
# is the meter whose wattage is integrated into the "today" kWh counter.
DEFAULT_DEVICE_NAMES = "Tub mains,Tub lights"

# Tuya datapoint codes we surface out of the plug's status report.
DP_SWITCH = "switch_1"
DP_POWER = "cur_power"
DP_VOLTAGE = "cur_voltage"
DP_CURRENT = "cur_current"
DP_ENERGY = "add_ele"

SESSION_KEYS = ("client_id", "user_code", "terminal_id", "endpoint", "token_info")
TOKEN_FIELDS = ("t", "uid", "expire_time", "access_token", "refresh_token")


def _cfg():
    """Read web.env tuya/plug keys (empty dict when web.env is absent)."""
    cfg = {}
    try:
        from scheduler import load_env
        env = load_env(os.path.join(BASE_DIR, "web.env"))
        cfg["device_id"] = env.get("TUYA_DEVICE_ID", "").strip()
        cfg["device_name"] = env.get("TUYA_DEVICE_NAME", "").strip()
        if not cfg["device_name"] and not cfg["device_id"]:
            cfg["device_name"] = DEFAULT_DEVICE_NAME
        cfg["devices"] = env.get("TUYA_DEVICES", "").strip() or DEFAULT_DEVICE_NAMES
        cfg["session"] = env.get("TUYA_SESSION", "").strip()
    except Exception:  # noqa: BLE001 - config is best-effort
        pass
    return cfg


def device_names():
    """Return the ordered list of plug device *names* to monitor.

    A single explicit ``TUYA_DEVICE_ID`` wins (names ignored); otherwise the
    comma-separated ``TUYA_DEVICES`` (or the default mains+lights set).
    """
    cfg = _cfg()
    if cfg.get("device_id"):
        return [cfg.get("device_name") or DEFAULT_DEVICE_NAME]
    names = [n.strip() for n in cfg.get("devices", "").split(",") if n.strip()]
    return names or [DEFAULT_DEVICE_NAME]


def is_energy_device(name):
    """Whether ``name`` is the meter whose wattage feeds the 'today' kWh."""
    return (name or "").lower() == DEFAULT_DEVICE_NAME.lower()


def session_path():
    """Resolve the session file path from web.env or the repo default."""
    return _cfg().get("session") or DEFAULT_SESSION


def configured():
    """True when a usable session file exists (tokens may still be expired)."""
    path = session_path()
    if not path:
        return False
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            session = json.load(f)
    except (OSError, ValueError):
        return False
    return all(session.get(k) is not None for k in SESSION_KEYS)


def _save_session(session, path):
    """Persist a session (tokens) atomically with 0600 perms."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(session, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def _spec_scale(spec):
    """Read the ``scale`` factor from a datapoint spec.

    ``spec`` is either a plain dict (web.env / repo format) or a
    ``DeviceStatusRange`` SimpleNamespace whose ``values`` holds a JSON string
    such as ``{"unit":"W","min":0,"max":50000,"scale":1,"step":1}``.
    """
    try:
        if isinstance(spec, dict):
            return int(spec.get("scale") or 0)
        values = getattr(spec, "values", None)
        if values:
            return int(json.loads(values).get("scale") or 0)
    except (TypeError, ValueError, AttributeError):
        pass
    return 0


def _spec_unit(spec):
    """Read the datapoint's ``unit`` string (lower-cased), or ''."""
    try:
        if isinstance(spec, dict):
            return str(spec.get("unit") or "")
        values = getattr(spec, "values", None)
        if values:
            return str(json.loads(values).get("unit") or "")
    except (TypeError, ValueError, AttributeError):
        pass
    return ""


def _scale_value(value, spec):
    """Apply a status_range scale (value / 10**scale). None when unusable."""
    if not isinstance(value, (int, float)):
        return None
    return value / (10.0 ** _spec_scale(spec))


class _TokenSaver:
    """Duck-typed tuya_sharing token listener that persists refreshed tokens.

    The SDK calls ``update_token(token_info)`` (camelCase keys as returned by
    the refresh endpoint) whenever it auto-refreshes; we rewrite the session
    file so the refresh survives server restarts.
    """

    def __init__(self, path, session):
        self.path = path
        self.session = session

    def update_token(self, token_info):
        info = dict(token_info or {})
        now = int(time.time() * 1000)
        self.session["token_info"] = {
            "t": info.get("t", now),
            "uid": info.get("uid", ""),
            "expire_time": info.get("expire_time") or info.get("expireTime", 7200),
            "access_token": info.get("access_token") or info.get("accessToken", ""),
            "refresh_token": info.get("refresh_token") or info.get("refreshToken", ""),
        }
        _save_session(self.session, self.path)


def _manager(session, path=None):
    """Build a tuya_sharing.Manager for the session (lazy import)."""
    from tuya_sharing import Manager

    path = path or session_path()
    saver = _TokenSaver(path, session)
    return Manager(
        session["client_id"],
        session["user_code"],
        session["terminal_id"],
        session["endpoint"],
        session["token_info"],
        saver,
    )


def _fetch_devices(manager, device_id, names):
    """Return matching CustomerDevices, ordered by name preference.

    All devices across the account's homes are visited once (fresh cloud
    status). A single ``device_id`` matches exactly; otherwise devices whose
    ``name`` is in ``names`` are returned in the configured order, keeping any
    configured name that has no match out of the result.
    """
    wanted = {n.lower() for n in (names or [])}
    present = {}
    homes = manager.home_repository.query_homes()
    for home in homes:
        for dev in manager.device_repository.query_devices_by_home(home.id):
            if device_id:
                if dev.id == device_id:
                    return [dev]
            elif dev.name and dev.name.lower() in wanted:
                present[dev.name.lower()] = dev
    if not device_id:
        ordered = []
        for n in (names or []):
            dev = present.get(n.lower())
            if dev is not None:
                ordered.append(dev)
        return ordered
    return []


def read_all(device_id=None, session_path_override=None):
    """Read status of every configured plug device from the Tuya cloud.

    Returns a list of normalised reading dicts (empty when unconfigured /
    nothing matches), or None when the cloud is unreachable. Never raises.
    """
    if not configured():
        return None
    path = session_path_override or session_path()
    try:
        with open(path) as f:
            session = json.load(f)
        cfg = _cfg()
        device_id = device_id or cfg.get("device_id") or None
        names = device_names()
        manager = _manager(session, path)
        devs = _fetch_devices(manager, device_id, names)
        return [_normalize(d) for d in devs]
    except Exception:  # noqa: BLE001 - the server must never break on this
        return None


def _normalize(dev, now=None):
    """Turn a CustomerDevice + its status/spec into a plain JSON-able dict."""
    now = int(now or time.time())
    status = dict(dev.status or {})
    spec = dev.status_range or {}
    watts = _scale_value(status.get(DP_POWER), spec.get(DP_POWER))
    voltage = _scale_value(status.get(DP_VOLTAGE), spec.get(DP_VOLTAGE))
    current = _scale_value(status.get(DP_CURRENT), spec.get(DP_CURRENT))
    # cur_current is defined in milliamps for this plug; scale to amps.
    cur_unit = _spec_unit(spec.get(DP_CURRENT)).lower()
    if current is not None and "ma" in cur_unit:
        current = current / 1000.0
    energy = _scale_value(status.get(DP_ENERGY), spec.get(DP_ENERGY))
    switch = status.get(DP_SWITCH)
    return {
        "ts": now,
        "device": dev.name,
        "device_id": dev.id,
        "online": bool(dev.online),
        "switch_on": (isinstance(switch, bool) and switch)
                      or switch in (1, "1", "true", "True", "on"),
        "watts": round(watts, 3) if watts is not None else None,
        "voltage_v": round(voltage, 1) if voltage is not None else None,
        "current_a": round(current, 3) if current is not None else None,
        "energy_kwh": round(energy, 4) if energy is not None else None,
        "fault": status.get("fault"),
        "source": "tuya-cloud",
    }


def read(device_id=None, session_path_override=None):
    """Read the primary (energy) plug's current status from the Tuya cloud.

    Returns the first reading dict on success, or None when unconfigured /
    no matching device / the cloud is unreachable. Never raises.
    """
    readings = read_all(device_id=device_id,
                        session_path_override=session_path_override) or []
    return readings[0] if readings else None


if __name__ == "__main__":
    import sys
    res = read()
    if res is None:
        print("plug: not reachable / not configured", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(res, indent=2))
