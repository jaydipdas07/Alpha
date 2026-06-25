"""Telegram helper tests (pure chat-id extraction; no network)."""

from __future__ import annotations

from typing import Any

from alpha_core.helpers.telegram import extract_chat_id


def test_extract_chat_id_from_message() -> None:
    updates: dict[str, Any] = {
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 12345}, "text": "hi"}},
        ]
    }
    assert extract_chat_id(updates) == "12345"


def test_extract_chat_id_uses_most_recent() -> None:
    updates: dict[str, Any] = {
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}}},
            {"update_id": 2, "message": {"chat": {"id": 222}}},
        ]
    }
    assert extract_chat_id(updates) == "222"


def test_extract_chat_id_channel_post() -> None:
    updates: dict[str, Any] = {"result": [{"channel_post": {"chat": {"id": -100}}}]}
    assert extract_chat_id(updates) == "-100"


def test_extract_chat_id_empty() -> None:
    assert extract_chat_id({"result": []}) is None
    assert extract_chat_id({}) is None
