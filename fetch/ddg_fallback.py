#!/usr/bin/env python3
"""ddg_fallback.py — VPS-side DuckDuckGo nutrition fallback, egress via the
home-socks tunnel (127.0.0.1:1080 -> Pi residential). Replicates the Pi's
proven camoufox_nutrition.py behaviour (DuckDuckGo html + text parsing) but
runs entirely on the VPS, so the Pi is never a dependency.

USDA is the primary food source; this is the fallback for foods USDA lacks.

CLI:
  python ddg_fallback.py "sušena vratina"      # JSON nutrition candidates
Importable:
  from ddg_fallback import search_nutrition; search_nutrition("...")
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import quote_plus

PROXY = os.environ.get("HOME_SOCKS_PROXY", "socks5://127.0.0.1:1080")
DDG_URL = "https://html.duckduckgo.com/html/"


def _parse_nutrition_text(text: str, query: str) -> dict | None:
    """Mirror the Pi script's parser: pull kcal/protein/carbs/fat from text."""
    t = text.lower()
    kcal = re.search(r"(\d+(?:\.\d+)?)\s*(?:kcal|calories|cal)\b", t)
    prot = re.search(r"protein[:\s]*(\d+(?:\.\d+)?)\s*g", t)
    carbs = re.search(r"(?:carbs|carbohydrates)[:\s]*(\d+(?:\.\d+)?)\s*g", t)
    fat = re.search(r"fat[:\s]*(\d+(?:\.\d+)?)\s*g", t)
    if not kcal and not prot:
        return None
    return {
        "display_name": query.strip().title(),
        "kcal_per_100g": float(kcal.group(1)) if kcal else None,
        "protein_g_per_100g": float(prot.group(1)) if prot else None,
        "carbs_g_per_100g": float(carbs.group(1)) if carbs else None,
        "fat_g_per_100g": float(fat.group(1)) if fat else None,
        "source": "duckduckgo",
        "confidence": 0.5,
    }


def search_nutrition(query: str) -> dict:
    """Search DDG through the tunnel, parse visible text for nutrition values."""
    from camoufox.sync_api import Camoufox
    candidates = []
    with Camoufox(headless=True, humanize=True, timeout=60000) as browser:
        ctx = browser.new_context(proxy={"server": PROXY})
        page = ctx.new_page()
        url = DDG_URL + "?q=" + quote_plus(query)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        if any(x in body.lower() for x in ("anomaly", "captcha", "blocked")):
            ctx.close()
            return {"candidates": [], "error": "blocked"}
        # Parse each .result block text
        results = page.query_selector_all(".result")
        for r in results[:8]:
            txt = r.inner_text()
            parsed = _parse_nutrition_text(txt, query)
            if parsed:
                candidates.append(parsed)
        ctx.close()
    return {"candidates": candidates}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python ddg_fallback.py 'food name'", file=sys.stderr)
        return 1
    q = " ".join(sys.argv[1:])
    print(json.dumps(search_nutrition(q), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
