"""Application configuration.

All non-secret settings come from environment variables prefixed DRHIRO_
(or .env). Secrets (DB password, tokens) also come from env, never from
committed files. See infra/.env.example for the full list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRHIRO_", env_file=".env", extra="ignore")

    app_name: str = "drHiro Core API"
    version: str = "0.9.0"
    debug: bool = False

    database_url: str = "postgresql+psycopg://drhiro:drhiro@localhost:5432/drhiro"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-in-production"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    telegram_bot_token: str = ""  # used to validate Mini App initData

    max_batch_size: int = 500
    max_photo_mb: int = 20

    # OpenClaw service identity (signed tool calls)
    openclaw_service_token: str = ""

    # Shared secret nginx injects on the Google-gated session location so the
    # API only trusts a Google identity that actually came through nginx.
    web_google_secret: str = ""

    # LLM for food validation -- Qwen on Windows
    llm_api_url: str = "http://100.119.202.68:8000/v1"
    llm_api_key: str = ""
    llm_model: str = "E:\Models\Qwen3.8-27B-UD-Q4_K_M.gguf"

    # Jev (TypeSafe System One) food-match verification.
    # Calibrated on this workload: correct matches score 0.88-0.99, wrong
    # matches 0.01-0.32. Threshold is 0.88 (>0.87, user-set).
    jev_api_url: str = "https://api.typesafe.ai"
    jev_api_key: str = ""
    jev_model: str = "jev-latest"
    jev_threshold: float = 0.88
    # Candidates scoring in [jev_review_floor, jev_threshold) are offered to
    # the user as a multiple-choice question instead of being accepted or dropped.
    jev_review_floor: float = 0.5
    # Ledger classification guard (services/ledger_classifier.py). When True, a
    # free-text log is classified into one of six ledgers before writing and a
    # confident disagreement blocks the write with a "which did you mean?"
    # payload. Fail-open: when Jev is unconfigured/unreachable, logging behaves
    # exactly as it did before the guard existed. One flag disables it.
    jev_ledger_guard_enabled: bool = True

    miniapp_allowed_origins: list[str] = ["https://t.me"]

    # Where stored medical document scans live (medical.py stores
    # <dir>/scans/<user_id>/<uuid>.<ext> and medical_records.source_image_path
    # holds the path relative to this directory).
    medical_scans_dir: str = "/opt/drhiro/data/medical_scans"

    # Generated artefacts (PDF reports, export bundles). Same data dir as scans.
    generated_files_dir: str = "/opt/drhiro/data/generated"
    # System backups (pg dumps, file tars, manifests). Mounted into the worker only.
    backups_dir: str = "/var/backups/drhiro"
    # GPG recipient for encrypted backups (env DRHIRO_GPG_RECIPIENT is what the
    # backup script reads directly; this mirrors it for the API/worker surface).
    backup_gpg_recipient: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
