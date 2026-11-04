"""Deterministic medication rules (0.9.0, spec §5.4).

The ONLY medication safety check in v1: duplicate ingredients across active
items. No upper-limit judgement, no interactions — those need a reviewed
source and owner sign-off as a new rule version. Separate entry point
``evaluate_medication_rules`` so the measurement path is untouched.
"""

from __future__ import annotations

from collections import defaultdict

from drhiro_rules.engine import AlertCandidate, register_rule

# normalise ingredient names: lower, strip, synonyms map
SYNONYMS = {
    "vitamin d3": "cholecalciferol",
    "vitamin d": "cholecalciferol",
    "cholecalciferol": "cholecalciferol",
    "magnezij": "magnesium",
    "magnesium": "magnesium",
    "mg": "magnesium",
    "vitamin c": "ascorbic acid",
    "ascorbic acid": "ascorbic acid",
    "vitamin b12": "cobalamin",
    "cobalamin": "cobalamin",
    "methylcobalamin": "cobalamin",
    "omega 3": "omega-3",
    "omega-3": "omega-3",
    "epa/dha": "omega-3",
    "iron": "iron",
    "željezo": "iron",
    "zinc": "zinc",
    "cink": "zinc",
}


def normalise_ingredient(name: str) -> str:
    key = (name or "").strip().lower()
    return SYNONYMS.get(key, key)


@register_rule(
    code="MED_DUPLICATE_INGREDIENT",
    version=1,
    jurisdiction="global",
    severity="info",
    description="Two or more active items list the same ingredient.",
)
def duplicate_ingredient(items: list[dict], ctx: dict) -> list[AlertCandidate] | None:
    """items: [{...item dict with name, ingredients_json}] — ACTIVE items only.
    Emits one candidate per ingredient present in >= 2 active items."""
    by_ingredient: dict[str, list[dict]] = defaultdict(list)
    for it in items or []:
        for ing in it.get("ingredients_json") or []:
            if not isinstance(ing, dict):
                continue
            canon = normalise_ingredient(str(ing.get("name", "")))
            if not canon:
                continue
            by_ingredient[canon].append(
                {
                    "item_id": str(it.get("id")),
                    "name": it.get("name"),
                    "amount": ing.get("amount"),
                    "unit": ing.get("unit"),
                }
            )
    candidates = []
    for canon, entries in sorted(by_ingredient.items()):
        if len(entries) < 2:
            continue
        units = {e.get("unit") for e in entries if e.get("unit") is not None}
        unit = next(iter(units)) if len(units) == 1 else ""
        total = sum(e["amount"] for e in entries if isinstance(e.get("amount"), (int, float)))
        names = ", ".join(str(e.get("name")) for e in entries)
        candidates.append(
            AlertCandidate(
                rule_code="MED_DUPLICATE_INGREDIENT",
                rule_version=1,
                severity="info",
                trigger_record_ids=[e["item_id"] for e in entries],
                explanation_template=(
                    f"{canon} appears in: {names}. Total listed per day: {total if total else 'sum of listed amounts'}"
                    + (f" {unit}" if unit else "") + "."
                ),
                params={"ingredient": canon, "entries": entries},
            )
        )
    return candidates or None


def evaluate_medication_rules(items: list[dict], jurisdiction: str = "global") -> list:
    """Entry point for the medication rule path (measurements path untouched)."""
    return duplicate_ingredient(items, {}) or []
