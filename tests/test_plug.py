"""Unit tests for plug.py — Tuya cloud smart-plug reader (stdlib + lazy sdk).

Covers the pure normalization/config logic. The tuya-device-sharing-sdk and
the live Tuya cloud are deliberately not touched here, so the suite stays fast
and hermetic (and passes on machines without the SDK installed).
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, ".")
import plug


def _ns(**kw):
    return SimpleNamespace(**kw)


def _sr(code, unit, scale):
    """A tuya_sharing.DeviceStatusRange stand-in."""
    return _ns(
        code=code, type="Integer",
        values=json.dumps({"unit": unit, "min": 0, "max": 99999,
                           "scale": scale, "step": 1}),
        report_type=None)


def _status_range_probe():
    return {
        "cur_power": _sr("cur_power", "W", 1),
        "cur_voltage": _sr("cur_voltage", "V", 1),
        "cur_current": _sr("cur_current", "mA", 0),
        "add_ele": _sr("add_ele", "kW\u00b7h", 3),
    }


def _device(status, online=True):
    return _ns(
        id="bf44bba069707d19bf5ysd", name="Tub mains", online=online,
        status=dict(status), status_range=_status_range_probe())


class TestSpecScale(unittest.TestCase):
    def test_scale_from_namespace_values(self):
        self.assertEqual(plug._spec_scale(_sr("cur_power", "W", 1)), 1)
        self.assertEqual(plug._spec_scale(_sr("x", "W", 2)), 2)

    def test_scale_from_dict(self):
        self.assertEqual(plug._spec_scale({"scale": 3}), 3)
        self.assertEqual(plug._spec_scale({"unit": "W"}), 0)

    def test_scale_missing(self):
        self.assertEqual(plug._spec_scale(None), 0)
        self.assertEqual(plug._spec_scale(_ns(values=None)), 0)
        self.assertEqual(plug._spec_scale(_ns()), 0)

    def test_unit_from_namespace(self):
        self.assertEqual(plug._spec_unit(_sr("cur_current", "mA", 0)), "mA")
        self.assertEqual(plug._spec_unit({"unit": "V"}), "V")


class TestScaleValue(unittest.TestCase):
    def test_divides_by_power_of_ten(self):
        self.assertEqual(plug._scale_value(124, _sr("cur_power", "W", 1)), 12.4)
        self.assertEqual(plug._scale_value(2398, _sr("v", "V", 1)), 239.8)

    def test_non_numeric(self):
        self.assertIsNone(plug._scale_value(None, _sr("x", "W", 1)))
        self.assertIsNone(plug._scale_value("no", None))


class TestNormalize(unittest.TestCase):
    def test_full_reading(self):
        dev = _device({
            "switch_1": True, "cur_power": 124, "cur_voltage": 2398,
            "cur_current": 107, "add_ele": 6, "fault": 0,
        })
        out = plug._normalize(dev, now=123)
        self.assertEqual(out["ts"], 123)
        self.assertTrue(out["switch_on"])
        self.assertTrue(out["online"])
        self.assertEqual(out["watts"], 12.4)      # cur_power scale 1
        self.assertEqual(out["voltage_v"], 239.8)  # cur_voltage scale 1
        self.assertEqual(out["current_a"], 0.107)  # mA -> A
        self.assertEqual(out["energy_kwh"], 0.006)  # add_ele scale 3

    def test_switch_off_as_int(self):
        out = plug._normalize(_device({"switch_1": 0}), now=0)
        self.assertFalse(out["switch_on"])

    def test_offline_device(self):
        out = plug._normalize(_device({}, online=False), now=0)
        self.assertFalse(out["online"])

    def test_missing_dp(self):
        out = plug._normalize(_device({}), now=0)
        self.assertIsNone(out["watts"])
        self.assertIsNone(out["voltage_v"])
        self.assertFalse(out["switch_on"])


class TestSessionConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, plug, "DEFAULT_SESSION", plug.DEFAULT_SESSION)
        self.addCleanup(setattr, plug, "_cfg", plug._cfg)
        plug.DEFAULT_SESSION = os.path.join(self.tmp.name, "tuya.session.json")

    def _write_session(self):
        path = plug.DEFAULT_SESSION
        with open(path, "w") as f:
            json.dump({
                "client_id": "cid", "user_code": "uc", "terminal_id": "tid",
                "endpoint": "https://example.com",
                "token_info": {"t": 1, "uid": "u", "expire_time": 7200,
                               "access_token": "a", "refresh_token": "r"},
            }, f)
        return path

    def test_configured_false_without_session(self):
        plug._cfg = lambda: {}
        self.assertFalse(plug.configured())

    def test_configured_true_with_session(self):
        plug._cfg = lambda: {}
        self._write_session()
        self.assertTrue(plug.configured())

    def test_read_returns_none_without_session(self):
        plug._cfg = lambda: {}
        self.assertIsNone(plug.read())

    def test_token_saver_persists_refresh(self):
        path = self._write_session()
        with open(path) as f:
            session = json.load(f)
        saver = plug._TokenSaver(path, session)
        saver.update_token({
            "accessToken": "newA", "refreshToken": "newR",
            "uid": "u", "expireTime": 3600, "t": 111,
        })
        with open(path) as f:
            saved = json.load(f)
        self.assertEqual(saved["token_info"]["access_token"], "newA")
        self.assertEqual(saved["token_info"]["refresh_token"], "newR")
        self.assertEqual(saved["token_info"]["expire_time"], 3600)


if __name__ == "__main__":
    unittest.main()