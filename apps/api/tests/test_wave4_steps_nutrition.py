"""Wave 4 (0.10.3): steps=0 bug + nutrition coverage + med composition."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from drhiro_api.services.series import _dedup_steps
from tests.conftest import auth_headers, make_user


def _rec(start, end, vj):
    return {"start_at": start, "end_at": end, "value_json": vj}


def test_dedup_steps_value_key_not_only_count():
    """Records written with value_json={'value': N} (generic data-points
    writer) must count — dashboard previously read only 'count' -> 0."""
    t0 = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    recs = [_rec(t0, t0 + timedelta(seconds=60), {"value": 500})]
    assert _dedup_steps(recs) == 500


def test_dedup_steps_zero_duration_daily_total():
    """Daily-total point records (start_at == end_at, e.g. Xiaomi CSV import)
    must count fully instead of deduping to 0 via total_sec == 0."""
    t0 = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
    recs = [_rec(t0, t0, {"count": 8123})]
    assert _dedup_steps(recs) == 8123


def test_dedup_steps_point_inside_bucket_still_skipped():
    """A point record inside a 30-min bucket interval is covered by the
    bucket and must not double-count."""
    t0 = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    recs = [
        _rec(t0, t0 + timedelta(minutes=30), {"count": 300}),
        _rec(t0 + timedelta(minutes=10), t0 + timedelta(minutes=10), {"count": 50}),
    ]
    assert _dedup_steps(recs) == 300


def test_update_medication_item_normalises_legacy_ingredient_keys(client, db):
    """0.11.0 spec §2.1: legacy ingredient keys (component/per_1_softgel_mg)
    are accepted on input and normalised into canonical shape on output.
    Unknown extra keys are dropped, not preserved verbatim."""
    user = make_user(db, "omega@test.local")
    h = auth_headers(user)
    r = client.post("/api/v1/medications", headers=h, json={
        "kind": "supplement", "name": "Omega-3",
        "ingredients_json": [{"component": "Fish oil", "per_1_softgel_mg": 1200}],
    })
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    ing = [{"component": "EPA", "per_1_softgel_mg": 360, "per_2_softgels_mg": 720,
            "custom_label_key": "x"}]
    r = client.patch(f"/api/v1/medications/{item_id}", headers=h,
                     json={"ingredients_json": ing})
    assert r.status_code == 200, r.text
    out = r.json()["ingredients_json"]
    assert len(out) == 1
    # Canonical companion shape (§2.1): legacy keys normalised, custom key gone.
    assert out[0] == {
        "name": "EPA", "nutrient_code": "epa_dha", "amount": 360, "unit": "mg",
        "per": "unit", "serving_units": 1, "form": None,
        "mapping": "suggested", "pct_dv": None,
    }


def test_nutrition_coverage_endpoint(client, db):
    user = make_user(db, "nutri@test.local")
    h = auth_headers(user)
    r = client.get("/api/v1/nutrition-coverage?date=2026-11-04", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    rows = data["rows"]
    keys = {row["key"] for row in rows}
    for k in ("vitamin_a", "vitamin_c", "vitamin_d", "vitamin_e", "vitamin_k",
              "vitamin_b1", "vitamin_b2", "vitamin_b3", "vitamin_b6",
              "folate", "vitamin_b12", "calcium", "magnesium", "zinc",
              "iron", "selenium", "omega3_epa_dha", "potassium", "iodine"):
        assert k in keys, f"missing {k}"
    mg = next(row for row in rows if row["key"] == "magnesium")
    assert mg["rda"] == 420 and mg["unit"] == "mg"  # NIH ODS adult male 31-50
    assert mg["from_food"] is None  # food DB has no micronutrients -> not fabricated


def test_composition_endpoint_per_capsule_vs_rda(client, db):
    """Canonical composition rows keyed by 'name' with per_unit/unit/ref;
    legacy component/per_1_softgel_mg input is accepted, canonical output."""
    user = make_user(db, "capsule@test.local")
    h = auth_headers(user)
    r = client.post("/api/v1/medications", headers=h, json={
        "kind": "supplement", "name": "Omega-3", "dose_amount": 2, "dose_unit": "softgel",
        "ingredients_json": [
            {"component": "EPA", "per_1_softgel_mg": 360, "per_2_softgels_mg": 720},
            {"component": "Magnesium (citrate)", "per_1_softgel_mg": 200},
            {"component": "Mystery blend", "per_1_softgel_mg": 50},
        ],
    })
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    r = client.get(f"/api/v1/medications/{item_id}/composition", headers=h)
    assert r.status_code == 200, r.text
    rows_list = r.json()["rows"]
    assert "component" not in rows_list[0]  # canonical rows keyed by 'name'
    rows = {row["name"]: row for row in rows_list}
    epa = rows["EPA"]
    assert epa["nutrient_code"] == "epa_dha"
    assert epa["per_unit"] == 360 and epa["unit"] == "mg"
    assert epa["ref"] == 250
    mg = rows["Magnesium (citrate)"]
    assert mg["nutrient_code"] == "magnesium" and mg["ref"] == 420
    assert mg["per_unit"] == 200 and mg["unit"] == "mg"
    assert rows["Mystery blend"]["nutrient_code"] is None  # unmapped, not dropped
    assert rows["Mystery blend"]["mapping"] == "none"
    assert rows["Mystery blend"]["per_unit"] == 50
    # Taken totals vs RDA: fixed taken_at so the day bucket is deterministic
    # (no dependence on server tz / time of day).
    r = client.post("/api/v1/medications/intake", headers=h,
                    json={"taken_at": "2026-11-04T08:00:00+00:00",
                          "entries": [{"item_id": item_id}]})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/medications/{item_id}/composition"
                   f"?scope=period&start=2026-11-04&end=2026-11-04", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["taken"]["doses"] == 1
    rows = {row["name"]: row for row in r.json()["rows"]}
    assert rows["EPA"]["taken_total"] == 720  # 360 per unit * dose_amount 2
    assert rows["EPA"]["pct_ref"] == 288.0    # 720 / 250 * 100


def _omega3_item(db, user, name, ingredients, dose_amount=2):
    from drhiro_api.models import DoseEvent, MedicationItem

    item = MedicationItem(
        user_id=user.id, kind="supplement", name=name,
        ingredients_json=ingredients, dose_amount=dose_amount,
        dose_unit="softgel", status="active",
    )
    db.add(item)
    db.flush()
    db.add(DoseEvent(
        user_id=user.id, item_id=item.id, status="taken",
        taken_at=datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc),
        amount=dose_amount, source="telegram",
    ))
    db.commit()
    return item


def test_omega3_epa_dha_not_double_counted_when_epa_dha_present(db):
    """Viva Naturals style label: fish oil + total omega-3 + EPA + DHA per
    softgel must count EPA+DHA exactly once ((750+285) x2 = 2070), not
    6820 from summing all four generic+specific components."""
    from drhiro_api.services.nutrition_rda import supplement_intake_for_day

    user = make_user(db, "omega3-double@test.local")
    _omega3_item(db, user, "Omega-3", [
        {"component": "Fish oil", "per_1_softgel_mg": 1250},
        {"component": "Total omega-3", "per_1_softgel_mg": 1125},
        {"component": "EPA", "per_1_softgel_mg": 750},
        {"component": "DHA", "per_1_softgel_mg": 285},
    ], dose_amount=2)
    totals = supplement_intake_for_day(db, user, date(2026, 11, 4), timezone.utc)
    assert totals["omega3_epa_dha"] == 2070


def test_omega3_generic_only_counts_generic(db):
    """An item with ONLY a generic fish-oil total still counts it."""
    from drhiro_api.services.nutrition_rda import supplement_intake_for_day

    user = make_user(db, "omega3-generic@test.local")
    _omega3_item(db, user, "Fish oil", [
        {"component": "Fish oil", "per_1_softgel_mg": 1200},
    ], dose_amount=1)
    totals = supplement_intake_for_day(db, user, date(2026, 11, 4), timezone.utc)
    assert totals["omega3_epa_dha"] == 1200


def test_omega3_dpa_not_double_counted(db):
    """DPA is not EPA/DHA: it must not add to the EPA+DHA total, alone or
    alongside EPA/DHA + generic components."""
    from drhiro_api.services.nutrition_rda import supplement_intake_for_day

    user = make_user(db, "omega3-dpa-only@test.local")
    _omega3_item(db, user, "DPA only", [
        {"component": "DPA", "per_1_softgel_mg": 100},
    ], dose_amount=1)
    totals = supplement_intake_for_day(db, user, date(2026, 11, 4), timezone.utc)
    assert totals.get("omega3_epa_dha", 0.0) == 0.0

    user2 = make_user(db, "omega3-dpa-mix@test.local")
    _omega3_item(db, user2, "Omega-3 + DPA", [
        {"component": "Fish oil", "per_1_softgel_mg": 1250},
        {"component": "EPA", "per_1_softgel_mg": 750},
        {"component": "DHA", "per_1_softgel_mg": 285},
        {"component": "DPA", "per_1_softgel_mg": 100},
    ], dose_amount=1)
    totals = supplement_intake_for_day(db, user2, date(2026, 11, 4), timezone.utc)
    assert totals["omega3_epa_dha"] == 1035


def test_vitamin_b12_not_swallowed_by_b1_keyword():
    """\"vitamin b1\" is a substring of \"vitamin b12\"; B12 must map to B12."""
    from drhiro_api.services.nutrition_rda import ingredient_to_rda

    assert ingredient_to_rda({"component": "Vitamin B12", "per_1_tablet_mcg": 10}) == (
        "vitamin_b12", 10.0,  # canonical unit = mcg
    )
    assert ingredient_to_rda({"component": "Thiamin (B1)", "per_1_tablet_mg": 6}) == (
        "vitamin_b1", 6.0,
    )
