"""Render tests (spec §3.2 render.py): every catalogue key has a label in both
locales, number formatting, D3 banned-wording guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.render import (
    LABELS,
    assert_no_banned_words,
    fmt_date,
    fmt_num,
    render_finding,
)
from drhiro_analytics.thresholds import BANNED_WORDS
from drhiro_analytics.types import Finding


def _level_finding(direction="down", **kw):
    base = dict(
        id="x", kind="level_change", metric="steps", direction=direction,
        anchor_date=date(2026, 5, 1), before=8000, after=6000, delta=-2000, unit="steps",
        stats={"n_after": 28, "coverage_after": 0.9}, confidence="high", text="",
    )
    base.update(kw)
    return Finding(**base)


def test_every_catalogue_key_has_label_in_both_locales():
    for key in METRIC_CATALOGUE:
        assert key in LABELS["en"], key
        assert key in LABELS["hr"], key


def test_number_formatting():
    assert fmt_num(430, "min") == "7h10"
    assert fmt_num(82.34, "kg") == "82.3"
    assert fmt_num(1250.0, "ml") == "1.2 L"
    assert fmt_num(7234.5, "steps") == "7234"
    assert fmt_num(125.4, "mmHg") == "125"


def test_date_formatting():
    assert fmt_date(date(2026, 7, 15), "en") == "15 Jul 2026"
    assert fmt_date(date(2026, 7, 15), "hr") == "15. srp 2026."


def test_rendered_findings_contain_no_banned_words():
    for locale in ("en", "hr"):
        for direction in ("up", "down"):
            txt = render_finding(_level_finding(direction=direction), locale)
            assert txt
            assert assert_no_banned_words(txt), (locale, direction, txt)
    # sanity: the guard actually rejects causal wording
    assert not assert_no_banned_words("Steps dropped due to the new job.")
    for w in BANNED_WORDS:
        assert not assert_no_banned_words(f"x {w} y")


def test_partial_suffix():
    f = _level_finding(stats={"n_after": 6, "coverage_after": 0.9, "partial": True})
    txt = render_finding(f, "en")
    assert "still settling" in txt
