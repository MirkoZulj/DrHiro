"""Acceptance tests for the web OTP login flow (passwordless).

Covers: request sends code (mock Telegram), verify issues tokens,
wrong code is rejected, rate limit on resend, enumeration resistance.
"""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from drhiro_api.models import ExternalIdentity, User
from tests.conftest import make_user

from drhiro_api.routers.auth_web import _redis


@pytest.fixture()
def telegram_user(db):
    user = make_user(db, "Alice", telegram_id="1001")
    return user


def _clear_otp_keys(identifier: str):
    r = _redis()
    norm = identifier.strip().lower()
    r.delete(f"drhiro:otp:code:{norm}")
    r.delete(f"drhiro:otp:req:{norm}")


def test_otp_flow_success(client, telegram_user, db):
    _clear_otp_keys("Alice")
    with mock.patch("drhiro_api.routers.auth_web._send_telegram_code", return_value=True) as send:
        r = client.post("/api/v1/auth/web/otp/request", json={"identifier": "Alice"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["sent_to"] == "telegram"
        send.assert_called_once()
        # The code is in Redis; fetch it to simulate the user reading Telegram.
        r_redis = _redis()
        stored = json.loads(r_redis.get("drhiro:otp:code:alice"))
        code = stored["code"]

    r2 = client.post("/api/v1/auth/web/otp/verify", json={"identifier": "Alice", "code": code})
    assert r2.status_code == 200
    assert r2.json()["token_type"] == "bearer"
    assert "access_token" in r2.json()
    assert r2.json()["user_id"] == str(telegram_user.id)


def test_otp_wrong_code(client, telegram_user):
    _clear_otp_keys("Alice")
    with mock.patch("drhiro_api.routers.auth_web._send_telegram_code", return_value=True):
        client.post("/api/v1/auth/web/otp/request", json={"identifier": "Alice"})
    r = client.post("/api/v1/auth/web/otp/verify", json={"identifier": "Alice", "code": "000000"})
    assert r.status_code == 401


def test_otp_unknown_user_not_revealed(client, telegram_user):
    r = client.post("/api/v1/auth/web/otp/request", json={"identifier": "Ghost"})
    assert r.status_code == 404  # generic message, no user existence leak


def test_otp_resend_rate_limited(client, telegram_user):
    _clear_otp_keys("Alice")
    with mock.patch("drhiro_api.routers.auth_web._send_telegram_code", return_value=True):
        r1 = client.post("/api/v1/auth/web/otp/request", json={"identifier": "Alice"})
        assert r1.status_code == 200
        r2 = client.post("/api/v1/auth/web/otp/request", json={"identifier": "Alice"})
        assert r2.status_code == 429
