# Lay-Z-Spa Protocol Notes

Unofficial notes, worked out on 2026-10-17 from the **Lay-Z-Spa WiFi App**
(Android, `com.wiltonbradley.layzspaapp` v2.2.2, Flutter) controlling a
**Lay-Z-Spa Toronto AirJet Plus (UltraFit pump, product_key `FTEW0E`)**.

The account-side flow described here had been unclear since the app's backend
migration in April 2024 (see cdpuk/ha-bestway issue #88, where the gateway
host was found in 2025 but the endpoints were never decoded - the login
lives in the app's Flutter `libapp.so` and bypasses HTTP proxies).

## Architecture (2024-2026 backend landscape)

| Backend | Host | Used by |
|---|---|---|
| ~~mobileapi gateway~~ | `mobileapi.lay-z-spa.co.uk` | **DEAD** (DNS resolves, nothing listens - decommissioned ~April 2024) |
| classic Gizwits | `euapi/usapi/api.gizwits.com` | Bestway Smart Hub app (V01 pumps, <=2024) |
| SmartSpa gateway | `smart-spa-{eu,us,cn}-app.bestwaycorp.com` | Bestway Connect app (from July 2026) |
| **Laravel account gateway** | `app-api.lay-z-spa.co.uk` | **Lay-Z-Spa app (this document)** - Cloudflare front, Laravel/PHP |
| **MXCHIP / AWS IoT** | `smarthub-eu.bestwaycorp.com` | Lay-Z-Spa app + Bestway Connect (device data plane) |

The Lay-Z-Spa app logs in against the Laravel gateway, which stores a link to
the MXCHIP platform ("mxchip" - the IoT vendor behind the AWS IoT backend; the
smarthub CSP header allows `*.mxchip.com`) and returns everything needed to
talk to the data plane directly.

## 1. Login (account plane)

```
POST https://app-api.lay-z-spa.co.uk/v1/login
Content-Type: application/json
Accept: application/json
X-Requested-With: com.wiltonbradley.layzspa
User-Agent: <android webview UA>

{"email": "user@example.com", "password": "<md5-of-password>"}
```

**The app sends `MD5(password)` (lowercase hex), not the raw password.**
Sending the raw password returns `{"errors":{"password":"Your password is incorrect"}}`.

Success response (trimmed to the important parts):

```json
{
  "data": {
    "user_id": 12345,
    "email": "user@example.com",
    "api_token": "<laravel api token>",
    "device_manufacturer": "samsung",
    "device_model": "SM-G998B",
    "mxchip_device": {
      "did": "<device id>",
      "mac": "<device mac>",
      "product_key": "FTEW0E",
      "device_name": "<device name>",
      "pump_type": "ultrafit"
    },
    "mxchip_user_account": {
      "visitor_id": "u_<numeric id>_AhFLL54HnChhrxcl9ZUJL6QNfolTIB",
      "token": "<mxchip/smarthub token - use as 'Authorization: token <this>'"
    },
    "gizwits_device": null,
    "gizwits_user_account": null,
    "gizwits_2026_devices": []
  },
  "message": "Authorisation Successful!"
}
```

Notes:
- There is also `POST /v1/register` (fields: firstname, lastname, email, password
  - presumably also MD5'd) and `POST /v1/forgot`-style routes are absent.
- 404s return a custom JSON `{"message":"The route X could not be found."}`
  (only when `Accept: application/json` is sent).
- Repeated logins can invalidate the phone app's session (community reports
  tokens are single-active-client). **Login once, cache the tokens.**
- The Laravel `/v1/gizwits/*` proxy routes exist (GET `/v1/gizwits/status`,
  `/v1/gizwits/turn_on`, POST `/v1/gizwits/temp_set`...) but return
  HTTP 500 "Server Error" even with valid tokens - broken server-side as of
  2026-10-17. Use the direct data plane below instead.

## 2. Signing scheme (data plane)

```
appid    = AhFLL54HnChhrxcl9ZUJL6QNfolTIB        (static, same for all apps)
appsecret = 4ECvVs13enL5AiYSmscNjvlaisklQDz7vWPCCWXcEFjhWfTmLT
nonce    = 32 chars from [a-z0-9]
ts       = unix seconds (as string)
sign     = UPPERCASE( MD5( appid + appsecret + nonce + ts ) )
```

Request headers for every data-plane call:

```
appid: <appid>
nonce: <nonce>
ts: <ts>
sign: <sign>
Authorization: token <mxchip token from login>
pushtype: Android
accept-language: en
Content-Type: application/json; charset=UTF-8
User-Agent: okhttp/4.9.0
```

## 3. Read device state

```
POST https://smarthub-eu.bestwaycorp.com/api/device/thing_shadow/
{"device_id": "<did>", "product_id": "FTEW0E"}
```

Response `data` (example; device-specific register values replaced with
placeholders — see `local_device.md` for the author's actual readings):

```json
{
  "wifivertion": 121, "otastatus": 0, "mcuversion": "(null)", "trdversion": "0.0.6",
  "ConnectType": "online", "power_state": 1, "heater_state": 6, "wave_state": 0,
  "filter_state": 2, "temperature_setting": 40, "temperature_unit": 1,
  "water_temperature": 40, "local_timer_rest": 0, "hydrojet_state": 0,
  "local_reservation": 0, "B1": 0, "B2": 0, "B3": 0, "B8": 0, "B9": 0,
  "P0": 0, "P1": 0, "P2": 0, "warning": "1",
  "error_code": "", "B0": 0, "W0": 0, "is_online": true
}
```

### Datapoint semantics

| Datapoint | Read | Write | Meaning |
|---|---|---|---|
| `power_state` | 0/1 | 0/1 | master power |
| `heater_state` | 0-8 | 0/1 | read: 0 off, 1 starting, 2-3 heating (1-2 elements), 4-6 holding temp, 7-8 winter mode. write: any non-zero = on |
| `filter_state` | 0/2 | 0/1 | filter pump (read 2 = running) |
| `wave_state` | 0/1 | 0/1 | AirJet bubbles |
| `hydrojet_state` | 0/1 | 0/1 | HydroJet massage (if equipped) |
| `temperature_setting` | 20-40 | 20-40 | target temp, in the spa's unit |
| `temperature_unit` | 1=C (observed), 0=F | - | display unit |
| `water_temperature` | number | - | current water temp |
| `warning` | "1" when at target | - | reached set temperature |
| `error_code` | string | - | pump error code |
| `locked` | 0/1 | 0/1 | panel lock |
| `ConnectType` / `is_online` | online/offline | - | reachability |

`B0`-`B9`, `P0`-`P2`, `W0` are raw device registers, meaning not yet decoded
(B1/B2/B3 ~40 look like additional temperature sensors, P0-P2 look like packed
timestamps/durations - TODO).

## 4. Write commands

```
POST https://smarthub-eu.bestwaycorp.com/api/device/command
{"desired": {"state": {"desired": {"wave_state": 1}}},
 "device_id": "<did>", "product_id": "FTEW0E"}
```

Response: `{"code": 0, "message": "SUCCESS", ...}`. Writes take 5-10 seconds to
be reflected in the shadow. A `/api/v2/device/command` variant exists that
AES-256-CBC encrypts the body (see epheterson/bestway-spa-v2); the plaintext
v1 endpoint above works fine.

Verified end-to-end on 2026-10-17: `wave_state` 1 then 0, both confirmed in
the shadow.

## Related community work

- cdpuk/ha-bestway issue #88 (SmartSpa/AWS IoT discovery, appid/secret/sign)
- gklka/homebridge-layzspa (SmartSpa gateway client, heater semantics)
- epheterson/bestway-spa-v2 (QR-share binding CLI for the AWS IoT backend)
- M1R4G376/New_bestway_spa (HA integration via traffic capture)
- andiwirz/homey-layz (clients for Gizwits / SmartSpa / smarthub backends)
- HA community thread "Lay-Z-Spa Hot Tub wi-fi pump automation" (229006)
- This repo's RESEARCH_LOG.md documents the full probing session, including
  every dead end, so nobody has to repeat them.

## 5. Manufacturer datapoint definition (found 2026-10-17)

Gizwits still publishes the full datapoint definition for this pump publicly:

```
GET https://euapi.gizwits.com/app/datapoint?product_key=FTEW0E
X-Gizwits-Application-Id: 98754e684ec045528b073876c34c7348
```

(Saved in `docs/datapoints-FTEW0E.json`.) Product "UltraFit_QK", packetVersion
0x00000004, standard binary protocol with per-datapoint byte/bit offsets -
this is the layout of the packets the WiFi module exchanges with the pump MCU.

**Writable datapoints (complete list, nothing hidden):**
power_state (bool, byte0.0), temperature_unit (bool, byte0.1), heater_state
(u8, byte1), wave_state (u8, byte2), filter_state (u8, byte3),
temperature_setting (u8, byte4), hydrojet_state (u8, byte5), local_timer_rest
(u16, byte6). Everything else (water_temperature, warning, error_code,
local_reservation, B0-B15, W0-W7, P0-P7) is status_readonly.

### Enforced setpoint limit (measured)

- temperature_setting 41/42 (C mode) and 105/106/108 (F mode) are all accepted
  by the cloud ("SUCCESS") but the device keeps its setpoint at 40C / 104F.
- The clamp is device-side (pump MCU or WiFi module), NOT cloud-side.
- There is no calibration, offset, or max-override datapoint - the writable
  set above is exhaustive.
- Where exactly the clamp lives (WiFi module vs MCU) is an open question; if
  it is only in the WiFi module, injecting raw protocol packets on the
  module-to-MCU serial link could bypass it. Untested.
