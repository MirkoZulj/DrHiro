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
        self.trueforge_url: str = os.environ.get("TRUEFORGE_URL", "http://trueforge:8790")
        self.agent_name: str = os.environ.get("TRUEFORGE_AGENT", "drhiro")
        self.poll_timeout: int = int(os.environ.get("POLL_TIMEOUT", "30"))
        self.debug: bool = os.environ.get("DRHIRO_DEBUG", "false").lower() == "true"

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
