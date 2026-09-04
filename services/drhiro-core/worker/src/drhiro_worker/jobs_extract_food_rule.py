"""RQ job: extract a reusable food-resolution rule after a user correction.

Enqueued from the API's PATCH /meals/{id}/items/{item_id} handler via
drhiro_api.services.task_queue (referenced by dotted path — the API process
never imports the worker package). Consumed by the `drhiro` RQ queue.
"""

from __future__ import annotations

import uuid

from drhiro_api.db import SessionLocal


def extract_food_rule_job(
    user_id: str,
    original_text: str,
    corrected_name: str,
    corrected_food_id: str | None = None,
) -> dict:
    """Worker entry point. Opens its own DB session and runs the extractor."""
    from drhiro_api.services.food_rule_extractor import extract_rule

    db = SessionLocal()
    try:
        rule_id = extract_rule(
            db,
            uuid.UUID(user_id),
            original_text,
            corrected_name,
            uuid.UUID(corrected_food_id) if corrected_food_id else None,
        )
        return {
            "rule_id": str(rule_id) if rule_id else None,
            "ok": rule_id is not None,
        }
    finally:
        db.close()
