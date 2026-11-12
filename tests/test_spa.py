"""Unit tests for spa.py — env parsing, HTTP helper (mocked), signing,
datapoint aliases, and status formatting. No network access."""
import hashlib
import io
import json
import sys
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, ".")
import spa  # noqa: E402


class _FakeResp:
    def __init__(self, body=b"{}", code=200):
        self._body = body
        self.code = code

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeFp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass


def _http_error(body, code):
    return urllib.error.HTTPError(
        url="https://example.test/x", code=code, msg="Error", hdrs=None,
        fp=_FakeFp(body))


class TestLoadEnv(unittest.TestCase):
    def test_parses_and_skips_comments(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nKEY=value\nNUM=123\n\nBARE\n")
            path = f.name
        try:
            env = spa.load_env(path)
        finally:
            os.unlink(path)
        self.assertEqual(env["KEY"], "value")
        self.assertEqual(env["NUM"], "123")
        self.assertNotIn("BARE", env)
        self.assertNotIn("comment", env)


class TestRequestJson(unittest.TestCase):
    def test_get_json(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(b'{"a": 1}')):
            res = spa.request_json("GET", "https://example.test/x")
        self.assertEqual(res, {"a": 1})

    def test_post_encodes_payload(self):
        captured = {}

        def fake_urlopen(req, timeout=25):
            captured["body"] = req.data
            captured["method"] = req.get_method()
            captured["ct"] = req.get_header("Content-type")
            return _FakeResp(b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = spa.request_json("POST", "https://example.test/x", {"k": "v"})
        self.assertEqual(res, {"ok": True})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["body"]), {"k": "v"})
        self.assertIn("application/json", captured["ct"])

    def test_http_error_returns_parsed_body(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=_http_error(b'{"code": 10001}', 401)):
            res = spa.request_json("GET", "https://example.test/x")
        self.assertEqual(res.get("code"), 10001)


class TestSigning(unittest.TestCase):
    def test_sign_is_uppercase_md5_of_appid_appsecret_nonce_ts(self):
        headers = spa.signed_headers("tok123")
        nonce, ts = headers["nonce"], headers["ts"]
        expected = hashlib.md5(
            (spa.APPID + spa.APPSECRET + nonce + ts).encode()).hexdigest().upper()
        self.assertEqual(headers["sign"], expected)

    def test_headers_shape(self):
        headers = spa.signed_headers("tok123")
        self.assertEqual(headers["Authorization"], "token tok123")
        self.assertEqual(len(headers["nonce"]), 32)
        self.assertTrue(headers["ts"].isdigit())
        self.assertAlmostEqual(int(headers["ts"]), int(time.time()), delta=5)
        self.assertEqual(headers["appid"], spa.APPID)


class TestAliases(unittest.TestCase):
    def test_alias_mapping(self):
        self.assertEqual(spa.DATAPONT_ALIASES["power"], "power_state")
        self.assertEqual(spa.DATAPONT_ALIASES["heat"], "heater_state")
        self.assertEqual(spa.DATAPONT_ALIASES["filter"], "filter_state")
        self.assertEqual(spa.DATAPONT_ALIASES["wave"], "wave_state")
        self.assertEqual(spa.DATAPONT_ALIASES["jet"], "hydrojet_state")
        self.assertEqual(spa.DATAPONT_ALIASES["lock"], "locked")

    def test_heater_state_text(self):
        self.assertEqual(spa.HEATER_STATE_TEXT[0], "off")
        self.assertEqual(spa.HEATER_STATE_TEXT[2], "heating (1 element)")
        self.assertEqual(spa.HEATER_STATE_TEXT[4], "holding temperature")


class TestPrintStatus(unittest.TestCase):
    def test_formats_status(self):
        state = {
            "water_temperature": 39, "temperature_setting": 40, "temperature_unit": 1,
            "power_state": 1, "heater_state": 6, "filter_state": 2,
            "wave_state": 0, "hydrojet_state": 0, "warning": "1",
            "error_code": "", "ConnectType": "online", "wifivertion": 121,
        }
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            spa.print_status(state)
        out = buf.getvalue()
        self.assertIn("39C", out)
        self.assertIn("40C", out)
        self.assertIn("ON", out)       # power
        self.assertIn("holding temperature", out)
        self.assertIn("online", out)

    def test_fahrenheit_unit(self):
        state = {"water_temperature": 100, "temperature_setting": 104,
                 "temperature_unit": 0, "power_state": 0, "heater_state": 0,
                 "filter_state": 0, "wave_state": 0, "hydrojet_state": 0,
                 "warning": "0", "error_code": "", "ConnectType": "offline",
                 "wifivertion": 0}
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            spa.print_status(state)
        out = buf.getvalue()
        self.assertIn("100F", out)
        self.assertIn("104F", out)


if __name__ == "__main__":
    unittest.main()
