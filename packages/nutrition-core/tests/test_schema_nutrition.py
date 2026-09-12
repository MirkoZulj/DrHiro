"""Unit tests for health-schema and nutrition-core."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from drhiro_schema.metrics import HEALTH_CONNECT_RECORD_MAP, MetricType
from drhiro_schema.values import BloodPressureValue, VALUE_SCHEMAS
from drhiro_nutrition.catalog import FoodItem, scale_nutrients
from drhiro_nutrition.composite import PrivateCatalog


def test_health_connect_map():
    assert HEALTH_CONNECT_RECORD_MAP["BloodPressureRecord"] == MetricType.BLOOD_PRESSURE
    assert HEALTH_CONNECT_RECORD_MAP["StepsRecord"] == MetricType.STEPS
    assert HEALTH_CONNECT_RECORD_MAP["SleepSessionRecord"] == MetricType.SLEEP


def test_bp_schema_valid():
    v = BloodPressureValue.model_validate({"systolic_mmhg": 128, "diastolic_mmhg": 78, "pulse_bpm": 64})
    assert v.systolic_mmhg == 128


def test_bp_schema_rejects_sys_lte_dia():
    with pytest.raises(ValidationError):
        BloodPressureValue.model_validate({"systolic_mmhg": 80, "diastolic_mmhg": 90})


def test_bp_schema_rejects_out_of_range():
    with pytest.raises(ValidationError):
        BloodPressureValue.model_validate({"systolic_mmhg": 500, "diastolic_mmhg": 50})


def test_all_value_schemas_present():
    for metric in ("steps", "weight", "blood_pressure", "water", "sleep", "heart_rate"):
        assert metric in VALUE_SCHEMAS


def test_scale_nutrients():
    item = FoodItem(
        external_id="x", source="open_food_facts", source_version="v3",
        display_name="Chicken breast",
        kcal_per_100g=165, protein_g_per_100g=31, carbs_g_per_100g=0, fat_g_per_100g=3.6,
    )
    totals = scale_nutrients(item, 200)
    assert totals.kcal == pytest.approx(330)
    assert totals.protein_g == pytest.approx(62)


def test_private_catalog_roundtrip(tmp_path):
    path = tmp_path / "catalog.json"
    cat = PrivateCatalog(str(path))
    saved = cat.save(FoodItem(
        external_id="", source="drhiro_private", source_version="1",
        display_name="Burek", barcode="1234567890123",
        kcal_per_100g=250, protein_g_per_100g=8,
    ))
    assert saved.external_id.startswith("private:")
    assert cat.by_barcode("1234567890123").display_name == "Burek"

    # Reload from disk
    cat2 = PrivateCatalog(str(path))
    assert cat2.by_barcode("1234567890123") is not None
