# Weather-aware heating scheduler

`python3 scheduler.py` plans when the 2 kW heater should run so the tub hits
**TARGET_TEMP (default 40 °C) by the start of each use window** while burning
as little energy as possible. It uses a free hourly weather forecast
(Open-Meteo, no API key) and a fitted thermal model of the tub, re-planning
every hour (receding horizon / MPC flavour).

## TL;DR

```bash
cp scheduler.env.example scheduler.env   # set LOCATION, windows, temps
python3 scheduler.py plan                # dry run: print the next 48h of heater decisions
python3 scheduler.py run                 # persistent controller (applies to the spa)
python3 scheduler.py run --dry-run       # log what it WOULD do, touch nothing
python3 scheduler.py calibrate           # fit loss coefficient + heater power from shadow log
python3 scheduler.py selftest            # physics sanity checks
```

## The thermal model

Newton's law of cooling plus the heater, discretised hourly:

```
C · dT/dt = P_heat · u(t) − UA · (T(t) − T_amb(t))
```

| Symbol | Value | Meaning |
|---|---|---|
| `C` | 1.45 kWh/°C | thermal mass of 1250 L water |
| `P_heat` | ~2 kW | heater power (community-measured; verify by `calibrate`) |
| `UA` | ~0.03 kW/°C | heat-loss coefficient (covered tub; fitted by `calibrate`) |
| `u` | 0/1 | heater on/off |
| `T_amb` | hourly forecast | Open-Meteo `temperature_2m`, UKMO model |

Temperature is clamped at 40 °C (the device's hard cap).

## The optimality argument

For a first-order thermal system with a hard deadline ("must be at 40 by
18:00") and no price signal, the **energy-minimal policy is bang-bang: heat at
full power as late as possible**. Every earlier preheat kWh is partly wasted —
losses scale with ΔT and time, so holding temperature costs more than coasting
down and reheating. This is the same structure used by grid-interactive water
heater MPC research (Stanford S3L, Buechler 2025) and smart-thermostat
"optimal start" recovery.

`optimal_start()` implements it with **backward induction**:

1. `req[i]` = minimum water temp required at the start of hour `i`. Starting
   from the horizon end, each hour is assigned the coldest feasible start temp
   (`need_on < need_off`, because heating lets you start colder).
2. Forward sweep turns the heater **on only when coasting would violate a
   future requirement**, i.e. exactly when it must run to make the deadline —
   never earlier.
3. During use windows the heater holds TARGET (mirroring the spa's real
   thermostat), then coasts to `ECO_TEMP` overnight.

Energy cost = Σ P_heat · u · Δt over the horizon (kWh).

### Policy invariants
- `T ≥ TARGET_TEMP` from the start of every use window (plus `MARGIN` for the
  compartment-sensor vs bulk-water offset)
- `T ≥ ECO_TEMP` while unused (comfort floor; also keeps reheat feasible —
  reheating 1250 L from 8→40 °C takes ~23 h at 2 kW, so a warm floor is what
  makes "hot by 5pm" actually achievable)
- `T ≥ FLOOR_TEMP` always (freeze protection)
- `T ≤ 40` always (device clamp)

## Controller behaviour (`run`)

Instead of toggling `heater_state` (which would make the spa overshoot to 40
whenever we wanted it at 28), the controller **switches `temperature_setting`**
and lets the spa's own thermostat do the on/off:

- `phase = window|preheat` → set `temperature_setting = TARGET_TEMP`
- `phase = eco` → set `temperature_setting = ECO_TEMP`

`hot_phase_now()` classifies the current hour: inside a window, or inside a
continuous heating block that runs straight into a window ("preheat"), or
coasting/holding ("eco"). The setting is only written when it changes.

Every hour it: fetches weather → re-plans → diffs the current shadow state →
sends commands only if something changed → sleeps to the top of the next hour
(min 10 min, with 10-min retry backoff on cloud errors). A log line is
appended to `scheduler.log` per cycle.

## Calibration

`calibrate` fits the two model parameters from `shadow_log.jsonl` (the
`spa.py watch --raw` log) against historical weather:

- **UA** from heater-off *coast-down* stretches: `UA = C·(−dT/dt)/(T−T_amb)`
- **P** from genuine *heating-phase* stretches (temp clearly rising) —
  holding stretches (heater on, temp ~constant at setpoint) are **excluded**
  because they measure loss-compensating power, not heater output

Fits need ≥3 clean samples each; otherwise the config defaults are kept.
Results are written to `scheduler_model.json` (gitignored) and loaded by `run`.

For the best fit: run `python3 spa.py watch --raw --interval 300 >> shadow_log.jsonl`
through one full overnight coast (heater off) plus one heating cycle.

## Weather & privacy

- Forecast: `api.open-meteo.com` — free, no key, CC BY 4.0 attribution; UKMO
  model auto-selected for UK locations, updates hourly, 16-day horizon.
- Geocoding: `geocoding-api.open-meteo.com` resolves `LOCATION` (city or
  postcode) to lat/lon; or set `LAT=`/`LON=` directly.
- `LOCATION`, the fitted model, and the runtime log are all in gitignored
  files (`scheduler.env`, `scheduler_model.json`, `scheduler.log`) — nothing
  location-specific is committed.

## Failure handling
- Cloud/network down: hold the last applied setting, retry every 10 min.
- Forecast outage: missing hours fall back to the previous hour's temperature.
- Heater or pump unresponsive: commands are simply not confirmed; the loop
  keeps logging and retrying.
- `FLOOR_TEMP` and the 40 °C clamp are hard invariants inside the optimizer,
  so even a bad forecast cannot schedule the tub below freezing.

## Safety notes
- This is unofficial reverse-engineered control of a real device. Test with
  `plan` and `run --dry-run` before letting it drive the tub.
- The displayed temperature is a compartment sensor (see SENSOR_HACK.md), so
  keep `MARGIN` ≥ the observed display-vs-bulk offset if the tub must be
  genuinely hot at the window start.
- `run` needs the scheduler to stay alive (systemd unit, tmux, etc.) — it is
  cloud-dependent, like the rest of this repo.
