"""Unit tests for spa.py — env parsing, HTTP helper (mocked), signing,
datapoint aliases, and status formatting. No network access."""
import hashlib
import io
import json
import os
import shutil
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


class TestIsOnline(unittest.TestCase):
    """spa.is_online — offline markers must win, unknown fields stay optimistic."""

    def test_online_states(self):
        self.assertTrue(spa.is_online({"ConnectType": "online", "is_online": True}))
        self.assertTrue(spa.is_online({"is_online": True}))
        self.assertTrue(spa.is_online({"is_online": "1"}))
        # unknown/partial shadows (older devices) default to online
        self.assertTrue(spa.is_online({"ConnectType": "online"}))
        self.assertTrue(spa.is_online({"water_temperature": 39, "heater_state": 6}))
        # but an empty/absent state does not mean the tub is reachable
        self.assertFalse(spa.is_online({}))

    def test_offline_states(self):
        self.assertFalse(spa.is_online(None))
        self.assertFalse(spa.is_online({"is_online": False}))
        self.assertFalse(spa.is_online({"is_online": "0"}))
        self.assertFalse(spa.is_online({"ConnectType": "offline"}))
        self.assertFalse(spa.is_online({"ConnectType": "offline", "is_online": True}))


class TestIsAuthError(unittest.TestCase):
    def test_detects_token_problems(self):
        self.assertTrue(spa.is_auth_error(spa.SpaError(
            'thing_shadow failed: {"data": {}, "code": 10001, '
            '"message": "Token is not authorized"}')))
        self.assertTrue(spa.is_auth_error(spa.SpaError("Please login first")))
        self.assertTrue(spa.is_auth_error(spa.SpaError("Authentication failed")))

    def test_ignores_transient_errors(self):
        self.assertFalse(spa.is_auth_error(spa.SpaError(
            "command failed: <urlopen error [Errno -3] Temporary failure in "
            "name resolution>")))
        self.assertFalse(spa.is_auth_error(spa.SpaError(
            "thing_shadow failed: device offline")))
        self.assertFalse(spa.is_auth_error(RuntimeError("connection reset")))


class TestLoginBinding(unittest.TestCase):
    """login()/get_tokens(force=True) — the no-device-binding case that used to
    crash with a bare TypeError ('NoneType' object is not subscriptable) must
    now raise a clear SpaError, and errors must never leak the account token."""

    def _tokens_path(self, tmp):
        return os.path.join(tmp, "tokens.json")

    def _login_with(self, response, tmp):
        with mock.patch.object(spa, "BASE_DIR", tmp), \
             mock.patch.object(spa.os.path, "exists", return_value=False), \
             mock.patch.object(spa, "load_env",
                               return_value={"EMAIL": "me@example.test",
                                             "PASSWORD": "pw"}), \
             mock.patch.object(spa, "request_json", return_value=response):
            return spa.get_tokens(force=True)

    def test_no_binding_raises_clear_error(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="spa-login-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        response = {
            "data": {"user_id": 1, "api_token": "token-abc123",
                     "mxchip_device": None, "mxchip_user_account": None,
                     "gizwits_2026_devices": [],
                     "push_notification_token": "push-xyz"},
            "message": "Authorisation Successful!",
        }
        with self.assertRaises(spa.SpaError) as cm:
            self._login_with(response, tmp)
        msg = str(cm.exception)
        self.assertIn("no spa binding returned", msg)
        self.assertIn("offline", msg)
        self.assertIn("wifi", msg)
        # Neither the Laravel token nor the push token leaks into the error.
        self.assertNotIn("token-abc123", msg)
        self.assertNotIn("push-xyz", msg)
        # And nothing was written to disk on a failed binding.
        self.assertFalse(os.path.exists(self._tokens_path(tmp)))

    def test_normal_binding_writes_tokens(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="spa-login-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        response = {
            "data": {
                "api_token": "laravel-token-1",
                "mxchip_device": {"did": "dev-1", "mac": "aa",
                                  "product_key": "FTEW0E",
                                  "device_name": "My Lay-Z-Spa",
                                  "pump_type": "ultrafit"},
                "mxchip_user_account": {"visitor_id": "u_1", "token": "mx-token-1"},
            },
        }
        with mock.patch.object(spa.os, "chmod") as chmod:
            tokens = self._login_with(response, tmp)
        self.assertEqual(tokens["did"], "dev-1")
        self.assertEqual(tokens["product_key"], "FTEW0E")
        self.assertEqual(tokens["mxchip_token"], "mx-token-1")
        self.assertEqual(tokens["laravel_token"], "laravel-token-1")
        with open(self._tokens_path(tmp)) as f:
            saved = json.load(f)
        self.assertEqual(saved["did"], "dev-1")
        self.assertEqual(saved["mxchip_token"], "mx-token-1")
        chmod.assert_called_with(self._tokens_path(tmp), 0o600)


class TestSmartSpaLogin(unittest.TestCase):
    """login() must prefer the gizwits_2026_devices binding (SmartSpa backend)
    over the legacy MXCHIP one and record the session in tokens.json."""

    def test_prefers_gizwits_binding(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="spa-smartspa-login-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        response = {
            "data": {
                "api_token": "laravel-1",
                "mxchip_device": None, "mxchip_user_account": None,
                "gizwits_2026_devices": [{
                    "device_id": "0123456789abcdef0123456789abcdef",
                    "mac": "00112233AABB",
                    "device_name": "My Lay-Z-Spa",
                    "gizwits_user_account": {"uid": "u1", "token": "giz-tok-1",
                                             "domain_id": "d1",
                                             "expires_at": "2027-05-19"},
                }],
            },
        }
        dev_list = {"code": "200",
                    "data": [{"productKey": "FTEW0E", "mac": "00112233AABB",
                              "did": "00000000000000000001", "name": "My Lay-z-spa",
                              "onlineStatus": 1}]}
        with mock.patch.object(spa, "BASE_DIR", tmp), \
             mock.patch.object(spa.os.path, "exists", return_value=False), \
             mock.patch.object(spa, "load_env",
                               return_value={"EMAIL": "me@example.test",
                                             "PASSWORD": "pw"}), \
             mock.patch.object(spa, "request_json", return_value=response), \
             mock.patch.object(spa, "_raw_request", return_value=(200, dev_list)), \
             mock.patch.object(spa.os, "chmod"):
            tokens = spa.get_tokens(force=True)
        self.assertEqual(tokens["backend"], "smartspa")
        self.assertEqual(tokens["smartspa_token"], "giz-tok-1")
        self.assertEqual(tokens["mac"], "00112233aabb")   # lowercased
        self.assertEqual(tokens["product_key"], "FTEW0E")
        self.assertEqual(tokens["smartspa_region"], "eu")
        self.assertNotIn("mxchip_token", tokens)
        with open(os.path.join(tmp, "tokens.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["backend"], "smartspa")
        self.assertEqual(saved["smartspa_token"], "giz-tok-1")
        self.assertEqual(saved["product_key"], "FTEW0E")

    def test_falls_back_to_mxchip_without_gizwits(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="spa-mxchip-login-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        response = {
            "data": {
                "api_token": "laravel-1",
                "gizwits_2026_devices": [],
                "mxchip_device": {"did": "dev-1", "product_key": "FTEW0E",
                                  "device_name": "My Lay-Z-Spa"},
                "mxchip_user_account": {"token": "mx-1"},
            },
        }
        with mock.patch.object(spa, "BASE_DIR", tmp), \
             mock.patch.object(spa.os.path, "exists", return_value=False), \
             mock.patch.object(spa, "load_env",
                               return_value={"EMAIL": "me@example.test",
                                             "PASSWORD": "pw"}), \
             mock.patch.object(spa, "request_json", return_value=response), \
             mock.patch.object(spa.os, "chmod"):
            tokens = spa.get_tokens(force=True)
        self.assertEqual(tokens["backend"], "mxchip")
        self.assertEqual(tokens["mxchip_token"], "mx-1")
        self.assertEqual(tokens["did"], "dev-1")


class TestSmartSpaApi(unittest.TestCase):
    """shadow()/send_command() dispatch to the SmartSpa gateway for
    backend=smartspa sessions — with the same datapoints as the MXCHIP shadow."""

    TOKENS = {"backend": "smartspa", "smartspa_token": "tok",
              "smartspa_region": "eu", "mac": "00112233aabb",
              "product_key": "FTEW0E"}

    def test_shadow_dispatches_and_returns_data(self):
        body = {"code": "200",
                "data": {"water_temperature": 40, "heater_state": 4,
                         "ConnectType": "online", "error_code": 0}}
        with mock.patch.object(spa, "_raw_request", return_value=(200, body)) as raw:
            state = spa.shadow(self.TOKENS)
        self.assertEqual(state["water_temperature"], 40)
        self.assertEqual(state["heater_state"], 4)
        url = raw.call_args[0][1]
        self.assertIn("smart-spa-eu-app.bestwaycorp.com/app/device/shadow/"
                      "FTEW0E/00112233aabb", url)

    def test_legacy_mxchip_still_uses_smarthub(self):
        tokens = {"backend": "mxchip", "mxchip_token": "mx",
                  "did": "dev-1", "product_key": "FTEW0E"}
        with mock.patch.object(spa, "request_json",
                               return_value={"code": 0,
                                             "data": {"water_temperature": 39}}) as rq, \
             mock.patch.object(spa, "signed_headers", return_value={}):
            state = spa.shadow(tokens)
        self.assertEqual(state["water_temperature"], 39)
        self.assertIn("smarthub-eu.bestwaycorp.com", rq.call_args[0][1])

    def test_control_maps_booleans_and_string_data(self):
        ok = {"code": "200", "data": True}
        with mock.patch.object(spa, "_raw_request", return_value=(200, ok)) as raw:
            spa.send_command(self.TOKENS, "heater_state", 4)
            spa.send_command(self.TOKENS, "temperature_setting", 40)
            spa.send_command(self.TOKENS, "power_state", 1)
        calls = raw.call_args_list
        self.assertEqual(len(calls), 3)
        # heater/power are boolean datapoints -> forced to 1/0; temperature kept.
        self.assertEqual(json.loads(calls[0].args[2]["data"]), {"heater_state": 1})
        self.assertEqual(json.loads(calls[2].args[2]["data"]), {"power_state": 1})
        self.assertEqual(json.loads(calls[1].args[2]["data"]),
                         {"temperature_setting": 40})
        for c in calls:
            url = c.args[1]
            self.assertIn("app/device/control/FTEW0E/00112233aabb", url)
            self.assertIn("Content-MD5", c.kwargs["headers"])

    def test_rejected_token_raises_auth_error(self):
        with mock.patch.object(spa, "_raw_request", return_value=(401, {"code": "505"})):
            try:
                spa.shadow(self.TOKENS)
                self.fail("expected an auth SpaError")
            except spa.SpaError as e:
                self.assertTrue(spa.is_auth_error(e), str(e))
                self.assertIn("not authorized", str(e))

    def test_non_auth_gateway_error_raises_spa_error(self):
        with mock.patch.object(spa, "_raw_request",
                               return_value=(200, {"code": "4001",
                                                   "message": "boom"})):
            with self.assertRaises(spa.SpaError) as cm:
                spa.shadow(self.TOKENS)
            self.assertFalse(spa.is_auth_error(cm.exception))


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
