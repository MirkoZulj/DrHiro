"""Application configuration.

All non-secret settings come from environment variables prefixed DRHIRO_
(or .env). Secrets (DB password, tokens) also come from env, never from
committed files. See infra/.env.example for the full list.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRHIRO_", env_file=".env", extra="ignore")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        import sys as _sys
        in_test = (
            _sys.argv[0].endswith(("pytest", "py.test"))
            or "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("DRHIRO_ENV") == "test"
        )
        if not in_test and not self.jwt_secret:
            raise RuntimeError(
                "DRHIRO_JWT_SECRET is required in non-test environments. "
                "An empty/default secret would allow token forgery."
            )

    app_name: str = "drHiro Core API"
    version: str = "0.5.0"
    debug: bool = False

    database_url: str = "postgresql+psycopg://drhiro:drhiro@localhost:5432/drhiro"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = ""
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    telegram_bot_token: str = ""  # used to validate Mini App initData

    max_batch_size: int = 500
    max_photo_mb: int = 20

    # OpenClaw service identity (signed tool calls)
    openclaw_service_token: str = ""

    # --- T1 trusted Telegram ingress ---------------------------------------
    # Shared secret the telegram-bridge uses to sign trusted events. Absent =>
    # the ingress endpoint fails closed.
    telegram_ingress_secret: str = ""
    # The VERIFIED bot identity (Telegram getMe.id), established by provisioning.
    # Absent/ambiguous => the ingress endpoint fails closed.
    telegram_bot_id: str = ""
    # When true, the trusted worker owns Telegram consumption writes.
    telegram_ingress_enabled: bool = False
    # When false, the legacy model-driven consumption writers are closed so the
    # conversational model cannot log a consumption by itself. Manual and
    # other authenticated callers keep their explicit idempotency contract.
    legacy_consumption_writers_enabled: bool = True

    # LLM for food-rule extraction (OpenAI-compatible endpoint)
    llm_api_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen/qwen-2.5-72b-instruct"

    miniapp_allowed_origins: list[str] = ["https://t.me"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
