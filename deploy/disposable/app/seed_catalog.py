"""Seed the disposable stack's food catalog (disposable stack only).

Why this exists: `resolve_item_nutrition` resolves DB-first and only falls back to
external search when the local catalog has no match. Seeding a small catalog makes
resolution deterministic AND offline, so the vertical slice can exercise the REAL
resolution path over a real SQL query instead of stubbing nutrition.

The catalog mirrors the one used by the frozen candidate's vertical slice, so
totals are directly comparable. Values are per 100 g (or per 100 ml for liquids).

Idempotent: re-running does not duplicate rows.
"""
from __future__ import annotations

import os
import sys
import uuid

import psycopg2
import psycopg2.extras

# (kcal, protein_g, carbs_g, fat_g, fiber_g, sodium_mg, is_liquid)
CATALOG = {
    "steak":   (271.0, 26.0,  0.0, 18.0, 0.0,  55.0, False),
    "chicken": (165.0, 31.0,  0.0,  3.6, 0.0,  74.0, False),
    "bread":   (265.0,  9.0, 49.0,  3.2, 2.7, 490.0, False),
    "wine":    ( 83.0,  0.1,  2.6,  0.0, 0.0,   5.0, True),
    "beer":    ( 43.0,  0.5,  3.6,  0.0, 0.0,   4.0, True),
    "water":   (  0.0,  0.0,  0.0,  0.0, 0.0,   0.0, True),
}

NUTRIENTS = [
    ("energy", "Energy", "kcal"),
    ("protein", "Protein", "g"),
    ("carbs", "Carbohydrate", "g"),
    ("fat", "Fat", "g"),
    ("fiber", "Fiber", "g"),
    ("sodium", "Sodium", "mg"),
]

# Stable ids so repeated seeding is a no-op and assertions can reference them.
NS = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def sid(key: str) -> str:
    return str(uuid.uuid5(NS, key))


def main() -> int:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    with conn, conn.cursor() as cur:
        source_id = sid("datasource:disposable")
        cur.execute(
            """
            INSERT INTO data_sources (id, source_key, source_label, is_active,
                                      created_at, updated_at)
            VALUES (%s, 'disposable', 'Disposable stack catalog', true, now(), now())
            ON CONFLICT (id) DO NOTHING
            """,
            (source_id,),
        )

        nutrient_ids = {}
        for code, label, unit in NUTRIENTS:
            nid = sid(f"nutrient:{code}")
            nutrient_ids[code] = nid
            cur.execute(
                """
                INSERT INTO nutrients (id, nutrient_code, nutrient_label, unit, category,
                                       created_at)
                VALUES (%s, %s, %s, %s, 'other', now())
                ON CONFLICT (id) DO NOTHING
                """,
                (nid, code, label, unit),
            )

        for name, (kcal, prot, carbs, fat, fiber, sodium, liquid) in CATALOG.items():
            fid = sid(f"food:{name}")
            cur.execute(
                """
                INSERT INTO foods (id, data_source_id, external_id, display_name,
                                   is_generic, is_liquid, serving_grams, serving_unit,
                                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, true, %s, 100, %s, now(), now())
                ON CONFLICT (id) DO NOTHING
                """,
                (fid, source_id, f"disp-{name}", name.capitalize(), liquid,
                 "ml" if liquid else "g"),
            )
            for code, amount in [
                ("energy", kcal), ("protein", prot), ("carbs", carbs),
                ("fat", fat), ("fiber", fiber), ("sodium", sodium),
            ]:
                cur.execute(
                    """
                    INSERT INTO food_nutrients (id, food_id, nutrient_id,
                                                amount_per_100g)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (sid(f"fn:{name}:{code}"), fid, nutrient_ids[code], amount),
                )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM foods")
        foods = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM food_nutrients")
        fns = cur.fetchone()[0]
    conn.close()

    print({"ok": True, "foods": foods, "food_nutrients": fns,
           "catalog": sorted(CATALOG)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
