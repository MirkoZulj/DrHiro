"""Indexed scan serving: GET /medical/scan/{id}?index=N. SYNTHETIC ONLY."""

from __future__ import annotations

import pytest

from drhiro_api import config
from drhiro_api.models import MedicalRecord, User
from tests.conftest import auth_headers, make_user

SYNTHETIC_LAB = "SYNTHETIC LAB REPORT (test fixture)\nHemoglobin: 145 g/L\n"


@pytest.fixture()
def scans_dir(tmp_path, monkeypatch):
    d = tmp_path / "medical_scans"
    monkeypatch.setattr(config.get_settings(), "medical_scans_dir", str(d))
    yield d


def _make_record_with_n_scans(db, scans_dir, n=69):
    user = make_user(db, "Scan Index Tester", None)
    user_dir = scans_dir / "scans" / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        # Distinct bytes per index so distinct-file assertions are meaningful.
        data = b"\x89PNG" + f"synthetic-scan-{i:03d}".encode()
        p = user_dir / f"scan-{i:03d}.png"
        p.write_bytes(data)
        paths.append(f"scans/{user.id}/scan-{i:03d}.png")
    rec = MedicalRecord(
        user_id=user.id,
        content=SYNTHETIC_LAB,
        source_type="telegram_photo",
        source_image_path=paths[0],
        source_image_paths_json=paths,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return user, rec, paths


def test_indexed_scan_returns_distinct_files(client, db, scans_dir):
    user, rec, paths = _make_record_with_n_scans(db, scans_dir, n=69)
    h = auth_headers(user)
    bodies = set()
    for i in (0, 1, 2, 34, 68):
        r = client.get(f"/api/v1/medical/scan/{rec.id}?index={i}", headers=h)
        assert r.status_code == 200, (i, r.text)
        assert r.content == b"\x89PNG" + f"synthetic-scan-{i:03d}".encode()
        bodies.add(r.content)
    assert len(bodies) == 5  # all distinct


def test_default_index_zero_unchanged(client, db, scans_dir):
    user, rec, _ = _make_record_with_n_scans(db, scans_dir, n=69)
    h = auth_headers(user)
    r_default = client.get(f"/api/v1/medical/scan/{rec.id}", headers=h)
    r_zero = client.get(f"/api/v1/medical/scan/{rec.id}?index=0", headers=h)
    assert r_default.status_code == 200
    assert r_default.content == r_zero.content


def test_out_of_range_index_rejected(client, db, scans_dir):
    user, rec, _ = _make_record_with_n_scans(db, scans_dir, n=69)
    h = auth_headers(user)
    for bad in (69, 100, -1):
        r = client.get(f"/api/v1/medical/scan/{rec.id}?index={bad}", headers=h)
        assert r.status_code == 404, (bad, r.status_code, r.text)


def test_indexed_scan_owner_only(client, db, scans_dir):
    user, rec, _ = _make_record_with_n_scans(db, scans_dir, n=69)
    other = make_user(db, "Other Scan User", None)
    r = client.get(f"/api/v1/medical/scan/{rec.id}?index=5", headers=auth_headers(other))
    assert r.status_code == 404


def test_index_without_list_falls_back(client, db, scans_dir):
    """Record with only source_image_path: index 0 works, index 1 rejected."""
    user = make_user(db, "Fallback Tester", None)
    user_dir = scans_dir / "scans" / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "only.png").write_bytes(b"\x89PNGonly")
    rec = MedicalRecord(
        user_id=user.id, content=SYNTHETIC_LAB, source_type="telegram_photo",
        source_image_path=f"scans/{user.id}/only.png", source_image_paths_json=None,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    h = auth_headers(user)
    assert client.get(f"/api/v1/medical/scan/{rec.id}?index=0", headers=h).status_code == 200
    assert client.get(f"/api/v1/medical/scan/{rec.id}?index=1", headers=h).status_code == 404
    assert client.get(f"/api/v1/medical/scan/{rec.id}", headers=h).status_code == 200
