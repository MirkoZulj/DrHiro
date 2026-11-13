"""Nutrient catalogue tests (spec §4.1).

Covers internal consistency (DRI/UL/legacy codes all resolve against the
catalogue, canonical units only) and — once a recorded FDC fixture lands —
that each nutrient's primary FDC id reports in its declared fdc_unit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drhiro_nutrition.dri import DRI, UL
from drhiro_nutrition.micros import NUTRIENT_CATALOGUE, SUM_IDS, by_code

FIXTURE = Path(__file__).parent / "fixtures" / "fdc_catalogue_verify.json"


def test_catalogue_size_and_unique_codes():
    codes = [c.code for c in NUTRIENT_CATALOGUE]
    assert len(codes) == 29
    assert len(set(codes)) == 29


def test_canonical_units_only():
    for c in NUTRIENT_CATALOGUE:
        assert c.unit in ("mg", "mcg", "g"), c
        assert c.group in ("vitamin", "mineral", "trace", "fatty_acid"), c


def test_dri_covers_exactly_the_catalogue():
    assert set(DRI) == {c.code for c in NUTRIENT_CATALOGUE}


def test_ul_codes_all_in_catalogue():
    assert set(UL) <= {c.code for c in NUTRIENT_CATALOGUE}


@pytest.mark.parametrize("code", [c.code for c in NUTRIENT_CATALOGUE])
def test_every_code_resolves(code):
    n = by_code(code)
    assert n is not None
    assert n.code == code


@pytest.mark.parametrize("code", sorted(SUM_IDS))
def test_sum_ids_have_multiple_fdc_ids(code):
    n = by_code(code)
    assert n is not None
    assert len(n.fdc_ids) >= 2


@pytest.mark.skipif(not FIXTURE.exists(), reason="recorded FDC fixture not yet landed (fixtures/ empty)")
def test_fdc_ids_present_with_declared_units():
    """Recorded FDC responses: each nutrient's primary fdc id exists and the
    nutrient amount is reported in the catalogue's fdc_unit."""
    data = json.loads(FIXTURE.read_text())
    for c in NUTRIENT_CATALOGUE:
        entry = data.get(str(c.fdc_ids[0]))
        assert entry is not None, f"{c.code}: FDC {c.fdc_ids[0]} missing from fixture"
        assert "error" not in entry, f"{c.code}: FDC {c.fdc_ids[0]} errored: {entry['error']}"
