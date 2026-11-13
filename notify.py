"""Telegram notifications for torronto — pure stdlib (zero extra deps).

Config lives in a gitignored ``telegram.env`` next to this file::

    TELEGRAM_BOT_TOKEN=<bot token, get one from @BotFather>
    TELEGRAM_CHAT_ID=<channel/group/user chat id the bot is allowed to post to>

Send the message with the standard Bot API
(``https://api.telegram.org/bot<token>/sendMessage``), the same mechanism the
project's portfolio/polymarket tooling uses (a bot token + a chat id).

All functions are safe to call before/without configuration — they return
``(ok, error)`` instead of raising, so the server never breaks if Telegram
isn't set up yet.
"""
import json
import os
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
CHAT_VAR = "TELEGRAM_CHAT_ID"
API = "https://api.telegram.org/bot%s/sendMessage"


def load_config():
    """Read telegram.env into a dict (empty when missing/unconfigurable)."""
    cfg = {}
    path = os.path.join(BASE_DIR, "telegram.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg


def configured():
    cfg = load_config()
    return bool(cfg.get(TOKEN_VAR) and cfg.get(CHAT_VAR))


def send_text(text, chat_id=None):
    """Send ``text`` to the configured chat. Returns ``(ok, error)``."""
    cfg = load_config()
    token = cfg.get(TOKEN_VAR)
    cid = chat_id or cfg.get(CHAT_VAR)
    if not token or not cid:
        return False, "telegram not configured (set telegram.env)"
    body = urllib.parse.urlencode({
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            API % token, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if payload.get("ok"):
            return True, ""
        return False, (payload.get("description") or "telegram sendMessage failed")
    except Exception as e:  # noqa: BLE001 - never let Telegram break the server
        return False, str(e)