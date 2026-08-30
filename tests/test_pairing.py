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


def test_server_url_canonicalized_not_blindly_trusted(mgr):
    """The deep-link server must be canonicalized and compared against the
    trusted server (DRHIRO_PUBLIC_URL), not trusted verbatim. A host differing
    only by case/port/whitespace must still match its canonical form."""
    trusted = "https://Bridge.Example.com"
    # Canonical: scheme + lowercased host, port stripped if default.
    from drhiro_bridge.pairing import _normalize
    assert _normalize("HTTPS://bridge.example.com:443") == _normalize(trusted)
    # Different host must NOT match.
    assert _normalize("https://evil.example.com") != _normalize(trusted)


def test_exchange_rejects_server_not_matching_token_bound(mgr):
    """The server URL from the deep link must equal the token's bound server
    (which came from DRHIRO_PUBLIC_URL). A different-but-plausible host fails."""
    r = mgr.create_token("user-1", "https://Bridge.Example.com")
    # Same host canonicalized (case/port differs) — accepted.
    ex = mgr.exchange(r["token"], None, "https://bridge.example.com:443", "Pixel")
    assert ex["ok"] is True
    # Different host — rejected.
    r2 = mgr.create_token("user-1", "https://trusted.example.com")
    with pytest.raises(WrongServerError):
        mgr.exchange(r2["token"], None, "https://evil.example.com")


def test_management_endpoints_require_service_token(tmp_path):
    """/pair/devices and /pair/revoke must be refused without the service token;
    only /pair/exchange is open to an unpaired Bridge."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from drhiro_bridge.pairing_http import _Handler, serve

    m = PairingManager(tmp_path / "p3", token_ttl=600)
    _Handler.manager = m
    _Handler.service_token = "svc-secret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def req(method, path, body=None, headers=None):
        data = json.dumps(body or {}).encode() if body is not None else None
        r = urllib.request.Request(base + path, data=data, method=method,
                                   headers=headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        # No token -> management refused.
        code, _ = req("GET", "/pair/devices?user=user-1")
        assert code == 401
        code, _ = req("POST", "/pair/revoke", {"device_id": "x", "user": "user-1"})
        assert code == 401
        # With token -> allowed.
        code, _ = req("GET", "/pair/devices?user=user-1",
                      headers={"X-Service-Token": "svc-secret"})
        assert code == 200
        # exchange is open without a token (unpaired bridge).
        tok = m.create_token("user-1", "https://bridge.example.com")
        code, _ = req("POST", "/pair/exchange",
                      {"token": tok["token"], "server_url": "https://bridge.example.com"})
        assert code == 200
    finally:
        server.shutdown()


def test_http_token_only_exchange_succeeds(tmp_path):
    """Qodo #2: the Android Bridge request contains only server + token (no
    telegram_user_id). The HTTP adapter must preserve that missing value as None
    so the manager uses the identity bound to the token, instead of coercing it
    to an empty string that would raise WrongUserError."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from drhiro_bridge.pairing_http import _Handler

    m = PairingManager(tmp_path / "p4", token_ttl=600)
    _Handler.manager = m
    _Handler.service_token = "svc-secret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    # Token bound to a real user.
    tok = m.create_token("user-1", "https://bridge.example.com")

    # Android-style request: ONLY token + server_url (no telegram_user_id).
    body = json.dumps({
        "token": tok["token"],
        "server_url": "https://bridge.example.com",
    }).encode()
    req = urllib.request.Request(
        base + "/pair/exchange", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.status
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        code = e.code
        result = json.loads(e.read() or b"{}")

    assert code == 200, result
    assert result["ok"] is True
    assert result["device_id"]
    assert result["device_secret"]
    # The device is owned by the token-bound user.
    assert m.list_devices("user-1")[0]["device_id"] == result["device_id"]
    server.shutdown()


def test_pairing_refuses_when_public_url_unset(bridge, mock_tg, mock_tf, tmp_path):
    """Qodo #3: when DRHIRO_PUBLIC_URL is unset, /pair must refuse to mint an
    unreachable localhost deep link and instead surface an actionable message —
    no unreachable pairing link may be generated."""
    import tempfile

    from drhiro_bridge.config import Config
    from drhiro_bridge.main import Bridge
    from drhiro_bridge.pairing import PairingManager

    # Fresh bridge with pairing_state in a temp dir and NO public URL.
    cfg = Config()
    cfg.bot_token = "123456:TESTTOKEN"
    cfg.allowed_username = "alice"
    cfg.trueforge_url = mock_tf["base"]
    cfg.agent_name = "drhiro"
    cfg.poll_timeout = 2
    cfg.pairing_state_dir = tempfile.mkdtemp(prefix="pairtest3")
    cfg.server_public_url = ""  # unset -> pairing must refuse
    b = Bridge(cfg)
    b.tg._api = f"{mock_tg['base']}/bottok"

    mock_tg["state"].enqueue_update(800, 555, "/pair", "alice")
    upd = mock_tg["state"].update_queue.pop(0)
    b._process_update(upd)

    last = mock_tg["state"].last_message_text()
    assert "DRHIRO_PUBLIC_URL" in last
    # No deep-link button / no unreachable localhost link was sent.
    assert "localhost" not in last
    # No pairing token was created for the chat user.
    tokens = [t for t in b.pairing._data["tokens"].values() if not t.get("used")]
    assert tokens == []
