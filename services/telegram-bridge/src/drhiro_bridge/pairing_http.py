"""Device-facing HTTP API for Android Bridge pairing.

Served by the drHiro server so the Bridge can exchange a one-time token for a
device credential, verify its credential, list, and revoke. Bound to localhost /
the Docker network — never exposed publicly.

Endpoints (JSON):
  POST /pair/exchange   {token, telegram_user_id, server_url, device_name}
  POST /pair/verify     {device_id, device_secret}
  GET  /pair/devices?user=<id>
  POST /pair/revoke     {device_id, user}
"""
from __future__ import annotations

import json
import logging
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pairing import PairingManager

log = logging.getLogger("drhiro_bridge.pairing_http")


def _ok(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _err(handler, message, status=400):
    _ok(handler, {"ok": False, "error": message}, status)


class _Handler(BaseHTTPRequestHandler):
    manager: PairingManager = None  # type: ignore[assignment]
    service_token: str = ""

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _require_service_auth(self) -> bool:
        """Management endpoints (/pair/devices, /pair/revoke) require the
        X-Service-Token header. Unauthenticated callers are refused."""
        if not self.service_token:
            return False
        provided = self.headers.get("X-Service-Token", "")
        return secrets.compare_digest(provided, self.service_token)

    def _auth_fail(self):
        _err(self, "unauthorized", 401)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()
        try:
            if path == "/pair/exchange":
                # Open to an UNPAIRED Bridge; uses a single-use token.
                self._exchange(body)
            elif path == "/pair/verify":
                self._verify(body)
            elif path == "/pair/revoke":
                # Management action — must be service-authenticated.
                if not self._require_service_auth():
                    self._auth_fail()
                    return
                self._revoke(body)
            else:
                _err(self, "not found", 404)
        except Exception as e:  # noqa: BLE001
            log.warning("pairing %s failed: %s", path, e)
            _err(self, str(e), 400)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/pair/devices":
            # Management read — must be service-authenticated.
            if not self._require_service_auth():
                self._auth_fail()
                return
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            user = (qs.get("user") or [""])[0]
            devices = self.manager.list_devices(user)
            _ok(self, {"ok": True, "devices": devices})
            return
        _err(self, "not found", 404)

    def _exchange(self, body):
        token = body.get("token", "")
        user = str(body.get("telegram_user_id", ""))
        server = body.get("server_url", "")
        name = body.get("device_name", "Android")
        result = self.manager.exchange(token, user, server, device_name=name)
        _ok(self, result)

    def _verify(self, body):
        result = self.manager.verify_device(body.get("device_id", ""), body.get("device_secret", ""))
        _ok(self, result)

    def _revoke(self, body):
        ok = self.manager.revoke_device(body.get("device_id", ""), str(body.get("user", "")))
        _ok(self, {"ok": ok})

    def log_message(self, format, *args):  # noqa: A002
        pass


def serve(manager: PairingManager, host: str = "0.0.0.0", port: int = 8091,
          service_token: str = "") -> None:
    """Run the pairing HTTP server (blocking).

    `service_token` protects the management endpoints (/pair/devices,
    /pair/revoke). Only /pair/exchange is open to an unpaired Bridge (single-use
    token). Host defaults to 0.0.0.0 but the container does NOT publish the port
    to the host; remote access must be fronted by HTTPS.
    """
    _Handler.manager = manager
    _Handler.service_token = service_token
    server = ThreadingHTTPServer((host, port), _Handler)
    log.info("Pairing HTTP server listening on %s:%s", host, port)
    server.serve_forever()
