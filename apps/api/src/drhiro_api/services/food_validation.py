"""LLM validation gate for parsed meal items.

Before committing a meal log, the parser output (food names + gram amounts)
is sent to the LLM for a sanity check. The LLM is asked to verify that each
parsed item actually matches what the user meant in the original text.

This is NOT a replacement for the parser — it is a safety net. The parser
still does the heavy lifting (extracting quantities, splitting fragments);
the LLM just sanity-checks the food-name resolution.

If the LLM finds a mismatch, it returns the corrected food name. The
corrected version is used instead of the deterministic match.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from .llm_client import chat_complete_sync

log = logging.getLogger(__name__)


class FoodMatchValidation(BaseModel):
    """LLM verdict on one parsed food item."""
    user_said: str
    matched_food: str
    is_correct: bool
    suggested_food: str | None = None
    confidence: float = 0.0
    reason: str | None = None


class ValidationResult(BaseModel):
    """Overall verdict for a parsed meal."""
    items: list[FoodMatchValidation]
    all_correct: bool
    needs_review: bool
    reasoning: str | None = None


def validate_parsed_meal(
    original_text: str,
    parsed_items: list[dict],
    meal_type: str = "meal",
) -> ValidationResult | None:
    """Send parser output to LLM for food-match sanity check.

    Each parsed item has at least:
      - display_name: the food the parser resolved
      - grams: the parsed weight in grams
      - original_text: the fragment of original text that produced this item

    Returns None if validation is skipped (no LLM configured or on error),
    so logging is never blocked by the validation gate.
    """
    if not parsed_items:
        return None

    item_lines = []
    for i, item in enumerate(parsed_items, 1):
        original = item.get("original_text") or item.get("fragment") or ""
        matched = item.get("display_name") or item.get("matched_name") or ""
        grams = item.get("grams") or item.get("quantity") or 0
        item_lines.append(
            f"{i}. User said: {original!r} -> Matched: {matched!r} ({grams}g)"
        )

    prompt = (
        'You are a food-logging validation assistant. A user wrote this text '
        f'about their {meal_type}:\n\n"{original_text}"\n\n'
        "Their text was parsed into these food items:\n\n"
        + "\n".join(item_lines)
        + "\n\nFor each item, verify:\n"
        "1. Does the matched food name accurately represent what the user meant?\n"
        '   - "chicken" should be chicken breast/meat, NOT chicken fat\n'
        '   - "sweet potato" should be sweet potato root, NOT sweet potato leaves\n'
        '   - "grana padano" should be the cheese, NOT a spirit or unrelated food\n'
        "2. Is the quantity (grams) reasonable for that food?\n"
        "3. Is there anything spurious that should not be there?\n\n"
        'Respond with ONLY valid JSON:\n'
        '{\n'
        '  "items": [\n'
        '    {\n'
        '      "user_said": "the original phrase",\n'
        '      "matched_food": "what the parser matched",\n'
        '      "is_correct": true or false,\n'
        '      "suggested_food": "correct food name if wrong, null if correct",\n'
        '      "confidence": 0.0-1.0,\n'
        '      "reason": "brief explanation"\n'
        "    }\n"
        "  ],\n"
        '  "all_correct": true or false,\n'
        '  "needs_review": true or false,\n'
        '  "reasoning": "overall assessment"\n'
        "}"
    )

    try:
        raw = chat_complete_sync(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        data = json.loads(raw)
        return ValidationResult(**data)
    except Exception as exc:
        log.warning("LLM validation failed, using parser output as-is: %s", exc)
        return None


def apply_validations(
    parsed_items: list[dict],
    validation: ValidationResult,
) -> list[dict]:
    """Apply LLM corrections to parsed items in-place."""
    for item, verdict in zip(parsed_items, validation.items, strict=True):
        if verdict.is_correct:
            continue
        if verdict.suggested_food and verdict.confidence >= 0.7:
            log.info(
                "Validation correction: %r -> %r (was %r, confidence %.2f)",
                item.get("original_text", ""),
                verdict.suggested_food,
                item.get("display_name", ""),
                verdict.confidence,
            )
            item["display_name"] = verdict.suggested_food
            item.pop("food_id", None)
            item.pop("nutrients", None)
            item["validated_by"] = "llm"
    return parsed_items
