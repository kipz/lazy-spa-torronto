#!/usr/bin/env python3
"""
Lay-Z-Spa (Toronto UltraFit) cloud control CLI.

Unofficial protocol notes for the Bestway Lay-Z-Spa Android/iOS app
("Lay-Z-Spa WiFi App", com.wiltonbradley.layzspaapp):

  1. Account login goes to the Laravel gateway  app-api.lay-z-spa.co.uk
     POST /v1/login  {"email": ..., "password": "<MD5 of password, lowercase hex>"}
     The app hashes the password client-side with plain MD5 before sending.

  2. The response carries the spa's identity (mxchip_device: did / product_key)
     and an MXCHIP (AWS IoT) visitor token (mxchip_user_account.token).

  3. Status + commands go to the MXCHIP/AWS IoT backend  smarthub-eu.bestwaycorp.com
     with a signed request:  sign = MD5(appid + appsecret + nonce + ts) in UPPERCASE.

See PROTOCOL.md for the full details and the research log behind them.

Usage:
  python3 spa.py login            # (re)login and cache tokens
  python3 spa.py status [--raw]   # current state of the spa
  python3 spa.py watch            # poll every 15s, print on change
  python3 spa.py power  on|off
  python3 spa.py heat   on|off
  python3 spa.py filter on|off
  python3 spa.py wave   on|off    # AirJet bubbles
  python3 spa.py jet    on|off    # HydroJet (if equipped)
  python3 spa.py lock   on|off
  python3 spa.py temp    38       # target temperature (20-40 in the spa's unit)
  python3 spa.py raw <datapoint> <value>   # arbitrary write, e.g. raw wave_state 1

Credentials are read from secrets.env (EMAIL, PASSWORD); tokens are cached in
tokens.json so we do NOT re-login (repeated logins invalidate the phone app's
session).
"""
import argparse
import base64
import hashlib
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- constants identified from the app (see PROTOCOL.md) ---------------------
APPID = "AhFLL54HnChhrxcl9ZUJL6QNfolTIB"
APPSECRET = "4ECvVs13enL5AiYSmscNjvlaisklQDz7vWPCCWXcEFjhWfTmLT"
LARAVEL_ROOT = "https://app-api.lay-z-spa.co.uk"
SMARTHUB_ROOT = "https://smarthub-eu.bestwaycorp.com"
COMMAND_PATHS = ["/api/device/command", "/api/device/command/", "/api/v2/device/command"]

# --- SmartSpa gateway (Bestway Connect app, from ~July 2026) ------------------
# Bestway has migrated 2025+ UltraFit spas off the MXCHIP data plane
# (smarthub-*.bestwaycorp.com) onto the "Smart Home" backend its Connect app
# serves from smart-spa-{eu,us,cn}-app.bestwaycorp.com. The Lay-Z-Spa Laravel
# gateway's login now returns the binding as ``gizwits_2026_devices``; the
# token inside ``gizwits_user_account`` is this gateway's Authorization token.
# Datapoints on this backend are the same as the MXCHIP shadow
# (water_temperature, temperature_setting, heater_state 0-8, ...), so callers
# can stay unchanged. Protocol verified against live hardware by
# https://github.com/cdpuk/ha-bestway (v1.11, PR #142).
SMARTSPA_APP_ID = "39ef26ff404a4cb1b5652f336d4bc045"
SMARTSPA_REGION_DEFAULT = "eu"
SMARTSPA_ENV_FILES = ("web.env", "scheduler.env")
# Datapoints the SmartSpa control endpoint wants as a plain 1/0 regardless of
# the value (its heater_state semantics differ from e.g. 2/3 heating states).
SMARTSPA_BOOL_DPS = {"power_state", "heater_state", "filter_state",
                     "wave_state", "hydrojet_state", "locked"}
WEB_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36")

DATAPONT_ALIASES = {  # CLI name -> shadow datapoint
    "power": "power_state",
    "heat": "heater_state",
    "filter": "filter_state",
    "wave": "wave_state",
    "jet": "hydrojet_state",
    "lock": "locked",
}

HEATER_STATE_TEXT = {
    0: "off",
    1: "heating (starting)",
    2: "heating (1 element)",
    3: "heating (2 elements)",
    4: "holding temperature",
    5: "holding temperature",
    6: "holding temperature",
    7: "winter/frost mode",
    8: "winter/frost mode",
}


class SpaError(Exception):
    """A Bestway/MXCHIP cloud call failed (bad/expired token, device offline,
    or API error). Raised instead of ``sys.exit`` so long-running processes
    (the web server, the scheduler loop) can catch it and keep running rather
    than dying silently on a single bad response."""


def is_online(state):
    """Best-effort device reachability from a raw shadow dict.

    An absent ``is_online``/``ConnectType`` (old devices, partial payloads)
    is treated as online; an explicit offline marker wins. Callers that care
    about *freshness* must also check the age of the snapshot.
    """
    if not state:
        return False
    io = state.get("is_online")
    if io is not None:
        if isinstance(io, bool):
            if not io:
                return False
        elif str(io).strip().lower() not in ("1", "true", "online"):
            return False
    ct = state.get("ConnectType")
    if ct is not None and str(ct).strip().lower() == "offline":
        return False
    return True


# Message markers that mean "your cached token is dead" (as opposed to a
# transient network/device error). Bestway returns these as JSON error codes
# like 10001 "Token is not authorized"; the login gateway uses "Please login
# first" / "Login expired" / "Authentication failed".
AUTH_ERR_MARKS = ("token is not authorized", "not authorized", "unauthorized",
                  "10001", "please login", "login first", "login expired",
                  "session expired", "authentication failed", "auth failed")


def is_auth_error(exc):
    """True when a cloud exception is a token/auth problem.

    Used by the server to hot-reload refreshed tokens (and to alert) instead
    of waiting for a restart. Transient failures (DNS, device offline, 5xx)
    won't match.
    """
    msg = str(exc).lower()
    return any(m in msg for m in AUTH_ERR_MARKS)


# --- tiny stdlib HTTP helper --------------------------------------------------
def request_json(method, url, payload=None, headers=None, timeout=25):
    data = None
    hdrs = {"User-Agent": WEB_UA, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"http_error": e.code, "body": body[:400]}


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


# --- auth ---------------------------------------------------------------------
def _scrub_response(res):
    """Strip account secrets from a login response so errors never leak them.

    The gateway's login response carries ``api_token``, ``push_notification_token``
    and (normally) the mxchip token; any of those ending up in a log/error is a
    credential leak. Replaces high-entropy values, keeps the structure readable.
    """
    SECRET_KEYS = ("api_token", "push_notification_token", "token",
                   "access_token", "refresh_token")

    def clean(v):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k in SECRET_KEYS and x is not None:
                    out[k] = "%%REDACted%%" if x not in (None, "", 0) else clean(x)
                elif k == "email" and isinstance(x, str):
                    out[k] = "…@" + x.rsplit("@", 1)[-1] if "@" in x else "…"
                else:
                    out[k] = clean(x)
            return out
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    return clean(res)


def _smartspa_region():
    """Region for the SmartSpa gateway, from env (SMARTSPA_REGION) or 'eu'."""
    for name in SMARTSPA_ENV_FILES:
        env = load_env(os.path.join(BASE_DIR, name))
        region = env.get("SMARTSPA_REGION", "").strip()
        if region:
            return region.lower()
    return SMARTSPA_REGION_DEFAULT


def _smartspa_root(tokens):
    return "https://smart-spa-%s-app.bestwaycorp.com" % (
        (tokens.get("smartspa_region") or SMARTSPA_REGION_DEFAULT).lower())


def _smartspa_product_key(entry):
    """Resolve (mac, product_key) for a gizwits binding via the SmartSpa device
    list. The Laravel login's ``gizwits_2026_devices`` entry carries mac /
    device_id / sno but no product key — the gateway's device list is the
    authoritative source. Returns ``(mac_lower, product_key)`` or ``(None, None)``.
    Never raises.
    """
    ga = entry.get("gizwits_user_account") or {}
    token = ga.get("token")
    if not token:
        return None, None
    mac = str(entry.get("mac") or "").lower()
    base = {"backend": "smartspa", "smartspa_token": token,
            "smartspa_region": _smartspa_region()}
    try:
        status, res = _raw_request(
            "GET", _smartspa_root(base) + "/app/smart_home/users/devices",
            headers=_smartspa_headers(base))
        _smartspa_result(status, res)
    except Exception:  # noqa: BLE001 - never break login on a flaky list call
        return None, None
    entries = res.get("data")
    if not isinstance(entries, list):
        return None, None
    want_did = str(entry.get("device_id") or "")
    match = None
    for d in entries:
        if not isinstance(d, dict):
            continue
        if want_did and want_did in (str(d.get("did") or ""),
                                     str(d.get("device_id") or "")):
            match = d
            break
        if mac and str(d.get("mac") or "").lower() == mac:
            match = d
            break
    if match is None and len(entries) == 1:
        match = entries[0]
    if match is None:
        return None, None
    pk = (match.get("productKey") or match.get("product_key")
          or match.get("productkey") or "").strip()
    resolved_mac = str(match.get("mac") or "").lower() or mac
    return (resolved_mac, pk) if pk else (None, None)


def login(email, password):
    payload = {
        "email": email,
        # the app sends MD5(password) hex, not the raw password
        "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
    }
    res = request_json(
        "POST", LARAVEL_ROOT + "/v1/login", payload,
        headers={"X-Requested-With": "com.wiltonbradley.layzspa"},
    )
    data = res.get("data") or {}
    if not data.get("api_token"):
        raise SpaError("Login failed: " + json.dumps(_scrub_response(res))[:400])

    def _fill_mxchip(device, account):
        return {
            "backend": "mxchip",
            "laravel_token": data["api_token"],
            "mxchip_token": account["token"],
            "did": device["did"],
            "product_key": device["product_key"],
            "device_name": device.get("device_name"),
            "saved_at": int(time.time()),
        }

    def _fill_smartspa(binding, ga, mac, pk):
        return {
            "backend": "smartspa",
            "laravel_token": data["api_token"],
            "smartspa_token": ga["token"],
            "smartspa_uid": ga.get("uid"),
            "smartspa_domain": ga.get("domain_id"),
            "smartspa_expires_at": ga.get("expires_at"),
            "smartspa_region": _smartspa_region(),
            "did": binding.get("device_id"),
            "mac": mac,
            "product_key": pk,
            "device_name": binding.get("device_name"),
            "saved_at": int(time.time()),
        }

    tokens = None
    # Bestway is migrating accounts to the SmartSpa backend: prefer the
    # gizwits_2026_devices binding when present, fall back to MXCHIP. The
    # binding lacks the product key, so it is resolved from the gateway's
    # device list before the session is considered usable.
    gizwits = [d for d in (data.get("gizwits_2026_devices") or [])
               if isinstance(d, dict)]
    if gizwits:
        ga = gizwits[0].get("gizwits_user_account")
        if isinstance(ga, dict) and ga.get("token") and gizwits[0].get("mac"):
            mac, pk = _smartspa_product_key(gizwits[0])
            if mac and pk:
                tokens = _fill_smartspa(gizwits[0], ga, mac, pk)
    if tokens is None:
        device = data.get("mxchip_device")
        account = data.get("mxchip_user_account")
        if isinstance(device, dict) and isinstance(account, dict) \
                and account.get("token"):
            tokens = _fill_mxchip(device, account)
    if tokens is None:
        # Login succeeded but Bestway returned no usable spa binding — the spa
        # is offline/unbound on their side (typically the wifi/network changed
        # and it has not rejoined). Crash-avoiding clarity, never a raw
        # TypeError, and no account tokens in the message.
        raise SpaError(
            "Login OK but no spa binding returned (gizwits_2026_devices=%d, "
            "mxchip_device=%s) — the spa looks offline on Bestway's side "
            "(wifi change? router change?). Reconnect the spa to WiFi via the "
            "app or panel so it shows online, then re-login. Response: %s"
            % (len(gizwits), type(data.get("mxchip_device")).__name__,
               json.dumps(_scrub_response(res))[:300]))
    with open(os.path.join(BASE_DIR, "tokens.json"), "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(os.path.join(BASE_DIR, "tokens.json"), 0o600)
    print("Logged in as %s <%s>" % (data.get("full_name", "?"), data.get("email", "?")))
    print("Spa: %s (did %s, product %s) [%s]" %
          (tokens["device_name"], tokens["did"], tokens["product_key"],
           tokens["backend"]))
    print("Tokens cached in tokens.json")
    return tokens


def get_tokens(force=False):
    path = os.path.join(BASE_DIR, "tokens.json")
    if not force and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    secrets = load_env(os.path.join(BASE_DIR, "secrets.env"))
    if not secrets.get("EMAIL") or not secrets.get("PASSWORD"):
        raise SpaError("No cached tokens. Create secrets.env (see secrets.env.example) "
                       "then run: python3 spa.py login")
    return login(secrets["EMAIL"], secrets["PASSWORD"])


def signed_headers(mxchip_token):
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=32))
    ts = str(int(time.time()))
    sign = hashlib.md5((APPID + APPSECRET + nonce + ts).encode("utf-8")).hexdigest().upper()
    return {
        "appid": APPID,
        "nonce": nonce,
        "ts": ts,
        "sign": sign,
        "Authorization": "token " + mxchip_token,
        "pushtype": "Android",
        "accept-language": "en",
        "User-Agent": "okhttp/4.9.0",
        "Content-Type": "application/json; charset=UTF-8",
    }


# --- API -----------------------------------------------------------------------
def _raw_request(method, url, payload=None, headers=None, timeout=25):
    """HTTP helper that keeps the status code (the SmartSpa gateway reports a
    rejected token as HTTP 401, which request_json would flatten into a JSON
    body that loses the auth marker). Returns (status, dict)."""
    data = None
    hdrs = {"User-Agent": WEB_UA, "Accept": "application/json; charset=utf-8"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(body) if body else {})
        except ValueError:
            return e.code, {"http_error": e.code, "body": body[:400]}


def _smartspa_headers(tokens, body=None):
    """Header set the SmartSpa gateway expects (bare token in Authorization,
    Content-MD5 = base64(md5(body)) on requests that carry a body)."""
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json; charset=utf-8",
        "Version": "1.0",
        "date": "null",
        "X-Gizwits-Application-Id": SMARTSPA_APP_ID,
        "Authorization": tokens.get("smartspa_token") or "",
    }
    if body is not None:
        hdrs["Content-MD5"] = base64.b64encode(
            hashlib.md5(body.encode("utf-8")).digest()).decode("ascii")
    return hdrs


def _smartspa_result(status, res):
    """Fold a SmartSpa gateway response into a SpaError / usable dict."""
    code = str(res.get("code") if isinstance(res, dict) else "")
    if status == 429:
        raise SpaError("SmartSpa gateway throttled (429): " + json.dumps(res)[:200])
    if status in (401, 403) or code in ("401", "403", "505", "4002"):
        raise SpaError("SmartSpa token is not authorized (HTTP %s, code %s) — re-login"
                       % (status, code))
    if code != "200":
        raise SpaError("SmartSpa request failed (HTTP %s, code %s): %s"
                       % (status, code, json.dumps(res)[:400]))
    return res


def smartspa_shadow(tokens):
    mac = tokens.get("mac")
    pk = tokens.get("product_key")
    if not mac or not pk:
        raise SpaError("SmartSpa session is missing mac/product_key — re-login")
    url = "%s/app/device/shadow/%s/%s" % (_smartspa_root(tokens), pk, mac)
    status, res = _raw_request("GET", url,
                               headers=_smartspa_headers(tokens))
    _smartspa_result(status, res)
    return res.get("data") or {}


def smartspa_control(tokens, datapoints):
    mac = tokens.get("mac")
    pk = tokens.get("product_key")
    if not mac or not pk:
        raise SpaError("SmartSpa session is missing mac/product_key — re-login")
    clean = {}
    for name, value in (datapoints or {}).items():
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if name in SMARTSPA_BOOL_DPS:
            clean[name] = 1 if numeric > 0 else 0
        else:
            clean[name] = numeric
    if not clean:
        raise SpaError("SmartSpa control: no usable datapoints")
    # The gateway wants the datapoints as a JSON *string* inside {"data": ...}.
    body_dict = {"appKey": SMARTSPA_APP_ID, "version": "1.0",
                 "data": json.dumps(clean)}
    url = "%s/app/device/control/%s/%s" % (_smartspa_root(tokens), pk, mac)
    status, res = _raw_request("POST", url, body_dict,
                               headers=_smartspa_headers(tokens, json.dumps(body_dict)))
    return _smartspa_result(status, res)


def _is_smartspa(tokens):
    return bool(tokens) and tokens.get("backend") == "smartspa"


def shadow(tokens):
    if _is_smartspa(tokens):
        return smartspa_shadow(tokens)
    res = request_json(
        "POST", SMARTHUB_ROOT + "/api/device/thing_shadow/",
        {"device_id": tokens["did"], "product_id": tokens["product_key"]},
        headers=signed_headers(tokens["mxchip_token"]),
    )
    if res.get("code") not in (0, "0"):
        raise SpaError("thing_shadow failed: " + json.dumps(res)[:400])
    return res.get("data") or {}


def send_command(tokens, datapoint, value):
    if _is_smartspa(tokens):
        return smartspa_control(tokens, {datapoint: value})
    payload = {
        "desired": {"state": {"desired": {datapoint: value}}},
        "device_id": tokens["did"],
        "product_id": tokens["product_key"],
    }
    last = None
    for path in COMMAND_PATHS:
        last = request_json("POST", SMARTHUB_ROOT + path, payload,
                            headers=signed_headers(tokens["mxchip_token"]))
        code = last.get("code")
        if code in (0, "0") and last.get("message") in ("SUCCESS", None):
            return last
    raise SpaError("command failed (tried %s): %s"
                   % (", ".join(COMMAND_PATHS), json.dumps(last)[:400]))


def wait_for(tokens, datapoint, expected, timeout=25):
    """Cloud writes take 5-10s to show up in the shadow; poll until confirmed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = shadow(tokens)
        if state.get(datapoint) == expected:
            return True
        time.sleep(3)
    return False


# --- output --------------------------------------------------------------------
def print_status(state, raw=False):
    if raw:
        print(json.dumps(state, indent=2, sort_keys=True))
        return
    unit = "C" if state.get("temperature_unit") in (1, "1") else "F"
    heater = state.get("heater_state")
    try:
        heater = HEATER_STATE_TEXT.get(int(heater), heater)
    except (TypeError, ValueError):
        pass
    print("Spa status")
    print("  water temperature : %s%s" % (state.get("water_temperature"), unit))
    print("  target            : %s%s" % (state.get("temperature_setting"), unit))
    print("  power             : %s" % ("ON" if state.get("power_state") else "OFF"))
    print("  heater            : %s" % heater)
    print("  filter pump       : %s" % ("ON" if state.get("filter_state") else "OFF"))
    print("  bubbles (AirJet)  : %s" % ("ON" if state.get("wave_state") else "OFF"))
    print("  hydrojet          : %s" % ("ON" if state.get("hydrojet_state") else "OFF"))
    print("  at target temp    : %s" % ("yes" if str(state.get("warning")) == "1" else "no"))
    print("  error code        : %s" % (state.get("error_code") or "none"))
    print("  connectivity      : %s (wifi fw %s)" % (state.get("ConnectType"), state.get("wifivertion")))


# --- main -----------------------------------------------------------------------
def _run_cmd(args, tokens):
    if args.cmd == "login":
        return
    if args.cmd == "ota":
        # The app's firmware-update check. An empty list means no updates are
        # published for this product; if Bestway ships one, the list should
        # contain the image info/URL - grab it for analysis when it appears.
        res = request_json("GET", SMARTHUB_ROOT + "/api/product/ota?product_id=" + tokens["product_key"],
                           headers=signed_headers(tokens["mxchip_token"]))
        data = res.get("data") or {}
        listing = data.get("list") or []
        if not listing:
            print("No OTA updates published for product %s." % tokens["product_key"])
            state = shadow(tokens)
            print("Current firmware: wifivertion=%s otastatus=%s trdversion=%s mcuversion=%s" % (
                state.get("wifivertion"), state.get("otastatus"),
                state.get("trdversion"), state.get("mcuversion")))
        else:
            print("OTA updates available:")
            print(json.dumps(listing, indent=2))
            print("(if an entry contains a download URL, fetch it - that is the firmware image)")
        return
    if args.cmd == "status":
        print_status(shadow(tokens), raw=args.raw)
        return
    if args.cmd == "watch":
        last = None
        while True:
            state = shadow(tokens)
            snapshot = json.dumps(state, sort_keys=True)
            if snapshot != last:
                if args.raw:
                    print(json.dumps({"ts": int(time.time()), "shadow": state}), flush=True)
                else:
                    print("\n[%s]" % time.strftime("%H:%M:%S"), flush=True)
                    print_status(state)
                last = snapshot
            time.sleep(args.interval)
        return

    if args.cmd == "temp":
        datapoint, value = "temperature_setting", args.value
    elif args.cmd == "raw":
        datapoint, value = args.datapoint, args.value
        try:
            value = int(value)
        except ValueError:
            pass
    else:
        datapoint = DATAPONT_ALIASES[args.cmd]
        value = 1 if args.state == "on" else 0

    res = send_command(tokens, datapoint, value)
    print("Sent %s = %s -> %s" % (datapoint, value, res.get("message")))
    ok = wait_for(tokens, datapoint, value)
    print("Confirmed in shadow: %s" % ("YES" if ok else "not yet (cloud lag; check 'status')"))


def main():
    parser = argparse.ArgumentParser(description="Lay-Z-Spa cloud control CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")
    p_status = sub.add_parser("status")
    p_status.add_argument("--raw", action="store_true")
    p_watch = sub.add_parser("watch")
    p_watch.add_argument("--raw", action="store_true",
                         help="emit one JSON line per change (JSONL log, for analysis)")
    p_watch.add_argument("--interval", type=int, default=15,
                        help="seconds between polls (default 15)")
    sub.add_parser("ota")

    for name in ("power", "heat", "filter", "wave", "jet", "lock"):
        p = sub.add_parser(name)
        p.add_argument("state", choices=["on", "off"])

    p_temp = sub.add_parser("temp")
    p_temp.add_argument("value", type=int)

    p_raw = sub.add_parser("raw")
    p_raw.add_argument("datapoint")
    p_raw.add_argument("value")

    args = parser.parse_args()
    try:
        tokens = get_tokens(force=(args.cmd == "login"))
    except SpaError as e:
        sys.exit(str(e))
    try:
        _run_cmd(args, tokens)
    except SpaError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
