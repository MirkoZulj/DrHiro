"""Acceptance tests for the OMRON CSV import endpoint."""

from __future__ import annotations

import io

from tests.conftest import auth_headers


OMRON_SAMPLE = """OmronBloodPressureKey,ID,DateTime,DateTimeLocal,DateTimeUtcOffset,Systolic,Diastolic,BloodPressureUnits,Pulse,PulseUnits,DeviceType
key-abc-1,1670439952000,2022-12-07T19:05:52,2022-12-07T14:05:52,-05:00,128,82,mmHg,68,bpm,HEM-7321
key-abc-2,1670440000000,2022-12-08T08:30:00,2022-12-08T03:30:00,-05:00,121,79,mmHg,64,bpm,HEM-7321
key-abc-3,1670440100000,2022-12-08T20:15:00,2022-12-08T15:15:00,-05:00,135,88,mmHg,71,bpm,HEM-7321
"""

OMRON_BAD_ROWS = """Date,Systolic,Diastolic,Pulse
2023-01-01,120,80,70
2023-01-02,,85,70
not-a-date,130,90,70
2023-01-04,500,60,70
"""


def _upload(client, token, csv_text: str, filename: str = "omron.csv"):
    return client.post(
        "/api/v1/import/omron-csv",
        files={"file": (filename, io.BytesIO(csv_text.encode()), "text/csv")},
        headers=auth_headers_from_token(token),
    )


def auth_headers_from_token(token):
    return {"Authorization": f"Bearer {token}"}


def test_omron_csv_import_success(client, user_a, db):
    token = _token_for(client, user_a)
    r = _upload(client, token, OMRON_SAMPLE)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 3
    assert body["duplicates"] == 0
    assert body["rejected"] == []

    # Idempotent re-upload: all duplicates, no new rows.
    r2 = _upload(client, token, OMRON_SAMPLE)
    assert r2.json()["accepted"] == 0
    assert r2.json()["duplicates"] == 3


def test_omron_csv_rejects_bad_rows(client, user_a, db):
    token = _token_for(client, user_a)
    r = _upload(client, token, OMRON_BAD_ROWS)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert len(body["rejected"]) == 3
    reasons = " ".join(x["reason"] for x in body["rejected"])
    assert "missing" in reasons
    assert "unparseable datetime" in reasons
    assert "invalid values" in reasons


def test_omron_csv_requires_auth(client):
    r = _upload(client, "bad-token", OMRON_SAMPLE)
    assert r.status_code in (401, 403)


def test_omron_csv_missing_columns(client, user_a, db):
    token = _token_for(client, user_a)
    r = _upload(client, token, "foo,bar\n1,2\n")
    assert r.status_code == 400


def _token_for(client, user):
    """Generate a valid access token for the user."""
    from drhiro_api.security import create_access_token
    return create_access_token(user.id)
