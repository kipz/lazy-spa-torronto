#!/usr/bin/env python3
"""
Lay-Z-Spa (Toronto UltraFit) cloud control CLI.

Reverse-engineered protocol for the Bestway Lay-Z-Spa Android/iOS app
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

# --- constants discovered by reverse engineering (see PROTOCOL.md) -----------
APPID = "AhFLL54HnChhrxcl9ZUJL6QNfolTIB"
APPSECRET = "4ECvVs13enL5AiYSmscNjvlaisklQDz7vWPCCWXcEFjhWfTmLT"
LARAVEL_ROOT = "https://app-api.lay-z-spa.co.uk"
SMARTHUB_ROOT = "https://smarthub-eu.bestwaycorp.com"
COMMAND_PATHS = ["/api/device/command", "/api/device/command/", "/api/v2/device/command"]
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
        sys.exit("Login failed: " + json.dumps(res)[:400])
    tokens = {
        "laravel_token": data["api_token"],
        "mxchip_token": data["mxchip_user_account"]["token"],
        "did": data["mxchip_device"]["did"],
        "product_key": data["mxchip_device"]["product_key"],
        "device_name": data["mxchip_device"].get("device_name"),
        "saved_at": int(time.time()),
    }
    with open(os.path.join(BASE_DIR, "tokens.json"), "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(os.path.join(BASE_DIR, "tokens.json"), 0o600)
    print("Logged in as %s <%s>" % (data.get("full_name", "?"), data.get("email", "?")))
    print("Spa: %s (did %s, product %s)" %
          (tokens["device_name"], tokens["did"], tokens["product_key"]))
    print("Tokens cached in tokens.json")
    return tokens


def get_tokens(force=False):
    path = os.path.join(BASE_DIR, "tokens.json")
    if not force and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    secrets = load_env(os.path.join(BASE_DIR, "secrets.env"))
    if not secrets.get("EMAIL") or not secrets.get("PASSWORD"):
        sys.exit("No cached tokens. Create secrets.env (see secrets.env.example) "
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
def shadow(tokens):
    res = request_json(
        "POST", SMARTHUB_ROOT + "/api/device/thing_shadow/",
        {"device_id": tokens["did"], "product_id": tokens["product_key"]},
        headers=signed_headers(tokens["mxchip_token"]),
    )
    if res.get("code") not in (0, "0"):
        sys.exit("thing_shadow failed: " + json.dumps(res)[:400])
    return res.get("data") or {}


def send_command(tokens, datapoint, value):
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
    sys.exit("command failed (tried %s): %s" % (", ".join(COMMAND_PATHS), json.dumps(last)[:400]))


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
    tokens = get_tokens(force=(args.cmd == "login"))

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


if __name__ == "__main__":
    main()
