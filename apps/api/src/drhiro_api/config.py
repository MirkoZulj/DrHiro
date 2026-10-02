"""Application configuration.

All non-secret settings come from environment variables prefixed DRHIRO_
(or .env). Secrets (DB password, tokens) also come from env, never from
committed files. See infra/.env.example for the full list.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AliasChoices, Field
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

    # --- Jev (TypeSafe System One) food-match verification ------------------
    # English-first equivalence model answering ONE bounded `noul` question:
    # does candidate name X refer to the same food as the user's input Y?
    # Empty url or key => verification disabled, and food search behaves
    # exactly as if Jev were never configured (no translation, no HTTP call).
    jev_api_url: str = Field(
        "",
        # Required env name is DRHIRO_JEV_URL; accept the field-derived
        # DRHIRO_JEV_API_URL too so either spelling works.
        validation_alias=AliasChoices("DRHIRO_JEV_URL", "DRHIRO_JEV_API_URL"),
    )
    jev_api_key: str = ""       # env DRHIRO_JEV_API_KEY ("" disables the feature)
    jev_model: str = "jev-latest"

    # Two thresholds, both configurable. score >= jev_accept_threshold => the
    # candidate is accepted silently. jev_threshold <= score < accept threshold
    # => surfaced but NOT auto-accepted; the user is asked. score below
    # jev_threshold => rejected, the cascade falls through to the next source.
    jev_threshold: float = 0.5
    jev_accept_threshold: float = 0.88

    @property
    def jev_enabled(self) -> bool:
        """True when both the Jev URL and key are configured."""
        return bool(self.jev_api_url and self.jev_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
