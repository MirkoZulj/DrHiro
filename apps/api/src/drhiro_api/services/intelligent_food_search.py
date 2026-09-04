"""Intelligent food search with a DuckDuckGo fallback.

When the local DB has no match, queries a host-side ddg-http service
(env DDG_HTTP_URL) that runs Camoufox egressing through a residential
tunnel, and extracts nutrition from the search results. All configuration
is environment-driven; no hardcoded hosts or credentials.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from drhiro_api.food_search import resolve_food, nutrient_map
from drhiro_api.models import Food, Nutrient

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
            "source": "database" | "duckduckgo" | "none",
            "candidates": [...],
            "needs_user_selection": bool,
        }
    """
    # 1. Try local DB first
    result = resolve_food(db, query, limit=limit, user_id=user_id)
    
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
        
        return {
            "query": query,
            "source": "database",
            "candidates": candidates,
            "needs_user_selection": result.ambiguous,
        }
    
    # 2. No DB match — try DDG via the host fallback service
    if use_google_fallback:
        ddg_results = _ddg_nutrition(query)
        if ddg_results:
            return {
                "query": query,
                "source": "duckduckgo",
                "candidates": ddg_results,
                "needs_user_selection": True,
            }
    
    # 3. Nothing found
    return {
        "query": query,
        "source": "none",
        "candidates": [],
        "needs_user_selection": True,
    }


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
