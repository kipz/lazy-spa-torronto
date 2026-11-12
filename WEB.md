# Torronto Web — control & visualisation SPA

A zero-dependency web UI for the Lay-Z-Spa: live temperature gauge, history
chart, full device controls, and the auto-heating scheduler — all in one page.

## Quick start

```bash
cp web.env.example web.env        # optional: BIND / PORT / MODE
python3 server.py                 # serve on http://0.0.0.0:8080/
```

Open `http://<host>:8080/` in a browser.

## Features

| Area | What you can do |
|---|---|
| **Gauge** | Current water temp, target badge, pulsing heater indicator |
| **Chart** | Water temp vs ambient vs target over 6h/24h/3d/7d; hover tooltip; heater-on bands |
| **Controls** | Power, heat, filter, bubbles, jet, lock toggles; 20–40 °C target slider |
| **Auto-heating** | Manual ⇄ Auto mode switch, live phase (eco/preheat/window), 24 h heater-on plan strip, replan button, config summary |
| **Settings** | Edit the default/per-day use windows, target/eco/floor temperatures and margin from the UI |
| **Feedback** | Command progress modal (sent → confirmed by status), AUTO lock hints, and a live operation log |
| **Weather** | Next-24 h forecast strip + ambient in chart/facts |

## Modes

- **Manual** — every control is direct. The scheduler does nothing.
- **Auto** — the scheduler owns `temperature_setting` (optimal-start policy:
  hot by the use window, minimum kWh). The temp slider is disabled with a hint;
  non-temp controls still work. The mode persists in `web.env`.

Switching to Auto while mid-preheat asks for confirmation.

## Feedback & the operation log

- **Manual commands** open a progress modal. It stays up while the cloud
  accepts the write, then waits for the next shadow poll to show the new value
  (switch read-backs use truthiness, so `filter_state=2`/`heater_state=4..8`
  count as ON). Success auto-closes; failures explain what happened.
- **AUTO mode** shows a lock banner and disables the scheduler-owned controls
  (power, heat, filter, target temp). Bubbles, jet and lock remain manual.
- **Operations** in the right-hand sidebar is fed by the server's in-memory
  log, so it records commands, confirmations, mode changes, settings edits,
  replans and poll failures. It updates over SSE and survives page reloads for
  the lifetime of the server process.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Live shadow + mode + phase + setting |
| GET | `/api/history?range=24` | Water/target/heater/ambient series (hours) |
| GET | `/api/plan` | 48 h optimal-start heater plan + energy |
| GET | `/api/scheduler` | Mode, windows, per-day overrides, target/eco/floor/margin |
| POST | `/api/scheduler` | `{"mode":"auto"\|"manual"}`, `{"replan":true}`, or `{"config":{...}}` |
| GET | `/api/weather` | Next-24 h forecast |
| POST | `/api/cmd` | `{"datapoint","value"}` — clamped 20–40, temp rejected in auto |
| GET | `/api/health` | Connectivity, last update age |
| GET | `/api/log?limit=100` | Recent server-side operations |
| GET | `/api/events` | SSE live push (state/plan/mode/config/log) |

A config update looks like:

```json
{"config": {
  "window": "17:00-23:00",
  "target": 40, "eco": 28, "floor": 8, "margin": 0,
  "overrides": {"SAT": "14:00-23:00", "SUN": ""}
}}
```

It is validated (HH:MM windows, `floor ≤ eco ≤ target`, device temp clamp),
saved to `scheduler.env`, and immediately replanned. A window whose end is
earlier than its start is an overnight window: `22:00-01:00` runs from 22:00
on its configured day until 01:00 the next morning. Start and end must differ.

Command datapoints: `power_state`, `heater_state`, `filter_state`,
`wave_state`, `hydrojet_state`, `locked`, `temperature_setting` (aliases
`power`/`heat`/`filter`/`wave`/`jet`/`lock` also accepted).

## How it works

`server.py` (stdlib `ThreadingHTTPServer`) runs three background threads:

1. **Poller** — fetches the shadow every 15 s, appends to `shadow_log.jsonl`
   history, and refreshes the heating plan (apply only in Auto mode).
2. **Scheduler thread** — reuses `scheduler.scheduler_step()` from
   `scheduler.py` (weather → optimal-start plan → temperature_setting).
3. **SSE** — pushes state/plan/mode events to the browser so the UI is live.

The frontend is hand-rolled vanilla HTML/CSS/JS (no npm, no CDN, no build
step). The chart is a custom `<canvas>` renderer with `requestAnimationFrame`.

## Config (`web.env`, gitignored)

```
BIND=0.0.0.0      # bind address
PORT=8080         # port
MODE=manual       # initial mode (auto | manual)
```

Scheduler parameters (windows, target/eco/floor, margin) come from
`scheduler.env`, exactly as the CLI scheduler uses them.

## Security

> [!CAUTION]
> **This server controls a real device and has NO authentication.** It is
> intended for a trusted LAN only. **Do not expose it to the internet.**

- Binds `0.0.0.0` by default — restrict with `BIND=127.0.0.1` for localhost
  only, or run behind a reverse proxy with auth for LAN/phone access.
- The server enforces the same invariants as the scheduler: 40 °C clamp,
  floor temps, and temp writes are **rejected while in Auto mode**.
- Credentials never leave `secrets.env`/`tokens.json` (gitignored); the API
  only exposes spa state and commands.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite mocks the spa cloud and weather APIs — no network, no real device.

## Troubleshooting

- **"offline" badge** — the cloud API is unreachable or tokens expired;
  re-login with `python3 spa.py login`.
- **Chart empty** — history fills as the poller runs; give it a minute.
- **Auto mode won't switch** — the spa must be reachable (a plan needs live
  water temp).
