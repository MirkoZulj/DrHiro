"""Tests for encounter-centric medical records (v0.8.0).

SYNTHETIC DOCUMENT CONTENT ONLY — this is a public repo; never store real
medical text here.
"""

from __future__ import annotations

import uuid

import pytest

from drhiro_api.models import MedicalRecord, User

from tests.conftest import auth_headers, make_user

SERVICE_TOKEN = "test-service-token"
TG = "700300400"

SYNTHETIC_LAB = (
    "SYNTHETIC LAB REPORT (test fixture)\n"
    "Hemoglobin: 145 g/L\n"
    "Collected 2026-05-11"
)


@pytest.fixture()
def service_headers(client, db):
    from drhiro_api.security import create_service_token
    make_user(db, "Encounter Tester", TG)
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": TG}


@pytest.fixture()
def jwt_user(client, db):
    return make_user(db, "JWT Encounter User", None)


@pytest.fixture()
def scans_dir(tmp_path, monkeypatch):
    """Point the scan storage at a throwaway dir for this test."""
    from drhiro_api import config
    d = tmp_path / "medical_scans"
    monkeypatch.setattr(config.get_settings(), "medical_scans_dir", str(d))
    yield d


# ---------------------------------------------------------------- encounters

def test_add_and_list_encounters(client, service_headers):
    r = client.post("/api/v1/tools/add_medical_encounter", json={
        "encounter_date": "2026-05-11", "facility": "SYNTHETIC Lab", "title": "Blood panel",
    }, headers=service_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["encounter_date"] == "2026-05-11"
    assert data["facility"] == "SYNTHETIC Lab"
    assert data["record_count"] == 0

    r = client.post("/api/v1/tools/list_medical_encounters", json={}, headers=service_headers)
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["count"] == 1
    assert body["encounters"][0]["id"] == data["id"]
    assert body["encounters"][0]["record_count"] == 0


def test_add_encounter_bad_date(client, service_headers):
    r = client.post("/api/v1/tools/add_medical_encounter", json={"encounter_date": "not-a-date"},
                    headers=service_headers)
    assert r.status_code == 400


def test_add_record_with_unknown_encounter_refused(client, service_headers):
    r = client.post("/api/v1/tools/add_medical_record", json={
        "content": SYNTHETIC_LAB, "encounter_id": str(uuid.uuid4()),
    }, headers=service_headers)
    assert r.status_code == 404


def test_encounter_grouping_full_lifecycle(client, service_headers):
    # 1. create encounter
    r = client.post("/api/v1/tools/add_medical_encounter", json={
        "encounter_date": "2026-05-11", "facility": "SYNTHETIC Lab",
    }, headers=service_headers)
    eid = r.json()["data"]["id"]
    # 2. store two records inside it
    rec_ids = []
    for title in ("Panel A", "Panel B"):
        r = client.post("/api/v1/tools/add_medical_record", json={
            "title": title, "content": SYNTHETIC_LAB, "recorded_at": "2026-05-11",
            "encounter_id": eid,
        }, headers=service_headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["encounter_id"] == eid
        rec_ids.append(r.json()["data"]["id"])
    # 3. list shows record_count 2
    r = client.post("/api/v1/tools/list_medical_encounters", json={}, headers=service_headers)
    assert r.json()["data"]["encounters"][0]["record_count"] == 2
    # 4. get returns encounter + both records
    r = client.post("/api/v1/tools/get_medical_encounter", json={"encounter_id": eid},
                    headers=service_headers)
    body = r.json()["data"]
    assert body["encounter"]["id"] == eid
    assert body["encounter"]["record_count"] == 2
    assert sorted(x["id"] for x in body["records"]) == sorted(rec_ids)
    # 5. delete removes encounter AND records
    r = client.post("/api/v1/tools/delete_medical_encounter", json={"encounter_id": eid},
                    headers=service_headers)
    assert r.status_code == 200
    assert r.json()["data"]["deleted_record_count"] == 2
    assert client.post("/api/v1/tools/list_medical_records", json={}, headers=service_headers
                       ).json()["data"]["count"] == 0


def test_delete_unknown_encounter_refused(client, service_headers):
    r = client.post("/api/v1/tools/delete_medical_encounter", json={"encounter_id": str(uuid.uuid4())},
                    headers=service_headers)
    assert r.status_code == 404


# --------------------------------------------------------- scan storage/serving

def _png_bytes() -> bytes:
    # Minimal valid PNG header + pixel data (synthetic test image)
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d4944415478da63fcffff3f0300050201f34a24d5"
        "0000000049454e44ae426082"
    )


def test_upload_scan_and_bind(client, service_headers, scans_dir):
    r = client.post("/api/v1/tools/add_medical_record", json={"content": SYNTHETIC_LAB},
                    headers=service_headers)
    rid = r.json()["data"]["id"]
    r = client.post("/api/v1/tools/upload_medical_scan", headers=service_headers,
                    files={"file": ("scan.png", _png_bytes(), "image/png")}, data={"record_id": rid})
    assert r.status_code == 200, r.text
    rel = r.json()["data"]["path"]
    assert rel.endswith(".png") and str(scans_dir) in str(scans_dir)
    assert r.json()["data"]["record_id"] == rid
    # bound on the record
    r = client.post("/api/v1/tools/list_medical_records", json={}, headers=service_headers)
    assert r.json()["data"]["records"][0]["source_image_path"] == rel


def test_upload_scan_rejects_bad_mime(client, service_headers, scans_dir):
    r = client.post("/api/v1/tools/upload_medical_scan", headers=service_headers,
                    files={"file": ("evil.txt", b"not an image", "text/plain")})
    assert r.status_code == 415


def test_scan_served_only_to_owner(client, service_headers, db, scans_dir):
    r = client.post("/api/v1/tools/add_medical_record", json={"content": SYNTHETIC_LAB},
                    headers=service_headers)
    rid = r.json()["data"]["id"]
    r = client.post("/api/v1/tools/upload_medical_scan", headers=service_headers,
                    files={"file": ("scan.png", _png_bytes(), "image/png")}, data={"record_id": rid})
    rel = r.json()["data"]["path"]

    # owner via JWT (the tools user) can fetch the image
    user = db.query(User).filter(User.display_name == "Encounter Tester").first()
    h = auth_headers(user)
    r = client.get(f"/api/v1/medical/scan/{rid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")

    # another user cannot
    make_user(db, "Other User", None)
    other = db.query(User).filter(User.display_name == "Other User").first()
    r = client.get(f"/api/v1/medical/scan/{rid}", headers=auth_headers(other))
    assert r.status_code == 404

    # anonymous cannot
    assert client.get(f"/api/v1/medical/scan/{rid}").status_code == 401


# --------------------------------------------------------- dashboard contract

def test_dashboard_encounters_contract(client, service_headers, db):
    r = client.post("/api/v1/tools/add_medical_encounter", json={
        "encounter_date": "2026-05-11", "facility": "SYNTHETIC Lab", "notes": "test",
    }, headers=service_headers)
    eid = r.json()["data"]["id"]
    client.post("/api/v1/tools/add_medical_record", json={
        "content": SYNTHETIC_LAB, "encounter_id": eid, "title": "Panel",
    }, headers=service_headers)

    user = db.query(User).filter(User.display_name == "Encounter Tester").first()
    h = auth_headers(user)

    r = client.get("/api/v1/medical/encounters", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    e = body["encounters"][0]
    assert set(e.keys()) == {"id", "encounter_date", "facility", "title", "notes",
                             "created_at", "updated_at", "record_count", "scan_count"}
    assert e["record_count"] == 1
    assert e["scan_count"] == 0

    r = client.get(f"/api/v1/medical/encounters/{eid}", headers=h)
    body = r.json()
    assert body["encounter"]["id"] == eid
    rec = body["records"][0]
    assert set(rec.keys()) == {"id", "title", "content", "source_type", "recorded_at",
                               "encounter_id", "source_image_path", "source_image_paths", "created_at",
                               "scan_count", "has_scans"}
    assert rec["encounter_id"] == eid

    # no anonymous access
    assert client.get("/api/v1/medical/encounters").status_code == 401

    # unknown id -> 404
    r = client.get(f"/api/v1/medical/encounters/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404
