"""Acceptance tests for the Mi Fitness export importer (P1).

Fixture: ``tests/fixtures/mifitness_sample.zip`` — a small ZipCrypto-encrypted
ZIP (password ``test-password-123``, a synthetic fixture value, not a secret)
with two CSVs shaped like the real export:

* 13 raw rows: 2 telemetry, 4 skipped-per-sample, 7 import-class
  (2 continuous HR, 1 single HR, 1 resting HR, 1 SpO2, 1 weight, 1 sleep).
* 11 daily rows: 1 weight (skipped), 10 mapped.

The two dates in play: D1 = 2024-01-10 (uncovered), D2 = 2026-08-20
(the day we pretend Health Connect already covers).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from drhiro_api.models import DailyAggregate, IngestBatch, Measurement
from drhiro_schema.metrics import MetricType
from tests.conftest import auth_headers

FIXTURE = Path(__file__).parent / "fixtures" / "mifitness_sample.zip"
PASSWORD = "test-password-123"
PROVIDER = "mifitness_export"

D1 = "2024-01-10"
D2 = "2026-08-20"
D2_HR_EPOCH = 1787212800  # 2026-08-20T08:00:00Z

RAW_ROWS = 13
RAW_IMPORTED = 7
RAW_TELEMETRY = 2
RAW_PER_SAMPLE = 4
DAILY_ROWS = 11
DAILY_MAPPED = 10
DAILY_SKIPPED = 1


def _post(client, user, *, dry_run: bool, password: str = PASSWORD):
    data = FIXTURE.read_bytes()
    return client.post(
        "/api/v1/import/mifitness",
        files={"file": ("mifitness_sample.zip", data, "application/zip")},
        data={"password": password, "dry_run": "true" if dry_run else "false"},
        headers=auth_headers(user),
    )


def _counts(db, user):
    export_meas = db.execute(
        select(Measurement.metric_type).where(
            Measurement.user_id == user.id, Measurement.source_provider == PROVIDER
        )
    ).scalars().all()
    daily = db.execute(
        select(DailyAggregate.metric_type).where(DailyAggregate.user_id == user.id)
    ).scalars().all()
    return export_meas, daily


def test_dry_run_reconciles_and_writes_nothing(client, user_a, db):
    r = _post(client, user_a, dry_run=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    raw, daily = body["raw"], body["daily"]
    # rows read = imported + telemetry + per-sample  (spec §8.1 arithmetic)
    assert raw["rows_read"] == RAW_ROWS
    assert raw["imported"] == RAW_IMPORTED
    assert raw["skipped_telemetry"] == RAW_TELEMETRY
    assert raw["skipped_per_sample"] == RAW_PER_SAMPLE
    assert raw["imported"] + raw["skipped_telemetry"] + raw["skipped_per_sample"] == raw["rows_read"]
    assert daily["rows_read"] == DAILY_ROWS
    assert daily["mapped"] == DAILY_MAPPED
    assert daily["skipped"] == DAILY_SKIPPED
    assert daily["mapped"] + daily["skipped"] == daily["rows_read"]
    # fixture has no per-day revisions
    assert daily["distinct_keys"] == DAILY_MAPPED
    assert daily["revisions_collapsed"] == 0
    # dry run must not touch the DB
    assert raw["inserted"] == RAW_IMPORTED  # would-insert
    meas, daily_rows = _counts(db, user_a)
    assert meas == []
    assert daily_rows == []


def test_real_import_then_idempotent_second_run(client, user_a, db):
    r1 = _post(client, user_a, dry_run=False)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["raw"]["inserted"] == RAW_IMPORTED
    assert b1["raw"]["duplicates"] == 0
    assert b1["daily"]["inserted"] == DAILY_MAPPED
    assert b1["raw"]["errors"] == 0 and b1["daily"]["errors"] == 0
    assert b1["batch_id"].startswith("mifitness-")

    meas, daily_rows = _counts(db, user_a)
    assert len(meas) == RAW_IMPORTED
    assert len(daily_rows) == DAILY_MAPPED
    assert set(meas) == {
        "heart_rate", "resting_heart_rate", "spo2", "weight", "sleep"
    }
    assert set(daily_rows) == {
        "steps", "active_calories", "sleep", "active_minutes",
        "heart_rate", "stress", "stand_hours", "goal_achievement", "spo2",
    }
    # report persisted
    batch = db.execute(
        select(IngestBatch).where(
            IngestBatch.user_id == user_a.id, IngestBatch.batch_id == b1["batch_id"]
        )
    ).scalar_one()
    assert batch.result_json["raw"]["inserted"] == RAW_IMPORTED

    # Second run: nothing new.
    r2 = _post(client, user_a, dry_run=False)
    b2 = r2.json()
    assert b2["raw"]["inserted"] == 0
    assert b2["raw"]["duplicates"] == RAW_IMPORTED
    assert b2["daily"]["inserted"] == 0
    meas2, daily2 = _counts(db, user_a)
    assert len(meas2) == RAW_IMPORTED
    assert len(daily2) == DAILY_MAPPED


def test_gapfill_hc_covered_day_gets_no_export_daily_row(client, user_a, db):
    db.add(
        DailyAggregate(
            user_id=user_a.id,
            local_date=D2,
            metric_type="steps",
            value_json={"steps": 9999},
            source_count=1,
            source_provider="health_connect",
        )
    )
    db.commit()

    r = _post(client, user_a, dry_run=False)
    assert r.status_code == 200, r.text

    rows = db.execute(
        select(DailyAggregate.source_provider, DailyAggregate.value_json).where(
            DailyAggregate.user_id == user_a.id, DailyAggregate.metric_type == "steps"
        )
    ).all()
    providers = {sp for sp, _ in rows}
    # The HC-covered day D2 is left alone; D1 is filled by the export.
    assert providers == {"health_connect", "mifitness_export"}
    by_src = {sp: vj for sp, vj in rows}
    assert by_src["health_connect"]["steps"] == 9999
    assert by_src["mifitness_export"]["steps"] == 2728  # D1


def test_union_hc_covered_day_hr_samples_are_imported(client, user_a, db):
    db.add(
        Measurement(
            user_id=user_a.id,
            metric_type="heart_rate",
            start_at=datetime.fromtimestamp(D2_HR_EPOCH, tz=timezone.utc),
            end_at=datetime.fromtimestamp(D2_HR_EPOCH, tz=timezone.utc),
            value_json={"bpm": 88},
            unit="bpm",
            source_provider="health_connect",
            source_record_id="hc-hr-1",
            recording_method="automatic",
        )
    )
    db.commit()

    r = _post(client, user_a, dry_run=False)
    assert r.status_code == 200, r.text

    hr = db.execute(
        select(Measurement.source_provider, Measurement.value_json).where(
            Measurement.user_id == user_a.id, Measurement.metric_type == "heart_rate"
        )
    ).all()
    srcs = {sp for sp, _ in hr}
    assert srcs == {"health_connect", "mifitness_export"}
    export_hr = [vj for sp, vj in hr if sp == PROVIDER]
    # All three export HR samples imported (union rule): two continuous
    # (D1 + the HC-covered D2) and one manual spot reading.
    assert len(export_hr) == 3
    assert {vj["bpm"] for vj in export_hr} == {65, 70, 74}
    assert any(vj["bpm"] == 70 for vj in export_hr)  # the D2 sample HC "covered"


def test_import_never_modifies_existing_rows(client, user_a, db):
    hc = Measurement(
        user_id=user_a.id,
        metric_type="heart_rate",
        start_at=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
        value_json={"bpm": 88},
        unit="bpm",
        source_provider="health_connect",
        source_record_id="hc-hr-1",
        recording_method="automatic",
    )
    db.add(hc)
    db.add(
        DailyAggregate(
            user_id=user_a.id, local_date=D2, metric_type="steps",
            value_json={"steps": 9999}, source_count=1, source_provider="health_connect",
        )
    )
    db.commit()
    db.refresh(hc)
    before_updated = hc.updated_at
    before_count = db.query(Measurement).filter(
        Measurement.user_id == user_a.id, Measurement.source_provider == "health_connect"
    ).count()

    _post(client, user_a, dry_run=False)

    db.expire_all()
    hc2 = db.get(Measurement, hc.id)
    assert hc2.updated_at == before_updated
    assert hc2.value_json == {"bpm": 88}
    after_count = db.query(Measurement).filter(
        Measurement.user_id == user_a.id, Measurement.source_provider == "health_connect"
    ).count()
    assert after_count == before_count


def test_wrong_password_rejected(client, user_a, db):
    r = _post(client, user_a, dry_run=True, password="not-the-password")
    assert r.status_code == 400
    assert "password" in r.json()["detail"].lower() or "zip" in r.json()["detail"].lower()


def test_non_zip_rejected(client, user_a, db):
    r = client.post(
        "/api/v1/import/mifitness",
        files={"file": ("x.zip", b"not a zip at all", "application/zip")},
        data={"password": PASSWORD, "dry_run": "true"},
        headers=auth_headers(user_a),
    )
    assert r.status_code == 400


def test_requires_auth(client):
    r = client.post(
        "/api/v1/import/mifitness",
        files={"file": ("x.zip", FIXTURE.read_bytes(), "application/zip")},
        data={"password": PASSWORD, "dry_run": "true"},
    )
    assert r.status_code in (401, 403)


def test_new_metric_types_exist():
    assert MetricType.STRESS == "stress"
    assert MetricType.ACTIVE_MINUTES == "active_minutes"
    assert MetricType.STAND_HOURS == "stand_hours"
    assert MetricType.GOAL_ACHIEVEMENT == "goal_achievement"


def test_daily_provenance_columns_populated(client, user_a, db):
    _post(client, user_a, dry_run=False)
    rows = db.execute(
        select(DailyAggregate.source_provider, DailyAggregate.source_record_id).where(
            DailyAggregate.user_id == user_a.id
        )
    ).all()
    assert rows
    for sp, rid in rows:
        assert sp == PROVIDER
        assert rid and rid.startswith("mf:")