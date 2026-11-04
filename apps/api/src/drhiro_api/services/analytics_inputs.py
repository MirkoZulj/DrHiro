"""Analytics inputs assembled from the DB (0.9.0).

Lives in the API package (not the worker) so both the API process (agent
tools) and the worker job import the SAME timeline/context builders — one
definition, no drift. Pure functions over a Session.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from drhiro_api.models import MedicationItem, MedicationItemEvent, User
from drhiro_analytics.types import TimelineEvent

ANALYSIS_TABLES = [
    "measurements", "meals", "activities", "medication_items", "dose_events", "medical_encounters",
]

_KIND_MAP = {
    "started": "med_started", "stopped": "med_stopped", "paused": "med_paused",
    "resumed": "med_resumed", "dose_changed": "med_dose_changed", "schedule_changed": "med_dose_changed",
}


def med_timeline(db: Session, user, start: date, end: date) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    items = db.query(MedicationItem).filter(MedicationItem.user_id == user.id).all()
    by_id = {i.id: i for i in items}
    evs = (
        db.query(MedicationItemEvent)
        .filter(
            MedicationItemEvent.user_id == user.id,
            MedicationItemEvent.effective_date >= start,
            MedicationItemEvent.effective_date <= end,
            MedicationItemEvent.kind != "edited",
        )
        .order_by(MedicationItemEvent.effective_date)
        .all()
    )
    for ev in evs:
        item = by_id.get(ev.item_id)
        if not item:
            continue
        label = f"{item.name}"
        if item.dose_amount:
            label += f" {item.dose_amount:g} {item.dose_unit or ''}".rstrip()
        events.append(TimelineEvent(day=ev.effective_date, kind=_KIND_MAP.get(ev.kind, ev.kind),
                                    label=label, ref_id=str(ev.id)))
    return events


def encounter_timeline(db: Session, user, start: date, end: date) -> list[TimelineEvent]:
    from drhiro_api.models import MedicalEncounter

    encs = (
        db.query(MedicalEncounter)
        .filter(MedicalEncounter.user_id == user.id,
                MedicalEncounter.encounter_date >= start,
                MedicalEncounter.encounter_date <= end)
        .order_by(MedicalEncounter.encounter_date)
        .all()
    )
    return [
        TimelineEvent(day=e.encounter_date, kind="encounter",
                      label=f"Encounter: {e.facility or ''}{' – ' if e.facility and e.title else ''}{e.title or ''}".strip(),
                      ref_id=str(e.id))
        for e in encs
    ]


def unknowns_context(db: Session, user, period_start: date, period_end: date) -> dict:
    """Counts for the unknowns builder (spec §3.5 context). D2: drafts count."""
    from drhiro_api.models import Meal

    context: dict = {}
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(period_end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    context["drafts_pending"] = (
        db.query(Meal)
        .filter(Meal.user_id == user.id, Meal.status.in_(["draft", "needs_review"]),
                Meal.eaten_at >= start_dt, Meal.eaten_at < end_dt)
        .count()
    )
    context["doses_not_logged"] = _doses_not_logged(db, user, period_start, period_end)
    return context


def _doses_not_logged(db: Session, user, period_start: date, period_end: date) -> list[str]:
    from drhiro_api.services import medications as medsvc

    out = []
    items = db.query(MedicationItem).filter(
        MedicationItem.user_id == user.id, MedicationItem.status == "active"
    ).all()
    for item in items:
        a = medsvc.adherence(db, user, item, period_start, period_end)
        if a["expected"] > 0 and (a["taken"] + a["skipped"]) == 0:
            out.append(item.name)
    return out


def data_fingerprint(db: Session, user_id, baseline_start: date, period_end: date) -> str:
    """sha256 over (table, count(*), max(updated_at)) for the analysis tables."""
    parts = {}
    for table in ANALYSIS_TABLES:
        try:
            row = db.execute(
                text(f"SELECT count(*), max(updated_at) FROM {table} WHERE user_id = :uid"),
                {"uid": str(user_id)},
            ).fetchone()
            parts[table] = [row[0], row[1].isoformat() if row[1] else None]
        except Exception:
            parts[table] = [None, None]
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
