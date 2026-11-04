"""0.10.0 wave-3 health card tests (spec §6.6)."""
from __future__ import annotations

from tests.conftest import auth_headers, make_user


def test_get_creates_empty_card(client, db, user_a):
    r = client.get("/api/v1/health-card", headers=auth_headers(user_a))
    assert r.status_code == 200
    card = r.json()["card"]
    assert card["stored"]["conditions"] == []
    assert card["derived"]["display_name"] == "Alice"
    assert card["stale_sections"] != []  # never reviewed -> stale
    assert card["reviewed_at"] is None


def test_section_validation_bad_enum(client, db, user_a):
    h = auth_headers(user_a)
    r = client.post("/api/v1/health-card/conditions", headers=h,
                    json={"name": "Hypertension", "status": "worse"})
    assert r.status_code == 422
    r = client.post("/api/v1/health-card/emergency_contacts", headers=h,
                    json={"name": "Mom", "relation": "mother", "phone": "x" * 40})
    assert r.status_code == 422
    # entries are never created by failed validation
    r2 = client.get("/api/v1/health-card", headers=h)
    assert r2.json()["card"]["stored"]["conditions"] == []


def test_entry_id_stability_and_crud(client, db, user_a):
    h = auth_headers(user_a)
    r = client.post("/api/v1/health-card/allergies", headers=h,
                    json={"substance": "Penicillin", "category": "drug",
                          "severity": "severe", "confirmed_by_hcp": True})
    assert r.status_code == 200
    entry = r.json()["entry"]
    eid = entry["id"]
    assert eid
    # id stays stable across reads
    card = client.get("/api/v1/health-card", headers=h).json()["card"]
    assert card["stored"]["allergies"][0]["id"] == eid
    # update
    r = client.patch(f"/api/v1/health-card/allergies/{eid}", headers=h,
                     json={"severity": "moderate"})
    assert r.status_code == 200
    assert r.json()["entry"]["severity"] == "moderate"
    assert r.json()["entry"]["substance"] == "Penicillin"  # untouched fields survive
    # delete
    assert client.delete(f"/api/v1/health-card/allergies/{eid}", headers=h).json() == {"ok": True}
    r = client.delete(f"/api/v1/health-card/allergies/{eid}", headers=h)
    assert r.status_code == 404


def test_tenant_isolation(client, db, user_a, user_b):
    h = auth_headers(user_a)
    eid = client.post("/api/v1/health-card/conditions", headers=h,
                      json={"name": "Asthma", "status": "active"}).json()["entry"]["id"]
    hb = auth_headers(user_b)
    r = client.patch(f"/api/v1/health-card/conditions/{eid}", headers=hb, json={"status": "resolved"})
    assert r.status_code == 404
    r = client.delete(f"/api/v1/health-card/conditions/{eid}", headers=hb)
    assert r.status_code == 404
    # user B's card is empty
    assert client.get("/api/v1/health-card", headers=hb).json()["card"]["stored"]["conditions"] == []


def test_derived_meds_reflect_stop_immediately(client, db, user_a):
    from datetime import date

    from drhiro_api.models import MedicationItem
    h = auth_headers(user_a)
    db.add(MedicationItem(user_id=user_a.id, kind="supplement", name="Magnesium",
                          status="active", dose_amount=1, dose_unit="tablet"))
    db.commit()
    card = client.get("/api/v1/health-card", headers=h).json()["card"]
    assert [m["name"] for m in card["derived"]["current_medications"]] == ["Magnesium"]
    item = db.query(MedicationItem).filter_by(user_id=user_a.id).first()
    item.status = "stopped"
    db.commit()
    card = client.get("/api/v1/health-card", headers=h).json()["card"]
    assert card["derived"]["current_medications"] == []


def test_audit_contains_no_content(client, db, user_a):
    from drhiro_api.models import AuditEvent
    h = auth_headers(user_a)
    client.post("/api/v1/health-card/conditions", headers=h,
                json={"name": "SecretCondition", "status": "active"})
    events = db.query(AuditEvent).filter(AuditEvent.action.like("health_card.%")).all()
    assert events, "expected a health_card audit event"
    for e in events:
        blob = str(e.metadata_json or {})
        assert "SecretCondition" not in blob


def test_mark_reviewed_clears_stale(client, db, user_a):
    h = auth_headers(user_a)
    client.post("/api/v1/health-card/reviewed", headers=h)
    card = client.get("/api/v1/health-card", headers=h).json()["card"]
    assert card["reviewed_at"] is not None
    assert card["stale_sections"] == []


def test_put_full_card_validation(client, db, user_a):
    h = auth_headers(user_a)
    r = client.put("/api/v1/health-card", headers=h, json={"blood_type": "Z+"})
    assert r.status_code == 422
    r = client.put("/api/v1/health-card", headers=h,
                   json={"blood_type": "0+", "organ_donor": "yes"})
    assert r.status_code == 200
    assert r.json()["card"]["stored"]["blood_type"] == "0+"
