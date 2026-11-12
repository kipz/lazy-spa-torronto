"""Unit tests for scheduler.py — config, windows, weather, thermal model,
optimal-start policy, calibration, and controller phase logic."""
import datetime as dt
import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")
from tests.helpers import ANCHOR_TS, make_cfg, make_series, ts_at  # noqa: E402
import scheduler  # noqa: E402


# --- config ------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_defaults_loaded(self):
        cfg = make_cfg()
        self.assertEqual(cfg["TARGET_TEMP"], 40)
        self.assertEqual(cfg["ECO_TEMP"], 28)
        self.assertEqual(cfg["WINDOW"], "17:00-23:00")

    def test_load_env_parsing(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\n\nKEY=value\nNUM=42\nEMPTY=\nSPACED = x \n")
            path = f.name
        try:
            env = scheduler.load_env(path)
        finally:
            os.unlink(path)
        self.assertEqual(env["KEY"], "value")
        self.assertEqual(env["NUM"], "42")
        self.assertEqual(env["SPACED"], "x")
        self.assertEqual(env["EMPTY"], "")  # present, empty value
        self.assertNotIn("comment", env)

    def test_load_config_type_coercion(self):
        cfg = make_cfg(TARGET_TEMP="39", P_HEAT_KW="2.5", HORIZON_H="24")
        self.assertEqual(cfg["TARGET_TEMP"], 39.0)
        self.assertEqual(cfg["P_HEAT_KW"], 2.5)
        self.assertEqual(cfg["HORIZON_H"], 24)


# --- windows -----------------------------------------------------------------
class TestWindows(unittest.TestCase):
    def test_parse_window_spec(self):
        self.assertEqual(scheduler.parse_window_spec("17:00-23:00"), ((17, 0), (23, 0)))
        self.assertEqual(scheduler.parse_window_spec(" 08:30-09:45 "), ((8, 30), (9, 45)))

    def test_windows_from_config_per_day_override(self):
        cfg = make_cfg(WINDOW="17:00-23:00", WINDOW_SAT="14:00-23:00")
        days = scheduler.windows_from_config(cfg)
        self.assertEqual(days[5], [((14, 0), (23, 0))])  # Saturday
        self.assertEqual(days[0], [((17, 0), (23, 0))])  # Monday

    def test_in_window(self):
        days = {0: [((17, 0), (23, 0))]}  # Monday 17:00-23:00 only
        self.assertTrue(scheduler.in_window(ts_at(17, 0), days))
        self.assertTrue(scheduler.in_window(ts_at(22, 59), days))
        self.assertFalse(scheduler.in_window(ts_at(23, 0), days))  # end is exclusive
        self.assertFalse(scheduler.in_window(ts_at(16, 59), days))
        self.assertFalse(scheduler.in_window(ts_at(12, 0), days))  # Tuesday


# --- weather -----------------------------------------------------------------
class TestWeather(unittest.TestCase):
    def test_forecast_series_slices_and_fills(self):
        cfg = make_cfg(HORIZON_H=4)
        weather = {ts_at(0): (5.0, 1.0, 0.0), ts_at(1): (None, 2.0, 0.0)}
        series = scheduler.forecast_series(cfg, weather, ts_at(0))
        self.assertEqual(len(series), 4)
        self.assertEqual(series[0], (ts_at(0), 5.0))
        # missing temp falls back to the previous hour's value
        self.assertEqual(series[1], (ts_at(1), 5.0))
        # entirely missing hours propagate the last known value
        self.assertEqual(series[2], (ts_at(2), 5.0))

    def test_forecast_series_eco_fallback_when_no_history(self):
        cfg = make_cfg(HORIZON_H=2)
        weather = {}
        series = scheduler.forecast_series(cfg, weather, ts_at(0))
        self.assertEqual(series[0], (ts_at(0), cfg["ECO_TEMP"]))

    def test_fetch_forecast_parses_hourly(self):
        payload = {
            "hourly": {
                "time": [ts_at(0), ts_at(0) + 3600, ts_at(0) + 7200],
                "temperature_2m": [5.0, None, 7.0],
                "wind_speed_10m": [1.0, 2.0, 3.0],
                "shortwave_radiation": [0.0, 0.0, 100.0],
            }
        }
        with mock.patch.object(scheduler, "http_get_json", return_value=payload):
            out = scheduler.fetch_forecast(51.5, -0.1)
        self.assertEqual(out[ts_at(0)], (5.0, 1.0, 0.0))
        self.assertEqual(out[ts_at(0) + 3600], (None, 2.0, 0.0))
        self.assertEqual(out[ts_at(0) + 7200], (7.0, 3.0, 100.0))


class TestGeocoding(unittest.TestCase):
    def test_resolve_location_uses_latlon_directly(self):
        cfg = make_cfg(LAT=51.5, LON=-0.12)
        with mock.patch.object(scheduler, "http_get_json") as g:
            lat, lon, where = scheduler.resolve_location(cfg)
        g.assert_not_called()
        self.assertEqual((lat, lon), (51.5, -0.12))

    def test_resolve_location_geocodes(self):
        cfg = make_cfg(LOCATION="London, UK")
        hit = {"latitude": 51.5, "longitude": -0.12, "name": "London", "country_code": "GB"}
        with mock.patch.object(scheduler, "http_get_json", return_value={"results": [hit]}):
            lat, lon, where = scheduler.resolve_location(cfg)
        self.assertEqual((lat, lon), (51.5, -0.12))
        self.assertIn("London", where)

    def test_resolve_location_retries_without_qualifier_then_exits(self):
        cfg = make_cfg(LOCATION="Bogusville, UK")
        with mock.patch.object(scheduler, "http_get_json", return_value={"results": []}):
            with self.assertRaises(SystemExit):
                scheduler.resolve_location(cfg)


# --- thermal model -----------------------------------------------------------
class TestThermalModel(unittest.TestCase):
    def test_make_ua_wind_sensitivity(self):
        cfg = make_cfg(UA_KW_PER_C=0.03, WIND_SENSITIVITY=0.1)
        self.assertAlmostEqual(scheduler.make_ua(cfg, wind_ms=10), 0.03 * 2.0)
        cfg2 = make_cfg(UA_KW_PER_C=0.03, WIND_SENSITIVITY=0.0)
        self.assertAlmostEqual(scheduler.make_ua(cfg2, wind_ms=50), 0.03)

    def test_coast_cools_toward_ambient(self):
        cfg = make_cfg(C_KWH_PER_C=1.45, UA_KW_PER_C=0.03)
        series = make_series(0, hours=2, t_amb=10.0)
        temps, us, energy = scheduler.simulate(cfg, series, T0=40.0, windows={},
                                               u_schedule=[0, 0])
        # 1h at 10C ambient, UA=0.03, C=1.45: dT = -0.03*(40-10)/1.45 = -0.62
        self.assertLess(temps[1], 40.0)
        self.assertGreater(temps[1], 38.0)
        self.assertEqual(energy, 0.0)

    def test_heating_rises_and_energy_accounts(self):
        cfg = make_cfg(C_KWH_PER_C=1.45, P_HEAT_KW=2.0, UA_KW_PER_C=0.03)
        series = make_series(0, hours=1, t_amb=10.0)
        temps, _, energy = scheduler.simulate(cfg, series, T0=20.0, windows={},
                                              u_schedule=[1])
        # dT = (2 - 0.03*(20-10))/1.45 = 1.17 C
        self.assertAlmostEqual(temps[1], 21.17, places=1)
        self.assertAlmostEqual(energy, 2.0, places=6)

    def test_clamp_at_40(self):
        cfg = make_cfg(P_HEAT_KW=2.0, UA_KW_PER_C=0.03)
        series = make_series(0, hours=5, t_amb=30.0)
        temps, _, _ = scheduler.simulate(cfg, series, T0=39.5, windows={},
                                         u_schedule=[1, 1, 1, 1, 1])
        self.assertLessEqual(max(temps), 40.0 + 1e-9)

    def test_holds_during_window_without_schedule(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = {0: [((17, 0), (23, 0))]}
        series = make_series(0, hours=24, t_amb=15.0)
        temps, us, _ = scheduler.simulate(cfg, series, T0=40.0, windows=days)
        # during the window (17:00-23:00 on Monday) the heater should be on
        for i, (ts, _) in enumerate(series):
            if scheduler.in_window(ts, days):
                self.assertEqual(us[i], 1)
                # greedy simulate only preheats as needed; once inside the
                # window it must be driving toward (and clamped at) 40
                self.assertGreaterEqual(temps[i + 1], cfg["ECO_TEMP"] - 1.0)
                self.assertLessEqual(temps[i + 1], 40.0 + 1e-6)


# --- optimal start -----------------------------------------------------------
class TestOptimalStart(unittest.TestCase):
    def test_no_unnecessary_heating_when_always_warm(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        series = make_series(0, hours=48, t_amb=30.0)  # warmer than eco
        us, energy, _ = scheduler.optimal_start(cfg, series, T0=30.0, windows={})
        self.assertEqual(sum(us), 0)
        self.assertEqual(energy, 0.0)

    def test_policy_bang_bang(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = make_series(12, hours=48, t_amb=5.0)
        us, energy, _ = scheduler.optimal_start(cfg, series, T0=28.0, windows=days)
        self.assertTrue(all(u in (0, 1) for u in us))
        self.assertGreaterEqual(energy, 0.0)

    def test_reaches_target_by_window(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)  # 17:00-23:00 every day
        # start at 00:00 so the preheat has a feasible lead time
        series = make_series(0, hours=48, t_amb=5.0)
        T0 = 28.0
        us, _, _ = scheduler.optimal_start(cfg, series, T0, windows=days)
        temps, _, _ = scheduler.simulate(cfg, series, T0, windows=days, u_schedule=us)
        for i, (ts, _) in enumerate(series):
            if scheduler.in_window(ts, days):
                self.assertGreaterEqual(
                    temps[i + 1], cfg["TARGET_TEMP"] - 0.5,
                    msg="failed at hour %d" % i)

    def test_never_exceeds_clamp(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = make_series(0, hours=48, t_amb=-5.0)
        T0 = 28.0
        us, _, _ = scheduler.optimal_start(cfg, series, T0, windows=days)
        temps, _, _ = scheduler.simulate(cfg, series, T0, windows=days, u_schedule=us)
        self.assertLessEqual(max(temps), 40.0 + 1e-6)

    def test_coasts_when_above_eco_and_no_window(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        series = make_series(0, hours=24, t_amb=20.0)
        us, energy, _ = scheduler.optimal_start(cfg, series, T0=35.0, windows={})
        # 35C > eco, ambient 20C: coasting never triggers heating
        self.assertEqual(sum(us), 0)


# --- calibration -------------------------------------------------------------
def _row(ts, water, heater_state, power=1):
    return {"ts": ts, "shadow": {
        "water_temperature": water, "heater_state": heater_state, "power_state": power}}


class TestCalibrate(unittest.TestCase):
    def test_fits_ua_from_coastdown(self):
        cfg = make_cfg(C_KWH_PER_C=1.45)
        ua_true, t_amb = 0.05, 10.0
        rows, ts = [], ts_at(0)
        T = 38.0
        for _ in range(8):
            rows.append(_row(ts, T, heater_state=0, power=0))
            T -= ua_true * (T - t_amb) / cfg["C_KWH_PER_C"]
            ts += 3600
        weather = {ts_at(0) + i * 3600: (t_amb, 0, 0) for i in range(8)}
        ua, p, nu, np_ = scheduler.calibrate_model(cfg, rows, weather)
        self.assertGreaterEqual(nu, 3)
        self.assertAlmostEqual(ua, ua_true, delta=0.01)

    def test_fits_power_from_heating(self):
        cfg = make_cfg(C_KWH_PER_C=1.45, UA_KW_PER_C=0.03)
        p_true, t_amb = 2.0, 10.0
        rows, ts = [], ts_at(0)
        T = 25.0
        for _ in range(8):
            rows.append(_row(ts, T, heater_state=1, power=1))
            T += (p_true - cfg["UA_KW_PER_C"] * (T - t_amb)) / cfg["C_KWH_PER_C"]
            ts += 3600
        weather = {ts_at(0) + i * 3600: (t_amb, 0, 0) for i in range(8)}
        _, p, nu, np_ = scheduler.calibrate_model(cfg, rows, weather)
        self.assertGreaterEqual(np_, 3)
        self.assertAlmostEqual(p, p_true, delta=0.1)

    def test_falls_back_when_too_few_samples(self):
        cfg = make_cfg(UA_KW_PER_C=0.03, P_HEAT_KW=2.0)
        rows = [_row(ts_at(0), 30, 0, 0), _row(ts_at(1), 29.9, 0, 0)]
        weather = {ts_at(0): (10.0, 0, 0), ts_at(1): (10.0, 0, 0)}
        ua, p, nu, np_ = scheduler.calibrate_model(cfg, rows, weather)
        self.assertEqual(ua, 0.03)
        self.assertEqual(p, 2.0)
        self.assertLess(nu, 3)


# --- controller phase --------------------------------------------------------
class TestControllerPhase(unittest.TestCase):
    def _series_with(self, start_hour, hours=24):
        return make_series(start_hour, hours=hours, t_amb=5.0)

    def test_window_phase(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = self._series_with(18)  # inside 17:00-23:00
        us = [1] * len(series)
        self.assertEqual(scheduler.hot_phase_now(series, us, days), "window")

    def test_preheat_phase_continuous_into_window(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = self._series_with(14)  # 14:00 Monday
        # heater on continuously 14:00..17:00 (window starts 17:00)
        us = [1] * 3 + [0] * (len(series) - 3)
        self.assertEqual(scheduler.hot_phase_now(series, us, days), "preheat")

    def test_eco_phase_when_coasting(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = self._series_with(1)  # 01:00 Monday, no window
        us = [0] * len(series)
        self.assertEqual(scheduler.hot_phase_now(series, us, days), "eco")

    def test_eco_phase_when_heating_but_not_until_window(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        days = scheduler.windows_from_config(cfg)
        series = self._series_with(1)  # 01:00 Monday
        us = [1, 1, 0] + [0] * (len(series) - 3)  # short burst, not into window
        self.assertEqual(scheduler.hot_phase_now(series, us, days), "eco")


# --- formatting --------------------------------------------------------------
class TestFormatting(unittest.TestCase):
    def test_fmt_hm(self):
        out = scheduler.fmt_hm(ts_at(9, 30))
        self.assertIn("09:30", out)


# --- scheduler_step (shared unit used by CLI run + web server) ---------------
class TestSchedulerStep(unittest.TestCase):
    def _mock_weather(self):
        weather = {}
        for i in range(48):
            ts = ANCHOR_TS + i * 3600
            weather[ts - (ts % 3600)] = (5.0, 0.0, 0.0)
        return weather

    def test_step_returns_plan_without_applying(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        mock_spa = mock.MagicMock()
        mock_spa.shadow.return_value = {
            "water_temperature": 40, "power_state": 1,
            "temperature_setting": 40, "heater_state": 6, "filter_state": 2}
        with mock.patch.dict(sys.modules, {"spa": mock_spa}), \
             mock.patch.object(scheduler, "fetch_forecast",
                               return_value=self._mock_weather()):
            res = scheduler.scheduler_step(cfg, 51.5, -0.12, apply=False)
        self.assertIn("phase", res)
        self.assertIn("setting", res)
        self.assertEqual(res["water_temp"], 40)
        self.assertEqual(len(res["plan_ts"]), len(res["plan_us"]))
        self.assertFalse(res["changed"])  # apply=False -> no commands sent
        mock_spa.send_command.assert_not_called()

    def test_step_applies_in_auto(self):
        cfg = make_cfg(TARGET_TEMP=40, ECO_TEMP=28)
        mock_spa = mock.MagicMock()
        mock_spa.shadow.return_value = {
            "water_temperature": 40, "power_state": 0,
            "temperature_setting": 40, "heater_state": 6, "filter_state": 2}
        with mock.patch.dict(sys.modules, {"spa": mock_spa}), \
             mock.patch.object(scheduler, "fetch_forecast",
                               return_value=self._mock_weather()):
            res = scheduler.scheduler_step(cfg, 51.5, -0.12, apply=True)
        # water already at/above target in-window -> power_state forced on
        self.assertTrue(res["changed"])
        calls = [c[0] for c in mock_spa.send_command.call_args_list]
        self.assertIn(("power_state", 1), [(dp, v) for _, dp, v in calls])


if __name__ == "__main__":
    unittest.main()
