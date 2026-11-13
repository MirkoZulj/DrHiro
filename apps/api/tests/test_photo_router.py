"""Photo routing gate tests (0.12.0-i).

The vision classifier is STUBBED in every test — no live model calls. These
tests pin the ROUTING decision only:

  * a confident food/meal batch -> ONE meal draft, NO encounter/record
  * a medication/supplement label -> medical encounter + record (label flow)
  * a medical document -> medical encounter + record
  * classifier failure / 'other' -> medical path (fail-safe)
  * replay with the same batch_key returns the SAME routing decision and
    creates nothing new
  * DRHIRO_PHOTO_ROUTER_ENABLED=false -> always the medical path

SYNTHETIC DOCUMENT CONTENT ONLY — this is a public repo; never store real
medical text here.

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

import pytest

import drhiro_api.services.photo_router as photo_router
import drhiro_api.routers.openclaw_tools as ot
from drhiro_api.models import IngestBatch, Meal, MedicalEncounter, MedicalRecord
from drhiro_api.security import create_service_token
from tests.conftest import make_user

TG = "700900900"

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050201f34a24d5"
    "0000000049454e44ae426082"
)


@pytest.fixture()
def service_headers(client, db):
    make_user(db, "Photo Router Tester", TG)
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": TG}


@pytest.fixture()
def scans_dir(tmp_path, monkeypatch):
    from drhiro_api import config
    d = tmp_path / "medical_scans"
    monkeypatch.setattr(config.get_settings(), "medical_scans_dir", str(d))
    yield d


def _stub(monkeypatch, verdict: dict | None):
    """Replace the classifier. ``verdict=None`` means 'raise'.

    Patch BOTH the defining module and the router's bound name: openclaw_tools
    imports ``classify_batch`` directly, so patching only photo_router would not
    be seen by the endpoint.
    """
    if verdict is None:
        def boom(images, caption=None):
            raise RuntimeError("model unreachable")
        monkeypatch.setattr(photo_router, "classify_batch", boom)
        monkeypatch.setattr(ot, "classify_batch", boom)
    else:
        def fake(images, caption=None):
            return dict(verdict)
        monkeypatch.setattr(photo_router, "classify_batch", fake)
        monkeypatch.setattr(ot, "classify_batch", fake)


def _batch(client, headers, key, n=2, caption=None):
    files = [("files", (f"page{i}.png", PNG, "image/png")) for i in range(n)]
    data = {"batch_key": key}
    if caption:
        data["caption"] = caption
    return client.post("/api/v1/tools/ingest_medical_batch", files=files, data=data, headers=headers)


def _counts(db):
    return (
        db.query(Meal).count(),
        db.query(MedicalEncounter).count(),
        db.query(MedicalRecord).count(),
    )


# (a) food_meal at high confidence -> meal draft, no encounter/record
def test_food_meal_routes_to_meal_draft(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "food_meal", "confidence": 0.95, "reason": "plate of food"})
    r = _batch(client, service_headers, "route-food-1", n=3, caption="sarma i kupus")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "meal"
    assert data["deduplicated"] is False
    assert data["photo_count"] == 3
    assert data["status"] == "needs_review"
    assert len(data["scan_paths"]) == 3
    for rel in data["scan_paths"]:
        assert (scans_dir / rel).is_file(), rel

    meals, encounters, records = _counts(db)
    assert (meals, encounters, records) == (1, 0, 0)
    meal = db.query(Meal).first()
    assert str(meal.id) == data["meal_id"]
    assert meal.status == "needs_review"
    assert meal.input_method == "photo"
    assert meal.meal_type is None
    assert meal.photo_asset_id == data["scan_paths"][0]
    assert meal.confidence == 0.0
    assert meal.notes == "sarma i kupus"
    assert meal.user_id is not None
    assert meal.eaten_at is not None

    # The message tells the agent a meal draft exists (and not to file a record)
    assert "meal" in (r.json()["message"] or "").lower()
    assert "meal_id=" in (r.json()["message"] or "")


# (a2) food_meal BELOW the threshold -> stays medical (fail-safe)
def test_food_meal_below_threshold_stays_medical(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "food_meal", "confidence": 0.5, "reason": "unsure"})
    r = _batch(client, service_headers, "route-food-low", n=1)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "medical"
    assert data["router_category"] == "food_meal"
    assert db.query(Meal).count() == 0
    assert db.query(MedicalRecord).count() == 1


# (b) medication_label -> medical encounter + record (label flow preserved)
def test_medication_label_stays_medical(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "medication_label", "confidence": 0.98, "reason": "supplement facts"})
    r = _batch(client, service_headers, "route-medlabel-1", n=2)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "medical"
    assert data["router_category"] == "medication_label"
    meals, encounters, records = _counts(db)
    assert (meals, encounters, records) == (0, 1, 1)
    rec = db.query(MedicalRecord).first()
    assert str(rec.id) == data["record_id"]
    assert rec.source_type == "telegram_photo_batch"
    assert "transcription pending" in rec.content
    assert str(rec.encounter_id) == data["encounter_id"]


# (c) medical_document -> medical path
def test_medical_document_stays_medical(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "medical_document", "confidence": 0.99, "reason": "lab report"})
    r = _batch(client, service_headers, "route-doc-1", n=4)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "medical"
    assert data["encounter_id"] and data["record_id"]
    assert (db.query(Meal).count(), db.query(MedicalEncounter).count(), db.query(MedicalRecord).count()) == (0, 1, 1)


# (d) classifier failure / 'other' -> medical path (fail-safe)
def test_classifier_failure_falls_back_to_medical(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, None)  # raises
    r = _batch(client, service_headers, "route-fail-1", n=2)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "medical"
    assert (db.query(Meal).count(), db.query(MedicalEncounter).count(), db.query(MedicalRecord).count()) == (0, 1, 1)


def test_other_category_falls_back_to_medical(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "other", "confidence": 0.0, "reason": "classification failed"})
    r = _batch(client, service_headers, "route-other-1", n=1)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["routed"] == "medical"
    assert db.query(Meal).count() == 0
    assert db.query(MedicalRecord).count() == 1


# (e) replay with the same batch_key returns the same meal_id, creates nothing
def test_replay_meal_returns_same_meal_and_creates_nothing(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "food_meal", "confidence": 0.95, "reason": "plate of food"})
    r1 = _batch(client, service_headers, "route-replay-1", n=2)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()["data"]
    assert d1["routed"] == "meal"
    meals_before, _, _ = _counts(db)

    # Even with the classifier now disagreeing, the stored decision wins.
    _stub(monkeypatch, {"category": "medical_document", "confidence": 0.99, "reason": "now a doc"})
    r2 = _batch(client, service_headers, "route-replay-1", n=2)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()["data"]
    assert d2["deduplicated"] is True
    assert d2["routed"] == "meal"
    assert d2["meal_id"] == d1["meal_id"]
    assert d2["scan_paths"] == d1["scan_paths"]
    assert _counts(db) == (meals_before, 0, 0)
    assert db.query(MedicalRecord).count() == 0
    assert db.query(IngestBatch).count() == 1
    # The routing decision is persisted in the idempotency row
    batch = db.query(IngestBatch).first()
    assert batch.result_json["routed"] == "meal"
    assert batch.result_json["meal_id"] == d1["meal_id"]


def test_replay_medical_returns_same_record(client, service_headers, scans_dir, db, monkeypatch):
    _stub(monkeypatch, {"category": "medical_document", "confidence": 0.99, "reason": "lab report"})
    r1 = _batch(client, service_headers, "route-replay-med", n=2)
    d1 = r1.json()["data"]
    r2 = _batch(client, service_headers, "route-replay-med", n=2)
    d2 = r2.json()["data"]
    assert d2["deduplicated"] is True
    assert d2["routed"] == "medical"
    assert d2["record_id"] == d1["record_id"]
    assert db.query(MedicalRecord).count() == 1


# (f) DRHIRO_PHOTO_ROUTER_ENABLED=false -> always medical path
def test_router_disabled_always_medical(client, service_headers, scans_dir, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_PHOTO_ROUTER_ENABLED", "false")
    _stub(monkeypatch, {"category": "food_meal", "confidence": 0.99, "reason": "plate of food"})
    r = _batch(client, service_headers, "route-disabled-1", n=2)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["routed"] == "medical"
    assert data["router_category"] is None
    assert (db.query(Meal).count(), db.query(MedicalEncounter).count(), db.query(MedicalRecord).count()) == (0, 1, 1)


# --------------------------------------------------------------------------- #
# classify_batch itself (pure parsing / fail-safe, no model)
# --------------------------------------------------------------------------- #
def test_classify_batch_no_images_is_other():
    out = photo_router.classify_batch([], None)
    assert out == {"category": "other", "confidence": 0.0, "reason": "classification failed"}


def test_classify_batch_unparseable_reply_is_other(monkeypatch):
    from drhiro_api import config
    monkeypatch.setattr(config.get_settings(), "llm_api_key", "test-key")
    import drhiro_api.services.llm_client as llm
    monkeypatch.setattr(llm, "chat_complete_sync", lambda *a, **k: "not json at all")
    out = photo_router.classify_batch([PNG], None)
    assert out == {"category": "other", "confidence": 0.0, "reason": "classification failed"}


def test_classify_batch_strips_code_fence(monkeypatch):
    from drhiro_api import config
    monkeypatch.setattr(config.get_settings(), "llm_api_key", "test-key")
    import drhiro_api.services.llm_client as llm

    reply = '```json\n{"category": "food_meal", "confidence": 0.93, "reason": "pasta"}\n```'
    monkeypatch.setattr(llm, "chat_complete_sync", lambda *a, **k: reply)
    out = photo_router.classify_batch([PNG], None)
    assert out["category"] == "food_meal"
    assert out["confidence"] == pytest.approx(0.93)


def test_classify_batch_without_api_key_is_other(monkeypatch):
    from drhiro_api import config
    monkeypatch.setattr(config.get_settings(), "llm_api_key", "")
    out = photo_router.classify_batch([PNG], None)
    assert out["category"] == "other"


def test_router_env_defaults(monkeypatch):
    monkeypatch.delenv("DRHIRO_PHOTO_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("DRHIRO_PHOTO_ROUTER_THRESHOLD", raising=False)
    assert photo_router.router_enabled() is True
    assert photo_router.router_threshold() == pytest.approx(0.8)
    monkeypatch.setenv("DRHIRO_PHOTO_ROUTER_ENABLED", "false")
    assert photo_router.router_enabled() is False
    monkeypatch.setenv("DRHIRO_PHOTO_ROUTER_THRESHOLD", "0.6")
    assert photo_router.router_threshold() == pytest.approx(0.6)
