"""DRI lookup tests (0.12.0 Part B, spec §4.2 / §6).

Covers the B6 regression (R5): 1.3 mg for men 19-50, 1.7 mg 51+.
"""
from __future__ import annotations

import pytest

from drhiro_nutrition.dri import DRI_VERSION, age_band, reference_for


def test_b6_male_35_is_1_3():
    ref = reference_for("vitamin_b6", "male", 35)
    assert ref["value"] == 1.3
    assert ref["band"] == "31-50"
    assert not ref["assumed"]


def test_b6_male_55_is_1_7():
    ref = reference_for("vitamin_b6", "male", 55)
    assert ref["value"] == 1.7
    assert ref["band"] == "51-70"


def test_b6_female_40_is_1_3_and_55_is_1_5():
    assert reference_for("vitamin_b6", "female", 40)["value"] == 1.3
    assert reference_for("vitamin_b6", "female", 55)["value"] == 1.5


def test_iron_female_40_is_18():
    ref = reference_for("iron", "female", 40)
    assert ref["value"] == 18
    assert ref["kind"] == "RDA"


def test_iron_female_55_drops_to_8():
    assert reference_for("iron", "female", 55)["value"] == 8


def test_missing_sex_and_age_assumed_male_31_50():
    ref = reference_for("vitamin_b6", None, None)
    assert ref["assumed"] is True
    assert ref["sex_used"] == "m"
    assert ref["band"] == "31-50"
    assert ref["value"] == 1.3


def test_missing_age_only_assumed():
    ref = reference_for("magnesium", "male", None)
    assert ref["assumed"] is True
    assert ref["value"] == 420


def test_magnesium_bands():
    assert reference_for("magnesium", "male", 25)["value"] == 400
    assert reference_for("magnesium", "male", 45)["value"] == 420
    assert reference_for("magnesium", "female", 25)["value"] == 310
    assert reference_for("magnesium", "female", 45)["value"] == 320


def test_ai_kind_flagged():
    assert reference_for("vitamin_k", "male", 40)["kind"] == "AI"
    assert reference_for("epa_dha", "male", 40)["kind"] == "EFSA AI"
    assert reference_for("zinc", "male", 40)["kind"] == "RDA"


def test_ul_scope_present_for_magnesium():
    ref = reference_for("magnesium", "male", 40)
    assert ref["ul"] == 350
    assert ref["ul_scope"] == "supplemental only"


def test_version_tag():
    assert reference_for("zinc", "male", 40)["version"] == DRI_VERSION
    assert DRI_VERSION == "nasem-2024.1"


@pytest.mark.parametrize("age,band", [
    (19, "19-30"), (30, "19-30"), (31, "31-50"), (50, "31-50"),
    (51, "51-70"), (70, "51-70"), (71, "71+"), (90, "71+"),
])
def test_age_bands(age, band):
    assert age_band(age) == band
