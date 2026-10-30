"""Intelligent food search with Google fallback via Camoufox.

When the local DB has no match, uses camoufox browser on the Pi to Google
"<food> nutritional information" and extract nutrition from the search results.
"""
from __future__ import annotations

import json
import logging
import subprocess
import os
import urllib.parse

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
    """Search for food: DB first, then Google fallback if 0 results.
    
    Returns:
        {
            "query": str,
            "source": "database" | "google" | "none",
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
    
    # 2. No DB match — try Google via camoufox
    if use_google_fallback:
        google_results = _google_nutrition(query)
        if google_results:
            return {
                "query": query,
                "source": "google",
                "candidates": google_results,
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


def _google_nutrition(query: str) -> list[dict]:
    """Use camoufox browser on Pi to Google for nutrition info."""
    try:
        search_query = f"{query} nutritional information"
        encoded = urllib.parse.quote(search_query)
        
        result = subprocess.run(
            [
                "sshpass", "-p", os.environ.get("DRHIRO_CAMOUFOX_SSH_PASSWORD", ""), "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=30",
                os.environ.get("DRHIRO_CAMOUFOX_SSH_HOST", ""),
                f"python3 /home/mirko/camoufox_nutrition.py '{encoded}'"
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        
        if result.returncode != 0:
            log.warning(f"Camoufox search failed: {result.stderr[:500]}")
            return []
        
        data = json.loads(result.stdout)
        return data.get("candidates", [])
    
    except subprocess.TimeoutExpired:
        log.warning("Camoufox search timed out")
        return []
    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse camoufox output: {e}")
        return []
    except Exception as e:
        log.warning(f"Camoufox search error: {e}")
        return []
