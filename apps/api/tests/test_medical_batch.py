"""Deterministic medical photo batch ingestion (structural guarantee).

SYNTHETIC DOCUMENT CONTENT ONLY — this is a public repo; never store real
medical text here.

Covers the guarantee that a photo batch is stored WITHOUT any LLM storage
call: POST /tools/ingest_medical_batch creates ONE encounter + ONE record
with all scans bound, is idempotent via batch_key, and the staged record is
enriched in place via POST /tools/update_medical_record.
"""

from __future__ import annotations

import pytest

from drhiro_api.models import IngestBatch, MedicalRecord
from tests.conftest import make_user

TG = "700900100"

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050201f34a24d5"
    "0000000049454e44ae426082"
)


@pytest.fixture()
def service_headers(client, db):
    from drhiro_api.security import create_service_token
    make_user(db, "Medical Batch Tester", TG)
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": TG}


@pytest.fixture()
def scans_dir(tmp_path, monkeypatch):
    from drhiro_api import config
    d = tmp_path / "medical_scans"
    monkeypatch.setattr(config.get_settings(), "medical_scans_dir", str(d))
    yield d


def _batch(client, headers, n=3, **extra):
    files = [("files", (f"page{i}.png", PNG, "image/png")) for i in range(n)]
    data = {"batch_key": "test-batch-1", **extra}
    return client.post("/api/v1/tools/ingest_medical_batch", files=files, data=data, headers=headers)


def test_ingest_batch_creates_one_encounter_one_record(client, service_headers, scans_dir, db):
    r = _batch(client, service_headers, n=10, encounter_date="2026-05-11", facility="SYNTHETIC Lab")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["photo_count"] == 10
    assert data["deduplicated"] is False
    assert data["status"] == "staged_transcription_pending"
    assert len(data["scan_paths"]) == 10
    assert data["encounter_id"] and data["record_id"]
    assert data["encounter_date"] == "2026-05-11"

    # Exactly ONE record + ONE encounter in DB (no LLM storage call involved)
    assert db.query(MedicalRecord).count() == 1
    from drhiro_api.models import MedicalEncounter
    assert db.query(MedicalEncounter).count() == 1
    rec = db.query(MedicalRecord).first()
    assert rec.source_type == "telegram_photo_batch"
    assert "transcription pending" in rec.content
    assert rec.source_image_path == data["scan_paths"][0]
    assert rec.source_image_paths_json == data["scan_paths"]
    assert str(rec.encounter_id) == data["encounter_id"]
    # All 10 scan files exist on disk
    for rel in data["scan_paths"]:
        assert (scans_dir / rel).is_file(), rel


def test_ingest_batch_idempotent_replay(client, service_headers, scans_dir, db):
    r1 = _batch(client, service_headers, n=3)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()["data"]
    r2 = _batch(client, service_headers, n=3)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()["data"]
    assert d2["deduplicated"] is True
    assert d2["record_id"] == d1["record_id"]
    assert d2["encounter_id"] == d1["encounter_id"]
    # Replay stored nothing new
    assert db.query(MedicalRecord).count() == 1
    assert db.query(IngestBatch).count() == 1


def test_ingest_batch_bad_date_rejected(client, service_headers, scans_dir):
    r = _batch(client, service_headers, n=1, encounter_date="not-a-date")
    assert r.status_code == 400


def test_update_medical_record_enriches_staged_record(client, service_headers, scans_dir, db):
    staged = _batch(client, service_headers, n=2).json()["data"]
    r = client.post(
        "/api/v1/tools/update_medical_record",
        json={
            "record_id": staged["record_id"],
            "title": "SYNTHETIC lab panel",
            "content": "SYNTHETIC: Hemoglobin 145 g/L, Glucose 5.2 mmol/L",
        },
        headers=service_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == staged["record_id"]
    assert data["title"] == "SYNTHETIC lab panel"
    assert "Hemoglobin" in data["content"]
    assert "transcription pending" not in data["content"]
    # Scans survive enrichment; still ONE record total
    assert data["source_image_paths"] == staged["scan_paths"]
    assert db.query(MedicalRecord).count() == 1


def test_update_medical_record_requires_a_field(client, service_headers, scans_dir):
    staged = _batch(client, service_headers, n=1).json()["data"]
    r = client.post(
        "/api/v1/tools/update_medical_record",
        json={"record_id": staged["record_id"]},
        headers=service_headers,
    )
    assert r.status_code == 400


def test_update_medical_record_scoped_to_owner(client, service_headers, scans_dir, db):
    staged = _batch(client, service_headers, n=1).json()["data"]
    from drhiro_api.models import MedicalRecord as MR
    other = make_user(db, "Other User", None)
    rec = db.query(MR).first()
    rec.user_id = other.id
    db.commit()
    r = client.post(
        "/api/v1/tools/update_medical_record",
        json={"record_id": staged["record_id"], "title": "SYNTHETIC hijack"},
        headers=service_headers,
    )
    assert r.status_code == 404


def test_ingest_requires_auth(client, scans_dir):
    files = [("files", ("page0.png", PNG, "image/png"))]
    r = client.post("/api/v1/tools/ingest_medical_batch", files=files, data={})
    assert r.status_code == 401
