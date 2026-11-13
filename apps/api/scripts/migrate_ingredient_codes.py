#!/usr/bin/env python3
"""Legacy ingredient-code suggester (0.12.0 Part B, spec §4.4).

--dry-run (default): print proposed nutrient_code/mapping table. No writes.
--apply: writes mapping='suggested' ONLY with --i-have-owner-review.
Without owner review, --apply refuses. Suggested entries are counted in
intake but flagged until confirmed.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
API_SRC = SCRIPT_DIR.parent / "src"
if str(API_SRC) not in sys.path:
    sys.path.insert(0, str(API_SRC))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-have-owner-review", action="store_true")
    args = ap.parse_args()
    if args.apply and not args.i_have_owner_review:
        print("REFUSED: --apply requires --i-have-owner-review (owner must review the dry-run table first).")
        return 2
    os.environ.setdefault("DRHIRO_DATABASE_URL", "postgresql+psycopg://drhiro:drhiro@localhost:5435/drhiro")
    from drhiro_api.db import SessionLocal
    from drhiro_api.models import MedicationItem
    from drhiro_api.services.schemas_meds import suggest_nutrient_code
    db = SessionLocal()
    try:
        rows = []
        for item in db.query(MedicationItem).all():
            for e in item.ingredients_json or []:
                name = str(e.get("name") or e.get("component") or e.get("ingredient") or "") if isinstance(e, dict) else str(e)
                if isinstance(e, dict) and e.get("nutrient_code") is not None:
                    continue
                code = suggest_nutrient_code(name)
                rows.append((item.name, name, code))
                if args.apply:
                    e["nutrient_code"] = code
                    e["mapping"] = "suggested" if code else "none"
            if args.apply:
                db.add(item)
        print(f"{'item':<30} {'ingredient':<40} {'proposed code'}")
        for item_name, name, code in rows:
            print(f"{item_name[:29]:<30} {name[:39]:<40} {code or '(none)'}")
        print(f"\n{len(rows)} legacy entries scanned; {sum(1 for r in rows if r[2])} with a suggested code.")
        if args.apply:
            db.commit()
            print("Applied mapping='suggested' (owner review confirmed).")
        else:
            print("Dry run only — no writes.")
        return 0
    finally:
        db.close()

if __name__ == "__main__":
    raise SystemExit(main())
