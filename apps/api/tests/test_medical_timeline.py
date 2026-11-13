"""Part A (0.12.0-b) timeline derivation tests: GET /medical/timeline.

SYNTHETIC data only. Covers: 3 dates -> 3 events sharing one series,
upload-timestamp rows (recorded_at ~= created_at) ignored, tenant isolation,
missing DOB -> birth_date null.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drhiro_api.models import MedicalEncounter, MedicalRecord

from tests.conftest import auth_headers, make_user


@pytest.fixture()
def jwt_user(client, db):
    u = make_user(db, "Timeline User", None)
    return u


def _mk_enc(db, user, d, title="Blood panel", facility="SYNTHETIC Lab"):
    enc = MedicalEncounter(
        user_id=user.id, encounter_date=d, title=title, facility=facility, notes=None
    )
    db.add(enc)
    db.commit()
    db.refresh(enc)
    return enc


def _mk_rec(db, user, enc, recorded, created=None, title="SYNTHETIC record"):
    rec = MedicalRecord(
        user_id=user.id,
        encounter_id=enc.id if enc else None,
        title=title,
        content="SYNTHETIC pending",
        source_type="telegram_photo",
        recorded_at=recorded,
    )
    if created is not None:
        rec.created_at = created
        rec.updated_at = created
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_three_dates_three_events_shared_series(client, db, jwt_user):
    from datetime import date

    jwt_user.date_of_birth = "1990-05-17"
    db.commit()
    enc = _mk_enc(db, jwt_user, date(2026, 5, 11))
    older = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _mk_rec(db, jwt_user, enc, older, created=older - timedelta(days=90))
    _mk_rec(db, jwt_user, enc, newer, created=newer - timedelta(days=90))

    r = client.get("/api/v1/medical/timeline", headers=auth_headers(jwt_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["birth_date"] == "1990-05-17"
    assert body["today"]
    evs = [e for e in body["events"] if e["encounter_id"] == str(enc.id)]
    assert [e["date"] for e in evs] == ["2026-01-10", "2026-05-11", "2026-09-03"]
    assert len({e["series_id"] for e in evs}) == 1
    assert evs[0]["series_id"] == str(enc.id)
    assert all(e["series_size"] == 3 for e in evs)
    assert evs[0]["id"] == f"enc:{enc.id}:2026-01-10"
    assert evs[0]["kind"] == "lab"


def test_upload_timestamp_ignored(client, db, jwt_user):
    from datetime import date

    enc = _mk_enc(db, jwt_user, date(2026, 5, 11))
    # Upload row: recorded_at == created_at (within 60s) -> ignored.
    now = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    _mk_rec(db, jwt_user, enc, now, created=now)

    r = client.get("/api/v1/medical/timeline", headers=auth_headers(jwt_user))
    assert r.status_code == 200, r.text
    evs = [e for e in r.json()["events"] if e["encounter_id"] == str(enc.id)]
    assert [e["date"] for e in evs] == ["2026-05-11"]
    assert evs[0]["series_id"] is None
    assert evs[0]["series_size"] == 1


def test_tenant_isolation(client, db, jwt_user):
    from datetime import date

    other = make_user(db, "Other User", None)
    _mk_enc(db, other, date(2026, 3, 1), title="Other panel")

    r = client.get("/api/v1/medical/timeline", headers=auth_headers(jwt_user))
    assert r.status_code == 200, r.text
    assert r.json()["events"] == []


def test_missing_dob_null(client, db, jwt_user):
    jwt_user.date_of_birth = None
    db.commit()
    r = client.get("/api/v1/medical/timeline", headers=auth_headers(jwt_user))
    assert r.status_code == 200, r.text
    assert r.json()["birth_date"] is None


def test_from_to_filter_and_empty_encounter(client, db, jwt_user):
    from datetime import date

    enc = _mk_enc(db, jwt_user, date(2026, 5, 11))
    assert enc  # no records -> single event at encounter_date

    r = client.get(
        "/api/v1/medical/timeline?from=2026-05-01&to=2026-05-11",
        headers=auth_headers(jwt_user),
    )
    assert r.status_code == 200, r.text
    evs = r.json()["events"]
    assert len(evs) == 1 and evs[0]["date"] == "2026-05-11"

    r = client.get(
        "/api/v1/medical/timeline?from=2026-06-01",
        headers=auth_headers(jwt_user),
    )
    assert r.json()["events"] == []
