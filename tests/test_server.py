"""Unit tests for server.py — config, state/history, commands, mode switch.

The spa cloud and weather APIs are mocked; no network and no real device.
"""
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


class FakeState(dict):
    pass


def _shadow():
    return {
        "water_temperature": 39, "temperature_setting": 40, "temperature_unit": 1,
        "power_state": 1, "heater_state": 6, "filter_state": 2,
        "wave_state": 0, "hydrojet_state": 0, "warning": "1",
        "error_code": "", "ConnectType": "online", "wifivertion": 121,
    }


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
        cfg = mock.MagicMock()
        cfg.mode = "manual"
        cfg.scheduler_cfg = _scheduler_cfg()
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            app = server.SpaServer(cfg)
        app.update_state({**_shadow(), "water_temperature": 40,
                          "temperature_setting": 28, "heater_state": 4})
        # exercise the payload builder directly
        state, _ = app.get_state()
        self.assertEqual(server.heater_status(state), "cooling")


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
        with mock.patch.object(server.scheduler, "load_env", return_value={}):
            self.app = server.SpaServer(cfg)

    def test_update_state_appends_history(self):
        self.app.update_state(_shadow())
        hist = self.app.get_history(24)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["water"], 39)
        self.assertIsNone(hist[0]["ambient"])

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
        base = 1_800_000_000            # aligned to the hour
        self.app._amb = {base + h * 3600: 5.0 for h in range(8)}
        self.app.cfg.scheduler_cfg["UA_KW_PER_C"] = 0.02
        clock = {"now": base}
        states = [(38.6, 28), (37.2, 28), (35.8, 28)]
        with mock.patch.object(server.time, "time", lambda: clock["now"]):
            self.app._maybe_sample_ua({
                "water_temperature": 40, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})       # anchor at +0h
            for i, (water, setpoint) in enumerate(states, start=1):
                clock["now"] = base + i * 7200
                self.app._maybe_sample_ua({
                    "water_temperature": water, "temperature_setting": setpoint,
                    "power_state": 1, "heater_state": 0})
        self.assertEqual(len(self.app._ua_samples), 3)
        self.assertAlmostEqual(self.app.cfg.scheduler_cfg["UA_KW_PER_C"],
                               0.0308, delta=0.002)
        path = os.path.join(self.tmp.name, "scheduler_model.json")
        with open(path) as f:
            model = json.load(f)
        self.assertEqual(model["source"], "online coast-down")
        self.assertEqual(model["ua_samples"], 3)
        self.assertAlmostEqual(model["UA_KW_PER_C"], 0.0308, delta=0.002)

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
            clock["now"] = base + 7200
            self.app._amb = {}                           # cache gone after replan
            self.app._maybe_sample_ua({
                "water_temperature": 36.6, "temperature_setting": 28,
                "power_state": 1, "heater_state": 0})    # cooled ~1.4C in 2h
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
        self.app = server.SpaServer(cfg)
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
        self.app.refresh_plan.assert_called_once_with(apply=False)

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
        app.get_history.return_value = [{"ts": int(time.time()) - i * 3600,
                                         "water": 39 - i, "target": 40,
                                         "heater": 1 if i < 2 else 0,
                                         "power": 1, "ambient": 8.0}
                                        for i in range(3)]
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
