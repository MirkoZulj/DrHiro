"""Configuration for the drHiro Telegram bridge.

Loads the FIVE installer inputs plus advanced overrides. Secrets are read from
environment variables only, never logged, and never written to disk beyond the
protected .env file the installer creates (mode 600).
"""
from __future__ import annotations

import os


class Config:
    def __init__(self) -> None:
        self.bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.allowed_username: str = os.environ.get("TELEGRAM_ALLOWED_USERNAME", "")
        # Numeric user id, resolved after first successful verification and then
        # trusted as an additional authorization key.
        self.allowed_user_id: str = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
        self.trueforge_url: str = os.environ.get("TRUEFORGE_URL", "http://trueforge:8790")
        self.agent_name: str = os.environ.get("TRUEFORGE_AGENT", "drhiro")
        self.poll_timeout: int = int(os.environ.get("POLL_TIMEOUT", "30"))
        self.apk_dir: str = os.environ.get("APK_DIR", "/data/apk")
        self.apk_max_size_mb: int = int(os.environ.get("APK_MAX_SIZE_MB", "45"))
        self.pairing_state_dir: str = os.environ.get("PAIRING_STATE_DIR", "/data/pairing")
        self.pairing_ttl: int = int(os.environ.get("PAIRING_TTL_SECONDS", "600"))
        self.pairing_http_host: str = os.environ.get("PAIRING_HTTP_HOST", "0.0.0.0")
        self.pairing_http_port: int = int(os.environ.get("PAIRING_HTTP_PORT", "8091"))
        self.server_public_url: str = os.environ.get("DRHIRO_PUBLIC_URL", "")
        self.pairing_service_token: str = os.environ.get("PAIRING_SERVICE_TOKEN", "")
        self.allow_http_lan: bool = os.environ.get("PAIRING_ALLOW_HTTP_LAN", "true").lower() == "true"
        self.debug: bool = os.environ.get("DRHIRO_DEBUG", "false").lower() == "true"

        # --- T1 trusted ingress ------------------------------------------------
        # Where the trusted ingestion worker lives (the drHiro API).
        self.ingress_api_url: str = os.environ.get("DRHIRO_API_URL", "http://drhiro-api:8080")
        # Shared secret used to sign trusted events. Absent => trusted path off.
        self.ingress_secret: str = os.environ.get("TELEGRAM_INGRESS_SECRET", "")
        # When true, the trusted worker owns Telegram consumption writes, and the
        # bridge routes consumption-eligible turns through it.
        self.telegram_ingress_enabled: bool = (
            os.environ.get("TELEGRAM_INGRESS_ENABLED", "false").lower() == "true"
        )
        # Verified bot identity (Telegram getMe.id). Resolved at startup when not
        # configured; provisioning should pin it in trusted configuration.
        self.telegram_bot_id: str = os.environ.get("TELEGRAM_BOT_ID", "")

    def validate(self) -> list[str]:
        """Return a list of missing required settings (empty = valid)."""
        missing: list[str] = []
        if not self.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_username:
            missing.append("TELEGRAM_ALLOWED_USERNAME")
        if not self.trueforge_url:
            missing.append("TRUEFORGE_URL")
        return missing
