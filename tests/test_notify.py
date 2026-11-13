"""Unit tests for notify.py — Telegram notifications (stdlib)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, ".")
import notify


class TestNotifyConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, notify, "BASE_DIR", notify.BASE_DIR)
        notify.BASE_DIR = self.tmp.name

    def test_not_configured_without_env(self):
        self.assertFalse(notify.configured())

    def test_configured_with_token_and_chat(self):
        with open(os.path.join(self.tmp.name, "telegram.env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=abc:123\nTELEGRAM_CHAT_ID=-100123\n")
        self.assertTrue(notify.configured())

    def test_send_text_not_configured_is_safe(self):
        ok, err = notify.send_text("hello")
        self.assertFalse(ok)
        self.assertIn("not configured", err)

    def test_send_text_posts_and_parses_ok(self):
        with open(os.path.join(self.tmp.name, "telegram.env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=abc:123\nTELEGRAM_CHAT_ID=-100123\n")
        payload = {"ok": True, "result": {"message_id": 5}}
        mock_open = mock.mock_open(read_data=json.dumps(payload).encode())
        with mock.patch.object(notify.urllib.request, "urlopen", mock_open):
            ok, err = notify.send_text("tub is hot")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_send_text_surfaces_api_error(self):
        with open(os.path.join(self.tmp.name, "telegram.env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=abc:123\nTELEGRAM_CHAT_ID=-100123\n")
        payload = {"ok": False, "description": "chat not found"}
        mock_open = mock.mock_open(read_data=json.dumps(payload).encode())
        with mock.patch.object(notify.urllib.request, "urlopen", mock_open):
            ok, err = notify.send_text("x")
        self.assertFalse(ok)
        self.assertIn("chat not found", err)

    def test_send_text_never_raises_on_network_error(self):
        with open(os.path.join(self.tmp.name, "telegram.env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=abc:123\nTELEGRAM_CHAT_ID=-100123\n")
        with mock.patch.object(notify.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            ok, err = notify.send_text("x")
        self.assertFalse(ok)
        self.assertIn("boom", err)


if __name__ == "__main__":
    unittest.main()