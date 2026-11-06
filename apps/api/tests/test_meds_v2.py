"""0.11.0-a meds v2 tests (spec §6 meds)."""
from __future__ import annotations
import uuid
from datetime import date, datetime, timezone
from tests.conftest import auth_headers
from drhiro_api.services.schemas_meds import normalise_ingredient, suggest_nutrient_code

def test_legacy_normaliser_exclusions():
    assert suggest_nutrient_code("calcium pantothenate") is None
    assert suggest_nutrient_code("magnesium stearate") is None
    assert suggest_nutrient_code("omega-6") is None

def test_normalise_legacy_per2_halved():
    e = normalise_ingredient({"component": "Zinc", "per_2_mg": 30})
    assert e.amount == 15 and e.unit == "mg" and e.nutrient_code == "zinc"

def test_unknown_code_422(client, db, user_a):
    r = client.post("/api/v1/medications", json={"kind": "supplement", "name": "X",
        "ingredients_json": [{"name": "Zinc", "nutrient_code": "bogus", "amount": 5, "unit": "mg"}]},
        headers=auth_headers(user_a))
    assert r.status_code == 422, r.text

def _create(client, user, **kw):
    payload = {"kind": "supplement", "name": "Vitamin D3", "dose_amount": 2, "dose_unit": "capsule", "unit_label": "capsule"}
    payload.update(kw)
    return client.post("/api/v1/medications", json=payload, headers=auth_headers(user))

def test_create_reminder_ignored_with_warning(client, db, user_a):
    r = _create(client, user_a, create_reminder=True, schedule_json={"type": "daily", "times": ["08:00"]})
    assert r.status_code in (200, 201), r.text
    assert r.json()["schedule_json"] is None
    assert "create_reminder is ignored" in r.json()["warnings"]

def test_pause_410_and_unarchive(client, db, user_a):
    item_id = _create(client, user_a).json()["id"]
    assert client.post(f"/api/v1/medications/{item_id}/pause", json={}, headers=auth_headers(user_a)).status_code == 410
    client.post(f"/api/v1/medications/{item_id}/stop", json={}, headers=auth_headers(user_a))
    assert db.query(__import__("drhiro_api.models", fromlist=["MedicationItem"]).MedicationItem).filter_by(id=uuid.UUID(item_id)).first().status == "stopped"
    r = client.post(f"/api/v1/medications/{item_id}/unarchive", json={}, headers=auth_headers(user_a))
    assert r.json()["status"] == "active"

def test_intake_only_days_and_bulk_idempotency(client, db, user_a):
    r = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a))
    assert r.json()["days"] == [] and r.json()["items"] == []
    item_id = _create(client, user_a).json()["id"]
    payload = {"entries": [{"item_id": item_id, "amount": 2}]}
    r = client.post("/api/v1/medications/intake", json=payload, headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    assert len(r.json()["doses"]) == 1 and r.json()["doses"][0]["scheduled_for"] is None
    r2 = client.post("/api/v1/medications/intake", json=payload, headers=auth_headers(user_a))
    assert r2.json()["doses"] == [] and item_id in r2.json()["already_logged"]
    # 21 entries -> 422
    too_many = {"entries": [{"item_id": item_id} for _ in range(21)]}
    assert client.post("/api/v1/medications/intake", json=too_many, headers=auth_headers(user_a)).status_code == 422
    r = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a))
    assert len(r.json()["days"]) == 1 and r.json()["items"][0]["total_amount"] == 2

def test_composition_serving_units_halved(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[{"name": "Zinc", "nutrient_code": "zinc", "amount": 20, "unit": "mg", "per": "serving", "serving_units": 2}]).json()["id"]
    r = client.get(f"/api/v1/medications/{item_id}/composition?scope=unit", headers=auth_headers(user_a))
    assert r.json()["rows"][0]["per_unit"] == 10

def test_patch_dose(client, db, user_a):
    item_id = _create(client, user_a).json()["id"]
    dose = client.post("/api/v1/medications/intake", json={"entries": [{"item_id": item_id}]}, headers=auth_headers(user_a)).json()["doses"][0]
    r = client.patch(f"/api/v1/medications/doses/{dose['id']}", json={"amount": 3}, headers=auth_headers(user_a))
    assert r.json()["amount"] == 3

def test_medical_record_patch(client, db, user_a):
    # create via tools needs service token; use JWT? medical records have no JWT create, only PATCH — create direct DB
    from drhiro_api.models import MedicalRecord
    rec = MedicalRecord(user_id=user_a.id, title="Krvna kartica - zapis 2000", content="Eye exam synthetic")
    db.add(rec); db.commit(); db.refresh(rec)
    r = client.patch(f"/api/v1/medical/records/{rec.id}", json={"title": "Eye exam"}, headers=auth_headers(user_a))
    assert r.json()["title"] == "Eye exam"
    r2 = client.patch(f"/api/v1/medical/records/{rec.id}", json={"content": "Corrected text"}, headers=auth_headers(user_a))
    assert r2.json()["content"] == "Corrected text"
