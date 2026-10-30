"""Jev (TypeSafe System One) verification cascade.

No network: the Jev HTTP call is replaced by a stub, and the local resolver /
online sources are injected. These tests pin the two hard guarantees:

* unconfigured Jev == the pre-Jev behaviour, exactly;
* a Jev error/timeout/malformed answer never breaks a search.
"""

from __future__ import annotations

import httpx
import pytest

from drhiro_api.config import get_settings
from drhiro_api.services import intelligent_food_search as ifs

pytestmark = pytest.mark.usefixtures("db")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _Match:
    def __init__(self, name, tier=0):
        self.food = type("F", (), {"display_name": name})()
        self.tier = tier


class _Result:
    def __init__(self, names, ambiguous=False):
        self.matches = [_Match(n) for n in names]
        self.ambiguous = ambiguous

    def __bool__(self):
        return bool(self.matches)


@pytest.fixture
def jev_on(monkeypatch):
    monkeypatch.setenv("DRHIRO_JEV_URL", "https://jev.example/v1")
    monkeypatch.setenv("DRHIRO_JEV_API_KEY", "test-key-not-a-real-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_ddg_network(monkeypatch):
    """Never let a test hit the DuckDuckGo service."""
    monkeypatch.setattr(ifs, "_ddg_nutrition", lambda query: [])


def _install_jev(monkeypatch, scores=None, raises=False):
    """Replace jev_client's httpx.Client with a deterministic stub.

    scores: dict {candidate_name: score} or callable(name)->score|None.
    """
    import drhiro_api.services.jev_client as jc

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            if raises:
                raise httpx.ConnectError("stub: Jev unreachable")
            state = (json or {}).get("state", "")
            name = ""
            if 'Candidate food name: "' in state:
                name = state.split('Candidate food name: "', 1)[1].rsplit('"', 1)[0]
            score = scores(name) if callable(scores) else (scores or {}).get(name)
            if score is None:
                # Malformed / unexpected answer shape.
                return _Resp({"answers": {"match": {"type": "not-noul"}}})
            return _Resp({"answers": {"match": {"type": "noul", "noul": score}}})

    monkeypatch.setattr(jc.httpx, "Client", _Client)


def _patch_resolver(monkeypatch, result, captured):
    def fake_resolve(db, q, limit=5, user_id=None):
        captured.append(q)
        return result
    monkeypatch.setattr(ifs, "resolve_food", fake_resolve)


def _patch_nutrients(monkeypatch):
    monkeypatch.setattr(ifs, "_extract_nutrients_per_100g", lambda db, food: {})


# --------------------------------------------------------------------------
# 1. unconfigured Jev == behaviour today (regression guard)
# --------------------------------------------------------------------------
def test_jev_unconfigured_matches_pre_jev_behaviour(monkeypatch, db):
    monkeypatch.delenv("DRHIRO_JEV_URL", raising=False)
    monkeypatch.delenv("DRHIRO_JEV_API_KEY", raising=False)
    get_settings.cache_clear()

    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)

    def _must_not_translate(text):
        raise AssertionError("translation must not run when Jev is unconfigured")

    monkeypatch.setattr(ifs, "translate_food_to_english", _must_not_translate)

    out = ifs.search_food_intelligent(db, "kava", user_id=None)

    assert out["source"] == "database"
    assert out["needs_user_selection"] is False
    assert [c["display_name"] for c in out["candidates"]] == ["Coffee, brewed"]
    assert out["query"] == "kava"
    # The matcher received the UNtranslated query, exactly as before.
    assert captured == ["kava"]


# --------------------------------------------------------------------------
# 2. high score -> accepted silently
# --------------------------------------------------------------------------
def test_jev_high_score_accepts_candidate(monkeypatch, db, jev_on):
    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    monkeypatch.setattr(ifs, "translate_food_to_english", lambda t: t)
    _install_jev(monkeypatch, {"Coffee, brewed": 0.95})

    out = ifs.search_food_intelligent(db, "kava")

    assert out["source"] == "database"
    assert out["needs_user_selection"] is False
    assert out["candidates"][0]["display_name"] == "Coffee, brewed"


# --------------------------------------------------------------------------
# 3. middle band -> surfaced but NOT silently accepted (user is asked)
# --------------------------------------------------------------------------
def test_jev_middle_band_asks_user(monkeypatch, db, jev_on):
    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    monkeypatch.setattr(ifs, "translate_food_to_english", lambda t: t)
    _install_jev(monkeypatch, {"Coffee, brewed": 0.70})

    out = ifs.search_food_intelligent(db, "kava")

    assert out["source"] == "database"
    assert out["needs_user_selection"] is True  # not auto-accepted
    assert [c["display_name"] for c in out["candidates"]] == ["Coffee, brewed"]


# --------------------------------------------------------------------------
# 4. low score -> rejected, cascade falls through to the next source
# --------------------------------------------------------------------------
def test_jev_low_score_falls_through_to_next_source(monkeypatch, db, jev_on):
    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    monkeypatch.setattr(ifs, "translate_food_to_english", lambda t: t)

    # DB candidate scores low; the DDG candidate is a genuine match.
    _install_jev(monkeypatch, lambda name: 0.95 if name == "Coffee" else 0.10)

    sources_tried: list[str] = []

    def fake_online(query, limit=5, source="auto"):
        sources_tried.append(source)
        if source == "ddg":
            return [{
                "external_id": "ddg:coffee",
                "display_name": "Coffee",
                "kcal_per_100g": 1.0,
                "protein_g_per_100g": 0.1,
                "carbs_g_per_100g": 0.0,
                "fat_g_per_100g": 0.0,
                "fiber_per_100g": None,
                "sodium_mg_per_100g": None,
                "source": "duckduckgo",
            }]
        return []

    monkeypatch.setattr(ifs, "online_food_candidates", fake_online)

    out = ifs.search_food_intelligent(db, "kava")

    assert sources_tried == ["usda", "ddg"]      # USDA empty -> DuckDuckGo
    assert out["source"] == "duckduckgo"
    assert out["needs_user_selection"] is False
    assert out["candidates"][0]["display_name"] == "Coffee"


# --------------------------------------------------------------------------
# 5. Jev raises/times out -> search still completes via the pre-existing path
# --------------------------------------------------------------------------
def test_jev_failure_degrades_to_pre_jev_path(monkeypatch, db, jev_on):
    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    monkeypatch.setattr(ifs, "translate_food_to_english", lambda t: t)
    _install_jev(monkeypatch, raises=True)

    out = ifs.search_food_intelligent(db, "kava")

    assert out["source"] == "database"
    assert [c["display_name"] for c in out["candidates"]] == ["Coffee, brewed"]
    # Unverified -> the ranker's own ambiguity verdict decides, as pre-Jev.
    assert out["needs_user_selection"] is False


def test_jev_malformed_answer_degrades(monkeypatch, db, jev_on):
    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["Coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    monkeypatch.setattr(ifs, "translate_food_to_english", lambda t: t)
    _install_jev(monkeypatch, scores=lambda name: None)  # unexpected answer shape

    out = ifs.search_food_intelligent(db, "kava")

    assert out["source"] == "database"
    assert out["needs_user_selection"] is False


# --------------------------------------------------------------------------
# 6. translation runs BEFORE the cascade (translated query reaches the matcher)
# --------------------------------------------------------------------------
def test_translation_runs_before_cascade(monkeypatch, db, jev_on):
    seen_inputs: list[str] = []

    def fake_translate(text):
        seen_inputs.append(text)
        return "coffee, brewed"

    monkeypatch.setattr(ifs, "translate_food_to_english", fake_translate)

    captured: list[str] = []
    _patch_resolver(monkeypatch, _Result(["coffee, brewed"]), captured)
    _patch_nutrients(monkeypatch)
    _install_jev(monkeypatch, {"coffee, brewed": 0.95})

    out = ifs.search_food_intelligent(db, "kava")

    assert seen_inputs == ["kava"]
    assert captured == ["coffee, brewed"]      # matcher got the TRANSLATED query
    assert out["needs_user_selection"] is False
