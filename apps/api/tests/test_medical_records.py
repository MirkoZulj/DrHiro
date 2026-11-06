"""Tests for the medical records tools (/tools/add_medical_record etc.).

SYNTHETIC DOCUMENT CONTENT ONLY — this is a public repo; never store real
medical text here.
"""

from __future__ import annotations

import uuid

import pytest

from drhiro_api.models import MedicalRecord, User

from tests.conftest import make_user

SERVICE_TOKEN = "test-service-token"
TG = "700100200"

SYNTHETIC_LAB = (
    "SYNTHETIC LAB REPORT (test fixture)\n"
    "Hemoglobin: 145 g/L\n"
    "Glucose: 5.2 mmol/L\n"
    "Collected 2026-05-11"
)


@pytest.fixture()
def service_headers(client, db):
    """Pair a user and mint a signed service token (same pattern as activity tools)."""
    from drhiro_api.security import create_service_token
    make_user(db, "Medical Records Tester", TG)
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": TG}


def test_add_and_get_back(client, service_headers):
    r = client.post(
        "/api/v1/tools/add_medical_record",
        json={"title": "Test lab panel", "content": SYNTHETIC_LAB, "recorded_at": "2026-05-12"},
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["title"] == "Test lab panel"
    assert "Hemoglobin" in data["content"]
    assert data["source_type"] == "telegram_photo"
    assert data["recorded_at"].startswith("2026-05-12")
    # recorded_at was naive -> resolved in the user's timezone (aware)
    assert "+00:00" in data["recorded_at"] or "+" in data["recorded_at"] or "Z" in data["recorded_at"]
    assert data["id"]


def test_add_requires_content(client, service_headers):
    r = client.post(
        "/api/v1/tools/add_medical_record",
        json={"content": "   "},
        headers=service_headers,
    )
    assert r.status_code == 400


def test_empty_title_becomes_null(client, service_headers):
    r = client.post(
        "/api/v1/tools/add_medical_record",
        json={"content": SYNTHETIC_LAB, "title": "  "},
        headers=service_headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["title"] is None


def test_list_and_search(client, service_headers):
    for title, content in [
        ("Panel A", SYNTHETIC_LAB),
        ("Prescription", "SYNTHETIC prescription: Amoxicillin 500 mg, three times daily"),
        ("Letter", "SYNTHETIC letter from Dr. Testovic, cardiology follow-up"),
    ]:
        r = client.post(
            "/api/v1/tools/add_medical_record",
            json={"title": title, "content": content},
            headers=service_headers,
        )
        assert r.status_code == 200

    r = client.post("/api/v1/tools/list_medical_records", json={}, headers=service_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["count"] == 3
    # newest first
    titles = [rec["title"] for rec in data["records"]]
    assert titles[0] == "Letter"

    r = client.post("/api/v1/tools/list_medical_records", json={"query": "amoxicillin"}, headers=service_headers)
    data = r.json()["data"]
    assert data["count"] == 1
    assert data["records"][0]["title"] == "Prescription"

    # search hits content too
    r = client.post("/api/v1/tools/list_medical_records", json={"query": "cardiology"}, headers=service_headers)
    assert r.json()["data"]["count"] == 1


def test_list_limit(client, service_headers):
    for i in range(7):
        r = client.post(
            "/api/v1/tools/add_medical_record",
            json={"content": f"SYNTHETIC record {i}"},
            headers=service_headers,
        )
        assert r.status_code == 200
    r = client.post("/api/v1/tools/list_medical_records", json={"limit": 3}, headers=service_headers)
    assert r.json()["data"]["count"] == 3


def test_delete(client, service_headers, db):
    user = db.query(User).filter(User.id.isnot(None)).first()
    rec = MedicalRecord(user_id=user.id, content=SYNTHETIC_LAB, source_type="telegram_photo")
    db.add(rec)
    db.commit()
    rid = str(rec.id)

    r = client.post(
        "/api/v1/tools/delete_medical_record",
        json={"record_id": rid},
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted"] is True
    assert db.query(MedicalRecord).count() == 0


def test_delete_scoped_to_owner(client, service_headers, db):
    """Another user's record id must 404 — no cross-user reads/deletes."""
    other = User(display_name="Other User")
    db.add(other)
    db.flush()
    rec = MedicalRecord(user_id=other.id, content="SYNTHETIC other-user record")
    db.add(rec)
    db.commit()

    r = client.post(
        "/api/v1/tools/delete_medical_record",
        json={"record_id": str(rec.id)},
        headers=service_headers,
    )
    assert r.status_code == 404

    r = client.post("/api/v1/tools/list_medical_records", json={"query": "other-user"}, headers=service_headers)
    assert r.json()["data"]["count"] == 0


def test_delete_bad_id(client, service_headers):
    r = client.post(
        "/api/v1/tools/delete_medical_record",
        json={"record_id": str(uuid.uuid4())},
        headers=service_headers,
    )
    assert r.status_code == 404

    r = client.post(
        "/api/v1/tools/delete_medical_record",
        json={"record_id": "not-a-uuid"},
        headers=service_headers,
    )
    assert r.status_code == 404


def test_requires_service_auth(client):
    r = client.post("/api/v1/tools/list_medical_records", json={})
    assert r.status_code == 401


# ----------------------------------------------------------------
# Card 8 regression (Kim qa-title-bug.md): stale encounter title
# must not shadow the corrected record title in the single-record case.
# SYNTHETIC content only.
# ----------------------------------------------------------------

def test_update_single_record_propagates_title_to_encounter(client, service_headers):
    """Stage with stale title A, enrich to B -> encounter headline == B."""
    # stage encounter with stale title
    r = client.post(
        "/api/v1/tools/add_medical_encounter",
        json={"title": "Krvna kartica - zapis 2000", "encounter_date": "2026-05-11"},
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    eid = r.json()["data"]["id"]
    # stage single record under it with same stale title (ingest_batch pattern)
    r = client.post(
        "/api/v1/tools/add_medical_record",
        json={"title": "Krvna kartica - zapis 2000", "content": "SYNTHETIC PLACEHOLDER", "encounter_id": eid},
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    rid = r.json()["data"]["id"]
    # enrich record to real title (tool path)
    r = client.post(
        "/api/v1/tools/update_medical_record",
        json={"record_id": rid, "title": "Eye exam", "content": "SYNTHETIC EYE EXAM\nVisual acuity: 1.0"},
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["title"] == "Eye exam"
    # encounter headline (what Records.tsx renders) must follow
    r = client.post("/api/v1/tools/get_medical_encounter", json={"encounter_id": eid}, headers=service_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["encounter"]["title"] == "Eye exam", data["encounter"]
    assert data["records"][0]["title"] == "Eye exam"
