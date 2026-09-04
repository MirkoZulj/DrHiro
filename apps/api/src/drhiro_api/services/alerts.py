"""Alert service: recompute deterministic rule alerts after ingestion.

The worker calls recompute_alerts_for_user() whenever new measurements
arrive. Rule output is stored as Alert rows; the LLM may explain an
alert but never invent, suppress, or change it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from drhiro_api.models import Alert, Measurement
from drhiro_rules.engine import evaluate_rules, serialize_candidate


def recompute_alerts_for_user(db: Session, user_id: uuid.UUID) -> list[Alert]:
    """Evaluate all rules for the user's recent measurements and persist
    new open alerts. Returns the newly created alerts."""
    cutoff_days = 90
    measurements = (
        db.query(Measurement)
        .filter(Measurement.user_id == user_id)
        .order_by(Measurement.start_at.desc())
        .limit(2000)
        .all()
    )
    rows = [
        {
            "id": str(m.id),
            "metric_type": m.metric_type,
            "value_json": m.value_json,
            "start_at": m.start_at,
            "source_provider": m.source_provider,
        }
        for m in measurements
    ]
    candidates = evaluate_rules(rows)
    created = []
    for cand in candidates:
        existing = (
            db.query(Alert)
            .filter(
                Alert.user_id == user_id,
                Alert.rule_code == cand.rule_code,
                Alert.rule_version == cand.rule_version,
                Alert.status == "open",
            )
            .first()
        )
        if existing:
            continue
        alert = Alert(
            user_id=user_id,
            rule_code=cand.rule_code,
            rule_version=cand.rule_version,
            trigger_record_ids=cand.trigger_record_ids,
            severity=cand.severity,
            status="open",
            explanation_template=cand.explanation_template,
            params_json=cand.params,
        )
        db.add(alert)
        created.append(alert)
    if created:
        db.commit()
    return created
