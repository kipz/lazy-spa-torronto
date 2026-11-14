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

**Writable via the cloud** (the complete set, `status_writable` in the
manufacturer's Gizwits datapoint def): `power_state`, `temperature_unit`,
`heater_state`, `wave_state`, `filter_state`, `temperature_setting`,
`hydrojet_state`, `local_timer_rest`. Everything else — `water_temperature`,
`warning`, `error_code`, `local_reservation`, and all of `B0..B15`,
`W0..W7`, `P0..P7` — is `status_readonly` (NOT writable from the cloud; the
device sets them).

### Raw read-only registers (B*/P*/W*) — full declared map

The Gizwits datapoint definition for product_key `FTEW0E` also states each
field's **byte offset / size in the binary protocol packet**, its data type,
and its R/W class. This is the authoritative packet layout (byte 0 = first
packet byte after the header):

| Byte | Name | Size | Type | R/W | Manufacturer meaning | Units / notes |
|---|---|---|---|---|---|---|
| 0.0 | `power_state` | 1 bit | bool | **W** | master power | 0 off / 1 on |
| 0.1 | `temperature_unit` | 1 bit | bool | **W** | display unit | 0 = °F, 1 = °C |
| 1 | `heater_state` | 1 | uint8 | **W** | heater | 0 off, 3 heating, 4-6 holding... (see table above) |
| 2 | `wave_state` | 1 | uint8 | **W** | AirJet | 0 off / 1–99 speed-2 / 100 speed-1 |
| 3 | `filter_state` | 1 | uint8 | **W** | filter pump | 0 off / 1 starting / 2 running / 3 wait-24h |
| 4 | `temperature_setting` | 1 | uint8 | **W** | target setpoint | 20–40 °C / 68–104 °F |
| 5 | `hydrojet_state` | 1 | uint8 | **W** | HydroJet | 0 off / 1 on |
| 6 | `local_timer_rest` | 2 | uint16 | **W** | timer | hours; 0 = off |
| 8 | `water_temperature` | 1 | uint8 | R | water temp | °C/F; 4–40 °C |
| 9 | `warning` | 1 | uint8 | R | at-target flag | 0 none / 1 reached setpoint |
| 10 | `error_code` | 1 | uint8 | R | error | 0 none / 1=E01 … 31=E31 |
| 11 | `local_reservation` | 2 | uint16 | R | reservation | hours; 0 = off |
| 13 | `B0` | 1 | byte | R | key code | last key/button |
| 14 | `B1` | 1 | byte | R | NTC1 temp | thermistor 1; raw (reads ~°C) |
| 15 | `B2` | 1 | byte | R | NTC2 temp | thermistor 2; raw (reads ~°C) |
| 16 | `B3` | 1 | byte | R | pump current | motor current; raw (unit unconfirmed) |
| 17–20 | `B4`–`B7` | 1 each | byte | R | *(undefined)* | — |
| 21 | `B8` | 1 | byte | R | E18 leak ADC | leakage-current sample |
| 22 | `B9` | 1 | byte | R | E19 PCB temp | board temperature; raw (reads ~°C) |
| 23–28 | `B10`–`B15` (BA–BF) | 1 each | byte | R | *(undefined)* | — |
| 29 | `W0` | 2 | uint16 | R | run time | filter-or-heating accumulated **hours** |
| 31–43 | `W1`–`W7` | 2 each | uint16 | R | *(undefined)* | — |
| 45 | `P0` | 4 | uint32 | R | bit flags | bit1 NTC1-change · bit2 NTC2-open · bit3 flow-switch · bit4 panel-lock |
| 49 | `P1` | 4 | uint32 | R | power-board version | `A bb C MM DD` → 6.16.2 (built 01/09) |
| 53 | `P2` | 4 | uint32 | R | display-board version | `A bb C MM DD` → 6.24.2 (built 04/09) |
| 57–73 | `P3`–`P7` | 4 each | uint32 | R | *(undefined)* | — |

**R/W via the cloud — confirmed:** only the 8 `status_writable` datapoints
(`power_state`, `temperature_unit`, `heater_state`, `wave_state`,
`filter_state`, `temperature_setting`, `hydrojet_state`, `local_timer_rest`)
can be written. **Every `B*`/`P*`/`W*` register is `status_readonly`** — the
device publishes them to the cloud, but the cloud API will not let you write
them, and there is no hidden writable calibration/temp register.

**Units:** Bestway publishes ratios only for the control datapoints (#8 etc.
scale ×1 in whole °C/°F). The `binary` fields carry no declared scaling:
- `W0` is a 16-bit **hours** counter (filter/heating run time; RESEARCH_LOG
  saw it freeze in standby). `W1`–`W7` are the same size, meaning undeclared.
- `P0` is a 32-bit flag word; `P1`/`P2` are 32-bit packed version strings.
- `B1`, `B2`, `B9` read in °C-ish values (NTC/PCB temps) but Bestway doesn't
  publish the raw→°C ratio, so treat them as raw — the *calibrated* control
  temperature is `water_temperature` (a pump-compartment probe, not bulk —
  see RESEARCH_LOG.md).

Sources: Gizwits datapoint definition (`euapi.gizwits.com`, `docs/datapoints-FTEW0E.json`),
cross-checked against live shadow values and RESEARCH_LOG.md Session 4. The
community reverse-engineering projects (visualapproach/WiFi-remote-for-Bestway-Lay-Z-SPA
etc.) decode the hardware display-bus rather than these cloud registers, so
the Gizwits table is the deepest authoritative source found.

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
