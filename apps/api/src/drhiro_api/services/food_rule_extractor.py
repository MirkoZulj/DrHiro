"""LLM-powered extraction of reusable food-resolution rules.

When a user corrects a meal item ("a glass of wine" resolved to vinegar →
user renames it), we ask the LLM for ONE short general rule and store it so
future identical queries resolve correctly without another correction.

Prompts are deliberately SHORT — Qwen-class models degrade quickly on long
instruction blocks.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from drhiro_api.models import FoodResolutionRule

log = logging.getLogger(__name__)

SYSTEM = (
    "You turn a user's food-name correction into a general matching rule. "
    "Reply ONLY with JSON."
)

PROMPT = (
    'User logged the food text: "{text}"\n'
    'It was corrected to: "{corrected}"\n'
    "Write a general rule so future queries containing the same ambiguous "
    "word resolve to this kind of food.\n"
    'Reply only: {{"rule": "<one sentence>", "pattern": "<trigger words>", "scope": "user"}}'
)


def extract_rule(
    db: Session,
    user_id: uuid.UUID,
    original_text: str,
    corrected_name: str,
    corrected_food_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Ask the LLM for a rule, persist it, return its id (None on failure)."""
    from drhiro_api.services.llm_client import chat_complete_sync

    try:
        raw = chat_complete_sync(
            [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": PROMPT.format(
                        text=(original_text or "")[:200],
                        corrected=(corrected_name or "")[:100],
                    ),
                },
            ]
        )
    except Exception:
        log.exception("LLM rule extraction failed")
        return None

    parsed = _parse_json(raw)
    if not parsed or not parsed.get("pattern") or not parsed.get("rule"):
        log.warning("unusable LLM rule response: %r", raw[:300])
        return None

    scope = parsed.get("scope") or "user"
    if scope not in ("user", "global"):
        scope = "user"

    # corrected_food_id may be a MealItem.catalog reference rather than a
    # foods.id — fall back to an exact-name match on the corrected name.
    from drhiro_api.models import Food

    food_uuid = None
    for candidate in (corrected_food_id, None):
        if candidate:
            try:
                food_uuid = uuid.UUID(str(candidate))
            except ValueError:
                food_uuid = None
            if food_uuid is not None:
                if db.get(Food, food_uuid) is None:
                    food_uuid = None
                break
    if food_uuid is None:
        row = (
            db.query(Food)
            .filter(func.lower(Food.display_name) == (corrected_name or "").lower())
            .first()
        )
        food_uuid = row.id if row else None

    rule = FoodResolutionRule(
        user_id=user_id,
        original_pattern=parsed["pattern"].strip().lower()[:200],
        resolved_food_id=food_uuid,
        rule_text=str(parsed["rule"])[:500],
        scope=scope,
        active=True,
    )
    db.add(rule)
    db.commit()
    return rule.id


def _parse_json(raw: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply (handles ``` fences)."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
