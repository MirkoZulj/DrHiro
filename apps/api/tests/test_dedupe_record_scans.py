"""Content-dedupe for merged medical record scans. SYNTHETIC ONLY."""
import hashlib
import pytest
from drhiro_api.models import MedicalRecord
from tests.conftest import make_user
from tests.test_medical_batch import PNG, service_headers, scans_dir  # noqa
TG = "700900100"

def _png_variant(tag: bytes):
    # Distinct content per tag but valid PNG bytes after header (never rendered)
    return PNG + tag

def _stage(client, headers, n_unique, copies, content="SYNTHETIC transcription keep-me"):
    from drhiro_api.routers.medical import store_scan
    # use API ingest then plant duplicates via db + files
    files = [("files", (f"u{i}.png", _png_variant(bytes([i])), "image/png")) for i in range(n_unique)]
    r = client.post("/api/v1/tools/ingest_medical_batch", files=files, data={"batch_key": "dedupe-test"}, headers=headers)
    assert r.status_code == 200, r.text
    staged = r.json()["data"]
    return staged

def test_dedupe_227_style_to_23_style_and_rerun(client, service_headers, scans_dir, db):
    # Build: 23 unique photos, each copied ~10x => 227 bound paths (planted duplicates)
    from drhiro_api.routers.medical import store_scan
    from drhiro_api.config import get_settings
    import os
    user = db.query(MedicalRecord).first()
    # create user/record via ingest
    files = [("files", (f"u{i}.png", _png_variant(bytes([i])), "image/png")) for i in range(23)]
    r = client.post("/api/v1/tools/ingest_medical_batch", files=files, data={"batch_key": "dedupe-227"}, headers=service_headers)
    assert r.status_code == 200, r.text
    staged = r.json()["data"]
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == staged["record_id"]).first()
    owner = rec.user_id
    rec.content = "SYNTHETIC transcription keep-me"
    # plant duplicates: copy each unique file 9 more times, shuffle order tail
    base = list(rec.source_image_paths_json)
    extra = []
    for rep in range(9):
        if len(base) + len(extra) >= 227:
            break
        for rel in base:
            if len(base) + len(extra) >= 227:
                break
            src = os.path.join(str(scans_dir), rel)
            data = open(src, "rb").read()
            # store_scan would need user obj; write directly with new name
            new_rel = rel.replace(".png", f"_copy{rep}.png")
            dst = os.path.join(str(scans_dir), new_rel)
            open(dst, "wb").write(data)
            extra.append(new_rel)
    all_paths = base + extra  # 23 + 207 = 230; trim to 227 to match shape
    all_paths = all_paths[:227]
    rec.source_image_paths_json = all_paths
    rec.source_image_path = all_paths[0]
    db.commit()
    before_files = sum(1 for _ in scans_dir.rglob("*.png"))
    assert len(all_paths) == 227
    r2 = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"]}, headers=service_headers)
    assert r2.status_code == 200, r2.text
    d = r2.json()["data"]
    assert d["before"] == 227, d
    assert d["after"] == 23, d
    assert d["removed_files"] == 204, d
    # kept are first-occurrence per hash
    assert d["kept_paths"] == base, (d["kept_paths"], base)
    db.refresh(rec)
    assert rec.source_image_paths_json == base
    assert rec.content == "SYNTHETIC transcription keep-me"
    after_files = sum(1 for _ in scans_dir.rglob("*.png"))
    assert after_files == 23, after_files
    for rel in base:
        assert (scans_dir / rel).is_file()
    # idempotent re-run
    r3 = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"]}, headers=service_headers)
    assert r3.status_code == 200, r3.text
    d3 = r3.json()["data"]
    assert d3["before"] == 23 and d3["after"] == 23 and d3["removed_files"] == 0, d3
    assert sum(1 for _ in scans_dir.rglob("*.png")) == 23

def test_dedupe_unknown_record_404(client, service_headers):
    import uuid
    r = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": str(uuid.uuid4())}, headers=service_headers)
    assert r.status_code == 404
