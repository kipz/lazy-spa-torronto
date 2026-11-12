# Research Log - 2026-10-17

Goal: integrate a Lay-Z-Spa Toronto (UltraFit pump) into the home IoT network
by studying the official app's cloud protocol.

## Environment

- Spa: Lay-Z-Spa Toronto AirJet Plus, UltraFit pump, product_key `FTEW0E`
- App: "Lay-Z-Spa WiFi App" (`com.wiltonbradley.layzspaapp` v2.2.2, Flutter,
  login logic inside `libapp.so`, uses raw sockets so HTTP proxies see nothing)
- Account: <redacted> (app email + password, stored only in secrets.env)

## Phase 1 - identify the backend

Web research found three documented generations:
1. Classic Gizwits cloud (Bestway Smart Hub, V01 pumps)
2. AWS IoT / MXCHIP backend `smarthub-*.bestwaycorp.com` (V02 pumps 2025+)
3. SmartSpa gateway `smart-spa-*-app.bestwaycorp.com` (Bestway Connect, Jul 2026+)

The classic Lay-Z-Spa app gateway `mobileapi.lay-z-spa.co.uk` (documented by
cyb3rl4d, BruceH5200, rossdargan) is **dead**: DNS resolves (3.8.71.75) but
nothing accepts connections on 443.

Community sources (ha-bestway issue #88, TechnoTaff's Toronto packet trace)
showed the current Lay-Z-Spa app authenticates via an undocumented Laravel
gateway `app-api.lay-z-spa.co.uk`, then controls the spa through
`smarthub-eu.bestwaycorp.com`.

## Phase 2 - rule out the wrong backends (with account credentials)

Gizwits `POST /app/login` with the classic Lay-Z-Spa/Bestway app IDs:

| app ID | eu / us / cn |
|---|---|
| 98754e684ec045528b073876c34c7348 (classic) | 9005 "user does not exist" |
| afb8f41dbb274cb19d0fdf472945fbf3 (alt) | 9005 |
| 43424551921d4ee18dd279140e11e198 (iOS) | 9005 |
| f50095a20a2f4152a3685a01a863ec5a (iOS alt) | 9005 |

Gizwits distinguishes 9005 (user does not exist, email-scoped - password
irrelevant) from 9020 (wrong password), so the account is not on Gizwits
under any known Bestway app ID.

SmartSpa gateway `POST /app/smart_home/login/pwd` (all 3 regions): always
`{"code":"1000000","message":"user does not exist"}` - this gateway conflates
"no account" and "wrong password", but combined with the Gizwits result the
account is not there either.

## Phase 3 - find the Laravel gateway's routes

`app-api.lay-z-spa.co.uk` is behind Cloudflare, sets Laravel session cookies
(`lay_z_spa_wifi_app_session`, `XSRF-TOKEN`), and returns a custom JSON 404
`{"message":"The route X could not be found."}` for `Accept: application/json`.

Ruled out (404): ~30 conventional paths (`/api/login`, `/api/auth/login`,
`/v1/auth/login`, `/login`, `/sanctum/csrf-cookie`, ...).

Found via method probing (405 "method not supported" responses leak route
existence):

- `POST /v1/login` - **the login route** (fields: email, password)
- `POST /v1/register` - (fields: firstname, lastname, email, password)
- `GET  /v1/gizwits/status`, `GET /v1/gizwits/turn_on` - exist but 500
  server-side even with valid tokens (broken proxy, unusable)
- `POST /v1/gizwits/temp_set` - 405 from an upstream Symfony-style error page
  (mixed frameworks behind the gateway); also 500 with valid params

## Phase 4 - crack the login

Raw password -> `{"errors":{"password":"Your password is incorrect"}}`
(account exists - the email lookup passed; password rejected).

Several remembered-password guesses: all rejected by the gateway.

Hash hypothesis (Flutter apps often pre-hash): **`MD5(password)` lowercase hex
worked** - `{"message":"Authorisation Successful!"}`. This is the key
undocumented quirk; it is also why traffic captures alone (TLS) plus proxy
sniffing never surfaced it.

The login response returns everything needed for the data plane:
`api_token`, `mxchip_device` (did, mac, product_key, pump_type),
`mxchip_user_account` (visitor_id, smarthub token).

## Phase 5 - verify the data plane (smarthub-eu.bestwaycorp.com)

Signing (from issue #88, Andrerm124's script): headers `appid`, `nonce` (32
[a-z0-9]), `ts`, `sign = UPPERCASE(MD5(appid+appsecret+nonce+ts))`,
`Authorization: token <mxchip token>`.

- `POST /api/device/thing_shadow/` -> live state, e.g. water 40C, target 40C,
  heater 6 (holding), filter 2 (on), wave 0, online.
- `POST /api/device/command` `{"desired":{"state":{"desired":{"wave_state":1}}},
  "device_id","product_id"}` -> SUCCESS, confirmed in shadow ~5s later.
  Tested on and off - both confirmed. **Full read/write control verified.**

## Artifacts

- `spa.py` - working CLI (login, status, watch, power/heat/filter/wave/jet/
  lock/temp, raw writes)
- `PROTOCOL.md` - full protocol documentation
- Dead ends above are logged so they don't get repeated.

## Open questions / TODO

- Decode raw registers B0-B9, P0-P2, W0
- Test `/api/v2/device/command` (AES-256-CBC body) vs plaintext v1
- MQTT bridge for the home IoT network (plain MQTT and/or Home Assistant)
- Verify whether `X-Requested-With` is actually enforced on /v1/login
- Password reset flow (routes not found by conventional names)
- The pump also supports local schedules (`local_timer_rest`,
  `local_reservation`) - endpoints unknown
- ~~Share findings upstream~~ **DONE 2026-10-17**: posted to
  https://github.com/cdpuk/ha-bestway/issues/88#issuecomment-6233830512
  (closes the 2.5-year-old open question from April 2024)

## Session 2 (2026-10-17): maximum-temperature investigation

Goal: raise the 40C setpoint cap by 1-2 degrees. Findings:

- C mode: cloud accepts temperature_setting 41/42, device clamps to 40.
- F mode: cloud accepts 105/106/108, device clamps to 104 (= 40C exactly).
- No hidden writable datapoints: the public Gizwits datapoint definition for
  product_key FTEW0E lists the complete writable set (8 datapoints, all
  already known) - no calibration/max-temp register exists.
- smarthub product/firmware routes probed: all 404.
- Conclusion: the cap is enforced in device firmware; cloud-side attack
  surface is exhausted. Remaining routes: MCU firmware RE, WiFi-module
  serial-link MITM (clamp location unverified), physical sensor bias, or
  auxiliary heating. See PROTOCOL.md section 5.

## Session 3 (2026-10-17): custom-firmware / OTA investigation

Q: has anyone lifted Bestway pump firmware, patched it, and reflashed it?

- **No public firmware dumps or patches exist for any Bestway pump generation.**
  Historical blocker: the main control boards are potted (resin-encapsulated)
  per the Hackaday.io hot-tub-remote project. The only proven "beat the cap"
  mod is physical (2015): resistor in series with the NTC sensor -> +2C offset.
- The community's "custom firmware" = external ESP display-replacement /
  interposer (visualapproach 2020-present, WifiWhirl, ESP32-C6 Matter fork
  lowlander10000/Bestway-Spa-ESP32-WiFi-Addon-Matter from Oct 2026). None of
  these touch the pump's own firmware; none support the UltraFit pump yet.
- Found + verified the app's OTA check endpoint:
  GET smarthub-eu.bestwaycorp.com/api/product/ota?product_id=FTEW0E
  -> SUCCESS, empty list (= no update published; endpoint is an "available
  updates" check, not a catalog). Added `spa.py ota` to monitor it: if Bestway
  ever publishes an image, the entry should contain the URL - grab it then.
- Laravel gateway: no firmware/ota routes (all probed 404).
- Next steps for real firmware work: (1) FCC filing for the pump's radio
  (internal photos = chip ID without opening; fcc.report blocked our fetch,
  need the FCC ID off the pump label), (2) teardown to identify WiFi module
  (MXCHIP backend name suggests STM32/MiCO or similar; dump via SWD if
  unlocked) and main MCU, (3) test where the setpoint clamp actually lives
  (module vs MCU) by injecting raw protocol packets on the internal serial
  link - byte layout is known from docs/datapoints-FTEW0E.json.

## Session 4 (2026-10-17 evening): register decode from a live power-off event

At ~19:10 the pump was put in standby (power_state 1->0, filter+heater off)
while holding at setpoint. The watch logger caught the transient:

- water_temperature (display/control) spiked several degrees above the
  setpoint and settled back over ~7 min. The 1250 L bulk cannot move that
  fast -> the control probe reads the pump compartment, which runs several
  degrees above bulk when flow stops (trapped heat slug mixing out).
- Register separation achieved: B2 = probe nearest the heater (instant spike
  on flow-stop); B1 = second, lagging probe; B3 = heater/flow-coupled (tracked
  heater_state closely; corr(B3, heater_state) = 0.944); B9 = board/ambient
  (rose once flow-cooling stopped); B0 and P0 changed with power state;
  W0 = runtime-hours counter (froze in standby).
- No error_code during the compartment spike -> read-alarms tolerate local
  transients to at least ~44C.

Sensor-hack implications (see SENSOR_HACK.md):
- Displayed temp is a compartment measurement; calibrate against a floating
  thermometer in the body water BEFORE choosing any bias - the real offset
  between display and bulk may already be 1-2C.
- Biasing the control probe shifts display, control and read-alarms together;
  the hardware E08 thermal cut-out remains unbiased.
