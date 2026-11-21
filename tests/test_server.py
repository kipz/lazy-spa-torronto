"""Unit tests for server.py — config, state/history, commands, mode switch.

The spa cloud and weather APIs are mocked; no network and no real device.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, ".")
import server  # noqa: E402
import spa  # noqa: E402


class FakeState(dict):
    pass


def _shadow():
    return {
        "water_temperature": 39, "temperature_setting": 40, "temperature_unit": 1,
        "power_state": 1, "heater_state": 6, "filter_state": 2,
        "wave_state": 0, "hydrojet_state": 0, "warning": "1",
        "error_code": "", "ConnectType": "online", "wifivertion": 121,
    }


def _spa_app(cfg):
    """Build a real SpaServer with real side effects disabled so the unit
    suite stays hermetic: no Telegram bot poll thread (telegram.env is present
    in the repo), no real web.env read, nothing hitting the network."""
    cfg.plug_notify = False                 # opt-in relay alerts stay off
    cfg.plug_notify_cooldown = 0.0
    with mock.patch.object(server.notify, "configured", return_value=False), \
         mock.patch.object(server.scheduler, "load_env", return_value={}):
        return server.SpaServer(cfg)


class TestHeaterStatus(unittest.TestCase):
    def test_eco_coastdown_is_cooling(self):
        # eco phase: water way above setpoint, but heater_state 4 (holding)
        st = _shadow()
        st.update({"water_temperature": 40, "temperature_setting": 28,
                   "heater_state": 4, "power_state": 1})
        self.assertEqual(server.heater_status(st), "cooling")

    def test_heater_off_coastdown_is_cooling(self):
        # After scheduler_step commands the heater off in ECO, the shadow shows
        # heater_state 0: the UI must still read "cooling", not "idle"/"off".
        st = _shadow()
        st.update({"water_temperature": 40, "temperature_setting": 28,
                   "heater_state": 0, "power_state": 1})
        self.assertEqual(server.heater_status(st), "cooling")

    def test_window_hold_is_holding(self):
        st = _shadow()
        st.update({"water_temperature": 40, "temperature_setting": 40,
                   "heater_state": 6, "power_state": 1})
        self.assertEqual(server.heater_status(st), "holding")

    def test_reheat_pull_is_heating(self):
        st = _shadow()
        st.update({"water_temperature": 39, "temperature_setting": 40,
                   "heater_state": 6, "power_state": 1})
        self.assertEqual(server.heater_status(st), "heating")

    def test_heating_codes(self):
        for code in (1, 2, 3):
            st = _shadow()
            st.update({"water_temperature": 37, "temperature_setting": 40,
                       "heater_state": code, "power_state": 1})
            self.assertEqual(server.heater_status(st), "heating")

    def test_off_power(self):
        st = _shadow()
        st.update({"power_state": 0, "heater_state": 4})
        self.assertEqual(server.heater_status(st), "off")

    def test_frost_watch(self):
        st = _shadow()
        st.update({"water_temperature": 8, "temperature_setting": 28,
                   "heater_state": 7, "power_state": 1})
        self.assertEqual(server.heater_status(st), "frost-watch")

    def test_state_endpoint_carries_heater(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, server, "BASE_DIR", server.BASE_DIR)
        server.BASE_DIR = tmp.name
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        app = _spa_app(cfg)
        app.update_state({**_shadow(), "water_temperature": 40,
                          "temperature_setting": 28, "heater_state": 4})
        # exercise the payload builder directly
        state, _ = app.get_state()
        self.assertEqual(server.heater_status(state), "cooling")


class TestTubConnectivity(unittest.TestCase):
    """Offline/staleness handling surfaced by the server."""

    def _app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, server, "BASE_DIR", server.BASE_DIR)
        server.BASE_DIR = self.tmp.name
        cfg = mock.MagicMock()
        cfg.mode = "auto"
        cfg.scheduler_cfg = _scheduler_cfg()
        return _spa_app(cfg)

    def test_heater_status_offline(self):
        st = _shadow()
        st.update({"is_online": False, "ConnectType": "offline", "heater_state": 6})
        self.assertEqual(server.heater_status(st), "offline")

    def test_tub_online_fresh_shadow(self):
        app = self._app()
        with mock.patch.object(server.time, "time", return_value=1_800_000_000):
            app.update_state({**_shadow(), "is_online": True})
        self.assertTrue(app.tub_online())

    def test_tub_offline_when_device_reports_offline(self):
        app = self._app()
        with mock.patch.object(server.time, "time", return_value=1_800_000_000):
            app.update_state({**_shadow(), "is_online": False, "ConnectType": "offline"})
        self.assertFalse(app.tub_online())

    def test_tub_offline_when_shadow_stale(self):
        app = self._app()
        with mock.patch.object(server.time, "time", return_value=1_800_000_000):
            app.update_state({**_shadow(), "is_online": True})
        with mock.patch.object(server.time, "time",
                               return_value=1_800_000_000 + server.STATE_STALE_SECONDS * 2):
            self.assertFalse(app.tub_online())

    def test_offline_transition_logs_and_notifies(self):
        app = self._app()
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text", return_value=(True, "")) as send, \
             mock.patch.object(server.time, "time", return_value=1_800_000_000):
            app.update_state({**_shadow(), "is_online": True})
            app.update_state({**_shadow(), "is_online": False, "ConnectType": "offline"})
        send.assert_called_once()
        msgs = [e["msg"] for e in app.get_log(50)]
        self.assertTrue(any("Tub OFFLINE" in m for m in msgs), msgs)


class TestWebConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            cfg = server.WebConfig()
        self.assertEqual(cfg.bind, "0.0.0.0")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.mode, "manual")

    def test_mode_from_env(self):
        with mock.patch.object(server.scheduler, "load_env",
                               return_value={"MODE": "auto", "PORT": "9000"}):
            cfg = server.WebConfig()
        self.assertEqual(cfg.mode, "auto")
        self.assertEqual(cfg.port, 9000)


def _scheduler_cfg():
    cfg = dict(server.scheduler.DEFAULTS)
    cfg.update({"WINDOW": "17:00-23:00", "TARGET_TEMP": 40, "ECO_TEMP": 28,
                "FLOOR_TEMP": 8, "MARGIN": 0.0})
    cfg["LAT"], cfg["LON"] = 51.5, -0.12  # avoid geocoding network calls
    return cfg


class TestSpaServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, server, "BASE_DIR", server.BASE_DIR)
        server.BASE_DIR = self.tmp.name
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        self.app = _spa_app(cfg)

    def test_update_state_appends_history(self):
        self.app.update_state(_shadow())
        hist = self.app.get_history(24)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["water"], 39)
        self.assertIsNone(hist[0]["ambient"])

    def test_plug_readings_energy_and_notify(self):
        app = self.app
        app.cfg.plug_notify = False          # the opt-in default
        app.cfg.plug_notify_cooldown = 0.0
        mains = {"device": "Tub mains", "device_id": "plug-mains",
                 "switch_on": False, "watts": 0.0, "voltage_v": 239.8,
                 "current_a": 0.0, "fault": 0, "online": True}
        lights = {"device": "Tub lights", "device_id": "plug-lights",
                  "switch_on": False, "watts": 0.0, "voltage_v": 239.8,
                  "current_a": 0.0, "fault": 0, "online": True}
        app.update_plug(dict(mains))
        app.update_plug(dict(lights))
        plugs, kwh = app.get_plugs()
        self.assertIn("plug-mains", plugs)
        self.assertIn("plug-lights", plugs)
        self.assertEqual(plugs["plug-lights"]["device"], "Tub lights")
        # 2000 W for 1 s -> ~0.0006 kWh, integrated only for the meter
        app._plug_prev_ts = time.time() - 1
        app.update_plug(dict(mains, switch_on=True, watts=2000.0))
        _, kwh = app.get_plugs()
        self.assertGreater(kwh, 0.0)
        self.assertLess(kwh, 0.01)
        # mains samples are recorded from the very first read (sparkline seed)
        self.assertEqual(len(app.get_plug_history(24)), 2)
        # measured window kWh (real-draw basis for cost): 2 kW for 60 s
        app._plug_hist = [
            {"ts": int(time.time()) - 120, "watts": 2000.0, "on": True, "fault": 0},
            {"ts": int(time.time()) - 60, "watts": 2000.0, "on": True, "fault": 0},
        ]
        self.assertAlmostEqual(app.plug_kwh_window(24),
                               2000.0 / 1000.0 * (60.0 / 3600.0), delta=0.0001)
        # lights relay toggles -> Telegram notification (opt-in: PLUG_NOTIFY=1,
        # and only after the state holds for two consecutive polls)
        self.app.cfg.plug_notify = True
        self.app.cfg.plug_notify_cooldown = 0.0
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            app.update_plug(dict(lights, switch_on=True))   # first sighting
            app.update_plug(dict(lights, switch_on=True))   # confirmed -> alert
        send.assert_called_once()
        self.assertIn("Tub lights", send.call_args[0][0])

    def test_plug_read_failure_quiet_suppresses_duplicate_log(self):
        # When the spa poll dies on the same network error, poll_plug(quiet)
        # still flags the plugs offline but must not add a second "Plug read
        # failed" line to the operation feed.
        app = self.app
        app._plugs = {"mains": {"device": "Tub mains", "online": True, "ts": 1}}
        with mock.patch.object(server.plug, "read_all", return_value=None):
            app.poll_plug(quiet=True)
        msgs = [e["msg"] for e in app.get_log(20)]
        self.assertFalse(any("Plug read failed" in m for m in msgs), msgs)
        self.assertFalse(app._plugs["mains"]["online"])   # still flagged
        # a standalone failure (no spa error) should still be logged (the
        # quiet call incremented the throttle counter, so reset it)
        app._plug_err = 0
        with mock.patch.object(server.plug, "read_all", return_value=None):
            app.poll_plug(quiet=False)
        msgs = [e["msg"] for e in app.get_log(20)]
        self.assertTrue(any("Plug read failed" in m for m in msgs), msgs)

    def test_measured_cost_per_day_figures(self):
        # "how much did it cost" needs a real calendar day + an average over
        # the recent full days of metering — both from the smart plug's real
        # wattage, and both excluding the partial current day.
        app = self.app
        app.cfg.scheduler_cfg["PRICE_PER_KWH"] = 0.25
        now = 1_800_000_000                      # 2017-01-19 ~04:00 UTC
        start = now - 3 * 86400
        hist = []
        ts = int(start)
        while ts < now:
            hour = dt.datetime.fromtimestamp(ts).hour
            watts = 1800.0 if 18 <= hour < 21 else 12.0   # heater 18:00-21:00
            hist.append({"ts": ts, "watts": watts, "on": True, "fault": 0})
            ts += 900                              # 4 samples/hour
        with app._lock:
            app._plug_hist = hist
        with mock.patch.object(server.time, "time", return_value=now):
            mc = app._measured_cost_payload()
        self.assertTrue(mc["enough"])
        self.assertIsNotNone(mc["kwh_yesterday"])
        # 3-day span: the newest full day, the one before it, and the oldest
        # day only has 16 h of coverage (samples start mid-day) so is excluded
        self.assertEqual(mc["days_measured"], 2)
        # each full day carries ~3 h of 1800 W
        self.assertAlmostEqual(mc["kwh_yesterday"], 3 * 1.8, delta=0.3)
        self.assertAlmostEqual(mc["cost_yesterday"],
                               0.25 * mc["kwh_yesterday"], delta=0.1)
        self.assertAlmostEqual(mc["kwh_avg_day"], mc["kwh_yesterday"], delta=0.3)
        self.assertIsNotNone(mc["cost_avg_day"])

    def test_plug_relay_alerts_off_by_default_and_debounced(self):
        # The user reported plug relay alerts as spam: PLUG_NOTIFY defaults to
        # off, so a real toggle must never notify without an explicit enable.
        app = self.app
        app.cfg.plug_notify = False            # the opt-in default
        app.cfg.plug_notify_cooldown = 0.0
        lights = {"device": "Tub lights", "device_id": "plug-lights",
                  "switch_on": False, "watts": 0.0, "voltage_v": 239.8,
                  "current_a": 0.0, "fault": 0, "online": True}
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            for _ in range(5):
                app.update_plug(dict(lights, switch_on=True))
        send.assert_not_called()          # feature off by default
        # With debounce but cooldown, a one-poll glitch (OFF -> ON -> OFF) is
        # never reported; a held ON is, but only if the cooldown has passed.
        self.app.cfg.plug_notify = True
        self.app.cfg.plug_notify_cooldown = 3600.0
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            # True False True True -> first confirmed ON alerts (no prior alert)
            for on in (True, False, True, True):
                app.update_plug(dict(lights, switch_on=on))
            send.assert_called_once()
            # ...but a second held toggle within the 1 h cooldown does not
            for on in (False, True, True):
                app.update_plug(dict(lights, switch_on=on))
            send.assert_called_once()         # cooldown respected
        # A held toggle (OFF -> ON -> ON) with a zero cooldown reports exactly
        # once, only after the second consecutive ON poll.
        self.app.cfg.plug_notify_cooldown = 0.0
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            for on in (False, True, True):
                app.update_plug(dict(lights, switch_on=on))
        send.assert_called_once()

    def test_plug_history_persists_across_restart(self):
        app = self.app
        mains = {"device": "Tub mains", "device_id": "plug-mains",
                 "switch_on": True, "watts": 2000.0, "voltage_v": 239.8,
                 "current_a": 8.0, "fault": 0, "online": True}
        # two reads -> the power sample is persisted to plug_log.jsonl
        app.update_plug(dict(mains))
        app.update_plug(dict(mains))
        path = os.path.join(self.tmp.name, server.PLUG_LOG_FILE)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(rows), 2)
        # simulate a restart: a fresh SpaServer over the same dir reloads them
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            app2 = _spa_app(cfg)
        self.assertEqual(len(app2._plug_hist), 2)
        self.assertEqual(app2._plug_hist[0]["watts"], 2000.0)

    def test_plug_history_seed_integrates_measured_kwh(self):
        # 2 kW for a full minute, persisted at boot -> 7-day window reflects it
        base = int(time.time()) - 120
        with open(os.path.join(self.tmp.name, server.PLUG_LOG_FILE), "w") as f:
            for ts in (base, base + 60):
                f.write(json.dumps({"ts": ts, "watts": 2000.0,
                                    "on": True, "fault": 0}) + "\n")
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            app2 = _spa_app(cfg)
        self.assertAlmostEqual(
            app2.plug_kwh_window(168), 2000.0 / 1000.0 * (60.0 / 3600.0), places=4)
        payload = server.scheduler_payload(app2)
        self.assertIn("measured_kwh_7d", payload)
        self.assertGreater(payload["measured_kwh_7d"], 0.0)

    def test_update_state_persists_shadow_log(self):
        self.app.update_state(_shadow())
        path = os.path.join(self.tmp.name, "shadow_log.jsonl")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["shadow"]["water_temperature"], 39)
        # throttled: an immediate second poll does not append
        self.app.update_state(_shadow())
        with open(path) as f:
            self.assertEqual(len([l for l in f if l.strip()]), 1)

    def test_measures_cooling_rate_from_coast_downs(self):
        # Sampling pairs readings SEVERAL hours apart (UA_SAMPLE_MIN_S): with
        # the spa's 1 C resolution a 1 h window cannot separate a
        # well-insulated "hold" from real cooling (that overfit was the
        # "preheat at noon" bug), so 3 h spans are the unit being tested.
        base = 1_800_000_000            # aligned to the hour
        self.app._amb = {base + h * 3600: 5.0 for h in range(16)}
        self.app.cfg.scheduler_cfg["UA_KW_PER_C"] = 0.06
        clock = {"now": base}
        states = [(38.6, 28), (37.2, 28), (35.8, 28)]
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._maybe_sample_ua({
                "water_temperature": 40, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})       # anchor at +0h
            for i, (water, setpoint) in enumerate(states, start=1):
                clock["now"] = base + i * 10800
                self.app._maybe_sample_ua({
                    "water_temperature": water, "temperature_setting": setpoint,
                    "power_state": 1, "heater_state": 0})
        self.assertEqual(len(self.app._ua_samples), 3)
        # exact Newton-cooling fit over 3 h spans: median ~0.0203 kW/C
        self.assertAlmostEqual(self.app.cfg.scheduler_cfg["UA_KW_PER_C"],
                               0.0203, delta=0.002)
        path = os.path.join(self.tmp.name, "scheduler_model.json")
        with open(path) as f:
            model = json.load(f)
        self.assertEqual(model["source"], "online coast-down")
        # model last updated on the 2nd sample (UA_MIN_SAMPLES=2); the third
        # is within tolerance so no re-save happened -> record says 2 samples
        self.assertEqual(model["ua_samples"], 2)
        self.assertAlmostEqual(model["UA_KW_PER_C"], 0.0203, delta=0.002)

    def test_register_telemetry_logs_probe_offsets(self):
        app = self.app
        base = 1_800_000_000
        clock = {"now": base}
        def tick(state):
            clock["now"] += 60
            with mock.patch.object(server.time, "time", lambda: clock["now"]):
                app._monitor_registers(state)
        tick({"water_temperature": 40})                 # no probes yet
        for _ in range(8):                              # build offset baselines
            tick({"water_temperature": 40, "B1": 41, "B2": 41, "B9": 40})
        self.assertEqual(app._reg_offsets,
                         {"B1": 1.0, "B2": 1.0, "B9": 0.0})
        # let the hourly summary fire and check it appears in the op log
        app._reg_mon_ts = clock["now"] - 7200
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            app._monitor_registers({"water_temperature": 40,
                                    "B1": 41, "B2": 41, "B9": 40})
        msgs = [e["msg"] for e in app.get_log(60)]
        self.assertTrue(any(m.startswith("registers:") for m in msgs), msgs)
        self.assertTrue(any("B1+1.0" in m and "B2+1.0" in m for m in msgs), msgs)

    def test_ua_learns_held_temp_as_upper_bound(self):
        # A well-insulated tub can hold the same integer degree for hours.
        # That "no drop" is still evidence of low heat loss: it must produce
        # an upper-bound sample (0.5 C assumed drop over the span), not be
        # dismissed. The fit uses the exact Newton-cooling log form.
        base = 1_800_000_000            # aligned to the hour
        self.app._amb = {base + h * 3600: 5.0 for h in range(4)}
        self.app.cfg.scheduler_cfg["UA_KW_PER_C"] = 0.03
        clock = {"now": base}
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._maybe_sample_ua({
                "water_temperature": 40, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})       # anchor at +0h
            clock["now"] = base + 10800
            self.app._maybe_sample_ua({
                "water_temperature": 40, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})       # held at 40
        self.assertEqual(len(self.app._ua_samples), 1)
        C = self.app.cfg.scheduler_cfg["C_KWH_PER_C"]
        import math
        expected = C / 3.0 * math.log(35.0 / 34.5)  # 0.5 C drop over 3 h
        self.assertAlmostEqual(self.app._ua_samples[-1], expected, delta=0.0005)

    def test_history_seeded_from_shadow_log(self):
        base = int(time.time())
        with open(os.path.join(self.tmp.name, "shadow_log.jsonl"), "w") as f:
            for i in range(10):
                f.write(json.dumps({"ts": base - (9 - i) * 300,
                                    "shadow": {"water_temperature": 39 + (i % 2),
                                               "temperature_setting": 40,
                                               "heater_state": 6, "power_state": 1}},
                                   separators=(",", ":")) + "\n")
        self.app._seed_history_from_log()
        hist = self.app.get_history(1)
        self.assertGreaterEqual(len(hist), 9)
        self.assertEqual(hist[0]["water"] if hist else None, 39)

    def test_register_history_derives_heater_kw(self):
        base = int(time.time()) - (int(time.time()) % 3600)
        rows = [
            {"ts": base - 2 * 3600, "shadow": {"water_temperature": 38, "heater_state": 6, "B3": 43}},
            {"ts": base - 3600,     "shadow": {"water_temperature": 39, "heater_state": 6, "B3": 43}},
            {"ts": base,            "shadow": {"water_temperature": 40, "heater_state": 4, "B3": 43}},
        ]
        with open(os.path.join(self.tmp.name, "shadow_log.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        with mock.patch.object(server.scheduler, "fetch_forecast", return_value={}):
            series = self.app._register_history(24)
        self.assertEqual(len(series), 3)
        self.assertTrue(series[1]["on"])      # heater_state 6 -> elements on
        self.assertFalse(series[2]["on"])     # heater_state 4 -> holding, off
        self.assertEqual(series[1]["b3"], 43)
        self.assertIsNotNone(series[1]["kw"])
        self.assertIsNone(series[2]["kw"])    # heater off -> no inferred kW
        self.assertAlmostEqual(series[1]["kw"], 1.45 * 1.0 / 1.0, places=2)  # C*dT/dt

    def test_does_not_fit_while_heating(self):
        base = 1_800_000_000
        self.app._amb = {base + h * 3600: 5.0 for h in range(8)}
        clock = {"now": base}
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._maybe_sample_ua({
                "water_temperature": 35, "temperature_setting": 40,
                "power_state": 1, "heater_state": 2})       # heating
            clock["now"] = base + 7200
            self.app._maybe_sample_ua({
                "water_temperature": 37, "temperature_setting": 40,
                "power_state": 1, "heater_state": 2})
        self.assertEqual(self.app._ua_samples, [])

    def test_ua_anchor_ambient_captured_at_anchor_time(self):
        # The anchor's ambient is stored on the anchor itself, so the sampler
        # must still produce a sample even after the forecast cache stops
        # covering the older hour (e.g. a replan drops it).
        base = 1_800_000_000
        self.app._amb = {base: 5.0}                     # only the anchor hour
        clock = {"now": base}
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._maybe_sample_ua({
                "water_temperature": 38.0, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})    # anchor at +0h
            clock["now"] = base + 10800
            self.app._amb = {}                           # cache gone after replan
            self.app._maybe_sample_ua({
                "water_temperature": 36.6, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})    # cooled ~1.4C in 3h
        self.assertEqual(len(self.app._ua_samples), 1)

    def test_notify_target_reached_shoots_once_per_hour(self):
        clock = {"now": 100.0}
        self.app._notify_at = clock["now"] - 4000   # past the hourly cooldown
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send, \
             mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._plan = {"phase": "window"}
            self.app._maybe_notify({"water_temperature": 39, "power_state": 1})
            self.app._maybe_notify({"water_temperature": 40, "power_state": 1})  # edge -> send
            self.app._maybe_notify({"water_temperature": 38, "power_state": 1})  # drop
            clock["now"] = 110.0
            self.app._maybe_notify({"water_temperature": 40, "power_state": 1})  # edge, cooled down < 1h
        self.assertEqual(send.call_count, 1)
        msgs = [e["msg"] for e in self.app.get_log(50)]
        self.assertTrue(any("Telegram target-reached sent" in m for m in msgs), msgs)

    def test_tg_send_queues_failed_message_for_retry(self):
        app = self.app
        # success: not queued
        with mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            ok, err = app._tg_send("hello")
        self.assertTrue(ok)
        send.assert_called_once_with("hello")
        self.assertEqual(app._pending_tg, [])
        # transient failure: queued, deduped, bounded
        with mock.patch.object(server.notify, "send_text",
                               return_value=(False, "read timed out")):
            ok, err = app._tg_send("alert-1")
            ok2, err2 = app._tg_send("alert-1")        # identical -> deduped
            ok3, err3 = app._tg_send("alert-2")
        self.assertFalse(ok)
        self.assertFalse(ok2)
        self.assertEqual(len(app._pending_tg), 2)      # alert-1 counted once
        self.assertEqual([m["text"] for m in app._pending_tg],
                         ["alert-1", "alert-2"])

    def test_tg_flush_retries_then_drops_stale(self):
        app = self.app
        app._pending_tg = [
            {"text": "fresh", "ts": 100},
            {"text": "stale", "ts": 100},
        ]
        # stale is dropped (older than the 1 h TTL), fresh is retried
        clock = {"now": 100 + app._pending_tg_ttl + 10}
        with mock.patch.object(server.time, "time", lambda: clock["now"]), \
             mock.patch.object(server.notify, "send_text",
                               side_effect=[(True, "")]) as send:
            app._pending_tg[0]["ts"] = clock["now"] - 10   # fresh
            app._pending_tg[1]["ts"] = clock["now"] - 4000  # stale
            app._flush_pending_tg()
        send.assert_called_once_with("fresh")
        self.assertEqual(app._pending_tg, [])
        # if the retry also fails the message stays queued
        with mock.patch.object(server.notify, "send_text",
                               return_value=(False, "nope")):
            app._pending_tg = [{"text": "x", "ts": 200}]
            app._flush_pending_tg(now=210)
        self.assertEqual([m["text"] for m in app._pending_tg], ["x"])

    def test_notify_skipped_in_eco(self):
        clock = {"now": 100.0}
        self.app._notify_at = clock["now"] - 4000
        with mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text") as send, \
             mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._plan = {"phase": "eco"}
            self.app._maybe_notify({"water_temperature": 40, "power_state": 1})
        send.assert_not_called()

    def test_notify_skipped_when_unconfigured(self):
        with mock.patch.object(server.notify, "configured", return_value=False), \
             mock.patch.object(server.notify, "send_text") as send:
            self.app._maybe_notify({"water_temperature": 40})
        send.assert_not_called()

    def test_tg_command_status(self):
        self.app.update_state({**_shadow(), "water_temperature": 39,
                               "temperature_setting": 40, "power_state": 1,
                               "heater_state": 4, "filter_state": 2})
        self.app._plan = {"phase": "preheat", "setting": 40}
        s = self.app._tg_command("/status")
        self.assertIn("Torronto", s)
        self.assertIn("preheat", s)
        self.assertIn("39°C", s)
        self.assertIn("heater", s)

    def test_tg_command_mode(self):
        self.app._plan = {"phase": "window", "setting": 40}
        s = self.app._tg_command("/mode")
        self.assertIn("MANUAL", s)
        self.assertIn("window", s)

    def test_tg_command_plan_lists_next_window_and_energy(self):
        now = int(time.time()) - (int(time.time()) % 3600)
        ts = [now + i * 3600 for i in range(48)]
        self.app._plan = {
            "phase": "eco", "setting": 28, "plan_ts": ts,
            "plan_phase": ["eco"] * 20 + ["window"] * 4 + ["eco"] * 24,
            "plan_us": [1, 1] + [0] * 46, "energy_kwh": 12.5}
        s = self.app._tg_command("/plan")
        self.assertIn("window", s)
        self.assertIn("12.5", s)

    def test_tg_command_cost(self):
        now = int(time.time()) - (int(time.time()) % 3600)
        self.app._plan = {"phase": "eco", "energy_kwh": 4.0,
                          "energy_alwayson_kwh": 10.0,
                          "plan_ts": [now + i * 3600 for i in range(24 * 7)]}
        self.app.cfg.scheduler_cfg["PRICE_PER_KWH"] = 0.2263
        s = self.app._tg_command("/cost")
        self.assertIn("kWh", s)
        self.assertIn("month", s)
        self.assertIn("0.2263", s)

    def test_tg_cost_leads_with_measured_plug_and_offline_warning(self):
        # Measured plug kWh is the ground truth; a stale plan / offline tub
        # must be labelled instead of being presented as a hard monthly bill.
        now = int(time.time()) - (int(time.time()) % 3600)
        self.app._plan = {"phase": "eco", "energy_kwh": 4.0,
                          "energy_alwayson_kwh": 10.0,
                          "plan_ts": [now + i * 3600 for i in range(24 * 7)]}
        self.app.cfg.scheduler_cfg["PRICE_PER_KWH"] = 0.2263
        with mock.patch.object(self.app, "plug_enabled", return_value=True), \
             mock.patch.object(self.app, "plug_kwh_window", return_value=5.0):
            s = self.app._tg_command("/cost")
        self.assertIn("measured (plug) 24h: 5.00 kWh", s)
        self.assertIn("measured (plug) 7d : 5.00 kWh", s)
        # no fresh plan / tub offline -> projections flagged as estimates
        self.assertIn("estimate", s)
        self.assertIn("month", s)

    def test_poller_survives_system_exit(self):
        # A stray SystemExit from a cloud call must not kill the poller loop
        # (this was the incident: an expired token made spa.shadow() raise
        # SystemExit, which silently stopped monitoring + auto-heating).
        import threading
        app = mock.MagicMock()
        app._get_tokens.return_value = {"t": 1}
        app.cfg.mode = "manual"
        app.plug_enabled.return_value = False
        app.refresh_plan.side_effect = lambda **kw: None
        app._plug_next = 0.0
        state = {"water_temperature": 39, "is_online": True}
        calls = {"n": 0}

        def flaky_shadow(tokens):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit("thing_shadow failed: Token is not authorized")
            return state

        stop = threading.Event()
        with mock.patch.object(server.spa, "shadow", side_effect=flaky_shadow), \
             mock.patch.object(server, "POLL_SECONDS", 0.02):
            t = threading.Thread(target=server._poller_loop, args=(app, stop), daemon=True)
            t.start()
            time.sleep(0.15)
            stop.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(app.update_state.call_count, 1)
        logged = [a for a in app.add_log.call_args_list]
        self.assertTrue(any("exited" in str(a) for a in logged), logged)

    def test_poller_self_heals_revoked_token(self):
        # When the cloud rejects the token, the poller must not stop: it drops
        # the cached token (so `python3 spa.py login` is picked up without a
        # restart), alerts via Telegram once, and resumes on the next poll.
        import threading
        app = mock.MagicMock()
        app._get_tokens.side_effect = [{"t": 1}, {"t": 2}, {"t": 3}]
        app.cfg.mode = "manual"
        app.plug_enabled.return_value = False
        app.refresh_plan.side_effect = lambda **kw: None
        app._plug_next = 0.0
        app._notified_auth = False
        state = {"water_temperature": 39, "is_online": True}
        calls = {"n": 0}

        def flaky_shadow(tokens):
            calls["n"] += 1
            if calls["n"] == 1:
                raise spa.SpaError("thing_shadow failed: Token is not authorized")
            return state

        stop = threading.Event()
        with mock.patch.object(server.spa, "shadow", side_effect=flaky_shadow), \
             mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send, \
             mock.patch.object(server, "POLL_SECONDS", 0.02):
            t = threading.Thread(target=server._poller_loop, args=(app, stop), daemon=True)
            t.start()
            time.sleep(0.15)
            stop.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        app._invalidate_tokens.assert_called()
        app._auto_relogin.assert_called()
        send.assert_called_once()   # one-shot alert, not one per poll
        self.assertGreaterEqual(app.update_state.call_count, 1)

    def test_measured_cost_gated_until_history(self):
        self.app.cfg.scheduler_cfg["PRICE_PER_KWH"] = 0.2263
        self.app._plan = {"energy_alwayson_kwh": 100.0, "plan_ts": [0] * (24 * 7)}
        # no history -> not enough
        self.app._plug_hist = []
        p = self.app._measured_cost_payload()
        self.assertIs(p["enough"], False)
        # a couple of hours is still too little for a 7-day figure
        now = int(time.time())
        self.app._plug_hist = [
            {"ts": now - 3600 * 3, "watts": 60.0, "on": True, "fault": 0},
            {"ts": now, "watts": 60.0, "on": True, "fault": 0},
        ]
        p2 = self.app._measured_cost_payload()
        self.assertIs(p2["enough"], False)
        # a full day of metered draw -> measured monthly + measured saving
        self.app._plug_hist = []
        start = now - 86400 - 300
        for ts in range(start, now, 300):
            self.app._plug_hist.append(
                {"ts": ts, "watts": 60.0, "on": True, "fault": 0})
        p3 = self.app._measured_cost_payload()
        self.assertTrue(p3["enough"])
        self.assertGreater(p3["coverage_h"], 20.0)
        self.assertIn("monthly_cost", p3)
        self.assertIn("saving_monthly", p3)
        self.assertIn("saving_pct", p3)
        self.assertGreater(p3["saving_pct"], 0.0)   # 60 W draw ~= 90% vs always-on

    def test_measured_cost_hidden_without_price(self):
        self.app.cfg.scheduler_cfg["PRICE_PER_KWH"] = 0.0
        self.assertIs(self.app._measured_cost_payload()["enough"], False)

    def test_auto_relogin_refreshes_tokens_and_notifies(self):
        with mock.patch.object(server.spa, "get_tokens",
                               return_value={"fresh": 1}) as gt, \
             mock.patch.object(server.notify, "configured", return_value=True), \
             mock.patch.object(server.notify, "send_text",
                               return_value=(True, "")) as send:
            ok = self.app._auto_relogin()
        self.assertTrue(ok)
        gt.assert_called_once_with(force=True)
        self.assertEqual(self.app._tokens, {"fresh": 1})
        send.assert_called_once()

    def test_auto_relogin_throttled(self):
        self.app._last_relogin = time.time()
        with mock.patch.object(server.spa, "get_tokens") as gt:
            self.assertFalse(self.app._auto_relogin())
        gt.assert_not_called()

    def test_auto_relogin_failure_is_harmless(self):
        with mock.patch.object(server.spa, "get_tokens",
                               side_effect=spa.SpaError("Login failed: nope")) as gt:
            self.assertFalse(self.app._auto_relogin())
        gt.assert_called_once_with(force=True)
        self.assertIsNone(self.app._tokens)
        msgs = [e["msg"] for e in self.app.get_log(50)]
        self.assertTrue(any("Auto re-login failed" in m for m in msgs), msgs)

    def test_auto_relogin_backs_off_when_no_binding(self):
        # Login succeeds but Bestway returns no spa (tub offline on their side,
        # e.g. wifi change): the server must log the real reason and extend its
        # own cooldown so it stops rotating the account session mid-fix.
        with mock.patch.object(server.spa, "get_tokens",
                               side_effect=spa.SpaError(
                                   "Login OK but no spa binding returned "
                                   "(mxchip_device=None) — the spa looks offline "
                                   "… wifi?")):
            self.assertFalse(self.app._auto_relogin())
        self.assertIsNone(self.app._tokens)
        msgs = [e["msg"] for e in self.app.get_log(50)]
        self.assertTrue(any("no spa bound" in m for m in msgs), msgs)
        # next attempt is gated by the long backoff, not the short cooldown
        self.assertGreater(server.REL_LOGIN_BACKOFF_S, server.REL_LOGIN_COOLDOWN_S)
        with mock.patch.object(server.spa, "get_tokens") as gt:
            with mock.patch.object(server.time, "time",
                                   return_value=self.app._last_relogin
                                   + server.REL_LOGIN_COOLDOWN_S - 1):
                self.assertFalse(self.app._auto_relogin())
            gt.assert_not_called()

    def test_tg_command_unknown_returns_none(self):
        self.assertIsNone(self.app._tg_command("/bogus"))

    def test_history_window_filtering(self):
        now = int(time.time())
        with self.app._lock:
            # k=0 is now; k>=1 is strictly more than k*3600 seconds ago
            self.app._history = [{"ts": now if k == 0 else now - (k * 3600 + 1),
                                  "water": 30 + k, "target": 40, "heater": 0,
                                  "power": 1, "ambient": None}
                                 for k in range(10)]
        self.assertEqual(len(self.app.get_history(1)), 1)
        self.assertEqual(len(self.app.get_history(3)), 3)

    def test_temp_command_clamped_and_rejected_in_auto(self):
        with mock.patch.object(server.spa, "send_command") as sc, \
             mock.patch.object(server.spa, "wait_for", return_value=True), \
             mock.patch.object(self.app, "_get_tokens", return_value={"t": 1}):
            # manual: valid
            res = self.app.send_command("temperature_setting", 38)
            sc.assert_called_once()
            self.assertTrue(res["confirmed"])
            # out of range
            with self.assertRaises(ValueError):
                self.app.send_command("temperature_setting", 45)
            # auto mode rejects temp writes
            self.app.cfg.mode = "auto"
            with self.assertRaises(ValueError):
                self.app.send_command("temperature_setting", 38)
            self.app.cfg.mode = "manual"

    def test_toggle_command_validation(self):
        with mock.patch.object(server.spa, "send_command") as sc, \
             mock.patch.object(server.spa, "wait_for", return_value=True), \
             mock.patch.object(self.app, "_get_tokens", return_value={"t": 1}):
            self.app.send_command("wave_state", 1)
            sc.assert_called_once()
            with self.assertRaises(ValueError):
                self.app.send_command("wave_state", 2)
            with self.assertRaises(ValueError):
                self.app.send_command("nonsense", 1)

    def test_alias_mapping(self):
        with mock.patch.object(server.spa, "send_command") as sc, \
             mock.patch.object(server.spa, "wait_for", return_value=True), \
             mock.patch.object(self.app, "_get_tokens", return_value={"t": 1}):
            self.app.send_command("heat", 1)
            args = sc.call_args[0]
            self.assertEqual(args[1], "heater_state")


class TestSchedulerConfigUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "scheduler.env"), "w") as f:
            f.write("LOCATION=London, UK\nWINDOW=17:00-23:00\n"
                    "TARGET_TEMP=40\nECO_TEMP=28\nFLOOR_TEMP=8\n")
        self.orig_server_base = server.BASE_DIR
        self.orig_sched_base = server.scheduler.BASE_DIR
        server.BASE_DIR = self.tmp.name
        server.scheduler.BASE_DIR = self.tmp.name
        self.addCleanup(setattr, server, "BASE_DIR", self.orig_server_base)
        self.addCleanup(setattr, server.scheduler, "BASE_DIR", self.orig_sched_base)

        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        self.app = _spa_app(cfg)
        self.app.refresh_plan = mock.MagicMock(return_value={"changed": False})

    def _env(self):
        return server.scheduler.load_env(os.path.join(self.tmp.name, "scheduler.env"))

    def test_update_window_temps_and_override(self):
        out = self.app.update_scheduler_config({
            "window": "14:00-23:30", "target": 39, "eco": 26,
            "floor": 7, "margin": 0.5,
            "overrides": {"SAT": "10:00-23:00", "SUN": ""},
        })
        self.assertEqual(out["windows"], "14:00-23:30")
        self.assertEqual(out["target"], 39)
        self.assertEqual(out["eco"], 26)
        self.assertEqual(out["floor"], 7)
        self.assertEqual(out["margin"], 0.5)
        self.assertEqual(out["overrides"]["SAT"], "10:00-23:00")
        self.assertEqual(out["overrides"]["SUN"], "")
        env = self._env()
        self.assertEqual(env["WINDOW"], "14:00-23:30")
        self.assertEqual(env["WINDOW_SAT"], "10:00-23:00")
        self.assertNotIn("WINDOW_SUN", env)
        self.app.refresh_plan.assert_called_once_with(apply=False, force_weather=True)

    def test_accepts_overnight_window(self):
        out = self.app.update_scheduler_config({"window": "23:00-17:00"})
        self.assertEqual(out["windows"], "23:00-17:00")
        self.assertEqual(self._env()["WINDOW"], "23:00-17:00")
        out = self.app.update_scheduler_config({"window": "17:00-00:00"})
        self.assertEqual(out["windows"], "17:00-00:00")

    def test_rejects_bad_window_and_temp_order(self):
        with self.assertRaises(ValueError):
            self.app.update_scheduler_config({"window": "nonsense"})
        with self.assertRaises(ValueError):
            self.app.update_scheduler_config({"window": "00:00-00:00"})
        with self.assertRaises(ValueError):
            self.app.update_scheduler_config({"target": 20, "eco": 28})
        with self.assertRaises(ValueError):
            self.app.update_scheduler_config({"target": 45})

    def test_update_location_accepts_pinned_coords(self):
        out = self.app.update_scheduler_config({
            "location": "Rome, Lazio, Italy", "lat": 41.9, "lon": 12.5})
        env = self._env()
        self.assertEqual(env["LOCATION"], "Rome, Lazio, Italy")
        self.assertAlmostEqual(float(env["LAT"]), 41.9)
        self.assertAlmostEqual(float(env["LON"]), 12.5)
        self.assertAlmostEqual(self.app._lat, 41.9)
        self.assertAlmostEqual(self.app._lon, 12.5)
        # ambient + coast-down caches dropped after a move
        self.assertEqual(self.app._amb, {})
        self.assertIsNone(self.app._ua_prev)

    def test_update_location_geocodes_free_text(self):
        hit = {"label": "Paris, Île-de-France, France", "name": "Paris",
               "admin1": "Île-de-France", "country": "France",
               "country_code": "FR", "lat": 48.85, "lon": 2.35}
        with mock.patch.object(server.scheduler, "geocode",
                               return_value=[hit]):
            out = self.app.update_scheduler_config({"location": "paris"})
        env = self._env()
        self.assertEqual(env["LOCATION"], "Paris, Île-de-France, France")
        self.assertAlmostEqual(float(env["LAT"]), 48.85)
        self.assertAlmostEqual(float(env["LON"]), 2.35)
        self.assertAlmostEqual(self.app._lat, 48.85)

    def test_update_location_rejects_unknown(self):
        with mock.patch.object(server.scheduler, "geocode", return_value=[]):
            with self.assertRaises(ValueError):
                self.app.update_scheduler_config({"location": "atlantis-xyz"})

    def test_update_location_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.app.update_scheduler_config({"location": "   "})

    def test_model_info_reports_source_and_samples(self):
        cfg = _scheduler_cfg()
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            pass
        cfg["UA_KW_PER_C"] = 0.025
        # no model file -> defaults
        info = server.scheduler.model_info(cfg)
        self.assertEqual(info["source"], "default")
        self.assertEqual(info["samples"], 0)
        # with a fitted model file
        with open(os.path.join(self.tmp.name, "scheduler_model.json"), "w") as f:
            json.dump({"UA_KW_PER_C": 0.025, "ua_samples": 7, "source": "online coast-down",
                       "saved_at": 123456}, f)
        info2 = server.scheduler.model_info(cfg)
        self.assertEqual(info2["ua"], 0.025)
        self.assertEqual(info2["samples"], 7)
        self.assertEqual(info2["source"], "online coast-down")
        self.assertEqual(info2["saved_at"], 123456)

    def test_cost_overview_computes_saving_and_monthly(self):
        cfg = _scheduler_cfg()
        cfg["PRICE_PER_KWH"] = 0.2263
        plan = {"energy_kwh": 4.0, "energy_alwayson_kwh": 10.0,
                "plan_ts": [0] * (24 * 7)}   # 7-day horizon
        c = server.cost_overview(cfg, plan)
        self.assertAlmostEqual(c["cost_plan"], 0.2263 * 4.0)
        self.assertAlmostEqual(c["cost_alwayson"], 0.2263 * 10.0)
        self.assertAlmostEqual(c["saving_cost"], 0.2263 * 6.0)
        self.assertAlmostEqual(c["saving_pct"], 60.0, places=6)
        self.assertAlmostEqual(c["days"], 7.0)
        self.assertAlmostEqual(c["cost_monthly"], 0.2263 * 4.0 * 30 / 7, places=6)
        self.assertAlmostEqual(c["saving_monthly"], 0.2263 * 6.0 * 30 / 7, places=6)

    def test_cost_overview_stale_flag(self):
        cfg = _scheduler_cfg()
        cfg["PRICE_PER_KWH"] = 0.2263
        plan = {"energy_kwh": 4.0, "energy_alwayson_kwh": 10.0,
                "plan_ts": [0] * (24 * 7)}
        fresh = server.cost_overview(cfg, plan, stale=False)
        stale = server.cost_overview(cfg, plan, stale=True)
        self.assertIs(fresh["stale"], False)
        self.assertIs(stale["stale"], True)
        # no plan / uncomputable -> stale-by-default estimate shape
        empty = server.cost_overview(cfg, None, stale=False)
        self.assertIs(empty["stale"], True)
        self.assertNotIn("cost_plan", empty)
        self.assertNotIn("cost_monthly", empty)

    def test_update_config_accepts_price(self):
        out = self.app.update_scheduler_config({"price": 0.312})
        self.assertAlmostEqual(out["price_per_kwh"], 0.312)
        self.assertAlmostEqual(float(self._env()["PRICE_PER_KWH"]), 0.312)

    def test_payload_includes_location_and_model(self):
        out = self.app.update_scheduler_config({"window": "17:00-23:00"})
        self.assertIn("location", out)
        self.assertIn("lat", out)
        self.assertIn("lon", out)
        self.assertIn("model", out)
        self.assertIn("ua", out["model"])
        self.assertAlmostEqual(out["model"]["ua"], 0.03)

    def test_operation_log_keeps_newest(self):
        self.app.add_log("ok", "one")
        self.app.add_log("err", "two")
        entries = self.app.get_log(1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[-1]["msg"], "two")
        self.assertEqual(entries[-1]["level"], "err")

    def test_operation_log_persists_across_restart(self):
        self.app.add_log("ok", "first boot worked fine")
        path = os.path.join(self.tmp.name, "operation_log.jsonl")
        self.assertTrue(os.path.exists(path))
        # simulate a server restart: a brand-new SpaServer over the same dir
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            app2 = server.SpaServer(cfg)
        entries = app2.get_log(200)
        msgs = [e["msg"] for e in entries]
        self.assertTrue(any("first boot worked fine" in m for m in msgs),
                        "history lost after restart: %s" % msgs)

    def test_log_endpoint_survives_restart_via_file(self):
        # the durable file is what /api/log reads; poll the file directly
        path = os.path.join(self.tmp.name, "operation_log.jsonl")
        self.app.add_log("warn", "unexpected temperature spike")
        with open(path) as f:
            raw = f.read()
        self.assertIn("unexpected temperature spike", raw)

class TestAPIEndpoints(unittest.TestCase):
    """Spin a real ThreadingHTTPServer with a mocked SpaServer and hit it."""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        # build a real app with mocked spa/weather side effects
        app = mock.MagicMock()
        app.cfg.mode = "manual"
        app.cfg.scheduler_cfg = _scheduler_cfg()
        app._plan = {"phase": "eco", "setting": 28, "energy_kwh": 12.5,
                     "ambient_now": 8.0, "plan_ts": [int(time.time())],
                     "plan_amb": [8.0], "plan_us": [0]}
        app._plan_ts = time.time()
        app.get_state.return_value = (_shadow(), time.time())
        app.tub_online.return_value = True
        app.state_age_s.return_value = 0
        app._plan_stale.return_value = False
        app.plug_enabled.return_value = False
        app._measured_cost_payload.return_value = {"enough": False, "coverage_h": 0.0}
        app.get_history.return_value = [{"ts": int(time.time()) - i * 3600,
                                         "water": 39 - i, "target": 40,
                                         "heater": 1 if i < 2 else 0,
                                         "power": 1, "ambient": 8.0}
                                        for i in range(3)]
        app.get_plugs.return_value = ({}, 0.0)
        app.plug_kwh_window.return_value = 0.0
        app.get_plug_history.return_value = []
        app._reg_offsets = {}
        app.send_command.side_effect = lambda dp, val: {"datapoint": dp, "value": val,
                                                        "confirmed": True, "response": {}}
        app.refresh_plan.return_value = app._plan
        app._lat, app._lon = 51.5, -0.12
        cls.app_mock = app

        server_mod = sys.modules["server"]
        # keep POST /api/scheduler mode changes away from the real web.env
        cls.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.tmp.name, "web.env"), "w") as f:
            f.write("BIND=0.0.0.0\nPORT=8080\nMODE=manual\n")
        cls.orig_base = server_mod.BASE_DIR
        server_mod.BASE_DIR = cls.tmp.name
        cls.orig_cfg = server_mod.WebConfig
        server_mod.WebConfig = lambda: mock.MagicMock(bind="127.0.0.1", port=0,
                                                      mode="manual",
                                                      scheduler_cfg=_scheduler_cfg())

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod._Handler)
        cls.httpd.app = app  # type: ignore[attr-defined]
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        # reset shared mock state between tests
        self.app_mock.cfg.mode = "manual"
        self.app_mock.send_command.reset_mock(side_effect=True)
        self.app_mock.send_command.side_effect = lambda dp, val: {"datapoint": dp,
                                                                  "value": val,
                                                                  "confirmed": True,
                                                                  "response": {}}
        self.app_mock.refresh_plan.reset_mock(side_effect=True)
        self.app_mock.refresh_plan.return_value = self.app_mock._plan

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        sys.modules["server"].WebConfig = cls.orig_cfg
        sys.modules["server"].BASE_DIR = cls.orig_base
        cls.tmp.cleanup()

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path),
                                    timeout=10) as r:
            return json.loads(r.read())

    def test_state_endpoint(self):
        d = self._get("/api/state")
        self.assertEqual(d["mode"], "manual")
        self.assertEqual(d["state"]["water_temperature"], 39)
        self.assertEqual(d["phase"], "eco")

    def test_history_endpoint(self):
        d = self._get("/api/history?range=24")
        self.assertEqual(len(d["series"]), 3)

    def test_plan_endpoint(self):
        d = self._get("/api/plan")
        self.assertEqual(d["phase"], "eco")
        self.assertEqual(d["setting"], 28)

    def test_scheduler_endpoint(self):
        d = self._get("/api/scheduler")
        self.assertEqual(d["mode"], "manual")

    def test_health_endpoint(self):
        d = self._get("/api/health")
        self.assertTrue(d["online"])

    def test_static_index(self):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port, timeout=10) as r:
            body = r.read().decode()
        self.assertIn("Torronto", body)
        self.assertIn("static/app.js", body)

    def test_cmd_endpoint(self):
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/cmd" % self.port,
            data=json.dumps({"datapoint": "wave_state", "value": 1}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        self.assertTrue(d["confirmed"])

    def test_scheduler_mode_post(self):
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/scheduler" % self.port,
            data=json.dumps({"mode": "auto"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        # handler sets app.cfg.mode then echoes it
        self.assertEqual(d["mode"], "auto")
        self.assertEqual(self.app_mock.cfg.mode, "auto")
        # and it persisted to the (temporary) web.env
        env = server.scheduler.load_env(os.path.join(self.tmp.name, "web.env"))
        self.assertEqual(env.get("MODE"), "auto")
        self.app_mock.cfg.mode = "manual"

    def test_unknown_route_404(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_location_search_endpoint(self):
        import urllib.request
        hit = {"label": "London, England, United Kingdom", "name": "London",
               "admin1": "England", "country": "United Kingdom",
               "country_code": "GB", "lat": 51.5, "lon": -0.1}
        with mock.patch.object(server.scheduler, "geocode",
                               return_value=[hit]) as g:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/location/search?q=Lon" % self.port)
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
        g.assert_called_once_with("Lon", 8)
        self.assertEqual(d["results"][0]["label"], "London, England, United Kingdom")
        self.assertAlmostEqual(d["results"][0]["lat"], 51.5)

    def test_weather_endpoint_multi_day(self):
        import urllib.request
        hour0 = int(time.time()) - (int(time.time()) % 3600)
        payload = {
            "days": 7,
            "current": {"temp": 4.0, "feels": 2.0, "humidity": 80, "label": "Clear",
                        "emoji": "☀️", "dew": 0.0, "wind": 1.0},
            "hourly": [{"ts": hour0 + i * 3600, "temp": 4.0 + (i % 6), "feels": 2.0,
                        "precipProb": (i % 4) * 25, "code": 0, "label": "Clear", "emoji": "☀️"}
                       for i in range(7 * 24)],
            "daily": [{"date": hour0 + i * 86400, "hi": 8.0, "lo": 2.0,
                       "precipProb": 20, "label": "Clear", "emoji": "☀️"}
                      for i in range(7)],
        }
        with mock.patch.object(server.scheduler, "fetch_weather_detail",
                               return_value=payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/weather?days=7" % self.port)
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
        self.assertEqual(d["days"], 7)
        self.assertGreaterEqual(len(d["hourly"]), 7 * 24 - 1)
        self.assertEqual(d["current"]["temp"], 4.0)
        self.assertGreaterEqual(len(d["daily"]), 7)


if __name__ == "__main__":
    unittest.main()
