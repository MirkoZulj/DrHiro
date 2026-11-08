"""to_canonical / unit conversion tests (spec §4.4 / §6).

Strictness rule: unknown units raise UnmappedUnit — never assume canonical.
"""
from __future__ import annotations

import pytest

from drhiro_nutrition.micros import UnmappedUnit, convert_value, to_canonical


def test_1000_iu_vitamin_d_is_25_mcg():
    value, assumed = to_canonical(1000, "iu", "vitamin_d", name="Cholecalciferol")
    assert value == pytest.approx(25.0)
    assert assumed is False


def test_iu_vitamin_a_retinol_vs_beta_carotene():
    retinol, _ = to_canonical(1000, "iu", "vitamin_a", name="Retinyl palmitate")
    beta, _ = to_canonical(1000, "iu", "vitamin_a", name="Beta-carotene")
    assert retinol == pytest.approx(300.0)   # 0.3 mcg RAE per IU
    assert beta == pytest.approx(150.0)      # 0.15 mcg RAE per IU


def test_iu_vitamin_e_unknown_form_flagged_assumed():
    value, assumed = to_canonical(100, "iu", "vitamin_e", name="Vitamin E")
    assert value == pytest.approx(45.0)      # falls back to dl-alpha 0.45
    assert assumed is True


def test_unknown_unit_raises_unmapped():
    with pytest.raises(UnmappedUnit):
        to_canonical(10, "furlongs", "zinc", name="Zinc")
    with pytest.raises(UnmappedUnit):
        to_canonical(10, None, "zinc", name="Zinc")
    with pytest.raises(UnmappedUnit):
        to_canonical(10, "", "zinc", name="Zinc")


def test_iu_for_undefined_nutrient_raises():
    with pytest.raises(UnmappedUnit):
        to_canonical(100, "iu", "zinc", name="Zinc")


def test_folic_acid_400_mcg_is_680_dfe():
    value, assumed = to_canonical(400, "mcg", "folate", name="Folic acid")
    assert value == pytest.approx(680.0)     # x1.7 for folic acid
    assert assumed is False


def test_methylfolate_and_dfe_labels_not_multiplied():
    methyl, _ = to_canonical(400, "mcg", "folate", name="5-MTHF methylfolate")
    dfe, _ = to_canonical(400, "mcg", "folate", name="Folate (as mcg DFE)")
    assert methyl == pytest.approx(400.0)
    assert dfe == pytest.approx(400.0)


def test_mass_conversions():
    assert to_canonical(1, "g", "magnesium")[0] == pytest.approx(1000.0)
    assert to_canonical(1000, "mcg", "magnesium")[0] == pytest.approx(1.0)
    assert to_canonical(1000, "µg", "selenium")[0] == pytest.approx(1000.0)
    assert to_canonical(1000, "ug", "selenium")[0] == pytest.approx(1000.0)


def test_convert_value_for_backfill_units():
    # Copper: FDC reports mg, canonical is mcg.
    assert convert_value(0.9, "mg", "copper") == pytest.approx(900.0)
    # EPA+DHA: FDC reports g, canonical is mg.
    assert convert_value(0.5, "g", "epa_dha") == pytest.approx(500.0)
    # ALA: g -> g.
    assert convert_value(1.4, "g", "ala") == pytest.approx(1.4)


def test_convert_value_unknown_unit_raises():
    with pytest.raises(UnmappedUnit):
        convert_value(1, "iu", "vitamin_d")
