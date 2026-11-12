# Community post — for cdpuk/ha-bestway issue #88 (also pasteable in HA thread 229006)

Posted as a comment by the repo owner. All personal data (email, tokens,
device did/mac, user ids) replaced with placeholders. The appid/appsecret and
signing scheme were already published upthread by other participants, so they
are repeated here for completeness.

---

**Solved: the Lay-Z-Spa app's account gateway (`app-api.lay-z-spa.co.uk`) — login route found, plus the client-side password hashing that made it untraceable**

Huge thanks to everyone in this thread — this builds directly on your work: @TechnoTaff identified the gateway host from a packet trace when Charles couldn't see it, and @bobbyverlaan / @Andrerm124 / @jteno documented the smarthub appid/secret/sign scheme and the device flow. What was still missing was the *account* side: which endpoint the app's `libapp.so` actually POSTs the email/password to, and in what form. Here it is, verified end-to-end today against a **Lay-Z-Spa Toronto AirJet Plus (UltraFit pump, product_key `FTEW0E`)** on the current Android app (`com.wiltonbradley.layzspaapp` v2.2.2).

## TL;DR — the login

```
POST https://app-api.lay-z-spa.co.uk/v1/login
Content-Type: application/json
Accept: application/json
X-Requested-With: com.wiltonbradley.layzspa

{"email": "you@example.com", "password": "<lowercase MD5 hex of your password>"}
```

**The app sends `MD5(password)`, not the raw password.** That single detail is why this stayed unsolved since April 2024 — a proxy capture shows a hash that doesn't relate to your password in any obvious way, and replaying your real password gets you:

```json
{"errors":{"password":"Your password is incorrect"}}
```

(Note that this is an authentication-stage error — the email lookup already passed — so the endpoint doubles as an account-existence oracle: wrong email vs wrong password behave differently.)

A successful login returns everything needed to drive the spa directly through the smarthub backend — no QR-share, no second phone, no proxy:

```json
{
  "data": {
    "user_id": 12345,
    "api_token": "<laravel api token>",
    "mxchip_device": {
      "did": "<device id>",
      "mac": "<device mac>",
      "product_key": "FTEW0E",
      "device_name": "<device name>",
      "pump_type": "ultrafit"
    },
    "mxchip_user_account": {
      "visitor_id": "u_<numeric id>_AhFLL54HnChhrxcl9ZUJL6QNfolTIB",
      "token": "<use this as 'Authorization: token <this>' on smarthub>"
    }
  },
  "message": "Authorisation Successful!"
}
```

## The data plane (unchanged from what's documented upthread)

With `mxchip_user_account.token` from the login response, the flow already documented in this issue works as-is — for completeness:

- Signing: `nonce` = 32 chars `[a-z0-9]`, `ts` = unix seconds, `sign` = `UPPERCASE(MD5(appid + appsecret + nonce + ts))` with `appid: AhFLL54HnChhrxcl9ZUJL6QNfolTIB`
- Read: `POST https://smarthub-eu.bestwaycorp.com/api/device/thing_shadow/` with body `{"device_id": "<did>", "product_id": "<product_key>"}`
- Write: `POST https://smarthub-eu.bestwaycorp.com/api/device/command` with body `{"desired": {"state": {"desired": {"<datapoint>": <value>}}}, "device_id": "<did>", "product_id": "<product_key>"}`
- Datapoints: `power_state`, `heater_state` (read 0–8: 0 off, 1 starting, 2–3 heating, 4–6 holding temp, 7–8 winter mode; write 1/0), `filter_state` (read 2 = running), `wave_state`, `hydrojet_state`, `temperature_setting` (20–40), `locked`; shadow also reports `water_temperature`, `warning` ("1" = at target), `error_code`, `ConnectType`/`is_online`.

Verified today: `wave_state` 1 → confirmed in the shadow in ~5 s → `wave_state` 0 → confirmed. Full read/write control from a plain stdlib Python script.

## How the route was found (in case it helps future backend changes)

The gateway is a Laravel app behind Cloudflare. Two quirks make route enumeration easy and credential-free:

1. With `Accept: application/json`, unknown routes return a custom JSON 404: `{"message":"The route X could not be found."}`
2. A wrong-method request to an existing route returns 405 **naming the allowed methods** — so a POST sweep over candidate paths reveals live routes without ever sending credentials.

That's how `POST /v1/login` (fields: email, password) and `POST /v1/register` (fields: firstname, lastname, email, password) were located. Conventional guesses — `/api/login`, `/api/auth/login`, `/v1/auth/login`, `/login`, `/sanctum/csrf-cookie`, ~30 variants — are all 404; it really is just `/v1/login`.

## Other findings

- `mobileapi.lay-z-spa.co.uk` is fully decommissioned (DNS resolves but nothing listens) — for anyone still hitting the 2020-era endpoints.
- The gateway still carries migrated copies of the old command routes (`GET /v1/gizwits/status`, `GET /v1/gizwits/turn_on`, `POST /v1/gizwits/temp_set`, …) but they return HTTP 500 even with valid tokens — broken server-side, use the direct smarthub backend instead.
- `POST /v1/register` exists, so the same MD5-password flow presumably applies to signups (untested).
- No password-reset routes found under any conventional path.
- Repeated logins can invalidate the phone app's session (consistent with the token behaviour noted upthread) — integrations should login once and cache `api_token` / the mxchip token.
- Shadow also contains raw registers `B0`–`B9`, `P0`–`P2`, `W0` — meaning still undecoded; `B1`/`B2`/`B3` track close to water temperature (possibly additional sensors).

## What this unlocks

Lay-Z-Spa app users (UK) can now use plain email + password integrations — no Charles/MITM, no visitor-ID harvesting, no QR sharing, no second device. Same shape as `gizwits`/`smartspa` backends in cdpuk/ha-bestway and gklka/homebridge-layzspa: login → pick up `did`/`product_key`/mxchip token → poll `thing_shadow` → write `device/command`.

I have a full protocol writeup + a dependency-free Python CLI (login / status / watch / power / heat / filter / wave / jet / lock / temp / raw writes) — happy to publish it or PR the backend into ha-bestway if the maintainers are interested.

Thanks again to everyone who chipped away at this over the last two and a half years — the groundwork in this issue made the last mile genuinely easy.
