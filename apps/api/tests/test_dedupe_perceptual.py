"""Perceptual (aHash) dedupe: recompressed variants cluster, distinct pages don't. SYNTHETIC ONLY."""
import io
import random
import pytest
from PIL import Image
from drhiro_api.models import MedicalRecord
from tests.test_medical_batch import service_headers, scans_dir  # noqa: F401

def _patterns(n):
    pats = []
    rng = random.Random(42)
    while len(pats) < n:
        bits = [rng.random() < 0.5 for _ in range(64)]
        if all(sum(a != b for a, b in zip(bits, p)) > 12 for p in pats):
            pats.append(bits)
    return pats

_PATTERNS = _patterns(10)

def _page(idx, w=320, h=320):
    bits = _PATTERNS[idx % len(_PATTERNS)]
    im = Image.new("RGB", (w, h), "white")
    cell_w, cell_h = w // 8, h // 8
    px = im.load()
    for r in range(8):
        for c in range(8):
            col = (0, 0, 0) if bits[r * 8 + c] else (255, 255, 255)
            for y in range(r * cell_h, (r + 1) * cell_h):
                for x in range(c * cell_w, (c + 1) * cell_w):
                    px[x, y] = col
    return im

def _to_bytes(im, fmt="PNG", **kw):
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()

def _recompressed(im):
    small = im.resize((im.width // 2, im.height // 2))
    return _to_bytes(small, "JPEG", quality=85)

def _stage(client, headers, db, scans_dir, n_pages, copies=2):
    files = [("files", (f"page{i}.png", _to_bytes(_page(i)), "image/png")) for i in range(n_pages)]
    r = client.post("/api/v1/tools/ingest_medical_batch", files=files, data={"batch_key": "perc-test"}, headers=headers)
    assert r.status_code == 200, r.text
    staged = r.json()["data"]
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == staged["record_id"]).first()
    rec.content = "SYNTHETIC transcription keep-me"
    base = list(rec.source_image_paths_json)
    extra = []
    for rep in range(copies):
        for i, rel in enumerate(base):
            im = _page(i)
            data = _recompressed(im)
            new_rel = rel.replace(".png", f"_re{rep}.jpg")
            (scans_dir / new_rel).write_bytes(data)
            extra.append(new_rel)
    rec.source_image_paths_json = base + extra
    rec.source_image_path = base[0]
    db.commit()
    return staged, base

def test_perceptual_recompressed_cluster_to_n_and_idempotent(client, service_headers, scans_dir, db):
    staged, base = _stage(client, service_headers, db, scans_dir, n_pages=5, copies=2)
    assert len(base) == 5
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == staged["record_id"]).first()
    assert len(rec.source_image_paths_json) == 15
    r0 = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"], "mode": "perceptual", "dry_run": True}, headers=service_headers)
    assert r0.status_code == 200, r0.text
    d0 = r0.json()["data"]
    assert d0["dry_run"] is True and d0["after"] == 5 and d0["clusters"] == 5, d0
    assert sum(1 for _ in scans_dir.rglob("*") if _.is_file()) == 15
    db.refresh(rec)
    assert len(rec.source_image_paths_json) == 15
    r = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"], "mode": "perceptual"}, headers=service_headers)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["before"] == 15 and d["after"] == 5 and d["clusters"] == 5 and d["removed_files"] == 10, d
    assert d["kept_paths"] == base, (d["kept_paths"], base)
    db.refresh(rec)
    assert rec.source_image_paths_json == base
    assert rec.content == "SYNTHETIC transcription keep-me"
    assert sum(1 for _ in scans_dir.rglob("*") if _.is_file()) == 5
    for c in d["cluster_summary"]:
        assert c["max_distance"] <= 4
    r2 = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"], "mode": "perceptual"}, headers=service_headers)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()["data"]
    assert d2["before"] == 5 and d2["after"] == 5 and d2["removed_files"] == 0, d2

def test_perceptual_distinct_pages_do_not_merge(client, service_headers, scans_dir, db):
    staged, base = _stage(client, service_headers, db, scans_dir, n_pages=4, copies=0)
    r = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"], "mode": "perceptual"}, headers=service_headers)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["after"] == 4 and d["clusters"] == 4 and d["removed_files"] == 0, d

def test_perceptual_invalid_threshold_400(client, service_headers, scans_dir, db):
    staged, base = _stage(client, service_headers, db, scans_dir, n_pages=2, copies=0)
    r = client.post("/api/v1/tools/dedupe_record_scans", json={"record_id": staged["record_id"], "mode": "perceptual", "hamming_threshold": 99}, headers=service_headers)
    assert r.status_code == 400, r.text
