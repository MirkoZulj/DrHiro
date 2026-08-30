"""Secure Android Bridge pairing tests.

Required scenarios:
  1. expired token
  2. reused token
  3. wrong Telegram user
  4. wrong server
  5. unauthorized user
  6. malformed deep link
  7. rejected non-HTTPS remote endpoint
  8. successful device link
  9. revoked device access
"""
from __future__ import annotations

import json
import time

import pytest

from drhiro_bridge.pairing import (
    DeviceRevokedError,
    PairingManager,
    RateLimitedError,
    TokenExpiredError,
    TokenReusedError,
    WrongServerError,
    WrongUserError,
    validate_server_url,
)


@pytest.fixture()
def mgr(tmp_path):
    return PairingManager(tmp_path / "pairing", token_ttl=600, max_create_per_window=50,
                          max_attempts_per_window=50)


def test_successful_device_link(mgr):
    """A valid token bound to the same user+server exchanges for a device."""
    r = mgr.create_token("user-1", "https://bridge.example.com")
    ex = mgr.exchange(r["token"], None, "https://bridge.example.com", "Pixel 9")
    assert ex["ok"] is True
    assert ex["device_id"]
    assert ex["device_secret"]
    assert ex["insecure"] is False
    # Device is listed for the owner.
    devices = mgr.list_devices("user-1")
    assert len(devices) == 1
    assert devices[0]["device_id"] == ex["device_id"]
    assert devices[0]["revoked"] is False
    # Credential verifies.
    assert mgr.verify_device(ex["device_id"], ex["device_secret"])["ok"] is True


def test_expired_token(mgr):
    r = mgr.create_token("user-1", "https://bridge.example.com")
    # Force expiry by rewriting the record's expires_at in the past.
    mgr._data["tokens"][r["token"]]["expires_at"] = time.time() - 1
    mgr._save()
    with pytest.raises(TokenExpiredError):
        mgr.exchange(r["token"], None, "https://bridge.example.com")


def test_reused_token(mgr):
    r = mgr.create_token("user-1", "https://bridge.example.com")
    mgr.exchange(r["token"], None, "https://bridge.example.com", "Phone A")
    with pytest.raises(TokenReusedError):
        mgr.exchange(r["token"], None, "https://bridge.example.com", "Phone B")


def test_wrong_telegram_user(mgr):
    r = mgr.create_token("user-1", "https://bridge.example.com")
    # Presenting the WRONG user id must be rejected.
    with pytest.raises(WrongUserError):
        mgr.exchange(r["token"], "user-999", "https://bridge.example.com")


def test_wrong_server(mgr):
    r = mgr.create_token("user-1", "https://bridge.example.com")
    with pytest.raises(WrongServerError):
        mgr.exchange(r["token"], None, "https://other.example.com")


def test_unauthorized_user_via_bridge(mgr, bridge, mock_tg, monkeypatch):
    """/pair and /devices only respond to the authorized user."""
    from drhiro_bridge.config import Config
    b = bridge
    b.cfg.allowed_username = "alice"
    b.cfg.pairing_state_dir = str(mgr.state_dir)
    b.cfg.server_public_url = "https://bridge.example.com"
    b.cfg.allowed_user_id = ""
    # Recreate pairing manager bound to the temp dir.
    b.pairing = mgr

    # Unauthorized user sends /pair -> denied, no token created.
    mock_tg["state"].enqueue_update(700, 888, "/pair", "eve")
    upd = mock_tg["state"].update_queue.pop(0)
    b._process_update(upd)
    assert "authorized" in mock_tg["state"].last_message_text().lower()
    # No pairing token was created (eve never reached it because she's unauthorized).
    texts = [m.get("text", "") for m in mock_tg["state"].sent_messages]
    assert not any("Pair your drHiro Bridge" in t for t in texts)


def test_malformed_deep_link():
    from drhiro_bridge.pairing import PairingManager
    # Not a drhiro link
    with pytest.raises(ValueError):
        PairingManager.parse_link("https://evil.example.com/pair?token=x")
    # Wrong scheme
    with pytest.raises(ValueError):
        PairingManager.parse_link("foo://pair?token=x")
    # Missing token/server
    with pytest.raises(ValueError):
        PairingManager.parse_link("drhiro://pair?server=https://a")
    with pytest.raises(ValueError):
        PairingManager.parse_link("drhiro://pair?token=abc")
    # Valid round-trip
    link = "drhiro://pair?server=https%3A%2F%2Fbridge.example.com&token=abc123&version=1"
    parsed = PairingManager.parse_link(link)
    assert parsed["server"] == "https://bridge.example.com"
    assert parsed["token"] == "abc123"


def test_rejected_non_https_remote_endpoint():
    # Remote (non-LAN) HTTP must be rejected.
    with pytest.raises(ValueError):
        validate_server_url("http://bridge.example.com")
    # HTTPS remote is fine.
    secure, warn = validate_server_url("https://bridge.example.com")
    assert secure is True and warn == ""
    # LAN HTTP allowed only as insecure with a warning.
    secure, warn = validate_server_url("http://192.168.1.5:8091")
    assert secure is False and "WARNING" in warn


def test_revoked_device_access(mgr):
    r = mgr.create_token("user-1", "https://bridge.example.com")
    ex = mgr.exchange(r["token"], None, "https://bridge.example.com", "Pixel")
    assert mgr.verify_device(ex["device_id"], ex["device_secret"])["ok"] is True
    # Revoke then verify fails.
    assert mgr.revoke_device(ex["device_id"], "user-1") is True
    with pytest.raises(DeviceRevokedError):
        mgr.verify_device(ex["device_id"], ex["device_secret"])
    # Revoke is owner-scoped: another user cannot revoke it.
    r2 = mgr.create_token("user-2", "https://bridge.example.com")
    ex2 = mgr.exchange(r2["token"], None, "https://bridge.example.com", "Other")
    assert mgr.revoke_device(ex2["device_id"], "user-1") is False


def test_rate_limit_token_creation(tmp_path):
    m = PairingManager(tmp_path / "p2", token_ttl=600, max_create_per_window=2)
    m.create_token("user-1", "https://a.example.com")
    m.create_token("user-1", "https://a.example.com")
    with pytest.raises(RateLimitedError):
        m.create_token("user-1", "https://a.example.com")
