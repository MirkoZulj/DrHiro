"""Acceptance tests 1-3: tenant isolation, Telegram identity, idempotency."""

from __future__ import annotations

import uuid
from datetime import datetime

from drhiro_api.models import Measurement
from drhiro_api.routers.ingest import HealthConnectBatchRequest, IngestResult
from drhiro_api.security import validate_telegram_init_data
from tests.conftest import auth_headers, link_device


def _batch(installation_id: str, batch_id: str, systolic: int = 120):
    return {
        "installation_id": installation_id,
        "batch_id": batch_id,
        "records": [
            {
                "source_record_id": "hc-record-1",
                "record_type": "BloodPressureRecord",
                "start_at": "2026-08-10T06:45:00Z",
                "end_at": "2026-08-10T06:45:00Z",
                "source_timezone": "Europe/Zagreb",
                "values": {"systolic_mmhg": systolic, "diastolic_mmhg": 78},
                "device": {"manufacturer": "OMRON", "model": "HEM-7321"},
            }
        ],
    }


# Test 1: Tenant isolation — User A cannot retrieve User B's records by
# changing an ID in a request.
def test_tenant_isolation(client, user_a, user_b, db):
    inst_a = link_device(db, user_a)
    r1 = client.post("/api/v1/ingest/health-connect/batch", json=_batch(inst_a, "batch-a-0001"), headers=auth_headers(user_a))
    assert r1.status_code == 200
    assert r1.json()["accepted"] == 1

    # User B tries to read User A's measurement by guessing its ID.
    m = db.query(Measurement).filter(Measurement.user_id == user_a.id).first()
    # There is no direct GET-by-id for measurements; B attempts a meal read
    # on A's meal (defense: meal endpoints filter by user_id).
    from drhiro_api.models import Meal
    meal = Meal(user_id=user_a.id, eaten_at=datetime.now(), status="confirmed")
    db.add(meal)
    db.commit()
    db.refresh(meal)

    r2 = client.get(f"/api/v1/meals/{meal.id}", headers=auth_headers(user_b))
    assert r2.status_code == 404  # not found, not forbidden-with-data


# Test 2: Telegram identity — a message can write data only to the paired user.
def test_telegram_identity_mapping(db, user_a, user_b):
    from drhiro_api.deps import get_user_by_telegram_id
    assert get_user_by_telegram_id(db, "1001").id == user_a.id
    assert get_user_by_telegram_id(db, "1002").id == user_b.id


# Test 3: Idempotency — uploading the same Health Connect batch twice
# creates no duplicate records.
def test_idempotency_same_batch(client, user_a, db):
    inst_a = link_device(db, user_a)
    r1 = client.post("/api/v1/ingest/health-connect/batch", json=_batch(inst_a, "idem-batch-1"), headers=auth_headers(user_a))
    assert r1.status_code == 200
    assert r1.json()["accepted"] == 1

    r2 = client.post("/api/v1/ingest/health-connect/batch", json=_batch(inst_a, "idem-batch-1"), headers=auth_headers(user_a))
    assert r2.status_code == 200
    # Replay returns the stored result (idempotent response), no new rows.
    assert r2.json()["accepted"] == 1
    assert r2.json()["duplicates"] == 0

    count = db.query(Measurement).filter(Measurement.user_id == user_a.id).count()
    assert count == 1


def test_idempotency_per_record_upsert(client, user_a, db):
    """Same source_record_id in two different batches does not duplicate."""
    inst_a = link_device(db, user_a)
    r1 = client.post("/api/v1/ingest/health-connect/batch", json=_batch(inst_a, "batch-x1"), headers=auth_headers(user_a))
    assert r1.json()["accepted"] == 1
    r2 = client.post("/api/v1/ingest/health-connect/batch", json=_batch(inst_a, "batch-x2"), headers=auth_headers(user_a))
    assert r2.json()["duplicates"] == 1
    count = db.query(Measurement).filter(Measurement.user_id == user_a.id).count()
    assert count == 1


# Test 2b: Mini App initData validation (HMAC) — forged data is rejected.
def test_telegram_initdata_validation():
    from drhiro_api.config import get_settings
    import hashlib
    import hmac
    import time

    bot_token = get_settings().telegram_bot_token
    user = '{"id": 1001, "first_name": "Alice"}'
    auth_date = str(int(time.time()))
    params = {"auth_date": auth_date, "user": user}
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    valid_hash = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    valid_init_data = "&".join(f"{k}={v}" for k, v in [*params.items(), ("hash", valid_hash)])

    ok = validate_telegram_init_data(valid_init_data)
    assert ok is not None
    assert ok["telegram_id"] == "1001"

    forged = valid_init_data.replace(valid_hash, "0" * 64)
    assert validate_telegram_init_data(forged) is None
