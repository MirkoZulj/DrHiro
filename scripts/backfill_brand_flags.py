"""Backfill is_generic=false for branded rows (spec Task 5).

A row is branded when its FDC record carries a brandOwner. We only touch the
`drhiro_private` rows that carry a numeric FDC external_id (the ones persisted
by _persist_food from a USDA-online lookup). DuckDuckGo/scraped rows are left
alone.

Run inside the API container:

    docker cp scripts/backfill_brand_flags.py drhiro-api-1:/tmp/
    docker exec drhiro-api-1 python /tmp/backfill_brand_flags.py

Idempotent: only rows still is_generic=true are fetched. FDC rate limit is
1000/hour; the private set is a few hundred rows, so it fits -- progress is
logged and the run resumes safely if interrupted.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from drhiro_api.db import SessionLocal
from drhiro_api.models import DataSource, Food


def _brand_owner(fdc_id: str, key: str):
    url = f"https://api.nal.usda.gov/fdc/v1/food/{fdc_id}?api_key={key}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                d = json.loads(r.read() or b"{}")
            return d.get("brandOwner"), d.get("dataType")
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return None, None
            time.sleep(2 * (attempt + 1))
    return None, None


def main() -> int:
    key = os.environ.get("USDA_API_KEY")
    if not key:
        print("USDA_API_KEY not set", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        ds = db.query(DataSource).filter(DataSource.source_key == "drhiro_private").first()
        if ds is None:
            print("no drhiro_private source")
            return 1
        rows = (db.query(Food)
                .filter(Food.data_source_id == ds.id, Food.is_generic.is_(True))
                .all())
        print(f"private rows still marked generic: {len(rows)}")
        branded = 0
        checked = 0
        for f in rows:
            ext = (f.external_id or "").strip()
            if not ext.isdigit():
                continue  # ddg: / scraped rows: not FDC ids
            checked += 1
            owner, dtype = _brand_owner(ext, key)
            if owner:
                f.is_generic = False
                branded += 1
                db.commit()
                print(f"  branded  {ext} -> {owner!r}  {f.display_name}")
            time.sleep(0.2)
        print(f"checked {checked} FDC ids; marked {branded} branded")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())