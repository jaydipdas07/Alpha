"""Telegram Bot API helpers (Phase 9 alerting; ADR 0011 — token from env only).

Pure-stdlib (urllib) calls to the Bot API: discover the chat id from recent
updates and send a message. The bot token comes from the environment; it is
never logged. Used by ``observability.notify.TelegramNotifier`` and
``scripts/telegram_setup.py``.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(  # pragma: no cover - HTTP call to Telegram
    token: str, method: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        parsed: dict[str, Any] = json.loads(resp.read())
        return parsed


def extract_chat_id(updates: dict[str, Any]) -> str | None:
    """Pure: pull the most recent chat id from a getUpdates response."""
    for update in reversed(updates.get("result", [])):
        message = update.get("message") or update.get("channel_post")
        if message and "chat" in message:
            return str(message["chat"]["id"])
    return None


def get_chat_id(token: str) -> str | None:  # pragma: no cover - HTTP getUpdates
    """The chat id of the most recent message sent to the bot (None if none)."""
    return extract_chat_id(_call(token, "getUpdates"))


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send ``text`` to ``chat_id``; returns True on success. Best-effort."""
    try:
        return bool(_call(token, "sendMessage", {"chat_id": chat_id, "text": text}).get("ok"))
    except Exception:
        return False
