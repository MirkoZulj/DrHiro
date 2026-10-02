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
            # `python -m pytest` leaves argv[0] pointing at the pytest package, so
            # the argv test above misses it and the guard fires during COLLECTION,
            # before PYTEST_CURRENT_TEST exists. Checking the imported module is
            # reliable at import time and costs nothing in production, where
            # pytest is never imported.
            or "pytest" in _sys.modules
        )
        if not in_test and not self.jwt_secret:
            raise RuntimeError(
                "DRHIRO_JWT_SECRET is required in non-test environments. "
                "An empty/default secret would allow token forgery."
            )
        if not in_test and (
            len(self.jwt_secret) < 32 or self.jwt_secret.startswith("change-me")
        ):
            # Filling only the five installer inputs from .env.example leaves the
            # documented placeholder in place, and the API would otherwise boot
            # happily signing tokens with a value published in this repository.
            # Refuse anything short or placeholder-shaped: fail loudly rather
            # than run insecurely.
            raise RuntimeError(
                f"DRHIRO_JWT_SECRET is too weak ({len(self.jwt_secret)} chars; 32 "
                "minimum) or is still the .env.example placeholder. A publicly "
                "known secret allows token forgery. install.sh generates a strong "
                "one; otherwise set a random value, e.g. "
                "head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \\n'"
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

    # Two thresholds. Names and defaults deliberately mirror the production
    # deployment, so an operator moving DRHIRO_JEV_* values between the two
    # cannot set the wrong bound:
    #   score >= jev_threshold                 -> accept and log        (0.88)
    #   jev_review_floor <= score < threshold  -> surface, ask the user (0.5)
    #   score <  jev_review_floor              -> reject, next candidate
    jev_threshold: float = 0.88       # env DRHIRO_JEV_THRESHOLD (the accept bar)
    jev_review_floor: float = 0.5     # env DRHIRO_JEV_REVIEW_FLOOR (lower bound)

    @property
    def jev_enabled(self) -> bool:
        """True when both the Jev URL and key are configured."""
        return bool(self.jev_api_url and self.jev_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
