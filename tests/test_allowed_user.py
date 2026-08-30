"""Allowed-user behaviour: only the configured username may use the bot."""
from __future__ import annotations

from drhiro_bridge.main import _authorized, _extract_text
from drhiro_bridge.config import Config


def test_only_configured_username_is_authorized():
    cfg = Config()
    cfg.allowed_username = "alice"

    assert _authorized({"from": {"username": "alice"}}, cfg) is True
    assert _authorized({"from": {"username": "Alice"}}, cfg) is True  # case-insensitive
    assert _authorized({"from": {"username": "alice2"}}, cfg) is False
    assert _authorized({"from": {"username": "bob"}}, cfg) is False
    assert _authorized({"from": {}}, cfg) is False


def test_unauthorized_sender_gets_denied_via_bridge(bridge):
    """Bridge must ignore an unauthorized sender (mock check)."""
    # Simulate the bridge's decision path without a live poll loop.
    cfg = bridge.cfg
    from drhiro_bridge.main import _authorized

    assert _authorized({"from": {"username": "eve"}}, cfg) is False
    assert _authorized({"from": {"username": cfg.allowed_username}}, cfg) is True


def test_message_text_extraction():
    assert _extract_text({"text": "  /start  "}) == "/start"
    assert _extract_text({"text": ""}) == ""
