# The sensor bias hack — engineering deep dive

The only method with a community success record for beating the 40C setpoint
cap (2015, Hackaday.io "Hot tub remote" project: a resistor in series with the
NTC probe -> permanent +2C offset). This document turns the folklore into a
measurable, reversible, bounded procedure — with the safety chain accounted
for. Written for the Toronto AirJet Plus (UltraFit pump, product FTEW0E).

## 1. How it works

The pump reads water temperature with an **NTC thermistor**: resistance falls
as temperature rises. The MCU converts resistance to temperature via a lookup
curve. If we add a fixed resistor **in series** with the probe, the MCU sees
`R_ntc(T_actual) + R_s`, which it interprets as a *colder* temperature. The
controller then heats until the *reading* hits the 40C setpoint — so the
actual water ends up ~1-2C above 40C.

Key property: a series resistor preserves the sensor's dynamic response (no
lag), unlike physically insulating the probe. It only shifts the reading.

## 2. What the sensor is (best available data)

- Legacy AirJet/HydroJet pumps: an NTC probe **threaded into the stainless
  heater tube** (plastic nut + O-ring), 2-wire lead with a **red connector**
  to the PCB (terminal `TEMP`/`NTC`). RapidRepair: "most Miami models require
  a standard **10k NTC**". Dual-flow HydroJets have **two** identical probes.
- The UltraFit pump uses a different internal layout (The Spa Workshop's
  sensor seal kit is explicitly "not compatible with S200xxx HydroJet or
  **Ultra Fit** pumps") — so **treat the 10k figure as unverified for our
  pump** and measure it (section 4).
- The UltraFit shadow reports **three temperature-ish registers** (B1, B2, B3)
  besides `water_temperature`. Observed: B1/B2 track water temp (~-1..-3C),
  B3 runs much hotter during heating (heater outlet). **Which probe drives the
  thermostat is unknown** — biasing the wrong one either does nothing or
  weakens the overtemp guard. Identify first (section 3).

## 3. Step 0 — identify the control sensor (no disassembly)

Poll the shadow across heating/cooling cycles and correlate:

```
python3 spa.py watch --raw --interval 60 >> shadow_log.jsonl
```

Over a natural overnight cycle (hold -> cool -> reheat), check which register
tracks `water_temperature` smoothly (control probe) versus which spikes when
`heater_state` is 2/3 (heater outlet / overtemp guard). Bias ONLY the control
probe. If B1 and B2 both track water temp, look for the probe whose readings
wander together — and expect to identify the physical counterpart in the
teardown.

## 4. Characterize the probe (one multimeter, two data points)

With the pump **unplugged** (mains out, wait 2 min for capacitors) and the
sensor connector detached, measure probe resistance at two stable, known
water temperatures (use a floating thermometer):

- `R1` at `T1` (e.g. 30.0C), `R2` at `T2` (e.g. 40.0C)

Fit the Beta model (T in Kelvin):

```
B   = ln(R1/R2) / (1/T1 - 1/T2)
R25 = R1 * exp(-B * (1/T1 - 1/298.15))
```

Then the series resistor for an offset of `d` degrees at operating point
`T_op` is simply:

```
R_s = R_ntc(T_op) - R_ntc(T_op + d)
```

## 5. Resistor sizing (assuming 10k, B=3950 — VERIFY on our probe)

| T (C) | R_ntc | sensitivity (ohm/C) |
|---|---|---|
| 20 | 12.5k | ~550 |
| 30 | 8.04k | ~340 |
| 40 | 5.30k | ~210 |
| 42 | 4.89k | ~195 |

With **R_s = 210 ohm** (1C at 40C): offset ~ +0.4C at 20C, +0.6C at 30C,
**+1.0C at 40C**, ~+1.2C at 42C.
With **R_s = 410 ohm** (2C at 40C): offset ~ +0.65C at 20C, +1.1C at 30C,
**+2.0C at 40C**, ~+2.6C at 45C.

The offset *grows with temperature* — conveniently concentrated exactly where
we want it (operating range), negligible where it doesn't matter (idle/cold).

If the probe turns out to be a 50k NTC instead, scale ~x5: about 1.0k for +1C.
The measurement step (section 4) decides.

## 6. Install — reversible, no cutting

The sensor lead terminates in a connector on the PCB (red plug on legacy
pumps). Build a **short adapter** at that connector: two mating pins/sockets
with the series resistor (metal film, 1%, 0.25W is plenty — it carries only
microamps) inline on ONE leg, sleeved in heatshrink, mounted in the dry
electronics compartment. Nothing is cut; removal restores stock in 30 seconds.

Options:
- **Fixed resistor** (recommended): soldered, essentially cannot fail. Pick
  after measuring; start at the +1C value, verify, step to +2C only if the
  calibration checks out.
- **Multi-turn trimmer** (e.g. 0-500R preset) to dial the offset: more
  convenient, BUT presets can fail open-circuit. An open series leg reads as
  "extremely cold" (max resistance) — which forces continuous heating until
  the hardware thermal cutout. If you use a trimmer, add a fixed resistor in
  series sized for your maximum acceptable offset, so the wiper failure mode
  is bounded.

## 7. What the bias does to the safety chain (measured against the manual)

| Protection | Stock | With -2C bias |
|---|---|---|
| Setpoint cap | 40C | 40C reading = **42C actual** (the goal) |
| Read-alarm "thermometer >50C" | actual 50 | **actual 52** |
| Read-alarm ">52C & water <40C = broken sensor" | shifts +2 | shifts +2 |
| Read-alarm "thermometer <4C" | actual 4 | actual 6 (freeze shield +2C early) — negligible |
| Hardware E08 resettable thermal cut-off on the heater | unaffected (bimetal switch) | **unaffected** |
| RCD | unaffected | unaffected |

So the software overtemp guard moves up by the bias, but the **hardware
cutout does not** — it remains the true backstop. Rules that follow from
this:

1. **Cap the offset at +2C.** Every extra degree moves the read-alarms too.
2. **Always keep an independent floating thermometer in the tub.** Bestway's
   own manual says panel vs external thermometer can differ by ~3C normally
   and advises verifying with an accurate thermometer.
3. **The panel and the app will lie afterwards** — they show 40C while actual
   is 41-42C. Label the tub ("runs +2C over display") so everyone who uses it
   knows. This is the hazard people forget: a guest sees "40C".
4. Verify the first heat cycle end-to-end with `spa.py watch --raw` — check
   that `water_temperature` (as displayed) settles 2C under the floating
   thermometer, that no error_code appears, and that heater_state cycles
   normally.

## 8. Order of operations

1. (now, passive) Overnight `watch --raw` log -> identify control register.
2. Teardown-lite: unplug mains, wait 2 min, open the control compartment,
   photograph the boards, locate probe(s) + connector(s). No modification yet.
3. Measure probe resistance at two known temps (section 4) -> Beta fit.
4. Compute R_s, build the adapter, install at the connector.
5. Calibrate: heat cycle, floating thermometer vs shadow, confirm +1C; step to
   +2C if wanted and re-verify.
6. Log a full cycle afterwards; keep the thermometer permanently.

## References

- Hackaday.io "Hot tub remote" (ximon; the 2015 series-resistor mod in
  comments by Hogges; ximon's note that the main board is potted)
- RapidRepair.blog — Miami thermometer repair (sensor = 10k NTC threaded into
  heater tube, `TEMP`/`NTC` PCB terminal)
- The Spa Workshop (spares ecosystem; UltraFit sensor layout differs)
- Argos S300202 manual — read-alarm thresholds (50C/52C/4C), +/-3C panel
  tolerance note, E-code table
- docs/datapoints-FTEW0E.json — the writable datapoint set (confirms no
  software calibration exists; the bias must be electrical)
