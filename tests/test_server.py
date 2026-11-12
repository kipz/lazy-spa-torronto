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

    def test_operation_log_keeps_newest(self):
        self.app.add_log("ok", "one")
        self.app.add_log("err", "two")
        entries = self.app.get_log(1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[-1]["msg"], "two")
        self.assertEqual(entries[-1]["level"], "err")


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


if __name__ == "__main__":
    unittest.main()
