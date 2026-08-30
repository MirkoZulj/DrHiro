"""Tool invocation: the four submission tools behave correctly against synthetic data."""
from __future__ import annotations

from drhiro_tools.server import create_visit_brief, get_demo_case, get_service_status, save_visit_brief


def test_get_demo_case_lists_synthetic_cases():
    r = get_demo_case(None)
    assert r["ok"] is True
    cases = r["cases"]
    assert len(cases) >= 2
    for c in cases:
        assert c["label"] == "SYNTHETIC"


def test_get_demo_case_by_id():
    r = get_demo_case("demo-001")
    assert r["ok"] is True
    assert r["case"]["case_id"] == "demo-001"
    assert r["notice"] == "SYNTHETIC demo data only."


def test_get_demo_case_unknown_id():
    r = get_demo_case("nope")
    assert r["ok"] is False


def test_create_visit_brief_structured_and_non_diagnostic():
    r = create_visit_brief("demo-001", "sleep and activity", "I want tips before my checkup")
    assert r["ok"] is True
    brief = r["brief"]
    assert brief["synthetic"] is True
    assert brief["case_id"] == "demo-001"
    assert "care-PREPARATION" in brief["disclaimer"]
    disc = brief["disclaimer"].lower()
    # The disclaimer must explicitly negate the medical claims — it is NOT a diagnosis,
    # prescription, triage, or a replacement for a clinician.
    assert "does not diagnose" in disc
    assert "not medical advice" in disc
    assert "prescribe" in disc and "does not" in disc
    assert "triage" in disc


def test_create_visit_brief_rejects_unknown_case():
    r = create_visit_brief("nope", "x")
    assert r["ok"] is False


def test_save_visit_brief_requires_synthetic_flag():
    # Non-synthetic brief must be refused.
    r = save_visit_brief("demo-001", {"title": "not marked"})
    assert r["ok"] is False
    assert "SYNTHETIC" in r["error"]


def test_save_visit_brief_writes_and_returns_path(tmp_path, monkeypatch):
    from pathlib import Path
    from drhiro_tools import server

    monkeypatch.setattr(server, "EXPORT_DIR", tmp_path)
    brief = {
        "title": "Care-preparation brief — demo-001",
        "synthetic": True,
        "case_id": "demo-001",
        "focus": "sleep",
    }
    r = save_visit_brief("demo-001", brief)
    assert r["ok"] is True
    assert r["saved"] is True
    saved_path = Path(r["file"])
    assert saved_path.exists()
    assert saved_path.name.startswith("visit-brief-")


def test_get_service_status_non_sensitive():
    r = get_service_status()
    assert r["ok"] is True
    assert r["service"] == "drhiro-tools"
    assert "synthetic_only" in r
    assert r["synthetic_only"] is True
    assert "telegram" not in r  # no user/session data
