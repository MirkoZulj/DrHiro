"""Intelligent food search with a DuckDuckGo fallback.

When the local DB has no match, queries a host-side ddg-http service
(env DDG_HTTP_URL) that runs Camoufox egressing through a residential
tunnel, and extracts nutrition from the search results. All configuration
is environment-driven; no hardcoded hosts or credentials.

Jev (TypeSafe System One) verification is an OPTIONAL layer on top of that
cascade, engaged only when DRHIRO_JEV_URL and DRHIRO_JEV_API_KEY are set:

  1. the raw input is translated to English first (Jev's equivalence model is
     English-first), then
  2. every candidate from a source is scored by Jev and the two-threshold gate
     decides -- accept silently (>= accept threshold), surface but ask the
     user (middle band), or reject and fall through to the next source.

With Jev unconfigured the function is byte-for-byte the pre-Jev behaviour:
no translation, no HTTP calls, same candidates and ordering. A Jev error,
timeout or malformed answer is treated as "unverified" and also degrades to
the pre-existing behaviour -- it can never break a food search.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.food_search import nutrient_map, online_food_candidates, resolve_food
from drhiro_api.models import Food, Nutrient
from drhiro_api.services.food_translate import translate_food_to_english
from drhiro_api.services.jev_client import (
    DECISION_ACCEPT,
    DECISION_ASK,
    DECISION_REJECT,
    DECISION_UNVERIFIED,
    jev_decision,
)

log = logging.getLogger(__name__)


def search_food_intelligent(
    db: Session,
    query: str,
    limit: int = 5,
    user_id: str | None = None,
    use_google_fallback: bool = True,
) -> dict:
    """Search for food: DB first, then DDG fallback if 0 results.

    Returns:
        {
            "query": str,
            "source": "database" | "usda-online" | "duckduckgo" | "none",
            "candidates": [...],
            "needs_user_selection": bool,
        }

    Jev is engaged only when configured; every branch below is otherwise the
    original DB-then-DDG cascade, unchanged.
    """
    raw_query = (query or "").strip()
    jev_on = bool(get_settings().jev_api_url and get_settings().jev_api_key)

    # English-first normalisation, only when Jev is on. The returned "query"
    # and the DuckDuckGo step keep the ORIGINAL language (stored display name).
    search_query = translate_food_to_english(raw_query) if jev_on else raw_query

    # 1. Try local DB first
    result = resolve_food(db, search_query, limit=limit, user_id=user_id)

    if result:
        candidates = []
        for match in result.matches[:limit]:
            food = match.food
            nmap = _extract_nutrients_per_100g(db, food)
            candidates.append({
                "display_name": food.display_name,
                "kcal_per_100g": nmap.get("energy"),
                "protein_g_per_100g": nmap.get("protein"),
                "carbs_g_per_100g": nmap.get("carbs"),
                "fat_g_per_100g": nmap.get("fat"),
                "fiber_g_per_100g": nmap.get("fiber"),
                "sodium_mg_per_100g": nmap.get("sodium"),
                "source": "database",
                "confidence": 1.0 if match.tier == 0 else (0.8 if match.tier <= 2 else 0.6),
            })

        if not jev_on:
            return {
                "query": query,
                "source": "database",
                "candidates": candidates,
                "needs_user_selection": result.ambiguous,
            }

        decision, idx = _gate_candidates(raw_query, [c["display_name"] for c in candidates])
        if decision != DECISION_REJECT:
            return _database_response(
                query, candidates, decision, idx, fallback_ambiguous=result.ambiguous
            )
        # Every DB candidate was rejected: fall through to the online sources.

    # 2. No DB match (or Jev rejected all DB candidates).
    if jev_on:
        online = _search_online(query, raw_query, search_query, limit)
        if online is not None:
            return online

    # 3. Pre-existing DuckDuckGo fallback.
    if use_google_fallback:
        ddg_results = _ddg_nutrition(search_query if jev_on else raw_query)
        if ddg_results:
            return {
                "query": query,
                "source": "duckduckgo",
                "candidates": ddg_results,
                "needs_user_selection": True,
            }

    # 4. Nothing found
    return {
        "query": query,
        "source": "none",
        "candidates": [],
        "needs_user_selection": True,
    }


def _database_response(
    query: str, candidates: list[dict], decision: str, idx: int | None,
    fallback_ambiguous: bool,
) -> dict:
    """Shape a DB-hit response from the Jev gate decision.

    ACCEPT: the chosen candidate is surfaced first and trusted (no question).
    ASK: candidates surfaced but NOT auto-accepted -- the user is asked.
    UNVERIFIED: Jev could not be consulted; behave exactly as pre-Jev, i.e.
    ``needs_user_selection`` follows the ranker's own ambiguity verdict.
    """
    if decision == DECISION_ACCEPT and idx is not None:
        accepted = candidates[idx]
        ordered = [accepted] + [c for i, c in enumerate(candidates) if i != idx]
        return {
            "query": query,
            "source": "database",
            "candidates": ordered,
            "needs_user_selection": False,
        }
    return {
        "query": query,
        "source": "database",
        "candidates": candidates,
        "needs_user_selection": True if decision == DECISION_ASK else fallback_ambiguous,
    }


def _search_online(query: str, raw_query: str, search_query: str, limit: int) -> dict | None:
    """Try USDA then DuckDuckGo, gating each source's candidates with Jev.

    Returns a response dict, or None when no source produced a candidate the
    gate would surface (all rejected / nothing found) -- the caller then falls
    back to the pre-existing DuckDuckGo path.
    """
    for source in ("usda", "ddg"):
        try:
            cands = online_food_candidates(search_query, limit=limit, source=source)
        except Exception:
            log.exception("online lookup failed (source=%s) for %r", source, search_query)
            continue
        if not cands:
            continue
        decision, idx = _gate_candidates(raw_query, [c["display_name"] for c in cands])
        if decision == DECISION_REJECT:
            continue
        if decision == DECISION_ACCEPT and idx is not None:
            accepted = cands[idx]
            ordered = [accepted] + [c for i, c in enumerate(cands) if i != idx]
            return {
                "query": query,
                "source": accepted.get("source", source),
                "candidates": ordered,
                "needs_user_selection": False,
            }
        return {
            "query": query,
            "source": cands[0].get("source", source),
            "candidates": cands,
            "needs_user_selection": True,
        }
    return None


def _gate_candidates(raw_query: str, names: list[str]) -> tuple[str, int | None]:
    """Apply the two-threshold Jev gate to a ranked candidate name list.

    Precedence: the first ACCEPT wins; failing that, the first middle-band ASK
    candidate is returned; failing that, any UNVERIFIED makes the whole batch
    UNVERIFIED (so the caller degrades to pre-existing behaviour); otherwise
    REJECT. Returns (decision, index_of_chosen_candidate_or_None).
    """
    first_ask: int | None = None
    any_unverified = False
    for i, name in enumerate(names):
        decision, _score = jev_decision(raw_query, name)
        if decision == DECISION_ACCEPT:
            return DECISION_ACCEPT, i
        if decision == DECISION_ASK and first_ask is None:
            first_ask = i
        elif decision == DECISION_UNVERIFIED:
            any_unverified = True
    if first_ask is not None:
        return DECISION_ASK, first_ask
    if any_unverified:
        return DECISION_UNVERIFIED, None
    return DECISION_REJECT, None


def _extract_nutrients_per_100g(db: Session, food: Food) -> dict:
    """Extract per-100g nutrient values from a Food record."""
    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
    return nutrient_map(food, code_by_id)


def _ddg_nutrition(query: str) -> list[dict]:
    """DuckDuckGo nutrition fallback via the VPS-host ddg-http service.

    Env: DDG_HTTP_URL (default http://172.20.0.1:8098). The host service runs
    Camoufox egressing through the home-socks tunnel to a residential IP.
    Replaces the deprecated Pi-SSH Camoufox path (no hardcoded credentials).
    """
    import httpx as _httpx
    url = os.environ.get("DDG_HTTP_URL", "http://172.20.0.1:8098")
    try:
        log.info(f"[ddg-fallback] searching: {query}")
        r = _httpx.post(f"{url}/lookup", json={"query": query}, timeout=75)
        if r.status_code != 200:
            log.warning(f"[ddg-fallback] http {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        cands = data.get("candidates", [])
        for c in cands:
            c["display_name"] = query.strip().title()
            c.setdefault("source", "duckduckgo")
        log.info(f"[ddg-fallback] got {len(cands)} candidates")
        return cands
    except Exception as e:
        log.warning(f"[ddg-fallback] error: {e}")
        return []
