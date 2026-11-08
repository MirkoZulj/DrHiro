"""0.11.0-a meds v2 tests (spec §6 meds)."""
from __future__ import annotations
import uuid
from datetime import date, datetime, timezone
from tests.conftest import auth_headers
from drhiro_api.services.schemas_meds import normalise_ingredient, suggest_nutrient_code

def test_legacy_normaliser_exclusions():
    # §6: calcium pantothenate -> pantothenic (suggested), not calcium, not None.
    assert suggest_nutrient_code("calcium pantothenate") == "pantothenic"
    assert suggest_nutrient_code("magnesium stearate") is None
    assert suggest_nutrient_code("omega-6") is None

def test_normalise_legacy_per2_halved():
    e = normalise_ingredient({"component": "Zinc", "per_2_mg": 30})
    assert e.amount == 15 and e.unit == "mg" and e.nutrient_code == "zinc"

def test_unknown_code_422(client, db, user_a):
    r = client.post("/api/v1/medications", json={"kind": "supplement", "name": "X",
        "ingredients_json": [{"name": "Zinc", "nutrient_code": "bogus", "amount": 5, "unit": "mg"}]},
        headers=auth_headers(user_a))
    assert r.status_code == 422, r.text

def _create(client, user, **kw):
    payload = {"kind": "supplement", "name": "Vitamin D3", "dose_amount": 2, "dose_unit": "capsule", "unit_label": "capsule"}
    payload.update(kw)
    return client.post("/api/v1/medications", json=payload, headers=auth_headers(user))

def test_create_reminder_ignored_with_warning(client, db, user_a):
    r = _create(client, user_a, create_reminder=True, schedule_json={"type": "daily", "times": ["08:00"]})
    assert r.status_code in (200, 201), r.text
    assert r.json()["schedule_json"] is None
    assert "create_reminder is ignored" in r.json()["warnings"]

def test_pause_410_and_unarchive(client, db, user_a):
    item_id = _create(client, user_a).json()["id"]
    assert client.post(f"/api/v1/medications/{item_id}/pause", json={}, headers=auth_headers(user_a)).status_code == 410
    client.post(f"/api/v1/medications/{item_id}/stop", json={}, headers=auth_headers(user_a))
    assert db.query(__import__("drhiro_api.models", fromlist=["MedicationItem"]).MedicationItem).filter_by(id=uuid.UUID(item_id)).first().status == "stopped"
    r = client.post(f"/api/v1/medications/{item_id}/unarchive", json={}, headers=auth_headers(user_a))
    assert r.json()["status"] == "active"

def test_intake_only_days_and_bulk_idempotency(client, db, user_a):
    r = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a))
    assert r.json()["days"] == [] and r.json()["items"] == []
    item_id = _create(client, user_a).json()["id"]
    payload = {"entries": [{"item_id": item_id, "amount": 2}]}
    r = client.post("/api/v1/medications/intake", json=payload, headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    assert len(r.json()["doses"]) == 1 and r.json()["doses"][0]["scheduled_for"] is None
    r2 = client.post("/api/v1/medications/intake", json=payload, headers=auth_headers(user_a))
    assert r2.json()["doses"] == [] and item_id in r2.json()["already_logged"]
    # 21 entries -> 422
    too_many = {"entries": [{"item_id": item_id} for _ in range(21)]}
    assert client.post("/api/v1/medications/intake", json=too_many, headers=auth_headers(user_a)).status_code == 422
    r = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a))
    assert len(r.json()["days"]) == 1 and r.json()["items"][0]["total_amount"] == 2

def test_composition_serving_units_halved(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[{"name": "Zinc", "nutrient_code": "zinc", "amount": 20, "unit": "mg", "per": "serving", "serving_units": 2}]).json()["id"]
    r = client.get(f"/api/v1/medications/{item_id}/composition?scope=unit", headers=auth_headers(user_a))
    assert r.json()["rows"][0]["per_unit"] == 10

def test_composition_period_day_and_legacy_codes(client, db, user_a):
    # Prod traceback regression: scope=period with start/end hit
    # AttributeError 'NoneType' has no 'fromisoformat' (param shadowed date).
    item_id = _create(client, user_a).json()["id"]
    # Simulate pre-0.11 rows written directly to DB (bypass API normalisation)
    from drhiro_api.models import MedicationItem
    item = db.query(MedicationItem).filter_by(id=uuid.UUID(item_id)).first()
    item.ingredients_json = [
        {"name": "Thiamin", "nutrient_code": "vitamin_b1", "amount": 1.2, "unit": "mg", "per": "capsule"},
        {"name": "Omega-3 EPA/DHA", "nutrient_code": "omega3_epa_dha", "amount": 300, "unit": "mg", "per": "unit"},
        {"name": "Glucosamine", "nutrient_code": "bogus_code", "amount": 500, "unit": "mg", "per": "unit"},
    ]
    db.commit()
    # Log a dose at a fixed taken_at so day/period scopes are
    # deterministic (no dependence on server tz / time of day).
    r = client.post("/api/v1/medications/intake", json={
        "taken_at": "2026-11-04T08:00:00+00:00",
        "entries": [{"item_id": item_id}]}, headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    day = "2026-11-04"
    window = f"start={day}&end={day}"
    r = client.get(f"/api/v1/medications/{item_id}/composition?scope=period&{window}", headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "period" and body["taken"]["doses"] == 1
    by_name = {row["name"]: row for row in body["rows"]}
    # Legacy RDA codes mapped before validation
    assert by_name["Thiamin"]["nutrient_code"] == "thiamin"
    assert by_name["Omega-3 EPA/DHA"]["nutrient_code"] == "epa_dha"
    # per='capsule' row degraded, not 500; amount preserved
    assert by_name["Thiamin"]["per_unit"] == 1.2
    # Unknown code degraded to unmapped, never 500
    assert by_name["Glucosamine"]["nutrient_code"] is None
    assert by_name["Glucosamine"]["mapping"] == "none"
    assert by_name["Glucosamine"]["per_unit"] == 500
    # Day scope (same shadowing bug, line 431)
    r = client.get(f"/api/v1/medications/{item_id}/composition?scope=day&date={day}", headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    assert r.json()["taken"]["doses"] == 1
    # Missing/bad params are 400, not 500
    assert client.get(f"/api/v1/medications/{item_id}/composition?scope=period", headers=auth_headers(user_a)).status_code == 400
    assert client.get(f"/api/v1/medications/{item_id}/composition?scope=period&start=nope&end={day}", headers=auth_headers(user_a)).status_code == 400
    assert client.get(f"/api/v1/medications/{item_id}/composition?scope=day", headers=auth_headers(user_a)).status_code == 400
    assert client.get(f"/api/v1/medications/{item_id}/composition?scope=day&date=nope", headers=auth_headers(user_a)).status_code == 400

def test_confirm_mapping_preserves_other_rows(client, db, user_a):
    # Regression (2026-11-06): clicking "Confirm mapping" in the composition
    # modal corrupted the table to a single Vitamin A row with dashes. The
    # web client read ingredients_json from the top level of GET /{id}
    # (which wraps the item under "item") and PATCHed a single amount-less
    # entry over the whole list. The server-side confirm endpoint flips one
    # row's mapping in place so no client payload can wipe the other rows.
    item_id = _create(client, user_a, ingredients_json=[
        {"component": "Vitamin A", "per_1_capsule_mcg": 900},
        {"component": "Vitamin C", "per_1_capsule_mg": 90},
        {"component": "Vitamin D", "per_1_capsule_mcg": 50},
    ]).json()["id"]
    r = client.post(f"/api/v1/medications/{item_id}/confirm-mapping",
                    json={"name": "Vitamin A", "nutrient_code": "vitamin_a"},
                    headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    stored = r.json()["ingredients_json"]
    assert len(stored) == 3
    by_name = {e["name"]: e for e in stored}
    assert by_name["Vitamin A"]["mapping"] == "confirmed"
    assert by_name["Vitamin A"]["amount"] == 900 and by_name["Vitamin A"]["unit"] == "mcg"
    assert by_name["Vitamin C"]["amount"] == 90 and by_name["Vitamin C"]["unit"] == "mg"
    assert by_name["Vitamin D"]["amount"] == 50 and by_name["Vitamin D"]["unit"] == "mcg"
    # Composition (incl. period scope) still shows every row with amounts/refs
    client.post("/api/v1/medications/intake", json={"entries": [{"item_id": item_id}]}, headers=auth_headers(user_a))
    today = date.today().isoformat()
    r = client.get(f"/api/v1/medications/{item_id}/composition?scope=period&start={today}&end={today}",
                   headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    rows = {row["name"]: row for row in r.json()["rows"]}
    assert set(rows) == {"Vitamin A", "Vitamin C", "Vitamin D"}
    assert rows["Vitamin A"]["per_unit"] == 900 and rows["Vitamin A"]["mapping"] == "confirmed"
    assert rows["Vitamin C"]["per_unit"] == 90 and rows["Vitamin D"]["per_unit"] == 50
    assert rows["Vitamin A"]["pct_ref"] is not None

def test_confirm_mapping_unknown_ingredient_404(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[
        {"name": "Zinc", "nutrient_code": "zinc", "amount": 5, "unit": "mg"}]).json()["id"]
    r = client.post(f"/api/v1/medications/{item_id}/confirm-mapping",
                    json={"name": "Nope"}, headers=auth_headers(user_a))
    assert r.status_code == 404, r.text
    # Nothing was changed
    stored = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_a)).json()["item"]["ingredients_json"]
    assert len(stored) == 1 and stored[0]["amount"] == 5

def test_patch_dose(client, db, user_a):
    item_id = _create(client, user_a).json()["id"]
    dose = client.post("/api/v1/medications/intake", json={"entries": [{"item_id": item_id}]}, headers=auth_headers(user_a)).json()["doses"][0]
    r = client.patch(f"/api/v1/medications/doses/{dose['id']}", json={"amount": 3}, headers=auth_headers(user_a))
    assert r.json()["amount"] == 3

def test_medical_record_patch(client, db, user_a):
    # create via tools needs service token; use JWT? medical records have no JWT create, only PATCH — create direct DB
    from drhiro_api.models import MedicalRecord
    rec = MedicalRecord(user_id=user_a.id, title="Krvna kartica - zapis 2000", content="Eye exam synthetic")
    db.add(rec); db.commit(); db.refresh(rec)
    r = client.patch(f"/api/v1/medical/records/{rec.id}", json={"title": "Eye exam"}, headers=auth_headers(user_a))
    assert r.json()["title"] == "Eye exam"
    r2 = client.patch(f"/api/v1/medical/records/{rec.id}", json={"content": "Corrected text"}, headers=auth_headers(user_a))
    assert r2.json()["content"] == "Corrected text"


# ── Owner-requested Meds editing (2026-11-07): pct_dv, short_name, Edit ──

def test_pct_dv_round_trip_create_and_composition(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[
        {"name": "Magnesium", "nutrient_code": "magnesium", "amount": 200, "unit": "mg", "pct_dv": 48},
        {"name": "Zinc", "nutrient_code": "zinc", "amount": 11, "unit": "mg"},
    ]).json()["id"]
    stored = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_a)).json()["item"]["ingredients_json"]
    by_name = {e["name"]: e for e in stored}
    assert by_name["Magnesium"]["pct_dv"] == 48
    assert by_name["Zinc"]["pct_dv"] is None
    body = client.get(f"/api/v1/medications/{item_id}/composition?scope=unit", headers=auth_headers(user_a)).json()
    rows = {r["name"]: r for r in body["rows"]}
    assert rows["Magnesium"]["pct_dv"] == 48
    assert rows["Zinc"]["pct_dv"] is None

def test_patch_edit_preserves_and_updates_rows(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[
        {"name": "Vitamin D3", "nutrient_code": "vitamin_d", "amount": 25, "unit": "mcg", "pct_dv": 125},
        {"name": "Vitamin K2", "nutrient_code": "vitamin_k", "amount": 90, "unit": "mcg", "pct_dv": 75},
    ]).json()["id"]
    # Full edit payload as sent by the Edit composition popup.
    r = client.patch(f"/api/v1/medications/{item_id}", json={
        "name": "Vitamin D3 + K2", "short_name": "D3K2", "brand": "Acme",
        "dose_amount": 1, "dose_unit": "capsule", "unit_label": "capsule",
        "ingredients_json": [
            {"name": "Vitamin D3", "amount": 25, "unit": "mcg", "nutrient_code": "vitamin_d",
             "per": "unit", "serving_units": 1, "form": None, "mapping": "confirmed", "pct_dv": 125},
            {"name": "Vitamin K2", "amount": 90, "unit": "mcg", "nutrient_code": "vitamin_k",
             "per": "unit", "serving_units": 1, "form": None, "mapping": "confirmed", "pct_dv": 75},
            {"name": "New row", "amount": 10, "unit": "mg", "nutrient_code": None,
             "per": "unit", "serving_units": 1, "form": None, "mapping": "none", "pct_dv": None},
        ],
    }, headers=auth_headers(user_a))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "Vitamin D3 + K2" and out["short_name"] == "D3K2" and out["brand"] == "Acme"
    assert len(out["ingredients_json"]) == 3
    rows = {e["name"]: e for e in out["ingredients_json"]}
    assert rows["Vitamin D3"]["amount"] == 25 and rows["Vitamin D3"]["pct_dv"] == 125
    assert rows["Vitamin K2"]["amount"] == 90 and rows["Vitamin K2"]["pct_dv"] == 75
    # Composition reflects the edit
    body = client.get(f"/api/v1/medications/{item_id}/composition?scope=unit", headers=auth_headers(user_a)).json()
    assert {row["name"] for row in body["rows"]} == {"Vitamin D3", "Vitamin K2", "New row"}

def test_short_name_stored_and_returned_in_intake(client, db, user_a):
    item_id = _create(client, user_a, short_name="Mg").json()["id"]
    item = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_a)).json()["item"]
    assert item["short_name"] == "Mg"
    client.post("/api/v1/medications/intake", json={"entries": [{"item_id": item_id}]}, headers=auth_headers(user_a))
    body = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a)).json()
    assert body["days"][0]["doses"][0]["short_name"] == "Mg"
    assert body["items"][0]["short_name"] == "Mg"
    # PATCH can update and clear it; intake then falls back to the computed name
    client.patch(f"/api/v1/medications/{item_id}", json={"short_name": "MultiV"}, headers=auth_headers(user_a))
    item = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_a)).json()["item"]
    assert item["short_name"] == "MultiV"
    client.patch(f"/api/v1/medications/{item_id}", json={"short_name": ""}, headers=auth_headers(user_a))
    item = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_a)).json()["item"]
    assert item["short_name"] is None
    body = client.get("/api/v1/medications/intake?view=week", headers=auth_headers(user_a)).json()
    assert body["days"][0]["doses"][0]["short_name"] == "Vitamin D3"

def test_pct_dv_negative_rejected(client, db, user_a):
    item_id = _create(client, user_a, ingredients_json=[
        {"name": "Zinc", "nutrient_code": "zinc", "amount": 5, "unit": "mg"}]).json()["id"]
    r = client.patch(f"/api/v1/medications/{item_id}", json={
        "ingredients_json": [{"name": "Zinc", "nutrient_code": "zinc", "amount": 5, "unit": "mg", "pct_dv": -10}],
    }, headers=auth_headers(user_a))
    assert r.status_code == 422, r.text

def _web_src(relpath):
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    return (root / relpath).read_text(encoding="utf-8")

def test_confirm_mapping_ui_removed():
    tsx = _web_src("apps/web/src/views/Medications.tsx")
    assert "confirm-mapping" not in tsx
    assert "Confirm mapping" not in tsx
    assert "confirmMapping" not in tsx

def test_taken_strip_removed():
    tsx = _web_src("apps/web/src/views/Medications.tsx")
    assert "meds-period-strip" not in tsx
    assert "meds-period-chip" not in tsx
    assert "Taken this {view}" not in tsx
    assert "sortedPeriodItems" not in tsx

def test_chip_layout_and_edit_popup_present():
    tsx = _web_src("apps/web/src/views/Medications.tsx")
    css = _web_src("apps/web/src/index.css")
    assert "meds-chip-name" in tsx and "meds-chip-name" in css
    assert "meds-chip-icon" in tsx and "flex: none" in css
    assert "min-width: 0" in css
    assert "EditCompositionForm" in tsx
    assert "Edit composition" in tsx
    assert "% DV (label)" in tsx
    assert "pct_dv" in tsx
    assert "short_name" in tsx
    assert "Add component" in tsx and "Remove" in tsx

def test_migration_single_head_and_guarded():
    import pathlib, re
    versions = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
    revisions, downs = {}, set()
    for f in versions.glob("*.py"):
        t = f.read_text(encoding="utf-8")
        rev = re.search(r"^revision = \"([^\"]+)\"", t, re.M)
        down = re.search(r"^down_revision = \"([^\"]+)\"", t, re.M)
        if rev:
            revisions[rev.group(1)] = f.name
            if down:
                downs.add(down.group(1))
    heads = set(revisions) - downs
    assert heads == {"e2f3a4b5c6d7"}, heads  # 0.12.0 Part B head (was a7b8c9d0e1f2)
    mig = (versions / "a7b8c9d0e1f2_meds_short_name.py").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS short_name" in mig

def test_week_chip_bundled_fan_icons():
    import re as _re
    tsx = _web_src("apps/web/src/views/Medications.tsx")
    css = _web_src("apps/web/src/index.css")
    # Owner 2026-11-08 reference ("Wrong again. Something like this"):
    # bundled fan — ONE svg, N filled leaves/capsules fanned from one base,
    # alternating light/dark greens, tips distinct.
    assert "function LeafBundleIcon" in tsx and "function CapsuleBundleIcon" in tsx
    assert "function FanBundle" in tsx
    assert "function LeafIcon" not in tsx and "function CapsuleIcon" not in tsx
    assert "PillIcon" not in tsx
    assert "ChipClusterIcon" in tsx
    assert "meds-chip-icon" in tsx and "meds-chip-icon" in css
    assert "meds-chip-icon--leaf" in tsx and "meds-chip-icon--capsule" in tsx
    assert "meds-chip-icons" not in tsx and "meds-chip-icons" not in css
    assert "c.first.kind === 'medication'" in tsx
    assert "isMedication ? <CapsuleBundleIcon" in tsx and ": <LeafBundleIcon" in tsx
    # One svg per count: FAN_ANGLES gives N rays, rendered once via angles.map.
    assert "FAN_ANGLES" in tsx and "BUNDLE_WIDTHS" in tsx
    for _n, _angles in [(1, "[0]"), (2, "[-22, 22]"), (3, "[-34, 0, 34]"),
                        (4, "[-42, -14, 14, 42]"), (5, "[-50, -25, 0, 25, 50]")]:
        assert _angles in tsx, (_n, _angles)
    assert "angles.map((angle" in tsx
    assert "clampCount" in tsx
    assert "Math.max(1, Math.min(5, Math.round(count)))" in tsx
    # Alternating fills (light/dark token greens), filled shapes, no fan lines.
    assert "meds-bundle--light" in tsx and "meds-bundle--dark" in tsx
    assert "pos % 2 === 0" in tsx
    assert "meds-chip-bundle" in tsx and "meds-chip-bundle" in css
    assert "meds-chip-leaf-bundle" not in tsx and "meds-chip-capsule-bundle" not in tsx
    assert "meds-chip-leaf\"" not in tsx and "meds-chip-capsule\"" not in tsx
    assert "fill=\"none\"" not in tsx
    # Leaf and capsule are distinct shapes on the same fan.
    assert "function leafPath" in tsx and "BUNDLE_LEAF_LEN" in tsx
    assert "BUNDLE_CAPSULE_LEN" in tsx and "rx={BUNDLE_CAPSULE_HALF}" in tsx
    # Rendered height 18px; viewBox widths grow 14->43 (rendered 9->28px).
    assert "BUNDLE_RENDER_HEIGHT = 18" in tsx
    _widths = [14, 31, 37, 41, 43]
    assert _widths == sorted(_widths) and _widths[0] < _widths[-1]
    for _w in _widths:
        assert str(_w) in tsx
    # Chip block: single cluster component, no emoji, no xN text.
    start = tsx.index("groupDayChips(doses).map")
    chip_block = tsx[start:tsx.index("</button>", start)]
    assert "<ChipClusterIcon count={c.total} isMedication={isMedication} />" in chip_block
    assert "🌿" not in chip_block and "💊" not in chip_block
    assert "×" not in chip_block
    assert "meds-chip-count" not in tsx and "meds-chip-count" not in css
    assert "c.total > 3" not in tsx and "Math.min(3," not in tsx
    assert "meds-chip-x" not in tsx and "meds-chip-x" not in css
    assert "shortName(c.first.name, c.first.short_name)" in tsx
    # CSS: icon box 18px, bundle flex:none, alternating fills from tokens,
    # name flex:1, chip polished.
    icon_css = css[css.index(".meds-chip-icon {"):css.index(".meds-chip-icon {") + 400]
    assert "display: inline-flex" in icon_css and "flex: none" in icon_css and "height: 18px" in icon_css
    bundle_css = css[css.index(".meds-chip-bundle {"):css.index(".meds-chip-bundle {") + 300]
    assert "display: block" in bundle_css and "flex: none" in bundle_css and "height: 18px" in bundle_css
    light_css = css[css.index(".meds-bundle--light {"):css.index(".meds-bundle--light {") + 200]
    dark_css = css[css.index(".meds-bundle--dark {"):css.index(".meds-bundle--dark {") + 200]
    assert "fill: var(--color-success)" in light_css
    assert "fill: var(--color-accent)" in dark_css
    name_css = css[css.index(".meds-chip-name"):css.index(".meds-chip-name") + 300]
    assert "flex: 1 1 auto" in name_css and "min-width: 0" in name_css
    week_css = css[css.index(".meds-week"):css.index(".meds-week") + 300]
    assert "repeat(7, 1fr)" in week_css
    assert "width: 100%" in css[css.index(".meds-chip {"):css.index(".meds-chip {") + 300]
    chip_css = css[css.index(".meds-chip {"):css.index(".meds-chip {") + 800]
    assert "font-size: var(--text-sm)" in chip_css
    assert "padding: 6px 10px" in chip_css and "box-shadow:" in chip_css
    assert "min-height: 32px" in chip_css and "border-radius: var(--radius-full)" in chip_css
    tokens = _web_src("apps/web/src/design/tokens.css")
    _sm = _re.search(r"--text-sm:\s*(\d+)px", tokens)
    assert _sm and 12 <= int(_sm.group(1)) <= 13, _sm and _sm.group(0)
    # Fit budget at 13px text, widest bundle (N=5 rendered ~28px):
    # Omega(5ch) + 28px + padding/gaps ~= 91px < 105px column.
    for _label, _w in [("MultiV", 9), ("Osteo", 9), ("Mg", 20), ("Omega", 28)]:
        _needed = len(_label) * 7.5 + _w + 26
        assert _needed < 105, (_label, _needed)
