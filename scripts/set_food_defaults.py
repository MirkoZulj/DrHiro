"""Curated default variant for bare generic food words (drhiro-api 0.1.19).

The ranker cannot know which variant of a food is "plain" for a bare word:
"Alcoholic beverage, beer, light" and "...beer, regular, all" are both head
matches for "beer", and nothing in the name says which one a user means. This
script records that human choice as data -- `foods.is_default = true` on
exactly the curated rows below -- and `resolve_food()` prefers those rows
ahead of the variant/token tie-breaks (but behind an exact-name match).

It is deliberately ONE documented, re-runnable script rather than scattered
SQL, so the default set can be reviewed and changed in a single place. It is
data-source-independent: every row whose display_name matches a curated name
exactly is flagged (the same food can exist under `usda` and
`drhiro_private`; both should carry the flag so de-duplication keeps a
defaulted row).

Run inside the API container (it has the DB URL):

    docker cp scripts/set_food_defaults.py drhiro-api-1:/tmp/
    docker exec drhiro-api-1 python /tmp/set_food_defaults.py

Idempotent: it clears is_default on every row first, then sets it on the
listed names, so the script fully owns the flag and a re-run cannot leave a
stale default behind. Names must be the NBSP-FOLDED display names (plain
spaces) -- run the NBSP normalisation before this.

NOT to be modelled with food_resolution_rules: those patterns are
substring-matched, so a pattern "pepper" would also hijack "pepperoni".
"""

from __future__ import annotations

import sys

from drhiro_api.db import SessionLocal
from drhiro_api.models import Food

# The curated plain defaults. Exactly these names, nothing else.
DEFAULTS = [
    "Alcoholic beverage, beer, regular, all",
    "Peppers, sweet, green, raw",
    "Bananas, raw",
    "Bacon, cooked",
    "Water, tap, drinking",
    "Ice creams, vanilla",
    "Espresso coffee, brewed",
    "Peaches, yellow, raw",
    "Chicken breast, raw",
    "Honey",
    "Salt, table",
    "Milk, whole",
    "Beef, tenderloin steak, raw",
]


def main() -> int:
    db = SessionLocal()
    try:
        cleared = (
            db.query(Food)
            .filter(Food.is_default.is_(True))
            .update({Food.is_default: None}, synchronize_session=False)
        )
        total = 0
        missing = []
        for name in DEFAULTS:
            rows = db.query(Food).filter(Food.display_name == name).all()
            if not rows:
                missing.append(name)
                continue
            for f in rows:
                f.is_default = True
                total += 1
        db.commit()

        print(f"cleared previous defaults: {cleared}")
        print(f"{'n':>2}  name")
        for name in DEFAULTS:
            n = (
                db.query(Food)
                .filter(Food.display_name == name, Food.is_default.is_(True))
                .count()
            )
            print(f"{n:>2}  {name}")
        print(
            f"rows set is_default=true: {total} "
            f"over {len(DEFAULTS) - len(missing)}/{len(DEFAULTS)} distinct names"
        )
        if missing:
            print("MISSING (name not in catalogue):", missing, file=sys.stderr)
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())