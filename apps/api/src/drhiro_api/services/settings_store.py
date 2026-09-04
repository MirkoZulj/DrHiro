"""Runtime settings store — instance-global configuration.

`.env` is BOOTSTRAP ONLY. On first boot the app_settings singleton row is
seeded from environment variables; after that the row is the source of truth
for settings editable from the web Settings screen.

Secrets (ai_api_key, telegram_bot_token) are write-only through the API: a
read returns a masked set/not-set indicator, never the stored secret. This
module never logs secret values.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from drhiro_api.models import AppSetting

# Fields a deployer may edit from the Settings screen, and whether each is a
# secret (never returned in full by the API).
EDITABLE_FIELDS = {
    "ai_backend_url": False,
    "model_name": False,
    "ai_api_key": True,
    "telegram_bot_token": True,
    "telegram_allowed_username": False,
}

SINGLETON_ID = "singleton"


def get_row(db: Session) -> AppSetting:
    """Return the singleton settings row, creating + seeding it if absent."""
    row = db.get(AppSetting, SINGLETON_ID)
    if row is None:
        row = AppSetting(id=SINGLETON_ID)
        db.add(row)
        db.flush()
    return row


def seed_from_env(db: Session, env: dict) -> bool:
    """Seed the singleton row from .env values (first-boot bootstrap).

    Called once at startup (or install). Only fills fields that are still
    NULL so it never overwrites a value the user has since set from the UI.
    Returns True if any field was seeded.
    """
    row = get_row(db)
    mappings = {
        "ai_backend_url": env.get("AI_BACKEND_BASE_URL"),
        "model_name": env.get("AI_MODEL"),
        "ai_api_key": env.get("AI_API_KEY"),
        "telegram_bot_token": env.get("TELEGRAM_BOT_TOKEN"),
        "telegram_allowed_username": env.get("TELEGRAM_ALLOWED_USERNAME"),
    }
    changed = False
    for field, value in mappings.items():
        if value is None or value == "":
            continue
        if getattr(row, field) is None:
            setattr(row, field, value)
            changed = True
    if changed:
        db.commit()
    return changed


def _masked(has_value: bool) -> dict:
    return {"set": has_value}


def to_masked_dict(row: AppSetting | None) -> dict:
    """Serialize the settings row for the API with secrets masked.

    Non-secret fields are returned in full; secret fields return a
    ``{"set": true|false}`` indicator only. Never return the stored secret.
    """
    if row is None:
        return {f: ("" if not secret else {"set": False}) for f, secret in EDITABLE_FIELDS.items()}
    out = {}
    for field, secret in EDITABLE_FIELDS.items():
        val = getattr(row, field, None)
        if secret:
            out[field] = _masked(bool(val))
        else:
            out[field] = val or ""
    return out


def apply_updates(db: Session, updates: dict) -> AppSetting:
    """Apply non-secret-field updates and secret-field set/clear semantics.

    For a secret field the payload value is either ``{"set": true,
    "value": "..."}`` (write the new secret) or ``{"set": false}`` (clear it).
    A plain non-empty string on a non-secret field writes it directly. A null
    or empty string on a non-secret field clears it.

    Returns the updated row (uncommitted; caller commits).
    """
    row = get_row(db)
    for field, secret in EDITABLE_FIELDS.items():
        if field not in updates:
            continue
        payload = updates[field]
        if secret:
            if isinstance(payload, dict):
                if payload.get("set") is True and payload.get("value"):
                    setattr(row, field, payload["value"])
                elif payload.get("set") is False:
                    setattr(row, field, None)
            # A raw string on a secret field is rejected by the router schema;
            # ignore defensively here.
            continue
        # Non-secret: accept a string (empty clears).
        if isinstance(payload, str):
            setattr(row, field, payload if payload else None)
    return row
