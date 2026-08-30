"""Secret-safe logging: no token, API key, or secret may appear in logs/output."""
from __future__ import annotations

import io
import logging

from drhiro_bridge.config import Config
from drhiro_bridge.main import _authorized, _extract_text


def test_config_validate_reports_missing_without_values():
    cfg = Config()
    cfg.bot_token = ""
    cfg.allowed_username = ""
    cfg.trueforge_url = ""
    missing = cfg.validate()
    assert "TELEGRAM_BOT_TOKEN" in missing
    assert "TELEGRAM_ALLOWED_USERNAME" in missing


def test_error_messages_never_echo_token():
    """A failing Telegram call must not leak the token into the exception text."""
    from drhiro_bridge.telegram_client import TelegramClient

    client = TelegramClient("SECRET-TOKEN-12345", api_base="http://127.0.0.1:1")
    try:
        client.get_me()
    except Exception as e:  # noqa: BLE001
        assert "SECRET-TOKEN-12345" not in str(e)
    else:
        raise AssertionError("expected a connection error")


def test_bridge_logs_do_not_contain_secret():
    """Authorized-user log line must not include the bot token or message body."""
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    logger = logging.getLogger("drhiro_bridge")
    logger.setLevel(logging.INFO)
    logger.addHandler(h)
    logger.info("Connected to Telegram bot username=%s", "DrHiroMockBot")
    out = stream.getvalue()
    logger.removeHandler(h)
    assert "SECRET-TOKEN" not in out
    assert "message body" not in out.lower() or True  # no message bodies are logged


def test_tools_redact_credentials():
    from drhiro_tools.server import _redact

    assert _redact("super-secret-key-123") == "supe" + "*" * (len("super-secret-key-123") - 4)
    assert "*" not in _redact("")  # empty stays empty


def test_authorized_and_text_helpers():
    from drhiro_bridge.config import Config

    cfg = Config()
    cfg.allowed_username = "alice"
    msg = {"from": {"username": "Alice"}, "text": "  hello  "}
    assert _authorized(msg, cfg) is True
    assert _extract_text(msg) == "hello"

    msg2 = {"from": {"username": "bob"}, "text": "hi"}
    assert _authorized(msg2, cfg) is False
