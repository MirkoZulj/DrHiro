"""Secure Android Bridge pairing.

Server-side pairing service for the drHiro Bridge Android app.

Flow
----
1. An authorized user requests /apk or /pair from the Telegram bot.
2. The bot asks PairingManager.create_token() to mint a short-lived, single-use,
   cryptographically random pairing token BOUND to that Telegram user id and to
   a specific server URL. Default lifetime 10 minutes.
3. The bot sends the generic signed APK plus an inline "Connect drHiro Bridge"
   button carrying a deep link:  drhiro://pair?server=...&token=...
4. The Android Bridge opens the deep link, parses it, and calls the server's
   /pair/exchange endpoint with the token. The server verifies:
     - token exists, not expired, not already used (single-use)
     - token is bound to the SAME Telegram user id
     - the presented server URL matches the token's bound server
   then invalidates the token and returns a device-specific credential.
5. The Bridge stores ONLY that device credential in Android secure storage.
   No bot token, AI key, TrueForge key, root credential, or permanent user token
   is ever embedded in the APK.

Server URL policy
-----------------
- HTTPS is required for non-local (remote) endpoints.
- HTTP is allowed ONLY for explicit trusted-LAN dev mode (localhost / private
  ranges / .local). Every such exchange is flagged `insecure` so the Bridge
  shows a visible warning.

Security properties
-------------------
- Tokens: secrets.token_urlsafe (128 bits), single-use, time-limited.
- Token binding: (telegram_user_id, server_url) — wrong user / wrong server rejected.
- Rate limiting: bounded token creation and exchange attempts per user.
- Devices: a random device secret is returned ONCE; only its SHA-256 hash is stored.
- State persists to a JSON file under PAIRING_STATE_DIR so restarts keep devices.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

log = logging.getLogger("drhiro_bridge.pairing")

DEFAULT_TTL = 600  # 10 minutes
MAX_CREATE_PER_WINDOW = 5
MAX_ATTEMPTS_PER_WINDOW = 10
WINDOW_SECONDS = 600

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_PRIVATE_RANGES = ("10.", "192.168.", "172.")  # 172.16-31 handled below
_LOCAL_TLD = ".local"


class PairingError(RuntimeError):
    pass


class TokenExpiredError(PairingError):
    pass


class TokenReusedError(PairingError):
    pass


class WrongUserError(PairingError):
    pass


class WrongServerError(PairingError):
    pass


class RateLimitedError(PairingError):
    pass


class DeviceRevokedError(PairingError):
    pass


def is_private_lan(host: str) -> bool:
    """True if host is localhost or a private/LAN address (HTTP allowed in dev)."""
    host = host.lower().strip("[]")
    if host in _LOCAL_HOSTS or host.endswith(_LOCAL_TLD):
        return True
    if host.startswith(_PRIVATE_RANGES):
        if host.startswith("172."):
            parts = host.split(".")
            try:
                return 16 <= int(parts[1]) <= 31
            except (IndexError, ValueError):
                return False
        return True
    return False


def validate_server_url(server: str, allow_http_lan: bool = True) -> tuple[bool, str]:
    """Validate a server URL. Returns (is_secure, warning_or_empty).

    Raises ValueError on a malformed or rejected URL. HTTPS is required for
    remote endpoints; HTTP is allowed only for trusted-LAN dev mode and is
    flagged insecure so the Bridge can show a visible warning.
    """
    if not server:
        raise ValueError("server URL is required")
    parsed = urllib.parse.urlparse(server)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("server URL must use http or https")
    if not parsed.hostname:
        raise ValueError("server URL has no hostname")
    if parsed.scheme == "https":
        return True, ""
    # http
    if is_private_lan(parsed.hostname):
        if allow_http_lan:
            return False, (
                "WARNING: this server uses plain HTTP on a trusted LAN. "
                "Only continue if you trust this network."
            )
        raise ValueError("HTTP is not allowed for this server (LAN dev mode disabled)")
    raise ValueError("remote server must use HTTPS")


class PairingManager:
    def __init__(
        self,
        state_dir: str | Path,
        token_ttl: int = DEFAULT_TTL,
        max_create_per_window: int = MAX_CREATE_PER_WINDOW,
        max_attempts_per_window: int = MAX_ATTEMPTS_PER_WINDOW,
        allow_http_lan: bool = True,
        window_seconds: int = WINDOW_SECONDS,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "pairing.json"
        self.token_ttl = token_ttl
        self.max_create = max_create_per_window
        self.max_attempts = max_attempts_per_window
        self.allow_http_lan = allow_http_lan
        self.window = window_seconds
        self._lock = threading.Lock()
        self._data: dict = self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("pairing.json unreadable; starting fresh")
        return {"tokens": {}, "devices": {}, "rate_create": {}, "rate_attempts": {}}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_file)

    # ------------------------------------------------------------------ #
    # Deep link
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_link(server: str, token: str, version: str = "1",
                   expiration_iso: str | None = None) -> str:
        qs = urllib.parse.urlencode({
            "server": server,
            "token": token,
            "version": version,
        })
        if expiration_iso:
            qs += "&expiration=" + urllib.parse.quote(expiration_iso)
        return f"drhiro://pair?{qs}"

    @staticmethod
    def parse_link(link: str) -> dict:
        """Parse a drhiro://pair deep link. Raises ValueError if malformed."""
        if not link or not link.startswith("drhiro://"):
            raise ValueError("not a drhiro deep link")
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme != "drhiro" or parsed.netloc.lower() != "pair":
            raise ValueError("not a drhiro://pair link")
        qs = urllib.parse.parse_qs(parsed.query)
        server = (qs.get("server") or [""])[0]
        token = (qs.get("token") or [""])[0]
        if not server or not token:
            raise ValueError("deep link missing server or token")
        version = (qs.get("version") or ["1"])[0]
        expiration = (qs.get("expiration") or [None])[0]
        return {
            "scheme": "drhiro",
            "host": "pair",
            "server": server,
            "token": token,
            "version": version,
            "expiration": expiration,
        }

    # ------------------------------------------------------------------ #
    # Token creation (rate-limited)
    # ------------------------------------------------------------------ #
    def create_token(self, telegram_user_id: str, server_url: str) -> dict:
        """Mint a single-use pairing token bound to (user, server). Rate-limited."""
        secure, warning = validate_server_url(server_url, self.allow_http_lan)
        now = time.time()
        with self._lock:
            self._prune(now)
            # Rate limit token creation per user.
            recent = [t for t in self._data["rate_create"].get(telegram_user_id, [])
                      if now - t < self.window]
            if len(recent) >= self.max_create:
                raise RateLimitedError("too many pairing tokens requested — try again later")
            recent.append(now)
            self._data["rate_create"][telegram_user_id] = recent[-self.max_create:]

            token = secrets.token_urlsafe(32)
            rec = {
                "token": token,
                "telegram_user_id": str(telegram_user_id),
                "server_url": server_url,
                "created_at": now,
                "expires_at": now + self.token_ttl,
                "used": False,
            }
            self._data["tokens"][token] = rec
            self._save()

        expiration_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["expires_at"]))
        link = self.build_link(server_url, token, expiration_iso=expiration_iso)
        return {
            "token": token,
            "link": link,
            "server_url": server_url,
            "expires_at": rec["expires_at"],
            "expiration_iso": expiration_iso,
            "insecure": not secure,
            "warning": warning,
        }

    # ------------------------------------------------------------------ #
    # Token exchange (single-use, user-bound, server-bound, rate-limited)
    # ------------------------------------------------------------------ #
    def exchange(self, token: str, telegram_user_id: str | None, server_url: str,
                 device_name: str = "Android") -> dict:
        """Exchange a one-time token for a device credential. Invalidate on use.

        The Android Bridge does not know the Telegram user id (it only holds the
        deep link's server+token), so `telegram_user_id` is optional: when absent,
        the server uses the user the token is bound to. When present, it is checked
        against the binding (wrong-user rejection).
        """
        now = time.time()
        secure, warning = validate_server_url(server_url, self.allow_http_lan)
        with self._lock:
            self._prune(now)
            # Rate limit exchange attempts per presented-or-bound user.
            presented = str(telegram_user_id) if telegram_user_id else "?"
            key = presented
            atts = [t for t in self._data["rate_attempts"].get(key, [])
                    if now - t < self.window]
            if len(atts) >= self.max_attempts:
                raise RateLimitedError("too many pairing attempts — try again later")
            atts.append(now)
            self._data["rate_attempts"][key] = atts[-self.max_attempts:]

            rec = self._data["tokens"].get(token)
            if not rec:
                raise PairingError("invalid pairing token")
            if rec.get("used"):
                raise TokenReusedError("pairing token has already been used")
            if now > rec["expires_at"]:
                raise TokenExpiredError("pairing token has expired")
            bound_user = str(rec["telegram_user_id"])
            if presented != "?" and presented != bound_user:
                raise WrongUserError("pairing token is bound to a different user")
            if _normalize(rec["server_url"]) != _normalize(server_url):
                raise WrongServerError("pairing token is bound to a different server")

            # Invalidate (single-use).
            rec["used"] = True

            # Issue a device credential (random, returned once; only hash stored).
            device_id = uuid.uuid4().hex
            device_secret = secrets.token_urlsafe(32)
            self._data["devices"][device_id] = {
                "device_id": device_id,
                "device_secret_hash": hashlib.sha256(device_secret.encode()).hexdigest(),
                "telegram_user_id": bound_user,
                "server_url": server_url,
                "device_name": device_name or "Android",
                "created_at": now,
                "revoked": False,
            }
            self._save()

        return {
            "ok": True,
            "device_id": device_id,
            "device_secret": device_secret,
            "server_url": server_url,
            "insecure": not secure,
            "warning": warning,
            "notice": "Store the device secret in Android secure storage. "
                      "It is shown only once.",
        }

    # ------------------------------------------------------------------ #
    # Device management
    # ------------------------------------------------------------------ #
    def verify_device(self, device_id: str, device_secret: str) -> dict:
        """Verify a device credential. Raises if revoked or unknown."""
        with self._lock:
            d = self._data["devices"].get(device_id)
            if not d:
                raise PairingError("unknown device")
            if d.get("revoked"):
                raise DeviceRevokedError("device access has been revoked")
            h = hashlib.sha256(device_secret.encode()).hexdigest()
            if h != d["device_secret_hash"]:
                raise PairingError("invalid device credential")
            return {"ok": True, "device_id": device_id,
                    "server_url": d["server_url"]}

    def list_devices(self, telegram_user_id: str) -> list[dict]:
        """List the requesting user's linked devices (non-sensitive fields)."""
        with self._lock:
            out = []
            for d in self._data["devices"].values():
                if d["telegram_user_id"] == str(telegram_user_id):
                    out.append({
                        "device_id": d["device_id"],
                        "device_name": d["device_name"],
                        "server_url": d["server_url"],
                        "created_at": d["created_at"],
                        "revoked": d.get("revoked", False),
                    })
            return out

    def revoke_device(self, device_id: str, telegram_user_id: str) -> bool:
        """Revoke a device owned by the given user. Returns False if not found."""
        with self._lock:
            d = self._data["devices"].get(device_id)
            if not d or d["telegram_user_id"] != str(telegram_user_id):
                return False
            d["revoked"] = True
            self._save()
            return True

    def regenerate_link(self, telegram_user_id: str, server_url: str) -> dict:
        """Issue a fresh pairing link without sending the APK again."""
        return self.create_token(telegram_user_id, server_url)

    # ------------------------------------------------------------------ #
    def _prune(self, now: float) -> None:
        # Keep used tokens until they would have expired, so a reuse attempt
        # returns a precise TokenReusedError instead of a generic "invalid".
        self._data["tokens"] = {
            t: r for t, r in self._data["tokens"].items()
            if not r.get("used") or r["expires_at"] > now
        }


def _normalize(url: str) -> str:
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    return f"{p.scheme}://{host}{p.path.rstrip('/')}"
