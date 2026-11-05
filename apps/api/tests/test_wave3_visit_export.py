"""0.10.x wave-3 tests: visit runs (spec §3.8), export bundle (§4.1), backups API (§4.3)."""
from __future__ import annotations

import io
import json
import uuid
import zipfile

from tests.conftest import auth_headers, make_user

from drhiro_api.models import MedicationItem, MedicalEncounter, MedicalRecord


import pytest


@pytest.fixture()
def fake_enqueue(monkeypatch):
    """Deterministic enqueue: returns a fake job id."""
    calls = []

    def _fake(queue_name, func_ref, *args):
        calls.append((queue_name, func_ref) + args)
        return "job-fake-1"

    monkeypatch.setattr("drhiro_api.services.task_queue.enqueue", _fake)
    return calls


def _visit_body(**overrides):
    body = {
        "kind": "visit",
        "period": {"preset": "90d"},
        "intent": {
            "provider_type": "cardiology",
            "purpose": "Check blood pressure and tiredness",
            "questions": ["Is my BP trending up?"],
            "include_metrics": ["weight_kg"],
            "exclude_metrics": ["alcohol_ml"],
        },
    }
    body["intent"].update(overrides)
    return body


# ---------- visit runs ----------

def test_visit_run_creation_and_profile(client, db, user_a, fake_enqueue):
    r = client.post("/api/v1/analytics/runs", headers=auth_headers(user_a), json=_visit_body())
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["kind"] == "visit"
    params = run["params_json"]
    assert params["intent"]["provider_type"] == "cardiology"
    assert params["profile_version"] == "visit-1.0"
    # profile ∪ include − exclude, restricted to catalogue
    assert "weight_kg" in params["visit_metrics"]
    assert "alcohol_ml" not in params["visit_metrics"]
    assert "bp_sys" in params["visit_metrics"]
    # fake_enqueue fixture: run stays queued for the worker
    assert run["status"] == "queued"


def test_visit_intent_validation(client, db, user_a):
    h = auth_headers(user_a)
    bad = _visit_body(provider_type="dentist")
    r = client.post("/api/v1/analytics/runs", headers=h, json=bad)
    assert r.status_code == 400
    bad = _visit_body(questions=["x" * 301])
    r = client.post("/api/v1/analytics/runs", headers=h, json=bad)
    assert r.status_code == 400


def test_general_kind_still_works(client, db, user_a, fake_enqueue):
    r = client.post("/api/v1/analytics/runs", headers=auth_headers(user_a),
                    json={"kind": "general", "period": {"preset": "30d"}})
    assert r.status_code == 200
    assert r.json()["run"]["kind"] == "general"


def test_tenant_isolation_on_run(client, db, user_a, fake_enqueue, user_b):
    h = auth_headers(user_a)
    rid = client.post("/api/v1/analytics/runs", headers=h,
                      json={"kind": "general", "period": {"preset": "30d"}}).json()["run"]["id"]
    r = client.get(f"/api/v1/analytics/runs/{rid}", headers=auth_headers(user_b))
    assert r.status_code == 404


def test_visit_profiles_endpoint(client, db, user_a):
    r = client.get("/api/v1/analytics/visit-profiles", headers=auth_headers(user_a))
    assert r.status_code == 200
    data = r.json()
    codes = {p["code"] for p in data["profiles"]}
    assert {"gp", "cardiology", "sleep"} <= codes
    assert data["profile_version"] == "visit-1.0"


def test_visit_suggest_deterministic(client, db, user_a):
    r = client.post("/api/v1/analytics/visit-suggest", headers=auth_headers(user_a),
                    json={"purpose": "Imam problema s tlakom i umorom", "provider_type": "gp"})
    assert r.status_code == 200
    s = r.json()["suggested_metrics"]
    assert "bp_sys" in s
    # same input -> same output (deterministic keyword map)
    r2 = client.post("/api/v1/analytics/visit-suggest", headers=auth_headers(user_a),
                     json={"purpose": "Imam problema s tlakom i umorom", "provider_type": "gp"})
    assert r2.json()["suggested_metrics"] == s


def test_report_endpoint_queues_file(client, db, user_a, fake_enqueue):
    h = auth_headers(user_a)
    rid = client.post("/api/v1/analytics/runs", headers=h,
                      json={"kind": "general", "period": {"preset": "30d"}}).json()["run"]["id"]
    r = client.post(f"/api/v1/analytics/runs/{rid}/report", headers=h, json={})
    assert r.status_code == 200
    assert r.json()["file"]["status"] in ("queued", "failed")


# ---------- export bundle (§4.1) ----------

def test_export_bundle_round_trip(client, db, user_a, fake_enqueue, user_b):
    from drhiro_worker import jobs_export
    from drhiro_api.models import GeneratedFile, Measurement
    from datetime import date, datetime, timezone

    h = auth_headers(user_a)
    # seed data for A only
    db.add(Measurement(user_id=user_a.id, metric_type="steps", source_record_id="test-steps-1",
                       value_json={"value": 1000.0}, source_provider="test",
                       start_at=datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))
    db.add(MedicalEncounter(user_id=user_a.id, encounter_date=date(2026, 10, 2),
                            facility="KBC Zagreb", title="Cardiology"))
    db.commit()
    enc = db.query(MedicalEncounter).filter_by(user_id=user_a.id).first()
    db.add(MedicalRecord(user_id=user_a.id, encounter_id=enc.id, content="Verbatim content X",
                         source_type="manual"))
    db.add(MedicalRecord(user_id=user_b.id, content="BOB SECRET", source_type="manual"))
    db.add(MedicationItem(user_id=user_a.id, kind="supplement", name="Mg", status="active"))
    db.commit()

    r = client.post("/api/v1/exports/bundle", headers=h, json={"include_files": False, "format": ["json", "csv"]})
    assert r.status_code == 200, r.text
    file = r.json()["file"]
    assert file["kind"] == "data_export"
    # worker runs the build directly
    jobs_export.build_export(file["id"])
    gf = db.query(GeneratedFile).get(uuid.UUID(file["id"]))
    db.refresh(gf)
    assert gf.status == "ready", gf.error
    import os

    from drhiro_api.config import get_settings

    path = os.path.join(get_settings().generated_files_dir, gf.rel_path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names and "README.txt" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema"] == "drhiro-export/1"
        # every manifest entry has a matching sha256
        for entry in manifest["files"]:
            if entry["sha256"]:
                import hashlib

                assert hashlib.sha256(zf.read(entry["path"])).hexdigest() == entry["sha256"]
        # row counts: measurements seeded 1 for A
        mrows = [json.loads(l) for l in zf.read("json/measurements.ndjson").decode().splitlines() if l]
        assert len(mrows) == 1
        # no other user's data leaks
        rec_rows = [json.loads(l) for l in zf.read("json/medical_records.ndjson").decode().splitlines() if l]
        assert all("BOB SECRET" not in json.dumps(r) for r in rec_rows)
        assert any(r["content"] == "Verbatim content X" for r in rec_rows)
        assert "json/health_profiles.ndjson" in names
    # dashboard-only download works
    r = client.get(f"/api/v1/files/{gf.id}/download", headers=h)
    assert r.status_code == 200
    # user B cannot download A's export
    r = client.get(f"/api/v1/files/{gf.id}/download", headers=auth_headers(user_b))
    assert r.status_code == 404


def test_export_fhir_rejected(client, db, user_a):
    r = client.post("/api/v1/exports/bundle", headers=auth_headers(user_a),
                    json={"fhir": True})
    assert r.status_code == 400


# ---------- backups API (§4.3) ----------

def test_backups_require_admin(client, db, user_a, user_b):
    # Configure an admin identity that is NOT user_a/user_b.
    from drhiro_api.services.settings_store import get_row

    row = get_row(db)
    row.telegram_allowed_user_id = "999999"
    db.commit()
    r = client.get("/api/v1/backups/status", headers=auth_headers(user_a))
    assert r.status_code == 403
    r = client.post("/api/v1/backups/run", headers=auth_headers(user_a))
    assert r.status_code == 403
    r = client.get("/api/v1/backups", headers=auth_headers(user_a))
    assert r.status_code == 403


# ---------- generated files ----------

def test_files_list_and_expiry_guard(client, db, user_a):
    from drhiro_api.models import GeneratedFile
    from datetime import datetime, timedelta, timezone

    h = auth_headers(user_a)
    db.add(GeneratedFile(user_id=user_a.id, kind="data_export", status="ready",
                         rel_path="generated/x/y.zip", filename="x.zip",
                         expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.commit()
    r = client.get("/api/v1/files?kind=data_export", headers=h)
    assert r.status_code == 200
    assert r.json()["count"] == 1
    fid = r.json()["files"][0]["id"]
    # expired -> 410 on download
    r = client.get(f"/api/v1/files/{fid}/download", headers=h)
    assert r.status_code == 410
    # delete works
    assert client.delete(f"/api/v1/files/{fid}", headers=h).json() == {"ok": True}
