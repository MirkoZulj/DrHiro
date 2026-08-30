"""Telegram webhook conflict detection: polling must never run with a webhook."""
from __future__ import annotations

import pytest

from drhiro_bridge.telegram_client import TelegramClient, WebhookConflictError


def test_no_webhook_allows_polling(mock_tg):
    client = TelegramClient("tok", api_base=mock_tg["base"])
    client._api = f"{mock_tg['base']}/bottok"
    assert client.ensure_polling_only() is True


def test_webhook_set_blocks_polling(mock_tg):
    mock_tg["state"].webhook_url = "https://example.com/hook"
    client = TelegramClient("tok", api_base=mock_tg["base"])
    client._api = f"{mock_tg['base']}/bottok"
    with pytest.raises(WebhookConflictError):
        client.ensure_polling_only()


def test_delete_webhook_clears_and_allows_polling(mock_tg):
    mock_tg["state"].webhook_url = "https://example.com/hook"
    client = TelegramClient("tok", api_base=mock_tg["base"])
    client._api = f"{mock_tg['base']}/bottok"
    with pytest.raises(WebhookConflictError):
        client.ensure_polling_only()
    # Explicit operator confirmation -> delete, then polling is safe.
    client.delete_webhook()
    assert client.ensure_polling_only() is True
    assert mock_tg["state"].webhook_url == ""
