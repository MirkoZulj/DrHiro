"""Unit tests for the settings store masking + update semantics (no DB)."""
from __future__ import annotations

import pytest

from drhiro_api.models import AppSetting
from drhiro_api.services.settings_store import EDITABLE_FIELDS, apply_updates, to_masked_dict


def _row(**kw) -> AppSetting:
    row = AppSetting(id="singleton")
    for k, v in kw.items():
        setattr(row, k, v)
    return row


def test_masked_dict_hides_secrets():
    row = _row(ai_backend_url="http://api:8000/v1", model_name="qwen",
               ai_api_key="super-secret-key", telegram_bot_token="123:ABC",
               telegram_allowed_username="alice")
    out = to_masked_dict(row)
    # Non-secret fields in full
    assert out["ai_backend_url"] == "http://api:8000/v1"
    assert out["model_name"] == "qwen"
    assert out["telegram_allowed_username"] == "alice"
    # Secret fields masked
    assert out["ai_api_key"] == {"set": True}
    assert out["telegram_bot_token"] == {"set": True}
    assert "super-secret-key" not in str(out)
    assert "123:ABC" not in str(out)


def test_masked_dict_unset_secrets():
    row = _row()
    out = to_masked_dict(row)
    assert out["ai_api_key"] == {"set": False}
    assert out["telegram_bot_token"] == {"set": False}
    assert out["model_name"] == ""


def test_empty_row_serialization():
    out = to_masked_dict(None)
    for f, secret in EDITABLE_FIELDS.items():
        if secret:
            assert out[f] == {"set": False}
        else:
            assert out[f] == ""


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    def get(self, model, key):
        return None

    def commit(self):
        self.committed = True

    def flush(self):
        pass


def test_apply_updates_secret_set_and_clear():
    db = _FakeSession()
    row = apply_updates(db, {
        "model_name": "new-model",
        "ai_api_key": {"set": True, "value": "new-secret"},
    })
    assert row.model_name == "new-model"
    assert row.ai_api_key == "new-secret"

    # Clear a secret
    row2 = apply_updates(db, {"ai_api_key": {"set": False}})
    assert row2.ai_api_key is None


def test_apply_updates_secret_rejects_no_value():
    db = _FakeSession()
    row = apply_updates(db, {"ai_api_key": {"set": True, "value": None}})
    assert row.ai_api_key is None


def test_apply_updates_non_secret_empty_clears():
    db = _FakeSession()
    row = apply_updates(db, {"ai_backend_url": ""})
    assert row.ai_backend_url is None


def test_resolve_runtime_store_first_env_fallback():
    from drhiro_api.services.settings_store import resolve_runtime

    # Store empty -> env used.
    db = _FakeSession()
    eff = resolve_runtime(db, {
        "AI_BACKEND_BASE_URL": "http://env:8000/v1",
        "AI_MODEL": "env-model",
        "AI_API_KEY": "env-key",
    })
    assert eff["ai_backend_url"] == "http://env:8000/v1"
    assert eff["model_name"] == "env-model"
    assert eff["ai_api_key"] == "env-key"


def test_resolve_runtime_prefers_store():
    from drhiro_api.models import AppSetting
    from drhiro_api.services.settings_store import SINGLETON_ID, resolve_runtime

    # A session whose get() returns a pre-seeded singleton row.
    seeded = AppSetting(id=SINGLETON_ID, model_name="store-model",
                        ai_backend_url="http://store:9000/v1", ai_api_key="store-key")

    class _StoreFake:
        def get(self, model, key):
            return seeded
        def add(self, o): pass
        def flush(self): pass
        def commit(self): pass

    db = _StoreFake()
    eff = resolve_runtime(db, {"AI_MODEL": "env-model", "AI_API_KEY": "env-key"})
    # Store wins over env for values that are set.
    assert eff["model_name"] == "store-model"
    assert eff["ai_backend_url"] == "http://store:9000/v1"
    assert eff["ai_api_key"] == "store-key"
