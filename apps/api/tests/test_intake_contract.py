"""Contract probes (0.12.0 Part B): response shapes for new endpoints."""
from __future__ import annotations
from tests.conftest import auth_headers
from tests.test_intake_v2 import _seed_nutrients, _user

def test_nutrients_contract(client, db):
    user = _user(db, "probe")
    r = client.get("/api/v1/intake/nutrients", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 29
    assert {"code", "label_en", "label_hr", "unit", "group"} <= set(body["codes"][0])

def test_overview_contract(client, db):
    user = _user(db, "probe2")
    _seed_nutrients(db)
    r = client.get("/api/v1/intake/overview?view=month&date=2026-11-04", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("view", "start", "end", "tz", "dri_version", "reference_profile", "days", "nutrients", "unknowns", "completeness_overall"):
        assert k in body, k
    assert 28 <= len(body["days"]) <= 31
    n = body["nutrients"][0]
    for k in ("code", "label", "unit", "group", "ref", "ref_kind", "ul", "ul_scope", "days", "summary"):
        assert k in n, k

def test_nutrition_coverage_alias_shape(client, db):
    user = _user(db, "probe3")
    _seed_nutrients(db)
    r = client.get("/api/v1/nutrition-coverage?date=2026-11-04", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alias_of"] == "/intake/day"
    assert {"key", "name", "rda", "unit", "from_supplements", "from_food", "coverage_pct"} <= set(body["rows"][0])
