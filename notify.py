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
import urllib.error
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


def get_updates(offset=None, timeout=20):
    """Long-poll Telegram getUpdates. Returns a list of update dicts.

    Used for the two-way bot (``/status``, ``/mode``, ...). Returns [] when
    the bot isn't configured or the API errors. The chunk stays limited to
    messages and channel posts so the poller sees commands only.
    """
    cfg = load_config()
    token = cfg.get(TOKEN_VAR)
    if not token:
        return []
    params = {
        "timeout": str(timeout),
        "allowed_updates": '["message","channel_post"]',
    }
    if offset:
        params["offset"] = str(offset)
    url = "https://api.telegram.org/bot%s/getUpdates?%s" % (
        token, urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=timeout + 5) as r:
            payload = json.loads(r.read().decode("utf-8"))
        return payload.get("result", []) if payload.get("ok") else []
    except Exception:  # noqa: BLE001 - long-poll must never raise up
        return []


def configured_chat_id():
    """Return the configured chat id (as a str), or None."""
    return load_config().get(CHAT_VAR)


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
    except urllib.error.HTTPError as e:  # Telegram returns errors as HTTP 4xx with a body
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
            desc = detail.get("description") or "HTTP " + str(e.code)
        except Exception:  # noqa: BLE001
            desc = "HTTP " + str(e.code)
        return False, desc
    except Exception as e:  # noqa: BLE001 - never let Telegram break the server
        return False, str(e)